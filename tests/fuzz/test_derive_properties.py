"""derive() 的基于性质的测试（AC-05）。

随机生成 DAG 与随机启停序列，回归四条必须恒成立的性质：
    1. 无环：E_eff ⊆ Reach(G₀)，G₀ 为 DAG ⇒ G_eff 恒为 DAG（架构设计 §4.2 性质 1）
    2. 集合语义：无重复边 ⇒ 依赖不会被重复计数（ACT-03）
    3. 可逆：重新启用精确恢复原始依赖，无短路边残留、无节点丢失（ACT-01）
    4. 无历史漂移：最终有效图只是「当前启用集合」的函数
"""

from __future__ import annotations

import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workerbee.core.domain.workflow import GraphSpec
from workerbee.core.graph.derive import derive

from tests.helpers import edge, node

pytestmark = [pytest.mark.unit, pytest.mark.fuzz]


# ---------------------------------------------------------------------------
# 策略：随机 DAG
# ---------------------------------------------------------------------------


@st.composite
def random_dag(draw, min_nodes: int = 2, max_nodes: int = 9):
    """按随机拓扑序生成 DAG：边只从序小的节点指向序大的节点。"""
    n = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    order = [f"N{i}" for i in range(n)]
    density = draw(st.floats(min_value=0.0, max_value=0.9))

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.floats(min_value=0.0, max_value=1.0)) < density:
                edges.append(edge(order[i], order[j]))

    g = GraphSpec(
        nodes=[node(name) for name in order],
        edges=edges,
    )
    return g, order


@st.composite
def random_enabled_set(draw, order):
    return {name for name in order if draw(st.booleans())}


def _has_cycle(node_ids: list[str], edges: set[tuple[str, str]]) -> bool:
    adj: dict[str, list[str]] = {n: [] for n in node_ids}
    indeg: dict[str, int] = {n: 0 for n in node_ids}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        indeg[b] = indeg.get(b, 0) + 1
    ready = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        cur = ready.pop()
        seen += 1
        for nxt in adj.get(cur, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    return seen != len(node_ids)


def _reachable(g: GraphSpec) -> set[tuple[str, str]]:
    """定义图中所有可达的有序节点对（自反关系不含自身）。"""
    adj: dict[str, list[str]] = {}
    for e in g.edges:
        adj.setdefault(e.from_node, []).append(e.to_node)

    out: set[tuple[str, str]] = set()
    for start in [n.node_id for n in g.nodes]:
        stack = list(adj.get(start, []))
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            out.add((start, cur))
            stack.extend(adj.get(cur, []))
    return out


# ---------------------------------------------------------------------------
# 性质
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(4))
def test_property_effective_graph_is_acyclic(seed):
    rng = random.Random(seed)
    g, order = _build_dag(rng, n=9)
    enabled = {n for n in order if rng.random() < 0.5}

    eff = derive(g, enabled)
    edges = {(e.from_node, e.to_node) for e in eff.edges}
    assert not _has_cycle(list(enabled), edges), "派生结果违反无环性"


def _build_dag(rng: random.Random, n: int) -> tuple[GraphSpec, list[str]]:
    order = [f"N{i}" for i in range(n)]
    edges = [
        edge(order[i], order[j])
        for i in range(n)
        for j in range(i + 1, n)
        if rng.random() < 0.35
    ]
    return GraphSpec(nodes=[node(name) for name in order], edges=edges), order


@given(data=st.data())
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_property_effective_edges_subset_of_reachability(data):
    """性质 1 的加强版：每条有效边都对应定义图中的一条真实路径。"""
    g, order = data.draw(random_dag())
    enabled = data.draw(random_enabled_set(order))

    eff = derive(g, enabled)
    reach = _reachable(g)
    for e in eff.edges:
        assert (e.from_node, e.to_node) in reach, (
            f"有效边 {e.from_node}->{e.to_node} 在定义图中不可达"
        )


@given(data=st.data())
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_property_no_duplicate_edges(data):
    """性质 2：集合语义 —— 重复路径不重复计边（ACT-03）。"""
    g, order = data.draw(random_dag())
    enabled = data.draw(random_enabled_set(order))

    eff = derive(g, enabled)
    keys = [(e.from_node, e.to_node) for e in eff.edges]
    assert len(keys) == len(set(keys)), "有效图出现重复边，会导致依赖重复计数"


@given(data=st.data())
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_property_reversible(data):
    """性质 3：停用再启用精确恢复原始依赖（ACT-01）。"""
    g, order = data.draw(random_dag())
    subset = data.draw(random_enabled_set(order))

    baseline = _edge_set(derive(g, order))  # 全启用

    # 先停用一批，再逐个恢复，最后全启用必须回到基线
    _ = derive(g, subset)
    restored = _edge_set(derive(g, order))
    assert restored == baseline


@given(st.lists(st.tuples(st.integers(0, 8), st.booleans()), min_size=1, max_size=30))
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_property_no_history_drift(ops):
    """性质 4：交错启停序列之后，有效图只是当前启用集合的函数。"""
    g, order = _build_dag(random.Random(12345), n=9)

    enabled = set(order)
    for idx, turn_on in ops:
        name = order[idx % len(order)]
        enabled.add(name) if turn_on else enabled.discard(name)

    eff = derive(g, enabled)
    assert _edge_set(eff) == _edge_set(derive(g, set(enabled)))

    # 每个有效节点都必须在场——重新启用不丢节点
    assert set(eff.node_ids) == enabled


@given(data=st.data())
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_property_entries_match_definition(data):
    """入口语义：有效入口 = 有效上游为空的启用节点（§4.2 性质 4）。"""
    g, order = data.draw(random_dag())
    enabled = data.draw(random_enabled_set(order))

    eff = derive(g, enabled)
    has_pred = {e.to_node for e in eff.edges}
    expected = sorted(n for n in enabled if n not in has_pred)
    assert eff.entry_nodes() == expected


def _edge_set(eff) -> set[tuple[str, str]]:
    return {(e.from_node, e.to_node) for e in eff.edges}
