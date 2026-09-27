"""任务发射（架构设计 v0.02 §6.1、RUN-01/02）。

发射 = 钉扎 + 建阶段 + 入口就绪。三件事必须在**同一个事务**里完成，
否则崩溃可能留下「任务存在但没有阶段」或「阶段存在但没有钉扎快照」的半截状态，
而启动对账（REC-03）无从判断该补哪一半。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Sequence

from pydantic import Field

from ..domain.base import DomainModel, utcnow
from ..domain.task import (
    DesiredState,
    OriginOfControl,
    PinnedGraph,
    StageState,
    Task,
    TaskStage,
    TaskState,
)
from ..graph.derive import derive
from ..graph.validate import ValidationMode, ValidationReport, validate
from ...data.event_log import EventActor, EventScope, EventType

__all__ = ["LaunchResult", "LaunchRejected", "launch_task", "to_actor"]


def to_actor(name: str) -> EventActor:  # noqa: D401
    """把「user / system / adapter / ai」这类字符串收敛为枚举。

    未知取值不静默兜底成 system——那会把「谁做的」这件事记错。
    """
    try:
        return EventActor(name)
    except ValueError as exc:
        raise ValueError(f"未知的操作者类型: {name}") from exc


class LaunchRejected(Exception):
    """校验未通过，任务不予发射。携带完整报告供 UI 定位到节点／连线。"""

    def __init__(self, report: ValidationReport) -> None:
        super().__init__(report.summary())
        self.report = report


class LaunchResult(DomainModel):
    task: Task
    created: bool
    """False 表示命中幂等键，返回的是已存在的那次提交（RUN-02）。"""

    report: ValidationReport | None = None

    def stages_hint(self) -> str:
        return f"任务 {self.task.task_id[:8]}（{'新建' if self.created else '已存在'}）"


async def launch_task(
    *,
    store: Any,
    workflow_id: str,
    input_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    priority: int = 50,
    actor: str = "user",
    origin_node_id: str | None = None,
    allow_unpublished: bool = False,
) -> LaunchResult:
    """向 Workflow 发射一次任务。"""
    workflow = await store.workflows.get(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow 不存在: {workflow_id}")

    if workflow.status.value == "deleted":
        raise ValueError(f"Workflow「{workflow.name}」已被删除，不接受新提交（LIFE-05）")

    if not allow_unpublished and workflow.status.value != "published":
        raise ValueError(
            f"Workflow「{workflow.name}」尚未发布；草稿不能发射任务（WF-01）"
        )

    revision = await store.workflows.get_current_revision(workflow_id)
    if revision is None:
        raise ValueError(f"Workflow「{workflow.name}」没有可用的修订版本")

    # 1) 幂等：同一次提交因网络重送不得创建额外任务（RUN-02）。
    #    先查一次是为了避免在校验失败时把「已存在的任务」也拒掉——
    #    重送一个已经成功的提交应当返回原任务，而不是返回校验错误。
    if idempotency_key:
        existing = await store.tasks.find_by_idempotency(workflow_id, idempotency_key)
        if existing is not None:
            return LaunchResult(task=existing, created=False)

    # 2) 发射前校验：LAUNCH 档位（含有效入口、输入衔接、能力匹配）
    registry = await store.registry.snapshot()
    report = validate(revision.graph, registry, mode=ValidationMode.LAUNCH)
    if not report.ok():
        raise LaunchRejected(report)

    # 3) 钉扎有效图
    effective = derive(revision.graph)
    pinned = PinnedGraph(
        graph=revision.graph,
        effective_edges=effective.to_edge_tuples(),
        effective_graph_version=effective.version(),
    )

    entries = set(effective.entry_nodes())
    now = utcnow()

    task = Task(
        workflow_id=workflow_id,
        workflow_name=workflow.name,
        idempotency_key=idempotency_key,
        revision_seq=revision.revision_seq,
        effective_graph_version=pinned.effective_graph_version,
        graph_snapshot=pinned,
        input_payload=input_payload or {},
        desired_state=DesiredState.ACTIVE,
        observed_state=TaskState.QUEUED,
        priority=priority,
        submitted_by=actor,
        last_origin=OriginOfControl(
            op="submit", from_node_id=origin_node_id, at=now, scope="task"
        )
        if origin_node_id
        else None,
        created_at=now,
        updated_at=now,
    )

    stages: list[TaskStage] = []
    # 入口节点先入队，保证同一任务内部的初始顺序稳定可复现
    ordered = sorted(effective.node_ids, key=lambda n: (n not in entries, n))
    for offset, node_id in enumerate(ordered):
        node = revision.graph.require_node(node_id)
        stages.append(
            TaskStage(
                task_id=task.task_id,
                node_id=node_id,
                node_name=node.name,
                desired_state=DesiredState.ACTIVE,
                observed_state=(
                    StageState.READY if node_id in entries else StageState.WAITING_DEPS
                ),
                node_priority=50,
                task_priority=priority,
                enqueued_at=now + timedelta(microseconds=offset),
                created_at=now,
                updated_at=now,
            )
        )

    stored_task, created = await store.tasks.create_task_with_stages(task, stages)

    if created:
        await store.events.append(
            scope=EventScope.TASK,
            type=EventType.TASK_SUBMITTED,
            actor=to_actor(actor),
            scope_id=stored_task.task_id,
            task_id=stored_task.task_id,
            payload={
                "workflow_id": workflow_id,
                "workflow_name": workflow.name,
                "revision_seq": revision.revision_seq,
                "effective_graph_version": pinned.effective_graph_version,
                "entry_nodes": sorted(entries),
                "node_count": len(ordered),
                "idempotency_key": idempotency_key,
            },
        )

    return LaunchResult(task=stored_task, created=created, report=report)
