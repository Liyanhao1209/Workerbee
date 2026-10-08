/**
 * 拓扑编辑器（核心页面，WF-01 / WF-02 / ACT-01–04 / D-02）。
 *
 * 三张图的纪律在这里体现得最直接：
 * - 画布上编辑的是**定义图 G₀**（用户保存的事实源）；
 * - 有效图 G_eff 由内核按启停派生，**不在这张画布上画可编辑的边**——
 *   要看有效边请走「启停预览」，那里是只读的差异视图；
 * - 本次执行记录是任务发射时 pinned 的快照，编辑它不影响在途任务。
 *
 * 保存走乐观并发：每次提交都带 `base_revision_seq`，冲突（409）时把
 * **服务端最新版 + 双方 diff** 摆给用户决定，而不是覆盖或悄悄重试。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import type {
  ConflictResponse,
  GraphSpec,
  NodeDefinition,
  ValidationMode,
  ValidationReport,
  WorkflowRevision,
} from '../api/types';
import { registry as registryApi, workflows as workflowApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { emptyGraph, emptyGraphSpec, newLocalId } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { useWorkspace } from '../store/workspace';
import { CanvasFocus, WorkflowCanvas } from '../graph/WorkflowCanvas';
import { NodeInspector } from '../components/NodeInspector';
import { DiagnosticsGrouped, type DiagnosticTarget } from '../components/Diagnostics';
import { ToggleFlow } from '../components/ToggleFlow';
import { SubmitTaskPanel } from '../components/SubmitTaskPanel';
import { Banner, Chip, Empty, Loading, Modal, Pill, RelTime, TimeText } from '../components/common';
import { REVISION_SOURCE_LABELS, WORKFLOW_STATUS_LABELS } from '../labels';

type RightTab = 'node' | 'revisions';

interface ToggleAsk {
  nodeId: string;
  name: string;
  enabling: boolean;
}

export function WorkflowEditorPage(): JSX.Element {
  const { workflowId = '' } = useParams<{ workflowId: string }>();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  // ------------------------------ 数据 ------------------------------
  const fetchWorkflow = useCallback(() => workflowApi.get(workflowId), [workflowId]);
  const fetchRevisions = useCallback(() => workflowApi.revisions(workflowId), [workflowId]);
  const fetchHarnesses = useCallback(() => registryApi.harnesses(), []);
  const fetchCredentials = useCallback(() => registryApi.credentials(), []);
  const fetchSkills = useCallback(() => registryApi.skills(), []);
  const fetchTools = useCallback(() => registryApi.tools(), []);

  const workflow = useAsync(fetchWorkflow, [workflowId], { pollMs: 20000 });
  const revisions = useAsync(fetchRevisions, [workflowId], { pollMs: 30000 });
  const harnesses = useAsync(fetchHarnesses, []);
  const credentials = useAsync(fetchCredentials, []);
  const skills = useAsync(fetchSkills, []);
  const tools = useAsync(fetchTools, []);
  const save = useSubmit();

  // 所属工作区（v0.03 §3）。工作区列表还没读到时按 id 前 8 位如实显示，不猜名字。
  const workspaceList = useWorkspace((s) => s.workspaces);
  const workspaceName = useMemo(() => {
    const id = workflow.data?.workspace_id;
    if (!id) return null;
    return workspaceList.find((w) => w.workspace_id === id)?.name ?? `工作区 ${id.slice(0, 8)}`;
  }, [workspaceList, workflow.data?.workspace_id]);

  // ------------------------------ 本地草稿 ------------------------------
  const [draft, setDraft] = useState<GraphSpec | null>(null);
  const [baseSeq, setBaseSeq] = useState<number | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [tab, setTab] = useState<RightTab>('node');
  const [focus, setFocus] = useState<CanvasFocus | null>(null);
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [validateMode, setValidateMode] = useState<ValidationMode>('publish');
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [validating, setValidating] = useState(false);
  /** 用户填写的修订说明，随保存一起提交（与下面那条「结果提示」是两回事）。 */
  const [noteInput, setNoteInput] = useState('');
  /** 保存/校验的结果提示，只用于显示。 */
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ConflictResponse | null>(null);
  const [toggleAsk, setToggleAsk] = useState<ToggleAsk | null>(null);
  const [lastToggleNote, setLastToggleNote] = useState<string | null>(null);

  /** 当前基点修订：以流程声明的 current_revision_seq 为准，取不到再退到最大序号。 */
  const baseRevision = useMemo(() => {
    const list = revisions.data?.revisions ?? [];
    if (list.length === 0) return null;
    const seq = workflow.data?.current_revision_seq;
    const exact = seq !== undefined ? list.find((r) => r.revision_seq === seq) : undefined;
    if (exact) return exact;
    return list.reduce((acc, r) => (acc === null || r.revision_seq > acc.revision_seq ? r : acc), null as null | (typeof list)[number]);
  }, [revisions.data, workflow.data]);

  /** 新建流程的合法初态：服务端还没有任何修订。 */
  const isNewWorkflow =
    workflow.data?.current_revision_seq === 0 &&
    revisions.loaded &&
    !revisions.error &&
    revisions.data?.revisions.length === 0;

  const baseGraph = useMemo<GraphSpec | null>(() => {
    if (baseRevision) return emptyGraphSpec(baseRevision.graph);
    return isNewWorkflow ? emptyGraph() : null;
  }, [baseRevision, isNewWorkflow]);

  /**
   * 初始化只做一次：把服务端当前修订读进画布。
   *
   * **之后服务端再出新修订也不覆盖画布**——静默重置会丢掉用户正在做的编辑。
   * 服务端更新如实提示，用户自己决定是继续编辑还是载入新版。
   */
  const [initialized, setInitialized] = useState(false);
  useEffect(() => {
    if (initialized || !workflow.data || !revisions.loaded || revisions.error) return;
    if (baseRevision) {
      setDraft(emptyGraphSpec(baseRevision.graph));
      setBaseSeq(baseRevision.revision_seq);
    } else if (isNewWorkflow) {
      setDraft(emptyGraph());
      setBaseSeq(0);
    } else {
      return;
    }
    setInitialized(true);
  }, [initialized, workflow.data, revisions.loaded, revisions.error, baseRevision, isNewWorkflow]);

  /** 服务端已经有比编辑基点更新的修订（不自动同步，只提示）。 */
  const serverNewer = useMemo(() => {
    if (baseSeq === null || !baseRevision) return null;
    if (baseRevision.revision_seq <= baseSeq) return null;
    return baseRevision;
  }, [baseRevision, baseSeq]);

  const dirty = useMemo(() => {
    if (!draft || !baseGraph) return false;
    return JSON.stringify(draft) !== JSON.stringify(baseGraph);
  }, [draft, baseGraph]);

  const selectedNode = useMemo(
    () => draft?.nodes.find((n) => n.node_id === selectedNodeId) ?? null,
    [draft, selectedNodeId],
  );

  const enabledCount = draft?.nodes.filter((n) => n.enabled).length ?? 0;
  const disabledCount = (draft?.nodes.length ?? 0) - enabledCount;

  // ------------------------------ 动作 ------------------------------
  const locate = useCallback((target: DiagnosticTarget) => {
    setFocus((prev) => ({
      nodeIds: target.node_id ? [target.node_id] : [],
      edges: target.edge ? [target.edge] : [],
      nonce: (prev?.nonce ?? 0) + 1,
    }));
    if (target.node_id) {
      setSelectedNodeId(target.node_id);
      setTab('node');
    }
  }, []);

  const patchGraph = useCallback((next: GraphSpec) => {
    setDraft(next);
    setReport(null);
  }, []);

  const runValidate = async (graph: GraphSpec): Promise<void> => {
    setValidating(true);
    try {
      const result = await workflowApi.validate(workflowId, { graph, mode: validateMode });
      setReport(result);
      const errors = result.diagnostics.filter((d) => d.severity === 'error');
      // 校验结论不自动跳视口：只高亮第一条错误，避免用户没点就乱跳。
      if (errors.length > 0) {
        const first = errors[0];
        if (first && (first.node_id || first.edge)) {
          setFocus((prev) => ({
            nodeIds: first.node_id ? [first.node_id] : [],
            edges: first.edge ? [first.edge] : [],
            nonce: (prev?.nonce ?? 0) + 1,
          }));
        }
      }
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      setReport(null);
      setSaveNote(
        apiError
          ? apiError.unreachable
            ? '无法连接内核，校验没有执行。'
            : `校验请求失败：${apiError.detail}`
          : '校验请求失败。',
      );
    } finally {
      setValidating(false);
    }
  };

  const commit = async (publish: boolean): Promise<void> => {
    if (!draft) return;
    setSaveNote(null);
    const result = await save.run(() =>
      workflowApi.saveRevision(workflowId, {
        graph: draft,
        publish,
        base_revision_seq: baseSeq,
        note: noteInput.trim() ? noteInput.trim() : null,
      }),
    );
    if (result) {
      setBaseSeq(result.revision_seq);
      setSaveNote(
        publish
          ? `已发布修订 #${result.revision_seq}。正在运行的任务不受影响，仍按各自启动时的版本执行。`
          : `已保存草稿修订 #${result.revision_seq}，尚未发布。`,
      );
      revisions.reload();
      workflow.reload();
      return;
    }
    const err = save.error;
    if (err && err.kind === 'conflict') {
      const detail = err.asConflict();
      if (detail) {
        setConflict(detail);
        revisions.reload();
        return;
      }
      setSaveNote('保存冲突（409）：服务端已有更新的修订，但响应里没有可解析的差异信息。请刷新后重试。');
    }
  };

  const openSubmit = (): void => {
    const next = new URLSearchParams(params);
    next.set('submit', '1');
    setParams(next, { replace: true });
  };
  const closeSubmit = (): void => {
    const next = new URLSearchParams(params);
    next.delete('submit');
    setParams(next, { replace: true });
  };

  // ------------------------------ 渲染 ------------------------------
  if (workflow.loading && !workflow.loaded) return <Loading label="加载流程" />;

  if (workflow.error) {
    return (
      <div className="page">
        <Banner
          variant="danger"
          title={workflow.error.unreachable ? '无法连接内核' : '无法读取流程'}
          hint={
            workflow.error.unreachable
              ? '流程定义没有读到，页面不显示任何推测内容。内核恢复后点重试即可。'
              : undefined
          }
          actions={
            <>
              <button type="button" className="btn btn--sm" onClick={workflow.reload}>
                重试
              </button>
              <button type="button" className="btn btn--sm" onClick={() => navigate('/workflows')}>
                返回流程列表
              </button>
            </>
          }
        >
          {workflow.error.detail}
        </Banner>
      </div>
    );
  }

  if (!draft) {
    if (revisions.error || (revisions.loaded && !baseRevision && !isNewWorkflow)) {
      return (
        <div className="page">
          <Banner
            variant="danger"
            title="无法读取流程修订"
            actions={<button type="button" className="btn btn--sm" onClick={revisions.reload}>重试</button>}
          >
            {revisions.error?.detail ?? '流程的当前修订不可用，请重试。'}
          </Banner>
        </div>
      );
    }
    return (
      <div className="page">
        <Loading label="准备画布" />
      </div>
    );
  }

  const registryError =
    harnesses.error?.detail ??
    credentials.error?.detail ??
    skills.error?.detail ??
    tools.error?.detail ??
    null;

  return (
    <div className="page page--flush" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* ------------------------------ 头部 ------------------------------ */}
      <div style={{ padding: 'var(--sp-3) var(--sp-4) 0', flex: '0 0 auto' }}>
        <div className="page-head" style={{ marginBottom: 'var(--sp-2)' }}>
          <div className="page-head__titles">
            <h1 className="row row--tight" style={{ gap: 'var(--sp-2)' }}>
              {workflow.data?.name ?? '（流程）'}
              {workflow.data ? (
                <Pill tone={WORKFLOW_STATUS_LABELS[workflow.data.status].tone}>
                  {WORKFLOW_STATUS_LABELS[workflow.data.status].text}
                </Pill>
              ) : null}
              {dirty ? <span className="chip chip--warn">有未保存的改动</span> : null}
            </h1>
            <div className="page-head__sub">
              {workspaceName ? <>{workspaceName}{' · '}</> : null}
              修订 #{baseSeq ?? '—'}
              {baseRevision ? (
                <>
                  {' · '}
                  {REVISION_SOURCE_LABELS[baseRevision.source]}
                  {' · 图版本 '}
                  {baseRevision.effective_graph_version}
                  {baseRevision.is_published ? ' · 已发布版本' : ' · 草稿版本（未发布）'}
                </>
              ) : (
                ' · 尚无修订：这里的保存会创建第一个修订'
              )}
              {' · 节点 '}
              {draft.nodes.length}（启用 {enabledCount} / 停用 {disabledCount}） · 依赖边 {draft.edges.length}
            </div>
          </div>
          <div className="page-head__actions">
            <select
              className="select select--sm"
              value={validateMode}
              onChange={(e) => setValidateMode(e.target.value === 'draft' ? 'draft' : 'publish')}
              title="草稿校验：只检查这一版能不能保存；发布校验：按能运行的标准检查（更严格）"
            >
              <option value="draft">草稿校验（能否保存）</option>
              <option value="publish">发布校验（能否运行）</option>
            </select>
            <button
              type="button"
              className="btn btn--sm"
              disabled={validating}
              onClick={() => void runValidate(draft)}
            >
              {validating ? '校验中…' : '校验'}
            </button>
            <button type="button" className="btn btn--sm" onClick={() => setLayoutNonce((n) => n + 1)}>
              自动布局
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => {
                const id = newLocalId();
                const node: NodeDefinition = {
                  node_id: id,
                  name: '新节点',
                  role: null,
                  description: null,
                  enabled: true,
                  system_prompt: null,
                  profiles: [],
                  skill_refs: [],
                  tool_refs: [],
                  required_inputs: [],
                  ui_position: null,
                };
                patchGraph({ ...draft, nodes: [...draft.nodes, node] });
                setSelectedNodeId(id);
                setTab('node');
              }}
            >
              + 添加节点
            </button>
            <input
              className="input input--sm"
              style={{ width: 168 }}
              placeholder="修订说明（可选）"
              value={noteInput}
              onChange={(e) => setNoteInput(e.target.value)}
              title="会写进这次修订的备注，供日后回看"
            />
            <button
              type="button"
              className="btn btn--sm"
              disabled={save.busy || !dirty}
              onClick={() => void commit(false)}
              title={dirty ? '保存为新修订，不发布' : '没有未保存的改动'}
            >
              保存草稿
            </button>
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={save.busy || !dirty}
              onClick={() => void commit(true)}
            >
              发布
            </button>
            <button type="button" className="btn btn--sm" onClick={openSubmit}>
              提交任务
            </button>
          </div>
        </div>

        {workflow.data?.status === 'archived' ? (
          <Banner variant="warn" title="该流程已归档">
            归档后仍可查看和继续修改；提交新任务前请确认这是你要用的流程。
          </Banner>
        ) : null}

        {baseRevision && !baseRevision.is_published ? (
          <Banner variant="warn" title="当前修改尚未发布">
            提交任务时执行的是已发布的版本。如果这里有改动还没发布，新任务用的仍是上一个已发布版本。
          </Banner>
        ) : null}

        {serverNewer ? (
          <Banner
            variant="warn"
            title={`服务端已有更新的修订 #${serverNewer.revision_seq}`}
            actions={
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => {
                  setDraft(emptyGraphSpec(serverNewer.graph));
                  setBaseSeq(serverNewer.revision_seq);
                  setReport(null);
                  setSaveNote(`已载入服务端修订 #${serverNewer.revision_seq}，画布上的旧内容被替换。`);
                }}
              >
                载入最新版覆盖画布
              </button>
            }
            hint="画布不会自动跟随服务端，否则会丢掉正在做的编辑。"
          >
            你正在编辑的是修订 #{baseSeq}
            {serverNewer.note ? `（${serverNewer.note}）` : ''}，而服务端当前已到 #
            {serverNewer.revision_seq}。继续保存会基于 #{baseSeq} 提交；若内核判定基点已过期，会返回冲突并给出差异。
          </Banner>
        ) : null}

        {save.error && save.error.kind !== 'conflict' ? (
          <Banner
            variant="danger"
            title={save.error.unreachable ? '无法连接内核' : '保存失败'}
            hint={save.error.unreachable ? '改动还在本地，没有写入内核。不要关掉页面。' : undefined}
          >
            {save.error.detail}
          </Banner>
        ) : null}

        {saveNote ? <Banner variant={saveNote.startsWith('已') ? 'ok' : 'info'}>{saveNote}</Banner> : null}

        {lastToggleNote ? (
          <Banner variant="info" actions={<button type="button" className="btn btn--xs" onClick={() => setLastToggleNote(null)}>知道了</button>}>
            {lastToggleNote}
          </Banner>
        ) : null}
      </div>

      {/* ------------------------------ 画布 + 右栏 ------------------------------ */}
      <div
        style={{
          flex: '1 1 auto',
          minHeight: 0,
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) 372px',
          gap: 'var(--sp-3)',
          padding: 'var(--sp-3) var(--sp-4)',
        }}
      >
        <div
          style={{
            border: '1px solid var(--line)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            minHeight: 0,
            position: 'relative',
          }}
        >
          <WorkflowCanvas
            graph={draft}
            selectedNodeId={selectedNodeId}
            onSelectNode={(id) => {
              setSelectedNodeId(id);
              if (id) setTab('node');
            }}
            onGraphChange={patchGraph}
            focus={focus}
            layoutNonce={layoutNonce}
          />
          <div
            className="text-xs dim"
            style={{
              position: 'absolute',
              left: 8,
              bottom: 8,
              background: 'var(--bg-1)',
              border: '1px solid var(--line)',
              borderRadius: 'var(--radius)',
              padding: '3px 7px',
              pointerEvents: 'none',
            }}
          >
            这是流程的定义图：停用的节点仍在这里可编辑，但**不参与执行**；执行时的实际依赖按各节点的启用状态计算。
          </div>
        </div>

        <div className="panel" style={{ minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          <div className="panel__head">
            <button
              type="button"
              className={tab === 'node' ? 'btn btn--xs btn--primary' : 'btn btn--xs'}
              onClick={() => setTab('node')}
            >
              节点
            </button>
            <button
              type="button"
              className={tab === 'revisions' ? 'btn btn--xs btn--primary' : 'btn btn--xs'}
              onClick={() => setTab('revisions')}
            >
              修订历史
            </button>
            <div className="panel__head-actions">
              {selectedNode && tab === 'node' ? (
                <>
                  <button
                    type="button"
                    className={selectedNode.enabled ? 'btn btn--xs btn--danger' : 'btn btn--xs btn--primary'}
                    onClick={() =>
                      setToggleAsk({
                        nodeId: selectedNode.node_id,
                        name: selectedNode.name,
                        enabling: !selectedNode.enabled,
                      })
                    }
                    title="启停要先看影响预览，再选存量任务怎么处理"
                  >
                    {selectedNode.enabled ? '停用该节点' : '启用该节点'}
                  </button>
                  <button type="button" className="btn btn--xs" onClick={() => setSelectedNodeId(null)}>
                    返回列表
                  </button>
                </>
              ) : null}
            </div>
          </div>
          <div className="panel__body" style={{ overflow: 'auto', minHeight: 0, flex: '1 1 auto' }}>
            {tab === 'revisions' ? (
              <RevisionList
                revisions={revisions.data?.revisions ?? []}
                currentSeq={baseSeq}
                loading={revisions.loading && !revisions.loaded}
                error={revisions.error}
                onReload={revisions.reload}
                onLoadIntoDraft={(graph, seq) => {
                  setDraft(graph);
                  setReport(null);
                  setTab('node');
                  setSaveNote(
                    `已把修订 #${seq} 的内容载入画布作为编辑起点。注意：保存的基点仍是内核当前修订 #${baseSeq ?? '—'}，` +
                      '也就是说这次改动会作为最新版之上的一个新修订提交，而不会回退历史。',
                  );
                }}
              />
            ) : selectedNode ? (
              <NodeInspector
                node={selectedNode}
                onChange={(next) =>
                  patchGraph({
                    ...draft,
                    nodes: draft.nodes.map((n) => (n.node_id === next.node_id ? next : n)),
                  })
                }
                onDelete={() => {
                  patchGraph({
                    ...draft,
                    nodes: draft.nodes.filter((n) => n.node_id !== selectedNode.node_id),
                    edges: draft.edges.filter(
                      (e) => e.from_node !== selectedNode.node_id && e.to_node !== selectedNode.node_id,
                    ),
                  });
                  setSelectedNodeId(null);
                }}
                harnesses={harnesses.data ?? []}
                credentials={credentials.data ?? []}
                skills={skills.data ?? []}
                tools={tools.data ?? []}
                onCredentialsChanged={credentials.reload}
                registryError={registryError}
              />
            ) : (
              <NodeList
                graph={draft}
                onSelect={(id) => {
                  setSelectedNodeId(id);
                  setFocus((prev) => ({ nodeIds: [id], edges: [], nonce: (prev?.nonce ?? 0) + 1 }));
                }}
                onToggle={(node) =>
                  setToggleAsk({ nodeId: node.node_id, name: node.name, enabling: !node.enabled })
                }
              />
            )}
          </div>
        </div>
      </div>

      {/* ------------------------------ 校验结论 ------------------------------ */}
      <div style={{ padding: '0 var(--sp-4) var(--sp-4)', flex: '0 0 auto' }}>
        <div className="panel">
          <div className="panel__head">
            校验结论
            {report ? (
              <>
                <span className="chip">
                  {report.mode === 'publish' ? '发布校验' : report.mode === 'launch' ? '提交校验' : '草稿校验'}
                </span>
                <span className="chip">
                  错误 {report.diagnostics.filter((d) => d.severity === 'error').length}
                </span>
                <span className="chip">
                  警告 {report.diagnostics.filter((d) => d.severity === 'warning').length}
                </span>
              </>
            ) : null}
            <div className="panel__head-actions">
              <button type="button" className="btn btn--xs" disabled={validating} onClick={() => void runValidate(draft)}>
                重新校验
              </button>
            </div>
          </div>
          <div className="panel__body" style={{ maxHeight: 220, overflow: 'auto' }}>
            {report ? (
              <DiagnosticsGrouped diagnostics={report.diagnostics} onLocate={locate} />
            ) : (
              <div className="empty text-sm">
                尚未校验。点「校验」检查当前草稿，结论里的每一条都可以点击定位。
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ------------------------------ 弹层 ------------------------------ */}
      {toggleAsk ? (
        <ToggleFlow
          workflowId={workflowId}
          nodeId={toggleAsk.nodeId}
          nodeName={toggleAsk.name}
          enabling={toggleAsk.enabling}
          onLocate={locate}
          onClose={() => setToggleAsk(null)}
          onApplied={(result) => {
            setLastToggleNote(
              result.awaiting_drain
                ? `节点「${toggleAsk.name}」将在正在运行的阶段跑完后正式停用。`
                : result.applied
                  ? `节点「${toggleAsk.name}」的启用状态已变更${result.new_revision_seq !== null ? `，新修订 #${result.new_revision_seq}` : ''}。`
                  : `节点「${toggleAsk.name}」的启停请求已受理，但状态尚未翻转。`,
            );
            // 内核已经写入了新修订；把本地的 enabled 也翻过来，画布才不会与事实不一致
            // （排水模式下 applied=false，说明还没翻转，本地就不动）。
            if (result.applied) {
              setDraft({ ...draft, nodes: draft.nodes.map((n) => n.node_id === toggleAsk.nodeId ? { ...n, enabled: toggleAsk.enabling } : n) });
            }
            void workflow.reload();
            void revisions.reload();
          }}
        />
      ) : null}

      {conflict ? (
        <ConflictDialog
          conflict={conflict}
          baseSeq={baseSeq}
          baseGraph={baseGraph}
          mine={draft}
          onDismiss={() => setConflict(null)}
          onTakeServer={(graph, seq) => {
            setDraft(graph);
            setBaseSeq(seq);
            setReport(null);
            setConflict(null);
            save.clear();
            setSaveNote(`已载入服务端最新修订 #${seq}，本地未保存的改动已丢弃。`);
          }}
          onRebaseAndRetry={(seq) => {
            setBaseSeq(seq);
            setConflict(null);
            save.clear();
            setSaveNote(`已把基点更新到服务端最新修订 #${seq}；请再次点「保存草稿」或「发布」提交你的改动。`);
          }}
        />
      ) : null}

      {params.get('submit') === '1' ? (
        <Modal title="提交任务" onClose={closeSubmit} wide>
          {dirty ? (
            <Banner variant="warn" title="有未保存的改动">
              提交使用的是内核里<strong>已保存</strong>的版本（修订 #{baseSeq ?? '—'}），不是这个画布上的草稿。
              要让改动生效，请先保存或发布。
            </Banner>
          ) : null}
          <SubmitTaskPanel
            workflowId={workflowId}
            graph={draft}
            onLocate={locate}
            onSubmitted={(taskId) => {
              closeSubmit();
              navigate(`/tasks/${taskId}`);
            }}
          />
        </Modal>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 节点列表
// ---------------------------------------------------------------------------

function NodeList({
  graph,
  onSelect,
  onToggle,
}: {
  graph: GraphSpec;
  onSelect: (nodeId: string) => void;
  onToggle: (node: NodeDefinition) => void;
}): JSX.Element {
  if (graph.nodes.length === 0) {
    return (
      <Empty
        title="这张图还没有节点"
        hint="用画布上方的「+ 添加节点」开始，然后连线表达依赖。孤立的节点是有效入口，会被当作起点执行。"
      />
    );
  }
  return (
    <div className="col" style={{ gap: 4 }}>
      <div className="text-xs dim">点节点名可在画布上定位；启停会先给出影响预览。</div>
      {graph.nodes.map((node) => {
        const primary = node.profiles[0];
        const outDegree = graph.edges.filter((e) => e.from_node === node.node_id).length;
        const inDegree = graph.edges.filter((e) => e.to_node === node.node_id).length;
        return (
          <div
            key={node.node_id}
            className="row row--tight"
            style={{
              border: '1px solid var(--line)',
              borderRadius: 'var(--radius)',
              background: node.enabled ? 'var(--bg-2)' : 'var(--bg-1)',
              opacity: node.enabled ? 1 : 0.65,
              padding: '5px 7px',
            }}
          >
            <button
              type="button"
              className="btn btn--xs"
              style={{ justifyContent: 'flex-start', flex: '1 1 auto', minWidth: 0 }}
              onClick={() => onSelect(node.node_id)}
              title="在画布上定位"
            >
              <span className="truncate" style={{ textDecoration: node.enabled ? undefined : 'line-through' }}>
                {node.name || '（未命名）'}
              </span>
            </button>
            <span className="text-xs dim mono">
              {primary ? `${primary.model_name}${primary.harness_ref ? ' @' + primary.harness_ref : ''}` : '无候选'}
            </span>
            <span className="text-xs dim" title="入边 / 出边">
              {inDegree}/{outDegree}
            </span>
            <button
              type="button"
              className={node.enabled ? 'btn btn--xs btn--danger' : 'btn btn--xs'}
              onClick={() => onToggle(node)}
            >
              {node.enabled ? '停用' : '启用'}
            </button>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 修订历史
// ---------------------------------------------------------------------------

function RevisionList({
  revisions,
  currentSeq,
  loading,
  error,
  onReload,
  onLoadIntoDraft,
}: {
  revisions: WorkflowRevision[];
  currentSeq: number | null;
  loading: boolean;
  error: ApiError | null;
  onReload: () => void;
  onLoadIntoDraft: (graph: GraphSpec, seq: number) => void;
}): JSX.Element {
  if (loading) return <Loading label="加载修订" />;
  if (error) {
    return (
      <Banner variant="danger" title={error.unreachable ? '无法连接内核' : '无法读取修订'}>
        {error.detail}
        <div style={{ marginTop: 6 }}>
          <button type="button" className="btn btn--sm" onClick={onReload}>
            重试
          </button>
        </div>
      </Banner>
    );
  }
  if (revisions.length === 0) {
    return <Empty title="还没有修订" hint="保存草稿会创建第一个修订。" />;
  }
  return (
    <div className="col" style={{ gap: 6 }}>
      <div className="text-xs dim">
        每次保存都会产生一个新版本，旧版本不会被修改；已在运行的任务仍按各自启动时的版本执行。
        「载入」把某一版读进画布作为新的编辑起点（不会改动服务端）。
      </div>
      {revisions.map((rev) => (
        <div
          key={rev.revision_seq}
          style={{
            border: '1px solid var(--line)',
            borderRadius: 'var(--radius)',
            background: 'var(--bg-2)',
            padding: '6px 8px',
          }}
        >
          <div className="row row--tight">
            <span className="mono">#{rev.revision_seq}</span>
            <Pill tone={rev.is_published ? 'success' : 'pending'} plain>
              {rev.is_published ? '已发布' : '草稿'}
            </Pill>
            {rev.revision_seq === currentSeq ? <Chip variant="accent">当前基点</Chip> : null}
            <span className="spacer" />
            <TimeText value={rev.created_at} />
          </div>
          <div className="text-xs dim" style={{ marginTop: 2 }}>
            {REVISION_SOURCE_LABELS[rev.source]} · 图版本 {rev.effective_graph_version} ·{' '}
            {rev.graph.nodes.length} 节点 · {rev.graph.edges.length} 边
            {rev.draft_of !== null ? ` · 基于 #${rev.draft_of}` : ''}
          </div>
          {rev.note ? <div className="text-xs muted" style={{ marginTop: 2 }}>{rev.note}</div> : null}
          <div className="row row--tight" style={{ marginTop: 4 }}>
            <button
              type="button"
              className="btn btn--xs"
              onClick={() => onLoadIntoDraft(emptyGraphSpec(rev.graph), rev.revision_seq)}
            >
              载入为编辑起点
            </button>
            <span className="text-xs dim">
              <RelTime value={rev.created_at} />
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 409 冲突：摆出双方差异让用户决定（D-02）
// ---------------------------------------------------------------------------

interface GraphDiff {
  nodesAdded: string[];
  nodesRemoved: string[];
  nodesChanged: string[];
  edgesAdded: [string, string][];
  edgesRemoved: [string, string][];
}

function diffGraphs(base: GraphSpec, other: GraphSpec): GraphDiff {
  const baseNodes = new Map(base.nodes.map((n) => [n.node_id, n]));
  const otherNodes = new Map(other.nodes.map((n) => [n.node_id, n]));
  const nodesAdded: string[] = [];
  const nodesRemoved: string[] = [];
  const nodesChanged: string[] = [];
  for (const [id, node] of otherNodes) {
    const before = baseNodes.get(id);
    if (!before) nodesAdded.push(node.name || id.slice(0, 8));
    else if (JSON.stringify(before) !== JSON.stringify(node)) nodesChanged.push(node.name || id.slice(0, 8));
  }
  for (const [id, node] of baseNodes) {
    if (!otherNodes.has(id)) nodesRemoved.push(node.name || id.slice(0, 8));
  }
  const key = (e: { from_node: string; to_node: string }): string => `${e.from_node}->${e.to_node}`;
  const baseEdges = new Set(base.edges.map(key));
  const otherEdges = new Set(other.edges.map(key));
  const edgesAdded: [string, string][] = other.edges
    .filter((e) => !baseEdges.has(key(e)))
    .map((e) => [e.from_node, e.to_node]);
  const edgesRemoved: [string, string][] = base.edges
    .filter((e) => !otherEdges.has(key(e)))
    .map((e) => [e.from_node, e.to_node]);
  return { nodesAdded, nodesRemoved, nodesChanged, edgesAdded, edgesRemoved };
}

function DiffBlock({ title, diff }: { title: string; diff: GraphDiff }): JSX.Element {
  const empty =
    diff.nodesAdded.length === 0 &&
    diff.nodesRemoved.length === 0 &&
    diff.nodesChanged.length === 0 &&
    diff.edgesAdded.length === 0 &&
    diff.edgesRemoved.length === 0;
  return (
    <div style={{ marginTop: 'var(--sp-2)' }}>
      <div className="section-title">{title}</div>
      {empty ? (
        <div className="text-sm dim">没有差异。</div>
      ) : (
        <div className="chips">
          {diff.nodesAdded.length > 0 ? <Chip variant="accent">新增节点：{diff.nodesAdded.join('、')}</Chip> : null}
          {diff.nodesRemoved.length > 0 ? <Chip variant="danger">删除节点：{diff.nodesRemoved.join('、')}</Chip> : null}
          {diff.nodesChanged.length > 0 ? <Chip variant="warn">改动节点：{diff.nodesChanged.join('、')}</Chip> : null}
          {diff.edgesAdded.map(([a, b]) => (
            <Chip key={`a${a}${b}`} variant="accent">
              新增边 {a.slice(0, 6)}→{b.slice(0, 6)}
            </Chip>
          ))}
          {diff.edgesRemoved.map(([a, b]) => (
            <Chip key={`r${a}${b}`} variant="danger">
              删除边 {a.slice(0, 6)}→{b.slice(0, 6)}
            </Chip>
          ))}
        </div>
      )}
    </div>
  );
}

function ConflictDialog({
  conflict,
  baseSeq,
  baseGraph,
  mine,
  onDismiss,
  onTakeServer,
  onRebaseAndRetry,
}: {
  conflict: ConflictResponse;
  baseSeq: number | null;
  baseGraph: GraphSpec | null;
  mine: GraphSpec;
  onDismiss: () => void;
  onTakeServer: (graph: GraphSpec, seq: number) => void;
  onRebaseAndRetry: (seq: number) => void;
}): JSX.Element {
  const latest = conflict.latest_revision;
  const latestGraph = latest ? emptyGraphSpec(latest.graph) : null;
  const myDiff = baseGraph ? diffGraphs(baseGraph, mine) : null;
  const theirDiff = baseGraph && latestGraph ? diffGraphs(baseGraph, latestGraph) : null;

  return (
    <Modal
      title="保存冲突：服务端已有更新的修订"
      onClose={onDismiss}
      wide
      footer={
        <>
          <button type="button" className="btn" onClick={onDismiss}>
            先不处理
          </button>
          <button
            type="button"
            className="btn btn--danger"
            disabled={!latest || !latestGraph}
            onClick={() => {
              if (latest && latestGraph) onTakeServer(latestGraph, latest.revision_seq);
            }}
            title="丢弃本地改动，把服务端最新版载入画布"
          >
            放弃我的改动，载入最新版
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => onRebaseAndRetry(conflict.latest_revision_seq)}
          >
            保留我的改动，基于最新版重存
          </button>
        </>
      }
    >
      <Banner variant="danger" title={conflict.detail}>
        这次保存被拒绝了，没有覆盖任何人的改动。下面是双方相对于你编辑起点（修订 #{baseSeq}）的差异。
        {conflict.hint ? <div className="banner__hint">{conflict.hint}</div> : null}
      </Banner>

      <div className="kv">
        <div className="kv__k">服务端最新修订</div>
        <div className="kv__v">
          <span className="mono">#{conflict.latest_revision_seq}</span>
          {latest ? (
            <>
              {' · '}
              {latest.is_published ? '已发布' : '草稿'}
              {' · '}
              <TimeText value={latest.created_at} />
              {latest.note ? ` · ${latest.note}` : ''}
            </>
          ) : (
            <span className="dim">（响应未附带最新修订内容）</span>
          )}
        </div>
      </div>

      {myDiff ? <DiffBlock title="你的改动（相对编辑起点）" diff={myDiff} /> : null}
      {theirDiff ? <DiffBlock title="服务端的改动（相对你的编辑起点）" diff={theirDiff} /> : null}
      {!latest ? (
        <Banner variant="warn" title="无法比较差异">
          内核返回了冲突但没带最新修订内容，这里不能推测对方改了什么。请刷新页面重新读取最新修订后再决定。
        </Banner>
      ) : null}
    </Modal>
  );
}
