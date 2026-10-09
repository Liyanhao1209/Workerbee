/**
 * Web Chat 页（v0.03 §5，Phase 3a）：对话 + 文件系统工具。
 *
 * 三栏：左 会话列表 ｜ 中 对话流 ｜ 右 工作区文件树。
 * 与全局助手小窗分域并存：这里允许模型经工具写文件（写类操作逐次过审批），
 * 助手小窗永远只读。输入框里的 `@路径` 会在发送时把文件内容注入上下文。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChatNode } from '../api/types';
import { extractAtRefs } from '../lib/atrefs';
import { renderMarkdown } from '../lib/markdown';
import { useChat } from '../store/chat';
import { useWorkspace } from '../store/workspace';
import { FileTree } from '../components/chat/FileTree';
import { Banner, Empty, Loading, Modal } from '../components/common';

export function ChatPage(): JSX.Element {
  const state = useChat();
  const currentWorkspaceId = useWorkspace((s) => s.currentId);
  const workspaceId = currentWorkspaceId === '' ? undefined : currentWorkspaceId;

  const [draft, setDraft] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottom = useRef(true);

  // 页面打开时与内核重新对账一次；平时靠 wireChat 的推送与重连兜底。
  useEffect(() => {
    void useChat.getState().refreshSessions(workspaceId);
    // 工作区切换由 AppShell 的切换器触发；这里只在挂载时对齐一次。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 只在用户本来就贴着底部时才自动滚动——往上翻看历史时不拽回来。
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [state.messages, state.sending, state.streaming]);

  const activeSession =
    state.sessions.find((s) => s.session_id === state.activeSessionId) ?? null;

  const refs = useMemo(() => extractAtRefs(draft), [draft]);

  const send = async (): Promise<void> => {
    const text = draft.trim();
    if (!text) return;
    pinnedToBottom.current = true;
    const ok = await state.send(text, extractAtRefs(text));
    if (ok) setDraft('');
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    // Enter 发送，Shift+Enter 换行。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const insertRef = (path: string): void => {
    setDraft((current) => {
      const token = `@${path}`;
      if (extractAtRefs(current).includes(path)) return current;
      return current.trim() ? `${current.replace(/\s+$/, '')} ${token} ` : `${token} `;
    });
  };

  const saveRename = async (): Promise<void> => {
    const title = renameDraft.trim();
    if (!title) return;
    const ok = await state.renameSession(title);
    if (ok) setRenaming(false);
  };

  return (
    <div className="chatpage">
      {/* 左栏：会话列表 */}
      <aside className="chatpage__sessions">
        <div className="chatpage__sessions-head">
          <strong>会话</strong>
          <span className="spacer" />
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void state.newSession(workspaceId)}
          >
            新会话
          </button>
        </div>
        {!state.sessionsLoaded ? (
          <Loading label="读取会话" />
        ) : state.sessions.length === 0 ? (
          <div className="chatpage__sessions-empty">还没有会话</div>
        ) : (
          state.sessions.map((s) => (
            <button
              key={s.session_id}
              type="button"
              className={
                s.session_id === state.activeSessionId
                  ? 'chatpage__session chatpage__session--active'
                  : 'chatpage__session'
              }
              title={s.title || s.session_id}
              onClick={() => void state.selectSession(s.session_id)}
            >
              {s.title || '（未命名）'}
            </button>
          ))
        )}
      </aside>

      {/* 中栏：对话流 */}
      <section className="chatpage__main">
        <div className="chatpage__main-head">
          {renaming && activeSession ? (
            <>
              <input
                className="input input--sm"
                style={{ maxWidth: 220 }}
                value={renameDraft}
                autoFocus
                maxLength={100}
                placeholder="会话名"
                onChange={(e) => setRenameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void saveRename();
                  if (e.key === 'Escape') setRenaming(false);
                }}
              />
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                disabled={!renameDraft.trim()}
                onClick={() => void saveRename()}
              >
                保存
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setRenaming(false)}
              >
                取消
              </button>
            </>
          ) : (
            <strong>{activeSession ? activeSession.title || '（未命名）' : '对话'}</strong>
          )}
          {state.supportsTools === false ? (
            <span className="chatpage__badge" title="当前后端不支持文件操作工具">
              仅纯对话
            </span>
          ) : null}
          {activeSession?.grants.includes('write') ? (
            <button
              type="button"
              className="chatpage__badge chatpage__badge--grant"
              title="本会话内写文件类操作不再逐次询问；点击撤销，恢复逐次批准"
              onClick={() => void state.revokeGrant('write')}
            >
              写文件免审批 ✕
            </button>
          ) : null}
          {activeSession?.grants.includes('run') ? (
            <button
              type="button"
              className="chatpage__badge chatpage__badge--grant"
              title="本会话内执行命令不再逐次询问（危险命令仍会询问）；点击撤销，恢复逐次批准"
              onClick={() => void state.revokeGrant('run')}
            >
              执行命令免审批 ✕
            </button>
          ) : null}
          <span className="spacer" />
          {activeSession && !renaming ? (
            <>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => {
                  setRenameDraft(activeSession.title);
                  setRenaming(true);
                }}
              >
                改名
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setConfirmDelete(true)}
              >
                删除
              </button>
            </>
          ) : null}
        </div>

        {state.loadError ? (
          <div className="chat-scroll">
            <Banner
              variant="danger"
              title={state.loadError.detail}
              hint={state.loadError.hint ?? undefined}
            />
          </div>
        ) : (
          <>
            <div
              className="chat-scroll"
              ref={scrollRef}
              onScroll={(e) => {
                const el = e.currentTarget;
                pinnedToBottom.current =
                  el.scrollHeight - el.scrollTop - el.clientHeight < 40;
              }}
            >
              {!state.messagesLoaded ? (
                <Loading label="读取对话历史" />
              ) : state.messages.length === 0 && !state.sending ? (
                <Empty
                  title="还没有消息"
                  hint={
                    <>
                      直接输入问题开始；用 <code>@路径</code> 引用工作区里的文件
                      （或点右侧文件树）。模型可以经工具读写工作区文件——写操作会
                      先请你批准。
                    </>
                  }
                />
              ) : null}

              {state.lastDropped > 0 ? (
                <Banner
                  variant="info"
                  title={`更早的 ${state.lastDropped} 条消息未随本次发送`}
                  hint="对话变长后只带最近的一段发给模型；历史完整保留在记录里。"
                />
              ) : null}

              {state.messages.map((m) => (
                <ChatNodeView
                  key={m.node_id}
                  node={m}
                  reasons={state.degradedReasons[m.node_id]}
                />
              ))}
              {state.streaming ? (
                <div className="chat-row">
                  <div style={{ maxWidth: '88%' }}>
                    <div className="chat-bubble chat-bubble--assistant">
                      {state.streaming.reasoning ? (
                        <ReasoningBlock text={state.streaming.reasoning} />
                      ) : null}
                      {state.streaming.text ? (
                        <MarkdownContent text={state.streaming.text} />
                      ) : (
                        <span className="chat-meta">正在生成…</span>
                      )}
                    </div>
                  </div>
                </div>
              ) : state.sending && !state.pendingStatus ? (
                <div className="chat-meta">正在生成…</div>
              ) : null}
            </div>

            <div className="chat-composer">
              {state.pendingStatus ? (
                <Banner
                  variant="warn"
                  title={`等待批准：${state.pendingStatus.detail}`}
                  hint="到审批中心（右上角铃铛）批准或拒绝；批准时可勾选「本会话不再询问此类操作」。批准后操作才会执行。"
                />
              ) : null}
              {state.sendError ? (
                <Banner
                  variant="danger"
                  title={state.sendError.detail}
                  hint={state.sendError.hint ?? undefined}
                />
              ) : null}
              {refs.length > 0 ? (
                <div className="chatpage__refs">
                  引用：{refs.map((r) => (
                    <code key={r}>@{r}</code>
                  ))}
                </div>
              ) : null}
              <textarea
                className="input"
                rows={3}
                value={draft}
                spellCheck={false}
                disabled={state.sending}
                placeholder="Enter 发送，Shift+Enter 换行；@路径 引用工作区文件"
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
      </section>

      {/* 右栏：工作区文件树 */}
      <aside className="chatpage__files">
        <FileTree workspaceId={workspaceId} onPick={insertRef} />
      </aside>

      {confirmDelete && activeSession ? (
        <Modal
          title="删除会话"
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <button
                type="button"
                className="btn"
                onClick={() => setConfirmDelete(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  void state.deleteSession().then((ok) => {
                    if (ok) setConfirmDelete(false);
                  });
                }}
              >
                确认删除
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>
            会话「{activeSession.title || '（未命名）'}」及其全部消息会被删除，
            不可恢复。工作区里的文件不受影响。
          </p>
        </Modal>
      ) : null}
    </div>
  );
}

/** 助手消息正文：marked 渲染 + dompurify 消毒后的 HTML。 */
function MarkdownContent({ text }: { text: string }): JSX.Element {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return <div className="chat-md" dangerouslySetInnerHTML={{ __html: html }} />;
}

/** 推理过程（思维链）：默认折叠的可展开区，内容按纯文本展示。 */
function ReasoningBlock({ text }: { text: string }): JSX.Element {
  return (
    <details className="chat-reasoning">
      <summary>思考过程</summary>
      <div className="chat-reasoning__body">{text}</div>
    </details>
  );
}

const TOOL_LABELS: Record<string, string> = {
  fs_list: '列目录',
  fs_read: '读文件',
  fs_write: '写文件',
  fs_mkdir: '建目录',
  fs_move: '移动/改名',
  fs_delete: '删除',
  fs_run: '执行命令',
};

function toolLabel(name: string | null): string {
  return (name && TOOL_LABELS[name]) ?? name ?? '工具';
}

function toolTarget(args: Record<string, unknown> | undefined): string {
  if (!args) return '';
  const candidate = args['path'] ?? args['command'] ?? args['dst'] ?? args['src'];
  return typeof candidate === 'string' ? candidate : '';
}

function ChatNodeView({
  node,
  reasons,
}: {
  node: ChatNode;
  reasons?: string[];
}): JSX.Element | null {
  if (node.role === 'tool') {
    // 工具结果节点：折叠展示，如实可见但不占对话流主体。
    return (
      <div className="chat-row">
        <div style={{ maxWidth: '88%' }}>
          <details className="chat-tool">
            <summary>
              {toolLabel(node.tool_name)} 的结果
              <span className="chat-meta">（点击展开）</span>
            </summary>
            <pre className="chat-tool__body">{node.content}</pre>
          </details>
        </div>
      </div>
    );
  }
  if (node.role !== 'user' && node.role !== 'assistant') return null;

  const isUser = node.role === 'user';
  const calls = node.tool_calls ?? [];
  return (
    <div className={isUser ? 'chat-row chat-row--user' : 'chat-row'}>
      <div style={{ maxWidth: '88%' }}>
        <div
          className={
            isUser ? 'chat-bubble chat-bubble--user' : 'chat-bubble chat-bubble--assistant'
          }
        >
          {isUser ? (
            node.content
          ) : (
            <>
              {node.reasoning ? <ReasoningBlock text={node.reasoning} /> : null}
              {node.content ? <MarkdownContent text={node.content} /> : null}
              {calls.length > 0 ? (
                <div className="chat-toolcalls">
                  {calls.map((c) => (
                    <div key={c.id} className="chat-toolcalls__item">
                      🔧 {toolLabel(c.name)} {toolTarget(c.arguments)}
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          )}
        </div>
        {!isUser ? (
          <div className="chat-meta">
            {node.backend ? `经由 ${node.backend}` : '后端未知'}
            {reasons && reasons.length > 0 ? ` · 降级：${reasons.join('；')}` : ''}
            {node.tokens_in !== null || node.tokens_out !== null
              ? ` · 用量 入 ${node.tokens_in ?? '未知'} / 出 ${node.tokens_out ?? '未知'}`
              : ''}
          </div>
        ) : null}
      </div>
    </div>
  );
}
