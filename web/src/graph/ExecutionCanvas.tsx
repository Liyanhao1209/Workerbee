/**
 * 实际执行图（OBS-02）。运维时看的是这一张，不是编辑器那张。
 *
 * 三条纪律：
 * 1. **画的是钉扎快照，不是当前定义。** 任务的依赖推进以发射时钉扎的
 *    effective_edges 为准，之后用户改了图也不影响它（WF-06、REC-03）。
 *    因此这里的边来自 `graph_snapshot.effective_edges`，不是 `graph.edges`。
 * 2. **节点着色按阶段状态**，过渡态（分派中／暂停中／退避中／等待审批／核对中／
 *    状态不明）必须有别于稳定态——控制操作尚在处理中必须可见（OBS-01、AC-22）。
 * 3. 被绕过的边（via 非空）如实标注：那是 derive 的产物，不是用户直接画的依赖。
 */

import { useMemo } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge as RFEdge,
  type Node as RFNode,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import type { GraphSpec, StageState, TaskStage } from '../api/types';
import { STAGE_TONE_VAR, stageStateLabel } from '../labels';
import { NODE_W, layoutGraph } from './layout';

type RuntimeNodeData = {
  nodeId: string;
  name: string;
  role: string | null;
  enabled: boolean;
  stage: TaskStage | null;
  /** 依赖未满足但尚未创建阶段时的占位说明。 */
  placeholder: string | null;
  isRunningBranch: boolean;
  selected: boolean;
};

type RtNode = RFNode<RuntimeNodeData, 'runtime'>;

function RuntimeNode({ data }: NodeProps<RtNode>): JSX.Element {
  const state: StageState | null = data.stage?.observed_state ?? null;
  const label = state ? stageStateLabel(state) : null;
  const accent = state ? STAGE_TONE_VAR[state] : 'var(--line-strong)';
  const transitioning = label?.transitioning ?? false;

  return (
    <div
      style={{
        width: NODE_W,
        minHeight: 66,
        background: 'var(--bg-2)',
        border: `${transitioning ? 2 : 1}px ${transitioning ? 'dashed' : 'solid'} ${accent}`,
        borderRadius: 'var(--radius)',
        padding: '7px 9px',
        opacity: data.enabled ? 1 : 0.5,
        boxShadow: data.selected
          ? '0 0 0 3px rgba(77,163,255,0.25)'
          : data.isRunningBranch
            ? '0 0 0 2px rgba(41,201,138,0.28)'
            : undefined,
        cursor: 'pointer',
        position: 'relative',
      }}
      title={data.stage ? `${state} — ${label?.hint ?? ''}` : data.placeholder ?? '尚无阶段记录'}
    >
      <Handle type="target" position={Position.Left} style={{ background: 'var(--line-strong)' }} />

      <div className="row row--tight" style={{ gap: 5 }}>
        <span className="truncate" style={{ fontWeight: 600, fontSize: 'var(--fs-md)' }}>
          {data.name}
        </span>
        {!data.enabled ? <span className="chip chip--off">已停用</span> : null}
      </div>

      <div className="row row--tight" style={{ marginTop: 4, gap: 5 }}>
        {label ? (
          <span
            className={transitioning ? 'pill pill--transition' : `pill pill--${label.tone}`}
            style={{ borderColor: accent, color: accent }}
          >
            <span className="pill__dot" />
            {label.text}
          </span>
        ) : (
          <span className="pill pill--idle">
            <span className="pill__dot" />
            {data.placeholder ?? '无阶段记录'}
          </span>
        )}
      </div>

      {/* 过渡态必须说明「在处理什么」，而不只是换个颜色 */}
      {transitioning && data.stage?.status_reason ? (
        <div className="text-xs" style={{ marginTop: 3, color: 'var(--st-transition)' }}>
          {data.stage.status_reason}
        </div>
      ) : null}

      {data.stage && (data.stage.observed_state === 'failed' || data.stage.observed_state === 'blocked') ? (
        <div className="text-xs truncate" style={{ marginTop: 3, color: 'var(--st-danger)' }}>
          {data.stage.blocked_reason ?? '（内核未提供原因）'}
        </div>
      ) : null}

      {data.stage && data.stage.attempt_count > 1 ? (
        <div className="text-xs dim" style={{ marginTop: 2 }}>
          第 {data.stage.attempt_count} 次尝试
          {data.stage.current_attempt_seq > 0 ? ` · 候选游标 ${data.stage.profile_cursor}` : ''}
        </div>
      ) : null}

      <Handle type="source" position={Position.Right} style={{ background: 'var(--line-strong)' }} />
    </div>
  );
}

const nodeTypes = { runtime: RuntimeNode };

export interface ExecutionCanvasProps {
  /** 钉扎的定义图（含节点名与 enabled 快照）。 */
  pinnedGraph: GraphSpec;
  /** 钉扎的有效边集：真正决定依赖推进的那一份。 */
  effectiveEdges: [string, string][];
  stages: TaskStage[];
  /** 任务当前失败时，仍在运行的分支（RUN-07 要求同时显示）。 */
  runningBranchNodeIds?: string[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  /** 编辑器中当前定义图的边集，用来标注「这条有效边是绕过产生的」。 */
  declaredEdges?: [string, string][];
}

export function ExecutionCanvas(props: ExecutionCanvasProps): JSX.Element {
  return (
    <ReactFlowProvider>
      <ExecutionCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function ExecutionCanvasInner({
  pinnedGraph,
  effectiveEdges,
  stages,
  runningBranchNodeIds,
  selectedNodeId,
  onSelectNode,
  declaredEdges,
}: ExecutionCanvasProps): JSX.Element {
  const positions = useMemo(() => layoutGraph(pinnedGraph), [pinnedGraph]);
  const stageByNode = useMemo(() => {
    const map = new Map<string, TaskStage>();
    for (const stage of stages) {
      const existing = map.get(stage.node_id);
      // 同一节点可能有多条阶段记录（历史或重入）；取最近更新的一条作为「当前」。
      if (!existing) map.set(stage.node_id, stage);
      else {
        const a = new Date(existing.updated_at ?? existing.created_at ?? 0).getTime();
        const b = new Date(stage.updated_at ?? stage.created_at ?? 0).getTime();
        if (b >= a) map.set(stage.node_id, stage);
      }
    }
    return map;
  }, [stages]);

  const runningSet = useMemo(() => new Set(runningBranchNodeIds ?? []), [runningBranchNodeIds]);

  const rfNodes: RtNode[] = useMemo(
    () =>
      pinnedGraph.nodes.map((node) => {
        const stage = stageByNode.get(node.node_id) ?? null;
        const pos = positions.get(node.node_id) ?? { x: 0, y: 0 };
        return {
          id: node.node_id,
          type: 'runtime' as const,
          position: pos,
          draggable: false,
          selected: node.node_id === selectedNodeId,
          data: {
            nodeId: node.node_id,
            name: node.name,
            role: node.role,
            enabled: node.enabled,
            stage,
            placeholder: null,
            isRunningBranch: runningSet.has(node.node_id),
            selected: node.node_id === selectedNodeId,
          },
        } satisfies RtNode;
      }),
    [pinnedGraph.nodes, stageByNode, positions, selectedNodeId, runningSet],
  );

  const declared = useMemo(() => {
    const set = new Set<string>();
    for (const [a, b] of declaredEdges ?? []) set.add(`${a}->${b}`);
    return set;
  }, [declaredEdges]);

  const rfEdges: RFEdge[] = useMemo(
    () =>
      effectiveEdges.map(([from, to], idx) => {
        const bypass = !declared.has(`${from}->${to}`);
        const toStage = stageByNode.get(to);
        const active =
          toStage !== undefined &&
          (toStage.observed_state === 'running' ||
            toStage.observed_state === 'dispatching' ||
            toStage.observed_state === 'retrying');
        return {
          id: `${from}->${to}#${idx}`,
          source: from,
          target: to,
          type: 'smoothstep',
          label: bypass ? '绕过' : undefined,
          labelStyle: { fill: 'var(--st-warn)', fontSize: 10 },
          labelBgStyle: { fill: 'var(--bg-1)' },
          style: {
            stroke: bypass ? 'var(--st-warn)' : active ? 'var(--st-running)' : 'var(--line-strong)',
            strokeWidth: active ? 2.2 : 1.4,
            strokeDasharray: bypass ? '5 3' : undefined,
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: bypass ? 'var(--st-warn)' : 'var(--line-strong)',
            width: 16,
            height: 16,
          },
          animated: active,
        } satisfies RFEdge;
      }),
    [effectiveEdges, declared, stageByNode],
  );

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      nodeTypes={nodeTypes}
      onNodeClick={(_e, node) => onSelectNode(node.id)}
      onPaneClick={() => onSelectNode(null)}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      fitView
      minZoom={0.15}
      maxZoom={2}
      proOptions={{ hideAttribution: false }}
      style={{ background: 'var(--bg-0)' }}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#1f2630" />
      <Controls showInteractive={false} />
      <MiniMap
        pannable
        zoomable
        style={{ background: 'var(--bg-1)', border: '1px solid var(--line)' }}
        maskColor="rgba(6,8,12,0.7)"
        nodeColor={(n) => {
          const data = n.data as RuntimeNodeData | undefined;
          const state = data?.stage?.observed_state;
          return state ? STAGE_TONE_VAR[state] : '#39414e';
        }}
      />
    </ReactFlow>
  );
}
