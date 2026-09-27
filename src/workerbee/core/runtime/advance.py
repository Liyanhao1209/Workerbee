"""依赖推进与完成判定（架构设计 v0.02 §6.1、RUN-05/06/07、D-04）。

首版汇聚语义：**全部必需有效上游在同一 task_id 下成功且输入可用**，方可转为 READY。
任意一个或指定数量成功不列为必需能力（清单 §3.5）。

完成判据三合一（RUN-06）：适配器报告会话正常结束 **且** 要求的产出物已通过边契约
校验（未声明契约的边退化为「产出物存在」）**且** 无影响结果的后台工作未结束。
三者缺一，不发出完成事件。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..domain.artifact import Artifact
from ..domain.edge import EdgeContract
from ..domain.message import Message, MessageType
from ..domain.task import Attempt, StageState, Task, TaskStage, TaskState

__all__ = [
    "ACTIVE_STAGE_STATES",
    "required_predecessors",
    "required_exits",
    "deps_satisfied",
    "blocked_by_upstream",
    "on_stage_succeeded",
    "on_stage_failed",
    "block_downstream",
    "evaluate_task_state",
    "contract_satisfied",
]

#: 「任务仍在推进」的判据：有这些阶段时任务不能判终态。
ACTIVE_STAGE_STATES: frozenset[StageState] = frozenset(
    {
        StageState.DISPATCHING,
        StageState.RUNNING,
        StageState.PAUSING,
        StageState.RETRYING,
        StageState.AWAITING_APPROVAL,
        StageState.RECONCILING,
    }
)


def required_predecessors(task: Task, node_id: str) -> list[str]:
    """本任务钉扎的有效上游。第一版全部为「必需」。"""
    return task.graph_snapshot.effective_predecessors(node_id)


def required_exits(task: Task) -> list[str]:
    return task.graph_snapshot.exit_nodes()


def _by_node(stages: Sequence[TaskStage]) -> dict[str, TaskStage]:
    return {s.node_id: s for s in stages}


def deps_satisfied(task: Task, stage: TaskStage, stages: dict[str, TaskStage]) -> bool:
    for n in required_predecessors(task, stage.node_id):
        up = stages.get(n)
        if up is None or up.observed_state != StageState.SUCCEEDED:
            return False
    return True


def blocked_by_upstream(
    task: Task, stage: TaskStage, stages: dict[str, TaskStage]
) -> str | None:
    """若某个必需上游已不可能成功，返回可读的受阻原因；否则 None。

    上游「不可能成功」= FAILED / CANCELLED / SKIPPED / BLOCKED / LOST。
    这些状态下下游永远不会被启动，必须显式说明而不是永久挂在等待状态（RUN-07）。
    """
    hopeless = {
        StageState.FAILED: "失败",
        StageState.CANCELLED: "已被取消",
        StageState.SKIPPED: "已被跳过",
        StageState.BLOCKED: "上游受阻",
        StageState.LOST: "状态不明（需核对）",
    }
    for n in required_predecessors(task, stage.node_id):
        up = stages.get(n)
        if up is None:
            return f"必需上游节点 {n[:8]} 在本任务中没有阶段记录"
        if up.observed_state in hopeless:
            return (
                f"必需上游「{up.node_name or n[:8]}」{hopeless[up.observed_state]}，"
                f"本阶段不会启动"
            )
    return None


def contract_satisfied(
    contracts: Iterable[EdgeContract | None],
    artifacts: Sequence[Artifact],
) -> tuple[bool, list[str], str | None]:
    """产出物是否满足边契约（§6.1 第 4 条、§7.2）。

    - **未声明契约的边**退化为「产出物存在」——这是如实声明的能力边界（§4.3 第 3 条）。
    - **声明了契约的边**逐字段比对：覆盖判定由摘要质量门禁（D-06）在产物落地时
      一次性完成并写入 ``Artifact.covered_fields``，这里只做集合包含判断。
      两处判定共用一份数据，因此不会互相矛盾。

    返回 ``(是否通过, 缺失字段, 失败原因)``。
    """
    declared = [c for c in contracts if c is not None and c.outputs]
    has_any_output = len(artifacts) > 0

    if not has_any_output:
        if declared:
            required = sorted({f for c in declared for f in c.outputs})
            return False, required, "没有任何产出物，无法满足边契约"
        return False, [], "没有任何产出物"

    # 摘要质量门禁未通过的产物，其覆盖声明不可信——按未覆盖处理，
    # 绝不允许以静默省略换取「成功」（DATA-03）。
    covered: set[str] = set()
    for art in artifacts:
        if art.summary_ok:
            covered.update(art.covered_fields)

    if not declared:
        return True, [], None

    missing = sorted({f for c in declared for f in c.outputs if f not in covered})
    if missing:
        return False, missing, f"产出物未覆盖契约要求的字段：{'、'.join(missing)}"
    return True, [], None


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


async def on_stage_succeeded(
    *,
    store: Any,
    sm: Any,
    task: Task,
    stage: TaskStage,
    attempt: Attempt,
    artifacts: Sequence[Artifact],
) -> dict[str, Any]:
    """阶段成功后：钉扎产物、通知下游、把满足依赖的下游置为 READY。

    幂等：同一完成通知重复到达时，靠「下游已不是 WAITING_DEPS 就不重复置位」
    与 Message 的 dedup_key 双重保证不重复启动下游（REC-05）。
    """
    artifact_ids = [a.artifact_id for a in artifacts]
    effective_successors = task.graph_snapshot.effective_successors(stage.node_id)

    # 1) 发 data_ready：负载只携带产物引用与元数据，正文不随通知传输（§7.1）
    for succ in effective_successors:
        msg = Message(
            type=MessageType.DATA_READY,
            task_id=task.task_id,
            from_stage=stage.stage_id,
            to_stage=succ,
            dedup_key=f"data_ready:{task.task_id}:{stage.node_id}:{stage.current_attempt_seq}:{succ}",
            payload_ref=artifact_ids[0] if artifact_ids else None,
            payload={
                "from_node": stage.node_id,
                "producer": {
                    "task_id": task.task_id,
                    "stage_id": stage.stage_id,
                    "attempt_seq": attempt.attempt_seq,
                },
                "artifact_ids": artifact_ids,
                "count": len(artifact_ids),
            },
        )
        await store.messages.send(msg)

    # 2) 为下游钉扎输入版本（D-06：就绪判定时钉扎，之后不变）
    stages = {s.node_id: s for s in await store.tasks.list_stages(task.task_id)}
    notes: dict[str, Any] = {"advanced": [], "pinned": artifact_ids}

    for succ in effective_successors:
        down = stages.get(succ)
        if down is None:
            continue
        pins = dict(down.upstream_pins)
        pins[stage.node_id] = artifact_ids
        await store.tasks.update_stage(down.stage_id, upstream_pins=pins)

    # 3) 把依赖已满足的下游置为 READY
    refreshed = {s.node_id: s for s in await store.tasks.list_stages(task.task_id)}
    for succ in effective_successors:
        down = refreshed.get(succ)
        if down is None or down.observed_state != StageState.WAITING_DEPS:
            continue
        if deps_satisfied(task, down, refreshed):
            if await sm.set_stage_state(
                down,
                StageState.READY,
                reason="全部必需上游已成功",
                actor="system",
            ):
                notes["advanced"].append(succ)

    return notes


async def block_downstream(
    *,
    store: Any,
    sm: Any,
    task: Task,
    origin_node_id: str,
    reason: str,
) -> list[str]:
    """沿有效边把受阻状态传播到全部下游（RUN-07）。

    传播是**传递闭包**：上游失败后，下游的下游也不可能成功，必须一起说明，
    否则任务会永久挂在「等待」上。
    """
    stages = {s.node_id: s for s in await store.tasks.list_stages(task.task_id)}
    blocked: list[str] = []
    frontier = list(task.graph_snapshot.effective_successors(origin_node_id))
    seen: set[str] = set()

    while frontier:
        node_id = frontier.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        st = stages.get(node_id)
        if st is None:
            continue
        if st.observed_state in (
            StageState.WAITING_DEPS,
            StageState.READY,
            StageState.BLOCKED,
        ):
            if await sm.set_stage_state(
                st,
                StageState.BLOCKED,
                reason=reason,
                actor="system",
                blocked_reason=reason,
            ):
                blocked.append(node_id)
        frontier.extend(task.graph_snapshot.effective_successors(node_id))

    return blocked


async def on_stage_failed(
    *,
    store: Any,
    sm: Any,
    task: Task,
    stage: TaskStage,
    reason: str,
) -> dict[str, Any]:
    """阶段失败：阻断下游、记录失败摘要、必要时推进任务终态。"""
    blocked = await block_downstream(
        store=store, sm=sm, task=task, origin_node_id=stage.node_id, reason=reason
    )
    return {"blocked": blocked}


# ---------------------------------------------------------------------------
# 任务聚合状态
# ---------------------------------------------------------------------------


async def evaluate_task_state(
    *,
    store: Any,
    sm: Any,
    task: Task,
    reason: str | None = None,
) -> TaskState | None:
    """由阶段状态推导任务的聚合状态。返回新状态，或 None 表示无需变更。

    判据顺序（D-04）：
    1. 全部有效出口成功 → SUCCEEDED
    2. 无活跃、无待派发阶段 → FAILED（死局；此时若用户主动暂停则不覆盖，见下）
    3. 存在受阻阶段 → BLOCKED（其余分支仍在跑）
    4. 否则 → RUNNING
    """
    stages = await store.tasks.list_stages(task.task_id)
    by_node = _by_node(stages)

    if task.observed_state in (TaskState.CANCELLED, TaskState.CANCELLING):
        return None
    if task.desired_state.value == "paused" or task.observed_state in (
        TaskState.PAUSING,
        TaskState.PAUSED,
    ):
        # 用户主动暂停的意图优先于聚合推导；暂停中的任务不能被系统判成失败
        return None
    if task.observed_state == TaskState.RECONCILING:
        return None

    exits = required_exits(task)
    if exits and all(
        by_node.get(n) is not None and by_node[n].observed_state == StageState.SUCCEEDED
        for n in exits
    ):
        return TaskState.SUCCEEDED

    has_active = any(s.observed_state in ACTIVE_STAGE_STATES for s in stages)
    has_pending = any(
        s.observed_state in (StageState.WAITING_DEPS, StageState.READY) for s in stages
    )
    has_blocked = any(s.observed_state == StageState.BLOCKED for s in stages)
    has_failed = any(s.observed_state == StageState.FAILED for s in stages)

    if not has_active and not has_pending:
        # 死局：没有东西能再推进了，而出口并未全部成功。
        # 区分「有明确失败」与「全部被跳过/取消」两种情况，给出不同的用户说明。
        if has_failed or has_blocked:
            return TaskState.FAILED
        if all(
            s.observed_state in (StageState.SKIPPED, StageState.CANCELLED)
            or s.observed_state == StageState.SUCCEEDED
            for s in stages
        ):
            return TaskState.FAILED
        return TaskState.FAILED

    if has_blocked or has_failed:
        return TaskState.BLOCKED

    return TaskState.RUNNING


async def summarize_failure(
    *, store: Any, task: Task, reason: str | None
) -> dict[str, Any]:
    """RUN-07：失败任务的界面必须**同时**显示失败原因与尚在运行的分支。"""
    stages = await store.tasks.list_stages(task.task_id)
    failed = [
        {
            "node_id": s.node_id,
            "node_name": s.node_name,
            "reason": s.blocked_reason or s.status_reason,
        }
        for s in stages
        if s.observed_state in (StageState.FAILED, StageState.BLOCKED)
    ]
    still_running = [
        {"node_id": s.node_id, "node_name": s.node_name, "state": s.observed_state.value}
        for s in stages
        if s.observed_state in ACTIVE_STAGE_STATES
    ]
    succeeded = [
        {"node_id": s.node_id, "node_name": s.node_name}
        for s in stages
        if s.observed_state == StageState.SUCCEEDED
    ]
    return {
        "reason": reason,
        "failed_paths": failed,
        "still_running": still_running,
        "succeeded": succeeded,
    }
