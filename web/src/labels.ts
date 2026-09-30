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
  queued: { text: '排队中', tone: 'pending', transitioning: false, terminal: false, hint: '已提交，等待调度' },
  running: { text: '运行中', tone: 'running', transitioning: false, terminal: false },
  pausing: {
    text: '暂停中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '正在停止进行中的阶段',
  },
  paused: { text: '已暂停', tone: 'idle', transitioning: false, terminal: false, hint: '不再派发新阶段，可恢复' },
  cancelling: {
    text: '取消中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '正在停止并清理',
  },
  cancelled: { text: '已删除', tone: 'skipped', transitioning: false, terminal: true, hint: '已删除，无法恢复' },
  succeeded: { text: '已成功', tone: 'success', transitioning: false, terminal: true },
  failed: { text: '已失败', tone: 'danger', transitioning: false, terminal: true },
  blocked: { text: '受阻', tone: 'blocked', transitioning: false, terminal: false, hint: '依赖未满足或输入不足' },
  reconciling: {
    text: '核对中',
    tone: 'transition',
    transitioning: true,
    terminal: false,
    hint: '正在核对实际状态，此时不能确定任务是否还在跑',
  },
};

const STAGE_LABELS: Record<StageState, StateLabel> = {
  waiting_deps: { text: '等待依赖', tone: 'idle', transitioning: false, terminal: false },
  ready: { text: '就绪排队', tone: 'ready', transitioning: false, terminal: false, hint: '依赖已满足，等待空出的执行位' },
  dispatching: { text: '分派中', tone: 'transition', transitioning: true, terminal: false, hint: '正在创建会话' },
  running: { text: '运行中', tone: 'running', transitioning: false, terminal: false },
  awaiting_approval: {
    text: '等待审批',
    tone: 'approval',
    transitioning: true,
    terminal: false,
    hint: '不占用执行位，同节点的后续任务可以继续',
  },
  pausing: { text: '暂停中', tone: 'transition', transitioning: true, terminal: false },
  paused: { text: '已暂停', tone: 'idle', transitioning: false, terminal: false },
  retrying: { text: '退避重试中', tone: 'transition', transitioning: true, terminal: false, hint: '等待重试，不占用执行位' },
  succeeded: { text: '已成功', tone: 'success', transitioning: false, terminal: true },
  failed: { text: '已失败', tone: 'danger', transitioning: false, terminal: true, hint: '可手动恢复或重新启用' },
  skipped: { text: '已跳过', tone: 'skipped', transitioning: false, terminal: true, hint: '节点被停用或本次被跳过' },
  cancelled: { text: '已取消', tone: 'skipped', transitioning: false, terminal: true },
  blocked: { text: '受阻', tone: 'blocked', transitioning: false, terminal: false, hint: '上游失败、被删除或输入不足' },
  lost: {
    text: '状态不明',
    tone: 'lost',
    transitioning: true,
    terminal: false,
    hint: '无法确认是否还在运行，需要人工核对后再决定怎么办',
  },
  reconciling: { text: '核对中', tone: 'transition', transitioning: true, terminal: false },
};

/**
 * 未知状态的兜底。
 *
 * 状态值来自网络（事件历史里可能是旧版本写入的、或后端将来新增的）。字典查不到
 * 时返回一个**原样显示**的标签，而不是 undefined —— 后者会让调用方取 `.text`
 * 时抛异常，把整页打成白屏。用户看到「queued」这种英文原文是可以接受的，
 * 看到白屏不可接受。
 */
function unknownStateLabel(value: string): StateLabel {
  return {
    text: value,
    tone: 'idle',
    transitioning: false,
    terminal: false,
    hint: '这个状态本界面还不认识，按原文显示',
  };
}

export function taskStateLabel(state: TaskState | string): StateLabel {
  return TASK_LABELS[state as TaskState] ?? unknownStateLabel(String(state));
}
export function stageStateLabel(state: StageState | string): StateLabel {
  return STAGE_LABELS[state as StageState] ?? unknownStateLabel(String(state));
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
  paused: '已暂停',
  cancelled: '已删除，不再运行',
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
  template: '从模板创建',
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
  base_url_pair: 'API Key + 自建服务地址',
  harness_login: '本机登录态',
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

/** 适配器能力的中文名（「支持的功能」表头）。 */
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
  pause_in_place: '原地暂停',
  checkpoint_resume: '断点续跑',
  keep_checkpoint_on_stop: '停止后保留断点',
  token_usage: '上报用量',
  structured_output: '结构化输出',
  reasoning_efforts: '可用 effort',
  models: '已知模型',
  auth_modes: '认证方式',
};

/**
 * 事件类型中文名（`workerbee/data/event_log.py` 的 `EventType`）。
 * 未列出的类型**原样显示英文**，不猜含义。
 */
export const EVENT_TYPE_LABELS: Record<string, string> = {
  'workflow.created': '流程已创建',
  'workflow.updated': '流程已更新',
  'workflow.deleted': '流程已删除',
  'revision.saved': '修订已保存',
  'revision.published': '修订已发布',
  'node.activation_requested': '节点启停已请求',
  'node.activation_applied': '节点启停已生效',
  'validation.failed': '校验未通过',
  'task.submitted': '任务已提交',
  'task.state_changed': '任务状态变化',
  'task.control': '任务控制操作',
  'task.completed': '任务结束',
  'stage.state_changed': '阶段状态变化',
  'stage.dispatched': '阶段已派发',
  'stage.retry': '阶段退避重试',
  'stage.candidate_switched': '阶段切换执行候选',
  'stage.reordered': '阶段调序',
  'stage.contract_violation': '输出契约违反',
  'attempt.started': '尝试开始',
  'attempt.ended': '尝试结束',
  'attempt.compact': '上下文整理',
  'attempt.usage': '用量记录',
  'attempt.discarded_late': '迟到回调已丢弃',
  'context.assembled': '上下文已组装',
  'handoff.failed': '交接失败',
  'data.ready': '数据已就绪',
  'feedback.raised': '反馈已提出',
  'session.created': '会话已创建',
  'session.resumed': '会话已恢复',
  'session.ended': '会话已结束',
  'session.lost': '会话失联',
  'resource.registered': '资源已登记',
  'resource.closed': '资源已关闭',
  'resource.teardown_failed': '资源清理失败',
  'resource.orphaned': '资源归属不明',
  'reaper.run': '清理器运行',
  'approval.requested': '审批已发起',
  'approval.decided': '审批已决定',
  'approval.invalidated': '审批已作废',
  'approval.undeliverable': '审批回注失败',
  'ai.draft_proposed': 'AI 草案已提出',
  'ai.draft_accepted': 'AI 草案已接受',
  'secret.bound': '凭据已绑定',
  'secret.revoked': '凭据已撤销',
  'reconcile.started': '开始对账',
  'reconcile.result': '对账结论',
  'resume.requested': '已请求恢复',
  'system.start': '系统启动',
  'system.stop': '系统停止',
  'storage.pruned': '存储已清理',
};

/** 事件作用域。 */
export const EVENT_SCOPE_LABELS: Record<string, string> = {
  workflow: '流程',
  task: '任务',
  stage: '阶段',
  attempt: '尝试',
  session: '会话',
  resource: '资源',
  approval: '审批',
  system: '系统',
};

/** 事件发起者。 */
export const EVENT_ACTOR_LABELS: Record<string, string> = {
  user: '用户',
  system: '系统',
  adapter: '适配器',
  ai: 'AI',
};

/**
 * 会话台账状态（`session_handle.state`）。
 *
 * 这是台账里的自由文本列，不是类型化枚举；这里只收录代码里实际出现过的取值
 * （supervisor 与适配器写入 alive / lost / disposed / ended）。
 * **未列出的取值原样显示英文**——看到一个不认识的会话状态时，用户需要的是原文，
 * 而不是一个猜出来的中文词。
 */
export const SESSION_STATE_LABELS: Record<string, { text: string; tone: Tone }> = {
  alive: { text: '存活', tone: 'success' },
  lost: { text: '已失联', tone: 'danger' },
  disposed: { text: '已释放', tone: 'idle' },
  ended: { text: '已结束', tone: 'idle' },
};

/** 上下文包的五个分区。顺序与组装器一致。 */
export const PARTITION_LABELS: Record<string, string> = {
  P1: '角色与任务',
  P2: '输入与上游材料',
  P3: '输出要求',
  P4: '工具与权限',
  P5: '运行预留',
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
