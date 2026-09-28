"""消息总线（架构设计 v0.02 §5.4 Message、REC-05）。

投递语义：**至少一次 + 消费端按 dedup_key 幂等**。

实现上做两道防线：
1. ``message.dedup_key`` 上的唯一索引——同一 dedup_key 的消息**不可能入库两次**。
   这比「投递时去重」更强，也更简单。
2. 消费端 ``claim()`` 的显式幂等表——即便调用方逻辑被重放（崩溃恢复、
   消息重投），同一个 dedup_key 也只会被认领一次。

两者互补：前者防重复入队，后者防重复生效。
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from ..core.domain.base import utcnow
from ..core.domain.message import Message, MessageType
from .db import Database, dumps, loads

__all__ = ["MessageBus", "MessageState"]


class MessageState:
    PENDING = "pending"
    DELIVERED = "delivered"
    CONSUMED = "consumed"
    DEAD = "dead"


class MessageBus:
    """消息入队、投递与幂等认领。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- 发送 ----

    async def send(self, msg: Message) -> tuple[Message, bool]:
        """入队。返回 ``(message, created)``。

        ``created=False`` 表示该 dedup_key 已存在，返回的是**已存在**的那条——
        调用方据此知道「这次是重送，不要重复触发副作用」。
        """
        async with self.db.transaction():
            try:
                await self.db.execute(
                    """INSERT INTO message(message_id, type, task_id, from_stage, to_stage,
                           dedup_key, causation_id, generation, payload_ref, payload, state,
                           created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        msg.message_id,
                        msg.type.value,
                        msg.task_id,
                        msg.from_stage,
                        msg.to_stage,
                        msg.dedup_key,
                        msg.causation_id,
                        msg.generation,
                        msg.payload_ref,
                        dumps(msg.payload) if msg.payload is not None else None,
                        MessageState.PENDING,
                        msg.created_at.isoformat(),
                        msg.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = await self._find_by_dedup(msg.dedup_key)
                if existing is None:
                    raise
                return existing, False
        return msg, True

    async def _find_by_dedup(self, dedup_key: str | None) -> Message | None:
        if not dedup_key:
            return None
        row = await self.db.fetch_one(
            "SELECT * FROM message WHERE dedup_key=?", (dedup_key,)
        )
        return self._to_message(row) if row else None

    async def exists(self, dedup_key: str) -> bool:
        return bool(
            await self.db.fetch_value(
                "SELECT 1 FROM message WHERE dedup_key=? LIMIT 1", (dedup_key,), default=0
            )
        )

    # ---- 收取 ----

    async def inbox(
        self, stage_id: str, *, states: Sequence[str] = (MessageState.PENDING,), limit: int = 100
    ) -> list[Message]:
        placeholders = ",".join("?" * len(states))
        rows = await self.db.fetch_all(
            f"""SELECT * FROM message WHERE to_stage=? AND state IN ({placeholders})
                ORDER BY created_at LIMIT ?""",
            (stage_id, *states, limit),
        )
        return [self._to_message(r) for r in rows]

    async def inbox_for_task(self, task_id: str, *, limit: int = 200) -> list[Message]:
        rows = await self.db.fetch_all(
            "SELECT * FROM message WHERE task_id=? ORDER BY created_at LIMIT ?",
            (task_id, limit),
        )
        return [self._to_message(r) for r in rows]

    async def by_type(
        self, type_: MessageType, *, limit: int = 200, states: Sequence[str] | None = None
    ) -> list[Message]:
        if states:
            placeholders = ",".join("?" * len(states))
            rows = await self.db.fetch_all(
                f"""SELECT * FROM message WHERE type=? AND state IN ({placeholders})
                    ORDER BY created_at LIMIT ?""",
                (type_.value, *states, limit),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM message WHERE type=? ORDER BY created_at LIMIT ?",
                (type_.value, limit),
            )
        return [self._to_message(r) for r in rows]

    async def get(self, message_id: str) -> Message | None:
        row = await self.db.fetch_one(
            "SELECT * FROM message WHERE message_id=?", (message_id,)
        )
        return self._to_message(row) if row else None

    # ---- 幂等认领 ----

    async def claim(self, dedup_key: str) -> bool:
        """消费端幂等：该 dedup_key 首次被认领返回 True，重复返回 False。

        用 ``message.state`` 的 CAS 实现——只有仍处于 pending 的消息能被认领。
        """
        return (
            await self.db.execute_rowcount(
                "UPDATE message SET state=?, updated_at=? WHERE dedup_key=? AND state=?",
                (
                    MessageState.CONSUMED,
                    utcnow().isoformat(),
                    dedup_key,
                    MessageState.PENDING,
                ),
            )
            > 0
        )

    async def mark_delivered(self, message_id: str) -> None:
        await self.db.execute(
            "UPDATE message SET state=?, updated_at=? WHERE message_id=? AND state=?",
            (MessageState.DELIVERED, utcnow().isoformat(), message_id, MessageState.PENDING),
        )

    async def mark_consumed(self, message_id: str) -> None:
        await self.db.execute(
            "UPDATE message SET state=?, updated_at=? WHERE message_id=?",
            (MessageState.CONSUMED, utcnow().isoformat(), message_id),
        )

    async def mark_dead(self, message_id: str, reason: str) -> None:
        await self.db.execute(
            "UPDATE message SET state=?, payload=COALESCE(payload,'{}'), updated_at=? "
            "WHERE message_id=?",
            (MessageState.DEAD, utcnow().isoformat(), message_id),
        )

    async def purge_stage(self, stage_id: str) -> int:
        """阶段被清理时，丢弃其未消费的投递，避免迟到消息唤醒已结束的阶段。"""
        return await self.db.execute_rowcount(
            "UPDATE message SET state=?, updated_at=? WHERE to_stage=? AND state IN (?,?)",
            (
                MessageState.DEAD,
                utcnow().isoformat(),
                stage_id,
                MessageState.PENDING,
                MessageState.DELIVERED,
            ),
        )

    async def purge_task(self, task_id: str) -> int:
        return await self.db.execute_rowcount(
            "UPDATE message SET state=?, updated_at=? WHERE task_id=? AND state IN (?,?)",
            (
                MessageState.DEAD,
                utcnow().isoformat(),
                task_id,
                MessageState.PENDING,
                MessageState.DELIVERED,
            ),
        )

    # ---- 维护 ----

    async def prune(self, *, keep_states: Sequence[str] = ("pending", "delivered")) -> int:
        """清理已消费／已死的消息，保留仍在等待处理的那些。"""
        placeholders = ",".join("?" * len(keep_states))
        return await self.db.execute_rowcount(
            f"DELETE FROM message WHERE state NOT IN ({placeholders})", tuple(keep_states)
        )

    @staticmethod
    def _to_message(row) -> Message:
        return Message(
            message_id=row["message_id"],
            type=MessageType(row["type"]),
            task_id=row["task_id"],
            from_stage=row["from_stage"],
            to_stage=row["to_stage"],
            dedup_key=row["dedup_key"],
            causation_id=row["causation_id"],
            generation=row["generation"],
            payload_ref=row["payload_ref"],
            payload=loads(row["payload"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
