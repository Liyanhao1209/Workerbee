"""助手草稿提案的协议与解析（workerbee-draft 围栏块）。

断言重点是「不报错但悄悄不生效」的反面：
- 正常块、多个块（只取一个）、坏 JSON、非提案回复——每种形态的行为都钉死；
- 提案能构造出真正的 GraphSpec / Template（字段与定义层对齐）；
- 编造实体 id 的提案，其「待配置」项与校验诊断如实出现，一个都不许丢。
"""

from __future__ import annotations

import json

import pytest

from workerbee.assistant.draft import (
    DraftInvalid,
    DraftProposal,
    collect_pending,
    extract_proposal,
    proposal_graph,
)
from workerbee.core.domain.registry import HarnessRegistration
from workerbee.core.graph.validate import InMemoryRegistry, ValidationMode, validate

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# 提取与解析
# ---------------------------------------------------------------------------

_WORKFLOW_BLOCK = {
    "kind": "workflow",
    "name": "两节点流程",
    "description": "先规划再执行",
    "nodes": [
        {
            "node_id": "planner",
            "name": "规划",
            "role": "规划者",
            "profiles": [{"harness_ref": "h-claude", "model_name": "claude-sonnet"}],
        },
        {"node_id": "coder", "name": "执行", "profiles": []},
    ],
    "edges": [{"from_node": "planner", "to_node": "执行", "output_contract": ["plan"]}],
}


def _reply(payload: dict | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"这是解释。\n\n```workerbee-draft\n{body}\n```\n\n以上。"


def test_extract_happy_path() -> None:
    proposal = extract_proposal(_reply(_WORKFLOW_BLOCK))
    assert proposal is not None
    assert proposal.kind == "workflow"
    assert proposal.name == "两节点流程"
    assert len(proposal.nodes) == 2
    assert proposal.nodes[0].profiles[0].harness_ref == "h-claude"
    assert proposal.edges[0].output_contract == ["plan"]


def test_extract_without_block_returns_none() -> None:
    assert extract_proposal("普通的回答，没有提案。") is None
    # 别的语言标记的代码块不算提案
    assert extract_proposal("```json\n{\"kind\": \"workflow\"}\n```") is None


def test_extract_bad_json_returns_none() -> None:
    assert extract_proposal(_reply("{这不是 json")) is None
    # JSON 合法但不是对象
    assert extract_proposal(_reply("[1, 2, 3]")) is None
    # 对象但字段形状不符（缺 kind）
    assert extract_proposal(_reply({"name": "缺 kind"})) is None


def test_extract_takes_only_the_first_block() -> None:
    """prompt 约定一条回复至多一个提案；模型违规输出多个时只取第一个。"""
    text = _reply({"kind": "workflow", "name": "第一份", "nodes": []}) + "\n" + _reply(
        {"kind": "node_template", "name": "第二份", "nodes": [{"name": "x"}]}
    )
    proposal = extract_proposal(text)
    assert proposal is not None
    assert proposal.name == "第一份"


def test_unknown_fields_are_ignored() -> None:
    """模型多输出的字段被忽略，而不是让整份提案判死。"""
    proposal = extract_proposal(_reply({**_WORKFLOW_BLOCK, "surprise": "多出字段"}))
    assert proposal is not None
    assert proposal.name == "两节点流程"


# ---------------------------------------------------------------------------
# 构造定义层实体
# ---------------------------------------------------------------------------


def test_to_graph_builds_graphspec_and_resolves_edge_by_name() -> None:
    proposal = DraftProposal.model_validate(_WORKFLOW_BLOCK)
    graph = proposal.to_graph()
    assert [n.node_id for n in graph.nodes] == ["planner", "coder"]
    # 边的 to_node 写的是节点名字「执行」，被解析到 node_id
    assert graph.edges[0].key() == ("planner", "coder")
    assert graph.edges[0].output_contract is not None
    assert graph.edges[0].output_contract.outputs == ["plan"]
    # 提案里没指定模型的候选，model_name 落为空（harness 默认语义），不是编造
    assert graph.nodes[0].profiles[0].model_name == "claude-sonnet"


def test_to_graph_missing_node_id_generates_one() -> None:
    proposal = DraftProposal.model_validate(
        {"kind": "workflow", "name": "x", "nodes": [{"name": "甲"}, {"name": "乙"}],
         "edges": [{"from_node": "甲", "to_node": "乙"}]}
    )
    graph = proposal.to_graph()
    assert all(n.node_id for n in graph.nodes)
    assert len(graph.edges) == 1


def test_to_graph_dangling_edge_is_explicit_error() -> None:
    proposal = DraftProposal.model_validate(
        {"kind": "workflow", "name": "x", "nodes": [{"name": "甲"}],
         "edges": [{"from_node": "甲", "to_node": "不存在的节点"}]}
    )
    with pytest.raises(DraftInvalid, match="不存在的节点"):
        proposal.to_graph()


def test_to_graph_requires_nodes() -> None:
    proposal = DraftProposal.model_validate({"kind": "workflow", "name": "x"})
    with pytest.raises(DraftInvalid, match="没有任何节点"):
        proposal.to_graph()


def test_node_template_shape_rules() -> None:
    ok = DraftProposal.model_validate(
        {"kind": "node_template", "name": "T", "nodes": [{"name": "单节点"}]}
    )
    template = ok.to_node_template()
    assert template.kind == "node"
    assert template.payload.nodes[0].name == "单节点"

    two_nodes = DraftProposal.model_validate(
        {"kind": "node_template", "name": "T", "nodes": [{"name": "a"}, {"name": "b"}]}
    )
    with pytest.raises(DraftInvalid, match="恰好包含一个节点"):
        two_nodes.to_node_template()

    with_edges = DraftProposal.model_validate(
        {"kind": "node_template", "name": "T", "nodes": [{"name": "a"}],
         "edges": [{"from_node": "a", "to_node": "a"}]}
    )
    with pytest.raises(DraftInvalid, match="不应该包含连线"):
        with_edges.to_node_template()


def test_proposal_graph_wraps_node_template() -> None:
    """node_template 包成单节点图，走同一条校验管线。"""
    proposal = DraftProposal.model_validate(
        {"kind": "node_template", "name": "T",
         "nodes": [{"name": "单节点", "profiles": [{"harness_ref": "h1"}]}]}
    )
    graph = proposal_graph(proposal)
    assert len(graph.nodes) == 1
    assert graph.edges == []


# ---------------------------------------------------------------------------
# 「待配置」与校验：编造的引用必须如实出现
# ---------------------------------------------------------------------------


def test_collect_pending_flags_unknown_and_missing_refs() -> None:
    proposal = DraftProposal.model_validate(
        {
            "kind": "workflow",
            "name": "x",
            "nodes": [
                {
                    "name": "甲",
                    "profiles": [
                        {"harness_ref": "编造的harness", "credential_ref": "编造的凭据"},
                        {},  # 整组留空
                    ],
                    "skill_refs": ["编造的skill"],
                    "tool_refs": ["编造的工具"],
                },
                {"name": "乙"},  # 没有候选
            ],
        }
    )
    pending = collect_pending(
        proposal,
        harness_ids=["h-real"],
        credential_ids=["c-real"],
        skill_ids=["s-real"],
        tool_ids=["t-real"],
    )
    text = "\n".join(pending)
    assert "编造的harness" in text
    assert "编造的凭据" in text
    assert "编造的skill" in text
    assert "编造的工具" in text
    assert "未指定 harness" in text  # 留空的那组候选
    assert "没有执行候选" in text  # 乙


def test_collect_pending_quiet_when_refs_are_known() -> None:
    proposal = DraftProposal.model_validate(_WORKFLOW_BLOCK)
    pending = collect_pending(
        proposal,
        harness_ids=["h-claude"],
        credential_ids=[],
        skill_ids=[],
        tool_ids=[],
    )
    # 乙没有候选是唯一如实该出现的项
    assert pending == ["节点「执行」没有执行候选：采用后需要在编辑器里补模型与 harness"]


def test_validation_reports_fabricated_refs() -> None:
    """提案经草稿档校验后，编造的引用以 WARNING 出现在诊断里（草稿不阻断，但如实）。"""
    proposal = DraftProposal.model_validate(_WORKFLOW_BLOCK)
    graph = proposal_graph(proposal)
    registry = InMemoryRegistry(
        harnesses=[
            HarnessRegistration(harness_id="h-real", name="真机", adapter_id="mock")
        ]
    )
    report = validate(graph, registry, mode=ValidationMode.DRAFT)
    assert report.ok()  # 草稿档：不阻断
    assert report.has_code("harness_not_registered")
    assert report.has_code("node_no_profile")
    warning = next(d for d in report.diagnostics if d.code == "harness_not_registered")
    assert warning.severity == "warning"
    assert "h-claude" in warning.message
