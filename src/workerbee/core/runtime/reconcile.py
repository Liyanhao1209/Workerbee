"""启动对账与断点续跑（架构设计 v0.02 §11、REC-01–05、D-08）。

四条纪律（每一条都对应一个曾经真实发生过的故障模式）：

1. **恢复 running 记录不等于确认进程存活。** 存活判定必须以 supervisor 会话台账
   为准；「数据库有记录」是意图，不是事实（§11.2、清单 §2）。
2. **恢复审批记录不等于恢复旧授权有效性。** 旧批准一律作废，重新走审批（HUM-04）。
3. **同一完成通知重复到达按 dedup_key 幂等**，不重复启动下游、不覆盖较新的结果（REC-05）。
4. **无法判断外部副作用是否完成时进入待核对，不盲目重放。** 本设计不承诺任意外部
   工具「恰好一次」（REC-04、清单 §1.3）。

先全部置 RECONCILING 再逐项核对，是为了让「核对中」成为一个用户可见的真实状态，
而不是一段「界面显示 running 但什么都不动」的静默期（OBS-01）。
"""

from __future__ import annotations

import contextlib
from typing import Any, Sequence

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.task import DesiredState, StageState, Task, TaskStage, TaskState
from ...data.event_log import EventActor, EventScope, EventType

__all__ = ["ReconcileReport", "reconcile_on_startup", "resume_from_stage"]

#: 非终态 = 需要对账的范围。CANCELLED 不在其中：已删除任务不因恢复流程复活。
_NON_TERMINAL_STAGE = (
    StageState.WAITING_DEPS,
    StageState.READY,
    StageState.DISPATCHING,
    StageState.RUNNING,
    StageState.AWAITING_APPROVAL,
    StageState.PAUSING,
    StageState.PAUSED,
    StageState.RETRYING,
    StageState.BLOCKED,
    StageState.LOST,
    StageState.RECONCILING,
)


class ReconcileReport(DomainModel):
    scanned_stages: int = 0
    reattached: list[str] = Field(default_factory=list)
    """会话仍活着：恢复监控，不重启工作。"""

    confirmed_success: list[str] = Field(default_factory=list)
    """有完成标记且产物校验通过：确认成功并推进下游。"""

    resumable: list[str] = Field(default_factory=list)
    """有断点：标记为可恢复，等待重放决策。"""

    marked_failed: list[str] = Field(default_factory=list)
    lost: list[str] = Field(default_factory=list)
    intents_enforced: dict[str, int] = Field(default_factory=dict)
    approvals_invalidated: int = 0
    notes: list[str] = Field(default_factory=list)


async def reconcile_on_startup(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    scheduler: Any | None = None,
    notifier: Any | None = None,
) -> ReconcileReport:
    """内核启动时的对账流程。"""
    report = ReconcileReport()

    await store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.RECONCILE_STARTED,
        actor=EventActor.SYSTEM,
        payload={"note": "内核启动，开始对账"},
    )

    # 0) 先守住用户意图。这一步必须最先做：如果先处理阶段再守意图，
    #    一个「用户已删除但崩溃前没写终态」的任务可能在中途被启动起来。
    report.intents_enforced = await enforce_user_intents(store=store, sm=sm, ledger=ledger)

    # 1) 把全部非终态阶段先置为「核对中」，仅接受用户控制。
    stages = await store.tasks.list_stages_in_states(_NON_TERMINAL_STAGE)
    report.scanned_stages = len(stages)
    for st in stages:
        with contextlib.suppress(Exception):
            await sm.set_stage_state(
                st,
                StageState.RECONCILING,
                reason="内核重启，正在核对真实执行状态",
                actor="system",
                requires_reconcile=True,
            )

    # 2) 逐项核对
    for st in stages:
        fresh = await store.tasks.get_stage(st.stage_id)
        if fresh is None or fresh.observed_state != StageState.RECONCILING:
            continue
        task = await store.tasks.get_task(fresh.task_id)
        if task is None:
            continue
        if task.desired_state == DesiredState.CANCELLED or task.observed_state == TaskState.CANCELLED:
            continue

        await _reconcile_one(
            store=store, sm=sm, harness=harness, ledger=ledger,
            scheduler=scheduler, task=task, stage=fresh, report=report,
        )

    # 3) 审批：恢复记录 ≠ 恢复授权。一律作废旧批准，需要时重新征求（HUM-04）。
    open_approvals = await store.approvals.list_open()
    for appr in open_approvals:
        with contextlib.suppress(Exception):
            await store.approvals.decide(
                appr.approval_id,
                status=_superseded(),
                decision=_system_decision(),
                detail="内核重启，旧批准不能授权新动作，需重新征求",
            )
    report.approvals_invalidated = len(open_approvals)

    # 4) 台账对账：回收孤儿资源（RES-02）
    try:
        orphans = await ledger.scan_orphans()
        for item in orphans.get("orphans", []):
            await ledger.close_for_task(item.get("owner_task_id") or "")
        if orphans.get("orphans"):
            report.notes.append(
                f"发现 {len(orphans['orphans'])} 项归属已终结的资源，已尝试回收"
            )
    except Exception as exc:  # noqa: BLE001 - 对账失败不能阻断启动
        report.notes.append(f"资源对账失败：{type(exc).__name__}: {exc}")

    # 5) 任务聚合状态重算
    for task in await store.tasks.list_live_tasks():
        if task.observed_state in (TaskState.RECONCILING, TaskState.PAUSING, TaskState.CANCELLING):
            continue
        fresh = await store.tasks.get_task(task.task_id)
        if fresh is None:
            continue
        from .advance import evaluate_task_state

        target = await evaluate_task_state(store=store, sm=sm, task=fresh)
        if target is not None and target != fresh.observed_state:
            with contextlib.suppress(Exception):
                await sm.set_task_state(
                    fresh, target, reason="启动对账后重算", actor="system"
                )

    await store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.RECONCILE_RESULT,
        actor=EventActor.SYSTEM,
        payload=report.model_dump(mode="json"),
    )

    if notifier is not None and (report.lost or report.marked_failed):
        with contextlib.suppress(Exception):
            await notifier.attention_required(
                kind="reconcile_issues",
                task_id=None,
                payload={
                    "lost": report.lost,
                    "failed": report.marked_failed,
                    "note": "重启对账发现需要人工核对的状态",
                },
            )
    return report


async def _reconcile_one(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    scheduler: Any | None,
    task: Task,
    stage: TaskStage,
    report: ReconcileReport,
) -> None:
    attempts = await store.tasks.list_attempts(stage.stage_id, descending=True)
    attempt = attempts[0] if attempts else None

    if attempt is None:
        # 从没派发过：按依赖现状决定回到等待还是就绪
        target = (
            StageState.READY
            if await _deps_ok(store, task, stage)
            else StageState.WAITING_DEPS
        )
        await sm.set_stage_state(
            stage, target, reason="从未派发，直接回到队列", actor="system",
            requires_reconcile=False,
        )
        return

    # 情况 A：会话仍活着且租约新鲜 → 恢复监控，不重启工作
    if attempt.session_ref and attempt.outcome is None:
        alive = False
        with contextlib.suppress(Exception):
            alive = bool(await harness.session_alive(attempt.session_ref))
        if alive:
            resumed = False
            if scheduler is not None:
                resumed = await _reattach(scheduler, attempt, stage)
            await sm.set_stage_state(
                stage,
                StageState.RUNNING if resumed else StageState.RECONCILING,
                reason="会话仍存活，已恢复监控（未重启工作）"
                if resumed
                else "会话仍存活，但无法恢复监控，等待人工处理",
                actor="system",
                requires_reconcile=not resumed,
            )
            (report.reattached if resumed else report.lost).append(stage.stage_id)
            if attempt.generation > 1:
                report.notes.append(
                    f"阶段 {stage.stage_id[:8]} 的尝试已被清理过（代次 {attempt.generation}），"
                    f"迟到的输出将被丢弃"
                )
            return

    # 情况 B：有完成标记且产物已验证 → 确认成功并按 dedup_key 推进
    if attempt.outcome is not None and attempt.outcome.error_class.value == "success":
        artifacts = await store.artifacts.resolve_pin(
            stage.stage_id, attempt.attempt_seq
        )
        if artifacts or not _requires_artifacts(task, stage):
            await sm.set_stage_state(
                stage,
                StageState.SUCCEEDED,
                reason="重启对账：完成标记与产物均已确认",
                actor="system",
                requires_reconcile=False,
            )
            from .advance import on_stage_succeeded

            await on_stage_succeeded(
                store=store, sm=sm, task=task, stage=stage, attempt=attempt,
                artifacts=artifacts,
            )
            report.confirmed_success.append(stage.stage_id)
            return
        report.notes.append(
            f"阶段 {stage.stage_id[:8]} 有完成标记但产物缺失，进入待核对"
        )
        await sm.set_stage_state(
            stage, StageState.LOST,
            reason="完成标记与产物不一致，需人工核对",
            actor="system",
        )
        report.lost.append(stage.stage_id)
        return

    # 情况 C：有断点 → 标记为可恢复，等待重放决策（不自动重放）
    if stage.checkpoint_ref:
        await sm.set_stage_state(
            stage,
            StageState.PAUSED,
            reason="重启对账：发现可用断点，等待用户决定是否继续",
            actor="system",
            status_reason="可从断点恢复，等待确认",
            requires_reconcile=False,
        )
        report.resumable.append(stage.stage_id)
        return

    # 情况 D：无法确认外部副作用是否已经发生 → 进入待核对，不盲目重放（REC-04）
    if attempt.outcome is None and attempt.session_ref is None:
        # 会话都没建起来，重放是安全的
        target = (
            StageState.READY if await _deps_ok(store, task, stage) else StageState.WAITING_DEPS
        )
        await sm.set_stage_state(
            stage, target,
            reason="重启对账：会话从未建立，安全地放回队列",
            actor="system", requires_reconcile=False,
        )
        return

    await sm.set_stage_state(
        stage,
        StageState.LOST,
        reason=(
            "内核重启，无法确认该阶段的外部副作用是否已经发生；"
            "已保留为「状态不明」等待核对，不自动重放"
        ),
        actor="system",
        requires_reconcile=False,
    )
    report.lost.append(stage.stage_id)


async def _reattach(scheduler: Any, attempt: Any, stage: TaskStage) -> bool:
    """把仍在跑的尝试重新挂回调度器的在途表。"""
    from .scheduler import AttemptRuntime

    try:
        rt = AttemptRuntime(
            attempt_id=attempt.attempt_id,
            stage_id=stage.stage_id,
            task_id=stage.task_id,
            node_id=stage.node_id,
            session_ref=attempt.session_ref,
        )
        scheduler._runtimes[attempt.attempt_id] = rt
        await scheduler.store.tasks.update_attempt(attempt.attempt_id, reattached=True)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _deps_ok(store: Any, task: Task, stage: TaskStage) -> bool:
    required = task.graph_snapshot.effective_predecessors(stage.node_id)
    if not required:
        return True
    stages = {s.node_id: s for s in await store.tasks.list_stages(task.task_id)}
    return all(
        stages.get(n) is not None and stages[n].observed_state == StageState.SUCCEEDED
        for n in required
    )


def _requires_artifacts(task: Task, stage: TaskStage) -> bool:
    """有效出边是否声明了输出契约——声明了就必须有产物才算成功。"""
    for succ in task.graph_snapshot.effective_successors(stage.node_id):
        edge = task.graph_snapshot.graph.edge(stage.node_id, succ)
        if edge is not None and edge.output_contract is not None:
            return True
    return False


async def enforce_user_intents(*, store: Any, sm: Any, ledger: Any) -> dict[str, int]:
    """守住用户的控制意图。崩溃、重启、迟到事件都不得翻转它们（REC-05、清单 §2）。

    - ``desired=PAUSED`` 的任务保持暂停：不得因系统重启自动运行。
    - ``desired=CANCELLED`` 的任务不复活：已删除就是已删除。
    """
    counts = {"paused_held": 0, "cancelled_held": 0, "cleaned": 0}
    for task in await store.tasks.list_live_tasks():
        if task.desired_state == DesiredState.CANCELLED and task.observed_state != TaskState.CANCELLED:
            # 崩溃前删除没走完：继续走完，而不是让它复活
            await sm.set_task_state(
                task, TaskState.CANCELLING, reason="重启后继续完成已接受的删除", actor="system"
            )
            counts["cancelled_held"] += 1
            # 未终结的阶段一律取消；在途会话的进程由 Reaper 按台账回收
            await sm.cancel_stages_of_task(task.task_id)
            await ledger.close_for_task(task.task_id)
            await sm.set_task_state(
                task, TaskState.CANCELLED, reason="删除已完成", actor="system"
            )
            counts["cleaned"] += 1
        elif task.desired_state == DesiredState.PAUSED and task.observed_state not in (
            TaskState.PAUSED,
            TaskState.PAUSING,
        ):
            await sm.set_task_state(
                task, TaskState.PAUSING, reason="重启后保持用户暂停意图", actor="system"
            )
            for st in await store.tasks.list_stages(task.task_id):
                if st.observed_state in (StageState.READY, StageState.WAITING_DEPS, StageState.RECONCILING):
                    with contextlib.suppress(Exception):
                        await sm.set_stage_state(
                            st, StageState.PAUSED, reason="用户已暂停", actor="system"
                        )
            await sm.set_task_state(
                task, TaskState.PAUSED, reason="重启后保持用户暂停意图", actor="system"
            )
            counts["paused_held"] += 1
    return counts


# ===========================================================================
# 断点续跑（§11.3）
# ===========================================================================


async def resume_from_stage(
    *,
    store: Any,
    sm: Any,
    task_id: str,
    node_id: str,
    actor: str = "user",
) -> dict[str, Any]:
    """从某个失败/中断的阶段重放。

    输入 = 该节点有效上游的产物引用（内容寻址、不可变）+ 发射时钉扎的配置快照。
    新 Attempt 继承血缘，历史不污染。

    **主动删除的任务没有这个入口**（清单 §2 与 §4 的区分：恢复只针对故障，
    不针对用户主动删除）。
    """
    task = await store.tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task.desired_state == DesiredState.CANCELLED or task.observed_state == TaskState.CANCELLED:
        raise ValueError("已主动删除的任务不允许续跑；再次提交相同输入是新任务（LIFE-04）")

    stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
    stage = stages.get(node_id)
    if stage is None:
        raise ValueError(f"本任务中没有节点 {node_id} 的阶段")

    if stage.observed_state not in (
        StageState.FAILED,
        StageState.BLOCKED,
        StageState.LOST,
        StageState.PAUSED,
    ):
        raise ValueError(
            f"阶段当前状态为 {stage.observed_state.value}，不支持重放"
        )

    if await sm.set_stage_state(
        stage, StageState.READY, reason="用户要求从本阶段重放", actor=actor
    ):
        # 下游之前因它失败而被阻断，现在重放意味着结果可能改变；把它们放回等待，
        # 让依赖推进在它真的成功之后重新放行（而不是立刻无条件唤醒）。
        for succ in task.graph_snapshot.effective_successors(node_id):
            down = stages.get(succ)
            if down is not None and down.observed_state == StageState.BLOCKED:
                with contextlib.suppress(Exception):
                    await sm.set_stage_state(
                        down, StageState.WAITING_DEPS,
                        reason="上游正在重放，等待新的结果", actor=actor,
                        blocked_reason=None,
                    )

    if task.observed_state in (TaskState.FAILED, TaskState.BLOCKED):
        with contextlib.suppress(Exception):
            await sm.set_task_state(task, TaskState.RUNNING, reason="用户重放某阶段", actor=actor)

    return {
        "task_id": task_id,
        "node_id": node_id,
        "stage_id": stage.stage_id,
        "had_checkpoint": bool(stage.checkpoint_ref),
        "note": (
            "将从断点继续" if stage.checkpoint_ref else "没有可用断点，将从头重跑本阶段"
        ),
    }


def _superseded():
    from ..domain.approval import ApprovalStatus

    return ApprovalStatus.SUPERSEDED


def _system_decision():
    from ..domain.approval import ApprovalDecision

    return ApprovalDecision(by="system", note="内核重启，旧批准失效")
