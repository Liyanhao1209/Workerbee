/**
 * 应用外壳：顶栏 + 「需处理」徽标 + 内核连通性 + 部署形态 + 降级声明。
 *
 * 三件必须显眼、不许折叠的事：
 * 1. 内核不可达时挂持续横幅，**不渲染空白页**（REC-01：服务端离线时显示不可达，不编造实时状态）。
 * 2. 部署形态（`session_hosting`）：`in_process` 意味着内核重启会打断在途任务，
 *    用户必须知道，否则会默认「重启不丢任务」。
 * 3. 降级声明（`startup_notes`）：supervisor 不可达、摘要器退化这类事实要如实呈现，
 *    不做成一个小图标——用户有权知道当前跑在降级模式下。
 */

import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useConnection } from '../store/connection';
import { attentionCount, useAttention, wireAttention } from '../store/attention';
import { wireAssistant } from '../store/assistant';
import { hostingDescription, useSystem, wireSystem } from '../store/system';
import { AssistantPanel } from './AssistantPanel';
import { AttentionDrawer } from './AttentionDrawer';
import { Banner } from './common';

const NAV = [
  { to: '/workflows', label: '流程' },
  { to: '/tasks', label: '任务' },
  { to: '/execution', label: '执行图' },
  { to: '/sessions', label: '会话' },
  { to: '/registry', label: '注册表' },
  { to: '/templates', label: '模板' },
  { to: '/storage', label: '存储' },
];

export function AppShell(): JSX.Element {
  const kernel = useConnection((s) => s.kernel);
  const version = useConnection((s) => s.version);
  const lastError = useConnection((s) => s.lastError);
  const ws = useConnection((s) => s.ws);
  const wsDetail = useConnection((s) => s.wsDetail);
  const resumedCount = useConnection((s) => s.resumedCount);
  const check = useConnection((s) => s.check);
  const attention = useAttention();
  const systemStatus = useSystem((s) => s.status);
  const refreshSystem = useSystem((s) => s.refresh);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(true);
  const location = useLocation();

  useEffect(() => {
    wireAttention();
    wireAssistant();
    wireSystem();
    void check();
    const timer = window.setInterval(() => {
      if (useConnection.getState().kernel !== 'ok') void check();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [check]);

  // 断连期间出现的审批要在重连后找回（AC-12）：推送通道一重新打开就拉一次。
  useEffect(() => {
    if (ws === 'open') {
      void attention.refresh();
      void refreshSystem();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  const count = attentionCount(attention);
  const hosting = hostingDescription(systemStatus);

  // 两处的降级声明可能同源但也可能不同（status 读的是启动期快照，attention 是实时的）；
  // 合并去重后统一呈现，避免同一条话术出现两遍。
  const notes = Array.from(new Set([...(systemStatus?.startup_notes ?? []), ...attention.startupNotes]));
  const schedulerOff = systemStatus !== null && !systemStatus.scheduler.enabled;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar__brand">
          Workerbee
          <span className="topbar__brand-sub">多 agent 流程编排</span>
        </div>
        <nav className="topbar__nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? 'navlink navlink--active' : 'navlink')}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar__right">
          {systemStatus ? (
            <span
              className={hosting.variant === 'ok' ? 'pill pill--success' : 'pill pill--warn'}
              title={hosting.detail}
            >
              <span className="pill__dot" />
              {hosting.label}
            </span>
          ) : null}
          <KernelIndicator
            kernel={kernel}
            version={version}
            ws={ws}
            wsDetail={wsDetail}
            resumedCount={resumedCount}
          />
          <button
            type="button"
            className={count > 0 ? 'btn btn--sm attention-btn' : 'btn btn--sm'}
            onClick={() => setDrawerOpen((v) => !v)}
            title="审批、失败任务、状态不明与未清理资源"
          >
            需处理
            {count > 0 ? <span className="badge">{count > 99 ? '99+' : count}</span> : null}
          </button>
        </div>
      </header>

      <div style={{ padding: 'var(--sp-3) var(--sp-4) 0' }}>
        {kernel === 'unreachable' ? (
          <Banner
            variant="danger"
            title="无法连接内核"
            hint={
              <>
                网页会自动重试。请确认本机的 <span className="mono">workerbee-core</span> 服务已启动
                （默认监听 127.0.0.1:8765）。服务恢复前，页面显示的不是实时状态。
              </>
            }
            actions={
              <button type="button" className="btn btn--sm" onClick={() => void check()}>
                立即重试
              </button>
            }
          >
            {lastError ? <span className="mono text-xs">{lastError}</span> : null}
          </Banner>
        ) : null}

        {/* 部署形态：只在有风险时展开成横幅。supervisor 正常时不占版面（顶栏已有一枚徽标）。 */}
        {systemStatus && hosting.variant === 'warn' ? (
          <Banner variant="warn" title={hosting.label}>
            {hosting.detail}
          </Banner>
        ) : null}

        {schedulerOff ? (
          <Banner variant="warn" title="任务不会自动推进">
            内核的调度器没有启动，排队和就绪的阶段会一直停在原地。请检查内核的启动配置并重启内核。
          </Banner>
        ) : null}

        {notes.length > 0 ? (
          <Banner
            variant="warn"
            title={`运行提示 · ${notes.length} 项`}
            actions={
              <button type="button" className="btn btn--sm" onClick={() => setNotesOpen((v) => !v)}>
                {notesOpen ? '收起' : '展开'}
              </button>
            }
          >
            {notesOpen ? (
              <ul className="list-reset">
                {notes.map((note, i) => (
                  <li key={i}>· {note}</li>
                ))}
              </ul>
            ) : (
              <div className="text-xs">{notes[0]}</div>
            )}
          </Banner>
        ) : null}
      </div>

      <main className="main">
        <Outlet />
      </main>

      {drawerOpen ? <AttentionDrawer onClose={() => setDrawerOpen(false)} /> : null}
      {assistantOpen ? <AssistantPanel onClose={() => setAssistantOpen(false)} /> : null}
      {/* 助手入口：右下角圆形悬浮按钮。面板打开时隐藏——面板头部自带「收起」。 */}
      {assistantOpen ? null : (
        <button
          type="button"
          className="assistant-fab"
          onClick={() => setAssistantOpen(true)}
          title="向内置助手提问：怎么用、现在什么状态、报错是什么意思"
          aria-label="打开助手"
        >
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M4 13a8 8 0 0 1 16 0" />
            <rect x="3" y="13" width="4" height="6" rx="1.5" />
            <rect x="17" y="13" width="4" height="6" rx="1.5" />
            <path d="M21 19a2 2 0 0 1-2 2h-4" />
          </svg>
        </button>
      )}
    </div>
  );
}

function KernelIndicator({
  kernel,
  version,
  ws,
  wsDetail,
  resumedCount,
}: {
  kernel: string;
  version: string | null;
  ws: string;
  wsDetail: string | null;
  /** 上一次重连按 after_event_id 补齐的事件条数（0 表示没有断档）。 */
  resumedCount: number;
}): JSX.Element {
  const kernelText =
    kernel === 'ok' ? '内核在线' : kernel === 'unreachable' ? '内核离线' : kernel === 'unauthorized' ? '需要访问令牌' : '检测中';
  const kernelCls =
    kernel === 'ok' ? 'pill pill--success' : kernel === 'unreachable' ? 'pill pill--danger' : 'pill pill--pending';

  // 推送通道与内核本身分开显示：内核在线但推送断了，页面照样能用（靠轮询兜底），
  // 但用户有权知道「你看到的可能不是实时的」。
  const wsText =
    ws === 'open'
      ? '推送已连接'
      : ws === 'connecting'
        ? '推送连接中'
        : ws === 'reconnecting'
          ? '推送重连中'
          : ws === 'closed'
            ? '推送已断开'
            : '推送未启动';
  const wsCls = ws === 'open' ? 'pill pill--success' : ws === 'reconnecting' ? 'pill pill--warn' : 'pill pill--idle';

  return (
    <div className="row row--tight">
      <span className={kernelCls} title={version ? `内核版本 ${version}` : undefined}>
        {kernelText}
      </span>
      <span
        className={wsCls}
        title={
          resumedCount > 0
            ? `${wsDetail ? `${wsDetail}\n` : ''}连接中断期间错过的 ${resumedCount} 条更新已在重连后补齐；页面数据以刷新时从内核拉取的为准。`
            : (wsDetail ?? undefined)
        }
      >
        {wsText}
        {resumedCount > 0 ? <span className="dim"> · 补齐 {resumedCount}</span> : null}
      </span>
    </div>
  );
}
