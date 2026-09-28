"""CLI 型 harness 适配器的共用骨架（对 SDK 的**纯增量**扩展）。

为什么存在：claude_code 与 kimi_code 两个适配器要做的是同一件事——
把一个本地 CLI 作为**会话子进程**拉起，把它的 stdout 解析成统一事件流，
并按 §10.4 的两级取消链把它收干净。这段逻辑与具体 harness 无关：
只有「怎么拼 argv」与「每一行怎么解释」是 harness 特有的。

让两个适配器各写一份进程管理，等于把「停止」语义复制两份——而 §8.3 要求
相同用户操作在不同 harness 下有一致的产品语义。因此这里用模板方法把
不变量收敛到一处：

- 会话台账（session_ref → 子进程）与 ``session.list/stat/dispose``；
- 事件序号、终止事件、非 JSON 行的**显式**上报（不静默丢弃）；
- 两级终止：SIGTERM → 宽限期 → SIGKILL，并把「是否升级过」如实回包；
- 适配器退出时收掉全部子进程（RES-01/02 的释放义务）。

边界说明：本模块只 import ``sdk`` 的公开类型（base/contract/protocol），
不修改它们的任何既有语义，也不改变协议本身；它是 SDK 的便利层而非契约的一部分。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from .base import AdapterBase
from .contract import (
    PermissionRequest,
    CreateSessionRequest,
    HarnessConfig,
    InputKind,
    SessionInfo,
)
from .protocol import AdapterError, ErrorCode, EventKind, NOTIFICATIONS

__all__ = [
    "CliSession",
    "CliHarnessAdapter",
    "parse_session_request",
    "find_executable",
    "run_version_probe",
    "DEFAULT_TERMINATE_GRACE_MS",
    "DEFAULT_KILL_WAIT_MS",
]

#: 两级取消链的默认宽限期（§10.4：软终止 → 等待落盘 → 硬杀）。
DEFAULT_TERMINATE_GRACE_MS = 5000
#: 升级为 SIGKILL 后等待回收的时长；超时即如实报告「未能确认回收」。
DEFAULT_KILL_WAIT_MS = 5000


def _now_iso() -> str:
    from ...core.domain.base import now_iso

    return now_iso()


# ----------------------------------------------------------------------
# 请求解析
# ----------------------------------------------------------------------


def parse_session_request(params: dict) -> tuple[CreateSessionRequest, dict]:
    """把协议参数解析成 ``CreateSessionRequest`` + 适配器私有参数。

    ``session.create`` / ``session.resume`` 的 params schema 由适配器定义
    （SDK 只固定方法名），但两种写法都要能收：整体包成 ``request`` 子对象，
    或把请求字段平铺在顶层再附带 ``session_ref`` / ``persist_locator`` 之类
    的适配器私有键。返回 ``(request, extras)``。
    """
    if not isinstance(params, dict):
        raise AdapterError(ErrorCode.INVALID_PARAMS, "params 必须是对象")

    work = dict(params)
    nested = work.pop("request", None)
    if nested is not None:
        if not isinstance(nested, dict):
            raise AdapterError(ErrorCode.INVALID_PARAMS, "request 必须是对象")
        base = dict(nested)
        # 顶层键优先（session_ref / persist_locator 等只为 resume 存在）
        base.update({k: v for k, v in work.items()})
        work = base

    known = set(CreateSessionRequest.model_fields)
    extras = {k: v for k, v in work.items() if k not in known}
    core = {k: v for k, v in work.items() if k in known}

    if "harness" not in core:
        raise AdapterError(
            ErrorCode.INVALID_PARAMS,
            "缺少 harness（HarnessConfig）；适配器无法判断启动哪个 harness",
        )
    try:
        request = CreateSessionRequest.model_validate(core)
    except Exception as exc:  # pydantic 校验失败在协议边界变成可读错误
        raise AdapterError(
            ErrorCode.INVALID_PARAMS, f"session 请求字段非法：{exc}"
        ) from exc
    return request, extras


def find_executable(exec_path: str | None, default: str) -> str:
    """解析 harness 可执行文件，找不到时给出可读错误而非裸 FileNotFoundError。"""
    candidate = exec_path or default
    if os.path.isabs(candidate) or os.sep in candidate:
        if not os.path.exists(candidate):
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness 可执行文件不存在：{candidate}",
                {"exec_path": candidate},
            )
        return candidate
    found = shutil.which(candidate)
    if found is None:
        raise AdapterError(
            ErrorCode.HARNESS_UNAVAILABLE,
            f"PATH 中找不到 {candidate}。请在 HarnessRegistration.exec_path "
            f"里指定绝对路径，或安装该 harness。",
            {"exec_path": candidate},
        )
    return found


async def run_version_probe(
    argv: Sequence[str], *, env: dict[str, str] | None = None, timeout: float = 15.0
) -> tuple[bool, str]:
    """跑一条只读的版本命令，返回 ``(ok, 输出或错误)``。

    用于 ``capabilities.probe`` 与 ``health.compat_check`` 的**实测**探测
    （§8.1 第 1 组：声明 + 实测）。任何失败都变成可读文本，不抛异常——
    「探不到」本身就是要如实上报的结果。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )
    except (OSError, ValueError) as exc:
        return False, f"无法启动 {argv[0]}: {exc}"

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return False, f"{argv[0]} {' '.join(argv[1:])} 超时（{timeout}s）"
    text = (out or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}：{text[:500]}"
    return True, text


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------


@dataclass
class CliSession:
    """一个 harness CLI 子进程的适配器侧台账项。

    与 ``SessionInfo``（对外视图）分开：这里放的是不该外泄的内部状态——
    解析计数、子进程句柄、stderr 尾巴。对外只暴露 ``to_info()``。
    """

    session_ref: str
    harness_id: str
    persist_locator: str | None
    proc: asyncio.subprocess.Process
    label: str
    attempt_id: str | None = None
    model_name: str | None = None
    cwd: str | None = None
    permission_mode: str | None = None
    reasoning_effort: str | None = None
    system_prompt: str | None = None
    state: str = "starting"
    """starting / alive / ended / lost。lost = 无法确认真实状态（OBS-01）。"""

    started_at: str = field(default_factory=_now_iso)
    ended_at: str | None = None
    exit_code: int | None = None

    lines_read: int = 0
    non_json_lines: int = 0
    parse_errors: int = 0
    unknown_line_types: dict[str, int] = field(default_factory=dict)
    emitted_text: bool = False
    """是否已把 assistant 的文本作为 OUTPUT 上报过（决定要不要用 result 兜底）。"""

    stderr_tail: list[str] = field(default_factory=list)
    pump_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    input_supported: bool = False
    received_inputs: list[dict] = field(default_factory=list)
    """收到的 io.send_input，供 HUM-01/02 断言「到达了所选的那个会话」。"""

    accepted_initial_input: bool = False
    initialized: bool = False
    """host 握手（``initialize`` 控制帧）是否已发出。"""

    #: 待答复的钩子回调：approval_id → 控制帧的 request_id。
    #: harness 通过 control_request 回调来问权限，我们的答复必须原样带回那个
    #: request_id，否则它匹配不上，工具会一直卡着（SDK 文档明说「权限提问
    #: 没有 park 超时，丢一次答复就是永久阻塞」）。
    pending_hooks: dict[str, str] = field(default_factory=dict)

    #: 本次会话声明过的钩子回调 id（initialize 里注册的那些）。
    hook_callback_ids: dict[str, str] = field(default_factory=dict)
    """建会话时是否**真的**把本轮首轮输入交付给了 harness。

    回包里的同名字段就是它。内核据此决定要不要再 ``send_input`` 一次：
    说 True 而实际没送到，阶段会永远等一个不会开始的任务；说 False 而其实
    已经送了，同一条指令会被执行两遍。两种都要求这里是实测事实，不是猜测。
    """

    started_reported: bool = False
    """是否已上报过 ``session_started``。

    流式输入通道下 harness 每轮都会重发 init（实测 claude 2.1.283 如此），
    重复上报会让上游以为开了好几个会话。
    """

    end_reason: str | None = None
    """会话结束的可读原因（如 ``idle_eof`` / ``terminate``）。
    只用于让「为什么结束」在事件里可见，不改变结束本身的判定。"""

    def alive(self) -> bool:
        return self.proc.returncode is None and self.state in ("starting", "alive")

    def to_info(self) -> SessionInfo:
        return SessionInfo(
            session_ref=self.session_ref,
            harness_id=self.harness_id,
            state="alive" if self.alive() else ("lost" if self.state == "lost" else "ended"),
            persist_locator=self.persist_locator,
            model_name=self.model_name,
            created_at=self.started_at,
            pid=self.proc.pid,
            cwd=self.cwd,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "lines_read": self.lines_read,
            "non_json_lines": self.non_json_lines,
            "parse_errors": self.parse_errors,
            "unknown_line_types": dict(self.unknown_line_types),
            "exit_code": self.proc.returncode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "received_inputs": list(self.received_inputs),
            "stderr_tail": self.stderr_tail[-10:],
        }


# ----------------------------------------------------------------------
# 适配器骨架
# ----------------------------------------------------------------------


class CliHarnessAdapter(AdapterBase):
    """本地 CLI harness 适配器基类。

    子类需要提供：
    - ``build_argv(request, prompt, resume_locator, extras)`` → argv。
      **约定 argv[0] 是可执行文件**：基类在拉起进程前会把它替换成解析后的
      绝对路径。``build_argv`` 本身不查可执行文件是否存在——构造参数列表是
      纯变换，让它依赖本机装了什么，会让每一个测 argv 形状的用例都变成
      环境依赖（CI 上没装 claude，一批纯逻辑用例当场就红）。
    - ``handle_json_line(session, obj)`` → 解释一行 NDJSON 并上报事件；
    - 可选覆盖 ``on_stream_closed`` / ``on_interrupt`` / ``on_send_input`` 等。

    基类保证的不变量：终止语义（两级 + 如实回报）、会话台账、
    ``session.list`` / ``session.stat`` / ``session.dispose`` / ``health.heartbeat``、
    退出时释放全部子进程。
    """

    #: 子类覆盖：描述性名称，仅用于日志与错误信息。
    exec_default: str = ""

    #: 该 harness 的「一轮结束」是否只能由进程退出推断（无显式 turn 结束标记）。
    turn_end_on_exit: bool = False

    #: 输入通道是否可用（text 输入模式下 harness 只吃 argv 里的 prompt）。
    input_channel: bool = False

    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, CliSession] = {}
        self.terminate_grace_ms = DEFAULT_TERMINATE_GRACE_MS
        #: 后台协程的强引用。asyncio 只持弱引用，不记下来就可能被 GC 掉。
        self._bg_tasks: set[asyncio.Task] = set()
        self.kill_wait_ms = DEFAULT_KILL_WAIT_MS
        self.max_reported_stray_lines = 20
        """非 JSON 行与未知行类型的上报上限：流是长尾的，不能让它淹没事件通道。"""

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def on_session_create(self, params: dict) -> dict:
        request, extras = parse_session_request(params)
        session = await self._spawn_session(
            request, extras, resume_locator=None, checkpoint=None
        )
        return {
            "session": session.to_info().model_dump(mode="json"),
            # 显式回包，不让内核去猜（猜错的两种后果都不小，见 SessionHandle）。
            "accepted_initial_input": session.accepted_initial_input,
        }

    async def on_session_resume(self, params: dict) -> dict:
        request, extras = parse_session_request(params)
        locator = extras.get("persist_locator")
        if not locator:
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                "session.resume 需要 persist_locator（该 harness 自己的会话持久化标识）",
                {"harness_family": self.manifest.harness_family},
            )
        session = await self._spawn_session(
            request,
            extras,
            resume_locator=str(locator),
            checkpoint=extras.get("checkpoint"),
        )
        return {
            "session": session.to_info().model_dump(mode="json"),
            "accepted_initial_input": session.accepted_initial_input,
            "resumed": True,
        }

    async def on_session_list(self, params: dict) -> dict:
        harness_id = params.get("harness_id")
        include_ended = bool(params.get("include_ended", True))
        infos = [
            s.to_info().model_dump(mode="json")
            for s in self._sessions.values()
            if (harness_id is None or s.harness_id == harness_id)
            and (include_ended or s.alive())
        ]
        return {"sessions": infos, "count": len(infos)}

    async def on_session_stat(self, params: dict) -> dict:
        session = self._get_session(params)
        return {
            "session": session.to_info().model_dump(mode="json"),
            "diagnostics": session.diagnostics(),
        }

    async def on_session_dispose(self, params: dict) -> dict:
        session = self._get_session(params)
        result = await self._terminate_session(
            session,
            signal=str(params.get("signal") or "TERM"),
            grace_ms=params.get("grace_ms"),
        )
        if params.get("forget", False):
            self._sessions.pop(session.session_ref, None)
        return {"ok": True, "disposed": True, **result}

    async def _spawn_session(
        self,
        request: CreateSessionRequest,
        extras: dict,
        *,
        resume_locator: str | None,
        checkpoint: Any,
    ) -> CliSession:
        session_ref = str(
            extras.get("session_ref") or request.session_ref_hint or uuid.uuid4()
        )
        if session_ref in self._sessions and self._sessions[session_ref].alive():
            raise AdapterError(
                ErrorCode.ADAPTER_BUSY,
                f"会话 {session_ref} 已存在且仍在运行",
                {"session_ref": session_ref},
            )

        prompt = extra_prompt(request, extras)
        if not request.session_ref_hint:
            # 让 harness 侧能采纳内核选定的会话标识（有 --session-id 这类参数的
            # harness 才能做到），这样 session_ref 与 persist_locator 一致，
            # 恢复会话时不必再猜。采纳不了（如 kimi）的 harness 会另外生成，
            # 由 persist_locator 如实回报。
            request = request.model_copy(update={"session_ref_hint": session_ref})
        argv = self.build_argv(
            request=request,
            prompt=prompt,
            resume_locator=resume_locator,
            checkpoint=checkpoint,
        )
        # argv[0] 按约定是 harness 可执行文件。在这里解析并校验它，而不是在
        # build_argv 里：构造参数列表是纯变换，不该取决于本机装没装这个 harness；
        # 而「装没装」在真正要拉起进程的这一刻才是必须回答的问题。
        # 顺带把裸名字换成绝对路径，保证拉起的就是刚刚校验过的那个文件。
        argv = list(argv)
        argv[0] = find_executable(request.harness.exec_path, self.exec_default)

        harness = request.harness
        env = self.build_env(request, extras)
        cwd = harness.cwd

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=(
                    asyncio.subprocess.PIPE
                    if self.input_channel
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=16 * 1024 * 1024,
            )
        except (OSError, ValueError) as exc:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"无法启动 harness：{type(exc).__name__}: {exc}",
                {"argv0": argv[0] if argv else "", "harness_family": self.manifest.harness_family},
            ) from exc

        session = CliSession(
            session_ref=session_ref,
            harness_id=harness.harness_id,
            persist_locator=resume_locator,
            proc=proc,
            label=f"{self.manifest.adapter_id}:{session_ref[:8]}",
            attempt_id=request.attempt_id,
            model_name=request.model_name or None,
            cwd=cwd,
            permission_mode=request.permission_mode,
            reasoning_effort=request.reasoning_effort,
            system_prompt=request.system_prompt,
            input_supported=self.input_channel,
        )
        self._sessions[session_ref] = session
        session.pump_task = asyncio.create_task(
            self._pump(session), name=f"{session.label}:stdout"
        )
        session.stderr_task = asyncio.create_task(
            self._drain_stderr(session), name=f"{session.label}:stderr"
        )

        # host 握手必须先于任何用户消息：harness 要靠它才知道「有人能回答我的
        # 权限提问」。不握手时它不是不问，而是**直接拒绝**——用户以为命令跑了。
        if self.input_channel:
            try:
                await self._send_initialize(session)
            except Exception as exc:  # noqa: BLE001 - 握手失败不该拖垮建会话
                await self._log_stray(
                    session, f"host 握手发送失败：{type(exc).__name__}: {exc}", level="warn"
                )

        if prompt is not None:
            try:
                if self.input_channel:
                    # 流式输入通道下首轮输入必须走 stdin：与 argv 同时给出会让
                    # harness 收到两遍同一条指令（claude 实测 -p <prompt> 与 stdin
                    # 帧都会被当成用户消息）。写完才算「已交付」。
                    await self.deliver_initial_input(session, prompt)
                # text 模式下 prompt 已经在 argv 里（build_argv 负责给出或报错），
                # 因此走到这里就是真的交付了。
                session.accepted_initial_input = True
            except Exception:
                # 交付失败就不能留一个没人管的子进程（RES-01）：先收掉再抛。
                with contextlib.suppress(Exception):
                    await self._terminate_session(
                        session, signal="TERM", grace_ms=None, emit=False
                    )
                raise

        await self.notify(
            NOTIFICATIONS.LOG,
            {
                "message": f"会话已启动 {session_ref[:8]}（pid={proc.pid}）",
                "session_ref": session_ref,
                "level": "info",
            },
        )
        await self.on_session_spawned(session)
        return session

    async def on_session_spawned(self, session: "CliSession") -> None:
        """子类钩子：子进程与台账都就绪后调用（watchdog 之类的会话级巡检在此起）。"""
        return None

    # ------------------------------------------------------------------
    # 子类钩子
    # ------------------------------------------------------------------

    def build_argv(
        self,
        *,
        request: CreateSessionRequest,
        prompt: str | None,
        resume_locator: str | None,
        checkpoint: Any,
    ) -> list[str]:
        raise NotImplementedError

    def build_env(self, request: CreateSessionRequest, extras: dict) -> dict[str, str]:
        """默认：把 HarnessConfig.env 与已解析凭据叠到本进程环境之上。

        凭据只以环境变量形式向内传给 harness 子进程，绝不进日志、不进事件、
        不进 summary（AUTH-02）。``credential_env_map()`` 由子类声明
        「本 harness 认哪些凭据键」，因为不同 harness 的环境变量名不同；
        不声明 = 不支持凭据注入，此时只依赖本机登录态。
        """
        env = {**os.environ, **request.harness.env}
        env.update(credential_env(request.harness, self.credential_env_map()))
        env.update(extra_env(request, extras))
        return env

    def credential_env_map(self) -> dict[str, str]:
        """凭据键 → 环境变量名的映射；空表示该 harness 不支持凭据注入。"""
        return {}

    # ------------------------------------------------------------------
    # host 控制通道（HUM-03：让 harness 的提问能到达用户）
    # ------------------------------------------------------------------

    #: 权限提问的兜底答复时限。harness 那边**没有 park 超时**，所以这条兜底是
    #: 「答复丢了」与「根本没有审批通道」两种情况的最后一道防线。
    permission_hook_timeout: float = 1800.0

    def _spawn_bg(self, coro: Any) -> None:
        """跑一个后台协程并保住强引用。

        asyncio 只持弱引用，不记下来就可能被 GC 掉——那种 bug 的表现是
        「偶尔不执行」，最难查。
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def initialize_request(self, session: CliSession) -> dict | None:
        """返回 ``initialize`` 控制帧的载荷；``None`` 表示本适配器不需要握手。

        需要这个握手的 harness，其权限提问会以 ``control_request`` 的形式回到
        我们这里（见 ``_handle_control_request``），而不是像没有 host 时那样
        **直接拒绝**。没有握手的后果不是「问不到」，是「静默拒绝」——
        用户以为命令跑了，其实一步都没执行。
        """
        return None

    async def _send_initialize(self, session: CliSession) -> None:
        payload = self.initialize_request(session)
        if payload is None:
            return
        request_id = f"init-{session.session_ref[:8]}"
        await self.send_control_request(session, request_id, payload)
        session.initialized = True

    async def send_control_request(
        self, session: CliSession, request_id: str, request: dict
    ) -> None:
        """往 harness 的 stdin 写一条控制帧。"""
        proc = session.proc
        if proc.stdin is None or proc.stdin.is_closing():
            raise AdapterError(
                ErrorCode.SESSION_DEAD, "会话的输入通道已关闭，无法发送控制帧"
            )
        frame = {"type": "control_request", "request_id": request_id, "request": request}
        proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    async def send_control_response(
        self, session: CliSession, request_id: str, response: dict
    ) -> None:
        proc = session.proc
        if proc.stdin is None or proc.stdin.is_closing():
            return
        frame = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": response},
        }
        proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    async def _handle_control_request(self, session: CliSession, obj: dict) -> None:
        """处理 harness 反向发来的控制帧。"""
        request = obj.get("request") or {}
        request_id = str(obj.get("request_id") or "")
        subtype = str(request.get("subtype") or "")

        if subtype == "hook_callback":
            await self._handle_hook_callback(session, request_id, request)
            return

        # 其它控制帧（如 harness 主动查询）如实记一笔，不假装处理过。
        await self._log_stray(
            session, f"未处理的控制帧 subtype={subtype} request_id={request_id}", level="warn"
        )

    async def _handle_control_response(self, session: CliSession, obj: dict) -> None:
        """harness 对我们发出的控制帧的答复。握手失败必须可见。"""
        response = obj.get("response") or {}
        if response.get("subtype") == "error" or obj.get("error"):
            await self._log_stray(
                session,
                f"host 握手被拒绝：{json.dumps(obj, ensure_ascii=False)[:300]}；"
                f"权限提问将不会被转达，harness 会自行拒绝这类操作",
                level="warn",
            )

    async def _handle_hook_callback(
        self, session: CliSession, request_id: str, request: dict
    ) -> None:
        """钩子回调。目前只认 ``PermissionRequest``（HUM-03）。

        子类可覆盖以支持更多钩子事件（例如 MCP 的 ``Elicitation``——那是
        agent 向用户要结构化输入的另一条路）。
        """
        payload = request.get("input") or {}
        event = str(payload.get("hook_event_name") or "")
        if event != "PermissionRequest":
            await self._log_stray(
                session, f"未处理的钩子事件 {event!r}（callback_id={request.get('callback_id')}）",
                level="warn",
            )
            return
        await self.on_permission_hook(session, request_id, payload)

    async def on_permission_hook(
        self, session: CliSession, request_id: str, payload: dict
    ) -> None:
        """把 harness 的权限提问转成内核认得的 ``PermissionRequest``。

        默认实现直接**拒绝**并说明原因——不静默放行，也不静默卡住。
        子类覆盖它来真正接上审批。
        """
        await self.send_control_response(
            session, request_id, {"behavior": "deny", "message": "本适配器未接线审批通道"}
        )

    def build_permission_request(
        self, session: CliSession, request_id: str, payload: dict
    ) -> PermissionRequest:
        """把 harness 的钩子载荷翻译成内核的审批实体。

        字段尽量取 harness 给的原文（``title`` 是它渲染好的整句话），
        而不是自己拿 tool_name + input 拼一句——那句原文里往往带着
        「为什么拦」和「会动哪个路径」，拼不出来的。
        """
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        summary = _describe_tool_input(tool_name, tool_input)

        return PermissionRequest(
            approval_id=f"ap-{uuid.uuid4().hex[:12]}",
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            action=summary,
            target=_target_of(tool_input),
            risk=str(payload.get("decision_reason") or payload.get("title") or "") or None,
            tool_name=tool_name or None,
            raw={
                "request_id": request_id,
                "tool_input": tool_input,
                "cwd": payload.get("cwd"),
                "permission_mode": payload.get("permission_mode"),
            },
        )

    async def on_permission_respond(self, params: dict) -> dict:
        """内核的审批决定 → 回到 harness 的控制帧（HUM-04）。

        ``approved`` 与否之外还要带 ```updatedInput``：用户「修改后批准」时，
        执行的必须是被改过的那份输入，否则「修改」只是个装饰。
        """
        approval_id = str(params.get("approval_id") or "")
        if not approval_id:
            raise AdapterError(ErrorCode.INVALID_PARAMS, "permission.respond 需要 approval_id")

        # 先按 approval_id 找到它在 harness 那边的 request_id。
        for session in self._sessions.values():
            request_id = session.pending_hooks.pop(approval_id, None)
            if request_id is None:
                continue
            decision = str(params.get("decision") or "").lower()
            approved = decision == "approve"
            inner: dict = {"behavior": "allow" if approved else "deny"}
            modified = params.get("modified_action")
            if approved and modified:
                inner["updatedInput"] = {"command": str(modified)}
            if not approved:
                inner["message"] = str(params.get("note") or "用户拒绝了该操作")

            # 钩子回调的答复必须包在 hookSpecificOutput 里并带上事件名——
            # 裸的 {"behavior": ...} 会被 harness 当成「没听懂」而退回自行拒绝，
            # 现象是「审批收到了、也批了、命令却没执行」。
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": inner,
                }
            }
            await self.send_control_response(session, request_id, response)
            await self.emit_event(
                EventKind.STATE_CHANGE,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={"state": "permission_answered", "approval_id": approval_id,
                      "decision": decision},
            )
            return {"ok": True, "approval_id": approval_id, "decision": decision}

        raise AdapterError(
            ErrorCode.INVALID_PARAMS,
            f"未知或已答复的 approval_id：{approval_id}（重复通知不构成再次授权）",
            {"approval_id": approval_id},
        )

    async def handle_json_line(self, session: CliSession, obj: dict) -> None:
        raise NotImplementedError

    async def on_stream_closed(self, session: CliSession, returncode: int | None) -> None:
        """子进程结束的默认处理：TURN_END（若该 harness 无显式轮次标记）+ SESSION_ENDED。

        非零退出码且没有正常结束时，额外上报 ERROR——「进程没了」必须可见，
        不能只留一个静默的 ended。
        """
        if self.turn_end_on_exit:
            await self.emit_event(
                EventKind.TURN_END,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={"exit_code": returncode, "implied_by": "process_exit"},
            )
        await self.emit_event(
            EventKind.SESSION_ENDED,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={
                "exit_code": returncode,
                "persist_locator": session.persist_locator,
                "lines_read": session.lines_read,
                # 只解释「为什么结束」，不改变结束本身：结束的判据始终是进程真的退了。
                "reason": session.end_reason or "process_exit",
            },
        )
        if returncode not in (0, None):
            error_class, error_kind = self.classify_crash(session, returncode)
            await self.emit_event(
                EventKind.ERROR,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=f"harness 进程异常退出（code={returncode}）",
                data={
                    "error_class": error_class,
                    "error_kind": error_kind,
                    "exit_code": returncode,
                    "stderr_tail": session.stderr_tail[-20:],
                },
            )

    def classify_crash(self, session: "CliSession", returncode: int | None) -> tuple[str, str]:
        """把「进程非正常退出」分类成 D-05 的重试决策输入。

        判据只有 stderr：认证/授权类失败是不可重试的（重试只会再失败一次，
        还会把账号打到限流），其余一律按可重试处理并交给上层退避。
        """
        if looks_like_auth_failure(session.stderr_tail):
            return "fatal_error", "auth"
        return "retryable_error", "harness_crash"

    # ------------------------------------------------------------------
    # 输入与打断
    # ------------------------------------------------------------------

    async def on_send_input(self, params: dict) -> dict:
        session = self._get_session(params)
        if not self.input_channel:
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                f"{self.manifest.display_name or self.manifest.adapter_id} 的会话在 "
                f"text 输入模式下以 argv 承载 prompt，运行中无法注入输入。"
                f"需要运行中交互时请以流式输入通道启动该适配器。",
                {"harness_family": self.manifest.harness_family, "mode": "text"},
            )
        kind = str(params.get("kind") or InputKind.USER)
        text = str(params.get("text") or "")
        await self.send_input(session, kind=kind, text=text)
        session.received_inputs.append({"kind": kind, "text": text})
        return {"ok": True, "delivered": True, "session_ref": session.session_ref, "kind": kind}

    async def send_input(self, session: CliSession, *, kind: str, text: str) -> None:
        """子类覆盖：把输入写进 harness 的输入通道。"""
        raise NotImplementedError

    async def deliver_initial_input(self, session: CliSession, prompt: str) -> None:
        """把**首轮**输入交给已经起来的 harness（仅流式输入通道下调用）。

        默认就是普通用户输入；需要区分「首轮」的适配器（例如首轮要带
        session_id 之类的元数据）可以覆盖。与 ``send_input`` 一样，抛异常
        就意味着**没送到**——调用方会据此不声明 accepted_initial_input。
        """
        await self.send_input(session, kind=InputKind.USER, text=prompt)

    async def on_interrupt(self, params: dict) -> dict:
        session = self._get_session(params)
        if not self.input_channel:
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                f"{self.manifest.display_name or self.manifest.adapter_id} 未启用流式输入通道，"
                f"无法向运行中的会话发送打断；如需停止请走 control.terminate"
                f"（§10.4 取消链）。",
                {"harness_family": self.manifest.harness_family},
            )
        await self.send_input(session, kind=InputKind.INTERRUPT, text="")
        return {"ok": True, "session_ref": session.session_ref, "interrupted": True}

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    async def on_terminate(self, params: dict) -> dict:
        session = self._get_session(params)
        result = await self._terminate_session(
            session,
            signal=str(params.get("signal") or "TERM"),
            grace_ms=params.get("grace_ms"),
        )
        return {"ok": True, **result}

    async def on_abort_stream(self, params: dict) -> dict:
        """中止远端流以停止继续计费（§10.4 的第一步）。

        本地 CLI harness 没有独立的远端流句柄：唯一能停止在途请求的动作就是
        终止该进程。因此这里发 SIGTERM 但**不等宽限期**——等待与升级留给
        随后的 ``control.terminate``，两者合起来仍是「软终止 → 宽限 → 硬杀」。
        """
        session = self._get_session(params)
        if not session.alive():
            return {"ok": True, "aborted": False, "reason": "会话已结束"}
        _signal_soft(session.proc)
        await self.notify(
            NOTIFICATIONS.LOG,
            {
                "message": f"abort_stream 等价于 SIGTERM（本地 CLI 无独立远端流句柄）",
                "session_ref": session.session_ref,
                "level": "info",
            },
        )
        return {"ok": True, "aborted": True, "equivalent_to": "terminate(SIGTERM)"}

    async def on_heartbeat(self, params: dict) -> dict:
        alive: list[str] = []
        for session in self._sessions.values():
            if session.alive():
                alive.append(session.session_ref)
                await self.emit_heartbeat(session.session_ref, alive=True)
            else:
                await self.emit_heartbeat(
                    session.session_ref,
                    alive=False,
                    detail=f"exit_code={session.proc.returncode}",
                )
        return {"ok": True, "alive": alive, "count": len(alive)}

    async def on_compact(self, params: dict) -> dict:
        raise AdapterError(
            ErrorCode.NOT_SUPPORTED,
            f"{self.manifest.display_name or self.manifest.adapter_id} 不提供运行时的上下文整理操作",
            {"harness_family": self.manifest.harness_family},
        )

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def on_shutdown(self) -> None:
        """适配器退出前收掉全部子进程（RES-01/02：清理只信台账）。

        这里不做无限等待：宽限期走完就升级为硬杀，并把收不掉的对象留在
        stderr 上可见，而不是假装清理完成（LIFE-06）。
        """
        for session in list(self._sessions.values()):
            if not session.alive():
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._terminate_session(
                        session,
                        signal="TERM",
                        grace_ms=self.terminate_grace_ms,
                        emit=True,
                    ),
                    timeout=(self.terminate_grace_ms + self.kill_wait_ms) / 1000 + 2,
                )
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        for session in self._sessions.values():
            for task in (session.pump_task, session.stderr_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_session(self, params: dict) -> CliSession:
        session_ref = params.get("session_ref")
        if not session_ref:
            raise AdapterError(ErrorCode.INVALID_PARAMS, "缺少 session_ref")
        session = self._sessions.get(str(session_ref))
        if session is None:
            raise AdapterError(
                ErrorCode.SESSION_NOT_FOUND,
                f"未知会话 {session_ref}",
                {"session_ref": str(session_ref), "known": list(self._sessions)},
            )
        return session

    async def _terminate_session(
        self,
        session: CliSession,
        *,
        signal: str,
        grace_ms: Any,
        emit: bool = True,
    ) -> dict:
        """两级终止（§10.4）。返回的字段如实描述**实际发生了什么**。

        ``forced=True`` 表示宽限期内没退出、被升级为 SIGKILL；
        ``reclaimed=False`` 表示连硬杀后都没能确认回收——此时会话置 lost，
        由上层交给 Reaper，而不是报告「已停止」。
        """
        proc = session.proc
        if proc.returncode is not None:
            session.state = "ended"
            return {
                "session_ref": session.session_ref,
                "already_dead": True,
                "forced": False,
                "reclaimed": True,
                "exit_code": proc.returncode,
            }

        grace = (
            float(grace_ms) / 1000 if grace_ms is not None else self.terminate_grace_ms / 1000
        )
        forced = signal.upper() == "KILL"

        if forced:
            _signal_hard(proc)
        else:
            _signal_soft(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                forced = True
                _signal_hard(proc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=self.kill_wait_ms / 1000)

        reclaimed = proc.returncode is not None
        session.exit_code = proc.returncode
        session.state = "ended" if reclaimed else "lost"
        session.ended_at = _now_iso()
        if session.end_reason is None:
            session.end_reason = "terminate"

        if emit:
            await self.emit_event(
                EventKind.STATE_CHANGE,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={
                    "state": session.state,
                    "reason": "terminate",
                    "forced": forced,
                    "exit_code": proc.returncode,
                },
            )
            if not reclaimed:
                await self.notify(
                    NOTIFICATIONS.LOG,
                    {
                        "message": f"会话 {session.session_ref[:8]} 在 SIGKILL 后仍未能确认回收，"
                        f"置为 lost 并移交 Reaper",
                        "session_ref": session.session_ref,
                        "level": "error",
                    },
                )
        return {
            "session_ref": session.session_ref,
            "already_dead": False,
            "forced": forced,
            "reclaimed": reclaimed,
            "exit_code": proc.returncode,
        }

    async def _pump(self, session: CliSession) -> None:
        """持续抽干子进程 stdout：每行要么是协议事件，要么被显式记一笔。"""
        assert session.proc.stdout is not None
        try:
            while True:
                try:
                    raw = await session.proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    session.parse_errors += 1
                    await self._log_stray(session, "单行超长，已丢弃", level="warn")
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                session.lines_read += 1
                await self._consume_line(session, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读取循环不能因单行异常而死
            session.parse_errors += 1
            await self.emit_event(
                EventKind.ERROR,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=f"解析 harness 输出时出错：{type(exc).__name__}: {exc}",
                data={"error_class": "retryable_error", "error_kind": "adapter_parse"},
            )
        finally:
            code = await session.proc.wait()
            session.exit_code = code
            if session.state != "lost":
                session.state = "ended"
            session.ended_at = _now_iso()
            with contextlib.suppress(Exception):
                await self.on_stream_closed(session, code)

    async def _consume_line(self, session: CliSession, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            session.non_json_lines += 1
            # 实测：claude 与 kimi 都会把诊断信息或子进程 stdout 混进协议流。
            # 这不是我们的解析 bug，但也不能静默吞掉——如实上报，限流。
            await self._log_stray(session, line, level="warn")
            return
        if not isinstance(obj, dict):
            session.non_json_lines += 1
            await self._log_stray(session, line, level="warn")
            return

        # 控制帧是 host 通道，属于适配层共有的事务，不该由各 harness 的
        # handle_json_line 各写一遍——写漏的那家会让权限提问静默消失。
        kind = obj.get("type")
        if kind == "control_request":
            await self._handle_control_request(session, obj)
            return
        if kind == "control_response":
            await self._handle_control_response(session, obj)
            return

        await self.handle_json_line(session, obj)

    async def _log_stray(self, session: CliSession, text: str, *, level: str) -> None:
        if (
            session.non_json_lines + sum(session.unknown_line_types.values())
            > self.max_reported_stray_lines
        ):
            return
        await self.notify(
            NOTIFICATIONS.LOG,
            {
                "message": f"harness 产生了非事件行（已跳过，原样保留供排查）：{text[:200]}",
                "session_ref": session.session_ref,
                "level": level,
            },
        )

    async def note_unknown_type(self, session: CliSession, type_name: str) -> None:
        """记录一个我们还不认识的行类型。

        harness 升级会带来新的行类型（§8.3）。认识不到就先如实记一笔，
        别当作不存在——否则「输出变少了」会被误判成「任务没做事」。
        """
        count = session.unknown_line_types.get(type_name, 0) + 1
        session.unknown_line_types[type_name] = count
        if count == 1 and len(session.unknown_line_types) <= self.max_reported_stray_lines:
            await self.notify(
                NOTIFICATIONS.LOG,
                {
                    "message": f"遇到未识别的 harness 行类型 {type_name}（首次出现，已按元数据跳过）",
                    "session_ref": session.session_ref,
                    "level": "warn",
                },
            )

    async def _drain_stderr(self, session: CliSession) -> None:
        """必须持续抽干 stderr，否则管道写满会让 harness 子进程阻塞。"""
        assert session.proc.stderr is not None
        try:
            while True:
                raw = await session.proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                session.stderr_tail.append(text)
                if len(session.stderr_tail) > 50:
                    del session.stderr_tail[:25]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stderr 抽干失败不影响主流程
            return


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def extra_prompt(request: CreateSessionRequest, extras: dict) -> str | None:
    """取出本轮要执行的 prompt。

    ``CreateSessionRequest`` 描述的是「怎么开会话」，不含本轮输入；本轮输入
    由内核放在 ``extra.prompt``（或 ``extra.input``）。取不到时返回 None，
    由适配器决定是报错还是走流式输入通道。
    """
    for key in ("prompt", "input", "user_input"):
        value = extras.get(key)
        if value is not None:
            return str(value)
    extra = request.extra or {}
    for key in ("prompt", "input", "user_input"):
        value = extra.get(key)
        if value is not None:
            return str(value)
    return None


def extra_env(request: CreateSessionRequest, extras: dict) -> dict[str, str]:
    values = extras.get("env") or request.extra.get("env") or {}
    if not isinstance(values, dict):
        raise AdapterError(ErrorCode.INVALID_PARAMS, "env 必须是字符串到字符串的映射")
    return {str(k): str(v) for k, v in values.items()}


def credential_env(harness: HarnessConfig, mapping: dict[str, str]) -> dict[str, str]:
    """把已解析的凭据映射成 harness 子进程的环境变量。

    ``credential`` 是**明文**，只在此处向内传给子进程：返回值不写日志、
    不进事件、不进 summary（AUTH-02）。不在 ``mapping`` 里的键一律丢弃，
    避免把内核的内部字段顺手递给 harness。
    """
    cred = harness.credential
    if not cred or not mapping:
        return {}
    return {
        env_name: str(cred[key])
        for key, env_name in mapping.items()
        if key in cred and cred[key]
    }


#: stderr 里出现这些片段时，把失败判为不可重试的认证问题（D-05）。
AUTH_FAILURE_HINTS = (
    "401",
    "403",
    "unauthorized",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "not logged in",
    "login required",
    "permission denied",
    "credential",
)


def looks_like_auth_failure(stderr_lines: Sequence[str]) -> bool:
    blob = "\n".join(stderr_lines[-30:]).lower()
    return any(hint in blob for hint in AUTH_FAILURE_HINTS)


def _signal_soft(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.terminate()


def _signal_hard(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.kill()


# ---------------------------------------------------------------------------
# 钩子载荷 → 审批实体的翻译
# ---------------------------------------------------------------------------

#: 各工具里最能代表「这次要动什么」的字段，按优先级取第一个存在的。
_TARGET_KEYS = (
    "file_path", "path", "notebook_path", "url", "command", "pattern", "query",
)


def _describe_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """把工具调用压成一行人类可读的动作描述。

    这里刻意**不**追求完整还原——完整内容随 ``raw`` 一起给前端，用户能展开看。
    这一行只用于列表与标题，要的是「一眼知道它要干嘛」。
    """
    if not tool_input:
        return tool_name or "未知操作"
    for key in _TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("\n", " ")
            if len(text) > 160:
                text = text[:157] + "…"
            return f"{tool_name}({text})" if tool_name else text
    # 没命中已知字段：给出键名而不是空字符串，至少让人知道它带了参数。
    keys = ", ".join(sorted(tool_input)[:6])
    return f"{tool_name}({keys})" if tool_name else keys


def _target_of(tool_input: dict[str, Any]) -> str | None:
    """取「这次动作作用在什么上」。取不到返回 None，不编造。"""
    for key in _TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return None
