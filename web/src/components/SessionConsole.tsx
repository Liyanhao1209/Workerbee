/**
 * 接入一个正在运行的会话：看它在产出什么，必要时插一句话。
 *
 * 这个组件的形状由两条约束决定：
 *
 * 1. **「能看不能发」必须是一个独立的、说得清的状态。** 有些 harness 的输入在
 *    创建会话时就已给定（`claude -p` 一类），运行中无法再注入。把这种情况和
 *    「会话不可用」混成一句话，用户就只知道「不行」，而不知道「能看」。
 *
 * 2. **输出是窗口，不是历史。** 缓冲只活在本次内核进程内，随尝试结束消失。
 *    这句话必须出现在界面上——否则用户会拿它当日志，然后在会话结束后以为
 *    记录丢了。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { sessions as sessionApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import type { SessionAttach, SessionRecord } from '../api/types';
import { Banner, Chip, Field, Loading, Modal, RelTime, ShortId } from './common';

/** 轮询间隔。会话输出是给人看的，1.5 秒足够跟上，也不至于把内核问烦。 */
const POLL_MS = 1500;

export function SessionConsole({
  record,
  onClose,
}: {
  record: SessionRecord;
  onClose: () => void;
}): JSX.Element {
  const [info, setInfo] = useState<SessionAttach | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [sendNote, setSendNote] = useState<{ ok: boolean; text: string } | null>(null);

  const outputRef = useRef<HTMLPreElement | null>(null);
  const pinnedToBottom = useRef(true);

  const sessionRef = record.session_ref;

  const poll = useCallback(async () => {
    try {
      const next = await sessionApi.attach(sessionRef);
      setInfo(next);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError) setError(err);
    }
  }, [sessionRef]);

  useEffect(() => {
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [poll]);

  // 只在用户本来就贴着底部时才自动滚动——否则他往上翻看历史时会被一直拽回来。
  useEffect(() => {
    const el = outputRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [info?.output]);

  const send = async (): Promise<void> => {
    const text = draft.trim();
    if (!text) return;
    setSending(true);
    setSendNote(null);
    try {
      const result = await sessionApi.sendInput(sessionRef, text);
      if (result.delivered) {
        setDraft('');
        pinnedToBottom.current = true;
        await poll();
      } else {
        // 送达失败要说清是为什么，不能只把输入框清空让用户以为发出去了。
        setSendNote({ ok: false, text: result.reason ?? '内核没有接受这条消息。' });
      }
    } catch (err) {
      setSendNote({
        ok: false,
        text: err instanceof ApiError ? err.detail : '发送失败。',
      });
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    // Enter 发送，Shift+Enter 换行。往 agent 那头发的消息经常要换行，
    // 所以换行不能是默认行为。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <Modal
      wide
      title={
        <span className="row row--tight">
          接入会话
          <span className="mono text-xs">{record.session_ref}</span>
        </span>
      }
      onClose={onClose}
    >
      <div className="col" style={{ gap: 'var(--sp-3)' }}>
        <div className="row row--tight" style={{ flexWrap: 'wrap' }}>
          <Chip variant="accent">{record.harness_id}</Chip>
          {record.owner_task_id ? (
            <span className="text-sm dim">
              所属任务 <ShortId id={record.owner_task_id} />
            </span>
          ) : null}
          <span className="text-sm dim">
            建立于 <RelTime value={record.created_at} />
          </span>
          <span className="text-sm dim">
            最近心跳 <RelTime value={record.last_heartbeat} />
          </span>
        </div>

        {error ? (
          <Banner variant="danger" title={error.unreachable ? '无法连接内核' : '无法读取会话状态'}>
            {error.detail}
          </Banner>
        ) : null}

        {info && !info.readable ? (
          <Banner variant="warn" title="这个会话已经不在运行">
            {info.reason ?? '内核没有给出说明。'}
          </Banner>
        ) : null}

        {info && info.readable && !info.writable ? (
          <Banner variant="warn" title="只能查看，不能发送">
            {info.reason}
          </Banner>
        ) : null}

        <div className="panel" style={{ margin: 0 }}>
          <div className="panel__head">
            输出
            {info?.truncated ? <span className="chip chip--warn">已截断</span> : null}
            {info ? (
              <span className="chip">{info.total_chars} 字符</span>
            ) : null}
          </div>
          <div className="panel__hint">
            只包含本次内核进程内产出的内容，会话结束后不再保留。已结束的尝试请到任务详情页看事件时间线与产物。
          </div>
          <div className="panel__body">
            {!info ? (
              <Loading label="读取会话输出" />
            ) : info.output ? (
              <pre
                ref={outputRef}
                className="mono text-xs"
                onScroll={(e) => {
                  const el = e.currentTarget;
                  pinnedToBottom.current =
                    el.scrollHeight - el.scrollTop - el.clientHeight < 40;
                }}
                style={{
                  maxHeight: 320,
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  margin: 0,
                  background: 'var(--bg-2)',
                  padding: 'var(--sp-2)',
                  borderRadius: 'var(--radius)',
                }}
              >
                {info.output}
              </pre>
            ) : (
              <div className="text-sm dim">
                这个会话还没有产出。harness 通常在接到输入之后才开始输出。
              </div>
            )}
          </div>
        </div>

        <Field
          label="发一条消息"
          hint="只投递首轮之后的消息：首轮输入已在创建会话时交付，重复投递会让同一条指令执行两遍。"
        >
          <textarea
            className="input"
            rows={3}
            value={draft}
            spellCheck={false}
            disabled={sending || !info?.writable}
            placeholder={
              info?.writable
                ? 'Enter 发送，Shift+Enter 换行'
                : info?.reason ?? '这个会话当前不接受输入'
            }
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
        </Field>

        {sendNote ? (
          <Banner variant={sendNote.ok ? 'info' : 'danger'} title="发送失败">
            {sendNote.text}
          </Banner>
        ) : null}

        <div className="row row--tight">
          <button
            type="button"
            className="btn btn--primary"
            disabled={sending || !info?.writable || !draft.trim()}
            onClick={() => void send()}
          >
            {sending ? '发送中…' : '发送'}
          </button>
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </Modal>
  );
}
