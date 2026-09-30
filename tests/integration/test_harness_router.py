"""适配层接进内核的接线点测试（架构设计 §3、§8.1、§8.3、§10.4、§12、D-07、D-09）。

``HarnessRouter`` 是 ``core`` 能看见的唯一适配器入口。这里测的不是「能不能跑通一次会话」
（那是 test_adapters.py 的事），而是接线本身有没有撒谎：

1. **映射对不对**：内核拿到的 ``SessionHandle`` / ``SessionCaps`` / 事件 payload
   与适配器自述、与内核读的字段名一致；
2. **降级路径**：适配器说 NOT_SUPPORTED 时，内核拿到的是保守值（False/None），
   不是异常，更不是「成功」；
3. **崩溃可见**：适配器进程死掉要在有界时间内变成「不存活」+ ``on_exit``，
   在途请求要拿到明确错误；
4. **凭据不外流**（AUTH-02）：凭据只进 ``HarnessConfig.credential``，
   不出现在任何回调、日志、异常消息里。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Callable

import pytest

from workerbee.adapters.host.router import HarnessRouter
from workerbee.adapters.sdk.contract import PermissionRequest
from workerbee.adapters.sdk.protocol import METHODS, AdapterError, ErrorCode
from workerbee.core.runtime.ports import SessionCaps
from workerbee.core.domain import Attempt
from workerbee.core.domain.registry import (
    AuthMode,
    CredentialKind,
    CredentialRef,
    HarnessRegistration,
)
from workerbee.security.secret_store import SecretStore

from tests.conftest import make_stage

pytestmark = pytest.mark.integration

MOCK_MAIN = [sys.executable, "-m", "workerbee.adapters.mock.main"]

#: 一个假得足够明显的凭据值：任何回调里出现它都算泄漏。
SECRET_VALUE = "sk-DO-NOT-LEAK-0123456789"


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------


class Recorder:
    """记录路由器回调出去的一切，供「有没有泄漏 / 有没有撒谎」的断言。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.permissions: list[PermissionRequest] = []
        self.exits: list[tuple[str, int | None, str]] = []
        self.session_ended: list[tuple[str, bool, str | None]] = []
        self.logs: list[str] = []

    async def on_event(self, session_ref: str, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((session_ref, kind, payload))

    async def on_permission(self, request: PermissionRequest) -> None:
        self.permissions.append(request)

    async def on_exit(self, harness_id: str, code: int | None, stderr: str) -> None:
        self.exits.append((harness_id, code, stderr))

    async def on_session_ended(self, session_ref: str, ok: bool, detail: str | None) -> None:
        self.session_ended.append((session_ref, ok, detail))

    def log(self, message: str) -> None:
        self.logs.append(message)

    # ---- 断言辅助 ----

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.events]

    def texts(self) -> list[str]:
        return [p.get("text", "") for _, k, p in self.events if k == "output"]

    def payload_for(self, kind: str) -> list[dict[str, Any]]:
        return [p for _, k, p in self.events if k == kind]

    def ended_ok(self, session_ref: str) -> list[bool]:
        return [ok for ref, ok, _ in self.session_ended if ref == session_ref]

    def dump(self) -> str:
        """把所有回调过的内容拼成一段文本，用于「凭据没出现在这里」的断言。"""
        return "\n".join(
            [
                json.dumps(self.events, ensure_ascii=False, default=str),
                json.dumps(
                    [p.model_dump(mode="json") for p in self.permissions], ensure_ascii=False
                ),
                json.dumps(self.exits, ensure_ascii=False),
                json.dumps(self.session_ended, ensure_ascii=False),
                "\n".join(self.logs),
            ]
        )


async def wait_until(pred: Callable[[], bool], *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


async def register_harness(store: Any, **overrides: Any) -> HarnessRegistration:
    defaults: dict[str, Any] = {
        "harness_id": "h1",
        "name": "Mock Harness",
        "adapter_id": "mock",
    }
    defaults.update(overrides)
    reg = HarnessRegistration(**defaults)
    await store.registry.upsert_harness(reg)
    return reg


def make_attempt(**overrides: Any) -> Attempt:
    defaults: dict[str, Any] = {
        "stage_id": "s1",
        "task_id": "t1",
        "node_id": "A",
        "attempt_seq": 1,
        "profile_id": "p1",
    }
    defaults.update(overrides)
    return Attempt(**defaults)


async def open_session(router: HarnessRouter, reg: HarnessRegistration, **overrides: Any):
    params: dict[str, Any] = {
        "harness_id": reg.harness_id,
        "attempt": make_attempt(),
        "stage": make_stage(),
        "model_name": "mock-model",
        "reasoning_effort": None,
        "system_prompt": "你是测试替身",
        "cwd": reg.cwd,
    }
    params.update(overrides)
    return await router.create_session(**params)


@pytest.fixture
async def mock_router(store, tmp_path):
    """按注册表拉起一个 mock 适配器；测试结束时保证进程收干净。"""
    routers: list[HarnessRouter] = []

    async def _build(
        script: dict[str, Any] | None = None,
        *,
        secret_store: Any | None = None,
        env_template: dict[str, str] | None = None,
        **reg_overrides: Any,
    ) -> tuple[HarnessRouter, Recorder, HarnessRegistration]:
        env = dict(env_template or {})
        if script is not None:
            env["WORKERBEE_MOCK_SCRIPT"] = json.dumps(script)
        reg_overrides.setdefault("harness_id", "h1")
        reg_overrides.setdefault("name", "Mock Harness")
        reg_overrides.setdefault("adapter_id", "mock")
        reg_overrides.setdefault("cwd", str(tmp_path))
        reg_overrides["env_template"] = env
        reg = await register_harness(store, **reg_overrides)

        rec = Recorder()
        router = HarnessRouter(
            store,
            adapter_commands={"mock": MOCK_MAIN},
            secret_store=secret_store,
            on_event=rec.on_event,
            on_permission=rec.on_permission,
            on_exit=rec.on_exit,
            on_session_ended=rec.on_session_ended,
            log=rec.log,
            call_timeout=10.0,
            alive_timeout=3.0,
        )
        routers.append(router)
        await router.start()
        return router, rec, reg

    try:
        yield _build
    finally:
        for router in routers:
            await router.stop()


# ----------------------------------------------------------------------
# 1. 基本映射
# ----------------------------------------------------------------------


async def test_create_send_input_events_alive_dispose(mock_router):
    """内核视角走一遍：建会话 → 发输入 → 收事件 → 查存活 → 释放。"""
    router, rec, reg = await mock_router(
        {
            "steps": [{"do": "output", "text": "准备好了"}, {"do": "turn_end"}],
            "on_input": [{"do": "output", "text": "收到输入"}],
        }
    )

    handle = await open_session(router, reg)
    assert handle.session_ref
    assert handle.harness_id == reg.harness_id
    assert handle.is_alive(), "刚建好的会话必须是 alive"
    assert handle.persist_locator, "适配器回报的持久化定位符要透传（§8.2）"
    assert handle.model_name == "mock-model"

    assert await wait_until(lambda: "准备好了" in rec.texts())
    assert rec.kinds()[0] == "session_started"
    assert await router.session_alive(handle.session_ref) is True

    assert await router.send_input(handle.session_ref, "继续") is True
    assert await wait_until(lambda: "收到输入" in rec.texts())

    await router.dispose(handle.session_ref)
    assert await router.session_alive(handle.session_ref) is False
    assert rec.ended_ok(handle.session_ref) == [True]


async def test_events_carry_the_field_names_the_kernel_reads(mock_router):
    """内核读 payload["text"]/["id"]/cost_estimate/message/kind，一个都不能少。"""
    router, rec, reg = await mock_router(
        {
            "steps": [
                {"do": "output", "text": "正文"},
                {"do": "background_start", "task_id": "bg-1"},
                {"do": "background_end", "task_id": "bg-1"},
                {"do": "usage", "input_tokens": 120, "output_tokens": 7, "cost_usd": 0.25},
                {"do": "error", "message": "模型说了句胡话", "error_kind": "model_refusal"},
                {"do": "turn_end"},
            ]
        }
    )
    await open_session(router, reg)
    assert await wait_until(lambda: "turn_end" in rec.kinds())

    assert rec.texts() == ["正文"]

    started = rec.payload_for("background_task_started")
    assert started and started[0]["id"] == "bg-1", "内核按 payload['id'] 记后台工作（RUN-06）"
    ended = rec.payload_for("background_task_ended")
    assert ended and ended[0]["id"] == "bg-1"

    usage = rec.payload_for("usage")[0]
    assert usage["input_tokens"] == 120
    assert usage["cost_estimate"] == 0.25, "cost_estimate 才是内核读的字段"

    error = rec.payload_for("error")[0]
    assert error["message"] == "模型说了句胡话"
    assert error["kind"] == "model_refusal"
    assert error["error_kind"] == "model_refusal", "适配器原有的字段不能被翻译掉"


async def test_capabilities_mirror_the_adapter_manifest(mock_router):
    router, _, reg = await mock_router({"steps": []})
    caps = await router.capabilities(reg.harness_id)

    assert caps.create_session is True
    assert caps.resume_session is True
    assert caps.interact is True
    assert caps.interrupt is True
    assert caps.compact is True
    assert caps.permission_hook is True
    assert caps.background_tasks is True
    assert caps.token_usage is True
    # 协议里没有 resume，mock 因此如实声明不支持原位暂停（D-07 第二档）
    assert caps.pause_in_place is False
    assert caps.checkpoint_resume is True
    assert caps.pause_support() == "checkpoint"
    assert caps.reasoning_efforts == ["low", "medium", "high"]


async def test_real_adapter_capabilities_pass_through_honestly(store, tmp_path):
    """注册表里写连字符也认得（manifest 自述就是连字符），且「宁可声明 False」
    的诚实性要原样传到内核看到的 ``SessionCaps``——路由层不许替适配器美化（D-09）。
    """
    await register_harness(
        store,
        harness_id="claude",
        name="Claude Code",
        adapter_id="claude-code",
        cwd=str(tmp_path),
    )
    rec = Recorder()
    router = HarnessRouter(store, on_event=rec.on_event, log=rec.log)
    try:
        await router.start()
        caps = await router.capabilities("claude")
        assert caps.create_session is True
        assert caps.resume_session is True
        assert caps.stop is True
        # claude code 做不到的三件事（autocompact 是启动参数、没有外部权限钩子）
        assert caps.compact is False
        assert caps.permission_hook is False
        assert caps.background_tasks is False
        assert caps.pause_support() == "restart"
    finally:
        await router.stop()


async def test_capabilities_conservative_when_unknown(store):
    """没有 manifest 的 harness：返回保守默认，不做乐观猜测（D-09）。"""
    router = HarnessRouter(store, adapter_commands={"mock": MOCK_MAIN})
    try:
        caps = await router.capabilities("从未启动过的 harness")
        assert caps.compact is False
        assert caps.resume_session is False
        assert caps.permission_hook is False
        assert caps.pause_support() == "restart"
    finally:
        await router.stop()


async def test_unregistered_harness_fails_loudly(store):
    router = HarnessRouter(store, adapter_commands={"mock": MOCK_MAIN})
    try:
        with pytest.raises(AdapterError) as excinfo:
            await router.create_session(
                harness_id="不存在",
                attempt=make_attempt(),
                stage=make_stage(),
                model_name="m",
                reasoning_effort=None,
                system_prompt=None,
            )
        assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
        assert "未登记" in excinfo.value.message
    finally:
        await router.stop()


async def test_unknown_adapter_id_lists_what_is_available(mock_router):
    router, _, reg = await mock_router(
        {"steps": []}, harness_id="h2", adapter_id="no-such-adapter"
    )
    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, reg)
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "no-such-adapter" in excinfo.value.message
    assert "mock" in excinfo.value.message, "报错要说清有哪些可用"


# ----------------------------------------------------------------------
# 2. 取消链（§10.4）与存活判定（LIFE-02）
# ----------------------------------------------------------------------


async def test_terminate_flips_session_alive_within_bound(mock_router):
    router, rec, reg = await mock_router(
        {"exit_on_terminate": False, "steps": [{"do": "sleep_forever"}]}
    )
    handle = await open_session(router, reg)
    assert await router.session_alive(handle.session_ref) is True

    started = time.monotonic()
    assert await router.terminate(handle.session_ref, signal="TERM") is True

    deadline = time.monotonic() + 5.0
    alive = True
    while time.monotonic() < deadline:
        alive = await router.session_alive(handle.session_ref)
        if not alive:
            break
        await asyncio.sleep(0.05)
    assert alive is False, "取消链必须在有界时间内让 session_alive 变成 False"
    assert time.monotonic() - started < 5.0, "取消链必须是有界的"
    # 被终止不是正常完成：结束回调必须报 ok=False（RUN-06 的「结束 ≠ 成功」）
    assert rec.ended_ok(handle.session_ref) == [False]
    assert any("terminate" in (detail or "") or "终止" in (detail or "") for _, _, detail in rec.session_ended)


async def test_session_alive_is_false_for_unknown_session(mock_router):
    """查不到就说不存活——宁可说「不确定」，也不说「还在跑」（LIFE-02）。"""
    router, _, _ = await mock_router({"steps": []})
    assert await router.session_alive("从来没见过的会话") is False


async def test_abort_stream_then_terminate_reclaims_the_process(mock_router):
    """完整取消链：abort_stream → terminate → 子进程真的死了。"""
    router, rec, reg = await mock_router({"steps": [{"do": "sleep_forever"}]})
    handle = await open_session(router, reg)
    proc = router._procs[reg.harness_id]  # noqa: SLF001 - 测试就是要看真实子进程

    await router.abort_stream(handle.session_ref)
    assert await router.terminate(handle.session_ref, signal="TERM") is True
    assert await wait_until(lambda: proc.proc.returncode is not None, timeout=5.0)
    assert await router.session_alive(handle.session_ref) is False
    assert await wait_until(lambda: bool(rec.exits))
    harness_id, code, _ = rec.exits[0]
    assert harness_id == reg.harness_id
    assert code == 0


# ----------------------------------------------------------------------
# 3. 崩溃可见（REC-02/03）
# ----------------------------------------------------------------------


async def test_adapter_crash_reports_exit_and_marks_sessions_lost(mock_router):
    router, rec, reg = await mock_router({"steps": [{"do": "sleep_forever"}]})
    handle = await open_session(router, reg)
    assert await router.session_alive(handle.session_ref) is True

    router._procs[reg.harness_id].proc.kill()  # noqa: SLF001

    assert await wait_until(lambda: bool(rec.exits))
    harness_id, code, _ = rec.exits[0]
    assert harness_id == reg.harness_id
    assert code is not None and code < 0, f"被信号杀掉应报负的退出码，实际 {code}"

    assert await router.session_alive(handle.session_ref) is False
    assert await wait_until(lambda: rec.ended_ok(handle.session_ref) == [False]), (
        "崩溃导致的会话丢失必须如实报 ok=False"
    )


async def test_inflight_request_fails_with_a_clear_error_when_adapter_dies(mock_router):
    router, rec, reg = await mock_router({"steps": [], "drop_methods": ["session.create"]})
    task = asyncio.create_task(open_session(router, reg))
    await asyncio.sleep(0.5)  # 请求确实发出去了，但适配器收下不回包
    assert not task.done(), "适配器没回包，请求就该挂着"

    router._procs[reg.harness_id].proc.kill()  # noqa: SLF001

    with pytest.raises(AdapterError) as excinfo:
        await asyncio.wait_for(task, timeout=5.0)
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "退出" in excinfo.value.message or "断开" in excinfo.value.message


async def test_next_session_after_a_crash_restarts_the_adapter_visibly(mock_router):
    """崩溃后按需重拉是允许的，但必须留下痕迹（不许静默复活）。"""
    router, rec, reg = await mock_router({"steps": [{"do": "turn_end"}]})
    first = await open_session(router, reg)
    assert first.session_ref
    killed = router._procs[reg.harness_id]  # noqa: SLF001

    killed.proc.kill()
    assert await wait_until(lambda: bool(rec.exits))
    assert any("适配器进程退出" in line for line in rec.logs), "崩溃必须在日志里看得见"

    second = await open_session(router, reg)
    assert second.session_ref != first.session_ref
    fresh = router._procs[reg.harness_id]  # noqa: SLF001
    assert fresh is not killed and fresh.alive, "再次用到这个 harness 时应按需重拉"


# ----------------------------------------------------------------------
# 4. 降级路径（D-07、D-09）
# ----------------------------------------------------------------------


async def test_not_supported_degrades_to_false_and_none(mock_router):
    """适配器说 NOT_SUPPORTED 时：pause→False、checkpoint→None、compact→明确否。"""
    router, rec, reg = await mock_router(
        {
            "manifest": {
                "capabilities": {
                    "pause_in_place": False,
                    "checkpoint_resume": False,
                    "compact": False,
                }
            },
            "unsupported_methods": ["control.pause", "control.checkpoint", "control.compact"],
            "steps": [{"do": "turn_end"}],
        }
    )
    handle = await open_session(router, reg)

    caps = await router.capabilities(reg.harness_id)
    assert caps.checkpoint_resume is False
    assert caps.compact is False
    assert caps.pause_support() == "restart", "还有 stop，降级档位是「从头重跑」"

    assert await router.pause(handle.session_ref) is False
    assert await router.checkpoint(handle.session_ref) is None
    compact = await router.compact(handle.session_ref, threshold=1000)
    assert compact["ok"] is False
    assert compact["reason"], "拒绝整理必须说明原因（CFG-04）"


async def test_checkpoint_token_expands_back_into_the_harness_locator(mock_router):
    """内核手里只有不透明 token；恢复时要还原成适配器认的会话定位符（D-07）。"""
    router, rec, reg = await mock_router(
        {"steps": [{"do": "output", "text": "第一步"}, {"do": "turn_end"}]}
    )
    handle = await open_session(router, reg)
    assert await wait_until(lambda: "第一步" in rec.texts())

    token = await router.checkpoint(handle.session_ref)
    assert isinstance(token, str) and token, "拿不到断点返回 None；拿到必须是字符串"

    resumed = await router.resume_session(
        harness_id=reg.harness_id,
        persist_locator=token,
        attempt=make_attempt(attempt_id="at2"),
        stage=make_stage(),
    )
    assert resumed.used_resume is True, "复用旧会话必须在句柄上留痕（§8.2）"
    assert resumed.persist_locator == handle.persist_locator, (
        "resume 要带的是 harness 自己的会话定位符，而不是那个不透明 token"
    )
    assert "session_resumed" in rec.kinds()


async def test_unknown_checkpoint_token_is_passed_through_with_a_warning(mock_router):
    """没见过的 token（例如路由器重启过）：原样透传 + 告警，不假装知道它是什么。"""
    router, rec, reg = await mock_router({"steps": [{"do": "turn_end"}]})
    reg_locator = await router.resume_session(
        harness_id=reg.harness_id,
        persist_locator="某个重建后不认识的断点",
        attempt=make_attempt(),
        stage=make_stage(),
    )
    assert reg_locator.persist_locator == "某个重建后不认识的断点"
    assert any("没有本进程内的展开记录" in line for line in rec.logs)


# ----------------------------------------------------------------------
# 5. 权限请求（HUM-03）
# ----------------------------------------------------------------------


async def test_permission_request_reaches_the_callback_and_unblocks(mock_router):
    router, rec, reg = await mock_router(
        {
            "steps": [
                {"do": "permission", "action": "Bash(rm -rf build/)", "target": "build/"},
                {"do": "output", "text": "批准后继续"},
                {"do": "turn_end"},
            ]
        }
    )
    handle = await open_session(router, reg)
    assert await wait_until(lambda: bool(rec.permissions))
    request = rec.permissions[0]
    assert request.session_ref == handle.session_ref
    assert request.action == "Bash(rm -rf build/)"

    await asyncio.sleep(0.3)
    assert "批准后继续" not in rec.texts(), "未答复期间 harness 必须真的卡住"

    proc = router._procs[reg.harness_id]  # noqa: SLF001
    await proc.call(
        METHODS.PERMISSION_RESPOND,
        {"approval_id": request.approval_id, "decision": "approve", "by": "user:1"},
    )
    assert await wait_until(lambda: "批准后继续" in rec.texts())


# ----------------------------------------------------------------------
# 6. 凭据（AUTH-02）
# ----------------------------------------------------------------------


async def test_credential_reaches_adapter_but_never_leaks(mock_router, tmp_path):
    vault = await SecretStore.create("测试口令", tmp_path / "vault.json")
    await vault.put("secret://mock-llm", {"api_key": SECRET_VALUE})
    credential = CredentialRef(
        credential_id="cred-1",
        label="Mock LLM",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://mock-llm",
    )

    router, rec, reg = await mock_router(
        {"steps": [{"do": "turn_end"}]},
        secret_store=vault,
        auth_binding=credential.credential_id,
        auth_mode=AuthMode.API_KEY,
    )
    await router.store.registry.upsert_credential(credential)

    handle = await open_session(router, reg)
    assert await wait_until(lambda: "turn_end" in rec.kinds())
    await router.send_input(handle.session_ref, "再来一轮")

    # 凭据确实随请求到了适配器（mock 只回报键名，不回报值）
    proc = router._procs[reg.harness_id]  # noqa: SLF001
    stat = await proc.call(METHODS.SESSION_STAT, {"session_ref": handle.session_ref})
    assert stat["diagnostics"]["credential_keys"] == ["api_key"]

    # 但它不出现在任何回调、日志里——连键名都不该出现
    dumped = rec.dump()
    assert SECRET_VALUE not in dumped, "凭据值泄漏到了回调/日志里"
    assert "api_key" not in dumped, "整个 credential 字典被透传进了回调/日志"

    # 凭据被撤销后必须拒绝建会话，且错误里只有 locator，没有值
    await vault.revoke("secret://mock-llm")
    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, reg)
    assert SECRET_VALUE not in excinfo.value.message
    assert "secret://mock-llm" in excinfo.value.message


async def test_missing_credential_reference_is_an_explicit_failure(mock_router):
    """绑定了不存在的凭据引用：明确失败，绝不「没凭据也照跑」。"""
    router, _, reg = await mock_router(
        {"steps": []}, auth_binding="cred-不存在", auth_mode=AuthMode.API_KEY
    )
    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, reg)
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "不存在的凭据" in excinfo.value.message


async def test_missing_secret_store_is_reported_not_ignored(mock_router):
    credential = CredentialRef(
        credential_id="cred-2",
        label="Mock LLM",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://mock-llm",
    )
    router, _, reg = await mock_router(
        {"steps": []}, secret_store=None, auth_binding=credential.credential_id
    )
    await router.store.registry.upsert_credential(credential)
    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, reg)
    assert "凭据库未解锁" in excinfo.value.message


async def test_native_login_harness_needs_no_credential(mock_router):
    """auth_binding 为空 = 依赖本机登录态：不解析凭据，也不报错。"""
    router, _, reg = await mock_router({"steps": [{"do": "turn_end"}]})
    handle = await open_session(router, reg)
    proc = router._procs[reg.harness_id]  # noqa: SLF001
    stat = await proc.call(METHODS.SESSION_STAT, {"session_ref": handle.session_ref})
    assert stat["diagnostics"]["credential_keys"] == []


# ----------------------------------------------------------------------
# 6.1 节点候选级凭据（ExecutionProfile.credential_ref）
# ----------------------------------------------------------------------


async def test_profile_credential_ref_reaches_adapter(mock_router, tmp_path):
    """harness 没有 auth_binding 时，候选上的 credential_ref 单独生效。"""
    vault = await SecretStore.create("测试口令", tmp_path / "vault.json")
    await vault.put("secret://per-node", {"auth_token": SECRET_VALUE})
    credential = CredentialRef(
        credential_id="cred-node",
        label="节点级凭据",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://per-node",
    )
    router, rec, reg = await mock_router({"steps": [{"do": "turn_end"}]}, secret_store=vault)
    await router.store.registry.upsert_credential(credential)

    handle = await open_session(router, reg, credential_ref="cred-node")
    proc = router._procs[reg.harness_id]  # noqa: SLF001
    stat = await proc.call(METHODS.SESSION_STAT, {"session_ref": handle.session_ref})
    assert stat["diagnostics"]["credential_keys"] == ["auth_token"]
    assert SECRET_VALUE not in rec.dump()


async def test_profile_credential_ref_overrides_auth_binding(mock_router, tmp_path):
    """候选上的 credential_ref 优先于 harness 的 auth_binding（候选是更具体的意图）。"""
    vault = await SecretStore.create("测试口令", tmp_path / "vault.json")
    await vault.put("secret://binding", {"api_key": "sk-binding-000000000000"})
    await vault.put("secret://override", {"auth_token": SECRET_VALUE})
    binding = CredentialRef(
        credential_id="cred-binding",
        label="harness 绑定",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://binding",
    )
    override = CredentialRef(
        credential_id="cred-override",
        label="候选覆盖",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://override",
    )
    router, _, reg = await mock_router(
        {"steps": [{"do": "turn_end"}]},
        secret_store=vault,
        auth_binding=binding.credential_id,
        auth_mode=AuthMode.API_KEY,
    )
    await router.store.registry.upsert_credential(binding)
    await router.store.registry.upsert_credential(override)

    handle = await open_session(router, reg, credential_ref="cred-override")
    proc = router._procs[reg.harness_id]  # noqa: SLF001
    stat = await proc.call(METHODS.SESSION_STAT, {"session_ref": handle.session_ref})
    assert stat["diagnostics"]["credential_keys"] == ["auth_token"], (
        "候选上的 credential_ref 必须压过 harness 的 auth_binding"
    )


async def test_revoked_profile_credential_is_an_explicit_failure(mock_router, tmp_path):
    """候选引用了已撤销的凭据：明确失败，不静默退回本机登录态。"""
    vault = await SecretStore.create("测试口令", tmp_path / "vault.json")
    await vault.put("secret://gone", {"api_key": SECRET_VALUE})
    credential = CredentialRef(
        credential_id="cred-gone",
        label="已撤销",
        kind=CredentialKind.API_KEY,
        secret_locator="secret://gone",
        revoked=True,
    )
    router, _, reg = await mock_router({"steps": []}, secret_store=vault)
    await router.store.registry.upsert_credential(credential)

    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, reg, credential_ref="cred-gone")
    assert "已被撤销" in excinfo.value.message
    assert SECRET_VALUE not in excinfo.value.message


async def test_secretish_env_template_is_warned_about(mock_router):
    """env_template 里直接塞凭据：至少要在日志里喊一声（AUTH-02）。"""
    router, rec, reg = await mock_router(
        {"steps": []}, env_template={"MOCK_API_KEY": "sk-plain-in-env"}
    )
    await open_session(router, reg)
    assert any("MOCK_API_KEY" in line and "凭据" in line for line in rec.logs)


# ----------------------------------------------------------------------
# 7. 启动路径的健壮性
# ----------------------------------------------------------------------


async def test_start_does_not_die_when_one_harness_cannot_spawn(store, tmp_path):
    """一个起不来的适配器不能拖垮整机启动；其余 harness 照常可用。"""
    await register_harness(store, harness_id="good", name="Good", adapter_id="mock")
    await register_harness(store, harness_id="bad", name="Bad", adapter_id="not-installed")

    rec = Recorder()
    router = HarnessRouter(
        store, adapter_commands={"mock": MOCK_MAIN}, on_event=rec.on_event, log=rec.log
    )
    try:
        await router.start()  # 不应抛异常
        assert any("bad" in line and "启动失败" in line for line in rec.logs)

        good = await store.registry.get_harness("good")
        handle = await open_session(router, good)
        assert handle.session_ref, "坏 harness 不得影响好 harness"
    finally:
        await router.stop()


async def test_stop_closes_processes_without_faking_a_crash(store, tmp_path):
    """主动停机不是崩溃：不得报 on_exit / on_session_ended（否则对账会误判）。"""
    reg = await register_harness(
        store,
        harness_id="h1",
        name="Mock",
        adapter_id="mock",
        cwd=str(tmp_path),
        env_template={
            "WORKERBEE_MOCK_SCRIPT": json.dumps({"steps": [{"do": "sleep_forever"}]})
        },
    )
    rec = Recorder()
    router = HarnessRouter(
        store,
        adapter_commands={"mock": MOCK_MAIN},
        on_exit=rec.on_exit,
        on_session_ended=rec.on_session_ended,
        log=rec.log,
    )
    await router.start()
    handle = await open_session(router, reg)

    await router.stop()
    assert rec.exits == []
    assert rec.session_ended == []
    assert await router.session_alive(handle.session_ref) is False


async def test_spawn_failure_surfaces_as_harness_unavailable(store, tmp_path):
    """适配器可执行文件不存在：建会话必须明确失败，而不是无声无息地什么都没有。"""
    reg = await register_harness(
        store, harness_id="h1", name="Mock", adapter_id="mock", cwd=str(tmp_path)
    )
    rec = Recorder()
    router = HarnessRouter(
        store,
        adapter_commands={"mock": ["/definitely/not/here"]},
        on_event=rec.on_event,
        log=rec.log,
    )
    try:
        with pytest.raises(AdapterError) as excinfo:
            await open_session(router, reg)
        assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
        assert "拉起适配器失败" in excinfo.value.message
    finally:
        await router.stop()


# ----------------------------------------------------------------------
# 7. 首轮输入（initial_input）与权限模式
# ----------------------------------------------------------------------


async def test_initial_input_travels_in_extra_prompt(mock_router):
    """首轮输入必须随建会话一起给到适配器（claude -p 只有这一次机会）。

    通道是协议约定的 ``extra["prompt"]``——适配器的 ``extra_prompt()`` 读它。
    """
    captured: dict[str, Any] = {}

    router, rec, reg = await mock_router(
        {"steps": [{"do": "output", "text": "收到"}]}
    )
    original = router._build_request

    def spy(*args: Any, **kwargs: Any):
        request = original(*args, **kwargs)
        captured["extra"] = dict(request.extra)
        return request

    router._build_request = spy  # type: ignore[method-assign]
    handle = await open_session(router, reg, initial_input="只回复四个字：你好世界")

    assert captured["extra"]["prompt"] == "只回复四个字：你好世界"
    assert handle.accepted_initial_input is True, "mock 已按 extra.prompt 认领首轮输入"


async def test_initial_input_is_not_overwritten_when_extra_already_has_one(mock_router):
    """调用方显式放了 prompt 就以它为准，路由器不覆盖上层准备的内容。"""
    captured: dict[str, Any] = {}
    router, rec, reg = await mock_router({"steps": []})
    original = router._build_request

    def spy(*args: Any, **kwargs: Any):
        request = original(*args, **kwargs)
        captured["extra"] = dict(request.extra)
        return request

    router._build_request = spy  # type: ignore[method-assign]
    await open_session(router, reg, initial_input="新输入", extra={"prompt": "上层给的"})
    assert captured["extra"]["prompt"] == "上层给的"


async def test_accepted_initial_input_uses_the_adapter_answer(mock_router):
    """适配器说没收到就是没收到：内核据此决定要不要再投一次。"""
    router, rec, reg = await mock_router(
        {"accepted_initial_input": False, "steps": [{"do": "output", "text": "x"}]}
    )
    handle = await open_session(router, reg, initial_input="投给我")
    assert handle.accepted_initial_input is False


async def test_accepted_initial_input_fallback_is_conservative(mock_router):
    """适配器没回这个字段（老适配器）时的兜底：只在「确实给了首轮输入」且
    「适配器自述 interact=False」两件事都成立才判 True。

    其余一律 False——判 True 猜错的代价是内核不再投输入，阶段会永远等一个
    不会开始的任务；判 False 猜错的代价只是一次会被如实拒绝的多余投递。
    """
    router, _, reg = await mock_router({"steps": []})
    proc = router._procs[reg.harness_id]
    caps = proc.manifest.capabilities
    original = caps.interact
    try:
        caps.interact = False
        assert router._accepted_initial_input({}, initial_input="有输入", proc=proc) is True
        assert router._accepted_initial_input({}, initial_input=None, proc=proc) is False
        # 自述可交互的适配器拿不到这份信任：它随时能收输入，不能假定已交付
        caps.interact = True
        assert router._accepted_initial_input({}, initial_input="有输入", proc=proc) is False
        # 适配器进程都没了更不敢说 True
        assert router._accepted_initial_input({}, initial_input="有输入", proc=None) is False
    finally:
        caps.interact = original


async def test_permission_mode_is_passed_through(mock_router):
    """内核算出来的候选权限模式必须原样到达适配器（CFG-02：不静默忽略）。"""
    captured: dict[str, Any] = {}
    router, _, reg = await mock_router({"steps": []})
    original = router._build_request

    def spy(*args: Any, **kwargs: Any):
        request = original(*args, **kwargs)
        captured["mode"] = request.permission_mode
        return request

    router._build_request = spy  # type: ignore[method-assign]
    await open_session(router, reg, permission_mode="auto")
    assert captured["mode"] == "auto"


async def test_permission_capabilities_reach_the_kernel(mock_router):
    """HUM-03 的校验读的是这份能力：两个新字段必须真的流到 SessionCaps。"""
    router, _, reg = await mock_router({"steps": []})
    caps = await router.capabilities(reg.harness_id)
    assert caps.permission_modes == ["default", "auto", "manual"]
    assert caps.non_interactive_modes == ["default", "auto"]


async def test_capabilities_start_the_harness_on_demand(store, tmp_path):
    """还没拉起适配器时问能力：按需拉起后问，而不是回一份全 False 的假声明。

    回假声明的后果很具体：校验管线会认为「该 harness 未声明任何不询问的权限
    模式」，于是 claude / kimi 这类没有权限钩子的 harness 永远不可用。
    """
    reg = await register_harness(
        store,
        harness_id="h1",
        name="Mock",
        adapter_id="mock",
        cwd=str(tmp_path),
        env_template={"WORKERBEE_MOCK_SCRIPT": json.dumps({"steps": []})},
    )
    router = HarnessRouter(
        store, adapter_commands={"mock": MOCK_MAIN}, on_event=Recorder().on_event
    )
    try:
        assert router._procs.get("h1") is None, "前提：这时还没拉起适配器"
        caps = await router.capabilities("h1")
        assert caps.interact is True and caps.permission_modes == ["default", "auto", "manual"]
    finally:
        await router.stop()


async def test_capabilities_of_an_unspawnable_harness_stay_conservative(store):
    """拉不起来就回保守默认值，并且**记一笔**——不假装它支持什么。"""
    await register_harness(
        store, harness_id="h1", name="Mock", adapter_id="mock"
    )
    rec = Recorder()
    router = HarnessRouter(
        store, adapter_commands={"mock": ["/definitely/not/here"]}, log=rec.log
    )
    try:
        caps = await router.capabilities("h1")
        assert caps == SessionCaps()
        assert any("未能拉起" in line for line in rec.logs)
    finally:
        await router.stop()
