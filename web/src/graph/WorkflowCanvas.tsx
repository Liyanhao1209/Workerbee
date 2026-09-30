/**
 * 拓扑编辑器画布（WF-01、ACT-01）。
 *
 * 这是一个**定义图 G₀** 的编辑器：节点与边就是用户保存的事实源，
 * 有效图（启停之后实际参与执行的依赖）是后端 derive 出来的，**不在这里画**——
 * 在这个画布上画有效边会让人以为它可以被直接编辑（§1.2 原则 1）。
 *
 * 停用的节点用灰化 + 虚线边框区分（ACT-01 的视觉要求），并且**仍然可见可编辑**：
 * 停用保留节点配置和原始关系，只是不参与执行。
 */

import { useCallback, useEffect, useMemo } from 'react';
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge as RFEdge,
  type EdgeChange,
  type Node as RFNode,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import type { Edge, GraphSpec, NodeDefinition } from '../api/types';
import { NODE_W, layoutGraph } from './layout';

// ---------------------------------------------------------------------------
// 自定义节点
// ---------------------------------------------------------------------------

/** 用 type 而非 interface：React Flow 的 Node<TData> 需要 TData 满足 Record<string, unknown>。 */
type WfNodeData = {
  node: NodeDefinition;
  /** 校验/预览高亮：命中时描边加粗。 */
  highlight: boolean;
  /** 预览里「受影响但未停用」的节点：用强调色描边。 */
  affected: boolean;
  /** 诊断选中：更强的高亮。 */
  selected: boolean;
};

export type WfNode = RFNode<WfNodeData, 'workflow'>;
export type WfEdge = RFEdge<{ via?: string[]; highlight?: boolean; declared: boolean }>;

function WorkflowNode({ data, selected }: NodeProps<WfNode>): JSX.Element {
  const { node } = data;
  const disabled = !node.enabled;
  const primary = node.profiles[0];

  const borderColor = data.selected
    ? 'var(--accent)'
    : data.highlight
      ? 'var(--st-danger)'
      : data.affected
        ? 'var(--st-warn)'
        : disabled
          ? 'var(--line-strong)'
          : 'var(--line-strong)';

  return (
    <div
      style={{
        width: NODE_W,
        minHeight: 62,
        background: disabled ? 'var(--bg-1)' : 'var(--bg-2)',
        border: `${data.selected || data.highlight ? 2 : 1}px ${disabled ? 'dashed' : 'solid'} ${borderColor}`,
        borderRadius: 'var(--radius)',
        padding: '7px 9px',
        opacity: disabled ? 0.55 : 1,
        boxShadow: selected ? '0 0 0 3px rgba(77,163,255,0.20)' : undefined,
        cursor: 'pointer',
      }}
      title={disabled ? '该节点已停用：配置与原始关系保留，不参与当前执行' : undefined}
    >
      <Handle type="target" position={Position.Left} style={{ background: 'var(--line-strong)' }} />

      <div className="row row--tight" style={{ gap: 5 }}>
        <span
          className="truncate"
          style={{
            fontWeight: 600,
            fontSize: 'var(--fs-md)',
            textDecoration: disabled ? 'line-through' : undefined,
            color: disabled ? 'var(--fg-2)' : 'var(--fg-0)',
          }}
        >
          {node.name || '（未命名节点）'}
        </span>
        {!node.enabled ? <span className="chip chip--off">已停用</span> : null}
      </div>

      {node.role ? (
        <div className="text-xs muted truncate" style={{ marginTop: 1 }}>
          {node.role}
        </div>
      ) : null}

      <div className="text-xs" style={{ marginTop: 4 }}>
        {primary ? (
          <span className="mono" style={{ color: disabled ? 'var(--fg-3)' : 'var(--fg-1)' }}>
            {primary.model_name}
            {primary.harness_ref ? ` @${primary.harness_ref}` : ''}
          </span>
        ) : (
          <span style={{ color: 'var(--st-warn)' }}>未配置执行候选</span>
        )}
        {node.profiles.length > 1 ? (
          <span className="dim"> +{node.profiles.length - 1}</span>
        ) : null}
      </div>

      <div className="row row--tight" style={{ marginTop: 4, gap: 3 }}>
        {node.skill_refs.length > 0 ? <span className="chip">{node.skill_refs.length} Skills</span> : null}
        {node.tool_refs.length > 0 ? <span className="chip">{node.tool_refs.length} 工具</span> : null}
        {node.required_inputs.length > 0 ? (
          <span className="chip" title={node.required_inputs.join(', ')}>
            需 {node.required_inputs.length} 输入
          </span>
        ) : null}
      </div>

      <Handle type="source" position={Position.Right} style={{ background: 'var(--line-strong)' }} />
    </div>
  );
}

const nodeTypes = { workflow: WorkflowNode };

// ---------------------------------------------------------------------------
// 高亮目标
// ---------------------------------------------------------------------------

export interface CanvasFocus {
  nodeIds: string[];
  edges: [string, string][];
  /** 每次点同一条诊断也要重新居中，所以带一个单调递增的序号。 */
  nonce: number;
}

// ---------------------------------------------------------------------------
// 画布
// ---------------------------------------------------------------------------

export interface WorkflowCanvasProps {
  graph: GraphSpec;
  /** 选中节点（右侧面板据此编辑）。 */
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onGraphChange: (next: GraphSpec, options?: { uiOnly?: boolean }) => void;
  focus: CanvasFocus | null;
  /** 自动布局（把 ui_position 重排为分层结果）。 */
  layoutNonce: number;
  readOnly?: boolean;
  /** 执行图叠加用：节点 → 阶段状态。 */
  stageStatus?: Map<string, string> | null;
}

export function WorkflowCanvas(props: WorkflowCanvasProps): JSX.Element {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function CanvasInner({
  graph,
  selectedNodeId,
  onSelectNode,
  onGraphChange,
  focus,
  layoutNonce,
  readOnly,
}: WorkflowCanvasProps): JSX.Element {
  const { fitView, setCenter } = useReactFlow();
  const focusKey = focus ? focus.nonce : -1;

  const positions = useMemo(() => layoutGraph(graph), [graph]);

  const rfNodes: WfNode[] = useMemo(
    () =>
      graph.nodes.map((node) => {
        const pos = positions.get(node.node_id) ?? { x: 0, y: 0 };
        return {
          id: node.node_id,
          type: 'workflow' as const,
          position: pos,
          selected: node.node_id === selectedNodeId,
          draggable: !readOnly,
          data: {
            node,
            highlight: Boolean(focus?.nodeIds.includes(node.node_id)),
            affected: false,
            selected: Boolean(focus?.nodeIds.includes(node.node_id)),
          },
        };
      }),
    [graph.nodes, positions, selectedNodeId, readOnly, focus],
  );

  const rfEdges: WfEdge[] = useMemo(
    () =>
      graph.edges.map((edge, idx) => {
        const id = `${edge.from_node}->${edge.to_node}`;
        const highlighted = Boolean(focus?.edges.some(([a, b]) => a === edge.from_node && b === edge.to_node));
        const fromNode = graph.nodes.find((n) => n.node_id === edge.from_node);
        const toNode = graph.nodes.find((n) => n.node_id === edge.to_node);
        const dead = fromNode?.enabled === false || toNode?.enabled === false;
        return {
          id: `${id}#${idx}`,
          source: edge.from_node,
          target: edge.to_node,
          type: 'smoothstep',
          animated: false,
          label: edge.output_contract?.outputs.length
            ? `${edge.output_contract.outputs.join(', ')}`
            : edge.desc ?? undefined,
          labelStyle: { fill: 'var(--fg-2)', fontSize: 10 },
          labelBgStyle: { fill: 'var(--bg-1)' },
          style: {
            stroke: highlighted
              ? 'var(--st-danger)'
              : dead
                ? 'var(--line)'
                : 'var(--line-strong)',
            strokeWidth: highlighted ? 2.5 : 1.4,
            strokeDasharray: dead ? '4 3' : undefined,
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: highlighted ? 'var(--st-danger)' : 'var(--line-strong)',
            width: 16,
            height: 16,
          },
          data: { highlight: highlighted, declared: edge.output_contract !== null },
        } satisfies WfEdge;
      }),
    [graph.edges, graph.nodes, focus],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange<WfNode>[]) => {
      if (readOnly) return;
      let next = graph;
      let touched = false;
      for (const change of changes) {
        if (change.type === 'position' && change.position) {
          const node = next.nodes.find((n) => n.node_id === change.id);
          if (!node) continue;
          if (node.ui_position && node.ui_position[0] === change.position.x && node.ui_position[1] === change.position.y) {
            continue;
          }
          next = {
            ...next,
            nodes: next.nodes.map((n) =>
              n.node_id === change.id ? { ...n, ui_position: [change.position!.x, change.position!.y] } : n,
            ),
          };
          touched = true;
        } else if (change.type === 'remove') {
          next = {
            ...next,
            nodes: next.nodes.filter((n) => n.node_id !== change.id),
            edges: next.edges.filter((e) => e.from_node !== change.id && e.to_node !== change.id),
          };
          touched = true;
        } else if (change.type === 'select') {
          if (change.selected) onSelectNode(change.id);
        }
      }
      if (touched) onGraphChange(next, { uiOnly: true });
    },
    [graph, onGraphChange, onSelectNode, readOnly],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange<WfEdge>[]) => {
      if (readOnly) return;
      const removed = changes.filter((c) => c.type === 'remove').map((c) => c.id);
      if (removed.length === 0) return;
      const nextEdges = graph.edges.filter((_e, idx) => {
        // 边 id 形如 "from->to#idx"；用 idx 比对，避免 from/to 含 '-' 时的歧义。
        const id = `${_e.from_node}->${_e.to_node}#${idx}`;
        return !removed.includes(id);
      });
      onGraphChange({ ...graph, edges: nextEdges });
    },
    [graph, onGraphChange, readOnly],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly) return;
      if (!connection.source || !connection.target) return;
      if (connection.source === connection.target) return;
      const exists = graph.edges.some(
        (e) => e.from_node === connection.source && e.to_node === connection.target,
      );
      if (exists) return;
      const edge: Edge = {
        from_node: connection.source,
        to_node: connection.target,
        output_contract: null,
        desc: null,
      };
      onGraphChange({ ...graph, edges: [...graph.edges, edge] });
    },
    [graph, onGraphChange, readOnly],
  );

  // 定位：诊断点一条 → 居中 + 高亮。
  useEffect(() => {
    if (!focus) return;
    const ids = [...focus.nodeIds];
    if (ids.length > 0) {
      void fitView({ nodes: ids.map((id) => ({ id })), padding: 0.6, duration: 400, maxZoom: 1.4 });
      return;
    }
    if (focus.edges.length > 0) {
      const first = focus.edges[0];
      if (!first) return;
      void fitView({
        nodes: [{ id: first[0] }, { id: first[1] }],
        padding: 0.6,
        duration: 400,
        maxZoom: 1.4,
      });
      return;
    }
    // 没有具体目标（全局项）时不做任何移动，避免视口乱跳。
  }, [focusKey, focus, fitView, setCenter]);

  // 自动布局
  useEffect(() => {
    if (layoutNonce === 0) return;
    const timer = window.setTimeout(() => {
      void fitView({ padding: 0.2, duration: 400 });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [layoutNonce, fitView]);

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onPaneClick={() => onSelectNode(null)}
      onNodeClick={(_e, node) => onSelectNode(node.id)}
      nodesDraggable={!readOnly}
      nodesConnectable={!readOnly}
      elementsSelectable
      deleteKeyCode={readOnly ? null : ['Delete', 'Backspace']}
      proOptions={{ hideAttribution: false }}
      fitView
      minZoom={0.15}
      maxZoom={2}
      defaultEdgeOptions={{ type: 'smoothstep' }}
      style={{ background: 'var(--bg-0)' }}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#1f2630" />
      <MiniMap
        pannable
        zoomable
        style={{ background: 'var(--bg-1)', border: '1px solid var(--line)', width: 132, height: 88 }}
        maskColor="rgba(6,8,12,0.7)"
        nodeColor={(n) => {
          const data = n.data as WfNodeData | undefined;
          return data?.node.enabled === false ? '#39414e' : '#2f6ea8';
        }}
      />
    </ReactFlow>
  );
}
