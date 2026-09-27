"""适配器 SDK：子进程侧的 JSON-RPC 事件循环（架构设计 v0.02 §8.1）。

适配器作者继承 ``AdapterBase``，实现自己关心的 ``on_*`` 协程，然后
``asyncio.run(MyAdapter().run())`` 即可。

两个容易踩的坑，SDK 替作者挡掉：

1. **stdout 是协议通道。** 任何 stray ``print()`` 都会污染协议。``run()`` 启动时把
   ``sys.stdout`` 重定向到 stderr，协议只用启动时捕获的那个原始终端流。
2. **stdin 不能直接 await。** 同步 ``readline()`` 会阻塞事件循环，而
   ``asyncio.to_thread`` 又无法取消。这里用一条后台线程推进行队列，
   由事件循环消费——线程只有一个，退出时可靠收束。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import traceback
from typing import Any, Awaitable, Callable

from .contract import (
    AdapterEvent,
    AdapterManifest,
    Heartbeat,
    InputKind,
    PermissionRequest,
)
from .protocol import PROTOCOL_VERSION, AdapterError, ErrorCode, METHODS, NOTIFICATIONS

__all__ = ["AdapterBase", "run_adapter"]


class AdapterBase:
    """适配器基类。子类实现 ``on_*``；未实现的返回 NOT_SUPPORTED。"""

    #: 子类必须提供。
    manifest: AdapterManifest

    def __init__(self) -> None:
        self._out: Any = None
        self._write_lock = asyncio.Lock()
        self._seq = 0
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """事件循环。适配器进程的入口。"""
        self._out = sys.stdout
        # 协议独占真实 stdout；此后任何 print 都进 stderr，不会污染协议。
        sys.stdout = sys.stderr

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _reader() -> None:
            try:
                for line in sys.stdin:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception:  # pragma: no cover - stdin 异常关闭
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_reader, name="adapter-stdin", daemon=True).start()

        try:
            while not self._stopping.is_set():
                line = await queue.get()
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                await self._handle_line(line)
        finally:
            await self.on_shutdown()

    async def on_shutdown(self) -> None:
        """子类可覆盖：清理自己拉起的会话与子进程。"""

    async def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------
    # 协议收发
    # ------------------------------------------------------------------

    async def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            await self._send_error(None, ErrorCode.PARSE_ERROR, "无法解析的 JSON")
            return

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            await self._send_error(msg.get("id"), ErrorCode.INVALID_REQUEST, "非 JSON-RPC 2.0")
            return

        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if not isinstance(method, str):
            await self._send_error(req_id, ErrorCode.INVALID_REQUEST, "缺少 method")
            return

        try:
            result = await self._dispatch(method, params)
        except AdapterError as exc:
            await self._send_error(req_id, exc.code, exc.message, exc.data)
        except NotImplementedError:
            await self._send_error(
                req_id,
                ErrorCode.NOT_SUPPORTED,
                f"该适配器未实现 {method}",
                {"method": method, "harness_family": self.manifest.harness_family},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 适配器边界，任何异常都要变成可读回包
            await self._send_error(
                req_id,
                ErrorCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc()[-2000:]},
            )
        else:
            await self._send_result(req_id, result)

    async def _dispatch(self, method: str, params: dict) -> Any:
        handlers: dict[str, Callable[[dict], Awaitable[Any]]] = {
            METHODS.HANDSHAKE: self.on_handshake,
            METHODS.DECLARE: self.on_declare,
            METHODS.PROBE: self.on_probe,
            METHODS.COMPAT_CHECK: self.on_compat_check,
            METHODS.HEARTBEAT: self.on_heartbeat,
            METHODS.SESSION_CREATE: self.on_session_create,
            METHODS.SESSION_RESUME: self.on_session_resume,
            METHODS.SESSION_DISPOSE: self.on_session_dispose,
            METHODS.SESSION_LIST: self.on_session_list,
            METHODS.SESSION_STAT: self.on_session_stat,
            METHODS.SEND_INPUT: self.on_send_input,
            METHODS.READ_OUTPUT: self.on_read_output,
            METHODS.PERMISSION_RESPOND: self.on_permission_respond,
            METHODS.SUBSCRIBE: self.on_subscribe,
            METHODS.UNSUBSCRIBE: self.on_unsubscribe,
            METHODS.INTERRUPT: self.on_interrupt,
            METHODS.TERMINATE: self.on_terminate,
            METHODS.PAUSE: self.on_pause,
            METHODS.CHECKPOINT: self.on_checkpoint,
            METHODS.ABORT_STREAM: self.on_abort_stream,
            METHODS.COMPACT: self.on_compact,
        }
        handler = handlers.get(method)
        if handler is None:
            raise AdapterError(ErrorCode.METHOD_NOT_FOUND, f"未知方法: {method}")
        return await handler(params)

    async def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        async with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    async def _send_result(self, req_id: Any, result: Any) -> None:
        if req_id is None:
            return  # 通知不需要回包
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send_error(
        self, req_id: Any, code: int, message: str, data: dict | None = None
    ) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data:
            err["data"] = data
        await self._send({"jsonrpc": "2.0", "id": req_id, "error": err})

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------
    # 主动上报
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def emit_event(
        self,
        kind: str,
        *,
        session_ref: str | None = None,
        attempt_id: str | None = None,
        text: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        from .contract import AdapterEvent as _E

        event = _E(
            kind=kind,
            session_ref=session_ref,
            attempt_id=attempt_id,
            seq=self._next_seq(),
            text=text,
            data=data or {},
        )
        await self.notify(NOTIFICATIONS.EVENT, event.model_dump(mode="json"))

    async def emit_permission_request(self, request: PermissionRequest) -> None:
        await self.notify(NOTIFICATIONS.PERMISSION_REQUEST, request.model_dump(mode="json"))

    async def emit_heartbeat(self, session_ref: str, *, alive: bool = True,
                             detail: str | None = None) -> None:
        from ..sdk.protocol import NOTIFICATIONS as _N

        hb = Heartbeat(
            session_ref=session_ref,
            ts=_now_iso(),
            alive=alive,
            detail=detail,
        )
        await self.notify(_N.HEARTBEAT, hb.model_dump(mode="json"))

    # ------------------------------------------------------------------
    # 默认处理器：未实现即 NOT_SUPPORTED，绝不静默降级
    # ------------------------------------------------------------------

    async def on_handshake(self, params: dict) -> dict:
        from .protocol import PROTOCOL_MAJOR

        core_version = str(params.get("protocol_version", ""))
        core_major = core_version.split(".")[0] if core_version else ""
        if core_major and core_major != PROTOCOL_MAJOR:
            raise AdapterError(
                ErrorCode.PROTOCOL_MISMATCH,
                f"协议主版本不兼容：适配器 {PROTOCOL_VERSION}，内核 {core_version}",
                {"adapter_protocol": PROTOCOL_VERSION, "core_protocol": core_version},
            )
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "adapter_id": self.manifest.adapter_id,
            "adapter_version": self.manifest.version,
            "harness_family": self.manifest.harness_family,
            "capabilities": self.manifest.capabilities.model_dump(mode="json"),
        }

    async def on_declare(self, params: dict) -> dict:
        return self.manifest.model_dump(mode="json")

    async def on_probe(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_compat_check(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_heartbeat(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_session_create(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_session_resume(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_session_dispose(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_session_list(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_session_stat(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_send_input(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_read_output(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_permission_respond(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_subscribe(self, params: dict) -> dict:
        # 事件流默认已通过 emit_event 主动推送，订阅是可选优化。
        return {"ok": True}

    async def on_unsubscribe(self, params: dict) -> dict:
        return {"ok": True}

    async def on_interrupt(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_terminate(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_pause(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_checkpoint(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_abort_stream(self, params: dict) -> dict:
        raise NotImplementedError

    async def on_compact(self, params: dict) -> dict:
        raise NotImplementedError


def _now_iso() -> str:
    from ...core.domain.base import utcnow

    return utcnow().isoformat()


def run_adapter(adapter: AdapterBase) -> None:
    """适配器进程入口的便捷包装。"""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(adapter.run())
