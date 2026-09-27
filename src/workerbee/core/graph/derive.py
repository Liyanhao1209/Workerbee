"""有效图派生 derive()（架构设计 v0.02 §4.1、§4.2、ACT-01–ACT-03）。

``derive`` 是**纯函数**：同样的定义图与 enabled 集合必得同样的有效图。
「不随操作历史漂移」由构造保证，而不是由「记得清理残留边」保证。

语义（§4.2）：对每个 enabled 节点 v，沿 E₀ 的入边向上游走，跳过 disabled 节点，
取每条路径上遇到的第一层 enabled 祖先作为 v 的有效上游。本实现自 enabled 源点
**向下**遍历等价地构造同一组边：只穿过 disabled 节点，一遇到 enabled 节点即记边
并停止下探。

可证明的性质（由 tests/fuzz 以随机启停序列回归）：
1. 无环：E_eff ⊆ Reach(G₀)，G₀ 为 DAG ⇒ G_eff 恒为 DAG。
2. 幂等且与操作顺序无关：纯函数。
3. 可逆：重新启用精确恢复原始依赖，无短路边残留、无节点丢失。
4. 集合语义：重复路径不重复计边，因而依赖计数不会重复（ACT-03）。
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.workflow import GraphSpec

__all__ = ["DerivedEdge", "EffectiveGraph", "derive", "GraphDelta", "preview_toggle"]


class DerivedEdge(DomainModel):
    """有效图中的一条边。"""

    from_node: str
    to_node: str

    via: list[str] = Field(default_factory=list)
    """被绕过的 disabled 中间节点（按路径顺序）。空表示原始直连边。"""

    def is_direct(self) -> bool:
        return not self.via

    def key(self) -> tuple[str, str]:
        return (self.from_node, self.to_node)


class EffectiveGraph(DomainModel):
    """派生结果。可缓存，但**永不持久化为可编辑对象**（§1.2 原则 1）。"""

    node_ids: list[str] = Field(default_factory=list)
    """参与执行的节点（= 启用节点集）。"""

    edges: list[DerivedEdge] = Field(default_factory=list)

    # ---- 索引（惰性、不参与相等性语义） ----

    def edge_set(self) -> set[tuple[str, str]]:
        return {(e.from_node, e.to_node) for e in self.edges}

    def edge_map(self) -> dict[tuple[str, str], DerivedEdge]:
        return {(e.from_node, e.to_node): e for e in self.edges}

    def successors(self, node_id: str) -> list[str]:
        return [e.to_node for e in self.edges if e.from_node == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        return [e.from_node for e in self.edges if e.to_node == node_id]

    def successor_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {n: [] for n in self.node_ids}
        for e in self.edges:
            out.setdefault(e.from_node, []).append(e.to_node)
        return out

    def predecessor_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {n: [] for n in self.node_ids}
        for e in self.edges:
            out.setdefault(e.to_node, []).append(e.from_node)
        return out

    def entry_nodes(self) -> list[str]:
        """有效入口 = 有效上游为空的 enabled 节点（§4.2 性质 4）。"""
        has_pred = {e.to_node for e in self.edges}
        return [n for n in self.node_ids if n not in has_pred]

    def exit_nodes(self) -> list[str]:
        has_succ = {e.from_node for e in self.edges}
        return [n for n in self.node_ids if n not in has_succ]

    def is_empty(self) -> bool:
        return not self.node_ids

    def version(self) -> int:
        """与 GraphSpec.effective_graph_version 同源的指纹，用于缓存键与钉扎比对。"""
        import hashlib
        import json

        payload = {
            "nodes": sorted(self.node_ids),
            "edges": sorted((e.from_node, e.to_node) for e in self.edges),
        }
        h = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return int(h[:15], 16)

    @classmethod
    def from_edges(
        cls, node_ids: Iterable[str], edges: Iterable[tuple[str, str] | Sequence[str]]
    ) -> "EffectiveGraph":
        """从钉扎的边表重建（任务快照回放用，不重新派生）。"""
        out: list[DerivedEdge] = []
        for e in edges:
            seq = list(e)
            out.append(DerivedEdge(from_node=seq[0], to_node=seq[1]))
        return cls(node_ids=list(node_ids), edges=out)

    def to_edge_tuples(self) -> list[tuple[str, str]]:
        return [(e.from_node, e.to_node) for e in self.edges]


def derive(graph: GraphSpec, enabled: set[str] | None = None) -> EffectiveGraph:
    """派生有效图。

    :param graph: 定义图 G₀。允许是草稿（可能含环）——本函数对环是防御性的，
        但环应由校验管线在发布前拒绝（WF-05）。
    :param enabled: 启用节点集。None 表示取 ``graph`` 中各节点自身的 ``enabled``。
    """
    if enabled is None:
        enabled = graph.enabled_node_ids()

    # 只保留真实存在的节点，避免外部传入脏集合扩大定义域
    known = {n.node_id for n in graph.nodes}
    enabled = set(enabled) & known

    adjacency: dict[str, list[str]] = {}
    for e in graph.edges:
        adjacency.setdefault(e.from_node, []).append(e.to_node)

    edges: list[DerivedEdge] = []
    for src in sorted(enabled):
        for dst, via in _first_enabled_descendants(src, adjacency, enabled):
            edges.append(DerivedEdge(from_node=src, to_node=dst, via=via))

    return EffectiveGraph(node_ids=sorted(enabled), edges=edges)


def _first_enabled_descendants(
    src: str, adjacency: dict[str, list[str]], enabled: set[str]
) -> list[tuple[str, list[str]]]:
    """从 ``src`` 向下穿过 disabled 节点，返回第一层 enabled 后代及其绕过路径。

    以 BFS 保证 ``via`` 是绕过节点最少的那条路径（可读性与 UI 展示）。
    ``seen_disabled`` 同时充当环路防御：即便 G₀ 意外含环也必然终止。
    """
    results: dict[str, list[str]] = {}
    seen_disabled: set[str] = {src}
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(src, ())])

    while queue:
        cur, via = queue.popleft()
        for nxt in adjacency.get(cur, ()):
            if nxt in enabled:
                if nxt != src and nxt not in results:
                    results[nxt] = list(via)
                # 遇到 enabled 节点即停止下探：它自己会作为源点继续派生
            else:
                if nxt in seen_disabled:
                    continue
                seen_disabled.add(nxt)
                queue.append((nxt, via + (nxt,)))

    return [(n, results[n]) for n in sorted(results)]


class GraphDelta(DomainModel):
    """一次拟议启停操作对有效图的影响（ACT-02 路径可见的机器表示）。

    仅描述**拓扑**影响；数据衔接影响由 validate 的契约检查补充。
    """

    node_id: str
    enabling: bool
    added_edges: list[DerivedEdge] = Field(default_factory=list)
    removed_edges: list[DerivedEdge] = Field(default_factory=list)
    entry_nodes_before: list[str] = Field(default_factory=list)
    entry_nodes_after: list[str] = Field(default_factory=list)
    exit_nodes_before: list[str] = Field(default_factory=list)
    exit_nodes_after: list[str] = Field(default_factory=list)
    affected_downstream: list[str] = Field(default_factory=list)
    """直接受影响的下游 enabled 节点。"""

    def removes_entry(self) -> bool:
        """停用后是否把某个原有入口变成了非入口。"""
        return bool(set(self.entry_nodes_before) - set(self.entry_nodes_after))

    def leaves_no_entry(self) -> bool:
        return not self.entry_nodes_after

    def leaves_no_exit(self) -> bool:
        return not self.exit_nodes_after


def preview_toggle(graph: GraphSpec, node_id: str, enable: bool) -> GraphDelta:
    """不修改任何状态，计算「若把 node_id 置为 enable」的有效图差异。

    ACT-02 要求操作前能看到受影响的节点与依赖，这就是那份数据的来源。
    """
    if graph.node(node_id) is None:
        raise KeyError(f"节点不存在: {node_id}")

    before = derive(graph)

    target = set(graph.enabled_node_ids())
    if enable:
        target.add(node_id)
    else:
        target.discard(node_id)
    after = derive(graph, target)

    before_edges = {(e.from_node, e.to_node): e for e in before.edges}
    after_edges = {(e.from_node, e.to_node): e for e in after.edges}

    added = [after_edges[k] for k in sorted(after_edges.keys() - before_edges.keys())]
    removed = [before_edges[k] for k in sorted(before_edges.keys() - after_edges.keys())]

    affected = sorted({e.from_node for e in added} | {e.to_node for e in removed})

    return GraphDelta(
        node_id=node_id,
        enabling=enable,
        added_edges=added,
        removed_edges=removed,
        entry_nodes_before=before.entry_nodes(),
        entry_nodes_after=after.entry_nodes(),
        exit_nodes_before=before.exit_nodes(),
        exit_nodes_after=after.exit_nodes(),
        affected_downstream=affected,
    )
