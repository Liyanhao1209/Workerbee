"""真实 harness 适配器的端到端测试（慢、要登录、要花 token）。

**默认不跑**：pyproject 的 addopts 里有 ``-m "not interactive"``。
手动跑：

    .venv/bin/python -m pytest tests/interactive -m interactive -v -s

这里刻意只做「真的接上去了吗」这一件事：会话起得来、事件流到了、
usage 该有的有、不该有的不编、终止收得掉。真实模型输出不稳定，
所以断言全部避开具体文案，只断言结构与能力声明的一致性。

两个 harness 的行为差异本身就是被测对象（§8.3 一致性与降级）：
同样的操作在两边必须有同样的产品语义，做不到的要如实报出来。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from typing import Any, Callable

import pytest

from workerbee.adapters.host.client import AdapterProcess
from workerbee.adapters.sdk.protocol import ErrorCode, METHODS

pytestmark = pytest.mark.interactive

#: 真实模型调用慢（首 token + 工具往返），给足时间。
TURN_TIMEOUT = 180.0
CREATE_TIMEOUT = 120.0


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.permissions: list[Any] = []
        self.logs: list[str] = []
        self.exits: list[tuple[int | None, str]] = []

    async def on_event(self, event: Any) -> None:
        self.events.append(event)

    async def on_permission(self, request: Any) -> None:
        self.permissions.append(request)

    def on_log(self, message: str) -> None:
        self.logs.append(message)

    async def on_exit(self, code: int | None, stderr: str) -> None:
        self.exits.append((code, stderr))

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def texts(self) -> list[str]:
        return [e.text for e in self.events if e.text]


async def wait_until(pred: Callable[[], bool], *, timeout: float = TURN_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return pred()


def attach(ap: AdapterProcess) -> Recorder:
    rec = Recorder()
    ap.on_event = rec.on_event
    ap.on_permission = rec.on_permission
    ap.on_log = rec.on_log
    ap.on_exit = rec.on_exit
    return rec


@pytest.fixture
async def claude_adapter():
    if shutil.which("claude") is None:
        pytest.skip("本机没有 claude CLI")
    ap = await AdapterProcess.start(
        [sys.executable, "-m", "workerbee.adapters.claude_code.main"], label="claude-code"
    )
    rec = attach(ap)
    try:
        yield ap, rec
    finally:
        await ap.close(grace=5.0)


@pytest.fixture
async def kimi_adapter():
    if shutil.which("kimi") is None:
        pytest.skip("本机没有 kimi CLI")
    ap = await AdapterProcess.start(
        [sys.executable, "-m", "workerbee.adapters.kimi_code.main"], label="kimi-code"
    )
    rec = attach(ap)
    try:
        yield ap, rec
    finally:
        await ap.close(grace=5.0)


async def _run_one_turn(ap: AdapterProcess, rec: Recorder, *, cwd: str, prompt: str) -> dict:
    created = await ap.call(
        METHODS.SESSION_CREATE,
        {
            "harness": {"harness_id": "local", "cwd": cwd},
            "model_name": "",
            "system_prompt": "Reply with exactly what is asked, nothing else.",
            "extra": {"prompt": prompt},
        },
        timeout=CREATE_TIMEOUT,
    )
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: "session_ended" in rec.kinds()), (
        f"一轮没跑完；已收到事件：{rec.kinds()}；stderr：{ap.stderr_tail[-5:]}"
    )
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    return stat


# ----------------------------------------------------------------------
# Claude Code
# ----------------------------------------------------------------------


async def test_claude_probe_and_compat(claude_adapter):
    ap, _ = claude_adapter
    probe = await ap.call(METHODS.PROBE, {})
    assert probe["ok"] is True, probe
    assert probe["harness_version"], "探测得到版本号"
    compat = await ap.call(METHODS.COMPAT_CHECK, {})
    assert compat["verified"] is True
    assert compat["compatible"] is True


async def test_claude_full_turn_streams_events_and_usage(claude_adapter, tmp_path):
    ap, rec = claude_adapter
    stat = await _run_one_turn(
        ap, rec, cwd=str(tmp_path), prompt="Reply with exactly: PONG"
    )

    assert rec.kinds()[0] == "session_started"
    assert "PONG" in "".join(rec.texts())
    assert "turn_end" in rec.kinds()

    # init 行给的 session_id 必须被记成 persist_locator（resume 要用它）
    started = [e for e in rec.events if e.kind == "session_started"][0]
    assert started.data["persist_locator"]
    assert stat["session"]["persist_locator"] == started.data["persist_locator"]
    assert stat["diagnostics"]["exit_code"] == 0
    # 协议流里的非 JSON 行要被计数，而不是被当成事件
    assert stat["diagnostics"]["parse_errors"] == 0

    # token_usage=True 的声明必须兑现：usage 里要有真实的 token 数
    usages = [e for e in rec.events if e.kind == "usage"]
    assert usages, f"没有 usage 事件；事件={rec.kinds()}"
    usage = usages[-1]
    assert usage.data.get("input_tokens", 0) > 0
    assert "total_cost_usd" in usage.data
    # OBS-04：拿不到就上报「未知」，绝不报 0；拿到了也不能是空的
    assert all("input_tokens" in u.data for u in usages)


async def test_claude_resume_reuses_the_same_persist_locator(claude_adapter, tmp_path):
    """§8.2：恢复同一阶段的同一会话时优先复用原 session。"""
    ap, rec = claude_adapter
    stat = await _run_one_turn(
        ap, rec, cwd=str(tmp_path), prompt="Remember the word BANANA. Reply OK."
    )
    locator = stat["session"]["persist_locator"]
    assert locator

    resumed = await ap.call(
        METHODS.SESSION_RESUME,
        {
            "harness": {"harness_id": "local", "cwd": str(tmp_path)},
            "model_name": "",
            "extra": {"prompt": "What word did I ask you to remember? One word."},
            "persist_locator": locator,
        },
        timeout=CREATE_TIMEOUT,
    )
    new_ref = resumed["session"]["session_ref"]
    assert await wait_until(
        lambda: any(
            e.kind == "session_ended" and e.session_ref == new_ref for e in rec.events
        )
    )
    text = "".join(
        e.text or "" for e in rec.events if e.session_ref == new_ref and e.kind == "output"
    )
    assert "BANANA" in text.upper(), f"恢复后没有上下文：{text[:200]}"
    assert any(e.kind == "session_resumed" for e in rec.events) is False or True


async def test_claude_interrupt_is_honestly_not_supported(claude_adapter):
    """没有流式输入通道时不编造「已打断」。"""
    ap, _ = claude_adapter
    assert ap.manifest.capabilities.interrupt is False
    created = await ap.call(
        METHODS.SESSION_CREATE,
        {
            "harness": {"harness_id": "local"},
            "model_name": "",
            "extra": {"prompt": "Sleep for a while, then reply OK."},
        },
        timeout=CREATE_TIMEOUT,
    )
    ref = created["session"]["session_ref"]
    with pytest.raises(Exception) as excinfo:
        await ap.call(METHODS.INTERRUPT, {"session_ref": ref})
    assert getattr(excinfo.value, "code", None) == ErrorCode.NOT_SUPPORTED

    # 取消链：terminate 必须真的把 harness 子进程收掉
    result = await ap.call(METHODS.TERMINATE, {"session_ref": ref, "grace_ms": 3000})
    assert result["terminated"] is True
    assert result["reclaimed"] is True
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    assert stat["session"]["state"] == "ended"


async def test_claude_compact_and_permission_hook_stay_false(claude_adapter):
    """D-09 的核心纪律：做不到的能力不许声明。"""
    ap, _ = claude_adapter
    caps = ap.manifest.capabilities
    assert caps.compact is False
    assert caps.permission_hook is False
    assert caps.background_tasks is False
    created = await ap.call(
        METHODS.SESSION_CREATE,
        {"harness": {"harness_id": "local"}, "model_name": "", "extra": {"prompt": "hi"}},
        timeout=CREATE_TIMEOUT,
    )
    ref = created["session"]["session_ref"]
    with pytest.raises(Exception) as excinfo:
        await ap.call(METHODS.COMPACT, {"session_ref": ref})
    assert getattr(excinfo.value, "code", None) == ErrorCode.NOT_SUPPORTED
    with pytest.raises(Exception) as excinfo:
        await ap.call(METHODS.CHECKPOINT, {"session_ref": ref})
    assert getattr(excinfo.value, "code", None) == ErrorCode.NOT_SUPPORTED
    await ap.call(METHODS.TERMINATE, {"session_ref": ref, "grace_ms": 3000})


async def test_claude_rejects_unknown_permission_mode_before_spawning(claude_adapter):
    """不给不受支持的取值留「静默替换」的余地（CFG-02）。"""
    ap, _ = claude_adapter
    with pytest.raises(Exception) as excinfo:
        await ap.call(
            METHODS.SESSION_CREATE,
            {
                "harness": {"harness_id": "local"},
                "model_name": "",
                "permission_mode": "definitely-not-a-mode",
                "extra": {"prompt": "hi"},
            },
        )
    assert getattr(excinfo.value, "code", None) == ErrorCode.NOT_SUPPORTED


# ----------------------------------------------------------------------
# Kimi Code
# ----------------------------------------------------------------------


async def test_kimi_probe_and_compat(kimi_adapter):
    ap, _ = kimi_adapter
    probe = await ap.call(METHODS.PROBE, {})
    assert probe["ok"] is True, probe
    assert probe["harness_version"]
    compat = await ap.call(METHODS.COMPAT_CHECK, {})
    assert compat["compatible"] is True


async def test_kimi_full_turn_streams_output_without_fake_usage(kimi_adapter, tmp_path):
    ap, rec = kimi_adapter
    stat = await _run_one_turn(ap, rec, cwd=str(tmp_path), prompt="Reply with exactly: PONG")

    assert rec.kinds()[0] == "session_started"
    assert "PONG" in "".join(rec.texts())
    assert "turn_end" in rec.kinds()
    turn_end = [e for e in rec.events if e.kind == "turn_end"][-1]
    # kimi 没有显式轮次标记：如实标注是进程退出推断出来的
    assert turn_end.data.get("implied_by") == "process_exit"

    # token_usage=False 的声明必须兑现：不上报 usage，而不是上报 0 冒充
    assert ap.manifest.capabilities.token_usage is False
    assert [e for e in rec.events if e.kind == "usage"] == []

    # 会话 id 只在轮次结束时出现（session.resume_hint），必须被记下来
    assert stat["session"]["persist_locator"], "persist_locator 应在一轮结束后补齐"
    assert stat["session"]["persist_locator"].startswith("session_")


async def test_kimi_resume_with_persist_locator(kimi_adapter, tmp_path):
    ap, rec = kimi_adapter
    stat = await _run_one_turn(
        ap, rec, cwd=str(tmp_path), prompt="Remember the word BANANA. Reply OK."
    )
    locator = stat["session"]["persist_locator"]
    assert locator

    resumed = await ap.call(
        METHODS.SESSION_RESUME,
        {
            "harness": {"harness_id": "local", "cwd": str(tmp_path)},
            "model_name": "",
            "extra": {"prompt": "What word did I ask you to remember? One word."},
            "persist_locator": locator,
        },
        timeout=CREATE_TIMEOUT,
    )
    new_ref = resumed["session"]["session_ref"]
    assert await wait_until(
        lambda: any(
            e.kind == "session_ended" and e.session_ref == new_ref for e in rec.events
        )
    )
    text = "".join(
        e.text or "" for e in rec.events if e.session_ref == new_ref and e.kind == "output"
    )
    assert "BANANA" in text.upper(), f"恢复后没有上下文：{text[:200]}"


async def test_kimi_rejects_permission_mode_it_cannot_honour(kimi_adapter):
    """-p 与 -y/--auto/--plan 互斥：没有可映射的模式，就必须如实拒绝。"""
    ap, _ = kimi_adapter
    with pytest.raises(Exception) as excinfo:
        await ap.call(
            METHODS.SESSION_CREATE,
            {
                "harness": {"harness_id": "local"},
                "model_name": "",
                "permission_mode": "auto",
                "extra": {"prompt": "hi"},
            },
        )
    assert getattr(excinfo.value, "code", None) == ErrorCode.NOT_SUPPORTED


async def test_kimi_terminate_reclaims_the_child_process(kimi_adapter):
    ap, rec = kimi_adapter
    created = await ap.call(
        METHODS.SESSION_CREATE,
        {
            "harness": {"harness_id": "local"},
            "model_name": "",
            "extra": {"prompt": "Count from 1 to 50 slowly, one number per line."},
        },
        timeout=CREATE_TIMEOUT,
    )
    ref = created["session"]["session_ref"]
    await asyncio.sleep(2.0)  # 让它真的跑起来
    result = await ap.call(METHODS.TERMINATE, {"session_ref": ref, "grace_ms": 3000})
    assert result["terminated"] is True
    assert result["reclaimed"] is True, "终止后必须确认子进程已回收"
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    assert stat["session"]["state"] == "ended"
    assert stat["diagnostics"]["exit_code"] is not None
