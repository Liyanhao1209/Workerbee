/**
 * 流程捕获（/captures）：用一个模型真实跑一遍任务，把执行过程整理成可复用的流程草案。
 *
 * 页面纪律：
 * - 捕获任务是一次**真实执行**——表单里如实写明，不让用户以为这只是「分析一下」；
 * - 列表状态跟着任务状态走（running / completed / failed 由后端从任务表收敛）；
 * - 进行中与失败的捕获都能跳到对应的任务详情页看实时进展。
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import { capture as captureApi, registry as registryApi } from '../api/endpoints';
import type { CaptureRun, CredentialRef, HarnessRegistration } from '../api/types';
import { Banner, Empty, Field, Loading, Modal, Pill, ShortId, TimeText } from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import type { Tone } from '../labels';
import { CREDENTIAL_KIND_LABELS } from '../labels';

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

export function CapturesPage(): JSX.Element {
  const navigate = useNavigate();
  const [createOpen, setCreateOpen] = useState(false);

  // 轮询跟进：进行中的捕获靠它推进到完成（推送是加速器，轮询是兜底）。
  const runs = useAsync(captureApi.listRuns, [], { pollMs: 5000 });
  const rows: CaptureRun[] = runs.data?.runs ?? [];

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
          <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
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
            hint="用一个模型实际跑一遍任务，系统把执行过程整理成可复用的流程草稿。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
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
                      <Link to={`/captures/${encodeURIComponent(run.run_id)}`}>{run.name}</Link>
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
                          {run.status !== 'completed' ? '（看实时进展）' : ''}
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

      {createOpen ? (
        <CreateCaptureModal
          onClose={() => setCreateOpen(false)}
          onCreated={(run) => navigate(`/captures/${encodeURIComponent(run.run_id)}`)}
        />
      ) : null}
    </div>
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
