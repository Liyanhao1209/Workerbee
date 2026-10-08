/**
 * 流程列表（`/workflows`）。
 *
 * 三条纪律：
 * 1. **「运行中任务数」是聚合出来的事实，不是猜的。** 内核不提供该字段，这里拉一次
 *    任务列表，按 `workflow_id` 统计**非终态**（终态 = succeeded / failed / cancelled）
 *    的任务数。任务列表取不到时该列显示「未知」——**未知不是 0**（OBS-04）。
 * 2. **列表拉不到就不是空列表。** 内核不可达与「一个流程都没有」是两件事，
 *    错误态绝不渲染一张空表。
 * 3. **删除的三个结论相互独立**（LIFE-06）：把内核返回的 `WorkflowDeleteResponse`
 *    原样交给 TriState，不合并成一个「删除成功」。
 *
 * 表单只提交用户真正填写的字段：并发上限留空即「不改动 / 交给内核默认」，前端不猜默认值。
 */

import { useCallback, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { TaskState, WorkflowDefinition, WorkflowDeleteResponse } from '../api/types';
import { tasks as taskApi, workflows as workflowApi } from '../api/endpoints';
import { asArray, asString, asTaskState, isRecord } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { useWorkspace } from '../store/workspace';
import {
  Banner,
  Chip,
  Empty,
  Field,
  Loading,
  Modal,
  Pill,
  RelTime,
  ShortId,
} from '../components/common';
import { TriState } from '../components/TriState';
import { WORKFLOW_STATUS_LABELS } from '../labels';

/** 终态：到达后不再有后续执行。其余状态（排队中、受阻、核对中…）都算「未结束」。 */
const TERMINAL_TASK_STATES: readonly TaskState[] = ['succeeded', 'failed', 'cancelled'];

interface TaskTally {
  /** 未结束（非终态）的任务数。 */
  live: number;
  /** 该流程的任务总数。 */
  total: number;
}

/** 只取聚合需要的两个字段，避免把整个 Task 搬进渲染层。 */
interface TaskStub {
  workflow_id: string;
  observed_state: TaskState;
}

function normalizeTask(raw: unknown): TaskStub | null {
  if (!isRecord(raw)) return null;
  const workflowId = asString(raw['workflow_id']);
  if (!workflowId) return null;
  // 状态缺失/不识别时 asTaskState 回落到 blocked（非终态）——宁可多算一个「未结束」，
  // 也不能把一个可能还在跑的任务算成已结束。
  return { workflow_id: workflowId, observed_state: asTaskState(raw['observed_state']) };
}

function tallyTasks(tasks: TaskStub[]): Map<string, TaskTally> {
  const map = new Map<string, TaskTally>();
  for (const task of tasks) {
    const prev = map.get(task.workflow_id) ?? { live: 0, total: 0 };
    const terminal = TERMINAL_TASK_STATES.includes(task.observed_state);
    map.set(task.workflow_id, {
      live: prev.live + (terminal ? 0 : 1),
      total: prev.total + 1,
    });
  }
  return map;
}

type FormTarget = { mode: 'create' } | { mode: 'edit'; workflow: WorkflowDefinition };

export function WorkflowListPage(): JSX.Element {
  const navigate = useNavigate();
  // 顶栏的工作区筛选：'' 表示全部（不往后端发这个参数）。
  const currentWs = useWorkspace((s) => s.currentId);
  const wsParam = currentWs === '' ? undefined : currentWs;

  const fetchWorkflows = useCallback(
    (_signal: AbortSignal) => workflowApi.list({ workspace_id: wsParam }),
    [wsParam],
  );
  const { data, loading, error, loaded, reload } = useAsync(fetchWorkflows, [wsParam]);

  const fetchTasks = useCallback(
    (_signal: AbortSignal) => taskApi.list({ workspace_id: wsParam }),
    [wsParam],
  );
  const { data: taskPage, error: taskError, reload: reloadTasks } = useAsync(fetchTasks, [wsParam]);

  const [form, setForm] = useState<FormTarget | null>(null);
  const [deleting, setDeleting] = useState<WorkflowDefinition | null>(null);
  const [deleteResult, setDeleteResult] = useState<WorkflowDeleteResponse | null>(null);
  const remove = useSubmit();

  const workflows = useMemo(
    () => (data ? asArray<WorkflowDefinition>(data.workflows) : []),
    [data],
  );

  const tallies = useMemo(() => {
    if (!taskPage) return new Map<string, TaskTally>();
    const rows = asArray<unknown>(taskPage.tasks)
      .map(normalizeTask)
      .filter((t): t is TaskStub => t !== null);
    return tallyTasks(rows);
  }, [taskPage]);

  const refreshAll = useCallback((): void => {
    reload();
    reloadTasks();
  }, [reload, reloadTasks]);

  function closeDelete(): void {
    setDeleting(null);
    setDeleteResult(null);
    remove.clear();
  }

  async function runDelete(): Promise<void> {
    const target = deleting;
    if (!target) return;
    const result = await remove.run(() => workflowApi.remove(target.workflow_id));
    if (result) {
      setDeleteResult(result);
      // 立刻重取列表：以内核的事实为准，不做「本地先移除」的乐观假设。
      reload();
    }
  }

  function handleSaved(saved: WorkflowDefinition): void {
    const creating = form?.mode === 'create';
    setForm(null);
    if (creating && saved.workflow_id) {
      // 新建后直接进编辑器，省掉一次「再点一次」。
      navigate(`/workflows/${encodeURIComponent(saved.workflow_id)}`);
      return;
    }
    // 拿不到 id 就不跳（宁可在列表里刷新，也不要跳到 /workflows/undefined）。
    reload();
  }

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>流程</h1>
          <div className="page-head__sub">
            流程定义任务按什么步骤执行。点名称进入编辑器修改流程图；删除流程不会删除历史任务记录。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={refreshAll} disabled={loading}>
            刷新
          </button>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            onClick={() => setForm({ mode: 'create' })}
          >
            新建流程
          </button>
        </div>
      </div>

      {error ? (
        error.unreachable ? (
          <Banner
            variant="danger"
            title="无法连接后台服务"
            hint={error.hint}
            actions={
              <button type="button" className="btn btn--sm" onClick={reload}>
                重试
              </button>
            }
          >
            <span className="mono text-xs">{error.detail}</span>
          </Banner>
        ) : (
          <Banner
            variant="danger"
            title="无法获取流程列表"
            hint={error.hint}
            actions={
              <button type="button" className="btn btn--sm" onClick={reload}>
                重试
              </button>
            }
          >
            <span className="mono text-xs">{error.detail}</span>
          </Banner>
        )
      ) : !loaded && loading ? (
        <Loading label="加载流程列表" />
      ) : (
        <div className="panel">
          <div className="panel__head">
            流程
            {workflows.length > 0 ? <span className="dim text-xs">{workflows.length} 个</span> : null}
            <div className="panel__head-actions">
              <button type="button" className="btn btn--xs" onClick={refreshAll} disabled={loading}>
                刷新
              </button>
            </div>
          </div>

          {taskError ? (
            <div className="panel__hint panel__hint--warn">
              <span className="row row--tight">
                <span>任务列表获取失败（{taskError.detail}），「运行中/总任务」显示为未知。</span>
                <button type="button" className="btn btn--xs" onClick={reloadTasks}>
                  重试
                </button>
              </span>
            </div>
          ) : taskPage && taskPage.has_more ? (
            <div className="panel__hint">
              任务列表未取全（本次返回 {taskPage.returned} 条，仍有更多）：
              「运行中/总任务」只统计本次返回的这部分任务。
            </div>
          ) : null}

          <div className="panel__body panel__body--flush">
            {workflows.length === 0 ? (
              <Empty
                title="还没有流程"
                hint="流程由节点和连线组成。新建后在编辑器里添加节点并发布，然后才能提交任务。"
                action={
                  <button
                    type="button"
                    className="btn btn--sm btn--primary"
                    onClick={() => setForm({ mode: 'create' })}
                  >
                    新建流程
                  </button>
                }
              />
            ) : (
              <div className="table-wrap">
                <table className="table table--dense">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>状态</th>
                      <th>描述</th>
                      <th className="table__num" title="当前版本号，每次在编辑器中保存修改后递增">
                        版本
                      </th>
                      <th className="table__num" title="该流程同时运行的任务数上限">
                        并发上限
                      </th>
                      <th
                        className="table__num"
                        title="正在运行的任务数 / 该流程的任务总数"
                      >
                        运行中/总任务
                      </th>
                      <th>更新时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {workflows.map((wf) => {
                      const statusLabel = WORKFLOW_STATUS_LABELS[wf.status];
                      const tally = tallies.get(wf.workflow_id);
                      const live = tally ? tally.live : 0;
                      const total = tally ? tally.total : 0;
                      return (
                        <tr key={wf.workflow_id}>
                          <td>
                            <Link to={`/workflows/${encodeURIComponent(wf.workflow_id)}`}>
                              {wf.name || '（未命名）'}
                            </Link>
                            <div className="table__id" title={wf.workflow_id}>
                              {wf.workflow_id.slice(0, 8)}
                            </div>
                          </td>
                          <td>
                            <Pill tone={statusLabel.tone}>{statusLabel.text}</Pill>
                          </td>
                          <td>
                            {wf.description ? (
                              <div className="truncate" style={{ maxWidth: 320 }} title={wf.description}>
                                {wf.description}
                              </div>
                            ) : (
                              <span className="dim">—</span>
                            )}
                          </td>
                          <td className="table__num">
                            <span className="mono">{wf.current_revision_seq}</span>
                          </td>
                          <td className="table__num">
                            <span className="mono">{wf.max_concurrent_tasks}</span>
                          </td>
                          <td className="table__num">
                            {taskError ? (
                              <span className="dim" title={`任务列表获取失败：${taskError.detail}`}>
                                未知
                              </span>
                            ) : !taskPage ? (
                              <span className="dim" title="任务列表加载中">
                                …
                              </span>
                            ) : (
                              <span className="mono" title={`正在运行 ${live} 个，共 ${total} 个任务`}>
                                {live} / {total}
                              </span>
                            )}
                          </td>
                          <td>
                            <RelTime value={wf.updated_at} />
                          </td>
                          <td>
                            <div className="row row--tight">
                              <button
                                type="button"
                                className="btn btn--xs"
                                onClick={() =>
                                  navigate(`/workflows/${encodeURIComponent(wf.workflow_id)}`)
                                }
                              >
                                打开编辑器
                              </button>
                              <button
                                type="button"
                                className="btn btn--xs"
                                title="打开该流程的提交面板"
                                onClick={() =>
                                  navigate(`/workflows/${encodeURIComponent(wf.workflow_id)}?submit=1`)
                                }
                              >
                                提交任务
                              </button>
                              <button
                                type="button"
                                className="btn btn--xs btn--ghost"
                                onClick={() => setForm({ mode: 'edit', workflow: wf })}
                              >
                                重命名
                              </button>
                              <button
                                type="button"
                                className="btn btn--xs btn--danger"
                                onClick={() => {
                                  remove.clear();
                                  setDeleteResult(null);
                                  setDeleting(wf);
                                }}
                              >
                                删除
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {form ? (
        <WorkflowFormModal
          mode={form.mode}
          workflow={form.mode === 'edit' ? form.workflow : null}
          createWorkspaceId={wsParam}
          onClose={() => setForm(null)}
          onSaved={handleSaved}
        />
      ) : null}

      {deleting ? (
        <Modal
          title={
            deleteResult ? '删除结果' : `删除流程「${deleting.name || deleting.workflow_id.slice(0, 8)}」`
          }
          onClose={closeDelete}
          wide={deleteResult !== null}
          footer={
            deleteResult ? (
              <button type="button" className="btn btn--primary" onClick={closeDelete}>
                完成
              </button>
            ) : (
              <>
                <button type="button" className="btn" onClick={closeDelete}>
                  取消
                </button>
                <button
                  type="button"
                  className="btn btn--danger"
                  disabled={remove.busy}
                  onClick={() => void runDelete()}
                >
                  {remove.busy ? '删除中…' : '确认删除'}
                </button>
              </>
            )
          }
        >
          {deleteResult ? (
            <DeleteResultBody result={deleteResult} />
          ) : (
            <div className="col">
              <Banner variant="danger" title="删除后立即生效">
                删除后将不再接受新任务，正在运行的任务会被终止；流程定义和历史任务记录会保留，
                仍可查看；被其他流程共用的配置不会被删除。
              </Banner>
              <div className="text-sm">
                即将删除：<strong>{deleting.name || '（未命名）'}</strong>{' '}
                <span className="table__id">{deleting.workflow_id}</span>
              </div>
              {deleting.status === 'published' ? (
                <div className="text-sm muted">
                  该流程当前状态为「已发布」，可能仍有任务在跑，它们会被一并终止。
                </div>
              ) : null}
              {remove.error ? (
                <Banner
                  variant="danger"
                  title={remove.error.unreachable ? '无法连接后台服务' : '删除失败'}
                  hint={remove.error.hint}
                >
                  <span className="mono text-xs">{remove.error.detail}</span>
                </Banner>
              ) : null}
            </div>
          )}
        </Modal>
      ) : null}
    </div>
  );
}

/**
 * 删除结果：三个独立结论 + 两类受影响对象。
 * 名单缺失时按「空」渲染，不编造内容；结论以 TriState 为准。
 */
function DeleteResultBody({ result }: { result: WorkflowDeleteResponse }): JSX.Element {
  const terminated = asArray<string>(result.tasks_terminated);
  const kept = asArray<string>(result.shared_configs_kept);
  return (
    <div className="col">
      <Banner variant="info" title="删除结果分三步确认">
        ① 删除请求是否被接受；② 正在运行的任务是否已停止；③ 相关资源是否已清理。
        哪一步没有完成，下面会标出来。
      </Banner>
      <TriState outcome={result} />
      <div>
        <div className="section-title">已终止的任务（{terminated.length}）</div>
        {terminated.length === 0 ? (
          <span className="dim text-sm">没有正在运行的任务需要终止</span>
        ) : (
          <div className="chips">
            {terminated.map((taskId) => (
              <Chip key={taskId} title={taskId}>
                <ShortId id={taskId} />
              </Chip>
            ))}
          </div>
        )}
      </div>
      <div>
        <div className="section-title">保留的共享配置（{kept.length}）</div>
        {kept.length === 0 ? (
          <span className="dim text-sm">没有被其他流程共用的配置</span>
        ) : (
          <div className="chips">
            {kept.map((ref) => (
              <Chip key={ref} variant="off" title={ref}>
                <ShortId id={ref} />
              </Chip>
            ))}
          </div>
        )}
        <div className="text-xs muted" style={{ marginTop: 'var(--sp-1)' }}>
          这些配置仍被其他流程使用，因此保留。
        </div>
      </div>
    </div>
  );
}

/**
 * 新建 / 重命名共用表单。
 *
 * - `mode === 'create'` 打 POST /api/workflows，成功后由调用方跳进编辑器；
 * - `mode === 'edit'` 打 PATCH，只提交用户改动过的字段（留空 = 不改动）。
 */
function WorkflowFormModal({
  mode,
  workflow,
  createWorkspaceId,
  onClose,
  onSaved,
}: {
  mode: 'create' | 'edit';
  workflow: WorkflowDefinition | null;
  /** 新建时归入的工作区（顶栏筛选的当前值）；undefined = 默认工作区。 */
  createWorkspaceId?: string;
  onClose: () => void;
  onSaved: (saved: WorkflowDefinition) => void;
}): JSX.Element {
  const isEdit = mode === 'edit';
  const workflowId = workflow ? workflow.workflow_id : null;

  const [name, setName] = useState(workflow ? workflow.name : '');
  const [description, setDescription] = useState(workflow?.description ?? '');
  const [maxConcurrent, setMaxConcurrent] = useState(
    workflow ? String(workflow.max_concurrent_tasks) : '',
  );
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmit();

  async function run(): Promise<void> {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setFormError('名称不能为空');
      return;
    }
    const rawMax = maxConcurrent.trim();
    let parsedMax: number | null = null;
    if (rawMax !== '') {
      const value = Number(rawMax);
      if (!Number.isInteger(value) || value < 1) {
        setFormError('并发上限必须是 ≥ 1 的整数');
        return;
      }
      parsedMax = value;
    }
    setFormError(null);

    const body: {
      name: string;
      description: string | null;
      max_concurrent_tasks?: number;
      workspace_id?: string;
    } = {
      name: trimmedName,
      description: description.trim() === '' ? null : description.trim(),
    };
    if (parsedMax !== null) body.max_concurrent_tasks = parsedMax;
    if (workflowId === null && createWorkspaceId) body.workspace_id = createWorkspaceId;

    const saved = await submit.run(() =>
      workflowId === null ? workflowApi.create(body) : workflowApi.patch(workflowId, body),
    );
    if (saved) onSaved(saved);
  }

  return (
    <Modal
      title={isEdit ? '重命名流程' : '新建流程'}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={submit.busy}
            onClick={() => void run()}
          >
            {submit.busy ? '提交中…' : isEdit ? '保存修改' : '创建并打开编辑器'}
          </button>
        </>
      }
    >
      <div className="col">
        <Field label="名称" hint="显示在流程列表、任务列表与审批记录里">
          <input
            className="input"
            value={name}
            autoFocus
            placeholder="例如：周报生成"
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label="描述" hint={isEdit ? '留空表示清除描述' : '可选，写给协作者看的说明'}>
          <textarea
            className="textarea"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <Field
          label="并发上限"
          hint={
            isEdit
              ? '留空表示不修改。限制该流程同时运行的任务数，超出部分排队'
              : '留空表示使用默认值。限制该流程同时运行的任务数，超出部分排队等待'
          }
        >
          <input
            className="input input--num"
            type="number"
            min={1}
            step={1}
            value={maxConcurrent}
            onChange={(e) => setMaxConcurrent(e.target.value)}
          />
        </Field>
        {formError ? <span className="text-danger text-sm">{formError}</span> : null}
        {submit.error ? (
          <Banner
            variant="danger"
            title={submit.error.unreachable ? '无法连接后台服务' : isEdit ? '保存失败' : '创建失败'}
            hint={submit.error.hint}
          >
            <span className="mono text-xs">{submit.error.detail}</span>
          </Banner>
        ) : null}
      </div>
    </Modal>
  );
}
