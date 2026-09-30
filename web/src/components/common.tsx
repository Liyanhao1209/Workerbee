/**
 * 通用展示组件。没有 UI 组件库，这里就是那「少量组件」。
 */

import type { ReactNode } from 'react';
import { useEffect } from 'react';
import type { TaskState, StageState, Severity } from '../api/types';
import type { Tone } from '../labels';
import { SEVERITY_LABELS, stageStateLabel, taskStateLabel } from '../labels';

// ---------------------------------------------------------------------------
// 状态徽标
// ---------------------------------------------------------------------------

export function Pill({
  tone,
  children,
  title,
  transition,
  plain,
}: {
  tone: Tone;
  children: ReactNode;
  title?: string;
  transition?: boolean;
  plain?: boolean;
}): JSX.Element {
  const cls = [`pill`, `pill--${tone}`];
  if (transition) cls.push('pill--transition');
  if (plain) cls.push('pill--plain');
  return (
    <span className={cls.join(' ')} title={title}>
      <span className="pill__dot" />
      {children}
    </span>
  );
}

/** 任务状态徽标。过渡态自带虚线边框与呼吸点，与稳定态一眼可分（OBS-01）。 */
export function TaskStatePill({ state }: { state: TaskState }): JSX.Element {
  const label = taskStateLabel(state);
  return (
    <Pill tone={label.tone} transition={label.transitioning} title={`${state}${label.hint ? ' — ' + label.hint : ''}`}>
      {label.text}
    </Pill>
  );
}

export function StageStatePill({ state }: { state: StageState }): JSX.Element {
  const label = stageStateLabel(state);
  return (
    <Pill tone={label.tone} transition={label.transitioning} title={`${state}${label.hint ? ' — ' + label.hint : ''}`}>
      {label.text}
    </Pill>
  );
}

export function SeverityPill({ severity }: { severity: Severity }): JSX.Element {
  const label = SEVERITY_LABELS[severity];
  return <Pill tone={label.tone} plain>{label.text}</Pill>;
}

/** 通用小标签。 */
export function Chip({
  children,
  variant,
  onClick,
  title,
}: {
  children: ReactNode;
  variant?: 'accent' | 'warn' | 'danger' | 'off';
  onClick?: () => void;
  title?: string;
}): JSX.Element {
  const cls = ['chip'];
  if (variant) cls.push(`chip--${variant}`);
  if (onClick) cls.push('chip--removable');
  return (
    <span className={cls.join(' ')} onClick={onClick} title={title}>
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// 空态与加载
// ---------------------------------------------------------------------------

export function Empty({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
}): JSX.Element {
  return (
    <div className="empty">
      <div className="empty__title">{title}</div>
      {hint ? <div>{hint}</div> : null}
      {action ? <div style={{ marginTop: 'var(--sp-3)' }}>{action}</div> : null}
    </div>
  );
}

export function Loading({ label = '加载中' }: { label?: string }): JSX.Element {
  return (
    <div className="empty">
      <span className="spin" /> <span style={{ marginLeft: 8 }}>{label}…</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 提示条
// ---------------------------------------------------------------------------

export function Banner({
  variant = 'info',
  title,
  children,
  hint,
  actions,
}: {
  variant?: 'info' | 'warn' | 'danger' | 'ok';
  title?: ReactNode;
  children?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className={`banner banner--${variant}`}>
      <div className="banner__body">
        {title ? <div className="banner__title">{title}</div> : null}
        {children}
        {hint ? <div className="banner__hint">{hint}</div> : null}
      </div>
      {actions ? <div className="row row--tight">{actions}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 弹层
// ---------------------------------------------------------------------------

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}): JSX.Element {
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={wide ? 'modal modal--wide' : 'modal'} role="dialog" aria-modal="true">
        <div className="modal__head">
          <div className="modal__title">{title}</div>
          <button type="button" className="btn btn--ghost btn--sm modal__close" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="modal__body">{children}</div>
        {footer ? <div className="modal__foot">{footer}</div> : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 字段
// ---------------------------------------------------------------------------

export function Field({
  label,
  hint,
  error,
  required,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  children: ReactNode;
}): JSX.Element {
  return (
    <label className="field">
      <span className="field__label">
        {label}
        {required ? (
          <span className="field__req" title="必填">
            *
          </span>
        ) : null}
      </span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
      {error ? <span className="field__error">{error}</span> : null}
    </label>
  );
}

export function KV({ items }: { items: { k: ReactNode; v: ReactNode }[] }): JSX.Element {
  return (
    <div className="kv">
      {items.map((item, idx) => (
        <div key={idx} style={{ display: 'contents' }}>
          <div className="kv__k">{item.k}</div>
          <div className="kv__v">{item.v}</div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 杂项
// ---------------------------------------------------------------------------

/** 长 ID 显示成短前缀，鼠标悬停看全文（列表里全量 UUID 会把版面撑爆）。 */
export function ShortId({ id, len = 8 }: { id: string | null | undefined; len?: number }): JSX.Element {
  if (!id) return <span className="dim">—</span>;
  return (
    <span className="mono" title={id}>
      {id.slice(0, len)}
    </span>
  );
}

export function TimeText({ value }: { value: string | null | undefined }): JSX.Element {
  if (!value) return <span className="dim">—</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <span className="mono">{value}</span>;
  return (
    <span className="mono" title={value}>
      {date.toLocaleString('zh-CN', { hour12: false })}
    </span>
  );
}

/** 相对时间：运维看「3 分钟前」比看绝对时间快。 */
export function RelTime({ value }: { value: string | null | undefined }): JSX.Element {
  if (!value) return <span className="dim">—</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <span className="mono">{value}</span>;
  const delta = Date.now() - date.getTime();
  return (
    <span className="mono" title={date.toLocaleString('zh-CN', { hour12: false })}>
      {humanDuration(delta)}前
    </span>
  );
}

export function humanDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '未知';
  const abs = Math.abs(ms);
  if (abs < 1000) return `${Math.round(abs)} 毫秒`;
  const sec = abs / 1000;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)} 秒`;
  const min = sec / 60;
  if (min < 60) return `${min.toFixed(min < 10 ? 1 : 0)} 分`;
  const hour = min / 60;
  if (hour < 24) return `${hour.toFixed(hour < 10 ? 1 : 0)} 小时`;
  return `${(hour / 24).toFixed(1)} 天`;
}

/** 两个时间戳之间的耗时；缺任一端返回 null（未知，不是 0）。 */
export function durationBetween(start: string | null, end: string | null): number | null {
  if (!start) return null;
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return b - a;
}

/**
 * 未知 ≠ 零（OBS-04）。用量不可取得时显示「未知」，不显示 0。
 */
export function CountOrUnknown({ value }: { value: number | null | undefined }): JSX.Element {
  if (value === null || value === undefined) return <span className="dim" title="不可取得">未知</span>;
  return <span className="mono">{value.toLocaleString('zh-CN')}</span>;
}

export function Bytes({ value }: { value: number | null | undefined }): JSX.Element {
  if (value === null || value === undefined) return <span className="dim">未知</span>;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return (
    <span className="mono">
      {n.toFixed(i === 0 ? 0 : 1)} {units[i]}
    </span>
  );
}

/** 折叠区。默认收起，避免把运维页面撑成文档。 */
export function Disclosure({
  summary,
  children,
  open,
  onToggle,
  meta,
}: {
  summary: ReactNode;
  children: ReactNode;
  open: boolean;
  onToggle: () => void;
  meta?: ReactNode;
}): JSX.Element {
  return (
    <div>
      <div
        className="row row--tight"
        style={{ cursor: 'pointer', padding: '4px 0' }}
        onClick={onToggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') onToggle();
        }}
      >
        <span className="dim mono" style={{ width: 12 }}>
          {open ? '▾' : '▸'}
        </span>
        <div className="truncate" style={{ flex: '1 1 auto' }}>
          {summary}
        </div>
        {meta}
      </div>
      {open ? <div style={{ paddingLeft: 18 }}>{children}</div> : null}
    </div>
  );
}
