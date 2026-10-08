"""SQLite 连接与事务（架构设计 v0.02 D-08、§15）。

口径：
- 单机部署，**单写者**。所有写路径经过同一把 ``asyncio.Lock``，配合
  ``BEGIN IMMEDIATE`` 让 CAS（compare-and-swap）语义在事务内成立。
- WAL 模式：读不阻塞写，监控与调度可以并发读。
- ``synchronous=NORMAL``：崩溃时可能丢最后若干个事务，但不会损坏数据库；
  对「控制意图先写日志再改状态」的写前日志顺序足够——丢的是尾部，不是中间。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from .schema import MIGRATIONS, SCHEMA_VERSION

__all__ = ["Database", "DatabaseError", "ConflictError", "dumps", "loads"]


class DatabaseError(RuntimeError):
    pass


class ConflictError(DatabaseError):
    """唯一约束或 CAS 失败。调用方据此返回「实际生效结果」而不是静默覆盖。"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, bytes):
        value = value.decode()
    if value == "":
        return default
    return json.loads(value)


class Database:
    """一个进程内共享的连接包装。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        # 事务归属必须按**任务**判定：若只用深度计数，任务 B 会在任务 A 持有事务
        # 期间被误判为「嵌套」，从而加入 A 的事务。
        self._tx_owner: asyncio.Task | None = None
        self._tx_depth = 0

    # ---- 生命周期 ----

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None 关闭 sqlite3 的**隐式**事务：任何不在显式
        # BEGIN 里的语句立即自动提交。否则一条裸 DML 会把连接留在未提交的隐式
        # 事务中，下一次 BEGIN IMMEDIATE 直接报 "cannot start a transaction
        # within a transaction"。事务边界必须完全由本类掌握。
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA temp_store=MEMORY")
        await self._conn.commit()
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("数据库尚未连接；请先 await db.connect()")
        return self._conn

    # ---- 迁移 ----

    async def _migrate(self) -> None:
        conn = self.conn
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.commit()

        cur = await conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        row = await cur.fetchone()
        await cur.close()
        current = int(row["v"]) if row else 0

        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
            await conn.commit()
            current = version

        if current > SCHEMA_VERSION:
            raise DatabaseError(
                f"数据库版本 {current} 高于本程序支持的 {SCHEMA_VERSION}；请升级 Workerbee"
            )

    # ---- 事务 ----

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """写事务。串行化所有写者；BEGIN IMMEDIATE 让读-改-写原子。

        支持嵌套：**同一 asyncio 任务**的内层调用用 SAVEPOINT 实现局部原子性——
        内层失败只回滚内层写入，不会连带把外层已写的内容一起丢掉；外层失败则
        整体回滚。嵌套路径不再取锁（锁已被外层持有），否则会自锁死。不同的
        asyncio 任务即便在 A 持有事务期间进入，也会正常排队等锁，不会误入 A 的事务。

        ``_tx_depth > 0`` 与 ``_tx_owner is me`` 必须同时成立才算嵌套：只比 owner
        会在 ``current_task()`` 返回 None 且 owner 恰好也是 None 时误判。
        """
        conn = self.conn
        me = asyncio.current_task()

        if self._tx_depth > 0 and self._tx_owner is me:
            self._tx_depth += 1
            savepoint = f"wb_sp_{self._tx_depth}"
            await conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    await conn.execute(f"ROLLBACK TO {savepoint}")
                    await conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                await conn.execute(f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1
            return

        async with self._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            self._tx_owner = me
            self._tx_depth = 1
            try:
                yield conn
            except Exception:
                self._tx_owner = None
                self._tx_depth = 0
                await conn.rollback()
                raise
            else:
                self._tx_owner = None
                self._tx_depth = 0
                await conn.commit()

    # ---- 便捷读写 ----

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self.transaction() as conn:
            await conn.execute(sql, params)

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句并返回受影响行数——CAS 失败判定要靠它。"""
        async with self.transaction() as conn:
            cur = await conn.execute(sql, params)
            n = cur.rowcount
            await cur.close()
            return n

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self.transaction() as conn:
            await conn.executemany(sql, list(rows))

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cur = await self.conn.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = await self.conn.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        return row[0]

    # ---- 维护 ----

    async def vacuum(self) -> None:
        await self.conn.execute("VACUUM")
        await self.conn.commit()

    async def storage_report(self) -> dict[str, Any]:
        """存储占用查看（RES-03）：提供查看入口，不强制自动 TTL。"""
        tables = [
            "workflow",
            "workflow_revision",
            "task",
            "task_stage",
            "attempt",
            "resource_record",
            "session_handle",
            "artifact",
            "message",
            "event_log",
            "approval",
            "workspace",
        ]
        counts: dict[str, int] = {}
        for t in tables:
            counts[t] = int(await self.fetch_value(f"SELECT COUNT(*) FROM {t}", default=0))
        size = 0
        if str(self.path) != ":memory:":
            with contextlib.suppress(OSError):
                size = os.path.getsize(self.path)
        return {"path": str(self.path), "bytes": size, "row_counts": counts}
