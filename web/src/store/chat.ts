/**
 * Web Chat（对话页）的状态（v0.03 §5）。
 *
 * 与 assistant store 同一条纪律：**推送只是提醒，事实始终从 REST 拉。**
 * 唯一的例外是 `chat_chunk`：逐字增量只累积到「正在生成」的临时气泡，
 * 最终 `chat_message` 推送到达时仍以重拉消息列表对账。
 * `chat_status` 承载工具循环的状态（等待审批 / 审批结果），是横幅而非消息。
 */

import { create } from 'zustand';
import { ApiError } from '../api/client';
import { chat as chatApi } from '../api/endpoints';
import type { ChatNode, ChatSession } from '../api/types';
import { onKernelPush, onKernelReconnect } from './connection';

export interface ChatFailure {
  detail: string;
  hint: string | null;
}

/** 正在生成中的回复：chunk 推送逐条累积，text 与 reasoning 分开。 */
export interface ChatStreaming {
  /** 关联 id = 后端预生成的回复节点 id；chat_message 对账时用它确认。 */
  nodeId: string;
  sessionId: string;
  text: string;
  reasoning: string;
}

/** 工具循环的状态横幅：等待审批时告诉用户去哪点批准。 */
export interface ChatStatus {
  status: string;
  detail: string;
  approvalId: string | null;
}

function asFailure(err: unknown, fallback: string): ChatFailure {
  if (err instanceof ApiError) return { detail: err.detail, hint: err.hint };
  return { detail: fallback, hint: null };
}

interface ChatStoreState {
  sessions: ChatSession[];
  sessionsLoaded: boolean;
  loadError: ChatFailure | null;

  activeSessionId: string | null;
  messages: ChatNode[];
  leafId: string | null;
  messagesLoaded: boolean;

  sending: boolean;
  streaming: ChatStreaming | null;
  /** 最近一次发送的失败。错误体里的 hint 是后端给的引导文案，原样展示。 */
  sendError: ChatFailure | null;
  /** 最近一次发送因窗口限制未带上的更早节点数。 */
  lastDropped: number;
  /** 本次会话内记录的降级原因（node_id → 原因列表）。 */
  degradedReasons: Record<string, string[]>;
  /** 当前后端是否支持工具（最近一次发送的结论；null = 还没发过）。 */
  supportsTools: boolean | null;
  /** 工具循环状态：waiting_approval 时展示横幅，approval_decided 后清除。 */
  pendingStatus: ChatStatus | null;

  refreshSessions: (workspaceId?: string) => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  newSession: (workspaceId?: string) => Promise<void>;
  renameSession: (title: string) => Promise<boolean>;
  deleteSession: () => Promise<boolean>;
  reloadMessages: () => Promise<void>;
  send: (content: string, refs?: string[]) => Promise<boolean>;
}

export const useChat = create<ChatStoreState>((set, get) => ({
  sessions: [],
  sessionsLoaded: false,
  loadError: null,

  activeSessionId: null,
  messages: [],
  leafId: null,
  messagesLoaded: false,

  sending: false,
  streaming: null,
  sendError: null,
  lastDropped: 0,
  degradedReasons: {},
  supportsTools: null,
  pendingStatus: null,

  refreshSessions: async (workspaceId) => {
    try {
      const data = await chatApi.sessions(workspaceId);
      const sessions = data.sessions ?? [];
      const active = get().activeSessionId;
      const stillThere = active !== null && sessions.some((s) => s.session_id === active);
      const nextActive = stillThere ? active : (sessions[0]?.session_id ?? null);
      const switched = nextActive !== active;
      set({ sessions, sessionsLoaded: true, loadError: null, activeSessionId: nextActive });
      if (switched) set({ messages: [], leafId: null, messagesLoaded: false, lastDropped: 0 });
      await get().reloadMessages();
    } catch (err) {
      set({ sessionsLoaded: true, loadError: asFailure(err, '读取会话列表失败。') });
    }
  },

  selectSession: async (sessionId) => {
    if (get().activeSessionId === sessionId) return;
    set({
      activeSessionId: sessionId,
      messages: [],
      leafId: null,
      messagesLoaded: false,
      sendError: null,
      lastDropped: 0,
      streaming: null, // 生成中的增量属于旧会话，切走即丢弃（完成后靠重拉对账）
      pendingStatus: null,
    });
    await get().reloadMessages();
  },

  newSession: async (workspaceId) => {
    try {
      const session = await chatApi.createSession(workspaceId ? { workspace_id: workspaceId } : {});
      set({ sendError: null, lastDropped: 0, streaming: null, pendingStatus: null });
      await get().refreshSessions(workspaceId);
      await get().selectSession(session.session_id);
    } catch (err) {
      set({ sendError: asFailure(err, '新建会话失败。') });
    }
  },

  renameSession: async (title) => {
    const sessionId = get().activeSessionId;
    if (!sessionId) return false;
    try {
      await chatApi.renameSession(sessionId, title);
      set({ sendError: null });
      await get().refreshSessions();
      return true;
    } catch (err) {
      set({ sendError: asFailure(err, '重命名会话失败。') });
      return false;
    }
  },

  deleteSession: async () => {
    const sessionId = get().activeSessionId;
    if (!sessionId) return false;
    try {
      await chatApi.deleteSession(sessionId);
      set({ activeSessionId: null, messages: [], leafId: null, messagesLoaded: false });
      await get().refreshSessions();
      return true;
    } catch (err) {
      set({ sendError: asFailure(err, '删除会话失败。') });
      return false;
    }
  },

  reloadMessages: async () => {
    const sessionId = get().activeSessionId;
    if (!sessionId) {
      set({ messages: [], leafId: null, messagesLoaded: true });
      return;
    }
    try {
      const data = await chatApi.messages(sessionId);
      // 拉取期间用户切了会话：结果属于旧会话，丢弃。
      if (get().activeSessionId !== sessionId) return;
      set({
        messages: data.messages ?? [],
        leafId: data.leaf_id,
        messagesLoaded: true,
        loadError: null,
      });
    } catch (err) {
      if (get().activeSessionId !== sessionId) return;
      set({ messagesLoaded: true, loadError: asFailure(err, '读取对话历史失败。') });
    }
  },

  send: async (content, refs = []) => {
    const text = content.trim();
    if (!text || get().sending) return false;
    set({ sending: true, sendError: null });
    try {
      let sessionId = get().activeSessionId;
      if (!sessionId) {
        // 第一条消息顺手建会话，不要求用户先点「新会话」。
        const session = await chatApi.createSession({});
        sessionId = session.session_id;
        set({ activeSessionId: sessionId, messages: [], leafId: null, messagesLoaded: true, lastDropped: 0 });
      }
      const result = await chatApi.send(sessionId, { content: text, refs });
      const reasons = { ...get().degradedReasons };
      if (result.degraded && result.degraded_reasons.length > 0) {
        reasons[result.reply.node_id] = result.degraded_reasons;
      }
      set({
        sending: false,
        streaming: null,
        pendingStatus: null,
        lastDropped: result.dropped,
        degradedReasons: reasons,
        supportsTools: result.supports_tools,
      });
      // 回复已在响应里，但会话排序与消息列表仍以服务端为准重拉一次。
      await get().refreshSessions();
      return true;
    } catch (err) {
      set({ sending: false, streaming: null, pendingStatus: null, sendError: asFailure(err, '发送失败。') });
      return false;
    }
  },
}));

let wired = false;

/** 订阅推送：chunk 累积到「正在生成」气泡；chat_message 到达时重拉对账；
 * chat_status 驱动「等待审批」横幅。重连成功后重拉会话与消息。 */
export function wireChat(): void {
  if (wired) return;
  wired = true;
  onKernelPush((push) => {
    const state = useChat.getState();
    if (push.kind === 'chat_chunk') {
      const sessionId = typeof push.payload['session_id'] === 'string' ? push.payload['session_id'] : null;
      const nodeId = typeof push.payload['node_id'] === 'string' ? push.payload['node_id'] : null;
      const kind = push.payload['kind'];
      const text = typeof push.payload['text'] === 'string' ? push.payload['text'] : '';
      if (!state.sending || !sessionId || sessionId !== state.activeSessionId || !nodeId || !text)
        return;
      const prev = state.streaming && state.streaming.nodeId === nodeId ? state.streaming : null;
      const next: ChatStreaming = {
        nodeId,
        sessionId,
        text: (prev?.text ?? '') + (kind === 'text' ? text : ''),
        reasoning: (prev?.reasoning ?? '') + (kind === 'reasoning' ? text : ''),
      };
      useChat.setState({ streaming: next });
      return;
    }
    if (push.kind === 'chat_message') {
      const sessionId = typeof push.payload['session_id'] === 'string' ? push.payload['session_id'] : null;
      const nodeId = typeof push.payload['node_id'] === 'string' ? push.payload['node_id'] : null;
      if (state.streaming && state.streaming.nodeId === nodeId) {
        useChat.setState({ streaming: null });
      }
      useChat.setState({ pendingStatus: null });
      // 对账：只有当前会话需要重拉；别的会话完成后等切换时再拉。
      if (sessionId && sessionId === state.activeSessionId) {
        void state.reloadMessages();
        void state.refreshSessions();
      }
      return;
    }
    if (push.kind === 'chat_status') {
      const sessionId = typeof push.payload['session_id'] === 'string' ? push.payload['session_id'] : null;
      if (!sessionId || sessionId !== state.activeSessionId) return;
      const status = typeof push.payload['status'] === 'string' ? push.payload['status'] : '';
      const detail = typeof push.payload['detail'] === 'string' ? push.payload['detail'] : '';
      const approvalId =
        typeof push.payload['approval_id'] === 'string' ? push.payload['approval_id'] : null;
      if (status === 'waiting_approval') {
        useChat.setState({ pendingStatus: { status, detail, approvalId } });
      } else {
        useChat.setState({ pendingStatus: null });
      }
    }
  });
  // 断连期间的问答不会有推送补给我们（推送是尽力而为的），重连后必须重拉一次。
  onKernelReconnect(() => {
    void useChat.getState().refreshSessions();
  });
  void useChat.getState().refreshSessions();
}
