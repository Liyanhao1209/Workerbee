"""Claude Code 适配器（HAR-01–03、§8.1 六组契约、D-09）。

无头模式实测命令行（claude 2.1.283）::

    claude -p "<prompt>" --output-format stream-json --verbose \
           [--session-id <uuid> | --resume <session-id>] \
           [--model M] [--effort E] [--permission-mode P] \
           [--append-system-prompt S] [--add-dir D]

stdout 是 NDJSON，已实测到的行类型（``type`` 字段）：

- ``system/init``：session_id、model、tools、permissionMode、claude_code_version…
- ``assistant``：``message.content[]`` 里的 text / thinking / tool_use 块，附 usage；
- ``user``：工具结果（``tool_result`` 块）；
- ``result``：一轮结束。带 usage、total_cost_usd、num_turns、is_error、
  terminal_reason、subagent_stats。

**实测还发现两件必须处理的事**：

1. 协议流里会混进非 JSON 行（例如 ``[claude-code:unrecognized_model] {...}``）。
   适配器必须容忍并如实上报，不能把「输出变少了」当成「任务没做事」。
2. ``-p`` 无 stdin 时会等 3 秒才继续。因此 text 输入模式下用 DEVNULL 而不是
   开一个空管道。

能力声明如实（D-09 是本次实现的纪律红线）：

- ``compact=False``：``--autocompact`` 是**启动参数**，不是运行时可调用的操作。
  适配器不提供 control.compact，也不声称支持自动整理。若 harness 自己触发了
  整理（``system/compact_boundary``），我们**如实上报**观察到的事件，
  但这不等于「本适配器会做整理」。
- ``permission_hook=False``：外部进程拿不到标准权限钩子。CLI 有
  ``--permission-prompt-tool``（需要自己实现一个 MCP 工具来回答权限询问），
  本适配器不提供该宿主，因此不声称支持，配了非自动权限模式的 harness
  不得被当成完整支持（HUM-03、HAR-02）。
- ``background_tasks=False``：``--bg`` / ``claude agents`` 管的是**游离会话**，
  不是会话内的后台工作；result 行里的 ``subagent_stats`` 只是事后的计数，
  不足以支撑 RUN-06 的「后台工作是否结束」判据。
- ``interrupt`` / ``interact``：仅当以 stream-json 输入通道启动时为 True
  （见 ``input_format``）。text 模式下运行中无法注入输入，如实返回 NOT_SUPPORTED。

**stream-json 输入通道（实测 2.1.283）**：``-p --input-format stream-json`` 下
stdin 的每一帧都是一条用户消息，会话常驻多轮；首轮输入由 stdin 的第一帧给出
（``-p`` 不带 prompt 值即可，正是官方 SDK 的做法）。要盯住的四件事：

1. 每轮都会重发 ``system/init``（同一个 session_id）——只上报一次 session_started；
2. ``--replay-user-messages`` 会把我们投进去的输入原样回显（``isReplay``）——
   那是我们自己发出去的，不能当成 harness 输出；
3. 打断帧 ``control_request{interrupt}`` 有回执（``control_response.success``），
   本轮以 ``result/error_during_execution`` 收尾，进程继续存活——如实报成
   用户取消而不是 harness 故障；
4. 会话不会自己结束（RUN-06 需要 session_ended）：静默一段时间后关掉 stdin
  让它退出，结束事件仍由进程退出触发，不谎报。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
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
    "ClaudeCodeAdapter",
    "SUPPORTED_PERMISSION_MODES",
    "NON_INTERACTIVE_PERMISSION_MODES",
    "SUPPORTED_EFFORTS",
]

ADAPTER_ID = "claude-code"
HARNESS_FAMILY = "claude-code"
ADAPTER_VERSION = "0.1.0"

#: harness 能力探测与 compat_check 支持的版本区间（主版本）。
SUPPORTED_HARNESS_MAJOR = "2"

#: ``--permission-mode`` 的合法取值（来自 claude --help 实测）。
SUPPORTED_PERMISSION_MODES = (
    # ``default`` 不在 --help 的 choices 里，但它确实被接受（实测：显式传它不报错，
    # 且 init 事件上报的 permissionMode 就是 "default"）。它是最常用的「遇事就问我」
    # 模式，漏掉它会让用户最想要的那个选项填不进去。
    "default",
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)

#: ``--effort`` 的合法取值（来自 claude --help 实测）。
SUPPORTED_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: 输入通道模式：text（prompt 走 argv，进程一轮结束即退出）或
#: stream-json（开启流式输入通道，运行中可注入输入/打断）。
INPUT_FORMAT_ENV = "WORKERBEE_CLAUDE_CODE_INPUT_FORMAT"

#: 流式输入通道下「一轮跑完后的静默多久算结束」。
#: 0 或负数 = 不自动结束（会话常驻到显式 control.terminate）。
IDLE_END_SECONDS_ENV = "WORKERBEE_CLAUDE_CODE_IDLE_END_SECONDS"
DEFAULT_IDLE_END_SECONDS = 30.0

#: ``--permission-mode`` 里**不会向用户请求授权**的取值（依据见各条目注释，均为实测）。
NON_INTERACTIVE_PERMISSION_MODES = (
    # 实测：直接删掉了目标文件，全程没有任何询问。
    "auto",
    # 语义就是跳过全部权限检查（--help：--dangerously-skip-permissions 等价物）。
    "bypassPermissions",
    # 实测：明确回「Bash 权限在当前「don't ask」模式下被拒绝」——是拒绝，不是询问。
    "dontAsk",
    # 实测：处于 plan mode 时拒绝任何非只读操作，也没有向用户提问。
    # 它不会自动放行任何写操作（比「会询问」更严格），因此归入「不询问」；
    # 代价是用在需要写产物的节点上会因拿不到产物而失败，而不是卡住等人。
    "plan",
)


def _idle_end_seconds() -> float:
    raw = os.environ.get(IDLE_END_SECONDS_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_IDLE_END_SECONDS
    try:
        return float(raw)
    except ValueError as exc:
        # CFG-02：配置值非法必须显式失败，不能悄悄退回默认值。
        raise AdapterError(
            ErrorCode.INVALID_PARAMS,
            f"{IDLE_END_SECONDS_ENV} 必须是数字（秒），收到 {raw!r}",
        ) from exc


class ClaudeCodeAdapter(CliHarnessAdapter):
    """Claude Code 的无头（``-p``）适配器。"""

    exec_default = "claude"
    turn_end_on_exit = False
    """Claude Code 有显式的 ``result`` 行标记一轮结束，不需要靠进程退出推断。"""

    def __init__(self, *, input_format: str | None = None) -> None:
        super().__init__()
        self.input_format = input_format or os.environ.get(INPUT_FORMAT_ENV, "text")
        if self.input_format not in ("text", "stream-json"):
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                f"{INPUT_FORMAT_ENV} 只能是 text 或 stream-json，收到 {self.input_format!r}",
            )
        # 流式输入通道下才存在运行中交互与打断；manifest 必须与之一致，
        # 不能声明一个当前配置下做不到的能力。
        self.input_channel = self.input_format == "stream-json"
        self.idle_end_seconds = _idle_end_seconds()
        self._activity: dict[str, float] = {}
        """session_ref → 最近一次收到 harness 输出行的时刻（单调秒）。"""
        self._turns_ended: dict[str, int] = {}
        """session_ref → 已看到的 result 行数；>0 才谈得上「一轮跑完后静默」。"""
        self._interrupted: dict[str, float] = {}
        """session_ref → 最近一次由我们发出打断的时刻，用于如实解释 result 的报错。"""
        self._watchdogs: dict[str, asyncio.Task] = {}
        self.manifest = _build_manifest(self.input_channel, idle_end_seconds=self.idle_end_seconds)

    # ------------------------------------------------------------------
    # 探测 / 兼容性
    # ------------------------------------------------------------------

    async def on_probe(self, params: dict) -> dict:
        """实测探测（§8.1 第 1 组：declare 之外还要有实测）。

        探不到本身就是要如实上报的结果：ok=False + 原因，而不是抛异常让
        配置界面显示一个没有信息的失败。
        """
        exec_path = (params.get("exec_path") or params.get("harness", {}).get("exec_path"))
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
            "input_format": self.input_format,
        }

    async def on_compat_check(self, params: dict) -> dict:
        """配置期与启动期两处定位不兼容（§8.3）。

        探不到版本时返回 ``compatible=None``（未验证），**不返回 True**——
        「没查出来」和「兼容」是两件事。
        """
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
                f"适配器的行解析可能跟不上 harness 的新事件类型"
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
        # 只取名字，**不查它在不在**。构造参数列表是纯变换；「本机装没装这个
        # harness」是要拉起进程时才必须回答的问题，由 create_session 统一解析。
        # 在这里查会让每一个测 argv 形状的用例都依赖环境——CI 上没有 claude，
        # 一批纯逻辑用例会红，而它们要测的根本不是这件事。
        exe = request.harness.exec_path or self.exec_default
        opts = dict(request.harness.adapter_options or {})
        opts.update(request.extra.get("options") or {})

        argv = [exe, "--output-format", "stream-json", "--verbose"]

        if resume_locator:
            # --resume 与 --session-id 互斥：恢复时沿用 harness 自己的会话 id。
            argv += ["--resume", str(resume_locator)]
        else:
            argv += ["--session-id", _session_uuid(request)]

        if self.input_channel:
            # 实测 2.1.283：``-p`` 可以不带 prompt 值，首轮用户消息由 stdin 的
            # 第一帧给出（正是官方 SDK 的做法）。prompt 放 stdin 而不是 argv，
            # 既避免进程命令行泄漏任务内容，也避免「argv 与 stdin 各送一遍」
            # 导致同一条指令被执行两次。
            argv += ["-p", "--input-format", "stream-json"]
        else:
            if prompt is None:
                raise AdapterError(
                    ErrorCode.INVALID_PARAMS,
                    "text 输入模式下 prompt 必须随会话创建一起给出"
                    "（放在 extra.prompt）；本模式下运行中无法再注入输入。",
                    {"hint": "或以 " + INPUT_FORMAT_ENV + "=stream-json 启动适配器"},
                )
            argv += ["-p", prompt]

        model = (request.model_name or "").strip()
        if model:
            argv += ["--model", model]

        effort = (request.reasoning_effort or "").strip()
        if effort:
            if effort not in SUPPORTED_EFFORTS:
                # CFG-02：不受支持的取值必须显式失败，不能静默忽略。
                raise AdapterError(
                    ErrorCode.NOT_SUPPORTED,
                    f"Claude Code 不支持 effort={effort!r}",
                    {"supported": list(SUPPORTED_EFFORTS)},
                )
            argv += ["--effort", effort]

        mode = (request.permission_mode or "").strip()
        if mode:
            if mode not in SUPPORTED_PERMISSION_MODES:
                raise AdapterError(
                    ErrorCode.NOT_SUPPORTED,
                    f"Claude Code 不支持 permission_mode={mode!r}；"
                    f"不支持时如实拒绝，不静默替换成别的模式",
                    {"supported": list(SUPPORTED_PERMISSION_MODES)},
                )
            argv += ["--permission-mode", mode]

        if request.system_prompt:
            # 实测 2.1.283 只列出 --append-system-prompt（--bare 的帮助里提到
            # --append-system-prompt-file，但它没有出现在选项表里，本适配器不依赖
            # 未文档化的 flag）。代价是 system prompt 会出现在进程命令行上。
            argv += ["--append-system-prompt", request.system_prompt]

        # 谁来回答权限提问。
        #
        # - 有 host 控制通道时用 ``host``：**我们就是那个 host**，握手时注册了
        #   PermissionRequest 钩子，harness 会反过来问我们，我们再问用户（HUM-03）。
        # - 没有控制通道时用 ``none``：此时没人能回答，``host`` 会让本会弹窗的动作
        #   永远挂着；``none`` 让它被明确拒绝而不是卡死。
        #
        # 这一行曾经硬编码成 ``none``，于是即使接通了钩子，CLI 也已经自行拒绝了——
        # 表现是「审批能收到、批了、命令却没执行」，很难从现象反推到这里。
        default_target = "host" if self.input_channel else "none"
        argv += [
            "--permission-prompts",
            str(opts.get("permission_prompts") or default_target),
        ]

        for d in opts.get("add_dirs") or request.extra.get("add_dirs") or []:
            argv += ["--add-dir", str(d)]
        for flag, key in (
            ("--allowedTools", "allowed_tools"),
            ("--disallowedTools", "disallowed_tools"),
            ("--settings", "settings"),
            ("--mcp-config", "mcp_config"),
            ("--agent", "agent"),
        ):
            value = opts.get(key)
            if value:
                argv += [flag, str(value)]
        if opts.get("dangerously_skip_permissions"):
            argv += ["--dangerously-skip-permissions"]
        return argv

    # ------------------------------------------------------------------
    # host 控制通道（HUM-03）
    # ------------------------------------------------------------------

    def build_env(self, request: CreateSessionRequest, extras: dict) -> dict[str, str]:
        env = super().build_env(request, extras)
        if self.input_channel:
            # 与官方 Agent SDK 一致：这两个变量告诉 CLI「上面有一个 host」。
            # 少了它们，CLI 遇到需要授权的操作时**不是不问，而是直接拒绝**——
            # 实测过：用户以为命令跑了，其实一步都没执行。
            # 这里用 setdefault：注册表里的显式配置优先。
            env.setdefault("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
            env.setdefault("CLAUDE_AGENT_SDK_VERSION", ADAPTER_VERSION)
        return env

    def initialize_request(self, session: CliSession) -> dict | None:
        """注册 ``PermissionRequest`` 钩子。

        这是 claude 的权限钩子——不在 CLI 的 flag 列表里，而在 host 控制协议里：
        握手时声明「这类事件回调我」，harness 需要授权时就反过来调我们。
        ``claude --help`` 只字未提，官方 SDK 也是这么做的（它设
        ``CLAUDE_CODE_ENTRYPOINT=sdk-ts`` 再发这条 initialize）。
        """
        if not self.input_channel:
            return None
        callback_id = f"hook_perm_{session.session_ref[:8]}"
        session.hook_callback_ids["PermissionRequest"] = callback_id
        return {
            "subtype": "initialize",
            "hooks": {
                "PermissionRequest": [
                    {"matcher": None, "hookCallbackIds": [callback_id], "timeout": 3600}
                ]
            },
            "sdkMcpServers": [],
            "sdkMcpServerConfigs": {},
            "sdkMcpServerManifests": {},
        }

    async def on_permission_hook(
        self, session: CliSession, request_id: str, payload: dict
    ) -> None:
        """harness 问「这个操作能跑吗」→ 转成内核的审批项。

        在用户答复之前，harness 会一直卡着（SDK 文档：权限提问没有 park 超时）。
        所以这里既不能忘了记下 request_id，也不能在没人能答的时候假装答过——
        两种情况都会让任务永远停住。
        """
        request = self.build_permission_request(session, request_id, payload)
        session.pending_hooks[request.approval_id] = request_id

        await self.notify(
            NOTIFICATIONS.PERMISSION_REQUEST, request.model_dump(mode="json")
        )
        # 兜底超时：harness 的权限提问**没有 park 超时**（SDK 明文写的），
        # 所以只要这条答复因为任何原因没送到，它就会永远卡着。
        # 内核侧的审批超时管的是「用户没答」，这里管的是「答复丢了」——
        # 两者叠加，才不会有任何一种情况把任务永久挂住。
        self._spawn_bg(self._hook_watchdog(session, request.approval_id))

    async def _hook_watchdog(self, session: CliSession, approval_id: str) -> None:
        await asyncio.sleep(self.permission_hook_timeout)
        request_id = session.pending_hooks.pop(approval_id, None)
        if request_id is None:
            return  # 已答复
        await self.send_control_response(
            session,
            request_id,
            {
                "behavior": "deny",
                "message": f"审批在 {self.permission_hook_timeout:.0f}s 内未送达，已按拒绝处理",
            },
        )
        await self.emit_event(
            EventKind.ERROR,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            text=f"审批 {approval_id} 超时未送达，已按拒绝处理",
            data={"error_kind": "approval_undelivered", "approval_id": approval_id},
        )

    def credential_env_map(self) -> dict[str, str]:
        """Claude Code 认 Anthropic 系环境变量；凭据只在此处向内传（AUTH-02）。"""
        return {
            "api_key": "ANTHROPIC_API_KEY",
            "base_url": "ANTHROPIC_BASE_URL",
            "auth_token": "ANTHROPIC_AUTH_TOKEN",
        }

    # ------------------------------------------------------------------
    # 输出解析
    # ------------------------------------------------------------------

    async def handle_json_line(self, session: CliSession, obj: dict) -> None:
        # 任何一行都算「有动静」：静默判定的依据是真实输出，不是猜测。
        self._activity[session.session_ref] = time.monotonic()
        kind = obj.get("type")
        if kind == "system":
            await self._handle_system(session, obj)
        elif kind == "assistant":
            await self._handle_assistant(session, obj)
        elif kind == "user":
            await self._handle_user(session, obj)
        elif kind == "result":
            await self._handle_result(session, obj)
        else:
            # 未知类型如实记一笔（harness 升级会带来新行类型，§8.3）。
            await self.note_unknown_type(session, str(kind))

    async def _handle_system(self, session: CliSession, obj: dict) -> None:
        subtype = str(obj.get("subtype") or "")
        if subtype == "init":
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                # persist_locator = harness 自己的会话 id（恢复会话时用它）。
                session.persist_locator = sid
            session.state = "alive"
            if session.started_reported:
                # 实测：流式输入通道下**每一轮**都会重发一条 init（同一个
                # session_id）。重复上报会让上游以为开了好几个会话，所以只在
                # 第一条 init 上报一次。
                return
            session.started_reported = True
            tools = obj.get("tools")
            await self.emit_event(
                EventKind.SESSION_STARTED,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={
                    "persist_locator": session.persist_locator,
                    "model": obj.get("model"),
                    "permission_mode": obj.get("permissionMode"),
                    "cwd": obj.get("cwd"),
                    "harness_version": obj.get("claude_code_version"),
                    "tools": list(tools) if isinstance(tools, list) else [],
                    "harness_capabilities": obj.get("capabilities") or [],
                },
            )
            return
        if subtype in ("compact_boundary", "pre_compact", "post_compact"):
            # harness 自己做的整理：如实上报观察到的事实，不代表适配器提供整理能力。
            meta = obj.get("compact_metadata") or {}
            await self.emit_event(
                EventKind.COMPACT,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={
                    "trigger": meta.get("trigger", subtype),
                    "pre_tokens": meta.get("pre_tokens"),
                    "observed_by": "harness",
                    "adapter_supports_compact": False,
                },
            )
            return
        await self.note_unknown_type(session, f"system/{subtype}")

    async def _handle_assistant(self, session: CliSession, obj: dict) -> None:
        message = obj.get("message")
        if not isinstance(message, dict):
            await self.note_unknown_type(session, "assistant(no message)")
            return
        content = message.get("content")
        if not isinstance(content, list):
            await self.note_unknown_type(session, "assistant(non-list content)")
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text:
                    session.emitted_text = True
                    await self.emit_event(
                        EventKind.OUTPUT,
                        session_ref=session.session_ref,
                        attempt_id=session.attempt_id,
                        text=text,
                        data={"block": "text"},
                    )
            elif btype == "thinking":
                # 思维链也属于 harness 的输出；打标记让上游能按需过滤，
                # 而不是由适配器替用户决定「这个不算输出」。
                text = block.get("thinking") or ""
                if text:
                    await self.emit_event(
                        EventKind.OUTPUT,
                        session_ref=session.session_ref,
                        attempt_id=session.attempt_id,
                        text=text,
                        data={"block": "thinking"},
                    )
            elif btype == "tool_use":
                await self.emit_event(
                    EventKind.TOOL_USE,
                    session_ref=session.session_ref,
                    attempt_id=session.attempt_id,
                    data={
                        "tool_name": block.get("name"),
                        "tool_use_id": block.get("id"),
                        "input": block.get("input") or {},
                    },
                )
            else:
                await self.note_unknown_type(session, f"assistant/{btype}")

    async def _handle_user(self, session: CliSession, obj: dict) -> None:
        if obj.get("isReplay"):
            # --replay-user-messages 会把我们投进去的输入原样回显（实测）。
            # 那是**我们自己发出去的东西**，当成 harness 输出会让产物里出现
            # 用户指令的副本——只记账，不上报。
            return
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            await self.note_unknown_type(session, "user(non-list content)")
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                await self.emit_event(
                    EventKind.TOOL_RESULT,
                    session_ref=session.session_ref,
                    attempt_id=session.attempt_id,
                    data={
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error", False)),
                        "content": _flatten_content(block.get("content")),
                    },
                )
            elif block.get("type") == "text":
                # 极少数情况下用户侧会带回文本（例如打断回执），如实转成输出。
                text = block.get("text") or ""
                if text:
                    await self.emit_event(
                        EventKind.OUTPUT,
                        session_ref=session.session_ref,
                        attempt_id=session.attempt_id,
                        text=text,
                        data={"block": "user_text"},
                    )
            else:
                await self.note_unknown_type(session, f"user/{block.get('type')}")

    async def _handle_result(self, session: CliSession, obj: dict) -> None:
        subtype = str(obj.get("subtype") or "")
        is_error = bool(obj.get("is_error"))
        succeeded = subtype == "success" and not is_error
        # 打进过断的标记沿用一次：被打断的那一轮 result 是 error_during_execution
        # （实测），不能把它当成 harness 自己出错。
        interrupted = self._interrupted.pop(session.session_ref, None) is not None
        self._turns_ended[session.session_ref] = self._turns_ended.get(session.session_ref, 0) + 1

        # usage：只上报真实拿到的字段。取不到就**不上报**，由内核记「未知」，
        # 绝不用 0 冒充（OBS-04）。
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        if usage:
            payload = {
                key: usage[key]
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
                if isinstance(usage.get(key), (int, float))
            }
            if isinstance(obj.get("total_cost_usd"), (int, float)):
                payload["total_cost_usd"] = obj["total_cost_usd"]
            payload["source"] = "result"
            payload["model_usage"] = obj.get("modelUsage") or {}
            await self.emit_event(
                EventKind.USAGE,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data=payload,
            )
        else:
            await self.notify(
                NOTIFICATIONS.LOG,
                {
                    "message": "result 行未携带 usage：本次用量记为未知（不是 0）",
                    "session_ref": session.session_ref,
                    "level": "warn",
                },
            )

        meta = {
            key: obj[key]
            for key in (
                "num_turns",
                "duration_ms",
                "duration_api_ms",
                "terminal_reason",
                "stop_reason",
                "permission_denials",
                # 事后计数，不足以支撑 RUN-06：只作为诊断信息携带。
                "subagent_stats",
            )
            if key in obj
        }
        await self.emit_event(
            EventKind.TURN_END,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"subtype": subtype, "is_error": is_error, **meta},
        )

        if not succeeded:
            if interrupted:
                # 这一轮是我们请求打断才结束的：如实说是「用户取消」，
                # 而不是报成 harness 故障（那会触发一次毫无意义的重试）。
                error_class, error_kind = "user_cancelled", "interrupted_by_user"
            else:
                error_class, error_kind = _classify_result_error(obj)
            await self.emit_event(
                EventKind.ERROR,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=f"本轮以错误结束：{subtype or 'unknown'}",
                data={"error_class": error_class, "error_kind": error_kind, **meta},
            )

        # 有些场景下 harness 只在 result 里给最终文本（前面没有 assistant 文本块）。
        # 此时把它作为输出上报，避免「任务做完了但产物是空的」。
        final_text = obj.get("result")
        if isinstance(final_text, str) and final_text and not session.emitted_text:
            session.emitted_text = True
            await self.emit_event(
                EventKind.OUTPUT,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=final_text,
                data={"block": "result_fallback"},
            )

        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={
                "state": "ended" if succeeded else "failed",
                "subtype": subtype,
                "is_error": is_error,
            },
        )

    # ------------------------------------------------------------------
    # 输入通道（仅 stream-json 模式）
    # ------------------------------------------------------------------

    async def on_session_spawned(self, session: CliSession) -> None:
        """流式输入通道下给会话挂一个静默巡检（见 ``_idle_watchdog``）。"""
        if not self.input_channel:
            return
        self._activity[session.session_ref] = time.monotonic()
        self._watchdogs[session.session_ref] = asyncio.create_task(
            self._idle_watchdog(session, self.idle_end_seconds),
            name=f"{session.label}:idle",
        )

    async def _idle_watchdog(self, session: CliSession, seconds: float) -> None:
        """一轮跑完后静默超过 N 秒就关掉输入通道，让 harness 自己退出。

        为什么需要它：流式输入通道是**常驻**的，harness 跑完一轮不会退出；
        而内核按「会话已结束」判定阶段完成（RUN-06）。所以必须有人把常驻会话
        真正结束掉——这里选择关 stdin（实测 2.1.283 在 stdin EOF 后跑完当轮、
        以 0 退出），结束事件仍然由**进程真的退出**触发，不谎报 ended。

        静默窗口内到达的任何输入（HUM-01 的 BTW、内核的补投）都会重置计时。
        ``seconds <= 0`` 表示不自动结束：会话常驻到显式 terminate 为止。
        """
        if seconds <= 0:
            return
        ref = session.session_ref
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(min(1.0, seconds))
                if not session.alive():
                    return
                if not self._turns_ended.get(ref):
                    continue  # 还没跑完任何一轮，谈不上静默
                quiet = loop.time() - self._activity.get(ref, loop.time())
                if quiet < seconds:
                    continue
                proc = session.proc
                if proc.stdin is None or proc.stdin.is_closing():
                    return
                session.end_reason = "idle_eof"
                await self.notify(
                    NOTIFICATIONS.LOG,
                    {
                        "message": (
                            f"会话静默 {quiet:.0f}s（>{seconds:.0f}s）无输出且没有新输入，"
                            f"关闭输入通道让 harness 结束；结束以进程退出为准"
                        ),
                        "session_ref": ref,
                        "level": "info",
                    },
                )
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                    proc.stdin.close()
                return
        finally:
            self._activity.pop(ref, None)
            self._turns_ended.pop(ref, None)
            self._watchdogs.pop(ref, None)

    async def on_shutdown(self) -> None:
        for task in self._watchdogs.values():
            if not task.done():
                task.cancel()
        self._watchdogs.clear()
        await super().on_shutdown()

    async def send_input(self, session: CliSession, *, kind: str, text: str) -> None:
        proc = session.proc
        if proc.stdin is None or proc.stdin.is_closing():
            raise AdapterError(
                ErrorCode.SESSION_DEAD, f"会话 {session.session_ref} 的输入通道已关闭"
            )
        if kind == "interrupt":
            # Claude Code 的控制帧走同一条流式输入通道。实测 2.1.283 的回执：
            # control_response{response:{subtype:"success", request_id, response:
            # {still_queued:[]}}}，随后本轮以 result/error_during_execution 收尾，
            # 进程继续存活（见 _handle_result 对 user_cancelled 的处理）。
            self._interrupted[session.session_ref] = time.monotonic()
            frame = {
                "type": "control_request",
                "request_id": f"int-{uuid.uuid4().hex[:8]}",
                "request": {"subtype": "interrupt"},
            }
        else:
            frame: dict[str, Any] = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
            if session.persist_locator:
                # session_id 只用来说明「发给哪个会话」；新会话还没有 id 时不给，
                # 官方 SDK 也不给（免得 harness 拿一个它自己还没确认的 id 去对账）。
                frame["session_id"] = session.persist_locator
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AdapterError(
                ErrorCode.SESSION_DEAD, f"会话 {session.session_ref} 输入通道已断开: {exc}"
            ) from exc
        # 注入输入也算「有动静」：静默巡检据此重置，避免刚投完输入就被判静默。
        self._activity[session.session_ref] = time.monotonic()


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _build_manifest(input_channel: bool, *, idle_end_seconds: float | None = None) -> AdapterManifest:
    return AdapterManifest(
        adapter_id=ADAPTER_ID,
        version=ADAPTER_VERSION,
        protocol_version=PROTOCOL_VERSION,
        harness_family=HARNESS_FAMILY,
        display_name="Claude Code",
        capabilities=AdapterCapabilities(
            create_session=True,
            resume_session=True,          # --resume <session-id>，已实测
            read_output=True,
            interact=input_channel,       # 仅流式输入通道下成立（实测可多轮注入）
            interrupt=input_channel,      # 控制帧实测有回执，见 send_input 注释
            stop=True,
            compact=False,                # --autocompact 是启动参数，不是运行时操作
            # 仅流式输入通道下成立：权限提问走 host 控制协议（initialize 注册
            # PermissionRequest 钩子），必须先有控制通道。
            permission_hook=input_channel,
            background_tasks=False,       # --bg/claude agents 管的是游离会话
            pause_in_place=False,
            checkpoint_resume=False,
            keep_checkpoint_on_stop=False,
            reasoning_efforts=list(SUPPORTED_EFFORTS),
            permission_modes=list(SUPPORTED_PERMISSION_MODES),
            non_interactive_modes=list(NON_INTERACTIVE_PERMISSION_MODES),
            # models 留空 = 不限定。--model 同时接受别名与完整模型名，集合是开放的；
            # 列举反而会让用户填对的名字被误判为「不受支持」（CFG-02）。
            models=[],
            auth_modes=["native_login", "api_key"],
            token_usage=True,             # result 行带 usage 与 total_cost_usd
            structured_output=True,       # stream-json 是结构化行流
        ),
        auth_modes=["native_login", "api_key"],
        platform_matrix={
            "linux": {"supported": True, "notes": "本机实测 2.1.283"},
            "macos": {"supported": True, "notes": "未实测，按 CLI 契约推断"},
            "windows": {
                "supported": True,
                "notes": "未实测；取消链的信号语义按 D-13 退化",
            },
        },
        notes=[
            "compact=False：--autocompact 只能作为启动参数给出，运行时不提供整理操作；"
            "harness 自己触发的 compact_boundary 会被如实上报，但不等于适配器提供该能力。",
            "permission_hook：**仅流式输入通道下为 True**。机制是 host 控制协议——"
            "启动握手（initialize）时注册 PermissionRequest 钩子，harness 需要授权时"
            "发 control_request 回调，我们回 control_response 给出决定。"
            "这条路径 claude --help 里完全没有，只有官方 SDK 暴露；"
            "它不需要 --permission-prompt-tool，也不需要自建 MCP 宿主。"
            "text 通道下仍为 False——没有控制通道就没有钩子，此时会询问的权限模式"
            "必须被 HUM-03 的门禁拦住，否则 harness 会卡在一个无人应答的提问上。",
            "background_tasks=False：--bg / claude agents 面向游离会话，"
            "result 行的 subagent_stats 只是事后计数，不满足 RUN-06 的完成判据。",
            "permission_modes：来自 --help 的 choices（acceptEdits/auto/bypassPermissions/"
            "manual/dontAsk/plan）。non_interactive_modes=[auto, bypassPermissions, dontAsk, plan] "
            "是实测结论（无 SDK 宿主时逐个跑「删除文件」任务）：auto 直接执行、dontAsk 明确回「被拒绝」、"
            "plan 拒绝一切非只读操作且不提问；manual 回「需要您批准」、acceptEdits 本次虽直接执行了，"
            "但只实测了一种动作，无法排除它在别的动作上仍会询问，故按会询问处理（宁可保守，"
            "把它错放进「不询问」等于替用户放行）。",
            "text 输入模式下 prompt 走 argv（进程命令行可见），且运行中无法注入输入。"
            "stream-json 输入通道下 prompt 走 stdin 的第一帧（不出现在命令行上）。",
            "stream-json 输入通道是**常驻**会话：跑完一轮不会自己退出，"
            f"适配器在静默 {DEFAULT_IDLE_END_SECONDS:.0f}s（{IDLE_END_SECONDS_ENV}，0=不自动结束）"
            "后关闭输入通道让 harness 退出；session_ended 始终以进程真的退出为准。",
            "无 SDK 宿主时默认 --permission-prompts none：需要审批的动作会被拒绝，"
            "而不是让整轮挂在一个没人能回答的询问上。",
        ],
    )


def _session_uuid(request: CreateSessionRequest) -> str:
    """``--session-id`` 要求合法 UUID；用内核给的建议值，否则自己生成。"""
    hint = request.session_ref_hint
    if hint:
        try:
            return str(uuid.UUID(str(hint)))
        except ValueError:
            pass
    return str(uuid.uuid4())


def _parse_version(output: str) -> str | None:
    """从 ``2.1.283 (Claude Code)`` 里取出 ``2.1.283``。"""
    token = (output or "").strip().split()[0] if (output or "").strip() else ""
    parts = token.split(".")
    if len(parts) >= 2 and parts[0].isdigit():
        return token
    return None


def _classify_result_error(obj: dict) -> tuple[str, str]:
    """把 result 的错误子类型映射到 D-05 的错误分类。"""
    subtype = str(obj.get("subtype") or "")
    status = obj.get("api_error_status")
    if subtype == "error_max_turns":
        return "fatal_error", "max_turns"
    if isinstance(status, int):
        if status == 429:
            return "retryable_error", "rate_limit"
        if status in (401, 403):
            return "fatal_error", "auth"
        if status >= 500:
            return "retryable_error", "network"
        if status >= 400:
            return "fatal_error", "config"
    if subtype == "error_during_execution":
        return "retryable_error", "harness_error"
    return "retryable_error", "harness_error"


def _flatten_content(content: Any) -> str:
    """工具结果内容可能是字符串或块数组；统一成文本，保持原样不做摘要。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)
