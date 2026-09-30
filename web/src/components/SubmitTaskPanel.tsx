/**
 * 提交任务（RUN-01 / RUN-02 / WF-05）。
 *
 * 这个面板的核心不是「填表」，而是**把 HTTP 422 变成可点击的定位**：
 * 内核拒绝提交时返回 ValidationReport，其中每条 diagnostic 都指向一个节点、
 * 一条连线或一个槽位；点一下就该在画布上跳过去并高亮。所以这里只负责渲染结论
 * 与把 `onLocate` 原样透传下去——跳转是页面（画布）的事。
 *
 * 纪律：
 * - 本地「有效入口」预检只是提示，**内核才是权威**，绝不代替内核下结论。
 * - 幂等键的语义如实说明：重送安全，两次**有意**的相同提交仍是两个任务。
 * - 提交成功不自己导航：只回调 `onSubmitted`，路由归页面。
 * - 成功响应里带的 report 也要显示（warning/info 同样是事实）。
 */

import { useId, useMemo, useState } from 'react';
import type { Diagnostic, GraphSpec, NodeDefinition, SubmitResult, ValidationMode } from '../api/types';
import { tasks as taskApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { isRecord } from '../api/guards';
import { useSubmit } from '../hooks/useAsync';
import { Banner, Chip, Field, Modal, ShortId } from './common';
import type { DiagnosticTarget } from './Diagnostics';
import { DiagnosticsGrouped } from './Diagnostics';

export interface SubmitTaskPanelProps {
  workflowId: string;
  /** The graph the user is looking at, used to render human names for node ids in diagnostics. */
  graph: GraphSpec | null;
  /** Called when the user clicks a diagnostic. The page scrolls/zooms the canvas there and highlights it. */
  onLocate?: (target: DiagnosticTarget) => void;
  /** Called after a successful submit with the new (or idempotently-matched) task id. */
  onSubmitted?: (taskId: string) => void;
  /** Rendered as a modal when true; inline panel otherwise. Default false. */
  asModal?: boolean;
  onClose?: () => void;
}

/**
 * node_id → 人看得懂的名字。
 * 优先用当前图里的节点名（后端 diagnostic 里的 node_name 是快照，可能已经改名），
 * 找不到就退回短 id，id 都没有才是「未知节点」。
 */
export function graphNodeName(graph: GraphSpec | null, nodeId: string | null): string {
  if (!nodeId) return '（未知节点）';
  const node = graph ? graph.nodes.find((n) => n.node_id === nodeId) : undefined;
  if (!node) return `节点 ${nodeId.slice(0, 8)}`;
  return nodeDisplayName(node);
}

// ---------------------------------------------------------------------------
// 本地预检（提示，不是闸门）
// ---------------------------------------------------------------------------

/**
 * 有效入口：自身已启用、且不存在任何来自「已启用」节点的入边。
 * 指向停用节点的边不算上游——停用节点会被 derive 绕过（ACT-02），
 * 所以下游仍然是入口。这里只做与内核一致的粗判，结论以内核为准。
 */
function effectiveEntryNodes(graph: GraphSpec | null): NodeDefinition[] {
  if (!graph) return [];
  const enabled = new Set<string>();
  for (const node of graph.nodes) {
    if (node.enabled) enabled.add(node.node_id);
  }
  return graph.nodes.filter((node) => {
    if (!node.enabled) return false;
    return !graph.edges.some((edge) => edge.to_node === node.node_id && enabled.has(edge.from_node));
  });
}

function nodeDisplayName(node: NodeDefinition): string {
  const name = node.name.trim();
  return name || `节点 ${node.node_id.slice(0, 8)}`;
}

// ---------------------------------------------------------------------------
// 表单 → 内核请求体
// ---------------------------------------------------------------------------

type Mode = 'json' | 'text';

const VALIDATION_MODE_TEXT: Record<ValidationMode, string> = {
  draft: '草稿校验',
  publish: '发布校验',
  launch: '提交校验',
};

type PayloadBuild = { ok: true; payload: Record<string, unknown> } | { ok: false; error: string };

/**
 * 内核的 input_payload 是 JSON 对象（`Record<string, unknown>`），没有裸字符串形式；
 * 纯文本模式因此显式包装成 `{ text: … }`。
 */
function buildPayload(mode: Mode, jsonText: string, text: string): PayloadBuild {
  if (mode === 'text') return { ok: true, payload: { text } };
  const raw = jsonText.trim();
  if (!raw) return { ok: true, payload: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'JSON 解析失败' };
  }
  if (!isRecord(parsed)) {
    return { ok: false, error: '输入必须是 JSON 对象（{ … }），不能是数组、字符串或数字。' };
  }
  return { ok: true, payload: parsed };
}

/** 非法优先级不发给内核（NaN 序列化成 null 会变成另一种错误）。 */
function parsePriority(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value < 0 || value > 100) return null;
  return value;
}

function involvedNodeIds(diagnostics: Diagnostic[]): string[] {
  const ids: string[] = [];
  for (const d of diagnostics) {
    if (d.node_id && !ids.includes(d.node_id)) ids.push(d.node_id);
  }
  return ids;
}

function noop(): void {
  /* asModal 但未传 onClose 时的占位：宁可按钮无反应，也不假装关闭了。 */
}

// ---------------------------------------------------------------------------
// 结果渲染
// ---------------------------------------------------------------------------

/** 422：这里就是整个组件的重点——每条结论都点得动，点了交给画布定位。 */
function renderSubmitError(
  error: ApiError,
  onLocate: ((target: DiagnosticTarget) => void) | undefined,
  nameOf: (nodeId: string) => string,
): JSX.Element {
  if (error.kind === 'unprocessable') {
    const report = error.asValidationReport();
    const diagnostics = error.asDiagnostics();
    const nodeNames = involvedNodeIds(diagnostics).map(nameOf);
    return (
      <div>
        <Banner variant="danger" title="提交被拒绝：当前流程无法执行">
          <div>流程的定义或配置有问题，暂时无法执行。下面列出了具体原因，点击可定位到对应的节点或连线。</div>
          {report ? (
            <div className="text-xs" style={{ marginTop: 2 }}>
              校验模式：{VALIDATION_MODE_TEXT[report.mode]}（{report.mode}）
            </div>
          ) : null}
          {nodeNames.length > 0 ? (
            <div className="text-xs" style={{ marginTop: 2 }}>
              涉及节点：{nodeNames.join('、')}            </div>
          ) : null}
          {diagnostics.length === 0 ? (
            <div style={{ marginTop: 2 }}>
              <div>{error.detail}</div>
              {error.hint ? <div className="text-xs">{error.hint}</div> : null}
            </div>
          ) : null}
        </Banner>
        {diagnostics.length > 0 ? (
          <DiagnosticsGrouped diagnostics={diagnostics} onLocate={onLocate} />
        ) : null}
      </div>
    );
  }
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接后台服务' : '提交失败'}
      hint={error.hint ?? undefined}
    >
      {error.detail}
    </Banner>
  );
}

function renderSubmitResult(
  result: SubmitResult,
  onLocate: ((target: DiagnosticTarget) => void) | undefined,
): JSX.Element {
  const taskId = result.task_id;
  return (
    <div>
      {result.created === false ? (
        <Banner variant="info" title="这个去重标识已经用过，返回的是原来那次提交，没有新建任务" />
      ) : result.created === true ? (
        <Banner variant="ok" title="已创建新任务" />
      ) : (
        <Banner
          variant="info"
          title="提交已接受"
          hint="无法确认这次是新建了任务，还是匹配到了之前的提交。"
        />
      )}
      {!result.accepted ? (
        <Banner variant="danger" title="提交未被接受">
          这次提交没有创建任务，原因如下（点击可定位到对应位置）。
        </Banner>
      ) : null}
      {taskId ? (
        <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
          <span className="muted text-sm">任务</span>
          <a href={`#/tasks/${taskId}`} title={`打开任务详情 ${taskId}`}>
            <ShortId id={taskId} len={12} />
          </a>
          <span className="dim text-xs">在此查看阶段、执行图与审批</span>
        </div>
      ) : (
        <div className="text-sm dim" style={{ marginBottom: 'var(--sp-2)' }}>
          未返回任务 ID。
        </div>
      )}
      {result.report ? (
        <div style={{ marginTop: 'var(--sp-2)' }}>
          <div className="section-title">校验结果 · {result.report.diagnostics.length}</div>
          <DiagnosticsGrouped diagnostics={result.report.diagnostics} onLocate={onLocate} />
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 面板
// ---------------------------------------------------------------------------

export function SubmitTaskPanel({
  workflowId,
  graph,
  onLocate,
  onSubmitted,
  asModal = false,
  onClose,
}: SubmitTaskPanelProps): JSX.Element {
  const [mode, setMode] = useState<Mode>('json');
  const [jsonText, setJsonText] = useState('');
  const [textValue, setTextValue] = useState('');
  const [idempotencyKey, setIdempotencyKey] = useState('');
  const [priorityText, setPriorityText] = useState('50');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [priorityError, setPriorityError] = useState<string | null>(null);
  const [result, setResult] = useState<SubmitResult | null>(null);
  const submit = useSubmit();
  const radioName = useId();

  const entries = useMemo(() => effectiveEntryNodes(graph), [graph]);

  /** 用户一改输入，旧的 422 就不再对应当前内容——立刻清掉，不留在屏幕上误导人。 */
  const clearStale = (): void => {
    if (submit.error) submit.clear();
  };

  const switchMode = (next: Mode): void => {
    if (next === mode) return;
    setMode(next);
    setJsonError(null);
    setPriorityError(null);
    clearStale();
  };

  const formatJson = (): void => {
    const raw = jsonText.trim();
    if (!raw) {
      setJsonError(null);
      clearStale();
      return;
    }
    try {
      const parsed: unknown = JSON.parse(raw);
      setJsonText(JSON.stringify(parsed, null, 2));
      setJsonError(null);
      clearStale();
    } catch (err) {
      setJsonError(err instanceof Error ? err.message : 'JSON 解析失败');
    }
  };

  const runSubmit = async (): Promise<void> => {
    const built = buildPayload(mode, jsonText, textValue);
    if (!built.ok) {
      setJsonError(built.error);
      return;
    }
    const priority = parsePriority(priorityText);
    if (priority === null) {
      setPriorityError('优先级必须是 0–100 之间的整数。');
      return;
    }
    setJsonError(null);
    setPriorityError(null);
    setResult(null);

    const key = idempotencyKey.trim();
    const res = await submit.run(() =>
      taskApi.submit(workflowId, {
        input_payload: built.payload,
        idempotency_key: key ? key : null,
        priority,
      }),
    );
    if (!res) return;
    setResult(res);
    if (res.accepted && res.task_id) onSubmitted?.(res.task_id);
  };

  const content = (
    <div className="col" style={{ gap: 'var(--sp-3)' }}>
      {submit.error ? renderSubmitError(submit.error, onLocate, (id) => graphNodeName(graph, id)) : null}
      {result ? renderSubmitResult(result, onLocate) : null}

      <div className="row row--tight">
        <span className="field__label">输入方式</span>
        <label className="check">
          <input
            type="radio"
            name={radioName}
            checked={mode === 'json'}
            disabled={submit.busy}
            onChange={() => switchMode('json')}
          />
          JSON
        </label>
        <label className="check">
          <input
            type="radio"
            name={radioName}
            checked={mode === 'text'}
            disabled={submit.busy}
            onChange={() => switchMode('text')}
          />
          纯文本
        </label>
      </div>

      {mode === 'json' ? (
        <Field
          label="输入内容（JSON 对象）"
          hint="将作为任务输入原样提交；留空表示提交空对象 {}。"
          error={jsonError}
        >
          <textarea
            className="textarea textarea--code"
            rows={9}
            spellCheck={false}
            value={jsonText}
            disabled={submit.busy}
            placeholder={'{\n  "topic": "…"\n}'}
            onChange={(e) => {
              setJsonText(e.target.value);
              setJsonError(null);
              clearStale();
            }}
          />
          <div className="row row--tight" style={{ marginTop: 'var(--sp-1)' }}>
            <button type="button" className="btn btn--sm" onClick={formatJson} disabled={submit.busy}>
              格式化
            </button>
            <span className="text-xs dim">解析失败只提示、不提交；成功则按 2 空格缩进重排。</span>
          </div>
        </Field>
      ) : (
        <Field
          label="输入内容（纯文本）"
          hint={
            <span>
              提交时会打包成 <code className="mono">{'{ "text": "…" }'}</code> 的形式。
            </span>
          }
          error={jsonError}
        >
          <textarea
            className="textarea"
            rows={9}
            value={textValue}
            disabled={submit.busy}
            placeholder="把要交给入口节点的原始输入贴在这里"
            onChange={(e) => {
              setTextValue(e.target.value);
              setJsonError(null);
              clearStale();
            }}
          />
        </Field>
      )}

      <div className="field-row">
        <Field
          label="去重标识（可选）"
          hint={
            <span>
              填了它，网络重试时用同一个值重发不会多出任务；换一个值再提交则会产生新任务。
            </span>
          }
        >
          <input
            className="input input--mono"
            value={idempotencyKey}
            spellCheck={false}
            disabled={submit.busy}
            placeholder="留空则每次提交都是新任务"
            onChange={(e) => {
              setIdempotencyKey(e.target.value);
              clearStale();
            }}
          />
        </Field>
        <Field
          label="优先级（0–100）"
          hint="数值越大越先执行；同一节点的队列里，依次按节点优先级、任务优先级、提交先后排序。"
          error={priorityError}
        >
          <input
            type="number"
            className="input input--num"
            min={0}
            max={100}
            step={1}
            value={priorityText}
            disabled={submit.busy}
            onChange={(e) => {
              setPriorityText(e.target.value);
              setPriorityError(null);
              clearStale();
            }}
          />
        </Field>
      </div>

      <div>
        <div className="section-title">提交前检查（本地预检，以实际提交结果为准）</div>
        {graph === null ? (
          <div className="text-sm dim">无法读取当前流程图，跳过入口检查</div>
        ) : entries.length === 0 ? (
          <Banner
            variant="warn"
            title="当前没有可执行的入口节点，提交会被拒绝。"
            hint="入口节点是「已启用、且没有已启用的上游节点」的节点。请检查各节点的启用状态。"
          />
        ) : (
          <div className="row row--tight">
            <span className="text-sm muted">当前有效入口节点（{entries.length}）：</span>
            <div className="chips">
              {entries.map((node) => (
                <Chip key={node.node_id} variant="accent" title={node.node_id}>
                  {nodeDisplayName(node)}
                </Chip>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="row row--tight">
        <button
          type="button"
          className="btn btn--primary"
          disabled={submit.busy}
          onClick={() => void runSubmit()}
        >
          {submit.busy ? <span className="spin" /> : null}
          提交任务
        </button>
        {submit.busy ? <span className="text-sm muted">正在提交…</span> : null}
        <span className="spacer" />
        <span className="text-xs dim">提交时会对整个流程做检查；如果被拒绝，下面会列出具体原因，点击可定位。</span>
      </div>
    </div>
  );

  if (asModal) {
    return (
      <Modal title="提交任务" onClose={onClose ?? noop} wide>
        {content}
      </Modal>
    );
  }
  return (
    <div className="panel">
      <div className="panel__body">{content}</div>
    </div>
  );
}
