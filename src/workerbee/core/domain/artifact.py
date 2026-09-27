"""产物（架构设计 v0.02 §5.4 Artifact、§7、DATA-01–04）。

三条不可动摇的语义：
1. **内容寻址、落地即不可变。** 「修改」一律表达为派生新版本（版本 +1，记录 lineage）。
2. **下游可辨认来源。** ``producer`` 三元组（task_id, stage_id, attempt_seq）让下游
   与历史回看都能回答「这个结果是谁在哪一次尝试里产出的」。
3. **已完成消费者的输入来源保持钉扎**，不被改写为「最新结果」。

token 估算口径：这是**近似值**，不同 harness 的 tokenizer 不同（架构设计 §16 第 5 条
已声明该风险）。它只用于上下文预算与 compact 触发，不用于计费。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import Entity, new_id
from .edge import Sensitivity

__all__ = ["ArtifactKind", "ArtifactProducer", "Artifact", "estimate_tokens", "SENSITIVITY_RANK"]


class ArtifactKind(StrEnum):
    TEXT = "text"
    FILE = "file"
    CODE = "code"
    STRUCTURED = "structured"


class ArtifactProducer(Entity):
    """来源三元组，下游可辨认（DATA-02）。"""

    task_id: str
    stage_id: str
    attempt_seq: int
    node_id: str | None = None
    attempt_id: str | None = None

    def label(self) -> str:
        return f"task={self.task_id[:8]} stage={self.stage_id[:8]} attempt#{self.attempt_seq}"


#: 敏感级序：沿血缘传播时取**最高**级（§9.4）。
SENSITIVITY_RANK: dict[str, int] = {"public": 0, "internal": 1, "sensitive": 2}


def max_sensitivity(values) -> Sensitivity:
    best = "public"
    for v in values:
        if v is None:
            continue
        if SENSITIVITY_RANK.get(str(v), 1) > SENSITIVITY_RANK.get(best, 0):
            best = str(v)
    return best  # type: ignore[return-value]


class Artifact(Entity):
    """一份不可变产物。"""

    artifact_id: str = Field(default_factory=new_id)

    digest: str
    """内容 sha256。相同内容只落一份物理文件，不同 artifact_id 各自计数。"""

    producer: ArtifactProducer | None = None
    """系统产生的产物（如摘要）可能没有上游阶段，此时为 None。"""

    kind: ArtifactKind = ArtifactKind.TEXT

    summary: str | None = None
    """摘要。覆盖不足即交接失败并显式报出（DATA-03），不允许静默省略。"""

    summary_ok: bool = True
    """摘要是否覆盖了边输出契约的必填要点。False 时下游必须显式受阻。"""

    covered_fields: list[str] = Field(default_factory=list)
    """本产物**实际覆盖**的契约字段名。

    它把 D-06 的摘要质量门禁与 RUN-06 的完成判据合并成同一个机制：
    摘要器判定「覆盖了哪些字段」，边契约校验只做集合包含判断，
    不需要第二次语义分析——也就不会出现两处判定互相矛盾的情况。
    """

    token_estimate: int | None = None
    size_bytes: int | None = None
    media_type: str | None = None

    sensitivity: Sensitivity = "internal"

    lineage: list[str] = Field(default_factory=list)
    """派生关系：本产物的直接父产物 id 列表。「修改」= 派生新版本。"""

    ref_count: int = 0
    """被活跃或可恢复任务引用的次数。驱动回收（RES-03）。"""

    tombstoned: bool = False
    """逻辑删除标记。物理回收前必须确认无活跃引用。"""

    storage_path: str | None = None

    def is_readable(self) -> bool:
        return not self.tombstoned

    def preview(self, limit: int = 200) -> str:
        if self.summary:
            return self.summary[:limit]
        return f"<{self.kind.value} artifact {self.artifact_id[:8]}>"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 字符约 1 token／字，其余约 4 字符／token。

    返回 0 表示无法估算（空内容）。调用方必须把「未知」与「零」区分对待。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ")
    other = len(text) - cjk
    return int(cjk + other / 4) + 1
