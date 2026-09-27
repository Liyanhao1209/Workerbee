/**
 * 任务列表（route `/tasks`）。
 *
 * 三条纪律：
 * 1. **筛选为空就不发这个参数**（`undefined` 让 query 里干脆没有这个键），
 *    不要把空字符串当成一个筛选值发给内核。
 * 2. **失败/受阻的行必须同时给出「尚在运行的分支」**（RUN-07）：只显示失败原因
 *    会让运维以为整条任务都停了，而实际上还有分支在跑、还在烧资源。
 *    字段缺失时什么都不显示——**不显示 0**（未知 ≠ 零）。
 * 3. **连接失败不是空列表**：内核对不上就显示错误横幅 + 重试，绝不渲染一张空表
 *    冒充「没有任务」。
 */

import { useCallback, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import type { Task, TaskState, WorkflowDefinition } from '../api/types';
import { tasks as taskApi, workflows as workflowApi } from '../api/endpoints';
import { asArray } from '../api/guards';
import { useAsync } from '../hooks/useAsync';
import { Banner, Chip, Empty, Field, Loading, RelTime, ShortId, TaskStatePill } from '../components/common';
import { TASK_STATE_OPTIONS, taskStateLabel } from '../labels';

type StateFilter = TaskState | '';

/** 只接受 labels.ts 里真实存在的枚举值，避免把脏值塞进筛选。 */
function isTaskStateValue(value: string): value is TaskState {
  return TASK_STATE_OPTIONS.some((option) => option.value === value);
}

/**
 * RUN-07：失败/受阻的任务还必须显示「尚在运行的分支」。
 * `failure_summary.running_branches` 可能整个缺失——缺失时**不显示 0**，
 * 因为它本来就不是「0 个分支在跑」，而是「内核没说」。
 */
function RunningBranchesChip({ task }: { task: Task }): JSX.Element | null {
  const branches = asArray<string>(task.failure_summary?.running_branches);
  if (branches.length === 0) return null;
  return (
    <Chip variant="warn" title={`尚在运行的分支（node_id）：${branches.join('、')}`}>
      运行中分支 {branches.length}
    </Chip>
  );
}

export function TaskListPage(): JSX.Element {
  const navigate = useNavigate();
  const [stateFilter, setStateFilter] = useState<StateFilter>('');
  const [workflowFilter, setWorkflowFilter] = useState('');

  // 未设置的筛选一律传 undefined：client 的 buildUrl 会跳过 undefined/null 的键，
  // 于是 query 里只有真正生效的筛选条件。
  const fetchTasks = useCallback(
    (_signal: AbortSignal) =>
      taskApi.list({
        workflow_id: workflowFilter === '' ? undefined : workflowFilter,
        state: stateFilter === '' ? undefined : stateFilter,
      }),
    [workflowFilter, stateFilter],
  );

  // 轮询兜底：推送（WS）只是加速器，不是事实源。它断了以后这张表仍要往前走。
  const { data, loading, error, loaded, reload } = useAsync(fetchTasks, [workflowFilter, stateFilter], {
    pollMs: 5000,
  });

  const fetchWorkflows = useCallback((_signal: AbortSignal) => workflowApi.list(), []);
  const workflowList = useAsync(fetchWorkflows, []);

  const tasks = useMemo(() => asArray<Task>(data?.tasks), [data]);
  const workflows = useMemo(() => asArray<WorkflowDefinition>(workflowList.data?.workflows), [workflowList.data]);
  const workflowNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const wf of workflows) map.set(wf.workflow_id, wf.name);
    return map;
  }, [workflows]);

  const hasMore = data?.has_more === true;
  const filtering = stateFilter !== '' || workflowFilter !== '';

  const refresh = (): void => {
    reload();
    workflowList.reload();
  };

  const openTask = (taskId: string): void => {
    navigate(`/tasks/${taskId}`);
  };

  const emptyHint: ReactNode = filtering ? (
    <>
      当前筛选：
      {stateFilter === '' ? '全部状态' : taskStateLabel(stateFilter).text}
      {' · '}
      {workflowFilter === '' ? '全部流程' : `流程 ${workflowNames.get(workflowFilter) ?? workflowFilter}`}
      。换一个筛选条件试试。
    </>
  ) : (
    '内核里还没有任何提交。到「流程」页选一个流程并提交任务。'
  );

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__titles">
          <h1>任务</h1>
          <div className="page-head__sub">
            内核已接受的提交。每行显示任务 pinned 的图版本；失败或受阻的任务同时显示尚在运行的分支。
          </div>
        </div>
      </div>

      {/* ---------------- 筛选 ---------------- */}
      <div className="panel">
        <div className="panel__body">
          <div className="field-row">
            <Field label="状态">
              <select
                className="select"
                value={stateFilter}
                onChange={(e) => {
                  const next = e.target.value;
                  setStateFilter(next === '' ? '' : isTaskStateValue(next) ? next : '');
                }}
              >
                <option value="">全部状态</option>
                {TASK_STATE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.text}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="流程"
              hint={workflowList.error ? '流程列表读取失败，暂时只能按「全部流程」看' : undefined}
            >
              <select
                className="select"
                value={workflowFilter}
                onChange={(e) => setWorkflowFilter(e.target.value)}
              >
                <option value="">全部流程</option>
                {workflows.map((wf) => (
                  <option key={wf.workflow_id} value={wf.workflow_id}>
                    {wf.name}
                  </option>
                ))}
              </select>
            </Field>

            <div style={{ flex: '0 0 auto' }}>
              <button type="button" className="btn" onClick={refresh} disabled={loading && !loaded}>
                {loading && !loaded ? <span className="spin" /> : null}
                刷新
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ---------------- 列表 ---------------- */}
      <div className="panel">
        <div className="panel__head">
          任务列表
          <span className="panel__head-actions">
            {data ? <span className="text-xs muted">{tasks.length} 条</span> : null}
            {loading && loaded ? <span className="spin" title="正在刷新" /> : null}
          </span>
        </div>

        {!loaded && loading ? <Loading label="加载任务列表" /> : null}

        {error ? (
          <div className="panel__body">
            <Banner
              variant="danger"
              title={error.unreachable ? '无法连接内核' : '无法获取任务列表'}
              hint={
                data
                  ? '下表是最近一次成功读取的结果，可能已经过期。'
                  : (error.hint ?? undefined)
              }
              actions={
                <button type="button" className="btn btn--sm" onClick={refresh}>
                  重试
                </button>
              }
            >
              {error.detail}
            </Banner>
          </div>
        ) : null}

        {loaded && !error && tasks.length === 0 ? <Empty title="没有符合条件的任务" hint={emptyHint} /> : null}

        {tasks.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="table table--rows-clickable">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>流程</th>
                    <th>状态</th>
                    <th className="table__num" title="优先级：大者先派发">
                      优先级
                    </th>
                    <th className="table__num" title="提交时的流程修订号 revision_seq">
                      修订
                    </th>
                    <th title="任务 pinned 的图版本，决定它按哪一版规则运行">
                      规则版本
                    </th>
                    <th>提交时间</th>
                    <th>失败原因</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => {
                    const reason = task.failure_summary?.reason ?? task.blocked_reason;
                    return (
                      <tr
                        key={task.task_id}
                        onClick={() => openTask(task.task_id)}
                        title="点击打开任务详情"
                      >
                        <td>
                          <a
                            href={`#/tasks/${task.task_id}`}
                            onClick={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              openTask(task.task_id);
                            }}
                          >
                            <ShortId id={task.task_id} />
                          </a>
                        </td>
                        <td>
                          <div
                            className="truncate"
                            style={{ maxWidth: 220 }}
                            title={task.workflow_name ?? task.workflow_id}
                          >
                            {task.workflow_name ?? <ShortId id={task.workflow_id} />}
                          </div>
                        </td>
                        <td>
                          <div className="row row--tight">
                            <TaskStatePill state={task.observed_state} />
                            <RunningBranchesChip task={task} />
                          </div>
                        </td>
                        <td className="table__num">{task.priority}</td>
                        <td className="table__num">{task.revision_seq}</td>
                        <td>
                          <Chip title={`effective_graph_version = ${task.effective_graph_version}`}>
                            图版本 {task.effective_graph_version}
                          </Chip>
                        </td>
                        <td>
                          <RelTime value={task.created_at} />
                        </td>
                        <td>
                          {reason ? (
                            <div className="truncate text-sm" style={{ maxWidth: 260 }} title={reason}>
                              {reason}
                            </div>
                          ) : (
                            <span className="dim">—</span>
                          )}
                        </td>
                        <td>
                          <a
                            href={`#/execution/${task.task_id}`}
                            onClick={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              navigate(`/execution/${task.task_id}`);
                            }}
                          >
                            打开执行图
                          </a>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {hasMore ? (
              <div className="text-xs muted" style={{ padding: '6px var(--sp-3)' }}>
                仅显示前 {tasks.length} 条（内核分页）
              </div>
            ) : null}
          </>
        ) : null}

        <div className="panel__hint">
          规则版本指任务 pinned 的图版本，节点启停等后续修订不会改写它。运行中分支一栏为空可能是没有分支在跑，也可能是内核没有上报。
        </div>
      </div>
    </div>
  );
}
