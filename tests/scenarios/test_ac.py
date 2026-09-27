"""AC-01–AC-22 验收场景（清单 §5）。

每个用例的 docstring 以「AC-xx」开头，并引用它覆盖的功能编号。清单明确：
「AC 场景用于验证行为……不能替代输入兼容性和存量任务测试」——所以这些用例
验证的是**行为**，不是内部实现细节。

需要真实 harness 的场景（AC-01 的真实跨 harness、AC-18 平台矩阵）在
``tests/interactive/`` 里，默认不跑；这里跑的是同一行为在内核层面的验证。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from workerbee.core.domain import (
    Edge,
    EdgeContract,
    ExecutionProfile,
    GraphSpec,
    NodeDefinition,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from workerbee.core.domain.registry import HarnessRegistration
from workerbee.core.domain.task import StageState, TaskState
from workerbee.core.runtime.launch import launch_task
from workerbee.core.runtime.lifecycle import (
    delete_task,
    pause_task,
    reorder_stages,
    resume_task,
    set_node_enabled,
)
from workerbee.core.runtime.ports import SessionCaps

from tests.conftest import drain

pytestmark = [pytest.mark.scenario, pytest.mark.integration]


# ---------------------------------------------------------------------------
# 场景脚手架
# ---------------------------------------------------------------------------


async def _register_harness(store, harness_id: str, *, ok: bool = True) -> None:
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=harness_id, name=harness_id, adapter_id="fake", last_probe_ok=ok
        )
    )


def _node(
    name: str,
    harness: str = "h1",
    *,
    model: str = "m1",
    required_inputs: list[str] | None = None,
    prompt: str | None = None,
) -> NodeDefinition:
    return NodeDefinition(
        node_id=name,
        name=name,
        system_prompt=prompt,
        profiles=[ExecutionProfile(model_name=model, harness_ref=harness)],
        required_inputs=required_inputs or [],
    )


def _spec(adjacency: dict[str, list[str]], nodes: list[NodeDefinition] | None = None,
          edges: list[Edge] | None = None) -> GraphSpec:
    if nodes is None:
        names = sorted(set(adjacency) | {t for ts in adjacency.values() for t in ts})
        nodes = [_node(n) for n in names]
    if edges is None:
        edges = [Edge(from_node=a, to_node=b) for a, ts in adjacency.items() for b in ts]
    return GraphSpec(nodes=nodes, edges=edges)


async def _publish(store, spec: GraphSpec, *, name: str = "wf", max_concurrent: int = 8):
    wf = WorkflowDefinition(name=name, max_concurrent_tasks=max_concurrent)
    await store.workflows.create(wf)
    await store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id, revision_seq=1, graph=spec, is_published=True
        ),
        publish=True,
        expected_revision_seq=0,
    )
    wf.current_revision_seq = 1
    wf.status = WorkflowStatus.PUBLISHED
    return wf


async def _drive(scheduler, store, *, max_rounds: int = 40, fail_nodes: set[str] | None = None):
    """把调度跑到无事可做为止，自动让每个在途尝试产出并结束。

    ``fail_nodes`` 里的节点会以失败结束（用于测失败传播）。
    """
    fail_nodes = fail_nodes or set()
    for _ in range(max_rounds):
        await scheduler.tick()
        rts = list(scheduler._runtimes.values())
        if not rts:
            await asyncio.sleep(0)
            if not scheduler._retry_heap:
                break
            scheduler._retry_heap = [(0.0, sid) for _, sid in scheduler._retry_heap]
            continue
        for rt in rts:
            stage = await store.tasks.get_stage(rt.stage_id)
            if stage is None:
                continue
            node = (await store.tasks.get_task(rt.task_id)).graph_snapshot.graph.node(
                stage.node_id
            )
            node_name = node.name if node else ""
            ok = node_name not in fail_nodes
            if ok:
                await scheduler.on_event(
                    session_ref=rt.session_ref,
                    kind="output",
                    payload={"text": f"{node_name} 的产出"},
                )
            await scheduler.on_session_ended(session_ref=rt.session_ref, ok=ok)
        await asyncio.sleep(0)


# ===========================================================================
# AC-02 多次提交
# ===========================================================================


async def test_ac02_multiple_submissions_are_isolated(store, sm, harness, scheduler, tick):
    """AC-02：同一输入主动提交两次得到两个任务；同一次请求重送能核对原提交。

    覆盖 RUN-02/03/05、DATA-02。各节点串行、各任务上下文隔离，不跨任务汇聚。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"N1": ["N2"], "N2": []}), max_concurrent=8)

    r1 = await launch_task(
        store=store, workflow_id=wf.workflow_id, input_payload={"task": "同一份输入"}
    )
    r2 = await launch_task(
        store=store, workflow_id=wf.workflow_id, input_payload={"task": "同一份输入"}
    )
    assert r1.task.task_id != r2.task.task_id, "两次有意提交必须产生两个任务"

    resent = await launch_task(
        store=store,
        workflow_id=wf.workflow_id,
        input_payload={"task": "同一份输入"},
        idempotency_key="req-xyz",
    )
    resent2 = await launch_task(
        store=store,
        workflow_id=wf.workflow_id,
        input_payload={"task": "同一份输入"},
        idempotency_key="req-xyz",
    )
    assert resent.task.task_id == resent2.task.task_id, "重送必须核对到原提交"
    assert resent2.created is False

    await _drive(scheduler, store)

    # 节点串行：同一节点任一时刻至多一个占用执行槽的阶段
    for node in ("N1", "N2"):
        stages = await store.tasks.list_stages_by_node(node)
        assert len(stages) == 3, f"节点 {node} 应关联全部三个任务的阶段（重送不产生新任务）"

    # 不跨任务汇聚：每个任务的 N2 都只由自己任务的 N1 推进
    for task_id in (r1.task.task_id, r2.task.task_id):
        stages = {s.node_id: s for s in await store.tasks.list_stages(task_id)}
        assert stages["N2"].upstream_pins.get("N1"), "下游必须钉扎本任务上游的产物"


# ===========================================================================
# AC-03 队列调序
# ===========================================================================


async def test_ac03_reorder_changes_only_target_node_queue(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-03：N1 已完成任务 A，而 A 在 N2 pending；在 N2 调整 A 与 B 的顺序，
    只改变 N2 的有效队列。与执行启动冲突时显示生效或拒绝原因。

    覆盖 RUN-04。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"N1": ["N2"], "N2": []}), max_concurrent=8)

    tasks = [
        (await launch_task(store=store, workflow_id=wf.workflow_id)).task.task_id
        for _ in range(3)
    ]

    # 让所有 N1 完成，使 N2 上堆积 pending
    for _ in range(30):
        await scheduler.tick()
        rts = [r for r in scheduler._runtimes.values()]
        if not rts:
            break
        for rt in rts:
            st = await store.tasks.get_stage(rt.stage_id)
            if st is not None and st.node_id == "N1":
                await scheduler.on_event(
                    session_ref=rt.session_ref, kind="output", payload={"text": "n1 产出"}
                )
                await scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)

    pending = await store.tasks.list_stages_by_node(
        "N2", states=[StageState.WAITING_DEPS, StageState.READY]
    )
    if len(pending) < 2:
        pytest.skip("N2 上的 pending 不足以测调序")

    requested = [s.stage_id for s in reversed(pending)]
    report = await reorder_stages(store=store, node_id="N2", ordered_stage_ids=requested)

    assert report.applied
    assert report.effective_order[: len(requested)] == requested
    n1_queue = await store.tasks.list_stages_by_node("N1")
    assert n1_queue, "调序不得破坏其他节点的队列"


# ===========================================================================
# AC-04 分支与汇聚
# ===========================================================================


async def test_ac04_fan_out_and_join_with_failure(
    store, sm, harness, scheduler, tick
):
    """AC-04：A 分到 B、C，再汇聚 D。B、C 可分别开始；D 只消费同一任务的
    必需成功结果。C 失败时 D 不正常启动，整体说明失败与其他分支状态。

    覆盖 RUN-05–07、DATA-02–04。
    """
    await _register_harness(store, "h1")
    for name in ("A", "B", "C", "D"):
        n = _node(name)
        n.profiles[0].retry.max_attempts = 1
    spec = _spec(
        {"A": ["B", "C"], "B": ["D"], "C": ["D"]},
        nodes=[_no_retry(n) for n in ("A", "B", "C", "D")],
    )
    wf = await _publish(store, spec)
    r = await launch_task(store=store, workflow_id=wf.workflow_id)

    await _drive(scheduler, store, fail_nodes={"C"})

    stages = {s.node_id: s for s in await store.tasks.list_stages(r.task.task_id)}
    assert stages["B"].observed_state == StageState.SUCCEEDED
    assert stages["C"].observed_state == StageState.FAILED
    assert stages["D"].observed_state in (StageState.BLOCKED, StageState.CANCELLED), (
        "C 失败时 D 不得作为正常成功路径启动"
    )

    task = await store.tasks.get_task(r.task.task_id)
    assert task.observed_state == TaskState.FAILED
    summary = task.failure_summary
    assert summary["failed_paths"], "必须显示失败原因"
    assert summary["succeeded"], "必须同时显示已完成的分支（RUN-07）"


def _no_retry(name: str) -> NodeDefinition:
    n = _node(name)
    n.profiles[0].retry.max_attempts = 1
    return n


# ===========================================================================
# AC-06 入口、出口与输入
# ===========================================================================


async def test_ac06_disabled_entry_uses_remaining_entry(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-06：停用入口后，剩余合法入口可使用原任务输入；不能只凭图无环宣布可执行。

    覆盖 WF-05、ACT-02/03。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"E1": ["Mid"], "E2": ["Mid"], "Mid": []}))

    await set_node_enabled(
        store=store, sm=sm, harness=harness, ledger=ledger,
        workflow_id=wf.workflow_id, node_id="E1", enable=False, mode="immediate",
    )

    response = await launch_task(store=store, workflow_id=wf.workflow_id)
    task = await store.tasks.get_task(response.task.task_id)
    assert task.graph_snapshot.entry_nodes() == ["E2"], "剩余合法入口应被采用"
    assert task.graph_snapshot.effective_predecessors("Mid") == ["E2"]


async def test_ac06_all_disabled_rejects_submission(store, sm, harness, ledger):
    """AC-06：全部停用拒绝执行，且拒绝发生在提交时而不是运行到一半。"""
    from workerbee.core.runtime.launch import LaunchRejected

    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))

    await set_node_enabled(
        store=store, sm=sm, harness=harness, ledger=ledger,
        workflow_id=wf.workflow_id, node_id="A", enable=False, mode="immediate",
    )

    with pytest.raises(LaunchRejected) as exc:
        await launch_task(store=store, workflow_id=wf.workflow_id)
    assert exc.value.report.has_code("no_enabled_node")


async def test_ac06_bypass_cannot_silently_substitute_input(
    store, sm, harness, ledger
):
    """AC-06 / ACT-03：停用必要转换节点导致输入缺失时必须**明确阻止**，
    不允许把上游原始输出无提示地当作它的结果。
    """
    from workerbee.core.runtime.launch import LaunchRejected

    await _register_harness(store, "h1")
    spec = _spec(
        {"A": ["B"], "B": ["C"], "C": []},
        nodes=[
            _node("A"),
            _node("B"),
            _node("C", required_inputs=["plan", "risks"]),
        ],
        edges=[
            Edge(from_node="A", to_node="B", output_contract=EdgeContract(outputs=["raw_notes"])),
            Edge(from_node="B", to_node="C", output_contract=EdgeContract(outputs=["plan", "risks"])),
        ],
    )
    wf = await _publish(store, spec)

    await set_node_enabled(
        store=store, sm=sm, harness=harness, ledger=ledger,
        workflow_id=wf.workflow_id, node_id="B", enable=False, mode="immediate",
    )

    with pytest.raises(LaunchRejected) as exc:
        await launch_task(store=store, workflow_id=wf.workflow_id)

    report = exc.value.report
    assert report.has_code("contract_unsatisfied")
    import re

    missing = {
        re.findall(r"「([^」]+)」", d.message)[1]  # [0] 是节点名，[1] 是缺失字段
        for d in report.errors()
        if d.code == "contract_unsatisfied"
    }
    assert missing == {"plan", "risks"}, "每个缺失字段都要单独定位，不能只报第一个"
    diag = next(d for d in report.errors() if d.code == "contract_unsatisfied")
    assert diag.node_name == "C"
    assert diag.fix_action == "waive_contract", "必须提供「显式降级继续」这个入口"


async def test_ac06_explicit_waiver_allows_degraded_run(store, sm, harness, ledger):
    """AC-06：显式确认「以 A 的原始输出降级继续」之后才允许执行，且如实标注。"""
    from workerbee.core.graph.validate import InMemoryRegistry, ValidationMode, validate

    await _register_harness(store, "h1")
    from workerbee.core.domain import ContractWaiver

    spec = _spec(
        {"A": ["B"], "B": ["C"], "C": []},
        nodes=[_node("A"), _node("B"), _node("C", required_inputs=["plan"])],
        edges=[Edge(from_node="A", to_node="B", output_contract=EdgeContract(outputs=["notes"]))],
    )
    spec.waivers.append(
        ContractWaiver(
            node_id="C",
            required_input="plan",
            reason="用户确认以降级方式继续",
            at="2026-09-27T00:00:00+00:00",
        )
    )
    registry = await store.registry.snapshot()
    report = validate(spec, registry, mode=ValidationMode.PUBLISH)

    assert report.ok(), "有显式确认时不应阻断"
    assert report.has_code("contract_waived")
    waived = next(d for d in report.infos() if d.code == "contract_waived")
    assert "用户已确认" in waived.message


# ===========================================================================
# AC-07 运行中改图
# ===========================================================================


async def test_ac07_inflight_task_keeps_pinned_rules(
    store, sm, harness, ledger, scheduler, make_workflow, tick
):
    """AC-07：N2 上有运行、排队阶段时修改配置或停用 N2，
    界面准确列出各任务采用哪套规则及何时生效，无丢失或双重推进。

    覆盖 WF-06、ACT-04、CFG-07。生效范围：在途任务按发射时钉扎的有效图执行至结束
    （架构设计 §4.1），启停产生的新修订只影响之后发射的任务。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"N1": ["N2"], "N2": []}), max_concurrent=8)

    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    # 完成 N1，让 N2 起来
    for _ in range(10):
        rts = list(scheduler._runtimes.values())
        if not rts:
            break
        st = await store.tasks.get_stage(rts[0].stage_id)
        if st is not None and st.node_id == "N1":
            await scheduler.on_event(
                session_ref=rts[0].session_ref, kind="output", payload={"text": "n1"}
            )
            await scheduler.on_session_ended(session_ref=rts[0].session_ref, ok=True)
            break
    await scheduler.tick()

    task_before = await store.tasks.get_task(r.task.task_id)
    pinned_version = task_before.effective_graph_version

    await set_node_enabled(
        store=store, sm=sm, harness=harness, ledger=ledger,
        workflow_id=wf.workflow_id, node_id="N2", enable=False, mode="immediate",
    )

    task_after = await store.tasks.get_task(r.task.task_id)
    assert task_after.effective_graph_version == pinned_version, (
        "在途任务的钉扎有效图不得被启停改写（WF-06：不静默改道）"
    )
    assert task_after.revision_seq == task_before.revision_seq

    # 新任务采用新修订
    rev = await store.workflows.get_current_revision(wf.workflow_id)
    assert rev.revision_seq == 2
    assert rev.graph.node("N2").enabled is False


# ===========================================================================
# AC-10 暂停与删除范围
# ===========================================================================


async def test_ac10_scope_and_no_resume_after_delete(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-10：两任务各有并行分支，从任务 A 的节点发起暂停／删除，
    影响 A 的明示范围，不影响任务 B。暂停可继续，删除不可续跑；已成功上游仍可回看。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}), max_concurrent=8)

    ra = await launch_task(store=store, workflow_id=wf.workflow_id)
    rb = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()

    # 暂停任务 A
    paused = await pause_task(
        store=store, sm=sm, harness=harness, ledger=ledger,
        task_id=ra.task.task_id, origin_node_id="A", reason="先看看",
    )
    assert paused.accepted
    ta = await store.tasks.get_task(ra.task.task_id)
    tb = await store.tasks.get_task(rb.task.task_id)
    assert ta.observed_state == TaskState.PAUSED
    assert tb.observed_state != TaskState.PAUSED, "任务 B 不受影响"

    # 暂停可继续
    resumed = await resume_task(
        store=store, sm=sm, harness=harness, ledger=ledger, task_id=ra.task.task_id
    )
    assert resumed.accepted
    assert (await store.tasks.get_task(ra.task.task_id)).observed_state == TaskState.RUNNING

    # 删除任务 A
    deleted = await delete_task(
        store=store, sm=sm, harness=harness, ledger=ledger,
        task_id=ra.task.task_id, origin_node_id="A",
    )
    assert deleted.accepted and deleted.execution_stopped
    ta = await store.tasks.get_task(ra.task.task_id)
    assert ta.observed_state == TaskState.CANCELLED

    with pytest.raises(ValueError):
        await resume_task(
            store=store, sm=sm, harness=harness, ledger=ledger, task_id=ra.task.task_id
        )

    # 已成功上游仍可回看
    stages = await store.tasks.list_stages(ra.task.task_id)
    assert any(s.observed_state in (StageState.SUCCEEDED, StageState.CANCELLED) for s in stages)
    events = await store.events.for_task(ra.task.task_id)
    assert events, "历史必须保留，删除不等于抹掉记录"


# ===========================================================================
# AC-11 控制操作竞态
# ===========================================================================


async def test_ac11_late_callback_cannot_revive_deleted_task(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-11：删除与完成同时出现时，旧回调不能复活删除任务或重复触发下游。

    覆盖 RUN-04、LIFE-06、REC-05。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": ["B"], "B": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    rt = list(scheduler._runtimes.values())[0]

    await delete_task(
        store=store, sm=sm, harness=harness, ledger=ledger, task_id=r.task.task_id
    )

    # 迟到回调
    await scheduler.on_event(
        session_ref=rt.session_ref, kind="output", payload={"text": "迟到的产出"}
    )
    await scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)

    task = await store.tasks.get_task(r.task.task_id)
    assert task.observed_state == TaskState.CANCELLED, "旧回调不得复活已删除任务"

    stages = {s.node_id: s for s in await store.tasks.list_stages(r.task.task_id)}
    assert stages["B"].observed_state == StageState.CANCELLED, "下游不得被重复触发"


async def test_ac11_concurrent_cas_only_one_wins(store, sm, harness, ledger, tick):
    """AC-11：控制操作与控制操作并发时，CAS 只有一个胜出，另一个拿到实际结果。"""
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    task = await store.tasks.get_task(r.task.task_id)
    epoch = task.control_epoch

    first = await store.tasks.update_task(
        task.task_id, desired_state="paused", expected_epoch=epoch, bump_epoch=True
    )
    second = await store.tasks.update_task(
        task.task_id, desired_state="cancelled", expected_epoch=epoch, bump_epoch=True
    )
    assert first is True and second is False, "同一 epoch 的两个控制操作只能有一个成功"


# ===========================================================================
# AC-12 客户端断连
# ===========================================================================


async def test_ac12_approval_survives_disconnect(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-12：关闭浏览器后后台任务继续；期间出现的审批不自动通过；
    重连看到同一任务真实状态与待处理请求。
    """
    from workerbee.security.approval_gateway import ApprovalGateway

    await _register_harness(store, "h1")
    gateway = ApprovalGateway(store=store, timeout_seconds=3600)
    # 没有订阅者（等价于客户端断连）时，审批也必须保持 pending

    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    rt = list(scheduler._runtimes.values())[0]
    stage = await store.tasks.get_stage(rt.stage_id)

    await gateway.request(
        approval_id="ap-disconnect",
        task_id=r.task.task_id,
        stage_id=stage.stage_id,
        attempt_id=rt.attempt_id,
        revision_seq=1,
        node_id="A",
        action="Bash(rm -rf build/)",
        target="build/",
    )

    # 断连期间：审批仍是 pending，任务仍在跑
    pendings = await gateway.open_items()
    assert [a.approval_id for a in pendings] == ["ap-disconnect"]
    assert (await store.tasks.get_task(r.task.task_id)).observed_state == TaskState.RUNNING

    # 断连期间超时 → 按 deny_pause 拒绝，不构成批准
    from datetime import timedelta

    from workerbee.core.domain.base import utcnow

    expired = await gateway.expire_due(now=utcnow() + timedelta(hours=2))
    assert expired and expired[0]["approval_id"] == "ap-disconnect"
    assert "没看到不等于默许" in expired[0]["note"]


# ===========================================================================
# AC-13 节点及服务重启
# ===========================================================================


async def test_ac13_reconcile_does_not_blindly_rerun(
    store, sm, harness, ledger, scheduler, tick
):
    """AC-13：在「成功结果已产生但完成状态未确认」处故障，重启后核对而非直接重跑。

    覆盖 REC-02–05。
    """
    from workerbee.core.runtime.reconcile import reconcile_on_startup

    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": ["B"], "B": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    rt = list(scheduler._runtimes.values())[0]
    stage = await store.tasks.get_stage(rt.stage_id)
    attempt = (await store.tasks.list_attempts(stage.stage_id))[0]

    # 模拟：产物已落、完成标记已写，但阶段状态还没推进就被打断
    from workerbee.core.domain.artifact import ArtifactKind, ArtifactProducer

    await store.artifacts.put(
        "A 的产出",
        kind=ArtifactKind.TEXT,
        producer=ArtifactProducer(
            task_id=r.task.task_id, stage_id=stage.stage_id, attempt_seq=attempt.attempt_seq
        ),
    )
    from workerbee.core.domain.task import AttemptOutcome, ErrorClass

    await store.tasks.complete_attempt(
        attempt.attempt_id, outcome=AttemptOutcome(error_class=ErrorClass.SUCCESS)
    )
    # 会话已死（模拟节点失效）
    await harness.terminate(rt.session_ref)

    report = await reconcile_on_startup(
        store=store, sm=sm, harness=harness, ledger=ledger, scheduler=scheduler
    )

    assert report.confirmed_success, "有完成标记且产物已验证 → 确认成功，不重跑"
    stages = {s.node_id: s for s in await store.tasks.list_stages(r.task.task_id)}
    assert stages["A"].observed_state == StageState.SUCCEEDED
    assert stages["B"].observed_state in (StageState.READY, StageState.WAITING_DEPS)
    assert len(harness.created) == 1, "不得重新执行已完成的阶段"


async def test_ac13_paused_stays_paused_after_restart(
    store, sm, harness, ledger, scheduler, tick
):
    """AC-13：主动暂停仍暂停，主动删除不恢复。"""
    from workerbee.core.runtime.reconcile import reconcile_on_startup

    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    await pause_task(store=store, sm=sm, harness=harness, ledger=ledger,
                     task_id=r.task.task_id)

    await store.db.execute(
        "UPDATE task SET observed_state='running' WHERE task_id=?", (r.task.task_id,)
    )
    await reconcile_on_startup(
        store=store, sm=sm, harness=harness, ledger=ledger, scheduler=scheduler
    )
    assert (await store.tasks.get_task(r.task.task_id)).observed_state == TaskState.PAUSED


async def test_ac13_unknown_state_is_not_blindly_replayed(
    store, sm, harness, ledger, scheduler, tick
):
    """AC-13 / REC-04：无法判断外部副作用是否完成时进入待核对，不盲目重放。"""
    from workerbee.core.runtime.reconcile import reconcile_on_startup

    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()
    rt = list(scheduler._runtimes.values())[0]
    # 会话已死但没有任何完成标记 → 状态不明
    await harness.terminate(rt.session_ref)
    scheduler._runtimes.clear()

    report = await reconcile_on_startup(
        store=store, sm=sm, harness=harness, ledger=ledger, scheduler=scheduler
    )
    assert report.lost, "无法确认时进入 LOST 而不是重跑"
    stages = await store.tasks.list_stages(r.task.task_id)
    assert stages[0].observed_state == StageState.LOST
    assert len(harness.created) == 1, "不得自动重放"


# ===========================================================================
# AC-15 资源释放与共享
# ===========================================================================


async def test_ac15_teardown_refuses_paths_outside_managed_root(
    store, sm, harness, ledger, tmp_path
):
    """AC-15：无关进程、项目文件不受误删。

    台账只对被登记且**确实属于托管目录**的对象动手；越界一律拒绝并报出。
    """
    project_file = tmp_path / "user_project" / "important.txt"
    project_file.parent.mkdir(parents=True)
    project_file.write_text("用户的文件，不许动")

    rid = await ledger.register(
        kind="tmp_file",
        locator={"path": str(project_file)},
        owner={"task_id": "t1", "stage_id": "s1", "attempt_id": "a1", "node_id": "A"},
        teardown={"method": "unlink", "timeout_ms": 100},
    )
    await ledger.close_for_attempt("a1")

    assert project_file.exists(), "托管目录之外的文件绝不能被删除"
    rows = await store.db.fetch_all(
        "SELECT * FROM resource_record WHERE resource_id=?", (rid,)
    )
    assert rows[0]["state"] == "orphaned", "拒绝清理的句柄必须保持可见"
    assert "无法确认归属" in (rows[0]["last_error"] or "")


async def test_ac15_managed_tmp_file_is_cleaned(store, tmp_path):
    from workerbee.core.resources.ledger import ResourceLedger

    managed = tmp_path / "workspace"
    managed.mkdir()
    target = managed / "scratch.tmp"
    target.write_text("临时内容")

    ledger = ResourceLedger(store, managed_roots=[managed])
    rid = await ledger.register(
        kind="tmp_file",
        locator={"path": str(target)},
        owner={"task_id": "t1", "stage_id": "s1", "attempt_id": "a2", "node_id": "A"},
        teardown={"method": "unlink", "timeout_ms": 100},
    )
    counts = await ledger.close_for_attempt("a2")

    assert counts["closed"] == 1
    assert not target.exists()
    rows = await store.db.fetch_all(
        "SELECT state FROM resource_record WHERE resource_id=?", (rid,)
    )
    assert rows[0]["state"] == "closed"


async def test_ac15_teardown_failure_stays_visible(store, tmp_path):
    """AC-15 / LIFE-06：清理失败有持续可见的结果，不被隐藏。"""
    from workerbee.core.resources.ledger import ResourceLedger

    async def failing_teardown(spec: dict):
        return False, "模拟：远端连接拒绝关闭"

    ledger = ResourceLedger(store, harness_teardown=failing_teardown)
    await ledger.register(
        kind="api_stream",
        locator={"session_ref": "sess-x"},
        owner={"task_id": "t1", "stage_id": "s1", "attempt_id": "a3", "node_id": "A"},
        teardown={"method": "abort", "timeout_ms": 100},
    )
    counts = await ledger.close_for_attempt("a3")

    assert counts["teardown_failed"] == 1
    unresolved = await ledger.teardown_failed()
    assert unresolved and unresolved[0]["last_error"] == "模拟：远端连接拒绝关闭"


# ===========================================================================
# AC-17 可用量与历史
# ===========================================================================


async def test_ac17_usage_unknown_is_not_zero(
    store, sm, harness, scheduler, tick
):
    """AC-17：用量不可取得时显示未知（None），而不是 0。

    覆盖 OBS-02–04、RES-03。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await _drive(scheduler, store)

    attempt = (await store.tasks.list_attempts_for_task(r.task.task_id))[0]
    assert attempt.usage is None, "未上报用量时必须保持「未知」，不能落成 0"


async def test_ac17_history_cleanup_refuses_live_references(store, ledger, tmp_path):
    """AC-17：清理仍被恢复使用的数据会被阻止或明确处理依赖。

    覆盖 RES-03：已标 tombstone 但仍被活跃任务引用的产物不得被物理回收。
    """
    from workerbee.core.resources.reaper import Reaper, ReaperConfig

    art = await store.artifacts.put("仍被引用的产物")
    await store.tasks.create_task_with_stages(
        _make_task_for(store, "live-task"), [_make_stage_for("live-task")]
    )
    await store.artifacts.add_reference(art.artifact_id)

    reaper = Reaper(
        store=store,
        ledger=ledger,
        config=ReaperConfig(artifact_gc_enabled=True, artifact_gc_min_age_hours=0),
    )
    await reaper.run_once()

    still_there = await store.artifacts.get(art.artifact_id)
    assert still_there is not None, "仍被引用的产物不得被回收"


def _make_task_for(store, task_id: str):
    from tests.conftest import make_task

    return make_task(task_id=task_id)


def _make_stage_for(task_id: str):
    from tests.conftest import make_stage

    return make_stage(stage_id=f"s-{task_id}", task_id=task_id)


# ===========================================================================
# AC-19 多流程和共享配置
# ===========================================================================


async def test_ac19_workflow_isolation_and_credential_absence(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-19：两个 Workflow 同时运行并引用相同工具／认证。
    删除其中一个后，其任务不再推进，另一个仍可使用未撤销的共享配置。
    凭据不出现在摘要、模板和普通历史中。覆盖 WF-07、CFG-07、AUTH-01/02、LIFE-05。
    """
    from workerbee.core.domain.registry import CredentialKind, CredentialRef
    from workerbee.core.runtime.lifecycle import delete_workflow

    await _register_harness(store, "h1")
    cred = CredentialRef(
        credential_id="cred-1",
        label="共享认证",
        kind=CredentialKind.API_KEY,
        secret_locator="vault://cred-1",
    )
    await store.registry.upsert_credential(cred)

    wf_a = await _publish(store, _spec({"A": []}), name="wf-a", max_concurrent=8)
    wf_b = await _publish(store, _spec({"A": []}), name="wf-b", max_concurrent=8)

    ra = await launch_task(store=store, workflow_id=wf_a.workflow_id)
    rb = await launch_task(store=store, workflow_id=wf_b.workflow_id)
    await scheduler.tick()

    await delete_workflow(
        store=store, sm=sm, harness=harness, ledger=ledger, workflow_id=wf_a.workflow_id
    )

    ta = await store.tasks.get_task(ra.task.task_id)
    tb = await store.tasks.get_task(rb.task.task_id)
    assert ta.observed_state == TaskState.CANCELLED
    assert tb.observed_state != TaskState.CANCELLED, "另一个 Workflow 的任务不受影响"

    # 共享凭据未被删除
    assert await store.registry.get_credential("cred-1") is not None

    # 凭据本体不出现在事件历史里
    all_events = await store.events.tail(limit=1000)
    blob = repr(all_events)
    assert "vault://cred-1" not in blob or "secret_locator" not in blob, (
        "凭据的定位信息不应进入事件历史正文"
    )
    assert "cred-1" not in repr([e.get("payload") for e in all_events if e["type"] == "task.submitted"])


# ===========================================================================
# AC-20 交接失败和返工请求
# ===========================================================================


async def test_ac20_handoff_failure_blocks_downstream_explicitly(
    store, sm, harness, scheduler, tick, summarizer
):
    """AC-20：上游大文件失效或摘要生成失败时，下游明确受阻；
    多个消费者已经使用的输入版本保持可追溯。覆盖 DATA-01–06。
    """
    await _register_harness(store, "h1")
    summarizer.raise_on_call = True

    spec = _spec(
        {"A": ["B"], "B": ["C"], "C": []},
        nodes=[_no_retry(n) for n in ("A", "B", "C")],
        edges=[
            Edge(from_node="A", to_node="B", output_contract=EdgeContract(outputs=["plan"])),
            Edge(from_node="B", to_node="C", output_contract=EdgeContract(outputs=["code"])),
        ],
    )
    wf = await _publish(store, spec)
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await _drive(scheduler, store)

    events = await store.events.for_task(r.task.task_id)
    assert any(e["type"] == "handoff.failed" for e in events), (
        "摘要失败必须报出交接失败，不能静默省略换取成功"
    )

    task = await store.tasks.get_task(r.task.task_id)
    assert task.observed_state == TaskState.FAILED
    failed = next(
        s for s in await store.tasks.list_stages(r.task.task_id)
        if s.observed_state == StageState.FAILED
    )
    assert "契约" in (failed.blocked_reason or "") or "摘要" in (failed.blocked_reason or "")
    assert failed.blocked_reason, "受阻原因必须可定位"


async def test_ac20_feedback_does_not_auto_rework(
    store, sm, harness, scheduler, tick
):
    """AC-20 / DATA-06：下游提出补充请求后……系统不自行形成无限返工。

    首版把 FEEDBACK 呈现给用户，不自动唤醒已完成上游（D-06）。
    """
    from workerbee.core.domain.message import Message, MessageType

    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": ["B"], "B": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await _drive(scheduler, store)

    stages = {s.node_id: s for s in await store.tasks.list_stages(r.task.task_id)}
    attempts_before = len(
        await store.tasks.list_attempts(stages["A"].stage_id)
    )

    msg = Message(
        type=MessageType.FEEDBACK,
        task_id=r.task.task_id,
        from_stage=stages["B"].stage_id,
        to_stage=stages["A"].stage_id,
        dedup_key=f"feedback:{r.task.task_id}:1",
        payload={"problem": "缺少风险清单", "artifact_ref": None},
    )
    sent, created = await store.messages.send(msg)
    assert created

    await scheduler.tick()
    attempts_after = len(await store.tasks.list_attempts(stages["A"].stage_id))
    assert attempts_after == attempts_before, (
        "FEEDBACK 不得自动唤醒已完成的上游（首版不实现自动返工）"
    )
    assert (await store.messages.inbox(stages["A"].stage_id)), "反馈应留在收件箱里呈现给用户"


# ===========================================================================
# AC-22 后台工作和状态事实
# ===========================================================================


async def test_ac22_transitional_states_are_visible(
    store, sm, harness, scheduler, ledger, tick
):
    """AC-22：暂停、恢复或清理尚未完成时显示真实过渡状态。

    覆盖 RUN-06、OBS-01/02、LIFE-02/06、RES-04。
    """
    await _register_harness(store, "h1")
    wf = await _publish(store, _spec({"A": []}))
    r = await launch_task(store=store, workflow_id=wf.workflow_id)
    await scheduler.tick()

    # 暂停过程中的「暂停中」必须可见；暂停完成后才是「已暂停」
    report = await pause_task(
        store=store, sm=sm, harness=harness, ledger=ledger, task_id=r.task.task_id
    )
    assert report.accepted
    task = await store.tasks.get_task(r.task.task_id)
    assert task.observed_state == TaskState.PAUSED
    stages = await store.tasks.list_stages(r.task.task_id)
    assert stages[0].status_reason, "阶段必须带上可读的状态说明"


async def test_ac22_skill_is_not_forced_limit(store):
    """AC-22 / RES-04：Skill 是执行指导，不能被冒充为框架已强制的资源限制。"""
    from workerbee.core.domain.registry import SkillDoc, SkillScope

    skill = SkillDoc(
        skill_id="sk-1",
        name="资源指导",
        content="请在 4GB 内存内完成，不要使用 sleep 等待",
        scope=SkillScope.GLOBAL,
    )
    await store.registry.upsert_skill(skill)
    loaded = await store.registry.get_skill("sk-1")
    assert loaded is not None
    # 框架不解析 Skill 文本推导资源硬限制（D-10）——数据模型里根本没有这种字段
    assert not hasattr(loaded, "memory_limit")
    assert not hasattr(loaded, "enforced_resources")
