/**
 * 基础助手（AI-01）聊天面板：任何页面都能打开的全局小窗。
 *
 * 助手回答三类问题：怎么用、现在什么状态、某个报错或概念是什么意思。
 * 它只输出文字与操作路径，不替用户改任何配置、不碰凭据、不提交任务。
 *
 * 布局不复用 sidepanel__body：消息区与输入区要分开——前者滚动，后者固定。
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { registry } from '../api/endpoints';
import type { AssistantConfigUpdate, AssistantMessage, CredentialRef } from '../api/types';
import { useAssistant, type AssistantFailure } from '../store/assistant';
import { Banner, Empty, Field, Loading, Modal } from './common';

export function AssistantPanel({ onClose }: { onClose: () => void }): JSX.Element {
  const state = useAssistant();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmCompact, setConfirmCompact] = useState(false);
  const [compactNote, setCompactNote] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottom = useRef(true);

  // 面板打开时与内核重新对账一次；平时靠 wireAssistant 的推送与重连兜底。
  useEffect(() => {
    void useAssistant.getState().refreshConfig();
    void useAssistant.getState().refreshThreads();
  }, []);

  // 只在用户本来就贴着底部时才自动滚动——往上翻看历史时不拽回来。
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [state.messages, state.sending]);

  const config = state.config;
  const configured = config !== null && config.enabled && config.credential_ref !== null;

  const send = async (): Promise<void> => {
    const text = draft.trim();
    if (!text) return;
    pinnedToBottom.current = true;
    const ok = await state.send(text);
    if (ok) setDraft('');
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    // Enter 发送，Shift+Enter 换行。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const runCompact = async (): Promise<void> => {
    const result = await state.compact();
    if (result === null) return; // 失败原因已进 state.sendError，在输入区上方展示
    setConfirmCompact(false);
    setCompactNote(
      result.compacted
        ? `已把 ${result.summarized} 条较早的消息整理成一条摘要。`
        : (result.note ?? '这次没有整理任何内容。'),
    );
  };

  return (
    <div className="sidepanel">
      <div className="sidepanel__head">
        <strong>助手</strong>
        {state.threads.length > 0 ? (
          <select
            className="select input--sm"
            style={{ maxWidth: 140 }}
            value={state.activeThreadId ?? ''}
            onChange={(e) => void state.selectThread(e.target.value)}
            title="切换对话"
          >
            {state.threads.map((t) => (
              <option key={t.thread_id} value={t.thread_id}>
                {t.title || `对话 ${t.thread_id.slice(0, 8)}`}
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => {
            setCompactNote(null);
            void state.newThread();
          }}
        >
          新对话
        </button>
        <span className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={!state.activeThreadId || state.sending || state.compacting}
          title="把较早的消息压缩成一条摘要（会额外调用一次模型）"
          onClick={() => setConfirmCompact(true)}
        >
          {state.compacting ? '整理中…' : '整理前文'}
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => setSettingsOpen((v) => !v)}
        >
          设置
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onClose}>
          收起
        </button>
      </div>

      {settingsOpen ? <AssistantSettings /> : null}

      {state.loadError ? (
        <div className="chat-scroll">
          <Banner variant="danger" title={state.loadError.detail} hint={state.loadError.hint ?? undefined} />
        </div>
      ) : !state.configLoaded || !state.threadsLoaded ? (
        <div className="chat-scroll">
          <Loading />
        </div>
      ) : !configured ? (
        <div className="chat-scroll">
          <Empty
            title="助手还没配好"
            hint={
              <>
                先选一个助手模型：到 <Link to="/registry">注册表 → 凭据</Link>{' '}
                建一条含 Base URL 和 Key 的凭据，再回到这里（右上角「设置」）选择它，并打开「启用助手」。
              </>
            }
            action={
              <button type="button" className="btn btn--sm" onClick={() => setSettingsOpen(true)}>
                打开设置
              </button>
            }
          />
        </div>
      ) : (
        <>
          <div
            className="chat-scroll"
            ref={scrollRef}
            onScroll={(e) => {
              const el = e.currentTarget;
              pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
          >
            {config && !config.secrets_unlocked ? (
              <Banner variant="warn" title="凭据库还没有解锁">
                暂时读不到助手模型的密钥，发问会失败；历史消息可以正常翻看。用带口令的方式重启内核后再试。
              </Banner>
            ) : null}

            {!state.messagesLoaded ? (
              <Loading label="读取对话历史" />
            ) : state.messages.length === 0 && !state.sending ? (
              <Empty title="还没有消息" hint="可以问：某个页面怎么用、现在系统什么状态、某个报错是什么意思。" />
            ) : null}

            {state.lastDropped > 0 ? (
              <Banner
                variant="info"
                title={`更早的 ${state.lastDropped} 条消息未随本次发送`}
                hint="对话变长后只带最近的一段发给模型。点「整理前文」可以把较早的消息压缩成摘要一并带上。"
              />
            ) : null}

            {state.messages.map((m) => (
              <ChatMessage key={m.message_id} message={m} reasons={state.degradedReasons[m.message_id]} />
            ))}
            {state.sending ? <div className="chat-meta">正在生成…</div> : null}
          </div>

          <div className="chat-composer">
            {state.sendError ? (
              <Banner variant="danger" title={state.sendError.detail} hint={state.sendError.hint ?? undefined} />
            ) : null}
            {compactNote ? <Banner variant="ok" title={compactNote} /> : null}
            <textarea
              className="input"
              rows={3}
              value={draft}
              spellCheck={false}
              disabled={state.sending}
              placeholder="Enter 发送，Shift+Enter 换行"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
            />
            <div className="row row--tight" style={{ marginTop: 'var(--sp-2)' }}>
              <button
                type="button"
                className="btn btn--primary btn--sm"
                disabled={state.sending || !draft.trim()}
                onClick={() => void send()}
              >
                {state.sending ? '正在生成…' : '发送'}
              </button>
            </div>
          </div>
        </>
      )}

      {confirmCompact ? (
        <Modal
          title="整理前文"
          onClose={() => setConfirmCompact(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirmCompact(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                disabled={state.compacting}
                onClick={() => void runCompact()}
              >
                {state.compacting ? '整理中…' : '确认整理'}
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>
            整理会把这段对话里较早的消息压缩成一条摘要，之后发问时带的是摘要。
            这需要额外调用一次模型。较早的消息不会删除，仍然留在对话记录里。
          </p>
        </Modal>
      ) : null}
    </div>
  );
}

function ChatMessage({
  message,
  reasons,
}: {
  message: AssistantMessage;
  reasons?: string[];
}): JSX.Element {
  if (message.role === 'memory') {
    return (
      <div className="chat-memory">
        <div className="chat-memory__label">已整理的前文摘要</div>
        {message.content}
      </div>
    );
  }
  const isUser = message.role === 'user';
  return (
    <div className={isUser ? 'chat-row chat-row--user' : 'chat-row'}>
      <div style={{ maxWidth: '88%' }}>
        <div className={isUser ? 'chat-bubble chat-bubble--user' : 'chat-bubble chat-bubble--assistant'}>
          {message.content}
        </div>
        {!isUser ? (
          <div className="chat-meta">
            {message.backend ? `经由 ${message.backend}` : '后端未知'}
            {message.degraded
              ? ` · 本次回答发生过降级${reasons && reasons.length > 0 ? `：${reasons.join('；')}` : ''}`
              : ''}
            {message.tokens_in !== null || message.tokens_out !== null
              ? ` · 用量 入 ${message.tokens_in ?? '未知'} / 出 ${message.tokens_out ?? '未知'}`
              : ''}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** 面板头部的折叠设置区：启用开关、选凭据、模型名覆盖。 */
function AssistantSettings(): JSX.Element {
  const config = useAssistant((s) => s.config);
  const updateConfig = useAssistant((s) => s.updateConfig);
  const [credentials, setCredentials] = useState<CredentialRef[] | null>(null);
  const [error, setError] = useState<AssistantFailure | null>(null);
  /** 模型名输入框的未提交草稿；null 表示未在编辑（跟随配置值）。 */
  const [modelDraft, setModelDraft] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    registry
      .credentials()
      .then((list) => {
        if (!cancelled) setCredentials(list);
      })
      .catch(() => {
        if (!cancelled) setCredentials([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!config) return <div className="assistant-settings"><Loading label="读取配置" /></div>;

  const save = async (body: AssistantConfigUpdate): Promise<void> => {
    setError(await updateConfig(body));
  };

  const usable = (credentials ?? []).filter((c) => !c.revoked);

  return (
    <div className="assistant-settings">
      {error ? (
        <Banner variant="danger" title={error.detail} hint={error.hint ?? undefined} />
      ) : null}
      <label className="row row--tight" style={{ cursor: 'pointer', marginBottom: 'var(--sp-2)' }}>
        <input
          type="checkbox"
          checked={config.enabled}
          onChange={(e) => void save({ enabled: e.target.checked })}
        />
        <span>启用助手</span>
      </label>
      <Field label="接口协议" hint="Base URL 路径里含 /anthropic 时选 Anthropic 兼容。">
        <select
          className="select"
          value={config.api_protocol}
          onChange={(e) => void save({ api_protocol: e.target.value as 'openai' | 'anthropic' })}
        >
          <option value="openai">OpenAI 兼容（大多数服务）</option>
          <option value="anthropic">Anthropic 兼容（Claude 及兼容端点）</option>
        </select>
      </Field>
      <Field label="模型凭据" hint="助手用这条凭据的 Base URL 和 Key 调用模型。">
        <select
          className="select"
          value={config.credential_ref ?? ''}
          disabled={credentials === null}
          onChange={(e) => void save({ credential_ref: e.target.value || null })}
        >
          <option value="">未选择</option>
          {usable.map((c) => (
            <option key={c.credential_id} value={c.credential_id}>
              {c.label}
              {c.base_url ? `（${c.base_url}）` : ''}
            </option>
          ))}
        </select>
      </Field>
      {credentials !== null && usable.length === 0 ? (
        <div className="text-xs" style={{ marginBottom: 'var(--sp-2)' }}>
          还没有可用的凭据。到 <Link to="/registry">注册表 → 凭据</Link> 建一条含 Base URL 和 Key
          的凭据，再回来选择。
        </div>
      ) : null}
      <Field label="模型名">
        <input
          className="input"
          value={modelDraft ?? config.model_override ?? ''}
          placeholder="留空则用凭据的默认模型"
          onChange={(e) => setModelDraft(e.target.value)}
          onBlur={() => {
            const value = (modelDraft ?? '').trim();
            setModelDraft(null);
            if (value !== (config.model_override ?? '')) {
              void save({ model_override: value || null });
            }
          }}
        />
      </Field>
    </div>
  );
}
