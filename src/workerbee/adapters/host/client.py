"""Adapter Host：拉起适配器子进程并与之通信（架构设计 v0.02 §3、§8.1）。

崩溃隔离是这里存在的理由：第三方适配器崩溃不得拖垮内核。因此
- 子进程的 stdout/stderr 都被独立任务持续抽干（否则管道写满会让孩子阻塞）；
- 子进程退出时把全部在途请求以明确错误结束，并触发 ``on_exit`` 让上层走对账；
- 「进程还在」不等于「harness 可用」，存活判定以心跳与 handshake 为准（HAR-03）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..sdk.contract import AdapterEvent, AdapterManifest, Heartbeat, PermissionRequest
from ..sdk.protocol import (
    PROTOCOL_VERSION,
    AdapterError,
    ErrorCode,
    JsonRpcError,
    METHODS,
    NOTIFICATIONS,
)

__all__ = ["AdapterProcess", "AdapterSpawnError"]

EventHandler = Callable[[AdapterEvent], Awaitable[None]]
PermissionHandler = Callable[[PermissionRequest], Awaitable[None]]
HeartbeatHandler = Callable[[Heartbeat], Awaitable[None]]
LogHandler = Callable[[str], None]
ExitHandler = Callable[[int | None, str], Awaitable[None]]


class AdapterSpawnError(RuntimeError):
    pass


class AdapterProcess:
    """一个适配器子进程的会话。"""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        label: str,
    ) -> None:
        self.proc = proc
        self.label = label
        self.manifest: AdapterManifest | None = None

        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._closing = False
        self.exit_code: int | None = None

        self.on_event: EventHandler | None = None
        self.on_permission: PermissionHandler | None = None
        self.on_heartbeat: HeartbeatHandler | None = None
        self.on_log: LogHandler | None = None
        self.on_exit: ExitHandler | None = None

        self.stderr_tail: list[str] = []

    # ------------------------------------------------------------------
    # 启动与握手
    # ------------------------------------------------------------------

    @classmethod
    async def start(
        cls,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        label: str | None = None,
        handshake: bool = True,
    ) -> "AdapterProcess":
        """拉起适配器子进程并完成握手。

        ``command`` 里的第一项会被 ``shutil.which`` 解析一次，以便给出
        「找不到可执行文件」这种可读错误，而不是一个裸 FileNotFoundError。
        """
        resolved = list(command)
        if resolved and not os.path.isabs(resolved[0]) and os.sep not in resolved[0]:
            found = shutil.which(resolved[0])
            if found is None:
                raise AdapterSpawnError(
                    f"找不到适配器可执行文件：{resolved[0]}。"
                    f"请检查 PATH 或在该适配器的配置里指定绝对路径。"
                )
            resolved[0] = found

        full_env = {**os.environ, **(env or {})}
        pythonpath = str(Path(__file__).resolve().parents[3])
        existing = full_env.get("PYTHONPATH", "")
        if pythonpath not in existing.split(os.pathsep):
            full_env["PYTHONPATH"] = (
                f"{pythonpath}{os.pathsep}{existing}" if existing else pythonpath
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *resolved,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
                cwd=cwd,
                limit=16 * 1024 * 1024,  # 单行可能很长（大段输出被转义后）
            )
        except OSError as exc:
            raise AdapterSpawnError(f"无法启动适配器 {resolved[0]}: {exc}") from exc

        self = cls(proc, label=label or resolved[0])
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"{self.label}:stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"{self.label}:stderr")
        self._wait_task = asyncio.create_task(self._watch_exit(), name=f"{self.label}:exit")

        if handshake:
            await self.handshake()
        return self

    async def handshake(self, *, timeout: float = 15.0) -> AdapterManifest:
        result = await self.call(
            METHODS.HANDSHAKE,
            {"protocol_version": PROTOCOL_VERSION, "core": "workerbee"},
            timeout=timeout,
        )
        manifest = await self.call(METHODS.DECLARE, {}, timeout=timeout)
        self.manifest = AdapterManifest.model_validate(manifest)
        if manifest.get("protocol_version") and (
            str(manifest["protocol_version"]).split(".")[0]
            != PROTOCOL_VERSION.split(".")[0]
        ):
            raise AdapterSpawnError(
                f"适配器 {self.label} 协议不兼容："
                f"{manifest['protocol_version']} vs {PROTOCOL_VERSION}"
            )
        return self.manifest

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    async def call(
        self, method: str, params: dict | None = None, *, timeout: float = 60.0
    ) -> Any:
        if self.proc.returncode is not None:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"适配器进程已退出（code={self.proc.returncode}）：{self.label}",
            )
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        await self._write(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        )

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise AdapterError(
                ErrorCode.INTERNAL_ERROR,
                f"适配器调用超时（{timeout}s）: {method}",
                {"method": method, "timeout": timeout},
            ) from exc

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _write(self, payload: dict) -> None:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            raise AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "适配器 stdin 已关闭")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        async with self._write_lock:
            try:
                self.proc.stdin.write((line + "\n").encode())
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AdapterError(
                    ErrorCode.HARNESS_UNAVAILABLE, f"适配器通道已断开: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # 读取循环
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                self._log(f"适配器输出单行超长，已丢弃：{exc}")
                continue
            except asyncio.CancelledError:
                raise
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 适配器违反了「stdout 只走协议」的约定。如实报出，不静默吞掉。
                self._log(f"适配器产生了非协议输出（已忽略）：{line[:200]}")
                continue
            await self._dispatch_message(msg)

    async def _dispatch_message(self, msg: dict) -> None:
        if "id" in msg and msg["id"] is not None:
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(
                    JsonRpcError(
                        int(err.get("code", ErrorCode.INTERNAL_ERROR)),
                        str(err.get("message", "未知错误")),
                        err.get("data"),
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            await self._handle_notification(method, params)
        except Exception as exc:  # noqa: BLE001 - 回调异常不得杀死读取循环
            self._log(f"处理适配器通知 {method} 时出错：{type(exc).__name__}: {exc}")

    async def _handle_notification(self, method: str, params: dict) -> None:
        if method == NOTIFICATIONS.EVENT and self.on_event is not None:
            await self.on_event(AdapterEvent.model_validate(params))
        elif method == NOTIFICATIONS.PERMISSION_REQUEST and self.on_permission is not None:
            await self.on_permission(PermissionRequest.model_validate(params))
        elif method == NOTIFICATIONS.HEARTBEAT and self.on_heartbeat is not None:
            await self.on_heartbeat(Heartbeat.model_validate(params))
        elif method == NOTIFICATIONS.LOG:
            self._log(str(params.get("message", "")))
        else:
            self._log(f"未处理的通知类型：{method}")

    async def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        while True:
            try:
                raw = await self.proc.stderr.readline()
            except asyncio.CancelledError:
                raise
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self.stderr_tail.append(text)
            if len(self.stderr_tail) > 200:
                del self.stderr_tail[:100]
            self._log(f"[stderr] {text}")

    async def _watch_exit(self) -> None:
        try:
            code = await self.proc.wait()
        except asyncio.CancelledError:
            raise
        self.exit_code = code
        self._fail_pending(
            AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"适配器进程退出（code={code}）：{self.label}",
            )
        )
        if self.on_exit is not None and not self._closing:
            with contextlib.suppress(Exception):
                await self.on_exit(code, "\n".join(self.stderr_tail[-20:]))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    async def close(self, *, grace: float = 5.0) -> None:
        """优雅关闭；超时升级为 kill。

        与 §10.4 的取消链同构：先关输入，再等宽限期，最后强杀。
        """
        self._closing = True
        if self.proc.returncode is None:
            if self.proc.stdin is not None and not self.proc.stdin.is_closing():
                with contextlib.suppress(Exception):
                    self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), timeout=grace)

        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        self._fail_pending(AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "适配器已关闭"))

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)
        else:  # pragma: no cover - 未装配回调时的兜底
            print(f"[adapter:{self.label}] {message}", file=sys.stderr)
