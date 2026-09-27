"""derive() 有效图派生：功能用例 + 与暴力参考实现的一致性。

对应 ACT-01（可逆启停）、ACT-02（路径可见）、ACT-03（不重复计数）。
"""

from __future__ import annotations

import pytest

from workerbee.core.graph.derive import derive, preview_toggle
from workerbee.core.domain.workflow import GraphSpec

from tests.helpers import chain, diamond, edge, graph, node

pytestmark = pytest.mark.unit


def _edges(g: GraphSpec) -> set[tuple[str, str]]:
    return {(e.from_node, e.to_node) for e in derive(g).edges}


def _edges_with(g: GraphSpec, disabled: set[str]) -> set[tuple[str, str]]:
    enabled = {n.node_id for n in g.nodes} - disabled
    return {(e.from_node, e.to_node) for e in derive(g, enabled).edges}


# ---------------------------------------------------------------------------
# 基本语义
# ---------------------------------------------------------------------------


def test_no_disabled_nodes_is_identity():
    """没有停用节点时，有效图就是定义图。"""
    g = diamond()
    assert _edges(g) == {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")}


def test_chain_middle_disabled_connects_neighbours():
    """A→B→C 停用 B 后，A 直接连到 C。"""
    g = chain("A", "B", "C")
    assert _edges_with(g, {"B"}) == {("A", "C")}


def test_chain_consecutive_disabled_nodes():
    """A→B→C→D 连续停用 B、C，A 直接连到 D。"""
    g = chain("A", "B", "C", "D")
    assert _edges_with(g, {"B", "C"}) == {("A", "D")}


def test_diamond_middle_disabled():
    """菱形图停用一支，其余关系保持。"""
    g = diamond()
    assert _edges_with(g, {"B"}) == {("A", "C"), ("A", "D"), ("C", "D")}


def test_diamond_join_disabled():
    """汇聚点被停用时，两个分支不再有共同下游。"""
    g = diamond()
    assert _edges_with(g, {"D"}) == {("A", "B"), ("A", "C")}


def test_shared_upstream_cross_dependency():
    """清单 §4 的 X→M、Y→M、M→P、M→Q 用例。

    停用 M 后 X→Q、Y→P 属于原图传递闭包里的可达关系，derive 把它们物化为有效边。
    它们是**两条不同的边**，不是同一条依赖被计两次。
    """
    g = graph({"X": ["M"], "Y": ["M"], "M": ["P", "Q"]})
    assert _edges_with(g, {"M"}) == {("X", "P"), ("X", "Q"), ("Y", "P"), ("Y", "Q")}


def test_duplicate_paths_collapse_to_one_edge():
    """A→B→D 与 A→C→D 同时被绕过时，D 只收到一条来自 A 的有效边（集合语义）。"""
    g = graph({"A": ["B", "C"], "B": ["D"], "C": ["D"]})
    eff = derive(g, {"A", "D"})
    assert {(e.from_node, e.to_node) for e in eff.edges} == {("A", "D")}
    assert len(eff.edges) == 1, "重复路径不得产生重复边，否则依赖会被重复计数"


def test_bypass_records_via_path():
    """绕过路径被记录，供 ACT-02/03 的定位与 UI 展示。"""
    g = graph({"A": ["B"], "B": ["C"], "C": ["D"]})
    eff = derive(g, {"A", "D"})
    bypass = [e for e in eff.edges if e.from_node == "A"]
    assert len(bypass) == 1
    assert bypass[0].to_node == "D"
    assert bypass[0].via == ["B", "C"]
    assert not bypass[0].is_direct()


def test_direct_edge_preferred_over_bypass_path():
    """直连边与绕过路径同时存在时，记录较短的 via。"""
    g = graph({"A": ["B", "C"], "B": ["C"]})
    eff = derive(g, {"A", "C"})
    a_to_c = [e for e in eff.edges if e == ("A", "C") or (e.from_node, e.to_node) == ("A", "C")]
    assert len(a_to_c) == 1
    assert a_to_c[0].via == []


# ---------------------------------------------------------------------------
# 可逆性（ACT-01）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("disabled", [{"B"}, {"C"}, {"B", "C"}, {"D"}])
def test_toggle_is_reversible(disabled):
    """停用后再启用，精确恢复原始依赖：无短路边残留、无节点丢失。"""
    g = diamond()
    original = _edges(g)

    after_disable = _edges_with(g, disabled)
    after_reenable = _edges_with(g, set())

    assert after_reenable == original
    if disabled:
        assert after_disable != original or disabled == {"X"}


def test_repeated_toggles_do_not_drift():
    """任意交错启停序列后，相同启用集合必得相同的有效图（不随操作历史漂移）。"""
    g = chain("A", "B", "C", "D")
    full = _edges_with(g, set())

    for _ in range(5):
        _edges_with(g, {"B"})
        _edges_with(g, {"B", "C"})
        _edges_with(g, {"C"})
        _edges_with(g, set())
        assert _edges_with(g, set()) == full


def test_derive_is_pure():
    """派生不修改输入。"""
    g = chain("A", "B", "C")
    before = g.canonical_json()
    derive(g, {"A", "C"})
    preview_toggle(g, "B", False)
    assert g.canonical_json() == before


# ---------------------------------------------------------------------------
# 入口 / 出口语义（ACT-02、WF-05）
# ---------------------------------------------------------------------------


def test_entry_becomes_downstream_when_source_disabled():
    """入口停用后，若存在新的合法入口，有效图照常可用（ACT-02）。"""
    g = chain("A", "B", "C")

    # 停用 B、C：只剩 A 参与执行，A 既是入口也是出口
    only_a = derive(g, {"A"})
    assert only_a.entry_nodes() == ["A"]
    assert only_a.exit_nodes() == ["A"]

    # 停用入口 A：B 成为新入口，C 仍是出口——不得因旧入口消失就拒绝执行
    no_a = derive(g, {"B", "C"})
    assert no_a.entry_nodes() == ["B"]
    assert no_a.exit_nodes() == ["C"]


def test_all_disabled_yields_empty_graph():
    g = chain("A", "B")
    eff = derive(g, set())
    assert eff.is_empty()
    assert eff.entry_nodes() == []


def test_multiple_entries_and_exits():
    g = graph({"A": ["C"], "B": ["C"], "C": ["D", "E"]})
    eff = derive(g)
    assert eff.entry_nodes() == ["A", "B"]
    assert eff.exit_nodes() == ["D", "E"]


def test_isolated_node_is_both_entry_and_exit():
    """单节点、独立分支不得因「非连通」被判错（WF-05）。"""
    g = graph({"A": ["B"], "Z": []})
    eff = derive(g)
    assert "Z" in eff.entry_nodes()
    assert "Z" in eff.exit_nodes()


# ---------------------------------------------------------------------------
# 与暴力参考实现的一致性（差分测试）
# ---------------------------------------------------------------------------


def _reference_effective_edges(g: GraphSpec, enabled: set[str]) -> set[tuple[str, str]]:
    """按 §4.2 的**字面定义**暴力枚举所有简单路径。

    只有用于小图交叉验证；生产实现走 BFS 版本。
    """
    adj: dict[str, list[str]] = {}
    for e in g.edges:
        adj.setdefault(e.from_node, []).append(e.to_node)

    def simple_paths(src: str, dst: str) -> list[list[str]]:
        found: list[list[str]] = []

        def walk(cur: str, path: list[str]) -> None:
            if cur == dst and len(path) > 1:
                found.append(path)
                return
            for nxt in adj.get(cur, []):
                if nxt in path:
                    continue
                walk(nxt, path + [nxt])

        walk(src, [src])
        return found

    result: set[tuple[str, str]] = set()
    for u in enabled:
        for v in enabled:
            if u == v:
                continue
            for path in simple_paths(u, v):
                intermediates = path[1:-1]
                if all(n not in enabled for n in intermediates):
                    result.add((u, v))
                    break
    return result


ALL_GRAPHS = {
    "chain4": chain("A", "B", "C", "D"),
    "diamond": diamond(),
    "cross": graph({"X": ["M"], "Y": ["M"], "M": ["P", "Q"]}),
    "wide": graph({"A": ["B", "C", "D"], "B": ["E"], "C": ["E"], "D": ["E"], "E": ["F"]}),
    "fsm": graph({"S": ["A", "B"], "A": ["C"], "B": ["C"], "C": ["T"], "T": []}),
}


@pytest.mark.parametrize("graph_name", sorted(ALL_GRAPHS))
def test_matches_brute_force_reference(graph_name):
    """对每个图枚举**全部** 2^n 启用集合，与暴力实现逐一对拍。"""
    from itertools import combinations

    g = ALL_GRAPHS[graph_name]
    names = [n.node_id for n in g.nodes]

    for r in range(len(names) + 1):
        for enabled_tuple in combinations(names, r):
            enabled = set(enabled_tuple)
            got = {(e.from_node, e.to_node) for e in derive(g, enabled).edges}
            want = _reference_effective_edges(g, enabled)
            assert got == want, f"{graph_name} enabled={sorted(enabled)}: {got} != {want}"


# ---------------------------------------------------------------------------
# 预览（ACT-02）
# ---------------------------------------------------------------------------


def test_preview_toggle_reports_added_and_removed_edges():
    g = chain("A", "B", "C")
    delta = preview_toggle(g, "B", False)

    assert delta.enabling is False
    assert {(e.from_node, e.to_node) for e in delta.added_edges} == {("A", "C")}
    assert {(e.from_node, e.to_node) for e in delta.removed_edges} == {("A", "B"), ("B", "C")}
    assert delta.affected_downstream == ["A", "B", "C"]


def test_preview_toggle_detects_entry_loss():
    g = chain("A", "B")
    delta = preview_toggle(g, "A", False)
    assert delta.entry_nodes_before == ["A"]
    assert delta.entry_nodes_after == ["B"]
    assert delta.removes_entry()
    assert not delta.leaves_no_entry()


def test_preview_toggle_detects_total_disable():
    g = graph({"A": []})
    delta = preview_toggle(g, "A", False)
    assert delta.leaves_no_entry()
    assert delta.leaves_no_exit()
