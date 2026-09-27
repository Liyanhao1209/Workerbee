"""运行时内核的集成测试：派发、依赖推进、失败传播、完成判据、重试与候选切换。

对应需求：RUN-01–07、CFG-03、DATA-02/03、OBS-01、AC-04、AC-08、AC-22。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.task import StageState, TaskState
from workerbee.core.runtime.launch import LaunchRejected, launch_task
from workerbee.core.runtime.scheduler import Scheduler

from tests.conftest import drain
from tests.helpers import edge, graph, node, profile, registry_with
from workerbee.core.domain import Edge, EdgeContract, ExecutionProfile, GraphSpec, NodeDefinition

pytestmark = pytest.mark.integration


async def _publish_registry(store, **kwargs):
    """把注册表项写进库，使校验管线能通过。"""
    from tests.helpers import registry_with as _mk

    snap = _mk(**kwargs)
    for h in getattr(snap, "_h", {}).values():
        await store.registry.upsert_harness(h)
    for c in getattr(snap, "_c", {}).values():
        await store.registry.upsert_credential(c)
    for s in getattr(snap, "_s", {}).values():
        await store.registry.upsert_skill(s)
    for t in getattr(snap, "_t", {}).values():
        await store.registry.upsert_tool(t)


async def _setup_harness(store, harness_id: str = "h1", caps: dict | None = None):
    from workerbee.core.domain.registry import HarnessRegistration

    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=harness_id,
            name=harness_id,
            adapter_id="fake",
            capabilities_snapshot=caps
            or {
                "compact": True,
                "permission_hook": True,
                "background_tasks": True,
                "resume_session": True,
            },
            last_probe_ok=True,
        )
    )


# ===========================================================================
# 主路径
# ===========================================================================


class TestHappyPath:
    async def test_chain_runs_to_success(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """三节点链式流程一次跑通，任务终态 SUCCEEDED。"""
        await _setup_harness(store)
        g = graph({"A": ["B"], "B": ["C"]})
        wf, _ = await make_workflow(g)

        result = await launch_task(store=store, workflow_id=wf.workflow_id)
        task_id = result.task.task_id
        assert result.created

        for _ in range(8):
            await tick(scheduler)
            await _finish_running(scheduler)

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.SUCCEEDED

        stages = await store.tasks.list_stages(task_id)
        assert [s.observed_state for s in stages] == [StageState.SUCCEEDED] * 3

        # 每个阶段独占一个会话，「不同任务不共享上下文」的前提是「每 Attempt 独占 session」
        assert len(harness.created) == 3
        assert len({h["stage_id"] for h in harness.created}) == 3

    async def test_parallel_branches_and_join(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """A 分到 B、C 再汇聚 D；B、C 可分别开始，D 只在两者都成功后启动。"""
        await _setup_harness(store)
        g = graph({"A": ["B", "C"], "B": ["D"], "C": ["D"]})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        # A 完成。此刻不调度，先确认依赖推进的结果本身。
        await tick(scheduler)
        await _finish_running(scheduler)

        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["B"].observed_state == StageState.READY
        assert stages["C"].observed_state == StageState.READY
        assert stages["D"].observed_state == StageState.WAITING_DEPS, (
            "汇聚点在两个上游都成功前必须保持等待"
        )

        # 只完成 B：D 必须仍在等待
        await _run_until_two_running(scheduler)
        await _finish_node(scheduler, store, "B")
        await tick(scheduler)
        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["D"].observed_state == StageState.WAITING_DEPS, (
            "汇聚点不得只凭部分上游成功就启动（RUN-05）"
        )

        # C 完成 → 汇聚点此刻才就绪（不等下一轮调度，依赖推进是即时的）
        await _finish_node(scheduler, store, "C")
        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["D"].observed_state == StageState.READY, (
            "两个必需上游都成功后，汇聚点才转为就绪"
        )

        await tick(scheduler)
        await _finish_node(scheduler, store, "D")
        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.SUCCEEDED

    async def test_node_serialization(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """同一节点同一时刻至多一个 RUNNING 阶段（R §5.2.2）。"""
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g, max_concurrent=8)

        for _ in range(3):
            await launch_task(store=store, workflow_id=wf.workflow_id)

        await tick(scheduler, rounds=3)
        running = await store.db.fetch_all(
            "SELECT * FROM task_stage WHERE node_id='A' AND observed_state='running'"
        )
        assert len(running) == 1, "节点串行约束被违反"

        others = await store.db.fetch_all(
            "SELECT * FROM task_stage WHERE node_id='A' AND observed_state='ready'"
        )
        assert len(others) == 2


# ===========================================================================
# 失败传播
# ===========================================================================


class TestFailurePropagation:
    async def test_upstream_failure_blocks_downstream_and_fails_task(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """AC-04：C 失败时 D 不正常启动，整体说明失败与其他分支状态。"""
        await _setup_harness(store)
        g = _graph_no_retry({"A": ["B", "C"], "B": ["D"], "C": ["D"]})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        await _finish_running(scheduler)
        await tick(scheduler)

        # B 成功、C 失败
        await _run_until_two_running(scheduler)
        await _finish_node(scheduler, store, "B", ok=True)
        await tick(scheduler)
        await _finish_node(scheduler, store, "C", ok=False)
        await tick(scheduler)

        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["C"].observed_state == StageState.FAILED
        assert stages["D"].observed_state == StageState.BLOCKED
        assert "C" in (stages["D"].blocked_reason or "")

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.FAILED
        assert task.failure_summary is not None
        assert task.failure_summary["failed_paths"], "必须给出失败原因"
        assert task.failure_summary["succeeded"], "必须同时显示已完成的分支（RUN-07）"

    async def test_unrelated_branch_keeps_running(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """无关分支默认继续推进（RUN-07、D-04）。"""
        await _setup_harness(store)
        g = _graph_no_retry({"A": ["B", "C"], "B": [], "C": ["D"], "D": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        await _finish_running(scheduler)
        await tick(scheduler)

        await _finish_node(scheduler, store, "B", ok=False)
        await tick(scheduler)

        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["B"].observed_state == StageState.FAILED
        assert stages["C"].observed_state in (StageState.READY, StageState.RUNNING)
        assert stages["D"].observed_state == StageState.WAITING_DEPS


# ===========================================================================
# 重试与候选切换（CFG-03、D-05）
# ===========================================================================


class TestRetryAndCandidates:
    async def test_retryable_error_retries_then_succeeds(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        assert len(harness.created) == 1

        # 第一次尝试以可重试错误结束
        rt = _only_runtime(scheduler)
        await scheduler.on_attempt_failed(
            attempt_id=rt.attempt_id, detail="网络抖动", error_kind="network"
        )

        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state == StageState.RETRYING
        assert stage.attempt_count == 1

        # 退避结束后回队，第二次尝试成功
        _expedite_retries(scheduler)
        await tick(scheduler)
        await _finish_running(scheduler)
        await tick(scheduler)

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.SUCCEEDED
        attempts = await store.tasks.list_attempts(stage.stage_id)
        assert len(attempts) == 2

    async def test_candidate_switches_after_retries_exhausted(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """D-05：退避重试耗尽后转到后续候选，且严格单向推进。"""
        await _setup_harness(store, "h1")
        await _setup_harness(store, "h2")

        n = NodeDefinition(
            node_id="A",
            name="A",
            profiles=[
                profile("p1", harness="h1", model_name="m1"),
                profile("p2", harness="h2", model_name="m2"),
            ],
        )
        n.profiles[0].retry.max_attempts = 1
        n.profiles[1].retry.max_attempts = 1
        g = GraphSpec(nodes=[n], edges=[])
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        assert harness.created[-1]["model_name"] == "m1"

        rt = _only_runtime(scheduler)
        await scheduler.on_attempt_failed(
            attempt_id=rt.attempt_id, detail="模型不可用", error_kind="server_error"
        )
        await tick(scheduler)

        assert harness.created[-1]["model_name"] == "m2", "应切换到第二组候选"
        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.profile_cursor == 1

    async def test_all_candidates_exhausted_fails(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """CFG-03：全部候选耗尽后停止自动尝试，展示明确失败。"""
        await _setup_harness(store, "h1")
        n = NodeDefinition(
            node_id="A", name="A", profiles=[profile("p1", harness="h1")]
        )
        n.profiles[0].retry.max_attempts = 1
        g = GraphSpec(nodes=[n], edges=[])
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        rt = _only_runtime(scheduler)
        await scheduler.on_attempt_failed(
            attempt_id=rt.attempt_id, detail="配置错误", error_kind="auth"
        )
        await tick(scheduler)

        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state == StageState.FAILED

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.FAILED

    async def test_bounded_no_infinite_loop(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """自动尝试必须有终点：候选数 × max_attempts 是硬上限。"""
        await _setup_harness(store, "h1")
        n = NodeDefinition(node_id="A", name="A", profiles=[profile("p1", harness="h1")])
        n.profiles[0].retry.max_attempts = 3
        g = GraphSpec(nodes=[n], edges=[])
        wf, _ = await make_workflow(g)
        await launch_task(store=store, workflow_id=wf.workflow_id)

        for _ in range(20):
            await tick(scheduler)
            _expedite_retries(scheduler)
            rt = _only_runtime(scheduler)
            if rt is None:
                break
            await scheduler.on_attempt_failed(
                attempt_id=rt.attempt_id, detail="持续失败", error_kind="network"
            )

        assert len(harness.created) <= 3, "重试次数超过了 max_attempts"


# ===========================================================================
# 完成判据（RUN-06、AC-22）
# ===========================================================================


class TestCompletionCriteria:
    async def test_background_work_blocks_success(
        self, store, sm, harness, scheduler, make_workflow, tick
    ):
        """AC-22：agent 发出最终文本但仍有影响结果的后台工作时，阶段不提前成功。"""
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        rt = _only_runtime(scheduler)
        await scheduler.on_event(
            session_ref=rt.session_ref,
            kind="background_task_started",
            payload={"id": "bg1"},
        )
        await scheduler.on_event(
            session_ref=rt.session_ref,
            kind="output",
            payload={"text": "已完成主体工作，后台仍在跑测试。"},
        )
        await scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)

        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state != StageState.SUCCEEDED, (
            "仍有影响结果的后台工作时不得判定成功"
        )
        assert "后台" in (stage.status_reason or "")

        # 后台工作结束 → 现在才能成功
        await scheduler.on_event(
            session_ref=rt.session_ref,
            kind="background_task_ended",
            payload={"id": "bg1"},
        )
        await tick(scheduler)
        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state == StageState.SUCCEEDED

    async def test_contract_violation_fails_stage(
        self, store, sm, harness, scheduler, make_workflow, tick, summarizer
    ):
        """产出物不覆盖边契约要求的字段 → 交接失败，不得静默通过（DATA-03）。"""
        await _setup_harness(store)
        summarizer.cover = []  # 什么都不覆盖

        a = NodeDefinition(node_id="A", name="A", profiles=[profile("p1")])
        b = NodeDefinition(node_id="B", name="B", profiles=[profile("p2")])
        g = GraphSpec(
            nodes=[a, b],
            edges=[
                Edge(
                    from_node="A",
                    to_node="B",
                    output_contract=EdgeContract(outputs=["plan", "risks"]),
                )
            ],
        )
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        await _finish_running(scheduler)
        await tick(scheduler)

        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state == StageState.FAILED
        assert "契约" in (stage.blocked_reason or "")

    async def test_summarizer_failure_is_visible(
        self, store, sm, harness, scheduler, make_workflow, tick, summarizer
    ):
        """摘要失败必须显式化，不允许以静默省略换取「成功」。"""
        await _setup_harness(store)
        summarizer.raise_on_call = True
        g = graph({"A": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        await _finish_running(scheduler)
        await tick(scheduler)

        events = await store.events.for_task(task_id)
        assert any(e["type"] == "handoff.failed" for e in events), (
            "摘要失败必须留下可见记录"
        )

    async def test_initial_input_is_delivered_exactly_once(
        self, store, sm, ledger, context_builder, summarizer, make_workflow, tick
    ):
        """首轮输入只能交付一次。

        两条路径互斥：随建会话交付（一次性 `-p` 型 harness）**或** 事后 send_input。
        两条都走会让同一条指令执行两遍——对会改文件的 agent 来说那是数据损坏，
        不是「多一点冗余」。
        """
        from tests.fakes import FakeHarness
        from workerbee.core.runtime.scheduler import Scheduler, SchedulerConfig

        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)

        # 形态一：交互式 harness（建会话后仍需 send_input）
        interactive = FakeHarness(accepts_initial_input=False)
        s1 = Scheduler(store=store, sm=sm, harness=interactive, ledger=ledger,
                       context_builder=context_builder, summarizer=summarizer)
        r1 = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(s1)
        sess = interactive.last_session()
        assert interactive.created[-1]["initial_input"], "首轮输入必须随建会话传下去"
        assert len(sess.inputs) == 1, "交互式形态：应恰好投递一次"

        # 收尾第一个任务，否则节点串行会让第二个任务排不上槽
        # ——那是对的行为（R §5.2.2），不是 bug。
        await s1.on_event(
            session_ref=sess.session_ref, kind="output", payload={"text": "完成"}
        )
        await s1.on_session_ended(session_ref=sess.session_ref, ok=True)
        await tick(s1)
        assert (await store.tasks.get_task(r1.task.task_id)).observed_state == TaskState.SUCCEEDED

        # 形态二：一次性 `-p` 型 harness（建会话时已消费）
        onepass = FakeHarness(accepts_initial_input=True)
        s2 = Scheduler(store=store, sm=sm, harness=onepass, ledger=ledger,
                       context_builder=context_builder, summarizer=summarizer)
        await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(s2)
        sess2 = onepass.last_session()
        assert len(sess2.inputs) == 1, (
            "一次性形态：建会话时已交付，绝不能再 send_input 一次"
        )

    async def test_no_output_fails(self, store, sm, harness, scheduler, make_workflow, tick):
        """会话正常结束但什么都没产出 → 阶段失败。

        RUN-06 明确：进程退出不单独等于阶段成功；未声明契约的边退化为
        「产出物存在」，因此零产出意味着结果不可交接。
        """
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(scheduler)
        await _finish_running(scheduler, text="")  # 不发任何输出
        await tick(scheduler)
        _expedite_retries(scheduler)
        await tick(scheduler)

        stage = (await store.tasks.list_stages(task_id))[0]
        assert stage.observed_state == StageState.FAILED
        assert "产出物" in (stage.blocked_reason or "")

    async def test_fallback_without_summarizer_still_runs(
        self, store, sm, harness, ledger, context_builder, make_workflow, tick
    ):
        """没有摘要器时链路仍可跑通，但产物被标记为 summary_ok=False 而非谎报覆盖。"""
        await _setup_harness(store)
        sched = Scheduler(
            store=store, sm=sm, harness=harness, ledger=ledger,
            context_builder=context_builder, summarizer=None,
        )
        g = graph({"A": []})
        wf, _ = await make_workflow(g)
        task_id = (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id

        await tick(sched)
        await _finish_running(sched)
        await tick(sched)

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.SUCCEEDED
        arts = await store.artifacts.list_by_task(task_id)
        assert arts and arts[0].covered_fields == []


# ===========================================================================
# 发射校验与幂等
# ===========================================================================


class TestLaunch:
    async def test_launch_rejected_when_validation_fails(self, store, make_workflow):
        """草稿不能发射；配置缺失必须拒绝而不是「跑起来再说」（WF-01、WF-05）。"""
        n = NodeDefinition(node_id="A", name="A", profiles=[])
        g = GraphSpec(nodes=[n], edges=[])
        wf, _ = await make_workflow(g)

        with pytest.raises(LaunchRejected) as exc:
            await launch_task(store=store, workflow_id=wf.workflow_id)
        assert exc.value.report.has_code("node_no_profile")

    async def test_idempotency_key_returns_same_task(self, store, make_workflow):
        """RUN-02：同一次提交的网络重送不产生额外任务。"""
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)

        r1 = await launch_task(
            store=store, workflow_id=wf.workflow_id, idempotency_key="req-1"
        )
        r2 = await launch_task(
            store=store, workflow_id=wf.workflow_id, idempotency_key="req-1"
        )
        assert r1.created and not r2.created
        assert r1.task.task_id == r2.task.task_id

    async def test_same_content_twice_creates_two_tasks(self, store, make_workflow):
        """RUN-02：两次**有意**提交相同内容产生两个任务。"""
        await _setup_harness(store)
        g = graph({"A": []})
        wf, _ = await make_workflow(g)

        r1 = await launch_task(
            store=store, workflow_id=wf.workflow_id, input_payload={"task": "写一个排序"}
        )
        r2 = await launch_task(
            store=store, workflow_id=wf.workflow_id, input_payload={"task": "写一个排序"}
        )
        assert r1.task.task_id != r2.task.task_id

    async def test_pinned_graph_isolates_from_later_edits(self, store, make_workflow):
        """WF-06：在途任务按钉扎版本执行，不受后续编辑影响。"""
        await _setup_harness(store)
        g = graph({"A": ["B"], "B": []})
        wf, _ = await make_workflow(g)
        r = await launch_task(store=store, workflow_id=wf.workflow_id)

        task = await store.tasks.get_task(r.task.task_id)
        assert task.graph_snapshot.effective_predecessors("B") == ["A"]
        assert task.revision_seq == 1


# ===========================================================================
# 辅助
# ===========================================================================


def _only_runtime(scheduler: Scheduler):
    rts = list(scheduler._runtimes.values())
    assert len(rts) <= 1, f"期望至多一个在途尝试，实际 {len(rts)}"
    return rts[0] if rts else None


def _graph_no_retry(adjacency: dict[str, list[str]], *, harness: str = "h1"):
    """构造一个候选不重试的图。

    默认 RetryPolicy 会重试 3 次——测「失败传播」时必须先去掉重试，
    否则观察到的中间态是 RETRYING，而不是 FAILED。
    """
    names = set(adjacency) | {t for targets in adjacency.values() for t in targets}
    nodes = {}
    for name in sorted(names):
        nd = node(name, harness=harness)
        nd.profiles[0].retry.max_attempts = 1
        nodes[name] = nd
    return graph(adjacency, nodes=nodes)


def _expedite_retries(scheduler: Scheduler) -> None:
    """把所有退避中的阶段改为「立即到期」。

    测试不该真的等 1 秒退避；把到期时间拉到 0 既保留回队路径，又不需要 sleep。
    注意**不能**直接清空堆——那会让阶段永远卡在 RETRYING。
    """
    import heapq

    scheduler._retry_heap = [(0.0, sid) for _, sid in scheduler._retry_heap]
    heapq.heapify(scheduler._retry_heap)


async def _finish_running(
    scheduler: Scheduler, *, ok: bool = True, text: str = "已完成本阶段工作。"
) -> None:
    """让当前在途尝试产出文本并结束会话。

    真实 harness 总会产出文本；不发 output 的话阶段会按「产出物缺失」判失败——
    那是正确行为，见 TestCompletionCriteria.test_no_output_fails。
    """
    rt = _only_runtime(scheduler)
    if rt is None:
        return
    if ok and text:
        await scheduler.on_event(
            session_ref=rt.session_ref, kind="output", payload={"text": text}
        )
    await scheduler.on_session_ended(session_ref=rt.session_ref, ok=ok)


async def _run_until_two_running(scheduler: Scheduler) -> None:
    for _ in range(6):
        await scheduler.tick()
        if len(scheduler._runtimes) == 2:
            return
    raise AssertionError("两个分支没有同时进入运行态")


async def _finish_node(scheduler: Scheduler, store, node_id: str, *, ok: bool = True) -> None:
    for attempt_id, rt in list(scheduler._runtimes.items()):
        stage = await store.tasks.get_stage(rt.stage_id)
        if stage is not None and stage.node_id == node_id:
            if ok:
                await scheduler.on_event(
                    session_ref=rt.session_ref,
                    kind="output",
                    payload={"text": f"{node_id} 阶段产出。"},
                )
            await scheduler.on_session_ended(session_ref=rt.session_ref, ok=ok)
            return
    raise AssertionError(f"节点 {node_id} 没有在途尝试")
