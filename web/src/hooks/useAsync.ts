/**
 * 一次异步读取的通用状态。
 *
 * 关键点：**错误要带类型地保留下来**，调用方需要区分
 * 「无法连接内核」（`error.unreachable`）与「内核说 404 / 422」。
 * 把错误压成 string 会让界面只能显示「加载失败」，那是没用的。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: ApiError | null;
  /** 首次加载尚未完成（用于区分「空列表」与「还没加载」）。 */
  loaded: boolean;
  reload: () => void;
  setData: (next: T | null) => void;
}

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  if (err instanceof DOMException && err.name === 'AbortError') {
    return new ApiError({ kind: 'http', status: 0, detail: '已取消' });
  }
  return new ApiError({
    kind: 'unreachable',
    status: 0,
    detail: err instanceof Error ? err.message : String(err),
  });
}

/**
 * @param fetcher 必须是稳定引用（用 useCallback 或模块级函数包一层），
 *                否则每次渲染都会重新请求。
 * @param deps    变化时重新请求。
 */
export function useAsync<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { enabled?: boolean; pollMs?: number } = {},
): AsyncState<T> {
  const enabled = options.enabled ?? true;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<ApiError | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [nonce, setNonce] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);

    fetcher(controller.signal)
      .then((result) => {
        if (cancelled || !mounted.current) return;
        setData(result);
        setError(null);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled || !mounted.current) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(toApiError(err));
        setLoaded(true);
      })
      .finally(() => {
        if (cancelled || !mounted.current) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  // 轮询兜底：WebSocket 断连时仍能看到推进（推送是加速器不是事实源）。
  useEffect(() => {
    if (!options.pollMs || !enabled) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), options.pollMs);
    return () => window.clearInterval(id);
  }, [options.pollMs, enabled]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, loading, error, loaded, reload, setData };
}

/** 表单提交用的「正在执行 + 最近一次错误」状态。 */
export interface SubmitState {
  busy: boolean;
  error: ApiError | null;
}

export function useSubmit(): {
  busy: boolean;
  error: ApiError | null;
  clear: () => void;
  run: <T>(fn: () => Promise<T>) => Promise<T | null>;
} {
  const [state, setState] = useState<SubmitState>({ busy: false, error: null });

  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setState({ busy: true, error: null });
    try {
      const result = await fn();
      setState({ busy: false, error: null });
      return result;
    } catch (err: unknown) {
      setState({ busy: false, error: toApiError(err) });
      return null;
    }
  }, []);

  const clear = useCallback(() => setState({ busy: false, error: null }), []);

  return { busy: state.busy, error: state.error, clear, run };
}
