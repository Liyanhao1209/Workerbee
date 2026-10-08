"""Workflow 定义与修订（架构设计 v0.02 §5.1）。

两个不可动摇的约束：
1. 修订不可变。任何有效编辑产生新 revision；任务发射时钉扎 revision 与有效图版本，
   在途任务按钉扎版本执行至结束（WF-06、CFG-07、§1.2 原则 2）。
2. 有效图是派生结果，不持久化为可编辑对象，不接受直接编辑（§1.2 原则 1）。
   ``effective_graph_version`` 只是 enabled 集合与边集的指纹，用于钉扎与比对。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .base import Entity, new_id
from .edge import ContractWaiver, Edge
from .node import NodeDefinition

__all__ = [
    "WorkflowStatus",
    "RevisionSource",
    "GraphSpec",
    "WorkflowDefinition",
    "WorkflowRevision",
]


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    DELETED = "deleted"
    """逻辑删除：定义与历史保留供回看，用户不可恢复（LIFE-05、清单 §2）。"""


class RevisionSource(StrEnum):
    MANUAL = "manual"
    AI_GENERATED = "ai_generated"
    GRAPH_CAPTURE = "graph_capture"
    TEMPLATE = "template"


class GraphSpec(Entity):
    """定义图 G₀ 的完整快照：节点集 + 边集。

    G₀ 恒为 DAG——由校验管线（WF-05）在发布前保证，不由本类保证。
    """

    nodes: list[NodeDefinition] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    waivers: list[ContractWaiver] = Field(default_factory=list)
    """用户对输入衔接校验失败的显式降级确认（§4.3 第 2 条）。"""

    # ---- 索引（派生，不参与序列化语义） ----

    def node_map(self) -> dict[str, NodeDefinition]:
        return {n.node_id: n for n in self.nodes}

    def node(self, node_id: str) -> NodeDefinition | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def require_node(self, node_id: str) -> NodeDefinition:
        n = self.node(node_id)
        if n is None:
            raise KeyError(f"节点不存在: {node_id}")
        return n

    def enabled_node_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes if n.enabled}

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.from_node == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.to_node == node_id]

    def successor_ids(self, node_id: str) -> set[str]:
        return {e.to_node for e in self.edges if e.from_node == node_id}

    def predecessor_ids(self, node_id: str) -> set[str]:
        return {e.from_node for e in self.edges if e.to_node == node_id}

    def edge(self, from_node: str, to_node: str) -> Edge | None:
        for e in self.edges:
            if e.from_node == from_node and e.to_node == to_node:
                return e
        return None

    def waiver_for(self, node_id: str, required_input: str) -> ContractWaiver | None:
        for w in self.waivers:
            if w.node_id == node_id and w.required_input == required_input:
                return w
        return None

    # ---- 指纹 ----

    def canonical_json(self) -> str:
        """稳定序列化：节点按 id 排序、边按 (from,to) 排序，属性键排序。

        enabled 标志参与指纹——启停会改变有效图，必须改变版本号（ACT-01）。
        """
        payload: dict[str, Any] = {
            "nodes": sorted(
                (
                    {
                        "node_id": n.node_id,
                        "name": n.name,
                        "enabled": n.enabled,
                        "profiles": [p.model_dump(mode="json") for p in n.profiles],
                        "system_prompt": n.system_prompt,
                        "required_inputs": sorted(n.required_inputs),
                        "skill_refs": [r.model_dump(mode="json") for r in n.skill_refs],
                        "tool_refs": [r.model_dump(mode="json") for r in n.tool_refs],
                    }
                    for n in self.nodes
                ),
                key=lambda d: d["node_id"],
            ),
            "edges": sorted(
                (
                    {
                        "from": e.from_node,
                        "to": e.to_node,
                        "contract": e.output_contract.model_dump(mode="json")
                        if e.output_contract
                        else None,
                    }
                    for e in self.edges
                ),
                key=lambda d: (d["from"], d["to"]),
            ),
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def edge_set_hash(self) -> str:
        """仅边集与启用集合的指纹，用于 effective_graph_version。"""
        payload = {
            "enabled": sorted(self.enabled_node_ids()),
            "edges": sorted((e.from_node, e.to_node) for e in self.edges),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def effective_graph_version(self) -> int:
        """有效图版本：由 enabled 集合与边集哈希派生（§5.1）。

        只取前 15 个 hex 字符（60 bit），足以避免单机规模下的碰撞，
        且不会超出 SQLite INTEGER 与 JS Number 的安全整数范围——前端要读它。
        """
        return int(self.edge_set_hash()[:15], 16)

    @model_validator(mode="after")
    def _unique_node_ids(self) -> "GraphSpec":
        seen: set[str] = set()
        for n in self.nodes:
            if n.node_id in seen:
                raise ValueError(f"重复的 node_id: {n.node_id}")
            seen.add(n.node_id)
        return self

    @model_validator(mode="after")
    def _edges_reference_existing_nodes(self) -> "GraphSpec":
        ids = {n.node_id for n in self.nodes}
        for e in self.edges:
            if e.from_node not in ids:
                raise ValueError(f"悬空边引用：from_node {e.from_node} 不存在")
            if e.to_node not in ids:
                raise ValueError(f"悬空边引用：to_node {e.to_node} 不存在")
        return self

    @model_validator(mode="after")
    def _no_duplicate_edges(self) -> "GraphSpec":
        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            k = e.key()
            if k in seen:
                raise ValueError(f"重复的边：{k[0]} -> {k[1]}")
            seen.add(k)
        return self


class WorkflowDefinition(Entity):
    """可复用的流程定义。"""

    workflow_id: str = Field(default_factory=new_id)
    name: str
    description: str | None = None

    current_revision_seq: int = 0
    """指向当前已发布（或当前编辑中的草稿）修订。0 表示尚无修订。"""

    status: WorkflowStatus = WorkflowStatus.DRAFT

    max_concurrent_tasks: int = 8
    """该 Workflow 并发任务上限（容量不足时的背压入口，D-03）。"""

    workspace_id: str = "default"
    """所属工作区（v0.03 §3）。任务执行的工作目录由它解析到 workspace.root_dir。"""

    def is_usable(self) -> bool:
        """可用 = 已发布且未被逻辑删除。AI-02 判定助手引用的 Workflow 是否可用。"""
        return self.status == WorkflowStatus.PUBLISHED


class WorkflowRevision(Entity):
    """不可变快照。任何有效编辑产生新 revision。"""

    workflow_id: str
    revision_seq: int

    graph: GraphSpec = Field(default_factory=GraphSpec)

    source: RevisionSource = RevisionSource.MANUAL
    """建图入口留痕（AC-16）。"""

    draft_of: int | None = None
    """AI／Capture 产物的草稿来源与 diff 基础（HUM-05）。"""

    is_published: bool = False
    """草稿可保存不完整内容，但不能发射任务（WF-01）。"""

    note: str | None = None
    """本次修订的说明，例如「停用节点 N2」——回看时可读。"""

    def effective_graph_version(self) -> int:
        return self.graph.effective_graph_version()

    def immutable_guard(self) -> None:
        """修订不可变的运行时断言。任何写路径都应先通过它。"""
        raise RuntimeError(
            f"WorkflowRevision(workflow={self.workflow_id}, seq={self.revision_seq}) "
            "是不可变快照；编辑必须创建新 revision"
        )
