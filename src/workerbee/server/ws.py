"""WebSocket 推送（OBS-02、REC-01）。

推送是**尽力而为**的：权威状态在数据库里，通知丢了不影响正确性。因此这里
不做可靠投递，只做两件事——把 ``Notification`` 尽快送到，以及让客户端能按
``after_event_id`` 把断连期间的事件补齐（REC-01）。

帧类型（客户端按 ``type`` 分派）：

- ``hello``：连接建立。带 ``latest_event_id`` 作为客户端的初始水位。
- ``event``：一条事件日志记录。**至少一次**投递：补拉与实时流可能重叠，
  客户端按 ``event_id`` 去重。
- ``resume_complete``：补拉结束，附带本次送回条数与当前水位。
- ``notification``：状态变更提醒（不含权威数据，只是「该刷新了」）。
- ``pong`` / ``error``：心跳回应与协议错误。

鉴权**只能走查询参数**——浏览器的 WebSocket API 不能自定义请求头（UI-02）。
令牌缺失或不匹配时接受握手后立即以 4401 关闭：先 accept 才能把关闭码送到浏览器，
否则客户端只会看到一个没有原因的 403。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.runtime.notifier import Notification
from .auth import is_loopback_client
from .deps import ws_state

__all__ = ["router"]

router = APIRouter(tags=["ws"])

#: 关闭码：令牌无效（自定义区间 4000–4999，属于应用层语义）。
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

#: 单帧最多补拉多少条事件。客户端据此决定是否继续发 resume。
_BACKFILL_LIMIT = 500


class _Sender:
    """串行化发送。

    实时泵与补拉分别在不同任务里跑，两个协程同时 ``send_json`` 会让帧交错。
    这里用一把锁把「一帧」变成不可分割的发送单元。
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._lock = asyncio.Lock()

    async def json(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._ws.send_json(payload)

    async def receive_text(self) -> str:
        """读一帧客户端文本。连接断开时抛 WebSocketDisconnect。"""
        return await self._ws.receive_text()


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """订阅状态推送与事件增量。"""
    engine, _, auth = ws_state(websocket)

    if not auth.authorize_scope(websocket.scope):
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="令牌缺失或无效")
        return
    if not websocket.app.state.allow_remote and not is_loopback_client(websocket.scope):
        await websocket.accept()
        await websocket.close(code=CLOSE_FORBIDDEN, reason="仅允许本机访问")
        return

    await _serve(engine, websocket)


async def _serve(engine: Any, websocket: WebSocket) -> None:
    sender = _Sender(websocket)

    # 先订阅再握手：从 accept 那一刻起的通知一条都不会漏。
    queue = engine.notifier.subscribe()
    try:
        await websocket.accept()
        watermark = await engine.store.events.latest_id()
        await sender.json(
            {
                "type": "hello",
                "latest_event_id": watermark,
                "notifier_dropped": engine.notifier.dropped,
                "startup_notes": list(engine.startup_notes),
                "note": "通知是尽力而为的提醒；断连后按 after_event_id 补拉事件（REC-01）",
            }
        )

        live = [int(watermark)]
        pump = asyncio.create_task(
            _pump_notifications(sender, engine, queue, live), name="ws-pump"
        )
        reader = asyncio.create_task(_read_client(sender, engine), name="ws-read")
        try:
            done, pending = await asyncio.wait(
                {pump, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
        except asyncio.CancelledError:
            pump.cancel()
            reader.cancel()
            raise
    except WebSocketDisconnect:
        # 客户端断开是正常路径，不是错误
        pass
    finally:
        engine.notifier.unsubscribe(queue)


async def _pump_notifications(
    sender: _Sender, engine: Any, queue: asyncio.Queue[Notification], live: list[int]
) -> None:
    """把通知送到客户端；每条通知顺带把新增事件推过去。

    状态变了才有通知，通知到达时顺手拉一次事件增量——这样前端不必为「事件」
    再开一条通道，也不会出现「通知到了但事件还没到」的错位。
    """
    while True:
        notification = await queue.get()
        await _push_events(sender, engine, live)
        await sender.json(
            {
                "type": "notification",
                "kind": notification.kind,
                "task_id": notification.task_id,
                "stage_id": notification.stage_id,
                "payload": notification.payload,
                "at": notification.at,
            }
        )


async def _read_client(sender: _Sender, engine: Any) -> None:
    """处理客户端消息。未知消息回一帧 error，不静默、也不断开。"""
    while True:
        raw = await sender.receive_text()
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            await sender.json({"type": "error", "detail": "消息不是合法 JSON"})
            continue
        if not isinstance(message, dict):
            await sender.json({"type": "error", "detail": "消息必须是 JSON 对象"})
            continue

        kind = message.get("type")
        if kind == "ping":
            await sender.json({"type": "pong"})
        elif kind == "resume":
            await _backfill(sender, engine, message)
        else:
            await sender.json(
                {
                    "type": "error",
                    "detail": f"未知消息类型: {kind!r}；可用: resume / ping",
                }
            )


async def _backfill(sender: _Sender, engine: Any, message: dict[str, Any]) -> None:
    try:
        after = int(message.get("after_event_id") or 0)
    except (TypeError, ValueError):
        await sender.json({"type": "error", "detail": "after_event_id 必须是整数"})
        return

    rows = await engine.store.events.tail(after_id=after, limit=_BACKFILL_LIMIT)
    for row in rows:
        await sender.json({"type": "event", "event": row})

    latest = await engine.store.events.latest_id()
    await sender.json(
        {
            "type": "resume_complete",
            "after_event_id": after,
            "latest_event_id": latest,
            "returned": len(rows),
            "has_more": len(rows) >= _BACKFILL_LIMIT,
            "note": "事件为至少一次投递：与实时流重叠的部分请按 event_id 去重",
        }
    )


async def _push_events(sender: _Sender, engine: Any, live: list[int]) -> int:
    """把水位之后的新事件推给客户端，返回推送条数。"""
    rows = await engine.store.events.tail(after_id=live[0], limit=_BACKFILL_LIMIT)
    for row in rows:
        await sender.json({"type": "event", "event": row})
        live[0] = int(row["event_id"])
    return len(rows)
