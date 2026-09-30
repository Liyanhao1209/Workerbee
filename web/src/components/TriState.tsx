/**
 * 生命周期操作的三个结论（LIFE-06）。
 *
 * **它们是三件事，不许合成一个「成功」。**
 * - 「已接受」只表示控制意图写进去了，不代表已经停下；
 * - 「执行已停止」是全部在途执行确认终止；
 * - 「资源清理完成」看的是台账里 closed 的句柄数。
 *
 * 清理未完成时必须继续可见并可处理，不能因为前面两步成功就报全部完成。
 */

import type { TriStateOutcome } from '../api/types';

export interface TriStateProps {
  outcome: TriStateOutcome;
  /** 资源计数单元格里的附加说明，例如「teardown_failed 需继续处理」。 */
  resourceNote?: string;
}

const RESOURCE_LABELS: Record<string, string> = {
  closed: '已关闭',
  closing: '关闭中',
  teardown_failed: '清理失败',
  orphaned: '归属不明',
  open: '仍打开',
  skipped: '跳过未清理',
};

export function TriState({ outcome, resourceNote }: TriStateProps): JSX.Element {
  const resources = Object.entries(outcome.resources ?? {});
  const pendingResources = resources.filter(
    ([key, count]) => count > 0 && ['teardown_failed', 'orphaned', 'open', 'closing'].includes(key),
  );

  return (
    <div>
      <div className="tristate">
        <Cell label="① 已接受操作" value={outcome.accepted} yes="已记录" no="未接受" />
        <Cell
          label="② 执行已停止"
          value={outcome.execution_stopped}
          yes="已确认终止"
          no="仍在停止中"
        />
        <div className={`tristate__cell tristate__cell--${pendingResources.length > 0 ? 'no' : 'yes'}`}>
          <div className="tristate__label">③ 资源清理完成</div>
          <div className="tristate__value">
            {resources.length === 0 ? '无归属资源' : pendingResources.length > 0 ? '仍有未清理项' : '已清理'}
          </div>
          {resources.length > 0 ? (
            <div className="chips" style={{ marginTop: 4 }}>
              {resources.map(([key, count]) => (
                <span
                  key={key}
                  className={
                    count > 0 && ['teardown_failed', 'orphaned'].includes(key)
                      ? 'chip chip--danger'
                      : 'chip'
                  }
                  title={key}
                >
                  {RESOURCE_LABELS[key] ?? key} {count}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      {resourceNote ? <div className="text-xs muted" style={{ marginTop: 6 }}>{resourceNote}</div> : null}
      {outcome.warnings.length > 0 ? (
        <ul className="list-reset" style={{ marginTop: 'var(--sp-2)' }}>
          {outcome.warnings.map((w, i) => (
            <li key={i} className="text-sm text-warn">
              · {w}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Cell({
  label,
  value,
  yes,
  no,
}: {
  label: string;
  value: boolean;
  yes: string;
  no: string;
}): JSX.Element {
  return (
    <div className={`tristate__cell tristate__cell--${value ? 'yes' : 'no'}`}>
      <div className="tristate__label">{label}</div>
      <div className="tristate__value">{value ? yes : no}</div>
    </div>
  );
}
