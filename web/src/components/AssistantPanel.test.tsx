/**
 * 助手面板（AI-01）的关键交互：
 * - 发送流程：输入 → 发送 → 「正在生成」→ 回复落进消息区、输入框清空；
 * - 发送失败：后端错误体里的 detail 与 hint（引导文案）原样展示；
 * - 草案卡片：节点清单渲染、点「采用」调对应 endpoint、成功后显示已采用。
 *
 * mock 的是 endpoints 层（与 store 同一层），断言的是用户看得到的内容
 * 与实际发出的调用参数。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ApiError } from '../api/client';
import type {
  AssistantConfig,
  AssistantDraft,
  AssistantMessage,
  AssistantSendResult,
  AssistantThread,
} from '../api/types';

const api = vi.hoisted(() => ({
  assistant: {
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
  registry: { credentials: vi.fn() },
}));

vi.mock('../api/endpoints', () => api);

import { useAssistant } from '../store/assistant';
import { AssistantPanel } from './AssistantPanel';

const CONFIG: AssistantConfig = {
  enabled: true,
  credential_ref: 'cred-1',
  model_override: null,
  api_protocol: 'openai',
  window_rounds: 20,
  window_chars: 10000,
  snapshot_budget: 4000,
  secrets_unlocked: true,
};

const THREAD: AssistantThread = {
  thread_id: 't-1',
  title: '测试对话',
  closed: false,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

function makeMessage(
  partial: Partial<AssistantMessage> & Pick<AssistantMessage, 'message_id' | 'role' | 'content'>,
): AssistantMessage {
  return {
    thread_id: 't-1',
    backend: 'openai',
    tokens_in: null,
    tokens_out: null,
    degraded: false,
    reasoning: null,
    drafts: [],
    created_at: '2026-10-01T00:00:00Z',
    ...partial,
  };
}

const DRAFT: AssistantDraft = {
  draft_id: 'd-1',
  message_id: 'm-a1',
  thread_id: 't-1',
  kind: 'workflow',
  name: '抓取日报',
  description: '抓取并汇总',
  payload: {
    nodes: [
      {
        node_id: 'n1',
        name: '抓取 issue',
        role: null,
        system_prompt: null,
        profiles: [{ harness_ref: 'claude', model_name: null, credential_ref: null, reasoning_effort: null }],
        skill_refs: [],
        tool_refs: [],
        required_inputs: [],
      },
    ],
    notes: [],
  },
  validation: { ok: true, summary: '校验通过', error: null, diagnostics: [], pending_config: [] },
  status: 'pending',
  adopted_ref: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

/** 消息列表由用例控制：发送 / 采用成功后换成新的内容，模拟服务端对账结果。 */
let messageList: AssistantMessage[] = [];

function renderPanel(): void {
  render(
    <MemoryRouter>
      <AssistantPanel onClose={() => undefined} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  messageList = [];
  vi.clearAllMocks();
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
  api.assistant.config.mockResolvedValue(CONFIG);
  api.assistant.threads.mockResolvedValue({ threads: [THREAD], returned: 1 });
  api.assistant.messages.mockImplementation(async () => ({ messages: messageList, returned: messageList.length }));
  api.registry.credentials.mockResolvedValue([]);
});

describe('发送流程', () => {
  it('输入 → 发送 → 正在生成 → 回复显示、输入框清空', async () => {
    let resolveSend: ((result: AssistantSendResult) => void) | null = null;
    api.assistant.send.mockImplementation(
      () =>
        new Promise<AssistantSendResult>((resolve) => {
          resolveSend = resolve;
        }),
    );

    const user = userEvent.setup();
    renderPanel();

    const textarea = await screen.findByPlaceholderText('Enter 发送，Shift+Enter 换行');
    await user.type(textarea, '你好');
    await user.click(screen.getByRole('button', { name: '发送' }));

    // 请求未返回时：按钮进入「正在生成」并禁用，消息区也有生成中提示。
    expect(await screen.findAllByText('正在生成…')).not.toHaveLength(0);
    expect(screen.getByRole('button', { name: '正在生成…' })).toBeDisabled();

    const userMessage = makeMessage({ message_id: 'm-u1', role: 'user', content: '你好' });
    const reply = makeMessage({ message_id: 'm-a1', role: 'assistant', content: '你好！我是助手。' });
    messageList = [userMessage, reply];
    resolveSend!({ message: reply, user_message: userMessage, dropped: 0, degraded: false, degraded_reasons: [] });

    expect(await screen.findByText('你好！我是助手。')).toBeInTheDocument();
    expect(api.assistant.send).toHaveBeenCalledWith('t-1', '你好');
    expect((textarea as HTMLTextAreaElement).value).toBe('');
  });

  it('发送失败：后端错误体里的 detail 与 hint 原样展示', async () => {
    api.assistant.send.mockRejectedValue(
      new ApiError({
        kind: 'http',
        status: 502,
        detail: '调用模型服务失败（502）',
        hint: '检查这条凭据的 Base URL 是否可达',
      }),
    );

    const user = userEvent.setup();
    renderPanel();

    const textarea = await screen.findByPlaceholderText('Enter 发送，Shift+Enter 换行');
    await user.type(textarea, '帮我看看');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('调用模型服务失败（502）')).toBeInTheDocument();
    expect(screen.getByText('检查这条凭据的 Base URL 是否可达')).toBeInTheDocument();
    // 失败后输入框里的内容保留，用户改完可以再发。
    expect((textarea as HTMLTextAreaElement).value).toBe('帮我看看');
  });
});

describe('草案卡片', () => {
  it('渲染提案与节点清单，点「采用」调 endpoint 后显示已采用', async () => {
    messageList = [makeMessage({ message_id: 'm-a1', role: 'assistant', content: '这是提案', drafts: [DRAFT] })];
    api.assistant.adoptDraft.mockImplementation(async () => {
      const adopted = { ...DRAFT, status: 'adopted', adopted_ref: 'wf-9' };
      messageList = [makeMessage({ message_id: 'm-a1', role: 'assistant', content: '这是提案', drafts: [adopted] })];
      return adopted;
    });

    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText('提案：流程「抓取日报」')).toBeInTheDocument();
    expect(screen.getByText(/抓取 issue（harness：claude/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '采用' }));

    expect(api.assistant.adoptDraft).toHaveBeenCalledWith('d-1');
    expect(await screen.findByText(/已采用。/)).toBeInTheDocument();
  });
});
