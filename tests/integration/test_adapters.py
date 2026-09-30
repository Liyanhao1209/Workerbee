"""L3 适配层集成测试（架构设计 §3、§8.1、§8.3、§10.4、D-09）。

测三件事：

1. **契约走得通**：``AdapterProcess.start`` → 握手 → declare → 会话 → 事件 →
   权限往返 → 终止 → 进程退出无残留。这条链子是所有上层功能的地基，
   任何一环「看起来在工作」都不算数，必须真的走完。
2. **取消链是有界的**（§10.4）：terminate 之后子进程必须真的死了，
   且在有限时间内；做不到时如实回报 forced / reclaimed，而不是报告成功。
3. **失败是显式的**（§8.3、D-09）：协议版本不兼容要抛错而不是挂起；
   适配器崩溃时在途请求必须拿到明确错误且 on_exit 被调用。

真实 harness 的部分在 tests/interactive/，默认不跑。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from typing import Any, Callable

import pytest

from workerbee.adapters.claude_code.adapter import (
    SUPPORTED_EFFORTS,
    SUPPORTED_PERMISSION_MODES,
    ClaudeCodeAdapter,
)
from workerbee.adapters.host.client import AdapterProcess, AdapterSpawnError
from workerbee.adapters.kimi_code.adapter import KimiCodeAdapter
from workerbee.adapters.mock.adapter import STEP_NAMES, MockAdapter, MockScript
from workerbee.adapters.sdk.cli import CliSession
from workerbee.adapters.sdk.contract import (
    CreateSessionRequest,
    HarnessConfig,
    PermissionRequest,
)
from workerbee.adapters.sdk.protocol import AdapterError, ErrorCode, METHODS

pytestmark = pytest.mark.integration

MOCK_MAIN = [sys.executable, "-m", "workerbee.adapters.mock.main"]


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------


class Recorder:
    """把适配器推上来的通知原样收好，供断言。"""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.permissions: list[Any] = []
        self.heartbeats: list[Any] = []
        self.logs: list[str] = []
        self.exits: list[tuple[int | None, str]] = []

    async def on_event(self, event: Any) -> None:
        self.events.append(event)

    async def on_permission(self, request: Any) -> None:
        self.permissions.append(request)

    async def on_heartbeat(self, hb: Any) -> None:
        self.heartbeats.append(hb)

    def on_log(self, message: str) -> None:
        self.logs.append(message)

    async def on_exit(self, code: int | None, stderr: str) -> None:
        self.exits.append((code, stderr))

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def texts(self) -> list[str]:
        return [e.text for e in self.events if e.text]


def attach(ap: AdapterProcess) -> Recorder:
    rec = Recorder()
    ap.on_event = rec.on_event
    ap.on_permission = rec.on_permission
    ap.on_heartbeat = rec.on_heartbeat
    ap.on_log = rec.on_log
    ap.on_exit = rec.on_exit
    return rec


async def wait_until(pred: Callable[[], bool], *, timeout: float = 8.0) -> bool:
    """轮询等待。带上限，避免测试挂住变成「跑不完」而不是「失败」。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


@pytest.fixture
async def mock_harness():
    """按剧本拉起一个 mock 适配器子进程，测试结束时确保收干净。"""
    started: list[AdapterProcess] = []

    async def _start(
        script: dict | None = None,
        *,
        env: dict[str, str] | None = None,
        handshake: bool = True,
        label: str = "mock",
    ) -> tuple[AdapterProcess, Recorder]:
        full_env = dict(env or {})
        if script is not None:
            full_env["WORKERBEE_MOCK_SCRIPT"] = json.dumps(script)
        ap = await AdapterProcess.start(
            MOCK_MAIN, env=full_env, label=label, handshake=handshake
        )
        started.append(ap)
        return ap, attach(ap)

    try:
        yield _start
    finally:
        for ap in started:
            await ap.close(grace=2.0)


def create_params(
    *,
    prompt: str | None = "go",
    harness_id: str = "h1",
    model: str = "mock-model",
    **extra: Any,
) -> dict:
    body: dict[str, Any] = {"harness": {"harness_id": harness_id}, "model_name": model}
    if prompt is not None:
        body["extra"] = {"prompt": prompt}
    body["extra"] = {**(body.get("extra") or {}), **extra}
    return body


# ----------------------------------------------------------------------
# 1. 完整往返
# ----------------------------------------------------------------------


async def test_mock_handshake_declares_capabilities(mock_harness):
    ap, _ = await mock_harness()
    manifest = ap.manifest
    assert manifest is not None
    assert manifest.harness_family == "mock"
    assert manifest.protocol_version == "1.0"
    caps = manifest.capabilities
    # mock 是自家测试替身，能力全开；permission_hook 为 True 是因为它
    # 真的会发权限请求并阻塞等答复，而不是为了好看。
    assert caps.permission_hook is True
    assert caps.compact is True
    assert caps.background_tasks is True
    assert caps.resume_session is True
    assert caps.interrupt is True
    assert manifest.missing_key_capabilities() == []
    # 暂停档位：协议里没有 resume，故不声称原位暂停。
    assert caps.pause_in_place is False
    assert str(caps.pause_support()) == "checkpoint"


async def test_full_round_trip_create_events_terminate(mock_harness):
    ap, rec = await mock_harness(
        {
            # 这条链要走完 dispose，所以终止后进程要活着（进程死亡另有用例）。
            "exit_on_terminate": False,
            "steps": [
                {"do": "output", "text": "第一段"},
                {"do": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"do": "tool_result", "name": "Bash", "content": "a.txt"},
                {"do": "usage", "input_tokens": 120, "output_tokens": 7, "cost_usd": 0.01},
                {"do": "turn_end"},
            ],
        }
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert created["session"]["state"] == "alive"

    assert await wait_until(lambda: "turn_end" in rec.kinds())
    assert rec.kinds()[0] == "session_started"
    assert "第一段" in rec.texts()
    tool = [e for e in rec.events if e.kind == "tool_use"][0]
    assert tool.data["tool_name"] == "Bash"
    usage = [e for e in rec.events if e.kind == "usage"][0]
    assert usage.data["input_tokens"] == 120
    assert all(e.session_ref == ref for e in rec.events)

    # 会话台账：list / stat 能看到它
    await asyncio.sleep(0.1)  # 让最后一步的计数落定，避免读到中间态
    listed = await ap.call(METHODS.SESSION_LIST, {})
    assert [s["session_ref"] for s in listed["sessions"]] == [ref]
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    assert stat["diagnostics"]["steps_done"] == 5

    terminated = await ap.call(METHODS.TERMINATE, {"session_ref": ref})
    assert terminated["terminated"] is True
    assert terminated["already_dead"] is False
    assert "session_ended" in rec.kinds()
    assert (await ap.call(METHODS.SESSION_STAT, {"session_ref": ref}))["session"][
        "state"
    ] == "ended"

    disposed = await ap.call(METHODS.SESSION_DISPOSE, {"session_ref": ref, "forget": True})
    assert disposed["disposed"] is True
    assert (await ap.call(METHODS.SESSION_LIST, {}))["count"] == 0


async def test_permission_request_blocks_until_responded(mock_harness):
    """HUM-03 / AC-14 的唯一测试手段：权限请求必须真的把 harness 卡住。"""
    ap, rec = await mock_harness(
        {
            "steps": [
                {"do": "output", "text": "before"},
                {
                    "do": "permission",
                    "action": "Bash(rm -rf build/)",
                    "target": "build/",
                    "tool_name": "Bash",
                    "risk": "high",
                },
                {"do": "output", "text": "after"},
                {"do": "turn_end"},
            ]
        }
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]

    assert await wait_until(lambda: len(rec.permissions) == 1)
    request = rec.permissions[0]
    assert request.session_ref == ref
    assert request.action == "Bash(rm -rf build/)"
    assert request.target == "build/"
    assert request.tool_name == "Bash"

    # 未答复期间：不得再往前走一步。
    await asyncio.sleep(0.5)
    assert rec.texts() == ["before"]
    assert "after" not in rec.texts()

    # 答复后继续，并且决定真的回注到了那个会话。
    await ap.call(
        METHODS.PERMISSION_RESPOND,
        {"approval_id": request.approval_id, "decision": "approve", "by": "user:1"},
    )
    assert await wait_until(lambda: "after" in rec.texts())
    settled = [e for e in rec.events if e.data.get("state") == "permission_settled"][0]
    assert settled.data["decision"] == "approve"
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    decisions = stat["diagnostics"]["permission_decisions"]
    assert decisions[0]["decision"] == "approve"
    assert decisions[0]["by"] == "user:1"


async def test_permission_deny_is_delivered_and_duplicate_respond_rejected(mock_harness):
    ap, rec = await mock_harness(
        {"steps": [{"do": "permission", "action": "Bash(curl evil)"}, {"do": "output", "text": "done"}]}
    )
    await ap.call(METHODS.SESSION_CREATE, create_params())
    assert await wait_until(lambda: len(rec.permissions) == 1)
    approval_id = rec.permissions[0].approval_id

    await ap.call(
        METHODS.PERMISSION_RESPOND, {"approval_id": approval_id, "decision": "deny"}
    )
    assert await wait_until(lambda: "done" in rec.texts())
    settled = [e for e in rec.events if e.data.get("state") == "permission_settled"][0]
    assert settled.data["decision"] == "deny"

    # AC-14：重复通知不构成再次授权——必须显式报错，不能静默吞掉。
    with pytest.raises(Exception) as excinfo:
        await ap.call(
            METHODS.PERMISSION_RESPOND, {"approval_id": approval_id, "decision": "approve"}
        )
    assert getattr(excinfo.value, "code", None) == ErrorCode.INVALID_PARAMS


async def test_permission_timeout_defaults_to_deny(mock_harness):
    """超时默认 deny_pause（HUM-03）：不能把「没人答复」当成批准。"""
    ap, rec = await mock_harness(
        {"steps": [{"do": "permission", "action": "Bash(x)", "timeout_ms": 200},
                   {"do": "output", "text": "after-timeout"}]}
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: "after-timeout" in rec.texts())
    errors = [e for e in rec.events if e.kind == "error"]
    assert errors and errors[0].data["error_kind"] == "approval_timeout"
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    assert stat["diagnostics"]["permission_decisions"][0]["decision"] == "denied_by_timeout"


def test_permission_fingerprint_changes_with_action():
    """AC-14 的基础：动作内容一变，指纹就变，旧批准不再匹配。"""
    a = PermissionRequest.fingerprint("Bash(rm -rf build/)", "build/", "Bash")
    b = PermissionRequest.fingerprint("Bash(rm -rf dist/)", "build/", "Bash")
    same = PermissionRequest.fingerprint("Bash(rm -rf build/)", "build/", "Bash")
    assert a == same
    assert a != b


async def test_all_event_kinds_round_trip(mock_harness):
    """统一事件流（§8.1 第 5 组）的每种语义都要真的到得了内核。"""
    ap, rec = await mock_harness(
        {
            "steps": [
                {"do": "state", "state": "running"},
                {"do": "compact", "pre_tokens": 1000, "post_tokens": 250},
                {"do": "background_start", "task_id": "bg1", "label": "index"},
                {"do": "background_end", "task_id": "bg1", "status": "ok"},
                {"do": "error", "message": "rate limited", "error_class": "retryable_error",
                 "error_kind": "rate_limit"},
                {"do": "heartbeat"},
                {"do": "log", "text": "adapter note"},
                {"do": "stderr", "text": "这行只进 stderr"},
                {"do": "turn_end"},
            ]
        }
    )
    await ap.call(METHODS.SESSION_CREATE, create_params())
    assert await wait_until(lambda: "turn_end" in rec.kinds())

    kinds = set(rec.kinds())
    assert {
        "session_started",
        "state_change",
        "compact",
        "background_task_started",
        "background_task_ended",
        "error",
        "turn_end",
    } <= kinds
    compact = [e for e in rec.events if e.kind == "compact"][0]
    assert compact.data["pre_tokens"] == 1000 and compact.data["post_tokens"] == 250
    err = [e for e in rec.events if e.kind == "error"][0]
    assert err.data["error_class"] == "retryable_error"
    assert await wait_until(lambda: bool(rec.heartbeats))
    assert any("adapter note" in m for m in rec.logs)
    # stderr 不进协议通道，但会被 host 收进 stderr_tail 供排查
    assert not any("stderr" in t for t in rec.texts())
    assert any("这行只进 stderr" in line for line in ap.stderr_tail)


async def test_script_from_file_env(mock_harness, tmp_path):
    script = {"steps": [{"do": "output", "text": "from-file"}, {"do": "turn_end"}]}
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    ap, rec = await mock_harness(env={"WORKERBEE_MOCK_SCRIPT_FILE": str(path)})
    await ap.call(METHODS.SESSION_CREATE, create_params())
    assert await wait_until(lambda: "from-file" in rec.texts())


async def test_unknown_script_step_is_reported_not_swallowed(mock_harness):
    ap, rec = await mock_harness({"steps": [{"do": "no-such-step"}]})
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: any(e.kind == "error" for e in rec.events))
    err = [e for e in rec.events if e.kind == "error"][0]
    assert err.data["error_kind"] == "mock_script"
    assert err.session_ref == ref
    assert "no-such-step" in (err.text or "")


# ----------------------------------------------------------------------
# 2. 取消链（§10.4）
# ----------------------------------------------------------------------


async def test_cancel_chain_kills_child_within_bound(mock_harness):
    """terminate 之后子进程必须真的死了，且是有界时间内。"""
    ap, rec = await mock_harness(
        {"steps": [{"do": "output", "text": "working"}, {"do": "sleep_forever"}]}
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: "working" in rec.texts())
    assert ap.proc.returncode is None

    started = time.monotonic()
    result = await ap.call(METHODS.TERMINATE, {"session_ref": ref, "signal": "TERM"})
    assert result["terminated"] is True
    assert await wait_until(lambda: ap.proc.returncode is not None, timeout=5.0)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"取消链超出有界时间：{elapsed:.2f}s"
    assert ap.proc.returncode == 0
    assert ap.alive is False

    # 会话状态如实转为 ended
    assert "state_change" in rec.kinds()
    assert "session_ended" in rec.kinds()
    assert await wait_until(lambda: bool(rec.exits))


async def test_terminate_after_death_is_reported_not_faked(mock_harness):
    """已经结束了就说已经结束了，不要表演第二次「成功终止」。"""
    ap, rec = await mock_harness(
        {"exit_on_terminate": False, "steps": [{"do": "sleep_forever"}]}
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    first = await ap.call(METHODS.TERMINATE, {"session_ref": ref})
    assert first["terminated"] is True
    assert first["already_dead"] is False

    second = await ap.call(METHODS.TERMINATE, {"session_ref": ref})
    assert second["already_dead"] is True
    assert second["terminated"] is False  # 不能谎报终止成功
    assert (await ap.call(METHODS.SESSION_STAT, {"session_ref": ref}))["session"][
        "state"
    ] == "ended"
    await asyncio.sleep(0.2)  # 第二次终止不该多出一条 session_ended
    assert rec.kinds().count("session_ended") == 1


async def test_calls_after_adapter_exit_fail_explicitly(mock_harness):
    """适配器进程没了：后续调用必须拿到明确错误，而不是「成功」或挂起。"""
    ap, _ = await mock_harness({"steps": [{"do": "sleep_forever"}]})
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    await ap.call(METHODS.TERMINATE, {"session_ref": ref})
    assert await wait_until(lambda: ap.proc.returncode is not None)

    with pytest.raises(AdapterError) as excinfo:
        await ap.call(METHODS.TERMINATE, {"session_ref": ref})
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE


async def test_dispose_and_close_leave_no_leftover(mock_harness):
    script = {"steps": [{"do": "sleep_forever"}]}
    ap, _ = await mock_harness(script)
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    await ap.call(METHODS.TERMINATE, {"session_ref": created["session"]["session_ref"]})
    assert await wait_until(lambda: ap.proc.returncode is not None)

    # close 之后句柄、读取任务都要收束
    await ap.close()
    assert ap.proc.returncode is not None
    assert ap.alive is False


async def test_pause_checkpoint_resume_continues_where_it_stopped(mock_harness):
    """D-07 第二档：协作停止 + checkpoint 重建，恢复时新会话从断点继续。"""
    script = {
        "steps": [
            {"do": "output", "text": "step-1"},
            {"do": "output", "text": "step-2"},
            {"do": "permission", "action": "Bash(needs-approval)", "timeout_ms": 3000},
            {"do": "output", "text": "step-4"},
        ]
    }
    ap, rec = await mock_harness(script)
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: "step-2" in rec.texts())

    paused = await ap.call(METHODS.PAUSE, {"session_ref": ref})
    assert paused["mode"] == "cooperative_stop"
    ckpt = await ap.call(METHODS.CHECKPOINT, {"session_ref": ref})
    checkpoint = ckpt["checkpoint"]
    assert checkpoint["next_index"] == 2  # 停在 permission 那一步

    # 恢复：新会话，从 checkpoint 续跑——step-1/step-2 不得重放。
    resumed = await ap.call(
        METHODS.SESSION_RESUME,
        {
            "harness": {"harness_id": "h1"},
            "model_name": "mock-model",
            "extra": {"prompt": "go"},
            "persist_locator": created["session"]["persist_locator"],
            "checkpoint": checkpoint,
        },
    )
    new_ref = resumed["session"]["session_ref"]
    assert new_ref != ref
    assert await wait_until(lambda: len(rec.permissions) >= 2)
    assert rec.kinds().count("output") == 2  # 两个 step-1/step-2 没有重放
    await ap.call(
        METHODS.PERMISSION_RESPOND,
        {"approval_id": rec.permissions[-1].approval_id, "decision": "approve"},
    )
    assert await wait_until(lambda: "step-4" in rec.texts())
    assert "session_resumed" in rec.kinds()


async def test_send_input_reaches_only_the_target_session(mock_harness):
    """HUM-01：输入必须到达所选的那个会话。"""
    ap, _ = await mock_harness({"steps": [{"do": "sleep_forever"}]})
    a = (await ap.call(METHODS.SESSION_CREATE, create_params()))["session"]["session_ref"]
    b = (await ap.call(METHODS.SESSION_CREATE, create_params()))["session"]["session_ref"]

    await ap.call(METHODS.SEND_INPUT, {"session_ref": b, "kind": "btw", "text": "补充指示"})
    stat_a = await ap.call(METHODS.SESSION_STAT, {"session_ref": a})
    stat_b = await ap.call(METHODS.SESSION_STAT, {"session_ref": b})
    assert stat_a["diagnostics"]["received_inputs"] == []
    assert stat_b["diagnostics"]["received_inputs"] == [
        {"kind": "btw", "text": "补充指示"}
    ]


async def test_interrupt_stops_script_but_keeps_session_alive(mock_harness):
    ap, rec = await mock_harness(
        {"steps": [{"do": "output", "text": "working"}, {"do": "sleep_forever"}]}
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    ref = created["session"]["session_ref"]
    assert await wait_until(lambda: "working" in rec.texts())
    result = await ap.call(METHODS.INTERRUPT, {"session_ref": ref})
    assert result["interrupted"] is True
    assert await wait_until(lambda: any(e.data.get("state") == "interrupted" for e in rec.events))
    stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
    assert stat["session"]["state"] == "alive"


# ----------------------------------------------------------------------
# 3. 失败显式化（§8.3、D-09）
# ----------------------------------------------------------------------


async def test_adapter_side_protocol_mismatch_fails_fast(mock_harness):
    """伪造一个报错版本号的适配器：握手必须抛错，而不是挂起。"""
    ap, _ = await mock_harness(
        {"manifest": {"protocol_version": "9.9"}, "steps": []}, handshake=False
    )
    started = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        await ap.handshake()
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"握手不兼容时耗时 {elapsed:.1f}s，接近超时=等于挂起"
    assert getattr(excinfo.value, "code", None) == ErrorCode.PROTOCOL_MISMATCH


async def test_core_side_protocol_mismatch_fails_fast(mock_harness):
    """适配器自己不检查、但 declare 出来的版本不兼容：内核侧必须拦住。"""
    ap, _ = await mock_harness(
        {
            "manifest": {"protocol_version": "9.9"},
            "handshake": {"skip_version_check": True},
            "steps": [],
        },
        handshake=False,
    )
    with pytest.raises(AdapterSpawnError):
        await ap.handshake()


async def test_silent_adapter_times_out_instead_of_hanging(mock_harness):
    ap, _ = await mock_harness({"drop_methods": ["handshake"], "steps": []}, handshake=False)
    started = time.monotonic()
    with pytest.raises(AdapterError) as excinfo:
        await ap.handshake(timeout=0.5)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert "超时" in str(excinfo.value)


async def test_slow_method_hits_call_timeout(mock_harness):
    ap, _ = await mock_harness({"methods_delay_ms": {"session.list": 1500}, "steps": []})
    with pytest.raises(AdapterError) as excinfo:
        await ap.call(METHODS.SESSION_LIST, {}, timeout=0.3)
    assert "超时" in str(excinfo.value)
    # 超时后适配器仍然可用（在途请求已丢弃，不会串包）
    assert (await ap.call(METHODS.SESSION_LIST, {}, timeout=10))["count"] == 0


async def test_adapter_crash_fails_inflight_request_and_reports_exit(mock_harness):
    """适配器崩溃：在途请求拿到明确错误，on_exit 被调用，之前的输出不丢。"""
    ap, rec = await mock_harness(
        {
            "drop_methods": ["session.list"],
            "steps": [
                {"do": "output", "text": "before-crash"},
                {"do": "wait", "ms": 600},
                {"do": "exit", "code": 1},
            ],
        }
    )
    created = await ap.call(METHODS.SESSION_CREATE, create_params())
    assert created["session"]["session_ref"]
    assert await wait_until(lambda: "before-crash" in rec.texts())

    with pytest.raises(AdapterError) as excinfo:
        await ap.call(METHODS.SESSION_LIST, {}, timeout=20)
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "退出" in excinfo.value.message

    assert await wait_until(lambda: bool(rec.exits), timeout=5.0)
    assert rec.exits[0][0] == 1
    assert ap.exit_code == 1
    # 崩溃前的输出已经送达，不会被吞掉
    assert "before-crash" in rec.texts()


async def test_unreachable_executable_is_a_readable_error(mock_harness):
    with pytest.raises(AdapterSpawnError) as excinfo:
        await AdapterProcess.start(["definitely-not-a-real-binary-xyz"], label="nope")
    assert "找不到适配器可执行文件" in str(excinfo.value)


async def test_unknown_session_is_session_not_found(mock_harness):
    ap, _ = await mock_harness({"steps": []})
    with pytest.raises(Exception) as excinfo:
        await ap.call(METHODS.SESSION_STAT, {"session_ref": "nope"})
    assert getattr(excinfo.value, "code", None) == ErrorCode.SESSION_NOT_FOUND


# ----------------------------------------------------------------------
# 4. 能力声明：宁可 False，不许谎报（D-09）
# ----------------------------------------------------------------------


def test_mock_capability_surface_covers_every_script_step():
    """声明了 capability 就必须有对应的真实行为：剧本步骤名与实现必须对得上。"""
    expected = {
        "output", "wait", "permission", "compact", "background_start",
        "background_end", "error", "usage", "state", "tool_use", "tool_result",
        "turn_end", "heartbeat", "stderr", "log", "sleep_forever", "exit",
    }
    assert STEP_NAMES == expected
    script = MockScript()
    assert script.exit_on_terminate is True


def _request(prompt: str | None = "hi", **kwargs: Any) -> CreateSessionRequest:
    extra = {"prompt": prompt} if prompt is not None else {}
    return CreateSessionRequest(
        harness=HarnessConfig(harness_id="h1"), model_name="m1", extra=extra, **kwargs
    )


def _with_exec_path(exec_path: str) -> CreateSessionRequest:
    """``_request`` 的 kwargs 落在 CreateSessionRequest 上，而 exec_path 属于
    HarnessConfig——需要单独指定它时用这个。"""
    return CreateSessionRequest(
        harness=HarnessConfig(harness_id="h1", exec_path=exec_path),
        model_name="m1",
        extra={"prompt": "hi"},
    )


def test_claude_manifest_is_honest_about_what_it_cannot_do():
    caps = ClaudeCodeAdapter().manifest.capabilities
    assert caps.create_session is True
    assert caps.resume_session is True       # --resume <session-id>
    assert caps.token_usage is True          # result 行带 usage + total_cost_usd
    assert caps.structured_output is True
    assert caps.stop is True
    # 以下是三个「宁可 False」的声明
    assert caps.compact is False             # --autocompact 是启动参数，不是运行时操作
    assert caps.permission_hook is False     # 外部进程拿不到权限钩子
    assert caps.background_tasks is False    # --bg / claude agents 管的是游离会话
    assert caps.pause_in_place is False
    assert caps.checkpoint_resume is False
    assert str(caps.pause_support()) == "restart"  # D-07 第三档
    # text 输入模式下运行中无法交互
    assert caps.interact is False and caps.interrupt is False
    missing = ClaudeCodeAdapter().manifest.missing_key_capabilities()
    assert "审批（permission_hook）" in missing


def test_claude_stream_json_input_channel_upgrades_interaction_caps():
    """换配置就换声明：能力必须与当前配置一致。"""
    caps = ClaudeCodeAdapter(input_format="stream-json").manifest.capabilities
    assert caps.interact is True and caps.interrupt is True
    with pytest.raises(AdapterError):
        ClaudeCodeAdapter(input_format="yolo")


def test_claude_argv_shape():
    adapter = ClaudeCodeAdapter()
    argv = adapter.build_argv(
        request=_request(), prompt="hello", resume_locator=None, checkpoint=None
    )
    assert argv[0].endswith("claude")
    assert "-p" in argv and argv[argv.index("-p") + 1] == "hello"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert "--session-id" in argv
    assert "--permission-prompts" in argv  # 没有 SDK 宿主时显式 deny 而不是挂死

    resumed = adapter.build_argv(
        request=_request(), prompt="again", resume_locator="sess-1", checkpoint=None
    )
    assert "--resume" in resumed and resumed[resumed.index("--resume") + 1] == "sess-1"
    assert "--session-id" not in resumed


def test_claude_rejects_unsupported_effort_and_permission_mode():
    """CFG-02：不受支持的取值必须显式失败，不能静默忽略或替换。"""
    adapter = ClaudeCodeAdapter()
    with pytest.raises(AdapterError) as excinfo:
        adapter.build_argv(
            request=_request(reasoning_effort="turbo"), prompt="x",
            resume_locator=None, checkpoint=None,
        )
    assert excinfo.value.code == ErrorCode.NOT_SUPPORTED
    assert excinfo.value.data["supported"] == list(SUPPORTED_EFFORTS)

    with pytest.raises(AdapterError) as excinfo:
        adapter.build_argv(
            request=_request(permission_mode="yolo"), prompt="x",
            resume_locator=None, checkpoint=None,
        )
    assert excinfo.value.code == ErrorCode.NOT_SUPPORTED
    assert excinfo.value.data["supported"] == list(SUPPORTED_PERMISSION_MODES)


def test_claude_text_mode_requires_prompt():
    with pytest.raises(AdapterError) as excinfo:
        ClaudeCodeAdapter().build_argv(
            request=_request(prompt=None), prompt=None, resume_locator=None, checkpoint=None
        )
    assert "prompt" in excinfo.value.message


def test_kimi_manifest_is_honest_about_what_it_cannot_do():
    caps = KimiCodeAdapter().manifest.capabilities
    assert caps.create_session is True
    assert caps.resume_session is True       # -S <session_id>，实测可用
    assert caps.stop is True
    # 以下是「宁可 False」的声明
    assert caps.token_usage is False         # stream-json 里没有 usage/费用
    assert caps.compact is False
    assert caps.permission_hook is False     # 且 -p 与 -y/--auto/--plan 互斥
    assert caps.background_tasks is False
    assert caps.interact is False and caps.interrupt is False
    assert caps.reasoning_efforts == []      # 该维度不适用
    assert str(caps.pause_support()) == "restart"


def test_kimi_argv_shape_and_unsupported_modes():
    adapter = KimiCodeAdapter()
    argv = adapter.build_argv(
        request=_request(), prompt="hello", resume_locator=None, checkpoint=None
    )
    assert argv[0].endswith("kimi")
    assert argv[1] == "-p" and argv[2] == "hello"
    assert argv[argv.index("--output-format") + 1] == "stream-json"

    resumed = adapter.build_argv(
        request=_request(), prompt="again", resume_locator="session_1", checkpoint=None
    )
    assert "-S" in resumed and resumed[resumed.index("-S") + 1] == "session_1"

    # kimi 的 -p 与 -y/--auto/--plan 互斥（实测），因此没有可映射的权限模式
    with pytest.raises(AdapterError) as excinfo:
        adapter.build_argv(
            request=_request(permission_mode="auto"), prompt="x",
            resume_locator=None, checkpoint=None,
        )
    assert excinfo.value.code == ErrorCode.NOT_SUPPORTED
    assert "互斥" in excinfo.value.message  # 说清楚「为什么做不到」


def test_kimi_prefixes_system_prompt_instead_of_dropping_it():
    adapter = KimiCodeAdapter()
    argv = adapter.build_argv(
        request=_request(system_prompt="你是审阅者"),
        prompt="看一下这个补丁",
        resume_locator=None,
        checkpoint=None,
    )
    assert "你是审阅者" in argv[2]
    assert "看一下这个补丁" in argv[2]


# ----------------------------------------------------------------------
# 节点引用（Skill / MCP 工具）→ extra → argv
# ----------------------------------------------------------------------


async def test_kimi_extra_skills_become_session_skills_dir(tmp_path):
    """extra["skills"] 必须真的落成 kimi 认的目录形态，并出现在 argv 里。"""
    adapter = KimiCodeAdapter()
    req = _request(session_ref_hint="s1")
    req.extra["skills"] = [
        {"name": "代码审查", "content": "先跑测试再下结论", "version": 2},
        {"name": "写文档", "content": "用平实的中文", "version": 1},
    ]
    argv = adapter.build_argv(request=req, prompt="hi", resume_locator=None, checkpoint=None)

    assert "--skills-dir" in argv
    skills_dir = argv[argv.index("--skills-dir") + 1]
    assert os.path.isdir(skills_dir)
    for name, body in (("代码审查", "先跑测试再下结论"), ("写文档", "用平实的中文")):
        text = open(os.path.join(skills_dir, name, "SKILL.md"), encoding="utf-8").read()
        assert body in text
        assert f"name: {name}" in text

    # 会话结束（进程退出）后临时目录被尽力清理
    adapter._out = io.StringIO()  # 协议通道替身：不拉起进程也能走结束钩子
    session = CliSession(
        session_ref="s1", harness_id="h1", persist_locator=None, proc=None, label="fake"
    )
    await adapter.on_stream_closed(session, 0)
    assert not os.path.exists(skills_dir)
    assert "s1" not in adapter._session_tmpdirs


def test_kimi_explicit_skills_dir_wins_over_extra_skills(tmp_path):
    """opts / extra 里显式给的 skills_dir 优先，且不会再落临时目录。"""
    adapter = KimiCodeAdapter()
    req = _request(session_ref_hint="s1")
    req.extra["skills"] = [{"name": "s", "content": "c", "version": 1}]
    req.extra["skills_dir"] = str(tmp_path)
    argv = adapter.build_argv(request=req, prompt="hi", resume_locator=None, checkpoint=None)
    assert argv[argv.index("--skills-dir") + 1] == str(tmp_path)
    assert adapter._session_tmpdirs == {}


def test_kimi_ignores_mcp_tools():
    """kimi 没有 MCP 支持（已核实 --help）：extra["mcp_tools"] 被忽略，不报错。"""
    adapter = KimiCodeAdapter()
    req = _request(session_ref_hint="s1")
    req.extra["mcp_tools"] = [{"name": "pgsql", "transport": "stdio", "command": "mcp-pgsql"}]
    argv = adapter.build_argv(request=req, prompt="hi", resume_locator=None, checkpoint=None)
    assert "--mcp-config" not in argv


async def test_claude_extra_mcp_tools_render_to_mcp_config(tmp_path):
    """extra["mcp_tools"] 必须渲染成 claude 的 mcpServers JSON 并出现在 argv 里。"""
    adapter = ClaudeCodeAdapter()
    req = _request(session_ref_hint="s1")
    req.extra["mcp_tools"] = [
        {
            "name": "pgsql", "transport": "stdio", "command": "mcp-pgsql",
            "args": ["--ro"], "env": {"PGHOST": "db"}, "cwd": "/srv", "url": None,
        },
        {
            "name": "remote", "transport": "http", "command": None,
            "args": [], "env": {}, "cwd": None, "url": "https://mcp.example.com/sse",
        },
        {"name": "broken", "transport": "stdio", "command": None},  # 无法拉起，跳过
    ]
    argv = adapter.build_argv(request=req, prompt="hi", resume_locator=None, checkpoint=None)

    assert "--mcp-config" in argv
    config_path = argv[argv.index("--mcp-config") + 1]
    with open(config_path, encoding="utf-8") as fh:
        config = json.load(fh)
    servers = config["mcpServers"]
    assert servers["pgsql"] == {
        "command": "mcp-pgsql", "args": ["--ro"], "env": {"PGHOST": "db"}, "cwd": "/srv",
    }
    assert servers["remote"] == {"type": "http", "url": "https://mcp.example.com/sse"}
    assert "broken" not in servers

    # 会话结束后临时文件被尽力清理
    adapter._out = io.StringIO()
    session = CliSession(
        session_ref="s1", harness_id="h1", persist_locator=None, proc=None, label="fake"
    )
    await adapter.on_stream_closed(session, 0)
    assert not os.path.exists(config_path)


def test_claude_explicit_mcp_config_wins_over_extra_mcp_tools(tmp_path):
    """opts 里显式给的 mcp_config 优先于 extra 渲染结果，且不产生临时文件。"""
    adapter = ClaudeCodeAdapter()
    req = _request(session_ref_hint="s1")
    req.extra["options"] = {"mcp_config": str(tmp_path / "mine.json")}
    req.extra["mcp_tools"] = [{"name": "pgsql", "transport": "stdio", "command": "mcp-pgsql"}]
    argv = adapter.build_argv(request=req, prompt="hi", resume_locator=None, checkpoint=None)
    assert argv.count("--mcp-config") == 1
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "mine.json")
    assert adapter._session_tmpfiles == {}



def _fake_cli_session(adapter: Any, ref: str = "s1") -> None:
    """塞一个没有真实子进程的会话，用于测「不支持」路径不触碰到进程。"""
    adapter._sessions[ref] = CliSession(
        session_ref=ref, harness_id="h1", persist_locator=None, proc=None, label="fake"
    )


async def test_interrupt_returns_not_supported_without_input_channel():
    """如实返回 NOT_SUPPORTED，而不是「已打断」然后什么都没发生。"""
    for adapter in (ClaudeCodeAdapter(), KimiCodeAdapter()):
        _fake_cli_session(adapter)
        with pytest.raises(AdapterError) as excinfo:
            await adapter.on_interrupt({"session_ref": "s1"})
        assert excinfo.value.code == ErrorCode.NOT_SUPPORTED
        assert excinfo.value.error_class() == "fatal_error"

        with pytest.raises(AdapterError) as excinfo:
            await adapter.on_send_input({"session_ref": "s1", "kind": "btw", "text": "hi"})
        assert excinfo.value.code == ErrorCode.NOT_SUPPORTED


async def test_compact_and_checkpoint_are_not_supported_by_real_adapters():
    """compact=False / checkpoint_resume=False 的声明必须在行为上兑现。"""
    for adapter in (ClaudeCodeAdapter(), KimiCodeAdapter()):
        _fake_cli_session(adapter)
        with pytest.raises(AdapterError) as excinfo:
            await adapter.on_compact({"session_ref": "s1"})
        assert excinfo.value.code == ErrorCode.NOT_SUPPORTED
        with pytest.raises(NotImplementedError):
            # on_checkpoint 未实现 → SDK 转成 NOT_SUPPORTED 回包
            await adapter.on_checkpoint({"session_ref": "s1"})


def test_credential_map_only_exposes_declared_keys():
    """AUTH-02：凭据只按声明的映射向内传，别的键一律丢弃。"""
    from workerbee.adapters.sdk.cli import credential_env

    env = credential_env(
        HarnessConfig(
            harness_id="h1",
            credential={"api_key": "sk-secret", "base_url": "http://x", "internal_token": "nope"},
        ),
        ClaudeCodeAdapter().credential_env_map(),
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-secret"
    assert env["ANTHROPIC_BASE_URL"] == "http://x"
    assert "nope" not in json.dumps(env)
    # 未声明映射的 harness 不注入任何凭据
    assert credential_env(
        HarnessConfig(harness_id="h2", credential={"api_key": "sk"}), KimiCodeAdapter().credential_env_map()
    ) == {}


# ----------------------------------------------------------------------
# 5. 首轮输入的交付与权限模式声明
# ----------------------------------------------------------------------


async def test_mock_infers_accepted_initial_input_from_the_request(mock_harness):
    """脚本没指定时按实际情形推断：带了本轮输入 → True，没带 → False。

    这个字段内核会照着做决定（True 就不再 send_input），所以它必须是事实。
    """
    ap, _ = await mock_harness({"steps": [{"do": "output", "text": "ok"}]})

    with_prompt = await ap.call(METHODS.SESSION_CREATE, create_params(prompt="跑起来"))
    assert with_prompt["accepted_initial_input"] is True

    without_prompt = await ap.call(METHODS.SESSION_CREATE, create_params(prompt=None))
    assert without_prompt["accepted_initial_input"] is False


@pytest.mark.parametrize("forced", [True, False])
async def test_mock_accepted_initial_input_is_script_controlled(mock_harness, forced):
    """两种取值都要能测到：内核的两条分支（自己投 / 不投）都得有覆盖。"""
    ap, _ = await mock_harness(
        {"accepted_initial_input": forced, "steps": [{"do": "output", "text": "ok"}]}
    )
    # 故意与推断结果反着来：剧本说了算，而不是「有 prompt 就 True」
    created = await ap.call(
        METHODS.SESSION_CREATE, create_params(prompt=None if forced else "投给我")
    )
    assert created["accepted_initial_input"] is forced


def test_claude_classifies_permission_modes_from_measurements():
    """HUM-03：哪些模式会向用户提问，必须按实测分类，不能想当然。

    实测环境：claude 2.1.283，无 SDK 宿主，逐个模式跑「用 Bash 删除文件」。
    """
    caps = ClaudeCodeAdapter().manifest.capabilities
    assert caps.permission_modes == list(SUPPORTED_PERMISSION_MODES)
    # auto 直接执行；dontAsk 明确回「被拒绝」；plan 拒绝一切非只读操作且不提问；
    # bypassPermissions 语义即跳过全部检查。
    assert set(caps.non_interactive_modes) == {"auto", "bypassPermissions", "dontAsk", "plan"}
    # manual 回「需要您批准」，acceptEdits 只实测了一种动作、无法排除别的动作仍会问
    # ——两者按「会询问」处理：错放进「不询问」等于替用户放行。
    assert "manual" not in caps.non_interactive_modes
    assert "acceptEdits" not in caps.non_interactive_modes


def test_claude_maps_permission_mode_to_the_cli_flag():
    """session.create 里的 permission_mode 必须变成 --permission-mode，不静默丢弃。"""
    argv = ClaudeCodeAdapter().build_argv(
        request=_request(permission_mode="bypassPermissions"),
        prompt="x",
        resume_locator=None,
        checkpoint=None,
    )
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_stream_json_takes_the_prompt_from_stdin_not_argv():
    """流式通道下 prompt 不进命令行：既避免泄漏，也避免同一条指令被送两遍。"""
    argv = ClaudeCodeAdapter(input_format="stream-json").build_argv(
        request=_request(prompt="机密任务内容"), prompt="机密任务内容",
        resume_locator=None, checkpoint=None,
    )
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1].startswith("--"), "`-p` 是裸标志，后面不能跟 prompt"
    assert "机密任务内容" not in " ".join(argv)


def test_kimi_declares_the_only_mode_it_can_honour():
    """-p 下唯一可用的是「不给任何开关」，且它不会向用户提问（实测）。"""
    caps = KimiCodeAdapter().manifest.capabilities
    assert caps.permission_modes == ["default"]
    assert caps.non_interactive_modes == ["default"]
    # 没有钩子这件事不能省略：框架拦不住审批，用户必须知道（HUM-03 的告警据此产生）
    assert caps.permission_hook is False


@pytest.mark.parametrize("mode", ["default", ""])
def test_kimi_default_permission_mode_adds_no_flag(mode):
    """default 的语义就是「不加任何策略开关」——三个策略 flag 与 -p 互斥（实测）。"""
    argv = KimiCodeAdapter().build_argv(
        request=_request(permission_mode=mode or None), prompt="x",
        resume_locator=None, checkpoint=None,
    )
    assert "-y" not in argv and "--yolo" not in argv
    assert "--auto" not in argv and "--plan" not in argv


# ----------------------------------------------------------------------
# 5b. build_argv 是纯变换，不依赖本机装了什么
# ----------------------------------------------------------------------


class TestBuildArgvDoesNotDependOnTheEnvironment:
    """构造参数列表不该查可执行文件在不在。

    这条曾经是坏的：``build_argv`` 里调 ``find_executable``，于是每一个测
    argv 形状的用例都隐式要求本机装了 claude / kimi。开发机上装了，全绿；
    CI 上没装，11 条纯逻辑用例当场变红——而它们要测的根本不是「装没装」。

    存在性检查没有消失，只是挪到了真正要拉起进程的那一刻（``create_session``）。
    """

    def test_which_returning_nothing_does_not_break_argv(self, monkeypatch):
        """把 PATH 查找掐掉，argv 仍然要构造得出来。"""
        monkeypatch.setattr("shutil.which", lambda _name: None)

        claude = ClaudeCodeAdapter().build_argv(
            request=_request(), prompt="hello", resume_locator=None, checkpoint=None
        )
        assert claude[0] == "claude", "配置为空时应回落到默认可执行文件名"

        kimi = KimiCodeAdapter().build_argv(
            request=_request(), prompt="hello", resume_locator=None, checkpoint=None
        )
        assert kimi[0] == "kimi"

    def test_explicit_exec_path_is_used_verbatim(self, monkeypatch):
        """注册表里给的路径直接进 argv，不在这里被解析成别的样子。"""
        monkeypatch.setattr("shutil.which", lambda _name: None)
        argv = ClaudeCodeAdapter().build_argv(
            request=_with_exec_path("/opt/bin/claude"),
            prompt="x",
            resume_locator=None,
            checkpoint=None,
        )
        assert argv[0] == "/opt/bin/claude"

    async def test_spawn_still_reports_a_missing_executable(self):
        """检查挪了位置，但不能挪没了——真拉起进程时仍要给可读错误。

        用绝对路径指向一个不存在的文件，这样在解析阶段就失败，不会真的起进程。
        """
        adapter = ClaudeCodeAdapter()
        with pytest.raises(AdapterError) as excinfo:
            await adapter._spawn_session(
                _with_exec_path("/nonexistent/claude"),
                {"prompt": "x"},
                resume_locator=None,
                checkpoint=None,
            )
        assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
        assert "/nonexistent/claude" in excinfo.value.message


# ----------------------------------------------------------------------
# 6. 流式输入通道的行解析纪律（实测发现的三个坑）
# ----------------------------------------------------------------------


def _emitted(adapter: Any, ref: str = "s1") -> list[tuple[str, str, dict]]:
    """收集适配器上报的事件（不启进程，纯解析）。"""
    out: list[tuple[str, str, dict]] = []

    async def _emit(kind: Any, **kwargs: Any) -> None:
        out.append((str(kind), kwargs.get("text") or "", kwargs.get("data") or {}))

    async def _notify(_method: Any, _params: Any) -> None:
        return None

    adapter.emit_event = _emit  # type: ignore[method-assign]
    adapter.notify = _notify  # type: ignore[method-assign]
    adapter._sessions.setdefault(
        ref,
        CliSession(session_ref=ref, harness_id="h1", persist_locator=None, proc=None, label="fake"),
    )
    return out


async def test_replayed_user_message_is_not_treated_as_harness_output():
    """实测：--replay-user-messages 会把我们投进去的输入原样回显。

    当成输出会让产物里出现用户指令的副本，看起来像「harness 说了这句话」。
    """
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    events = _emitted(adapter)
    session = adapter._sessions["s1"]
    await adapter._handle_user(
        session,
        {
            "type": "user",
            "isReplay": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "我投进去的话"}]},
        },
    )
    assert events == []

    # 非 replay 的 user 行（工具结果、打断回执）仍然如实转成事件
    await adapter._handle_user(
        session,
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
    )
    assert [e[1] for e in events] == ["[Request interrupted by user]"]


async def test_only_the_first_init_line_reports_session_started():
    """实测：流式通道下**每一轮**都会重发 system/init（同一个会话）。

    重复上报会让上游以为开了好几个会话；但 persist_locator 仍要跟着更新。
    """
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    events = _emitted(adapter)
    session = adapter._sessions["s1"]

    await adapter._handle_system(session, {"subtype": "init", "session_id": "abc"})
    await adapter._handle_system(session, {"subtype": "init", "session_id": "abc"})
    assert [e[0] for e in events] == ["session_started"]
    assert session.persist_locator == "abc"


async def test_interrupted_turn_is_reported_as_user_cancelled_not_harness_error():
    """实测：打断后本轮以 result/error_during_execution 收尾。

    如实报成「用户取消」；报成 harness 故障会触发一次毫无意义的重试。
    """
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    events = _emitted(adapter)
    session = adapter._sessions["s1"]
    adapter._interrupted["s1"] = time.monotonic()

    await adapter._handle_result(
        session,
        {"subtype": "error_during_execution", "is_error": True, "usage": {"input_tokens": 10, "output_tokens": 0}},
    )
    error = [e for e in events if e[0] == "error"][0]
    assert error[2]["error_class"] == "user_cancelled"
    assert error[2]["error_kind"] == "interrupted_by_user"
    # 一次打断只解释一轮：下一轮真出错时照旧按 harness 错误报
    await adapter._handle_result(
        session, {"subtype": "error_during_execution", "is_error": True}
    )
    second = [e for e in events if e[0] == "error"][1]
    assert second[2]["error_kind"] == "harness_error"
