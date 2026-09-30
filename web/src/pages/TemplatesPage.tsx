/**
 * 模板（/templates）：从流程生成模板，再按模板实例化出新流程。
 *
 * 两条硬约束贯穿整页：
 *   1. 模板**不携带凭据**——生成时把凭据剥离成占位引用，实例化时必须显式绑定；
 *   2. 绑定不全时**不编造凭据**——如实把 missing_bindings 摆出来，绝不谎报「实例化成功」。
 */

import { useState } from 'react';
import { ApiError } from '../api/client';
import { registry as registryApi, templates as templatesApi, workflows as workflowApi } from '../api/endpoints';
import type {
  CredentialKind,
  CredentialPlaceholder,
  CredentialRef,
  MissingBinding,
  NodeDefinition,
  Template,
  TemplateInstantiateResult,
  TemplateKind,
  TemplatePayload,
} from '../api/types';
import { Banner, Chip, Empty, Field, Loading, Modal, Pill, ShortId } from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { CREDENTIAL_KIND_LABELS, TEMPLATE_KIND_LABELS } from '../labels';

// ===========================================================================
// 共用的错误呈现
// ===========================================================================

function ReadError({ error, what, onRetry }: { error: ApiError; what: string; onRetry: () => void }): JSX.Element {
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接后台服务' : `无法读取${what}`}
      hint={
        error.unreachable
          ? '请确认后台服务已启动，然后点重试。连接失败期间这里不会显示任何内容。'
          : error.hint ?? undefined
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

function SubmitError({ error, what }: { error: ApiError; what: string }): JSX.Element {
  return (
    <Banner variant="danger" title={error.unreachable ? '无法连接后台服务' : what}>
      <span className="mono text-xs">{error.detail}</span>
    </Banner>
  );
}

// ===========================================================================
// 小工具
// ===========================================================================

/**
 * payload 是模板的实体内容。类型上它一定存在，但真缺了的时候要显示「—」而不是
 * 把它当成 0 个节点——「没有节点」和「读不到」不是一回事。
 */
function payloadOf(template: Template): TemplatePayload | null {
  const payload: TemplatePayload | undefined = template.payload;
  return payload ?? null;
}

function kindLabelOf(kind: string | null): string | null {
  if (kind === null) return null;
  if (Object.prototype.hasOwnProperty.call(CREDENTIAL_KIND_LABELS, kind)) {
    return CREDENTIAL_KIND_LABELS[kind as CredentialKind];
  }
  return kind;
}

function slotsTitle(slots: CredentialPlaceholder[]): string {
  const described = slots.map((slot) => {
    const kind = kindLabelOf(slot.original_kind);
    const label = slot.original_label ?? '未命名';
    return kind ? `${slot.slot}（原 ${label} · ${kind}）` : `${slot.slot}（原 ${label}）`;
  });
  return `使用模板创建流程时，这些凭据需要重新选择：${described.join('、')}`;
}

// ===========================================================================
// 页面
// ===========================================================================

export function TemplatesPage(): JSX.Element {
  const templates = useAsync(templatesApi.list, []);
  const [createOpen, setCreateOpen] = useState(false);
  const [bindTarget, setBindTarget] = useState<Template | null>(null);
  const [outcome, setOutcome] = useState<{ template: Template; result: TemplateInstantiateResult } | null>(null);
  const [removing, setRemoving] = useState<Template | null>(null);
  const submit = useSubmit();

  const rows: Template[] = templates.data?.templates ?? [];

  const runInstantiate = async (template: Template, bindings: Record<string, string>): Promise<void> => {
    const result = await submit.run(() => templatesApi.instantiate(template.template_id, { bindings }));
    if (result) {
      setBindTarget(null);
      setOutcome({ template, result });
    }
  };

  const startInstantiate = (template: Template): void => {
    const slots = payloadOf(template)?.sensitive_slots ?? [];
    if (slots.length > 0) {
      // 有槽位就先绑；不绑等于把「没有凭据」这件事丢给运行期才发现。
      setBindTarget(template);
      return;
    }
    void runInstantiate(template, {});
  };

  const confirmRemove = async (template: Template): Promise<void> => {
    const done = await submit.run(async () => {
      await templatesApi.remove(template.template_id);
      return true;
    });
    if (done) {
      setRemoving(null);
      templates.reload();
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>模板</h1>
          <div className="page-head__sub">
            模板把一套流程结构保存下来重复使用：包含节点、连线和配置，但不保存凭据，也不保存运行中的内容。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={templates.reload}>
            刷新
          </button>
          <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
            生成模板
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel__head">模板列表</div>

        {submit.error && !bindTarget && !removing ? (
          <div style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
            <SubmitError error={submit.error} what="操作失败" />
          </div>
        ) : null}

        {templates.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={templates.error} what="模板列表" onRetry={templates.reload} />
          </div>
        ) : !templates.loaded ? (
          <Loading label="加载模板列表" />
        ) : rows.length === 0 ? (
          <Empty
            title="还没有任何模板"
            hint="从已有流程生成模板时，凭据不会存进模板；用模板创建流程时需要重新选择凭据。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
                生成模板
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>template_id</th>
                  <th>类型</th>
                  <th className="table__num">版本</th>
                  <th>来源流程</th>
                  <th className="table__num">来源修订</th>
                  <th className="table__num">节点数</th>
                  <th className="table__num">边数</th>
                  <th>凭据</th>
                  <th style={{ width: 150 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((template) => {
                  const payload = payloadOf(template);
                  const slots = payload ? payload.sensitive_slots : null;
                  return (
                    <tr key={template.template_id}>
                      <td>
                        <div>{template.name}</div>
                        {template.description ? (
                          <div className="text-xs dim truncate" style={{ maxWidth: 320 }} title={template.description}>
                            {template.description}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <ShortId id={template.template_id} />
                      </td>
                      <td>{TEMPLATE_KIND_LABELS[template.kind]}</td>
                      <td className="table__num">v{template.version}</td>
                      <td>
                        {template.source_workflow_id ? (
                          <ShortId id={template.source_workflow_id} />
                        ) : (
                          <span className="dim">—</span>
                        )}
                      </td>
                      <td className="table__num">
                        {template.source_revision === null ? (
                          <span className="dim">—</span>
                        ) : (
                          template.source_revision
                        )}
                      </td>
                      <td className="table__num">
                        {payload ? payload.nodes.length : <span className="dim">—</span>}
                      </td>
                      <td className="table__num">
                        {payload ? payload.edges.length : <span className="dim">—</span>}
                      </td>
                      <td>
                        {slots === null ? (
                          <span className="dim" title="后台服务未返回模板内容">
                            —
                          </span>
                        ) : slots.length === 0 ? (
                          <span className="dim">0</span>
                        ) : (
                          <div className="row row--tight">
                            <span className="mono text-xs">{slots.length}</span>
                            <Chip variant="warn" title={slotsTitle(slots)}>
                              需重新选择
                            </Chip>
                          </div>
                        )}
                      </td>
                      <td>
                        <div className="row row--tight">
                          <button
                            type="button"
                            className="btn btn--sm btn--primary"
                            disabled={submit.busy}
                            onClick={() => startInstantiate(template)}
                          >
                            使用模板
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm"
                            disabled={submit.busy}
                            onClick={() => setRemoving(template)}
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {createOpen ? (
        <CreateTemplateModal
          onClose={() => setCreateOpen(false)}
          onSaved={() => {
            setCreateOpen(false);
            templates.reload();
          }}
        />
      ) : null}

      {bindTarget ? (
        <BindModal
          template={bindTarget}
          slots={payloadOf(bindTarget)?.sensitive_slots ?? []}
          busy={submit.busy}
          error={submit.error}
          onClose={() => {
            setBindTarget(null);
            submit.clear();
          }}
          onConfirm={(bindings) => void runInstantiate(bindTarget, bindings)}
        />
      ) : null}

      {outcome ? (
        <InstantiateResultModal
          template={outcome.template}
          result={outcome.result}
          onClose={() => setOutcome(null)}
        />
      ) : null}

      {removing ? (
        <RemoveModal
          template={removing}
          busy={submit.busy}
          error={submit.error}
          onClose={() => {
            setRemoving(null);
            submit.clear();
          }}
          onConfirm={() => void confirmRemove(removing)}
        />
      ) : null}
    </div>
  );
}

// ===========================================================================
// 生成模板
// ===========================================================================

function CreateTemplateModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }): JSX.Element {
  const workflows = useAsync(workflowApi.list, []);
  const [fromWorkflowId, setFromWorkflowId] = useState('');
  const [fromRevision, setFromRevision] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [kind, setKind] = useState<TemplateKind>('workflow');
  const [keepCredentials, setKeepCredentials] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const workflowRows = workflows.data?.workflows ?? [];

  const save = async (): Promise<void> => {
    if (!fromWorkflowId) {
      setFormError('请选择来源流程：模板的内容取自所选流程的某个修订。');
      return;
    }
    if (!name.trim()) {
      setFormError('名称必填。');
      return;
    }
    let revisionSeq: number | null = null;
    if (fromRevision.trim()) {
      const parsed = Number(fromRevision.trim());
      if (!Number.isInteger(parsed) || parsed <= 0) {
        setFormError('来源修订必须是正整数；留空表示取最新修订。');
        return;
      }
      revisionSeq = parsed;
    }
    setFormError(null);
    const saved = await submit.run(() =>
      templatesApi.create({
        name: name.trim(),
        description: description.trim() || null,
        kind,
        from_workflow_id: fromWorkflowId,
        from_revision_seq: revisionSeq,
        keep_credential_refs: keepCredentials,
      }),
    );
    if (saved) onSaved();
  };

  return (
    <Modal
      title="生成模板"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn--sm btn--primary" disabled={submit.busy} onClick={() => void save()}>
            {submit.busy ? '生成中…' : '生成'}
          </button>
        </>
      }
    >
      <Banner variant="info" title="密钥不会存进模板">
        模板只保存流程的结构和配置，<strong>密钥本体永远不会被保存</strong>。模板里只保留凭据的引用（指向本机凭据库的一条记录），之后用模板创建流程时会自动绑定同名凭据。模板不保存运行中的内容（进行中的会话、审批、执行结果等）。
      </Banner>

      {formError ? (
        <Banner variant="danger" title="无法提交">
          {formError}
        </Banner>
      ) : null}
      {submit.error ? <SubmitError error={submit.error} what="生成模板失败" /> : null}
      {workflows.error ? <ReadError error={workflows.error} what="流程列表" onRetry={workflows.reload} /> : null}

      <div className="field-row">
        <Field label="来源流程">
          {workflows.loading && !workflows.loaded ? (
            <span className="text-sm dim">加载流程列表…</span>
          ) : (
            <select className="select" value={fromWorkflowId} onChange={(e) => setFromWorkflowId(e.target.value)}>
              <option value="">请选择流程</option>
              {workflowRows.map((workflow) => (
                <option key={workflow.workflow_id} value={workflow.workflow_id}>
                  {workflow.name}（修订 {workflow.current_revision_seq}）
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="来源修订" hint="留空表示取该流程的最新修订。">
          <input
            className="input input--mono input--num"
            value={fromRevision}
            onChange={(e) => setFromRevision(e.target.value)}
            placeholder="最新"
          />
        </Field>
      </div>

      <div className="field-row" style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="名称">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="标准三节点流水线" />
        </Field>
        <Field label="类型">
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value as TemplateKind)}>
            {(Object.keys(TEMPLATE_KIND_LABELS) as TemplateKind[]).map((value) => (
              <option key={value} value={value}>
                {TEMPLATE_KIND_LABELS[value]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="描述">
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="这个模板适用于什么场景"
          />
        </Field>
      </div>

      <label className="check" style={{ marginTop: 'var(--sp-3)' }}>
        <input type="checkbox" checked={keepCredentials} onChange={(e) => setKeepCredentials(e.target.checked)} />
        保留凭据引用（本机复用时自动绑定；要把模板分享给别人用时取消勾选）
      </label>
    </Modal>
  );
}

// ===========================================================================
// 实例化：绑定凭据槽位（TPL-03）
// ===========================================================================

/** 槽位对应的模板节点上仍保留的凭据引用（同机复用时自动绑定的来源）。 */
function carriedRefOf(template: Template, slot: string): string | null {
  const m = /^(.*)\.profiles\[(\d+)\]\.credential_ref$/.exec(slot);
  if (!m) return null;
  const cfg = template.payload.nodes.find((n) => n.name === m[1]);
  const ref = cfg?.profiles?.[Number(m[2])]?.credential_ref;
  return typeof ref === 'string' && ref ? ref : null;
}

function BindModal({
  template,
  slots,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  template: Template;
  slots: CredentialPlaceholder[];
  busy: boolean;
  error: ApiError | null;
  onClose: () => void;
  onConfirm: (bindings: Record<string, string>) => void;
}): JSX.Element {
  const credentials = useAsync(registryApi.credentials, []);
  const [bindings, setBindings] = useState<Record<string, string>>({});

  const credentialRows: CredentialRef[] = (credentials.data ?? []).filter((item) => !item.revoked);

  const setBinding = (slot: string, value: string): void => {
    setBindings((prev) => ({ ...prev, [slot]: value }));
  };

  const confirm = (): void => {
    const payload: Record<string, string> = {};
    for (const slot of slots) {
      const value = bindings[slot.slot] ?? '';
      // 选了「不绑定」就不发这个键：让内核如实把它报成 missing_binding，而不是替它编一个。
      if (value) payload[slot.slot] = value;
    }
    onConfirm(payload);
  };

  return (
    <Modal
      wide
      title={`使用模板 · ${template.name}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn--sm btn--primary" disabled={busy} onClick={confirm}>
            {busy ? '创建中…' : '从模板创建'}
          </button>
        </>
      }
    >
      <Banner variant="info" title="凭据只保存引用，不保存密钥">
        模板里保留的凭据引用会自动绑定本机同名凭据；只有本机找不到（比如模板来自别的机器）的项才需要重新选择。
      </Banner>

      {error ? <SubmitError error={error} what="使用模板失败" /> : null}
      {credentials.error ? <ReadError error={credentials.error} what="凭据列表" onRetry={credentials.reload} /> : null}

      <div className="table-wrap">
        <table className="table table--dense">
          <thead>
            <tr>
              <th>位置</th>
              <th>原凭据</th>
              <th style={{ minWidth: 260 }}>选择本机凭据</th>
            </tr>
          </thead>
          <tbody>
            {slots.map((slot) => {
              const kind = kindLabelOf(slot.original_kind);
              const carried = carriedRefOf(template, slot.slot);
              const carriedUsable =
                carried !== null && credentialRows.some((c) => c.credential_id === carried);
              const carriedLabel = carriedUsable
                ? (credentialRows.find((c) => c.credential_id === carried)?.label ?? carried)
                : null;
              return (
                <tr key={slot.slot}>
                  <td className="mono text-xs">{slot.slot}</td>
                  <td>
                    <div>{slot.original_label ?? <span className="dim">未命名</span>}</div>
                    {kind ? <div className="text-xs dim">{kind}</div> : null}
                    {carriedUsable ? (
                      <div className="text-xs text-success">引用已保留，将自动绑定「{carriedLabel}」</div>
                    ) : carried !== null ? (
                      <div className="text-xs text-warn">原凭据在本机不存在或已撤销，请重新选择</div>
                    ) : null}
                  </td>
                  <td>
                    {credentials.loading && !credentials.loaded ? (
                      <span className="spin" />
                    ) : (
                      <select
                        className="select"
                        value={bindings[slot.slot] ?? (carriedUsable ? carried : '')}
                        onChange={(e) => setBinding(slot.slot, e.target.value)}
                      >
                        <option value="">
                          {carriedUsable ? '改选…（不选则用自动绑定）' : '不选择（创建结果会标记为未绑定）'}
                        </option>
                        {credentialRows.map((credential) => (
                          <option key={credential.credential_id} value={credential.credential_id}>
                            {credential.label}（{CREDENTIAL_KIND_LABELS[credential.kind]}）
                          </option>
                        ))}
                      </select>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="text-xs dim" style={{ marginTop: 6 }}>
        只列出未撤销的凭据。要新建凭据请到「注册表 › 凭据」；这里只选择已有凭据，不输入密钥。
      </div>
    </Modal>
  );
}

// ===========================================================================
// 实例化结果
// ===========================================================================

function missingBindingLine(binding: MissingBinding, index: number): JSX.Element {
  return (
    <li key={`${binding.slot}-${index}`} className="text-sm">
      <span className="mono text-xs">{binding.slot}</span>
      {binding.label ? <span> · {binding.label}</span> : null}
      <span className="text-warn"> · {binding.reason}</span>
    </li>
  );
}

function NodeTable({ nodes }: { nodes: NodeDefinition[] }): JSX.Element {
  return (
    <div className="table-wrap">
      <table className="table table--dense">
        <thead>
          <tr>
            <th>节点名</th>
            <th>角色</th>
            <th>模型候选</th>
            <th>凭据</th>
            <th>必需输入</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((node) => {
            const models = node.profiles.map((profile) => profile.model_name);
            const refs = node.profiles
              .map((profile) => profile.credential_ref)
              .filter((ref): ref is string => ref !== null);
            return (
              <tr key={node.node_id}>
                <td>{node.name}</td>
                <td>{node.role ?? <span className="dim">—</span>}</td>
                <td>
                  {models.length > 0 ? (
                    <span className="mono text-xs">{models.join(' / ')}</span>
                  ) : (
                    <span className="dim">—</span>
                  )}
                </td>
                <td>
                  {refs.length > 0 ? (
                    <span className="row row--tight">
                      {refs.map((ref) => (
                        <ShortId key={ref} id={ref} />
                      ))}
                    </span>
                  ) : (
                    <span className="text-warn">未绑定凭据</span>
                  )}
                </td>
                <td>
                  {node.required_inputs.length > 0 ? (
                    <span className="mono text-xs">{node.required_inputs.join(', ')}</span>
                  ) : (
                    <span className="dim">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function InstantiateResultModal({
  template,
  result,
  onClose,
}: {
  template: Template;
  result: TemplateInstantiateResult;
  onClose: () => void;
}): JSX.Element {
  const missing = result.report.missing_bindings;
  const report = result.report;
  return (
    <Modal
      wide
      title={`创建结果 · ${template.name}`}
      onClose={onClose}
      footer={
        <button type="button" className="btn btn--sm" onClick={onClose}>
          关闭
        </button>
      }
    >
      {missing.length > 0 ? (
        <Banner
          variant="warn"
          title="有凭据未绑定"
          hint="这次创建没有为它们选择凭据，系统也不会替你编造。选好凭据后请重新从模板创建。"
        >
          <div>
            以下凭据未绑定，这次创建的流程<strong>还不能用</strong>：
            <ul className="list-reset" style={{ marginTop: 4 }}>
              {missing.map((binding, index) => missingBindingLine(binding, index))}
            </ul>
          </div>
        </Banner>
      ) : null}

      <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
        <span className="text-sm muted">是否可用</span>
        <Pill tone={result.usable ? 'success' : 'danger'}>{result.usable ? '可用' : '不可用'}</Pill>
        <span className="text-xs dim">（本次创建的整体结论）</span>
      </div>

      <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
        <span className="text-sm muted">检查报告</span>
        <Pill tone={report.usable ? 'success' : 'danger'}>{report.usable ? '可用' : '不可用'}</Pill>
        <span className="text-xs dim">（报告中的结论）</span>
        {report.usable !== result.usable ? (
          <span className="text-xs text-warn">两个结论不一致时以检查报告为准，报告里列出了未绑定的凭据。</span>
        ) : null}
      </div>

      <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
        后台返回的说明
      </div>
      {report.notes.length === 0 ? (
        <div className="text-sm dim">后台服务没有返回说明。</div>
      ) : (
        <ul className="list-reset">
          {report.notes.map((note, index) => (
            <li key={index} className="text-sm">
              · {note}
            </li>
          ))}
        </ul>
      )}

      <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
        创建出的流程结构（节点 {result.nodes.length} / 连线 {result.edges.length}）
      </div>
      {result.nodes.length === 0 ? (
        <div className="text-sm dim">这次创建没有产出任何节点。</div>
      ) : (
        <NodeTable nodes={result.nodes} />
      )}

      <Banner variant="info" title="这还只是草稿">
        这次创建只生成了流程草稿，还没有保存成正式流程；请在编辑器中确认后再保存。
      </Banner>
    </Modal>
  );
}

// ===========================================================================
// 删除模板
// ===========================================================================

function RemoveModal({
  template,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  template: Template;
  busy: boolean;
  error: ApiError | null;
  onClose: () => void;
  onConfirm: () => void;
}): JSX.Element {
  const payload = payloadOf(template);
  const slotCount = payload ? payload.sensitive_slots.length : 0;
  return (
    <Modal
      title={`删除模板 · ${template.name}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn--sm btn--danger" disabled={busy} onClick={onConfirm}>
            {busy ? '删除中…' : '确认删除'}
          </button>
        </>
      }
    >
      <Banner variant="warn" title="删除只影响模板本身">
        删除模板不会改动已经用它创建的流程。
      </Banner>
      {error ? <SubmitError error={error} what="删除模板失败" /> : null}
      <div className="text-sm muted">
        已创建的流程保留自己创建时的内容和记录，之后模板的任何改动都不会影响它们。
      </div>
      {slotCount > 0 ? (
        <div className="text-sm muted" style={{ marginTop: 'var(--sp-2)' }}>
          该模板有 {slotCount} 处凭据需要使用时重新选择；删除模板不影响已创建的流程里选好的凭据。
        </div>
      ) : null}
    </Modal>
  );
}
