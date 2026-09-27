/**
 * 内核连通性状态。
 *
 * 存在的理由很简单：**「连不上内核」与「没有数据」必须长得不一样。**
 * 内核没起来时页面要么显示连接失败横幅，要么给出重试入口——绝不能是一片空白，
 * 那会让人以为「任务跑完了但列表是空的」。
 */

import { create } from 'zustand';
import { ApiError, pingHealth, readToken, writeToken } from '../api/client';
import type { WsPush } from '../api/types';
import { KernelSocket, type WsStatus } from '../api/ws';

export type KernelState = 'unknown' | 'checking' | 'ok' | 'unreachable' | 'unauthorized';

interface ConnectionStore {
  kernel: KernelState;
  version: string | null;
  lastError: string | null;
  ws: WsStatus;
  wsDetail: string | null;
  /** 客户端已见过的最大 event_id：重连时用它补齐断档（REC-01）。 */
  watermark: number;
  /** 断连后补齐过的事件条数——告诉用户「你断连期间发生了什么」。 */
  resumedCount: number;
  lastPushAt: number | null;

  token: string;
  setToken: (token: string) => void;

  check: () => Promise<boolean>;
  bumpWatermark: (eventId: number) => void;
  noteResume: (count: number) => void;
}

let socket: KernelSocket | null = null;

/** 全局推送订阅者：各 store 注册自己的刷新逻辑，避免 socket 与 store 相互依赖。 */
type PushListener = (push: WsPush) => void;
const pushListeners = new Set<PushListener>();

export function onKernelPush(listener: PushListener): () => void {
  pushListeners.add(listener);
  return () => pushListeners.delete(listener);
}

/** 重连成功（含首次连上）后的订阅者：断连期间的变化必须靠 REST 重新拉，而不是等推送。 */
const reconnectListeners = new Set<() => void>();

export function onKernelReconnect(listener: () => void): () => void {
  reconnectListeners.add(listener);
  return () => reconnectListeners.delete(listener);
}

function fireReconnect(): void {
  for (const listener of reconnectListeners) {
    try {
      listener();
    } catch {
      /* 单个订阅者出错不影响其他订阅者 */
    }
  }
}

export const useConnection = create<ConnectionStore>((set, get) => ({
  kernel: 'unknown',
  version: null,
  lastError: null,
  ws: 'idle',
  wsDetail: null,
  watermark: 0,
  resumedCount: 0,
  lastPushAt: null,
  token: readToken(),

  setToken: (token: string) => {
    writeToken(token);
    set({ token, kernel: 'unknown' });
  },

  check: async () => {
    set({ kernel: 'checking' });
    try {
      const health = await pingHealth();
      set({ kernel: 'ok', version: health.version, lastError: null });
      ensureSocket();
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.kind === 'unauthorized') {
        set({ kernel: 'unauthorized', lastError: err.detail });
        return false;
      }
      // health 免鉴权：它失败只可能是「进程不在」或「地址不对」。
      const message = err instanceof ApiError ? err.detail : String(err);
      set({ kernel: 'unreachable', lastError: message });
      return false;
    }
  },

  bumpWatermark: (eventId: number) => {
    if (eventId > get().watermark) set({ watermark: eventId });
  },

  noteResume: (count: number) => set({ resumedCount: count }),
}));

function ensureSocket(): void {
  if (socket) return;
  socket = new KernelSocket({
    watermark: () => useConnection.getState().watermark,
    onStatus: (status, detail) => {
      const previous = useConnection.getState().ws;
      useConnection.setState({ ws: status, wsDetail: detail ?? null });
      // 每次进入 open（首次连上或重连成功）都让订阅者重新对账：
      // 断连期间审批、任务状态可能已经变了，徽标不能停在旧数字上。
      if (status === 'open' && previous !== 'open') fireReconnect();
    },
    onEvent: (eventId) => useConnection.getState().bumpWatermark(eventId),
    onResume: (info) => useConnection.getState().noteResume(info.returned),
    onPush: (push) => {
      useConnection.setState({ lastPushAt: Date.now() });
      for (const listener of pushListeners) {
        try {
          listener(push);
        } catch {
          /* 单个订阅者出错不影响其他订阅者 */
        }
      }
    },
  });
  socket.start();
}

/** 应用卸载 / 令牌更换时重连（令牌变了旧连接仍然带着旧令牌）。 */
export function reconnectSocket(): void {
  socket?.stop();
  socket = null;
  ensureSocket();
}
