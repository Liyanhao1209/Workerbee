/**
 * 端点封装。路径与 `workerbee/server` 的路由一一对应（路径已冻结，前端不改写）。
 *
 * 这里只做「拼路径 + 打类型」，不做业务判断：状态码语义（409/422）由
 * `client.ApiError` 承载，调用方按 kind 分支。
 */

import { ApiError, request } from './client';
import { isRecord } from './guards';
import type {
  Approval,
  AttentionResponse,
  CredentialKind,
  CredentialRef,
  DeleteTaskResponse,
  DeliveryResult,
  EventPage,
  GraphSpec,
  HarnessRegistration,
  HealthResponse,
  NodeDefinition,
  NodeQueueResponse,
  NodeToggleResponse,
  PauseResponse,
  ProbeResult,
  PruneResult,
  ReorderResult,
  ArtifactContent,
  AttemptWork,
  ResumeResponse,
  RevisionSaveResult,
  RevisionSource,
  SessionRecord,
  SessionAttach,
  SessionInputResult,
  SkillDoc,
  SkillScope,
  StageResumeResponse,
  StorageReport,
  SubmitResult,
  SystemStatus,
  Task,
  TaskDetail,
  TaskState,
  Template,
  TemplateInstantiateResult,
  ToggleMode,
  TogglePreview,
  ToolSpec,
  ValidationMode,
  ValidationReport,
  WorkflowDefinition,
  WorkflowDeleteResponse,
  WorkflowRevision,
  WorkflowStatus,
} from './types';

// ---------------------------------------------------------------------------
// 系统
// ---------------------------------------------------------------------------

export const system = {
  health: (signal?: AbortSignal) => request<HealthResponse>('/api/health', { noAuth: true, signal }),
  status: () => request<SystemStatus>('/api/system/status'),
  /** 全局事件流。按任务看事件用 `/api/tasks/{task_id}/events`（后者支持 task_id 维度）。 */
  events: (params: { after_id?: number; limit?: number } = {}) =>
    request<EventPage>('/api/system/events', { query: params }),
  attention: () => request<AttentionResponse>('/api/attention'),
  storage: () => request<StorageReport>('/api/storage'),
  prune: (body: {
    dry_run: boolean;
    task_id?: string | null;
    keep_last?: number;
    older_than?: string | null;
    include_orphans?: boolean;
    include_artifacts?: boolean;
    include_unreferenced_artifacts?: boolean;
  }) => request<PruneResult>('/api/storage/prune', { method: 'POST', body }),
};

// ---------------------------------------------------------------------------
// Workflow
// ---------------------------------------------------------------------------

export const workflows = {
  list: () => request<{ workflows: WorkflowDefinition[]; returned: number }>('/api/workflows'),

  create: (body: { name: string; description?: string | null; max_concurrent_tasks?: number }) =>
    request<WorkflowDefinition>('/api/workflows', { method: 'POST', body }),

  get: (id: string) => request<WorkflowDefinition>(`/api/workflows/${encodeURIComponent(id)}`),

  patch: (
    id: string,
    body: {
      name?: string;
      description?: string | null;
      max_concurrent_tasks?: number;
      status?: WorkflowStatus;
    },
  ) => request<WorkflowDefinition>(`/api/workflows/${encodeURIComponent(id)}`, { method: 'PATCH', body }),

  remove: (id: string) =>
    request<WorkflowDeleteResponse>(`/api/workflows/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  revisions: (id: string) =>
    request<{ workflow_id: string; revisions: WorkflowRevision[] }>(
      `/api/workflows/${encodeURIComponent(id)}/revisions`,
    ),

  revision: (id: string, seq: number) =>
    request<WorkflowRevision>(`/api/workflows/${encodeURIComponent(id)}/revisions/${seq}`),

  /** 保存修订。带 base_revision_seq 做乐观并发；冲突时抛 kind='conflict' 的 ApiError。 */
  saveRevision: (
    id: string,
    body: {
      graph: GraphSpec;
      publish: boolean;
      base_revision_seq: number | null;
      note?: string | null;
      source?: RevisionSource;
    },
  ) =>
    request<RevisionSaveResult>(`/api/workflows/${encodeURIComponent(id)}/revisions`, {
      method: 'POST',
      body,
    }),

  validate: (id: string, body: { graph?: GraphSpec | null; mode: ValidationMode }) =>
    request<ValidationReport>(`/api/workflows/${encodeURIComponent(id)}/validate`, {
      method: 'POST',
      body,
    }),

  /** 启停预览（ACT-02）。**不改变任何状态。** */
  togglePreview: (id: string, nodeId: string) =>
    request<TogglePreview>(
      `/api/workflows/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nodeId)}/toggle`,
      { query: { preview: true } },
    ),

  /** 真正执行启停。 */
  toggle: (id: string, nodeId: string, body: { enable: boolean; mode: ToggleMode }) =>
    request<NodeToggleResponse>(
      `/api/workflows/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nodeId)}/toggle`,
      { query: { preview: false }, method: 'POST', body },
    ),

  /** 某节点的队列投影（RUN-04）：pending / running / history。 */
  nodeQueue: (nodeId: string) =>
    request<NodeQueueResponse>(`/api/nodes/${encodeURIComponent(nodeId)}/queue`),

  reorder: (nodeId: string, stageIds: string[]) =>
    request<ReorderResult>(`/api/nodes/${encodeURIComponent(nodeId)}/reorder`, {
      method: 'POST',
      body: { stage_ids: stageIds },
    }),
};

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------

export const tasks = {
  submit: (
    workflowId: string,
    body: { input_payload: Record<string, unknown>; idempotency_key?: string | null; priority: number },
  ) =>
    request<SubmitResult>(`/api/workflows/${encodeURIComponent(workflowId)}/tasks`, {
      method: 'POST',
      body,
    }),

  list: (params: { workflow_id?: string; state?: TaskState } = {}) =>
    request<{ tasks: Task[]; returned: number; limit: number; offset: number; has_more: boolean }>(
      '/api/tasks',
      { query: params },
    ),

  get: (taskId: string) => request<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`),

  /** 单次执行尝试的工作细节：输入、推理、工具调用、涉及的文件。 */
  attemptWork: (taskId: string, attemptId: string) =>
    request<AttemptWork>(
      `/api/tasks/${encodeURIComponent(taskId)}/attempts/${encodeURIComponent(attemptId)}/work`,
    ),

  /** 产物正文（有界截断，已脱敏）。 */
  artifactContent: (taskId: string, artifactId: string) =>
    request<ArtifactContent>(
      `/api/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}/content`,
    ),
  pause: (taskId: string, body: { from_node_id?: string | null; reason?: string | null } = {}) =>
    request<PauseResponse>(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: 'POST', body }),

  resume: (taskId: string, body: { restart_failed?: boolean } = {}) =>
    request<ResumeResponse>(`/api/tasks/${encodeURIComponent(taskId)}/resume`, {
      method: 'POST',
      body,
    }),

  remove: (taskId: string, body: { from_node_id?: string | null; reason?: string | null } = {}) =>
    request<DeleteTaskResponse>(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: 'DELETE',
      body,
    }),

  /** 断点续跑某阶段（§11.3）。 */
  resumeStage: (taskId: string, nodeId: string) =>
    request<StageResumeResponse>(
      `/api/tasks/${encodeURIComponent(taskId)}/stages/${encodeURIComponent(nodeId)}/resume`,
      { method: 'POST' },
    ),

  events: (taskId: string, params: { after_id?: number; limit?: number } = {}) =>
    request<EventPage>(`/api/tasks/${encodeURIComponent(taskId)}/events`, { query: params }),
};

// ---------------------------------------------------------------------------
// 会话台账（REC-03）
// ---------------------------------------------------------------------------

export const sessions = {
  /**
   * supervisor / 内核持有的 harness 会话（只读）。
   *
   * 不带查询参数地取全量，由界面自己筛选：会话量级很小（并发任务数级别），
   * 而「按任务看它的会话」是排障主路径，客户端筛选能保证切筛选不重新请求。
   */
  list: () => listRequest<SessionRecord>('/api/sessions', 'sessions'),

  /**
   * 接入一个正在运行的会话，取它的输出与可做的操作。
   *
   * 只对**此刻在运行**的会话有效。已结束的尝试会返回 `attachable: false` 与
   * 一句说明，指向任务详情页——那里才是持久化的历史。
   */
  attach: (sessionRef: string) =>
    request<SessionAttach>(`/api/sessions/${encodeURIComponent(sessionRef)}/attach`),

  /**
   * 向运行中的会话注入一条消息。
   *
   * 只投**首轮之后**的消息：首轮输入若已在建会话时交付，重复投递会让同一条
   * 指令执行两遍，对会改文件的 agent 是数据损坏。
   */
  sendInput: (sessionRef: string, text: string) =>
    request<SessionInputResult>(`/api/sessions/${encodeURIComponent(sessionRef)}/input`, {
      method: 'POST',
      body: { text },
    }),
};

// ---------------------------------------------------------------------------
// 注册表
// ---------------------------------------------------------------------------

/**
 * 列表端点统一成数组：内核的列表路由直接返回 `[...]`（如 `/api/harnesses`），
 * 这里也接受 `{<key>: [...]}` / `{items: [...]}` 的信封写法。三种都不是就抛错——
 * **不把没读懂的响应当空列表**（那会把「读不到」画成「本来就是空的」）。
 */
async function listRequest<T>(path: string, key: string): Promise<T[]> {
  const body = await request<unknown>(path);
  if (Array.isArray(body)) return body as T[];
  if (isRecord(body)) {
    const direct = body[key];
    if (Array.isArray(direct)) return direct as T[];
    const items = body['items'];
    if (Array.isArray(items)) return items as T[];
  }
  throw new ApiError({
    kind: 'parse',
    status: 200,
    detail: '后台服务返回的数据格式无法识别',
    hint: '通常是前后端版本不一致；请把后台服务和页面都更新到同一版本后重试',
  });
}

// 注册表四类资源的路径已唯一化：/api/harnesses、/api/credentials、/api/skills、/api/tools
// （早期用过的 /api/registry/* 前缀已废弃，客户端不保留回退——两套并存本身就是困惑源）。
export const registry = {
  harnesses: () => listRequest<HarnessRegistration>('/api/harnesses', 'harnesses'),

  createHarness: (body: {
    name: string;
    adapter_id: string;
    adapter_version?: string | null;
    exec_path?: string | null;
    env_template?: Record<string, string>;
    cwd?: string | null;
    auth_binding?: string | null;
    auth_mode?: string;
    enabled?: boolean;
  }) => request<HarnessRegistration>('/api/harnesses', { method: 'POST', body }),

  patchHarness: (
    id: string,
    body: {
      name?: string;
      adapter_id?: string;
      adapter_version?: string | null;
      exec_path?: string | null;
      env_template?: Record<string, string>;
      cwd?: string | null;
      auth_binding?: string | null;
      auth_mode?: string;
      enabled?: boolean;
    },
  ) =>
    request<HarnessRegistration>(`/api/harnesses/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body,
    }),

  /** 能力探测（HAR-02）。失败如实上报，不伪造 capabilities。 */
  probeHarness: (id: string) =>
    request<ProbeResult>(`/api/harnesses/${encodeURIComponent(id)}/probe`, { method: 'POST' }),

  credentials: () => listRequest<CredentialRef>('/api/credentials', 'credentials'),

  createCredential: (body: {
    label: string;
    kind: CredentialKind;
    secret_locator?: string | null;
    base_url?: string | null;
    /** 密钥本体（如 { api_key: 'sk-...' }）。只在请求体出现一次，响应不回显。 */
    secret?: Record<string, string> | null;
  }) => request<CredentialRef>('/api/credentials', { method: 'POST', body }),

  revokeCredential: (id: string, revoked: boolean) =>
    request<CredentialRef>(`/api/credentials/${encodeURIComponent(id)}/revoke`, {
      method: 'POST',
      body: { revoked },
    }),

  skills: () => listRequest<SkillDoc>('/api/skills', 'skills'),

  createSkill: (body: {
    name: string;
    content?: string;
    version?: number;
    scope?: SkillScope;
    enabled?: boolean;
  }) => request<SkillDoc>('/api/skills', { method: 'POST', body }),

  tools: () => listRequest<ToolSpec>('/api/tools', 'tools'),

  createTool: (body: {
    name: string;
    launch?: Record<string, unknown>;
    description?: string | null;
    io_schema?: Record<string, unknown>;
    risk_level?: string;
    approval_policy?: string;
    version?: number;
    health_check?: boolean;
    enabled?: boolean;
  }) => request<ToolSpec>('/api/tools', { method: 'POST', body }),
};

// ---------------------------------------------------------------------------
// 模板
// ---------------------------------------------------------------------------

export const templates = {
  list: () => request<{ templates: Template[]; returned: number }>('/api/templates'),

  create: (body: {
    name: string;
    description?: string | null;
    kind?: 'workflow' | 'node';
    from_workflow_id?: string | null;
    from_revision_seq?: number | null;
    /** 直接给载荷时，凭据必须已剥离为 sensitive_slots 占位。 */
    payload?: {
      nodes: unknown[];
      edges: unknown[];
      sensitive_slots: { slot: string; original_label?: string | null; original_kind?: string | null }[];
    } | null;
  }) => request<Template>('/api/templates', { method: 'POST', body }),

  get: (id: string) => request<Template>(`/api/templates/${encodeURIComponent(id)}`),

  remove: (id: string) => request<unknown>(`/api/templates/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 实例化。`report.missing_bindings` 非空时**不编造凭据**，要求用户补齐。 */
  instantiate: (id: string, body: { bindings?: Record<string, string>; node_id_map?: Record<string, string> }) =>
    request<TemplateInstantiateResult>(`/api/templates/${encodeURIComponent(id)}/instantiate`, {
      method: 'POST',
      body,
    }),
};

// ---------------------------------------------------------------------------
// 审批
// ---------------------------------------------------------------------------

export const approvals = {
  list: () => request<{ approvals: Approval[]; returned: number }>('/api/approvals'),

  decide: (id: string, body: { approve: boolean; modified_action?: string | null; note?: string | null }) =>
    request<DeliveryResult>(`/api/approvals/${encodeURIComponent(id)}/decide`, { method: 'POST', body }),

  /** 回注失败（undeliverable）后的重试入口（HUM-04）。 */
  retryDelivery: (id: string) =>
    request<DeliveryResult>(`/api/approvals/${encodeURIComponent(id)}/retry-delivery`, { method: 'POST' }),
};

/** 供页面复用：从节点列表里取名字，用于诊断与预览的定位文案。 */
export function nodeLabel(nodes: NodeDefinition[], nodeId: string | null | undefined): string {
  if (!nodeId) return '（未知节点）';
  const node = nodes.find((n) => n.node_id === nodeId);
  return node ? node.name : `节点 ${nodeId.slice(0, 8)}`;
}
