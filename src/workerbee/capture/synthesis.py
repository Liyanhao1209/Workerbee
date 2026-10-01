"""捕获草案的合成与复核（WF-03、D-12、AC-16）。

合成是**显式触发的一次模型调用**（用户点「生成流程草案」才发生，费用可控），
复用助手的 LLM 后端配置与 ``workerbee-draft`` 提案协议，外加两条捕获专属纪律：

1. **每个节点/边必须标注 ``basis``**：``observed``（有材料佐证）或
   ``inferred``（模型补的衔接）。标注了 observed 就必须给出 ``evidence``——
   材料里真实出现过的事件/产物 id（``E<id>`` / ``A<id>``）。
2. **服务端复核（模型不能给自己贴金）**：标了 observed 但 evidence 一个都
   对不上本次材料的，强制降级为 inferred，降级事实写进草案说明与事件——
   用户看到的是复核后的结论，不是模型的自我评价。

解析坏、模型不可用、提案构造失败都如实抛 :class:`CaptureError`——
报告在捕获记录上，可重试；已跑完的任务结果不受影响。
"""

from __future__ import annotations

from typing import Any

from ..assistant.draft import DraftProposal
from .material import CaptureMaterial

__all__ = ["build_synthesis_prompt", "review_basis", "SYSTEM_PROMPT"]


def build_synthesis_prompt(
    material: CaptureMaterial,
    *,
    base_profile: dict[str, Any],
    harness_ids: list[str],
    credential_ids: list[str],
    skill_ids: list[str],
    tool_ids: list[str],
) -> str:
    """拼合成的用户消息：材料正文 + 输出协议 + 可引用清单。"""
    return _USER_TEMPLATE.format(
        material=material.text or "（这次执行没有留下可用材料）",
        harness_ref=base_profile.get("harness_ref") or "",
        model_name=base_profile.get("model_name") or "（harness 默认）",
        credential_ref=base_profile.get("credential_ref") or "（无）",
        harness_ids="、".join(harness_ids) or "（无）",
        credential_ids="、".join(credential_ids) or "（无）",
        skill_ids="、".join(skill_ids) or "（无）",
        tool_ids="、".join(tool_ids) or "（无）",
    )


SYSTEM_PROMPT = """你是 Workerbee 的流程捕获分析器。给你一次任务真实执行的材料
（任务输入、工具调用序列、模型当时显式写出的执行计划、产物清单、用量），
把这次执行整理成一个**可复用的流程草案**。

纪律：
- 只依据给出的材料。材料里没有的步骤就是「推断的」，不要假装观察到过。
- 材料里没有显式计划时，阶段划分几乎只能是推断——如实标注，不要编计划。
- 每个节点默认沿用捕获时的基础候选（harness / 模型 / 凭据），除非材料明确
  显示某一步需要不同的执行者。
- 只能引用下面清单里列出的 harness/凭据/Skill/工具 id；清单外的不要编造，
  对应字段留空并在 notes 里写「待配置：缺×××」。"""

_USER_TEMPLATE = """## 本次执行的材料

{material}

## 捕获时的基础候选

- harness：{harness_ref}
- 模型：{model_name}
- 凭据：{credential_ref}

## 可引用的注册表清单

- harness：{harness_ids}
- 凭据：{credential_ids}
- Skill：{skill_ids}
- 工具：{tool_ids}

## 输出要求

在回复末尾输出一个提案块（围栏语言标记 workerbee-draft，内容是一个 JSON）：

```workerbee-draft
{{"kind": "workflow", "name": "流程名", "description": "……",
 "nodes": [{{"node_id": "step1", "name": "阶段名", "role": "……",
            "system_prompt": "……",
            "profiles": [{{"harness_ref": "{harness_ref}", "model_name": null,
                          "credential_ref": null}}],
            "basis": "observed", "evidence": ["E12", "A34"]}}],
 "edges": [{{"from_node": "step1", "to_node": "step2",
            "basis": "inferred", "evidence": []}}],
 "notes": ["……"]}}
```

字段约定（在助手提案协议之上扩展）：
- nodes[].basis / edges[].basis：必填。"observed" 表示这一步在材料里有真实
  佐证（工具调用、计划段、产物），并必须在 evidence 里给出对应的引用
  （材料里的 [E数字] 或 [A产物id] 标记）；"inferred" 表示这是你为了流程
  可复用而补的衔接，evidence 留空。
- **标 observed 但给不出材料里真实存在的 evidence，会被系统强制降级为
  inferred 并记录在案**——宁标 inferred，不要乱贴 observed。
- 其余字段同助手提案协议：profiles 可留空槽位表示待配置；
  edges[].output_contract 可省略。
- 一条回复至多一个提案块。"""


def review_basis(
    proposal: DraftProposal, evidence: set[str]
) -> list[dict[str, str]]:
    """复核 observed 标注：evidence 不在本次材料里的，强制降级为 inferred。

    就地修改 ``proposal``（降级后要落库与走校验的是复核后的版本），
    返回降级记录清单（节点/边、原标注、原因），供草案说明与事件留痕。
    模型的 evidence 写法允许裸 id 或带前缀（``E12`` / ``12`` 都算指向事件 12）。
    """
    downgrades: list[dict[str, str]] = []

    def check(label: str, item: Any) -> None:
        if item.basis != "observed":
            return
        valid = [ref for ref in item.evidence if ref in evidence]
        if valid:
            item.evidence = valid  # 清掉指向材料外的编造引用，留下真的
            return
        item.basis = "inferred"
        item.evidence = []
        downgrades.append(
            {
                "target": label,
                "reason": "标注为「观察到的」但给出的佐证不在本次捕获材料里",
            }
        )

    for node in proposal.nodes:
        check(f"节点「{node.name}」", node)
    for i, edge in enumerate(proposal.edges, 1):
        check(f"第 {i} 条连线（{edge.from_node} → {edge.to_node}）", edge)

    if downgrades:
        proposal.notes = [
            *proposal.notes,
            *(
                f"{d['target']}：{d['reason']}，已按「推断的」处理"
                for d in downgrades
            ),
        ]
    return downgrades
