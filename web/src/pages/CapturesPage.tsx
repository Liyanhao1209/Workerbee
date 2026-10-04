/**
 * 流程捕获（/captures）：把一次真实执行整理成可复用的流程草案。
 *
 * 两种来源：
 * - 「新跑一个任务捕获」：选基础候选，系统真实执行一次任务说明；
 * - 「从已跑过的任务生成」：直接把既有任务的执行记录转成捕获记录，不重新执行。
 *
 * 页面纪律：
 * - 捕获任务是一次**真实执行**——表单里如实写明，不让用户以为这只是「分析一下」；
 * - 列表状态跟着任务状态走（running / completed / failed 由后端从任务表收敛）；
 * - 进行中与失败的捕获都能跳到对应的任务详情页看实时进展。
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import { capture as captureApi, registry as registryApi, tasks as tasksApi } from '../api/endpoints';
import type { CaptureRun, CredentialRef, HarnessRegistration, Task } from '../api/types';
import { Banner, Empty, Field, Loading, Modal, Pill, ShortId, TimeText } from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import type { Tone } from '../labels';
import { CREDENTIAL_KIND_LABELS, taskStateLabel } from '../labels';

/** 捕获任务的状态徽标。status 由后端从任务真实状态收敛（running / completed / failed）。 */
const RUN_STATUS_LABELS: Record<string, { text: string; tone: Tone }> = {
  running: { text: '进行中', tone: 'running' },
  completed: { text: '已完成', tone: 'success' },
  failed: { text: '失败', tone: 'danger' },
};

export function RunStatusPill({ status }: { status: string }): JSX.Element {
  const label = RUN_STATUS_LABELS[status] ?? { text: status, tone: 'idle' as Tone };
  return (
    <Pill tone={label.tone} title={status}>
      {label.text}
    </Pill>
  );
}

function ReadError({ error, what, onRetry }: { error: ApiError; what: string; onRetry: () => void }): JSX.Element {
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接后台服务' : `无法读取${what}`}
      hint={
        error.unreachable
          ? '请确认后台服务已启动，然后点重试。连接失败期间这里不会显示任何内容。'
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

/** 「既有任务」徽标：from_task 的捕获是从已跑过的任务生成的，没有重新执行。 */
export function OriginBadge({ origin }: { origin: string }): JSX.Element | null {
  if (origin !== 'from_task') return null;
  return (
    <Pill tone="idle" plain title="从已跑过的任务生成，没有重新执行">
      既有任务
    </Pill>
  );
}

export function CapturesPage(): JSX.Element {
  const navigate = useNavigate();
  /** 新建入口：先选方式（chooser），再进对应弹窗（live 表单 / 任务选择）。 */
  const [chooserOpen, setChooserOpen] = useState(false);
  const [liveOpen, setLiveOpen] = useState(false);
  const [fromTaskOpen, setFromTaskOpen] = useState(false);

  // 轮询跟进：进行中的捕获靠它推进到完成（推送是加速器，轮询是兜底）。
  const runs = useAsync(captureApi.listRuns, [], { pollMs: 5000 });
  const rows: CaptureRun[] = runs.data?.runs ?? [];

  const openCreate = () => setChooserOpen(true);

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>捕获</h1>
          <div className="page-head__sub">
            用一个模型实际跑一遍任务，系统把执行过程整理成可复用的流程草稿。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={runs.reload}>
            刷新
          </button>
          <button type="button" className="btn btn--sm btn--primary" onClick={openCreate}>
            新建捕获任务
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel__head">捕获任务</div>
        {runs.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={runs.error} what="捕获任务列表" onRetry={runs.reload} />
          </div>
        ) : !runs.loaded ? (
          <Loading label="加载捕获任务" />
        ) : rows.length === 0 ? (
          <Empty
            title="还没有捕获任务"
            hint="用一个模型实际跑一遍任务，或从已跑过的任务生成，系统把执行过程整理成可复用的流程草稿。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={openCreate}>
                新建捕获任务
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th>执行任务</th>
                  <th style={{ width: 90 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((run) => (
                  <tr key={run.run_id}>
                    <td>
                      <span className="row row--tight">
                        <OriginBadge origin={run.origin} />
                        <Link to={`/captures/${encodeURIComponent(run.run_id)}`}>{run.name}</Link>
                      </span>
                    </td>
                    <td>
                      <RunStatusPill status={run.status} />
                    </td>
                    <td>
                      <TimeText value={run.created_at} />
                    </td>
                    <td>
                      {run.task_id ? (
                        <Link to={`/tasks/${encodeURIComponent(run.task_id)}`} title={run.task_id}>
                          <ShortId id={run.task_id} />
                          {run.origin === 'from_task'
                            ? '（来源任务）'
                            : run.status !== 'completed'
                              ? '（看实时进展）'
                              : ''}
                        </Link>
                      ) : (
                        <span className="dim">未跑起来</span>
                      )}
                    </td>
                    <td>
                      <Link to={`/captures/${encodeURIComponent(run.run_id)}`} className="btn btn--sm">
                        查看
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {chooserOpen ? (
        <CreateChooserModal
          onClose={() => setChooserOpen(false)}
          onPickLive={() => {
            setChooserOpen(false);
            setLiveOpen(true);
          }}
          onPickFromTask={() => {
            setChooserOpen(false);
            setFromTaskOpen(true);
          }}
        />
      ) : null}
      {liveOpen ? (
        <CreateCaptureModal
          onClose={() => setLiveOpen(false)}
          onCreated={(run) => navigate(`/captures/${encodeURIComponent(run.run_id)}`)}
        />
      ) : null}
      {fromTaskOpen ? (
        <FromTaskModal
          onClose={() => setFromTaskOpen(false)}
          onCreated={(run) => navigate(`/captures/${encodeURIComponent(run.run_id)}`)}
        />
      ) : null}
    </div>
  );
}

// ===========================================================================
// 新建入口：先选方式
// ===========================================================================

function CreateChooserModal({
  onClose,
  onPickLive,
  onPickFromTask,
}: {
  onClose: () => void;
  onPickLive: () => void;
  onPickFromTask: () => void;
}): JSX.Element {
  return (
    <Modal title="新建捕获任务" onClose={onClose}>
      <div className="col">
        <button type="button" className="btn" style={{ textAlign: 'left' }} onClick={onPickLive}>
          新跑一个任务捕获
          <div className="text-xs dim" style={{ marginTop: 2 }}>
            选一个基础候选，系统真实执行一次你写的任务说明，再整理成流程草案。
          </div>
        </button>
        <button type="button" className="btn" style={{ textAlign: 'left' }} onClick={onPickFromTask}>
          从已跑过的任务生成
          <div className="text-xs dim" style={{ marginTop: 2 }}>
            选一个已经跑过的任务，直接把它的执行记录整理成流程草案——不会重新执行。
          </div>
        </button>
      </div>
    </Modal>
  );
}

// ===========================================================================
// 从已跑过的任务生成
// ===========================================================================

/** 任务的展示名：输入里那段话的前若干字，取不到就如实显示任务号。 */
function taskInputPreview(task: Task): string {
  const payload = task.input_payload ?? {};
  for (const key of ['task', 'goal']) {
    const v = payload[key];
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  for (const v of Object.values(payload)) {
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  return '';
}

function FromTaskModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (run: CaptureRun) => void;
}): JSX.Element {
  // 只列有执行记录的任务：没有执行记录的任务没有材料可以捕获。
  const taskList = useAsync(() => tasksApi.list({ has_attempts: true }), []);
  const submit = useSubmit();
  const [selected, setSelected] = useState<string | null>(null);

  const rows: Task[] = taskList.data?.tasks ?? [];

  const create = async (): Promise<void> => {
    if (!selected) return;
    const run = await submit.run(() => captureApi.createRunFromTask({ task_id: selected }));
    if (run) onCreated(run);
  };

  return (
    <Modal
      wide
      title="从已跑过的任务生成"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            disabled={!selected || submit.busy}
            onClick={() => void create()}
          >
            {submit.busy ? '创建中…' : '用这个任务生成捕获'}
          </button>
        </>
      }
    >
      <Banner variant="info" title="不会重新执行任务">
        系统直接读取这个任务已有的执行记录（输入、工具调用、产物、执行路径）来整理流程草案。
        这里只列出有执行记录的任务。
      </Banner>

      {submit.error ? (
        <Banner variant="danger" title="创建捕获任务失败" hint={submit.error.hint ?? undefined}>
          <span className="mono text-xs">{submit.error.detail}</span>
        </Banner>
      ) : null}

      {taskList.error ? (
        <ReadError error={taskList.error} what="任务列表" onRetry={taskList.reload} />
      ) : !taskList.loaded ? (
        <Loading label="加载任务" />
      ) : rows.length === 0 ? (
        <Empty title="还没有跑过的任务" hint="先到流程页发射一个任务跑一遍，再回来从这里生成。" />
      ) : (
        <div className="table-wrap">
          <table className="table table--rows-clickable">
            <thead>
              <tr>
                <th style={{ width: 28 }} />
                <th>任务</th>
                <th>流程</th>
                <th>状态</th>
                <th>提交时间</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((task) => {
                const stateLabel = taskStateLabel(task.observed_state);
                const preview = taskInputPreview(task);
                return (
                  <tr
                    key={task.task_id}
                    onClick={() => setSelected(task.task_id)}
                    style={selected === task.task_id ? { outline: '2px solid var(--accent)' } : undefined}
                  >
                    <td>
                      <input
                        type="radio"
                        checked={selected === task.task_id}
                        onChange={() => setSelected(task.task_id)}
                      />
                    </td>
                    <td>
                      <div className="truncate" style={{ maxWidth: 320 }} title={preview || task.task_id}>
                        {preview || <ShortId id={task.task_id} />}
                      </div>
                    </td>
                    <td>
                      <span className="dim text-xs">{task.workflow_name ?? <ShortId id={task.workflow_id} />}</span>
                    </td>
                    <td>
                      <Pill tone={stateLabel.tone} transition={stateLabel.transitioning} title={task.observed_state}>
                        {stateLabel.text}
                      </Pill>
                    </td>
                    <td>
                      <TimeText value={task.created_at ?? null} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

// ===========================================================================
// 新建捕获任务
// ===========================================================================

/** 从能力快照里取 harness 声明过的模型清单，只用于占位提示，取不到就当没有。 */
function declaredModels(harness: HarnessRegistration | undefined): string[] {
  const raw = harness?.capabilities_snapshot?.['models'];
  if (!Array.isArray(raw)) return [];
  return raw.filter((x): x is string => typeof x === 'string');
}

function CreateCaptureModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (run: CaptureRun) => void;
}): JSX.Element {
  const harnesses = useAsync(registryApi.harnesses, []);
  const credentials = useAsync(registryApi.credentials, []);
  const [name, setName] = useState('');
  const [instructions, setInstructions] = useState('');
  const [harnessRef, setHarnessRef] = useState('');
  const [modelName, setModelName] = useState('');
  const [credentialRef, setCredentialRef] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const harnessRows: HarnessRegistration[] = (harnesses.data ?? []).filter((h) => h.enabled);
  const credentialRows: CredentialRef[] = (credentials.data ?? []).filter((c) => !c.revoked);
  const selectedHarness = harnessRows.find((h) => h.harness_id === harnessRef);
  const models = declaredModels(selectedHarness);

  const create = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError('给这次捕获起个名字。');
      return;
    }
    if (!instructions.trim()) {
      setFormError('任务说明不能为空——捕获就是拿这段话真实执行一次。');
      return;
    }
    if (!harnessRef) {
      setFormError('请选择一个 harness。');
      return;
    }
    setFormError(null);
    const run = await submit.run(() =>
      captureApi.createRun({
        name: name.trim(),
        instructions: instructions.trim(),
        harness_ref: harnessRef,
        model_name: modelName.trim() || null,
        credential_ref: credentialRef || null,
      }),
    );
    if (run) onCreated(run);
  };

  return (
    <Modal
      wide
      title="新建捕获任务"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            disabled={submit.busy}
            onClick={() => void create()}
          >
            {submit.busy ? '创建中…' : '创建并开始执行'}
          </button>
        </>
      }
    >
      <Banner variant="info" title="这会真实地跑一遍任务">
        选定的模型会实际执行你写的任务说明——过程中可以审批、暂停、看输出，和一次普通任务一样。
        跑完之后，再把执行过程整理成流程草案（那一步才会调用一次助手配置的模型）。
      </Banner>

      {formError ? (
        <Banner variant="danger" title="无法提交">
          {formError}
        </Banner>
      ) : null}
      {submit.error ? (
        <Banner variant="danger" title="创建捕获任务失败" hint={submit.error.hint ?? undefined}>
          <span className="mono text-xs">{submit.error.detail}</span>
        </Banner>
      ) : null}
      {harnesses.error ? <ReadError error={harnesses.error} what="harness 列表" onRetry={harnesses.reload} /> : null}
      {credentials.error ? (
        <ReadError error={credentials.error} what="凭据列表" onRetry={credentials.reload} />
      ) : null}

      <div className="field-row">
        <Field label="名称" required>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：抓取并汇总本周的 issue"
          />
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="任务说明" required hint="这段话会原样发给模型执行。">
          <textarea
            className="textarea"
            rows={4}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="把这个任务交给一个模型去做，就像你平时交代给它一样写。"
          />
        </Field>
      </div>

      <div className="field-row" style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="Harness" required hint="执行这次捕获的 agent 程序。">
          <select
            className="select"
            value={harnessRef}
            disabled={!harnesses.loaded}
            onChange={(e) => setHarnessRef(e.target.value)}
          >
            <option value="">请选择 harness</option>
            {harnessRows.map((h) => (
              <option key={h.harness_id} value={h.harness_id}>
                {h.name}
                {h.last_probe_ok === false ? '（探测失败）' : h.last_probe_ok === null ? '（未探测）' : ''}
              </option>
            ))}
          </select>
        </Field>
        <Field label="模型名" hint="留空时用所选凭据的默认模型；凭据也没填就用 harness 登录态的默认模型。">
          <input
            className="input input--mono"
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder={models.length > 0 ? `如 ${models[0]}` : '如 claude-sonnet-4-6 / kimi-k2'}
          />
        </Field>
        <Field label="凭据" hint="用哪份凭据调用模型服务；不选则用 harness 的本机登录态。">
          <select
            className="select"
            value={credentialRef}
            disabled={!credentials.loaded}
            onChange={(e) => setCredentialRef(e.target.value)}
          >
            <option value="">（使用 harness 本机登录态）</option>
            {credentialRows.map((c) => (
              <option key={c.credential_id} value={c.credential_id}>
                {c.label} · {CREDENTIAL_KIND_LABELS[c.kind]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {harnesses.loaded && harnessRows.length === 0 ? (
        <div className="text-xs" style={{ marginTop: 'var(--sp-2)' }}>
          还没有可用的 harness。到 <Link to="/registry">注册表</Link> 先登记一个，再回来创建。
        </div>
      ) : null}
    </Modal>
  );
}
