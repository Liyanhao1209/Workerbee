"""append-only 事件日志（架构设计 v0.02 §5.4、D-08）。

它是监控（OBS-02）、历史回看（OBS-03）、审批留痕（HUM-04）、崩溃对账（REC-03）
的**共同数据源**——一处投入三处收益，这也是选择「写前日志」的原因。

两条纪律：
1. **只插入，不更新。** 表结构不含 UPDATE 路径（清理时按任务批量删除除外）。
2. **落库前脱敏。** 凭据任何情况下不得进入历史（AUTH-02、§9.1）。脱敏器以可调用
   对象注入，data 层不感知 security 层的实现细节。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence

from ..core.domain.base import utcnow
from ..core.domain.message import Message
from .db import Database, dumps

__all__ = ["EventScope", "EventActor", "EventType", "EventLog", "RedactorFn"]


class EventScope(StrEnum):
    WORKFLOW = "workflow"
    TASK = "task"
    STAGE = "stage"
    ATTEMPT = "attempt"
    SESSION = "session"
    RESOURCE = "resource"
    APPROVAL = "approval"
    SYSTEM = "system"


class EventActor(StrEnum):
    USER = "user"
    SYSTEM = "system"
    ADAPTER = "adapter"
    AI = "ai"


class EventType(StrEnum):
    # 定义层
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_UPDATED = "workflow.updated"
    WORKFLOW_DELETED = "workflow.deleted"
    REVISION_SAVED = "revision.saved"
    REVISION_PUBLISHED = "revision.published"
    NODE_ACTIVATION_REQUESTED = "node.activation_requested"
    NODE_ACTIVATION_APPLIED = "node.activation_applied"
    VALIDATION_FAILED = "validation.failed"

    # 运行时
    TASK_SUBMITTED = "task.submitted"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_CONTROL = "task.control"
    TASK_COMPLETED = "task.completed"
    STAGE_STATE_CHANGED = "stage.state_changed"
    STAGE_DISPATCHED = "stage.dispatched"
    STAGE_RETRY = "stage.retry"
    STAGE_CANDIDATE_SWITCHED = "stage.candidate_switched"
    STAGE_REORDERED = "stage.reordered"
    STAGE_CONTRACT_VIOLATION = "stage.contract_violation"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_ENDED = "attempt.ended"
    ATTEMPT_COMPACT = "attempt.compact"
    ATTEMPT_USAGE = "attempt.usage"
    ATTEMPT_DISCARDED_LATE = "attempt.discarded_late"

    # 交接与上下文
    CONTEXT_ASSEMBLED = "context.assembled"
    HANDOFF_FAILED = "handoff.failed"
    DATA_READY = "data.ready"
    FEEDBACK_RAISED = "feedback.raised"

    # 会话与资源
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    SESSION_ENDED = "session.ended"
    SESSION_LOST = "session.lost"
    RESOURCE_REGISTERED = "resource.registered"
    RESOURCE_CLOSED = "resource.closed"
    RESOURCE_TEARDOWN_FAILED = "resource.teardown_failed"
    RESOURCE_ORPHANED = "resource.orphaned"
    REAPER_RUN = "reaper.run"

    # 安全
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_INVALIDATED = "approval.invalidated"
    APPROVAL_UNDELIVERABLE = "approval.undeliverable"
    AI_DRAFT_PROPOSED = "ai.draft_proposed"
    AI_DRAFT_ACCEPTED = "ai.draft_accepted"
    SECRET_BOUND = "secret.bound"
    SECRET_REVOKED = "secret.revoked"

    # 恢复
    RECONCILE_STARTED = "reconcile.started"
    RECONCILE_RESULT = "reconcile.result"
    RESUME_REQUESTED = "resume.requested"

    # 系统
    SYSTEM_START = "system.start"
    SYSTEM_STOP = "system.stop"
    STORAGE_PRUNED = "storage.pruned"


RedactorFn = Callable[[Any], Any]


def _identity(value: Any) -> Any:
    return value


class EventLog:
    """事件写入与读取。"""

    def __init__(self, db: Database, redactor: RedactorFn | None = None) -> None:
        self.db = db
        self._redact: RedactorFn = redactor or _identity

    def set_redactor(self, redactor: RedactorFn) -> None:
        """由组合根在启动时注入安全层的脱敏实现。"""
        self._redact = redactor

    async def append(
        self,
        *,
        scope: EventScope,
        type: EventType,
        actor: EventActor = EventActor.SYSTEM,
        scope_id: str | None = None,
        task_id: str | None = None,
        stage_id: str | None = None,
        payload: dict[str, Any] | None = None,
        refs: Sequence[str] = (),
    ) -> int:
        """追加一条事件，返回 event_id。

        走 ``db.transaction()``：没有外层事务时自成一个已提交事务；在事务内调用时
        以 SAVEPOINT 参与外层事务——这使「先写日志再改状态」成为真的原子操作，
        状态回滚时那条日志也一并回滚，不会留下「日志说有、状态说没」的假记录。
        """
        safe = self._redact(payload) if payload else {}
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                """INSERT INTO event_log(scope, scope_id, type, actor, task_id, stage_id,
                       payload, refs, ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    scope.value,
                    scope_id,
                    type.value,
                    actor.value,
                    task_id,
                    stage_id,
                    dumps(safe),
                    dumps(list(refs)),
                    utcnow().isoformat(),
                ),
            )
            event_id = cursor.lastrowid
            await cursor.close()
        return int(event_id or 0)

    # ---- 读取 ----

    async def for_task(self, task_id: str, *, limit: int = 500, after_id: int = 0) -> list[dict]:
        rows = await self.db.fetch_all(
            """SELECT * FROM event_log WHERE task_id=? AND event_id>?
               ORDER BY event_id LIMIT ?""",
            (task_id, after_id, limit),
        )
        return [self._row(r) for r in rows]

    async def by_scope(
        self, scope: EventScope, scope_id: str, *, limit: int = 500, after_id: int = 0
    ) -> list[dict]:
        rows = await self.db.fetch_all(
            """SELECT * FROM event_log WHERE scope=? AND scope_id=? AND event_id>?
               ORDER BY event_id LIMIT ?""",
            (scope.value, scope_id, after_id, limit),
        )
        return [self._row(r) for r in rows]

    async def tail(self, *, after_id: int = 0, limit: int = 500) -> list[dict]:
        """增量拉取，供 WebSocket 推送与监控（OBS-02）。"""
        rows = await self.db.fetch_all(
            "SELECT * FROM event_log WHERE event_id>? ORDER BY event_id LIMIT ?",
            (after_id, limit),
        )
        return [self._row(r) for r in rows]

    async def latest_id(self) -> int:
        return int(await self.db.fetch_value("SELECT COALESCE(MAX(event_id),0) FROM event_log",
                                             default=0))

    async def count_for_task(self, task_id: str) -> int:
        return int(
            await self.db.fetch_value(
                "SELECT COUNT(*) FROM event_log WHERE task_id=?", (task_id,), default=0
            )
        )

    # ---- 维护（RES-03：提供入口，不强制自动 TTL） ----

    async def prune_task(self, task_id: str, *, keep_last: int = 0) -> int:
        """按任务归档：删除某任务的历史事件。保留 ``keep_last`` 条最新记录。"""
        if keep_last <= 0:
            return await self.db.execute_rowcount(
                "DELETE FROM event_log WHERE task_id=?", (task_id,)
            )
        threshold = await self.db.fetch_value(
            """SELECT MIN(event_id) FROM (
                   SELECT event_id FROM event_log WHERE task_id=?
                   ORDER BY event_id DESC LIMIT ?)""",
            (task_id, keep_last),
            default=None,
        )
        if threshold is None:
            return 0
        return await self.db.execute_rowcount(
            "DELETE FROM event_log WHERE task_id=? AND event_id<?", (task_id, int(threshold))
        )

    async def prune_older_than(self, iso_ts: str) -> int:
        return await self.db.execute_rowcount(
            "DELETE FROM event_log WHERE ts<?", (iso_ts,)
        )

    @staticmethod
    def _row(row) -> dict:
        return {
            "event_id": row["event_id"],
            "scope": row["scope"],
            "scope_id": row["scope_id"],
            "type": row["type"],
            "actor": row["actor"],
            "task_id": row["task_id"],
            "stage_id": row["stage_id"],
            "payload": json.loads(row["payload"]) if row["payload"] else {},
            "refs": json.loads(row["refs"]) if row["refs"] else [],
            "ts": row["ts"],
        }


def message_event_type(message: Message) -> EventType:
    """把消息类型映射为事件类型，供统一留痕。"""
    from ..core.domain.message import MessageType

    return {
        MessageType.DATA_READY: EventType.DATA_READY,
        MessageType.FEEDBACK: EventType.FEEDBACK_RAISED,
        MessageType.APPROVAL_REQ: EventType.APPROVAL_REQUESTED,
        MessageType.APPROVAL_RESP: EventType.APPROVAL_DECIDED,
        MessageType.STATE_NOTIFY: EventType.TASK_STATE_CHANGED,
        MessageType.CANCEL: EventType.TASK_CONTROL,
        MessageType.BTW_INPUT: EventType.TASK_CONTROL,
    }[message.type]
