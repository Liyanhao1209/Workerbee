"""Kimi Code 适配器（HAR-01–03、§8.1 六组契约、D-09）。

无头模式实测命令行（kimi 2.1.1）::

    kimi -p "<prompt>" --output-format stream-json \
         [-m <model>] [--add-dir <dir>] [--agent <name>] \
         [-S <session_id> | -c]

stdout 是 NDJSON，已实测到的行类型（``role`` 字段）：

- ``{"role":"meta","type":"system.version","version":"2.1.1"}`` —— 开场；
- ``{"role":"assistant","content":"文本"}`` —— 助手输出；
- ``{"role":"assistant","tool_calls":[{"id":..,"function":{"name":..,"arguments":"<json>"}}]}``；
- ``{"role":"tool","tool_call_id":"..","content":".."}`` —— 工具结果；
- ``{"role":"meta","type":"session.resume_hint","session_id":"session_<uuid>",...}``
  —— 一轮结束时给出会话 id 与恢复命令（``kimi -r <id>``）。

**实测发现的三个限制**（都直接影响能力声明）：

1. ``-p`` 与 ``-y/--yolo``、``--auto``、``--plan`` **互斥**（CLI 直接报
   ``Cannot combine --prompt with --auto``）。也就是说非交互模式下没有任何
   权限策略开关可用，``permission_mode`` 只接受默认值，其余如实返回 NOT_SUPPORTED。
2. 流里会混进非 JSON 行（工具子进程的 stdout，实测看到裸 ``DONE``）。
   适配器容忍并如实上报。
3. 会话 id **只在轮次结束时**才出现，创建时无法指定（没有 ``--session-id``）。
   因此若会话在一轮结束前被杀，拿不到 persist_locator，无法 resume——
   这一点如实写进 manifest.notes，不假装可恢复。

故 ``token_usage=False``（流里没有 usage/费用信息）、``compact=False``、
``permission_hook=False``、``background_tasks=False``、
``interact/interrupt=False``（``-p`` 没有输入通道；``kimi acp`` 是另一套
ACP 协议，本适配器未实现，也不据此声明能力）。

权限模式（HUM-03）：``-p`` 下唯一可选的取值是「不给任何开关」，实测它既不会
向用户提问、也不会因为权限受阻（见 ``NON_INTERACTIVE_PERMISSION_MODES``）；
用户选了它就等于把权限决定交给 harness 自己，校验管线会给出对应告警。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any

from ..sdk.cli import (
    CliHarnessAdapter,
    CliSession,
    find_executable,
    run_version_probe,
)
from ..sdk.contract import (
    AdapterCapabilities,
    AdapterManifest,
    CreateSessionRequest,
)
from ..sdk.protocol import (
    PROTOCOL_VERSION,
    AdapterError,
    ErrorCode,
    EventKind,
    NOTIFICATIONS,
)

__all__ = [
    "KimiCodeAdapter",
    "SUPPORTED_PERMISSION_MODES",
    "NON_INTERACTIVE_PERMISSION_MODES",
]

ADAPTER_ID = "kimi-code"
HARNESS_FAMILY = "kimi-code"
ADAPTER_VERSION = "0.1.0"

SUPPORTED_HARNESS_MAJOR = "2"

#: 非交互 prompt 模式下唯一可用的权限策略：不给任何开关（kimi 的 -y/--auto/--plan
#: 都与 -p 互斥）。列在这里是为了让「不支持」这件事有明确的可读原因。
SUPPORTED_PERMISSION_MODES = ("default",)

#: 其中不会向用户请求授权的取值。
#:
#: ``-p`` 模式没有可提问的通道，实测（cd 到临时目录后让它用 Bash 执行 rm）：
#: 那条删除**被直接执行**，全程没有任何询问，也没有等 stdin。本机 --help 也写明
#: ``-y/--yolo`` 是「Ask When Needed：risky actions… still ask」、
#: ``--auto`` 是「Never Ask」——两者都与 ``-p`` 互斥（CLI 直接报
#: ``Cannot combine --prompt with --auto``），因此 ``-p`` 下不存在会提问的取值。
#: 注意这只说明「不会停下来等用户」，不说明权限被框架拦住了：框架没有钩子，
#: 权限决定完全由 harness 自己做（HUM-03 的告警会如实讲清这一点）。
NON_INTERACTIVE_PERMISSION_MODES = ("default",)


class KimiCodeAdapter(CliHarnessAdapter):
    """Kimi Code 的无头（``-p``）适配器。"""

    exec_default = "kimi"
    turn_end_on_exit = True
    """kimi 的流里没有显式的轮次结束标记：``-p`` 跑完一轮就退出，
    因此 TURN_END 只能由进程退出来推断（如实标注 implied_by=process_exit）。"""

    def __init__(self) -> None:
        super().__init__()
        self.input_channel = False
        self.manifest = _build_manifest()
        #: session_ref → 本会话专属的 skills 临时目录（由 extra["skills"] 落成），
        #: 会话结束时尽力清理。
        self._session_tmpdirs: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 探测 / 兼容性
    # ------------------------------------------------------------------

    async def on_probe(self, params: dict) -> dict:
        exec_path = params.get("exec_path") or (
            params.get("harness") or {}
        ).get("exec_path")
        try:
            exe = find_executable(exec_path, self.exec_default)
        except AdapterError as exc:
            return {
                "ok": False,
                "adapter_id": ADAPTER_ID,
                "harness_family": HARNESS_FAMILY,
                "error": exc.message,
                "capabilities": self.manifest.capabilities.model_dump(mode="json"),
            }
        ok, output = await run_version_probe([exe, "--version"])
        return {
            "ok": ok,
            "adapter_id": ADAPTER_ID,
            "harness_family": HARNESS_FAMILY,
            "exec_path": exe,
            "harness_version": _parse_version(output) if ok else None,
            "raw": output[:200],
            "error": None if ok else output,
            "capabilities": self.manifest.capabilities.model_dump(mode="json"),
        }

    async def on_compat_check(self, params: dict) -> dict:
        exec_path = params.get("exec_path")
        try:
            exe = find_executable(exec_path, self.exec_default)
        except AdapterError as exc:
            return {
                "compatible": None,
                "harness_version": None,
                "notes": [exc.message],
                "verified": False,
            }
        ok, output = await run_version_probe([exe, "--version"])
        if not ok:
            return {
                "compatible": None,
                "harness_version": None,
                "notes": [f"无法确认 harness 版本：{output}"],
                "verified": False,
            }
        version = _parse_version(output)
        major = (version or "").split(".")[0]
        compatible = major == SUPPORTED_HARNESS_MAJOR
        notes = []
        if not compatible:
            notes.append(
                f"实测版本 {version} 不在声明支持的 {SUPPORTED_HARNESS_MAJOR}.x 区间内；"
                f"kimi 的 stream-json 行格式属于未公开契约，升级后需重新验证"
            )
        return {
            "compatible": compatible,
            "harness_version": version,
            "exec_path": exe,
            "notes": notes,
            "verified": True,
        }

    # ------------------------------------------------------------------
    # 启动参数
    # ------------------------------------------------------------------

    def build_argv(
        self,
        *,
        request: CreateSessionRequest,
        prompt: str | None,
        resume_locator: str | None,
        checkpoint: Any,
    ) -> list[str]:
        # 同 claude 适配器：这里只取名字，不查存在性。存在性在 create_session
        # 拉起进程前统一解析，否则测 argv 形状的用例会平白依赖本机装了什么。
        exe = request.harness.exec_path or self.exec_default
        opts = dict(request.harness.adapter_options or {})
        opts.update(request.extra.get("options") or {})

        if prompt is None:
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                "kimi 的 -p 模式需要 prompt（放在 extra.prompt）；"
                "该模式没有运行中的输入通道，无法先开会话再喂输入。",
            )

        effective_prompt = prompt
        if request.system_prompt:
            # kimi 的 prompt 模式没有 system prompt 参数。把用户给定的系统提示
            # 以显式分隔的方式前置到 prompt，而不是静默丢弃（CFG-06 的约束：
            # system_prompt 留空也能跑，填了就必须生效）。这是**有损降级**，
            # 已在 manifest.notes 里如实标注。
            effective_prompt = f"{request.system_prompt}\n\n---\n\n{prompt}"

        argv = [
            exe,
            "-p",
            effective_prompt,
            "--output-format",
            str(opts.get("output_format") or "stream-json"),
        ]

        model = (request.model_name or "").strip()
        if model:
            argv += ["-m", model]

        mode = (request.permission_mode or "").strip()
        if mode and mode not in SUPPORTED_PERMISSION_MODES:
            # -y / --auto / --plan 与 -p 互斥（实测），因此这里没有可映射的取值。
            # 如实拒绝，不静默替换成「默认」——那会让用户以为策略生效了。
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                f"Kimi Code 的 -p 非交互模式不支持 permission_mode={mode!r}："
                f"-y/--auto/--plan 与 -p 互斥，该模式没有任何权限策略开关。"
                f"需要非默认策略时请改用其他 harness 或以交互模式运行。",
                {"supported": list(SUPPORTED_PERMISSION_MODES)},
            )

        if request.reasoning_effort:
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                f"Kimi Code 没有暴露 effort 参数，无法接受 reasoning_effort="
                f"{request.reasoning_effort!r}",
                {"supported": []},
            )

        if resume_locator:
            argv += ["-S", str(resume_locator)]
        elif opts.get("continue_last"):
            argv += ["-c"]

        agent = opts.get("agent")
        if agent:
            argv += ["--agent", str(agent)]
        agent_file = opts.get("agent_file")
        if agent_file:
            argv += ["--agent-file", str(agent_file)]
        for d in opts.get("add_dirs") or request.extra.get("add_dirs") or []:
            argv += ["--add-dir", str(d)]
        skills_dir = opts.get("skills_dir") or request.extra.get("skills_dir")
        if not skills_dir:
            # 节点引用到的 Skill 随 extra["skills"] 到达：落成 kimi 认的
            # 「目录下每个 skill 一个子目录、内含 SKILL.md」形态。
            # 注意 --skills-dir 会**替换** kimi 自动发现的技能目录（已核实），
            # 这一点写在 manifest.notes 与节点配置界面上。
            skills_dir = self._materialize_skills(request)
        if skills_dir:
            argv += ["--skills-dir", str(skills_dir)]
        return argv

    def _materialize_skills(self, request: CreateSessionRequest) -> str | None:
        """把 extra["skills"] 落成本会话专属的临时 skills 目录，返回目录路径。

        每个会话一个独立目录（并发会话不共享），键为 session_ref；会话结束
        （``on_stream_closed``）时尽力清理。没有引用任何 Skill 时返回 None。
        """
        skills = request.extra.get("skills")
        if not isinstance(skills, list) or not skills:
            return None
        tmpdir = tempfile.mkdtemp(prefix="workerbee-kimi-skills-")
        used: set[str] = set()
        for item in skills:
            if not isinstance(item, dict):
                continue
            name = _safe_dirname(str(item.get("name") or "skill"))
            base = name
            suffix = 2
            while name in used:
                name = f"{base}-{suffix}"
                suffix += 1
            used.add(name)
            body = str(item.get("content") or "")
            text = f"---\nname: {name}\n---\n\n# {item.get('name') or name}\n\n{body}\n"
            skill_dir = os.path.join(tmpdir, name)
            os.makedirs(skill_dir, exist_ok=True)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write(text)
        key = str(request.session_ref_hint or "")
        if key:
            self._session_tmpdirs[key] = tmpdir
        return tmpdir

    async def on_stream_closed(self, session: CliSession, returncode: int | None) -> None:
        try:
            await super().on_stream_closed(session, returncode)
        finally:
            # harness 进程已退出，skills 目录不再被读取；尽力清理，失败不掩盖结束事件。
            tmpdir = self._session_tmpdirs.pop(session.session_ref, None)
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def credential_env_map(self) -> dict[str, str]:
        """kimi 以本机登录态为主（``kimi login``）；未验证其环境变量凭据形式，
        因此不声明任何凭据注入映射——宁可让用户依赖登录态，也不猜一个变量名。"""
        return {}

    # ------------------------------------------------------------------
    # 输出解析
    # ------------------------------------------------------------------

    async def handle_json_line(self, session: CliSession, obj: dict) -> None:
        role = obj.get("role")
        if role == "meta":
            await self._handle_meta(session, obj)
            return
        if role == "assistant":
            content = obj.get("content")
            if isinstance(content, str) and content:
                session.emitted_text = True
                await self.emit_event(
                    EventKind.OUTPUT,
                    session_ref=session.session_ref,
                    attempt_id=session.attempt_id,
                    text=content,
                    data={"block": "text"},
                )
            calls = obj.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    await self.emit_event(
                        EventKind.TOOL_USE,
                        session_ref=session.session_ref,
                        attempt_id=session.attempt_id,
                        data={
                            "tool_name": fn.get("name"),
                            "tool_use_id": call.get("id"),
                            "input": _maybe_json(fn.get("arguments")),
                        },
                    )
            if not content and not calls:
                await self.note_unknown_type(session, "assistant(empty)")
            return
        if role == "tool":
            await self.emit_event(
                EventKind.TOOL_RESULT,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={
                    "tool_use_id": obj.get("tool_call_id"),
                    "content": obj.get("content"),
                },
            )
            return
        if role == "user":
            content = obj.get("content")
            if isinstance(content, str) and content:
                await self.emit_event(
                    EventKind.OUTPUT,
                    session_ref=session.session_ref,
                    attempt_id=session.attempt_id,
                    text=content,
                    data={"block": "user_text"},
                )
                return
        await self.note_unknown_type(session, f"role={role}")

    async def _handle_meta(self, session: CliSession, obj: dict) -> None:
        mtype = str(obj.get("type") or "")
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid and sid != session.persist_locator:
            # 会话 id 只在轮次结束时才出现；记下来供 session.stat / 后续 resume 使用。
            session.persist_locator = sid
            await self.notify(
                NOTIFICATIONS.LOG,
                {
                    "message": f"取得会话持久化标识 {sid}（resume 时用 -S <id>）",
                    "session_ref": session.session_ref,
                    "level": "info",
                },
            )
        if mtype == "system.version":
            session.state = "alive"
            await self.emit_event(
                EventKind.SESSION_STARTED,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={
                    "harness_version": obj.get("version"),
                    "persist_locator": session.persist_locator,
                    "note": "kimi 不在创建时返回 session id，persist_locator 在轮次结束时补齐",
                },
            )
            return
        if mtype == "session.resume_hint":
            return  # 上面已经把 session_id 记进 persist_locator
        await self.note_unknown_type(session, f"meta/{mtype}")


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _build_manifest() -> AdapterManifest:
    return AdapterManifest(
        adapter_id=ADAPTER_ID,
        version=ADAPTER_VERSION,
        protocol_version=PROTOCOL_VERSION,
        harness_family=HARNESS_FAMILY,
        display_name="Kimi Code",
        capabilities=AdapterCapabilities(
            create_session=True,
            resume_session=True,      # -S <session_id>，已实测
            read_output=True,
            interact=False,           # -p 没有输入通道（kimi acp 是另一套协议，未实现）
            interrupt=False,
            stop=True,
            compact=False,
            permission_hook=False,    # 没有面向外部进程的权限钩子
            background_tasks=False,   # 流里没有任何后台工作状态行
            pause_in_place=False,
            checkpoint_resume=False,
            keep_checkpoint_on_stop=False,
            reasoning_efforts=[],     # kimi 没有 effort 参数；空 = 此维度不适用
            permission_modes=list(SUPPORTED_PERMISSION_MODES),
            non_interactive_modes=list(NON_INTERACTIVE_PERMISSION_MODES),
            models=[],                # -m 取 config.toml 里的模型别名，集合开放
            auth_modes=["native_login"],
            token_usage=False,        # stream-json 里没有 usage / 费用信息
            structured_output=True,   # 行是结构化的（role/type）
        ),
        auth_modes=["native_login"],
        platform_matrix={
            "linux": {"supported": True, "notes": "本机实测 2.1.1"},
            "macos": {"supported": False, "notes": "未实测，不做未验证的声明"},
            "windows": {"supported": False, "notes": "未实测，不做未验证的声明"},
        },
        notes=[
            "token_usage=False：stream-json 不携带 usage/费用，用量只能记为「未知」"
            "（OBS-04 的上报纪律），不会用 0 冒充。",
            "permission_hook=False：没有权限钩子；且 -p 与 -y/--auto/--plan 互斥，"
            "非交互模式下连权限策略都无法配置（实测），因此本 harness 只在自动权限模式下可用。",
            "permission_modes=[default]，non_interactive_modes=[default]：-p 模式下"
            "实测既不提问也不受阻（用 Bash 删文件的动作为 harness 自己放行了）。"
            "这是一条**如实的能力边界**：框架拦不住审批，用户选了 default 就等于把权限决定"
            "交给 harness 自己（HUM-03 会给出对应的告警）。",
            "kimi acp（ACP over stdio）未实现：那是另一套协议（需要客户端侧实现 fs/permission "
            "回调与 session/update 流），本适配器不据此声明 interact=True。",
            "compact=False：没有运行时的上下文整理操作。",
            "background_tasks=False：流里没有后台工作状态，不满足 RUN-06 的完成判据。",
            "system_prompt 无原生参数：以显式分隔的形式前置到 prompt（有损降级），"
            "不静默丢弃，但也不等价于真正的 system prompt。",
            "persist_locator 只能在一轮结束时拿到（session.resume_hint）；"
            "若会话在首轮结束前被杀，将无法 resume。",
            "stream-json 行格式属未公开契约，本适配器按实测格式解析，"
            "遇到不认识的 role/type 会如实上报而不是静默丢弃。",
            "extra.skills 会落成会话级临时目录并以 --skills-dir 传给 kimi；"
            "--skills-dir 会**替换** kimi 自动发现的用户/项目技能目录（已核实），"
            "会话结束后临时目录被清理。",
            "kimi 没有 MCP 支持（已核实 --help）：extra.mcp_tools 被忽略，"
            "节点引用的工具只经上下文组装进入 prompt 说明，不会被真正拉起。",
        ],
    )


def _safe_dirname(name: str) -> str:
    """Skill 名落盘为目录名：剔除路径分隔与特殊字符，避免越出临时目录。"""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name).strip("._")
    return cleaned or "skill"


def _parse_version(output: str) -> str | None:
    token = (output or "").strip().split()[-1] if (output or "").strip() else ""
    parts = token.split(".")
    if len(parts) >= 2 and parts[0].isdigit():
        return token
    return None


def _maybe_json(value: Any) -> Any:
    """tool_calls 的 arguments 是 JSON 字符串；解析失败就原样返回，不丢信息。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
