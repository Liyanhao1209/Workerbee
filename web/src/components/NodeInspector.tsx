/**
 * 节点检查器（右侧面板）。编辑选中节点的全部定义字段。
 *
 * 要点：
 * - **执行候选是有序列表**，顺序即优先级；对齐键是 `profile_id`，
 *   重排与删除不会造成模型／harness／凭据错配（CFG-01）。
 * - 凭据只存**引用**，这里永远不出现密钥本体（AUTH-02）。
 * - 系统 prompt 留空是合法的，且**不取消必需交接**（CFG-06）——UI 上要说明，
 *   否则用户会以为留空就等于「什么都不要」。
 */

import { useState } from 'react';
import type {
  CredentialRef,
  ExecutionProfile,
  HarnessRegistration,
  NodeDefinition,
  SkillDoc,
  ToolSpec,
  VersionedRef,
} from '../api/types';
import { defaultRetryPolicy, newLocalId } from '../api/guards';
import { registry as registryApi, templates as templatesApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { CREDENTIAL_KIND_LABELS } from '../labels';
import { Chip, Field, ShortId } from './common';

export interface NodeInspectorProps {
  node: NodeDefinition;
  onChange: (next: NodeDefinition) => void;
  onDelete: () => void;
  harnesses: HarnessRegistration[];
  credentials: CredentialRef[];
  skills: SkillDoc[];
  tools: ToolSpec[];
  /** 新建凭据后通知父级刷新列表。 */
  onCredentialsChanged: () => void;
  /** 注册表读取失败时如实说明，而不是显示成「一个都没有」。 */
  registryError: string | null;
}

/** 从能力快照里取字符串列表；快照可能缺失或形状不符，取不到就当空列表。 */
function capList(capabilities: Record<string, unknown> | null, key: string): string[] {
  const raw = capabilities?.[key];
  if (!Array.isArray(raw)) return [];
  return raw.filter((x): x is string => typeof x === 'string');
}

export function NodeInspector({
  node,
  onChange,
  onDelete,
  harnesses,
  credentials,
  skills,
  tools,
  onCredentialsChanged,
  registryError,
}: NodeInspectorProps): JSX.Element {
  const patch = (changes: Partial<NodeDefinition>): void => onChange({ ...node, ...changes });

  return (
    <div className="col" style={{ gap: 'var(--sp-3)' }}>
      <div className="section-title">基本信息</div>
      <Field label="名称" required hint="节点在图中与历史里显示的名字">
        <input
          className="input"
          value={node.name}
          onChange={(e) => patch({ name: e.target.value })}
          placeholder="如：实现"
        />
      </Field>
      <Field label="角色" hint="节点扮演的角色，会写进发给模型的指令，如「规划者」「编码者」「审计者」">
        <input
          className="input"
          value={node.role ?? ''}
          onChange={(e) => patch({ role: e.target.value || null })}
        />
      </Field>
      <Field label="描述">
        <textarea
          className="textarea"
          rows={2}
          value={node.description ?? ''}
          onChange={(e) => patch({ description: e.target.value || null })}
        />
      </Field>

      <Field
        label="要求上游提供的内容"
        hint="每行一个名称。上游节点完成后必须把这些内容交给本节点；如果上游没有声明会提供，校验时会报错。留空表示不检查。"
      >
        <textarea
          className="textarea textarea--code"
          rows={3}
          placeholder={'如：\n需求说明\n接口设计'}
          value={node.required_inputs.join('\n')}
          onChange={(e) =>
            patch({
              required_inputs: e.target.value
                .split('\n')
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
        />
      </Field>

      <Field
        label="系统 prompt"
        hint="发给模型的长期指令。可以留空：模型仍会拿到任务说明和上游产出的摘要。"
      >
        <textarea
          className="textarea textarea--code"
          rows={6}
          value={node.system_prompt ?? ''}
          onChange={(e) => patch({ system_prompt: e.target.value || null })}
          placeholder="（可留空）"
        />
      </Field>

      {/* -------------------- 执行候选 -------------------- */}
      <div className="divider" />
      <div className="row row--between">
        <div className="section-title" style={{ margin: 0 }}>
          执行候选（按顺序尝试，第一个失败才用下一个）
        </div>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() =>
            patch({
              profiles: [
                ...node.profiles,
                {
                  profile_id: newLocalId(),
                  model_name: '',
                  harness_ref: null,
                  credential_ref: null,
                  reasoning_effort: null,
                  permission_mode: null,
                  retry: defaultRetryPolicy(),
                  compact_threshold: null,
                  extra: {},
                },
              ],
            })
          }
        >
          + 添加候选
        </button>
      </div>
      {node.profiles.length === 0 ? (
        <div className="banner banner--warn">
          <div className="banner__body">
            <div className="banner__title">至少需要一组执行候选</div>
            没有执行候选的节点无法运行，发布校验不会通过。
          </div>
        </div>
      ) : null}
      {node.profiles.map((profile, index) => (
        <ProfileEditor
          key={profile.profile_id}
          profile={profile}
          index={index}
          total={node.profiles.length}
          harnesses={harnesses}
          credentials={credentials}
          onCredentialsChanged={onCredentialsChanged}
          onChange={(next) =>
            patch({ profiles: node.profiles.map((p) => (p.profile_id === next.profile_id ? next : p)) })
          }
          onMove={(direction) => {
            const target = index + direction;
            if (target < 0 || target >= node.profiles.length) return;
            const next = [...node.profiles];
            const [item] = next.splice(index, 1);
            if (!item) return;
            next.splice(target, 0, item);
            patch({ profiles: next });
          }}
          onRemove={() => patch({ profiles: node.profiles.filter((p) => p.profile_id !== profile.profile_id) })}
        />
      ))}

      {/* -------------------- Skills / 工具 -------------------- */}
      <div className="divider" />
      <RefPicker
        title="Skills"
        hint="写给模型的执行指导，运行这个节点时会随任务一起发给模型。"
        refs={node.skill_refs}
        options={skills.filter((s) => s.enabled).map((s) => ({ id: s.skill_id, label: `${s.name} @v${s.version}` }))}
        onChange={(refs) => patch({ skill_refs: refs })}
        registryError={registryError}
      />
      <RefPicker
        title="MCP 工具"
        hint="模型运行这个节点时可以调用的外部工具。工具自身的风险分级与审批策略不受影响。"
        refs={node.tool_refs}
        options={tools.filter((t) => t.enabled).map((t) => ({ id: t.tool_id, label: `${t.name} @v${t.version}` }))}
        onChange={(refs) => patch({ tool_refs: refs })}
        registryError={registryError}
      />

      <div className="divider" />
      <SaveNodeAsTemplate node={node} credentials={credentials} />

      <div className="divider" />
      <div className="row row--between">
        <span className="text-xs dim">节点 ID（跨修订稳定，供历史归因）</span>
        <ShortId id={node.node_id} len={12} />
      </div>
      <button type="button" className="btn btn--danger btn--sm" onClick={onDelete}>
        从图中删除该节点
      </button>
      <div className="text-xs dim">
        删除会同时移除与它相连的依赖边。这是对定义图的修改，需要保存修订后才对内核生效。
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 单个候选
// ---------------------------------------------------------------------------

function ProfileEditor({
  profile,
  index,
  total,
  harnesses,
  credentials,
  onCredentialsChanged,
  onChange,
  onMove,
  onRemove,
}: {
  profile: ExecutionProfile;
  index: number;
  total: number;
  harnesses: HarnessRegistration[];
  credentials: CredentialRef[];
  onCredentialsChanged: () => void;
  onChange: (next: ExecutionProfile) => void;
  onMove: (direction: number) => void;
  onRemove: () => void;
}): JSX.Element {
  const [open, setOpen] = useState(index === 0);
  const patch = (changes: Partial<ExecutionProfile>): void => onChange({ ...profile, ...changes });

  const harness = harnesses.find((h) => h.harness_id === profile.harness_ref);
  const capabilities = harness?.capabilities_snapshot ?? null;
  const supportsCompact = capabilities ? capabilities['compact'] === true : null;
  const efforts = capList(capabilities, 'reasoning_efforts');
  const permissionModes = capList(capabilities, 'permission_modes');
  const nonInteractive = capList(capabilities, 'non_interactive_modes');
  const declaredModels = capList(capabilities, 'models');
  const noPermissionHook = capabilities ? capabilities['permission_hook'] === false : false;

  return (
    <div
      style={{
        border: '1px solid var(--line)',
        borderRadius: 'var(--radius)',
        background: 'var(--bg-2)',
        padding: 'var(--sp-2)',
      }}
    >
      <div className="row row--tight">
        <span className="chip chip--accent">#{index + 1}</span>
        <span className="truncate text-sm" style={{ flex: '1 1 auto' }}>
          {profile.model_name || '（未填模型名）'}
          {profile.harness_ref ? <span className="dim mono"> @{profile.harness_ref}</span> : null}
        </span>
        <button type="button" className="btn btn--xs" disabled={index === 0} onClick={() => onMove(-1)} title="上移（先用它）">
          ↑
        </button>
        <button
          type="button"
          className="btn btn--xs"
          disabled={index === total - 1}
          onClick={() => onMove(1)}
          title="下移（后用）"
        >
          ↓
        </button>
        <button type="button" className="btn btn--xs" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : '编辑'}
        </button>
        <button type="button" className="btn btn--xs btn--danger" onClick={onRemove} title="删除该候选">
          ×
        </button>
      </div>

      {open ? (
        <div className="col" style={{ marginTop: 'var(--sp-2)', gap: 'var(--sp-2)' }}>
          <Field label="Harness" required hint="运行这个节点的 agent 程序（如 Claude Code、Kimi Code）">
            <select
              className="select"
              value={profile.harness_ref ?? ''}
              onChange={(e) => patch({ harness_ref: e.target.value || null })}
            >
              <option value="">（未选择）</option>
              {harnesses.map((h) => (
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
              value={profile.model_name}
              onChange={(e) => patch({ model_name: e.target.value })}
              placeholder={declaredModels.length > 0 ? `如 ${declaredModels[0]}` : '如 claude-sonnet-4-6 / kimi-k2'}
            />
          </Field>

          <Field label="凭据" hint="用哪份凭据调用模型服务。凭据在「注册表 → 凭据」里保存一次（服务地址、密钥、默认模型），各节点直接引用。">
            <select
              className="select"
              value={profile.credential_ref ?? ''}
              onChange={(e) => patch({ credential_ref: e.target.value || null })}
            >
              <option value="">（使用 harness 本机登录态）</option>
              {credentials.map((c) => (
                <option key={c.credential_id} value={c.credential_id} disabled={c.revoked}>
                  {c.label} · {CREDENTIAL_KIND_LABELS[c.kind]}
                  {c.revoked ? '（已撤销，不可选）' : ''}
                </option>
              ))}
            </select>
            <CredentialQuickCreate onCreated={onCredentialsChanged} />
          </Field>

          {!harness ? (
            <Field label="Reasoning effort" hint="选择 harness 后，这里会列出它支持的取值。">
              <select className="select" disabled value="">
                <option value="">（先选择 harness）</option>
              </select>
            </Field>
          ) : efforts.length === 0 ? (
            <Field label="Reasoning effort" hint="该 harness 没有这个设置项，不需要填。">
              <select className="select" disabled value="">
                <option value="">（不适用）</option>
              </select>
              {profile.reasoning_effort ? (
                <span className="field__error">
                  当前填了「{profile.reasoning_effort}」，该 harness 不支持这个取值，校验会报错。
                  <button
                    type="button"
                    className="btn btn--xs"
                    style={{ marginLeft: 6 }}
                    onClick={() => patch({ reasoning_effort: null })}
                  >
                    清除
                  </button>
                </span>
              ) : null}
            </Field>
          ) : (
            <Field label="Reasoning effort" hint="模型思考投入程度。留空表示不指定。">
              <select
                className="select"
                value={profile.reasoning_effort ?? ''}
                onChange={(e) => patch({ reasoning_effort: e.target.value || null })}
              >
                <option value="">（不指定）</option>
                {efforts.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
                {profile.reasoning_effort && !efforts.includes(profile.reasoning_effort) ? (
                  <option value={profile.reasoning_effort}>{profile.reasoning_effort}（该 harness 不支持）</option>
                ) : null}
              </select>
            </Field>
          )}

          {harness && permissionModes.length > 0 ? (
            <Field
              label="权限模式"
              required={noPermissionHook}
              hint={
                noPermissionHook
                  ? '该 harness 运行中不会向你请求授权，必须选一个不会中途停下来问人的模式（标注了「不会询问」的），否则发布校验不通过。'
                  : '该 harness 运行中会把授权请求转发给你处理，一般可以不指定。'
              }
            >
              <select
                className="select"
                value={profile.permission_mode ?? ''}
                onChange={(e) => patch({ permission_mode: e.target.value || null })}
              >
                <option value="">（不指定）</option>
                {permissionModes.map((m) => (
                  <option key={m} value={m}>
                    {m}
                    {nonInteractive.includes(m) ? '（不会询问）' : ''}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          {harness && noPermissionHook && permissionModes.length === 0 ? (
            <div className="field__error">
              该 harness 不会向你请求授权，却也没有声明任何可用的权限模式；这样的组合无法无人值守运行，请换用其他 harness。
            </div>
          ) : null}

          <details>
            <summary className="text-sm dim" style={{ cursor: 'pointer' }}>
              高级设置（通常不用改）
            </summary>
            <div className="col" style={{ marginTop: 'var(--sp-2)', gap: 'var(--sp-2)' }}>
              <Field
                label="上下文整理阈值（token）"
                hint={
                  supportsCompact === false
                    ? '该 harness 不支持上下文整理，填了也不会生效。'
                    : '对话长度超过这个值时主动压缩上下文。留空表示不主动整理。'
                }
              >
                <input
                  className="input input--num input--mono"
                  type="number"
                  min={1}
                  placeholder="如 120000"
                  value={profile.compact_threshold ?? ''}
                  onChange={(e) => {
                    const raw = e.target.value.trim();
                    const parsed = raw === '' ? null : Number(raw);
                    patch({ compact_threshold: parsed !== null && Number.isFinite(parsed) && parsed > 0 ? parsed : null });
                  }}
                />
              </Field>

              <div className="section-title" style={{ marginTop: 4 }}>
                重试策略
              </div>
              <div className="field-row">
                <Field label="最大尝试次数" hint="含首次">
                  <input
                    className="input input--num input--mono"
                    type="number"
                    min={1}
                    value={profile.retry.max_attempts}
                    onChange={(e) =>
                      patch({
                        retry: { ...profile.retry, max_attempts: Math.max(1, Number(e.target.value) || 1) },
                      })
                    }
                  />
                </Field>
                <Field label="退避基数（毫秒）" hint="首次重试前的等待">
                  <input
                    className="input input--num input--mono"
                    type="number"
                    min={0}
                    value={profile.retry.backoff_base_ms}
                    onChange={(e) =>
                      patch({
                        retry: { ...profile.retry, backoff_base_ms: Math.max(0, Number(e.target.value) || 0) },
                      })
                    }
                  />
                </Field>
                <Field label="退避上限（毫秒）" hint="等待时间不超过它">
                  <input
                    className="input input--num input--mono"
                    type="number"
                    min={0}
                    value={profile.retry.backoff_cap_ms}
                    onChange={(e) =>
                      patch({
                        retry: { ...profile.retry, backoff_cap_ms: Math.max(0, Number(e.target.value) || 0) },
                      })
                    }
                  />
                </Field>
              </div>
              <Field
                label="可重试的错误（逗号分隔）"
                hint="哪些错误值得重试。网络错误、限流、服务器 5xx 默认可重试；认证失败和配置错误不重试，直接换下一个候选。"
              >
                <input
                  className="input input--mono"
                  value={profile.retry.retryable_errors.join(', ')}
                  onChange={(e) =>
                    patch({
                      retry: {
                        ...profile.retry,
                        retryable_errors: e.target.value
                          .split(',')
                          .map((s) => s.trim())
                          .filter(Boolean),
                      },
                    })
                  }
                />
              </Field>
              {profile.retry.backoff_cap_ms < profile.retry.backoff_base_ms ? (
                <div className="field__error">退避上限不能小于基数，保存时会被拒绝。</div>
              ) : null}

              <ExtraEditor extra={profile.extra} onChange={(extra) => patch({ extra })} />
            </div>
          </details>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 内联新建凭据
// ---------------------------------------------------------------------------

function CredentialQuickCreate({
  onCreated,
}: {
  onCreated: () => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState('');
  const [kind, setKind] = useState<'api_key' | 'base_url_pair'>('api_key');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button type="button" className="btn btn--xs" onClick={() => setOpen(true)}>
        + 新建凭据
      </button>
    );
  }

  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const secret: Record<string, string> = { api_key: apiKey.trim() };
      if (kind === 'base_url_pair') secret['base_url'] = baseUrl.trim();
      await registryApi.createCredential({
        label: label.trim(),
        kind,
        base_url: kind === 'base_url_pair' ? baseUrl.trim() || null : null,
        secret,
      });
      onCreated();
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="col"
      style={{
        gap: 'var(--sp-2)',
        marginTop: 4,
        padding: 'var(--sp-2)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--radius)',
      }}
    >
      <Field label="名称" required hint="给自己看的名字，如「公司 Claude 账号」">
        <input className="input input--sm" value={label} onChange={(e) => setLabel(e.target.value)} />
      </Field>
      <Field label="类型" required>
        <select className="select" value={kind} onChange={(e) => setKind(e.target.value as 'api_key' | 'base_url_pair')}>
          <option value="api_key">API Key</option>
          <option value="base_url_pair">API Key + 自定义服务地址</option>
        </select>
      </Field>
      <Field label="API Key" required hint="只保存加密后的内容，这里和历史记录都不会显示它。">
        <input
          className="input input--sm input--mono"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="sk-..."
        />
      </Field>
      {kind === 'base_url_pair' ? (
        <Field label="服务地址" required>
          <input
            className="input input--sm input--mono"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.example.com/v1"
          />
        </Field>
      ) : null}
      {error ? <div className="field__error">{error}</div> : null}
      <div className="row row--tight">
        <button
          type="button"
          className="btn btn--sm btn--primary"
          disabled={busy || !label.trim() || !apiKey.trim() || (kind === 'base_url_pair' && !baseUrl.trim())}
          onClick={() => void submit()}
        >
          {busy ? '保存中…' : '保存'}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

function ExtraEditor({
  extra,
  onChange,
}: {
  extra: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}): JSX.Element {
  const entries = Object.entries(extra);
  return (
    <div className="col" style={{ marginTop: 6, gap: 4 }}>
      <div className="text-xs dim">
        传给 harness 的附加参数。不要在这里写密钥——它会进入历史记录；密钥请用上面的「凭据」。
      </div>
      {entries.map(([key, value]) => (
        <div className="row row--tight" key={key}>
          <input className="input input--sm input--mono" value={key} readOnly style={{ flex: '0 0 140px' }} />
          <input
            className="input input--sm input--mono"
            value={value}
            onChange={(e) => onChange({ ...extra, [key]: e.target.value })}
          />
          <button
            type="button"
            className="btn btn--xs btn--danger"
            onClick={() => {
              const next = { ...extra };
              delete next[key];
              onChange(next);
            }}
          >
            ×
          </button>
        </div>
      ))}
      <ExtraAdd onAdd={(key) => onChange({ ...extra, [key]: '' })} />
    </div>
  );
}

function ExtraAdd({ onAdd }: { onAdd: (key: string) => void }): JSX.Element {
  const [key, setKey] = useState('');
  return (
    <div className="row row--tight">
      <input
        className="input input--sm input--mono"
        placeholder="新参数名"
        value={key}
        onChange={(e) => setKey(e.target.value)}
      />
      <button
        type="button"
        className="btn btn--xs"
        disabled={!key.trim()}
        onClick={() => {
          onAdd(key.trim());
          setKey('');
        }}
      >
        添加
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 存为节点模板
// ---------------------------------------------------------------------------

function SaveNodeAsTemplate({
  node,
  credentials,
}: {
  node: NodeDefinition;
  credentials: CredentialRef[];
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  if (!open) {
    return (
      <div className="col" style={{ gap: 4 }}>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => {
            setName(node.name);
            setDone(false);
            setError(null);
            setOpen(true);
          }}
        >
          存为节点模板
        </button>
        <div className="text-xs dim">把这个节点的配置存进模板库，以后建图时可以直接复用。</div>
      </div>
    );
  }

  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const slots: { slot: string; original_label: string | null }[] = [];
      const profiles = node.profiles.map((p, idx) => {
        if (p.credential_ref) {
          const cred = credentials.find((c) => c.credential_id === p.credential_ref);
          slots.push({
            slot: `${node.name}.profiles[${idx}].credential_ref`,
            original_label: cred?.label ?? p.credential_ref,
          });
        }
        // 凭据引用随模板保留：密钥永远进不了模板，同机复用时实例化自动绑定。
        return p;
      });
      await templatesApi.create({
        name: name.trim(),
        kind: 'node',
        payload: {
          nodes: [
            {
              name: node.name,
              role: node.role,
              description: node.description,
              system_prompt: node.system_prompt,
              profiles,
              skill_refs: node.skill_refs,
              tool_refs: node.tool_refs,
              required_inputs: node.required_inputs,
            },
          ],
          edges: [],
          sensitive_slots: slots,
        },
      });
      setDone(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 'var(--sp-2)' }}>
      <Field label="模板名称" required hint="凭据不会存进模板；使用模板时需要重新选择凭据。">
        <input className="input input--sm" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      {error ? <div className="field__error">{error}</div> : null}
      {done ? <div className="text-xs" style={{ color: 'var(--st-success)' }}>已存入模板库。</div> : null}
      <div className="row row--tight">
        <button
          type="button"
          className="btn btn--sm btn--primary"
          disabled={busy || !name.trim()}
          onClick={() => void submit()}
        >
          {busy ? '保存中…' : '保存'}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 引用选择器（Skills / 工具）
// ---------------------------------------------------------------------------

function RefPicker({
  title,
  hint,
  refs,
  options,
  onChange,
  registryError,
}: {
  title: string;
  hint: string;
  refs: VersionedRef[];
  options: { id: string; label: string }[];
  onChange: (next: VersionedRef[]) => void;
  registryError: string | null;
}): JSX.Element {
  const [adding, setAdding] = useState(false);
  const available = options.filter((o) => !refs.some((r) => r.ref_id === o.id));

  return (
    <div>
      <div className="row row--between">
        <div className="section-title" style={{ margin: 0 }}>
          {title}
        </div>
        <button
          type="button"
          className="btn btn--xs"
          onClick={() => setAdding((v) => !v)}
          disabled={available.length === 0 && !adding}
        >
          {adding ? '取消' : '+ 引用'}
        </button>
      </div>
      <div className="text-xs dim" style={{ marginBottom: 4 }}>
        {hint}
      </div>
      {registryError ? (
        <div className="text-xs text-warn">注册表读取失败：{registryError}（未读到的项不显示，不代表不存在）</div>
      ) : null}
      {refs.length === 0 ? (
        <div className="text-xs dim">未引用任何项。</div>
      ) : (
        <div className="chips">
          {refs.map((ref) => (
            <Chip
              key={ref.ref_id}
              onClick={() => onChange(refs.filter((r) => r.ref_id !== ref.ref_id))}
              title="点击移除"
            >
              {options.find((o) => o.id === ref.ref_id)?.label ?? ref.ref_id.slice(0, 8)}
              {ref.version !== null ? ` @${ref.version}` : ''} ×
            </Chip>
          ))}
        </div>
      )}
      {adding ? (
        <div className="col" style={{ marginTop: 6, gap: 4 }}>
          {available.length === 0 ? (
            <div className="text-xs dim">没有可引用的项（可能都已被引用，或注册表中没有启用的项）。</div>
          ) : (
            available.map((option) => (
              <button
                key={option.id}
                type="button"
                className="btn btn--sm"
                style={{ justifyContent: 'flex-start' }}
                onClick={() => {
                  onChange([...refs, { ref_id: option.id, version: null }]);
                  setAdding(false);
                }}
              >
                {option.label}
              </button>
            ))
          )}
          <div className="text-xs dim">
            版本留空表示跟随最新。填写具体版本可以保证每次执行用的是同一份。
          </div>
        </div>
      ) : null}
    </div>
  );
}
