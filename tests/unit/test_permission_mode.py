"""权限模式与审批可用性的校验（HUM-03、§4.4、§8.1）。

这条规则的由来：第一版把它写成「没有权限钩子就拒绝」，结果 claude 与 kimi
这两个唯一可用的本地 harness 全都被拒，框架对自己最主流的用例说不。

清单 HUM-03 的原话是「无法可靠接入审批的适配**不能声称支持该权限模式**」——
也就是：没有钩子仍然可用，但必须由用户**显式**选一个不会询问的模式。
框架绝不替用户默认放行：权限相关的事不做隐式默认。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain import (
    ExecutionProfile,
    GraphSpec,
    HarnessRegistration,
    NodeDefinition,
)
from workerbee.core.graph.validate import (
    InMemoryRegistry,
    Severity,
    ValidationMode,
    validate,
)

pytestmark = pytest.mark.unit

CLAUDE_MODES = ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
NON_INTERACTIVE = ["auto", "bypassPermissions", "dontAsk"]


def _registry(*, hook: bool) -> InMemoryRegistry:
    return InMemoryRegistry(
        harnesses=[
            HarnessRegistration(
                harness_id="h1",
                name="Claude Code",
                adapter_id="claude_code",
                last_probe_ok=True,
                capabilities_snapshot={
                    "create_session": True,
                    "permission_hook": hook,
                    "compact": False,
                    "background_tasks": False,
                    "permission_modes": CLAUDE_MODES,
                    "non_interactive_modes": NON_INTERACTIVE,
                },
            )
        ]
    )


def _graph(mode: str | None) -> GraphSpec:
    return GraphSpec(
        nodes=[
            NodeDefinition(
                node_id="A",
                name="A",
                profiles=[
                    ExecutionProfile(
                        profile_id="p1", model_name="m1", harness_ref="h1",
                        permission_mode=mode,
                    )
                ],
            )
        ],
        edges=[],
    )


def _codes(report) -> dict[str, Severity]:
    return {d.code: d.severity for d in report.diagnostics}


def test_without_hook_and_without_mode_is_an_error():
    """未指定模式 → 拒绝，并告诉用户可选值。

    不能默认放行：那等于框架替用户做了权限决定。
    """
    report = validate(_graph(None), _registry(hook=False), mode=ValidationMode.LAUNCH)
    codes = _codes(report)
    assert codes.get("permission_mode_unset") == Severity.ERROR
    diag = next(d for d in report.errors() if d.code == "permission_mode_unset")
    assert "bypassPermissions" in (diag.hint or ""), "必须列出可选的自动模式"
    assert "不会替你默认放行" in (diag.hint or "")


def test_without_hook_and_asking_mode_is_an_error():
    """选了会询问的模式却没有钩子 → 拒绝。运行时会卡在无人应答的提问上。"""
    report = validate(
        _graph("manual"), _registry(hook=False), mode=ValidationMode.LAUNCH
    )
    assert _codes(report).get("permission_mode_needs_hook") == Severity.ERROR


def test_without_hook_and_auto_mode_is_allowed_but_visible():
    """显式选了不询问的模式 → 放行，但「审批不可用」必须可见（HAR-02）。

    这正是 claude / kimi 的实际情况：能用，但用户得知道框架拦不住审批。
    """
    report = validate(
        _graph("bypassPermissions"), _registry(hook=False), mode=ValidationMode.LAUNCH
    )
    codes = _codes(report)
    assert "permission_mode_needs_hook" not in codes
    assert "permission_mode_unset" not in codes
    assert codes.get("approval_unavailable") == Severity.WARNING
    assert report.ok(), "警告不应阻断执行"


def test_unsupported_mode_is_rejected():
    report = validate(
        _graph("no-such-mode"), _registry(hook=False), mode=ValidationMode.LAUNCH
    )
    assert _codes(report).get("permission_mode_unsupported") == Severity.ERROR


def test_with_hook_any_mode_is_fine():
    """有钩子时不存在这个问题——审批由框架接管，用户可以用会询问的模式。"""
    report = validate(_graph("manual"), _registry(hook=True), mode=ValidationMode.LAUNCH)
    codes = _codes(report)
    assert "approval_unavailable" not in codes
    assert "permission_mode_unset" not in codes
    assert "permission_mode_needs_hook" not in codes


def test_draft_mode_downgrades_to_warning():
    """草稿允许不完整——权限模式没填不该阻断保存，但也要如实提示。"""
    report = validate(_graph(None), _registry(hook=False), mode=ValidationMode.DRAFT)
    assert report.ok()
    assert _codes(report).get("permission_mode_unset") == Severity.WARNING
