"""审批网关（架构设计 v0.02 §5.5、§9.2、HUM-03/04、AC-12/14）。

拦截点统一在**适配层**：permission hook 把各 harness 的权限事件转译为 Approval；
等待期间受保护操作不得执行。

四条必须守住的规则：

1. **断连、超时、重复通知都不构成批准。** 客户端断连期间审批保持 pending，
   重连后找回（AC-12）；重复通知不重复授权（AC-14）——靠 ``decide()`` 的
   「只允许从 pending 迁移」CAS 保证。
2. **审批精确绑定执行尝试与配置版本。** 尝试失效或命令内容改变后旧批准自动作废，
   使「改了命令仍复用旧批准」在数据层面不可能发生（AC-14）。
3. **超时默认 deny_pause**：拒绝该动作并把阶段置为显式等待态，释放 stream 资源。
   内核收到这个结果后把阶段置 BLOCKED，等待用户处置——而不是让它一直显示 running。
4. **回传失败必须可见。** 决定已产生但回注原会话失败 → ``undeliverable``，
   可追溯、可重试，不得假装已送达（HUM-04）。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any, Awaitable, Callable

from pydantic import Field

from ..core.domain.approval import (
    Approval,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTimeoutPolicy,
)
from ..core.domain.base import DomainModel, utcnow
from ..data.event_log import EventActor, EventScope, EventType

__all__ = [
    "ApprovalGateway",
    "ApprovalOutcome",
    "PermissionRequestLike",
    "DeliveryResult",
]

#: 回注回调：(approval, status, modified_action) -> 是否送达
DeliverFn = Callable[[Approval, ApprovalStatus, str | None], Awaitable[bool]]


class DeliveryResult(DomainModel):
    delivered: bool
    status: ApprovalStatus
    detail: str | None = None


class PermissionRequestLike:
    """适配层上报的权限请求。这里用结构化协议而不是具体类，避免 L5 依赖 L3。"""


class ApprovalGateway:
    """审批生命周期、超时与回注。"""

    def __init__(
        self,
        *,
        store: Any,
        notifier: Any | None = None,
        deliver: DeliverFn | None = None,
        timeout_seconds: float = 900.0,  # 默认等待时限，超时即按 deny_pause 处理
        timeout_policy: ApprovalTimeoutPolicy = ApprovalTimeoutPolicy.DENY_PAUSE,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self._deliver = deliver
        self.timeout_seconds = timeout_seconds
        self.timeout_policy = timeout_policy

    def set_deliver(self, deliver: DeliverFn) -> None:
        """由组合根注入「把决定回注到会话」的实现（走适配层）。"""
        self._deliver = deliver

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    async def request(
        self,
        *,
        approval_id: str,
        task_id: str,
        stage_id: str,
        attempt_id: str,
        revision_seq: int,
        node_id: str | None,
        action: str,
        target: str | None = None,
        risk: str | None = None,
        tool_name: str | None = None,
        workflow_name: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> Approval:
        """登记一个待决审批。

        同一 ``approval_id`` 重复到达时返回已存在的那条——**不重复授权、不重复通知**
        （AC-14）。适配器若复用 approval_id 却改了动作内容，旧请求作废。
        """
        existing = await self.store.approvals.get(approval_id)
        fingerprint = ApprovalBinding.fingerprint(action, target, tool_name)

        if existing is not None:
            if existing.action_changed(fingerprint):
                existing.invalidate("命令内容已改变，旧批准不能授权新动作")
                await self.store.approvals.decide(
                    approval_id,
                    status=ApprovalStatus.SUPERSEDED,
                    decision=ApprovalDecision(by="system", note="命令内容变化"),
                    detail="命令内容已改变，旧批准不能授权新动作",
                )
                await self._event(
                    EventType.APPROVAL_INVALIDATED,
                    approval_id,
                    task_id,
                    {"reason": "命令内容变化"},
                )
                # 落到下面重新创建
            else:
                return existing

        approval = Approval(
            approval_id=approval_id,
            bound_to=ApprovalBinding(
                task_id=task_id,
                stage_id=stage_id,
                attempt_id=attempt_id,
                revision_seq=revision_seq,
                node_id=node_id,
                action_fingerprint=fingerprint,
            ),
            action=action,
            target=target,
            risk=risk,
            tool_name=tool_name,
            workflow_name=workflow_name,
            status=ApprovalStatus.PENDING,
            timeout_policy=self.timeout_policy,
            expires_at=utcnow() + timedelta(seconds=self.timeout_seconds),
        )
        await self.store.approvals.create(approval)
        await self._event(
            EventType.APPROVAL_REQUESTED,
            approval_id,
            task_id,
            {
                "action": action,
                "target": target,
                "tool": tool_name,
                "node_id": node_id,
                "attempt_id": attempt_id,
                "revision_seq": revision_seq,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                "raw_keys": sorted((raw or {}).keys()),
            },
            stage_id=stage_id,
        )
        await self._notify(
            kind="approval_required",
            task_id=task_id,
            payload={
                "approval_id": approval_id,
                "action": action,
                "target": target,
                "risk": risk,
                "tool_name": tool_name,
                "node_id": node_id,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            },
        )
        return approval

    # ------------------------------------------------------------------
    # 决定
    # ------------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        by: str = "user",
        modified_action: str | None = None,
        note: str | None = None,
    ) -> DeliveryResult:
        """用户做出决定并回注原会话。

        重复调用不会第二次生效：``decide`` 只在 pending 上成功（AC-14）。
        """
        approval = await self.store.approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"审批不存在: {approval_id}")

        if not approval.is_pending():
            # 不报错、不重复授权；把「实际生效结果」如实返回给前端（AC-03 的同一条纪律）
            return DeliveryResult(
                delivered=approval.status
                in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED, ApprovalStatus.SUPERSEDED),
                status=approval.status,
                detail=f"该审批已处于 {approval.status.value}，本次决定未生效",
            )

        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        decision = ApprovalDecision(
            by=by, approved=approve, modified_action=modified_action, note=note
        )

        ok = await self.store.approvals.decide(
            approval_id, status=status, decision=decision
        )
        if not ok:
            fresh = await self.store.approvals.get(approval_id)
            return DeliveryResult(
                delivered=False,
                status=fresh.status if fresh else ApprovalStatus.EXPIRED,
                detail="审批状态在本次决定提交前已变化，未生效",
            )

        approval.status = status
        approval.decision = decision

        await self._event(
            EventType.APPROVAL_DECIDED,
            approval_id,
            approval.bound_to.task_id,
            {
                "approved": approve,
                "by": by,
                "modified": modified_action is not None,
                "note": note,
            },
            stage_id=approval.bound_to.stage_id,
        )

        delivered = await self._try_deliver(approval, status, modified_action)
        return DeliveryResult(delivered=delivered, status=status)

    async def _try_deliver(
        self, approval: Approval, status: ApprovalStatus, modified_action: str | None
    ) -> bool:
        if self._deliver is None:
            # 没有回注通道：如实标记 undeliverable，而不是假装已送达（HUM-04）
            await self.store.approvals.decide(
                approval.approval_id,
                status=ApprovalStatus.UNDELIVERABLE,
                decision=approval.decision or ApprovalDecision(by="system"),
                detail="尚未装配回注通道，决定无法送达原会话",
            )
            await self._event(
                EventType.APPROVAL_UNDELIVERABLE,
                approval.approval_id,
                approval.bound_to.task_id,
                {"reason": "未装配回注通道"},
            )
            return False

        try:
            delivered = await self._deliver(approval, status, modified_action)
        except Exception as exc:  # noqa: BLE001 - 送达失败必须可见而非炸穿
            delivered = False
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = None if delivered else "适配器拒绝了回注"

        if not delivered:
            # 决定已产生但没送达。不能回退状态——用户确实做了决定，
            # 只是它没到会话里。标记为 undeliverable 并让它留在「需处理」列表里。
            await self.store.db.execute(
                "UPDATE approval SET status=?, detail=?, updated_at=? WHERE approval_id=?",
                (
                    ApprovalStatus.UNDELIVERABLE.value,
                    detail or "回注失败",
                    utcnow().isoformat(),
                    approval.approval_id,
                ),
            )
            await self._event(
                EventType.APPROVAL_UNDELIVERABLE,
                approval.approval_id,
                approval.bound_to.task_id,
                {"detail": detail},
            )
            await self._notify(
                kind="approval_undeliverable",
                task_id=approval.bound_to.task_id,
                payload={"approval_id": approval.approval_id, "detail": detail},
            )
        return delivered

    async def retry_delivery(self, approval_id: str) -> DeliveryResult:
        """对 undeliverable 的审批重试回注（HUM-04：保留可见性并允许继续处理）。

        undeliverable 的含义是「决定已产生、只是没送到」，因此重试沿用原决定，
        不让用户再批准一次——重复征求会训练用户盲目点同意。
        """
        approval = await self.store.approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"审批不存在: {approval_id}")
        if approval.status != ApprovalStatus.UNDELIVERABLE or approval.decision is None:
            return DeliveryResult(
                delivered=False,
                status=approval.status,
                detail="该审批没有待重试的决定",
            )

        target = (
            ApprovalStatus.APPROVED
            if approval.decision.approved
            else ApprovalStatus.DENIED
        )
        delivered = await self._try_deliver(
            approval, target, approval.decision.modified_action
        )
        return DeliveryResult(delivered=delivered, status=target if delivered else approval.status)

    # ------------------------------------------------------------------
    # 失效
    # ------------------------------------------------------------------

    async def invalidate_for_attempt(self, attempt_id: str, why: str) -> int:
        n = await self.store.approvals.invalidate_for_attempt(attempt_id, why)
        if n:
            await self._event(
                EventType.APPROVAL_INVALIDATED, attempt_id, None,
                {"scope": "attempt", "count": n, "reason": why},
            )
        return n

    async def invalidate_for_task(self, task_id: str, why: str) -> int:
        return await self.store.approvals.invalidate_for_task(task_id, why)

    # ------------------------------------------------------------------
    # 超时
    # ------------------------------------------------------------------

    async def expire_due(self, *, now: Any | None = None) -> list[dict[str, Any]]:
        """把超时的 pending 审批按 ``deny_pause`` 处理。

        返回每条超时的处置结果；内核据此把对应阶段置为显式等待态、释放 stream 资源。
        由周期任务或 WebSocket 心跳驱动。
        """
        moment = now or utcnow()
        open_items = await self.store.approvals.list_open()
        expired: list[dict[str, Any]] = []

        for approval in open_items:
            if approval.expires_at is None or approval.expires_at > moment:
                continue
            if approval.status != ApprovalStatus.PENDING:
                continue
            if approval.timeout_policy != ApprovalTimeoutPolicy.DENY_PAUSE:
                continue

            decision = ApprovalDecision(
                by="system", note="等待超时，按 deny_pause 策略拒绝该动作"
            )
            ok = await self.store.approvals.decide(
                approval.approval_id,
                status=ApprovalStatus.EXPIRED,
                decision=decision,
                detail="等待超时，已拒绝该动作；阶段进入显式等待态",
            )
            if not ok:
                continue

            approval.status = ApprovalStatus.EXPIRED
            approval.decision = decision
            delivered = await self._try_deliver(
                approval, ApprovalStatus.EXPIRED, None
            )
            expired.append(
                {
                    "approval_id": approval.approval_id,
                    "task_id": approval.bound_to.task_id,
                    "stage_id": approval.bound_to.stage_id,
                    "attempt_id": approval.bound_to.attempt_id,
                    "delivered": delivered,
                    "note": "断连期间的超时同样适用：用户没看到不等于默许",
                }
            )
            await self._event(
                EventType.APPROVAL_DECIDED,
                approval.approval_id,
                approval.bound_to.task_id,
                {"approved": False, "reason": "timeout", "delivered": delivered},
                stage_id=approval.bound_to.stage_id,
            )

        return expired

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def pending_for_attempt(self, attempt_id: str) -> list[Approval]:
        items = await self.store.approvals.list_for_attempt(attempt_id)
        return [a for a in items if a.is_pending()]

    async def pending_for_task(self, task_id: str) -> list[Approval]:
        return [a for a in await self.store.approvals.list_for_task(task_id) if a.is_open()]

    async def open_items(self) -> list[Approval]:
        return await self.store.approvals.list_open()

    # ------------------------------------------------------------------

    async def _event(self, type_: EventType, approval_id: str, task_id: str | None,
                     payload: dict, *, stage_id: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.store.events.append(
                scope=EventScope.APPROVAL,
                type=type_,
                actor=EventActor.SYSTEM,
                scope_id=approval_id,
                task_id=task_id,
                stage_id=stage_id,
                payload=payload,
            )

    async def _notify(self, *, kind: str, task_id: str | None, payload: dict) -> None:
        if self.notifier is None:
            return
        with contextlib.suppress(Exception):
            await self.notifier.attention_required(
                kind=kind, task_id=task_id, payload=payload
            )
