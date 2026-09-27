"""路由器在独立进程里的形态：supervisor 复用同一个 ``HarnessRouter``
（架构设计 §3、§8.3、REC-01/02/03、D-07、D-09）。

``supervisor`` 持有 harness 子进程却**没有** core 的数据库，所以它用
``store=None`` + ``standalone=True``：注册信息改从显式传入的映射拿。
这里要钉住的就是这条路径没有偷偷改变语义：

1. 没有 store 必须**显式**声明 standalone，不许静默降级（装配期的错要在装配期报）；
2. 没有 store 也能建会话、发输入、收事件、查存活、释放——和常规模式一模一样；
3. ``ensure_harness`` 幂等：第二次调用不再拉进程，返回 ``True``；
4. ``stop_harness`` 只停一个 harness，把它的会话如实标成 lost（不是「正常结束」，
   也不是「崩溃」，REC-02），别的 harness 不受影响；
5. ``harness_ids`` 只列真的拉起过、且进程还活着的 harness；
6. ``adapter_env`` 能把剧本文件路径注入给适配器子进程，优先级
   ``env_template > adapter_env > os.environ``；
7. ``on_event`` 收原始 ``AdapterEvent`` 的形态照样能收到全部事件（supervisor 与
   ``app.py`` 用的是这一种）；
8. ``SessionCaps`` 经 ``asdict``/``__dict__`` → JSON → ``from_mapping`` 往返后与原值一致
   （supervisor 的 ``caps.__dict__`` 序列化路径；``ports.py`` 不动）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from workerbee.adapters.host.router import HarnessRouter, child_env
from workerbee.adapters.sdk.contract import AdapterEvent, PermissionRequest
from workerbee.adapters.sdk.protocol import AdapterError, ErrorCode
from workerbee.core.domain import Attempt
from workerbee.core.domain.registry import AuthMode, HarnessRegistration
from workerbee.core.runtime.ports import SessionCaps

from tests.conftest import make_stage

pytestmark = pytest.mark.integration

MOCK_MAIN = [sys.executable, "-m", "workerbee.adapters.mock.main"]


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------


class Recorder:
    """记录路由器回调出去的一切。

    两种 ``on_event`` 形态都收：``on_event`` 是内核三元组，``on_raw_event`` 是原始
    ``AdapterEvent``——supervisor 与 ``app.py`` 走的是后者。
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.raw_events: list[AdapterEvent] = []
        self.permissions: list[PermissionRequest] = []
        self.exits: list[tuple[str, int | None, str]] = []
        self.session_ended: list[tuple[str, bool, str | None]] = []
        self.logs: list[str] = []

    async def on_event(self, session_ref: str, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((session_ref, kind, payload))

    async def on_raw_event(self, event: AdapterEvent) -> None:
        self.raw_events.append(event)

    async def on_permission(self, request: PermissionRequest) -> None:
        self.permissions.append(request)

    async def on_exit(self, harness_id: str, code: int | None, stderr: str) -> None:
        self.exits.append((harness_id, code, stderr))

    async def on_session_ended(self, session_ref: str, ok: bool, detail: str | None) -> None:
        self.session_ended.append((session_ref, ok, detail))

    def log(self, message: str) -> None:
        self.logs.append(message)

    # ---- 断言辅助 ----

    def texts_for(self, session_ref: str) -> list[str]:
        """只有某个会话产出的文本。多 harness 并存时不能混着看。"""
        return [
            p.get("text", "")
            for ref, kind, p in self.events
            if kind == "output" and ref == session_ref
        ]

    def raw_kinds_for(self, session_ref: str) -> list[str]:
        return [
            str(e.kind) for e in self.raw_events if getattr(e, "session_ref", None) == session_ref
        ]

    def ended_ok(self, session_ref: str) -> list[bool]:
        return [ok for ref, ok, _ in self.session_ended if ref == session_ref]


async def wait_until(pred: Callable[[], bool], *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def _registration(harness_id: str = "h1", **overrides: Any) -> HarnessRegistration:
    defaults: dict[str, Any] = {
        "harness_id": harness_id,
        "name": harness_id,
        "adapter_id": "mock",
        "auth_mode": AuthMode.NATIVE_LOGIN,
        "enabled": True,
    }
    defaults.update(overrides)
    return HarnessRegistration(**defaults)


def _script_file(tmp_path: Path, name: str, script: dict[str, Any]) -> str:
    """把一份剧本写成文件，路径交给 ``adapter_env`` 或 ``env_template`` 下发。"""
    path = tmp_path / name
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _say(*texts: str) -> list[dict[str, Any]]:
    return [{"do": "output", "text": text} for text in texts]


def _make_attempt(**overrides: Any) -> Attempt:
    defaults: dict[str, Any] = {
        "stage_id": "s1",
        "task_id": "t1",
        "node_id": "A",
        "attempt_seq": 1,
        "profile_id": "p1",
    }
    defaults.update(overrides)
    return Attempt(**defaults)


async def open_session(router: HarnessRouter, harness_id: str, **overrides: Any):
    params: dict[str, Any] = {
        "harness_id": harness_id,
        "attempt": _make_attempt(),
        "stage": make_stage(),
        "model_name": "mock-model",
        "reasoning_effort": None,
        "system_prompt": None,
    }
    params.update(overrides)
    return await router.create_session(**params)


@pytest.fixture
async def make_router(tmp_path):
    """造一个 standalone 路由器（没有 store），测试结束时保证进程收干净。"""
    routers: list[HarnessRouter] = []

    async def _build(
        *,
        registrations: dict[str, HarnessRegistration] | None = None,
        adapter_env: dict[str, str] | None = None,
        raw_events: bool = False,
        **kwargs: Any,
    ) -> tuple[HarnessRouter, Recorder]:
        rec = Recorder()
        router = await HarnessRouter.create(
            None,
            standalone=True,
            registrations=registrations,
            adapter_env=adapter_env,
            adapter_commands={"mock": MOCK_MAIN},
            on_event=rec.on_raw_event if raw_events else rec.on_event,
            on_permission=rec.on_permission,
            on_exit=rec.on_exit,
            on_session_ended=rec.on_session_ended,
            log=rec.log,
            call_timeout=10.0,
            alive_timeout=3.0,
            **kwargs,
        )
        routers.append(router)
        return router, rec

    try:
        yield _build
    finally:
        for router in routers:
            await router.stop()


# ----------------------------------------------------------------------
# 1. 装配：没有 store 必须显式声明
# ----------------------------------------------------------------------


def test_store_is_required_unless_standalone_is_declared():
    """``store=None`` 且没声明 standalone：装配期就报错，不许静默降级。"""
    with pytest.raises(ValueError) as excinfo:
        HarnessRouter(None, adapter_commands={"mock": MOCK_MAIN})
    assert "standalone" in str(excinfo.value)

    # 显式声明之后就没有 store 也能构造（进程还没起）
    router = HarnessRouter(
        None,
        standalone=True,
        registrations={"h1": _registration("h1")},
        adapter_commands={"mock": MOCK_MAIN},
    )
    assert router.store is None
    assert router.standalone is True
    assert router.harness_ids == [], "没 start 之前不算已启动"


async def test_create_without_store_and_without_standalone_fails_fast():
    """同一个纪律也要落在 ``create`` 上——组合根用的是它。"""
    with pytest.raises(ValueError):
        await HarnessRouter.create(None, adapter_commands={"mock": MOCK_MAIN})


# ----------------------------------------------------------------------
# 2. 没有数据库也能走完一轮会话
# ----------------------------------------------------------------------


async def test_standalone_round_trip_without_a_store(make_router, tmp_path):
    script = _script_file(
        tmp_path,
        "hello.json",
        {"steps": _say("独立进程里也跑得起来"), "on_input": _say("收到输入")},
    )
    router, rec = await make_router(
        registrations={"h1": _registration("h1", cwd=str(tmp_path))},
        # supervisor 就是这么给 mock 下发剧本的：注入环境变量，不必为每个剧本写个入口模块
        adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": script},
    )
    assert router.store is None
    assert router.harness_ids == ["h1"]

    handle = await open_session(router, "h1")
    assert handle.is_alive()
    assert handle.persist_locator, "适配器回报的定位符照样要透传"

    assert await wait_until(lambda: "独立进程里也跑得起来" in rec.texts_for(handle.session_ref))
    assert await router.session_alive(handle.session_ref) is True

    assert await router.send_input(handle.session_ref, "继续") is True
    assert await wait_until(lambda: "收到输入" in rec.texts_for(handle.session_ref))

    await router.dispose(handle.session_ref)
    assert await router.session_alive(handle.session_ref) is False
    assert rec.ended_ok(handle.session_ref) == [True]


async def test_unregistered_harness_says_where_standalone_gets_registrations(make_router):
    """standalone 下查不到注册信息：错误消息要说清注册表从哪来。"""
    router, _ = await make_router(registrations={})
    with pytest.raises(AdapterError) as excinfo:
        await open_session(router, "没登记过的")
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "未登记" in excinfo.value.message
    assert "standalone" in excinfo.value.message


async def test_registration_added_to_the_map_later_is_honoured(make_router, tmp_path):
    """组合根往同一个映射里加 harness（supervisor 的 ``harness.ensure`` 就是这么做），
    路由器必须看得见——所以它留的是那个 dict 本身，不是启动时的一份拷贝。"""
    script = _script_file(tmp_path, "late.json", {"steps": _say("后来才登记")})
    registry: dict[str, HarnessRegistration] = {}
    router, rec = await make_router(
        registrations=registry, adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": script}
    )
    assert router.harness_ids == []

    registry["h9"] = _registration("h9", cwd=str(tmp_path))  # 启动之后才加进来的

    handle = await open_session(router, "h9")
    assert router.harness_ids == ["h9"]
    assert await wait_until(lambda: "后来才登记" in rec.texts_for(handle.session_ref))


# ----------------------------------------------------------------------
# 3. ensure_harness：按需拉起 + 幂等
# ----------------------------------------------------------------------


async def test_ensure_harness_is_idempotent(make_router, tmp_path):
    script = _script_file(tmp_path, "hello.json", {"steps": _say("按需拉起")})
    router, rec = await make_router(registrations={})  # 空注册表：注册信息随 ensure 带进来

    first = await router.ensure_harness(
        "h1", adapter_id="mock", env={"WORKERBEE_MOCK_SCRIPT_FILE": script}, cwd=str(tmp_path)
    )
    assert first is False, "第一次是这次调用真的把进程拉起来了"
    assert router.harness_ids == ["h1"]
    pid = router._procs["h1"].proc.pid

    second = await router.ensure_harness(
        "h1", adapter_id="mock", env={"WORKERBEE_MOCK_SCRIPT_FILE": script}, cwd=str(tmp_path)
    )
    assert second is True, "第二次是幂等命中：已经在跑，不重复拉进程"
    assert router._procs["h1"].proc.pid == pid, "幂等意味着进程没换"
    assert sum(1 for line in rec.logs if "已拉起适配器" in line) == 1

    # ensure 带进来的注册信息要够建会话（core → supervisor 的 harness.ensure → session.create）
    handle = await open_session(router, "h1")
    assert handle.is_alive()
    assert await wait_until(lambda: "按需拉起" in rec.texts_for(handle.session_ref))


async def test_ensure_harness_without_any_registration_is_explicit(make_router):
    router, _ = await make_router(registrations={})
    with pytest.raises(AdapterError) as excinfo:
        await router.ensure_harness("没登记过的")
    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "adapter_id" in excinfo.value.message


async def test_ensure_harness_refuses_a_disabled_harness(make_router):
    """停用是配置层的决定：按需拉起也不能绕过去。"""
    router, _ = await make_router(registrations={"h1": _registration("h1", enabled=False)})
    with pytest.raises(AdapterError) as excinfo:
        await router.ensure_harness("h1", adapter_id="mock")
    assert "已停用" in excinfo.value.message
    assert router.harness_ids == []


# ----------------------------------------------------------------------
# 4. stop_harness：只停一个 harness，会话如实标 lost
# ----------------------------------------------------------------------


async def test_stop_harness_marks_its_sessions_lost_and_leaves_others_alone(
    make_router, tmp_path
):
    script = _script_file(
        tmp_path, "busy.json", {"steps": [{"do": "output", "text": "在跑"}, {"do": "sleep_forever"}]}
    )
    router, rec = await make_router(
        registrations={
            "h1": _registration("h1", cwd=str(tmp_path)),
            "h2": _registration("h2", cwd=str(tmp_path)),
        },
        adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": script},
    )
    h1 = await open_session(router, "h1")
    h2 = await open_session(router, "h2")
    assert await router.session_alive(h1.session_ref) is True
    assert await router.session_alive(h2.session_ref) is True

    await router.stop_harness("h1")

    assert await router.session_alive(h1.session_ref) is False
    assert router.harness_ids == ["h2"], "停的是 h1，h2 不受影响"
    assert await router.session_alive(h2.session_ref) is True
    assert rec.ended_ok(h1.session_ref) == [False], (
        "会话确实没有进程在替它干活了，必须如实报 lost——报成正常结束会让内核以为"
        "这一轮跑完了（RUN-06）"
    )
    assert rec.exits == [], "主动停机不是崩溃，不许报 on_exit（REC-02）"

    # 停的是「这一次运行」，不是把 harness 注销：再需要时还能按需拉起
    assert await router.ensure_harness("h1", adapter_id="mock") is False
    assert router.harness_ids == ["h1", "h2"]
    # 老会话不会因为重新拉起适配器就复活——它已经没了，这一点不能改口（LIFE-02）
    assert await router.session_alive(h1.session_ref) is False


async def test_stop_harness_is_quiet_when_nothing_is_running(make_router):
    router, rec = await make_router(registrations={"h1": _registration("h1")})
    await router.stop_harness("h1")
    assert any("已停止 harness h1" in line for line in rec.logs)

    await router.stop_harness("h1")  # 再停一次：安静地什么都不做，不抛异常
    assert any("没有在跑的适配器进程" in line for line in rec.logs)


# ----------------------------------------------------------------------
# 5. harness_ids：只列活着的那几个
# ----------------------------------------------------------------------


async def test_harness_ids_lists_started_harnesses_only(make_router):
    router, rec = await make_router(
        registrations={
            "h1": _registration("h1"),
            "h2": _registration("h2"),
            "h3": _registration("h3", enabled=False),  # 停用的不启动
        }
    )
    assert router.harness_ids == ["h1", "h2"]
    assert any("h3" in line and "已停用" in line for line in rec.logs)


async def test_harness_ids_drops_a_harness_whose_adapter_died(make_router, tmp_path):
    """进程死了就不再算「已启动」——把「以为还在跑」报出去比报空列表更危险。"""
    script = _script_file(
        tmp_path,
        "die.json",
        {"steps": _say("起来了"), "on_input": [{"do": "exit"}]},
    )
    router, rec = await make_router(
        registrations={"h1": _registration("h1", cwd=str(tmp_path))},
        adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": script},
    )
    handle = await open_session(router, "h1")
    assert router.harness_ids == ["h1"]

    assert await router.send_input(handle.session_ref, "把进程弄死") is True
    assert await wait_until(lambda: router.harness_ids == [])
    assert rec.exits, "崩溃要报 on_exit 让上层对账（REC-02）"
    assert await router.session_alive(handle.session_ref) is False


# ----------------------------------------------------------------------
# 6. adapter_env：注入顺序 os.environ → adapter_env → env_template
# ----------------------------------------------------------------------


def test_child_env_priority_is_system_then_injected_then_registration(monkeypatch):
    """直接钉住合成顺序：注册表显式配置 > adapter_env > 系统环境。"""
    monkeypatch.setenv("WB_PRIORITY", "from-os")
    monkeypatch.setenv("WB_SYSTEM_ONLY", "keep-me")
    env = child_env(
        adapter_env={"WB_PRIORITY": "from-adapter", "WB_INJECTED_ONLY": "yes"},
        registration_env={"WB_PRIORITY": "from-registration"},
    )
    assert env["WB_PRIORITY"] == "from-registration"
    assert env["WB_SYSTEM_ONLY"] == "keep-me", "系统环境必须打底（PATH/HOME 这类不能丢）"
    assert env["WB_INJECTED_ONLY"] == "yes"
    assert env["PATH"] == child_env()["PATH"]

    assert child_env(adapter_env={"WB_PRIORITY": "from-adapter"})["WB_PRIORITY"] == "from-adapter"
    assert child_env({}, {"WB_PRIORITY": "from-registration"})["WB_PRIORITY"] == (
        "from-registration"
    )


async def test_registration_env_beats_adapter_env_in_the_child(make_router, tmp_path):
    """端到端：注册表没写就用 adapter_env，写了就听注册表的。"""
    injected = _script_file(tmp_path, "injected.json", {"steps": _say("来自 adapter_env")})
    configured = _script_file(tmp_path, "configured.json", {"steps": _say("来自注册表")})

    router, rec = await make_router(
        registrations={
            "h1": _registration("h1", env_template={"WORKERBEE_MOCK_SCRIPT_FILE": configured}),
            "h2": _registration("h2"),
        },
        adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": injected},
    )
    h1 = await open_session(router, "h1")
    h2 = await open_session(router, "h2")

    assert await wait_until(lambda: bool(rec.texts_for(h1.session_ref)))
    assert await wait_until(lambda: bool(rec.texts_for(h2.session_ref)))
    assert rec.texts_for(h1.session_ref) == ["来自注册表"], "注册表里的显式配置优先级最高"
    assert rec.texts_for(h2.session_ref) == ["来自 adapter_env"], "没写模板就用注入的那份"


async def test_system_env_still_reaches_the_child(make_router, tmp_path, monkeypatch):
    """什么都不覆盖时，子进程照样看得见继承来的系统环境变量。"""
    inherited = _script_file(tmp_path, "inherited.json", {"steps": _say("来自系统环境")})
    monkeypatch.setenv("WORKERBEE_MOCK_SCRIPT_FILE", inherited)

    router, rec = await make_router(registrations={"h1": _registration("h1")})
    handle = await open_session(router, "h1")
    assert await wait_until(lambda: rec.texts_for(handle.session_ref) == ["来自系统环境"])


async def test_secretish_adapter_env_is_warned_about(make_router):
    """adapter_env 里塞凭据同样要在日志里喊一声（AUTH-02）。"""
    router, rec = await make_router(
        registrations={"h1": _registration("h1")},
        adapter_env={"MOCK_API_KEY": "sk-plain-in-adapter-env"},
    )
    assert any(
        "MOCK_API_KEY" in line and "adapter_env" in line and "凭据" in line for line in rec.logs
    )


# ----------------------------------------------------------------------
# 7. 原始事件形态（supervisor / app.py 用的那一种）
# ----------------------------------------------------------------------


async def test_raw_event_callbacks_get_events_including_session_ended(make_router, tmp_path):
    """``on_event`` 收原始 ``AdapterEvent`` 时，session_ended 也要送到。

    ``app.py`` 与 supervisor 都在自己的回调里翻译 ``data["ok"]``；只给三元组形态送、
    不给它们送，它们就永远等不到「结束了」。
    """
    script = _script_file(
        tmp_path,
        "short.json",
        {"exit_on_terminate": False, "steps": _say("原始形态")},
    )
    router, rec = await make_router(
        registrations={"h1": _registration("h1", cwd=str(tmp_path))},
        adapter_env={"WORKERBEE_MOCK_SCRIPT_FILE": script},
        raw_events=True,
    )
    assert rec.events == [], "原始形态下不该再被翻译成三元组"

    handle = await open_session(router, "h1")
    assert await wait_until(lambda: "output" in rec.raw_kinds_for(handle.session_ref))
    assert isinstance(rec.raw_events[0], AdapterEvent)
    outputs = [e for e in rec.raw_events if str(e.kind) == "output"]
    assert [e.text for e in outputs] == ["原始形态"], "原始事件保留适配器原样字段"

    await router.terminate(handle.session_ref)
    assert await wait_until(lambda: "session_ended" in rec.raw_kinds_for(handle.session_ref))
    assert rec.ended_ok(handle.session_ref) == [False], "专用回调仍然照发（内核判据靠它）"
    ended = [e for e in rec.raw_events if str(e.kind) == "session_ended"]
    assert ended[-1].data.get("reason") == "terminate", "原始事件保留适配器原样字段"


# ----------------------------------------------------------------------
# 8. SessionCaps 往返（supervisor 的 caps.__dict__ 序列化路径）
# ----------------------------------------------------------------------

_CAPS_FIELDS = (
    "create_session",
    "resume_session",
    "interact",
    "interrupt",
    "stop",
    "compact",
    "permission_hook",
    "background_tasks",
    "pause_in_place",
    "checkpoint_resume",
    "token_usage",
)


def _caps_cases() -> list[tuple[str, SessionCaps]]:
    all_true = SessionCaps(
        **{name: True for name in _CAPS_FIELDS}, reasoning_efforts=["low", "medium", "high"]
    )
    all_false = SessionCaps(**{name: False for name in _CAPS_FIELDS}, reasoning_efforts=[])
    partial = SessionCaps(compact=True, permission_hook=True, reasoning_efforts=["low"])
    return [("all_true", all_true), ("all_false", all_false), ("partial", partial)]


@pytest.mark.parametrize(
    "caps", [case[1] for case in _caps_cases()], ids=[case[0] for case in _caps_cases()]
)
def test_session_caps_round_trip_through_mapping(caps: SessionCaps):
    """``SessionCaps.from_mapping(asdict(caps)) == caps``，三种取值组合都要成立。"""
    assert is_dataclass(SessionCaps)
    assert hasattr(caps, "__dict__"), "supervisor 直接取 caps.__dict__ 序列化"

    assert SessionCaps.from_mapping(asdict(caps)) == caps
    # supervisor 的实际路径：caps.__dict__ → JSON（socket）→ from_mapping
    over_the_wire = json.loads(json.dumps(caps.__dict__, ensure_ascii=False))
    assert SessionCaps.from_mapping(over_the_wire) == caps
    assert caps.pause_support() in ("in_place", "checkpoint", "restart", "none")


async def test_standalone_capabilities_come_from_the_started_adapter(make_router):
    """standalone 下能力也来自适配器自述（mock 全开），并原样往返。"""
    router, _ = await make_router(registrations={"h1": _registration("h1")})
    caps = await router.capabilities("h1")
    assert caps.permission_hook is True and caps.background_tasks is True, "mock 的能力全开"
    assert caps.pause_support() == "checkpoint"
    assert SessionCaps.from_mapping(caps.__dict__) == caps

    unknown = await router.capabilities("没启动过的 harness")
    assert unknown == SessionCaps(), "没有 manifest 就是保守默认，不猜（D-09）"
