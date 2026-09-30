"""Session 托管进程（架构设计 v0.02 §3）。

它存在的理由是回答审计指出的自指性问题：**Workerbee 自身不能复现「断连即失」**。
如果 harness 子进程由 core 持有，那么 core 一重启，用户正在跑的长程任务就没了——
这恰恰是这款软件要消灭的痛点。

因此这里只做三件事，且坚持只做这三件事：

1. **持有**适配器与 harness 子进程，跨 core 重启存活。
2. **缓冲**每个会话的事件并编号，使重连的 core 能补齐断连期间发生的事。
3. **持久化**会话台账，使重启后的 core 能据此判断「谁还活着」而不是猜。

它刻意不承载任何领域逻辑：不知道任务、阶段、尝试是什么，也不做依赖判断。
「谁是权威」必须清晰——权威在 core 的数据库，supervisor 只是**事实的持有者**。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections import deque
from pathlib import Path
from typing import Any

import aiosqlite

from .protocol import (
    SUPERVISOR_PROTOCOL_VERSION,
    ErrorCode,
    METHODS,
    NOTIFICATIONS,
    SupervisorError,
)

__all__ = ["Supervisor"]

#: 每个会话保留的事件条数。超出后最旧的被丢弃，并在 core 请求时明确回 EVENTS_GONE，
#: 而不是悄悄给出一个不完整的事件流让 core 以为补齐了。
EVENT_BUFFER = 1000

#: 单次会话存活探测的超时。心跳循环每 5 秒过一轮，探测本身不该拖过一轮间隔，
#: 否则探测会互相追赶，最终把整个循环钉死。
PROBE_TIMEOUT_SECONDS = 4.0


class SessionLedger:
    """supervisor 自己的会话台账。

    独立于 core 的数据库：它必须在 core 完全不可用时也能读写，
    因此不能依赖 core 的连接与迁移。两个进程各写各的库，互不阻塞。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS session_ledger (
                session_ref      TEXT PRIMARY KEY,
                harness_id       TEXT NOT NULL,
                owner_task_id    TEXT,
                owner_stage_id   TEXT,
                owner_attempt_id TEXT,
                state            TEXT NOT NULL,
                persist_locator  TEXT,
                pid              INTEGER,
                created_at       TEXT NOT NULL,
                last_heartbeat   TEXT,
                generation       INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_state ON session_ledger(state)"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def upsert(
        self,
        *,
        session_ref: str,
        harness_id: str,
        owner: dict[str, str | None] | None = None,
        state: str = "alive",
        persist_locator: str | None = None,
        pid: int | None = None,
    ) -> None:
        from ..core.domain.base import utcnow

        owner = owner or {}
        now = utcnow().isoformat()
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO session_ledger(session_ref, harness_id, owner_task_id,
                   owner_stage_id, owner_attempt_id, state, persist_locator, pid,
                   created_at, last_heartbeat)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_ref) DO UPDATE SET
                   state=excluded.state, persist_locator=excluded.persist_locator,
                   pid=excluded.pid, last_heartbeat=excluded.last_heartbeat,
                   owner_task_id=COALESCE(excluded.owner_task_id, owner_task_id),
                   owner_stage_id=COALESCE(excluded.owner_stage_id, owner_stage_id),
                   owner_attempt_id=COALESCE(excluded.owner_attempt_id, owner_attempt_id)""",
            (
                session_ref,
                harness_id,
                owner.get("task_id"),
                owner.get("stage_id"),
                owner.get("attempt_id"),
                state,
                persist_locator,
                pid,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def set_state(self, session_ref: str, state: str) -> None:
        from ..core.domain.base import utcnow

        assert self._db is not None
        await self._db.execute(
            "UPDATE session_ledger SET state=?, last_heartbeat=? WHERE session_ref=?",
            (state, utcnow().isoformat(), session_ref),
        )
        await self._db.commit()

    async def heartbeat(self, session_ref: str) -> None:
        from ..core.domain.base import utcnow

        assert self._db is not None
        await self._db.execute(
            "UPDATE session_ledger SET last_heartbeat=? WHERE session_ref=?",
            (utcnow().isoformat(), session_ref),
        )
        await self._db.commit()

    async def get(self, session_ref: str) -> dict[str, Any] | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM session_ledger WHERE session_ref=?", (session_ref,)
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def alive_sessions(self) -> list[dict[str, Any]]:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM session_ledger WHERE state='alive' ORDER BY created_at"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def all_sessions(self) -> list[dict[str, Any]]:
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM session_ledger ORDER BY created_at")
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


class _SessionBuffer:
    """一个会话的事件环形缓冲。"""

    def __init__(self, capacity: int = EVENT_BUFFER) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.next_seq = 1
        self.dropped_before = 1
        """已被丢弃的最早序号。core 请求更早的序号时据此报 EVENTS_GONE。"""

    def append(self, payload: dict[str, Any]) -> int:
        seq = self.next_seq
        self.next_seq += 1
        if len(self.events) == self.events.maxlen:
            self.dropped_before = self.events[0]["seq"] + 1
        self.events.append({"seq": seq, **payload})
        return seq

    def since(self, after_seq: int) -> tuple[list[dict[str, Any]], bool]:
        """返回 (事件列表, 是否有缺口)。"""
        if after_seq + 1 < self.dropped_before:
            return [e for e in self.events if e["seq"] > after_seq], True
        return [e for e in self.events if e["seq"] > after_seq], False


class Supervisor:
    """托管进程本体。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        socket_path: Path | None = None,
        adapter_commands: dict[str, list[str]] | None = None,
        registrations: dict[str, Any] | None = None,
        adapter_env: dict[str, str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.socket_path = socket_path or (self.data_dir / "supervisor.sock")
        self.adapter_commands = adapter_commands or {}
        #: standalone 模式下的 harness 注册表。supervisor 不依赖 core 的数据库——
        #: 它必须在 core 完全不可用时也能工作。
        self.registrations = registrations or {}
        #: 额外注入给适配器子进程的环境变量（测试用它下发 mock 剧本）。
        self.adapter_env = adapter_env or {}
        self.ledger = SessionLedger(self.data_dir / "supervisor.db")

        self.harness: Any | None = None
        self._buffers: dict[str, _SessionBuffer] = {}
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._write_locks: dict[int, asyncio.Lock] = {}
        self._server: asyncio.AbstractServer | None = None
        self._stopping = False
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.ledger.open()
        self.harness = await self._build_harness()
        await self._reconcile_ledger()

        if self.socket_path.exists():
            # 上一个实例没有正常退出。socket 文件残留会让 bind 失败，
            # 而它对应的进程已经不存在——删掉是安全的，但我们先确认没有人在监听。
            if not await self._socket_is_live():
                self.socket_path.unlink(missing_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.socket_path, 0o600)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        print(f"[supervisor] 已就绪：{self.socket_path}", file=sys.stderr)

    async def stop(self) -> None:
        self._stopping = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

        for writer in list(self._subscribers):
            with contextlib.suppress(Exception):
                writer.close()
        self._subscribers.clear()

        if self.harness is not None and hasattr(self.harness, "stop"):
            with contextlib.suppress(Exception):
                await self.harness.stop()
        await self.ledger.close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    async def _build_harness(self) -> Any:
        from ..adapters.host.router import HarnessRouter

        return await HarnessRouter.create(
            store=None,  # supervisor 不需要 core 的数据库
            adapter_commands=self.adapter_commands or None,
            registrations=self.registrations,
            adapter_env=self.adapter_env,
            on_event=self._on_harness_event,
            on_permission=self._on_permission,
            on_exit=self._on_harness_exit,
            log=lambda m: print(f"[supervisor:adapter] {m}", file=sys.stderr),
            standalone=True,
        )

    async def _socket_is_live(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=1.0
            )
        except Exception:  # noqa: BLE001
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _probe_alive(self, session_ref: str) -> bool:
        """探测会话存活。**必须带超时。**

        不带超时的话，一个卡住的探测会把整个心跳循环钉死——此后所有会话的心跳
        与死亡通知一起停摆，故障从一个会话扩散到全部。探测失败一律按「不活着」
        处理，由调用方标记 lost 并通知，不在这里吞掉结论。
        """
        try:
            return bool(
                await asyncio.wait_for(
                    self.harness.session_alive(session_ref),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
            )
        except Exception:  # noqa: BLE001 - 探不出来就是不活着
            return False

    async def _mark_session_lost(self, session_ref: str, reason: str) -> None:
        """标记会话已死，并通知 core。**标记与通知必须成对，且顺序不可换。**

        拆开写的代价实测过：标记落了库，通知抛异常被上层 suppress 吞掉。此后
        ``alive_sessions()`` 不再返回这个会话，它永远不会被复查；core 也永远
        收不到这次死亡——阶段停在 running，任务无限期挂起，而两侧日志都干净。
        """
        await self.ledger.set_state(session_ref, "lost")
        try:
            await self._notify(
                NOTIFICATIONS.SESSION_DIED,
                {"session_ref": session_ref, "reason": reason},
            )
        except Exception as exc:  # noqa: BLE001 - _notify 已逐订阅者兜底，这里再兜一层
            print(
                f"[supervisor] 会话 {session_ref} 的死亡通知广播失败：{exc}",
                file=sys.stderr,
            )

    async def _reconcile_ledger(self) -> None:
        """把台账里标 alive 但实际已死的会话改成 lost，并通知 core。

        「台账说活着」不等于「真的活着」——这条纪律在 core 与 supervisor 两侧都成立。
        对账同样是死亡通知的来源之一，不能只改状态不通知。
        """
        for row in await self.ledger.alive_sessions():
            session_ref = row["session_ref"]
            if not await self._probe_alive(session_ref):
                await self._mark_session_lost(session_ref, "对账发现进程已不存在")

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(5.0)
            for row in await self.ledger.alive_sessions():
                session_ref = row["session_ref"]
                if await self._probe_alive(session_ref):
                    with contextlib.suppress(Exception):
                        await self.ledger.heartbeat(session_ref)
                    continue
                await self._mark_session_lost(session_ref, "心跳缺失")

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._write_locks[id(writer)] = asyncio.Lock()
        self._subscribers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                await self._handle_line(text, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.discard(writer)
            self._write_locks.pop(id(writer), None)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle_line(self, text: str, writer: asyncio.StreamWriter) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(writer, None, ErrorCode.PARSE_ERROR, "无法解析的 JSON")
            return

        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            await self._send_error(writer, req_id, ErrorCode.INVALID_REQUEST, "缺少 method")
            return

        try:
            result = await self._dispatch(method, params)
        except SupervisorError as exc:
            await self._send_error(writer, req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读回包
            await self._send_error(
                writer, req_id, ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
        else:
            await self._send(writer, {"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send(
        self, writer: asyncio.StreamWriter, payload: dict[str, Any]
    ) -> None:
        lock = self._write_locks.get(id(writer))
        if lock is None:
            return
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        async with lock:
            with contextlib.suppress(Exception):
                writer.write((line + "\n").encode())
                await writer.drain()

    async def _send_error(
        self, writer: asyncio.StreamWriter, req_id: Any, code: int, message: str,
        data: dict | None = None,
    ) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data:
            err["data"] = data
        if req_id is not None:
            await self._send(writer, {"jsonrpc": "2.0", "id": req_id, "error": err})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """广播给所有订阅者。**单个订阅者写失败不得影响其余订阅者。**

        这里曾经是裸循环：一个写不进去的连接抛异常，后面所有订阅者都收不到这条
        通知。而调用方（心跳循环）把它包在 suppress 里，于是异常静默消失，
        「已标记 lost」却已经落库——那个会话再也不会被复查，死亡通知永久丢失。
        core 被强杀时读循环可能一直不知道对面已经没了，死 writer 会一直留在
        集合里，所以这里主动摘除，不指望读循环来收尸。
        """
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        for writer in list(self._subscribers):
            try:
                await self._send(writer, frame)
            except Exception:  # noqa: BLE001 - 通知是尽力而为，坏连接即摘除
                self._subscribers.discard(writer)
                self._write_locks.pop(id(writer), None)
                with contextlib.suppress(Exception):
                    writer.close()

    # ------------------------------------------------------------------
    # 方法分发
    # ------------------------------------------------------------------

    async def _dispatch(self, method: str, params: dict) -> Any:
        handler = getattr(self, f"_m_{method.replace('.', '_')}", None)
        if handler is None:
            raise SupervisorError(ErrorCode.METHOD_NOT_FOUND, f"未知方法: {method}")
        return await handler(params)

    async def _m_hello(self, params: dict) -> dict:
        return {
            "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "sessions": [r["session_ref"] for r in await self.ledger.alive_sessions()],
        }

    async def _m_ping(self, params: dict) -> dict:
        return {"ok": True, "pid": os.getpid()}

    async def _m_system_status(self, params: dict) -> dict:
        sessions = await self.ledger.all_sessions()
        return {
            "pid": os.getpid(),
            "socket": str(self.socket_path),
            "session_total": len(sessions),
            "session_alive": sum(1 for s in sessions if s["state"] == "alive"),
            "session_lost": sum(1 for s in sessions if s["state"] == "lost"),
            "subscribers": len(self._subscribers),
            "harness_ids": getattr(self.harness, "harness_ids", None),
        }

    async def _m_harness_ensure(self, params: dict) -> dict:
        """按需拉起适配器。

        core 每次都把注册信息带过来（而不是让 supervisor 去读 core 的库）：
        这样 supervisor 在 core 完全不可用时依然能工作——这是它存在的理由。
        """
        harness_id = params["harness_id"]
        registration = self.registrations.get(harness_id)
        if registration is None and params.get("adapter_id"):
            from ..core.domain.registry import AuthMode, HarnessRegistration

            registration = HarnessRegistration(
                harness_id=harness_id,
                name=harness_id,
                adapter_id=params["adapter_id"],
                exec_path=params.get("exec_path"),
                env_template=dict(params.get("env") or {}),
                cwd=params.get("cwd"),
                auth_mode=AuthMode.NATIVE_LOGIN,
                enabled=True,
            )
            self.registrations[harness_id] = registration

        if registration is None:
            raise SupervisorError(
                ErrorCode.HARNESS_NOT_FOUND,
                f"未提供 harness 注册信息：{harness_id}",
                {"harness_id": harness_id},
            )

        # ``ensure_harness`` 的返回值是「之前就已拉起」而不是「成功与否」：
        # 起不来会抛异常。这里把两个含义拆成两个 JSON 字段，避免调用方误读
        # ——把「新拉起来了」当成失败会让 core 拒绝一次本来成功的派发。
        already_running = await self.harness.ensure_harness(
            harness_id,
            adapter_id=registration.adapter_id,
            exec_path=registration.exec_path,
            env=dict(registration.env_template or {}),
            cwd=registration.cwd,
        )
        return {
            "ok": True,
            "harness_id": harness_id,
            "already_running": bool(already_running),
        }

    async def _m_harness_capabilities(self, params: dict) -> dict:
        caps = await self.harness.capabilities(params["harness_id"])
        return caps.__dict__ if hasattr(caps, "__dict__") else dict(caps)

    async def _m_harness_list(self, params: dict) -> dict:
        return {"harnesses": list(getattr(self.harness, "harness_ids", []) or [])}

    async def _m_harness_stop(self, params: dict) -> dict:
        harness_id = params.get("harness_id")
        stop = getattr(self.harness, "stop_harness", None)
        if stop is not None:
            await stop(harness_id)
        return {"ok": True}

    async def _m_session_create(self, params: dict) -> dict:
        from ..core.domain.task import Attempt, TaskStage

        # supervisor 不认识领域对象，但 HarnessPort 的签名要它们。
        # 用轻量替身承载标识字段——supervisor 只需要把它们透传给适配器并记账。
        attempt = Attempt(
            attempt_id=params["attempt_id"],
            stage_id=params["stage_id"],
            task_id=params["task_id"],
            node_id=params["node_id"],
            attempt_seq=params.get("attempt_seq", 1),
            profile_id=params.get("profile_id", "unknown"),
        )
        stage = TaskStage(
            stage_id=params["stage_id"], task_id=params["task_id"],
            node_id=params["node_id"], node_name=params.get("node_name"),
        )

        handle = await self.harness.create_session(
            harness_id=params["harness_id"],
            attempt=attempt,
            stage=stage,
            model_name=params["model_name"],
            reasoning_effort=params.get("reasoning_effort"),
            system_prompt=params.get("system_prompt"),
            initial_input=params.get("initial_input"),
            permission_mode=params.get("permission_mode"),
            # core 已解析好的凭据材料（supervisor 没有凭据库口令，自己解不开）。
            # 只从这里流向 HarnessConfig.credential，不落日志。
            credential=params.get("credential"),
            cwd=params.get("cwd"),
            extra=params.get("extra"),
        )
        await self.ledger.upsert(
            session_ref=handle.session_ref,
            harness_id=params["harness_id"],
            owner={
                "task_id": params["task_id"],
                "stage_id": params["stage_id"],
                "attempt_id": params["attempt_id"],
            },
            state="alive",
            persist_locator=handle.persist_locator,
            pid=handle.pid,
        )
        self._buffers.setdefault(handle.session_ref, _SessionBuffer())
        return _handle_to_dict(handle)

    async def _m_session_resume(self, params: dict) -> dict:
        from ..core.domain.task import Attempt, TaskStage

        attempt = Attempt(
            attempt_id=params["attempt_id"],
            stage_id=params["stage_id"],
            task_id=params["task_id"],
            node_id=params["node_id"],
            attempt_seq=params.get("attempt_seq", 1),
            profile_id=params.get("profile_id", "unknown"),
        )
        stage = TaskStage(
            stage_id=params["stage_id"], task_id=params["task_id"], node_id=params["node_id"]
        )
        handle = await self.harness.resume_session(
            harness_id=params["harness_id"],
            persist_locator=params["persist_locator"],
            attempt=attempt,
            stage=stage,
        )
        await self.ledger.upsert(
            session_ref=handle.session_ref,
            harness_id=params["harness_id"],
            owner={"task_id": params["task_id"], "stage_id": params["stage_id"],
                   "attempt_id": params["attempt_id"]},
            state="alive",
            persist_locator=handle.persist_locator,
            pid=handle.pid,
        )
        self._buffers.setdefault(handle.session_ref, _SessionBuffer())
        return _handle_to_dict(handle)

    async def _m_session_send_input(self, params: dict) -> dict:
        ok = await self.harness.send_input(
            params["session_ref"], params["text"], kind=params.get("kind", "user")
        )
        return {"ok": ok}

    async def _m_session_interrupt(self, params: dict) -> dict:
        return {"ok": await self.harness.interrupt(params["session_ref"])}

    async def _m_session_terminate(self, params: dict) -> dict:
        return {
            "ok": await self.harness.terminate(
                params["session_ref"], signal=params.get("signal", "TERM")
            )
        }

    async def _m_session_abort_stream(self, params: dict) -> dict:
        await self.harness.abort_stream(params["session_ref"])
        return {"ok": True}

    async def _m_session_pause(self, params: dict) -> dict:
        return {"ok": await self.harness.pause(params["session_ref"])}

    async def _m_session_checkpoint(self, params: dict) -> dict:
        return {"checkpoint": await self.harness.checkpoint(params["session_ref"])}

    async def _m_session_compact(self, params: dict) -> dict:
        return await self.harness.compact(
            params["session_ref"], params.get("threshold")
        )

    async def _m_session_alive(self, params: dict) -> dict:
        session_ref = params["session_ref"]
        alive = False
        with contextlib.suppress(Exception):
            alive = bool(await self.harness.session_alive(session_ref))
        if not alive:
            row = await self.ledger.get(session_ref)
            if row is not None and row["state"] == "alive":
                await self.ledger.set_state(session_ref, "lost")
        return {"alive": alive}

    async def _m_session_dispose(self, params: dict) -> dict:
        session_ref = params["session_ref"]
        await self.harness.dispose(session_ref)
        await self.ledger.set_state(session_ref, "disposed")
        return {"ok": True}

    async def _m_session_list(self, params: dict) -> dict:
        return {"sessions": await self.ledger.all_sessions()}

    async def _m_session_events(self, params: dict) -> dict:
        session_ref = params["session_ref"]
        after = int(params.get("after_seq", 0))
        buf = self._buffers.get(session_ref)
        if buf is None:
            # 会话在 supervisor 重启前建的：缓冲已丢，如实回 EVENTS_GONE 而不是空列表。
            # 空列表会让 core 以为「没有事件」，那是把「不知道」伪装成「没有」。
            raise SupervisorError(
                ErrorCode.EVENTS_GONE,
                "该会话的事件缓冲不存在（supervisor 重启过）",
                {"session_ref": session_ref},
            )
        events, gap = buf.since(after)
        return {"events": events, "gap": gap, "next_seq": buf.next_seq}

    async def _m_subscribe(self, params: dict) -> dict:
        return {"ok": True, "note": "本连接已自动订阅"}

    async def _m_shutdown(self, params: dict) -> dict:
        asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(self.stop()))
        return {"ok": True}

    # ------------------------------------------------------------------
    # 来自适配层的事件
    # ------------------------------------------------------------------

    async def _on_harness_event(self, event: Any) -> None:
        session_ref = getattr(event, "session_ref", None)
        if not session_ref:
            return
        buf = self._buffers.setdefault(session_ref, _SessionBuffer())
        payload = {
            "kind": getattr(event, "kind", ""),
            "session_ref": session_ref,
            "attempt_id": getattr(event, "attempt_id", None),
            "text": getattr(event, "text", None),
            "data": dict(getattr(event, "data", {}) or {}),
        }
        seq = buf.append(payload)
        await self._notify(NOTIFICATIONS.EVENT, {"seq": seq, **payload})

    async def _on_permission(self, request: Any) -> None:
        session_ref = getattr(request, "session_ref", None)
        if session_ref:
            buf = self._buffers.setdefault(session_ref, _SessionBuffer())
            payload = {
                "kind": "permission_request",
                "session_ref": session_ref,
                "data": request.model_dump(mode="json")
                if hasattr(request, "model_dump")
                else dict(request),
            }
            seq = buf.append(payload)
            await self._notify(NOTIFICATIONS.EVENT, {"seq": seq, **payload})

    async def _on_harness_exit(self, harness_id: str, code: int | None, stderr: str) -> None:
        await self._notify(
            NOTIFICATIONS.HARNESS_DIED,
            {"harness_id": harness_id, "exit_code": code, "stderr_tail": stderr[-1000:]},
        )
        for row in await self.ledger.alive_sessions():
            if row["harness_id"] == harness_id:
                await self.ledger.set_state(row["session_ref"], "lost")
                await self._notify(
                    NOTIFICATIONS.SESSION_DIED,
                    {"session_ref": row["session_ref"], "reason": "harness 进程退出"},
                )


def _handle_to_dict(handle: Any) -> dict[str, Any]:
    return {
        "session_ref": handle.session_ref,
        "harness_id": handle.harness_id,
        "state": handle.state,
        "persist_locator": handle.persist_locator,
        "pid": handle.pid,
        "model_name": handle.model_name,
        "used_resume": handle.used_resume,
        "accepted_initial_input": handle.accepted_initial_input,
        "detail": handle.detail,
    }
