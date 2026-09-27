"""运行时内核实体与状态机（架构设计 v0.02 §5.2、§6.2）。

三条纪律贯穿本模块：
1. **双轨**：``desired_state`` 是控制意图（用户或系统写入），``observed_state``
   是系统观测到的事实。控制操作只写 desired 并 bump ``control_epoch``。
2. **CAS**：状态迁移以 (entity_id, control_epoch) 提交；完成回调携带
   lease_id 与 generation，租约已吊销或代次过期的回调直接丢弃（REC-05、AC-11）。
3. **未知 ≠ 零**：用量不可取得时字段为 None（未知），不是 0（OBS-04）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel, Entity, new_id, utcnow
from .workflow import GraphSpec

__all__ = [
    "DesiredState",
    "TaskState",
    "StageState",
    "TASK_TRANSITIONS",
    "STAGE_TRANSITIONS",
    "ErrorClass",
    "OriginOfControl",
    "AttemptOutcome",
    "Usage",
    "CompactEvent",
    "PinnedGraph",
    "Task",
    "TaskStage",
    "Attempt",
]


class DesiredState(StrEnum):
    """控制意图。暂停／删除／继续都只写这里，由执行器收敛。"""

    ACTIVE = "active"
    """运行或继续运行。"""

    PAUSED = "paused"

    CANCELLED = "cancelled"
    """用户主动删除（清单 §2：此意图在重启后依然有效，系统不得自动复活）。"""


class TaskState(StrEnum):
    """任务的可观测状态（用户可见，OBS-01）。"""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECONCILING = "reconciling"
    """系统无法确认状态的过渡态，是显式状态而非伪装成 RUNNING（OBS-01、REC-04）。"""


class StageState(StrEnum):
    """节点阶段的可观测状态。"""

    WAITING_DEPS = "waiting_deps"
    READY = "ready"
    """依赖已满足、排队等待节点槽位。"""

    DISPATCHING = "dispatching"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSING = "pausing"
    PAUSED = "paused"
    RETRYING = "retrying"
    """退避等待中。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    LOST = "lost"
    """系统无法确认真实状态（如崩溃后对账失败）。"""

    RECONCILING = "reconciling"


#: 占用节点执行槽的状态（D-03）。
#: 注意 AWAITING_APPROVAL / RETRYING / PAUSED / RECONCILING 都**不占槽**——
#: 审批等待期间同节点后续任务可继续执行，资源不被安全问题锁死。
SLOT_OCCUPYING_STATES: frozenset[StageState] = frozenset(
    {StageState.DISPATCHING, StageState.RUNNING}
)

#: 阶段终态：不再自动推进。FAILED / SKIPPED 可由显式恢复操作重新入队。
STAGE_TERMINAL_STATES: frozenset[StageState] = frozenset(
    {StageState.SUCCEEDED, StageState.FAILED, StageState.SKIPPED, StageState.CANCELLED}
)

#: 任务终态。「已删除任务无续跑入口」由 CANCELLED 的转移集为空保证。
TASK_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)

#: 任务处于「已接受控制、正在收敛」的过渡态——必须对用户可见（OBS-01、AC-22）。
TASK_TRANSITIONING_STATES: frozenset[TaskState] = frozenset(
    {TaskState.PAUSING, TaskState.CANCELLING, TaskState.RECONCILING}
)

_STAGE_ALL = frozenset(StageState)

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PAUSING,
            TaskState.CANCELLING,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.RECONCILING,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.PAUSING,
            TaskState.CANCELLING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.RECONCILING,
        }
    ),
    TaskState.PAUSING: frozenset(
        {TaskState.PAUSED, TaskState.CANCELLING, TaskState.FAILED, TaskState.RECONCILING}
    ),
    TaskState.PAUSED: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLING, TaskState.FAILED, TaskState.RECONCILING}
    ),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.RECONCILING}),
    TaskState.BLOCKED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PAUSING,
            TaskState.CANCELLING,
            TaskState.FAILED,
            TaskState.RECONCILING,
        }
    ),
    # 对账是「未知」的出口，因此可以落到任何确定态。
    TaskState.RECONCILING: frozenset(
        {
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.PAUSED,
            TaskState.BLOCKED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

STAGE_TRANSITIONS: dict[StageState, frozenset[StageState]] = {
    StageState.WAITING_DEPS: frozenset(
        {
            StageState.READY,
            StageState.BLOCKED,
            StageState.PAUSED,
            StageState.CANCELLED,
            StageState.SKIPPED,
            StageState.RECONCILING,
        }
    ),
    StageState.READY: frozenset(
        {
            StageState.DISPATCHING,
            StageState.PAUSED,
            StageState.BLOCKED,
            StageState.CANCELLED,
            StageState.SKIPPED,
            StageState.RECONCILING,
        }
    ),
    StageState.DISPATCHING: frozenset(
        {
            StageState.RUNNING,
            StageState.AWAITING_APPROVAL,
            StageState.RETRYING,
            StageState.FAILED,
            StageState.PAUSING,
            StageState.BLOCKED,
            StageState.CANCELLED,
            StageState.LOST,
            StageState.RECONCILING,
        }
    ),
    StageState.RUNNING: frozenset(
        {
            StageState.SUCCEEDED,
            StageState.FAILED,
            StageState.RETRYING,
            StageState.AWAITING_APPROVAL,
            StageState.PAUSING,
            StageState.BLOCKED,
            StageState.CANCELLED,
            StageState.LOST,
            StageState.RECONCILING,
        }
    ),
    StageState.AWAITING_APPROVAL: frozenset(
        {
            StageState.RUNNING,
            StageState.BLOCKED,
            StageState.FAILED,
            StageState.PAUSING,
            StageState.PAUSED,
            StageState.CANCELLED,
            StageState.LOST,
            StageState.RECONCILING,
        }
    ),
    StageState.PAUSING: frozenset(
        {
            StageState.PAUSED,
            StageState.FAILED,
            StageState.CANCELLED,
            StageState.LOST,
            StageState.RECONCILING,
        }
    ),
    # 恢复：未启动阶段直接重入就绪集；被协作停止的阶段创建新 Attempt（§10.2、11.3）。
    StageState.PAUSED: frozenset(
        {
            StageState.READY,
            StageState.CANCELLED,
            StageState.BLOCKED,
            StageState.FAILED,
            StageState.RECONCILING,
        }
    ),
    # 退避结束回到就绪队列重新竞争执行槽，而不是直接抢占——§6.3 的 claim 是 READY 的职责。
    StageState.RETRYING: frozenset(
        {
            StageState.READY,
            StageState.FAILED,
            StageState.PAUSING,
            StageState.PAUSED,
            StageState.BLOCKED,
            StageState.CANCELLED,
            StageState.RECONCILING,
        }
    ),
    StageState.BLOCKED: frozenset(
        {
            StageState.READY,
            StageState.FAILED,
            StageState.PAUSED,
            StageState.CANCELLED,
            StageState.RECONCILING,
        }
    ),
    StageState.LOST: frozenset(
        {StageState.RECONCILING, StageState.FAILED, StageState.CANCELLED, StageState.READY}
    ),
    StageState.RECONCILING: _STAGE_ALL,
    StageState.SUCCEEDED: frozenset(),
    # FAILED 与 SKIPPED 可被**显式的恢复／重新启用**操作拉回 READY（§11.3、D-01）。
    # 这不是自动推进：没有任何后台路径会离开这两个状态。
    StageState.FAILED: frozenset({StageState.READY, StageState.RECONCILING}),
    StageState.SKIPPED: frozenset({StageState.READY, StageState.RECONCILING}),
    StageState.CANCELLED: frozenset(),
}


def task_can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in TASK_TRANSITIONS[src]


def stage_can_transition(src: StageState, dst: StageState) -> bool:
    return dst in STAGE_TRANSITIONS[src]


class ErrorClass(StrEnum):
    """错误分类，供重试决策（D-05）。

    网络错误、限流（429）、5xx 为可重试；认证失败、4xx 配置错误、
    契约校验失败为不可重试（直接切换候选或失败）。
    """

    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"
    USER_CANCELLED = "user_cancelled"


class OriginOfControl(DomainModel):
    """暂停／删除的发起位置与时间（OBS-03）。

    节点是定位任务与记录操作来源的入口，不是控制范围本身——
    第一版控制默认作用于整次任务（§10.1、清单 §3.9）。
    """

    op: str
    """pause / resume / delete / disable / requeue …"""

    from_node_id: str | None = None
    at: datetime = Field(default_factory=utcnow)
    scope: str = "task"
    """默认 task（整任务）。保留扩展位，但不得与整任务操作共用含混文案。"""

    detail: str | None = None


class AttemptOutcome(DomainModel):
    """一次执行尝试的结局。"""

    error_class: ErrorClass
    detail: str | None = None
    error_kind: str | None = None
    """适配器上报的原始错误类别（network / rate_limit / auth / contract …），
    用于与 RetryPolicy.retryable_errors 比对。"""

    at: datetime = Field(default_factory=utcnow)


class Usage(DomainModel):
    """用量信息（OBS-04）。

    **None 表示不可取得（未知），不是 0。** 费用估算必须说明依据。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_estimate: float | None = None
    cost_basis: str | None = None
    """费用估算依据（如「按厂商公开单价 × 实测 token」）。缺失则说明为何不可用。"""

    notes: str | None = None

    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None  # 未知，不是 0
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class CompactEvent(DomainModel):
    """一次上下文整理的发生记录（CFG-04、§7.4）。

    「整理发生过」必须可追溯；不支持或失败时不得宣称已执行整理（CFG-05）。
    """

    at: datetime = Field(default_factory=utcnow)
    trigger: str
    """触发原因：threshold / manual / forced。"""

    effective_threshold: int | None = None
    """实际触发阈值 = min(用户阈值, harness 上限) − 安全余量。"""

    tokens_before: int | None = None
    tokens_after: int | None = None
    ok: bool = True
    detail: str | None = None


class PinnedGraph(DomainModel):
    """任务发射时钉扎的执行快照（WF-06、OBS-03、REC-03）。

    任务的依赖推进、历史回看、恢复全部以该快照为准，不受后续编辑影响。
    """

    graph: GraphSpec
    effective_edges: list[tuple[str, str]] = Field(default_factory=list)
    effective_graph_version: int = 0

    def effective_successors(self, node_id: str) -> list[str]:
        return [b for a, b in self.effective_edges if a == node_id]

    def effective_predecessors(self, node_id: str) -> list[str]:
        return [a for a, b in self.effective_edges if b == node_id]

    def edges_set(self) -> set[tuple[str, str]]:
        return set(self.effective_edges)

    def entry_nodes(self) -> list[str]:
        """有效入口 = 有效上游为空的已启用节点（§4.2 性质 4）。"""
        has_pred = {b for _, b in self.effective_edges}
        return [n.node_id for n in self.graph.nodes if n.enabled and n.node_id not in has_pred]

    def exit_nodes(self) -> list[str]:
        """有效出口 = 有效下游为空的已启用节点。"""
        has_succ = {a for a, _ in self.effective_edges}
        return [n.node_id for n in self.graph.nodes if n.enabled and n.node_id not in has_succ]


class Task(Entity):
    """用户向 Workflow 的一次主动提交（对应审计稿的 WorkflowRun）。"""

    task_id: str = Field(default_factory=new_id)

    idempotency_key: str | None = None
    """同一请求的网络重送命中原任务，不产生额外任务（RUN-02）。
    唯一约束为 (workflow_id, idempotency_key)。"""

    workflow_id: str
    workflow_name: str | None = None
    """发射时的 Workflow 名称快照，供历史回看时避免额外的定义查询。"""

    revision_seq: int
    effective_graph_version: int
    graph_snapshot: PinnedGraph

    input_payload: dict[str, Any] = Field(default_factory=dict)
    """提交输入。入口节点的 P1 分区来源（§7.3）。"""

    desired_state: DesiredState = DesiredState.ACTIVE
    observed_state: TaskState = TaskState.QUEUED

    control_epoch: int = 0
    """单调递增，控制操作 CAS 仲裁（AC-11）。"""

    priority: int = 50
    """任务级基础优先级，0–100。越大越先执行。"""

    failure_summary: dict[str, Any] | None = None
    """失败／受阻原因与尚在运行的分支（RUN-07）。任务失败时界面必须同时显示两者。"""

    blocked_reason: str | None = None

    last_origin: OriginOfControl | None = None
    """最近一次控制操作的来源，供 OBS-03 的暂停／删除记录展示。"""

    submitted_by: str = "user"

    @model_validator(mode="after")
    def _priority_range(self) -> "Task":
        if not 0 <= self.priority <= 100:
            raise ValueError("priority 必须在 0–100 之间")
        return self

    # ---- 便捷谓词 ----

    def is_terminal(self) -> bool:
        return self.observed_state in TASK_TERMINAL_STATES

    def is_cancelled(self) -> bool:
        return self.observed_state == TaskState.CANCELLED

    def is_paused_like(self) -> bool:
        return self.observed_state in (TaskState.PAUSED, TaskState.PAUSING)

    def blocks_new_dispatch(self) -> bool:
        """暂停中、删除中、已终止的任务都不再派发新阶段。"""
        return self.observed_state in {
            TaskState.PAUSING,
            TaskState.PAUSED,
            TaskState.CANCELLING,
            TaskState.CANCELLED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.RECONCILING,
        }


class TaskStage(Entity):
    """某任务在某节点上的执行部分。"""

    stage_id: str = Field(default_factory=new_id)
    task_id: str
    node_id: str
    node_name: str | None = None
    """节点名快照，供历史回看（节点可能已被改名或从图中移除）。"""

    desired_state: DesiredState = DesiredState.ACTIVE
    observed_state: StageState = StageState.WAITING_DEPS

    control_epoch: int = 0
    """阶段级 CAS 仲裁号。调度循环的 claim（§6.3 伪代码的 ``expected_epoch``）
    与调序、暂停、删除都通过它竞争，失败方拿到「实际生效结果」而非静默覆盖（AC-03、AC-11）。"""

    node_priority: int = 50
    """用户在该节点投影上的调序结果；只影响本节点队列，不破坏依赖（RUN-04）。"""

    task_priority: int = 50
    """发射时从 Task.priority 拷贝的排序副本，供单表排序。"""

    enqueued_at: datetime = Field(default_factory=utcnow)
    """进入本节点队列的时间，队列排序的第三键。"""

    current_attempt_seq: int = 0
    attempt_count: int = 0

    profile_cursor: int = 0
    """当前候选在钉扎节点 profiles 中的下标。D-05：候选切换严格单向推进，
    已失败的候选在同一次阶段执行中不再回访。"""

    blocked_reason: str | None = None
    status_reason: str | None = None
    """面向用户的状态说明，例如「等待审批中，不占用执行槽」。"""

    origin_of_control: OriginOfControl | None = None

    upstream_pins: dict[str, list[str]] = Field(default_factory=dict)
    """D-06 版本选择：就绪判定时钉扎各有效上游「该任务内当前成功」的产物版本，
    之后不变。已完成消费者的输入来源不会被改写为「最新结果」（DATA-02/04）。

    键是上游 node_id，值是该上游在**本次成功尝试**中产出的 artifact_id 列表。
    """

    checkpoint_ref: str | None = None
    """最近一次协作停止时保留的断点（D-07）。None 表示只能从头重跑本阶段。"""

    requires_reconcile: bool = False
    """启动对账期间被标记为需要人工／系统核对的阶段。"""

    def queue_sort_key(self) -> tuple[int, int, float]:
        """队列默认顺序 (node_priority, task_priority, enqueued_at)，大者优先。"""
        return (-self.node_priority, -self.task_priority, self.enqueued_at.timestamp())

    def occupies_slot(self) -> bool:
        return self.observed_state in SLOT_OCCUPYING_STATES

    def is_terminal(self) -> bool:
        return self.observed_state in STAGE_TERMINAL_STATES

    def is_ready_to_dispatch(self) -> bool:
        return self.observed_state == StageState.READY


class Attempt(Entity):
    """一次执行尝试：首次执行、重试、候选切换各占一条。"""

    attempt_id: str = Field(default_factory=new_id)
    stage_id: str
    task_id: str
    node_id: str
    attempt_seq: int

    profile_id: str
    """实际采用的候选，CFG-07 可追溯。"""

    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    """实际采用的参数快照（模型、harness、effort、实际 compact 阈值…）。
    历史不因当前配置变化而改写（CFG-07）。"""

    session_ref: str | None = None
    """本次尝试使用的 SessionHandle。"""

    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    """心跳租约；supervisor 定期续期。过期回调按代次丢弃（REC-05）。"""

    generation: int = 1
    """代次号；清理后递增，迟到回调按代次丢弃（REC-05）。"""

    usage: Usage | None = None
    outcome: AttemptOutcome | None = None
    compact_events: list[CompactEvent] = Field(default_factory=list)

    started_at: datetime | None = None
    ended_at: datetime | None = None

    resume_from_checkpoint: str | None = None
    """本尝试是「从断点重建」还是「从头重跑」，历史中必须留痕（§8.2）。"""

    reattached: bool = False
    """启动对账后判定为「恢复监控、不重启工作」的尝试（§11.2）。"""

    def is_in_flight(self) -> bool:
        return self.outcome is None

    def is_lease_fresh(self, now: datetime | None = None) -> bool:
        if self.lease_expires_at is None:
            return False
        return self.lease_expires_at > (now or utcnow())
