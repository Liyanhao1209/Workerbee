/**
 * 审批项（HUM-03/04、OBS-05）。
 *
 * 展示纪律（HUM-03）：「展示操作内容、目标、所属 Workflow／任务／节点，
 * 以及可取得的权限和风险信息」——四项缺一不可，缺哪项就如实写「未知」。
 *
 * 决定纪律（HUM-04）：**断连、超时、重复通知都不构成批准**。回注失败
 * （undeliverable）不能显示成「已批准」，它有自己的重试入口。
 */

import { useState } from 'react';
import type { Approval } from '../api/types';
import { APPROVAL_LABELS } from '../labels';
import { Banner, Pill, ShortId, RelTime } from './common';
import { approvals as approvalApi } from '../api/endpoints';
import { useSubmit } from '../hooks/useAsync';
import { ApiError } from '../api/client';

export function ApprovalCard({
  approval,
  onChanged,
  /** 需要跳转到所属任务时用。 */
  onOpenTask,
}: {
  approval: Approval;
  onChanged: () => void;
  onOpenTask?: (taskId: string) => void;
}): JSX.Element {
  const label = APPROVAL_LABELS[approval.status];
  const [modifying, setModifying] = useState(false);
  const [modifiedAction, setModifiedAction] = useState(approval.action);
  const submit = useSubmit();

  const decide = async (approve: boolean, modified?: string): Promise<void> => {
    const result = await submit.run(() =>
      approvalApi.decide(approval.approval_id, {
        approve,
        modified_action: modified ?? null,
      }),
    );
    if (result) {
      setModifying(false);
      onChanged();
    }
  };

  const retry = async (): Promise<void> => {
    const result = await submit.run(() => approvalApi.retryDelivery(approval.approval_id));
    if (result) onChanged();
  };

  const decided = approval.decision;

  return (
    <div
      style={{
        border: '1px solid var(--line-strong)',
        borderRadius: 'var(--radius)',
        padding: 'var(--sp-3)',
        background: label.action ? 'var(--st-approval-bg)' : 'var(--bg-2)',
        marginBottom: 'var(--sp-2)',
      }}
    >
      <div className="row row--tight" style={{ marginBottom: 6 }}>
        <Pill tone={label.tone} transition={label.action} plain>
          {label.text}
        </Pill>
        {approval.tool_name ? <span className="chip">{approval.tool_name}</span> : null}
        <span className="spacer" />
        <RelTime value={approval.created_at ?? null} />
      </div>

      {/* 操作内容 */}
      <div className="field__label">操作内容</div>
      <pre className="code-block" style={{ maxHeight: 150 }}>
        {approval.action || '（未提供操作内容）'}
      </pre>

      <div className="kv" style={{ marginTop: 'var(--sp-2)' }}>
        <div className="kv__k">目标</div>
        <div className="kv__v">
          {approval.target ? <span className="mono">{approval.target}</span> : <span className="dim">未提供</span>}
        </div>

        <div className="kv__k">所属流程</div>
        <div className="kv__v">
          {approval.workflow_name ? approval.workflow_name : <span className="dim">未提供</span>}
        </div>

        <div className="kv__k">所属任务</div>
        <div className="kv__v">
          {onOpenTask && approval.bound_to.task_id ? (
            <a
              href={`#/tasks/${approval.bound_to.task_id}`}
              onClick={(e) => {
                e.preventDefault();
                onOpenTask(approval.bound_to.task_id);
              }}
            >
              <ShortId id={approval.bound_to.task_id} />
            </a>
          ) : (
            <ShortId id={approval.bound_to.task_id} />
          )}
        </div>

        <div className="kv__k">所属节点</div>
        <div className="kv__v">
          <ShortId id={approval.bound_to.node_id} />
        </div>

        <div className="kv__k">绑定尝试</div>
        <div className="kv__v">
          <ShortId id={approval.bound_to.attempt_id} />
          <span className="dim text-xs"> · 修订 #{approval.bound_to.revision_seq}</span>
        </div>

        <div className="kv__k">风险信息</div>
        <div className="kv__v">
          {approval.risk ? approval.risk : <span className="dim">未提供风险信息</span>}
        </div>

        <div className="kv__k">超时策略</div>
        <div className="kv__v">
          <span className="mono">{approval.timeout_policy}</span>
          <span className="dim text-xs"> · 超时未处理将自动拒绝</span>
        </div>

        {approval.expires_at ? (
          <>
            <div className="kv__k">过期时间</div>
            <div className="kv__v">
              <RelTime value={approval.expires_at} />
            </div>
          </>
        ) : null}
      </div>

      {approval.detail ? (
        <div className="text-xs muted" style={{ marginTop: 6 }}>
          {approval.detail}
        </div>
      ) : null}

      {decided ? (
        <div className="text-xs muted" style={{ marginTop: 6 }}>
          决定记录：{decided.approved ? '批准' : '拒绝'} · {decided.by} ·{' '}
          {new Date(decided.at).toLocaleString('zh-CN', { hour12: false })}
          {decided.modified_action ? ' · 使用修改后的动作' : ''}
        </div>
      ) : null}

      {submit.error ? (
        <Banner variant="danger" title="决定未能生效">
          {submit.error.detail}
          {submit.error.hint ? <div className="banner__hint">{submit.error.hint}</div> : null}
        </Banner>
      ) : null}

      {label.action ? (
        <div style={{ marginTop: 'var(--sp-3)' }}>
          {modifying ? (
            <div className="col">
              <div className="field__label">修改后的动作（原动作将不再被授权）</div>
              <textarea
                className="textarea textarea--code"
                value={modifiedAction}
                onChange={(e) => setModifiedAction(e.target.value)}
                rows={3}
              />
              <div className="row">
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  disabled={submit.busy || !modifiedAction.trim()}
                  onClick={() => void decide(true, modifiedAction)}
                >
                  修改后批准
                </button>
                <button type="button" className="btn btn--sm" onClick={() => setModifying(false)}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div className="row row--tight">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                disabled={submit.busy}
                onClick={() => void decide(true)}
              >
                批准
              </button>
              <button
                type="button"
                className="btn btn--danger btn--sm"
                disabled={submit.busy}
                onClick={() => void decide(false)}
              >
                拒绝
              </button>
              <button
                type="button"
                className="btn btn--sm"
                disabled={submit.busy}
                onClick={() => {
                  setModifiedAction(approval.action);
                  setModifying(true);
                }}
              >
                修改后批准
              </button>
              {approval.status === 'undeliverable' ? (
                <button type="button" className="btn btn--sm" disabled={submit.busy} onClick={() => void retry()}>
                  重新送达
                </button>
              ) : null}
              {submit.busy ? <span className="spin" /> : null}
            </div>
          )}
        </div>
      ) : (
        <div className="row row--tight" style={{ marginTop: 'var(--sp-2)' }}>
          {/* 已决定但回注失败的仍可重试；已生效的只提供追溯。 */}
          {approval.status === 'undeliverable' ? (
            <>
              <Banner variant="danger" title="决定已记录，但没能送达对应的会话">
                决定尚未生效。可点击「重新送达」重试；若多次失败，请到会话页确认该会话是否还在运行。
              </Banner>
              <button type="button" className="btn btn--sm" disabled={submit.busy} onClick={() => void retry()}>
                重新送达
              </button>
            </>
          ) : (
            <span className="dim text-xs">该审批已处理完毕，此处仅保留记录。</span>
          )}
        </div>
      )}
    </div>
  );
}

/** 提交决定后可能出现的「实际生效结果」提示（重复决定不报错，返回真实状态）。 */
export function describeDelivery(err: ApiError): string {
  if (err.kind === 'unreachable') return '无法连接后台服务，决定未送达。';
  return err.detail;
}
