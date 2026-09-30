/**
 * 顶部常驻「需处理」面板（OBS-05）。
 *
 * 覆盖四类需要人介入的事：审批请求、任务失败、状态不明（LOST）的阶段、
 * 未清理资源。它不推送普通进度——普通进度在列表里看就够了。
 * 断连期间的审批在重连后由 REST 拉回（AC-12），不依赖推送是否到达。
 */

import { useNavigate } from 'react-router-dom';
import { useAttention } from '../store/attention';
import { ApprovalCard } from './Approval';
import { Empty, Loading, ShortId, StageStatePill, TaskStatePill, RelTime, Banner } from './common';

export function AttentionDrawer({ onClose }: { onClose: () => void }): JSX.Element {
  const navigate = useNavigate();
  const state = useAttention();

  const openApprovals = state.approvals.filter(
    (a) => a.status === 'pending' || a.status === 'undeliverable',
  );
  const historyApprovals = state.approvals.filter(
    (a) => a.status !== 'pending' && a.status !== 'undeliverable',
  );

  return (
    <div className="sidepanel">
      <div className="sidepanel__head">
        <strong>需处理</strong>
        <span className="spacer" />
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => void state.refresh()}>
          刷新
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onClose}>
          收起
        </button>
      </div>
      <div className="sidepanel__body">
        {state.error ? (
          <Banner variant="danger" title="无法获取需处理列表">
            {state.error.detail}
          </Banner>
        ) : null}

        {state.startupNotes.length > 0 ? (
          <Banner variant="warn" title="内核启动提示">
            <ul className="list-reset">
              {state.startupNotes.map((note, i) => (
                <li key={i}>· {note}</li>
              ))}
            </ul>
          </Banner>
        ) : null}

        <div className="section-title">待审批 · {openApprovals.length}</div>
        {!state.loaded ? (
          <Loading />
        ) : openApprovals.length === 0 ? (
          <Empty title="没有等待处理的审批" hint="需要人工确认的操作在执行前会出现在这里。" />
        ) : (
          openApprovals.map((approval) => (
            <ApprovalCard
              key={approval.approval_id}
              approval={approval}
              onChanged={() => void state.refresh()}
              onOpenTask={(taskId) => {
                onClose();
                navigate(`/tasks/${taskId}`);
              }}
            />
          ))
        )}

        <div className="divider" />

        <div className="section-title">失败的任务 · {state.failedTasks.length}</div>
        {state.failedTasks.length === 0 ? (
          <div className="empty text-sm">没有失败任务。</div>
        ) : (
          <ul className="list-reset">
            {state.failedTasks.map((task) => (
              <li
                key={task.task_id}
                style={{ padding: '6px 0', borderBottom: '1px solid var(--line-faint)', cursor: 'pointer' }}
                onClick={() => {
                  onClose();
                  navigate(`/tasks/${task.task_id}`);
                }}
              >
                <div className="row row--tight">
                  <TaskStatePill state={task.observed_state} />
                  <ShortId id={task.task_id} />
                  <span className="spacer" />
                  <RelTime value={task.updated_at ?? null} />
                </div>
                <div className="text-xs muted truncate">
                  {task.workflow_name ?? task.workflow_id} ·{' '}
                  {task.failure_summary?.reason ?? task.blocked_reason ?? '未提供失败原因'}
                </div>
              </li>
            ))}
          </ul>
        )}

        <div className="divider" />

        <div className="section-title">状态不明的阶段 · {state.lostStages.length}</div>
        {state.lostStages.length === 0 ? (
          <div className="empty text-sm">没有无法确认状态的阶段。</div>
        ) : (
          <ul className="list-reset">
            {state.lostStages.map((stage) => (
              <li
                key={stage.stage_id}
                style={{ padding: '6px 0', borderBottom: '1px solid var(--line-faint)', cursor: 'pointer' }}
                onClick={() => {
                  onClose();
                  navigate(`/tasks/${stage.task_id}`);
                }}
              >
                <div className="row row--tight">
                  <StageStatePill state={stage.observed_state} />
                  <span>{stage.node_name ?? <ShortId id={stage.node_id} />}</span>
                  <span className="spacer" />
                  <ShortId id={stage.task_id} />
                </div>
                <div className="text-xs muted">
                  {stage.blocked_reason ?? stage.status_reason ?? '无法确认该阶段的执行结果，请人工核对后再决定是否重跑。'}
                </div>
              </li>
            ))}
          </ul>
        )}

        <div className="divider" />

        <div className="section-title">未清理资源 · {state.unresolvedResources.length}</div>
        {state.unresolvedResources.length === 0 ? (
          <div className="empty text-sm">没有待清理的资源。</div>
        ) : (
          <ul className="list-reset">
            {/* 台账字段由 `ResourceLedger.teardown_failed()` 固定：
                resource_id / kind / state / last_error / owner_task_id。 */}
            {state.unresolvedResources.map((res, i) => (
              <li key={String(res['resource_id'] ?? i)} style={{ padding: '4px 0' }}>
                <div className="text-xs">
                  <span className="mono">{String(res['kind'] ?? '未知类型')}</span>
                  {' · '}
                  <span className="mono">{String(res['state'] ?? '未知状态')}</span>
                  {' · '}
                  <span className="mono dim" title={String(res['resource_id'] ?? '')}>
                    {String(res['resource_id'] ?? '').slice(0, 8) || '无 id'}
                  </span>
                  {typeof res['owner_task_id'] === 'string' && res['owner_task_id'] ? (
                    <>
                      {' · 所属任务 '}
                      <a href={`#/tasks/${res['owner_task_id']}`} className="mono">
                        {res['owner_task_id'].slice(0, 8)}
                      </a>
                    </>
                  ) : (
                    <span className="dim"> · 无所属任务</span>
                  )}
                </div>
                {typeof res['last_error'] === 'string' && res['last_error'] ? (
                  <div className="text-xs text-warn">上次失败原因：{res['last_error']}</div>
                ) : null}
              </li>
            ))}
          </ul>
        )}

        {historyApprovals.length > 0 ? (
          <>
            <div className="divider" />
            <div className="section-title">已处理的审批 · {historyApprovals.length}</div>
            {historyApprovals.slice(0, 20).map((approval) => (
              <ApprovalCard
                key={approval.approval_id}
                approval={approval}
                onChanged={() => void state.refresh()}
              />
            ))}
          </>
        ) : null}
      </div>
    </div>
  );
}
