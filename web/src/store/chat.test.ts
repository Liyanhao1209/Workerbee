/**
 * chat store 的推送处理逻辑：chat_chunk 累积、chat_message 对账、
 * chat_status 横幅、重连对账。
 *
 * 与 assistant store 同一份契约：推送只是提醒，事实始终从 REST 拉；
 * chunk 只累积到「正在生成」的临时气泡。
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WsPush } from '../api/types';

// 用 vi.hoisted 保证 mock 工厂（会被提升）能拿到这些引用。
const harness = vi.hoisted(() => ({
  push: null as null | ((push: WsPush) => void),
  reconnect: null as null | (() => void),
  api: {
    createSession: vi.fn(),
    sessions: vi.fn(),
    renameSession: vi.fn(),
    deleteSession: vi.fn(),
    messages: vi.fn(),
    send: vi.fn(),
    grantSession: vi.fn(),
    revokeGrant: vi.fn(),
    tree: vi.fn(),
    forkNode: vi.fn(),
    deleteNode: vi.fn(),
    moveNode: vi.fn(),
    restoreNode: vi.fn(),
    purgeDeleted: vi.fn(),
  },
}));

vi.mock('../api/endpoints', () => ({ chat: harness.api }));
vi.mock('./connection', () => ({
  onKernelPush: (listener: (push: WsPush) => void) => {
    harness.push = listener;
    return () => undefined;
  },
  onKernelReconnect: (listener: () => void) => {
    harness.reconnect = listener;
    return () => undefined;
  },
}));

import { useChat, wireChat } from './chat';

const SESSION = {
  session_id: 's-1',
  workspace_id: 'default',
  title: '测试会话',
  credential_ref: null,
  model_override: null,
  closed: false,
  grants: [] as string[],
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

function push(kind: WsPush['kind'], payload: Record<string, unknown>): WsPush {
  return { kind, task_id: null, stage_id: null, payload };
}

function resetStore(): void {
  useChat.setState({
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
  });
}

beforeAll(async () => {
  harness.api.sessions.mockResolvedValue({ sessions: [SESSION], returned: 1 });
  harness.api.messages.mockResolvedValue({ messages: [], leaf_id: null, returned: 0 });
  harness.api.tree.mockResolvedValue({ session_id: 's-1', nodes: [], returned: 0 });
  // wireChat 有模块级 wired 防重入，整个文件只挂一次。
  wireChat();
  await vi.waitFor(() => {
    expect(useChat.getState().sessionsLoaded).toBe(true);
  });
});

beforeEach(() => {
  resetStore();
  vi.clearAllMocks();
});

describe('chat_chunk 累积', () => {
  it('同一条回复的 text 与 reasoning 分开累积，关联到 node_id', () => {
    useChat.setState({ sending: true, activeSessionId: 's-1' });

    harness.push!(push('chat_chunk', { session_id: 's-1', node_id: 'n-1', kind: 'reasoning', text: '先想' }));
    harness.push!(push('chat_chunk', { session_id: 's-1', node_id: 'n-1', kind: 'text', text: '你好' }));
    harness.push!(push('chat_chunk', { session_id: 's-1', node_id: 'n-1', kind: 'text', text: '，世界' }));

    expect(useChat.getState().streaming).toEqual({
      nodeId: 'n-1',
      sessionId: 's-1',
      text: '你好，世界',
      reasoning: '先想',
    });
  });

  it('不属于当前会话的 chunk 被丢弃', () => {
    useChat.setState({ sending: true, activeSessionId: 's-1' });
    harness.push!(push('chat_chunk', { session_id: 's-other', node_id: 'n-1', kind: 'text', text: '内容' }));
    harness.push!(push('chat_chunk', { session_id: 's-1', node_id: 'n-1', kind: 'text', text: '' }));
    expect(useChat.getState().streaming).toBeNull();

    useChat.setState({ sending: false });
    harness.push!(push('chat_chunk', { session_id: 's-1', node_id: 'n-1', kind: 'text', text: '内容' }));
    expect(useChat.getState().streaming).toBeNull();
  });
});

describe('chat_message 对账', () => {
  it('确认的是正在累积的那条：清掉临时气泡并重拉消息', async () => {
    useChat.setState({
      sending: true,
      activeSessionId: 's-1',
      streaming: { nodeId: 'n-1', sessionId: 's-1', text: '生成中…', reasoning: '' },
      pendingStatus: { status: 'waiting_approval', detail: '写文件', approvalId: 'a-1' },
    });

    harness.push!(push('chat_message', { session_id: 's-1', node_id: 'n-1' }));

    expect(useChat.getState().streaming).toBeNull();
    expect(useChat.getState().pendingStatus).toBeNull();
    await vi.waitFor(() => expect(harness.api.messages).toHaveBeenCalledWith('s-1', undefined));
  });

  it('别的会话完成：当前气泡不动，也不重拉', async () => {
    useChat.setState({
      sending: true,
      activeSessionId: 's-1',
      streaming: { nodeId: 'n-1', sessionId: 's-1', text: '生成中…', reasoning: '' },
    });

    harness.push!(push('chat_message', { session_id: 's-other', node_id: 'n-9' }));

    expect(useChat.getState().streaming?.nodeId).toBe('n-1');
    await Promise.resolve();
    expect(harness.api.messages).not.toHaveBeenCalled();
  });
});

describe('chat_status 横幅', () => {
  it('waiting_approval 显示横幅，approval_decided 清除', () => {
    useChat.setState({ activeSessionId: 's-1' });

    harness.push!(
      push('chat_status', {
        session_id: 's-1',
        node_id: 'n-1',
        status: 'waiting_approval',
        detail: '写入文件（fs_write）：out.txt',
        approval_id: 'a-1',
      }),
    );
    expect(useChat.getState().pendingStatus).toEqual({
      status: 'waiting_approval',
      detail: '写入文件（fs_write）：out.txt',
      approvalId: 'a-1',
    });

    harness.push!(
      push('chat_status', {
        session_id: 's-1',
        node_id: 'n-1',
        status: 'approval_decided',
        detail: '已批准',
        approval_id: 'a-1',
      }),
    );
    expect(useChat.getState().pendingStatus).toBeNull();
  });

  it('别的会话的状态不影响当前页', () => {
    useChat.setState({ activeSessionId: 's-1' });
    harness.push!(
      push('chat_status', { session_id: 's-other', status: 'waiting_approval', detail: 'x' }),
    );
    expect(useChat.getState().pendingStatus).toBeNull();
  });
});

describe('重连对账', () => {
  it('重连成功后重拉会话列表', async () => {
    harness.reconnect!();
    await vi.waitFor(() => expect(harness.api.sessions).toHaveBeenCalled());
  });
});

describe('会话级临时授权（D-G）', () => {
  it('撤销授权调用 REST 并重拉会话（授权标识随列表对账）', async () => {
    harness.api.revokeGrant.mockResolvedValue({ ...SESSION, grants: [] });
    harness.api.sessions.mockResolvedValue({ sessions: [SESSION], returned: 1 });
    useChat.setState({ activeSessionId: 's-1' });

    const ok = await useChat.getState().revokeGrant('run');

    expect(ok).toBe(true);
    expect(harness.api.revokeGrant).toHaveBeenCalledWith('s-1', 'run');
    await vi.waitFor(() => expect(harness.api.sessions).toHaveBeenCalled());
  });

  it('撤销失败时错误如实可见', async () => {
    harness.api.revokeGrant.mockRejectedValue(new Error('网络不可达'));
    useChat.setState({ activeSessionId: 's-1' });

    const ok = await useChat.getState().revokeGrant('write');

    expect(ok).toBe(false);
    expect(useChat.getState().sendError?.detail).toBe('撤销授权失败。');
  });

  it('chat_session 推送触发当前会话的重拉', async () => {
    useChat.setState({ activeSessionId: 's-1' });
    harness.api.sessions.mockResolvedValue({ sessions: [SESSION], returned: 1 });

    harness.push!(push('chat_session', { session_id: 's-1' }));

    await vi.waitFor(() => expect(harness.api.sessions).toHaveBeenCalled());
  });

  it('别的会话的 chat_session 推送不影响当前页', async () => {
    useChat.setState({ activeSessionId: 's-1' });
    vi.clearAllMocks();

    harness.push!(push('chat_session', { session_id: 's-other' }));

    await Promise.resolve();
    expect(harness.api.sessions).not.toHaveBeenCalled();
  });
});

describe('发送流程', () => {
  it('没有会话时第一条消息顺手建会话', async () => {
    const replyNode = { node_id: 'n-2', session_id: 's-new', role: 'assistant' };
    harness.api.createSession.mockResolvedValue({ ...SESSION, session_id: 's-new' });
    harness.api.send.mockResolvedValue({
      reply: replyNode,
      user_node: { node_id: 'n-1', session_id: 's-new', role: 'user' },
      nodes: [replyNode],
      dropped: 0,
      degraded: false,
      degraded_reasons: [],
      supports_tools: true,
    });
    harness.api.sessions.mockResolvedValue({
      sessions: [{ ...SESSION, session_id: 's-new' }],
      returned: 1,
    });

    const ok = await useChat.getState().send('你好', ['a.txt']);

    expect(ok).toBe(true);
    expect(harness.api.createSession).toHaveBeenCalled();
    expect(harness.api.send).toHaveBeenCalledWith('s-new', {
      content: '你好',
      refs: ['a.txt'],
    });
    expect(useChat.getState().supportsTools).toBe(true);
  });

  it('降级原因随回复记录到 degradedReasons', async () => {
    harness.api.send.mockResolvedValue({
      reply: { node_id: 'n-2', session_id: 's-1', role: 'assistant' },
      user_node: { node_id: 'n-1', session_id: 's-1', role: 'user' },
      nodes: [],
      dropped: 3,
      degraded: true,
      degraded_reasons: ['当前后端不支持文件操作，仅纯对话'],
      supports_tools: false,
    });
    // 上一个用例把 sessions 改成了 s-new；这里恢复成包含当前会话，
    // 否则发送后的重拉会判定「会话被切走」而清空 lastDropped。
    harness.api.sessions.mockResolvedValue({ sessions: [SESSION], returned: 1 });
    useChat.setState({ activeSessionId: 's-1' });

    const ok = await useChat.getState().send('问');

    expect(ok).toBe(true);
    expect(useChat.getState().lastDropped).toBe(3);
    expect(useChat.getState().supportsTools).toBe(false);
    expect(useChat.getState().degradedReasons['n-2']).toEqual([
      '当前后端不支持文件操作，仅纯对话',
    ]);
  });
});


// ---------------------------------------------------------------------------
// Fork 树（v0.03 §6）：分支标识、分支切换、树操作与撤销
// ---------------------------------------------------------------------------

import { computeBranchInfo, nextBranchLeaf } from './chat';
import type { ChatNode } from '../api/types';

function mkNode(id: string, parentId: string | null, deleted = false): ChatNode {
  return {
    node_id: id,
    session_id: 's-1',
    parent_id: parentId,
    role: 'user',
    content: id,
    reasoning: null,
    backend: null,
    tokens_in: null,
    tokens_out: null,
    tool_calls: null,
    tool_name: null,
    tool_call_id: null,
    deleted_at: deleted ? '2026-10-02T00:00:00Z' : null,
    created_at: '2026-10-01T00:00:00Z',
  };
}

/** r → a → b1（当前路径）；a → b2 → c2（兄弟分支）。 */
const FORKED_TREE = [
  mkNode('r', null),
  mkNode('a', 'r'),
  mkNode('b1', 'a'),
  mkNode('b2', 'a'),
  mkNode('c2', 'b2'),
];
const CURRENT_PATH = [FORKED_TREE[0]!, FORKED_TREE[1]!, FORKED_TREE[2]!];

describe('分支标识与切换（纯函数）', () => {
  it('computeBranchInfo：分叉点给出兄弟数与当前序号，单分支不出现', () => {
    const info = computeBranchInfo(FORKED_TREE, CURRENT_PATH);
    expect(info['a']).toEqual({ index: 0, count: 2 });
    expect(info['r']).toBeUndefined();
    expect(info['b1']).toBeUndefined();
  });

  it('computeBranchInfo：多根森林时首条消息携带根分支信息', () => {
    const tree = [mkNode('t1', null), mkNode('t2', null)];
    const info = computeBranchInfo(tree, [tree[1]!]);
    expect(info['t2']).toEqual({ index: 1, count: 2 });
  });

  it('computeBranchInfo：软删的兄弟不算分支', () => {
    const tree = [...FORKED_TREE.slice(0, 3), mkNode('b2', 'a', true)];
    const info = computeBranchInfo(tree, CURRENT_PATH);
    expect(info['a']).toBeUndefined();
  });

  it('nextBranchLeaf：切到兄弟分支并下到其最深最新叶', () => {
    expect(nextBranchLeaf(FORKED_TREE, 'a', ['r', 'a', 'b1'])).toBe('c2');
    // 再点一次环形回到原分支。
    expect(nextBranchLeaf(FORKED_TREE, 'a', ['r', 'a', 'b2', 'c2'])).toBe('b1');
  });

  it('nextBranchLeaf：根分支切换与无分支时的 null', () => {
    const tree = [mkNode('t1', null), mkNode('t1a', 't1'), mkNode('t2', null)];
    expect(nextBranchLeaf(tree, 't1', ['t1', 't1a'])).toBe('t2');
    expect(nextBranchLeaf(FORKED_TREE, 'b1', ['r', 'a', 'b1'])).toBeNull();
  });
});

describe('分叉入口与分支切换（store）', () => {
  it('forkAt 切换线性视图到分叉点路径，下一次发送以它为父', async () => {
    const path = [mkNode('r', null), mkNode('a', 'r')];
    harness.api.forkNode.mockResolvedValue({ messages: path, leaf_id: 'a' });
    harness.api.send.mockResolvedValue({
      reply: { node_id: 'n-reply', session_id: 's-1', role: 'assistant' },
      user_node: { node_id: 'n-user', session_id: 's-1', role: 'user' },
      nodes: [],
      dropped: 0,
      degraded: false,
      degraded_reasons: [],
      supports_tools: true,
    });
    // 发送后的重拉以服务端为准：新分支叶随对账回来。
    harness.api.messages.mockResolvedValue({
      messages: [...path, mkNode('n-user', 'a'), mkNode('n-reply', 'n-user')],
      leaf_id: 'n-reply',
      returned: 4,
    });
    useChat.setState({ activeSessionId: 's-1' });

    expect(await useChat.getState().forkAt('a')).toBe(true);
    expect(useChat.getState().messages.map((m) => m.node_id)).toEqual(['r', 'a']);
    expect(useChat.getState().leafId).toBe('a');
    expect(useChat.getState().forkParentId).toBe('a');

    await useChat.getState().send('改问');
    expect(harness.api.send).toHaveBeenCalledWith('s-1', {
      content: '改问',
      parent_id: 'a',
      refs: [],
    });
    // 发送后分叉点消费掉，分支叶推进到新回复。
    expect(useChat.getState().forkParentId).toBeNull();
    expect(useChat.getState().leafId).toBe('n-reply');
  });

  it('forkAt 失败如实可见', async () => {
    harness.api.forkNode.mockRejectedValue(new Error('网络不可达'));
    useChat.setState({ activeSessionId: 's-1' });
    expect(await useChat.getState().forkAt('a')).toBe(false);
    expect(useChat.getState().treeError?.detail).toBe('分叉失败。');
    expect(useChat.getState().forkParentId).toBeNull();
  });

  it('switchBranch 重拉到兄弟分支的叶', async () => {
    const newPath = [mkNode('r', null), mkNode('a', 'r'), mkNode('b2', 'a'), mkNode('c2', 'b2')];
    harness.api.messages.mockResolvedValue({ messages: newPath, leaf_id: 'c2', returned: 4 });
    useChat.setState({
      activeSessionId: 's-1',
      tree: FORKED_TREE,
      messages: CURRENT_PATH,
      leafId: 'b1',
      forkParentId: 'a',
    });

    await useChat.getState().switchBranch('a');

    expect(harness.api.messages).toHaveBeenCalledWith('s-1', 'c2');
    expect(useChat.getState().leafId).toBe('c2');
    // 切分支即放弃未发送的分叉意图。
    expect(useChat.getState().forkParentId).toBeNull();
  });
});

describe('树操作与撤销（store）', () => {
  it('deleteSubtree 记录可撤销操作并重拉树与消息', async () => {
    harness.api.deleteNode.mockResolvedValue({
      session_id: 's-1',
      node_id: 'b1',
      deleted: ['b1'],
      deleted_at: '2026-10-02T00:00:00Z',
      count: 1,
    });
    useChat.setState({ activeSessionId: 's-1', tree: FORKED_TREE, leafId: 'b1' });

    expect(await useChat.getState().deleteSubtree('b1')).toBe(true);
    expect(useChat.getState().undo).toEqual({ kind: 'delete', nodeId: 'b1' });
    await vi.waitFor(() => expect(harness.api.tree).toHaveBeenCalledWith('s-1'));
    await vi.waitFor(() => expect(harness.api.messages).toHaveBeenCalledWith('s-1', 'b1'));
  });

  it('undoLast：删除的撤销 = 恢复同一节点', async () => {
    harness.api.restoreNode.mockResolvedValue({ session_id: 's-1', node_id: 'b1', restored: 1 });
    useChat.setState({ activeSessionId: 's-1', undo: { kind: 'delete', nodeId: 'b1' } });

    expect(await useChat.getState().undoLast()).toBe(true);
    expect(harness.api.restoreNode).toHaveBeenCalledWith('b1');
    expect(useChat.getState().undo).toBeNull();
  });

  it('undoLast：移动的撤销 = 移回旧父节点（空为森林根）', async () => {
    harness.api.moveNode.mockResolvedValue({
      session_id: 's-1',
      node: mkNode('b1', null),
      previous_parent_id: null,
    });
    useChat.setState({
      activeSessionId: 's-1',
      undo: { kind: 'move', nodeId: 'b1', previousParentId: 'a' },
    });

    expect(await useChat.getState().undoLast()).toBe(true);
    expect(harness.api.moveNode).toHaveBeenCalledWith('b1', 'a');
  });

  it('purgeDeleted 关闭撤销窗口', async () => {
    harness.api.purgeDeleted.mockResolvedValue({ session_id: 's-1', purged: 2 });
    useChat.setState({ activeSessionId: 's-1', undo: { kind: 'delete', nodeId: 'b1' } });

    expect(await useChat.getState().purgeDeleted()).toBe(true);
    expect(harness.api.purgeDeleted).toHaveBeenCalledWith('s-1');
    expect(useChat.getState().undo).toBeNull();
  });

  it('删除失败如实可见且不产生撤销项', async () => {
    harness.api.deleteNode.mockRejectedValue(new Error('网络不可达'));
    useChat.setState({ activeSessionId: 's-1' });
    expect(await useChat.getState().deleteSubtree('b1')).toBe(false);
    expect(useChat.getState().treeError?.detail).toBe('删除失败。');
    expect(useChat.getState().undo).toBeNull();
  });
});

describe('chat_tree_changed 推送', () => {
  it('当前会话的树变更触发树与消息重拉', async () => {
    useChat.setState({ activeSessionId: 's-1', leafId: null });
    harness.push!(push('chat_tree_changed', { session_id: 's-1' }));
    await vi.waitFor(() => expect(harness.api.tree).toHaveBeenCalledWith('s-1'));
    await vi.waitFor(() => expect(harness.api.messages).toHaveBeenCalledWith('s-1', undefined));
  });

  it('别的会话的树变更不影响当前页', async () => {
    useChat.setState({ activeSessionId: 's-1' });
    vi.clearAllMocks();
    harness.push!(push('chat_tree_changed', { session_id: 's-other' }));
    await Promise.resolve();
    expect(harness.api.tree).not.toHaveBeenCalled();
    expect(harness.api.messages).not.toHaveBeenCalled();
  });
});
