/**
 * 对话分支森林的布局（v0.03 §6.3）。
 *
 * 纯树算法（比定义图的 DAG 布局简单，**刻意不复用** `layout.ts`——那个是
 * 为 DAG 的同层名字排序堆叠写的）：
 *
 * - x 由深度分层：每层一列，父子必不同列；
 * - y 由后序遍历分配：叶子占一格，父节点居中于其孩子区间；
 * - 多根树（森林）纵向依次排布，树与树之间空一格。
 *
 * 纯函数、确定性输出：同样的节点集必得同样的坐标。父指针缺失的孤儿节点
 * 按树根对待（数据损坏时仍然可画，不抛异常）；环由访问集合截断。
 */

export interface ForestNodeLike {
  id: string;
  parentId: string | null;
}

export interface ForestLayoutOptions {
  /** 相邻两列（相邻两层）的水平间距。 */
  colWidth?: number;
  /** 相邻两格的垂直间距（叶子节点中心距）。 */
  rowHeight?: number;
  /** 两棵树之间额外空几格（默认 1）。 */
  treeGap?: number;
}

export interface ForestPosition {
  x: number;
  y: number;
}

export const FOREST_COL_W = 280;
export const FOREST_ROW_H = 120;

/** 计算森林布局。返回 id → 左上角坐标。 */
export function layoutForest(
  nodes: ForestNodeLike[],
  options: ForestLayoutOptions = {},
): Map<string, ForestPosition> {
  const colW = options.colWidth ?? FOREST_COL_W;
  const rowH = options.rowHeight ?? FOREST_ROW_H;
  const treeGap = options.treeGap ?? 1;

  const ids = new Set(nodes.map((n) => n.id));
  const children = new Map<string, string[]>();
  const roots: string[] = [];
  for (const n of nodes) {
    children.set(n.id, []);
  }
  for (const n of nodes) {
    if (n.parentId !== null && ids.has(n.parentId) && n.parentId !== n.id) {
      children.get(n.parentId)!.push(n.id);
    } else {
      roots.push(n.id);
    }
  }

  const positions = new Map<string, ForestPosition>();
  const visited = new Set<string>();
  let cursor = 0; // 纵向格子游标（单位：格）

  // 后序遍历：先放孩子，父节点居中于孩子区间。返回子树占的格子数。
  const place = (id: string, depth: number): number => {
    if (visited.has(id)) return 0; // 环截断：数据损坏时不死循环
    visited.add(id);
    const kids = children.get(id) ?? [];
    if (kids.length === 0) {
      positions.set(id, { x: depth * colW, y: cursor * rowH });
      cursor += 1;
      return 1;
    }
    const start = cursor;
    let units = 0;
    for (const kid of kids) units += place(kid, depth + 1);
    units = Math.max(1, units);
    positions.set(id, { x: depth * colW, y: (start + (units - 1) / 2) * rowH });
    return units;
  };

  for (const root of roots) {
    place(root, 0);
    cursor += treeGap;
  }
  // 只经环可达的残余节点（正常数据不会走到这里）：补画成树根，保证可见。
  for (const n of nodes) {
    if (!visited.has(n.id)) {
      place(n.id, 0);
      cursor += treeGap;
    }
  }
  return positions;
}
