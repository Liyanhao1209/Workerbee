/**
 * 节点队列（RUN-04）。
 *
 * 硬要求：**显示「实际生效顺序」**。调序请求返回 `effective_order` 与 `rejected`——
 * 与用户的操作不符时明确告知原因，不显示乐观假设。所以这里的列表在收到响应后
 * 一律以 `effective_order` 重排，而不是保留本地拖拽后的顺序。
 *
 * 队列是只读投影：权威队列在 TaskStage 表里，这个视图按 node_id 过滤 + 排序。
 * 不参与调序的阶段（暂停、已删除、失败、完成）如实标注为「不可调序」。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ReorderResult } from '../api/types';
import { workflows as workflowApi } from '../api/endpoints';
import { asArray, asNumber, asStageState, asString, isRecord } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { Banner, Empty, Loading, ShortId, StageStatePill, RelTime, Chip } from './common';
import { stageStateLabel } from '../labels';

/** 队列项：内核未在 schemas.py 固定形状，这里按可用字段做宽容规范化。 */
export interface QueueItem {
  stage_id: string;
  task_id: string | null;
  node_priority: number;
  task_priority: number;
  enqueued_at: string | null;
  observed_state: ReturnType<typeof asStageState>;
  status_reason: string | null;
  /** 暂停/删除等控制操作来源（OBS-03）。 */
  origin_op: string | null;
  origin_node_id: string | null;
}

function normalizeQueueItem(raw: unknown): QueueItem | null {
  if (!isRecord(raw)) return null;
  const stageId = asString(raw['stage_id']);
  if (!stageId) return null;
  const origin = isRecord(raw['origin_of_control']) ? raw['origin_of_control'] : null;
  return {
    stage_id: stageId,
    task_id: typeof raw['task_id'] === 'string' ? raw['task_id'] : null,
    node_priority: asNumber(raw['node_priority'], 50),
    task_priority: asNumber(raw['task_priority'], 50),
    enqueued_at: typeof raw['enqueued_at'] === 'string' ? raw['enqueued_at'] : null,
    observed_state: asStageState(raw['observed_state']),
    status_reason: typeof raw['status_reason'] === 'string' ? raw['status_reason'] : null,
    origin_op: origin ? asString(origin['op']) : null,
    origin_node_id: origin && typeof origin['from_node_id'] === 'string' ? origin['from_node_id'] : null,
  };
}

export function NodeQueue({ nodeId, nodeName }: { nodeId: string; nodeName?: string }): JSX.Element {
  const navigate = useNavigate();
  const fetchQueue = useCallback(
    (signal: AbortSignal) => workflowApi.nodeQueue(nodeId).then((raw) => raw),
    [nodeId],
  );
  const { data, loading, error, reload, loaded } = useAsync(fetchQueue, [nodeId], { pollMs: 5000 });
  const submit = useSubmit();

  const rawStages = useMemo(() => {
    if (!data) return [];
    const record = data as unknown as Record<string, unknown>;
    return asArray<unknown>(record['stages']);
  }, [data]);

  const serverItems = useMemo(
    () => rawStages.map(normalizeQueueItem).filter((i): i is QueueItem => i !== null),
    [rawStages],
  );

  /** 本地顺序：仅用于拖拽过程中的预览；收到响应后立刻以 effective_order 覆盖。 */
  const [order, setOrder] = useState<string[]>([]);
  const [lastResult, setLastResult] = useState<ReorderResult | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);

  useEffect(() => {
    setOrder(serverItems.map((i) => i.stage_id));
  }, [serverItems]);

  const itemsById = useMemo(() => {
    const map = new Map<string, QueueItem>();
    for (const item of serverItems) map.set(item.stage_id, item);
    return map;
  }, [serverItems]);

  const orderedItems = useMemo(
    () => order.map((id) => itemsById.get(id)).filter((i): i is QueueItem => i !== undefined),
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
    if (serverItems.length !== order.length) return true;
    return serverItems.some((item, idx) => item.stage_id !== order[idx]);
  }, [serverItems, order]);

  if (loading && !loaded) return <Loading label="加载节点队列" />;

  if (error) {
    return (
      <Banner variant="danger" title={error.unreachable ? '无法连接内核' : '无法读取节点队列'}>
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
        <span className="spacer" />
        <button type="button" className="btn btn--sm" onClick={reload}>
          刷新
        </button>
        <button type="button" className="btn btn--primary btn--sm" disabled={!dirty || submit.busy} onClick={() => void commit()}>
          应用调序
        </button>
      </div>

      {submit.error ? (
        <Banner variant="danger" title="调序请求失败">
          {submit.error.detail}
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
              <div>以下阶段被拒绝，原因如下（显示的是实际生效顺序，不是你的操作）：</div>
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
        <Empty title="该节点当前没有待执行阶段" hint="阶段完成、失败或被删除后不再作为 pending 项参与调序。" />
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
                <th className="table__num" title="node_priority（本节点队列内的调序结果）">
                  node_pri
                </th>
                <th className="table__num" title="task_priority（发射时从任务优先级拷贝）">
                  task_pri
                </th>
                <th>入队时间</th>
              </tr>
            </thead>
            <tbody>
              {orderedItems.map((item, index) => {
                const reorderable = item.observed_state === 'ready' || item.observed_state === 'waiting_deps';
                return (
                  <tr
                    key={item.stage_id}
                    draggable={reorderable}
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
                      cursor: reorderable ? 'grab' : 'default',
                      background: overId === item.stage_id ? 'var(--bg-sel)' : undefined,
                      opacity: reorderable ? 1 : 0.6,
                    }}
                  >
                    <td className="dim mono">{reorderable ? '⠿' : '·'}</td>
                    <td className="table__num">{index + 1}</td>
                    <td>
                      <span className="mono text-xs" title={item.stage_id}>
                        {item.stage_id.slice(0, 8)}
                      </span>
                      {item.status_reason ? (
                        <span className="text-xs muted"> · {item.status_reason}</span>
                      ) : null}
                    </td>
                    <td>
                      {item.task_id ? (
                        <a
                          href={`#/tasks/${item.task_id}`}
                          onClick={(e) => {
                            e.preventDefault();
                            navigate(`/tasks/${item.task_id}`);
                          }}
                        >
                          <ShortId id={item.task_id} />
                        </a>
                      ) : (
                        <span className="dim">—</span>
                      )}
                    </td>
                    <td>
                      <StageStatePill state={item.observed_state} />
                      {!reorderable ? <Chip title="暂停／已完成／失败的阶段不参与普通调序">不可调序</Chip> : null}
                    </td>
                    <td className="table__num">{item.node_priority}</td>
                    <td className="table__num">{item.task_priority}</td>
                    <td>
                      <RelTime value={item.enqueued_at} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="text-xs dim" style={{ marginTop: 6 }}>
        默认顺序 (node_priority, task_priority, enqueued_at)，大者优先。调序只作用于本节点队列，
        不破坏任务依赖，也不会抢占已运行的阶段；与派发并发时以 CAS 仲裁并返回实际生效结果。
      </div>
    </div>
  );
}
