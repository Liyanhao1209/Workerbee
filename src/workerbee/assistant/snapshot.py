"""系统快照组装：助手的「动态感知」层（AI-01，计划 §4）。

分层纪律：**本模块不 import server 层**。它只依赖 :class:`SnapshotSource` 协议——
一组结构化签名的只读查询方法；由 server 层在装配时把 Services 的只读方法
适配注入进来。助手因此只能看到「快照那一刻」的被动感知，没有任何主动访问系统
的能力，更没有写入口。

预算纪律：字符预算默认 6000。超出时按保留优先级「错误事件 > 需处理 > 任务 >
registry」裁剪（registry 最先被丢），被丢的分区记进 ``trimmed``——裁剪事实
会写进事件日志，不允许悄悄少喂。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import Field

from ..core.domain.base import DomainModel

__all__ = ["SnapshotSource", "Snapshot", "build_snapshot", "DEFAULT_SNAPSHOT_BUDGET"]

#: 快照注入的默认字符预算。未经实测标定：字符数是 token 的近似。
DEFAULT_SNAPSHOT_BUDGET = 6000

#: 各分区保留优先级：数字越小越先被裁剪（registry 最先，错误事件最后）。
_TRIM_ORDER = ("registry", "tasks", "attention", "errors")


@runtime_checkable
class SnapshotSource(Protocol):
    """快照数据源。由 server 层装配时注入（把 Services 的只读方法适配进来）。

    全部方法只读、返回已脱敏的普通字典。任何一路取数失败都不应拖垮整份快照——
    实现方各自兜底，或让本模块把异常记为该分区不可用。
    """

    async def system_status(self) -> dict[str, Any]:
        """服务与凭据库解锁状态等系统级事实。"""
        ...

    async def attention_items(self) -> dict[str, Any]:
        """「需处理」清单：待审批、失败任务、清理未完成、启动降级说明。"""
        ...

    async def recent_tasks(self, limit: int) -> list[dict[str, Any]]:
        """最近的任务摘要：id / 名称 / 状态 / 失败原因。"""
        ...

    async def registry_overview(self) -> dict[str, Any]:
        """registry 概览：**可引用实体清单**——harness（id+名称+可用性）、

        凭据（id+label+base_url+默认模型，绝无密值）、Skill（id+名称）、
        MCP 工具（id+名称）。助手生成草稿提案时只能引用这里列出的 id。
        """
        ...

    async def recent_error_events(self, limit: int) -> list[dict[str, Any]]:
        """最近的错误级事件（事件时间线尾部）。"""
        ...


class Snapshot(DomainModel):
    """一份组装完成的快照。"""

    text: str = ""
    trimmed: list[str] = Field(default_factory=list)
    """因预算被裁掉或截断的分区名。非空即说明快照不完整——这个事实必须可见。"""

    degraded: list[str] = Field(default_factory=list)
    """取数失败的分区与原因（已过脱敏）。"""

    budget: int = DEFAULT_SNAPSHOT_BUDGET


async def build_snapshot(
    source: SnapshotSource | None,
    *,
    budget: int = DEFAULT_SNAPSHOT_BUDGET,
    redactor: Callable[[str], str] | None = None,
    task_limit: int = 10,
    error_limit: int = 10,
) -> Snapshot:
    """组装系统快照。``source`` 为 None 时返回空快照并如实标注。"""
    if source is None:
        return Snapshot(
            text="",
            degraded=["快照数据源未装配：本次回答看不到系统当前状态"],
            budget=budget,
        )

    sections: dict[str, str] = {}
    degraded: list[str] = []

    async def fill(name: str, render: Callable[[], Any]) -> None:
        try:
            sections[name] = await render()
        except Exception as exc:  # noqa: BLE001 - 一路取数失败不拖垮整份快照
            degraded.append(f"{name}: {type(exc).__name__}")

    await fill("status", lambda: _render_status(source))
    await fill("attention", lambda: _render_attention(source))
    await fill("tasks", lambda: _render_tasks(source, task_limit))
    await fill("registry", lambda: _render_registry(source))
    await fill("errors", lambda: _render_errors(source, error_limit))

    # 预算裁剪：按保留优先级逐个分区取舍；优先级最高的单个分区仍超预算时
    # 截断其文本。被裁分区如实记录。系统状态分区很小且是基本语境，恒保留、
    # 不计入预算。
    trimmed: list[str] = []
    kept: dict[str, str] = {}
    if sections.get("status"):
        kept["status"] = sections["status"]
    remaining = budget
    for name in reversed(_TRIM_ORDER):  # errors → attention → tasks → registry
        text = sections.get(name, "")
        if not text:
            continue
        if len(text) <= remaining:
            kept[name] = text
            remaining -= len(text)
        elif name == "errors" and remaining > 0:
            # 最高优先级分区也值得保留一个截断版本，而不是整个丢掉。
            kept[name] = text[:remaining].rstrip() + "\n（因预算截断）"
            trimmed.append(name)
            remaining = 0
        else:
            trimmed.append(name)

    # 展示顺序固定（与裁剪优先级无关）：状态 → 需处理 → 任务 → registry → 错误事件。
    order = ("status", "attention", "tasks", "registry", "errors")
    text = "\n\n".join(kept[name] for name in order if name in kept)
    if redactor is not None and text:
        text = redactor(text)
    return Snapshot(text=text, trimmed=trimmed, degraded=degraded, budget=budget)


# ---------------------------------------------------------------------------
# 各分区的渲染
# ---------------------------------------------------------------------------


async def _render_status(source: SnapshotSource) -> str:
    status = await source.system_status()
    lines = ["## 系统状态"]
    lines.append(f"- 凭据库已解锁：{'是' if status.get('secrets_unlocked') else '否'}")
    counts = status.get("counts") or {}
    lines.append(
        "- 任务：在途 {live}，运行中阶段 {run}，待派发阶段 {pend}，待审批 {appr}".format(
            live=counts.get("live_tasks", 0),
            run=counts.get("running_stages", 0),
            pend=counts.get("pending_stages", 0),
            appr=counts.get("open_approvals", 0),
        )
    )
    notes = status.get("startup_notes") or []
    if notes:
        lines.append("- 启动时的降级说明：")
        lines.extend(f"  - {note}" for note in notes[:5])
    return "\n".join(lines)


async def _render_attention(source: SnapshotSource) -> str:
    items = await source.attention_items()
    lines = ["## 需处理"]
    approvals = items.get("approvals") or []
    failed = items.get("failed_tasks") or []
    lost = items.get("lost_stages") or []
    unresolved = items.get("unresolved_resources") or []
    if approvals:
        lines.append(f"- 待审批 {len(approvals)} 项：")
        for a in approvals[:5]:
            lines.append(
                f"  - {a.get('action', '')[:80]}（任务 {str(a.get('bound_to', {}).get('task_id', ''))[:8]}）"
            )
    if failed:
        lines.append(f"- 失败或受阻任务 {len(failed)} 个：")
        for t in failed[:5]:
            reason = t.get("blocked_reason") or _failure_reason(t) or "（未记录原因）"
            lines.append(
                f"  - {t.get('workflow_name') or t.get('workflow_id')} / "
                f"{str(t.get('task_id'))[:8]}：{str(reason)[:120]}"
            )
    if lost:
        lines.append(f"- 状态不明的阶段 {len(lost)} 个（需人工核对）")
    if unresolved:
        lines.append(f"- 清理未完成的资源 {len(unresolved)} 项")
    if len(lines) == 1:
        lines.append("- 当前没有等待处理的事项")
    return "\n".join(lines)


async def _render_tasks(source: SnapshotSource, limit: int) -> str:
    tasks = await source.recent_tasks(limit)
    lines = ["## 最近的任务"]
    if not tasks:
        lines.append("- 还没有任务")
        return "\n".join(lines)
    for t in tasks:
        reason = _failure_reason(t)
        line = (
            f"- {str(t.get('task_id'))[:8]} [{t.get('observed_state')}] "
            f"{t.get('workflow_name') or t.get('workflow_id')}"
        )
        if reason:
            line += f"（{str(reason)[:120]}）"
        lines.append(line)
    return "\n".join(lines)


async def _render_registry(source: SnapshotSource) -> str:
    """registry 分区：可引用实体清单（id 是给提案用的引用键，必须出现在文本里）。

    凭据只列引用与接入信息（label / base_url / default_model），绝无密值。
    """
    overview = await source.registry_overview()
    lines = ["## 注册表概览（可引用的实体清单；生成提案时只能用这里的 id）"]
    harnesses = overview.get("harnesses") or []
    if harnesses:
        lines.append("- harness：")
        for h in harnesses:
            probed = h.get("last_probe_ok")
            probe_text = {True: "探测正常", False: "探测失败", None: "未探测"}[probed]
            lines.append(
                f"  - id={h.get('harness_id')} 名称={h.get('name') or h.get('harness_id')}"
                f"（{'启用' if h.get('enabled') else '停用'}，{probe_text}）"
            )
    else:
        lines.append("- 还没有登记任何 harness")
    credentials = overview.get("credentials") or []
    if credentials:
        lines.append("- 凭据（只有引用与接入信息，没有密钥本体）：")
        for c in credentials:
            bits = [f"id={c.get('credential_id')}", f"label={c.get('label')}"]
            if c.get("base_url"):
                bits.append(f"base_url={c.get('base_url')}")
            if c.get("default_model"):
                bits.append(f"默认模型={c.get('default_model')}")
            lines.append("  - " + " ".join(bits) + ("（已撤销）" if c.get("revoked") else ""))
    else:
        lines.append("- 还没有登记任何凭据")
    skills = overview.get("skills") or []
    if skills:
        lines.append("- Skill：")
        for s in skills:
            lines.append(
                f"  - id={s.get('skill_id')} 名称={s.get('name') or s.get('skill_id')}"
                + ("" if s.get("enabled", True) else "（停用）")
            )
    else:
        lines.append("- 还没有登记任何 Skill")
    tools = overview.get("tools") or []
    if tools:
        lines.append("- MCP 工具：")
        for t in tools:
            lines.append(
                f"  - id={t.get('tool_id')} 名称={t.get('name') or t.get('tool_id')}"
                + ("" if t.get("enabled", True) else "（停用）")
            )
    else:
        lines.append("- 还没有登记任何 MCP 工具")
    return "\n".join(lines)


async def _render_errors(source: SnapshotSource, limit: int) -> str:
    events = await source.recent_error_events(limit)
    lines = ["## 最近的错误事件"]
    if not events:
        lines.append("- 最近没有错误事件")
        return "\n".join(lines)
    for e in events:
        note = e.get("note") or e.get("type") or ""
        lines.append(f"- [{e.get('ts', '')[:19]}] {e.get('type')}: {str(note)[:150]}")
    return "\n".join(lines)


def _failure_reason(task: dict[str, Any]) -> str | None:
    summary = task.get("failure_summary")
    if isinstance(summary, dict):
        return summary.get("summary") or summary.get("reason")
    return None
