/**
 * 类型守卫与规范化。
 *
 * 后端响应是 extra="ignore" 的宽容模型：多余字段忽略，但**声明过的字段可能缺失**
 * （例如早期版本的内核、或某个字段为 null）。这里集中处理「从 unknown 到具体视图」
 * 的读取，避免在 JSX 里散落 `any` 与 `!`。
 */

import type {
  Approval,
  Diagnostic,
  ExecutionProfile,
  GraphSpec,
  NodeDefinition,
  Severity,
  TaskState,
  StageState,
  ValidationReport,
} from './types';
import { STAGE_STATES, TASK_STATES } from './types';

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

export function asNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

export function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/** 未知状态一律回落到一个显式值，绝不猜成 "running"（OBS-01）。 */
export function asTaskState(value: unknown): TaskState {
  return TASK_STATES.includes(value as TaskState) ? (value as TaskState) : 'blocked';
}

export function asStageState(value: unknown): StageState {
  return STAGE_STATES.includes(value as StageState) ? (value as StageState) : 'blocked';
}

export function asSeverity(value: unknown): Severity {
  return value === 'error' || value === 'warning' || value === 'info' ? value : 'info';
}

/** 后端可能把 tuple 序列化成数组——统一成 [from, to]。 */
export function asEdgePair(value: unknown): [string, string] | null {
  if (!Array.isArray(value) || value.length < 2) return null;
  const [a, b] = value;
  if (typeof a !== 'string' || typeof b !== 'string') return null;
  return [a, b];
}

export function isValidationReport(value: unknown): value is ValidationReport {
  if (!isRecord(value)) return false;
  const mode = value['mode'];
  const diags = value['diagnostics'];
  return (
    (mode === 'draft' || mode === 'publish' || mode === 'launch') &&
    Array.isArray(diags) &&
    diags.every((d) => isRecord(d) && typeof d['code'] === 'string' && typeof d['message'] === 'string')
  );
}

export function normalizeDiagnostic(raw: unknown): Diagnostic | null {
  if (!isRecord(raw)) return null;
  const code = asString(raw['code']);
  const message = asString(raw['message']);
  if (!code && !message) return null;
  return {
    code: code || 'unknown',
    severity: asSeverity(raw['severity']),
    message,
    node_id: typeof raw['node_id'] === 'string' ? raw['node_id'] : null,
    node_name: typeof raw['node_name'] === 'string' ? raw['node_name'] : null,
    edge: asEdgePair(raw['edge']),
    profile_id: typeof raw['profile_id'] === 'string' ? raw['profile_id'] : null,
    slot: typeof raw['slot'] === 'string' ? raw['slot'] : null,
    hint: typeof raw['hint'] === 'string' ? raw['hint'] : null,
    requirement: typeof raw['requirement'] === 'string' ? raw['requirement'] : null,
    fix_action: typeof raw['fix_action'] === 'string' ? raw['fix_action'] : null,
  };
}

export function normalizeReport(raw: unknown): ValidationReport | null {
  if (!isValidationReport(raw)) return null;
  const diagnostics = raw.diagnostics
    .map(normalizeDiagnostic)
    .filter((d): d is Diagnostic => d !== null);
  return { mode: raw.mode, diagnostics };
}

/** 空图：新建 Workflow 且尚无修订时的合法初值。 */
export function emptyGraph(): GraphSpec {
  return { nodes: [], edges: [], waivers: [] };
}

export function emptyGraphSpec(raw: unknown): GraphSpec {
  if (!isRecord(raw)) return emptyGraph();
  return {
    nodes: asArray<NodeDefinition>(raw['nodes']),
    edges: asArray(raw['edges']),
    waivers: asArray(raw['waivers']),
  };
}

export function defaultRetryPolicy(): ExecutionProfile['retry'] {
  return {
    max_attempts: 3,
    backoff_base_ms: 1000,
    backoff_cap_ms: 60000,
    retryable_errors: ['network', 'rate_limit', 'server_error'],
    jitter_ratio: 0.2,
  };
}

/** 新节点 / 新候选的本地默认值——只用于表单初值，服务端仍是权威。 */
export function newLocalId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `local-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

/** 审批是否仍在等待处理（OBS-05 入口的判据）。 */
export function approvalNeedsAction(approval: Approval): boolean {
  return approval.status === 'pending' || approval.status === 'undeliverable';
}

/** 阶段是否占用执行槽（D-03）。分派中与运行中占槽，等待审批/退避/暂停不占槽。 */
export function occupiesSlot(state: StageState): boolean {
  return state === 'dispatching' || state === 'running';
}
