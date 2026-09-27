/**
 * 应用外壳：顶栏 + 「需处理」徽标 + 内核连通性指示。
 *
 * 内核不可达时**不渲染空白页**，而是在内容区顶部挂一条持续的横幅，
 * 并保留「重试」入口。所有子页面照常挂载，它们各自的错误态会显示细节。
 */

import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useConnection } from '../store/connection';
import { attentionCount, useAttention, wireAttention } from '../store/attention';
import { AttentionDrawer } from './AttentionDrawer';
import { Banner } from './common';

const NAV = [
  { to: '/workflows', label: '流程' },
  { to: '/tasks', label: '任务' },
  { to: '/execution', label: '执行图' },
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
  const check = useConnection((s) => s.check);
  const attention = useAttention();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    wireAttention();
    void check();
    const timer = window.setInterval(() => {
      if (useConnection.getState().kernel !== 'ok') void check();
    }, 10_000);
    return () => window.clearInterval(timer);
    // check 是 zustand 上的稳定引用
  }, [check]);

  // 断连期间出现的审批要在重连后找回（AC-12）：WS 一旦重新打开就拉一次。
  useEffect(() => {
    if (ws === 'open') void attention.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  const count = attentionCount(attention);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar__brand">
          Workerbee
          <span className="topbar__brand-sub">多 agent harness 协作</span>
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
          <KernelIndicator kernel={kernel} version={version} ws={ws} wsDetail={wsDetail} />
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

      {kernel === 'unreachable' ? (
        <div style={{ padding: 'var(--sp-3) var(--sp-4) 0' }}>
          <Banner
            variant="danger"
            title="无法连接内核"
            hint={
              <>
                网页会继续重试。请确认 <span className="mono">workerbee-core</span> 已启动并监听
                127.0.0.1:8787；开发模式下由 Vite 代理转发（可改 WORKERBEE_KERNEL 环境变量）。
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
        </div>
      ) : null}

      <main className="main">
        <Outlet />
      </main>

      {drawerOpen ? <AttentionDrawer onClose={() => setDrawerOpen(false)} /> : null}
    </div>
  );
}

function KernelIndicator({
  kernel,
  version,
  ws,
  wsDetail,
}: {
  kernel: string;
  version: string | null;
  ws: string;
  wsDetail: string | null;
}): JSX.Element {
  const kernelText =
    kernel === 'ok' ? '内核在线' : kernel === 'unreachable' ? '内核离线' : kernel === 'unauthorized' ? '未授权' : '检测中';
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
      <span className={wsCls} title={wsDetail ?? undefined}>
        {wsText}
      </span>
    </div>
  );
}
