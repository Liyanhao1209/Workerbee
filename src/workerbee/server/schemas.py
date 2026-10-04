"""API 网关的请求／响应模型（前端契约的唯一来源）。

三条纪律：

1. **OpenAPI 是契约。** 每个路由都带 ``response_model``，前端只按 OpenAPI 写，
   不猜字段。路由返回字典前一律过一遍这里的模型，形状不一致在服务端就暴露。
2. **请求严格、响应宽容。** 请求模型 ``extra="forbid"``：字段拼错在 400 级失败，
   而不是被静默忽略；响应模型 ``extra="ignore"``：内核给报告加字段时，
   网关不会因为「多了一个键」而 500——前端契约由本文件显式声明，不靠内核的偶然形状。
3. **三个结论不合成一个布尔值。** 生命周期操作（LIFE-06）的响应一律继承
   :class:`TriStateOutcome`，「已接受 / 执行已停止 / 资源清理完成」三个字段并列呈现。
   前端不得把 accepted 当成「暂停成功」展示。

实体类响应（Task / TaskStage / Approval / SkillDoc …）直接复用领域模型：
它们就是前端要渲染的字段集，另抄一份只会引入漂移。本层自定义的模型只覆盖
API 独有的形状——分页、报告、请求体、冲突响应。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.domain import (
    Approval,
    Attempt,
    CredentialKind,
    CredentialRef,
    Edge,
    GraphSpec,
    HarnessRegistration,
    InstantiationReport,
    NodeDefinition,
    RevisionSource,
    SkillDoc,
    SkillScope,
    Task,
    TaskStage,
    Template,
    TemplateKind,
    TemplatePayload,
    ToolSpec,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from ..core.domain.registry import ApprovalPolicy, AuthMode, RiskLevel
from ..core.graph.derive import GraphDelta
from ..core.graph.validate import ValidationMode, ValidationReport

__all__ = [
    "ApiRequest",
    "ApiResponse",
    "ErrorResponse",
    "HealthResponse",
    "TriStateOutcome",
    "PauseResponse",
    "ResumeResponse",
    "DeleteTaskResponse",
    "WorkflowDeleteResponse",
    "NodeToggleResponse",
    "TogglePreviewResponse",
    "ToggleRequest",
    "ReorderRequest",
    "ReorderResponse",
    "NodeQueueResponse",
    "SystemStatusResponse",
    "EventRecord",
    "EventPage",
    "AttentionResponse",
    "StorageReportResponse",
    "SessionRecord",
    "SessionListResponse",
    "PruneRequest",
    "PruneAction",
    "PruneResponse",
    "WorkflowResponse",
    "WorkflowListResponse",
    "WorkflowCreateRequest",
    "WorkflowPatchRequest",
    "RevisionResponse",
    "RevisionListResponse",
    "RevisionSaveRequest",
    "RevisionSaveResponse",
    "ConflictResponse",
    "ValidateRequest",
    "TaskSubmitRequest",
    "SubmitResponse",
    "TaskListResponse",
    "TaskDetailResponse",
    "StageResumeResponse",
    "HarnessCreateRequest",
    "HarnessPatchRequest",
    "ProbeResponse",
    "CredentialCreateRequest",
    "RevokeRequest",
    "SkillCreateRequest",
    "ToolCreateRequest",
    "TemplateCreateRequest",
    "TemplateListResponse",
    "TemplateInstantiateRequest",
    "TemplateInstantiateResponse",
    "ApprovalListResponse",
    "ApprovalDecideRequest",
    "DeliveryResponse",
    "AssistantThreadCreateRequest",
    "AssistantThreadRenameRequest",
    "AssistantThreadResponse",
    "AssistantThreadListResponse",
    "AssistantMessageResponse",
    "AssistantMessageListResponse",
    "AssistantDraftResponse",
    "AssistantSendRequest",
    "AssistantSendResponse",
    "AssistantCompactResponse",
    "AssistantConfigResponse",
    "AssistantConfigUpdateRequest",
    "CaptureRunCreateRequest",
    "CaptureRunFromTaskRequest",
    "CaptureRunResponse",
    "CaptureRunListResponse",
    "CaptureMaterialSummary",
    "CaptureRunDetailResponse",
    "CaptureDraftResponse",
    "CaptureDraftAdoptRequest",
    # 领域模型的再导出：前端契约与领域字段集保持一致
    "Approval",
    "Attempt",
    "CredentialRef",
    "Edge",
    "GraphSpec",
    "GraphDelta",
    "HarnessRegistration",
    "InstantiationReport",
    "NodeDefinition",
    "SkillDoc",
    "Task",
    "TaskStage",
    "Template",
    "ToolSpec",
    "ValidationMode",
    "ValidationReport",
    "WorkflowDefinition",
    "WorkflowRevision",
]


class ApiRequest(BaseModel):
    """请求体基类。严格模式：未知字段即失败（拼错字段名不该被静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


class ApiResponse(BaseModel):
    """响应体基类。宽容模式：内核新增字段不会把响应打成 500。"""

    model_config = ConfigDict(extra="ignore")


class ErrorResponse(ApiResponse):
    """统一错误体。**绝不回显令牌**或任何凭据材料。"""

    detail: str
    hint: str | None = None


class HealthResponse(ApiResponse):
    """存活探针。免鉴权——它不含任何数据，只说明服务在。"""

    ok: bool = True
    version: str


# ===========================================================================
# 生命周期：三个独立结论（LIFE-06）
# ===========================================================================


class TriStateOutcome(ApiResponse):
    """生命周期操作的公共形状：三个结论互不蕴含，必须分开呈现。

    - ``accepted``：控制意图已落库（desired_state 已写）。**不代表已经停下。**
    - ``execution_stopped``：全部在途执行确认终止。
    - ``resources``：closed / teardown_failed / orphaned / skipped 计数。
      计数器非零不等于失败，前端按 ``warnings`` 如实展示未清理项。
    """

    accepted: bool
    execution_stopped: bool
    resources: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PauseResponse(TriStateOutcome):
    task_id: str
    paused_immediately: list[str] = Field(default_factory=list)
    """未启动就被暂停的阶段（零成本）。"""
    stopped_cooperatively: list[str] = Field(default_factory=list)
    """协作停止的阶段——可能有重复工作，UI 必须说明（LIFE-02）。"""
    paused_in_place: list[str] = Field(default_factory=list)
    checkpoints: dict[str, str | None] = Field(default_factory=dict)
    """阶段 → 断点。值为 None 表示只能从头重跑本阶段，必须如实展示。"""
    unsupported: list[str] = Field(default_factory=list)
    """harness 不支持暂停的阶段。收到暂停请求时如实拒绝，不显示为已暂停。"""


class ResumeResponse(TriStateOutcome):
    task_id: str
    requeued: list[str] = Field(default_factory=list)
    restarted: list[str] = Field(default_factory=list)
    """从断点或从头重建的阶段——用户需要知道「哪些要重做」。"""
    reused_sessions: list[str] = Field(default_factory=list)


class DeleteTaskResponse(TriStateOutcome):
    task_id: str
    cancelled_stages: list[str] = Field(default_factory=list)
    preserved_succeeded: list[str] = Field(default_factory=list)
    """已完成阶段保留其**真实结果**，不被改写为失败（LIFE-04、OBS-03）。"""


class WorkflowDeleteResponse(TriStateOutcome):
    """删除 Workflow（LIFE-05）：同样三态分离——「已停止接受提交」「任务已终止」
    「资源已清理」是三件事。定义与历史保留供回看，不恢复。"""

    workflow_id: str
    tasks_terminated: list[str] = Field(default_factory=list)
    shared_configs_kept: list[str] = Field(default_factory=list)
    """仍被其他 Workflow 引用的共享配置不予删除（LIFE-05）。"""


class NodeToggleResponse(ApiResponse):
    """启停结果（ACT-01–04、D-01）。

    这里没有 ``accepted/execution_stopped/resources``：启停不是控制操作，
    它只翻转 enabled 并产生新修订。「新修订已产生」与「排水还在进行」是两件事，
    分别由 ``applied`` 与 ``awaiting_drain`` 表达。
    """

    workflow_id: str
    node_id: str
    enabling: bool
    mode: str = "drain"
    applied: bool = False
    """enabled 已翻转并产生新修订。排水模式下，在途阶段跑完前这里为 False。"""
    awaiting_drain: bool = False
    new_revision_seq: int | None = None
    affected_tasks: list[str] = Field(default_factory=list)
    drained_stages: list[str] = Field(default_factory=list)
    withdrawn_stages: list[str] = Field(default_factory=list)
    revived_stages: list[str] = Field(default_factory=list)
    added_edges: list[tuple[str, str]] = Field(default_factory=list)
    removed_edges: list[tuple[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TogglePreviewResponse(ApiResponse):
    """ACT-02 的路径可见：操作前展示拓扑差异、可执行性与受影响任务。

    **本响应不改变任何状态**——客户端看过它之后才决定是否真的执行。
    """

    node_id: str
    enabling: bool
    delta: GraphDelta
    report: ValidationReport
    affected_tasks: list[str] = Field(default_factory=list)


class ToggleRequest(ApiRequest):
    enable: bool
    mode: Literal["drain", "immediate"] = "drain"
    """仅对停用有意义（D-01）：drain 排水（默认，不丢弃已完成工作）；
    immediate 立即撤回（在途走取消链，排队标记 SKIPPED）。"""


class ReorderRequest(ApiRequest):
    stage_ids: list[str]
    """期望的队列顺序（只列本节点待执行的阶段）。未列出的阶段保持原相对次序。"""


class ReorderResponse(ApiResponse):
    node_id: str
    applied: bool = False
    effective_order: list[str] = Field(default_factory=list)
    """**实际生效**的顺序。与请求不符时前端显示这个，而不是自己的乐观假设（AC-03）。"""
    rejected: list[dict[str, str]] = Field(default_factory=list)
    reason: str | None = None


class NodeQueueResponse(ApiResponse):
    """节点队列投影（RUN-04）：**只读视图**。

    权威队列始终是任务／阶段表，这里是它此刻的投影——前端不得据它反推状态，
    有变化以推送或重新拉取为准。
    """

    node_id: str
    pending: list[TaskStage] = Field(default_factory=list)
    """等待依赖与就绪的阶段，按实际派发次序排列（与调序端的 effective_order 同源）。"""
    running: list[TaskStage] = Field(default_factory=list)
    """占用执行槽的阶段（D-03）。等待审批的阶段不在此列——它不占槽。"""
    history: list[TaskStage] = Field(default_factory=list)


# ===========================================================================
# 系统
# ===========================================================================


class EventRecord(ApiResponse):
    """append-only 事件日志的一条记录（OBS-02/03）。"""

    event_id: int
    scope: str
    scope_id: str | None = None
    type: str
    actor: str
    task_id: str | None = None
    stage_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    ts: str


class EventPage(ApiResponse):
    """事件增量页。``latest_event_id`` 是客户端下次续拉的水位（REC-01）。"""

    events: list[EventRecord] = Field(default_factory=list)
    latest_event_id: int = 0
    returned: int = 0
    has_more: bool = False
    note: str | None = None


class ToolCallView(ApiResponse):
    """一次工具调用的展示视图。``is_error`` 为 None 表示结果还没回来（仍在执行）。"""

    tool_use_id: str | None = None
    name: str | None = None
    target: str | None = None
    input_preview: str = ""
    input_truncated: bool = False
    is_error: bool | None = None
    result_preview: str | None = None
    result_truncated: bool = False


class AttemptWorkResponse(ApiResponse):
    """一次执行尝试的工作细节（OBS-03）：实际输入、推理过程、工具调用、涉及的文件。

    全部来自事件日志的如实记录；没有任何一项时对应字段为空而不是编造。
    """

    task_id: str
    attempt_id: str
    stage_id: str | None = None
    node_id: str | None = None
    input: dict[str, Any] | None = None
    reasoning: str | None = None
    reasoning_truncated: bool = False
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)


class ArtifactContentResponse(ApiResponse):
    """产物正文。有界截断；内容过凭据脱敏后才离开内核。"""

    artifact_id: str
    text: str
    truncated: bool = False
    size_bytes: int | None = None
    media_type: str | None = None


class SchedulerStats(ApiResponse):
    enabled: bool = False
    """调度循环是否已装配。未装配时任务不会自动推进——界面必须如实显示。"""
    poll_interval: float = 0.0


class ReaperStats(ApiResponse):
    interval_seconds: float = 0.0
    artifact_gc_enabled: bool = False
    last_run: dict[str, Any] | None = None


class CountsResponse(ApiResponse):
    live_tasks: int = 0
    running_stages: int = 0
    pending_stages: int = 0
    open_approvals: int = 0
    workflows: int = 0


class SystemStatusResponse(ApiResponse):
    """系统状态（OBS-01/05）。startup_notes 如实展示降级，不静默（§1.2 原则 4）。"""

    version: str
    startup_notes: list[str] = Field(default_factory=list)
    data_dir: str
    workspace_dir: str
    secrets_unlocked: bool = False
    """凭据库是否已解锁。未解锁时引用凭据的节点会明确报错，而不是匿名运行。"""
    harness_attached: bool = False
    allow_remote: bool = False
    loopback_only: bool = True
    scheduler: SchedulerStats
    reaper: ReaperStats
    counts: CountsResponse
    session_hosting: str = "in_process"
    """会话托管方式：``supervisor``（独立进程托管，core 重启不打断在跑的任务）
    或 ``in_process``（子进程由 core 自己持有，core 重启即失去它们）。

    **必须与 ``supervisor_connected`` 一起看**：前者是配置的托管方式，
    后者才说明它此刻是否真的立起来了。
    """
    supervisor_connected: bool = False
    notifier_subscribers: int = 0
    notifier_dropped: int = 0
    """被丢弃的推送条数。>0 说明有客户端跟不上——它重连后靠 REST 拉真实状态。"""
    latest_event_id: int = 0
    storage: "StorageReportResponse"


class AttentionResponse(ApiResponse):
    """「需处理」入口（OBS-05）：审批、失败、清理未完成、状态不明。"""

    approvals: list[Approval] = Field(default_factory=list)
    failed_tasks: list[Task] = Field(default_factory=list)
    lost_stages: list[TaskStage] = Field(default_factory=list)
    unresolved_resources: list[dict[str, Any]] = Field(default_factory=list)
    startup_notes: list[str] = Field(default_factory=list)


class StorageReportResponse(ApiResponse):
    """存储占用（RES-03）。提供查看入口，不强制自动 TTL：历史≠泄漏。"""

    database: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    unresolved_resources: int = 0
    last_run: dict[str, Any] | None = None


class SessionRecord(ApiResponse):
    """一条会话台账记录（REC-03）。

    字段与 supervisor 的 ``session_ledger``、内核的 ``session_handle`` 同源——
    排障时最要紧的问题是「我的任务还连着哪个会话、它还活着吗」。
    """

    session_ref: str
    harness_id: str
    owner_task_id: str | None = None
    owner_stage_id: str | None = None
    owner_attempt_id: str | None = None
    state: str = "unknown"
    created_at: str | None = None
    last_heartbeat: str | None = None


class SessionListResponse(ApiResponse):
    """会话台账视图。**如实反映当前部署形态**，不伪造。"""

    sessions: list[SessionRecord] = Field(default_factory=list)
    returned: int = 0
    source: str = "none"
    """台账来源：``supervisor``（独立进程持有会话）或 ``session_handle``（内核本地表）。"""
    reachable: bool = True
    """来源此刻是否真的可读。**为 False 时空列表不能读成「没有会话」**——
    它表示「问不到」，与「确实没有」是两件事。"""
    note: str | None = None


class SessionAttachResponse(ApiResponse):
    """接入一个正在运行的会话（HUM-01/02）。

    ``readable`` 与 ``writable`` 分开报，因为它们对应不同的处置：有些 harness
    的输入在创建会话时就已给定（``claude -p`` 一类），运行中无法再注入——那是
    「能看不能发」，不是「不可用」。合成一个布尔值，界面就只能对用户说
    「不支持」，而用户需要知道的是「能看，但发不进去」。

    ``output`` 只包含**本次内核进程内**累积的输出。它随尝试结束而消失，这是
    有意的：它是「看现在跑到哪了」的窗口，不是历史。回看已结束的尝试请看
    事件时间线与产物。
    """

    attachable: bool = False
    readable: bool = False
    writable: bool = False
    interruptible: bool = False
    reason: str | None = None
    """不可用的原因。可用时为 ``None``。"""
    output: str = ""
    total_chars: int = 0
    truncated: bool = False
    harness_id: str = ""


class SessionInputRequest(ApiRequest):
    """向运行中的会话注入一条消息（HUM-01）。"""

    text: str
    """要注入的内容。空字符串会被拒绝。"""


class SessionInputResponse(ApiResponse):
    delivered: bool = False
    reason: str | None = None


class PruneRequest(ApiRequest):
    """手动清理入口（RES-03）。**默认 dry_run**：先看清要删什么，再动手。"""

    dry_run: bool = True
    task_id: str | None = None
    """按任务清理其事件历史（连同该任务的删除是可回看的整块操作）。"""
    keep_last: int = 0
    """按任务清理时保留的最新条数。"""
    older_than: str | None = None
    """ISO 时间戳：清理早于该时刻的事件。与 task_id 互斥。"""
    include_orphans: bool = False
    """是否顺带跑一轮孤儿进程／句柄对账（Reaper.run_once）。"""
    include_artifacts: bool = False
    """是否顺带回收 tombstone 且无引用的产物物理文件。"""
    include_unreferenced_artifacts: bool = False
    """更激进：连未 tombstone 的零引用产物一并回收（仍拒绝清理被活跃任务引用的）。"""


class PruneAction(ApiResponse):
    """一条已执行（或拟执行）的清理动作。**要说明删了什么**，不是只回一个数字。"""

    kind: str
    scope: str
    deleted: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class PruneResponse(ApiResponse):
    dry_run: bool
    actions: list[PruneAction] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    storage: StorageReportResponse


# ===========================================================================
# Workflow 定义
# ===========================================================================


class WorkflowResponse(ApiResponse):
    workflow_id: str
    name: str
    description: str | None = None
    current_revision_seq: int = 0
    status: WorkflowStatus
    max_concurrent_tasks: int = 8
    created_at: str | None = None
    updated_at: str | None = None


class WorkflowListResponse(ApiResponse):
    workflows: list[WorkflowResponse] = Field(default_factory=list)
    returned: int = 0


class WorkflowCreateRequest(ApiRequest):
    name: str
    description: str | None = None
    max_concurrent_tasks: int = Field(default=8, ge=1)


class WorkflowPatchRequest(ApiRequest):
    """局部更新。状态与会话无关的字段可改；修订内容一律走 revision 端点。"""

    name: str | None = None
    description: str | None = None
    max_concurrent_tasks: int | None = Field(default=None, ge=1)
    status: WorkflowStatus | None = None


class RevisionResponse(ApiResponse):
    workflow_id: str
    revision_seq: int
    graph: GraphSpec
    source: RevisionSource
    draft_of: int | None = None
    is_published: bool = False
    note: str | None = None
    effective_graph_version: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class RevisionListResponse(ApiResponse):
    workflow_id: str
    revisions: list[RevisionResponse] = Field(default_factory=list)


class RevisionSaveRequest(ApiRequest):
    """保存新修订（修订不可变：编辑即新建，WF-06）。"""

    graph: GraphSpec
    publish: bool = False
    """True：同时设为当前已发布版本，之后发射的任务钉扎它。"""
    base_revision_seq: int | None = None
    """**乐观并发 CAS（D-02）**：客户端所基于的修订号。
    与服务端 current_revision_seq 不一致时返回 409 并附最新版本，由用户决定合并或重试。
    None 表示不做 CAS 检查（仅供脚本化导入使用，前端不要省这个字段）。"""
    note: str | None = None
    source: RevisionSource = RevisionSource.MANUAL


class RevisionSaveResponse(ApiResponse):
    accepted: bool = True
    workflow_id: str
    revision_seq: int
    published: bool = False
    effective_graph_version: int = 0
    note: str | None = None
    cas_checked: bool = False
    base_revision_seq: int | None = None


class ConflictResponse(ApiResponse):
    """CAS 冲突（409）。附**最新版本**，让前端显示差异而不是让用户干猜（D-02）。"""

    detail: str
    workflow_id: str
    latest_revision_seq: int = 0
    latest_revision: RevisionResponse | None = None
    hint: str | None = None


class ValidateRequest(ApiRequest):
    graph: GraphSpec | None = None
    """None 表示校验该 Workflow 当前的修订（前端「保存前预检」可只传 mode）。"""
    mode: ValidationMode = ValidationMode.PUBLISH


# ===========================================================================
# 任务
# ===========================================================================


class TaskSubmitRequest(ApiRequest):
    input_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    """同 key 重复提交返回**同一个 task_id**，不产生额外任务（RUN-02）。"""
    priority: int = Field(default=50, ge=0, le=100)


class SubmitResponse(ApiResponse):
    """发射结果。``accepted=False`` 时 ``report`` 必然存在且可定位到节点／连线／配置。

    校验失败返回 **422**（不是笼统的 400）：这是「请求本身合法、内容不可执行」。
    """

    accepted: bool
    task_id: str | None = None
    created: bool | None = None
    """False 表示命中幂等键，返回的是**已存在**的那次提交。"""
    report: ValidationReport | None = None


class TaskListResponse(ApiResponse):
    tasks: list[Task] = Field(default_factory=list)
    returned: int = 0
    limit: int = 50
    offset: int = 0
    has_more: bool = False


class TaskDetailResponse(ApiResponse):
    """一次任务的完整视图（OBS-03）。"""

    task: Task
    stages: list[TaskStage] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)


class PauseRequest(ApiRequest):
    """暂停整次任务（LIFE-01）。节点只是定位任务与记录来源的入口（§10.1）。"""

    from_node_id: str | None = None
    reason: str | None = None


class ResumeRequest(ApiRequest):
    restart_failed: bool = False
    """是否连失败阶段一起重跑。默认 False——只恢复被暂停的。
    不重跑已成功阶段（LIFE-03）。"""


class DeleteTaskRequest(ApiRequest):
    from_node_id: str | None = None
    reason: str | None = None


class StageResumeResponse(ApiResponse):
    """断点续跑（§11.3）。``had_checkpoint=False`` 时必须如实告诉用户「会从头重跑」。"""

    task_id: str
    node_id: str
    stage_id: str
    had_checkpoint: bool = False
    note: str


# ===========================================================================
# 注册表
# ===========================================================================


class HarnessCreateRequest(ApiRequest):
    harness_id: str | None = None
    name: str
    adapter_id: str
    adapter_version: str | None = None
    exec_path: str | None = None
    env_template: dict[str, str] = Field(default_factory=dict)
    """只放**非凭据型**环境变量；需要认证请用 ``auth_binding`` 引用凭据（AUTH-02）。"""
    cwd: str | None = None
    auth_binding: str | None = None
    auth_mode: AuthMode = AuthMode.NATIVE_LOGIN
    enabled: bool = True


class HarnessPatchRequest(ApiRequest):
    name: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    exec_path: str | None = None
    env_template: dict[str, str] | None = None
    cwd: str | None = None
    auth_binding: str | None = None
    auth_mode: AuthMode | None = None
    enabled: bool | None = None


class ProbeResponse(ApiResponse):
    """能力探测结果（HAR-02）。失败如实上报，不伪造 capabilities。"""

    harness_id: str
    ok: bool
    capabilities: dict[str, Any] | None = None
    error: str | None = None
    probed_at: str
    source: str
    """这次结论来自哪里：``probe``（适配器实测）或 ``capabilities``（接口声明）。"""
    note: str | None = None


class CredentialCreateRequest(ApiRequest):
    """登记一份凭据**引用**，或同时把密钥本体交给内核写入凭据库。

    ``secret`` 提供时：内核把它写进 Secret Store（加密落盘）并自动建立引用，
    响应里**不含**密钥本体。不提供时本接口只登记引用，指向凭据库里已有的条目。
    """

    credential_id: str | None = None
    label: str
    kind: CredentialKind
    secret_locator: str | None = None
    """指向 Secret Store 的条目名（如 ``secret://openai``）。
    ``harness_login`` 类型留空——凭据由 harness 自身登录态提供。
    提供 ``secret`` 时可省略，内核会按 credential_id 自动生成。"""
    base_url: str | None = None
    default_model: str | None = None
    """该凭据对应的默认模型名；节点候选留空模型时回退到它。"""
    secret: dict[str, str] | None = None
    """密钥本体（如 ``{"api_key": "sk-..."}``，base_url_pair 再加 ``"base_url"``）。
    只在请求体里出现一次，写库后即弃；不落日志、不进事件、不在响应里回显。"""


class RevokeRequest(ApiRequest):
    revoked: bool = True


class SkillCreateRequest(ApiRequest):
    skill_id: str | None = None
    name: str
    content: str = ""
    version: int = 1
    scope: SkillScope = SkillScope.GLOBAL
    enabled: bool = True


class ToolCreateRequest(ApiRequest):
    tool_id: str | None = None
    name: str
    launch: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    io_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    approval_policy: ApprovalPolicy = ApprovalPolicy.ASK
    version: int = 1
    health_check: bool = True
    enabled: bool = True


# ===========================================================================
# 模板
# ===========================================================================


class TemplateCreateRequest(ApiRequest):
    """两种来源，二选一（都不允许携带明文凭据，TPL-03）：

    - 直接给 ``payload``：模板载荷（凭据只能是引用，类型上装不下密钥本体）；
    - 给 ``from_workflow_id``：从该 Workflow 当前修订构造——委托给
      ``Template.from_nodes``，密钥结构性进不了模板；本机凭据的**引用**
      默认随模板保留（``keep_credential_refs``），同机实例化自动绑定。
    """

    name: str
    description: str | None = None
    kind: TemplateKind = TemplateKind.WORKFLOW
    payload: TemplatePayload | None = None
    from_workflow_id: str | None = None
    from_revision_seq: int | None = None
    keep_credential_refs: bool = True
    """从流程生成时是否保留本机凭据的引用（只是 id，不含密钥）。
    同机复用省去逐个重绑；要把模板分享给他机时置 False 全剥离。"""


class TemplateListResponse(ApiResponse):
    templates: list[Template] = Field(default_factory=list)
    returned: int = 0


class TemplateInstantiateRequest(ApiRequest):
    bindings: dict[str, str] = Field(default_factory=dict)
    """槽位（或原凭据 label）→ 本机 credential_id。"""
    node_id_map: dict[str, str] = Field(default_factory=dict)
    """可选：节点名 → 指定 node_id，便于调用方对齐既有图。"""


class TemplateInstantiateResponse(ApiResponse):
    """实例化结果。``report.missing_bindings`` 非空时**不编造凭据**，
    如实要求补齐（WF-02 的同一条纪律）。"""

    template_id: str
    usable: bool
    nodes: list[NodeDefinition] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    report: InstantiationReport


# ===========================================================================
# 审批
# ===========================================================================


class ApprovalListResponse(ApiResponse):
    approvals: list[Approval] = Field(default_factory=list)
    returned: int = 0


class ApprovalDecideRequest(ApiRequest):
    approve: bool
    modified_action: str | None = None
    """修改后批准：回注原会话的是修改后的动作，原动作不再被授权。"""
    by: str = "user"
    note: str | None = None


class DeliveryResponse(ApiResponse):
    """决定回注结果（HUM-04）。

    **重复决定不报错**：返回的是「实际生效结果」——``status`` 是审批当前的真实状态，
    ``detail`` 说明本次决定为何没有第二次生效（AC-14）。
    """

    approval_id: str
    delivered: bool = False
    status: str
    detail: str | None = None


# ===========================================================================
# 基础助手（AI-01）
# ===========================================================================


class AssistantThreadCreateRequest(ApiRequest):
    title: str | None = None
    """留空时用小窗默认标题。"""


class AssistantThreadResponse(ApiResponse):
    thread_id: str
    title: str = ""
    closed: bool = False
    created_at: str
    updated_at: str


class AssistantThreadListResponse(ApiResponse):
    threads: list[AssistantThreadResponse] = Field(default_factory=list)
    returned: int = 0


class AssistantThreadRenameRequest(ApiRequest):
    """改对话名。故意不用长度/非空的字段校验：非法取值由服务层报
    400 + 大白话提示，而不是框架默认的 422 校验报错。"""

    title: str


class AssistantDraftResponse(ApiResponse):
    """一份助手草稿提案（迁移 9 的 assistant_draft 表）。

    ``validation`` 是提案落库（或采用前复核）时刻的校验结论：
    ``ok`` / ``summary`` / ``diagnostics``（与校验管线同形状）/
    ``pending_config``（「待配置」项清单，前端标黄）/ ``error``（提案本身
    无法构造时的大白话原因）。
    """

    draft_id: str
    message_id: str
    thread_id: str
    kind: str
    """workflow / node_template。"""
    name: str = ""
    description: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    """提案原文（节点与连线清单）。"""
    validation: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    """pending / adopted / rejected（单向流转）。"""
    adopted_ref: str | None = None
    """采用产物的 id：workflow 提案是 workflow_id，节点模板是 template_id。"""
    created_at: str
    updated_at: str


class AssistantMessageResponse(ApiResponse):
    """一条对话消息。``tokens_*`` 为 None 表示后端没给用量（未知，不是 0）。"""

    message_id: str
    thread_id: str
    role: str
    """user / assistant / memory（「整理前文」产出的摘要）。"""
    content: str
    backend: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    degraded: bool = False
    """True 表示这条回复发生过降级（备用后端、快照裁剪、指引缺失等）。"""
    reasoning: str | None = None
    """助手的推理过程（思维链）。None 表示这次回答没有推理内容（未知 ≠ 空串）。"""
    drafts: list[AssistantDraftResponse] = Field(default_factory=list)
    """这条回复携带的草稿提案（通常为空列表）。"""
    created_at: str


class AssistantMessageListResponse(ApiResponse):
    messages: list[AssistantMessageResponse] = Field(default_factory=list)
    returned: int = 0


class AssistantSendRequest(ApiRequest):
    content: str
    """用户的问题。空白内容会被拒绝。"""


class AssistantSendResponse(ApiResponse):
    """一次问答的结果：助手回复 + 本次发送的窗口/降级事实。"""

    message: AssistantMessageResponse
    user_message: AssistantMessageResponse
    dropped: int = 0
    """因窗口限制未随本次发送的更早消息条数（滑动窗口截断，不产生额外调用）。"""
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)


class AssistantCompactResponse(ApiResponse):
    """「整理前文」的结果。这是唯一会额外调用一次模型的记忆操作。"""

    compacted: bool
    summarized: int = 0
    """被整理进摘要的旧消息条数。"""
    memory_message_id: str | None = None
    note: str | None = None


class AssistantConfigResponse(ApiResponse):
    """助手配置。**只含引用与参数，密钥本体永远不会出现在这里**（只进不出）。"""

    enabled: bool = False
    credential_ref: str | None = None
    model_override: str | None = None
    api_protocol: Literal["openai", "anthropic"] = "openai"
    """助手请求凭据 Base URL 用的接口协议。``openai`` 打 ``/chat/completions``；
    ``anthropic`` 打 ``/v1/messages``（Base URL 路径含 /anthropic 时选它）。"""
    window_rounds: int = 12
    window_chars: int = 12000
    snapshot_budget: int = 6000
    secrets_unlocked: bool = False
    """凭据库此刻是否已解锁。未解锁时即使配置齐了也读不到密钥——
    前端据此显示引导文案，而不是等发问时才报错。"""


class AssistantConfigUpdateRequest(ApiRequest):
    """局部更新。只接受引用（credential_ref），不接受任何密钥本体。

    ``api_protocol`` 故意不用 Literal：取值不合法时由服务层报 400 + 大白话提示，
    而不是框架默认的 422 校验报错。
    """

    enabled: bool | None = None
    credential_ref: str | None = None
    model_override: str | None = None
    api_protocol: str | None = None
    """接口协议：``openai`` / ``anthropic``。其它取值在服务层拒绝。"""
    window_rounds: int | None = Field(default=None, ge=1, le=100)
    window_chars: int | None = Field(default=None, ge=500, le=200000)
    snapshot_budget: int | None = Field(default=None, ge=500, le=100000)


# ===========================================================================
# 流程捕获（Graph Capture，WF-03）
# ===========================================================================


class CaptureRunCreateRequest(ApiRequest):
    """新建捕获任务：写任务说明、选一个基础候选。系统会**真实执行**一次。"""

    name: str
    instructions: str
    """任务说明——就是发给基础模型去执行的那段话。"""
    harness_ref: str
    model_name: str | None = None
    """留空表示用 harness 的默认模型（与节点候选同一口径）。"""
    credential_ref: str | None = None


class CaptureRunFromTaskRequest(ApiRequest):
    """从既有任务补捕获：任务已经跑过，直接把它的执行记录转成流程草案。

    **不重新执行任务**——没有执行记录（没有任何 attempt）的任务会被拒绝。
    """

    task_id: str
    name: str | None = None
    """留空时用任务输入的前 20 个字。"""


class CaptureRunResponse(ApiResponse):
    """一次捕获任务的台账。执行状态的事实源是任务表，``status`` 是它的收敛。"""

    run_id: str
    name: str
    origin: str = "live"
    """live：专门为捕获跑的临时流程；from_task：从既有任务补捕获。"""
    workflow_id: str
    """live 时是捕获专用的临时单节点 Workflow（不进默认流程列表）；
    from_task 时是来源任务所属的真实流程（仅作信息关联，不会被隐藏）。"""
    task_id: str | None = None
    """真实执行的任务 id；发射失败时为 None。"""
    profile: dict[str, Any] = Field(default_factory=dict)
    """基础候选快照（harness/模型/凭据引用 + 任务说明）。from_task 时取来源
    任务最近一次 attempt 的候选快照，取不到的字段如实为空（界面显示「未知」）。"""
    status: str = "running"
    """running / completed / failed。"""
    created_at: str
    updated_at: str


class CaptureRunListResponse(ApiResponse):
    runs: list[CaptureRunResponse] = Field(default_factory=list)
    returned: int = 0


class CaptureMaterialSummary(ApiResponse):
    """材料汇编的概览。「被裁过」必须可见——trimmed 非空说明材料不是全貌。"""

    tool_calls: int = 0
    artifacts: int = 0
    stages: int = 0
    """任务实际走过的阶段数。大于 1 时材料含「执行路径」分区且按阶段分组。"""
    has_plan: bool = False
    """模型是否在输出里显式写了执行计划。False 时草案的划分几乎全是推断。"""
    chars: int = 0
    trimmed: list[str] = Field(default_factory=list)


class CaptureDraftResponse(ApiResponse):
    """一份捕获草案（capture_draft 表）。

    ``payload`` 是提案原文，节点/连线带 ``basis``（observed/inferred）与
    ``evidence`` 标注；``validation`` 含校验结论、「待配置」项与服务端
    复核的降级记录（``downgrades``）。
    """

    draft_id: str
    run_id: str
    name: str = ""
    description: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    """pending / adopted / rejected（单向流转）。"""
    adopted_ref: str | None = None
    """采用产物的 id：workflow_id 或 template_id。"""
    created_at: str
    updated_at: str


class CaptureRunDetailResponse(ApiResponse):
    run: CaptureRunResponse
    task_state: str | None = None
    """任务的可观测状态（queued/running/succeeded/...）。没发射成功时为 None。"""
    material: CaptureMaterialSummary | None = None
    drafts: list[CaptureDraftResponse] = Field(default_factory=list)


class CaptureDraftAdoptRequest(ApiRequest):
    """采用草案。``as_template=True`` 时存为模板而不是建流程。"""

    as_template: bool = False


StorageReportResponse.model_rebuild()
SystemStatusResponse.model_rebuild()
