"""系统快照组装：预算裁剪优先级与脱敏（AI-01，计划 §4/§8）。

断言重点不是「有输出」，而是：

- 超预算时裁剪顺序是 registry → 任务 → 需处理 → 错误事件（后者最后才被丢）；
- 被裁掉的分区如实记进 ``trimmed``；
- 取数失败的分区不拖垮整份快照，记进 ``degraded``；
- 过 redactor 之后密值形态不得出现在快照文本里。
"""

from __future__ import annotations

import pytest

from workerbee.assistant.snapshot import build_snapshot

pytestmark = pytest.mark.unit


class FakeSource:
    """可脚本化的快照数据源。"""

    def __init__(self, **overrides) -> None:
        self._data = overrides

    async def system_status(self):
        return self._data.get(
            "status",
            {
                "secrets_unlocked": True,
                "counts": {"live_tasks": 2, "running_stages": 1,
                           "pending_stages": 0, "open_approvals": 1},
                "startup_notes": [],
            },
        )

    async def attention_items(self):
        return self._data.get("attention", {"approvals": [], "failed_tasks": []})

    async def recent_tasks(self, limit):
        return self._data.get("tasks", [])

    async def registry_overview(self):
        return self._data.get("registry", {"harnesses": []})

    async def recent_error_events(self, limit):
        return self._data.get("errors", [])


class FailingSource(FakeSource):
    async def recent_tasks(self, limit):
        raise RuntimeError("数据库读失败")


async def test_snapshot_contains_all_sections() -> None:
    source = FakeSource(
        tasks=[{"task_id": "t1234567890", "workflow_id": "w1",
                "workflow_name": "部署", "observed_state": "failed",
                "failure_summary": {"summary": "节点 A 超时"}, "blocked_reason": None}],
        errors=[{"ts": "2026-09-30T01:02:03", "type": "session.lost", "note": "会话丢失"}],
    )
    snap = await build_snapshot(source, budget=6000)
    assert "## 系统状态" in snap.text
    assert "## 需处理" in snap.text
    assert "## 最近的任务" in snap.text
    assert "节点 A 超时" in snap.text
    assert "## 注册表概览" in snap.text
    assert "session.lost" in snap.text
    assert snap.trimmed == []
    assert snap.degraded == []


async def test_snapshot_trims_lowest_priority_first() -> None:
    """预算不够时先丢 registry，再丢任务；错误事件最后才被裁。"""
    source = FakeSource(
        tasks=[
            {"task_id": f"t{i}", "workflow_id": "w", "workflow_name": f"流程{i}",
             "observed_state": "running", "failure_summary": None, "blocked_reason": None}
            for i in range(50)
        ],
        registry={"harnesses": [
            {"harness_id": f"h{i}", "name": f"harness-{i}", "enabled": True,
             "last_probe_ok": True} for i in range(50)
        ]},
        errors=[{"ts": "2026-09-30T01:02:03", "type": "session.lost", "note": "会话丢失"}],
    )
    snap = await build_snapshot(source, budget=800)
    assert "registry" in snap.trimmed
    assert "## 注册表概览" not in snap.text
    # 错误事件优先级最高，必须留下
    assert "session.lost" in snap.text
    assert len(snap.text) <= 800 + 200  # 状态分区不计入预算，允许超出一点


async def test_snapshot_failing_section_is_degraded_not_fatal() -> None:
    snap = await build_snapshot(FailingSource(), budget=6000)
    assert "## 系统状态" in snap.text  # 其它分区照常
    assert any("tasks" in d for d in snap.degraded)


async def test_snapshot_without_source_is_explicitly_empty() -> None:
    snap = await build_snapshot(None)
    assert snap.text == ""
    assert snap.degraded  # 如实标注「看不到系统状态」


async def test_registry_section_lists_referenceable_entities() -> None:
    """registry 分区是可引用实体清单：id、名称、可用性都在；凭据绝无密值。"""
    source = FakeSource(
        registry={
            "harnesses": [{"harness_id": "h1", "name": "Claude", "enabled": True,
                           "last_probe_ok": True}],
            "credentials": [{"credential_id": "c1", "label": "API",
                             "base_url": "https://x.example.com",
                             "default_model": "gpt-test", "revoked": False},
                            {"credential_id": "c2", "label": "旧凭据",
                             "base_url": None, "default_model": None, "revoked": True}],
            "skills": [{"skill_id": "s1", "name": "写周报", "enabled": True}],
            "tools": [{"tool_id": "t1", "name": "查日历", "enabled": False}],
        }
    )
    snap = await build_snapshot(source, budget=6000)
    text = snap.text
    assert "id=h1" in text and "Claude" in text
    assert "id=c1" in text and "https://x.example.com" in text and "gpt-test" in text
    assert "id=c2" in text and "已撤销" in text  # 撤销状态如实展示
    assert "id=s1" in text and "写周报" in text
    assert "id=t1" in text and "停用" in text
    # 密值绝不出现在 registry 分区（这里连字段都不该有）
    assert "secret" not in text.lower()
    assert "api_key" not in text.lower()


async def test_snapshot_passes_through_redactor() -> None:
    secret = "sk-live-abcdef0123456789"
    source = FakeSource(
        errors=[{"ts": "t", "type": "session.lost", "note": f"key={secret}"}],
    )
    from workerbee.data.redact import redact_text

    snap = await build_snapshot(source, budget=6000, redactor=redact_text)
    assert secret not in snap.text
