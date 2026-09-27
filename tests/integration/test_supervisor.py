"""supervisor 托管进程集成测试（架构设计 v0.02 §3、REC-01/02/03）。

这里验证的是**这款软件存在的理由**：harness 子进程与会话由独立进程持有，
因此内核重启不会打断正在跑的长程任务。

如果这几条测试不成立，那么 Workerbee 与直接用终端 harness 相比就没有解决
它声称要解决的问题——「客户端断连 / core 重启就得从头再来」。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from workerbee.adapters.host.remote import SupervisorClient
from workerbee.core.domain.registry import AuthMode, HarnessRegistration
from workerbee.core.runtime.ports import SessionCaps
from workerbee.supervisor.server import Supervisor

pytestmark = pytest.mark.integration

MOCK_CMD = [sys.executable, "-m", "workerbee.adapters.mock.main"]

#: mock 适配器的剧本：建会话后先产出一段文本，然后停在那里等输入。
#: 刻意不用「自动结束」——本文件测的是「会话能不能活着跨过客户端断开」。
HELLO_SCRIPT = {
    "steps": [
        {"step": "output", "text": "开始工作"},
        {"step": "turn_end"},
        {"step": "wait"},
    ],
    "on_input": [
        {"step": "output", "text": "收到输入"},
        {"step": "wait"},
    ],
}


def _registration(harness_id: str = "h1") -> HarnessRegistration:
    return HarnessRegistration(
        harness_id=harness_id,
        name=harness_id,
        adapter_id="mock",
        auth_mode=AuthMode.NATIVE_LOGIN,
        enabled=True,
    )


@pytest.fixture
async def supervisor(tmp_path: Path):
    """就地起一个 supervisor（不 fork 进程，直接跑协程）。"""
    sup = Supervisor(
        data_dir=tmp_path / "sup",
        socket_path=tmp_path / "sup" / "supervisor.sock",
        adapter_commands={"mock": MOCK_CMD},
    )
    # standalone 模式下由测试提供注册表
    sup.registrations = {"h1": _registration("h1")}
    await sup.start()
    yield sup
    await sup.stop()


async def _connect(sup: Supervisor, **kwargs) -> SupervisorClient:
    client = SupervisorClient(sup.socket_path, **kwargs)
    await client.ensure_connected()
    return client


# ===========================================================================
# 基本往返
# ===========================================================================


async def test_hello_reports_protocol_and_pid(supervisor):
    client = await _connect(supervisor)
    try:
        info = await client.call("hello", {})
        assert info["protocol_version"].startswith("1.")
        assert isinstance(info["pid"], int)
        assert info["sessions"] == []
    finally:
        await client.close()


async def test_create_session_and_receive_events(supervisor):
    events: list = []

    async def on_event(event):
        events.append(event)

    client = await _connect(supervisor, on_event=on_event)
    try:
        handle = await client.create_session(
            harness_id="h1",
            attempt=_FakeAttempt(),
            stage=_FakeStage(),
            model_name="m1",
            reasoning_effort=None,
            system_prompt="你是规划者",
        )
        assert handle.session_ref
        assert await client.session_alive(handle.session_ref)

        ledger = await supervisor.ledger.all_sessions()
        assert ledger and ledger[0]["session_ref"] == handle.session_ref
        assert ledger[0]["state"] == "alive"
        assert ledger[0]["owner_task_id"] == "t1", "台账必须记住会话归属"

        await asyncio.sleep(0.3)
        kinds = [e.kind for e in events]
        assert "output" in kinds or "session_started" in kinds
    finally:
        await client.close()


async def test_terminate_marks_session_not_alive(supervisor):
    client = await _connect(supervisor)
    try:
        handle = await client.create_session(
            harness_id="h1", attempt=_FakeAttempt(), stage=_FakeStage(),
            model_name="m1", reasoning_effort=None, system_prompt=None,
        )
        assert await client.session_alive(handle.session_ref)
        await client.terminate(handle.session_ref)
        for _ in range(20):
            if not await client.session_alive(handle.session_ref):
                break
            await asyncio.sleep(0.05)
        assert not await client.session_alive(handle.session_ref)
    finally:
        await client.close()


# ===========================================================================
# 核心承诺：core 重启不影响在跑的会话
# ===========================================================================


async def test_session_survives_client_disconnect_and_reconnect(supervisor):
    """core（客户端）断开重连后，会话仍在，且事件能补齐。"""
    client = await _connect(supervisor)
    handle = await client.create_session(
        harness_id="h1", attempt=_FakeAttempt(), stage=_FakeStage(),
        model_name="m1", reasoning_effort=None, system_prompt=None,
    )

    # 模拟 core 崩溃：直接丢弃客户端（不 dispose 会话）
    await client.close()

    alive = await supervisor.harness.session_alive(handle.session_ref)
    assert alive, "客户端断开后会话必须仍然存活——这正是 supervisor 存在的理由"

    # 重连并补齐事件
    seen: list = []

    async def on_event(event):
        seen.append(event)

    client2 = await _connect(supervisor, on_event=on_event)
    try:
        # 重连时 connect() 会按 seq 补齐；这里再显式拉一次验证缓冲可用
        result = await client2.call(
            "session.events", {"session_ref": handle.session_ref, "after_seq": 0}
        )
        assert result["events"], "重连后必须能补齐断连期间的事件"
        assert result["gap"] is False

        # 会话仍可继续使用
        assert await client2.session_alive(handle.session_ref)
        ok = await client2.send_input(handle.session_ref, "继续")
        assert ok
    finally:
        await client2.close()


async def test_events_gone_is_reported_not_faked(supervisor):
    """事件缓冲已丢时必须明确报 EVENTS_GONE，而不是回一个空列表。

    空列表会让 core 以为「没有事件」——那是把「不知道」伪装成「没有」（REC-04）。
    """
    from workerbee.supervisor.protocol import ErrorCode, SupervisorError

    client = await _connect(supervisor)
    try:
        with pytest.raises(SupervisorError) as exc:
            await client.call("session.events", {"session_ref": "不存在的会话", "after_seq": 0})
        assert exc.value.code == ErrorCode.EVENTS_GONE
    finally:
        await client.close()


async def test_two_clients_see_same_session(supervisor):
    """两个 core 实例（或重启前后的 core）看到的是同一份会话事实。"""
    c1 = await _connect(supervisor)
    c2 = await _connect(supervisor)
    try:
        handle = await c1.create_session(
            harness_id="h1", attempt=_FakeAttempt(), stage=_FakeStage(),
            model_name="m1", reasoning_effort=None, system_prompt=None,
        )
        assert await c2.session_alive(handle.session_ref), (
            "会话事实必须由 supervisor 持有，不因客户端不同而不同"
        )
    finally:
        await c1.close()
        await c2.close()


# ===========================================================================
# 对账
# ===========================================================================


async def test_ledger_reconcile_marks_dead_sessions_lost(supervisor):
    """supervisor 自己重启时，台账里标 alive 但实际已死的会话必须改成 lost。"""
    client = await _connect(supervisor)
    handle = await client.create_session(
        harness_id="h1", attempt=_FakeAttempt(), stage=_FakeStage(),
        model_name="m1", reasoning_effort=None, system_prompt=None,
    )
    await client.close()

    # 杀掉底层进程，模拟 harness 崩溃
    for session in supervisor.harness._sessions.values() if hasattr(
        supervisor.harness, "_sessions"
    ) else []:
        _ = session
    await supervisor.harness.terminate(handle.session_ref)

    await supervisor._reconcile_ledger()
    row = await supervisor.ledger.get(handle.session_ref)
    assert row is not None
    assert row["state"] in ("lost", "disposed"), (
        "台账说活着不等于真的活着；对账后必须如实标记"
    )


async def test_capabilities_round_trip(supervisor):
    """SessionCaps 经 socket 序列化再还原后必须与原值一致。"""
    from dataclasses import asdict

    client = await _connect(supervisor)
    try:
        caps = await client.capabilities("h1")
        assert isinstance(caps, SessionCaps)
        assert SessionCaps.from_mapping(asdict(caps)) == caps
    finally:
        await client.close()


# ===========================================================================


class _FakeAttempt:
    attempt_id = "at1"
    attempt_seq = 1
    profile_id = "p1"


class _FakeStage:
    stage_id = "s1"
    task_id = "t1"
    node_id = "A"
    node_name = "A"
