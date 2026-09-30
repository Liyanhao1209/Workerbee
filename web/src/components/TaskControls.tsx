/**
 * 生命周期控制（LIFE-01–06）。
 *
 * 三条纪律：
 * 1. **默认范围是整次任务**，不是「这个节点」。从节点发起时，节点只是定位任务
 *    与记录来源的入口——界面必须把这件事说清楚（§10.1），不能让人以为只停了分支。
 * 2. **三种完成判据分开显示**（已接受 / 执行已停止 / 资源清理完成），见 TriState。
 * 3. 暂停的代价如实呈现：哪些原位暂停、哪些协作停止（可能有重复工作）、
 *    哪些 harness 不支持（拒绝，而不是显示为已暂停）。
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { DeleteTaskResponse, PauseResponse, ResumeResponse, Task } from '../api/types';
import { tasks as taskApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { useSubmit } from '../hooks/useAsync';
import { Banner, Chip, Modal, ShortId } from './common';
import { TriState } from './TriState';

export interface TaskControlsProps {
  task: Task;
  /** 从某节点发起控制时传入；会作为 origin_of_control 记录来源。 */
  fromNodeId?: string | null;
  fromNodeName?: string | null;
  onChanged?: () => void;
  compact?: boolean;
}

type Pending =
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'delete' }
  | null;

export function TaskControls({
  task,
  fromNodeId,
  fromNodeName,
  onChanged,
  compact,
}: TaskControlsProps): JSX.Element {
  const navigate = useNavigate();
  const [pending, setPending] = useState<Pending>(null);
  const [pauseResult, setPauseResult] = useState<PauseResponse | null>(null);
  const [resumeResult, setResumeResult] = useState<ResumeResponse | null>(null);
  const [deleteResult, setDeleteResult] = useState<DeleteTaskResponse | null>(null);
  const [restartFailed, setRestartFailed] = useState(false);
  const submit = useSubmit();

  const state = task.observed_state;
  const canPause = state === 'queued' || state === 'running' || state === 'blocked';
  const canResume = state === 'paused' || state === 'pausing';
  const canDelete = state !== 'cancelled';

  const runPause = async (): Promise<void> => {
    const result = await submit.run(() =>
      taskApi.pause(task.task_id, { from_node_id: fromNodeId ?? null }),
    );
    if (result) {
      setPauseResult(result);
      setPending(null);
      onChanged?.();
    }
  };

  const runResume = async (): Promise<void> => {
    const result = await submit.run(() => taskApi.resume(task.task_id, { restart_failed: restartFailed }));
    if (result) {
      setResumeResult(result);
      setPending(null);
      onChanged?.();
    }
  };

  const runDelete = async (): Promise<void> => {
    const result = await submit.run(() =>
      taskApi.remove(task.task_id, { from_node_id: fromNodeId ?? null }),
    );
    if (result) {
      setDeleteResult(result);
      setPending(null);
      onChanged?.();
    }
  };

  return (
    <>
      <div className="row row--tight">
        <button
          type="button"
          className={compact ? 'btn btn--sm' : 'btn'}
          disabled={!canPause || submit.busy}
          onClick={() => {
            submit.clear();
            setPending({ kind: 'pause' });
          }}
          title={
            fromNodeName
              ? `暂停整次任务（从节点「${fromNodeName}」发起，作用于整个任务）`
              : '暂停整次任务'
          }
        >
          暂停
        </button>
        <button
          type="button"
          className={compact ? 'btn btn--sm' : 'btn'}
          disabled={!canResume || submit.busy}
          onClick={() => {
            submit.clear();
            setPending({ kind: 'resume' });
          }}
        >
          恢复
        </button>
        <button
          type="button"
          className={compact ? 'btn btn--danger btn--sm' : 'btn btn--danger'}
          disabled={!canDelete || submit.busy}
          onClick={() => {
            submit.clear();
            setPending({ kind: 'delete' });
          }}
        >
          删除任务
        </button>
        {submit.busy ? <span className="spin" /> : null}
      </div>

      {submit.error && !pending ? (
        <Banner variant="danger" title="操作未生效">
          {submit.error.detail}
          {submit.error.hint ? <div className="banner__hint">{submit.error.hint}</div> : null}
        </Banner>
      ) : null}

      {/* ---------------- 暂停 ---------------- */}
      {pending?.kind === 'pause' ? (
        <Modal
          title="暂停任务"
          onClose={() => setPending(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setPending(null)}>
                取消
              </button>
              <button type="button" className="btn btn--primary" disabled={submit.busy} onClick={() => void runPause()}>
                确认暂停
              </button>
            </>
          }
        >
          <Banner variant="info" title="暂停范围：整次任务">
            暂停作用于<strong>整次任务</strong>，覆盖它的所有并行分支。
            {fromNodeName ? (
              <>
                {' '}
                本次操作从节点「{fromNodeName}」发起，暂停的仍是整次任务。
              </>
            ) : null}
            其他提交不受影响。
          </Banner>
          <ul className="list-reset text-sm">
            <li>· 未启动的阶段直接暂停，没有额外代价。</li>
            <li>· 正在运行的阶段：harness 支持原地暂停的就原地停；不支持的会协商停止，并尽量保留断点。</li>
            <li>· 协商停止可能造成<strong>重复工作</strong>——恢复时会提示哪些阶段要重做。</li>
            <li>· 已完成的上游结果保留，不被改写为失败。</li>
          </ul>
          {submit.error ? (
            <Banner variant="danger" title="暂停失败">
              {submit.error.detail}
            </Banner>
          ) : null}
        </Modal>
      ) : null}

      {/* ---------------- 恢复 ---------------- */}
      {pending?.kind === 'resume' ? (
        <Modal
          title="恢复任务"
          onClose={() => setPending(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setPending(null)}>
                取消
              </button>
              <button type="button" className="btn btn--primary" disabled={submit.busy} onClick={() => void runResume()}>
                确认恢复
              </button>
            </>
          }
        >
          <div className="col">
            <label className="check">
              <input
                type="checkbox"
                checked={restartFailed}
                onChange={(e) => setRestartFailed(e.target.checked)}
              />
              同时重跑已失败的阶段
            </label>
            <div className="text-xs muted">
              默认只恢复被暂停的部分。<strong>已成功的阶段不会被重跑</strong>；
              协商停止的阶段会重新执行（有断点则续跑，没有则从头再来，恢复后会告知）。
            </div>
            {submit.error ? (
              <Banner variant="danger" title="恢复失败">
                {submit.error.detail}
              </Banner>
            ) : null}
          </div>
        </Modal>
      ) : null}

      {/* ---------------- 删除 ---------------- */}
      {pending?.kind === 'delete' ? (
        <Modal
          title="删除任务（不可续跑）"
          onClose={() => setPending(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setPending(null)}>
                取消
              </button>
              <button type="button" className="btn btn--danger" disabled={submit.busy} onClick={() => void runDelete()}>
                确认删除
              </button>
            </>
          }
        >
          <Banner variant="danger" title="删除后不能续跑">
            将停止该任务的所有排队、重试与执行，并清理运行资源，保留必要历史。
            再次提交相同输入会作为<strong>新任务</strong>执行。
          </Banner>
          <ul className="list-reset text-sm">
            <li>· 已完成的阶段保留原有结果，不会被改写成失败。</li>
            <li>· 删除只停止后续执行；已写入的项目文件和已产生的外部操作<strong>不会被撤销</strong>。</li>
            <li>· 进度分三步显示：已受理 / 执行已停止 / 资源清理完成；清理失败会一直显示。</li>
          </ul>
          {submit.error ? (
            <Banner variant="danger" title="删除失败">
              {submit.error.detail}
            </Banner>
          ) : null}
        </Modal>
      ) : null}

      {/* ---------------- 结果三态 ---------------- */}
      {pauseResult ? (
        <ResultModal title="暂停结果" onClose={() => setPauseResult(null)}>
          <TriState outcome={pauseResult} />
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <DetailList title="未启动，直接暂停" ids={pauseResult.paused_immediately} />
            <DetailList title="原位暂停" ids={pauseResult.paused_in_place} />
            <DetailList
              title="协商停止（可能重复工作）"
              ids={pauseResult.stopped_cooperatively}
              tone="warn"
            />
            <DetailList
              title="harness 不支持暂停（这些阶段仍在运行）"
              ids={pauseResult.unsupported}
              tone="danger"
            />
            {Object.keys(pauseResult.checkpoints).length > 0 ? (
              <div style={{ marginTop: 'var(--sp-2)' }}>
                <div className="section-title">断点</div>
                <ul className="list-reset text-xs">
                  {Object.entries(pauseResult.checkpoints).map(([stageId, cp]) => (
                    <li key={stageId} className="mono">
                      {stageId.slice(0, 8)} →{' '}
                      {cp ? (
                        <span className="text-success">{cp}</span>
                      ) : (
                        <span className="text-warn">无断点，恢复时从头重跑本阶段</span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </ResultModal>
      ) : null}

      {resumeResult ? (
        <ResultModal title="恢复结果" onClose={() => setResumeResult(null)}>
          <TriState outcome={resumeResult} />
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <DetailList title="重新排队" ids={resumeResult.requeued} />
            <DetailList title="需要重做（有断点则续跑，没有则从头再来）" ids={resumeResult.restarted} tone="warn" />
            <DetailList title="复用原会话" ids={resumeResult.reused_sessions} />
          </div>
        </ResultModal>
      ) : null}

      {deleteResult ? (
        <ResultModal
          title="删除结果"
          onClose={() => {
            setDeleteResult(null);
            navigate('/tasks');
          }}
        >
          <TriState outcome={deleteResult} />
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <DetailList title="已取消的阶段" ids={deleteResult.cancelled_stages} />
            <DetailList title="保留真实结果的已完成阶段" ids={deleteResult.preserved_succeeded} />
          </div>
          <Banner variant="info" title="任务已删除">
            已删除的任务不会再被启动，历史记录仍可在任务列表中查看。
          </Banner>
        </ResultModal>
      ) : null}
    </>
  );
}

function ResultModal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <Modal title={title} onClose={onClose} footer={<button type="button" className="btn btn--primary" onClick={onClose}>知道了</button>}>
      {children}
    </Modal>
  );
}

function DetailList({
  title,
  ids,
  tone,
}: {
  title: string;
  ids: string[];
  tone?: 'warn' | 'danger';
}): JSX.Element | null {
  if (!ids || ids.length === 0) return null;
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

/** 从节点发起控制时，把来源如实写在任务详情上（OBS-03）。 */
export function OriginOfControlText({ task }: { task: Task }): JSX.Element | null {
  const origin = task.last_origin;
  if (!origin) return null;
  return (
    <div className="text-xs muted">
      最近一次控制：{origin.op} · 范围 {origin.scope} · 来源节点{' '}
      {origin.from_node_id ? <ShortId id={origin.from_node_id} /> : '（任务级）'} ·{' '}
      {new Date(origin.at).toLocaleString('zh-CN', { hour12: false })}
      {origin.detail ? ` · ${origin.detail}` : ''}
    </div>
  );
}

/** 便于页面统一显示「操作失败」时的错误。 */
export function ErrorBanner({ error }: { error: ApiError | null }): JSX.Element | null {
  if (!error) return null;
  return (
    <Banner variant="danger" title={error.unreachable ? '无法连接后台服务' : '请求失败'}>
      {error.detail}
      {error.hint ? <div className="banner__hint">{error.hint}</div> : null}
    </Banner>
  );
}
