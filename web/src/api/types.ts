/**
 * 内核 HTTP/WS 契约的类型镜像。
 *
 * 字段名与后端 `workerbee/server/schemas.py` + `workerbee/core/domain/*.py` 逐字对齐，
 * 只做 camel→原始保留（后端就是 snake_case，前端不转换），避免两处手写漂移。
 * 后端模型是 response_model（extra="ignore"）——多出来的字段前端忽略即可，
 * 但**声明了的字段一个都不能少**，否则渲染层会读到 undefined。
 *
 * 状态枚举与 `core/domain/task.py` 的 TaskState / StageState 完全一致。
 * 任何后端没有的状态（例如 "loading"）都不允许出现在这里：用户看到的必须是事实。
 */

// ===========================================================================
// 状态枚举 —— 权威定义在 core/domain/task.py
// ===========================================================================

/** TaskState。终态：succeeded / failed / cancelled。 */
export const TASK_STATES = [
  'queued',
  'running',
  'pausing',
  'paused',
  'cancelling',
  'cancelled',
  'succeeded',
  'failed',
  'blocked',
  'reconciling',
] as const;
export type TaskState = (typeof TASK_STATES)[number];

/** StageState。 */
export const STAGE_STATES = [
  'waiting_deps',
  'ready',
  'dispatching',
  'running',
  'awaiting_approval',
  'pausing',
  'paused',
  'retrying',
  'succeeded',
  'failed',
  'skipped',
  'cancelled',
  'blocked',
  'lost',
  'reconciling',
] as const;
export type StageState = (typeof STAGE_STATES)[number];

/** DesiredState：控制意图，与 observed 双轨。 */
export const DESIRED_STATES = ['active', 'paused', 'cancelled'] as const;
export type DesiredState = (typeof DESIRED_STATES)[number];

/**
 * 过渡态（OBS-01 硬要求：控制操作尚在处理中必须可见）。
 * 这些状态必须与稳定态在视觉上区分——运维时「正在收敛」和「已经停住」是两回事。
 */
export const TASK_TRANSITIONING_STATES: readonly TaskState[] = ['pausing', 'cancelling', 'reconciling'];
export const STAGE_TRANSITIONING_STATES: readonly StageState[] = [
  'dispatching',
  'pausing',
  'retrying',
  'awaiting_approval',
  'reconciling',
  'lost',
];

export type WorkflowStatus = 'draft' | 'published' | 'archived' | 'deleted';
export type RevisionSource = 'manual' | 'ai_generated' | 'graph_capture' | 'template';
export type ValidationMode = 'draft' | 'publish' | 'launch';
export type Severity = 'error' | 'warning' | 'info';
export type ErrorClass = 'success' | 'retryable_error' | 'fatal_error' | 'user_cancelled';
export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired' | 'undeliverable' | 'superseded';
export type CredentialKind = 'api_key' | 'oauth' | 'base_url_pair' | 'harness_login';
export type AuthMode = 'api_key' | 'oauth' | 'base_url_pair' | 'native_login';
export type RiskLevel = 'low' | 'medium' | 'high';
export type ApprovalPolicy = 'auto' | 'ask' | 'deny';
export type SkillScope = 'global' | 'node_local';
export type TemplateKind = 'workflow' | 'node';
export type ArtifactKind = 'text' | 'file' | 'code' | 'structured';
export type Sensitivity = 'public' | 'internal' | 'sensitive';
export type ContractFormat = 'text' | 'markdown' | 'json' | 'code' | 'file' | 'any';

// ===========================================================================
// 定义层（L1）
// ===========================================================================

export interface RetryPolicy {
  max_attempts: number;
  backoff_base_ms: number;
  backoff_cap_ms: number;
  retryable_errors: string[];
  jitter_ratio: number;
}

export interface ExecutionProfile {
  profile_id: string;
  model_name: string;
  harness_ref: string | null;
  credential_ref: string | null;
  /** 以适配器验证的能力为准；不可用的取值必须提示，不能静默忽略（CFG-02）。 */
  reasoning_effort: string | null;
  /** 权限模式（HUM-03）。harness 无权限钩子时必须显式选一个不询问的模式。 */
  permission_mode: string | null;
  retry: RetryPolicy;
  /** 用户**期望**触发整理的阈值，不是模型最大窗口（CFG-04）。 */
  compact_threshold: number | null;
  extra: Record<string, string>;
}

export interface VersionedRef {
  ref_id: string;
  version: number | null;
}

export interface NodeDefinition {
  node_id: string;
  name: string;
  role: string | null;
  description: string | null;
  /** 启停唯一事实源（ACT-01）。 */
  enabled: boolean;
  system_prompt: string | null;
  /** 执行候选，有序即优先级。 */
  profiles: ExecutionProfile[];
  skill_refs: VersionedRef[];
  tool_refs: VersionedRef[];
  /** 下游声明的必需输入字段名（ACT-03）。 */
  required_inputs: string[];
  ui_position: [number, number] | null;
  created_at?: string;
  updated_at?: string;
}

export interface EdgeContract {
  outputs: string[];
  format: ContractFormat;
  description: string | null;
  example: string | null;
}

export interface Edge {
  from_node: string;
  to_node: string;
  output_contract: EdgeContract | null;
  desc: string | null;
}

export interface ContractWaiver {
  node_id: string;
  required_input: string;
  reason: string | null;
  at: string;
}

export interface GraphSpec {
  nodes: NodeDefinition[];
  edges: Edge[];
  waivers: ContractWaiver[];
}

export interface WorkflowDefinition {
  workflow_id: string;
  name: string;
  description: string | null;
  current_revision_seq: number;
  status: WorkflowStatus;
  max_concurrent_tasks: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkflowRevision {
  workflow_id: string;
  revision_seq: number;
  graph: GraphSpec;
  source: RevisionSource;
  draft_of: number | null;
  is_published: boolean;
  note: string | null;
  effective_graph_version: number;
  created_at: string | null;
  updated_at: string | null;
}

/** 保存修订的结果（`RevisionSaveResponse`）。修订不可变：每次保存产生新序号。 */
export interface RevisionSaveResult {
  accepted: boolean;
  workflow_id: string;
  revision_seq: number;
  published: boolean;
  /** 有效图版本。草稿不推进它——只有发布才改有效图。 */
  effective_graph_version: number;
  note: string | null;
  /** 本次是否真的做了 CAS 检查（无 base_revision_seq 时为 false）。 */
  cas_checked: boolean;
  base_revision_seq: number | null;
}

// ===========================================================================
// 校验（WF-05 / ACT-03）
// ===========================================================================

/**
 * 一条可定位的校验结论。`node_id` / `edge` / `slot` 是「点一条跳过去」的依据——
 * 界面上每条都必须能落到具体节点或连线上。
 */
export interface Diagnostic {
  code: string;
  severity: Severity;
  message: string;
  node_id: string | null;
  node_name: string | null;
  edge: [string, string] | null;
  profile_id: string | null;
  slot: string | null;
  hint: string | null;
  requirement: string | null;
  fix_action: string | null;
}

export interface ValidationReport {
  mode: ValidationMode;
  diagnostics: Diagnostic[];
}

// ===========================================================================
// 三张图：derive 的差异表示（ACT-02 路径可见）
// ===========================================================================

export interface DerivedEdge {
  from_node: string;
  to_node: string;
  /** 被绕过的 disabled 中间节点，按路径顺序。非空即「绕过边」。 */
  via: string[];
}

export interface GraphDelta {
  node_id: string;
  enabling: boolean;
  added_edges: DerivedEdge[];
  removed_edges: DerivedEdge[];
  entry_nodes_before: string[];
  entry_nodes_after: string[];
  exit_nodes_before: string[];
  exit_nodes_after: string[];
  affected_downstream: string[];
}

// ===========================================================================
// 运行时（L2）
// ===========================================================================

export interface OriginOfControl {
  op: string;
  from_node_id: string | null;
  at: string;
  scope: string;
  detail: string | null;
}

export interface Usage {
  /** None = 不可取得（未知），不是 0（OBS-04）。 */
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cost_estimate: number | null;
  cost_basis: string | null;
  notes: string | null;
}

export interface CompactEvent {
  at: string;
  trigger: string;
  effective_threshold: number | null;
  tokens_before: number | null;
  tokens_after: number | null;
  ok: boolean;
  detail: string | null;
}

export interface AttemptOutcome {
  error_class: ErrorClass;
  detail: string | null;
  error_kind: string | null;
  at: string;
}

export interface Attempt {
  attempt_id: string;
  stage_id: string;
  task_id: string;
  node_id: string;
  attempt_seq: number;
  profile_id: string;
  /** 实际采用的参数快照（模型、harness、effort、实际 compact 阈值…）。 */
  profile_snapshot: Record<string, unknown>;
  session_ref: string | null;
  lease_id: string | null;
  lease_expires_at: string | null;
  generation: number;
  usage: Usage | null;
  outcome: AttemptOutcome | null;
  compact_events: CompactEvent[];
  started_at: string | null;
  ended_at: string | null;
  resume_from_checkpoint: string | null;
  reattached: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface TaskStage {
  stage_id: string;
  task_id: string;
  node_id: string;
  /** 节点名快照，供历史回看（节点可能已被改名或移除）。 */
  node_name: string | null;
  desired_state: DesiredState;
  observed_state: StageState;
  control_epoch: number;
  node_priority: number;
  task_priority: number;
  enqueued_at: string;
  current_attempt_seq: number;
  attempt_count: number;
  profile_cursor: number;
  blocked_reason: string | null;
  /** 面向用户的状态说明，例如「等待审批中，不占用执行槽」。 */
  status_reason: string | null;
  origin_of_control: OriginOfControl | null;
  /** 上游 node_id → 该上游本次成功尝试产出的 artifact_id 列表（D-06 版本 pinned ）。 */
  upstream_pins: Record<string, string[]>;
  checkpoint_ref: string | null;
  requires_reconcile: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface PinnedGraph {
  graph: GraphSpec;
  effective_edges: [string, string][];
  effective_graph_version: number;
}

export interface FailureSummary {
  /** 失败/受阻原因与**尚在运行的分支**必须同时显示（RUN-07）。 */
  reason?: string;
  error_class?: string;
  detail?: string;
  running_branches?: string[];
  failed_stages?: string[];
  blocked_stages?: string[];
  [key: string]: unknown;
}

export interface Task {
  task_id: string;
  idempotency_key: string | null;
  workflow_id: string;
  workflow_name: string | null;
  revision_seq: number;
  effective_graph_version: number;
  graph_snapshot: PinnedGraph;
  input_payload: Record<string, unknown>;
  desired_state: DesiredState;
  observed_state: TaskState;
  control_epoch: number;
  priority: number;
  failure_summary: FailureSummary | null;
  blocked_reason: string | null;
  last_origin: OriginOfControl | null;
  submitted_by: string;
  created_at?: string;
  updated_at?: string;
}

/** 一次工具调用的展示视图。is_error 为 null 表示结果还没回来（仍在执行）。 */
export interface ToolCallView {
  tool_use_id: string | null;
  name: string | null;
  target: string | null;
  input_preview: string;
  input_truncated: boolean;
  is_error: boolean | null;
  result_preview: string | null;
  result_truncated: boolean;
}

/** 一次执行尝试的工作细节（GET /api/tasks/{id}/attempts/{aid}/work）。 */
export interface AttemptWork {
  task_id: string;
  attempt_id: string;
  stage_id: string | null;
  node_id: string | null;
  input: {
    system_prompt?: string;
    system_prompt_truncated?: boolean;
    user_input?: string;
    user_input_truncated?: boolean;
  } | null;
  reasoning: string | null;
  reasoning_truncated: boolean;
  tool_calls: ToolCallView[];
  files_written: string[];
  files_read: string[];
  commands: string[];
  artifact_ids: string[];
}

/** 产物正文（GET /api/tasks/{id}/artifacts/{aid}/content）。有界截断，已过脱敏。 */
export interface ArtifactContent {
  artifact_id: string;
  text: string;
  truncated: boolean;
  size_bytes: number | null;
  media_type: string | null;
}

// ===========================================================================
// 审批（L5，HUM-03/04）
// ===========================================================================
export interface ApprovalBinding {
  task_id: string;
  stage_id: string;
  attempt_id: string;
  revision_seq: number;
  node_id: string | null;
  action_fingerprint: string | null;
}

export interface ApprovalDecision {
  by: string;
  at: string;
  approved: boolean;
  modified_action: string | null;
  note: string | null;
}

export interface Approval {
  approval_id: string;
  bound_to: ApprovalBinding;
  action: string;
  target: string | null;
  risk: string | null;
  tool_name: string | null;
  workflow_name: string | null;
  status: ApprovalStatus;
  timeout_policy: string;
  decision: ApprovalDecision | null;
  expires_at: string | null;
  detail: string | null;
  created_at?: string;
  updated_at?: string;
}

// ===========================================================================
// 注册表（L1 共享配置）
// ===========================================================================

export interface SkillDoc {
  skill_id: string;
  name: string;
  content: string;
  version: number;
  scope: SkillScope;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface ToolLaunch {
  command: string | null;
  args: string[];
  env: Record<string, string>;
  cwd: string | null;
  url: string | null;
  transport: 'stdio' | 'http' | 'sse';
  credential_ref: string | null;
}

export interface ToolSpec {
  tool_id: string;
  name: string;
  kind: 'mcp';
  launch: ToolLaunch;
  description: string | null;
  io_schema: Record<string, unknown>;
  risk_level: RiskLevel;
  approval_policy: ApprovalPolicy;
  version: number;
  health_check: boolean;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface CredentialRef {
  credential_id: string;
  label: string;
  kind: CredentialKind;
  secret_locator: string | null;
  base_url: string | null;
  revoked: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface HarnessRegistration {
  harness_id: string;
  name: string;
  adapter_id: string;
  adapter_version: string | null;
  exec_path: string | null;
  env_template: Record<string, string>;
  cwd: string | null;
  auth_binding: string | null;
  auth_mode: AuthMode;
  /**
   * 最近一次 probe() 的能力声明缓存。**是接口支持情况的声明，不是模型能力评级。**
   * 未探测时为 null——界面必须显示「未探测」，不能留空白让人以为是支持。
   */
  capabilities_snapshot: Record<string, unknown> | null;
  last_probe_at: string | null;
  last_probe_ok: boolean | null;
  last_probe_error: string | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

/**
 * 能力矩阵里逐项如实显示的布尔能力名（HAR-02）。
 * 取自适配器契约 `AdapterCapabilities`（`adapters/sdk/contract.py`，字段名与架构设计 §5.3 对齐）：
 * 每项都可能是 true / false / 缺失 / 非布尔四种情况，界面必须**分开呈现**——
 * 「显式 false」是「不支持」，「缺失」是「未声明」，两者不能都画成空白。
 */
export const HARNESS_CAPABILITY_KEYS = [
  'create_session',
  'resume_session',
  'read_output',
  'interact',
  'interrupt',
  'stop',
  'compact',
  'permission_hook',
  'background_tasks',
  // 暂停能力（D-07 四档：in_place / checkpoint / restart / none 的判定依据）
  'pause_in_place',
  'checkpoint_resume',
  'keep_checkpoint_on_stop',
  // 用量与输出（OBS-04：token_usage=false 时用量是「未知」而不是 0）
  'token_usage',
  'structured_output',
] as const;

/** 列表型能力：值不是布尔而是列表，单独渲染（空列表 = 该维度不适用）。 */
export const HARNESS_LIST_CAPABILITY_KEYS = ['reasoning_efforts', 'models', 'auth_modes'] as const;

// ===========================================================================
// 模板（TPL-01/02/03）
// ===========================================================================

export interface CredentialPlaceholder {
  slot: string;
  original_label: string | null;
  original_kind: string | null;
}

export interface TemplateNodeConfig {
  name: string;
  role: string | null;
  description: string | null;
  system_prompt: string | null;
  profiles: ExecutionProfile[];
  skill_refs: VersionedRef[];
  tool_refs: VersionedRef[];
  required_inputs: string[];
}

export interface TemplatePayload {
  nodes: TemplateNodeConfig[];
  edges: Edge[];
  sensitive_slots: CredentialPlaceholder[];
}

export interface Template {
  template_id: string;
  name: string;
  description: string | null;
  kind: TemplateKind;
  payload: TemplatePayload;
  version: number;
  source_revision: number | null;
  source_workflow_id: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface MissingBinding {
  slot: string;
  label: string | null;
  reason: string;
}

export interface InstantiationReport {
  /** 非空时**不编造凭据**，如实要求补齐（TPL-03）。 */
  missing_bindings: MissingBinding[];
  usable: boolean;
  notes: string[];
}

export interface TemplateInstantiateResult {
  template_id: string;
  usable: boolean;
  nodes: NodeDefinition[];
  edges: Edge[];
  report: InstantiationReport;
}

// ===========================================================================
// 系统
// ===========================================================================

export interface EventRecord {
  event_id: number;
  scope: string;
  scope_id: string | null;
  type: string;
  actor: string;
  task_id: string | null;
  stage_id: string | null;
  payload: Record<string, unknown>;
  refs: string[];
  ts: string;
}

export interface EventPage {
  events: EventRecord[];
  /** 客户端下次续拉的水位（REC-01 断连补齐）。 */
  latest_event_id: number;
  returned: number;
  has_more: boolean;
  note: string | null;
}

export interface SchedulerStats {
  /** 未装配时任务不会自动推进——界面必须如实显示。 */
  enabled: boolean;
  /** 调度循环的空转间隔，**单位秒**（内核里作为 asyncio 超时使用，默认 1.0）。 */
  poll_interval: number;
}

export interface ReaperStats {
  interval_seconds: number;
  artifact_gc_enabled: boolean;
  last_run: Record<string, unknown> | null;
}

export interface CountsResponse {
  live_tasks: number;
  running_stages: number;
  pending_stages: number;
  open_approvals: number;
  workflows: number;
}

export interface StorageReport {
  database: Record<string, unknown>;
  artifacts: Record<string, unknown>;
  unresolved_resources: number;
  last_run: Record<string, unknown> | null;
}

/**
 * 会话托管形态。
 *
 * `supervisor`：harness 子进程由独立的 supervisor 进程持有，内核重启不会打断在途任务。
 * `in_process`：harness 子进程由内核自己持有——**内核重启会打断正在跑的任务**。
 * 用户必须知道自己处在哪一种形态下，否则会默认「重启不丢任务」而实际上会丢。
 */
export type SessionHosting = 'supervisor' | 'in_process';

export interface SystemStatus {
  version: string;
  /** 降级声明（如「supervisor 不可达」「摘要器不可用，已退化为截断式摘要」）。必须显著呈现。 */
  startup_notes: string[];
  session_hosting: SessionHosting;
  /** supervisor 是否连通。in_process 形态下该值无意义，不应据此显示故障。 */
  supervisor_connected: boolean;
  data_dir: string;
  workspace_dir: string;
  /** 凭据库是否已解锁。未解锁时引用凭据的节点会明确报错。 */
  secrets_unlocked: boolean;
  harness_attached: boolean;
  allow_remote: boolean;
  loopback_only: boolean;
  scheduler: SchedulerStats;
  reaper: ReaperStats;
  counts: CountsResponse;
  notifier_subscribers: number;
  /** >0 说明有客户端跟不上——它重连后靠 REST 拉真实状态。 */
  notifier_dropped: number;
  latest_event_id: number;
  storage: StorageReport;
}

export interface AttentionResponse {
  approvals: Approval[];
  failed_tasks: Task[];
  lost_stages: TaskStage[];
  unresolved_resources: Record<string, unknown>[];
  /** 与 SystemStatus.startup_notes 同源，一并如实呈现（不折叠成小图标）。 */
  startup_notes: string[];
}

export interface PruneAction {
  kind: string;
  scope: string;
  deleted: number;
  detail: Record<string, unknown>;
}

export interface PruneResult {
  dry_run: boolean;
  actions: PruneAction[];
  notes: string[];
  storage: StorageReport;
}

export interface HealthResponse {
  ok: boolean;
  version: string;
}

// ===========================================================================
// 生命周期操作：三个独立结论（LIFE-06）
// ===========================================================================

/**
 * **不要把这三个合成一个「成功」。**
 * accepted 只表示控制意图已落库，不代表已经停下。
 */
export interface TriStateOutcome {
  accepted: boolean;
  execution_stopped: boolean;
  /** closed / teardown_failed / orphaned / skipped 计数。 */
  resources: Record<string, number>;
  warnings: string[];
}

export interface PauseResponse extends TriStateOutcome {
  task_id: string;
  /** 未启动就被暂停的阶段（零成本）。 */
  paused_immediately: string[];
  /** 协作停止的阶段——可能有重复工作，UI 必须说明（LIFE-02）。 */
  stopped_cooperatively: string[];
  paused_in_place: string[];
  /** 阶段 → 断点。值为 null 表示只能从头重跑本阶段，必须如实展示。 */
  checkpoints: Record<string, string | null>;
  /** harness 不支持暂停的阶段。如实拒绝，不显示为已暂停。 */
  unsupported: string[];
}

export interface ResumeResponse extends TriStateOutcome {
  task_id: string;
  requeued: string[];
  /** 从断点或从头重建的阶段——用户需要知道「哪些要重做」。 */
  restarted: string[];
  reused_sessions: string[];
}

export interface DeleteTaskResponse extends TriStateOutcome {
  task_id: string;
  cancelled_stages: string[];
  /** 已完成阶段保留其真实结果，不被改写为失败。 */
  preserved_succeeded: string[];
}

export interface WorkflowDeleteResponse extends TriStateOutcome {
  workflow_id: string;
  tasks_terminated: string[];
  shared_configs_kept: string[];
}

export interface StageResumeResponse {
  task_id: string;
  node_id: string;
  stage_id: string;
  /** false 时必须如实告诉用户「会从头重跑」。 */
  had_checkpoint: boolean;
  note: string;
}

// ===========================================================================
// 启停（ACT-01–04、D-01）
// ===========================================================================

export interface TogglePreview {
  node_id: string;
  enabling: boolean;
  delta: GraphDelta;
  report: ValidationReport;
  affected_tasks: string[];
}

export interface NodeToggleResponse {
  workflow_id: string;
  node_id: string;
  enabling: boolean;
  mode: string;
  /** enabled 已翻转并产生新修订。排水模式下，在途阶段跑完前为 False。 */
  applied: boolean;
  awaiting_drain: boolean;
  new_revision_seq: number | null;
  affected_tasks: string[];
  drained_stages: string[];
  withdrawn_stages: string[];
  revived_stages: string[];
  added_edges: [string, string][];
  removed_edges: [string, string][];
  warnings: string[];
}

export type ToggleMode = 'drain' | 'immediate';

export interface ReorderResult {
  node_id: string;
  applied: boolean;
  /** **实际生效**的顺序。与请求不符时显示这个，而不是乐观假设（AC-03）。 */
  effective_order: string[];
  rejected: { [k: string]: string }[];
  reason: string | null;
}

/**
 * 会话台账的一条记录（`GET /api/sessions`，只读）。
 *
 * 排障时最常问的两个问题都由它回答：这次任务还连着哪个 session、那个 session 还活着吗。
 * `state` 是台账里的自由文本列（`session_handle.state`），代码里出现过
 * alive / lost / disposed / ended；**不认识的取值原样显示英文**，不猜含义。
 */
export interface SessionRecord {
  session_ref: string;
  harness_id: string;
  owner_task_id: string | null;
  owner_stage_id: string | null;
  owner_attempt_id: string | null;
  state: string;
  created_at: string | null;
  /** 最近一次心跳。与当前时间的差距是判断「是不是卡住了」的依据。 */
  last_heartbeat: string | null;
}

/**
 * `GET /api/sessions/{ref}/attach`：接入一个正在运行的会话。
 *
 * `readable` 与 `writable` 分开，是因为它们对应不同的处置：有些 harness 的输入
 * 在创建会话时就已给定，运行中无法再注入——那是「能看不能发」，不是「不可用」。
 */
export interface SessionAttach {
  attachable: boolean;
  /** 能看输出。 */
  readable: boolean;
  /** 能往里发消息。 */
  writable: boolean;
  /** 能打断当前这一轮。 */
  interruptible: boolean;
  /** 不可用的原因。可用时为 null。 */
  reason: string | null;
  /**
   * 已累积的输出。**只包含本次内核进程内产出的部分**——它随尝试结束而消失。
   * 它是「看现在跑到哪了」的窗口，不是历史。
   */
  output: string;
  total_chars: number;
  /** 输出超过缓冲上限，这里只给到了前面一段。 */
  truncated: boolean;
  harness_id: string;
}

/** `POST /api/sessions/{ref}/input`：向运行中的会话注入一条消息。 */
export interface SessionInputResult {
  delivered: boolean;
  reason: string | null;
}

/** `GET /api/nodes/{node_id}/queue`（RUN-04）：只读投影，权威队列在任务／阶段表里。 */
export interface NodeQueueResponse {
  node_id: string;
  /** 等待依赖与就绪的阶段，**按实际派发次序**排列（与调序端的 effective_order 同源）。 */
  pending: TaskStage[];
  /** 占用执行槽的阶段（D-03）。等待审批的阶段**不在此列**——它不占槽。 */
  running: TaskStage[];
  /** 回看用的最近记录。 */
  history: TaskStage[];
}

// ===========================================================================
// 提交
// ===========================================================================

export interface SubmitResult {
  accepted: boolean;
  task_id: string | null;
  /** false 表示命中幂等键，返回的是**已存在**的那次提交。 */
  created: boolean | null;
  report: ValidationReport | null;
}

export interface TaskDetail {
  task: Task;
  stages: TaskStage[];
  /** 产物列表。summary_ok=false 必须显著标红——那是交接失败。 */
  artifacts: ArtifactRecord[];
  attempts: Attempt[];
  approvals: Approval[];
}

/**
 * 产物记录。
 *
 * 注意：任务详情端点（`TaskDetailResponse.artifacts`）**只投影一部分字段**——
 * `artifact_id / kind / summary / summary_ok / covered_fields / size_bytes /
 * sensitivity / producer`（见 `workerbee/app.py` 的 `task_detail`）。其余字段在
 * 领域对象 `Artifact` 上存在、但不会出现在这个响应里，所以标成可选：
 * 界面读到 undefined 就显示「未知」，**不要**当成 0 或 false。
 */
export interface ArtifactRecord {
  artifact_id: string;
  producer: {
    task_id: string;
    stage_id: string;
    attempt_seq: number;
    node_id: string | null;
    attempt_id: string | null;
  } | null;
  kind: ArtifactKind;
  summary: string | null;
  /** 摘要是否覆盖了边输出契约的必填要点。false 时下游必须显式受阻。 */
  summary_ok: boolean;
  covered_fields: string[];
  size_bytes: number | null;
  sensitivity: Sensitivity;
  // ---- 以下字段不在任务详情的投影里（领域对象有，响应里未必有） ----
  digest?: string;
  token_estimate?: number | null;
  media_type?: string | null;
  lineage?: string[];
  ref_count?: number;
  tombstoned?: boolean;
  storage_path?: string | null;
  created_at?: string;
  updated_at?: string;
}

// ===========================================================================
// 冲突（409，D-02）
// ===========================================================================

export interface ConflictResponse {
  detail: string;
  workflow_id: string;
  latest_revision_seq: number;
  latest_revision: WorkflowRevision | null;
  hint: string | null;
}

export interface ProbeResult {
  harness_id: string;
  ok: boolean;
  capabilities: Record<string, unknown> | null;
  error: string | null;
  probed_at: string;
  /** 这次结论来自哪里：probe（适配器实测）或 capabilities（接口声明）。 */
  source: string;
  note: string | null;
}

export interface DeliveryResult {
  approval_id: string;
  delivered: boolean;
  status: string;
  detail: string | null;
}

// ===========================================================================
// WebSocket 推送
// ===========================================================================

export interface WsPush {
  kind: 'state_changed' | 'attention';
  task_id: string | null;
  stage_id: string | null;
  payload: Record<string, unknown>;
}

export interface WsResumeRequest {
  type: 'resume';
  after_event_id: number;
}
