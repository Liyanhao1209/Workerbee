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
    await vi.waitFor(() => expect(harness.api.messages).toHaveBeenCalledWith('s-1'));
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
