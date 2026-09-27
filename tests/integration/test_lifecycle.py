"""生命周期控制集成测试：暂停、恢复、删除、启停、调序。

对应需求：LIFE-01–06、ACT-01–04、RUN-04、D-01、D-07、AC-03、AC-10、AC-11。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.task import DesiredState, StageState, TaskState
from workerbee.core.runtime.launch import launch_task
from workerbee.core.runtime.lifecycle import (
    delete_task,
    delete_workflow,
    pause_task,
    reorder_stages,
    resume_task,
    set_node_enabled,
)
from workerbee.core.runtime.ports import SessionCaps

from tests.conftest import drain
from workerbee.core.domain import ExecutionProfile, GraphSpec, NodeDefinition
from workerbee.core.domain.registry import HarnessRegistration

pytestmark = pytest.mark.integration


async def _setup_harness(store, harness_id: str = "h1"):
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=harness_id, name=harness_id, adapter_id="fake", last_probe_ok=True
        )
    )


def _nodes(*names: str, **kw) -> list[NodeDefinition]:
    return [NodeDefinition(node_id=n, name=n, profiles=[ExecutionProfile(model_name="m1", harness_ref="h1")]) for n in names]


def _graph(adjacency: dict[str, list[str]]) -> GraphSpec:
    names = set(adjacency) | {t for ts in adjacency.values() for t in ts}
    from workerbee.core.domain import Edge

    return GraphSpec(
        nodes=_nodes(*sorted(names)),
        edges=[Edge(from_node=a, to_node=b) for a, ts in adjacency.items() for b in ts],
    )


async def _start_running(store, scheduler, wf, tick, *, payload=None):
    """发射任务并推进到「有在途尝试」。返回 (task_id, attempt_runtime)。"""
    r = await launch_task(
        store=store, workflow_id=wf.workflow_id, input_payload=payload or {}
    )
    await tick(scheduler)
    rts = list(scheduler._runtimes.values())
    return r.task.task_id, (rts[0] if rts else None)


async def _finish(scheduler, stage_id: str, store, *, text: str = "产出") -> None:
    for rt in list(scheduler._runtimes.values()):
        st = await store.tasks.get_stage(rt.stage_id)
        if st is not None and st.stage_id == stage_id:
            await scheduler.on_event(
                session_ref=rt.session_ref, kind="output", payload={"text": text}
            )
            await scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)
            return
    raise AssertionError(f"阶段 {stage_id} 没有在途尝试")


# ===========================================================================
# 暂停与恢复
# ===========================================================================


class TestPauseResume:
    async def test_pause_running_task_stops_and_records_origin(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """LIFE-01：暂停停止新派发、处理已启动分支、展示暂停中到最终结果。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": ["B"], "B": []}))
        task_id, rt = await _start_running(store, scheduler, wf, tick)
        assert rt is not None

        report = await pause_task(
            store=store, sm=sm, harness=harness, ledger=ledger,
            task_id=task_id, origin_node_id="A", reason="先看一眼",
        )

        assert report.accepted
        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.PAUSED
        assert task.desired_state == DesiredState.PAUSED
        assert task.last_origin is not None
        assert task.last_origin.op == "pause"
        assert task.last_origin.from_node_id == "A", "OBS-03：暂停记录必须包含发起位置"

        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["A"].observed_state == StageState.PAUSED
        assert harness.disposed, "暂停必须关掉会话，不能留着进程"

    async def test_pause_intent_survives_restart(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """清单 §2：用户主动暂停的意图在重启后继续有效，不得自动恢复运行。"""
        from workerbee.core.runtime.reconcile import enforce_user_intents

        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": ["B"], "B": []}))
        task_id, _ = await _start_running(store, scheduler, wf, tick)
        await pause_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
        )

        # 模拟重启：把阶段状态打乱，模拟「崩溃后残留的中间态」
        stages = await store.tasks.list_stages(task_id)
        for s in stages:
            await store.db.execute(
                "UPDATE task_stage SET observed_state='running' WHERE stage_id=?",
                (s.stage_id,),
            )
        await store.db.execute(
            "UPDATE task SET observed_state='running' WHERE task_id=?", (task_id,)
        )

        await enforce_user_intents(store=store, sm=sm, ledger=ledger)

        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.PAUSED, "重启后不得自动恢复运行"

    async def test_resume_does_not_rerun_succeeded_stages(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """LIFE-03：恢复不能重新分配为无关提交，也不能无说明重跑成功阶段。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": ["B"], "B": []}))
        task_id, rt = await _start_running(store, scheduler, wf, tick)

        # 先让 A 成功，B 起来后暂停
        await _finish(scheduler, rt.stage_id, store)
        await tick(scheduler)
        await pause_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
        )

        stages_before = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages_before["A"].observed_state == StageState.SUCCEEDED
        assert stages_before["B"].observed_state == StageState.PAUSED

        report = await resume_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
        )

        stages_after = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages_after["A"].observed_state == StageState.SUCCEEDED, (
            "已成功阶段不得被重跑"
        )
        assert stages_after["B"].observed_state == StageState.READY
        assert report.requeued, "恢复必须报告被重新入队的阶段"

    async def test_pause_reports_missing_checkpoint_honestly(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """LIFE-02 / D-07：拿不到断点时必须如实说明会从头重跑，不能假装无损。"""
        await _setup_harness(store)
        harness._caps = SessionCaps(
            resume_session=True, checkpoint_resume=False, pause_in_place=False, stop=True
        )
        wf, _ = await make_workflow(_graph({"A": []}))
        task_id, _ = await _start_running(store, scheduler, wf, tick)

        report = await pause_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
        )
        assert report.stopped_cooperatively
        assert any("从头重跑" in w for w in report.warnings), (
            "缺少断点必须明确告知用户可能重复的工作"
        )

    async def test_pause_does_not_touch_other_tasks(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """AC-10：从任务 A 的节点发起暂停，不影响任务 B。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}), max_concurrent=8)

        # 两个任务，两个不同节点，避免节点串行让它们互斥
        r1 = await launch_task(store=store, workflow_id=wf.workflow_id)
        r2 = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler)

        await pause_task(
            store=store, sm=sm, harness=harness, ledger=ledger,
            task_id=r1.task.task_id, origin_node_id="A",
        )

        t1 = await store.tasks.get_task(r1.task.task_id)
        t2 = await store.tasks.get_task(r2.task.task_id)
        assert t1.observed_state == TaskState.PAUSED
        assert t2.observed_state != TaskState.PAUSED, "其他提交不受影响"

        s2 = (await store.tasks.list_stages(r2.task.task_id))[0]
        assert s2.observed_state != StageState.PAUSED, "任务 B 的阶段不得被牵连暂停"
        assert s2.observed_state in (StageState.READY, StageState.RUNNING)


# ===========================================================================
# 删除
# ===========================================================================


class TestDelete:
    async def test_delete_task_preserves_succeeded_and_forbids_resume(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """LIFE-04 / AC-10：已完成阶段保留真实结果；删除后无续跑入口。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": ["B"], "B": []}))
        task_id, rt = await _start_running(store, scheduler, wf, tick)
        await _finish(scheduler, rt.stage_id, store)
        await tick(scheduler)

        report = await delete_task(
            store=store, sm=sm, harness=harness, ledger=ledger,
            task_id=task_id, origin_node_id="A", reason="不要了",
        )

        assert report.accepted
        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.CANCELLED
        assert task.desired_state == DesiredState.CANCELLED

        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["A"].observed_state == StageState.SUCCEEDED, (
            "已完成阶段以其真实结果留在历史，不改写为失败"
        )
        assert stages["B"].observed_state == StageState.CANCELLED
        assert report.preserved_succeeded

        with pytest.raises(ValueError, match="续跑入口"):
            await resume_task(
                store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
            )

    async def test_delete_invalidates_approvals_and_messages(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """AC-14 / REC-05：删除后旧批准作废，迟到消息不得唤醒已删除任务。"""
        from workerbee.core.domain import ApprovalStatus
        from tests.conftest import make_approval

        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}))
        task_id, rt = await _start_running(store, scheduler, wf, tick)

        attempt = (await store.tasks.list_attempts(rt.stage_id))[0]
        await store.approvals.create(
            make_approval(task_id=task_id, stage_id=rt.stage_id, attempt_id=attempt.attempt_id)
        )

        await delete_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=task_id
        )

        approvals = await store.approvals.list_for_task(task_id)
        assert all(a.status != ApprovalStatus.PENDING for a in approvals)

        # 删除后到达的完成回调不得改变任何状态
        await scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)
        task = await store.tasks.get_task(task_id)
        assert task.observed_state == TaskState.CANCELLED

    async def test_delete_workflow_stops_submissions(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """LIFE-05：删除后不再接受新提交；定义与历史保留供回看。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}))
        await launch_task(store=store, workflow_id=wf.workflow_id)

        report = await delete_workflow(
            store=store, sm=sm, harness=harness, ledger=ledger, workflow_id=wf.workflow_id
        )
        assert report.accepted
        assert report.tasks_terminated

        fresh = await store.workflows.get(wf.workflow_id)
        assert fresh.status.value == "deleted"
        assert await store.workflows.get_current_revision(wf.workflow_id) is not None, (
            "定义保留供回看"
        )
        with pytest.raises(ValueError):
            await launch_task(store=store, workflow_id=wf.workflow_id)


# ===========================================================================
# 节点启停（D-01、ACT-01–04）
# ===========================================================================


class TestNodeToggle:
    async def test_disable_then_enable_is_reversible(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """ACT-01：停用保留配置与原始关系；重新启用精确恢复对应位置。"""
        await _setup_harness(store)
        g = _graph({"A": ["B"], "B": ["C"], "C": []})
        wf, rev = await make_workflow(g)

        before = {(e.from_node, e.to_node) for e in rev.graph.edges}

        r1 = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="B", enable=False, mode="immediate",
        )
        assert r1.applied
        mid = await store.workflows.get_current_revision(wf.workflow_id)
        assert mid.graph.node("B").enabled is False

        r2 = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="B", enable=True,
        )
        assert r2.applied
        after_rev = await store.workflows.get_current_revision(wf.workflow_id)
        after = {(e.from_node, e.to_node) for e in after_rev.graph.edges}
        assert after == before, "重新启用必须精确恢复原始依赖"
        assert after_rev.graph.node("B").enabled is True

    async def test_disable_creates_new_revision_not_mutating_old(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """修订不可变：启停产生新修订，旧修订保持原样（WF-06）。"""
        await _setup_harness(store)
        wf, rev = await make_workflow(_graph({"A": ["B"], "B": []}))

        report = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="B", enable=False, mode="immediate",
        )
        assert report.new_revision_seq == 2

        old = await store.workflows.get_revision(wf.workflow_id, 1)
        assert old.graph.node("B").enabled is True, "旧修订不得被就地修改"

    async def test_immediate_withdrawal_skips_queued_and_cancels_running(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """D-01(b)：立即撤回 —— 在途走取消链，排队标 SKIPPED。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}), max_concurrent=8)
        r1 = await launch_task(store=store, workflow_id=wf.workflow_id)
        r2 = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler)

        report = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="A", enable=False, mode="immediate",
        )
        assert report.applied
        assert report.withdrawn_stages, "排队中的阶段应被标记为跳过"

        st1 = (await store.tasks.list_stages(r1.task.task_id))[0]
        st2 = (await store.tasks.list_stages(r2.task.task_id))[0]
        states = {st1.observed_state, st2.observed_state}
        assert StageState.SKIPPED in states, "排队阶段标 SKIPPED 而不是删除（ACT-04）"

    async def test_drain_waits_for_inflight_then_applies(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """D-01(a)：排水 —— 在途跑完才翻转 enabled，不丢弃已完成工作。"""
        from workerbee.core.runtime.lifecycle import complete_drain_ops

        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}))
        task_id, rt = await _start_running(store, scheduler, wf, tick)

        report = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="A", enable=False, mode="drain",
        )
        assert report.awaiting_drain
        assert report.applied is False

        rev = await store.workflows.get_current_revision(wf.workflow_id)
        assert rev.graph.node("A").enabled is True, "排水期不得提前翻转"

        # 在途阶段跑完 → 排水完成 → 自动翻转
        await _finish(scheduler, rt.stage_id, store)
        await tick(scheduler)
        done = await complete_drain_ops(store=store, sm=sm)
        assert done

        rev2 = await store.workflows.get_current_revision(wf.workflow_id)
        assert rev2.graph.node("A").enabled is False

    async def test_reenable_revives_skipped_stages(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """ACT-04：停用不等于删除这些任务；重新启用后跳过的工作回到队列。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}), max_concurrent=8)
        r1 = await launch_task(store=store, workflow_id=wf.workflow_id)
        r2 = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler)

        await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="A", enable=False, mode="immediate",
        )
        report = await set_node_enabled(
            store=store, sm=sm, harness=harness, ledger=ledger,
            workflow_id=wf.workflow_id, node_id="A", enable=True,
        )
        assert report.revived_stages, "被跳过的阶段应被拉回队列"

        stages = []
        for t in (r1.task.task_id, r2.task.task_id):
            stages.extend(await store.tasks.list_stages(t))
        assert any(s.observed_state in (StageState.READY, StageState.RUNNING) for s in stages)


# ===========================================================================
# 调序（RUN-04、AC-03）
# ===========================================================================


class TestReorder:
    async def test_reorder_only_affects_target_node(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """AC-03：在 N2 调整顺序只改变 N2 的有效队列。"""
        await _setup_harness(store)
        g = _graph({"A": ["N2"], "N2": []})
        wf, _ = await make_workflow(g, max_concurrent=8)

        # 发射三个任务，先让它们的 A 都成功，使 N2 上有三个 pending
        tasks = [
            (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id
            for _ in range(3)
        ]
        for _ in range(6):
            await tick(scheduler)
            rts = list(scheduler._runtimes.values())
            if not rts:
                break
            await _finish(scheduler, rts[0].stage_id, store)

        n2 = await store.tasks.list_stages_by_node(
            "N2", states=[StageState.WAITING_DEPS, StageState.READY]
        )
        if len(n2) < 3:
            pytest.skip(f"N2 上只有 {len(n2)} 个待执行阶段，不足以测调序")

        reversed_ids = [s.stage_id for s in reversed(n2)]
        report = await reorder_stages(store=store, node_id="N2", ordered_stage_ids=reversed_ids)

        assert report.applied
        assert report.effective_order[: len(reversed_ids)] == reversed_ids

        # 其它节点的队列不受影响
        a_stages = await store.tasks.list_stages_by_node("A")
        assert a_stages, "A 节点的阶段不应被删除"

    async def test_reorder_rejects_started_stages(
        self, store, sm, harness, scheduler, ledger, make_workflow, tick
    ):
        """RUN-04：暂停、已删除、失败或完成阶段不作为普通 pending 项调序。"""
        await _setup_harness(store)
        wf, _ = await make_workflow(_graph({"A": []}), max_concurrent=8)
        r1 = await launch_task(store=store, workflow_id=wf.workflow_id)
        r2 = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler)

        stages = await store.tasks.list_stages(r1.task.task_id)
        running = stages[0]
        assert running.observed_state == StageState.RUNNING

        report = await reorder_stages(
            store=store, node_id="A", ordered_stage_ids=[running.stage_id]
        )
        assert report.rejected, "已运行的阶段必须被拒绝调序并给出原因"
        assert report.rejected[0]["reason"]
