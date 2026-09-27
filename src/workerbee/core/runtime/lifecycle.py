"""生命周期控制：暂停、恢复、删除、启停、调序（架构设计 v0.02 §10、LIFE-01–06、D-01）。

**操作范围**：第一版暂停与删除默认作用于**整次任务**，覆盖其并行分支；
从某节点发起时，节点只是定位任务与记录来源的入口（``origin_of_control``）。
不提供分支级单独取消——保留扩展位，但不与整任务操作共用含混文案（§10.1、清单 §3.9）。

**完成判据三态分离**（LIFE-06）：所有操作都返回
「已接受 / 执行已停止 / 资源清理完成」三个独立结论，绝不合成一个布尔值。
清理失败或无法确认的远端工作保持可见并提供继续处理入口。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Sequence

from pydantic import Field

from ..domain.base import DomainModel, utcnow
from ..domain.task import (
    DesiredState,
    OriginOfControl,
    StageState,
    Task,
    TaskStage,
    TaskState,
)
from ..graph.derive import preview_toggle
from ...data.event_log import EventActor, EventScope, EventType
from .cancel import CancelReport, cancel_chain
from .launch import to_actor

__all__ = [
    "PauseReport",
    "ResumeReport",
    "DeleteReport",
    "WorkflowDeleteReport",
    "NodeToggleReport",
    "ReorderReport",
    "pause_task",
    "resume_task",
    "delete_task",
    "delete_workflow",
    "set_node_enabled",
    "reorder_stages",
]


class _TriState(DomainModel):
    """三态结论的公共形状。"""

    accepted: bool = False
    """已接受：控制意图写入成功（desired_state 已落库）。"""

    execution_stopped: bool = False
    """执行已停止：全部在途 Attempt 确认终止。"""

    resources: dict[str, int] = Field(default_factory=dict)
    """closed / teardown_failed / orphaned / skipped 计数。"""

    warnings: list[str] = Field(default_factory=list)
    """无法确认或需要用户知悉的事实。空列表表示没有保留项。"""

    def resources_clean(self) -> bool:
        return (
            self.resources.get("teardown_failed", 0) == 0
            and self.resources.get("orphaned", 0) == 0
        )

    def fully_complete(self) -> bool:
        return self.accepted and self.execution_stopped and self.resources_clean()


class PauseReport(_TriState):
    task_id: str = ""
    paused_immediately: list[str] = Field(default_factory=list)
    """未启动就被暂停的阶段（零成本，无需停止任何东西）。"""

    stopped_cooperatively: list[str] = Field(default_factory=list)
    """走协作停止的阶段。可能有重复工作，UI 必须说明（LIFE-02）。"""

    paused_in_place: list[str] = Field(default_factory=list)
    checkpoints: dict[str, str | None] = Field(default_factory=dict)
    """阶段 → 断点。值为 None 表示只能从头重跑本阶段，必须如实展示。"""

    unsupported: list[str] = Field(default_factory=list)
    """harness 不支持暂停的阶段。收到暂停请求如实拒绝，不得显示为已暂停。"""


class ResumeReport(_TriState):
    task_id: str = ""
    requeued: list[str] = Field(default_factory=list)
    restarted: list[str] = Field(default_factory=list)
    """从断点或从头重建的阶段——用户需要知道「哪些要重做」。"""

    reused_sessions: list[str] = Field(default_factory=list)


class DeleteReport(_TriState):
    task_id: str = ""
    cancelled_stages: list[str] = Field(default_factory=list)
    preserved_succeeded: list[str] = Field(default_factory=list)
    """已完成阶段以其**真实结果**留在历史，不改写为失败（LIFE-04、OBS-03）。"""


class WorkflowDeleteReport(_TriState):
    workflow_id: str = ""
    tasks_terminated: list[str] = Field(default_factory=list)
    shared_configs_kept: list[str] = Field(default_factory=list)
    """仍被其他 Workflow 引用的共享配置不予删除（LIFE-05）。"""


class NodeToggleReport(DomainModel):
    workflow_id: str = ""
    node_id: str = ""
    enabling: bool = False
    mode: str = "drain"
    applied: bool = False
    awaiting_drain: bool = False
    new_revision_seq: int | None = None

    affected_tasks: list[str] = Field(default_factory=list)
    drained_stages: list[str] = Field(default_factory=list)
    withdrawn_stages: list[str] = Field(default_factory=list)
    revived_stages: list[str] = Field(default_factory=list)
    added_edges: list[tuple[str, str]] = Field(default_factory=list)
    removed_edges: list[tuple[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReorderReport(DomainModel):
    node_id: str = ""
    applied: bool = False
    effective_order: list[str] = Field(default_factory=list)
    """实际生效的顺序。与请求不符时前端据此显示真相，而不是显示自己的乐观假设。"""

    rejected: list[dict[str, str]] = Field(default_factory=list)
    reason: str | None = None


# ===========================================================================
# 暂停 / 恢复
# ===========================================================================

#: 暂停时需要处理的在途状态。READY 单独处理——它没启动，零成本。
_IN_FLIGHT = (
    StageState.DISPATCHING,
    StageState.RUNNING,
    StageState.RETRYING,
    StageState.AWAITING_APPROVAL,
)


async def pause_task(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    task_id: str,
    origin_node_id: str | None = None,
    reason: str | None = None,
    actor: str = "user",
    grace_seconds: float = 5.0,
) -> PauseReport:
    """暂停整次任务（LIFE-01/02）。"""
    task = await store.tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task.observed_state in (TaskState.CANCELLED, TaskState.CANCELLING):
        raise ValueError("任务已被删除，不能再暂停（已删除任务无续跑入口）")
    if task.observed_state in (TaskState.SUCCEEDED, TaskState.FAILED):
        raise ValueError(f"任务已终结（{task.observed_state.value}），无法暂停")

    report = PauseReport(task_id=task_id)

    # 1) 先写控制意图并 bump epoch：此后所有用旧 epoch 提交的迁移都会失败（AC-11）
    await sm.set_task_desired(
        task,
        DesiredState.PAUSED,
        origin=OriginOfControl(
            op="pause", from_node_id=origin_node_id, scope="task", detail=reason
        ),
        reason=reason,
        actor=actor,
    )
    await sm.set_task_state(task, TaskState.PAUSING, reason=reason, actor=actor)
    report.accepted = True

    stages = await store.tasks.list_stages(task_id)

    # 2) 未启动的：零成本直接置 PAUSED
    for st in stages:
        if st.observed_state == StageState.READY:
            if await sm.set_stage_state(
                st,
                StageState.PAUSED,
                reason="任务暂停时尚未启动",
                actor=actor,
                status_reason="已暂停（未启动）",
            ):
                report.paused_immediately.append(st.stage_id)

    # 3) 在途的：按 harness 能力逐档处理（D-07）
    stopped_ok = True
    for st in stages:
        if st.observed_state not in _IN_FLIGHT:
            continue

        if not await sm.set_stage_state(
            st, StageState.PAUSING, reason="任务暂停中", actor=actor
        ):
            continue

        attempt = await _current_attempt(store, st)
        if attempt is None or not attempt.session_ref:
            await sm.set_stage_state(st, StageState.PAUSED, reason="无需停止", actor=actor)
            report.paused_immediately.append(st.stage_id)
            continue

        node = task.graph_snapshot.graph.node(st.node_id)
        harness_id = (
            node.profiles[st.profile_cursor].harness_ref
            if node and st.profile_cursor < len(node.profiles)
            else None
        )
        caps = await harness.capabilities(harness_id or "")

        if caps.pause_in_place:
            try:
                ok = await harness.pause(attempt.session_ref)
            except Exception:  # noqa: BLE001 - 不支持时如实降级，不假装成功
                ok = False
            if ok:
                if await sm.set_stage_state(
                    st,
                    StageState.PAUSED,
                    reason="原位暂停成功",
                    actor=actor,
                    status_reason="已原位暂停",
                ):
                    report.paused_in_place.append(st.stage_id)
                continue
            report.warnings.append(
                f"节点「{st.node_name or st.node_id[:8]}」声称支持原位暂停但调用失败，"
                f"已降级为协作停止"
            )

        # 协作停止：尽量留断点，留不到就明说会从头重跑（LIFE-02）
        cancel = await cancel_chain(
            store=store,
            sm=sm,
            harness=harness,
            ledger=ledger,
            stage=st,
            keep_checkpoint=caps.checkpoint_resume,
            reason=reason or "任务暂停",
            actor=actor,
            grace_seconds=grace_seconds,
        )
        report.checkpoints[st.stage_id] = cancel.checkpoint
        report.stopped_cooperatively.append(st.stage_id)
        _merge_resources(report.resources, cancel.resources)

        if not cancel.execution_stopped:
            stopped_ok = False
            report.warnings.append(
                f"节点「{st.node_name or st.node_id[:8]}」无法确认已停止；"
                f"界面将如实显示为「未确认」，不计入已暂停"
            )
            with contextlib.suppress(Exception):
                await sm.set_stage_state(
                    st,
                    StageState.LOST,
                    reason="暂停时无法确认远端执行是否已停止",
                    actor=actor,
                )
            continue

        if cancel.checkpoint:
            await store.tasks.update_stage(st.stage_id, checkpoint_ref=cancel.checkpoint)
            report.warnings.append(
                f"节点「{st.node_name or st.node_id[:8]}」保留断点，恢复时从断点继续"
            )
        elif caps.checkpoint_resume:
            report.warnings.append(
                f"节点「{st.node_name or st.node_id[:8]}」未能取得断点，"
                f"恢复时将从头重跑本阶段"
            )
        else:
            report.warnings.append(
                f"节点「{st.node_name or st.node_id[:8]}」的 harness 不支持断点恢复，"
                f"恢复时将从头重跑本阶段"
            )

        await sm.set_stage_state(
            st,
            StageState.PAUSED,
            reason="协作停止完成",
            actor=actor,
            status_reason="已暂停（协作停止）",
        )

    report.execution_stopped = stopped_ok and not _has_in_flight(
        await store.tasks.list_stages(task_id)
    )

    if report.execution_stopped:
        await sm.set_task_state(task, TaskState.PAUSED, reason="全部阶段已停止", actor=actor)
        await store.events.append(
            scope=EventScope.TASK,
            type=EventType.TASK_CONTROL,
            actor=to_actor(actor),
            scope_id=task_id,
            task_id=task_id,
            payload={
                "op": "pause",
                "from_node_id": origin_node_id,
                "result": "paused",
                "paused_immediately": len(report.paused_immediately),
                "stopped_cooperatively": len(report.stopped_cooperatively),
                "paused_in_place": len(report.paused_in_place),
                "resources": report.resources,
            },
        )

    return report


async def resume_task(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    task_id: str,
    actor: str = "user",
    restart_failed: bool = False,
) -> ResumeReport:
    """恢复被暂停的任务（LIFE-03）。

    未启动阶段直接重入就绪集；被协作停止的阶段创建新 Attempt，
    输入 = 钉扎快照 + 已确认上游产物 + 断点（如有）。**不重跑已成功阶段。**
    """
    task = await store.tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task.observed_state == TaskState.CANCELLED:
        raise ValueError("已删除任务没有续跑入口（LIFE-04）")
    if task.observed_state not in (TaskState.PAUSED, TaskState.PAUSING, TaskState.BLOCKED):
        raise ValueError(f"任务当前状态为 {task.observed_state.value}，无法恢复")

    report = ResumeReport(task_id=task_id)
    await sm.set_task_desired(task, DesiredState.ACTIVE, reason="用户恢复", actor=actor)
    report.accepted = True

    stages = await store.tasks.list_stages(task_id)
    for st in stages:
        if st.observed_state in (StageState.PAUSED, StageState.BLOCKED):
            if await sm.set_stage_state(
                st,
                StageState.READY,
                reason="任务恢复",
                actor=actor,
                status_reason="恢复中",
            ):
                report.requeued.append(st.stage_id)
                if st.current_attempt_seq > 0:
                    report.restarted.append(st.stage_id)
                    if st.checkpoint_ref:
                        report.warnings.append(
                            f"节点「{st.node_name or st.node_id[:8]}」将从断点继续"
                        )
                    else:
                        report.warnings.append(
                            f"节点「{st.node_name or st.node_id[:8]}」无断点，将从头重跑本阶段"
                        )
        elif st.observed_state == StageState.FAILED and restart_failed:
            if await sm.set_stage_state(
                st, StageState.READY, reason="用户要求重跑失败阶段", actor=actor
            ):
                report.requeued.append(st.stage_id)
                report.restarted.append(st.stage_id)

    report.execution_stopped = True  # 恢复路径不涉及停止
    await sm.set_task_state(task, TaskState.RUNNING, reason="用户恢复", actor=actor)
    await store.events.append(
        scope=EventScope.TASK,
        type=EventType.RESUME_REQUESTED,
        actor=to_actor(actor),
        scope_id=task_id,
        task_id=task_id,
        payload={
            "requeued": len(report.requeued),
            "restarted": len(report.restarted),
        },
    )
    return report


# ===========================================================================
# 删除
# ===========================================================================


async def delete_task(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    task_id: str,
    origin_node_id: str | None = None,
    reason: str | None = None,
    actor: str = "user",
    grace_seconds: float = 5.0,
) -> DeleteReport:
    """主动删除任务（LIFE-04/06）。

    删除只终止后续执行，不承诺撤销已写入项目文件或已发生的外部副作用。
    """
    task = await store.tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task.observed_state == TaskState.CANCELLED:
        report = DeleteReport(task_id=task_id, accepted=True, execution_stopped=True)
        return report

    report = DeleteReport(task_id=task_id)
    await sm.set_task_desired(
        task,
        DesiredState.CANCELLED,
        origin=OriginOfControl(
            op="delete", from_node_id=origin_node_id, scope="task", detail=reason
        ),
        reason=reason,
        actor=actor,
    )
    await sm.set_task_state(task, TaskState.CANCELLING, reason=reason, actor=actor)
    report.accepted = True

    # 1) 停掉所有在途执行（不保留断点——删除后没有续跑入口）
    stages = await store.tasks.list_stages(task_id)
    all_stopped = True
    for st in stages:
        if st.observed_state in _IN_FLIGHT or st.observed_state == StageState.PAUSING:
            cancel = await cancel_chain(
                store=store,
                sm=sm,
                harness=harness,
                ledger=ledger,
                stage=st,
                keep_checkpoint=False,
                reason=reason or "任务被删除",
                actor=actor,
                grace_seconds=grace_seconds,
            )
            _merge_resources(report.resources, cancel.resources)
            if not cancel.execution_stopped:
                all_stopped = False
                report.warnings.append("有会话无法确认已终止，已保持为「状态不明」待核对")
        elif st.observed_state == StageState.SUCCEEDED:
            # 已完成阶段保留其真实结果，不改写为失败（LIFE-04、OBS-03）
            report.preserved_succeeded.append(st.stage_id)

    # 2) 其余非终态阶段一并取消。
    #    必须**重新读取**：上面那轮取消链改变了在途阶段的状态，
    #    用旧列表判断会把它们漏掉，留下永远停在 RUNNING 的僵尸阶段。
    for st in await store.tasks.list_stages(task_id):
        if st.observed_state in (
            StageState.SUCCEEDED,
            StageState.FAILED,
            StageState.SKIPPED,
            StageState.CANCELLED,
        ):
            continue
        if await sm.set_stage_state(
            st, StageState.CANCELLED, reason=reason or "任务被删除", actor=actor
        ):
            report.cancelled_stages.append(st.stage_id)

    report.execution_stopped = all_stopped
    # 3) 任务级资源遍历清理（跨阶段的共享资源）
    task_resources = await ledger.close_for_task(task_id)
    _merge_resources(report.resources, task_resources)

    # 4) 丢未消费的投递，防止迟到消息唤醒已删除的任务（REC-05）
    await store.messages.purge_task(task_id)
    await store.approvals.invalidate_for_task(task_id, "任务已被删除")

    await sm.set_task_state(task, TaskState.CANCELLED, reason=reason, actor=actor)
    await store.events.append(
        scope=EventScope.TASK,
        type=EventType.TASK_CONTROL,
        actor=to_actor(actor),
        scope_id=task_id,
        task_id=task_id,
        payload={
            "op": "delete",
            "from_node_id": origin_node_id,
            "execution_stopped": report.execution_stopped,
            "resources": report.resources,
            "cancelled_stages": len(report.cancelled_stages),
            "preserved_succeeded": len(report.preserved_succeeded),
        },
    )
    return report


async def delete_workflow(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    workflow_id: str,
    actor: str = "user",
) -> WorkflowDeleteReport:
    """删除 Workflow（LIFE-05）。

    顺序：停止接受新提交 → 终止全部未结束任务 → 清理归属资源 → 状态置 deleted。
    定义与任务历史保留供回看；**不恢复**被主动删除的 Workflow。
    """
    workflow = await store.workflows.get(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow 不存在: {workflow_id}")

    report = WorkflowDeleteReport(workflow_id=workflow_id)

    # 1) 先落 status=deleted 之外的一步：置为 archived 以立刻停止接受新提交，
    #    同时避免「删除到一半崩溃」留下一个仍可提交的 Workflow。
    await store.workflows.update(workflow_id, status="archived")
    report.accepted = True

    # 2) 终止全部未结束任务
    live = await store.tasks.list_live_tasks()
    mine = [t for t in live if t.workflow_id == workflow_id]
    all_stopped = True
    for task in mine:
        try:
            sub = await delete_task(
                store=store, sm=sm, harness=harness, ledger=ledger,
                task_id=task.task_id, reason=f"Workflow「{workflow.name}」被删除",
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不应阻断整体删除
            all_stopped = False
            report.warnings.append(
                f"任务 {task.task_id[:8]} 终止失败：{type(exc).__name__}: {exc}"
            )
            continue
        report.tasks_terminated.append(task.task_id)
        _merge_resources(report.resources, sub.resources)
        if not sub.fully_complete():
            all_stopped = False
            report.warnings.extend(sub.warnings)

    report.execution_stopped = all_stopped

    # 3) 共享配置的影响面：仍被其他 Workflow 引用的不予删除（LIFE-05）
    others = [
        w for w in await store.workflows.list()
        if w.workflow_id != workflow_id and w.status.value != "deleted"
    ]
    if others:
        report.shared_configs_kept.append(
            f"{len(others)} 个其他 Workflow 仍可能引用共享配置（Skill／工具／凭据／harness），"
            f"这些配置未被删除"
        )

    # 4) 状态置 deleted，从可用列表移除；定义与历史保留供回看
    await store.workflows.update(workflow_id, status="deleted")
    await store.events.append(
        scope=EventScope.WORKFLOW,
        type=EventType.WORKFLOW_DELETED,
        actor=to_actor(actor),
        scope_id=workflow_id,
        payload={
            "tasks_terminated": len(report.tasks_terminated),
            "resources": report.resources,
            "execution_stopped": report.execution_stopped,
        },
    )
    return report


# ===========================================================================
# 节点启停（D-01）
# ===========================================================================


async def set_node_enabled(
    *,
    store: Any,
    sm: Any,
    harness: Any,
    ledger: Any,
    workflow_id: str,
    node_id: str,
    enable: bool,
    mode: str = "drain",
    actor: str = "user",
    reason: str | None = None,
    grace_seconds: float = 5.0,
) -> NodeToggleReport:
    """启用／停用节点（ACT-01–04、D-01）。

    ``mode`` 仅对停用有意义：

    - ``drain``（默认）：节点置 draining，正在执行的阶段跑完、排队阶段保留，
      不再领取新阶段；全部存量结束后才翻转 enabled 并产生新修订。不丢弃已完成工作。
    - ``immediate``：对应原 Proposal 的「撤回已有 request」——在途阶段走取消链
      （不保留断点），排队阶段标记 SKIPPED，enabled 立即翻转。

    **生效范围**：在途任务按发射时钉扎的有效图执行至结束（架构设计 §4.1）。
    启停产生的新修订只影响之后发射的任务；这保证「不重复推进、不无说明跳过」。
    """
    workflow = await store.workflows.get(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow 不存在: {workflow_id}")
    revision = await store.workflows.get_current_revision(workflow_id)
    if revision is None:
        raise ValueError("Workflow 没有可用的修订版本")

    node = revision.graph.node(node_id)
    if node is None:
        raise ValueError(f"节点不存在于当前修订: {node_id}")

    if node.enabled == enable:
        return NodeToggleReport(
            workflow_id=workflow_id,
            node_id=node_id,
            enabling=enable,
            mode=mode,
            applied=True,
            warnings=["节点已处于该状态，未产生新修订"],
        )

    delta = preview_toggle(revision.graph, node_id, enable)
    report = NodeToggleReport(
        workflow_id=workflow_id,
        node_id=node_id,
        enabling=enable,
        mode=mode,
        added_edges=[(e.from_node, e.to_node) for e in delta.added_edges],
        removed_edges=[(e.from_node, e.to_node) for e in delta.removed_edges],
    )

    await store.events.append(
        scope=EventScope.WORKFLOW,
        type=EventType.NODE_ACTIVATION_REQUESTED,
        actor=to_actor(actor),
        scope_id=workflow_id,
        payload={
            "node_id": node_id,
            "node_name": node.name,
            "enabling": enable,
            "mode": mode,
            "reason": reason,
            "added_edges": report.added_edges,
            "removed_edges": report.removed_edges,
        },
    )

    if not enable and mode == "immediate":
        # 立即撤回：把该节点上所有未结束阶段按任务逐个走取消链
        for st in await _stages_of_node(store, node_id, terminal=False):
            task = await store.tasks.get_task(st.task_id)
            if task is None or task.observed_state in (
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            ):
                continue
            report.affected_tasks.append(task.task_id)
            if st.observed_state in _IN_FLIGHT:
                cancel = await cancel_chain(
                    store=store, sm=sm, harness=harness, ledger=ledger, stage=st,
                    keep_checkpoint=False, reason=reason or "节点被立即停用",
                    actor=actor, grace_seconds=grace_seconds,
                )
                if not cancel.execution_stopped:
                    report.warnings.append(
                        f"节点「{node.name}」上有会话无法确认已停止"
                    )
                with contextlib.suppress(Exception):
                    await sm.set_stage_state(
                        st, StageState.SKIPPED, reason="节点被立即停用", actor=actor
                    )
                    report.withdrawn_stages.append(st.stage_id)
            elif st.observed_state in (
                StageState.WAITING_DEPS,
                StageState.READY,
                StageState.RETRYING,
                StageState.PAUSED,
            ):
                if await sm.set_stage_state(
                    st, StageState.SKIPPED, reason="节点被立即停用", actor=actor
                ):
                    report.withdrawn_stages.append(st.stage_id)

    elif not enable and mode == "drain":
        # 排水：只登记一个过渡操作，不翻转 enabled。
        # 正在跑的阶段会自然跑完；调度器因节点处于 draining 而不再领取新阶段。
        pending = [
            s.stage_id
            for s in await _stages_of_node(store, node_id, terminal=False)
            if s.observed_state in _IN_FLIGHT
        ]
        import json as _json

        await store.db.execute(
            """INSERT INTO node_activation_op(op_id, workflow_id, revision_seq, node_id,
                   enabling, mode, state, pending_stage_ids, detail, created_at, updated_at)
               VALUES (?,?,?,?,0,?,?,?,?,?,?)""",
            (
                _new_id(),
                workflow_id,
                revision.revision_seq,
                node_id,
                mode,
                "draining" if pending else "drain_complete",
                _json.dumps(pending),
                reason,
                utcnow().isoformat(),
                utcnow().isoformat(),
            ),
        )
        if pending:
            report.awaiting_drain = True
            report.warnings.append(
                f"节点「{node.name}」进入排水：{len(pending)} 个在途阶段将跑完，"
                f"期间不再领取新阶段；全部结束后自动翻转启用状态"
            )
            return report

    # 启用：立即产生新修订
    new_seq = await _commit_enabled_change(
        store=store, workflow=workflow, revision=revision, node_id=node_id,
        enable=enable, actor=actor, reason=reason or ("启用节点" if enable else "停用节点"),
    )
    report.applied = True
    report.new_revision_seq = new_seq

    if enable:
        # 重新启用时把被立即撤回跳过的阶段拉回队列（ACT-04：停用不等于删除这些任务）
        for st in await _stages_of_node(store, node_id, terminal=False, states=[StageState.SKIPPED]):
            if await sm.set_stage_state(
                st, StageState.READY, reason="节点重新启用", actor=actor
            ):
                report.revived_stages.append(st.stage_id)

    await store.events.append(
        scope=EventScope.WORKFLOW,
        type=EventType.NODE_ACTIVATION_APPLIED,
        actor=to_actor(actor),
        scope_id=workflow_id,
        payload={
            "node_id": node_id,
            "enabling": enable,
            "mode": mode,
            "new_revision_seq": new_seq,
            "revived": len(report.revived_stages),
        },
    )
    return report


async def complete_drain_ops(*, store: Any, sm: Any, actor: str = "system") -> list[dict]:
    """检查排水中的节点是否已排空；是则翻转 enabled 并产生新修订。

    由调度循环周期调用。排水是**有状态的过渡过程**，必须可查、可续、可见。
    """
    import json as _json

    rows = await store.db.fetch_all(
        "SELECT * FROM node_activation_op WHERE state='draining'"
    )
    done: list[dict] = []
    for row in rows:
        node_id = row["node_id"]
        remaining = [
            s for s in await _stages_of_node(store, node_id, terminal=False)
            if s.observed_state in _IN_FLIGHT
        ]
        if remaining:
            continue

        workflow = await store.workflows.get(row["workflow_id"])
        revision = await store.workflows.get_current_revision(row["workflow_id"])
        if workflow is None or revision is None:
            await store.db.execute(
                "UPDATE node_activation_op SET state=?, detail=? WHERE op_id=?",
                ("failed", "Workflow 或修订已不可用", row["op_id"]),
            )
            continue

        node = revision.graph.node(node_id)
        if node is None or not node.enabled:
            await store.db.execute(
                "UPDATE node_activation_op SET state=?, updated_at=? WHERE op_id=?",
                ("applied", utcnow().isoformat(), row["op_id"]),
            )
            continue

        new_seq = await _commit_enabled_change(
            store=store, workflow=workflow, revision=revision, node_id=node_id,
            enable=False, actor=actor, reason="排水完成，停用节点",
        )
        await store.db.execute(
            "UPDATE node_activation_op SET state=?, updated_at=? WHERE op_id=?",
            ("applied", utcnow().isoformat(), row["op_id"]),
        )
        done.append({"node_id": node_id, "new_revision_seq": new_seq})

    return done


async def _commit_enabled_change(
    *,
    store: Any,
    workflow: Any,
    revision: Any,
    node_id: str,
    enable: bool,
    actor: str,
    reason: str,
) -> int:
    """翻转 enabled 并产生**新修订**（修订不可变，编辑即新建）。"""
    import copy

    from ..domain.workflow import RevisionSource, WorkflowRevision

    new_graph = copy.deepcopy(revision.graph)
    new_graph.require_node(node_id).enabled = enable
    new_graph.touch()

    async with store.db.transaction():
        new_seq = await store.workflows.next_revision_seq(workflow.workflow_id)
        new_rev = WorkflowRevision(
            workflow_id=workflow.workflow_id,
            revision_seq=new_seq,
            graph=new_graph,
            source=RevisionSource.MANUAL,
            draft_of=revision.revision_seq,
            is_published=True,
            note=reason,
        )
        await store.workflows.save_revision(
            new_rev, publish=True, expected_revision_seq=workflow.current_revision_seq
        )
    return new_seq


# ===========================================================================
# 调序（RUN-04、AC-03）
# ===========================================================================


async def reorder_stages(
    *,
    store: Any,
    node_id: str,
    ordered_stage_ids: Sequence[str],
    actor: str = "user",
) -> ReorderReport:
    """调整某节点上仍在 pending 的阶段顺序。

    只作用于该节点队列，不破坏任务依赖、不抢占已运行阶段。
    与派发并发时返回**实际生效的顺序**，而不是请求方的乐观假设（AC-03）。
    """
    report = ReorderReport(node_id=node_id)

    current = await store.tasks.list_stages_by_node(
        node_id, states=[StageState.WAITING_DEPS, StageState.READY]
    )
    by_id = {s.stage_id: s for s in current}

    for sid in ordered_stage_ids:
        st = by_id.get(sid)
        if st is None:
            report.rejected.append(
                {"stage_id": sid, "reason": "该阶段不在本节点的待执行队列中（可能已开始、已暂停或已终结）"}
            )

    accepted = [sid for sid in ordered_stage_ids if sid in by_id]
    if not accepted:
        report.reason = "没有任何可调序的阶段"
        report.effective_order = [s.stage_id for s in current]
        return report

    # 用递减的 priority 表达顺序：列表前面的优先级更高。
    # 保留未出现在请求里的阶段在末尾，避免调序意外改变它们之间的相对次序。
    top = max((s.node_priority for s in current), default=50) + len(accepted) + 1
    for idx, sid in enumerate(accepted):
        await store.tasks.reorder_stage(sid, node_priority=top - idx)

    for s in current:
        if s.stage_id not in accepted:
            await store.tasks.reorder_stage(s.stage_id, node_priority=50)

    refreshed = await store.tasks.list_stages_by_node(
        node_id, states=[StageState.WAITING_DEPS, StageState.READY]
    )
    report.effective_order = [s.stage_id for s in refreshed]
    report.applied = report.effective_order[: len(accepted)] == accepted
    if not report.applied:
        report.reason = "调序与派发并发发生，部分阶段已经开始执行；以上是实际生效顺序"
    else:
        report.reason = None

    await store.events.append(
        scope=EventScope.STAGE,
        type=EventType.STAGE_REORDERED,
        actor=to_actor(actor),
        scope_id=node_id,
        payload={
            "requested": accepted,
            "effective": report.effective_order,
            "applied": report.applied,
            "rejected": report.rejected,
        },
    )
    return report


# ===========================================================================
# 辅助
# ===========================================================================


async def _current_attempt(store: Any, stage: TaskStage):
    attempts = await store.tasks.list_attempts(stage.stage_id, descending=True)
    return attempts[0] if attempts else None


async def _stages_of_node(
    store: Any,
    node_id: str,
    *,
    terminal: bool,
    states: Sequence[StageState] | None = None,
) -> list[TaskStage]:
    if states is not None:
        out: list[TaskStage] = []
        for st in await store.tasks.list_all_stages_by_node(node_id):
            if st.observed_state in states:
                out.append(st)
        return out
    return await store.tasks.list_all_stages_by_node(node_id)


def _has_in_flight(stages: Sequence[TaskStage]) -> bool:
    return any(s.observed_state in _IN_FLIGHT or s.observed_state == StageState.PAUSING for s in stages)


def _merge_resources(acc: dict[str, int], add: dict[str, int]) -> None:
    for k, v in (add or {}).items():
        acc[k] = acc.get(k, 0) + v


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())
