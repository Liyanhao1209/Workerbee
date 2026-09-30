/**
 * 任务详情（RUN-07 硬要求页）。
 *
 * RUN-07 的「同时显示」在这里是三条并列的事实，缺一不可：
 * 1. 每个阶段的状态；
 * 2. 失败原因（含可定位的阶段）；
 * 3. **仍然在运行的分支**——失败的主链不会自动停掉它们。
 *
 * 另外三件必须如实呈现的事：
 * - 每次 Attempt 的候选/模型/harness/用量/耗时/上下文整理记录；
 * - 产物摘要质量门禁：`summary_ok=false` 是**交接失败**，要红得很显眼；
 * - ContextPackage 的 P1–P5 组装记录（来自 `context.assembled` 事件）。
 *
 * 用量未知时显示「未知」而不是 0（OBS-04）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import type { ArtifactRecord, EventPage, EventRecord, TaskDetail, TaskStage } from '../api/types';
import { tasks as taskApi } from '../api/endpoints';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { asArray, asString, isRecord } from '../api/guards';
import { AttemptCard } from './ExecutionGraphPage';
import { TaskControls, OriginOfControlText, ErrorBanner } from '../components/TaskControls';
import { ApprovalCard } from '../components/Approval';
import { NodeQueue } from '../components/NodeQueue';
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
} from '../components/common';
import {
  ARTIFACT_KIND_LABELS,
  DESIRED_LABELS,
  ERROR_CLASS_LABELS,
  EVENT_ACTOR_LABELS,
  EVENT_SCOPE_LABELS,
  EVENT_TYPE_LABELS,
  PARTITION_LABELS,
  SENSITIVITY_LABELS,
  stageStateLabel,
  taskStateLabel,
} from '../labels';

export function TaskDetailPage(): JSX.Element {
  const { taskId = '' } = useParams<{ taskId: string }>();
  const fetchDetail = useCallback(() => taskApi.get(taskId), [taskId]);
  const { data, loading, error, loaded, reload } = useAsync(fetchDetail, [taskId], { pollMs: 4000 });
  const [queueNodeId, setQueueNodeId] = useState<string | null>(null);

  const task = data?.task ?? null;

  const stages = useMemo(() => data?.stages ?? [], [data]);
  const attempts = useMemo(() => data?.attempts ?? [], [data]);

  /** 仍在运行的分支：任务的 failure_summary 与阶段事实取并集，避免任一侧缺失导致漏报。 */
  const runningBranches = useMemo(() => {
    const fromSummary = asArray<string>(task?.failure_summary?.['running_branches']);
    const fromStages = stages
      .filter(
        (s) =>
          s.observed_state === 'running' ||
          s.observed_state === 'dispatching' ||
          s.observed_state === 'retrying',
      )
      .map((s) => s.node_id);
    return Array.from(new Set([...fromSummary, ...fromStages]));
  }, [task, stages]);

  if (loading && !loaded) return <Loading label="加载任务详情" />;

  if (error && !task) {
    return (
      <div className="page">
        <ErrorBanner error={error} />
        <Link to="/tasks" className="btn btn--sm">
          返回任务列表
        </Link>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="page">
        <Empty title="没有读到任务" hint="服务返回了空内容，或这个任务已被清理。" />
      </div>
    );
  }

  const snapshot = task.graph_snapshot;
  const artifacts = data?.artifacts ?? [];
  const approvals = data?.approvals ?? [];
  const brokenArtifacts = artifacts.filter((a) => !a.summary_ok && !a.tombstoned);
  const failedStages = stages.filter((s) => s.observed_state === 'failed' || s.observed_state === 'blocked');
  const queueNode =
    (queueNodeId ? snapshot.graph.nodes.find((n) => n.node_id === queueNodeId) : undefined) ??
    snapshot.graph.nodes[0] ??
    null;

  return (
    <div className="page page--wide">
      {/* ------------------------------ 头部 ------------------------------ */}
      <div className="page-head">
        <div className="page-head__titles">
          <h1 className="row row--tight" style={{ gap: 'var(--sp-2)' }}>
            任务
            <TaskStatePill state={task.observed_state} />
            <span className="mono text-sm dim">
              <ShortId id={task.task_id} len={16} />
            </span>
          </h1>
          <div className="page-head__sub">
            {task.workflow_name ?? <ShortId id={task.workflow_id} />} · 修订 #{task.revision_seq} · 图版本{' '}
            {task.effective_graph_version} · 优先级 {task.priority} · 提交者 {task.submitted_by} ·{' '}
            <TimeText value={task.created_at ?? null} />
          </div>
          <OriginOfControlText task={task} />
        </div>
        <div className="page-head__actions">
          <Link className="btn btn--sm" to={`/execution/${task.task_id}`}>
            打开执行图
          </Link>
          <button type="button" className="btn btn--sm" onClick={reload}>
            刷新
          </button>
        </div>
      </div>

      <div className="row row--tight" style={{ marginBottom: 'var(--sp-3)' }}>
        <TaskControls task={task} onChanged={reload} />
        <span className="text-xs dim">
          控制范围是<strong>整次任务</strong>。暂停或删除不会改变已完成阶段的结果。
        </span>
      </div>

      {error ? (
        <Banner variant="warn" title="最近一次刷新失败，显示的是上一次读到的内容">
          {error.detail}
        </Banner>
      ) : null}

      {/* ------------------------------ RUN-07：三件事同时显示 ------------------------------ */}
      <RunOverview
        task={task}
        stages={stages}
        runningBranches={runningBranches}
        failedStages={failedStages}
        onReload={reload}
      />

      {/* ------------------------------ 产物 ------------------------------ */}
      {brokenArtifacts.length > 0 ? (
        <Banner
          variant="danger"
          title={`${brokenArtifacts.length} 项产物的摘要不合格（交接失败）`}
          hint="摘要没有覆盖下游需要的要点时，下游会被挡住，直到材料补齐。这不代表任务本身失败。"
        >
          涉及产物：
          {brokenArtifacts.map((a) => (
            <span key={a.artifact_id} className="mono text-xs" style={{ marginRight: 8 }}>
              {(a.digest ?? a.artifact_id).slice(0, 10)}
              {a.producer?.node_id ? `（节点 ${a.producer.node_id.slice(0, 6)}）` : ''}
            </span>
          ))}
        </Banner>
      ) : null}

      <div className="panel">
        <div className="panel__head">
          产物 · {artifacts.length}
          {brokenArtifacts.length > 0 ? <span className="chip chip--danger">摘要不合格 {brokenArtifacts.length}</span> : null}
        </div>
        <div className="panel__body">
          {artifacts.length === 0 ? (
            <Empty title="还没有产物" hint="阶段完成后才产生产物。" />
          ) : (
            <div className="col" style={{ gap: 6 }}>
              {artifacts.map((artifact) => (
                <ArtifactRow key={artifact.artifact_id} artifact={artifact} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------ 执行尝试 ------------------------------ */}
      <div className="panel">
        <div className="panel__head">
          执行尝试 · {attempts.length}
          <div className="panel__head-actions">
            <span className="text-xs dim">按阶段分组；用量未知时显示「未知」</span>
          </div>
        </div>
        <div className="panel__body">
          {attempts.length === 0 ? (
            <Empty title="还没有执行尝试" hint="阶段开始执行后才会产生记录。" />
          ) : (
            <div className="col" style={{ gap: 'var(--sp-4)' }}>
              {stages.map((stage) => {
                const list = attempts.filter((a) => a.stage_id === stage.stage_id);
                if (list.length === 0) return null;
                return (
                  <div key={stage.stage_id}>
                    <div className="row row--tight">
                      <StageStatePill state={stage.observed_state} />
                      <span>{stage.node_name ?? <ShortId id={stage.node_id} />}</span>
                      <span className="text-xs dim">
                        阶段 <ShortId id={stage.stage_id} len={10} /> · {list.length} 次尝试
                      </span>
                    </div>
                    <div className="col" style={{ gap: 6, marginTop: 6 }}>
                      {list.map((attempt) => (
                        <AttemptCard key={attempt.attempt_id} attempt={attempt} />
                      ))}
                    </div>
                  </div>
                );
              })}
              {attempts.every((a) => !stages.some((s) => s.stage_id === a.stage_id)) ? (
                <div className="text-sm dim">
                  有 {attempts.length} 条尝试记录，但它们对应的阶段不在本次详情的阶段列表里（可能是历史记录）。
                </div>
              ) : null}
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------ 审批 ------------------------------ */}
      <div className="panel">
        <div className="panel__head">
          审批 · {approvals.length}
          {approvals.some((a) => a.status === 'pending') ? (
            <span className="chip chip--warn">
              待处理 {approvals.filter((a) => a.status === 'pending').length}
            </span>
          ) : null}
          {approvals.some((a) => a.status === 'undeliverable') ? (
            <span className="chip chip--danger">回注失败</span>
          ) : null}
        </div>
        <div className="panel__body">
          {approvals.length === 0 ? (
            <Empty
              title="没有审批请求"
              hint="只在节点配置了需要审批的操作时产生。超时后按拒绝处理，任务暂停。"
            />
          ) : (
            <div className="col" style={{ gap: 'var(--sp-3)' }}>
              {approvals.map((approval) => (
                <ApprovalCard key={approval.approval_id} approval={approval} onChanged={reload} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------ 节点队列 ------------------------------ */}
      <div className="panel">
        <div className="panel__head">
          节点队列
          <div className="panel__head-actions">
            <select
              className="select select--sm"
              value={queueNode?.node_id ?? ''}
              onChange={(e) => setQueueNodeId(e.target.value || null)}
            >
              {snapshot.graph.nodes.map((node) => (
                <option key={node.node_id} value={node.node_id}>
                  {node.name}
                  {node.enabled ? '' : '（已停用）'}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="panel__body">
          {queueNode ? (
            <NodeQueue nodeId={queueNode.node_id} nodeName={queueNode.name} />
          ) : (
            <Empty title="任务快照里没有节点" />
          )}
        </div>
      </div>

      {/* ------------------------------ ContextPackage ------------------------------ */}
      <ContextPackagePanel taskId={task.task_id} />

      {/* ------------------------------ 事件时间线 ------------------------------ */}
      <EventTimeline taskId={task.task_id} onRefreshRequested={reload} />

      {/* ------------------------------ 输入与 pinned 快照 ------------------------------ */}
      <div className="panel">
        <div className="panel__head">输入与执行快照</div>
        <div className="panel__body">
          <KV
            items={[
              { k: '去重标识', v: task.idempotency_key ? <span className="mono">{task.idempotency_key}</span> : <span className="dim">未提供</span> },
              { k: '期望状态', v: DESIRED_LABELS[task.desired_state] },
              { k: '控制版本号', v: <span className="mono">{task.control_epoch}</span> },
              { k: '最后更新', v: <TimeText value={task.updated_at ?? null} /> },
              {
                k: '执行快照的边',
                v: (
                  <span className="mono">
                    {snapshot.effective_edges.length} 条（有效图版本 {snapshot.effective_graph_version}）
                  </span>
                ),
              },
              {
                k: '执行快照的节点',
                v: (
                  <span className="mono">
                    {snapshot.graph.nodes.length} 个（其中停用{' '}
                    {snapshot.graph.nodes.filter((n) => !n.enabled).length} 个，停用节点不参与执行）
                  </span>
                ),
              },
              { k: '受阻原因', v: task.blocked_reason ?? <span className="dim">—</span> },
            ]}
          />
          <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
            输入载荷
          </div>
          <pre className="code-block" style={{ maxHeight: 240, overflow: 'auto' }}>
            {JSON.stringify(task.input_payload, null, 2)}
          </pre>
          <div className="text-xs dim" style={{ marginTop: 4 }}>
            任务启动时锁定了当时的流程图与依赖关系；之后再修改流程定义，不会影响本次执行。
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RUN-07 总览：阶段状态 / 失败原因 / 仍在运行的分支，三者并列
// ---------------------------------------------------------------------------

function RunOverview({
  task,
  stages,
  runningBranches,
  failedStages,
  onReload,
}: {
  task: TaskDetail['task'];
  stages: TaskStage[];
  runningBranches: string[];
  failedStages: TaskStage[];
  onReload: () => void;
}): JSX.Element {
  const summary = task.failure_summary;
  const reason = typeof summary?.['reason'] === 'string' ? summary['reason'] : null;
  const detail = typeof summary?.['detail'] === 'string' ? summary['detail'] : null;
  const errorClass = typeof summary?.['error_class'] === 'string' ? summary['error_class'] : null;

  const stateCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const stage of stages) {
      map.set(stage.observed_state, (map.get(stage.observed_state) ?? 0) + 1);
    }
    return map;
  }, [stages]);

  return (
    <div className="panel">
      <div className="panel__head">
        运行总览
        <div className="panel__head-actions">
          <span className="chip">{stages.length} 个阶段</span>
          {failedStages.length > 0 ? <span className="chip chip--danger">失败/受阻 {failedStages.length}</span> : null}
          {runningBranches.length > 0 ? (
            <span className="chip chip--accent">仍在运行 {runningBranches.length}</span>
          ) : null}
        </div>
      </div>
      <div className="panel__body">
        <div className="grid grid--3">
          {/* ① 阶段状态 */}
          <div>
            <div className="section-title">① 每个阶段的状态</div>
            <div className="chips">
              {Array.from(stateCounts.entries()).map(([state, count]) => (
                <Chip key={state} title={stageStateLabel(state as TaskStage['observed_state']).hint}>
                  {stageStateLabel(state as TaskStage['observed_state']).text} {count}
                </Chip>
              ))}
              {stateCounts.size === 0 ? <span className="dim text-sm">还没有阶段记录。</span> : null}
            </div>
          </div>

          {/* ② 失败原因 */}
          <div>
            <div className="section-title">② 失败 / 受阻原因</div>
            {reason || detail || errorClass ? (
              <div className="text-sm">
                {errorClass ? (
                  <Chip variant="danger">
                    {ERROR_CLASS_LABELS[errorClass as keyof typeof ERROR_CLASS_LABELS]?.text ?? errorClass}
                  </Chip>
                ) : null}
                <div style={{ marginTop: 4 }}>{reason ?? '未给出文字原因。'}</div>
                {detail ? <div className="text-xs muted">{detail}</div> : null}
              </div>
            ) : (
              <div className="text-sm dim">
                {task.observed_state === 'succeeded'
                  ? '任务已成功结束，没有失败记录。'
                  : '没有失败摘要，请看下方的阶段明细。'}
              </div>
            )}
            {failedStages.length > 0 ? (
              <div className="col" style={{ gap: 3, marginTop: 6 }}>
                {failedStages.map((stage) => (
                  <div key={stage.stage_id} className="text-xs">
                    <StageStatePill state={stage.observed_state} />{' '}
                    <span>{stage.node_name ?? stage.node_id.slice(0, 8)}</span>
                    <div className="muted" style={{ marginLeft: 2 }}>
                      {stage.blocked_reason ?? '（未提供原因）'}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {/* ③ 仍在运行的分支 */}
          <div>
            <div className="section-title">③ 仍在运行的分支</div>
            {runningBranches.length === 0 ? (
              <div className="text-sm dim">当前没有阶段在运行。</div>
            ) : (
              <>
                <div className="chips">
                  {runningBranches.map((nodeId) => {
                    const stage = stages.find((s) => s.node_id === nodeId);
                    return (
                      <Chip key={nodeId} variant="accent" title={stage ? stage.status_reason ?? undefined : undefined}>
                        {stage?.node_name ?? nodeId.slice(0, 8)}
                        {stage ? ` · ${stageStateLabel(stage.observed_state).text}` : ''}
                      </Chip>
                    );
                  })}
                </div>
                <div className="text-xs muted" style={{ marginTop: 6 }}>
                  这些分支不会因主链失败而自动停止，仍在占用执行资源。
                </div>
              </>
            )}
          </div>
        </div>

        {/* 阶段明细表：状态 + 原因 + 单阶段恢复入口 */}
        <div className="table-wrap" style={{ marginTop: 'var(--sp-3)' }}>
          <table className="table table--dense">
            <thead>
              <tr>
                <th>节点</th>
                <th>状态</th>
                <th>说明 / 原因</th>
                <th className="table__num">尝试</th>
                <th>入队</th>
                <th>控制</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {stages.length === 0 ? (
                <tr>
                  <td colSpan={7} className="dim">
                    还没有阶段记录。
                  </td>
                </tr>
              ) : (
                stages.map((stage) => <StageRow key={stage.stage_id} taskId={task.task_id} stage={stage} onChanged={onReload} />)
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function StageRow({
  taskId,
  stage,
  onChanged,
}: {
  taskId: string;
  stage: TaskStage;
  onChanged: () => void;
}): JSX.Element {
  const submit = useSubmit();
  const [note, setNote] = useState<string | null>(null);
  const resumable =
    stage.observed_state === 'failed' || stage.observed_state === 'blocked' || stage.observed_state === 'lost';

  const doResume = async (): Promise<void> => {
    const result = await submit.run(() => taskApi.resumeStage(taskId, stage.node_id));
    if (result) {
      setNote(`${result.note}${result.had_checkpoint ? '' : '（无断点：本阶段从头重跑）'}`);
      onChanged();
    }
  };

  return (
    <tr>
      <td>
        {stage.node_name ?? <ShortId id={stage.node_id} />}
        <div className="text-xs dim mono" title={stage.node_id}>
          {stage.node_id.slice(0, 10)}
        </div>
      </td>
      <td>
        <StageStatePill state={stage.observed_state} />
        <div className="text-xs dim">{DESIRED_LABELS[stage.desired_state]}</div>
      </td>
      <td style={{ maxWidth: 420 }}>
        {stage.blocked_reason ? (
          <span className="text-danger text-sm">{stage.blocked_reason}</span>
        ) : stage.status_reason ? (
          <span className="text-sm muted">{stage.status_reason}</span>
        ) : (
          <span className="dim">—</span>
        )}
        {note ? <div className="text-xs text-success">{note}</div> : null}
        {submit.error ? <div className="text-xs text-danger">{submit.error.detail}</div> : null}
      </td>
      <td className="table__num">
        {stage.attempt_count}
        {stage.requires_reconcile ? (
          <div className="text-xs text-warn" title="后台服务无法确认该阶段的实际状态，请人工核对">
            需核对
          </div>
        ) : null}
      </td>
      <td>
        <RelTime value={stage.enqueued_at} />
      </td>
      <td className="text-xs">
        {stage.origin_of_control ? (
          <>
            {stage.origin_of_control.op}
            {stage.origin_of_control.from_node_id ? ` ← ${stage.origin_of_control.from_node_id.slice(0, 6)}` : ''}
          </>
        ) : (
          <span className="dim">—</span>
        )}
      </td>
      <td>
        {resumable ? (
          <button
            type="button"
            className="btn btn--xs"
            disabled={submit.busy}
            onClick={() => void doResume()}
            title="只重跑这一个阶段"
          >
            续跑
          </button>
        ) : null}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// 产物
// ---------------------------------------------------------------------------

function ArtifactRow({ artifact }: { artifact: ArtifactRecord }): JSX.Element {
  const bad = !artifact.summary_ok;
  // 任务详情端点只投影部分字段（没有 digest / lineage / ref_count 等）：
  // 取不到的显示 artifact_id 或「未知」，**不补 0**。
  const label = artifact.digest ?? artifact.artifact_id;
  const lineage = artifact.lineage ?? [];
  return (
    <div
      style={{
        border: `1px solid ${bad ? 'var(--st-danger)' : 'var(--line)'}`,
        background: bad ? 'var(--st-danger-bg)' : 'var(--bg-2)',
        borderRadius: 'var(--radius)',
        padding: '7px 9px',
      }}
    >
      <div className="row row--tight">
        <span
          className="mono text-xs"
          title={artifact.digest ? `digest ${artifact.digest}` : `artifact_id ${artifact.artifact_id}（该响应未带 digest）`}
        >
          {label.slice(0, 14)}
        </span>
        <Chip>{ARTIFACT_KIND_LABELS[artifact.kind]}</Chip>
        <Chip
          variant={SENSITIVITY_LABELS[artifact.sensitivity].tone === 'warn' ? 'warn' : undefined}
        >
          {SENSITIVITY_LABELS[artifact.sensitivity].text}
        </Chip>
        {bad ? <Chip variant="danger">摘要不合格 · 交接失败</Chip> : <Chip variant="accent">摘要合格</Chip>}
        {artifact.tombstoned ? <Chip variant="off">已清理</Chip> : null}
        <span className="spacer" />
        <span className="text-xs dim">
          被引用 <CountOrUnknown value={artifact.ref_count ?? null} /> 次 · <Bytes value={artifact.size_bytes} /> · ~
          <CountOrUnknown value={artifact.token_estimate ?? null} /> token
        </span>
      </div>

      {artifact.summary ? (
        <div
          className="text-sm"
          style={{ marginTop: 4, color: bad ? 'var(--st-danger)' : undefined }}
        >
          {artifact.summary}
        </div>
      ) : (
        <div className="text-sm dim" style={{ marginTop: 4 }}>
          没有摘要——下游只能沿引用读取原文。
        </div>
      )}

      <div className="text-xs dim" style={{ marginTop: 3 }}>
        {artifact.producer ? (
          <>
            产出者：节点 {artifact.producer.node_id?.slice(0, 8) ?? '未知'} · 阶段{' '}
            {artifact.producer.stage_id.slice(0, 8)} · 第 {artifact.producer.attempt_seq} 次尝试
          </>
        ) : (
          '产出者：未记录'
        )}
        {artifact.covered_fields.length > 0 ? ` · 已覆盖字段：${artifact.covered_fields.join(', ')}` : ''}
        {artifact.media_type ? ` · ${artifact.media_type}` : ''}
        {artifact.storage_path ? (
          <>
            {' · '}
            <span className="mono">{artifact.storage_path}</span>
          </>
        ) : null}
      </div>

      {lineage.length > 0 ? (
        <div className="text-xs dim" style={{ marginTop: 2 }}>
          来源链：{lineage.map((l) => l.slice(0, 8)).join(' → ')}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ContextPackage 组装记录（P1–P5）
// ---------------------------------------------------------------------------

interface PartitionRecord {
  key: string;
  label: string;
  ratio: number | null;
  budget_tokens: number | null;
  used_tokens: number | null;
  items: number | null;
  truncated: boolean;
  borrowed_from_reserve: number | null;
  degraded: string[];
}

interface SourceRecord {
  from_node_id: string | null;
  to_node_id: string | null;
  artifact_id: string | null;
  digest: string | null;
  producer_label: string | null;
  sensitivity: string | null;
}

interface AssemblyRecord {
  eventId: number;
  at: string;
  stageId: string | null;
  attemptSeq: number | null;
  partitions: PartitionRecord[];
  degraded: string[];
  excluded: string[];
  handoffFailures: string[];
  totalTokens: number | null;
  budgetTokens: number | null;
  budgetBasis: string | null;
  systemPromptSource: string | null;
  contentRecorded: boolean;
  sources: SourceRecord[];
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function normalizeAssembly(event: EventRecord): AssemblyRecord | null {
  const payload = event.payload;
  const rawPartitions = payload['partitions'];
  if (!isRecord(rawPartitions)) return null;
  const partitions: PartitionRecord[] = Object.entries(rawPartitions).map(([key, raw]) => {
    const rec = isRecord(raw) ? raw : {};
    return {
      key,
      label: asString(rec['label'], PARTITION_LABELS[key] ?? key),
      ratio: num(rec['ratio']),
      budget_tokens: num(rec['budget_tokens']),
      used_tokens: num(rec['used_tokens']),
      items: num(rec['items']),
      truncated: rec['truncated'] === true,
      borrowed_from_reserve: num(rec['borrowed_from_reserve']),
      degraded: asArray<string>(rec['degraded']),
    };
  });
  partitions.sort((a, b) => a.key.localeCompare(b.key));
  const sources: SourceRecord[] = asArray<unknown>(payload['sources']).map((raw) => {
    const rec = isRecord(raw) ? raw : {};
    return {
      from_node_id: typeof rec['from_node_id'] === 'string' ? rec['from_node_id'] : null,
      to_node_id: typeof rec['to_node_id'] === 'string' ? rec['to_node_id'] : null,
      artifact_id: typeof rec['artifact_id'] === 'string' ? rec['artifact_id'] : null,
      digest: typeof rec['digest'] === 'string' ? rec['digest'] : null,
      producer_label: typeof rec['producer_label'] === 'string' ? rec['producer_label'] : null,
      sensitivity: typeof rec['sensitivity'] === 'string' ? rec['sensitivity'] : null,
    };
  });
  return {
    eventId: event.event_id,
    at: event.ts,
    stageId: event.stage_id,
    attemptSeq: num(payload['attempt_seq']),
    partitions,
    degraded: asArray<string>(payload['degraded']),
    excluded: asArray<string>(payload['excluded']),
    handoffFailures: asArray<string>(payload['handoff_failures']),
    totalTokens: num(payload['total_tokens_estimate']),
    budgetTokens: num(payload['budget_tokens']),
    budgetBasis: asString(payload['budget_basis']) || null,
    systemPromptSource: asString(payload['system_prompt_source']) || null,
    contentRecorded: payload['content_recorded'] === true,
    sources,
  };
}

function ContextPackagePanel({ taskId }: { taskId: string }): JSX.Element {
  const [records, setRecords] = useState<AssemblyRecord[]>([]);
  const [page, setPage] = useState<EventPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setRecords([]);
    taskApi
      .events(taskId, { limit: 500 })
      .then((result) => {
        if (cancelled) return;
        setPage(result);
        setRecords(
          result.events
            .filter((e) => e.type === 'context.assembled')
            .map(normalizeAssembly)
            .filter((r): r is AssemblyRecord => r !== null),
        );
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err : null);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  return (
    <div className="panel">
      <div className="panel__head">
        发给模型的材料 · 组装记录
        <div className="panel__head-actions">
          {records.length > 0 ? <span className="chip">{records.length} 次组装</span> : null}
          {page?.has_more ? <span className="chip chip--warn">事件未读全，记录可能不全</span> : null}
        </div>
      </div>
      <div className="panel__hint">
        每次开始执行一个阶段时，发给模型的材料。「降级」不为空，说明这次材料被压缩过。
      </div>
      <div className="panel__body">
        {loading ? (
          <Loading label="加载组装记录" />
        ) : error ? (
          <Banner variant="danger" title="无法读取事件日志">
            组装记录来自事件日志，当前读取失败，请稍后重试。
          </Banner>
        ) : records.length === 0 ? (
          <Empty
            title="没有组装记录"
            hint="任务还没有开始执行任何阶段，或历史记录已被清理。"
          />
        ) : (
          <div className="col" style={{ gap: 'var(--sp-3)' }}>
            {records
              .slice()
              .reverse()
              .map((record) => (
                <div
                  key={record.eventId}
                  style={{
                    border: '1px solid var(--line)',
                    borderRadius: 'var(--radius)',
                    background: 'var(--bg-2)',
                    padding: '7px 9px',
                  }}
                >
                  <div className="row row--tight">
                    <span className="mono text-xs" title={`event_id=${record.eventId}`}>
                      #{record.eventId}
                    </span>
                    <TimeText value={record.at} />
                    {record.stageId ? (
                      <span className="text-xs dim mono">阶段 {record.stageId.slice(0, 8)}</span>
                    ) : null}
                    {record.attemptSeq !== null ? (
                      <span className="text-xs dim">第 {record.attemptSeq} 次尝试</span>
                    ) : null}
                    <span className="spacer" />
                    {record.systemPromptSource === 'node' ? (
                      <Chip>系统提示词：节点自定义</Chip>
                    ) : (
                      <Chip>系统提示词：默认组装</Chip>
                    )}
                    {record.contentRecorded ? <Chip variant="accent">正文已留档</Chip> : <Chip variant="off">未留正文</Chip>}
                  </div>

                  {record.handoffFailures.length > 0 ? (
                    <Banner variant="danger" title="交接失败，这个阶段因此受阻">
                      <ul className="list-reset text-xs">
                        {record.handoffFailures.map((f, i) => (
                          <li key={i}>· {f}</li>
                        ))}
                      </ul>
                    </Banner>
                  ) : null}

                  <div className="table-wrap" style={{ marginTop: 6 }}>
                    <table className="table table--dense">
                      <thead>
                        <tr>
                          <th style={{ width: 44 }}>段</th>
                          <th>名称</th>
                          <th className="table__num">占比</th>
                          <th className="table__num">预算 token</th>
                          <th className="table__num">实际 token</th>
                          <th className="table__num">条目</th>
                          <th>状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {record.partitions.map((p) => {
                          const over =
                            p.used_tokens !== null && p.budget_tokens !== null && p.used_tokens > p.budget_tokens;
                          return (
                            <tr key={p.key}>
                              <td className="mono">{p.key}</td>
                              <td>{p.label}</td>
                              <td className="table__num">{p.ratio === null ? '—' : `${Math.round(p.ratio * 100)}%`}</td>
                              <td className="table__num">{p.budget_tokens ?? '—'}</td>
                              <td className="table__num" style={{ color: over ? 'var(--st-danger)' : undefined }}>
                                {p.used_tokens ?? '—'}
                              </td>
                              <td className="table__num">{p.items ?? '—'}</td>
                              <td className="text-xs">
                                {over ? <Chip variant="danger">超支</Chip> : null}
                                {p.truncated ? <Chip variant="warn">已截断</Chip> : null}
                                {p.borrowed_from_reserve ? (
                                  <Chip variant="warn">借用预留额度 {p.borrowed_from_reserve}</Chip>
                                ) : null}
                                {p.degraded.length > 0 ? (
                                  <Chip variant="warn" title={p.degraded.join('；')}>
                                    降级 {p.degraded.length} 项
                                  </Chip>
                                ) : null}
                                {!over && !p.truncated && !p.borrowed_from_reserve && p.degraded.length === 0 ? (
                                  <span className="dim">正常</span>
                                ) : null}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  <div className="text-xs dim" style={{ marginTop: 4 }}>
                    合计估算 <CountOrUnknown value={record.totalTokens} /> token · 预算{' '}
                    <CountOrUnknown value={record.budgetTokens} />
                    {record.budgetBasis ? `（${record.budgetBasis}）` : ''}
                  </div>

                  {record.degraded.length > 0 ? (
                    <div className="text-xs text-warn" style={{ marginTop: 2 }}>
                      降级记录：{record.degraded.join('；')}
                    </div>
                  ) : null}
                  {record.excluded.length > 0 ? (
                    <div className="text-xs muted" style={{ marginTop: 2 }}>
                      被排除的来源：{record.excluded.join('；')}
                    </div>
                  ) : null}

                  {record.sources.length > 0 ? <SourceDisclosure sources={record.sources} /> : null}
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 事件时间线
// ---------------------------------------------------------------------------

const INTERESTING_TYPES = new Set([
  'task.state_changed',
  'task.control',
  'stage.state_changed',
  'stage.retry',
  'stage.candidate_switched',
  'stage.contract_violation',
  'attempt.discarded_late',
  'handoff.failed',
  'approval.requested',
  'approval.decided',
  'approval.invalidated',
  'approval.undeliverable',
  'session.lost',
  'resource.teardown_failed',
  'resource.orphaned',
  'reconcile.started',
  'reconcile.result',
  'task.completed',
]);

function EventTimeline({
  taskId,
  onRefreshRequested,
}: {
  taskId: string;
  onRefreshRequested: () => void;
}): JSX.Element {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [page, setPage] = useState<EventPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [newestFirst, setNewestFirst] = useState(true);
  const [onlyImportant, setOnlyImportant] = useState(false);
  const [filter, setFilter] = useState('');
  const [expanded, setExpanded] = useState<number | null>(null);

  const loadFrom = useCallback(
    async (afterId: number, replace: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const result = await taskApi.events(taskId, { after_id: afterId, limit: 200 });
        setPage(result);
        setEvents((prev) => {
          if (replace) return result.events;
          const seen = new Set(prev.map((e) => e.event_id));
          return [...prev, ...result.events.filter((e) => !seen.has(e.event_id))];
        });
      } catch (err) {
        setError(err instanceof ApiError ? err : null);
      } finally {
        setLoading(false);
      }
    },
    [taskId],
  );

  useEffect(() => {
    setEvents([]);
    setPage(null);
    void loadFrom(0, true);
  }, [loadFrom]);

  const maxSeen = events.length > 0 ? events[events.length - 1]?.event_id ?? 0 : 0;
  const unseen = page ? Math.max(0, page.latest_event_id - maxSeen) : 0;

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const list = events.filter((event) => {
      if (onlyImportant && !INTERESTING_TYPES.has(event.type)) return false;
      if (!needle) return true;
      return (
        event.type.toLowerCase().includes(needle) ||
        (event.stage_id ?? '').toLowerCase().includes(needle) ||
        (event.actor ?? '').toLowerCase().includes(needle) ||
        JSON.stringify(event.payload).toLowerCase().includes(needle)
      );
    });
    return newestFirst ? [...list].reverse() : list;
  }, [events, filter, newestFirst, onlyImportant]);

  return (
    <div className="panel">
      <div className="panel__head">
        事件时间线 · 已加载 {events.length}
        <div className="panel__head-actions">
          {unseen > 0 ? <span className="chip chip--warn">还有 {unseen} 条更新的事件未加载</span> : null}
          {page?.has_more ? <span className="chip">还有更早的记录未读取</span> : null}
        </div>
      </div>
      <div className="panel__hint">事件按发生顺序分页加载。</div>
      <div className="panel__body">
        <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
          <input
            className="input input--sm"
            placeholder="过滤：类型 / 阶段 / 发起者 / 载荷内容"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            style={{ flex: '1 1 220px' }}
          />
          <label className="check">
            <input type="checkbox" checked={onlyImportant} onChange={(e) => setOnlyImportant(e.target.checked)} />
            只看状态变化与异常
          </label>
          <label className="check">
            <input type="checkbox" checked={newestFirst} onChange={(e) => setNewestFirst(e.target.checked)} />
            最新在上
          </label>
          <button
            type="button"
            className="btn btn--sm"
            disabled={loading}
            onClick={() => {
              void loadFrom(maxSeen, false);
              onRefreshRequested();
            }}
          >
            增量拉取
          </button>
          <button type="button" className="btn btn--sm" disabled={loading} onClick={() => void loadFrom(0, true)}>
            从头重载
          </button>
        </div>

        {error ? (
          <Banner variant="danger" title={error.unreachable ? '无法连接后台服务' : '无法读取事件'}>
            {error.detail}
          </Banner>
        ) : null}

        {loading && events.length === 0 ? (
          <Loading label="加载事件" />
        ) : visible.length === 0 ? (
          <Empty
            title={events.length === 0 ? '没有事件' : '没有符合过滤条件的事件'}
            hint={events.length === 0 ? '任务刚提交、或被清理过时，这里会是空的。' : undefined}
          />
        ) : (
          <div className="table-wrap" style={{ maxHeight: 460, overflow: 'auto' }}>
            <table className="table table--dense">
              <thead>
                <tr>
                  <th style={{ width: 90 }}>时间</th>
                  <th style={{ width: 150 }}>类型</th>
                  <th style={{ width: 70 }}>发起者</th>
                  <th style={{ width: 70 }}>作用域</th>
                  <th>摘要</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((event) => (
                  <tr
                    key={event.event_id}
                    onClick={() => setExpanded((prev) => (prev === event.event_id ? null : event.event_id))}
                    style={{ cursor: 'pointer' }}
                  >
                    <td className="mono text-xs">
                      <div>{new Date(event.ts).toLocaleTimeString('zh-CN', { hour12: false })}</div>
                      <div className="dim">#{event.event_id}</div>
                    </td>
                    <td>
                      <div className="text-sm">{EVENT_TYPE_LABELS[event.type] ?? event.type}</div>
                      <div className="text-xs dim mono">{event.type}</div>
                    </td>
                    <td className="text-xs">{EVENT_ACTOR_LABELS[event.actor] ?? event.actor}</td>
                    <td className="text-xs">
                      {EVENT_SCOPE_LABELS[event.scope] ?? event.scope}
                      {event.stage_id ? (
                        <div className="mono dim" title={event.stage_id}>
                          {event.stage_id.slice(0, 6)}
                        </div>
                      ) : null}
                    </td>
                    <td className="text-xs">
                      {summarizeEvent(event)}
                      {expanded === event.event_id ? (
                        <pre className="code-block" style={{ marginTop: 4, maxHeight: 260, overflow: 'auto' }}>
                          {JSON.stringify(event.payload, null, 2)}
                        </pre>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="text-xs dim" style={{ marginTop: 6 }}>
          点任意一行展开详情；恢复入口在阶段行的「续跑」按钮上。
        </div>
      </div>
    </div>
  );
}

/** 从已知的事件载荷里挑出一句人话；挑不出就原样显示键值，绝不编造。 */
function summarizeEvent(event: EventRecord): JSX.Element {
  const p = event.payload;
  const from =
    typeof p['from_state'] === 'string' ? p['from_state'] : typeof p['from'] === 'string' ? p['from'] : null;
  const to = typeof p['to_state'] === 'string' ? p['to_state'] : typeof p['to'] === 'string' ? p['to'] : null;
  if (from && to) {
    // 按**事件类型**选标签表，不能只看载荷里有没有 from/to：任务状态与阶段状态
    // 是两套枚举（如 queued 只属于 TaskState），用错表会查到 undefined。
    const isTaskState = event.type.startsWith('task.');
    const label = isTaskState ? taskStateLabel : stageStateLabel;
    return (
      <span>
        {label(from).text} → {label(to).text}
        {typeof p['reason'] === 'string' ? ` · ${p['reason']}` : ''}
      </span>
    );
  }
  const keys = Object.keys(p);
  if (keys.length === 0) return <span className="dim">（无载荷）</span>;
  const interesting = ['reason', 'detail', 'message', 'note', 'status', 'op', 'mode', 'error', 'handoff_failures'];
  const bits = interesting
    .filter((k) => p[k] !== undefined && p[k] !== null && typeof p[k] !== 'object')
    .slice(0, 3)
    .map((k) => `${k}=${String(p[k])}`);
  if (bits.length > 0) return <span>{bits.join(' · ')}</span>;
  return <span className="dim">{keys.slice(0, 6).join(', ')}…</span>;
}

/** 注入来源列表：默认收起，避免把一次组装的几十条引用铺满版面。 */
function SourceDisclosure({ sources }: { sources: SourceRecord[] }): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 4 }}>
      <button type="button" className="btn btn--xs" onClick={() => setOpen((v) => !v)}>
        {open ? '▾' : '▸'} 材料来源 · {sources.length}（每条材料来自哪里、交给了谁）
      </button>
      {open ? (
        <ul className="list-reset text-xs mono" style={{ marginTop: 4 }}>
          {sources.map((s, i) => (
            <li key={i}>
              {s.from_node_id?.slice(0, 8) ?? '外部'} → {s.to_node_id?.slice(0, 8) ?? '本节点'}
              {s.artifact_id ? ` · 产物 ${s.artifact_id.slice(0, 10)}` : ''}
              {s.digest ? ` · ${s.digest.slice(0, 10)}` : ''}
              {s.sensitivity ? ` · ${s.sensitivity}` : ''}
              {s.producer_label ? ` · ${s.producer_label}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
