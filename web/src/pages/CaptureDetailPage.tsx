/**
 * 捕获记录详情（/captures/:runId）：材料概览、生成流程草案、草案预览与采用/拒绝。
 *
 * 如实呈现的三件事：
 * - 材料汇编摘要：工具调用数、有没有显式计划、被裁剪过哪些部分——裁剪过就不是全貌；
 * - 「生成流程草案」按钮：点了才调用一次助手配置的模型；任务没跑完时禁用并说明；
 * - 草案上每条节点/连线的「观察到的 / 推断的」标注，以及服务端复核的降级记录。
 */

import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { capture as captureApi, templates as templatesApi, workflows as workflowsApi } from '../api/endpoints';
import type { CaptureDraft, CaptureDraftEdge, CaptureDraftNode, CaptureRunDetail } from '../api/types';
import { Banner, Empty, KV, Loading, Pill, TimeText } from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { taskStateLabel } from '../labels';
import { OriginBadge, RunStatusPill } from './CapturesPage';

/** 后端允许生成草案的任务终态（与 capture/service.py 的判定一致）。 */
const DRAFTABLE_TASK_STATES = new Set(['succeeded', 'failed', 'cancelled', 'blocked']);

/** profile 里取字符串字段；取不到就是空串（调用方决定怎么如实呈现「未知」）。 */
function profileText(run: CaptureRunDetail['run'], key: string): string {
  const v = run.profile[key];
  return typeof v === 'string' ? v : '';
}

function ReadError({ error, what, onRetry }: { error: ApiError; what: string; onRetry: () => void }): JSX.Element {
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接后台服务' : `无法读取${what}`}
      hint={
        error.unreachable
          ? '请确认后台服务已启动，然后点重试。'
          : (error.hint ?? undefined)
      }
      actions={
        <button type="button" className="btn btn--sm" onClick={onRetry}>
          重试
        </button>
      }
    >
      <span className="mono text-xs">{error.detail}</span>
    </Banner>
  );
}

/** 溯源标注徽标：observed 有真实执行记录佐证；inferred 是模型补的衔接。 */
export function BasisBadge({ basis }: { basis: 'observed' | 'inferred' | null }): JSX.Element {
  if (basis === 'observed') {
    return (
      <Pill tone="success" plain title="这一步在本次执行的材料里有真实记录佐证">
        观察到的
      </Pill>
    );
  }
  if (basis === 'inferred') {
    return (
      <Pill tone="pending" plain title="材料里没有直接佐证，是模型为了流程可复用补的衔接">
        推断的
      </Pill>
    );
  }
  return (
    <Pill tone="idle" plain title="这条没有溯源标注">
      未标注
    </Pill>
  );
}

export function CaptureDetailPage(): JSX.Element {
  const { runId = '' } = useParams();
  const detail = useAsync(() => captureApi.getRun(runId), [runId], { pollMs: 4000 });
  const generate = useSubmit();

  const data: CaptureRunDetail | null = detail.data;
  const run = data?.run ?? null;
  const taskState = data?.task_state ?? null;
  const material = data?.material ?? null;
  const drafts = data?.drafts ?? [];

  const taskLabel = taskState !== null ? taskStateLabel(taskState) : null;
  const canGenerate = run !== null && run.task_id !== null && taskState !== null && DRAFTABLE_TASK_STATES.has(taskState);
  const generateDisabledReason =
    run === null
      ? null
      : run.task_id === null
        ? '这次捕获的任务没有跑起来，没有材料可以整理。'
        : taskState === null
          ? '读不到任务状态，暂时不能生成。'
          : !DRAFTABLE_TASK_STATES.has(taskState)
            ? `任务还在跑（当前：${taskLabel?.text ?? taskState}），跑完才能生成流程草案。`
            : null;

  const onGenerate = async (): Promise<void> => {
    if (!run) return;
    const draft = await generate.run(() => captureApi.generateDraft(run.run_id));
    if (draft) detail.reload();
  };

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>
            <Link to="/captures" className="dim" style={{ fontWeight: 400 }}>
              捕获
            </Link>
            {' / '}
            {run ? run.name : ''}
          </h1>
          <div className="page-head__sub">
            {run ? (
              <span className="row row--tight">
                <OriginBadge origin={run.origin} />
                <RunStatusPill status={run.status} />
                {taskLabel ? (
                  <Pill tone={taskLabel.tone} transition={taskLabel.transitioning} title={taskState ?? ''}>
                    任务：{taskLabel.text}
                  </Pill>
                ) : null}
                <TimeText value={run.created_at} />
              </span>
            ) : null}
          </div>
        </div>
        <div className="page-head__actions">
          {run?.task_id ? (
            <Link to={`/tasks/${encodeURIComponent(run.task_id)}`} className="btn btn--sm">
              {run.origin === 'from_task' ? '看来源任务' : '看任务实时进展'}
            </Link>
          ) : null}
          <button type="button" className="btn btn--sm" onClick={detail.reload}>
            刷新
          </button>
        </div>
      </div>

      {detail.error ? (
        <ReadError error={detail.error} what="捕获记录" onRetry={detail.reload} />
      ) : !detail.loaded || !run ? (
        <Loading label="加载捕获记录" />
      ) : (
        <>
          <div className="panel">
            <div className="panel__head">基础候选与任务说明</div>
            <div className="panel__body">
              {run.origin === 'from_task' ? (
                <div className="text-xs dim" style={{ marginBottom: 'var(--sp-2)' }}>
                  这条捕获来自既有任务的执行记录，没有重新跑任务；基础候选取自当时实际执行的候选快照，取不到的字段如实显示「未知」。
                </div>
              ) : null}
              <KV
                items={[
                  {
                    k: 'harness',
                    v: <span className="mono">{profileText(run, 'harness_ref') || '未知'}</span>,
                  },
                  {
                    k: '模型',
                    v: (
                      <span className="mono">
                        {profileText(run, 'model_name') ||
                          (profileText(run, 'harness_ref') ? '（harness / 凭据默认）' : '未知')}
                      </span>
                    ),
                  },
                  {
                    k: '凭据',
                    v: (
                      <span className="mono">
                        {profileText(run, 'credential_ref') ||
                          (profileText(run, 'harness_ref') ? '（harness 本机登录态）' : '未知')}
                      </span>
                    ),
                  },
                ]}
              />
              <div className="section-title" style={{ marginTop: 'var(--sp-3)', marginBottom: 4 }}>
                任务说明
              </div>
              <div className="text-sm" style={{ whiteSpace: 'pre-wrap' }}>
                {profileText(run, 'instructions') || '未知'}
              </div>
            </div>
          </div>

          <div className="panel">
            <div className="panel__head">材料概览</div>
            <div className="panel__body">
              {material === null ? (
                <div className="text-sm dim">
                  {run.task_id ? '还没有可整理的材料。' : '这次捕获的任务没有跑起来，没有材料。'}
                </div>
              ) : (
                <>
                  <KV
                    items={[
                      { k: '工具调用', v: <span className="mono">{material.tool_calls} 次</span> },
                      { k: '产物', v: <span className="mono">{material.artifacts} 份</span> },
                      ...(material.stages > 1
                        ? [
                            {
                              k: '阶段',
                              v: (
                                <span className="mono">
                                  {material.stages} 个（材料含执行路径，按阶段分组）
                                </span>
                              ),
                            },
                          ]
                        : []),
                      {
                        k: '显式计划',
                        v: material.has_plan ? (
                          <span className="text-success">有——模型在输出里写了执行计划</span>
                        ) : (
                          <span className="text-warn">
                            没有——草案里的步骤划分会大多是「推断的」
                          </span>
                        ),
                      },
                      { k: '材料体量', v: <span className="mono">{material.chars.toLocaleString('zh-CN')} 字符</span> },
                    ]}
                  />
                  {material.trimmed.length > 0 ? (
                    <Banner variant="warn" title="材料被裁剪过，不是全貌">
                      以下内容因超出材料预算被裁掉：{material.trimmed.join('、')}。草案依据的是裁剪后的材料。
                    </Banner>
                  ) : null}
                </>
              )}

              <div className="divider" />
              <div className="row row--tight">
                <button
                  type="button"
                  className="btn btn--sm btn--primary"
                  disabled={!canGenerate || generate.busy}
                  title={generateDisabledReason ?? undefined}
                  onClick={() => void onGenerate()}
                >
                  {generate.busy ? '正在生成（会调用一次模型）…' : '生成流程草案'}
                </button>
                <span className="text-xs dim">
                  这会调用一次助手配置的模型（与助手共用同一份模型配置），不会重新执行任务。
                </span>
              </div>
              {generateDisabledReason ? (
                <div className="text-xs dim" style={{ marginTop: 'var(--sp-1)' }}>
                  {generateDisabledReason}
                </div>
              ) : null}
              {generate.error ? (
                <Banner
                  variant="danger"
                  title={generate.error.detail}
                  hint={generate.error.hint ?? undefined}
                />
              ) : null}
            </div>
          </div>

          <div className="panel">
            <div className="panel__head">流程草案（{drafts.length}）</div>
            <div className="panel__body">
              {drafts.length === 0 ? (
                <Empty
                  title="还没有草案"
                  hint="任务跑完后点上面的「生成流程草案」，系统会把这次执行整理成一份可复用的流程草案。"
                />
              ) : (
                <div className="col">
                  {drafts.map((draft) => (
                    <CaptureDraftCard key={draft.draft_id} draft={draft} onChanged={detail.reload} />
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ===========================================================================
// 草案预览卡片
// ===========================================================================

/** 边的端点可以是 node_id 也可以是节点名字；两种都解析成节点名来显示。 */
function edgeEndpointLabel(nodes: CaptureDraftNode[], ref: string): string {
  const byId = nodes.find((n) => n.node_id === ref);
  if (byId) return byId.name;
  const byName = nodes.find((n) => n.name === ref);
  return byName ? byName.name : ref;
}

function CaptureDraftCard({
  draft,
  onChanged,
}: {
  draft: CaptureDraft;
  onChanged: () => void;
}): JSX.Element {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  /** 本次操作里采用成了什么（流程 / 模板）；历史已采用的靠 AdoptedNote 现场核对。 */
  const [adoptedKind, setAdoptedKind] = useState<'workflow' | 'template' | null>(null);

  const nodes = draft.payload?.nodes ?? [];
  const edges = draft.payload?.edges ?? [];
  const notes = draft.payload?.notes ?? [];
  const validation = draft.validation;
  const diagnostics = validation?.diagnostics ?? [];
  const problems = diagnostics.filter((d) => d.severity === 'error' || d.severity === 'warning');
  const pending = validation?.pending_config ?? [];
  const downgrades = validation?.downgrades ?? [];

  const adopt = async (asTemplate: boolean): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const result = await captureApi.adoptDraft(draft.draft_id, { as_template: asTemplate });
      if (result.status === 'adopted') {
        setAdoptedKind(asTemplate ? 'template' : 'workflow');
        onChanged();
      }
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError({ kind: 'unreachable', status: 0, detail: String(err) }));
    } finally {
      setBusy(false);
    }
  };

  const reject = (): void => {
    if (!window.confirm('确认拒绝这份草案？拒绝后它不能再采用，想要的话需要重新生成一份。')) return;
    setBusy(true);
    setError(null);
    captureApi
      .rejectDraft(draft.draft_id)
      .then(() => onChanged())
      .catch((err: unknown) =>
        setError(err instanceof ApiError ? err : new ApiError({ kind: 'unreachable', status: 0, detail: String(err) })),
      )
      .finally(() => setBusy(false));
  };

  return (
    <div className="draft-card">
      <div className="draft-card__title">流程草案「{draft.name || '未命名'}」</div>
      {draft.description ? <div>{draft.description}</div> : null}

      {nodes.length > 0 ? (
        <div className="draft-card__section">
          <div>节点（{nodes.length} 个）：</div>
          <ul className="draft-card__list">
            {nodes.map((node, i) => {
              const profile = node.profiles?.[0];
              const harness = profile?.harness_ref || '待配置';
              const model = profile?.model_name || '默认模型';
              return (
                <li key={node.node_id ?? i}>
                  {node.name}
                  {node.role ? `（${node.role}）` : ''} · harness：{harness} · 模型：{model}{' '}
                  <BasisBadge basis={node.basis} />
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {edges.length > 0 ? (
        <div className="draft-card__section">
          <div>依赖（{edges.length} 条）：</div>
          <ul className="draft-card__list">
            {edges.map((edge: CaptureDraftEdge, i) => (
              <li key={i}>
                {edgeEndpointLabel(nodes, edge.from_node)} → {edgeEndpointLabel(nodes, edge.to_node)}{' '}
                <BasisBadge basis={edge.basis} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {validation ? (
        <div className="draft-card__section">
          <div>校验：{validation.error ? `草案有问题——${validation.error}` : validation.summary}</div>
          {problems.length > 0 ? (
            <ul className="draft-card__list">
              {problems.map((d, i) => (
                <li key={i}>{d.message}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {pending.length > 0 ? (
        <div className="draft-card__section">
          {pending.map((p, i) => (
            <div key={i} className="draft-card__pending">
              待配置：{p}
            </div>
          ))}
        </div>
      ) : null}

      {downgrades.length > 0 ? (
        <div className="draft-card__section">
          <div className="draft-card__pending">复核降级（{downgrades.length} 条）：</div>
          <ul className="draft-card__list">
            {downgrades.map((d, i) => (
              <li key={i} className="draft-card__pending">
                {d.target}：{d.reason}，已按「推断的」处理
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {notes.map((n, i) => (
        <div key={i} className="draft-card__note">
          {n}
        </div>
      ))}

      {draft.status === 'pending' ? (
        <>
          <div className="draft-card__note">
            采用后存为流程草稿，要到流程编辑器里发布后才生效；「存为模板」只保存结构与配置，不保存密钥。
          </div>
          <div className="draft-card__actions">
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={busy}
              onClick={() => void adopt(false)}
            >
              {busy ? '处理中…' : '采用为流程草稿'}
            </button>
            <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void adopt(true)}>
              存为模板
            </button>
            <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={reject}>
              拒绝
            </button>
          </div>
        </>
      ) : draft.status === 'adopted' ? (
        <div className="draft-card__note">
          <AdoptedNote adoptedRef={draft.adopted_ref} kind={adoptedKind} />
        </div>
      ) : (
        <div className="draft-card__note">已拒绝。</div>
      )}

      {error ? (
        <div className="draft-card__error">
          {error.detail}
          {error.hint ? `（${error.hint}）` : ''}
        </div>
      ) : null}
    </div>
  );
}

/** 已采用的落点提示与链接。本次操作知道是流程还是模板；历史记录现场核对一次。 */
function AdoptedNote({
  adoptedRef,
  kind,
}: {
  adoptedRef: string | null;
  kind: 'workflow' | 'template' | null;
}): JSX.Element {
  const probe = useAsync(
    async (): Promise<'workflow' | 'template' | null> => {
      if (kind !== null || adoptedRef === null) return kind;
      try {
        await workflowsApi.get(adoptedRef);
        return 'workflow';
      } catch {
        try {
          await templatesApi.get(adoptedRef);
          return 'template';
        } catch {
          return null;
        }
      }
    },
    [adoptedRef, kind],
  );

  if (adoptedRef === null) return <>已采用。</>;
  const resolved = kind ?? probe.data;
  if (resolved === 'workflow') {
    return (
      <>
        已采用，存为流程草稿（发布后才会生效）。
        <Link to={`/workflows/${encodeURIComponent(adoptedRef)}`}>到流程编辑器里继续编辑、发布</Link>。
      </>
    );
  }
  if (resolved === 'template') {
    return (
      <>
        已存为模板。<Link to="/templates">到模板页查看</Link>。
      </>
    );
  }
  if (!probe.loaded) return <>已采用。</>;
  return <>已采用（产物 {adoptedRef.slice(0, 8)}，在本机已找不到对应的流程或模板）。</>;
}
