"""边与输出契约（架构设计 v0.02 §5.1 Edge / §4.3 ACT-03）。

契约语义：
- ``EdgeContract.outputs`` 声明「上游在这条边上交付哪些字段」。
- 下游的必需输入声明在 ``NodeDefinition.required_inputs``（不是边上），
  因为停用 B 后 B→C 这条边消失，若需求挂在边上则需求也随之消失，
  ACT-03 的检查将失去依据。
- 契约整体可选。未声明契约的边回退为文本交接，不做机器校验（§4.3 第 3 条），
  这是如实声明的能力边界。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import DomainModel

__all__ = ["EdgeContract", "Edge", "ContractWaiver", "Sensitivity"]

#: 产物敏感级，沿血缘取最高级（§5.4、§9.4）
Sensitivity = Literal["public", "internal", "sensitive"]

#: 契约声明的载荷形态。仅作提示与 UI 展示，不作机器判据。
ContractFormat = Literal["text", "markdown", "json", "code", "file", "any"]


class EdgeContract(DomainModel):
    """一条边上的输出契约。整体可选；声明了才参与机器校验。"""

    outputs: list[str] = Field(default_factory=list)
    """上游在这条边上交付的字段名清单。下游的 required_inputs 逐项与并集比对。"""

    format: ContractFormat = "any"
    """载荷形态提示（json / code / markdown …），供 ContextPackage 组装与 UI 展示。"""

    description: str | None = None
    """该边交接内容的自然语言说明，可 AI 生成，仅供展示与摘要，不作机器判据。"""

    example: str | None = None
    """格式示例，注入 ContextPackage 的 P3 输出要求分区（§7.3）。"""

    def covers(self, field_name: str) -> bool:
        return field_name in self.outputs


class ContractWaiver(DomainModel):
    """用户对「输入衔接校验失败」的显式降级确认（§4.3 第 2 条）。

    没有它就不允许把 A 的原始输出当作 B 的结果。有它才允许降级继续，
    并被历史与 UI 如实标注。
    """

    node_id: str
    """声明了该必需输入的下游节点。"""

    required_input: str
    """无法由有效上游满足的输入字段名。"""

    reason: str | None = None
    """用户填写或系统生成的降级理由。"""

    at: str
    """确认时间（ISO8601）。"""


class Edge(DomainModel):
    """定义图 G₀ 中的一条有向边。拓扑的唯一事实源。"""

    from_node: str
    to_node: str
    output_contract: EdgeContract | None = None
    desc: str | None = None
    """关系描述，可 AI 生成；展示用途，不作机器判据。"""

    def key(self) -> tuple[str, str]:
        return (self.from_node, self.to_node)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"<Edge {self.from_node[:8]}->{self.to_node[:8]}>"
