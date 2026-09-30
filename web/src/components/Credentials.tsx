/**
 * 凭据面板：列表 + 新建 + 撤销（含影响面扫描）。
 *
 * 共享给两个页面：注册表（/registry）的「凭据」页签，模板（/templates）的
 * 「凭据模板」页签。两处操作的是同一个凭据库——凭据模板不是新实体，
 * 只是「保存一次、到处引用」这个用途在模板页的露出。
 */

import { useEffect, useState } from 'react';
import { ApiError } from '../api/client';
import { registry as registryApi, workflows as workflowApi } from '../api/endpoints';
import type { CredentialKind, CredentialRef } from '../api/types';
import { Banner, Empty, Field, Loading, Modal, Pill, ShortId } from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { CREDENTIAL_KIND_LABELS } from '../labels';

// ---------------------------------------------------------------------------
// 错误呈现（与各页面同款：连不上服务和「列表为空」是两件事，不混着画）
// ---------------------------------------------------------------------------

function ReadError({ error, what, onRetry }: { error: ApiError; what: string; onRetry: () => void }): JSX.Element {
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接后台服务' : `无法读取${what}`}
      hint={error.unreachable ? '请确认后台服务已启动，然后点重试。' : (error.hint ?? undefined)}
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
// 凭据
// ===========================================================================

export function CredentialsTab({ enabled }: { enabled: boolean }): JSX.Element {
  const credentials = useAsync(registryApi.credentials, [], { enabled });
  const [createOpen, setCreateOpen] = useState(false);
  const [revoking, setRevoking] = useState<CredentialRef | null>(null);
  const submit = useSubmit();

  const rows = credentials.data ?? [];

  const restore = async (credential: CredentialRef): Promise<void> => {
    const saved = await submit.run(() => registryApi.revokeCredential(credential.credential_id, false));
    if (saved) credentials.reload();
  };

  const confirmRevoke = async (credential: CredentialRef): Promise<void> => {
    const saved = await submit.run(() => registryApi.revokeCredential(credential.credential_id, true));
    if (saved) {
      setRevoking(null);
      credentials.reload();
    }
  };

  return (
    <div>
      <div className="panel">
        <div className="panel__head">
          凭据引用
          <div className="panel__head-actions">
            <button type="button" className="btn btn--sm" onClick={credentials.reload}>
              刷新
            </button>
            <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
              新建凭据
            </button>
          </div>
        </div>
        <div className="panel__hint">
          密钥加密保存在本机凭据库，界面只显示引用信息，不显示密钥本身。保存好的凭据就是可复用的「凭据模板」：配置节点候选时在「凭据」下拉里引用它，模型名留空会自动用凭据的默认模型。
        </div>

        {submit.error && !revoking ? (
          <div style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
            <SubmitError error={submit.error} what="操作失败" />
          </div>
        ) : null}

        {credentials.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={credentials.error} what="凭据列表" onRetry={credentials.reload} />
          </div>
        ) : !credentials.loaded ? (
          <Loading label="加载凭据列表" />
        ) : rows.length === 0 ? (
          <Empty
            title="还没有任何凭据"
            hint="新建一份凭据后，配置节点时就可以选择用它调用模型服务；不选则默认使用 harness 本机的登录状态。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
                新建凭据
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>ID</th>
                  <th>认证方式</th>
                  <th>服务地址</th>
                  <th>默认模型</th>
                  <th>状态</th>
                  <th style={{ width: 90 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((credential) => (
                  <tr key={credential.credential_id}>
                    <td>{credential.label}</td>
                    <td>
                      <ShortId id={credential.credential_id} />
                    </td>
                    <td>{CREDENTIAL_KIND_LABELS[credential.kind]}</td>
                    <td>
                      {credential.base_url ? (
                        <span className="mono text-xs" title={credential.base_url}>
                          {credential.base_url}
                        </span>
                      ) : (
                        <span className="dim">—</span>
                      )}
                    </td>
                    <td>
                      {credential.default_model ? (
                        <span className="mono text-xs" title={credential.default_model}>
                          {credential.default_model}
                        </span>
                      ) : (
                        <span className="dim">—</span>
                      )}
                    </td>
                    <td>
                      {credential.revoked ? <Pill tone="danger">已撤销</Pill> : <Pill tone="success">有效</Pill>}
                    </td>
                    <td>
                      {credential.revoked ? (
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={submit.busy}
                          onClick={() => void restore(credential)}
                        >
                          恢复
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          disabled={submit.busy}
                          onClick={() => setRevoking(credential)}
                        >
                          撤销
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {createOpen ? (
        <CreateCredentialModal
          onClose={() => setCreateOpen(false)}
          onSaved={() => {
            setCreateOpen(false);
            credentials.reload();
          }}
        />
      ) : null}

      {revoking ? (
        <Modal
          wide
          title={`撤销凭据 · ${revoking.label}`}
          onClose={() => setRevoking(null)}
          footer={
            <>
              <button type="button" className="btn btn--sm" onClick={() => setRevoking(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--sm btn--danger"
                disabled={submit.busy}
                onClick={() => void confirmRevoke(revoking)}
              >
                {submit.busy ? '撤销中…' : '确认撤销'}
              </button>
            </>
          }
        >
          <Banner variant="warn" title="撤销的影响">
            撤销后，引用该凭据的节点在下一次执行前需要重新选择凭据。正在运行的任务会完成当前请求，历史记录不变。
          </Banner>

          {submit.error ? <SubmitError error={submit.error} what="撤销失败" /> : null}

          <div className="section-title">引用该凭据的节点</div>
          <ImpactScan credentialId={revoking.credential_id} />
        </Modal>
      ) : null}
    </div>
  );
}

function CreateCredentialModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }): JSX.Element {
  const [label, setLabel] = useState('');
  const [auth, setAuth] = useState<'login' | 'key'>('key');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [defaultModel, setDefaultModel] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const isLogin = auth === 'login';

  const save = async (): Promise<void> => {
    if (!label.trim()) {
      setFormError('名称必填。');
      return;
    }
    if (!isLogin && !apiKey.trim()) {
      setFormError('请填写 API Key。');
      return;
    }
    setFormError(null);
    // 类型由填写内容推导：填了服务地址就是 base_url_pair，否则是 api_key。
    // 不向用户暴露这个枚举——它对行为的影响只有「注入哪些环境变量」。
    const kind: CredentialKind = isLogin ? 'harness_login' : baseUrl.trim() ? 'base_url_pair' : 'api_key';
    const secret: Record<string, string> | null = isLogin
      ? null
      : baseUrl.trim()
        ? { api_key: apiKey.trim(), base_url: baseUrl.trim() }
        : { api_key: apiKey.trim() };
    const saved = await submit.run(() =>
      registryApi.createCredential({
        label: label.trim(),
        kind,
        base_url: baseUrl.trim() || null,
        default_model: defaultModel.trim() || null,
        secret,
      }),
    );
    if (saved) onSaved();
  };

  return (
    <Modal
      title="新建凭据"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn--sm btn--primary" disabled={submit.busy} onClick={() => void save()}>
            {submit.busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {formError ? (
        <Banner variant="danger" title="无法提交">
          {formError}
        </Banner>
      ) : null}
      {submit.error ? <SubmitError error={submit.error} what="新建凭据失败" /> : null}

      <Field label="名称" required hint="给自己看的名字，建节点时按名字选它">
        <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="如：公司 Claude 账号" />
      </Field>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="怎么认证" required>
          <select className="select" value={auth} onChange={(e) => setAuth(e.target.value as 'login' | 'key')}>
            <option value="key">填写 API Key（key 会加密保存在本机）</option>
            <option value="login">用 harness 在这台机器上的登录状态（不保存任何 key）</option>
          </select>
        </Field>
      </div>

      {isLogin ? (
        <div style={{ marginTop: 'var(--sp-3)' }} className="text-sm dim">
          什么都不用填：执行时直接使用 harness 当前的登录状态（比如已经跑过 kimi login / claude 登录）。
        </div>
      ) : (
        <>
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <Field label="API Key" required hint="加密后保存；保存后这里、日志和历史记录都不会再显示它。">
              <input
                className="input input--mono"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-..."
              />
            </Field>
          </div>

          <div style={{ marginTop: 'var(--sp-3)' }}>
            <Field label="服务地址" hint="可留空。只有走第三方网关或自建服务时才填，用官方服务就留空。">
              <input
                className="input input--mono"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.example.com/v1"
              />
            </Field>
          </div>

          <div style={{ marginTop: 'var(--sp-3)' }}>
            <Field label="默认模型" hint="可留空。节点没填模型名时会用这个值，这样一份凭据就是完整的接入配置。">
              <input
                className="input input--mono"
                value={defaultModel}
                onChange={(e) => setDefaultModel(e.target.value)}
                placeholder="如 claude-sonnet-4-6 / kimi-k2"
              />
            </Field>
          </div>
        </>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// 影响面扫描（AUTH-02 / CFG-07 / EXT-03）
// ---------------------------------------------------------------------------

interface ImpactNode {
  nodeId: string;
  nodeName: string;
}

interface ImpactEntry {
  workflowId: string;
  workflowName: string;
  nodes: ImpactNode[];
}

interface ImpactScan {
  loading: boolean;
  entries: ImpactEntry[];
  /** 读取失败的流程名。非空即「影响面可能不完整」，必须显式说出来。 */
  failed: string[];
  total: number;
  error: string | null;
}

/**
 * 列表接口不提供「哪些节点引用了这个凭据」，所以自己算：
 * 遍历每个流程的最新修订，收集 profiles[].credential_ref === credential_id 的节点。
 * 单个流程读失败不吞掉——记下来，宁可承认不完整，也不假装扫全了。
 */
async function scanCredentialImpact(credentialId: string): Promise<ImpactScan> {
  const list = await workflowApi.list();
  const results = await Promise.all(
    list.workflows.map(async (workflow) => {
      try {
        const revisions = await workflowApi.revisions(workflow.workflow_id);
        const sorted = [...revisions.revisions].sort((a, b) => b.revision_seq - a.revision_seq);
        const latest = sorted[0];
        if (!latest) return { ok: true as const, entry: null };
        const nodes = latest.graph.nodes
          .filter((node) => node.profiles.some((profile) => profile.credential_ref === credentialId))
          .map((node) => ({ nodeId: node.node_id, nodeName: node.name }));
        const entry: ImpactEntry | null =
          nodes.length > 0
            ? { workflowId: workflow.workflow_id, workflowName: workflow.name, nodes }
            : null;
        return { ok: true as const, entry };
      } catch {
        return { ok: false as const, name: workflow.name };
      }
    }),
  );

  const entries: ImpactEntry[] = [];
  const failed: string[] = [];
  for (const result of results) {
    if (result.ok) {
      if (result.entry) entries.push(result.entry);
    } else {
      failed.push(result.name);
    }
  }
  return { loading: false, entries, failed, total: list.workflows.length, error: null };
}

function ImpactScan({ credentialId }: { credentialId: string }): JSX.Element {
  const [nonce, setNonce] = useState(0);
  const [scan, setScan] = useState<ImpactScan>({
    loading: true,
    entries: [],
    failed: [],
    total: 0,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    setScan({ loading: true, entries: [], failed: [], total: 0, error: null });
    void (async () => {
      try {
        const result = await scanCredentialImpact(credentialId);
        if (!cancelled) setScan(result);
      } catch (err: unknown) {
        if (cancelled) return;
        setScan({
          loading: false,
          entries: [],
          failed: [],
          total: 0,
          error:
            err instanceof ApiError ? (err.unreachable ? '无法连接后台服务' : err.detail) : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [credentialId, nonce]);

  if (scan.loading) return <Loading label="正在检查哪些流程引用了该凭据" />;

  if (scan.error !== null) {
    return (
      <Banner
        variant="danger"
        title="无法检查引用情况"
        hint="流程列表读取失败，无法判断哪些流程还在引用该凭据。"
        actions={
          <button type="button" className="btn btn--sm" onClick={() => setNonce((n) => n + 1)}>
            重试
          </button>
        }
      >
        <span className="mono text-xs">{scan.error}</span>
      </Banner>
    );
  }

  return (
    <div>
      {scan.failed.length > 0 ? (
        <Banner
          variant="warn"
          title="部分流程读取失败，以下结果可能不完整"
          hint="以下流程读取失败，无法确认它们是否引用该凭据。"
        >
          <span className="mono text-xs">{scan.failed.join('、')}</span>
        </Banner>
      ) : null}

      {scan.entries.length === 0 ? (
        <Empty
          title="已读取的流程里没有节点引用该凭据"
          hint={`已检查 ${scan.total} 个流程的最新版本；历史运行记录中的引用不在检查范围内。`}
        />
      ) : (
        <div>
          {scan.entries.map((entry) => (
            <div key={entry.workflowId} style={{ marginBottom: 'var(--sp-2)' }}>
              <div className="text-sm">
                {entry.workflowName} <ShortId id={entry.workflowId} />
              </div>
              <ul className="list-reset" style={{ paddingLeft: 'var(--sp-3)' }}>
                {entry.nodes.map((node) => (
                  <li key={node.nodeId} className="text-sm">
                    <span className="dim">›</span> {node.nodeName} <ShortId id={node.nodeId} />
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      <div className="text-xs dim" style={{ marginTop: 6 }}>
        检查范围：{scan.total} 个流程的最新版本。历史运行记录不在其中，撤销不会改写它们。
      </div>
    </div>
  );
}
