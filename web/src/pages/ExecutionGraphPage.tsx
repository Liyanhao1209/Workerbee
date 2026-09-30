/**
 * 实际执行图（OBS-02）。运维时看的是这一张，不是编辑器那张。
 *
 * 硬要求：
 * - 图来自任务**发射时 pinned 的快照**（`graph_snapshot`），不是当前流程定义；
 * - 节点状态用阶段的 `observed_state` 着色，**过渡态（分派中／暂停中／退避中／
 *   等待审批／核对中／状态不明）必须与稳定态一眼可分**（OBS-01）；
 * - 任务失败时，**失败原因与仍在运行的分支同时显示**（RUN-07）。
 */

import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { Attempt, StageState, TaskDetail, TaskStage } from '../api/types';
import { tasks as taskApi } from '../api/endpoints';
import { useAsync } from '../hooks/useAsync';
import { asArray } from '../api/guards';
import { STAGE_STATES, STAGE_TRANSITIONING_STATES } from '../api/types';
import { ExecutionCanvas } from '../graph/ExecutionCanvas';
import { TaskControls, OriginOfControlText, ErrorBanner } from '../components/TaskControls';
import { ApprovalCard } from '../components/Approval';
import {
  Banner,
  Bytes,
  Chip,
  CountOrUnknown,
  Empty,
  KV,
  Loading,
  RelTime,
  ShortId,
  StageStatePill,
  TaskStatePill,
  TimeText,
  humanDuration,
  durationBetween,
} from '../components/common';
import {
  ARTIFACT_KIND_LABELS,
  DESIRED_LABELS,
  ERROR_CLASS_LABELS,
  SENSITIVITY_LABELS,
  STAGE_TONE_VAR,
  stageStateLabel,
} from '../labels';

export function ExecutionGraphPage(): JSX.Element {
  const { taskId } = useParams<{ taskId: string }>();
  if (!taskId) return <TaskPicker />;
  return <ExecutionView taskId={taskId} />;
}

// ---------------------------------------------------------------------------
// 索引：挑一个任务
// ---------------------------------------------------------------------------

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

function TaskPicker(): JSX.Element {
  const navigate = useNavigate();
  const fetchTasks = useCallback(() => taskApi.list(), []);
  const { data, loading, error, loaded, reload } = useAsync(fetchTasks, [], { pollMs: 5000 });

  const tasks = useMemo(() => {
    const list = asArray<TaskDetail['task']>(data?.tasks);
    // 未结束的排在前面：运维打开这一页通常是为了看正在跑的东西。
    return [...list].sort((a, b) => {
      const at = TERMINAL.has(a.observed_state) ? 1 : 0;
      const bt = TERMINAL.has(b.observed_state) ? 1 : 0;
      if (at !== bt) return at - bt;
      return (b.created_at ?? '').localeCompare(a.created_at ?? '');
    });
  }, [data]);

  if (loading && !loaded) return <Loading label="加载任务" />;

  return (
    <div className="page page--wide">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>实际执行图</h1>
          <div className="page-head__sub">
            选一个任务，查看它实际执行的流程和实时状态。这里展示的是执行记录，不能编辑。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={reload}>
            刷新
          </button>
          <button type="button" className="btn btn--sm" onClick={() => navigate('/tasks')}>
            任务列表
          </button>
        </div>
      </div>

      <ErrorBanner error={error} />

      {tasks.length === 0 && !error ? (
        <Empty title="没有任务" hint="提交一个任务后，这里会出现它的执行图。" />
      ) : tasks.length > 0 ? (
        <div className="panel">
          <div className="table-wrap">
            <table className="table table--dense table--rows-clickable">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>流程</th>
                  <th>状态</th>
                  <th>运行中分支</th>
                  <th>修订 / 图版本</th>
                  <th>提交时间</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const branches = asArray<string>(task.failure_summary?.['running_branches']);
                  return (
                    <tr key={task.task_id} onClick={() => navigate(`/execution/${task.task_id}`)}>
                      <td className="table__id">
                        <ShortId id={task.task_id} len={10} />
                      </td>
                      <td>{task.workflow_name ?? <ShortId id={task.workflow_id} />}</td>
                      <td>
                        <TaskStatePill state={task.observed_state} />
                      </td>
                      <td>
                        {branches.length > 0 ? (
                          <Chip variant="accent" title={branches.join(', ')}>
                            运行中分支 {branches.length}
                          </Chip>
                        ) : (
                          <span className="dim text-xs">—</span>
                        )}
                      </td>
                      <td className="mono text-xs">
                        #{task.revision_seq} / v{task.effective_graph_version}
                      </td>
                      <td>
                        <RelTime value={task.created_at ?? null} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 单个任务的执行图
// ---------------------------------------------------------------------------

function ExecutionView({ taskId }: { taskId: string }): JSX.Element {
  const navigate = useNavigate();
  const fetchDetail = useCallback(() => taskApi.get(taskId), [taskId]);
  const { data, loading, error, loaded, reload } = useAsync(fetchDetail, [taskId], { pollMs: 3000 });
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const task = data?.task ?? null;
  const stages = useMemo(() => data?.stages ?? [], [data]);

  const runningBranches = useMemo(
    () => asArray<string>(task?.failure_summary?.['running_branches']),
    [task],
  );

  const declaredEdges = useMemo<[string, string][]>(() => {
    const graph = task?.graph_snapshot.graph;
    if (!graph) return [];
    return graph.edges.map((e) => [e.from_node, e.to_node]);
  }, [task]);

  const stageByNode = useMemo(() => {
    const map = new Map<string, TaskStage>();
    for (const stage of stages) {
      const prev = map.get(stage.node_id);
      if (!prev) map.set(stage.node_id, stage);
      else {
        const a = new Date(prev.updated_at ?? prev.created_at ?? 0).getTime();
        const b = new Date(stage.updated_at ?? stage.created_at ?? 0).getTime();
        if (b >= a) map.set(stage.node_id, stage);
      }
    }
    return map;
  }, [stages]);

  if (loading && !loaded) return <Loading label="加载任务" />;

  if (error && !task) {
    return (
      <div className="page">
        <ErrorBanner error={error} />
        <button type="button" className="btn btn--sm" onClick={reload}>
          重试
        </button>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="page">
        <Empty title="没有读到任务" hint="后台服务返回了空内容，请刷新重试。" />
      </div>
    );
  }

  const snapshot = task.graph_snapshot;
  const selectedStage = selectedNodeId ? stageByNode.get(selectedNodeId) ?? null : null;
  const selectedNode = selectedNodeId
    ? snapshot.graph.nodes.find((n) => n.node_id === selectedNodeId) ?? null
    : null;

  return (
    <div className="page page--flush" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ padding: 'var(--sp-3) var(--sp-4) 0', flex: '0 0 auto' }}>
        <div className="page-head" style={{ marginBottom: 'var(--sp-2)' }}>
          <div className="page-head__titles">
            <h1 className="row row--tight" style={{ gap: 'var(--sp-2)' }}>
              执行图
              <TaskStatePill state={task.observed_state} />
              <span className="text-xs dim mono">
                <ShortId id={task.task_id} len={12} />
              </span>
            </h1>
            <div className="page-head__sub">
              {task.workflow_name ?? <ShortId id={task.workflow_id} />} · 修订 #{task.revision_seq} ·
              图版本 {task.effective_graph_version} · 目标状态：{DESIRED_LABELS[task.desired_state]} ·
              操作序号 {task.control_epoch}
            </div>
            <OriginOfControlText task={task} />
          </div>
          <div className="page-head__actions">
            <TaskControls
              task={task}
              compact
              onChanged={reload}
            />
            <button type="button" className="btn btn--sm" onClick={() => navigate(`/tasks/${task.task_id}`)}>
              任务详情
            </button>
            <button type="button" className="btn btn--sm" onClick={reload}>
              刷新
            </button>
          </div>
        </div>

        {error ? (
          <Banner variant="warn" title="最近一次刷新失败，下面显示的是上一次读到的内容">
            {error.detail}
          </Banner>
        ) : null}

        <Banner variant="info" title="这张图记录的是任务提交时的流程版本" hint="之后对流程定义的修改不会改变它。">
          虚线节点 = 状态还在变化中 · 绿色描边 = 仍在运行的分支 · 虚线边 = 实际执行中绕过的连线。
        </Banner>

        {task.failure_summary ? (
          <FailurePanel
            summary={task.failure_summary}
            runningBranches={runningBranches}
            onFocusNode={(nodeId) => setSelectedNodeId(nodeId)}
          />
        ) : null}

        {task.blocked_reason && !task.failure_summary ? (
          <Banner variant="warn" title="受阻原因">
            {task.blocked_reason}
          </Banner>
        ) : null}
      </div>

      <div
        style={{
          flex: '1 1 auto',
          minHeight: 0,
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) 380px',
          gap: 'var(--sp-3)',
          padding: 'var(--sp-3) var(--sp-4)',
        }}
      >
        <div
          style={{
            border: '1px solid var(--line)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            minHeight: 0,
            position: 'relative',
          }}
        >
          {snapshot.graph.nodes.length === 0 ? (
            <Empty title="该任务的流程图为空" hint="任务提交时的流程版本里没有任何节点。" />
          ) : (
            <ExecutionCanvas
              pinnedGraph={snapshot.graph}
              effectiveEdges={snapshot.effective_edges}
              stages={stages}
              runningBranchNodeIds={runningBranches}
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
              declaredEdges={declaredEdges}
            />
          )}
        </div>

        <div className="panel" style={{ minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          <div className="panel__head">
            {selectedNode ? `节点「${selectedNode.name}」` : '图例与阶段'}
            <div className="panel__head-actions">
              {selectedNodeId ? (
                <button type="button" className="btn btn--xs" onClick={() => setSelectedNodeId(null)}>
                  返回图例
                </button>
              ) : null}
            </div>
          </div>
          <div className="panel__body" style={{ overflow: 'auto', minHeight: 0, flex: '1 1 auto' }}>
            {selectedNodeId ? (
              <StagePanel
                taskId={task.task_id}
                stage={selectedStage}
                nodeName={selectedNode?.name ?? null}
                attempts={(data?.attempts ?? []).filter((a) => a.stage_id === selectedStage?.stage_id)}
                artifacts={(data?.artifacts ?? []).filter(
                  (a) => selectedStage && a.producer?.stage_id === selectedStage.stage_id,
                )}
                approvals={(data?.approvals ?? []).filter(
                  (a) => !selectedStage || a.bound_to.stage_id === selectedStage.stage_id,
                )}
                onChanged={reload}
              />
            ) : (
              <Legend stages={stages} onPick={setSelectedNodeId} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 失败摘要（RUN-07：原因与仍在运行的分支必须同时出现）
// ---------------------------------------------------------------------------

function FailurePanel({
  summary,
  runningBranches,
  onFocusNode,
}: {
  summary: Record<string, unknown>;
  runningBranches: string[];
  onFocusNode: (nodeId: string) => void;
}): JSX.Element {
  const reason = typeof summary['reason'] === 'string' ? summary['reason'] : null;
  const detail = typeof summary['detail'] === 'string' ? summary['detail'] : null;
  const errorClass = typeof summary['error_class'] === 'string' ? summary['error_class'] : null;
  const failedStages = asArray<string>(summary['failed_stages']);
  const blockedStages = asArray<string>(summary['blocked_stages']);

  return (
    <Banner
      variant="danger"
      title={
        <>
          任务未成功结束
          {errorClass
            ? ` · ${ERROR_CLASS_LABELS[errorClass as keyof typeof ERROR_CLASS_LABELS]?.text ?? errorClass}`
            : ''}
        </>
      }
    >
      <div className="grid grid--2" style={{ gap: 'var(--sp-3)' }}>
        <div>
          <div className="section-title">为什么失败</div>
          <div className="text-sm">{reason ?? '没有记录失败原因'}</div>
          {detail ? <div className="text-xs muted" style={{ marginTop: 3 }}>{detail}</div> : null}
          {failedStages.length > 0 ? (
            <div className="chips" style={{ marginTop: 6 }}>
              {failedStages.map((id) => (
                <Chip key={id} variant="danger" onClick={() => onFocusNode(id)} title="在图上定位">
                  失败阶段 <ShortId id={id} len={6} />
                </Chip>
              ))}
            </div>
          ) : null}
          {blockedStages.length > 0 ? (
            <div className="chips" style={{ marginTop: 6 }}>
              {blockedStages.map((id) => (
                <Chip key={id} variant="warn" onClick={() => onFocusNode(id)} title="在图上定位">
                  受阻阶段 <ShortId id={id} len={6} />
                </Chip>
              ))}
            </div>
          ) : null}
        </div>
        <div>
          <div className="section-title">仍在运行的分支（任务失败不会自动停止它们）</div>
          {runningBranches.length === 0 ? (
            <div className="text-sm dim">没有仍在运行的阶段。</div>
          ) : (
            <>
              <div className="chips">
                {runningBranches.map((id) => (
                  <Chip key={id} variant="accent" onClick={() => onFocusNode(id)} title="在图上定位">
                    <ShortId id={id} len={6} />
                  </Chip>
                ))}
              </div>
              <div className="text-xs muted" style={{ marginTop: 4 }}>
                这些分支仍在运行并占用资源。要全部停下，请使用「暂停」或「删除任务」（作用于整个任务）。
              </div>
            </>
          )}
        </div>
      </div>
    </Banner>
  );
}

// ---------------------------------------------------------------------------
// 图例
// ---------------------------------------------------------------------------

function Legend({
  stages,
  onPick,
}: {
  stages: TaskStage[];
  onPick: (nodeId: string) => void;
}): JSX.Element {
  const present = new Set(stages.map((s) => s.observed_state));
  return (
    <div>
      <div className="section-title">状态图例</div>
      <div className="col" style={{ gap: 3 }}>
        {STAGE_STATES.map((state: StageState) => {
          const label = stageStateLabel(state);
          const transitioning = STAGE_TRANSITIONING_STATES.includes(state);
          const count = stages.filter((s) => s.observed_state === state).length;
          return (
            <div
              key={state}
              className="row row--tight"
              style={{
                opacity: present.has(state) ? 1 : 0.45,
                border: `1px ${transitioning ? 'dashed' : 'solid'} ${present.has(state) ? STAGE_TONE_VAR[state] : 'var(--line)'}`,
                borderRadius: 'var(--radius)',
                padding: '3px 6px',
              }}
              title={label.hint}
            >
              <span
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: 2,
                  background: STAGE_TONE_VAR[state],
                  flex: '0 0 auto',
                }}
              />
              <span className="text-sm">{label.text}</span>
              {transitioning ? <span className="chip chip--warn">变化中</span> : null}
              <span className="spacer" />
              <span className="mono text-xs">{count}</span>
            </div>
          );
        })}
      </div>

      <div className="divider" />
      <div className="section-title">本次任务的阶段</div>
      {stages.length === 0 ? (
        <Empty title="还没有阶段记录" hint="任务刚提交、还没来得及调度时，这里会暂时为空。" />
      ) : (
        <div className="col" style={{ gap: 3 }}>
          {stages.map((stage) => (
            <button
              key={stage.stage_id}
              type="button"
              className="btn btn--xs"
              style={{ justifyContent: 'flex-start' }}
              onClick={() => onPick(stage.node_id)}
            >
              <StageStatePill state={stage.observed_state} />
              <span className="truncate" style={{ flex: '1 1 auto' }}>
                {stage.node_name ?? stage.node_id.slice(0, 8)}
              </span>
              {stage.attempt_count > 1 ? <span className="text-xs dim">×{stage.attempt_count}</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 阶段详情
// ---------------------------------------------------------------------------

function StagePanel({
  taskId,
  stage,
  nodeName,
  attempts,
  artifacts,
  approvals,
  onChanged,
}: {
  taskId: string;
  stage: TaskStage | null;
  nodeName: string | null;
  attempts: Attempt[];
  artifacts: NonNullable<TaskDetail['artifacts']>;
  approvals: NonNullable<TaskDetail['approvals']>;
  onChanged: () => void;
}): JSX.Element {
  const [resumeNote, setResumeNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  if (!stage) {
    return (
      <Empty
        title={`节点「${nodeName ?? '（未知）'}」还没有阶段记录`}
        hint="该节点可能被停用，或还没轮到它执行。"
      />
    );
  }

  const canResumeStage =
    stage.observed_state === 'failed' || stage.observed_state === 'blocked' || stage.observed_state === 'lost';

  const doResumeStage = async (): Promise<void> => {
    setBusy(true);
    setResumeError(null);
    try {
      const result = await taskApi.resumeStage(taskId, stage.node_id);
      setResumeNote(result.note);
      onChanged();
    } catch (err) {
      setResumeError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 'var(--sp-3)' }}>
      <div>
        <StageStatePill state={stage.observed_state} />
        <div className="text-xs muted" style={{ marginTop: 4 }}>
          {stage.status_reason ?? stageStateLabel(stage.observed_state).hint ?? ''}
        </div>
      </div>

      <KV
        items={[
          { k: '节点', v: nodeName ?? <ShortId id={stage.node_id} /> },
          { k: '阶段', v: <ShortId id={stage.stage_id} len={12} /> },
          { k: '目标状态', v: DESIRED_LABELS[stage.desired_state] },
          { k: '操作序号', v: <span className="mono">{stage.control_epoch}</span> },
          {
            k: '优先级',
            v: (
              <span className="mono">
                节点 {stage.node_priority} · 任务 {stage.task_priority}
              </span>
            ),
          },
          { k: '入队时间', v: <TimeText value={stage.enqueued_at} /> },
          {
            k: '尝试次数',
            v: (
              <span className="mono">
                {stage.attempt_count}（当前第 {stage.current_attempt_seq} 次 · 候选配置 {stage.profile_cursor}）
              </span>
            ),
          },
          {
            k: '断点',
            v: stage.checkpoint_ref ? <span className="mono">{stage.checkpoint_ref}</span> : <span className="dim">无（恢复时本阶段从头重跑）</span>,
          },
          {
            k: '需要核对',
            v: stage.requires_reconcile ? <span className="text-warn">是——无法确认它是否还在运行，请人工核对</span> : '否',
          },
          {
            k: '上游产物',
            v:
              Object.keys(stage.upstream_pins).length === 0 ? (
                <span className="dim">无上游</span>
              ) : (
                <div className="col" style={{ gap: 2 }}>
                  {Object.entries(stage.upstream_pins).map(([nodeId, ids]) => (
                    <span key={nodeId} className="text-xs mono">
                      {nodeId.slice(0, 8)} → {ids.length > 0 ? ids.map((i) => i.slice(0, 8)).join(', ') : '（无产物）'}
                    </span>
                  ))}
                </div>
              ),
          },
        ]}
      />

      {stage.blocked_reason ? (
        <Banner variant="warn" title="受阻 / 失败原因">
          {stage.blocked_reason}
        </Banner>
      ) : null}

      {stage.origin_of_control ? (
        <div className="text-xs muted">
          操作来源：{stage.origin_of_control.op} · 范围 {stage.origin_of_control.scope} ·{' '}
          {stage.origin_of_control.from_node_id ? `来自节点 ${stage.origin_of_control.from_node_id.slice(0, 8)}` : '任务级'} ·{' '}
          {new Date(stage.origin_of_control.at).toLocaleString('zh-CN', { hour12: false })}
          {stage.origin_of_control.detail ? ` · ${stage.origin_of_control.detail}` : ''}
        </div>
      ) : null}

      {canResumeStage ? (
        <div className="row row--tight">
          <button type="button" className="btn btn--sm btn--primary" disabled={busy} onClick={() => void doResumeStage()}>
            断点续跑该阶段
          </button>
          <span className="text-xs dim">只重跑这一个阶段。没有断点时会从头重跑。</span>
        </div>
      ) : null}
      {resumeNote ? <Banner variant="ok">{resumeNote}</Banner> : null}
      {resumeError ? <Banner variant="danger" title="续跑请求失败">{resumeError}</Banner> : null}

      {/* ---------------- 执行尝试 ---------------- */}
      <div>
        <div className="section-title">执行尝试 · {attempts.length}</div>
        {attempts.length === 0 ? (
          <div className="text-sm dim">该阶段尚未创建执行尝试。</div>
        ) : (
          <div className="col" style={{ gap: 6 }}>
            {attempts.map((attempt) => (
              <AttemptCard key={attempt.attempt_id} attempt={attempt} />
            ))}
          </div>
        )}
      </div>

      {/* ---------------- 产物 ---------------- */}
      <div>
        <div className="section-title">该阶段产物 · {artifacts.length}</div>
        {artifacts.length === 0 ? (
          <div className="text-sm dim">没有产物。</div>
        ) : (
          <div className="col" style={{ gap: 4 }}>
            {artifacts.map((a) => (
              <div
                key={a.artifact_id}
                style={{
                  border: `1px solid ${a.summary_ok ? 'var(--line)' : 'var(--st-danger)'}`,
                  background: a.summary_ok ? 'var(--bg-2)' : 'var(--st-danger-bg)',
                  borderRadius: 'var(--radius)',
                  padding: '5px 7px',
                }}
              >
                <div className="row row--tight">
                  <span className="mono text-xs">{(a.digest ?? a.artifact_id).slice(0, 12)}</span>
                  <Chip>{ARTIFACT_KIND_LABELS[a.kind]}</Chip>
                  <Chip variant={SENSITIVITY_LABELS[a.sensitivity].tone === 'warn' ? 'warn' : undefined}>
                    {SENSITIVITY_LABELS[a.sensitivity].text}
                  </Chip>
                  {!a.summary_ok ? (
                    <Chip variant="danger" title="摘要缺少下游要求的内容，下游节点会被暂停等待处理">
                      摘要不完整
                    </Chip>
                  ) : null}
                  {a.tombstoned ? <Chip variant="off">已清理</Chip> : null}
                  <span className="spacer" />
                  <Bytes value={a.size_bytes} />
                </div>
                {a.summary ? (
                  <div className="text-xs" style={{ marginTop: 3, color: a.summary_ok ? undefined : 'var(--st-danger)' }}>
                    {a.summary}
                  </div>
                ) : (
                  <div className="text-xs dim" style={{ marginTop: 3 }}>
                    没有摘要，下游只能拿到产物引用。
                  </div>
                )}
                {a.covered_fields.length > 0 ? (
                  <div className="text-xs dim" style={{ marginTop: 2 }}>
                    已覆盖：{a.covered_fields.join(', ')}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ---------------- 审批 ---------------- */}
      {approvals.length > 0 ? (
        <div>
          <div className="section-title">该阶段的审批 · {approvals.length}</div>
          <div className="col" style={{ gap: 6 }}>
            {approvals.map((approval) => (
              <ApprovalCard key={approval.approval_id} approval={approval} onChanged={onChanged} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function AttemptCard({ attempt }: { attempt: Attempt }): JSX.Element {
  const duration = durationBetween(attempt.started_at, attempt.ended_at);
  const outcome = attempt.outcome;
  const snapshot = attempt.profile_snapshot;
  const model = typeof snapshot['model_name'] === 'string' ? snapshot['model_name'] : null;
  const harness = typeof snapshot['harness_ref'] === 'string' ? snapshot['harness_ref'] : null;
  const effort = typeof snapshot['reasoning_effort'] === 'string' ? snapshot['reasoning_effort'] : null;
  const threshold = typeof snapshot['effective_compact_threshold'] === 'number' ? snapshot['effective_compact_threshold'] : null;

  return (
    <div
      style={{
        border: '1px solid var(--line)',
        borderRadius: 'var(--radius)',
        background: 'var(--bg-2)',
        padding: '6px 8px',
      }}
    >
      <div className="row row--tight">
        <span className="chip">#{attempt.attempt_seq}</span>
        <span className="mono text-xs" title={attempt.profile_id}>
          {model ?? '（未记录模型）'}
        </span>
        {harness ? <span className="dim mono text-xs">@{harness}</span> : null}
        {effort ? <Chip>effort {effort}</Chip> : null}
        {attempt.reattached ? <Chip variant="warn">重启后接回</Chip> : null}
        {attempt.resume_from_checkpoint ? <Chip variant="accent">从断点续跑</Chip> : null}
        <span className="spacer" />
        {outcome ? (
          <Chip variant={outcome.error_class === 'success' ? 'accent' : 'danger'} title={outcome.detail ?? undefined}>
            {ERROR_CLASS_LABELS[outcome.error_class].text}
            {outcome.error_kind ? ` · ${outcome.error_kind}` : ''}
          </Chip>
        ) : (
          <Chip variant="warn">尚无结论（可能仍在运行）</Chip>
        )}
      </div>

      <div className="text-xs dim" style={{ marginTop: 4 }}>
        <TimeText value={attempt.started_at} /> → {attempt.ended_at ? <TimeText value={attempt.ended_at} /> : '进行中'}
        {' · 用时 '}
        {duration === null ? <span className="dim">未知</span> : humanDuration(duration)}
        {attempt.session_ref ? (
          <>
            {' · 会话 '}
            {/* 链到会话台账：这里只给引用，是否还活着由台账回答（不在此处猜）。 */}
            <a
              href={`#/sessions?task_id=${encodeURIComponent(attempt.task_id)}`}
              title={`在会话台账里查看 ${attempt.session_ref}`}
              className="mono"
            >
              {attempt.session_ref.slice(0, 12)}
            </a>
          </>
        ) : null}
        {attempt.lease_id ? (
          <>
            {' · 租约 '}
            <span className="mono">{attempt.lease_id.slice(0, 8)}</span>
            {' gen '}
            {attempt.generation}
          </>
        ) : null}
      </div>

      <div className="text-xs" style={{ marginTop: 3 }}>
        用量：输入 <CountOrUnknown value={attempt.usage?.input_tokens ?? null} /> · 输出{' '}
        <CountOrUnknown value={attempt.usage?.output_tokens ?? null} /> · 缓存读{' '}
        <CountOrUnknown value={attempt.usage?.cache_read_tokens ?? null} /> · 缓存写{' '}
        <CountOrUnknown value={attempt.usage?.cache_write_tokens ?? null} />
        {attempt.usage?.cost_estimate != null ? (
          <>
            {' · 估算成本 '}
            <span className="mono">{attempt.usage.cost_estimate}</span>
            {attempt.usage.cost_basis ? <span className="dim">（{attempt.usage.cost_basis}）</span> : null}
          </>
        ) : null}
      </div>
      {attempt.usage?.notes ? <div className="text-xs dim">用量备注：{attempt.usage.notes}</div> : null}

      {threshold !== null ? (
        <div className="text-xs dim" style={{ marginTop: 2 }}>
          上下文整理触发点：{threshold}（取用户阈值与 harness 上限中较小的值，再留安全余量）
        </div>
      ) : null}

      {outcome?.detail ? (
        <div className="text-xs" style={{ marginTop: 3, color: 'var(--st-danger)' }}>
          {outcome.detail}
        </div>
      ) : null}

      <div className="section-title" style={{ marginTop: 6 }}>
        上下文整理记录 · {attempt.compact_events.length}
      </div>
      {attempt.compact_events.length === 0 ? (
        <div className="text-xs dim">
          这次尝试没有发生上下文整理（若配置了阈值却从未触发，检查 harness 是否支持该能力）。
        </div>
      ) : (
        <ul className="list-reset text-xs">
          {attempt.compact_events.map((event, i) => (
            <li key={i} className="mono">
              {new Date(event.at).toLocaleTimeString('zh-CN', { hour12: false })} · {event.trigger} ·{' '}
              {event.ok ? '成功' : '失败'} · {event.tokens_before ?? '未知'} → {event.tokens_after ?? '未知'}
              {event.effective_threshold !== null ? `（阈值 ${event.effective_threshold}）` : ''}
              {event.detail ? ` · ${event.detail}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
