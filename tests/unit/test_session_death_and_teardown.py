"""会话死亡通知与适配层清理注入的回归测试。

这批测试对应两条**实测出来的静默失效**——两条的共同点是：日志干净、界面正常，
只有「任务永远挂着」这一个外部症状能暴露它们。

1. ``Supervisor._notify`` 是裸循环广播：一个写不进去的订阅者抛异常，后面所有
   订阅者都收不到这条通知。而心跳循环把整段包在 ``suppress`` 里，于是
   「已标记 lost」落了库、通知却没了。``alive_sessions()`` 此后不再返回该会话，
   它永远不会被复查——core 永远收不到这次死亡，阶段停在 running。
   core 被强杀时死 writer 会一直留在订阅集合里，所以这条路径不是理论上的。

2. ``Engine`` 构造 ``ResourceLedger`` 时没有注入 ``harness_teardown``
   （只有测试传了）。后果是每一次会话清理都失败，资源停在 teardown_failed，
   Reaper 每 5 分钟重试、每轮失败，错误永久挂在「需处理」里。而失败的资源
   属于**已经成功完成**的阶段——报错和真正出问题的地方不在一处，所以特别难反推。

3. ``session_alive`` 查不到结果时返回 False（见 ``SupervisorClient.session_alive``），
   所以单次否定不足以判定死亡。存活巡检必须连续两次否定才收束，否则会把
   健康阶段误杀——那是比漏报更糟的故障。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from workerbee.core.runtime.scheduler import AttemptRuntime, Scheduler
from workerbee.supervisor.server import PROBE_TIMEOUT_SECONDS, Supervisor
from workerbee.supervisor.protocol import NOTIFICATIONS

pytestmark = pytest.mark.unit


# ===========================================================================
# 1. 广播不能被子一个坏订阅者打断
# ===========================================================================


class _Writer:
    """够用的 StreamWriter 替身：写入成功或抛指定异常。"""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.frames: list[dict] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _supervisor(tmp_path) -> Supervisor:
    """只构造、不启动：以下几条测的是单个协程的行为。"""
    sup = Supervisor(
        data_dir=tmp_path / "sup",
        socket_path=tmp_path / "sup" / "supervisor.sock",
        adapter_commands={},
    )
    sup._subscribers.clear()
    sup._write_locks.clear()
    return sup


def _route(sup: Supervisor, writers: list[_Writer]) -> None:
    """把 _send 换成按 writer 写入或抛错。"""

    async def _send(writer: _Writer, frame: dict) -> None:
        if writer.broken:
            raise ConnectionResetError("对面已经没了")
        writer.frames.append(frame)

    sup._send = _send  # type: ignore[method-assign]
    for w in writers:
        sup._subscribers.add(w)  # type: ignore[arg-type]
        sup._write_locks[id(w)] = asyncio.Lock()


async def test_broken_subscriber_does_not_block_the_others(tmp_path):
    """坏连接在前、好连接在后——好连接**必须**收到。

    这条曾经是坏的：裸循环遇到第一个 writer 抛异常就退出，排在后面的订阅者
    永远收不到。core 是唯一的订阅者，它一旦排在坏连接后面，所有会话死亡通知
    都会静默丢失。
    """
    sup = _supervisor(tmp_path)
    broken, good = _Writer(broken=True), _Writer()
    _route(sup, [broken, good])

    await sup._notify(NOTIFICATIONS.SESSION_DIED, {"session_ref": "s1"})

    assert len(good.frames) == 1, "坏连接把好连接的通知吃掉了"
    assert good.frames[0]["params"]["session_ref"] == "s1"


async def test_broken_subscriber_is_dropped(tmp_path):
    """坏连接当场摘除，不指望读循环来收尸。"""
    sup = _supervisor(tmp_path)
    broken, good = _Writer(broken=True), _Writer()
    _route(sup, [broken, good])

    await sup._notify(NOTIFICATIONS.SESSION_DIED, {"session_ref": "s1"})

    assert broken not in sup._subscribers, "坏连接留在了订阅集合里，每轮都要再失败一次"
    assert id(broken) not in sup._write_locks, "写锁没跟着一起清，会缓慢泄漏"
    assert broken.closed
    assert good in sup._subscribers


async def test_notify_survives_every_subscriber_being_broken(tmp_path):
    """全坏也不能抛出去——调用方（心跳循环）不该因为广播失败而中断。"""
    sup = _supervisor(tmp_path)
    _route(sup, [_Writer(broken=True), _Writer(broken=True)])

    await sup._notify(NOTIFICATIONS.SESSION_DIED, {"session_ref": "s1"})

    assert not sup._subscribers


# ===========================================================================
# 2. 标记丢失与通知必须成对发生
# ===========================================================================


class _FakeLedger:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []

    async def set_state(self, session_ref: str, state: str) -> None:
        self.states.append((session_ref, state))


async def test_mark_lost_marks_and_notifies(tmp_path):
    """两个动作必须都发生。只做一个都会让阶段永远挂着。"""
    sup = _supervisor(tmp_path)
    sup.ledger = _FakeLedger()  # type: ignore[assignment]
    good = _Writer()
    _route(sup, [good])

    await sup._mark_session_lost("s1", "心跳缺失")

    assert sup.ledger.states == [("s1", "lost")], "没有落库，会话会被反复复查"
    assert len(good.frames) == 1, "没有通知，core 不知道会话死了"
    assert good.frames[0]["method"] == NOTIFICATIONS.SESSION_DIED
    assert good.frames[0]["params"]["reason"] == "心跳缺失"


async def test_mark_lost_survives_a_failing_broadcast(tmp_path, capsys):
    """广播炸了也不能把状态标记回滚，更不能把异常抛给心跳循环。

    状态已经改了、通知没发出去——这是最坏的情况，但至少要在 stderr 留痕，
    不能像以前那样被 suppress 吞成完全静默。
    """
    sup = _supervisor(tmp_path)
    sup.ledger = _FakeLedger()  # type: ignore[assignment]

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("广播炸了")

    sup._notify = _boom  # type: ignore[method-assign]

    await sup._mark_session_lost("s1", "心跳缺失")  # 不应抛出

    assert sup.ledger.states == [("s1", "lost")]
    assert "s1" in capsys.readouterr().err, "通知失败没有留下任何痕迹"


# ===========================================================================
# 3. 探测必须有超时
# ===========================================================================


async def test_probe_times_out_instead_of_hanging(tmp_path):
    """一个卡住的探测不能把整个心跳循环钉死。

    不带超时时，某次 session_alive 挂住会让循环永远停在那一个 await 上，
    此后**所有**会话的心跳与死亡通知一起停摆——故障从一个会话扩散到全部。
    """
    sup = _supervisor(tmp_path)

    async def _hang(_session_ref: str) -> bool:
        await asyncio.sleep(3600)
        return True  # pragma: no cover - 不会走到

    sup.harness = type("H", (), {"session_alive": staticmethod(_hang)})()  # type: ignore[assignment]

    started = asyncio.get_running_loop().time()
    assert await sup._probe_alive("s1") is False
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < PROBE_TIMEOUT_SECONDS + 1.0, f"探测没有被超时截断（耗时 {elapsed:.1f}s）"


async def test_probe_reports_failure_as_not_alive(tmp_path):
    """探测抛异常按「不活着」处理，由调用方决定标记与通知。"""
    sup = _supervisor(tmp_path)

    async def _boom(_session_ref: str) -> bool:
        raise RuntimeError("适配器忙")

    sup.harness = type("H", (), {"session_alive": staticmethod(_boom)})()  # type: ignore[assignment]
    assert await sup._probe_alive("s1") is False


# ===========================================================================
# 4. 存活巡检：连续两次否定才收束
# ===========================================================================


class _AliveHarness:
    """可控存活的假 harness，同时记录被探测过几次。"""

    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.probes = 0

    async def session_alive(self, _session_ref: str) -> bool:
        self.probes += 1
        return self.alive


def _runtime(scheduler: Scheduler, session_ref: str = "sess-1") -> AttemptRuntime:
    rt = AttemptRuntime(attempt_id="at-1", stage_id="st-1", task_id="tk-1", node_id="N")
    rt.session_ref = session_ref
    scheduler._runtimes["at-1"] = rt
    return rt


def _scheduler_with(harness: Any, base: Scheduler) -> Scheduler:
    base.harness = harness
    return base


async def test_single_negative_probe_does_not_kill_the_stage(scheduler):
    """一次否定不算数——``session_alive`` 查不到就返回 False，抖动很常见。"""
    h = _AliveHarness(alive=False)
    sched = _scheduler_with(h, scheduler)
    rt = _runtime(sched)

    await sched._sweep_dead_sessions()

    assert rt.session_ended is False, "单次否定就判死，会把健康阶段误杀"
    assert sched._liveness_misses["at-1"] == 1


async def test_positive_probe_clears_the_miss_counter(scheduler):
    """中间活过来一次，计数必须清零，不能累积成「连续两次」。"""
    h = _AliveHarness(alive=False)
    sched = _scheduler_with(h, scheduler)
    _runtime(sched)

    await sched._sweep_dead_sessions()
    h.alive = True
    await sched._sweep_dead_sessions()

    assert sched._liveness_misses.get("at-1") is None, "计数器没被清零"
    h.alive = False
    await sched._sweep_dead_sessions()
    assert sched._liveness_misses["at-1"] == 1, "清零后应重新从 1 开始"


async def test_two_consecutive_negatives_settle_the_stage(scheduler):
    """连续两次否定才收束：置 session_ended 并交回完成判据。"""
    h = _AliveHarness(alive=False)
    sched = _scheduler_with(h, scheduler)
    rt = _runtime(sched)

    await sched._sweep_dead_sessions()
    await sched._sweep_dead_sessions()

    assert rt.session_ended is True
    assert rt.ended_ok is False
    assert rt.error_detail, "收束要留下原因，否则界面只显示「异常结束」"
    assert "at-1" not in sched._liveness_misses


async def test_sweep_skips_sessions_that_already_ended(scheduler):
    """已经结束的运行时不重复处理——否则会对着死会话反复发探测。"""
    h = _AliveHarness(alive=False)
    sched = _scheduler_with(h, scheduler)
    rt = _runtime(sched)
    rt.session_ended = True

    await sched._sweep_dead_sessions()

    assert h.probes == 0


# ===========================================================================
# 5. 清理钩子必须被装配
# ===========================================================================


async def test_engine_wires_harness_teardown(engine_factory):
    """``ResourceLedger`` 的 harness_teardown 必须由组合根注入。

    漏掉这条装配不会有任何报错：每一次会话清理都失败，资源停在
    teardown_failed，Reaper 每轮重试每轮失败。阶段本身却是成功的——
    所以症状是「已完成的阶段一直报会话关不掉」。
    """
    engine = await engine_factory()

    assert engine.ledger.harness_teardown is not None, (
        "清理钩子没有装配，会话类资源永远关不掉"
    )


async def test_teardown_reports_missing_session_ref(engine_factory):
    """台账里没有 session_ref 时如实报错，不猜、不静默成功。"""
    engine = await engine_factory()

    ok, err = await engine._teardown_harness_session({})

    assert ok is False
    assert err and "session_ref" in err


async def test_teardown_disposes_and_confirms(engine_factory):
    """正常路径：调 dispose，并复核会话确实不在了。

    用自带存活状态的替身而不是 ``engine_factory`` 装的真 HarnessRouter——
    这里测的是「关掉了没有」的判定逻辑，不该依赖真实适配层的会话管理。
    """

    class _Obedient:
        def __init__(self) -> None:
            self.disposed: list[str] = []
            self.alive = True

        async def dispose(self, session_ref: str) -> None:
            self.disposed.append(session_ref)
            self.alive = False  # 真的关掉了

        async def session_alive(self, _session_ref: str) -> bool:
            return self.alive

    engine = await engine_factory()
    engine.harness = _Obedient()

    ok, err = await engine._teardown_harness_session({"session_ref": "sess-1"})

    assert ok is True, f"应当成功：{err}"
    assert err is None
    assert engine.harness.disposed == ["sess-1"]


async def test_teardown_fails_when_session_survives_dispose(engine_factory):
    """dispose 没抛异常但会话还活着，必须判失败。

    ``HarnessRouter.dispose`` 把适配器异常吞成日志后照常返回，所以
    「没抛异常」不能当成「已关闭」——否则台账会开始说谎，把一个仍然活着的
    会话记成已释放。
    """

    class _Stubborn:
        def __init__(self) -> None:
            self.disposed: list[str] = []

        async def dispose(self, session_ref: str) -> None:
            self.disposed.append(session_ref)  # 收下请求，但什么都不做

        async def session_alive(self, _session_ref: str) -> bool:
            return True  # 一直活着

    engine = await engine_factory()
    engine.harness = _Stubborn()

    ok, err = await engine._teardown_harness_session({"session_ref": "sess-1"})

    assert ok is False
    assert err and "仍然存活" in err
    assert engine.harness.disposed == ["sess-1"]


async def test_teardown_reports_dispose_exception(engine_factory):
    """dispose 抛异常要变成可读结果，而不是穿到 Reaper 里。"""

    class _Exploding:
        async def dispose(self, _session_ref: str) -> None:
            raise RuntimeError("适配器连接断了")

        async def session_alive(self, _session_ref: str) -> bool:
            return False

    engine = await engine_factory()
    engine.harness = _Exploding()

    ok, err = await engine._teardown_harness_session({"session_ref": "sess-1"})

    assert ok is False
    assert err and "适配器连接断了" in err
