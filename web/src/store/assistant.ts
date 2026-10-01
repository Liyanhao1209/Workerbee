/**
 * 基础助手（AI-01）聊天面板的状态。
 *
 * 与本项目其他 store 同一条纪律：**推送只是提醒，事实始终从 REST 拉。**
 * 唯一的例外是 `assistant_chunk`：它是逐字增量，REST 里没有对应物，
 * 只累积到「正在生成」的临时气泡；最终 `assistant_message` 推送到达时
 * 仍以重拉消息列表对账。重连成功后连配置一起对账——断连期间的问答不会丢。
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

/** 正在生成中的助手回复：chunk 推送逐条累积，text 与 reasoning 分开。 */
export interface AssistantStreaming {
  /** 关联 id = 后端预生成的回复 message_id；assistant_message 对账时用它确认。 */
  messageId: string;
  threadId: string;
  text: string;
  reasoning: string;
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
  /** 正在生成中的回复（流式累积）。null = 没有正在生成的回复。 */
  streaming: AssistantStreaming | null;
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
  renameThread: (title: string) => Promise<boolean>;
  reloadMessages: () => Promise<void>;
  send: (content: string) => Promise<boolean>;
  compact: () => Promise<AssistantCompactResult | null>;
  /** 采用/拒绝草稿提案；返回 null 表示成功，失败返回后端错误由卡片展示。 */
  adoptDraft: (draftId: string) => Promise<AssistantFailure | null>;
  rejectDraft: (draftId: string) => Promise<AssistantFailure | null>;
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
  streaming: null,
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
      streaming: null, // 生成中的增量属于旧线程，切走即丢弃（那边完成后靠重拉对账）
    });
    await get().reloadMessages();
  },

  newThread: async () => {
    try {
      const thread = await assistantApi.createThread();
      set({ sendError: null, lastDropped: 0, streaming: null });
      await get().refreshThreads();
      // refreshThreads 会把新线程顶到最前并选中；此处兜底确保选中它。
      await get().selectThread(thread.thread_id);
    } catch (err) {
      set({ sendError: asFailure(err, '新建对话失败。') });
    }
  },

  renameThread: async (title) => {
    const threadId = get().activeThreadId;
    if (!threadId) return false;
    try {
      await assistantApi.renameThread(threadId, title);
      set({ sendError: null });
      await get().refreshThreads();
      return true;
    } catch (err) {
      set({ sendError: asFailure(err, '重命名对话失败。') });
      return false;
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
      set({ sending: false, streaming: null, lastDropped: result.dropped, degradedReasons: reasons });
      // 回复已在响应里，但线程排序与消息列表仍以服务端为准重拉一次。
      await get().refreshThreads();
      return true;
    } catch (err) {
      set({ sending: false, streaming: null, sendError: asFailure(err, '发送失败。') });
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

  adoptDraft: async (draftId) => {
    try {
      await assistantApi.adoptDraft(draftId);
      // 卡片状态以服务端为准重拉，不在本地乐观改写。
      await get().reloadMessages();
      return null;
    } catch (err) {
      return asFailure(err, '采用提案失败。');
    }
  },

  rejectDraft: async (draftId) => {
    try {
      await assistantApi.rejectDraft(draftId);
      await get().reloadMessages();
      return null;
    } catch (err) {
      return asFailure(err, '拒绝提案失败。');
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

/** 订阅推送：chunk 逐条累积到「正在生成」气泡；最终 assistant_message 到达时
 * 重拉对账；重连成功后连配置一起对账。 */
export function wireAssistant(): void {
  if (wired) return;
  wired = true;
  onKernelPush((push) => {
    const state = useAssistant.getState();
    if (push.kind === 'assistant_chunk') {
      // 只收「当前线程且正在生成」的增量：其它线程的 chunk 丢弃，
      // 那边的回复完成后由 assistant_message 触发重拉，不会丢内容。
      const threadId = typeof push.payload['thread_id'] === 'string' ? push.payload['thread_id'] : null;
      const messageId = typeof push.payload['message_id'] === 'string' ? push.payload['message_id'] : null;
      const kind = push.payload['kind'];
      const text = typeof push.payload['text'] === 'string' ? push.payload['text'] : '';
      if (!state.sending || !threadId || threadId !== state.activeThreadId || !messageId || !text)
        return;
      const prev = state.streaming && state.streaming.messageId === messageId ? state.streaming : null;
      const next: AssistantStreaming = {
        messageId,
        threadId,
        text: (prev?.text ?? '') + (kind === 'text' ? text : ''),
        reasoning: (prev?.reasoning ?? '') + (kind === 'reasoning' ? text : ''),
      };
      useAssistant.setState({ streaming: next });
      return;
    }
    if (push.kind === 'assistant_message') {
      // 对账通知：若它确认的就是正在累积的那条，清掉临时气泡，以 REST 重拉为准。
      const messageId = typeof push.payload['message_id'] === 'string' ? push.payload['message_id'] : null;
      if (state.streaming && state.streaming.messageId === messageId) {
        useAssistant.setState({ streaming: null });
      }
      void state.refreshThreads();
    }
  });
  // 断连期间的问答不会有推送补给我们（推送是尽力而为的），重连后必须重拉一次。
  onKernelReconnect(() => {
    void useAssistant.getState().refreshConfig();
    void useAssistant.getState().refreshThreads();
  });
  void useAssistant.getState().refreshConfig();
  void useAssistant.getState().refreshThreads();
}
