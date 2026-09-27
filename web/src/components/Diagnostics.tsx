/**
 * 校验结论列表（WF-05 / ACT-03 / RUN-01）。
 *
 * 硬要求：「错误应定位到节点、连线或配置」——**每一条都必须点得动**，
 * 点了就在画布上高亮并把视口居中过去。做不到定位的条目要如实说明是全局项，
 * 而不是给个点了没反应的假按钮。
 */

import type { Diagnostic, Severity } from '../api/types';
import { SEVERITY_LABELS } from '../labels';
import { Pill } from './common';

export interface DiagnosticTarget {
  node_id: string | null;
  edge: [string, string] | null;
  profile_id: string | null;
  slot: string | null;
}

export function diagnosticTarget(d: Diagnostic): DiagnosticTarget | null {
  if (d.node_id || d.edge) {
    return { node_id: d.node_id, edge: d.edge, profile_id: d.profile_id, slot: d.slot };
  }
  return null;
}

export interface DiagnosticsProps {
  diagnostics: Diagnostic[];
  /** 点一条时把定位信息交给画布。为 null 时该条不可点。 */
  onLocate?: (target: DiagnosticTarget) => void;
  /** 已选中的条目（用于高亮）。 */
  selectedKey?: string | null;
  /** 只显示某一严重级。 */
  only?: Severity;
  emptyText?: string;
}

export function diagnosticKey(d: Diagnostic, index: number): string {
  return `${d.code}:${d.node_id ?? ''}:${d.edge ? d.edge.join('>') : ''}:${d.slot ?? ''}:${index}`;
}

export function Diagnostics({
  diagnostics,
  onLocate,
  selectedKey,
  only,
  emptyText = '没有校验结论。',
}: DiagnosticsProps): JSX.Element {
  const list = only ? diagnostics.filter((d) => d.severity === only) : diagnostics;
  if (list.length === 0) {
    return <div className="empty text-sm">{emptyText}</div>;
  }
  return (
    <ul className="list-reset">
      {list.map((d, index) => {
        const key = diagnosticKey(d, index);
        const target = diagnosticTarget(d);
        const clickable = Boolean(target && onLocate);
        const selected = selectedKey === key;
        return (
          <li
            key={key}
            onClick={clickable ? () => onLocate?.(target as DiagnosticTarget) : undefined}
            style={{
              display: 'flex',
              gap: 'var(--sp-2)',
              alignItems: 'flex-start',
              padding: '6px var(--sp-2)',
              borderBottom: '1px solid var(--line-faint)',
              cursor: clickable ? 'pointer' : 'default',
              background: selected ? 'var(--bg-sel)' : undefined,
            }}
            title={clickable ? '点击在画布上定位' : '全局项，无法定位到节点或连线'}
          >
            <SeverityMark severity={d.severity} />
            <div style={{ minWidth: 0, flex: '1 1 auto' }}>
              <div className="row row--tight" style={{ alignItems: 'baseline' }}>
                <span>{d.message}</span>
                <span className="dim mono text-xs">{d.code}</span>
                {d.requirement ? <span className="dim text-xs">{d.requirement}</span> : null}
              </div>
              <div className="row row--tight text-xs dim" style={{ marginTop: 2 }}>
                <LocationText diagnostic={d} />
                {d.slot ? <span className="mono">{d.slot}</span> : null}
                {!target ? <span>（全局项）</span> : null}
              </div>
              {d.hint ? <div className="text-xs muted" style={{ marginTop: 2 }}>建议：{d.hint}</div> : null}
            </div>
            {clickable ? <span className="dim text-xs nowrap">定位 →</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

function SeverityMark({ severity }: { severity: Severity }): JSX.Element {
  const label = SEVERITY_LABELS[severity];
  return (
    <Pill tone={label.tone} plain>
      {label.text}
    </Pill>
  );
}

function LocationText({ diagnostic }: { diagnostic: Diagnostic }): JSX.Element {
  const bits: string[] = [];
  if (diagnostic.node_name) bits.push(`节点「${diagnostic.node_name}」`);
  else if (diagnostic.node_id) bits.push(`节点 ${diagnostic.node_id.slice(0, 8)}`);
  if (diagnostic.edge) bits.push(`连线 ${diagnostic.edge[0].slice(0, 6)}→${diagnostic.edge[1].slice(0, 6)}`);
  if (bits.length === 0) return <span className="dim">—</span>;
  return <span>{bits.join(' · ')}</span>;
}

/** 按严重级分组的容器。 */
export function DiagnosticsGrouped({
  diagnostics,
  onLocate,
  selectedKey,
}: {
  diagnostics: Diagnostic[];
  onLocate?: (target: DiagnosticTarget) => void;
  selectedKey?: string | null;
}): JSX.Element {
  const groups: { severity: Severity; title: string }[] = [
    { severity: 'error', title: '错误（阻断发布 / 发射）' },
    { severity: 'warning', title: '警告（可继续，但需知悉）' },
    { severity: 'info', title: '说明' },
  ];
  const total = diagnostics.length;
  return (
    <div>
      {groups.map((group) => {
        const items = diagnostics.filter((d) => d.severity === group.severity);
        if (items.length === 0) return null;
        return (
          <div key={group.severity} style={{ marginBottom: 'var(--sp-3)' }}>
            <div className="section-title">
              {group.title} · {items.length}
            </div>
            <Diagnostics diagnostics={items} onLocate={onLocate} selectedKey={selectedKey} />
          </div>
        );
      })}
      {total === 0 ? <div className="empty text-sm">校验通过，没有结论。</div> : null}
    </div>
  );
}
