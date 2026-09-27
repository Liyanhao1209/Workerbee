/**
 * 后端枚举 → 中文文案 + 视觉分级。
 *
 * 纪律（OBS-01）：**状态文案必须与后端枚举一一对应**，不新增、不合并、
 * 不用「loading」这类后端没有的状态。值本身（英文枚举）始终可查——它是
 * 排查问题时与后端日志对齐的锚点。
 */

import type {
  ApprovalStatus,
  ArtifactKind,
  ContractFormat,
  CredentialKind,
  ErrorClass,
  DesiredState,
  RevisionSource,
  RiskLevel,
  Sensitivity,
  Severity,
  SkillScope,
  StageState,
  TaskState,
  TemplateKind,
  WorkflowStatus,
} from './api/types';

/** pill 的视觉分级。 */
export type Tone =
  | 'idle'
  | 'pending'
  | 'ready'
  | 'running'
  | 'success'
  | 'warn'
  | 'danger'
  | 'blocked'
  | 'lost'
  | 'skipped'
  | 'approval'
  | 'transition';

export interface StateLabel {
  /** 中文文案。 */
  text: string;
  tone: Tone;
  /** 是否是「控制操作尚在处理中」的过渡态（OBS-01 硬要求）。 */
  transitioning: boolean;
  /** 是否是终态。 */
  terminal: boolean;
  hint?: string;
}

const TASK_LABELS: Record<TaskState, StateLabel> = {
  queued: { text: '排队中', tone: 'pending', transitioning: false, terminal: false, hint: '已接受提交，等待调度领取' },
  running: { text: '运行中', tone: 'running', transitioning: false, terminal: false },
  pausing: {
    text: '暂停中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '已接受暂停意图，正在收敛在途执行',
  },
  paused: { text: '已暂停', tone: 'idle', transitioning: false, terminal: false, hint: '不再派发新阶段，可恢复' },
  cancelling: {
    text: '取消中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '已接受删除意图，正在停止并清理',
  },
  cancelled: { text: '已删除', tone: 'skipped', transitioning: false, terminal: true, hint: '已终止且无续跑入口' },
  succeeded: { text: '已成功', tone: 'success', transitioning: false, terminal: true },
  failed: { text: '已失败', tone: 'danger', transitioning: false, terminal: true },
  blocked: { text: '受阻', tone: 'blocked', transitioning: false, terminal: false, hint: '依赖未满足或输入不足' },
  reconciling: {
    text: '核对中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '系统无法确认真实状态，正在对账（不是运行中）',
  },
};

const STAGE_LABELS: Record<StageState, StateLabel> = {
  waiting_deps: { text: '等待依赖', tone: 'idle', transitioning: false, terminal: false },
  ready: { text: '就绪排队', tone: 'ready', transitioning: false, terminal: false, hint: '依赖已满足，等待节点槽位' },
  dispatching: { text: '分派中', tone: 'transition', transitioning: true, terminal: false, hint: '已占执行槽，正在创建会话' },
  running: { text: '运行中', tone: 'running', transitioning: false, terminal: false },
  awaiting_approval: {
    text: '等待审批',
    tone: 'approval',
    transitioning: true,
    terminal: false,
    hint: '不占用执行槽，同节点后续任务可继续',
  },
  pausing: { text: '暂停中', tone: 'transition', transitioning: true, terminal: false },
  paused: { text: '已暂停', tone: 'idle', transitioning: false, terminal: false },
  retrying: { text: '退避重试中', tone: 'transition', transitioning: true, terminal: false, hint: '不占用执行槽' },
  succeeded: { text: '已成功', tone: 'success', transitioning: false, terminal: true },
  failed: { text: '已失败', tone: 'danger', transitioning: false, terminal: true, hint: '可显式恢复或重新启用' },
  skipped: { text: '已跳过', tone: 'skipped', transitioning: false, terminal: true, hint: '节点停用撤回或显式跳过' },
  cancelled: { text: '已取消', tone: 'skipped', transitioning: false, terminal: true },
  blocked: { text: '受阻', tone: 'blocked', transitioning: false, terminal: false, hint: '上游失败／被删除／输入不足' },
  lost: {
    text: '状态不明',
    tone: 'lost',
    transitioning: true,
    terminal: false,
    hint: '无法确认真实状态，需人工核对，不得盲目重放',
  },
  reconciling: { text: '核对中', tone: 'transition', transitioning: true, terminal: false },
};

export function taskStateLabel(state: TaskState): StateLabel {
  return TASK_LABELS[state];
}
export function stageStateLabel(state: StageState): StateLabel {
  return STAGE_LABELS[state];
}

/** Task 状态的完整中文名清单，供筛选器使用。顺序与后端枚举一致。 */
export const TASK_STATE_OPTIONS: { value: TaskState; text: string }[] = (
  Object.keys(TASK_LABELS) as TaskState[]
).map((value) => ({ value, text: TASK_LABELS[value].text }));

/** Stage 状态清单，供筛选与图例使用。 */
export const STAGE_STATE_OPTIONS: { value: StageState; text: string }[] = (
  Object.keys(STAGE_LABELS) as StageState[]
).map((value) => ({ value, text: STAGE_LABELS[value].text }));

/**
 * React Flow 节点着色用的状态 → 颜色映射。
 * 与 `.pill--*` 的色相一致；过渡态用虚线边框额外区分。
 */
export const STAGE_TONE_VAR: Record<StageState, string> = {
  waiting_deps: 'var(--st-idle)',
  ready: 'var(--st-ready)',
  dispatching: 'var(--st-transition)',
  running: 'var(--st-running)',
  awaiting_approval: 'var(--st-approval)',
  pausing: 'var(--st-transition)',
  paused: 'var(--st-idle)',
  retrying: 'var(--st-transition)',
  succeeded: 'var(--st-success)',
  failed: 'var(--st-danger)',
  skipped: 'var(--st-skipped)',
  cancelled: 'var(--st-skipped)',
  blocked: 'var(--st-blocked)',
  lost: 'var(--st-lost)',
  reconciling: 'var(--st-transition)',
};

export const DESIRED_LABELS: Record<DesiredState, string> = {
  active: '运行或继续运行',
  paused: '用户已暂停',
  cancelled: '用户已删除（不复活）',
};

export const APPROVAL_LABELS: Record<ApprovalStatus, { text: string; tone: Tone; action: boolean }> = {
  pending: { text: '待处理', tone: 'approval', action: true },
  approved: { text: '已批准', tone: 'success', action: false },
  denied: { text: '已拒绝', tone: 'danger', action: false },
  expired: { text: '已超时', tone: 'warn', action: false },
  undeliverable: { text: '回注失败', tone: 'danger', action: true },
  superseded: { text: '已作废', tone: 'skipped', action: false },
};

export const WORKFLOW_STATUS_LABELS: Record<WorkflowStatus, { text: string; tone: Tone }> = {
  draft: { text: '草稿', tone: 'pending' },
  published: { text: '已发布', tone: 'success' },
  archived: { text: '已归档', tone: 'idle' },
  deleted: { text: '已删除', tone: 'skipped' },
};

export const REVISION_SOURCE_LABELS: Record<RevisionSource, string> = {
  manual: '手动建图',
  ai_generated: 'AI 生成',
  graph_capture: 'Graph Capture',
  template: '模板实例化',
};

export const ERROR_CLASS_LABELS: Record<ErrorClass, { text: string; tone: Tone }> = {
  success: { text: '成功', tone: 'success' },
  retryable_error: { text: '可重试错误', tone: 'warn' },
  fatal_error: { text: '不可重试错误', tone: 'danger' },
  user_cancelled: { text: '用户取消', tone: 'skipped' },
};

export const SEVERITY_LABELS: Record<Severity, { text: string; tone: Tone }> = {
  error: { text: '错误', tone: 'danger' },
  warning: { text: '警告', tone: 'warn' },
  info: { text: '说明', tone: 'idle' },
};

export const CREDENTIAL_KIND_LABELS: Record<CredentialKind, string> = {
  api_key: 'API Key',
  oauth: 'OAuth',
  base_url_pair: 'Base URL + Key',
  harness_login: 'Harness 本机登录态',
};

export const AUTH_MODE_LABELS: Record<string, string> = {
  api_key: 'API Key',
  oauth: 'OAuth',
  base_url_pair: 'Base URL + Key',
  native_login: '本机登录态',
};

export const RISK_LEVEL_LABELS: Record<RiskLevel, { text: string; tone: Tone }> = {
  low: { text: '低风险', tone: 'idle' },
  medium: { text: '中风险', tone: 'warn' },
  high: { text: '高风险', tone: 'danger' },
};

export const APPROVAL_POLICY_LABELS: Record<string, string> = {
  auto: '自动放行',
  ask: '需审批',
  deny: '禁止',
};

export const SKILL_SCOPE_LABELS: Record<SkillScope, string> = {
  global: '工具库（可复用）',
  node_local: '节点级临时',
};

export const TEMPLATE_KIND_LABELS: Record<TemplateKind, string> = {
  workflow: '流程模板',
  node: '节点模板',
};

export const ARTIFACT_KIND_LABELS: Record<ArtifactKind, string> = {
  text: '文本',
  file: '文件',
  code: '代码',
  structured: '结构化',
};

export const SENSITIVITY_LABELS: Record<Sensitivity, { text: string; tone: Tone }> = {
  public: { text: '公开', tone: 'idle' },
  internal: { text: '内部', tone: 'pending' },
  sensitive: { text: '敏感', tone: 'warn' },
};

export const CONTRACT_FORMAT_LABELS: Record<ContractFormat, string> = {
  text: '文本',
  markdown: 'Markdown',
  json: 'JSON',
  code: '代码',
  file: '文件',
  any: '未指定',
};

/** 适配器能力的中文名（HAR-02 能力矩阵表头）。 */
export const CAPABILITY_LABELS: Record<string, string> = {
  create_session: '创建会话',
  resume_session: '恢复会话',
  read_output: '读取输出',
  interact: '运行中交互',
  interrupt: '打断',
  stop: '停止',
  compact: '上下文整理',
  permission_hook: '审批钩子',
  background_tasks: '后台工作',
};

/** 生命周期操作来源的中文名。 */
export function originOpLabel(op: string): string {
  const map: Record<string, string> = {
    pause: '暂停',
    resume: '恢复',
    delete: '删除',
    disable: '停用节点',
    enable: '启用节点',
    requeue: '重新入队',
    reorder: '调序',
  };
  return map[op] ?? op;
}
