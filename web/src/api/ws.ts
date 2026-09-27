/**
 * 事件推送通道（REC-01、OBS-05）。
 *
 * 三条纪律：
 * 1. **推送是加速器，不是事实源。** 任何一次推送到达后，页面仍然从 REST 拉真实状态；
 *    断连期间发生的事由 `resume` 补齐，而不是靠「我猜它没变」。
 * 2. **重连必须补齐水位。** 重连后发 `{"type":"resume","after_event_id":N}`，
 *    N 是客户端见过的最大 event_id。服务端据此回放断连期间的事件（AC-12）。
 * 3. **令牌只走查询参数**——浏览器的 WebSocket API 不能自定义请求头（见后端 auth.py）。
 */

import { readToken } from './client';
import type { WsPush } from './types';
import { isRecord, asString } from './guards';

export type WsStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface WsHandlers {
  onPush: (push: WsPush) => void;
  onStatus: (status: WsStatus, detail?: string) => void;
  /** 重连成功后要补齐的水位（客户端已见过的最大 event_id）。 */
  watermark: () => number;
}

const MAX_BACKOFF_MS = 15_000;
const BASE_BACKOFF_MS = 800;

export class KernelSocket {
  private ws: WebSocket | null = null;
  private readonly handlers: WsHandlers;
  private attempt = 0;
  private timer: number | null = null;
  private stopped = false;

  constructor(handlers: WsHandlers) {
    this.handlers = handlers;
  }

  private url(): string {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = readToken();
    const qs = token ? `?token=${encodeURIComponent(token)}` : '';
    return `${proto}//${window.location.host}/api/ws${qs}`;
  }

  start(): void {
    this.stopped = false;
    this.open(false);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.ws) {
      // 主动关闭：把 onclose 里的重连逻辑摘掉，避免关页面时还在排重连。
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.handlers.onStatus('closed');
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.attempt += 1;
    const delay = Math.min(BASE_BACKOFF_MS * 2 ** (this.attempt - 1), MAX_BACKOFF_MS);
    this.handlers.onStatus('reconnecting', `第 ${this.attempt} 次重连，${Math.round(delay / 1000)} 秒后`);
    this.timer = window.setTimeout(() => this.open(true), delay);
  }

  private open(isReconnect: boolean): void {
    if (this.stopped) return;
    this.handlers.onStatus(isReconnect ? 'reconnecting' : 'connecting');
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url());
    } catch (err) {
      this.handlers.onStatus('closed', err instanceof Error ? err.message : String(err));
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.attempt = 0;
      this.handlers.onStatus('open');
      if (isReconnect) {
        const after = this.handlers.watermark();
        // 补齐断连期间的事件（AC-12）。水位为 0 表示本地没有见过任何事件，
        // 此时也要发一次，让服务端决定回放多少。
        try {
          ws.send(JSON.stringify({ type: 'resume', after_event_id: after }));
        } catch {
          /* 发送失败会在 onclose 里走重连路径 */
        }
      }
    };

    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== 'string') return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data) as unknown;
      } catch {
        return;
      }
      if (!isRecord(parsed)) return;
      const kind = asString(parsed['kind']);
      if (kind !== 'state_changed' && kind !== 'attention') return;
      this.handlers.onPush({
        kind,
        task_id: typeof parsed['task_id'] === 'string' ? parsed['task_id'] : null,
        stage_id: typeof parsed['stage_id'] === 'string' ? parsed['stage_id'] : null,
        payload: isRecord(parsed['payload']) ? parsed['payload'] : {},
      });
    };

    ws.onerror = () => {
      // onerror 之后必然有 onclose；这里不重复调度重连。
    };

    ws.onclose = () => {
      this.ws = null;
      if (this.stopped) return;
      this.scheduleReconnect();
    };
  }
}
