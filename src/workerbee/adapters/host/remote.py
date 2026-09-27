"""`SupervisorClient`：把 supervisor 当作 HarnessPort 使用（架构设计 v0.02 §3）。

内核通过它访问会话，而不是自己拉起 harness 子进程。因此 core 重启时，
harness 子进程与它们的会话**不受影响**——这正是这款软件要解决的问题本身。

两条正确性要点：

1. **重连要补齐事件。** 连接断了之后重新连上时，必须按上次收到的 ``seq`` 请求
   `session.events` 拉回断连期间的事件。补齐不了时（缓冲已丢）**不能假装补齐**，
   而要把该会话标记为需要核对——「不知道」与「没有」是两回事（REC-04）。
2. **存活判定以 supervisor 为准。** 查不到一律按不存活处理并由上层走对账，
   不乐观猜测。LIFE-02 明确禁止把仍在跑的说成已停，反过来也一样。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...core.runtime.ports import SessionCaps, SessionHandle
from ..sdk.contract import AdapterEvent
from ..sdk.protocol import AdapterError, ErrorCode
from ...supervisor.protocol import (
    METHODS,
    NOTIFICATIONS,
    SupervisorError,
)
from ...supervisor.protocol import ErrorCode as SupervisorErrorCode

__all__ = ["SupervisorClient"]

EventCallback = Callable[[AdapterEvent], Awaitable[None]]
DiedCallback = Callable[[str, str], Awaitable[None]]


class SupervisorClient:
    """以 HarnessPort 的形状访问 supervisor。"""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        on_event: EventCallback | None = None,
        on_session_died: DiedCallback | None = None,
        on_log: Callable[[str], None] | None = None,
        registration_provider: Any | None = None,
        call_timeout: float = 60.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.on_event = on_event
        self.on_session_died = on_session_died
        self.on_log = on_log
        #: 由组合根注入：harness_id → HarnessRegistration。
        #: supervisor 刻意不读 core 的数据库，所以「怎么启动这个 harness」
        #: 必须由 core 显式告诉它。
        self.registration_provider = registration_provider
        self.call_timeout = call_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._seq_seen: dict[str, int] = {}
        self.needs_reconcile: set[str] = set()
        """事件缓冲丢掉过、无法保证状态完整的会话。上层必须对它们走对账。"""

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        self._reader, self._writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        # 先起读循环再发 hello：回包是读循环喂给 pending future 的，
        # 顺序反了就永远等不到回包——连接看着是通的，实际什么都收不到。
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            hello = await self.call(METHODS.HELLO, {"client": "workerbee-core"})
        except Exception:
            # 握手没成：把这条半开的通道收干净，否则 connected 会一直为真，
            # ensure_connected 重试时会直接「成功」返回（假连接比连不上更糟）。
            task, self._reader_task = self._reader_task, None
            if task is not None:
                task.cancel()
            with contextlib.suppress(Exception):
                self._writer.close()
            self._reader = None
            self._writer = None
            raise
        self._log(f"已连接 supervisor (pid={hello.get('pid')})")

        # 重连：把断连期间的事件补回来。补不上就把会话标为需核对。
        for session_ref in hello.get("sessions", []) or []:
            await self._catch_up(session_ref)

    async def ensure_connected(self, *, attempts: int = 5, delay: float = 0.5) -> None:
        """带退避的连接尝试。连不上就如实抛出——不静默降级成本地拉进程。"""
        last: Exception | None = None
        for i in range(attempts):
            try:
                await self.connect()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(delay * (i + 1))
        raise AdapterError(
            ErrorCode.HARNESS_UNAVAILABLE,
            f"无法连接 supervisor（{self.socket_path}）：{last}",
        )

    async def close(self) -> None:
        self._closing = True
        for task in (self._reader_task, self._reconnect_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._writer = None
        self._reader = None
        self._fail_pending(AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "连接已关闭"))

    async def _catch_up(self, session_ref: str) -> None:
        after = self._seq_seen.get(session_ref, 0)
        try:
            result = await self.call(
                METHODS.SESSION_EVENTS, {"session_ref": session_ref, "after_seq": after}
            )
        except SupervisorError as exc:
            if exc.code == SupervisorErrorCode.EVENTS_GONE:
                # 事件流有缺口，我们无法确定这个会话的完整历史。
                # 标出来让上层走对账，而不是拿残缺的事件流当完整事实。
                self.needs_reconcile.add(session_ref)
                self._log(f"会话 {session_ref} 的事件缓冲已丢，需对账")
                return
            raise
        if result.get("gap"):
            self.needs_reconcile.add(session_ref)
        for item in result.get("events", []):
            await self._dispatch_event(item)

    # ------------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------------

    async def call(self, method: str, params: dict | None = None, *, timeout: float | None = None):
        if self._writer is None:
            raise AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "尚未连接 supervisor")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        line = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        async with self._write_lock:
            try:
                self._writer.write((line + "\n").encode())
                await self._writer.drain()
            except Exception as exc:  # noqa: BLE001
                self._pending.pop(req_id, None)
                raise AdapterError(
                    ErrorCode.HARNESS_UNAVAILABLE, f"supervisor 通道断开：{exc}"
                ) from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout or self.call_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise AdapterError(
                ErrorCode.INTERNAL_ERROR, f"supervisor 调用超时（{method}）"
            ) from exc

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            try:
                raw = await self._reader.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") is not None:
                fut = self._pending.pop(msg["id"], None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(
                        SupervisorError(
                            int(err.get("code", ErrorCode.INTERNAL_ERROR)),
                            str(err.get("message", "未知错误")),
                            err.get("data"),
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))
            else:
                await self._handle_notification(msg.get("method"), msg.get("params") or {})

        # 连接断了：在途请求全部失败，并尝试重连
        self._fail_pending(
            AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "supervisor 连接中断")
        )
        if not self._closing and self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(1.0)
                try:
                    self._writer = None
                    await self.connect()
                    self._log("已重连 supervisor")
                    return
                except Exception:  # noqa: BLE001
                    continue
        finally:
            self._reconnect_task = None

    async def _handle_notification(self, method: str | None, params: dict) -> None:
        if method == NOTIFICATIONS.EVENT:
            await self._dispatch_event(params)
        elif method == NOTIFICATIONS.SESSION_DIED:
            session_ref = params.get("session_ref", "")
            if self.on_session_died is not None:
                with contextlib.suppress(Exception):
                    await self.on_session_died(session_ref, params.get("reason", ""))
        elif method == NOTIFICATIONS.HARNESS_DIED:
            self._log(f"harness 进程退出：{params}")
        elif method == NOTIFICATIONS.HEARTBEAT:
            self._seq_seen.setdefault(params.get("session_ref", ""), 0)

    async def _dispatch_event(self, item: dict) -> None:
        session_ref = item.get("session_ref") or ""
        seq = item.get("seq")
        if isinstance(seq, int):
            self._seq_seen[session_ref] = max(self._seq_seen.get(session_ref, 0), seq)

        kind = item.get("kind")
        if kind == "permission_request":
            # 权限请求走 on_event 之外的通道由组合根处理；这里转成事件的形式
            # 保持单一入口，具体归属由 core 决定（它才知道 attempt 是谁的）。
            pass

        if self.on_event is not None:
            event = AdapterEvent(
                kind=str(kind),
                session_ref=session_ref or None,
                attempt_id=item.get("attempt_id"),
                seq=seq,
                text=item.get("text"),
                data=dict(item.get("data") or {}),
            )
            with contextlib.suppress(Exception):
                await self.on_event(event)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    # ------------------------------------------------------------------
    # HarnessPort
    # ------------------------------------------------------------------

    async def create_session(
        self, *, harness_id: str, attempt: Any, stage: Any, model_name: str,
        reasoning_effort: str | None, system_prompt: str | None,
        initial_input: str | None = None,
        permission_mode: str | None = None,
        cwd: str | None = None, extra: dict | None = None,
    ) -> SessionHandle:
        await self._ensure_harness_ready(harness_id)
        result = await self.call(
            METHODS.SESSION_CREATE,
            {
                "harness_id": harness_id,
                "attempt_id": attempt.attempt_id,
                "stage_id": stage.stage_id,
                "task_id": stage.task_id,
                "node_id": stage.node_id,
                "node_name": stage.node_name,
                "attempt_seq": attempt.attempt_seq,
                "profile_id": attempt.profile_id,
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
                "system_prompt": system_prompt,
                "initial_input": initial_input,
                "permission_mode": permission_mode,
                "cwd": cwd,
                "extra": extra,
            },
            timeout=180.0,
        )
        return SessionHandle(
            session_ref=result["session_ref"],
            harness_id=result.get("harness_id", harness_id),
            state=result.get("state", "alive"),
            persist_locator=result.get("persist_locator"),
            pid=result.get("pid"),
            model_name=result.get("model_name"),
            used_resume=bool(result.get("used_resume")),
            accepted_initial_input=bool(result.get("accepted_initial_input")),
            detail=result.get("detail"),
        )

    async def resume_session(
        self, *, harness_id: str, persist_locator: str, attempt: Any, stage: Any
    ) -> SessionHandle:
        result = await self.call(
            METHODS.SESSION_RESUME,
            {
                "harness_id": harness_id,
                "persist_locator": persist_locator,
                "attempt_id": attempt.attempt_id,
                "stage_id": stage.stage_id,
                "task_id": stage.task_id,
                "node_id": stage.node_id,
                "attempt_seq": attempt.attempt_seq,
                "profile_id": attempt.profile_id,
            },
            timeout=180.0,
        )
        return SessionHandle(
            session_ref=result["session_ref"],
            harness_id=result.get("harness_id", harness_id),
            persist_locator=result.get("persist_locator"),
            pid=result.get("pid"),
            used_resume=True,
        )

    async def send_input(self, session_ref: str, text: str, *, kind: str = "user") -> bool:
        result = await self.call(
            METHODS.SESSION_SEND, {"session_ref": session_ref, "text": text, "kind": kind}
        )
        return bool(result.get("ok"))

    async def interrupt(self, session_ref: str) -> bool:
        return bool((await self.call(METHODS.SESSION_INTERRUPT, {"session_ref": session_ref})).get("ok"))

    async def terminate(self, session_ref: str, *, signal: str = "TERM") -> bool:
        return bool(
            (
                await self.call(
                    METHODS.SESSION_TERMINATE,
                    {"session_ref": session_ref, "signal": signal},
                    timeout=30.0,
                )
            ).get("ok")
        )

    async def abort_stream(self, session_ref: str) -> None:
        with contextlib.suppress(Exception):
            await self.call(METHODS.SESSION_ABORT_STREAM, {"session_ref": session_ref})

    async def pause(self, session_ref: str) -> bool:
        result = await self.call(METHODS.SESSION_PAUSE, {"session_ref": session_ref})
        return bool(result.get("ok"))

    async def checkpoint(self, session_ref: str) -> str | None:
        result = await self.call(METHODS.SESSION_CHECKPOINT, {"session_ref": session_ref})
        return result.get("checkpoint")

    async def compact(self, session_ref: str, threshold: int | None) -> dict[str, Any]:
        return await self.call(
            METHODS.SESSION_COMPACT, {"session_ref": session_ref, "threshold": threshold}
        )

    async def session_alive(self, session_ref: str) -> bool:
        try:
            result = await self.call(
                METHODS.SESSION_ALIVE, {"session_ref": session_ref}, timeout=15.0
            )
        except Exception:  # noqa: BLE001 - 查不到就是不存活，由上层对账
            return False
        return bool(result.get("alive"))

    async def capabilities(self, harness_id: str) -> SessionCaps:
        try:
            result = await self.call(METHODS.HARNESS_CAPABILITIES, {"harness_id": harness_id})
        except Exception:  # noqa: BLE001
            return SessionCaps()
        return SessionCaps.from_mapping(result)

    async def dispose(self, session_ref: str) -> None:
        with contextlib.suppress(Exception):
            await self.call(METHODS.SESSION_DISPOSE, {"session_ref": session_ref})

    # ------------------------------------------------------------------
    # supervisor 专有
    # ------------------------------------------------------------------

    async def ensure_harness(
        self, harness_id: str, *, adapter_id: str | None = None,
        exec_path: str | None = None, env: dict | None = None, cwd: str | None = None,
    ) -> bool:
        result = await self.call(
            METHODS.HARNESS_ENSURE,
            {
                "harness_id": harness_id,
                "adapter_id": adapter_id,
                "exec_path": exec_path,
                "env": env,
                "cwd": cwd,
            },
        )
        return bool(result.get("ok"))

    async def status(self) -> dict[str, Any]:
        return await self.call(METHODS.STATUS, {})

    async def list_sessions(self) -> list[dict[str, Any]]:
        """会话台账（session.list）。supervisor 才是会话的持有者，因此它才是权威。

        查不到时**不返回空列表**：空列表会被读成「没有会话」，而事实是「不知道」。
        异常如实上抛，由调用方决定怎么呈现（本项目的纪律：不把不知道伪装成没有）。
        """
        result = await self.call(METHODS.SESSION_LIST, {})
        return list(result.get("sessions", []) or [])

    async def _ensure_harness_ready(self, harness_id: str) -> None:
        """按需让 supervisor 拉起该 harness 的适配器。

        每次都调一次是有意的：它幂等，而「以为已经起来了」在实际崩溃后
        会让下一次派发直接失败。让 supervisor 去判断「要不要启」比我们猜更可靠。
        """
        if self.registration_provider is None:
            return
        reg = await self.registration_provider(harness_id)
        if reg is None:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness 未登记：{harness_id}",
                {"harness_id": harness_id},
            )
        await self.ensure_harness(
            harness_id,
            adapter_id=reg.adapter_id,
            exec_path=reg.exec_path,
            env=dict(reg.env_template or {}),
            cwd=reg.cwd,
        )
