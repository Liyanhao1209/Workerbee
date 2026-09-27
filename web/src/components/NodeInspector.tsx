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
  /** 注册表读取失败时如实说明，而不是显示成「一个都没有」。 */
  registryError: string | null;
}

export function NodeInspector({
  node,
  onChange,
  onDelete,
  harnesses,
  credentials,
  skills,
  tools,
  registryError,
}: NodeInspectorProps): JSX.Element {
  const patch = (changes: Partial<NodeDefinition>): void => onChange({ ...node, ...changes });

  return (
    <div className="col" style={{ gap: 'var(--sp-3)' }}>
      <div className="section-title">基本信息</div>
      <Field label="名称" hint="节点在图中与历史里显示的名字">
        <input
          className="input"
          value={node.name}
          onChange={(e) => patch({ name: e.target.value })}
          placeholder="如：实现"
        />
      </Field>
      <Field label="角色" hint="进入 ContextPackage 的 P1 分区（§7.3），如「规划者」「编码者」「审计者」">
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
        label="必需输入（每行一个字段名）"
        hint="声明后即选择「可机器校验的契约机制」：有效上游若没有声明对应输出契约，校验会报错（ACT-03）。留空表示不参与机器校验。"
      >
        <textarea
          className="textarea textarea--code"
          rows={3}
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
        hint="留空是合法的：仍会以任务输入与上游摘要执行（P1/P2）。填写它不会取消必需交接；上游材料只进入 P2，不会获得修改系统约束的权限。"
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
          执行候选（有序，顺序即优先级）
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
            可运行节点必须至少配置一组候选；没有候选的节点在发布校验中会失败（WF-05、CFG-01）。
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
        hint="执行指导；框架未据此强制任何操作系统级限制（RES-04）。"
        refs={node.skill_refs}
        options={skills.filter((s) => s.enabled).map((s) => ({ id: s.skill_id, label: `${s.name} @v${s.version}` }))}
        onChange={(refs) => patch({ skill_refs: refs })}
        registryError={registryError}
      />
      <RefPicker
        title="MCP 工具"
        hint="会进入 ContextPackage 的 P4 分区；风险分级与审批策略挂在工具本身。"
        refs={node.tool_refs}
        options={tools.filter((t) => t.enabled).map((t) => ({ id: t.tool_id, label: `${t.name} @v${t.version}` }))}
        onChange={(refs) => patch({ tool_refs: refs })}
        registryError={registryError}
      />

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
  onChange,
  onMove,
  onRemove,
}: {
  profile: ExecutionProfile;
  index: number;
  total: number;
  harnesses: HarnessRegistration[];
  credentials: CredentialRef[];
  onChange: (next: ExecutionProfile) => void;
  onMove: (direction: number) => void;
  onRemove: () => void;
}): JSX.Element {
  const [open, setOpen] = useState(index === 0);
  const patch = (changes: Partial<ExecutionProfile>): void => onChange({ ...profile, ...changes });

  const harness = harnesses.find((h) => h.harness_id === profile.harness_ref);
  const capabilities = harness?.capabilities_snapshot ?? null;
  const supportsCompact = capabilities ? capabilities['compact'] === true : null;

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
        <button type="button" className="btn btn--xs" disabled={index === 0} onClick={() => onMove(-1)} title="上移（提高优先级）">
          ↑
        </button>
        <button
          type="button"
          className="btn btn--xs"
          disabled={index === total - 1}
          onClick={() => onMove(1)}
          title="下移（降低优先级）"
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
          <Field label="模型名" hint="系统不评价模型强弱，只做兼容性检查">
            <input
              className="input input--mono"
              value={profile.model_name}
              onChange={(e) => patch({ model_name: e.target.value })}
              placeholder="如 gpt-5-codex / claude-sonnet-4-6"
            />
          </Field>

          <Field
            label="Harness"
            hint={
              harness
                ? '模型与 harness 解耦，同一候选只绑定一个 harness'
                : '未选择 harness 时，节点校验会报「待配置」'
            }
          >
            <select
              className="select"
              value={profile.harness_ref ?? ''}
              onChange={(e) => patch({ harness_ref: e.target.value || null })}
            >
              <option value="">（未绑定 harness）</option>
              {harnesses.map((h) => (
                <option key={h.harness_id} value={h.harness_id}>
                  {h.name}
                  {h.last_probe_ok === false ? '（探测失败）' : h.last_probe_ok === null ? '（未探测）' : ''}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="凭据"
            hint="只保存引用；密钥本体在 Secret Store，永不出现在这里、模板或历史里（AUTH-02）"
          >
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
          </Field>

          <Field
            label="Reasoning effort"
            hint="以适配器验证的能力为准；不可用的取值会被拒绝，不会静默忽略（CFG-02）。留空表示 harness 没有这个概念。"
          >
            <input
              className="input input--mono"
              value={profile.reasoning_effort ?? ''}
              onChange={(e) => patch({ reasoning_effort: e.target.value || null })}
              placeholder="如 low / medium / high"
            />
          </Field>

          <Field
            label="Compact 阈值（token）"
            hint={
              supportsCompact === false
                ? '该 harness 的能力声明里没有上下文整理，填了也不会生效——不支持就是不会整理（CFG-05）。'
                : supportsCompact === null
                  ? 'harness 尚未探测，上限未知。实际触发点 = min(用户阈值, harness 实际上限) − 安全余量。'
                  : '期望触发整理的阈值，不是模型最大窗口。留空表示不主动整理。'
            }
          >
            <input
              className="input input--num input--mono"
              type="number"
              min={1}
              value={profile.compact_threshold ?? ''}
              onChange={(e) => {
                const raw = e.target.value.trim();
                const parsed = raw === '' ? null : Number(raw);
                patch({ compact_threshold: parsed !== null && Number.isFinite(parsed) && parsed > 0 ? parsed : null });
              }}
            />
          </Field>

          <div className="section-title" style={{ marginTop: 4 }}>
            重试策略（有界，必须有终点）
          </div>
          <div className="field-row">
            <Field label="最大尝试次数（含首次）">
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
            <Field label="退避基数（ms）">
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
            <Field label="退避上限（ms）">
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
            label="可重试错误类别（逗号分隔）"
            hint="默认对应 D-05：网络错误 / 限流 / 5xx 可重试；认证失败与 4xx 配置错误不可重试，直接切换候选。"
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
            <div className="field__error">退避上限不能小于基数，内核会拒绝该配置。</div>
          ) : null}

          <details>
            <summary className="text-xs dim" style={{ cursor: 'pointer' }}>
              附加参数（透传给适配器）
            </summary>
            <ExtraEditor
              extra={profile.extra}
              onChange={(extra) => patch({ extra })}
            />
          </details>
        </div>
      ) : null}
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
        透传给适配器的附加参数（如 API base url 覆盖、模型别名）。含凭据性内容时必须走凭据引用，
        不要写在这里——它会进入历史记录。
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
            版本留空表示「跟随最新」。要满足 CFG-07 的可追溯要求，应钉扎到具体版本。
          </div>
        </div>
      ) : null}
    </div>
  );
}
