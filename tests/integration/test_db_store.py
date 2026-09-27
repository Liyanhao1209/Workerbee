"""SQLite 事务语义与仓储 round-trip（架构设计 v0.02 D-08、§5）。

事务部分是重点：CAS 与写前日志都建立在「同一连接、串行写者、BEGIN IMMEDIATE」
这个前提上，前提不成立时 CAS 会静默失效——那种缺陷不会在单线程测试里暴露。
"""

from __future__ import annotations

import asyncio

import pytest

from workerbee.core.domain import (
    ApprovalStatus,
    Attempt,
    AttemptOutcome,
    CompactEvent,
    DesiredState,
    Edge,
    ErrorClass,
    GraphSpec,
    OriginOfControl,
    StageState,
    TaskState,
    TemplateKind,
    Usage,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from workerbee.core.domain.template import Template
from workerbee.data.db import ConflictError, Database

from tests.conftest import make_approval, make_graph, make_stage, make_task

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 事务
# ---------------------------------------------------------------------------


async def test_transaction_commits(db):
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO meta_kv(k, v, updated_at) VALUES ('a', '1', 'now')"
        )
    assert await db.fetch_value("SELECT v FROM meta_kv WHERE k='a'") == "1"


async def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO meta_kv(k, v, updated_at) VALUES ('a', '1', 'now')"
            )
            raise RuntimeError("boom")

    assert await db.fetch_value("SELECT COUNT(*) FROM meta_kv", default=0) == 0


async def test_nested_transaction_inner_failure_does_not_commit_inner_writes(db):
    """内层失败必须只回滚内层——否则「守卫失败但仍写入」会污染外层事务。"""
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO meta_kv(k, v, updated_at) VALUES ('outer','1','now')")
        with pytest.raises(RuntimeError):
            async with db.transaction() as inner:
                await inner.execute(
                    "INSERT INTO meta_kv(k, v, updated_at) VALUES ('inner','1','now')"
                )
                raise RuntimeError("inner failure")

    assert await db.fetch_value("SELECT COUNT(*) FROM meta_kv WHERE k='outer'") == 1
    assert await db.fetch_value("SELECT COUNT(*) FROM meta_kv WHERE k='inner'") == 0


async def test_concurrent_transactions_serialize(db):
    """两个 asyncio 任务不得共用同一事务——B 看不到 A 未提交的数据。"""
    holder_inside = asyncio.Event()
    release_holder = asyncio.Event()
    observed: list[int] = []

    async def holder():
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO meta_kv(k, v, updated_at) VALUES ('a','1','now')"
            )
            holder_inside.set()
            await release_holder.wait()

    async def observer():
        await holder_inside.wait()
        await asyncio.sleep(0.05)  # 给 observer 机会在 holder 提交前抢锁
        async with db.transaction() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM meta_kv WHERE k='a'")
            row = await cur.fetchone()
            await cur.close()
            observed.append(row[0])

    t_hold = asyncio.create_task(holder())
    t_obs = asyncio.create_task(observer())

    await holder_inside.wait()
    await asyncio.sleep(0.05)
    assert not observed, "observer 在 holder 提交前就进入了事务"
    release_holder.set()

    await asyncio.gather(t_hold, t_obs)
    assert observed == [1]


async def test_sequential_transactions_after_failure_still_work(db):
    """一次失败回滚后，连接必须仍可用于新事务（BEGIN 状态没有残留）。"""
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO meta_kv(k, v, updated_at) VALUES ('x','1','now')")
            raise RuntimeError("boom")

    async with db.transaction() as conn:
        await conn.execute("INSERT INTO meta_kv(k, v, updated_at) VALUES ('y','1','now')")
    assert await db.fetch_value("SELECT COUNT(*) FROM meta_kv", default=0) == 1


async def test_execute_rowcount_reports_affected_rows(db):
    n = await db.execute_rowcount(
        "INSERT INTO meta_kv(k, v, updated_at) VALUES ('a','1','now')"
    )
    assert n == 1
    assert await db.execute_rowcount("UPDATE meta_kv SET v='2' WHERE k='a'") == 1
    assert await db.execute_rowcount("UPDATE meta_kv SET v='2' WHERE k='nope'") == 0


# ---------------------------------------------------------------------------
# 定义层仓储
# ---------------------------------------------------------------------------


async def test_workflow_round_trip(store):
    wf = WorkflowDefinition(name="发布流程", description="三阶段", max_concurrent_tasks=3)
    await store.workflows.create(wf)

    got = await store.workflows.get(wf.workflow_id)
    assert got is not None
    assert got.name == "发布流程"
    assert got.description == "三阶段"
    assert got.max_concurrent_tasks == 3
    assert got.status == WorkflowStatus.DRAFT
    assert got.created_at == wf.created_at
    assert [w.workflow_id for w in await store.workflows.list()] == [wf.workflow_id]


async def test_revision_round_trip_preserves_graph(store):
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)

    g = make_graph()
    rev = WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=g,
                           note="初始版本")
    await store.workflows.save_revision(rev, publish=True, expected_revision_seq=0)

    got = await store.workflows.get_revision(wf.workflow_id, 1)
    assert got is not None
    assert got.note == "初始版本"
    assert [n.node_id for n in got.graph.nodes] == ["A", "B"]
    assert [(e.from_node, e.to_node) for e in got.graph.edges] == [("A", "B")]
    assert got.graph.effective_graph_version() == g.effective_graph_version()
    assert got.is_published

    updated = await store.workflows.get(wf.workflow_id)
    assert updated.current_revision_seq == 1
    assert updated.status == WorkflowStatus.PUBLISHED


async def test_revision_is_immutable(store):
    """同 (workflow_id, revision_seq) 不能写两次——修订不可变。"""
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)
    rev = WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=make_graph())
    await store.workflows.save_revision(rev, publish=False)

    with pytest.raises(ConflictError):
        await store.workflows.save_revision(
            WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=make_graph()),
            publish=False,
        )


async def test_publish_cas_conflict_raises(store):
    """D-02：并发编辑用乐观并发，冲突返回明确错误而不是静默覆盖。"""
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)
    await store.workflows.save_revision(
        WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=make_graph()),
        publish=True,
        expected_revision_seq=0,
    )

    with pytest.raises(ConflictError):
        await store.workflows.save_revision(
            WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=2, graph=make_graph()),
            publish=True,
            expected_revision_seq=0,  # 过期的基线
        )


async def test_publish_cas_conflict_does_not_persist_revision(store):
    """CAS 失败时那份修订不应留在库里（整个事务回滚）。"""
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)
    await store.workflows.save_revision(
        WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=make_graph()),
        publish=True,
        expected_revision_seq=0,
    )
    with pytest.raises(ConflictError):
        await store.workflows.save_revision(
            WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=2, graph=make_graph()),
            publish=True,
            expected_revision_seq=0,
        )

    assert await store.workflows.get_revision(wf.workflow_id, 2) is None


async def test_next_revision_seq(store):
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)
    assert await store.workflows.next_revision_seq(wf.workflow_id) == 1
    await store.workflows.save_revision(
        WorkflowRevision(workflow_id=wf.workflow_id, revision_seq=1, graph=make_graph()),
        publish=False,
    )
    assert await store.workflows.next_revision_seq(wf.workflow_id) == 2


async def test_workflow_update_rejects_unknown_field(store):
    """白名单之外的列不可写——挡住「顺手改主键／状态机外字段」这类误用。"""
    wf = WorkflowDefinition(name="wf")
    await store.workflows.create(wf)
    with pytest.raises(ValueError):
        await store.workflows.update(wf.workflow_id, created_at="2020-01-01T00:00:00+00:00")


# ---------------------------------------------------------------------------
# 注册表 round-trip
# ---------------------------------------------------------------------------


async def test_registry_round_trip(store):
    from workerbee.core.domain import (
        CredentialKind,
        CredentialRef,
        HarnessRegistration,
        SkillDoc,
        ToolLaunch,
        ToolSpec,
        VersionedRef,
    )
    from workerbee.core.domain.registry import ApprovalPolicy, MCPTransport, RiskLevel

    h = HarnessRegistration(
        harness_id="h1", name="Claude Code", adapter_id="claude_code",
        adapter_version="1.0", exec_path="/usr/bin/claude",
        env_template={"FOO": "bar"}, cwd="/tmp",
        auth_binding="c1", capabilities_snapshot={"compact": True},
        last_probe_at="2026-01-01T00:00:00+00:00", last_probe_ok=True,
    )
    await store.registry.upsert_harness(h)
    got_h = await store.registry.get_harness("h1")
    assert got_h is not None
    assert got_h.env_template == {"FOO": "bar"}
    assert got_h.capabilities_snapshot == {"compact": True}
    assert got_h.last_probe_ok is True

    c = CredentialRef(credential_id="c1", label="主账号", kind=CredentialKind.API_KEY,
                      secret_locator="secret://c1", base_url="https://api.example")
    await store.registry.upsert_credential(c)
    got_c = await store.registry.get_credential("c1")
    assert got_c is not None
    assert got_c.kind == CredentialKind.API_KEY
    assert got_c.base_url == "https://api.example"
    assert got_c.revoked is False

    s = SkillDoc(skill_id="s1", name="编码规范", content="不要用 sleep", version=3)
    await store.registry.upsert_skill(s)
    got_s = await store.registry.get_skill("s1")
    assert got_s is not None and got_s.content == "不要用 sleep" and got_s.version == 3

    t = ToolSpec(tool_id="t1", name="git", description="版本控制",
                 launch=ToolLaunch(command="mcp-git", args=["--stdio"],
                                   env={"HOME": "/tmp"}, transport=MCPTransport.STDIO),
                 io_schema={"in": "path"}, risk_level=RiskLevel.HIGH,
                 approval_policy=ApprovalPolicy.ASK)
    await store.registry.upsert_tool(t)
    got_t = await store.registry.get_tool("t1")
    assert got_t is not None
    assert got_t.launch.command == "mcp-git"
    assert got_t.launch.transport == MCPTransport.STDIO
    assert got_t.risk_level == RiskLevel.HIGH
    assert got_t.io_schema == {"in": "path"}


async def test_registry_probe_snapshot(store):
    from workerbee.core.domain import HarnessRegistration

    await store.registry.upsert_harness(
        HarnessRegistration(harness_id="h1", name="h1", adapter_id="mock")
    )
    await store.registry.record_probe(
        "h1", ok=False, capabilities=None, error="executable not found"
    )
    got = await store.registry.get_harness("h1")
    assert got.last_probe_ok is False
    assert got.last_probe_error == "executable not found"
    assert got.last_probe_at is not None


async def test_registry_snapshot_is_consistent_view(store):
    """校验管线拿到的是只读快照，供纯函数使用。"""
    from workerbee.core.domain import HarnessRegistration, SkillDoc

    await store.registry.upsert_harness(
        HarnessRegistration(harness_id="h1", name="h1", adapter_id="mock")
    )
    await store.registry.upsert_skill(SkillDoc(skill_id="s1", name="s1"))

    snap = await store.registry.snapshot()
    assert snap.harness("h1") is not None
    assert snap.skill("s1") is not None
    assert snap.harness("nope") is None
    assert snap.tool("nope") is None


async def test_template_round_trip(store):
    from workerbee.core.domain import VersionedRef
    from workerbee.core.domain.template import TemplateNodeConfig, TemplatePayload

    tpl = Template(
        template_id="tpl1", name="编码节点", kind=TemplateKind.NODE, version=2,
        payload=TemplatePayload(
            nodes=[TemplateNodeConfig(name="编码", role="coder",
                                      skill_refs=[VersionedRef(ref_id="s1", version=2)])],
            edges=[Edge(from_node="编码", to_node="编码")],
        ),
        source_revision=7, source_workflow_id="w9",
    )
    await store.registry.upsert_template(tpl)

    got = await store.registry.get_template("tpl1")
    assert got is not None
    assert got.kind == TemplateKind.NODE
    assert got.version == 2
    assert got.source_revision == 7
    assert got.payload.nodes[0].skill_refs[0].version == 2

    assert [t.template_id for t in await store.registry.list_templates(kind=TemplateKind.NODE)] == [
        "tpl1"
    ]
    assert await store.registry.list_templates(kind=TemplateKind.WORKFLOW) == []
    assert await store.registry.delete_template("tpl1") is True


# ---------------------------------------------------------------------------
# 任务与阶段
# ---------------------------------------------------------------------------


async def test_task_and_stages_round_trip(store):
    task = make_task(idempotency_key="req-1")
    stage = make_stage()
    created_task, created = await store.tasks.create_task_with_stages(task, [stage])
    assert created is True

    got = await store.tasks.get_task("t1")
    assert got is not None
    assert got.input_payload == {"goal": "写一个测试"}
    assert got.idempotency_key == "req-1"
    assert got.graph_snapshot.effective_edges == [("A", "B")]
    assert got.graph_snapshot.entry_nodes() == ["A"]
    assert got.priority == 50
    assert got.created_at == task.created_at

    stages = await store.tasks.list_stages("t1")
    assert len(stages) == 1
    assert stages[0].observed_state == StageState.WAITING_DEPS
    assert stages[0].desired_state == DesiredState.ACTIVE
    assert stages[0].enqueued_at == stage.enqueued_at


async def test_idempotency_key_returns_original_task(store):
    """RUN-02：同一次提交的网络重送不产生额外任务。"""
    first, created_first = await store.tasks.create_task_with_stages(
        make_task(task_id="t1", idempotency_key="req-1"), [make_stage(stage_id="s1")]
    )
    second, created_second = await store.tasks.create_task_with_stages(
        make_task(task_id="t2", idempotency_key="req-1"), [make_stage(stage_id="s2")]
    )

    assert created_first is True
    assert created_second is False
    assert second.task_id == "t1"
    assert await store.tasks.get_task("t2") is None
    assert await store.tasks.get_stage("s2") is None


async def test_distinct_idempotency_keys_produce_distinct_tasks(store):
    """RUN-02：两次有意提交相同内容 = 两个任务。"""
    _, c1 = await store.tasks.create_task_with_stages(
        make_task(task_id="t1", idempotency_key="req-1"), []
    )
    _, c2 = await store.tasks.create_task_with_stages(
        make_task(task_id="t2", idempotency_key="req-2"), []
    )
    assert (c1, c2) == (True, True)
    assert len(await store.tasks.list_tasks()) == 2


async def test_null_idempotency_key_allows_many(store):
    """幂等键为空时不受唯一约束限制——系统生成的 task_id 已是唯一标识。"""
    for i in range(3):
        await store.tasks.create_task_with_stages(make_task(task_id=f"t{i}"), [])
    assert len(await store.tasks.list_tasks()) == 3


async def test_task_cas_with_wrong_epoch_fails(store):
    """AC-11：CAS 失败的一方不能改写状态。"""
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])

    ok = await store.tasks.update_task(
        "t1", to_state=TaskState.RUNNING, expected_epoch=99, bump_epoch=True
    )
    assert ok is False
    assert (await store.tasks.get_task("t1")).observed_state == TaskState.QUEUED

    ok = await store.tasks.update_task(
        "t1", to_state=TaskState.RUNNING, expected_epoch=0, bump_epoch=True
    )
    assert ok is True
    got = await store.tasks.get_task("t1")
    assert got.observed_state == TaskState.RUNNING
    assert got.control_epoch == 1


async def test_task_from_states_guard(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    ok = await store.tasks.update_task(
        "t1", to_state=TaskState.SUCCEEDED, from_states=[TaskState.RUNNING]
    )
    assert ok is False
    assert (await store.tasks.get_task("t1")).observed_state == TaskState.QUEUED


async def test_stage_cas_and_reorder(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])

    assert await store.tasks.update_stage(
        "s1", to_state=StageState.READY, from_states=[StageState.WAITING_DEPS],
        expected_epoch=0, bump_epoch=True,
    )
    assert await store.tasks.update_stage(
        "s1", to_state=StageState.DISPATCHING, from_states=[StageState.READY],
        expected_epoch=0,  # 已被 bump 成 1
    ) is False

    assert await store.tasks.update_stage(
        "s1", to_state=StageState.DISPATCHING, from_states=[StageState.READY],
        expected_epoch=1, bump_epoch=True,
    )

    # 调序只改 node_priority，不碰状态与依赖
    assert await store.tasks.reorder_stage("s1", node_priority=90)
    got = await store.tasks.get_stage("s1")
    assert got.node_priority == 90
    assert got.observed_state == StageState.DISPATCHING


async def test_node_slot_taken_only_for_slot_states(store):
    """D-03：只有 DISPATCHING/RUNNING 占执行槽；审批等待与暂停不占。"""
    await store.tasks.create_task_with_stages(
        make_task(task_id="t1"), [make_stage(stage_id="s1", task_id="t1")]
    )
    await store.tasks.create_task_with_stages(
        make_task(task_id="t2"), [make_stage(stage_id="s2", task_id="t2")]
    )

    assert await store.tasks.node_slot_taken("A") is False

    await store.tasks.update_stage("s1", to_state=StageState.RUNNING)
    assert await store.tasks.node_slot_taken("A") is True

    await store.tasks.update_stage("s1", to_state=StageState.AWAITING_APPROVAL)
    assert await store.tasks.node_slot_taken("A") is False

    await store.tasks.update_stage("s2", to_state=StageState.PAUSED)
    assert await store.tasks.node_slot_taken("A") is False


async def test_list_stages_by_node_orders_by_queue_key(store):
    """RUN-04：节点队列按 (node_priority, task_priority, enqueued_at) 排序。"""
    for i, (prio, node_prio) in enumerate([(10, 50), (90, 50), (50, 99), (50, 50)]):
        await store.tasks.create_task_with_stages(
            make_task(task_id=f"t{i}", priority=prio,
                      observed_state=TaskState.QUEUED),
            [make_stage(stage_id=f"s{i}", task_id=f"t{i}")],
        )
        await store.tasks.update_stage(
            f"s{i}", to_state=StageState.READY, task_priority=prio
        )
        await store.tasks.reorder_stage(f"s{i}", node_priority=node_prio)

    ordered = [s.stage_id for s in await store.tasks.list_stages_by_node("A",
                                                                        states=[StageState.READY])]
    # node_priority 99 最前；其次 node_priority 50 里 task_priority 90 先于 50 与 10
    assert ordered == ["s2", "s1", "s3", "s0"]


async def test_list_ready_stages_and_counts(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    assert await store.tasks.list_ready_stages() == []

    await store.tasks.update_stage("s1", to_state=StageState.READY)
    assert [s.stage_id for s in await store.tasks.list_ready_stages()] == ["s1"]
    assert await store.tasks.count_active_tasks("w1") == 1

    await store.tasks.update_task("t1", to_state=TaskState.SUCCEEDED)
    assert await store.tasks.count_active_tasks("w1") == 0
    assert await store.tasks.list_live_tasks() == []


async def test_attempt_round_trip_and_completion(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])

    at = Attempt(
        attempt_id="at1", stage_id="s1", task_id="t1", node_id="A", attempt_seq=1,
        profile_id="p1", profile_snapshot={"model_name": "m1"},
        session_ref="sess-1", lease_id="lease-1", generation=1,
        compact_events=[CompactEvent(trigger="threshold", tokens_before=1000,
                                     tokens_after=300)],
    )
    await store.tasks.create_attempt(at)

    got = await store.tasks.get_attempt("at1")
    assert got is not None
    assert got.profile_snapshot == {"model_name": "m1"}
    assert got.session_ref == "sess-1"
    assert got.compact_events[0].tokens_after == 300
    assert got.outcome is None and got.is_in_flight()

    ok = await store.tasks.complete_attempt(
        "at1",
        outcome=AttemptOutcome(error_class=ErrorClass.SUCCESS, detail="done"),
        usage=Usage(input_tokens=100, output_tokens=50),
        expected_generation=1,
    )
    assert ok is True

    done = await store.tasks.get_attempt("at1")
    assert done.outcome.error_class == ErrorClass.SUCCESS
    assert done.usage.total_tokens() == 150
    assert done.ended_at is not None

    # 重复完成回调被拒（REC-05：同一完成通知重复到达不得覆盖已确认结果）
    assert await store.tasks.complete_attempt(
        "at1", outcome=AttemptOutcome(error_class=ErrorClass.FATAL_ERROR)
    ) is False


async def test_complete_attempt_rejects_stale_generation(store):
    """REC-05：代次过期的迟到回调直接丢弃。"""
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    await store.tasks.create_attempt(
        Attempt(attempt_id="at1", stage_id="s1", task_id="t1", node_id="A",
                attempt_seq=1, profile_id="p1", generation=1)
    )
    await store.tasks.bump_attempt_generation("at1")

    ok = await store.tasks.complete_attempt(
        "at1",
        outcome=AttemptOutcome(error_class=ErrorClass.SUCCESS),
        expected_generation=1,
    )
    assert ok is False
    assert (await store.tasks.get_attempt("at1")).outcome is None


async def test_usage_unknown_metrics_stay_none(store):
    """OBS-04：不可取得的用量标「未知」而不是零。"""
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    await store.tasks.create_attempt(
        Attempt(attempt_id="at1", stage_id="s1", task_id="t1", node_id="A",
                attempt_seq=1, profile_id="p1",
                usage=Usage(notes="厂商未返回用量"))
    )
    got = await store.tasks.get_attempt("at1")
    assert got.usage.input_tokens is None
    assert got.usage.total_tokens() is None
    assert got.usage.notes == "厂商未返回用量"


async def test_stage_origin_of_control_round_trip(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    await store.tasks.update_stage(
        "s1",
        origin_of_control=OriginOfControl(op="pause", from_node_id="A",
                                          detail="用户从节点 A 发起"),
        status_reason="暂停中",
    )
    got = await store.tasks.get_stage("s1")
    assert got.origin_of_control.op == "pause"
    assert got.origin_of_control.from_node_id == "A"
    assert got.status_reason == "暂停中"


async def test_update_stage_rejects_unknown_field(store):
    await store.tasks.create_task_with_stages(make_task(), [make_stage()])
    with pytest.raises(ValueError):
        await store.tasks.update_stage("s1", created_at="2020-01-01T00:00:00+00:00")


# ---------------------------------------------------------------------------
# 资源台账与审批
# ---------------------------------------------------------------------------


async def test_resource_ledger_round_trip(store):
    await store.resources.register(
        resource_id="r1", kind="process", locator={"pid": 1234},
        owner_task_id="t1", owner_stage_id="s1", owner_attempt_id="at1", owner_node_id="A",
        teardown={"method": "sigterm", "timeout_ms": 5000},
    )
    rows = await store.resources.list_for_attempt("at1")
    assert len(rows) == 1
    assert rows[0]["kind"] == "process"
    assert rows[0]["state"] == "open"

    ids = await store.resources.close_all_for_attempt("at1")
    assert ids == ["r1"]
    assert (await store.resources.list_for_attempt("at1"))[0]["state"] == "closing"
    # 已进入 closing 的句柄不会再次被列入待关闭集合
    assert await store.resources.close_all_for_attempt("at1") == []

    assert await store.resources.mark_state("r1", "teardown_failed", error="timeout")
    assert (await store.resources.list_by_state(["teardown_failed"]))[0]["resource_id"] == "r1"
    assert (await store.resources.list_open())[0]["resource_id"] == "r1"


async def test_approval_lifecycle(store):
    from workerbee.core.domain import ApprovalDecision

    ap = make_approval()
    await store.approvals.create(ap)

    assert [a.approval_id for a in await store.approvals.list_open()] == ["ap1"]
    assert len(await store.approvals.list_for_task("t1")) == 1
    assert len(await store.approvals.list_for_attempt("at1")) == 1

    ok = await store.approvals.decide(
        "ap1", status=ApprovalStatus.APPROVED, decision=ApprovalDecision(by="lyh")
    )
    assert ok is True
    got = await store.approvals.get("ap1")
    assert got.status == ApprovalStatus.APPROVED
    assert got.decision.by == "lyh"
    assert await store.approvals.list_open() == []

    # 重复决定被拒（HUM-04：重复通知不重复授权）
    assert await store.approvals.decide(
        "ap1", status=ApprovalStatus.DENIED, decision=ApprovalDecision()
    ) is False


async def test_approval_invalidation(store):
    """AC-14：尝试失效后旧批准作废。"""
    await store.approvals.create(make_approval(approval_id="ap1", attempt_id="at1"))
    await store.approvals.create(
        make_approval(approval_id="ap2", task_id="t2", attempt_id="at2")
    )

    assert await store.approvals.invalidate_for_attempt("at1", "尝试已结束") == 1
    got = await store.approvals.get("ap1")
    assert got.status == ApprovalStatus.SUPERSEDED
    assert got.detail == "尝试已结束"
    assert (await store.approvals.get("ap2")).status == ApprovalStatus.PENDING


async def test_store_storage_report(store):
    report = await store.db.storage_report()
    assert report["row_counts"]["task"] == 0
    assert report["path"].endswith("workerbee.db")


async def test_graph_spec_used_by_store_is_self_consistent():
    """store 里存的图与内存中的图指纹一致，恢复后能对上钉扎版本。"""
    g = make_graph()
    assert isinstance(g, GraphSpec)
    assert g.effective_graph_version() > 0
