/**
 * 存储与清理（route `/storage`）。
 *
 * 这个页面的第一职责是**如实**：
 * 1. **不猜后端字段**。`StorageReport.database` / `artifacts` 是 `Record<string, unknown>`，
 *    键集没有在前端契约里固定——所以逐键原样渲染，不挑字段、不留空、不编默认值。
 * 2. **未清理资源不许藏**。拆除失败或归属不明的资源会一直显示，直到被处理。
 * 3. **清理前先说明删什么**。默认 dry_run；真实执行前必须过一次确认弹层，
 *    弹层里逐项列出将发送的参数。未知 ≠ 零：取不到的计数显示「未知」。
 */

import { useCallback, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { PruneResult, StorageReport, SystemStatus } from '../api/types';
import { system } from '../api/endpoints';
import { asArray, isRecord } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import {
  Banner,
  Bytes,
  Chip,
  CountOrUnknown,
  Empty,
  Field,
  KV,
  Loading,
  Modal,
  Pill,
} from '../components/common';
import { ErrorBanner } from '../components/TaskControls';
import { hostingDescription } from '../store/system';

// ---------------------------------------------------------------------------
// 读取辅助：后端字段可能缺失（早期内核）。缺失时给 null，界面显示「未知」而不是 0。
// ---------------------------------------------------------------------------

function nested(source: unknown, key: string): unknown {
  return isRecord(source) ? source[key] : undefined;
}

function numField(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function boolField(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

/** 键名里出现 size/bytes 就当字节数看，其余数字按千分位。 */
function looksLikeBytes(key: string): boolean {
  const lower = key.toLowerCase();
  return lower.includes('size') || lower.includes('bytes');
}

// ---------------------------------------------------------------------------
// 通用键值渲染
// ---------------------------------------------------------------------------

/** 单个值：数字带千分位、字节量走 Bytes、对象/数组原样 JSON 展开。 */
function GenericValue({ name, value }: { name: string; value: unknown }): JSX.Element {
  if (value === null) {
    return (
      <span className="dim" title="null">
        —
      </span>
    );
  }
  if (value === undefined) {
    return (
      <span className="dim" title="内核未返回该值">
        —
      </span>
    );
  }

  if (typeof value === 'number') {
    if (looksLikeBytes(name)) return <Bytes value={value} />;
    return <span className="mono">{value.toLocaleString('zh-CN')}</span>;
  }
  if (typeof value === 'boolean') {
    return <span className="mono">{value ? 'true' : 'false'}</span>;
  }
  if (typeof value === 'string') {
    if (!value) return <span className="dim">（空）</span>;
    return <span style={{ overflowWrap: 'anywhere' }}>{value}</span>;
  }

  // 对象 / 数组：原样展开。**不猜内部结构**——键集本来就未在前端固定。
  const text = JSON.stringify(value, null, 2);
  return <pre className="code-block">{text || String(value)}</pre>;
}

/** 通用键值表：有多少键就渲染多少行，一个都不省。 */
function KeyValueTable({ what, rows }: { what: string; rows: [string, unknown][] }): JSX.Element {
  if (rows.length === 0) {
    return (
      <Empty
        title={`内核未返回「${what}」的任何字段`}
        hint="前端不假设键名，也不编造缺省值；这里只显示内核真正给了什么。"
      />
    );
  }
  return (
    <div className="table-wrap">
      <table className="table table--dense">
        <thead>
          <tr>
            <th style={{ width: '42%' }}>键</th>
            <th>值</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, value]) => (
            <tr key={key}>
              <td className="mono text-xs">{key}</td>
              <td>
                <GenericValue name={key} value={value} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StorageSection({
  title,
  data,
  hint,
}: {
  title: string;
  data: Record<string, unknown>;
  hint?: ReactNode;
}): JSX.Element {
  const rows = useMemo(() => Object.entries(data), [data]);
  return (
    <div>
      <div className="section-title">
        {title} · {rows.length} 个字段
      </div>
      {hint ? (
        <div className="text-xs muted" style={{ marginBottom: 'var(--sp-2)' }}>
          {hint}
        </div>
      ) : null}
      <KeyValueTable what={title} rows={rows} />
    </div>
  );
}

function Stat({ label, value, title }: { label: string; value: number | null; title?: string }): JSX.Element {
  return (
    <div title={title}>
      <div className="text-xs muted">{label}</div>
      <div className="mono" style={{ fontSize: 'var(--fs-lg)', fontWeight: 600 }}>
        <CountOrUnknown value={value} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 系统状态
// ---------------------------------------------------------------------------

function SystemPanel({ status }: { status: SystemStatus }): JSX.Element {
  const hosting = hostingDescription(status);
  const inProcess = status.session_hosting === 'in_process';
  const notes = asArray<string>(status.startup_notes);

  const schedulerEnabled = boolField(nested(status.scheduler, 'enabled'));
  const pollInterval = numField(nested(status.scheduler, 'poll_interval'));

  const reaperInterval = numField(nested(status.reaper, 'interval_seconds'));
  const artifactGc = boolField(nested(status.reaper, 'artifact_gc_enabled'));
  const lastRunRaw = nested(status.reaper, 'last_run');
  const lastRun = isRecord(lastRunRaw) ? lastRunRaw : null;

  const counts = status.counts;
  const notifierSubscribers = numField(nested(status, 'notifier_subscribers'));
  const notifierDropped = numField(nested(status, 'notifier_dropped'));

  const accessBoundary = status.loopback_only
    ? '仅本机（loopback）'
    : status.allow_remote
      ? '已开放远端访问，注意 UI-02'
      : '既未限定本机、也未声明开放远端——以内核声明为准';

  return (
    <div className="col" style={{ gap: 'var(--sp-3)' }}>
      {/* 部署形态：in_process 时必须刺眼——否则用户会默认「重启不丢任务」而丢工作。 */}
      {inProcess ? (
        <Banner variant="warn" title={<Pill tone="warn">{hosting.label}</Pill>}>
          {hosting.detail}
        </Banner>
      ) : (
        <div className="row row--tight">
          <Pill tone={hosting.variant === 'ok' ? 'success' : 'warn'}>{hosting.label}</Pill>
          <span className="text-sm muted">{hosting.detail}</span>
        </div>
      )}

      {/* 运行提示：不折叠、不截断、不塞进 tooltip。 */}
      {notes.length > 0 ? (
        <Banner variant="warn" title={`运行提示 · ${notes.length} 项`}>
          <ul className="list-reset text-sm">
            {notes.map((note, index) => (
              <li key={index}>
                · <span style={{ overflowWrap: 'anywhere' }}>{note}</span>
              </li>
            ))}
          </ul>
        </Banner>
      ) : null}

      <KV
        items={[
          { k: '内核版本', v: <span className="mono">{status.version}</span> },
          {
            k: '数据目录',
            v: (
              <span className="mono" style={{ overflowWrap: 'anywhere' }}>
                {status.data_dir}
              </span>
            ),
          },
          {
            k: '工作目录',
            v: (
              <span className="mono" style={{ overflowWrap: 'anywhere' }}>
                {status.workspace_dir}
              </span>
            ),
          },
          {
            k: '凭据库',
            v: status.secrets_unlocked ? (
              <span className="text-success">已解锁</span>
            ) : (
              <span>
                <span className="text-warn">未解锁</span>
                <span className="text-xs muted">。引用凭据的节点会明确报错，不会匿名运行。</span>
              </span>
            ),
          },
          { k: 'harness 附加', v: status.harness_attached ? '已附加' : '未附加' },
          { k: '访问边界', v: accessBoundary },
          {
            k: '调度器',
            v: schedulerEnabled === null ? '未知' : schedulerEnabled ? '已装配' : '未装配',
          },
          {
            k: '调度轮询间隔',
            v:
              pollInterval === null ? (
                <span className="dim">未知</span>
              ) : (
                <span className="mono">{pollInterval} 秒</span>
              ),
          },
          {
            k: '清理器间隔',
            v:
              reaperInterval === null ? (
                <span className="dim">未知</span>
              ) : (
                <span className="mono">{reaperInterval} 秒</span>
              ),
          },
          { k: '产物回收', v: artifactGc === null ? '未知' : artifactGc ? '已启用' : '未启用' },
          { k: '已连接的页面数', v: <CountOrUnknown value={notifierSubscribers} /> },
          { k: '丢弃的推送数', v: <CountOrUnknown value={notifierDropped} /> },
        ]}
      />

      {schedulerEnabled === false ? (
        <Banner variant="warn" title="调度循环未装配">
          任务不会自动推进，排队与就绪阶段会一直停在那里。
        </Banner>
      ) : null}

      {notifierDropped !== null && notifierDropped > 0 ? (
        <Banner variant="warn" title="有客户端跟不上推送">
          有客户端跟不上推送，重连后靠 REST 拉真实状态。
        </Banner>
      ) : null}

      <div>
        <div className="section-title">计数</div>
        <div className="row" style={{ gap: 'var(--sp-5)' }}>
          <Stat label="存活任务" value={numField(nested(counts, 'live_tasks'))} title="counts.live_tasks" />
          <Stat
            label="运行中阶段"
            value={numField(nested(counts, 'running_stages'))}
            title="counts.running_stages"
          />
          <Stat
            label="等待中阶段"
            value={numField(nested(counts, 'pending_stages'))}
            title="counts.pending_stages"
          />
          <Stat
            label="待处理审批"
            value={numField(nested(counts, 'open_approvals'))}
            title="counts.open_approvals"
          />
          <Stat label="流程数" value={numField(nested(counts, 'workflows'))} title="counts.workflows" />
        </div>
      </div>

      <div>
        <div className="section-title">清理器上次运行</div>
        {lastRun === null ? (
          <div className="text-sm dim">尚未运行</div>
        ) : (
          <KeyValueTable what="reaper.last_run" rows={Object.entries(lastRun)} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export function StoragePage(): JSX.Element {
  const fetchStatus = useCallback((_signal: AbortSignal) => system.status(), []);
  const statusState = useAsync(fetchStatus, []);

  const fetchStorage = useCallback((_signal: AbortSignal) => system.storage(), []);
  const { data, loading, error, loaded, reload, setData } = useAsync(fetchStorage, []);
  const [fromPrune, setFromPrune] = useState(false);
  const [result, setResult] = useState<PruneResult | null>(null);

  // --- 清理表单 ---
  const [dryRun, setDryRun] = useState(true);
  const [taskId, setTaskId] = useState('');
  const [keepLast, setKeepLast] = useState('0');
  const [olderThan, setOlderThan] = useState('');
  const [includeOrphans, setIncludeOrphans] = useState(false);
  const [includeArtifacts, setIncludeArtifacts] = useState(false);
  const [includeUnreferenced, setIncludeUnreferenced] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const submit = useSubmit();

  const taskIdValue = taskId.trim();
  const conflict = taskIdValue !== '' && olderThan !== '';
  const olderThanDate = olderThan === '' ? null : new Date(olderThan);
  const olderThanInvalid = olderThanDate !== null && Number.isNaN(olderThanDate.getTime());
  const olderThanIso =
    olderThanDate !== null && !Number.isNaN(olderThanDate.getTime()) ? olderThanDate.toISOString() : null;

  const keepLastNum = keepLast.trim() === '' ? 0 : Number(keepLast);
  const keepLastInvalid = !Number.isInteger(keepLastNum) || keepLastNum < 0;
  const keepLastValue = keepLastInvalid ? 0 : keepLastNum;

  const formError: string | null = conflict
    ? '二者互斥：task_id 与 older_than 只能填一个'
    : olderThanInvalid
      ? '时间无效，请重新选择'
      : keepLastInvalid
        ? '保留条数必须是非负整数'
        : null;

  const refreshAll = (): void => {
    statusState.reload();
    reload();
    setFromPrune(false);
  };

  const runPrune = async (): Promise<void> => {
    setConfirming(false);
    const pruned = await submit.run(() =>
      system.prune({
        dry_run: dryRun,
        // 未填就是 null，绝不发空字符串。
        task_id: taskIdValue === '' ? null : taskIdValue,
        keep_last: keepLastValue,
        older_than: olderThanIso,
        include_orphans: includeOrphans,
        include_artifacts: includeArtifacts,
        include_unreferenced_artifacts: includeUnreferenced,
      }),
    );
    if (pruned) {
      setResult(pruned);
      // 响应里带着清理后的存储报告：直接用它重渲染，不再多打一次 GET /api/storage。
      setData(pruned.storage);
      setFromPrune(true);
    }
  };

  const onSubmitClick = (): void => {
    if (formError !== null) return;
    if (dryRun) void runPrune();
    else setConfirming(true);
  };

  const confirmItems: { k: string; v: ReactNode }[] = [
    {
      k: 'dry_run',
      v: dryRun ? (
        <span className="text-success">true（仅预览，不删除）</span>
      ) : (
        <span className="text-danger">false（真实删除，不可撤销）</span>
      ),
    },
    {
      k: 'task_id',
      v:
        taskIdValue === '' ? (
          <span className="dim">null（不限任务）</span>
        ) : (
          <span className="mono">{taskIdValue}</span>
        ),
    },
    {
      k: 'older_than',
      v: olderThanIso ? (
        <span className="mono">{olderThanIso}</span>
      ) : (
        <span className="dim">null（不限时间）</span>
      ),
    },
    { k: 'keep_last', v: <span className="mono">{keepLastValue.toLocaleString('zh-CN')}</span> },
    { k: 'include_orphans', v: <span className="mono">{includeOrphans ? 'true' : 'false'}</span> },
    { k: 'include_artifacts', v: <span className="mono">{includeArtifacts ? 'true' : 'false'}</span> },
    {
      k: 'include_unreferenced_artifacts',
      v: <span className="mono">{includeUnreferenced ? 'true' : 'false'}</span>,
    },
  ];

  const report: StorageReport | null = data;
  const database = report && isRecord(report.database) ? report.database : {};
  const artifacts = report && isRecord(report.artifacts) ? report.artifacts : {};
  const unresolved = report ? numField(report.unresolved_resources) : null;
  const lastRun = report && isRecord(report.last_run) ? report.last_run : null;

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>存储与清理</h1>
          <div className="page-head__sub">
            部署形态、运行提示与数据占用。手动清理只能从这一页发起，默认只做预览。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn" onClick={refreshAll}>
            刷新
          </button>
        </div>
      </div>

      {/* ---------------- 系统状态 ---------------- */}
      <div className="panel">
        <div className="panel__head">
          系统状态
          <span className="panel__head-actions">
            {!statusState.loaded && statusState.loading ? <span className="spin" /> : null}
            <span className="text-xs muted">GET /api/system/status</span>
          </span>
        </div>

        {statusState.error ? (
          <div className="panel__body">
            <Banner
              variant="danger"
              title={statusState.error.unreachable ? '无法连接内核' : '无法获取系统状态'}
              hint={
                statusState.data
                  ? '下列内容是最近一次成功读取的结果，可能已经过期。'
                  : (statusState.error.hint ?? undefined)
              }
              actions={
                <button type="button" className="btn btn--sm" onClick={statusState.reload}>
                  重试
                </button>
              }
            >
              {statusState.error.detail}
            </Banner>
          </div>
        ) : null}

        {!statusState.loaded && statusState.loading ? <Loading label="读取系统状态" /> : null}

        {/* 读不到就不显示数字：未知 ≠ 零，这里绝不渲染占位的 0 计数。 */}
        {statusState.data ? (
          <div className="panel__body">
            <SystemPanel status={statusState.data} />
          </div>
        ) : null}
      </div>

      {/* ---------------- 存储占用 ---------------- */}
      <div className="panel">
        <div className="panel__head">
          存储占用
          <span className="panel__head-actions">
            {fromPrune ? (
              <span className="text-xs muted" title="来自最近一次清理响应内附的 storage">
                含最近一次清理结果
              </span>
            ) : (
              <span className="text-xs muted">GET /api/storage</span>
            )}
            <button type="button" className="btn btn--sm" onClick={refreshAll} disabled={loading && !loaded}>
              {loading && !loaded ? <span className="spin" /> : null}
              刷新
            </button>
          </span>
        </div>

        {!loaded && loading ? <Loading label="读取存储报告" /> : null}

        {error ? (
          <div className="panel__body">
            <Banner
              variant="danger"
              title={error.unreachable ? '无法连接内核' : '无法获取存储报告'}
              hint={data ? '下表是最近一次成功读取的结果，可能已经过期。' : (error.hint ?? undefined)}
              actions={
                <button type="button" className="btn btn--sm" onClick={refreshAll}>
                  重试
                </button>
              }
            >
              {error.detail}
            </Banner>
          </div>
        ) : null}

        {report ? (
          <div className="panel__body">
            <div className="grid grid--2">
              <StorageSection
                title="数据库"
                data={database}
                hint="键集未在前端契约中固定：这里原样列出内核返回的每一个键，不挑字段、不猜键名。"
              />
              <StorageSection
                title="产物"
                data={artifacts}
                hint="同上。字节量按原始字节数换算显示，对象与数组原样展开。"
              />
            </div>

            <div className="divider" />

            <div className="section-title">未清理资源</div>
            <div className="row row--tight" style={{ marginBottom: 'var(--sp-2)' }}>
              <CountOrUnknown value={unresolved} />
              <span className="text-sm muted">项</span>
            </div>
            {unresolved !== null && unresolved > 0 ? (
              <Banner variant="warn" title={`有 ${unresolved.toLocaleString('zh-CN')} 项资源未清理`}>
                这些是拆除失败、或归属无法确认的资源。它们会一直显示在这里，直到被处理——
                界面不会把它们藏起来，这个计数也不表示它们已经被回收。
              </Banner>
            ) : null}

            <div style={{ marginTop: 'var(--sp-3)' }}>
              <div className="section-title">上次清理（last_run）</div>
              {lastRun === null ? (
                <div className="text-sm dim">尚未运行</div>
              ) : (
                <KeyValueTable what="last_run" rows={Object.entries(lastRun)} />
              )}
            </div>
          </div>
        ) : null}

        {!report && loaded && !error ? (
          <Empty title="内核未返回存储报告" hint="没有数据时这里保持为空，不显示编造的占用数字。" />
        ) : null}
      </div>

      {/* ---------------- 手动清理 ---------------- */}
      <div className="panel">
        <div className="panel__head">手动清理</div>
        <div className="panel__hint panel__hint--warn">
          清理是永久删除，不能撤销。默认只预览，不实际删除。
        </div>
        <div className="panel__body">
          <div className="col" style={{ gap: 'var(--sp-3)' }}>
            <div>
              <label className="check">
                <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
                仅预览（不删除）
              </label>
              <div className="text-xs muted" style={{ marginTop: 2 }}>
                <span className="mono">dry_run = true</span>：内核只回报「将要删除什么」，不改动任何数据。
              </div>
            </div>

            <div className="field-row">
              <Field
                label="任务 ID（task_id，可选）"
                hint="按任务清理其事件历史；留空即不按任务限定"
                error={conflict ? '二者互斥：已填 older_than，请清空其中一项' : undefined}
              >
                <input
                  className="input input--mono"
                  value={taskId}
                  placeholder="留空 = 不限任务"
                  onChange={(e) => setTaskId(e.target.value)}
                />
              </Field>

              <Field
                label="保留最新条数（keep_last）"
                hint="保留最新条数，更早的才进入清理范围"
                error={keepLastInvalid ? '必须是非负整数' : undefined}
              >
                <input
                  className="input input--num"
                  type="number"
                  min={0}
                  value={keepLast}
                  onChange={(e) => setKeepLast(e.target.value)}
                />
              </Field>

              <Field
                label="早于此时间（older_than）"
                hint="时间界，与 task_id 互斥；按本机时区填写，发送时转成 ISO"
                error={
                  conflict ? '二者互斥：已填 task_id，请清空其中一项' : olderThanInvalid ? '时间无效' : undefined
                }
              >
                <input
                  className="input"
                  type="datetime-local"
                  value={olderThan}
                  onChange={(e) => setOlderThan(e.target.value)}
                />
              </Field>
            </div>

            {olderThanIso ? (
              <div className="text-xs dim">
                将发送 <span className="mono">older_than = {olderThanIso}</span>
              </div>
            ) : null}

            <div className="col" style={{ gap: 'var(--sp-1)' }}>
              <label className="check">
                <input
                  type="checkbox"
                  checked={includeOrphans}
                  onChange={(e) => setIncludeOrphans(e.target.checked)}
                />
                顺带跑一轮孤儿进程／句柄对账
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={includeArtifacts}
                  onChange={(e) => setIncludeArtifacts(e.target.checked)}
                />
                回收已标记删除且无引用的产物文件
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={includeUnreferenced}
                  onChange={(e) => setIncludeUnreferenced(e.target.checked)}
                />
                连同未标记删除的零引用产物一并回收
              </label>
              <div className="text-xs text-warn">
                更激进——连未标记删除的零引用产物一并回收；仍会拒绝清理被活跃任务引用的数据。
              </div>
            </div>

            <div className="row">
              <button
                type="button"
                className={dryRun ? 'btn btn--primary' : 'btn btn--danger'}
                disabled={submit.busy || formError !== null}
                onClick={onSubmitClick}
              >
                {dryRun ? '预览清理（不删除）' : '真实清理（不可撤销）'}
              </button>
              {submit.busy ? <span className="spin" /> : null}
              {formError ? <span className="text-xs text-danger">{formError}</span> : null}
            </div>

            <ErrorBanner error={submit.error} />

            <div className="text-xs dim">
              实际删除范围以内核返回的 <span className="mono">actions</span> 为准——下方逐条列出，不由前端推断。
            </div>
          </div>
        </div>
      </div>

      {/* ---------------- 动作结果 ---------------- */}
      {result ? (
        <div className="panel">
          <div className="panel__head">
            {result.dry_run ? '将要执行的动作（预览）' : '已执行的动作'}
            <span className="panel__head-actions">
              <span className="text-xs muted">{result.actions.length} 项</span>
              <Chip variant={result.dry_run ? 'accent' : 'danger'}>
                {result.dry_run ? 'dry_run = true' : 'dry_run = false'}
              </Chip>
            </span>
          </div>

          {result.actions.length === 0 ? (
            <Empty
              title="内核没有返回任何动作"
              hint="没有需要清理的内容，或内核认为本次参数下没有可清理项。"
            />
          ) : (
            <div className="table-wrap">
              <table className="table table--dense">
                <thead>
                  <tr>
                    <th>动作（kind）</th>
                    <th>范围（scope）</th>
                    <th className="table__num" title="该动作涉及／删除的记录数">
                      {result.dry_run ? '将删除' : '已删除'}
                    </th>
                    <th>明细（detail）</th>
                  </tr>
                </thead>
                <tbody>
                  {result.actions.map((action, index) => (
                    <tr key={`${action.kind}-${action.scope}-${index}`}>
                      <td className="mono text-xs">{action.kind}</td>
                      <td className="mono text-xs">{action.scope}</td>
                      <td className="table__num">
                        {typeof action.deleted === 'number' ? action.deleted.toLocaleString('zh-CN') : '未知'}
                      </td>
                      <td>
                        {action.detail && Object.keys(action.detail).length > 0 ? (
                          <GenericValue name="detail" value={action.detail} />
                        ) : (
                          <span className="dim">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="panel__body">
            <div className="section-title">说明（notes）</div>
            {result.notes.length === 0 ? (
              <div className="text-sm dim">内核未返回说明。</div>
            ) : (
              <ul className="list-reset text-sm">
                {result.notes.map((note, index) => (
                  <li key={index}>
                    · <span style={{ overflowWrap: 'anywhere' }}>{note}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="text-xs dim" style={{ marginTop: 'var(--sp-2)' }}>
              上方「存储占用」已按本次响应内附的报告刷新，未额外重取。
            </div>
          </div>
        </div>
      ) : null}

      {confirming ? (
        <Modal
          title="确认真实清理（dry_run = false）"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--danger"
                disabled={submit.busy}
                onClick={() => void runPrune()}
              >
                确认执行清理
              </button>
            </>
          }
        >
          <Banner variant="danger" title="这一步真的会删数据">
            以下参数将原样发送给内核 <span className="mono">POST /api/storage/prune</span>。删除不可撤销，
            也不会先进回收站。
          </Banner>
          <KV items={confirmItems} />
          <div className="text-xs muted" style={{ marginTop: 'var(--sp-3)' }}>
            内核对每类数据的处理规则以返回的 <span className="mono">actions</span> 为准；执行后会在本页
            「已执行的动作」下列出实际删除了什么。
          </div>
          {includeUnreferenced ? (
            <div style={{ marginTop: 'var(--sp-3)' }}>
              <Banner variant="warn" title="已启用最彻底的产物回收">
                连没有被标记删除的零引用产物也会被回收。仍被活跃任务引用的数据不会被清理。
              </Banner>
            </div>
          ) : null}
        </Modal>
      ) : null}
    </div>
  );
}
