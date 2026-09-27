"""append-only 事件日志（§5.4、D-08、AUTH-02）。

两条纪律各有对应测试：只追加不更新、落库前脱敏。另外钉住「写前日志」的原子性——
事件在事务内追加时，必须与状态同生共死，否则崩溃后会留下
「日志说有、状态说没」的假记录，对账就失去依据。
"""

from __future__ import annotations

import pytest

from workerbee.data.event_log import EventActor, EventScope, EventType

pytestmark = pytest.mark.integration


async def _append(store, **kw):
    params = dict(
        scope=EventScope.TASK,
        type=EventType.TASK_SUBMITTED,
        actor=EventActor.USER,
        task_id="t1",
    )
    params.update(kw)
    return await store.events.append(**params)


async def test_append_and_read_back(store):
    await _append(store, payload={"goal": "写测试"})

    events = await store.events.for_task("t1")
    assert len(events) == 1
    assert events[0]["type"] == "task.submitted"
    assert events[0]["actor"] == "user"
    assert events[0]["payload"] == {"goal": "写测试"}
    assert events[0]["ts"]


async def test_standalone_append_is_committed_immediately(store):
    """没有外层事务时，事件必须自己落地——否则后续 BEGIN 会被隐式事务挡住。"""
    await _append(store)
    assert await store.events.count_for_task("t1") == 1

    # 关键回归：紧接着开一个写事务必须成功
    async with store.db.transaction() as conn:
        await conn.execute("INSERT INTO meta_kv(k,v,updated_at) VALUES ('a','1','now')")
    assert await store.db.fetch_value("SELECT v FROM meta_kv WHERE k='a'") == "1"


async def test_event_rolls_back_with_outer_transaction(store):
    """写前日志的原子性：状态回滚时那条日志也必须一起回滚。"""
    with pytest.raises(RuntimeError):
        async with store.db.transaction():
            await store.events.append(
                scope=EventScope.TASK,
                type=EventType.TASK_CONTROL,
                actor=EventActor.USER,
                task_id="t1",
                payload={"op": "pause"},
            )
            raise RuntimeError("状态迁移失败")

    assert await store.events.count_for_task("t1") == 0


async def test_event_commits_with_outer_transaction(store):
    async with store.db.transaction():
        await _append(store)

    assert await store.events.count_for_task("t1") == 1


async def test_by_scope(store):
    await _append(store, scope=EventScope.WORKFLOW, scope_id="w1", task_id=None)
    await _append(store, scope=EventScope.TASK, scope_id="t1")

    assert len(await store.events.by_scope(EventScope.WORKFLOW, "w1")) == 1
    assert len(await store.events.by_scope(EventScope.TASK, "t1")) == 1
    assert await store.events.by_scope(EventScope.WORKFLOW, "w2") == []


async def test_for_task_filters_by_task(store):
    await _append(store, task_id="t1")
    await _append(store, task_id="t2")

    assert len(await store.events.for_task("t1")) == 1
    assert len(await store.events.for_task("t2")) == 1


async def test_tail_returns_incremental_slice(store):
    """增量拉取是监控与 WebSocket 推送的基础（OBS-02）。"""
    ids = [await _append(store) for _ in range(3)]
    assert ids == sorted(ids)

    first_page = await store.events.tail(after_id=0, limit=2)
    assert [e["event_id"] for e in first_page] == ids[:2]

    second_page = await store.events.tail(after_id=first_page[-1]["event_id"])
    assert [e["event_id"] for e in second_page] == ids[2:]

    assert await store.events.latest_id() == ids[-1]


async def test_refs_and_stage_are_recorded(store):
    await _append(store, stage_id="s1", refs=["artifact:a1", "artifact:a2"])
    event = (await store.events.for_task("t1"))[0]
    assert event["stage_id"] == "s1"
    assert event["refs"] == ["artifact:a1", "artifact:a2"]


async def test_redactor_is_applied_before_persistence(store):
    """AUTH-02：凭据不得进入历史。脱敏在**落库前**发生。"""
    calls: list[dict] = []

    def redactor(payload):
        calls.append(payload)
        return {k: ("***" if "token" in k else v) for k, v in payload.items()}

    store.events.set_redactor(redactor)
    await _append(store, payload={"api_token": "sk-secret", "goal": "ok"})

    assert calls == [{"api_token": "sk-secret", "goal": "ok"}], "脱敏器必须被调用"

    stored = (await store.events.for_task("t1"))[0]["payload"]
    assert stored["api_token"] == "***"
    assert "sk-secret" not in str(stored)
    assert stored["goal"] == "ok"


async def test_empty_payload_is_stored_as_object(store):
    await _append(store, payload=None)
    assert (await store.events.for_task("t1"))[0]["payload"] == {}


async def test_prune_task_keeps_latest(store):
    for _ in range(5):
        await _append(store)

    assert await store.events.prune_task("t1", keep_last=2) == 3
    remaining = await store.events.for_task("t1")
    assert len(remaining) == 2
    assert remaining[-1]["event_id"] == await store.events.latest_id()


async def test_prune_task_removes_all_when_keep_last_zero(store):
    for _ in range(3):
        await _append(store)
    assert await store.events.prune_task("t1") == 3
    assert await store.events.for_task("t1") == []


async def test_prune_task_only_affects_that_task(store):
    await _append(store, task_id="t1")
    await _append(store, task_id="t2")

    await store.events.prune_task("t1")
    assert await store.events.for_task("t1") == []
    assert len(await store.events.for_task("t2")) == 1


async def test_prune_older_than(store):
    await _append(store)
    assert await store.events.prune_older_than("2999-01-01T00:00:00+00:00") == 1
    assert await store.events.count_for_task("t1") == 0


async def test_events_are_append_only_ids_monotonic(store):
    a = await _append(store)
    b = await _append(store)
    c = await _append(store)
    assert a < b < c


async def test_pruned_task_leaves_other_scopes_intact(store):
    await _append(store, scope=EventScope.SYSTEM, task_id=None,
                  type=EventType.SYSTEM_START)
    await _append(store, task_id="t1")
    await store.events.prune_task("t1")

    assert await store.events.for_task("t1") == []
    assert len(await store.events.by_scope(EventScope.SYSTEM, "")) == 0
    assert len(await store.events.tail()) == 1
