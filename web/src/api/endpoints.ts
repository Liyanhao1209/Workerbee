/**
 * 端点封装。路径与 `workerbee/server` 的路由一一对应（路径已冻结，前端不改写）。
 *
 * 这里只做「拼路径 + 打类型」，不做业务判断：状态码语义（409/422）由
 * `client.ApiError` 承载，调用方按 kind 分支。
 */

import { request } from './client';
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
  NodeToggleResponse,
  PauseResponse,
  ProbeResult,
  PruneResult,
  ReorderResult,
  ResumeResponse,
  RevisionSource,
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
  events: (params: { after_id?: number; limit?: number; task_id?: string } = {}) =>
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
    request<{
      accepted: boolean;
      workflow_id: string;
      revision_seq: number;
      published: boolean;
      effective_graph_version: number;
      note: string | null;
      cas_checked: boolean;
      base_revision_seq: number | null;
    }>(`/api/workflows/${encodeURIComponent(id)}/revisions`, { method: 'POST', body }),

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

  /** 某节点的 pending 队列（RUN-04）。 */
  nodeQueue: (nodeId: string) =>
    request<{ node_id: string; stages: unknown[] }>(`/api/nodes/${encodeURIComponent(nodeId)}/queue`),

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
// 注册表
// ---------------------------------------------------------------------------

export const registry = {
  harnesses: () => request<{ harnesses: HarnessRegistration[]; returned: number }>('/api/registry/harnesses'),

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
  }) => request<HarnessRegistration>('/api/registry/harnesses', { method: 'POST', body }),

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
    request<HarnessRegistration>(`/api/registry/harnesses/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body,
    }),

  /** 能力探测（HAR-02）。失败如实上报，不伪造 capabilities。 */
  probeHarness: (id: string) =>
    request<ProbeResult>(`/api/registry/harnesses/${encodeURIComponent(id)}/probe`, { method: 'POST' }),

  credentials: () => request<{ credentials: CredentialRef[]; returned: number }>('/api/registry/credentials'),

  createCredential: (body: {
    label: string;
    kind: CredentialKind;
    secret_locator?: string | null;
    base_url?: string | null;
  }) => request<CredentialRef>('/api/registry/credentials', { method: 'POST', body }),

  revokeCredential: (id: string, revoked: boolean) =>
    request<CredentialRef>(`/api/registry/credentials/${encodeURIComponent(id)}/revoke`, {
      method: 'POST',
      body: { revoked },
    }),

  skills: () => request<{ skills: SkillDoc[]; returned: number }>('/api/registry/skills'),

  createSkill: (body: {
    name: string;
    content?: string;
    version?: number;
    scope?: SkillScope;
    enabled?: boolean;
  }) => request<SkillDoc>('/api/registry/skills', { method: 'POST', body }),

  tools: () => request<{ tools: ToolSpec[]; returned: number }>('/api/registry/tools'),

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
  }) => request<ToolSpec>('/api/registry/tools', { method: 'POST', body }),
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
