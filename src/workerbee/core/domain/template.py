"""模板（架构设计 v0.02 §5.1 Template、TPL-01/02/03）。

模板的硬边界：
- **不含任何运行时内容**：队列、活跃 session、审批状态、执行结果（``excludes``）。
- **不携带明文凭据**：密钥本体永远进不了模板（TPL-03、AC-09）。本机凭据的
  **引用**（credential_id，只是一个指针）默认保留在模板里，同机实例化时自动
  重绑定；跨机使用时由服务层校验，引用解析不到再要求手动绑定。
- 模板编辑不静默改写已创建流程；同步更新需 diff + 显式应用。

本模块把「不含密钥」实现为**结构性保证**而非约定：模板载荷只接受
``TemplateNodeConfig``，它的凭据字段类型天生只能装引用，装不下密钥本体。
"""

from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel, Entity, new_id
from .edge import Edge, EdgeContract
from .node import ExecutionProfile, NodeDefinition, VersionedRef

__all__ = [
    "TemplateKind",
    "CredentialPlaceholder",
    "TemplateNodeConfig",
    "TemplatePayload",
    "Template",
    "MissingBinding",
    "InstantiationReport",
]


class TemplateKind(StrEnum):
    WORKFLOW = "workflow"
    NODE = "node"


class CredentialPlaceholder(DomainModel):
    """模板中的凭据占位。

    只记录「原流程此处的凭据叫什么」，不含 locator，更不含密钥。
    引用保留在模板里时，实例化优先沿用原引用；引用在本机解析不到
    （共享给他人的模板）时才要求重绑定到接收方已登记的凭据（TPL-03）。
    """

    slot: str
    """占位槽位名，例如 ``profiles[0].credential_ref``。"""

    original_label: str | None = None
    """原凭据的 label，仅用于实例化时给出人类可读的提示。"""

    original_kind: str | None = None


class TemplateNodeConfig(DomainModel):
    """节点的可复用配置（TPL-02）。

    不固化原 Workflow 中不可复用的相邻节点关系或运行状态。
    """

    name: str
    role: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    profiles: list[ExecutionProfile] = Field(default_factory=list)
    skill_refs: list[VersionedRef] = Field(default_factory=list)
    tool_refs: list[VersionedRef] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)


class TemplatePayload(DomainModel):
    """模板载荷：拓扑 + 配置快照。

    ``sensitive_slots`` 记录每个凭据槽位的来源；引用本身默认留在
    ``profiles[].credential_ref`` 里供同机自动绑定。
    """

    nodes: list[TemplateNodeConfig] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    sensitive_slots: list[CredentialPlaceholder] = Field(default_factory=list)


class Template(Entity):
    """可复用的流程模板或节点模板。"""

    template_id: str = Field(default_factory=new_id)
    name: str
    description: str | None = None
    kind: TemplateKind = TemplateKind.WORKFLOW

    payload: TemplatePayload = Field(default_factory=TemplatePayload)

    version: int = 1
    source_revision: int | None = None
    """来源修订，供 TPL-03 的「同步更新」给出 diff 基础。"""

    source_workflow_id: str | None = None

    @model_validator(mode="after")
    def _kind_shape(self) -> "Template":
        if self.kind == TemplateKind.NODE and len(self.payload.nodes) > 1:
            raise ValueError("节点模板只能包含一个节点配置")
        return self

    # ---- 构造 ----

    @classmethod
    def from_nodes(
        cls,
        nodes: list[NodeDefinition],
        edges: list[Edge] | None = None,
        *,
        name: str,
        kind: TemplateKind,
        source_workflow_id: str | None = None,
        source_revision: int | None = None,
        description: str | None = None,
        keep_credential_refs: bool = True,
    ) -> "Template":
        """从定义层实体构造模板。**密钥本体永远进不了模板**（类型上装不下）。

        ``keep_credential_refs=True``（默认）时把本机凭据的引用一并保留：
        引用只是指向本机凭据库的 id，不含任何密钥材料，同机实例化即可自动
        绑定，省去逐个重选。跨机分享模板时可传 False 退回全剥离形态。

        这是模板的唯一构造入口：绕过它无法产生含凭据的模板。
        """
        slots: list[CredentialPlaceholder] = []
        configs: list[TemplateNodeConfig] = []

        for node in nodes:
            profiles = copy.deepcopy(node.profiles)
            for idx, profile in enumerate(profiles):
                if profile.credential_ref:
                    slots.append(
                        CredentialPlaceholder(
                            slot=f"{node.name}.profiles[{idx}].credential_ref",
                            original_label=profile.credential_ref,
                        )
                    )
                    if not keep_credential_refs:
                        profile.credential_ref = None
            configs.append(
                TemplateNodeConfig(
                    name=node.name,
                    role=node.role,
                    description=node.description,
                    system_prompt=node.system_prompt,
                    profiles=profiles,
                    skill_refs=copy.deepcopy(node.skill_refs),
                    tool_refs=copy.deepcopy(node.tool_refs),
                    required_inputs=list(node.required_inputs),
                )
            )

        return cls(
            name=name,
            description=description,
            kind=kind,
            payload=TemplatePayload(
                nodes=configs,
                edges=copy.deepcopy(edges or []),
                sensitive_slots=slots,
            ),
            source_workflow_id=source_workflow_id,
            source_revision=source_revision,
        )

    # ---- 实例化 ----

    def instantiate(
        self,
        bindings: dict[str, str] | None = None,
        *,
        node_id_map: dict[str, str] | None = None,
    ) -> tuple[list[NodeDefinition], list[Edge], "InstantiationReport"]:
        """实例化为定义层实体。

        ``bindings`` 以 ``slot`` 或 ``原始 label`` 为键，值为本机 credential_id。
        显式绑定优先；没有显式绑定但模板保留了原引用时沿用原引用（同机复用）。
        两者都没有的必填槽位不会伪造，而是如实进入报告，要求用户补齐（TPL-03）。
        """
        bindings = bindings or {}
        node_id_map = node_id_map or {}
        missing: list[MissingBinding] = []
        carried: list[str] = []

        out_nodes: list[NodeDefinition] = []
        for cfg in self.payload.nodes:
            profiles = copy.deepcopy(cfg.profiles)
            for idx, profile in enumerate(profiles):
                slot = f"{cfg.name}.profiles[{idx}].credential_ref"
                placeholder = next(
                    (s for s in self.payload.sensitive_slots if s.slot == slot), None
                )
                if placeholder is None:
                    continue
                bound = bindings.get(slot)
                if bound is None and placeholder.original_label is not None:
                    bound = bindings.get(placeholder.original_label)
                if bound is None and profile.credential_ref:
                    # 模板保留了原引用：同机实例化直接沿用。引用是否在本机
                    # 可解析由服务层校验（域层不碰注册表）。
                    carried.append(slot)
                    continue
                if bound is None:
                    missing.append(
                        MissingBinding(
                            slot=slot,
                            label=placeholder.original_label,
                            reason="模板未携带凭据引用；实例化时必须绑定本机凭据",
                        )
                    )
                    continue
                profile.credential_ref = bound

            node = NodeDefinition(
                node_id=new_id(),
                name=cfg.name,
                role=cfg.role,
                description=cfg.description,
                system_prompt=cfg.system_prompt,
                profiles=profiles,
                skill_refs=copy.deepcopy(cfg.skill_refs),
                tool_refs=copy.deepcopy(cfg.tool_refs),
                required_inputs=list(cfg.required_inputs),
            )
            out_nodes.append(node)
            node_id_map.setdefault(cfg.name, node.node_id)

        out_edges = _remap_edges(self.payload.edges, self.payload.nodes, node_id_map)

        report = InstantiationReport(
            missing_bindings=missing,
            usable=not missing,
            notes=_template_notes(self, carried),
        )
        return out_nodes, out_edges, report


def _remap_edges(
    edges: list[Edge], configs: list[TemplateNodeConfig], node_id_map: dict[str, str]
) -> list[Edge]:
    """模板边按节点**名称**索引，实例化时映射到新生成的 node_id。"""
    name_to_idx = {cfg.name: i for i, cfg in enumerate(configs)}
    out: list[Edge] = []
    for e in edges:
        from_id = _resolve(e.from_node, configs, name_to_idx, node_id_map)
        to_id = _resolve(e.to_node, configs, name_to_idx, node_id_map)
        if from_id is None or to_id is None:
            continue  # 名称无法对应时丢弃该边，由校验管线报「入口/出口」问题
        out.append(
            Edge(
                from_node=from_id,
                to_node=to_id,
                output_contract=copy.deepcopy(e.output_contract),
                desc=e.desc,
            )
        )
    return out


def _resolve(
    ref: str,
    configs: list[TemplateNodeConfig],
    name_to_idx: dict[str, int],
    node_id_map: dict[str, str],
) -> str | None:
    if ref in node_id_map:
        return node_id_map[ref]
    idx = name_to_idx.get(ref)
    if idx is None:
        return None
    return node_id_map.get(configs[idx].name)


def _template_notes(t: Template, carried: list[str] | None = None) -> list[str]:
    notes: list[str] = []
    if carried:
        notes.append(f"{len(carried)} 处凭据引用已随模板保留，实例化时自动沿用")
    unbound = len(t.payload.sensitive_slots) - len(carried or [])
    if t.kind == TemplateKind.WORKFLOW and unbound > 0:
        notes.append(f"模板含 {unbound} 处凭据占位，需要绑定本机凭据")
    return notes


class MissingBinding(DomainModel):
    """实例化时缺失的凭据绑定，必须显式补配置而不是编造（WF-02 的同一条纪律）。"""

    slot: str
    label: str | None = None
    reason: str


class InstantiationReport(DomainModel):
    missing_bindings: list[MissingBinding] = Field(default_factory=list)
    usable: bool = True
    notes: list[str] = Field(default_factory=list)


class TemplateDiff(DomainModel):
    """模板 vs 已创建流程的差异（TPL-03：同步更新需 diff + 显式应用）。"""

    added_nodes: list[str] = Field(default_factory=list)
    removed_nodes: list[str] = Field(default_factory=list)
    changed_nodes: list[str] = Field(default_factory=list)
    changed_edges: list[tuple[str, str]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
