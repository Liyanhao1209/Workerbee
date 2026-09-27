/**
 * 定义图的自动布局。
 *
 * 只在节点没有 `ui_position` 时才用（那是对手工摆位的兜底），因此不需要
 * 通用图布局库：分层 + 同层中序就够，且结果稳定（同样的图必得同样的坐标，
 * 不会每次打开都跳一下）。
 */

import type { Edge, GraphSpec, NodeDefinition } from '../api/types';

export const NODE_W = 216;
export const NODE_H = 76;
const LAYER_GAP_X = 96;
const SIBLING_GAP_Y = 26;

/** 拓扑分层（最长路径分层）。图有环时把环上的节点放进最后一层，不抛异常。 */
export function layerNodes(nodes: NodeDefinition[], edges: Edge[]): Map<string, number> {
  const ids = new Set(nodes.map((n) => n.node_id));
  const indeg = new Map<string, number>();
  const succ = new Map<string, string[]>();
  for (const id of ids) {
    indeg.set(id, 0);
    succ.set(id, []);
  }
  for (const e of edges) {
    if (!ids.has(e.from_node) || !ids.has(e.to_node)) continue;
    if (e.from_node === e.to_node) continue;
    indeg.set(e.to_node, (indeg.get(e.to_node) ?? 0) + 1);
    succ.get(e.from_node)?.push(e.to_node);
  }

  const layer = new Map<string, number>();
  const queue: string[] = [];
  for (const [id, deg] of indeg) {
    if (deg === 0) {
      queue.push(id);
      layer.set(id, 0);
    }
  }

  let guard = 0;
  const maxGuard = nodes.length * nodes.length + nodes.length + 16;
  while (queue.length > 0 && guard < maxGuard) {
    guard += 1;
    const id = queue.shift();
    if (id === undefined) break;
    const base = layer.get(id) ?? 0;
    for (const next of succ.get(id) ?? []) {
      layer.set(next, Math.max(layer.get(next) ?? 0, base + 1));
      const deg = (indeg.get(next) ?? 1) - 1;
      indeg.set(next, deg);
      if (deg <= 0 && !queue.includes(next)) queue.push(next);
    }
  }

  // 未分配到的（处于环中）：放到最后一层，仍然是确定性的。
  let maxLayer = 0;
  for (const value of layer.values()) maxLayer = Math.max(maxLayer, value);
  for (const id of ids) {
    if (!layer.has(id)) layer.set(id, maxLayer + 1);
  }
  return layer;
}

/**
 * 计算坐标。已有 `ui_position` 的节点保留原位；
 * 其余节点按分层结果摆放，并与已有坐标所在的层对齐。
 */
export function layoutGraph(graph: GraphSpec): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const layer = layerNodes(graph.nodes, graph.edges);

  // 依据已有坐标推算「每层大概在哪一列」，让新节点落在已有版面里而不是飘到一边。
  const layerAnchorX = new Map<number, number>();
  for (const node of graph.nodes) {
    if (!node.ui_position) continue;
    const l = layer.get(node.node_id) ?? 0;
    const [x] = node.ui_position;
    const existing = layerAnchorX.get(l);
    if (existing === undefined) layerAnchorX.set(l, x);
    else layerAnchorX.set(l, Math.min(existing, x));
  }

  const byLayer = new Map<number, NodeDefinition[]>();
  for (const node of graph.nodes) {
    const l = layer.get(node.node_id) ?? 0;
    const bucket = byLayer.get(l);
    if (bucket) bucket.push(node);
    else byLayer.set(l, [node]);
  }

  for (const node of graph.nodes) {
    if (node.ui_position) {
      positions.set(node.node_id, { x: node.ui_position[0], y: node.ui_position[1] });
    }
  }

  const usedColumns = new Set<number>();
  const sortedLayers = [...byLayer.keys()].sort((a, b) => a - b);
  let autoColumn = 0;
  for (const l of sortedLayers) {
    const anchor = layerAnchorX.get(l);
    let columnX: number;
    if (anchor !== undefined) {
      columnX = anchor;
    } else {
      while (usedColumns.has(autoColumn)) autoColumn += 1;
      columnX = autoColumn * (NODE_W + LAYER_GAP_X);
      usedColumns.add(autoColumn);
    }
    const bucket = (byLayer.get(l) ?? []).slice().sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
    let cursorY = 0;
    for (const node of bucket) {
      if (positions.has(node.node_id)) continue;
      positions.set(node.node_id, { x: columnX, y: cursorY });
      cursorY += NODE_H + SIBLING_GAP_Y;
    }
  }

  return positions;
}

/** 渲染执行图时用：把 pinned 的有效边集摊平成边列表。 */
export function effectiveEdgeList(graph: GraphSpec, effectiveEdges: [string, string][]): Edge[] {
  const declared = new Map<string, Edge>();
  for (const e of graph.edges) declared.set(`${e.from_node}->${e.to_node}`, e);
  return effectiveEdges.map(([from, to]) => {
    const found = declared.get(`${from}->${to}`);
    return found ?? { from_node: from, to_node: to, output_contract: null, desc: null };
  });
}
