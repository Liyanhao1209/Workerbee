/**
 * 首次访问的令牌输入（UI-02）。
 *
 * 内核默认绑 loopback + 访问令牌；令牌由内核启动横幅打印。
 * 流程：health 免鉴权先探活 → 再试一次带鉴权的请求 → 401 才要令牌。
 * 内核本身不可达时**不拦人**：让外壳去显示「无法连接内核」，
 * 否则用户会对着一个「请输入令牌」的框排查半天网络问题。
 */

import { useCallback, useEffect, useState } from 'react';
import { useConnection } from '../store/connection';
import { system } from '../api/endpoints';
import { ApiError } from '../api/client';
import { Banner, Field } from './common';

type GateState = 'checking' | 'open' | 'need-token' | 'offline';

export function TokenGate({ children }: { children: React.ReactNode }): JSX.Element {
  const setToken = useConnection((s) => s.setToken);
  const storedToken = useConnection((s) => s.token);
  const [state, setState] = useState<GateState>('checking');
  const [draft, setDraft] = useState(storedToken);
  const [error, setError] = useState<string | null>(null);

  const probe = useCallback(async (): Promise<void> => {
    setState('checking');
    try {
      await system.status();
      setState('open');
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.kind === 'unauthorized') {
          setState('need-token');
          setError(storedToken ? '当前令牌无效或已轮换。' : null);
          return;
        }
        // 连接层面失败：交给外壳的横幅，不在这里拦。
        setState('offline');
        return;
      }
      setState('offline');
    }
  }, [storedToken]);

  useEffect(() => {
    void probe();
  }, [probe]);

  if (state === 'open' || state === 'offline') {
    return <>{children}</>;
  }

  if (state === 'checking') {
    return (
      <div className="page" style={{ maxWidth: 460, marginTop: '12vh' }}>
        <div className="panel">
          <div className="panel__body row">
            <span className="spin" />
            <span>正在连接内核…</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page" style={{ maxWidth: 520, marginTop: '10vh' }}>
      <div className="panel">
        <div className="panel__head">访问令牌</div>
        <div className="panel__body col">
          <p className="muted text-sm" style={{ margin: 0 }}>
            内核要求访问令牌（默认绑 loopback，不把控制能力开放给任意网络来访者）。
            令牌在内核启动时打印在启动横幅里，也可以由部署方另行配置。
          </p>

          {error ? (
            <Banner variant="warn" title="令牌未通过校验">
              {error}
            </Banner>
          ) : null}

          <Field label="X-Workerbee-Token" hint="仅保存在本机浏览器 localStorage，不写入日志或页面其它位置。">
            <input
              className="input input--mono"
              type="password"
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setToken(draft.trim());
                  void probe();
                }
              }}
              placeholder="粘贴内核启动时打印的令牌"
            />
          </Field>

          <div className="row">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!draft.trim()}
              onClick={() => {
                setToken(draft.trim());
                void probe();
              }}
            >
              保存并连接
            </button>
            <button type="button" className="btn" onClick={() => void probe()}>
              重新检测
            </button>
          </div>

          <div className="text-xs dim">
            令牌为空时内核可能未启用鉴权（仅当显式绑定了非 loopback 地址才应如此）。
            此时直接点「重新检测」即可。
          </div>
        </div>
      </div>
    </div>
  );
}
