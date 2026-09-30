/**
 * 节点启停：预览 → 选策略 → 执行（ACT-01/02/04、D-01）。
 *
 * 这不是一个开关了事的功能，三步都必须走完：
 * 1. **预览**（`preview=true`）：受影响的节点、新增/消失的有效依赖边、
 *    有效入口/出口是否变化、受影响的任务列表。这一步**不改变任何状态**。
 * 2. **选存量任务处理方式**（仅停用时）：排水（默认，在途跑完、排队保留）
 *    还是立即撤回（在途走取消链、排队标记 SKIPPED）。
 * 3. **确认后**才 `preview=false`，并按响应的「实际生效结果」告知用户，
 *    而不是显示乐观假设。
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { GraphDelta, NodeToggleResponse, ToggleMode, TogglePreview } from '../api/types';
import { workflows as workflowApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { useSubmit } from '../hooks/useAsync';
import { Banner, Chip, Modal, ShortId } from './common';
import { DiagnosticsGrouped, type DiagnosticTarget } from './Diagnostics';

export interface ToggleFlowProps {
  workflowId: string;
  nodeId: string;
  nodeName: string;
  /** 目标状态：true 启用，false 停用。 */
  enabling: boolean;
  onClose: () => void;
  /** 执行成功（或已应用）后刷新流程与图。 */
  onApplied: (result: NodeToggleResponse) => void;
  /** 点校验条目时在画布上定位。 */
  onLocate?: (target: DiagnosticTarget) => void;
}

export function ToggleFlow({
  workflowId,
  nodeId,
  nodeName,
  enabling,
  onClose,
  onApplied,
  onLocate,
}: ToggleFlowProps): JSX.Element {
  const navigate = useNavigate();
  const [preview, setPreview] = useState<TogglePreview | null>(null);
  const [previewError, setPreviewError] = useState<ApiError | null>(null);
  const [mode, setMode] = useState<ToggleMode>('drain');
  const [result, setResult] = useState<NodeToggleResponse | null>(null);
  const submit = useSubmit();

  const loadPreview = useCallback(async () => {
    setPreviewError(null);
    try {
      const data = await workflowApi.togglePreview(workflowId, nodeId);
      setPreview(data);
    } catch (err) {
      setPreviewError(err instanceof ApiError ? err : null);
    }
  }, [workflowId, nodeId]);

  useEffect(() => {
    void loadPreview();
  }, [loadPreview]);

  const apply = async (): Promise<void> => {
    const response = await submit.run(() =>
      workflowApi.toggle(workflowId, nodeId, { enable: enabling, mode }),
    );
    if (response) {
      setResult(response);
      onApplied(response);
    }
  };

  // ---------------- 结果 ----------------
  if (result) {
    return (
      <Modal
        title={enabling ? '节点已启用' : '节点停用结果'}
        onClose={onClose}
        wide
        footer={
          <button type="button" className="btn btn--primary" onClick={onClose}>
            知道了
          </button>
        }
      >
        <div className="tristate" style={{ marginBottom: 'var(--sp-3)' }}>
          <div className={`tristate__cell tristate__cell--${result.applied ? 'yes' : 'no'}`}>
            <div className="tristate__label">启停状态</div>
            <div className="tristate__value">{result.applied ? '已生效' : '等待生效'}</div>
            {result.new_revision_seq !== null ? (
              <div className="text-xs dim">新修订 #{result.new_revision_seq}</div>
            ) : null}
          </div>
          <div className={`tristate__cell tristate__cell--${result.awaiting_drain ? 'no' : 'yes'}`}>
            <div className="tristate__label">进行中的阶段</div>
            <div className="tristate__value">
              {result.awaiting_drain ? '仍在运行，等跑完' : mode === 'drain' ? '已全部跑完' : '已立即取消'}
            </div>
            <div className="text-xs dim">处理方式：{result.mode === 'immediate' ? '立即取消' : '等跑完再停用'}</div>
          </div>
        </div>

        {result.awaiting_drain ? (
          <Banner variant="warn" title="停用等待生效">
            这个节点上还有正在运行的阶段，等它们跑完后节点才会正式停用，已完成的工作不会丢弃。
            在此期间该节点不再接手新阶段；停用生效后流程会产生一个新版本。
          </Banner>
        ) : null}

        {result.affected_tasks.length > 0 ? (
          <div style={{ marginTop: 'var(--sp-2)' }}>
            <div className="section-title">受影响的任务 · {result.affected_tasks.length}</div>
            <div className="chips">
              {result.affected_tasks.map((id) => (
                <Chip
                  key={id}
                  onClick={() => {
                    onClose();
                    navigate(`/tasks/${id}`);
                  }}
                  title="点击查看该任务"
                >
                  <ShortId id={id} />
                </Chip>
              ))}
            </div>
          </div>
        ) : null}

        <EdgeDeltaList title="新增的依赖连线" edges={result.added_edges} tone="accent" />
        <EdgeDeltaList title="消失的依赖连线" edges={result.removed_edges} tone="warn" />
        <IdList title="等跑完再停的阶段" ids={result.drained_stages} />
        <IdList title="被立即取消的阶段" ids={result.withdrawn_stages} tone="danger" />
        <IdList title="重新启用后回到队列的阶段" ids={result.revived_stages} tone="accent" />

        {result.warnings.length > 0 ? (
          <Banner variant="warn" title="注意事项">
            <ul className="list-reset">
              {result.warnings.map((w, i) => (
                <li key={i}>· {w}</li>
              ))}
            </ul>
          </Banner>
        ) : null}
      </Modal>
    );
  }

  // ---------------- 预览 ----------------
  const delta: GraphDelta | null = preview?.delta ?? null;
  const entryChanged =
    delta !== null &&
    (delta.entry_nodes_before.join() !== delta.entry_nodes_after.join() ||
      delta.exit_nodes_before.join() !== delta.exit_nodes_after.join());
  const noEntry = delta !== null && delta.entry_nodes_after.length === 0;
  const report = preview?.report ?? null;
  const blocking = report ? report.diagnostics.filter((d) => d.severity === 'error').length : 0;

  return (
    <Modal
      title={enabling ? `启用节点「${nodeName}」` : `停用节点「${nodeName}」`}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className={enabling ? 'btn btn--primary' : 'btn btn--danger'}
            disabled={submit.busy || preview === null || noEntry}
            onClick={() => void apply()}
            title={noEntry ? '停用后流程没有可执行的入口，无法停用' : undefined}
          >
            {enabling ? '确认启用' : mode === 'immediate' ? '确认停用（立即取消）' : '确认停用（等跑完再停）'}
          </button>
        </>
      }
    >
      <Banner variant="info" title="这一步只是预览，不会改动任何内容">
        下面是这次操作对流程结构和进行中任务的影响，确认之后才会真正生效。
      </Banner>

      {previewError ? (
        <Banner variant="danger" title={previewError.unreachable ? '无法连接后台服务' : '无法获取预览'}>
          {previewError.detail}
          <div style={{ marginTop: 6 }}>
            <button type="button" className="btn btn--sm" onClick={() => void loadPreview()}>
              重试
            </button>
          </div>
        </Banner>
      ) : null}

      {preview === null && !previewError ? (
        <div className="empty">
          <span className="spin" /> <span style={{ marginLeft: 8 }}>正在计算影响…</span>
        </div>
      ) : null}

      {delta ? (
        <>
          <div className="section-title">对流程结构的影响</div>
          <div className="kv">
            <div className="kv__k">受影响的下游节点</div>
            <div className="kv__v">
              {delta.affected_downstream.length === 0 ? (
                <span className="dim">无</span>
              ) : (
                <div className="chips">
                  {delta.affected_downstream.map((id) => (
                    <Chip key={id}>{ShortId({ id } as never) && <ShortId id={id} />}</Chip>
                  ))}
                </div>
              )}
            </div>
            <div className="kv__k">入口节点</div>
            <div className="kv__v">
              <NodeList ids={delta.entry_nodes_before} /> → <NodeList ids={delta.entry_nodes_after} />
              {entryChanged ? <span className="chip chip--warn">发生变化</span> : null}
            </div>
            <div className="kv__k">出口节点</div>
            <div className="kv__v">
              <NodeList ids={delta.exit_nodes_before} /> → <NodeList ids={delta.exit_nodes_after} />
              {entryChanged ? <span className="chip chip--warn">发生变化</span> : null}
            </div>
          </div>

          <EdgeDeltaList title="新增的依赖连线（绕过被停用节点后出现的路径）" edges={delta.added_edges.map((e) => [e.from_node, e.to_node])} tone="accent" via={delta.added_edges.map((e) => e.via)} />
          <EdgeDeltaList title="消失的依赖连线" edges={delta.removed_edges.map((e) => [e.from_node, e.to_node])} tone="warn" via={delta.removed_edges.map((e) => e.via)} />

          {noEntry ? (
            <Banner variant="danger" title="停用后流程没有可执行的入口">
              流程至少要保留一个可执行的入口节点，所以这次停用不能执行。
            </Banner>
          ) : null}

          <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
            受影响的任务 · {preview?.affected_tasks.length ?? 0}
          </div>
          {preview && preview.affected_tasks.length > 0 ? (
            <>
              <div className="chips">
                {preview.affected_tasks.map((id) => (
                  <Chip
                    key={id}
                    onClick={() => {
                      onClose();
                      navigate(`/tasks/${id}`);
                    }}
                  >
                    <ShortId id={id} />
                  </Chip>
                ))}
              </div>
              {!enabling ? (
                <div className="col" style={{ marginTop: 'var(--sp-3)' }}>
                  <div className="section-title" style={{ margin: 0 }}>
                    进行中的任务怎么处理（必须选一项）
                  </div>
                  <label className="check" style={{ alignItems: 'flex-start' }}>
                    <input
                      type="radio"
                      name="toggle-mode"
                      checked={mode === 'drain'}
                      onChange={() => setMode('drain')}
                      style={{ marginTop: 3 }}
                    />
                    <span>
                      <strong>等跑完再停用（默认）</strong>
                      <div className="text-xs muted">
                        正在运行的阶段会跑完，排队中的阶段保留；该节点不再接手新阶段。等全部结束后才正式停用。
                        <strong>不会丢弃已完成的工作</strong>，但停用生效会有延迟。
                      </div>
                    </span>
                  </label>
                  <label className="check" style={{ alignItems: 'flex-start' }}>
                    <input
                      type="radio"
                      name="toggle-mode"
                      checked={mode === 'immediate'}
                      onChange={() => setMode('immediate')}
                      style={{ marginTop: 3 }}
                    />
                    <span>
                      <strong>立即取消</strong>
                      <div className="text-xs muted">
                        正在运行的阶段立即取消（<strong>不保留进度</strong>，重新启用后可能要重跑），
                        排队中的阶段标记为已跳过。停用立即生效。
                      </div>
                    </span>
                  </label>
                </div>
              ) : (
                <div className="text-xs dim" style={{ marginTop: 6 }}>
                  启用立即生效：该节点重新参与流程，之后启动的任务使用新的流程结构；
                  已经在运行的任务仍按启动时的结构执行到结束。
                </div>
              )}
            </>
          ) : (
            <div className="empty text-sm">没有正在使用该节点的任务。</div>
          )}

          {report && report.diagnostics.length > 0 ? (
            <>
              <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
                改动后的可执行性检查
                {blocking > 0 ? <span className="chip chip--danger">{blocking} 项必须处理的问题</span> : null}
              </div>
              <DiagnosticsGrouped
                diagnostics={report.diagnostics}
                onLocate={onLocate}
              />
            </>
          ) : null}
        </>
      ) : null}

      {submit.error ? (
        <Banner variant="danger" title="启停请求失败">
          {submit.error.detail}
          {submit.error.hint ? <div className="banner__hint">{submit.error.hint}</div> : null}
        </Banner>
      ) : null}
    </Modal>
  );
}

function NodeList({ ids }: { ids: string[] }): JSX.Element {
  if (ids.length === 0) return <span className="dim">（无）</span>;
  return (
    <span className="chips">
      {ids.map((id) => (
        <Chip key={id}>
          <ShortId id={id} />
        </Chip>
      ))}
    </span>
  );
}

function EdgeDeltaList({
  title,
  edges,
  tone,
  via,
}: {
  title: string;
  edges: [string, string][];
  tone?: 'accent' | 'warn' | 'danger';
  via?: string[][];
}): JSX.Element | null {
  if (edges.length === 0) return null;
  return (
    <div style={{ marginTop: 'var(--sp-2)' }}>
      <div className="section-title">
        {title} · {edges.length}
      </div>
      <div className="chips">
        {edges.map(([from, to], i) => {
          const bypass = via?.[i] ?? [];
          return (
            <Chip
              key={`${from}->${to}`}
              variant={tone}
              title={bypass.length > 0 ? `绕过：${bypass.join(' → ')}` : undefined}
            >
              <ShortId id={from} len={6} /> → <ShortId id={to} len={6} />
              {bypass.length > 0 ? '（绕过）' : ''}
            </Chip>
          );
        })}
      </div>
    </div>
  );
}

function IdList({
  title,
  ids,
  tone,
}: {
  title: string;
  ids: string[];
  tone?: 'accent' | 'warn' | 'danger';
}): JSX.Element | null {
  if (ids.length === 0) return null;
  return (
    <div style={{ marginTop: 'var(--sp-2)' }}>
      <div className="section-title">
        {title} · {ids.length}
      </div>
      <div className="chips">
        {ids.map((id) => (
          <Chip key={id} variant={tone}>
            <ShortId id={id} />
          </Chip>
        ))}
      </div>
    </div>
  );
}
