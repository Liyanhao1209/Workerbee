/**
 * 节点队列（RUN-04）。
 *
 * 硬要求：**显示「实际生效顺序」**。调序请求返回 `effective_order` 与 `rejected`——
 * 与用户的操作不符时明确告知原因，不显示乐观假设。所以这里的列表在收到响应后
 * 一律以 `effective_order` 重排，而不是保留本地拖拽后的顺序。
 *
 * 数据形状来自 `NodeQueueResponse`（`workerbee/server/schemas.py`）：
 * `{ node_id, pending[], running[], history[] }`。**只读投影**——权威队列在
 * 任务／阶段表里，这里不据它反推状态。
 *
 * 只有 `pending`（等待依赖 + 就绪）参与普通调序；`running` 占着执行槽（D-03），
 * 调序不会抢占它；`history` 是回看用的最近记录。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ReorderResult, TaskStage } from '../api/types';
import { workflows as workflowApi } from '../api/endpoints';
import { asArray, asStageState, asString, isRecord } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { Banner, Chip, Empty, Loading, RelTime, ShortId, StageStatePill } from './common';

function normalizeStage(raw: unknown): TaskStage | null {
  if (!isRecord(raw)) return null;
  const stageId = asString(raw['stage_id']);
  if (!stageId) return null;
  const origin = isRecord(raw['origin_of_control']) ? raw['origin_of_control'] : null;
  const upstream = isRecord(raw['upstream_pins']) ? raw['upstream_pins'] : {};
  const pins: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(upstream)) {
    pins[key] = asArray<string>(value);
  }
  return {
    stage_id: stageId,
    task_id: asString(raw['task_id']),
    node_id: asString(raw['node_id']),
    node_name: typeof raw['node_name'] === 'string' ? raw['node_name'] : null,
    desired_state:
      raw['desired_state'] === 'paused' || raw['desired_state'] === 'cancelled' ? raw['desired_state'] : 'active',
    observed_state: asStageState(raw['observed_state']),
    control_epoch: typeof raw['control_epoch'] === 'number' ? raw['control_epoch'] : 0,
    node_priority: typeof raw['node_priority'] === 'number' ? raw['node_priority'] : 0,
    task_priority: typeof raw['task_priority'] === 'number' ? raw['task_priority'] : 0,
    enqueued_at: asString(raw['enqueued_at']),
    current_attempt_seq: typeof raw['current_attempt_seq'] === 'number' ? raw['current_attempt_seq'] : 0,
    attempt_count: typeof raw['attempt_count'] === 'number' ? raw['attempt_count'] : 0,
    profile_cursor: typeof raw['profile_cursor'] === 'number' ? raw['profile_cursor'] : 0,
    blocked_reason: typeof raw['blocked_reason'] === 'string' ? raw['blocked_reason'] : null,
    status_reason: typeof raw['status_reason'] === 'string' ? raw['status_reason'] : null,
    origin_of_control: origin
      ? {
          op: asString(origin['op']),
          from_node_id: typeof origin['from_node_id'] === 'string' ? origin['from_node_id'] : null,
          at: asString(origin['at']),
          scope: asString(origin['scope']),
          detail: typeof origin['detail'] === 'string' ? origin['detail'] : null,
        }
      : null,
    upstream_pins: pins,
    checkpoint_ref: typeof raw['checkpoint_ref'] === 'string' ? raw['checkpoint_ref'] : null,
    requires_reconcile: raw['requires_reconcile'] === true,
    created_at: typeof raw['created_at'] === 'string' ? raw['created_at'] : undefined,
    updated_at: typeof raw['updated_at'] === 'string' ? raw['updated_at'] : undefined,
  };
}

function stagesOf(list: TaskStage[] | undefined): TaskStage[] {
  return asArray<unknown>(list)
    .map(normalizeStage)
    .filter((s): s is TaskStage => s !== null);
}

export function NodeQueue({ nodeId, nodeName }: { nodeId: string; nodeName?: string }): JSX.Element {
  const navigate = useNavigate();
  const fetchQueue = useCallback((_signal: AbortSignal) => workflowApi.nodeQueue(nodeId), [nodeId]);
  const { data, loading, error, reload, loaded } = useAsync(fetchQueue, [nodeId], { pollMs: 5000 });
  const submit = useSubmit();

  const pending = useMemo(() => stagesOf(data?.pending), [data]);
  const running = useMemo(() => stagesOf(data?.running), [data]);
  const history = useMemo(() => stagesOf(data?.history), [data]);

  /** 本地顺序：仅用于拖拽过程中的预览；收到响应后立刻以 effective_order 覆盖。 */
  const [order, setOrder] = useState<string[]>([]);
  const [lastResult, setLastResult] = useState<ReorderResult | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);

  useEffect(() => {
    setOrder(pending.map((s) => s.stage_id));
  }, [pending]);

  const itemsById = useMemo(() => {
    const map = new Map<string, TaskStage>();
    for (const item of pending) map.set(item.stage_id, item);
    return map;
  }, [pending]);

  const orderedItems = useMemo(
    () => order.map((id) => itemsById.get(id)).filter((s): s is TaskStage => s !== undefined),
    [order, itemsById],
  );

  const move = (fromId: string, toId: string): void => {
    setOrder((prev) => {
      const next = [...prev];
      const from = next.indexOf(fromId);
      const to = next.indexOf(toId);
      if (from < 0 || to < 0 || from === to) return prev;
      next.splice(from, 1);
      next.splice(to, 0, fromId);
      return next;
    });
  };

  const commit = async (): Promise<void> => {
    const result = await submit.run(() => workflowApi.reorder(nodeId, order));
    if (result) {
      setLastResult(result);
      if (result.effective_order.length > 0) {
        // 以**实际生效顺序**为准，而不是本地的乐观顺序（AC-03）。
        setOrder(result.effective_order);
      } else {
        reload();
      }
    }
  };

  const dirty = useMemo(() => {
    if (pending.length !== order.length) return true;
    return pending.some((item, idx) => item.stage_id !== order[idx]);
  }, [pending, order]);

  if (loading && !loaded) return <Loading label="加载节点队列" />;

  if (error) {
    return (
      <Banner
        variant="danger"
        title={error.unreachable ? '无法连接后台服务' : '无法读取节点队列'}
        actions={
          <button type="button" className="btn btn--sm" onClick={reload}>
            重试
          </button>
        }
      >
        {error.detail}
      </Banner>
    );
  }

  return (
    <div>
      <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
        <span className="text-sm muted">
          节点「{nodeName ?? <ShortId id={nodeId} />}」的待执行阶段
        </span>
        <span className="chip">待执行 {pending.length}</span>
        {running.length > 0 ? <span className="chip chip--accent">执行中 {running.length}</span> : null}
        <span className="spacer" />
        <button type="button" className="btn btn--sm" onClick={reload}>
          刷新
        </button>
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={!dirty || submit.busy}
          onClick={() => void commit()}
        >
          应用调序
        </button>
      </div>

      {submit.error ? (
        <Banner variant="danger" title="调序请求失败">
          {submit.error.detail}
          {submit.error.hint ? <div className="banner__hint">{submit.error.hint}</div> : null}
        </Banner>
      ) : null}

      {lastResult ? (
        <Banner
          variant={lastResult.applied && lastResult.rejected.length === 0 ? 'ok' : 'warn'}
          title={lastResult.applied ? '调序已生效' : '调序未完全生效'}
          hint={lastResult.reason ?? undefined}
        >
          {lastResult.rejected.length > 0 ? (
            <div>
              <div>以下阶段的调序未生效，原因如下。列表显示的是实际生效顺序：</div>
              <ul className="list-reset text-xs" style={{ marginTop: 4 }}>
                {lastResult.rejected.map((item, i) => (
                  <li key={i} className="mono">
                    {Object.entries(item)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' · ')}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div>队列顺序与你的操作一致。</div>
          )}
        </Banner>
      ) : null}

      {orderedItems.length === 0 ? (
        <Empty
          title="该节点当前没有待执行阶段"
          hint="只有等待依赖和排队就绪的阶段可以调序；正在执行、已完成或已失败的不在列表中。"
        />
      ) : (
        <div className="table-wrap">
          <table className="table table--dense">
            <thead>
              <tr>
                <th style={{ width: 24 }} />
                <th style={{ width: 34 }}>#</th>
                <th>阶段</th>
                <th>所属任务</th>
                <th>状态</th>
                <th className="table__num" title="本节点队列内的优先级，数值大者先执行">
                  节点优先级
                </th>
                <th className="table__num" title="任务提交时的优先级，数值大者先执行">
                  任务优先级
                </th>
                <th>入队时间</th>
              </tr>
            </thead>
            <tbody>
              {orderedItems.map((item, index) => (
                <tr
                  key={item.stage_id}
                  draggable
                  onDragStart={() => setDragId(item.stage_id)}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOverId(item.stage_id);
                  }}
                  onDragLeave={() => setOverId((prev) => (prev === item.stage_id ? null : prev))}
                  onDrop={() => {
                    if (dragId && dragId !== item.stage_id) move(dragId, item.stage_id);
                    setDragId(null);
                    setOverId(null);
                  }}
                  style={{
                    cursor: 'grab',
                    background: overId === item.stage_id ? 'var(--bg-sel)' : undefined,
                  }}
                >
                  <td className="dim mono">⠿</td>
                  <td className="table__num">{index + 1}</td>
                  <td>
                    <span className="mono text-xs" title={item.stage_id}>
                      {item.stage_id.slice(0, 8)}
                    </span>
                    {item.status_reason ? <span className="text-xs muted"> · {item.status_reason}</span> : null}
                  </td>
                  <td>
                    <TaskLink taskId={item.task_id} onOpen={(id) => navigate(`/tasks/${id}`)} />
                  </td>
                  <td>
                    <StageStatePill state={item.observed_state} />
                  </td>
                  <td className="table__num">{item.node_priority}</td>
                  <td className="table__num">{item.task_priority}</td>
                  <td>
                    <RelTime value={item.enqueued_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {running.length > 0 ? (
        <div style={{ marginTop: 'var(--sp-3)' }}>
          <div className="section-title">正在执行（不参与调序）· {running.length}</div>
          <div className="table-wrap">
            <table className="table table--dense">
              <tbody>
                {running.map((stage) => (
                  <tr key={stage.stage_id}>
                    <td style={{ width: 110 }}>
                      <StageStatePill state={stage.observed_state} />
                    </td>
                    <td className="mono text-xs">{stage.stage_id.slice(0, 8)}</td>
                    <td>
                      <TaskLink taskId={stage.task_id} onOpen={(id) => navigate(`/tasks/${id}`)} />
                    </td>
                    <td className="text-xs muted" style={{ maxWidth: 320 }}>
                      {stage.status_reason ?? stage.blocked_reason ?? ''}
                    </td>
                    <td className="text-xs">第 {Math.max(1, stage.current_attempt_seq)} 次尝试</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {history.length > 0 ? (
        <div style={{ marginTop: 'var(--sp-3)' }}>
          <div className="section-title">最近记录（回看用）· {history.length}</div>
          <div className="chips">
            {history.slice(0, 40).map((stage) => (
              <Chip
                key={stage.stage_id}
                title={`${stage.stage_id}\n${stage.status_reason ?? stage.blocked_reason ?? ''}`}
              >
                <StageStatePill state={stage.observed_state} />
                <span className="mono text-xs">{stage.stage_id.slice(0, 6)}</span>
              </Chip>
            ))}
            {history.length > 40 ? <span className="text-xs dim">…以及更早的 {history.length - 40} 条</span> : null}
          </div>
        </div>
      ) : null}

      <div className="text-xs dim" style={{ marginTop: 6 }}>
        默认按节点优先级、任务优先级、入队时间排序，数值大者优先。调序只影响本节点队列，
        不改变任务依赖，也不打断正在执行的阶段；应用后以上方显示的实际生效顺序为准。
      </div>
    </div>
  );
}

function TaskLink({ taskId, onOpen }: { taskId: string | null; onOpen: (id: string) => void }): JSX.Element {
  if (!taskId) return <span className="dim">—</span>;
  return (
    <a
      href={`#/tasks/${taskId}`}
      onClick={(e) => {
        e.preventDefault();
        onOpen(taskId);
      }}
    >
      <ShortId id={taskId} />
    </a>
  );
}
