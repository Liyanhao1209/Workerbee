"""助手草稿提案（Base Assistant 建图提案）的协议与解析。

模型在回复正文里输出一个围栏块（语言标记 ``workerbee-draft``），内容是一个 JSON：

.. code-block:: text

    ```workerbee-draft
    {"kind": "workflow", "name": "...", "nodes": [...], "edges": [...]}
    ```

字段名与 :class:`GraphSpec` / :class:`NodeDefinition` 对齐，使提案可以直接
构造出定义层实体，再过既有的校验管线（WF-05）。

硬边界（与助手的第一版边界一致）：
- **提案不是创建**。本模块只产出「待用户决定的草稿」；真正的创建发生在用户
  点「采用」之后，且走与手动建图完全相同的服务层入口。
- **不编造引用**。提案引用的 harness/凭据/Skill/工具只能是快照清单里列出的
  实体；清单外的 id 会被 ``collect_pending`` 如实标为「待配置」，而不是被
  静默接受。
- **解析永远不为一次问答判死刑**。提取不到块、JSON 损坏、字段缺失——一律
  返回 ``None``，回复照常显示，只是没有提案卡片。
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.domain.base import new_id
from ..core.domain.edge import Edge, EdgeContract
from ..core.domain.node import ExecutionProfile, NodeDefinition, VersionedRef
from ..core.domain.template import (
    Template,
    TemplateKind,
    TemplateNodeConfig,
    TemplatePayload,
)
from ..core.domain.workflow import GraphSpec

__all__ = [
    "DraftInvalid",
    "DraftProposal",
    "DraftNode",
    "DraftProfile",
    "DraftEdge",
    "FENCE_LANG",
    "extract_proposal",
    "proposal_graph",
    "collect_pending",
]

#: 提案块的围栏语言标记。
FENCE_LANG = "workerbee-draft"

_FENCE_RE = re.compile(r"```workerbee-draft\s*\n(?P<body>.*?)```", re.DOTALL)


class DraftInvalid(RuntimeError):
    """提案能解析成 JSON、但内容无法构造出合法实体（大白话中文，可直接展示）。"""


class _LenientModel(BaseModel):
    """提案模型基类：模型多输出的字段被忽略，而不是让整份提案因一个多余键判死——

    卡片要展示的是提案主体，不是字段洁癖。
    """

    model_config = ConfigDict(extra="ignore")


class DraftProfile(_LenientModel):
    """提案里的一组执行候选。全部字段可空——留空即「待配置」。"""

    harness_ref: str | None = None
    model_name: str | None = None
    credential_ref: str | None = None
    reasoning_effort: str | None = None


class DraftNode(_LenientModel):
    """提案里的一个节点。``node_id`` 可省：缺失时构造阶段生成，

    边也可以用节点**名字**引用它（模型更常写名字而不是 id）。
    """

    node_id: str | None = None
    name: str
    role: str | None = None
    system_prompt: str | None = None
    profiles: list[DraftProfile] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    """Skill 的 id 清单（不带版本，跟随最新）。"""
    tool_refs: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    basis: Literal["observed", "inferred"] | None = None
    """流程捕获专用：这个节点是「观察到的」（有材料佐证）还是「推断的」（模型补的）。
    助手提案不使用该字段（留空）；捕获合成会强制填写并做服务端复核。"""
    evidence: list[str] = Field(default_factory=list)
    """流程捕获专用：佐证材料里的引用（``E<event_id>`` / ``A<artifact_id>``）。"""


class DraftEdge(_LenientModel):
    """提案里的一条边。端点可以是 node_id，也可以是节点名字。"""

    from_node: str
    to_node: str
    output_contract: list[str] = Field(default_factory=list)
    """该边交付的字段名清单；空表示不声明契约（文本交接）。"""
    basis: Literal["observed", "inferred"] | None = None
    """流程捕获专用：这条依赖是观察到的还是推断的（同 DraftNode.basis）。"""
    evidence: list[str] = Field(default_factory=list)
    """流程捕获专用：佐证材料里的引用（``E<event_id>`` / ``A<artifact_id>``）。"""


class DraftProposal(_LenientModel):
    """一份解析成功的草稿提案。"""

    kind: Literal["workflow", "node_template"]
    name: str
    description: str | None = None
    notes: list[str] = Field(default_factory=list)
    """模型自己标注的说明，典型用法是列出「待配置」项。"""
    nodes: list[DraftNode] = Field(default_factory=list)
    edges: list[DraftEdge] = Field(default_factory=list)

    # ---- 构造定义层实体 ----

    def to_graph(self) -> GraphSpec:
        """构造 workflow 提案的 :class:`GraphSpec`。

        节点 id 缺失时生成新 id；边的端点先按 node_id 解析，解析不到再按
        节点名字解析。两端都解析不到的边是提案内容错误，显式报出。
        """
        if not self.nodes:
            raise DraftInvalid("提案里没有任何节点，无法生成流程草稿")
        nodes = [_build_node(n) for n in self.nodes]
        ref_map = _ref_map(nodes)
        edges = [
            _build_edge(e, ref_map, index=i + 1) for i, e in enumerate(self.edges)
        ]
        return GraphSpec(nodes=nodes, edges=edges)

    def to_node(self) -> NodeDefinition:
        """节点模板提案的单节点（恰好一个、不带边，否则显式报出）。"""
        if len(self.nodes) != 1:
            raise DraftInvalid(
                f"节点模板提案应该恰好包含一个节点，这份提案里有 {len(self.nodes)} 个"
            )
        if self.edges:
            raise DraftInvalid("节点模板提案不应该包含连线（edges）")
        return _build_node(self.nodes[0])

    def to_node_template(self) -> Template:
        """构造节点模板提案的 :class:`Template`（恰好一个节点，不带边）。"""
        node = self.to_node()
        return Template.from_nodes(
            [node],
            [],
            name=self.name,
            kind=TemplateKind.NODE,
            description=self.description,
        )


def extract_proposal(text: str) -> DraftProposal | None:
    """从回复正文中提取提案块。

    - 没有 ``workerbee-draft`` 围栏块 → ``None``（这不是错误，大多数回复没有提案）；
    - 有多个块 → 只取第一个（prompt 约定一条回复至多一个提案，多出来的忽略）；
    - 块内 JSON 损坏或字段形状不符 → ``None``（回复照常显示，只是没有卡片）。
    """
    match = _FENCE_RE.search(text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group("body"))
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return DraftProposal.model_validate(raw)
    except ValueError:
        return None


def proposal_graph(proposal: DraftProposal) -> GraphSpec:
    """把提案包成可走校验管线的图：workflow 直接用；node_template 包成单节点图。

    节点模板没有连线，单节点图让它共享同一条 WF-05 校验管线
    （候选完整性、引用存在性、草稿档降级口径全都一致）。
    """
    if proposal.kind == "workflow":
        return proposal.to_graph()
    return GraphSpec(nodes=[proposal.to_node()])


def collect_pending(
    proposal: DraftProposal,
    *,
    harness_ids: Iterable[str],
    credential_ids: Iterable[str],
    skill_ids: Iterable[str],
    tool_ids: Iterable[str],
) -> list[str]:
    """把提案里的「待配置」项如实列出来：留空的槽位 + 引用了清单外实体的槽位。

    返回的是大白话中文说明，直接进 validation 供前端标黄展示。
    这不是校验结论的替代品——校验管线（draft 档）会给同一事实出 WARNING；
    这里是给「一眼看清还差什么」的汇总。
    """
    known_harness = set(harness_ids)
    known_credential = set(credential_ids)
    known_skill = set(skill_ids)
    known_tool = set(tool_ids)

    pending: list[str] = []
    for node in proposal.nodes:
        label = f"节点「{node.name}」"
        if not node.profiles:
            pending.append(f"{label}没有执行候选：采用后需要在编辑器里补模型与 harness")
        for idx, profile in enumerate(node.profiles):
            slot = f"{label}的第 {idx + 1} 组候选"
            if not profile.harness_ref:
                pending.append(f"{slot}未指定 harness（待配置）")
            elif profile.harness_ref not in known_harness:
                pending.append(
                    f"{slot}引用了清单外的 harness「{profile.harness_ref}」（待配置或改选）"
                )
            if profile.credential_ref and profile.credential_ref not in known_credential:
                pending.append(
                    f"{slot}引用了清单外的凭据「{profile.credential_ref}」（待配置或改选）"
                )
        for ref in node.skill_refs:
            if ref not in known_skill:
                pending.append(f"{label}引用了清单外的 Skill「{ref}」（待配置或移除）")
        for ref in node.tool_refs:
            if ref not in known_tool:
                pending.append(f"{label}引用了清单外的工具「{ref}」（待配置或移除）")
    return pending


# ---------------------------------------------------------------------------
# 内部：提案 → 领域实体
# ---------------------------------------------------------------------------


def _build_node(draft: DraftNode) -> NodeDefinition:
    return NodeDefinition(
        node_id=draft.node_id or new_id(),
        name=draft.name,
        role=draft.role,
        system_prompt=draft.system_prompt,
        profiles=[
            ExecutionProfile(
                harness_ref=p.harness_ref,
                model_name=p.model_name or "",
                credential_ref=p.credential_ref,
                reasoning_effort=p.reasoning_effort,
            )
            for p in draft.profiles
        ],
        skill_refs=[VersionedRef(ref_id=r) for r in draft.skill_refs],
        tool_refs=[VersionedRef(ref_id=r) for r in draft.tool_refs],
        required_inputs=list(draft.required_inputs),
    )


def _ref_map(nodes: list[NodeDefinition]) -> dict[str, str]:
    """边的解析表：node_id 与节点名字都映射到 node_id（id 优先）。"""
    mapping: dict[str, str] = {}
    for n in nodes:
        mapping.setdefault(n.name, n.node_id)
    for n in nodes:
        mapping[n.node_id] = n.node_id
    return mapping


def _build_edge(draft: DraftEdge, ref_map: dict[str, str], *, index: int) -> Edge:
    from_id = ref_map.get(draft.from_node)
    to_id = ref_map.get(draft.to_node)
    if from_id is None or to_id is None:
        missing = draft.from_node if from_id is None else draft.to_node
        raise DraftInvalid(f"第 {index} 条连线引用了提案里不存在的节点「{missing}」")
    return Edge(
        from_node=from_id,
        to_node=to_id,
        output_contract=EdgeContract(outputs=list(draft.output_contract))
        if draft.output_contract
        else None,
    )
