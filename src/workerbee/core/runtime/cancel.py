"""统一取消协议 cancel_chain（架构设计 v0.02 §10.4）。

所有「停止」语义复用这一条链：暂停的非原位路径、删除任务、停用撤回、Workflow 删除、
崩溃清理。把它们收敛到一处，是为了让「已接受删除 / 执行已停止 / 资源清理完成」
三个判据在每条路径上的实现完全一致（LIFE-06）——否则总有一条路径会漏掉某一态。

两级升级：先停远端流（终止继续计费），再 SIGTERM 给落盘宽限期，超时升 SIGKILL。
策略在**内核**而非适配器里，这样不同 harness 下「删除」的产品语义一致（HAR-03）；
机制（怎么杀）留给适配器，因为只有它知道自己拉起了什么。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.task import TaskStage
from ...data.event_log import EventActor, EventScope, EventType

__all__ = ["CancelReport", "cancel_chain", "CancelOutcome"]


class CancelOutcome:
    ALREADY_STOPPED = "already_stopped"
    STOPPED = "stopped"
    ESCALATED = "escalated"
    UNCONFIRMED = "unconfirmed"
    """无法确认远端真的停了。**不得谎报已停止**（LIFE-02、LIFE-06）。"""


class CancelReport(DomainModel):
    """取消链的执行结果。

    刻意把「执行已停止」与「资源清理完成」分成两个字段：它们是 LIFE-06 要求
    分开呈现的两个判据，合成一个布尔值就等于丢掉其中一个。
    """

    stage_id: str | None = None
    attempt_id: str | None = None

    outcome: str = CancelOutcome.ALREADY_STOPPED
    execution_stopped: bool = False
    """执行已停止：全部 Attempt 确认终止。"""

    resources: dict[str, int] = Field(default_factory=dict)
    """closed / teardown_failed / orphaned / skipped 三态计数。"""

    checkpoint: str | None = None
    approvals_invalidated: int = 0
    session_closed: bool = False
    detail: str | None = None

    def resources_clean(self) -> bool:
        return (
            self.resources.get("teardown_failed", 0) == 0
            and self.resources.get("orphaned", 0) == 0
        )

    def fully_complete(self) -> bool:
        """三态全部满足才算真的完成；任何一项不满足都必须继续可见（LIFE-06）。"""
        return self.execution_stopped and self.resources_clean()


async def cancel_chain(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    stage: TaskStage,
    keep_checkpoint: bool = False,
    reason: str = "cancelled",
    actor: str = "system",
    grace_seconds: float = 5.0,
    escalate: bool = True,
) -> CancelReport:
    """停止一个阶段上的执行并清理其归属资源。"""
    attempts = await store.tasks.list_attempts(stage.stage_id, descending=True)
    in_flight = next((a for a in attempts if a.outcome is None), None)

    report = CancelReport(stage_id=stage.stage_id)

    if in_flight is None:
        report.outcome = CancelOutcome.ALREADY_STOPPED
        report.execution_stopped = True
        report.resources = await ledger.close_for_stage(stage.stage_id)
        return report

    report.attempt_id = in_flight.attempt_id
    session_ref = in_flight.session_ref

    if session_ref:
        # 1) 先停远端流：终止继续计费。能否真正停止取决于厂商实现，
        #    这里如实记录「已请求」，不承诺计费已停（清单 1.3）。
        try:
            await harness.abort_stream(session_ref)
        except Exception as exc:  # noqa: BLE001
            report.detail = f"停止远端流失败：{type(exc).__name__}: {exc}"

        # 2) 软终止 + 宽限期
        with contextlib.suppress(Exception):
            await harness.terminate(session_ref, signal="TERM")

        stopped = await _await_stop(harness, session_ref, grace_seconds)

        # 3) 超时升级
        if not stopped and escalate:
            report.outcome = CancelOutcome.ESCALATED
            with contextlib.suppress(Exception):
                await harness.terminate(session_ref, signal="KILL")
            stopped = await _await_stop(harness, session_ref, grace_seconds)
        elif stopped:
            report.outcome = CancelOutcome.STOPPED
    else:
        stopped = True
        report.outcome = CancelOutcome.ALREADY_STOPPED

    if not stopped:
        # 谎报「已停止」会让用户以为资源已释放，是最危险的静默失败
        report.outcome = CancelOutcome.UNCONFIRMED
        report.detail = (
            (report.detail + "；" if report.detail else "")
            + "无法确认远端会话已终止，请人工核对"
        )

    report.execution_stopped = stopped

    # 4) 断点（暂停路径需要；删除路径不保留）
    if keep_checkpoint and session_ref:
        try:
            report.checkpoint = await harness.checkpoint(session_ref)
        except Exception as exc:  # noqa: BLE001
            report.checkpoint = None
            report.detail = (
                (report.detail + "；" if report.detail else "")
                + f"断点保存失败：{type(exc).__name__}: {exc}"
            )

    # 5) 台账遍历清理。只信台账，不看内存状态（RES-01）。
    report.resources = await ledger.close_for_attempt(in_flight.attempt_id)

    # 5b) 把这次尝试记成已结束。不做这一步，尝试会永远停留在「在途」，
    #     而「执行已停止」的判据依赖它（LIFE-06 的第二态）。
    from ..domain.task import AttemptOutcome, ErrorClass

    await store.tasks.complete_attempt(
        in_flight.attempt_id,
        outcome=AttemptOutcome(
            error_class=ErrorClass.USER_CANCELLED,
            detail=f"执行被取消（{reason}）",
            error_kind="user_cancelled",
        ),
    )

    # 6) 代次递增：此后到达的迟到回调会被按代次丢弃（REC-05）
    await store.tasks.bump_attempt_generation(in_flight.attempt_id)

    # 7) 旧批准作废：尝试失效后旧批准不能授权新动作（AC-14、HUM-04）
    report.approvals_invalidated = await store.approvals.invalidate_for_attempt(
        in_flight.attempt_id, f"执行尝试已失效（{reason}）"
    )

    # 8) 关闭会话句柄
    if session_ref:
        with contextlib.suppress(Exception):
            await harness.dispose(session_ref)
        report.session_closed = True

    await store.events.append(
        scope=EventScope.ATTEMPT,
        type=EventType.ATTEMPT_ENDED,
        actor=EventActor(actor),
        scope_id=in_flight.attempt_id,
        task_id=stage.task_id,
        stage_id=stage.stage_id,
        payload={
            "reason": reason,
            "outcome": report.outcome,
            "execution_stopped": report.execution_stopped,
            "resources": report.resources,
            "checkpoint": report.checkpoint,
            "approvals_invalidated": report.approvals_invalidated,
        },
    )
    return report


async def _await_stop(harness: Any, session_ref: str, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if not await harness.session_alive(session_ref):
                return True
        except Exception:  # noqa: BLE001 - 查不到存活就当没停，继续等
            pass
        await asyncio.sleep(0.1)
    try:
        return not await harness.session_alive(session_ref)
    except Exception:  # noqa: BLE001
        return False
