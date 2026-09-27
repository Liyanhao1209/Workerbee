"""接入运行中的会话（HUM-01/02）。

两条纪律决定了这个功能的形状，测试主要围着它们转：

1. **「能看不能发」是正常状态，不是错误。** 有些 harness 的输入在创建会话时
   就已给定（``claude -p`` 一类的一次性形态），运行中无法再注入。把这种情况
   和「会话已失联」混成同一个 false，界面就只能对用户说「不支持」——而用户
   需要知道的是「能看，但发不进去」。

2. **只投首轮之后的消息。** 首轮输入若已在建会话时交付，重复投递会让同一条
   指令执行两遍。对会改文件的 agent，那是数据损坏，不是小毛病。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.registry import HarnessRegistration
from workerbee.core.runtime.launch import launch_task
from workerbee.core.runtime.ports import SessionCaps

from tests.conftest import drain
from tests.fakes import FakeHarness
from tests.helpers import graph

pytestmark = pytest.mark.integration


def _caps(*, interact: bool, interrupt: bool = True) -> SessionCaps:
    return SessionCaps(
        resume_session=True,
        interrupt=interrupt,
        compact=True,
        token_usage=True,
        permission_hook=False,
        background_tasks=False,
        checkpoint_resume=False,
        interact=interact,
    )


async def _register(store, caps: SessionCaps | None = None) -> None:
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id="h1",
            name="h1",
            adapter_id="fake",
            capabilities_snapshot={"compact": True, "interact": True},
            last_probe_ok=True,
        )
    )


async def _running(engine, store, make_workflow) -> tuple[str, str]:
    """把一条单节点流程派发到 running，返回 ``(task_id, session_ref)``。

    先停掉后台调度循环再手工 tick：测试要的是确定的时序，不是「等一秒大概就好了」。
    """
    engine.scheduler.stop()
    await _register(store)
    wf, _ = await make_workflow(graph({"A": []}))
    task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id
    await drain(engine.scheduler)
    rt = engine.scheduler.runtime_for_session(
        next(iter(engine.scheduler._runtimes.values())).session_ref or ""
    )
    assert rt is not None and rt.session_ref, "阶段没有派发到 running，测试前提不成立"
    return task_id, rt.session_ref


async def _engine_with(engine_factory, harness: FakeHarness):
    engine = await engine_factory()
    engine.harness = harness
    engine.scheduler.harness = harness
    return engine


# ===========================================================================
# 可接入
# ===========================================================================


async def test_running_session_is_attachable(engine_factory, store, make_workflow):
    """在跑的会话可接入，且带上已累积的输出。"""
    harness = FakeHarness(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    # 模拟 agent 已经在产出
    await engine.scheduler.on_event(
        session_ref=session_ref, kind="output", payload={"text": "正在读取文件"}
    )

    info = await engine.attach_session(session_ref)

    assert info["attachable"] is True
    assert info["readable"] is True
    assert info["writable"] is True
    assert info["reason"] is None
    assert "正在读取文件" in info["output"]
    assert info["harness_id"] == "h1"


async def test_input_reaches_the_session(engine_factory, store, make_workflow):
    """注入的消息必须真的到达那个会话——不是「调用没报错」就算数。

    断言看**最后一条**而不是全部：交互式 harness 的首轮输入（上下文围栏）本来
    就走 ``send_input`` 投递，所以 ``inputs`` 里已经有一条了。attach 投的是
    首轮之后的消息，这正是它该有的语义。
    """
    harness = FakeHarness(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)
    before = len(harness.sessions[session_ref].inputs)

    result = await engine.send_to_session(session_ref, "顺带看一眼测试文件")

    assert result["delivered"] is True
    assert result["reason"] is None
    sent = harness.sessions[session_ref].inputs
    assert len(sent) == before + 1, "消息没有到达会话本身"
    assert sent[-1] == "顺带看一眼测试文件"


# ===========================================================================
# 能看不能发
# ===========================================================================


async def test_no_interact_is_readable_but_not_writable(engine_factory, store, make_workflow):
    """不支持运行中交互的 harness：能看输出，发不进去，且原因说得清楚。

    这是 Kimi 的 ``-p`` 模式那种情况。它不是故障——是这类 harness 的形态。
    """
    harness = FakeHarness(caps=_caps(interact=False))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    info = await engine.attach_session(session_ref)

    assert info["readable"] is True, "不能因为发不进去就不让看"
    assert info["writable"] is False
    assert info["attachable"] is False
    assert info["reason"] and "不支持运行中交互" in info["reason"]


async def test_input_to_non_interactive_session_is_refused_with_reason(
    engine_factory, store, make_workflow
):
    """拒绝要给出原因，不能只回一个 false。"""
    harness = FakeHarness(caps=_caps(interact=False))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    before = list(harness.sessions[session_ref].inputs)
    result = await engine.send_to_session(session_ref, "在吗")

    assert result["delivered"] is False
    assert result["reason"] and "不支持运行中交互" in result["reason"]
    assert harness.sessions[session_ref].inputs == before, "不该真的把消息投出去"


async def test_capability_lookup_failure_defaults_to_not_writable(
    engine_factory, store, make_workflow
):
    """能力取不到时按「不支持」处理。

    宁可少给一个按钮，也不能让用户输完一整句才失败。
    """

    class _Flaky(FakeHarness):
        async def capabilities(self, harness_id: str):  # type: ignore[override]
            raise RuntimeError("适配器没响应")

    harness = _Flaky(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    info = await engine.attach_session(session_ref)

    assert info["writable"] is False
    assert info["reason"] and "不支持运行中交互" in info["reason"]


# ===========================================================================
# 接入不了的情况
# ===========================================================================


async def test_dead_session_is_not_readable(engine_factory, store, make_workflow):
    """会话失联后既不能看也不能发，且原因说的是「失联」而不是「不支持交互」。"""
    harness = FakeHarness(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    harness.sessions[session_ref].alive = False
    info = await engine.attach_session(session_ref)

    assert info["readable"] is False
    assert info["writable"] is False
    assert info["reason"] and "失联" in info["reason"]


async def test_ended_stage_has_nothing_to_attach(engine_factory, store, make_workflow):
    """已结束的尝试没有可接入的对象，并指向事件时间线。

    这条对应一个真实的使用困惑：任务跑完了再去点「接入」，用户需要知道的是
    「这个会话已经结束了，历史在别处」，而不是一个空白控制台。
    """
    harness = FakeHarness(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    task_id, session_ref = await _running(engine, store, make_workflow)

    # 让这次尝试正常结束，运行态随之弹出
    await engine.scheduler.on_session_ended(session_ref=session_ref, ok=True)
    assert engine.scheduler.runtime_for_session(session_ref) is None

    info = await engine.attach_session(session_ref)

    assert info["attachable"] is False
    assert info["reason"] and "不在运行中" in info["reason"]
    assert "任务详情" in info["reason"], "要告诉用户去哪儿看历史"
    assert task_id  # 任务确实存在，只是这个会话已经结束


# ===========================================================================
# 输出缓冲
# ===========================================================================


async def test_output_truncation_is_reported(engine_factory, store, make_workflow):
    """输出超上限时如实标记截断，不假装看到了全部。

    超出上限的会话如果返回一段安静的截断文本，用户会以为那就是全部输出。
    所以要同时给出「被截断了」和「总共有多少」。
    """
    harness = FakeHarness(caps=_caps(interact=True))
    engine = await _engine_with(engine_factory, harness)
    _, session_ref = await _running(engine, store, make_workflow)

    limit = engine.scheduler.config.default_max_output_chars
    rt = engine.scheduler.runtime_for_session(session_ref)
    assert rt is not None
    rt.output = ["x" * (limit + 500)]
    rt.output_chars = limit + 500

    info = await engine.attach_session(session_ref)

    assert info["truncated"] is True
    assert info["total_chars"] == limit + 500
    assert len(info["output"]) == limit, "应当截到上限而不是原样返回"
