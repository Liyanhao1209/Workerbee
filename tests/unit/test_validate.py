"""校验管线（WF-05、ACT-02/03、CFG-02/05、HUM-03、AUTH-01/02、EXT-01/02）。

对应架构设计 §4.3（ACT-03 三条规则）与 §4.4（校验项表）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workerbee.core.domain import ContractWaiver, GraphSpec
from workerbee.core.graph.derive import EffectiveGraph, derive
from workerbee.core.graph.validate import (
    Diagnostic,
    InMemoryRegistry,
    Severity,
    ValidationMode,
    ValidationReport,
    validate,
    validate_toggle,
)

from tests.helpers import chain, diamond, edge, graph, node, profile, registry_with

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


def test_cycle_rejected():
    """WF-05：第一版采用无环依赖，环必须被拒并定位到节点。"""
    g = GraphSpec(nodes=[node("A"), node("B")], edges=[edge("A", "B"), edge("B", "A")])
    report = validate(g, registry_with())

    assert not report.ok()
    assert report.has_code("cycle")
    diag = next(d for d in report.errors() if d.code == "cycle")
    assert diag.node_id is not None
    assert diag.requirement == "WF-05"


def test_self_loop_rejected():
    g = GraphSpec(nodes=[node("A")], edges=[edge("A", "A")])
    report = validate(g, registry_with())
    assert not report.ok()
    assert report.has_code("self_loop")


def test_dangling_edge_rejected_at_construction():
    """悬空边引用在构造点即失败（GraphSpec 的模型校验），不会流进校验管线。"""
    with pytest.raises(ValidationError):
        GraphSpec(nodes=[node("A")], edges=[edge("A", "Ghost")])


def test_duplicate_edge_rejected_at_construction():
    with pytest.raises(ValidationError):
        GraphSpec(nodes=[node("A"), node("B")], edges=[edge("A", "B"), edge("A", "B")])


def test_duplicate_node_id_rejected_at_construction():
    with pytest.raises(ValidationError):
        GraphSpec(nodes=[node("A"), node("A")], edges=[])


# ---------------------------------------------------------------------------
# 入口 / 出口（WF-05、ACT-02）
# ---------------------------------------------------------------------------


def test_all_nodes_disabled_rejected():
    """ACT-02：全部停用或无可执行节点时不得接受正常执行提交。"""
    g = graph({"A": ["B"], "B": []}, enabled={"A", "B"})
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert not report.ok()
    assert report.has_code("no_enabled_node")


def test_no_entry_and_no_exit_are_unreachable_for_valid_input():
    """`no_entry` / `no_exit` 对合法输入不可达——有限非空 DAG 必有源也有汇。

    这不是缺陷：两项检查是**防御性**的（为将来可能支持的循环拓扑留位，
    见架构设计 §1.3 与 D-13）。这里直接驱动检查函数，避免日后被误删。
    """
    from workerbee.core.graph.derive import DerivedEdge
    from workerbee.core.graph.validate import _check_entry_exit

    # 事实核查：任何合法定义图派生出的有效图都必有入口与出口
    for g in (
        chain("A", "B", "C"),
        diamond(),
        graph({"A": ["B"], "B": []}, enabled={"A"}),
    ):
        eff = derive(g)
        assert eff.entry_nodes(), "DAG 必有源"
        assert eff.exit_nodes(), "DAG 必有汇"

    # 防御性分支本身：手工构造一个「每个节点都有上游」的有效图
    cyclic_eff = EffectiveGraph(
        node_ids=["A", "B"],
        edges=[DerivedEdge(from_node="A", to_node="B"),
               DerivedEdge(from_node="B", to_node="A")],
    )
    assert cyclic_eff.entry_nodes() == []
    assert cyclic_eff.exit_nodes() == []

    report = ValidationReport(mode=ValidationMode.PUBLISH)
    _check_entry_exit(
        GraphSpec(nodes=[node("A"), node("B")], edges=[]),
        cyclic_eff,
        report,
        ValidationMode.PUBLISH,
    )
    assert report.has_code("no_entry")
    assert report.has_code("no_exit")


def test_disabled_entry_leaves_valid_new_entry():
    """入口停用后存在新的合法入口时，不得因旧入口消失就拒绝执行（ACT-02）。"""
    g = graph({"A": ["B"], "B": ["C"], "C": []}, enabled={"A"})
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert report.ok(), [d.message for d in report.errors()]
    assert not report.has_code("no_entry")


def test_isolated_and_multi_branch_nodes_are_valid():
    """WF-05：单节点、多入口、多出口、独立分支均有明确语义，不因「非连通」判错。"""
    g = graph({"A": ["C"], "B": ["C"], "C": ["D", "E"], "Z": []})
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert report.ok(), [d.message for d in report.errors()]


# ---------------------------------------------------------------------------
# 节点可运行性
# ---------------------------------------------------------------------------


def test_node_without_profile_rejected():
    g = GraphSpec(nodes=[node("A", with_profile=False)], edges=[])
    report = validate(g, registry_with())
    assert not report.ok()
    assert report.has_code("node_no_profile")


def test_profile_without_harness_rejected():
    g = GraphSpec(nodes=[node("A", harness=None)], edges=[])
    report = validate(g, registry_with())
    assert not report.ok()
    assert report.has_code("profile_no_harness")


def test_unregistered_harness_rejected():
    g = GraphSpec(nodes=[node("A", harness="ghost")], edges=[])
    report = validate(g, registry_with(harnesses=("h1",)))
    assert not report.ok()
    assert report.has_code("harness_not_registered")


def test_registered_harness_passes():
    g = GraphSpec(nodes=[node("A")], edges=[])
    assert validate(g, registry_with()).ok()


def test_duplicate_profile_id_rejected():
    """CFG-01：候选必须有稳定对齐键，否则重排／删除会错配。

    在**构造点**拒绝（NodeDefinition 的模型校验），比等到校验管线更早失败；
    validate 里的同名检查是防御层，正常路径不会走到。
    """
    n = node("A")
    n.profiles = [profile("same"), profile("same", model_name="m2")]
    with pytest.raises(ValidationError):
        GraphSpec(nodes=[n], edges=[])


def test_unregistered_credential_rejected():
    g = GraphSpec(nodes=[node("A", credential="ghost")], edges=[])
    report = validate(g, registry_with(credentials=("c1",)))
    assert not report.ok()
    assert report.has_code("credential_not_registered")


def test_revoked_credential_rejected():
    """CFG-07：已撤销的访问权限不能被钉扎快照绕过。"""
    g = GraphSpec(nodes=[node("A", credential="c1")], edges=[])
    report = validate(g, registry_with(credentials=("c1",), revoked=("c1",)))
    assert not report.ok()
    assert report.has_code("credential_revoked")


def test_valid_credential_passes():
    g = GraphSpec(nodes=[node("A", credential="c1")], edges=[])
    assert validate(g, registry_with(credentials=("c1",))).ok()


def test_harness_with_auth_binding_but_no_credential_warns():
    """harness 登记了凭据绑定，候选却没绑凭据 —— 要让用户看到。"""
    from workerbee.core.domain import HarnessRegistration

    reg = InMemoryRegistry(
        harnesses=[
            HarnessRegistration(
                harness_id="h1", name="h1", adapter_id="mock", auth_binding="c1",
                last_probe_ok=True,
            )
        ]
    )
    g = GraphSpec(nodes=[node("A", credential=None)], edges=[])
    report = validate(g, reg)
    assert not report.ok()
    assert report.has_code("credential_missing")


def test_missing_skill_and_tool_rejected():
    n = node("A")
    from workerbee.core.domain import VersionedRef

    n.skill_refs = [VersionedRef(ref_id="ghost_skill")]
    n.tool_refs = [VersionedRef(ref_id="ghost_tool")]
    report = validate(GraphSpec(nodes=[n], edges=[]), registry_with())
    assert report.has_code("skill_not_registered")
    assert report.has_code("tool_not_registered")
    assert not report.ok()


def test_disabled_skill_warns_but_does_not_block():
    """EXT-03：引用了已停用的共享配置，要展示影响但不阻断发布。"""
    from workerbee.core.domain import SkillDoc, VersionedRef

    from workerbee.core.domain import HarnessRegistration

    n = node("A")
    n.skill_refs = [VersionedRef(ref_id="s1")]
    reg = InMemoryRegistry(
        harnesses=[HarnessRegistration(harness_id="h1", name="h1", adapter_id="mock",
                                       last_probe_ok=True)],
        skills=[SkillDoc(skill_id="s1", name="s1", enabled=False)],
    )
    report = validate(GraphSpec(nodes=[n], edges=[]), reg)
    assert report.ok(), [d.message for d in report.errors()]
    assert report.has_code("skill_disabled")


def test_disabled_node_is_reported_as_info():
    """ACT-01：停用节点不参与有效图，但要让用户看到它还在定义里。"""
    g = graph({"A": ["B"], "B": []}, enabled={"B"})
    report = validate(g, registry_with())
    assert report.ok()
    assert report.has_code("node_disabled")


# ---------------------------------------------------------------------------
# 能力匹配（CFG-02/05、HUM-03）
# ---------------------------------------------------------------------------


def test_capability_unprobed_is_info_not_error():
    """HAR-02：能力未实测时如实标注，不阻断。"""
    g = GraphSpec(nodes=[node("A")], edges=[])
    report = validate(g, registry_with(harness_capabilities={}))
    assert report.ok()
    assert report.has_code("capability_unprobed")


def test_compact_threshold_without_compact_capability_rejected():
    """CFG-05：harness 不支持整理时不能宣称会自动整理。"""
    g = GraphSpec(nodes=[node("A", compact_threshold=100_000)], edges=[])
    report = validate(
        g, registry_with(harness_capabilities={"h1": {"compact": False}})
    )
    assert not report.ok()
    assert report.has_code("capability_missing")
    diag = next(d for d in report.errors() if d.code == "capability_missing")
    assert diag.slot == "profiles[0].compact_threshold"
    assert diag.requirement == "CFG-05"


def test_compact_threshold_with_capability_passes():
    g = GraphSpec(nodes=[node("A", compact_threshold=100_000)], edges=[])
    report = validate(g, registry_with(harness_capabilities={"h1": {"compact": True}}))
    assert report.ok()


def test_permission_hook_unsupported_rejected():
    """HUM-03：不支持权限钩子的适配器不得声称支持非自动权限模式。"""
    g = GraphSpec(nodes=[node("A")], edges=[])
    report = validate(
        g, registry_with(harness_capabilities={"h1": {"permission_hook": False}})
    )
    assert not report.ok()
    assert report.has_code("capability_missing")


def test_unsupported_reasoning_effort_rejected():
    """CFG-02：不可用参数须提示，不能接受后静默忽略。"""
    g = GraphSpec(nodes=[node("A", reasoning_effort="max")], edges=[])
    report = validate(
        g, registry_with(harness_capabilities={"h1": {"reasoning_efforts": ["low", "high"]}})
    )
    assert not report.ok()
    assert report.has_code("reasoning_effort_unsupported")


def test_supported_reasoning_effort_passes():
    g = GraphSpec(nodes=[node("A", reasoning_effort="high")], edges=[])
    report = validate(
        g, registry_with(harness_capabilities={"h1": {"reasoning_efforts": ["low", "high"]}})
    )
    assert report.ok()


def test_launch_mode_checks_probe_failure():
    """HAR-01：登记过的 harness 探测失败，发射前必须拦住。"""
    from workerbee.core.domain import HarnessRegistration

    reg = InMemoryRegistry(
        harnesses=[
            HarnessRegistration(
                harness_id="h1", name="h1", adapter_id="mock",
                capabilities_snapshot={"compact": True},
                last_probe_ok=False, last_probe_error="executable not found",
            )
        ]
    )
    g = GraphSpec(nodes=[node("A")], edges=[])
    publish = validate(g, reg, mode=ValidationMode.PUBLISH)
    launch = validate(g, reg, mode=ValidationMode.LAUNCH)

    assert not launch.ok()
    assert launch.has_code("harness_probe_failed")
    # 发布期不因运行期的探测失败而阻断——那是运行前检查
    assert not publish.has_code("harness_probe_failed")


# ---------------------------------------------------------------------------
# ACT-03 输入衔接（§4.3 三条规则）
# ---------------------------------------------------------------------------


def _act03_graph(*, waiver: bool = False, contracts: bool = True) -> GraphSpec:
    """A → B → C，C 声明需要 B 加工后的结果 b_result。"""
    nodes = [
        node("A"),
        node("B"),
        node("C", required_inputs=["b_result"]),
    ]
    edges = [edge("A", "B"), edge("B", "C", outputs=["b_result"] if contracts else None)]
    waivers = (
        [ContractWaiver(node_id="C", required_input="b_result", at="2026-01-01T00:00:00+00:00",
                        reason="用户确认以降级方式继续")]
        if waiver
        else []
    )
    # 停用 B：C 的有效上游变成 A
    for n in nodes:
        n.enabled = n.node_id != "B"
    return GraphSpec(nodes=nodes, edges=edges, waivers=waivers)


def test_contract_satisfied_by_direct_upstream():
    """B 在岗时，C 的必需输入由 B 的输出契约满足。"""
    g = _act03_graph()
    g.require_node("B").enabled = True
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert not report.has_code("contract_unsatisfied")
    assert report.ok(), [d.message for d in report.errors()]


def test_bypassed_contract_is_unsatisfied_error():
    """§4.3 规则 1/2：绕过后输入无法满足 → 必须报错，不允许无提示地降级。"""
    g = _act03_graph()
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)

    assert not report.ok()
    assert report.has_code("contract_unsatisfied")
    diag = next(d for d in report.errors() if d.code == "contract_unsatisfied")
    assert diag.node_id == "C"
    assert diag.requirement == "ACT-03"
    assert diag.fix_action == "waive_contract"


def test_explicit_waiver_downgrades_to_info():
    """§4.3 规则 2 的后半段：用户显式确认降级继续后不再阻断，但如实标注。"""
    g = _act03_graph(waiver=True)
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)

    assert report.ok(), [d.message for d in report.errors()]
    assert report.has_code("contract_waived")
    waived = next(d for d in report.infos() if d.code == "contract_waived")
    assert waived.severity == Severity.INFO


def test_no_declared_contract_falls_back_to_text_handoff():
    """§4.3 规则 3：未声明契约的边回退为文本交接，不做机器校验——这是能力边界。"""
    nodes = [node("A"), node("B"), node("C")]
    for n in nodes:
        n.enabled = n.node_id != "B"
    g = GraphSpec(nodes=nodes, edges=[edge("A", "B"), edge("B", "C")])

    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert report.ok(), [d.message for d in report.errors()]
    assert report.has_code("bypass_unverifiable")
    assert not report.has_code("contract_unsatisfied")


def test_bypass_with_contract_is_annotated_as_machine_checked():
    """原路径声明过契约时，绕过要标为「会被机器校验」而不是「无法校验」。"""
    g = _act03_graph(waiver=True)
    report = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    assert report.has_code("bypass_changed_input")
    assert not report.has_code("bypass_unverifiable")


# ---------------------------------------------------------------------------
# 三档模式
# ---------------------------------------------------------------------------


def test_draft_mode_downgrades_errors_to_warnings():
    """WF-01：草稿可保存不完整内容，但不能伪装成可执行流程。"""
    g = GraphSpec(nodes=[node("A", with_profile=False)], edges=[])
    strict = validate(g, registry_with(), mode=ValidationMode.PUBLISH)
    draft = validate(g, registry_with(), mode=ValidationMode.DRAFT)

    assert not strict.ok()
    assert draft.ok()
    assert draft.has_code("node_no_profile")
    assert all(d.severity != Severity.ERROR for d in draft.diagnostics)


def test_draft_mode_still_surfaces_structural_problems():
    """草稿档不下达 ERROR，但环这类结构问题仍要如实展示给用户。"""
    g = GraphSpec(nodes=[node("A"), node("B")], edges=[edge("A", "B"), edge("B", "A")])
    draft = validate(g, registry_with(), mode=ValidationMode.DRAFT)
    assert draft.ok()
    assert draft.has_code("cycle")


def test_draft_mode_skips_executability_checks():
    g = graph({"A": ["B"], "B": []}, enabled={"A", "B"})
    draft = validate(g, registry_with(), mode=ValidationMode.DRAFT)
    assert not draft.has_code("no_enabled_node")


def test_validate_with_enabled_override():
    """预览某次启停后的可执行性，不修改任何状态。"""
    g = graph({"A": ["B"], "B": []})
    before = validate(g, registry_with(), mode=ValidationMode.LAUNCH)
    after = validate(g, registry_with(), mode=ValidationMode.LAUNCH, enabled=set())

    assert before.ok()
    assert after.has_code("no_enabled_node")


# ---------------------------------------------------------------------------
# 启停预览（ACT-02）
# ---------------------------------------------------------------------------


def test_validate_toggle_returns_delta_and_report():
    g = graph({"A": ["B"], "B": ["C"], "C": []})
    delta, report = validate_toggle(g, "B", False, registry_with())

    assert delta.node_id == "B"
    assert delta.enabling is False
    assert {(e.from_node, e.to_node) for e in delta.added_edges} == {("A", "C")}
    assert report.ok()


def test_validate_toggle_does_not_mutate_graph():
    g = graph({"A": ["B"], "B": ["C"], "C": []})
    before = g.canonical_json()
    validate_toggle(g, "B", False, registry_with())
    assert g.canonical_json() == before


def test_validate_toggle_detects_disabling_last_entry():
    g = graph({"A": ["B"], "B": []})
    delta, report = validate_toggle(g, "A", False, registry_with())
    assert delta.entry_nodes_after == ["B"]
    assert report.ok()


def test_validate_toggle_rejects_unknown_node():
    g = graph({"A": []})
    with pytest.raises(KeyError):
        validate_toggle(g, "ghost", False, registry_with())


# ---------------------------------------------------------------------------
# 报告本身
# ---------------------------------------------------------------------------


def test_report_locates_diagnostics():
    """WF-05：错误应定位到节点、连线或配置。"""
    n = node("Alpha", with_profile=False)
    g = GraphSpec(nodes=[n], edges=[])
    report = validate(g, registry_with())
    diag = report.errors()[0]
    assert "Alpha" in diag.location()
    assert report.summary()


def test_diagnostic_edge_projection():
    d = Diagnostic(
        code="x", severity=Severity.INFO, message="m", edge=("a", "b"), node_name="N"
    )
    assert "N" in d.location()
    assert "a" in d.location()
