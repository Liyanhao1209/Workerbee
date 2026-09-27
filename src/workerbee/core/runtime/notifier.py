"""状态推送与「需处理」提醒（架构设计 v0.02 §9.2、OBS-05）。

刻意保持**内存态、尽力而为**：通知丢了不影响正确性，因为权威状态在数据库里，
客户端重连后靠 REST 拉取真实状态（REC-01）。把通知做成可靠投递反而会引入
第二份事实源——那正是本项目要避免的。

订阅者队列有界：慢客户端只会丢自己的通知，不会拖住调度循环。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator

from pydantic import Field

from ..domain.base import DomainModel, utcnow

__all__ = ["Notification", "BroadcastNotifier"]


class Notification(DomainModel):
    kind: str
    """state_changed / attention / task_submitted / event …"""

    task_id: str | None = None
    stage_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at: str = Field(default_factory=lambda: utcnow().isoformat())


class BroadcastNotifier:
    """进程内广播。每个订阅者一个有界队列。"""

    def __init__(self, *, max_queue: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Notification]] = set()
        self._max_queue = max_queue
        self._dropped = 0

    def subscribe(self) -> asyncio.Queue[Notification]:
        q: asyncio.Queue[Notification] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Notification]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped(self) -> int:
        """被丢弃的通知条数。>0 说明有客户端跟不上——它重连后会拉到真实状态。"""
        return self._dropped

    def publish(self, notification: Notification) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(notification)
            except asyncio.QueueFull:
                self._dropped += 1
                # 丢弃最旧的一条，保证订阅者至少能看到最新状态
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(notification)

    async def stream(self) -> AsyncIterator[Notification]:
        """异步迭代订阅。断开时自行退订。"""
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)

    # ---- NotifierPort ----

    async def state_changed(self, *, task_id: str, stage_id: str | None = None) -> None:
        self.publish(
            Notification(kind="state_changed", task_id=task_id, stage_id=stage_id)
        )

    async def attention_required(
        self, *, kind: str, task_id: str | None, payload: dict[str, Any]
    ) -> None:
        self.publish(
            Notification(kind="attention", task_id=task_id, payload={"kind": kind, **payload})
        )
