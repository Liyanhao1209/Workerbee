/**
 * 单次执行尝试的工作细节：输入、推理过程、工具调用、涉及的文件、产物正文。
 *
 * 数据来自事件日志的如实记录（attempt.input / attempt.reasoning /
 * attempt.tool_use / attempt.tool_result）。哪一项没有记录就明说「没有记录」，
 * 不显示空容器冒充。
 */

import { useEffect, useState } from 'react';
import { ApiError } from '../api/client';
import { tasks as taskApi } from '../api/endpoints';
import type { ArtifactContent, AttemptWork, ToolCallView } from '../api/types';
import { Banner, Chip, Loading } from './common';

export function AttemptWorkPanel({
  taskId,
  attemptId,
}: {
  taskId: string;
  attemptId: string;
}): JSX.Element {
  const [data, setData] = useState<AttemptWork | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    taskApi
      .attemptWork(taskId, attemptId)
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err : null);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, attemptId]);

  if (loading) return <Loading label="读取工作细节" />;
  if (error) {
    return (
      <Banner variant="danger" title="读取失败">
        {error.detail}
      </Banner>
    );
  }
  if (!data) return <div className="text-sm dim">没有记录。</div>;

  const failedCalls = data.tool_calls.filter((c) => c.is_error === true).length;

  return (
    <div className="col" style={{ gap: 'var(--sp-3)', marginTop: 'var(--sp-2)' }}>
      <div className="row row--tight" style={{ flexWrap: 'wrap' }}>
        <Chip>工具调用 {data.tool_calls.length} 次{failedCalls > 0 ? `（失败 ${failedCalls}）` : ''}</Chip>
        {data.files_written.length > 0 ? <Chip>写入/修改 {data.files_written.length} 个文件</Chip> : null}
        {data.commands.length > 0 ? <Chip>执行命令 {data.commands.length} 条</Chip> : null}
        {data.reasoning !== null ? (
          <Chip>推理 {data.reasoning.length} 字{data.reasoning_truncated ? '（未记全）' : ''}</Chip>
        ) : null}
      </div>

      <section>
        <div className="section-title">发给 harness 的输入</div>
        {data.input ? (
          <div className="col" style={{ gap: 'var(--sp-2)' }}>
            {data.input.user_input ? (
              <details>
                <summary className="text-sm" style={{ cursor: 'pointer' }}>
                  任务输入{data.input.user_input_truncated ? '（过长，只记录了开头）' : ''}
                </summary>
                <pre className="code-block">{data.input.user_input}</pre>
              </details>
            ) : null}
            {data.input.system_prompt ? (
              <details>
                <summary className="text-sm" style={{ cursor: 'pointer' }}>
                  系统提示词{data.input.system_prompt_truncated ? '（过长，只记录了开头）' : ''}
                </summary>
                <pre className="code-block">{data.input.system_prompt}</pre>
              </details>
            ) : null}
          </div>
        ) : (
          <div className="text-sm dim">没有输入记录（这次尝试早于该功能，或记录已被清理）。</div>
        )}
      </section>

      <section>
        <div className="section-title">推理过程</div>
        {data.reasoning !== null ? (
          <>
            {data.reasoning_truncated ? (
              <div className="text-xs dim">内容过长，只记录了开头部分。</div>
            ) : null}
            <pre className="code-block">{data.reasoning || '（空）'}</pre>
          </>
        ) : (
          <div className="text-sm dim">该 harness 没有产生可回看的推理记录。</div>
        )}
      </section>

      <section>
        <div className="section-title">工具调用</div>
        {data.tool_calls.length === 0 ? (
          <div className="text-sm dim">没有工具调用记录。</div>
        ) : (
          <div className="col" style={{ gap: 'var(--sp-2)' }}>
            {data.tool_calls.map((call, i) => (
              <ToolCallRow key={call.tool_use_id ?? i} call={call} />
            ))}
          </div>
        )}
      </section>

      {data.files_written.length > 0 || data.files_read.length > 0 ? (
        <section>
          <div className="section-title">涉及的文件</div>
          {data.files_written.length > 0 ? (
            <div className="text-sm">
              写入或修改：
              {data.files_written.map((f) => (
                <div key={f} className="mono text-sm">
                  {f}
                </div>
              ))}
            </div>
          ) : null}
          {data.files_read.length > 0 ? (
            <div className="text-sm" style={{ marginTop: 4 }}>
              读取：
              {data.files_read.map((f) => (
                <div key={f} className="mono text-sm dim">
                  {f}
                </div>
              ))}
            </div>
          ) : null}
          <div className="text-xs dim" style={{ marginTop: 4 }}>
            按工具调用推断；「写入或修改」不区分新建与改动已有文件。
          </div>
        </section>
      ) : null}

      {data.commands.length > 0 ? (
        <section>
          <div className="section-title">执行的命令</div>
          <pre className="code-block">{data.commands.join('\n')}</pre>
        </section>
      ) : null}

      {data.artifact_ids.length > 0 ? (
        <section>
          <div className="section-title">产物正文</div>
          <div className="col" style={{ gap: 'var(--sp-2)' }}>
            {data.artifact_ids.map((id) => (
              <ArtifactText key={id} taskId={taskId} artifactId={id} />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ToolCallRow({ call }: { call: ToolCallView }): JSX.Element {
  return (
    <div
      style={{
        border: '1px solid var(--line)',
        borderRadius: 'var(--radius)',
        padding: '4px 8px',
      }}
    >
      <div className="row row--tight">
        <span className="text-sm">{call.name ?? '（未知工具）'}</span>
        {call.target ? (
          <span className="mono text-xs dim truncate" title={call.target}>
            {call.target}
          </span>
        ) : null}
        <span className="spacer" />
        {call.is_error === null ? (
          <Chip variant="warn">进行中或无结果</Chip>
        ) : call.is_error ? (
          <Chip variant="danger">失败</Chip>
        ) : (
          <Chip variant="accent">成功</Chip>
        )}
      </div>
      {call.input_preview || call.result_preview ? (
        <details style={{ marginTop: 4 }}>
          <summary className="text-xs dim" style={{ cursor: 'pointer' }}>
            参数与结果
          </summary>
          {call.input_preview ? (
            <>
              <div className="text-xs dim" style={{ marginTop: 4 }}>
                参数{call.input_truncated ? '（过长，只记录了开头）' : ''}
              </div>
              <pre className="code-block">{call.input_preview}</pre>
            </>
          ) : null}
          {call.result_preview ? (
            <>
              <div className="text-xs dim" style={{ marginTop: 4 }}>
                结果{call.result_truncated ? '（过长，只记录了开头）' : ''}
              </div>
              <pre className="code-block">{call.result_preview}</pre>
            </>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}

export function ArtifactText({ taskId, artifactId }: { taskId: string; artifactId: string }): JSX.Element {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ArtifactContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || data !== null) return;
    let cancelled = false;
    taskApi
      .artifactContent(taskId, artifactId)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [open, data, taskId, artifactId]);

  return (
    <div>
      <button type="button" className="btn btn--xs" onClick={() => setOpen((v) => !v)}>
        {open ? '收起' : `查看产物 ${artifactId.slice(0, 8)} 的正文`}
      </button>
      {open ? (
        error ? (
          <div className="field__error" style={{ marginTop: 4 }}>
            {error}
          </div>
        ) : data === null ? (
          <div className="text-xs dim" style={{ marginTop: 4 }}>
            读取中…
          </div>
        ) : (
          <>
            {data.truncated ? (
              <div className="text-xs dim" style={{ marginTop: 4 }}>
                内容过长，只显示前一部分（共 {data.size_bytes ?? '未知'} 字节）。
              </div>
            ) : null}
            <pre className="code-block" style={{ marginTop: 4 }}>
              {data.text}
            </pre>
          </>
        )
      ) : null}
    </div>
  );
}
