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

/** 分支视图里可撤销的最近一次树操作（撤销窗口 = 清空前，D-F）。 */
export type ChatUndo =
  | { kind: 'delete'; nodeId: string }
  | { kind: 'move'; nodeId: string; previousParentId: string | null };

/** 一条消息的分支标识：它有 count 个分支，当前显示第 index 个（0 起）。 */
export interface BranchInfo {
  index: number;
  count: number;
}

function asFailure(err: unknown, fallback: string): ChatFailure {
  if (err instanceof ApiError) return { detail: err.detail, hint: err.hint };
  return { detail: fallback, hint: null };
}

/** 未删除的子节点（插入顺序）。parentId 为 null 时取森林的根列表。 */
export function liveChildren(tree: ChatNode[], parentId: string | null): ChatNode[] {
  return tree.filter(
    (n) => (n.parent_id ?? null) === parentId && !n.deleted_at,
  );
}

/**
 * 线性视图的分支标识：当前路径上每个「后面还有别的分支」的消息，
 * 给出兄弟分支数与当前所在分支的序号（含森林多根：首条消息携带根分支信息）。
 */
export function computeBranchInfo(
  tree: ChatNode[],
  messages: ChatNode[],
): Record<string, BranchInfo> {
  const info: Record<string, BranchInfo> = {};
  if (messages.length === 0 || tree.length === 0) return info;
  const first = messages[0]!;
  const roots = liveChildren(tree, null);
  if (roots.length > 1) {
    const index = roots.findIndex((r) => r.node_id === first.node_id);
    info[first.node_id] = { index: Math.max(index, 0), count: roots.length };
  }
  for (let i = 0; i < messages.length - 1; i += 1) {
    const current = messages[i]!;
    const next = messages[i + 1]!;
    const kids = liveChildren(tree, current.node_id);
    if (kids.length < 2) continue;
    const index = kids.findIndex((k) => k.node_id === next.node_id);
    info[current.node_id] = { index: Math.max(index, 0), count: kids.length };
  }
  return info;
}

/**
 * 分支切换的目标叶：从 nodeId 的第 (current+1) 个分支（环形）一路向下，
 * 每步取最近活跃的未删除子节点，直到叶子。没有别的分支时返回 null。
 */
export function nextBranchLeaf(
  tree: ChatNode[],
  nodeId: string,
  currentPath: string[],
): string | null {
  const node = tree.find((n) => n.node_id === nodeId);
  if (!node) return null;
  // 根分支：nodeId 本身在根列表里时，兄弟是其他根；否则兄弟是 nodeId 的子节点。
  const roots = liveChildren(tree, null);
  let siblings: ChatNode[];
  let currentId: string | null;
  if (roots.length > 1 && roots.some((r) => r.node_id === nodeId)) {
    siblings = roots;
    currentId = nodeId;
  } else {
    siblings = liveChildren(tree, nodeId);
    const idx = currentPath.indexOf(nodeId);
    currentId = idx >= 0 && idx + 1 < currentPath.length ? currentPath[idx + 1]! : null;
  }
  if (siblings.length < 2) return null;
  const currentIdx = siblings.findIndex((s) => s.node_id === currentId);
  const next = siblings[(currentIdx + 1) % siblings.length]!;
  // 沿「最近活跃」的子节点一路向下到叶子。
  let leaf = next;
  for (;;) {
    const children = liveChildren(tree, leaf.node_id);
    if (children.length === 0) return leaf.node_id;
    leaf = children[children.length - 1]!;
  }
}

interface ChatStoreState {
  sessions: ChatSession[];
  sessionsLoaded: boolean;
  loadError: ChatFailure | null;

  activeSessionId: string | null;
  messages: ChatNode[];
  leafId: string | null;
  messagesLoaded: boolean;

  /** 会话的完整节点森林（含软删节点），分支视图与分支标识的事实源。 */
  tree: ChatNode[];
  treeLoaded: boolean;
  /** 待生效的分叉点：下一次发送以它为 parent（「从此分叉」之后）。 */
  forkParentId: string | null;
  /** 最近一次树操作（可撤销；清空后撤销窗口关闭）。 */
  undo: ChatUndo | null;
  /** 树操作的失败（分支视图横幅；与发送错误分开，互不覆盖）。 */
  treeError: ChatFailure | null;

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
  reloadMessages: (leafId?: string) => Promise<void>;
  reloadTree: () => Promise<void>;
  send: (content: string, refs?: string[]) => Promise<boolean>;
  /** 撤销本会话某类操作的临时授权（D-G），恢复逐次审批。 */
  revokeGrant: (category: string) => Promise<boolean>;

  /** 从此分叉：把线性视图切到该节点所在路径，下一次发送以它为父。 */
  forkAt: (nodeId: string) => Promise<boolean>;
  /** 取消待生效的分叉点（回到「挂在当前分支末尾」的默认语义）。 */
  cancelFork: () => void;
  /** 把线性视图切到以该节点为末端的分支（分支视图双击节点用）。 */
  openBranchAt: (nodeId: string) => Promise<void>;
  /** 切换到该消息的下一个兄弟分支（环形）。 */
  switchBranch: (nodeId: string) => Promise<void>;
  /** 级联软删除子树（清空前可经 undoLast 撤销）。 */
  deleteSubtree: (nodeId: string) => Promise<boolean>;
  /** 恢复软删子树（按删除批次还原）。 */
  restoreSubtree: (nodeId: string) => Promise<boolean>;
  /** 移动/合并子树；newParentId 为空串表示挂到森林根。 */
  moveSubtree: (nodeId: string, newParentId: string) => Promise<boolean>;
  /** 清空全部软删消息（硬删，不可恢复，撤销窗口关闭）。 */
  purgeDeleted: () => Promise<boolean>;
  /** 撤销最近一次树操作（删除→恢复；移动→移回原父节点）。 */
  undoLast: () => Promise<boolean>;
}

export const useChat = create<ChatStoreState>((set, get) => ({
  sessions: [],
  sessionsLoaded: false,
  loadError: null,

  activeSessionId: null,
  messages: [],
  leafId: null,
  messagesLoaded: false,

  tree: [],
  treeLoaded: false,
  forkParentId: null,
  undo: null,
  treeError: null,

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
      if (switched)
        set({
          messages: [],
          leafId: null,
          messagesLoaded: false,
          lastDropped: 0,
          tree: [],
          treeLoaded: false,
          forkParentId: null,
          undo: null,
          treeError: null,
        });
      await get().reloadMessages();
      void get().reloadTree();
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
      tree: [],
      treeLoaded: false,
      forkParentId: null,
      undo: null,
      treeError: null,
    });
    await get().reloadMessages();
    void get().reloadTree();
  },

  newSession: async (workspaceId) => {
    try {
      const session = await chatApi.createSession(workspaceId ? { workspace_id: workspaceId } : {});
      set({
        sendError: null,
        lastDropped: 0,
        streaming: null,
        pendingStatus: null,
        tree: [],
        treeLoaded: false,
        forkParentId: null,
        undo: null,
        treeError: null,
      });
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

  reloadMessages: async (leafId) => {
    const sessionId = get().activeSessionId;
    if (!sessionId) {
      set({ messages: [], leafId: null, messagesLoaded: true });
      return;
    }
    // 缺省沿当前分支叶读取；显式传 leafId 用于分支切换。
    const leaf = leafId ?? get().leafId ?? undefined;
    try {
      const data = await chatApi.messages(sessionId, leaf);
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
      // 当前叶已被删除（如刚删了所在分支）：落回默认分支再试一次。
      if (leafId !== undefined || get().leafId !== null) {
        set({ leafId: null });
        try {
          const data = await chatApi.messages(sessionId);
          if (get().activeSessionId !== sessionId) return;
          set({
            messages: data.messages ?? [],
            leafId: data.leaf_id,
            messagesLoaded: true,
            loadError: null,
          });
          return;
        } catch {
          // 落到下面的错误处理
        }
      }
      set({ messagesLoaded: true, loadError: asFailure(err, '读取对话历史失败。') });
    }
  },

  reloadTree: async () => {
    const sessionId = get().activeSessionId;
    if (!sessionId) {
      set({ tree: [], treeLoaded: true });
      return;
    }
    try {
      const data = await chatApi.tree(sessionId);
      if (get().activeSessionId !== sessionId) return;
      set({ tree: data.nodes ?? [], treeLoaded: true });
    } catch (err) {
      if (get().activeSessionId !== sessionId) return;
      set({ treeLoaded: true, treeError: asFailure(err, '读取分支结构失败。') });
    }
  },

  revokeGrant: async (category) => {
    const sessionId = get().activeSessionId;
    if (!sessionId) return false;
    try {
      await chatApi.revokeGrant(sessionId, category);
      await get().refreshSessions();
      return true;
    } catch (err) {
      set({ sendError: asFailure(err, '撤销授权失败。') });
      return false;
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
      // 分叉点优先；否则沿当前分支叶继续（不会跳到别的分支的最新叶上）。
      const parentId = get().forkParentId ?? get().leafId ?? undefined;
      const result = await chatApi.send(sessionId, {
        content: text,
        ...(parentId !== undefined ? { parent_id: parentId } : {}),
        refs,
      });
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
        forkParentId: null,
        // 分支叶推进到本轮回复：紧随其后的重拉沿新分支读取。
        leafId: result.reply.node_id,
      });
      // 回复已在响应里，但消息列表、分支结构与会话排序仍以服务端为准重拉一次。
      await get().refreshSessions();
      return true;
    } catch (err) {
      set({ sending: false, streaming: null, pendingStatus: null, sendError: asFailure(err, '发送失败。') });
      return false;
    }
  },

  forkAt: async (nodeId) => {
    try {
      const result = await chatApi.forkNode(nodeId);
      set({
        messages: result.messages ?? [],
        leafId: result.leaf_id,
        forkParentId: result.leaf_id,
        messagesLoaded: true,
        treeError: null,
        sendError: null,
      });
      return true;
    } catch (err) {
      set({ treeError: asFailure(err, '分叉失败。') });
      return false;
    }
  },

  cancelFork: () => {
    set({ forkParentId: null });
  },

  openBranchAt: async (nodeId) => {
    set({ forkParentId: null });
    await get().reloadMessages(nodeId);
  },

  switchBranch: async (nodeId) => {
    const { tree, messages } = get();
    const leaf = nextBranchLeaf(
      tree,
      nodeId,
      messages.map((m) => m.node_id),
    );
    if (!leaf || leaf === get().leafId) return;
    set({ forkParentId: null });
    await get().reloadMessages(leaf);
  },

  deleteSubtree: async (nodeId) => {
    try {
      const result = await chatApi.deleteNode(nodeId);
      const { forkParentId } = get();
      set({
        undo: { kind: 'delete', nodeId },
        treeError: null,
        // 分叉点被删掉了：清掉，避免下一次发送挂在已删除节点上。
        ...(forkParentId && result.deleted.includes(forkParentId)
          ? { forkParentId: null }
          : {}),
      });
      await get().reloadTree();
      await get().reloadMessages();
      return true;
    } catch (err) {
      set({ treeError: asFailure(err, '删除失败。') });
      return false;
    }
  },

  restoreSubtree: async (nodeId) => {
    try {
      await chatApi.restoreNode(nodeId);
      set({ treeError: null });
      await get().reloadTree();
      await get().reloadMessages();
      return true;
    } catch (err) {
      set({ treeError: asFailure(err, '恢复失败。') });
      return false;
    }
  },

  moveSubtree: async (nodeId, newParentId) => {
    try {
      const result = await chatApi.moveNode(nodeId, newParentId);
      set({
        undo: { kind: 'move', nodeId, previousParentId: result.previous_parent_id },
        treeError: null,
      });
      await get().reloadTree();
      await get().reloadMessages();
      return true;
    } catch (err) {
      set({ treeError: asFailure(err, '移动失败。') });
      return false;
    }
  },

  purgeDeleted: async () => {
    const sessionId = get().activeSessionId;
    if (!sessionId) return false;
    try {
      await chatApi.purgeDeleted(sessionId);
      // 硬删后撤销窗口关闭（D-F）。
      set({ undo: null, treeError: null });
      await get().reloadTree();
      await get().reloadMessages();
      return true;
    } catch (err) {
      set({ treeError: asFailure(err, '清空失败。') });
      return false;
    }
  },

  undoLast: async () => {
    const undo = get().undo;
    if (!undo) return false;
    set({ undo: null });
    if (undo.kind === 'delete') return get().restoreSubtree(undo.nodeId);
    return get().moveSubtree(undo.nodeId, undo.previousParentId ?? '');
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
      return;
    }
    if (push.kind === 'chat_tree_changed') {
      // 树结构变更（软删/移动/恢复/清空）：重拉树与线性视图对账。
      const sessionId = typeof push.payload['session_id'] === 'string' ? push.payload['session_id'] : null;
      if (sessionId && sessionId === state.activeSessionId) {
        void state.reloadTree();
        void state.reloadMessages();
      }
      return;
    }
    if (push.kind === 'chat_session') {
      // 会话属性变化（如在审批中心授予了临时授权）：重拉列表对齐授权标识。
      const sessionId = typeof push.payload['session_id'] === 'string' ? push.payload['session_id'] : null;
      if (sessionId && sessionId === state.activeSessionId) {
        void state.refreshSessions();
      }
    }
  });
  // 断连期间的问答不会有推送补给我们（推送是尽力而为的），重连后必须重拉一次。
  onKernelReconnect(() => {
    void useChat.getState().refreshSessions();
  });
  void useChat.getState().refreshSessions();
}
