/**
 * 基础助手（AI-01）聊天面板的状态。
 *
 * 与本项目其他 store 同一条纪律：**推送只是提醒，事实始终从 REST 拉。**
 * `assistant_message` 推送到达时重新拉线程与当前消息；重连成功后连配置
 * 一起对账——断连期间的问答不会丢，只是推送没送到。
 */

import { create } from 'zustand';
import { ApiError } from '../api/client';
import { assistant as assistantApi } from '../api/endpoints';
import type {
  AssistantCompactResult,
  AssistantConfig,
  AssistantConfigUpdate,
  AssistantMessage,
  AssistantThread,
} from '../api/types';
import { onKernelPush, onKernelReconnect } from './connection';

export interface AssistantFailure {
  detail: string;
  hint: string | null;
}

function asFailure(err: unknown, fallback: string): AssistantFailure {
  if (err instanceof ApiError) return { detail: err.detail, hint: err.hint };
  return { detail: fallback, hint: null };
}

interface AssistantStore {
  threads: AssistantThread[];
  threadsLoaded: boolean;
  loadError: AssistantFailure | null;

  activeThreadId: string | null;
  messages: AssistantMessage[];
  messagesLoaded: boolean;

  sending: boolean;
  /** 最近一次发送/整理的失败。错误体里的 hint 是后端给的引导文案，原样展示。 */
  sendError: AssistantFailure | null;
  /** 最近一次发送因窗口限制未带上的更早消息条数（截断不产生额外调用）。 */
  lastDropped: number;
  /**
   * 本次会话内记录的降级原因（message_id → 原因列表）。
   * 历史消息只有 degraded 标记，原因只随当次响应返回，重启页面后不再可知。
   */
  degradedReasons: Record<string, string[]>;

  compacting: boolean;

  config: AssistantConfig | null;
  configLoaded: boolean;

  refreshThreads: () => Promise<void>;
  selectThread: (threadId: string) => Promise<void>;
  newThread: () => Promise<void>;
  reloadMessages: () => Promise<void>;
  send: (content: string) => Promise<boolean>;
  compact: () => Promise<AssistantCompactResult | null>;
  refreshConfig: () => Promise<void>;
  /** 返回 null 表示成功；失败返回后端错误（含引导文案）由调用方展示。 */
  updateConfig: (body: AssistantConfigUpdate) => Promise<AssistantFailure | null>;
}

export const useAssistant = create<AssistantStore>((set, get) => ({
  threads: [],
  threadsLoaded: false,
  loadError: null,

  activeThreadId: null,
  messages: [],
  messagesLoaded: false,

  sending: false,
  sendError: null,
  lastDropped: 0,
  degradedReasons: {},

  compacting: false,

  config: null,
  configLoaded: false,

  refreshThreads: async () => {
    try {
      const data = await assistantApi.threads();
      const threads = data.threads ?? [];
      // 当前线程可能已被删除（或从未选择）：回退到最新的一条，没有就置空。
      const active = get().activeThreadId;
      const stillThere = active !== null && threads.some((t) => t.thread_id === active);
      const nextActive = stillThere ? active : (threads[0]?.thread_id ?? null);
      const switched = nextActive !== active;
      set({ threads, threadsLoaded: true, loadError: null, activeThreadId: nextActive });
      if (switched) set({ messages: [], messagesLoaded: false, lastDropped: 0 });
      await get().reloadMessages();
    } catch (err) {
      set({ threadsLoaded: true, loadError: asFailure(err, '读取对话列表失败。') });
    }
  },

  selectThread: async (threadId) => {
    if (get().activeThreadId === threadId) return;
    set({
      activeThreadId: threadId,
      messages: [],
      messagesLoaded: false,
      sendError: null,
      lastDropped: 0,
    });
    await get().reloadMessages();
  },

  newThread: async () => {
    try {
      const thread = await assistantApi.createThread();
      set({ sendError: null, lastDropped: 0 });
      await get().refreshThreads();
      // refreshThreads 会把新线程顶到最前并选中；此处兜底确保选中它。
      await get().selectThread(thread.thread_id);
    } catch (err) {
      set({ sendError: asFailure(err, '新建对话失败。') });
    }
  },

  reloadMessages: async () => {
    const threadId = get().activeThreadId;
    if (!threadId) {
      set({ messages: [], messagesLoaded: true });
      return;
    }
    try {
      const data = await assistantApi.messages(threadId);
      // 拉取期间用户切了线程：结果属于旧线程，丢弃。
      if (get().activeThreadId !== threadId) return;
      set({ messages: data.messages ?? [], messagesLoaded: true, loadError: null });
    } catch (err) {
      if (get().activeThreadId !== threadId) return;
      set({ messagesLoaded: true, loadError: asFailure(err, '读取对话历史失败。') });
    }
  },

  send: async (content) => {
    const text = content.trim();
    if (!text || get().sending) return false;
    set({ sending: true, sendError: null });
    try {
      let threadId = get().activeThreadId;
      if (!threadId) {
        // 第一条消息顺手建线程，不要求用户先点「新对话」。
        const thread = await assistantApi.createThread();
        threadId = thread.thread_id;
        set({ activeThreadId: threadId, messages: [], messagesLoaded: true, lastDropped: 0 });
      }
      const result = await assistantApi.send(threadId, text);
      const reasons = { ...get().degradedReasons };
      if (result.degraded && result.degraded_reasons.length > 0) {
        reasons[result.message.message_id] = result.degraded_reasons;
      }
      set({ sending: false, lastDropped: result.dropped, degradedReasons: reasons });
      // 回复已在响应里，但线程排序与消息列表仍以服务端为准重拉一次。
      await get().refreshThreads();
      return true;
    } catch (err) {
      set({ sending: false, sendError: asFailure(err, '发送失败。') });
      return false;
    }
  },

  compact: async () => {
    const threadId = get().activeThreadId;
    if (!threadId || get().compacting) return null;
    set({ compacting: true, sendError: null });
    try {
      const result = await assistantApi.compact(threadId);
      set({ compacting: false, lastDropped: 0 });
      await get().reloadMessages();
      return result;
    } catch (err) {
      set({ compacting: false, sendError: asFailure(err, '整理前文失败。') });
      return null;
    }
  },

  refreshConfig: async () => {
    try {
      const config = await assistantApi.config();
      set({ config, configLoaded: true });
    } catch (err) {
      set({ configLoaded: true, loadError: asFailure(err, '读取助手配置失败。') });
    }
  },

  updateConfig: async (body) => {
    try {
      const config = await assistantApi.updateConfig(body);
      set({ config, configLoaded: true, loadError: null });
      return null;
    } catch (err) {
      return asFailure(err, '保存助手配置失败。');
    }
  },
}));

let wired = false;

/** 订阅推送：新回复到达时重拉当前线程；重连成功后连配置一起对账。 */
export function wireAssistant(): void {
  if (wired) return;
  wired = true;
  onKernelPush((push) => {
    if (push.kind === 'assistant_message') void useAssistant.getState().refreshThreads();
  });
  // 断连期间的问答不会有推送补给我们（推送是尽力而为的），重连后必须重拉一次。
  onKernelReconnect(() => {
    void useAssistant.getState().refreshConfig();
    void useAssistant.getState().refreshThreads();
  });
  void useAssistant.getState().refreshConfig();
  void useAssistant.getState().refreshThreads();
}
