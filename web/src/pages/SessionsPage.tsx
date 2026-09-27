/**
 * 会话台账（route `/sessions`）。
 *
 * 排障时最常被问的两个问题，这个页面负责回答：
 *   「我的任务到底还连着哪个 session？」——按任务筛，列出归属的会话；
 *   「它还活着吗？」——状态 + 最近心跳，心跳超时**明确说超时**，不含糊。
 *
 * 三条纪律：
 * 1. `state` 是台账里的自由文本列，**不是**内核枚举。只翻译代码里确实出现过的取值
 *    （alive / lost / disposed / ended），其余原样显示英文——看到一个不认识的会话状态时，
 *    用户需要的是原文，而不是一个猜出来的中文词。
 * 2. 心跳超时只在**时间戳自带时区**时才判定。裸时间字符串会被浏览器按本地时区解析，
 *    跨时区直接差若干小时，据此报「心跳停滞」是假警报。判不了就不判，只显示时间。
 * 3. 列表为空与「连不上内核」必须长得不一样（连接失败显示横幅 + 重试，不渲染空表）。
 */

import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { SessionRecord } from '../api/types';
import { sessions as sessionApi } from '../api/endpoints';
import { asArray, asString, isRecord } from '../api/guards';
import { useAsync } from '../hooks/useAsync';
import { useSystem } from '../store/system';
import {
  Banner,
  Chip,
  Empty,
  Field,
  Loading,
  Pill,
  RelTime,
  ShortId,
  TimeText,
  humanDuration,
} from '../components/common';
import { SESSION_STATE_LABELS } from '../labels';

/**
 * supervisor 的心跳循环每 5 秒刷一次台账（`supervisor/server.py` 的 `_heartbeat_loop`）。
 * 超过 60 秒没刷新，说明刷心跳的那一侧没在跑（supervisor 没起来 / 形态是 in_process），
 * 或者循环卡住了——两种都值得看一眼。这个阈值是常数，不是凭感觉定的。
 */
const HEARTBEAT_STALE_MS = 60_000;
const HEARTBEAT_INTERVAL_NOTE = 'supervisor 每 5 秒刷一次心跳，超过 60 秒未刷新即标记';

function normalizeSession(raw: unknown): SessionRecord | null {
  if (!isRecord(raw)) return null;
  const sessionRef = asString(raw['session_ref']);
  if (!sessionRef) return null;
  return {
    session_ref: sessionRef,
    harness_id: asString(raw['harness_id']),
    owner_task_id: typeof raw['owner_task_id'] === 'string' ? raw['owner_task_id'] : null,
    owner_stage_id: typeof raw['owner_stage_id'] === 'string' ? raw['owner_stage_id'] : null,
    owner_attempt_id: typeof raw['owner_attempt_id'] === 'string' ? raw['owner_attempt_id'] : null,
    state: asString(raw['state']),
    created_at: typeof raw['created_at'] === 'string' ? raw['created_at'] : null,
    last_heartbeat: typeof raw['last_heartbeat'] === 'string' ? raw['last_heartbeat'] : null,
  };
}

/**
 * 只有带显式时区的时间戳才敢用来做判断。
 * `2026-09-27T17:42:00+00:00` / `...Z` → 可信；`2026-09-27 17:42:00` → 交给浏览器按本地
 * 时区解析，误差随时区大小而定，因此**不用它下结论**。
 */
function parseExplicitTs(value: string | null): number | null {
  if (!value) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(value.trim());
  if (!hasZone) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function SessionStatePill({ state }: { state: string }): JSX.Element {
  const known = SESSION_STATE_LABELS[state];
  if (known) {
    return (
      <Pill tone={known.tone} title={`台账取值 ${state}`}>
        {known.text}
      </Pill>
    );
  }
  // 未收录的取值：原样显示，不猜。
  return (
    <Pill tone="idle" title={`台账里的取值 ${state || '（空）'}，本界面未收录，故原样显示`}>
      {state || '（空）'}
    </Pill>
  );
}

function HeartbeatCell({ row }: { row: SessionRecord }): JSX.Element {
  if (!row.last_heartbeat) {
    return <span className="dim" title="台账里没有心跳时间">无心跳记录</span>;
  }
  const ms = parseExplicitTs(row.last_heartbeat);
  if (ms === null) {
    // 时间戳没带时区：只展示，不判定。
    return (
      <span title={`该时间戳未带时区，按原样显示：${row.last_heartbeat}`}>
        <TimeText value={row.last_heartbeat} />
        <span className="text-xs dim"> · 未带时区，不判定是否停滞</span>
      </span>
    );
  }
  const age = Date.now() - ms;
  const stale = row.state === 'alive' && age > HEARTBEAT_STALE_MS;
  return (
    <span title={`${row.last_heartbeat}（${HEARTBEAT_INTERVAL_NOTE}）`}>
      <span className="mono">{humanDuration(age)}前</span>
      {stale ? (
        <>
          {' '}
          <Chip variant="warn">心跳停滞</Chip>
        </>
      ) : null}
    </span>
  );
}

export function SessionsPage(): JSX.Element {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const taskFilter = params.get('task_id') ?? '';
  const [stateFilter, setStateFilter] = useState('');
  const systemStatus = useSystem((s) => s.status);

  const fetchSessions = useCallback((_signal: AbortSignal) => sessionApi.list(), []);
  const { data, loading, error, reload, loaded } = useAsync(fetchSessions, [], { pollMs: 5000 });

  const rows = useMemo(
    () =>
      asArray<unknown>(data)
        .map(normalizeSession)
        .filter((s): s is SessionRecord => s !== null),
    [data],
  );

  /** 状态下拉里的选项来自**实际观测到的取值**，不是前端硬编码的枚举。 */
  const stateOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const row of rows) seen.add(row.state);
    return Array.from(seen).sort();
  }, [rows]);

  const taskOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const row of rows) {
      if (row.owner_task_id) seen.add(row.owner_task_id);
    }
    return Array.from(seen).sort();
  }, [rows]);

  const filtered = useMemo(() => {
    const needle = taskFilter.trim().toLowerCase();
    return rows.filter((row) => {
      if (stateFilter && row.state !== stateFilter) return false;
      if (needle) {
        const owners = [row.owner_task_id, row.owner_stage_id, row.owner_attempt_id];
        if (!owners.some((v) => v !== null && v.toLowerCase().includes(needle))) return false;
      }
      return true;
    });
  }, [rows, stateFilter, taskFilter]);

  const alive = rows.filter((row) => row.state === 'alive').length;
  const lost = rows.filter((row) => row.state === 'lost').length;
  const orphan = rows.filter((row) => row.owner_task_id === null).length;
  const staleCount = rows.filter((row) => {
    const ms = parseExplicitTs(row.last_heartbeat);
    return row.state === 'alive' && ms !== null && Date.now() - ms > HEARTBEAT_STALE_MS;
  }).length;

  const setTaskFilter = (value: string): void => {
    const next = new URLSearchParams(params);
    if (value) next.set('task_id', value);
    else next.delete('task_id');
    setParams(next, { replace: true });
  };

  if (loading && !loaded) return <Loading label="读取会话台账" />;

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>会话</h1>
          <div className="page-head__sub">
            来自会话台账 <span className="mono">GET /api/sessions</span>
            （只读）。这里回答排障时最常问的问题：「这次任务还连着哪个 session、它还活着吗」。
            台账落在库里，因此内核重启后这些记录仍然可查。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn" onClick={reload}>
            刷新
          </button>
        </div>
      </div>

      {error ? (
        <Banner
          variant="danger"
          title={error.unreachable ? '无法连接内核' : '无法读取会话台账'}
          hint={error.hint}
          actions={
            <button type="button" className="btn btn--sm" onClick={reload}>
              重试
            </button>
          }
        >
          {error.detail}
          {error.kind === 'not_found' ? (
            <div className="text-xs" style={{ marginTop: 4 }}>
              内核还没有这个端点（<span className="mono">GET /api/sessions</span>
              ）。这里如实报「读不到」，不会显示成「没有会话」。
            </div>
          ) : null}
        </Banner>
      ) : null}

      {systemStatus && systemStatus.session_hosting === 'in_process' ? (
        <Banner variant="warn" title="会话托管：内核内进程（in_process）">
          harness 会话由内核自己持有——内核重启会打断在途会话，台账里标着「存活」的记录届时会变成失联。
          需要跨重启保活，请改用独立的 supervisor 进程重启内核。
        </Banner>
      ) : null}

      <div className="panel">
        <div className="panel__head">
          台账
          <span className="chip">共 {rows.length}</span>
          <span className="chip chip--accent">存活 {alive}</span>
          {lost > 0 ? <span className="chip chip--danger">已失联 {lost}</span> : null}
          {staleCount > 0 ? <span className="chip chip--warn">心跳停滞 {staleCount}</span> : null}
          {orphan > 0 ? <span className="chip">无归属任务 {orphan}</span> : null}
        </div>

        <div className="panel__hint">
          <div className="row row--tight" style={{ flexWrap: 'wrap' }}>
            <Field label="状态">
              <select
                className="input input--sm"
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value)}
              >
                <option value="">全部状态</option>
                {stateOptions.map((value) => (
                  <option key={value} value={value}>
                    {SESSION_STATE_LABELS[value]?.text ?? value}
                    {SESSION_STATE_LABELS[value] ? `（${value}）` : ''}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="按归属过滤（任务 / 阶段 / 尝试，支持前缀）">
              <input
                className="input input--sm"
                list="session-owner-tasks"
                value={taskFilter}
                placeholder="粘贴 task_id 或它的前几位"
                onChange={(e) => setTaskFilter(e.target.value)}
              />
              <datalist id="session-owner-tasks">
                {taskOptions.map((id) => (
                  <option key={id} value={id} />
                ))}
              </datalist>
            </Field>
            {stateFilter || taskFilter ? (
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => {
                  setStateFilter('');
                  setTaskFilter('');
                }}
              >
                清除筛选
              </button>
            ) : null}
            <span className="spacer" />
            <span className="text-xs dim">
              显示 {filtered.length} / {rows.length}
            </span>
          </div>
        </div>

        {error ? null : filtered.length === 0 ? (
          <Empty
            title={rows.length === 0 ? '台账里还没有会话记录' : '当前筛选下没有匹配的会话'}
            hint={
              rows.length === 0
                ? '任务开始执行、harness 创建会话之后，这里会出现记录。空台账不等于「没有任务在跑」——任务可能还在排队，或还没进入派发。'
                : '换个状态或归属再试。'
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table table--dense">
              <thead>
                <tr>
                  <th>会话</th>
                  <th>harness</th>
                  <th>归属</th>
                  <th>状态</th>
                  <th>最近心跳</th>
                  <th>创建</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr key={row.session_ref}>
                    <td className="mono text-xs" title={row.session_ref}>
                      <ShortId id={row.session_ref} len={12} />
                    </td>
                    <td className="mono text-xs" title={row.harness_id}>
                      {row.harness_id || <span className="dim">未标注</span>}
                    </td>
                    <td className="text-xs">
                      {row.owner_task_id ? (
                        <a
                          href={`#/tasks/${row.owner_task_id}`}
                          onClick={(e) => {
                            e.preventDefault();
                            navigate(`/tasks/${row.owner_task_id ?? ''}`);
                          }}
                        >
                          <ShortId id={row.owner_task_id} />
                        </a>
                      ) : (
                        <Chip variant="warn" title="台账里没有归属任务：可能是内核/监督进程启动前的残留，或是任务删除后未回收">
                          无归属任务
                        </Chip>
                      )}
                      <div className="dim mono" style={{ marginTop: 2 }}>
                        {row.owner_stage_id ? `阶段 ${row.owner_stage_id.slice(0, 8)}` : '阶段 —'}
                        {' · '}
                        {row.owner_attempt_id ? `尝试 ${row.owner_attempt_id.slice(0, 8)}` : '尝试 —'}
                      </div>
                    </td>
                    <td>
                      <SessionStatePill state={row.state} />
                    </td>
                    <td className="text-xs">
                      <HeartbeatCell row={row} />
                    </td>
                    <td className="text-xs">
                      <RelTime value={row.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="panel__hint">
          「已失联」是监督进程实测 <span className="mono">session_alive</span> 为假后写入的结论——
          它意味着该会话不能再复用；对应阶段会走重试或对账，不会假装还连着。
          心跳只说明「进程还在」，不说明「任务在推进」：进展请看执行图里的阶段状态与事件时间线。
        </div>
      </div>
    </div>
  );
}
