"""跨层集成：敏感级传播、出站扫描，以及它们与 Secret Store / Event Log 的接线。

对应 §9.1（凭据本体不出 L5、事件落库前脱敏）、§9.4（跨 harness／厂商边界前
出站脱敏）、§5.4（sensitivity 沿血缘取最高级）。

这一层测的是「接起来以后还成不成立」——单模块内部的细节在
``tests/unit/test_secret_store.py`` 里测。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from workerbee.core.domain.artifact import Artifact, max_sensitivity
from workerbee.core.domain.registry import HarnessRegistration
from workerbee.data.db import Database
from workerbee.data.event_log import EventLog, EventScope, EventType
from workerbee.security.redaction import (
    Boundary,
    Finding,
    FindingKind,
    OutboundScanner,
    SensitivityPropagator,
    scan,
    scan_and_redact,
)
from workerbee.security.secret_store import KdfParams, SecretRedactor, SecretStore

pytestmark = pytest.mark.integration

#: 测试用轻量参数：本文件检验的是接线，不是 Argon2 的抗爆破强度。
LIGHT = KdfParams(time_cost=1, memory_cost=8192, parallelism=1)

LIVE_KEY = "sk-live-ZZZZ0123456789abcdefghijkl"
GH_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SLACK_TOKEN = "xoxb-123456789012-ABCDEFGHIJKLMN"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"

PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
    "aG9zdC1rZXkAAAAgVU5ERVNFUlZFRF9QUklWQVRFX0tFWV9NQVRFUklBTAAAAA\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

ENV_FILE = (
    "# 本地凭据，不要提交\n"
    "OPENAI_API_KEY=sk-live-ZZZZ0123456789abcdefghijkl\n"
    "export GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n"
    'DB_PASSWORD="correct horse battery staple"\n'
    "LOG_LEVEL=debug\n"
    "PATH=/usr/local/bin:/usr/bin\n"
)


# ---------------------------------------------------------------------------
# 敏感级传播
# ---------------------------------------------------------------------------


def test_effective_sensitivity_takes_the_maximum_along_lineage():
    prop = SensitivityPropagator()
    assert prop.effective("internal", ["public"]) == "internal"
    assert prop.effective("public", ["internal", "sensitive"]) == "sensitive"
    assert prop.effective("internal", [None, "internal"]) == "internal"
    assert prop.effective("public") == "public"
    assert prop.max_of([]) == "public"


def test_propagation_agrees_with_the_artifact_layer():
    """同一口径只应有一处实现：与 core.domain 的 max_sensitivity 对拍。"""
    prop = SensitivityPropagator()
    cases = [
        ["public", "internal"],
        ["internal", "sensitive"],
        ["public", "public"],
        ["sensitive"],
    ]
    for case in cases:
        assert prop.max_of(case) == max_sensitivity(case)


def test_artifact_lineage_raises_the_effective_level():
    """派生不会降级：父产物敏感，子产物即使声明 public 也按 sensitive 处理。"""
    prop = SensitivityPropagator()
    parent = Artifact(digest="a" * 64, sensitivity="sensitive")
    child = Artifact(digest="b" * 64, sensitivity="public", lineage=[parent.artifact_id])
    assert prop.effective(child.sensitivity, [parent.sensitivity]) == "sensitive"


def test_unknown_sensitivity_is_reported_not_silently_downgraded():
    with pytest.raises(ValueError):
        SensitivityPropagator().max_of(["secret-ish"])


def test_decide_keeps_flow_inside_one_boundary_untouched():
    prop = SensitivityPropagator()
    local = Boundary(harness_id="h1", vendor="claude-code")
    decision = prop.decide(
        own="sensitive", source=local, target=Boundary(harness_id="h1", vendor="claude-code")
    )
    assert decision.required is False
    assert decision.crosses_boundary is False


def test_decide_requires_redaction_when_crossing_vendors():
    prop = SensitivityPropagator()
    decision = prop.decide(
        own="internal",
        source=Boundary(harness_id="h1", vendor="claude-code"),
        target=Boundary(harness_id="h2", vendor="kimi-code"),
    )
    assert decision.required is True
    assert "厂商" in decision.reason


def test_decide_treats_same_vendor_differently_from_same_harness():
    prop = SensitivityPropagator()
    src = Boundary(harness_id="h1", vendor="claude-code")
    other_harness = Boundary(harness_id="h2", vendor="claude-code")

    lenient = prop.decide(own="internal", source=src, target=other_harness)
    strict = SensitivityPropagator(strict=True).decide(
        own="internal", source=src, target=other_harness
    )
    sensitive = prop.decide(own="sensitive", source=src, target=other_harness)

    assert lenient.required is False
    assert strict.required is True
    assert sensitive.required is True


def test_public_material_crosses_without_redaction():
    prop = SensitivityPropagator()
    decision = prop.decide(
        own="public",
        source=Boundary("h1", "claude-code"),
        target=Boundary("h2", "kimi-code"),
    )
    assert decision.required is False
    assert decision.crosses_boundary is True


def test_boundary_from_harness_registration_uses_adapter_as_vendor():
    reg = HarnessRegistration(harness_id="h1", name="Claude Code", adapter_id="claude-code")
    boundary = Boundary.from_registration(reg)
    assert boundary.harness_id == "h1"
    assert boundary.vendor == "claude-code"


# ---------------------------------------------------------------------------
# 出站扫描
# ---------------------------------------------------------------------------


def test_scan_finds_credential_shapes():
    text = f"key={LIVE_KEY}\ngh={GH_TOKEN}\nslack={SLACK_TOKEN}\nauth: {BEARER}\n"
    kinds = {f.rule for f in scan(text)}
    assert {"sk_prefixed_key", "github_token", "slack_token", "authorization_header"} <= kinds
    assert all(isinstance(f, Finding) for f in scan(text))


def test_scan_finds_private_key_blocks_without_splitting_them():
    findings = [f for f in scan(PRIVATE_KEY) if f.kind is FindingKind.PRIVATE_KEY]
    assert len(findings) == 1, "整块私钥应该只命中一次，而不是被头/尾规则切碎"
    assert findings[0].length == len(PRIVATE_KEY)


def test_scan_finds_env_file_content():
    findings = scan(ENV_FILE)
    env_hits = [f for f in findings if f.kind is FindingKind.ENV_ASSIGNMENT]
    assert {f.rule for f in env_hits} == {"env_assignment"}
    assert len(env_hits) == 3, "三行含敏感词，LOG_LEVEL / PATH 不应命中"
    assert all(f.line in (2, 3, 4) for f in env_hits)


def test_findings_carry_no_value_material():
    """命中项本身不得携带任何原文片段——报告会流向日志与 UI（§9.1）。"""
    text = f"{LIVE_KEY}\n{PRIVATE_KEY}\n{ENV_FILE}"
    rendered = "\n".join(f.describe() + repr(f) for f in scan(text))

    for marker in (LIVE_KEY, GH_TOKEN, "OPENSSH PRIVATE KEY", "b3BlbnNzaC1rZXktdjE"):
        assert marker not in rendered


def test_findings_point_at_the_right_place():
    text = "line one is fine\nOPENAI_API_KEY=whatever-value-here\n"
    finding, = scan(text)
    assert finding.line == 2
    assert text[finding.start : finding.end].startswith("OPENAI_API_KEY=")


def test_clean_text_is_left_completely_alone():
    clean = (
        "Stage B consumed the artifact produced by Stage A.\n"
        "See https://example.com/docs for details.\n"
        "LOG_LEVEL=debug\n"
        "def derive(graph, enabled):\n"
        "    return graph\n"
    )
    redacted, findings = scan_and_redact(clean)
    assert findings == []
    assert redacted == clean


def test_scan_and_redact_removes_every_hit():
    redacted, findings = scan_and_redact(f"{ENV_FILE}\n{PRIVATE_KEY}\n{BEARER}\n")
    assert findings
    for marker in (
        "sk-live-ZZZZ",
        "ghp_",
        "correct horse battery staple",
        "OPENSSH PRIVATE KEY",
        "b3BlbnNzaC1rZXktdjE",
        "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert marker not in redacted
    # 非敏感行保持原样，说明遮罩是定位替换而不是整段丢弃。
    assert "LOG_LEVEL=debug" in redacted
    assert "PATH=/usr/local/bin:/usr/bin" in redacted


def test_scanner_reports_its_own_limits_honestly():
    """自造格式的高熵串**不在**覆盖范围内——这是已知缺口，不该假装能拦。"""
    homemade = "my-internal-token-9f3a1c77b2e845d0a6c1"
    redacted, findings = scan_and_redact(f"internal credential: {homemade}")
    assert findings == []
    assert homemade in redacted, "无固定形态的凭据只能靠已知值替换，正则兜底认不出"

    # 登记成已知值之后才拦得住——这正是 SecretRedactor.bind_store 的用途。
    scanner = OutboundScanner(known_values=[homemade])
    assert homemade not in scanner.redact(f"internal credential: {homemade}")


def test_scanner_repr_hides_registered_values():
    scanner = OutboundScanner(known_values=[LIVE_KEY])
    assert LIVE_KEY not in repr(scanner)


def test_unpaired_private_key_headers_are_still_masked():
    """只有头没有尾（被截断）时按行兜底，不能让 BEGIN 行原样出站。"""
    text = "-----BEGIN RSA PRIVATE KEY-----\n" * 3
    redacted, findings = scan_and_redact(text)
    assert len(findings) == 3
    assert all(f.kind is FindingKind.PRIVATE_KEY for f in findings)
    assert "PRIVATE KEY" not in redacted


def test_scanning_hostile_input_stays_linear():
    """出站扫描处理的是可能被塞东西的文本，正则不得退化成二次复杂度。

    历史教训：``BEGIN.*?END`` 在「很多 BEGIN 没有 END」的输入上，每个 BEGIN 都要
    重扫余下文本，64KB 就要 1.3 秒；命中项行号逐条从头数也会造成同样的放大。
    这里给一个宽松上界（当前实测约 0.15 秒，留 30 倍余量）作为回归护栏。
    """
    hostile = "-----BEGIN RSA PRIVATE KEY-----\n" * 10000
    started = time.monotonic()
    redacted, findings = scan_and_redact(hostile)
    elapsed = time.monotonic() - started

    assert len(findings) == 10000
    assert "PRIVATE KEY" not in redacted
    assert elapsed < 5.0, f"扫描耗时 {elapsed:.2f}s，疑似复杂度回退"


# ---------------------------------------------------------------------------
# 接线：Secret Store → Redactor → Event Log
# ---------------------------------------------------------------------------


async def test_store_values_never_reach_the_event_log(tmp_path: Path):
    """端到端：库里的凭据被写进自由文本后，落库前必须已经变成遮罩。"""
    db = Database(tmp_path / "wb.db")
    await db.connect()
    try:
        store = await SecretStore.create("pw", tmp_path / "vault.bin", params=LIGHT)
        await store.put("secret://openai", {"api_key": LIVE_KEY})
        await store.put("secret://github", {"token": GH_TOKEN})

        redactor = SecretRedactor()
        assert await redactor.bind_store(store) == 2

        log = EventLog(db)
        log.set_redactor(redactor)
        await log.append(
            scope=EventScope.ATTEMPT,
            type=EventType.ATTEMPT_STARTED,
            payload={
                "note": f"harness env carries {LIVE_KEY} and {GH_TOKEN}",
                "nested": {"list": [f"prefix {LIVE_KEY} suffix"]},
                "harmless": "no secrets here",
            },
            refs=["secret://openai"],
        )

        row = (await log.tail())[0]
        stored = str(row["payload"])
        assert LIVE_KEY not in stored and GH_TOKEN not in stored
        assert row["payload"]["note"] == "harness env carries *** and ***"
        assert row["payload"]["nested"]["list"] == ["prefix *** suffix"]
        assert row["payload"]["harmless"] == "no secrets here"
        # 事件里保留的是 locator（引用），这才是凭据该出现的位置（AUTH-02）。
        assert row["refs"] == ["secret://openai"]
    finally:
        await db.close()


async def test_cross_boundary_handoff_is_scanned_before_leaving(tmp_path: Path):
    """一次跨厂商交接的完整走查：先判要不要脱敏，再脱敏，最后才允许出站。"""
    prop = SensitivityPropagator()
    source = Boundary("h1", "claude-code")
    target = Boundary("h2", "kimi-code")

    decision = prop.decide(
        own="internal",
        lineage=["sensitive"],  # 上游产出过敏感产物，沿血缘升到 sensitive
        source=source,
        target=target,
    )
    assert decision.required is True

    payload = f"artifact body\nOPENAI_API_KEY={LIVE_KEY}\n"
    outbound, findings = scan_and_redact(payload)

    assert findings
    assert LIVE_KEY not in outbound
    assert "artifact body" in outbound


async def test_redactor_can_be_cleared_with_the_store(tmp_path: Path):
    """锁定 store 时一并 clear()，明文不应继续驻留在脱敏器里。"""
    store = await SecretStore.create("pw", tmp_path / "vault.bin", params=LIGHT)
    await store.put("secret://a", {"api_key": "plain-value-0001"})

    redactor = SecretRedactor()
    await redactor.bind_store(store)
    assert redactor("x plain-value-0001 y") == "x *** y"

    await store.lock()
    redactor.clear()
    assert redactor.bound_count == 0
    assert redactor("x plain-value-0001 y") == "x plain-value-0001 y"
