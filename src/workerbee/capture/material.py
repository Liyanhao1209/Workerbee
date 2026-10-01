"""捕获材料汇编（WF-03、D-12）。

一次捕获任务跑完后，草案合成只允许使用**可实际取得**的材料。本模块从
event_log 与产物表里把它们取出来，拼成一份有字符预算的材料正文：

- 任务输入（``ATTEMPT_INPUT`` 事件里实际发给 harness 的输入）；
- 工具调用序列（``ATTEMPT_TOOL_USE`` / ``ATTEMPT_TOOL_RESULT``，按时间序）；
- 显式计划（模型按引导输出在产物正文里的计划段，找不到就如实记「没有」）；
- 产物清单与摘要（``store.artifacts``）；
- 用量与耗时（attempt 记录）。

两条硬边界：

1. **明确排除 ``ATTEMPT_REASONING``（思考链）**——「捕获不依赖私有推理链」
   是 WF-03 的明文要求，不是默认值。这里连读都不读它。
2. **超出预算按 工具调用序列 > 计划 > 产物摘要 的优先级裁剪，且裁剪留痕**——
   ``trimmed`` 列出被裁的分区，调用方把它写进事件与草案，用户看得到
   「材料被裁过」，不会把裁后的材料当成全貌。

所有文本再过一遍 redactor：事件落库时已脱敏，这里是「发送给模型前」的
第二道（AUTH-02 的同一条纪律，两处都不能漏）。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import Field

from ..core.domain.base import DomainModel
from ..data.event_log import EventType

__all__ = ["CaptureMaterial", "assemble_material", "DEFAULT_BUDGET_CHARS"]

#: 材料正文的字符预算。超出时按优先级裁剪，裁剪留痕。
DEFAULT_BUDGET_CHARS = 12_000

#: 从输出正文提取计划段时的读取上限（产物正文可能很大）。
_OUTPUT_READ_CHARS = 8_000

#: 计划段本身的容量上限。
_PLAN_CHARS = 2_000

#: 任务输入在材料里的容量上限。
_INPUT_CHARS = 2_000

#: 每条产物摘要的容量上限。
_SUMMARY_CHARS = 500

#: 「计划段」标题的识别规则：markdown 标题（或加粗行）里含「计划/plan」。
_PLAN_HEADING = re.compile(r"^(#{1,6}\s+.*(?:计划|[Pp]lan)|\*\*[^*]*计划[^*]*\*\*)\s*$", re.M)
_NEXT_HEADING = re.compile(r"^#{1,6}\s", re.M)


class CaptureMaterial(DomainModel):
    """一份汇编好的捕获材料。``text`` 是发给模型的正文；其余字段供留痕与界面展示。"""

    task_id: str
    text: str = ""
    total_chars: int = 0
    tool_call_count: int = 0
    artifact_count: int = 0
    has_plan: bool = False
    has_input: bool = False
    usage: dict[str, Any] | None = None
    trimmed: list[str] = Field(default_factory=list)
    """被裁剪的分区说明（大白话中文）。空列表表示没有发生裁剪。"""
    evidence: list[str] = Field(default_factory=list)
    """本次材料里真实出现的可引用 id（事件 ``E<id>`` 与产物 ``A<id>``，另附裸 id 写法）。
    草案的 observed 复核以它为唯一依据——模型引用了清单外的 id，就是不可信标注。"""

    def evidence_set(self) -> set[str]:
        return set(self.evidence)


class _Section:
    """一个材料分区：正文 + 它贡献的可引用证据。

    证据按分区统计，是为了让「被裁掉的分区不得再贡献 evidence」这条规则
    自然成立——否则复核会采信模型「看见过」它根本拿不到的材料。
    """

    def __init__(self, name: str, body: str, evidence: set[str] | None = None) -> None:
        self.name = name
        self.body = body
        self.evidence = evidence or set()


async def assemble_material(
    store: Any,
    task_id: str,
    *,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    redactor: Callable[[Any], Any] | None = None,
) -> CaptureMaterial:
    """汇编一次捕获任务的材料。只读，不写任何状态。"""

    def red(value: str) -> str:
        if redactor is None or not value:
            return value
        return str(redactor(value))

    events = await store.events.for_task(task_id, limit=5000)
    artifacts = [a for a in await store.artifacts.list_by_task(task_id) if not a.tombstoned]

    material = CaptureMaterial(task_id=task_id)
    material.usage = await _extract_usage(store, task_id)

    sections: list[_Section] = []

    # ---- 任务输入 ----
    task_input, input_ref = _extract_input(events)
    material.has_input = bool(task_input)
    if task_input:
        text = red(task_input)
        if len(text) > _INPUT_CHARS:
            text = text[:_INPUT_CHARS] + "\n（输入过长，已截断）"
            material.trimmed.append("任务输入（超出容量，已截断）")
        sections.append(
            _Section("输入", f"# 任务输入\n[E{input_ref}] {text}", {str(input_ref), f"E{input_ref}"})
        )

    # ---- 工具调用序列（时间序） ----
    tool_calls = _extract_tool_calls(events)
    material.tool_call_count = len(tool_calls)
    tool_section = _tool_section(tool_calls, red)
    if tool_section is not None:
        sections.append(tool_section)

    # ---- 显式计划 ----
    plan = await _extract_plan(store, artifacts)
    material.has_plan = plan is not None
    if plan is not None:
        sections.append(_Section("计划", f"# 模型输出的执行计划\n{red(plan)}"))

    # ---- 产物清单 ----
    material.artifact_count = len(artifacts)
    artifact_section = _artifact_section(artifacts, red)
    if artifact_section is not None:
        sections.append(artifact_section)

    # ---- 用量与耗时 ----
    if material.usage:
        bits = []
        if material.usage.get("input_tokens") is not None:
            bits.append(f"输入 tokens：{material.usage['input_tokens']}")
        if material.usage.get("output_tokens") is not None:
            bits.append(f"输出 tokens：{material.usage['output_tokens']}")
        if material.usage.get("duration_s") is not None:
            bits.append(f"耗时：{material.usage['duration_s']:.1f}s")
        bits.append(f"尝试次数：{material.usage['attempts']}")
        sections.append(_Section("用量", "# 用量与耗时\n" + "，".join(bits)))

    # ---- 预算裁剪：优先级最低的先裁（产物摘要 → 计划 → 工具调用序列） ----
    sections = _trim(sections, budget_chars, material)

    material.text = "\n\n".join(s.body for s in sections)
    material.total_chars = len(material.text)
    evidence: set[str] = set()
    for s in sections:
        evidence |= s.evidence
    material.evidence = sorted(evidence)
    if not any(s.name == "计划" for s in sections):
        material.has_plan = False
    return material


def _trim(sections: list[_Section], budget: int, material: CaptureMaterial) -> list[_Section]:
    """按优先级裁剪直到装进预算。裁剪的分区名记入 ``material.trimmed``。"""

    def total(items: list[_Section]) -> int:
        return sum(len(s.body) + 2 for s in items)

    result = list(sections)
    # 1) 产物摘要整个裁掉（只留数量事实——material.artifact_count 仍在）。
    if total(result) > budget and any(s.name == "产物" for s in result):
        result = [s for s in result if s.name != "产物"]
        material.trimmed.append("产物清单摘要（超出预算，只保留产物数量）")
    # 2) 计划整个裁掉。
    if total(result) > budget and any(s.name == "计划" for s in result):
        result = [s for s in result if s.name != "计划"]
        material.trimmed.append("模型输出的执行计划（超出预算，未纳入材料）")
    # 3) 最后才动工具调用序列：从最早的开始丢，保住最近的行为。
    if total(result) > budget:
        for i, s in enumerate(result):
            if s.name != "工具调用序列":
                continue
            lines = s.body.splitlines()
            kept = list(lines)
            dropped: list[str] = []

            def body_with_note() -> str:
                body = "\n".join(kept)
                if dropped:
                    body += f"\n（最早的 {len(dropped)} 次调用超出预算，已裁掉）"
                return body

            result[i] = _Section(s.name, body_with_note(), s.evidence)
            while len(kept) > 1 and total(result) > budget:
                dropped.append(kept.pop(1))  # 第 0 行是标题
                result[i] = _Section(s.name, body_with_note(), s.evidence)
            if dropped:
                # 被丢的行连同行内的 evidence 一起撤出。
                dropped_refs = set(re.findall(r"\[(E\d+)\]", "\n".join(dropped)))
                result[i].evidence = (s.evidence - dropped_refs) - {r[1:] for r in dropped_refs}
                material.trimmed.append(f"工具调用序列最早的 {len(dropped)} 次")
            break
    return result


def _tool_section(
    tool_calls: list[dict[str, Any]], red: Callable[[str], str]
) -> _Section | None:
    if not tool_calls:
        return None
    lines: list[str] = []
    evidence: set[str] = set()
    for i, call in enumerate(tool_calls, 1):
        result = call.get("is_error")
        outcome = "失败" if result else "成功" if result is False else "结果未记录"
        target = f" → {red(str(call['target']))}" if call.get("target") else ""
        ref = f"E{call['event_id']}"
        evidence.add(ref)
        evidence.add(str(call["event_id"]))
        lines.append(f"{i}. [{ref}] {call.get('tool_name') or '?'}{target}（{outcome}）")
    body = f"# 工具调用序列（按时间先后，共 {len(lines)} 次）\n" + "\n".join(lines)
    return _Section("工具调用序列", body, evidence)


def _artifact_section(artifacts: list[Any], red: Callable[[str], str]) -> _Section | None:
    if not artifacts:
        return None
    lines: list[str] = []
    evidence: set[str] = set()
    for art in artifacts:
        ref = f"A{art.artifact_id}"
        evidence.add(ref)
        evidence.add(art.artifact_id)
        summary = red(art.summary or "（无摘要）")
        if len(summary) > _SUMMARY_CHARS:
            summary = summary[:_SUMMARY_CHARS] + "…"
        lines.append(f"- [{ref}] kind={art.kind.value}：{summary}")
    body = f"# 产物（共 {len(lines)} 份）\n" + "\n".join(lines)
    return _Section("产物", body, evidence)


def _extract_input(events: list[dict[str, Any]]) -> tuple[str, int | None]:
    """任务输入 = 最后一次 ATTEMPT_INPUT 的 user_input（重试会覆盖，取最新）。"""
    for row in reversed(events):
        if row["type"] == EventType.ATTEMPT_INPUT.value:
            return str(row["payload"].get("user_input") or ""), int(row["event_id"])
    return "", None


def _extract_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具调用序列（时间序）。TOOL_USE 建条目，TOOL_RESULT 按 tool_use_id 回填结果。

    刻意不读 ATTEMPT_REASONING：思考链是 harness 的私有推理，不是可用材料（WF-03）。
    """
    calls: list[dict[str, Any]] = []
    by_use_id: dict[str, dict[str, Any]] = {}
    for row in events:
        type_ = row["type"]
        payload = row["payload"]
        if type_ == EventType.ATTEMPT_TOOL_USE.value:
            call = {
                "event_id": int(row["event_id"]),
                "tool_name": payload.get("tool_name"),
                "target": payload.get("target"),
                "is_error": None,
            }
            calls.append(call)
            if payload.get("tool_use_id"):
                by_use_id[str(payload["tool_use_id"])] = call
        elif type_ == EventType.ATTEMPT_TOOL_RESULT.value:
            call = by_use_id.get(str(payload.get("tool_use_id") or ""))
            if call is not None:
                call["is_error"] = bool(payload.get("is_error"))
    return calls


async def _extract_plan(store: Any, artifacts: list[Any]) -> str | None:
    """从最终输出正文里提取显式计划段。

    找不到就是 None——「模型没有输出显式计划」是正常且必须如实呈现的事实
    （此时草案的节点划分几乎全是 inferred，合成提示词会如实说明）。
    """
    for art in reversed(artifacts):  # 最后落的产物通常是最终输出
        if art.kind.value != "text":
            continue
        try:
            text = await store.artifacts.read_text(art.artifact_id)
        except Exception:  # noqa: BLE001 - 产物读不出就是没有，不编造
            continue
        plan = _find_plan_section(text[:_OUTPUT_READ_CHARS])
        if plan:
            return plan[:_PLAN_CHARS]
    return None


def _find_plan_section(text: str) -> str | None:
    """在输出正文里找「计划」段：一个标题含「计划/plan」的 markdown 小节。"""
    match = _PLAN_HEADING.search(text)
    if match is None:
        return None
    rest = text[match.start():]
    nxt = _NEXT_HEADING.search(rest, match.end() - match.start() + 1)
    section = rest[: nxt.start()] if nxt else rest
    return section.strip() or None


async def _extract_usage(store: Any, task_id: str) -> dict[str, Any] | None:
    """各次尝试的用量与耗时汇总。取不到的字段保持缺失（未知 ≠ 0）。"""
    attempts = await store.tasks.list_attempts_for_task(task_id)
    if not attempts:
        return None
    input_tokens = 0
    output_tokens = 0
    saw_tokens = False
    duration = 0.0
    saw_duration = False
    for at in attempts:
        if at.usage is not None:
            if at.usage.input_tokens is not None:
                input_tokens += at.usage.input_tokens
                saw_tokens = True
            if at.usage.output_tokens is not None:
                output_tokens += at.usage.output_tokens
                saw_tokens = True
        if at.started_at is not None and at.ended_at is not None:
            duration += (at.ended_at - at.started_at).total_seconds()
            saw_duration = True
    usage: dict[str, Any] = {"attempts": len(attempts)}
    if saw_tokens:
        usage["input_tokens"] = input_tokens
        usage["output_tokens"] = output_tokens
    if saw_duration:
        usage["duration_s"] = duration
    return usage
