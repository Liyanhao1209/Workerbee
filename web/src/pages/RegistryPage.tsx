/**
 * 注册表（/registry）：Harness、凭据、Skill、MCP 工具四类共享配置。
 *
 * 这个页面的第一职责是「如实」：
 *   - 能力没探测过就写「未探测」，显式不支持就写「不支持」——任何留白都会被读成「支持」；
 *   - 凭据只登记引用，密钥本体不进前端，也不回显；
 *   - 撤销凭据前必须把影响面摆出来。列表接口不提供「谁引用了它」，所以这里扫描
 *     各流程最新修订的图，自己算出来；算不全就明说算不全。
 */

import { useEffect, useState } from 'react';
import { ApiError } from '../api/client';
import { registry as registryApi, workflows as workflowApi } from '../api/endpoints';
import { HARNESS_CAPABILITY_KEYS } from '../api/types';
import type {
  CredentialKind,
  CredentialRef,
  HarnessRegistration,
  ProbeResult,
  SkillDoc,
  SkillScope,
  ToolSpec,
} from '../api/types';
import {
  Banner,
  Chip,
  Disclosure,
  Empty,
  Field,
  KV,
  Loading,
  Modal,
  Pill,
  RelTime,
  ShortId,
  TimeText,
} from '../components/common';
import { useAsync, useSubmit } from '../hooks/useAsync';
import {
  APPROVAL_POLICY_LABELS,
  AUTH_MODE_LABELS,
  CAPABILITY_LABELS,
  CREDENTIAL_KIND_LABELS,
  RISK_LEVEL_LABELS,
  SKILL_SCOPE_LABELS,
} from '../labels';

// ===========================================================================
// 共用的错误呈现
// ===========================================================================

/**
 * 读取失败时**不渲染空表**：内核没起来与「列表本来就是空」是两件事，
 * 把前者画成后者等于告诉用户「你没配过任何东西」。
 */
function ReadError({
  error,
  what,
  onRetry,
}: {
  error: ApiError;
  what: string;
  onRetry: () => void;
}): JSX.Element {
  return (
    <Banner
      variant="danger"
      title={error.unreachable ? '无法连接内核' : `无法读取${what}`}
      hint={
        error.unreachable
          ? '这不是「没有数据」，而是读取失败——所以下面不会显示任何占位表格。'
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
    <Banner variant="danger" title={error.unreachable ? '无法连接内核' : what}>
      <span className="mono text-xs">{error.detail}</span>
    </Banner>
  );
}

// ===========================================================================
// 能力矩阵（HAR-02）
// ===========================================================================

/** 单元格的取值只有这五种，每一种都必须能落在界面上，不能有「空白」这一种。 */
type CapabilityCell =
  | { kind: 'unprobed' }
  | { kind: 'supported' }
  | { kind: 'unsupported' }
  | { kind: 'undeclared' }
  | { kind: 'limited'; detail: string };

const UNPROBED_NOTE = '尚未探测，能力未知；请先运行探测。';

function stringifyValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value);
  }
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  try {
    const json: unknown = JSON.stringify(value);
    return typeof json === 'string' ? json : String(value);
  } catch {
    return String(value);
  }
}

function capabilityCell(snapshot: Record<string, unknown> | null, key: string): CapabilityCell {
  if (snapshot === null) return { kind: 'unprobed' };
  if (!Object.prototype.hasOwnProperty.call(snapshot, key)) return { kind: 'undeclared' };
  const value = snapshot[key];
  if (value === true) return { kind: 'supported' };
  if (value === false) return { kind: 'unsupported' };
  // 非布尔取值 = 适配器在报「有条件的支持」。如实显示「受限」并把原文放进悬停提示。
  return { kind: 'limited', detail: stringifyValue(value) };
}

function CapabilityCellView({ cell }: { cell: CapabilityCell }): JSX.Element {
  switch (cell.kind) {
    case 'unprobed':
      return <Pill tone="idle" title={UNPROBED_NOTE}>未探测</Pill>;
    case 'supported':
      return <Pill tone="success" title="capabilities_snapshot 中该项为 true。">支持</Pill>;
    case 'unsupported':
      return <Pill tone="danger" title="capabilities_snapshot 中该项显式为 false——是「不支持」，不是「没声明」。">不支持</Pill>;
    case 'undeclared':
      return <Pill tone="idle" title="适配器未在 manifest 中声明该项。">未声明</Pill>;
    case 'limited':
      return <Pill tone="warn" title={`适配器返回了非布尔取值：${cell.detail}`}>受限</Pill>;
  }
}

function CapabilityMatrix({ harnesses }: { harnesses: HarnessRegistration[] }): JSX.Element {
  return (
    <div className="table-wrap">
      <table className="table table--dense">
        <thead>
          <tr>
            <th>Harness</th>
            {HARNESS_CAPABILITY_KEYS.map((key) => (
              <th key={key} title={key}>
                {CAPABILITY_LABELS[key] ?? key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {harnesses.map((harness) => (
            <tr key={harness.harness_id}>
              <td>
                <div className="nowrap">{harness.name}</div>
                <div className="text-xs dim mono" title={harness.adapter_id}>
                  {harness.adapter_id}
                </div>
                {harness.capabilities_snapshot === null ? (
                  <div className="text-xs text-warn">{UNPROBED_NOTE}</div>
                ) : null}
              </td>
              {HARNESS_CAPABILITY_KEYS.map((key) => (
                <td key={key}>
                  <CapabilityCellView cell={capabilityCell(harness.capabilities_snapshot, key)} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CapabilityLegend(): JSX.Element {
  return (
    <div className="row row--tight text-xs muted">
      <span className="row row--tight">
        <Pill tone="success">支持</Pill> 接口声明为 true
      </span>
      <span className="row row--tight">
        <Pill tone="danger">不支持</Pill> 显式声明为 false，而不是「没声明」
      </span>
      <span className="row row--tight">
        <Pill tone="idle">未声明</Pill> 适配器未在 manifest 中声明该项
      </span>
      <span className="row row--tight">
        <Pill tone="warn">受限</Pill> 返回非布尔取值，原文见悬停提示
      </span>
      <span className="row row--tight">
        <Pill tone="idle">未探测</Pill> 尚无 capabilities_snapshot
      </span>
    </div>
  );
}

/** 通用值渲染：探测结果里 capabilities 的形状不做任何假设。 */
function RenderValue({ value }: { value: unknown }): JSX.Element {
  if (value === null) return <span className="dim mono">null</span>;
  if (value === undefined) return <span className="dim">—</span>;
  if (typeof value === 'boolean') return <span className="mono">{value ? 'true' : 'false'}</span>;
  if (typeof value === 'number' || typeof value === 'string') {
    const text = String(value);
    if (text.length > 60 || text.includes('\n')) {
      return <pre className="code-block">{text}</pre>;
    }
    return <span className="mono">{text}</span>;
  }
  return <pre className="code-block">{stringifyValue(value)}</pre>;
}

// ===========================================================================
// 表单小工具
// ===========================================================================

function envToText(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([key, value]) => `${key}=${value}`)
    .join('\n');
}

function parseEnvTemplate(text: string): { env: Record<string, string> } | { error: string } {
  const env: Record<string, string> = {};
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = (lines[i] ?? '').trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) return { error: `第 ${i + 1} 行不是 KEY=VALUE 形式` };
    const key = line.slice(0, eq).trim();
    if (!key) return { error: `第 ${i + 1} 行缺少变量名` };
    env[key] = line.slice(eq + 1).trim();
  }
  return { env };
}

/** 名称里带凭据字样的环境变量——后端会拒绝它们的明文取值。 */
const CREDENTIAL_ENV_HINTS = ['KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'];

function suspiciousEnvKeys(env: Record<string, string>): string[] {
  return Object.keys(env).filter((name) => {
    const upper = name.toUpperCase();
    return CREDENTIAL_ENV_HINTS.some((hint) => upper.includes(hint));
  });
}

// ===========================================================================
// 页面骨架与页签
// ===========================================================================

type TabKey = 'harness' | 'credentials' | 'skills' | 'tools';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'harness', label: 'Harness' },
  { key: 'credentials', label: '凭据' },
  { key: 'skills', label: 'Skills' },
  { key: 'tools', label: 'MCP 工具' },
];

export function RegistryPage(): JSX.Element {
  const [tab, setTab] = useState<TabKey>('harness');
  // 首次激活才拉数据：四个页签都只拉一次，之后切来切去不再重新请求。
  const [visited, setVisited] = useState<Set<TabKey>>(() => new Set<TabKey>(['harness']));

  const select = (key: TabKey): void => {
    setTab(key);
    setVisited((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  };

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>注册表</h1>
          <div className="page-head__sub">
            Harness、凭据、Skill、MCP 工具的共享配置。这里登记的都是**引用**：密钥本体只在
            Secret Store，能力结论只在探测之后。
          </div>
        </div>
      </div>

      <div className="row row--tight" style={{ marginBottom: 'var(--sp-3)' }}>
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={tab === item.key ? 'btn btn--sm btn--primary' : 'btn btn--sm'}
            onClick={() => select(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div style={{ display: tab === 'harness' ? 'block' : 'none' }}>
        <HarnessTab enabled={visited.has('harness')} />
      </div>
      <div style={{ display: tab === 'credentials' ? 'block' : 'none' }}>
        <CredentialsTab enabled={visited.has('credentials')} />
      </div>
      <div style={{ display: tab === 'skills' ? 'block' : 'none' }}>
        <SkillsTab enabled={visited.has('skills')} />
      </div>
      <div style={{ display: tab === 'tools' ? 'block' : 'none' }}>
        <ToolsTab enabled={visited.has('tools')} />
      </div>
    </div>
  );
}

// ===========================================================================
// Harness
// ===========================================================================

function probePill(ok: boolean | null): JSX.Element {
  if (ok === true) return <Pill tone="success">成功</Pill>;
  if (ok === false) return <Pill tone="danger">失败</Pill>;
  // null 是「还没探测过」，不是「没数据」——必须显式写出来。
  return <Pill tone="idle" title={UNPROBED_NOTE}>未探测</Pill>;
}

function HarnessTab({ enabled }: { enabled: boolean }): JSX.Element {
  const harnesses = useAsync(registryApi.harnesses, [], { enabled });
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<HarnessRegistration | null>(null);
  const [probeTarget, setProbeTarget] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<{ harness: HarnessRegistration; result: ProbeResult } | null>(null);
  const submit = useSubmit();

  // 表单一打开就需要凭据列表（auth_binding 的选项），此时补拉一次。
  const credentials = useAsync(registryApi.credentials, [], {
    enabled: enabled || formOpen || editing !== null,
  });

  const rows = harnesses.data ?? [];
  const credentialList = credentials.data ?? [];

  const probe = async (harness: HarnessRegistration): Promise<void> => {
    setProbeTarget(harness.harness_id);
    const result = await submit.run(() => registryApi.probeHarness(harness.harness_id));
    setProbeTarget(null);
    if (result) {
      setProbeResult({ harness, result });
      harnesses.reload();
    }
  };

  const toggle = async (harness: HarnessRegistration): Promise<void> => {
    const saved = await submit.run(() =>
      registryApi.patchHarness(harness.harness_id, { enabled: !harness.enabled }),
    );
    if (saved) harnesses.reload();
  };

  return (
    <div>
      <div className="panel">
        <div className="panel__head">
          Harness 登记
          <div className="panel__head-actions">
            <button type="button" className="btn btn--sm" onClick={harnesses.reload}>
              刷新
            </button>
            <button type="button" className="btn btn--sm btn--primary" onClick={() => setFormOpen(true)}>
              登记 harness
            </button>
          </div>
        </div>
        <div className="panel__hint">
          env_template 只放非凭据类环境变量；需要认证请用 auth_binding 引用凭据，不要把密钥写进环境变量。
        </div>

        {submit.error ? (
          <div style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
            <SubmitError error={submit.error} what="操作失败" />
          </div>
        ) : null}

        {harnesses.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={harnesses.error} what="Harness 列表" onRetry={harnesses.reload} />
          </div>
        ) : !harnesses.loaded ? (
          <Loading label="加载 Harness 列表" />
        ) : rows.length === 0 ? (
          <Empty
            title="尚未登记任何 harness"
            hint="登记后需要运行一次探测，能力矩阵才有依据——在此之前所有能力都显示「未探测」。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setFormOpen(true)}>
                登记 harness
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>harness_id</th>
                  <th>适配器</th>
                  <th>可执行路径</th>
                  <th>认证</th>
                  <th>启用</th>
                  <th>最近探测</th>
                  <th style={{ width: 190 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((harness) => (
                  <tr key={harness.harness_id}>
                    <td>
                      <div>{harness.name}</div>
                      {harness.cwd ? (
                        <div className="text-xs dim mono" title={harness.cwd}>
                          cwd {harness.cwd}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <ShortId id={harness.harness_id} />
                    </td>
                    <td>
                      <div className="mono text-xs">{harness.adapter_id}</div>
                      <div className="text-xs dim">
                        {harness.adapter_version ? `v${harness.adapter_version}` : '版本未声明'}
                      </div>
                    </td>
                    <td>
                      {harness.exec_path ? (
                        <span className="mono text-xs" title={harness.exec_path}>
                          {harness.exec_path}
                        </span>
                      ) : (
                        <span className="dim">由适配器自行解析</span>
                      )}
                    </td>
                    <td>
                      <div>{AUTH_MODE_LABELS[harness.auth_mode] ?? harness.auth_mode}</div>
                      <div className="text-xs">
                        {harness.auth_binding ? (
                          <ShortId id={harness.auth_binding} />
                        ) : (
                          <span className="dim" title="auth_binding 为空：认证由本机登录态提供">
                            本机登录态
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <Pill tone={harness.enabled ? 'success' : 'idle'}>
                        {harness.enabled ? '已启用' : '已停用'}
                      </Pill>
                    </td>
                    <td>
                      {probePill(harness.last_probe_ok)}
                      {harness.last_probe_at ? (
                        <div className="text-xs dim">
                          <RelTime value={harness.last_probe_at} />
                        </div>
                      ) : null}
                      {harness.last_probe_error ? (
                        <div className="text-xs text-danger" title={harness.last_probe_error}>
                          {harness.last_probe_error}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <div className="row row--tight">
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={probeTarget === harness.harness_id}
                          onClick={() => void probe(harness)}
                        >
                          {probeTarget === harness.harness_id ? '探测中…' : '探测'}
                        </button>
                        <button type="button" className="btn btn--sm" onClick={() => setEditing(harness)}>
                          编辑
                        </button>
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={submit.busy}
                          onClick={() => void toggle(harness)}
                        >
                          {harness.enabled ? '停用' : '启用'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {rows.length > 0 ? (
        <div className="panel">
          <div className="panel__head">能力矩阵（HAR-02）</div>
          <div className="panel__hint">
            <div className="text-xs">能力声明是接口支持情况，不是模型能力评级。</div>
            <div style={{ marginTop: 4 }}>
              <CapabilityLegend />
            </div>
          </div>
          <CapabilityMatrix harnesses={rows} />
        </div>
      ) : null}

      {formOpen ? (
        <HarnessFormModal
          initial={null}
          credentials={credentialList}
          onClose={() => setFormOpen(false)}
          onSaved={() => {
            setFormOpen(false);
            harnesses.reload();
          }}
        />
      ) : null}

      {editing ? (
        <HarnessFormModal
          initial={editing}
          credentials={credentialList}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            harnesses.reload();
          }}
        />
      ) : null}

      {probeResult ? (
        <ProbeResultModal
          harness={probeResult.harness}
          result={probeResult.result}
          onClose={() => setProbeResult(null)}
        />
      ) : null}
    </div>
  );
}

function HarnessFormModal({
  initial,
  credentials,
  onClose,
  onSaved,
}: {
  initial: HarnessRegistration | null;
  credentials: CredentialRef[];
  onClose: () => void;
  onSaved: () => void;
}): JSX.Element {
  const [name, setName] = useState(initial?.name ?? '');
  const [adapterId, setAdapterId] = useState(initial?.adapter_id ?? '');
  const [adapterVersion, setAdapterVersion] = useState(initial?.adapter_version ?? '');
  const [execPath, setExecPath] = useState(initial?.exec_path ?? '');
  const [cwd, setCwd] = useState(initial?.cwd ?? '');
  const [authMode, setAuthMode] = useState<string>(initial?.auth_mode ?? 'native_login');
  const [authBinding, setAuthBinding] = useState(initial?.auth_binding ?? '');
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [envText, setEnvText] = useState(envToText(initial?.env_template ?? {}));
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const save = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError('名称必填。');
      return;
    }
    if (!adapterId.trim()) {
      setFormError('adapter_id 必填——能力矩阵按适配器声明渲染。');
      return;
    }
    const parsed = parseEnvTemplate(envText);
    if ('error' in parsed) {
      setFormError(`env_template 解析失败：${parsed.error}`);
      return;
    }
    setFormError(null);
    const body = {
      name: name.trim(),
      adapter_id: adapterId.trim(),
      adapter_version: adapterVersion.trim() || null,
      exec_path: execPath.trim() || null,
      cwd: cwd.trim() || null,
      auth_mode: authMode,
      auth_binding: authBinding || null,
      env_template: parsed.env,
      enabled,
    };
    const saved = initial
      ? await submit.run(() => registryApi.patchHarness(initial.harness_id, body))
      : await submit.run(() => registryApi.createHarness(body));
    if (saved) onSaved();
  };

  return (
    <Modal
      title={initial ? `编辑 harness · ${initial.name}` : '登记 harness'}
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
      {submit.error ? <SubmitError error={submit.error} what="保存 harness 失败" /> : null}

      <div className="field-row">
        <Field label="名称">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="claude-code 本机" />
        </Field>
        <Field label="adapter_id">
          <input
            className="input input--mono"
            value={adapterId}
            onChange={(e) => setAdapterId(e.target.value)}
            placeholder="claude_code"
          />
        </Field>
        <Field label="adapter_version" hint="留空表示由适配器自行解析。">
          <input
            className="input input--mono"
            value={adapterVersion}
            onChange={(e) => setAdapterVersion(e.target.value)}
            placeholder="1.0.0"
          />
        </Field>
      </div>

      <div className="field-row" style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="exec_path" hint="留空表示由适配器自行解析可执行文件。">
          <input
            className="input input--mono"
            value={execPath}
            onChange={(e) => setExecPath(e.target.value)}
            placeholder="/usr/local/bin/claude"
          />
        </Field>
        <Field label="cwd">
          <input className="input input--mono" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/srv/work" />
        </Field>
      </div>

      <div className="field-row" style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="auth_mode">
          <select className="select" value={authMode} onChange={(e) => setAuthMode(e.target.value)}>
            {Object.entries(AUTH_MODE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="auth_binding（凭据引用）"
          hint="选项为空 = 本机登录态：不引用任何凭据条目，认证由 harness 自身的登录态提供。"
        >
          <select className="select" value={authBinding} onChange={(e) => setAuthBinding(e.target.value)}>
            <option value="">本机登录态（不使用凭据引用）</option>
            {credentials.map((credential) => (
              <option key={credential.credential_id} value={credential.credential_id}>
                {credential.label}（{CREDENTIAL_KIND_LABELS[credential.kind]}
                {credential.revoked ? ' · 已撤销' : ''}）
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field
          label="env_template（每行 KEY=VALUE）"
          hint="只放非凭据类环境变量。需要认证请用 auth_binding 引用凭据——密钥不属于这里，写进来会以明文落在配置里。"
        >
          <textarea
            className="textarea textarea--code"
            value={envText}
            onChange={(e) => setEnvText(e.target.value)}
            placeholder={'ANTHROPIC_BASE_URL=https://...\n# 需要认证请改用 auth_binding'}
          />
        </Field>
      </div>

      <label className="check" style={{ marginTop: 'var(--sp-3)' }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        启用（停用后不会被调度选用）
      </label>
    </Modal>
  );
}

function sourceNote(source: string): string {
  if (source === 'probe') return 'probe = 适配器实测：内核真的调用过适配器，结论来自这次调用。';
  if (source === 'capabilities') return 'capabilities = 接口声明：来自适配器 manifest 的声明，未经实测。';
  return `未知来源「${source}」：内核返回了未识别的来源标识，这里如实显示原值。`;
}

function ProbeResultModal({
  harness,
  result,
  onClose,
}: {
  harness: HarnessRegistration;
  result: ProbeResult;
  onClose: () => void;
}): JSX.Element {
  const capabilities = result.capabilities;
  const entries = capabilities === null ? [] : Object.entries(capabilities);
  return (
    <Modal
      wide
      title={`探测结果 · ${harness.name}`}
      onClose={onClose}
      footer={
        <button type="button" className="btn btn--sm" onClick={onClose}>
          关闭
        </button>
      }
    >
      <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
        <Pill tone={result.ok ? 'success' : 'danger'}>{result.ok ? '探测成功' : '探测失败'}</Pill>
        <Chip variant={result.source === 'probe' ? 'accent' : undefined}>{result.source}</Chip>
      </div>
      <div className="text-xs muted" style={{ marginBottom: 'var(--sp-2)' }}>
        {sourceNote(result.source)}
      </div>

      <KV
        items={[
          { k: 'harness_id', v: <ShortId id={result.harness_id} /> },
          { k: 'probed_at', v: <TimeText value={result.probed_at} /> },
          { k: 'source', v: <span className="mono">{result.source}</span> },
        ]}
      />

      {result.error ? (
        <Banner variant="danger" title="探测错误">
          <span className="mono text-xs">{result.error}</span>
        </Banner>
      ) : null}

      {result.note ? (
        <div className="text-sm muted" style={{ marginTop: 'var(--sp-2)' }}>
          {result.note}
        </div>
      ) : null}

      <div className="section-title" style={{ marginTop: 'var(--sp-3)' }}>
        能力声明（capabilities）
      </div>
      {capabilities === null ? (
        <div className="text-sm dim">
          内核没有返回能力声明。界面不会替它补默认值——未知就是未知，能力矩阵里会保持「未探测」。
        </div>
      ) : entries.length === 0 ? (
        <div className="text-sm dim">内核返回了空的能力声明（没有任何键）。</div>
      ) : (
        <div className="table-wrap">
          <table className="table table--dense">
            <thead>
              <tr>
                <th>键</th>
                <th>中文名</th>
                <th>取值</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([key, value]) => (
                <tr key={key}>
                  <td className="mono text-xs">{key}</td>
                  <td>{CAPABILITY_LABELS[key] ?? <span className="dim">—</span>}</td>
                  <td>
                    <RenderValue value={value} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

// ===========================================================================
// 凭据
// ===========================================================================

function CredentialsTab({ enabled }: { enabled: boolean }): JSX.Element {
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
          本接口只登记<span className="text-warn">引用</span>；密钥本体只进 Secret Store，前端不收、不显示、不回显。
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
            title="尚未登记任何凭据引用"
            hint="凭据引用只记录「去哪里取」。密钥本身请先写入 Secret Store，这里只登记条目名。"
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
                  <th>label</th>
                  <th>credential_id</th>
                  <th>类型</th>
                  <th>secret_locator</th>
                  <th>base_url</th>
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
                      {credential.kind === 'harness_login' ? (
                        <span className="dim">由 harness 登录态提供</span>
                      ) : credential.secret_locator ? (
                        <span className="mono text-xs" title={credential.secret_locator}>
                          {credential.secret_locator}
                        </span>
                      ) : (
                        <span className="dim">未登记条目名</span>
                      )}
                    </td>
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
          <Banner variant="warn" title="撤销的影响面">
            撤销后，引用该凭据的节点在<strong>新的</strong>执行尝试中必须重新绑定有效凭据，不得以钉扎快照绕过；在途尝试允许完成当前请求；历史记录不被改写。
          </Banner>

          {submit.error ? <SubmitError error={submit.error} what="撤销失败" /> : null}

          <div className="section-title">引用该凭据的节点（前端扫描各流程最新修订得出）</div>
          <ImpactScan credentialId={revoking.credential_id} />
        </Modal>
      ) : null}
    </div>
  );
}

function CreateCredentialModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }): JSX.Element {
  const [label, setLabel] = useState('');
  const [kind, setKind] = useState<CredentialKind>('api_key');
  const [locator, setLocator] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const save = async (): Promise<void> => {
    if (!label.trim()) {
      setFormError('label 必填。');
      return;
    }
    if (kind !== 'harness_login' && !locator.trim()) {
      setFormError('该类型必须填写指向 Secret Store 的条目名（只有 harness_login 可以留空）。');
      return;
    }
    setFormError(null);
    const saved = await submit.run(() =>
      registryApi.createCredential({
        label: label.trim(),
        kind,
        secret_locator: kind === 'harness_login' ? null : locator.trim(),
        base_url: baseUrl.trim() || null,
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
      <Banner variant="info" title="这里只登记引用">
        本接口只登记<strong>引用</strong>；密钥本体只进 Secret Store，前端不收、不显示、不回显。
      </Banner>

      {formError ? (
        <Banner variant="danger" title="无法提交">
          {formError}
        </Banner>
      ) : null}
      {submit.error ? <SubmitError error={submit.error} what="新建凭据失败" /> : null}

      <div className="field-row">
        <Field label="label">
          <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="OpenAI 生产 Key" />
        </Field>
        <Field label="类型">
          <select
            className="select"
            value={kind}
            onChange={(e) => {
              const next = e.target.value as CredentialKind;
              setKind(next);
              if (next === 'harness_login') setLocator('');
            }}
          >
            {(Object.keys(CREDENTIAL_KIND_LABELS) as CredentialKind[]).map((value) => (
              <option key={value} value={value}>
                {CREDENTIAL_KIND_LABELS[value]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field
          label="指向 Secret Store 的条目名（如 secret://openai）"
          hint="harness_login 类型请留空。"
        >
          <input
            className="input input--mono"
            value={locator}
            disabled={kind === 'harness_login'}
            onChange={(e) => setLocator(e.target.value)}
            placeholder="secret://openai"
          />
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="base_url" hint="Base URL + Key 类型才需要；其它类型可留空。">
          <input
            className="input input--mono"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.openai.com/v1"
          />
        </Field>
      </div>
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
            err instanceof ApiError ? (err.unreachable ? '无法连接内核' : err.detail) : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [credentialId, nonce]);

  if (scan.loading) return <Loading label="扫描流程修订以计算影响面" />;

  if (scan.error !== null) {
    return (
      <Banner
        variant="danger"
        title="影响面无法计算"
        hint="流程列表读不到，就无法判断还有谁在引用它。撤销前请先确认。"
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
          title="部分流程未能读取，影响面可能不完整"
          hint="下列流程的最新修订没有读到，它们是否引用该凭据未知——不要在未确认的情况下当成「没有引用」。"
        >
          <span className="mono text-xs">{scan.failed.join('、')}</span>
        </Banner>
      ) : null}

      {scan.entries.length === 0 ? (
        <Empty
          title="已读取的流程里没有节点引用该凭据"
          hint={`已扫描 ${scan.total} 个流程的最新修订。这不代表运行中的会话没用到它——历史尝试的钉扎快照不在此列。`}
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
        扫描范围：{scan.total} 个流程的最新修订（graph.nodes[].profiles[].credential_ref）。
        在途任务的钉扎快照不在其中，撤销不会改写它们。
      </div>
    </div>
  );
}

// ===========================================================================
// Skills
// ===========================================================================

const SKILL_DISCLAIMER = 'Skill 是执行指导，不是框架已强制的资源限制；框架强制的只有资源归属与释放义务。';

function SkillsTab({ enabled }: { enabled: boolean }): JSX.Element {
  const skills = useAsync(registryApi.skills, [], { enabled });
  const [createOpen, setCreateOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set<string>());
  const submit = useSubmit();

  const rows: SkillDoc[] = skills.data ?? [];

  const toggleRow = (skillId: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(skillId)) next.delete(skillId);
      else next.add(skillId);
      return next;
    });
  };

  return (
    <div>
      <Banner variant="warn" title="Skill 的边界">
        {SKILL_DISCLAIMER}
      </Banner>

      <div className="panel">
        <div className="panel__head">
          Skill 文档
          <div className="panel__head-actions">
            <button type="button" className="btn btn--sm" onClick={skills.reload}>
              刷新
            </button>
            <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
              新建 Skill
            </button>
          </div>
        </div>

        {submit.error ? (
          <div style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
            <SubmitError error={submit.error} what="操作失败" />
          </div>
        ) : null}

        {skills.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={skills.error} what="Skill 列表" onRetry={skills.reload} />
          </div>
        ) : !skills.loaded ? (
          <Loading label="加载 Skill 列表" />
        ) : rows.length === 0 ? (
          <Empty
            title="尚未登记任何 Skill"
            hint="Skill 是给执行者的指导文本，不改变框架的资源归属与释放义务。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
                新建 Skill
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>skill_id</th>
                  <th>版本</th>
                  <th>范围</th>
                  <th>启用</th>
                  <th style={{ minWidth: 280 }}>内容</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((skill) => (
                  <tr key={skill.skill_id}>
                    <td>{skill.name}</td>
                    <td>
                      <ShortId id={skill.skill_id} />
                    </td>
                    <td className="table__num">v{skill.version}</td>
                    <td>{SKILL_SCOPE_LABELS[skill.scope]}</td>
                    <td>
                      <Pill tone={skill.enabled ? 'success' : 'idle'}>{skill.enabled ? '已启用' : '已停用'}</Pill>
                    </td>
                    <td>
                      <Disclosure
                        open={expanded.has(skill.skill_id)}
                        onToggle={() => toggleRow(skill.skill_id)}
                        summary={
                          <span className="text-xs muted">
                            {expanded.has(skill.skill_id) ? '收起内容' : '展开内容'}（{skill.content.length} 字）
                          </span>
                        }
                      >
                        {skill.content ? (
                          <pre className="code-block">{skill.content}</pre>
                        ) : (
                          <div className="text-xs dim">该 Skill 的内容为空。</div>
                        )}
                      </Disclosure>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {createOpen ? (
        <CreateSkillModal
          onClose={() => setCreateOpen(false)}
          onSaved={() => {
            setCreateOpen(false);
            skills.reload();
          }}
        />
      ) : null}
    </div>
  );
}

function CreateSkillModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }): JSX.Element {
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [scope, setScope] = useState<SkillScope>('node_local');
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const save = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError('名称必填。');
      return;
    }
    setFormError(null);
    const saved = await submit.run(() =>
      registryApi.createSkill({ name: name.trim(), content: content.trim(), scope }),
    );
    if (saved) onSaved();
  };

  return (
    <Modal
      title="新建 Skill"
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
      <Banner variant="warn" title="Skill 的边界">
        {SKILL_DISCLAIMER}
      </Banner>

      {formError ? (
        <Banner variant="danger" title="无法提交">
          {formError}
        </Banner>
      ) : null}
      {submit.error ? <SubmitError error={submit.error} what="新建 Skill 失败" /> : null}

      <div className="field-row">
        <Field label="名称">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="统一产物摘要格式" />
        </Field>
        <Field label="范围">
          <select className="select" value={scope} onChange={(e) => setScope(e.target.value as SkillScope)}>
            {(Object.keys(SKILL_SCOPE_LABELS) as SkillScope[]).map((value) => (
              <option key={value} value={value}>
                {SKILL_SCOPE_LABELS[value]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="内容">
          <textarea
            className="textarea textarea--code"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="写给执行者的指导文本"
          />
        </Field>
      </div>
    </Modal>
  );
}

// ===========================================================================
// MCP 工具
// ===========================================================================

function ToolsTab({ enabled }: { enabled: boolean }): JSX.Element {
  const tools = useAsync(registryApi.tools, [], { enabled });
  const [createOpen, setCreateOpen] = useState(false);
  const submit = useSubmit();

  const rows: ToolSpec[] = tools.data ?? [];

  return (
    <div>
      <div className="panel">
        <div className="panel__head">
          MCP 工具
          <div className="panel__head-actions">
            <button type="button" className="btn btn--sm" onClick={tools.reload}>
              刷新
            </button>
            <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
              登记工具
            </button>
          </div>
        </div>

        {submit.error ? (
          <div style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
            <SubmitError error={submit.error} what="操作失败" />
          </div>
        ) : null}

        {tools.error ? (
          <div style={{ padding: 'var(--sp-3)' }}>
            <ReadError error={tools.error} what="MCP 工具列表" onRetry={tools.reload} />
          </div>
        ) : !tools.loaded ? (
          <Loading label="加载 MCP 工具列表" />
        ) : rows.length === 0 ? (
          <Empty
            title="尚未登记任何 MCP 工具"
            hint="登记工具只是声明它存在；风险等级与审批策略决定它在运行时是否需要人工放行。"
            action={
              <button type="button" className="btn btn--sm btn--primary" onClick={() => setCreateOpen(true)}>
                登记工具
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>tool_id</th>
                  <th>传输</th>
                  <th>启动</th>
                  <th>风险</th>
                  <th>审批策略</th>
                  <th className="table__num">版本</th>
                  <th>启用</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((tool) => {
                  const risk = RISK_LEVEL_LABELS[tool.risk_level];
                  const envWarnings = suspiciousEnvKeys(tool.launch.env);
                  return (
                    <tr key={tool.tool_id}>
                      <td>
                        <div>{tool.name}</div>
                        {tool.description ? (
                          <div className="text-xs dim truncate" style={{ maxWidth: 320 }} title={tool.description}>
                            {tool.description}
                          </div>
                        ) : null}
                        {envWarnings.length > 0 ? (
                          <div style={{ marginTop: 3 }}>
                            <Chip
                              variant="warn"
                              title={`检测到名称含凭据字样的环境变量：${envWarnings.join('、')}。后端会拒绝这些取值，除非它是以 $ 开头的占位引用（如 \${VAR}）；请改用 credential_ref 或 \${VAR} 占位符，不要把明文密钥写进 env。`}
                            >
                              环境变量含凭据字样
                            </Chip>
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <ShortId id={tool.tool_id} />
                      </td>
                      <td>
                        <span className="mono text-xs">{tool.launch.transport}</span>
                      </td>
                      <td>{launchSummary(tool)}</td>
                      <td>
                        <Pill tone={risk.tone}>{risk.text}</Pill>
                      </td>
                      <td>{APPROVAL_POLICY_LABELS[tool.approval_policy] ?? tool.approval_policy}</td>
                      <td className="table__num">v{tool.version}</td>
                      <td>
                        <Pill tone={tool.enabled ? 'success' : 'idle'}>{tool.enabled ? '已启用' : '已停用'}</Pill>
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
        <CreateToolModal
          onClose={() => setCreateOpen(false)}
          onSaved={() => {
            setCreateOpen(false);
            tools.reload();
          }}
        />
      ) : null}
    </div>
  );
}

function launchSummary(tool: ToolSpec): JSX.Element {
  const { transport, command, args, url } = tool.launch;
  const parts = command ? [command, ...args] : [...args];
  if (transport === 'stdio') {
    if (parts.length === 0) return <span className="dim">未声明 command</span>;
    return (
      <span className="mono text-xs" title={parts.join(' ')}>
        {parts.join(' ')}
      </span>
    );
  }
  if (url) {
    return (
      <span className="mono text-xs" title={url}>
        {url}
      </span>
    );
  }
  if (parts.length > 0) {
    return (
      <span className="mono text-xs" title={parts.join(' ')}>
        {parts.join(' ')}
      </span>
    );
  }
  return <span className="dim">未声明 url</span>;
}

function parseArgs(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function CreateToolModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }): JSX.Element {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [transport, setTransport] = useState<'stdio' | 'http' | 'sse'>('stdio');
  const [command, setCommand] = useState('');
  const [argsText, setArgsText] = useState('');
  const [url, setUrl] = useState('');
  const [cwd, setCwd] = useState('');
  const [riskLevel, setRiskLevel] = useState<string>('medium');
  const [approvalPolicy, setApprovalPolicy] = useState<string>('ask');
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  const stdio = transport === 'stdio';
  const needsUrl = transport === 'http' || transport === 'sse';

  const save = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError('名称必填。');
      return;
    }
    if (stdio && !command.trim()) {
      setFormError('传输为 stdio 时必须填写 command——没有可执行命令就无法启动这个工具。');
      return;
    }
    if (needsUrl && !url.trim()) {
      setFormError(`传输为 ${transport} 时必须填写 url。`);
      return;
    }
    setFormError(null);
    const launch: Record<string, unknown> = {
      transport,
      command: stdio ? command.trim() : null,
      args: stdio ? parseArgs(argsText) : [],
      url: stdio ? null : url.trim(),
      cwd: cwd.trim() || null,
    };
    const saved = await submit.run(() =>
      registryApi.createTool({
        name: name.trim(),
        description: description.trim() || null,
        launch,
        risk_level: riskLevel,
        approval_policy: approvalPolicy,
      }),
    );
    if (saved) onSaved();
  };

  return (
    <Modal
      title="登记 MCP 工具"
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
      {submit.error ? <SubmitError error={submit.error} what="登记工具失败" /> : null}

      <div className="field-row">
        <Field label="名称">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="filesystem" />
        </Field>
        <Field label="传输">
          <select
            className="select"
            value={transport}
            onChange={(e) => setTransport(e.target.value as 'stdio' | 'http' | 'sse')}
          >
            <option value="stdio">stdio</option>
            <option value="http">http</option>
            <option value="sse">sse</option>
          </select>
        </Field>
        <Field label="cwd">
          <input className="input input--mono" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/srv/mcp" />
        </Field>
      </div>

      <div style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="描述">
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="这个工具做什么"
          />
        </Field>
      </div>

      {stdio ? (
        <>
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <Field label="command（stdio 必填）">
              <input
                className="input input--mono"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx"
              />
            </Field>
          </div>
          <div style={{ marginTop: 'var(--sp-3)' }}>
            <Field label="args（逗号或换行分隔）">
              <textarea
                className="textarea textarea--code"
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                placeholder={'-y\n@modelcontextprotocol/server-filesystem\n/srv/data'}
              />
            </Field>
          </div>
        </>
      ) : (
        <div style={{ marginTop: 'var(--sp-3)' }}>
          <Field label={`url（${transport} 必填）`}>
            <input
              className="input input--mono"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://mcp.example.com/sse"
            />
          </Field>
        </div>
      )}

      <div className="field-row" style={{ marginTop: 'var(--sp-3)' }}>
        <Field label="风险等级">
          <select className="select" value={riskLevel} onChange={(e) => setRiskLevel(e.target.value)}>
            {Object.entries(RISK_LEVEL_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label.text}
              </option>
            ))}
          </select>
        </Field>
        <Field label="审批策略">
          <select className="select" value={approvalPolicy} onChange={(e) => setApprovalPolicy(e.target.value)}>
            {Object.entries(APPROVAL_POLICY_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      </div>
    </Modal>
  );
}
