/**
 * assistant store 的推送处理逻辑：chunk 累积、assistant_message 对账、重连对账。
 *
 * 这里不断言内部实现，只断言对外契约：
 * - streaming 气泡里 text / reasoning 分开累积、归属正确的 message_id；
 * - assistant_message 到达时临时气泡被清掉、线程列表被重拉；
 * - 重连后配置与线程都被重拉（断连期间的问答不丢）。
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WsPush } from '../api/types';

// 用 vi.hoisted 保证 mock 工厂（会被提升）能拿到这些引用。
const harness = vi.hoisted(() => ({
  push: null as null | ((push: WsPush) => void),
  reconnect: null as null | (() => void),
  api: {
    threads: vi.fn(),
    createThread: vi.fn(),
    renameThread: vi.fn(),
    messages: vi.fn(),
    send: vi.fn(),
    compact: vi.fn(),
    config: vi.fn(),
    updateConfig: vi.fn(),
    adoptDraft: vi.fn(),
    rejectDraft: vi.fn(),
  },
}));

vi.mock('../api/endpoints', () => ({ assistant: harness.api }));
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

import { useAssistant, wireAssistant } from './assistant';

const THREAD = {
  thread_id: 't-1',
  title: '测试对话',
  closed: false,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const CONFIG = {
  enabled: true,
  credential_ref: 'cred-1',
  model_override: null,
  api_protocol: 'openai' as const,
  window_rounds: 20,
  window_chars: 10000,
  snapshot_budget: 4000,
  secrets_unlocked: true,
};

function chunk(payload: Record<string, unknown>): WsPush {
  return { kind: 'assistant_chunk', task_id: null, stage_id: null, payload };
}

function assistantMessage(messageId: string): WsPush {
  return { kind: 'assistant_message', task_id: null, stage_id: null, payload: { message_id: messageId } };
}

function resetStore(): void {
  useAssistant.setState({
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
  });
}

beforeAll(async () => {
  harness.api.config.mockResolvedValue(CONFIG);
  harness.api.threads.mockResolvedValue({ threads: [THREAD], returned: 1 });
  harness.api.messages.mockResolvedValue({ messages: [], returned: 0 });
  // wireAssistant 有模块级 wired 防重入，整个文件只挂一次；
  // 挂载时会立刻做一次 refreshConfig / refreshThreads。
  wireAssistant();
  await vi.waitFor(() => {
    expect(useAssistant.getState().configLoaded).toBe(true);
    expect(useAssistant.getState().threadsLoaded).toBe(true);
  });
});

beforeEach(() => {
  resetStore();
  vi.clearAllMocks();
});

describe('assistant_chunk 累积', () => {
  it('同一条回复的 text 与 reasoning 分开累积，关联到 message_id', () => {
    useAssistant.setState({ sending: true, activeThreadId: 't-1' });

    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'reasoning', text: '先想' }));
    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: '你好' }));
    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: '，世界' }));

    expect(useAssistant.getState().streaming).toEqual({
      messageId: 'm-1',
      threadId: 't-1',
      text: '你好，世界',
      reasoning: '先想',
    });
  });

  it('新的 message_id 到来时替换正在累积的气泡（上一条靠重拉对账）', () => {
    useAssistant.setState({ sending: true, activeThreadId: 't-1' });

    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: '旧回复' }));
    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-2', kind: 'text', text: '新回复' }));

    expect(useAssistant.getState().streaming).toEqual({
      messageId: 'm-2',
      threadId: 't-1',
      text: '新回复',
      reasoning: '',
    });
  });

  it('不属于当前上下文的 chunk 被丢弃', () => {
    useAssistant.setState({ sending: true, activeThreadId: 't-1' });
    const stale = chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: '内容' });

    // 其它线程的 chunk：那边完成后由 assistant_message 触发重拉，不在这里累积。
    harness.push!(chunk({ thread_id: 't-other', message_id: 'm-1', kind: 'text', text: '内容' }));
    // 空文本、缺 id 的畸形推送。
    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: '' }));
    harness.push!(chunk({ thread_id: 't-1', kind: 'text', text: '内容' }));
    harness.push!(chunk({ thread_id: 't-1', message_id: 'm-1', kind: 'text', text: 42 }));
    expect(useAssistant.getState().streaming).toBeNull();

    // 没有在发送（sending=false）时整条推送都不该落地。
    useAssistant.setState({ sending: false });
    harness.push!(stale);
    expect(useAssistant.getState().streaming).toBeNull();
  });
});

describe('assistant_message 对账', () => {
  it('确认的是正在累积的那条：清掉临时气泡并重拉线程列表', async () => {
    useAssistant.setState({
      sending: true,
      activeThreadId: 't-1',
      streaming: { messageId: 'm-1', threadId: 't-1', text: '生成中…', reasoning: '' },
    });

    harness.push!(assistantMessage('m-1'));

    expect(useAssistant.getState().streaming).toBeNull();
    await vi.waitFor(() => expect(harness.api.threads).toHaveBeenCalled());
  });

  it('不是正在累积的那条：气泡保留，但仍重拉对账', async () => {
    useAssistant.setState({
      sending: true,
      activeThreadId: 't-1',
      streaming: { messageId: 'm-1', threadId: 't-1', text: '生成中…', reasoning: '' },
    });

    harness.push!(assistantMessage('m-other'));

    expect(useAssistant.getState().streaming?.messageId).toBe('m-1');
    await vi.waitFor(() => expect(harness.api.threads).toHaveBeenCalled());
  });
});

describe('重连对账', () => {
  it('重连成功后重拉配置与线程列表', async () => {
    harness.reconnect!();

    await vi.waitFor(() => {
      expect(harness.api.config).toHaveBeenCalled();
      expect(harness.api.threads).toHaveBeenCalled();
    });
  });
});
