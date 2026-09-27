"""消息总线（§5.4、REC-05）。

投递语义是「至少一次 + 消费端按 dedup_key 幂等」，因此这里的核心断言是
**同一个 dedup_key 只能生效一次**，而不是「只会投一次」。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.message import Message, MessageType

pytestmark = pytest.mark.integration


def _msg(
    *,
    message_id: str = "m1",
    type_: MessageType = MessageType.DATA_READY,
    dedup_key: str | None = "d1",
    to_stage: str = "s1",
    task_id: str = "t1",
    payload: dict | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        type=type_,
        task_id=task_id,
        from_stage="s0",
        to_stage=to_stage,
        dedup_key=dedup_key,
        payload=payload,
    )


async def test_send_and_read_back(store):
    sent, created = await store.messages.send(_msg(payload={"artifact_id": "a1"}))
    assert created is True

    got = await store.messages.get(sent.message_id)
    assert got is not None
    assert got.type == MessageType.DATA_READY
    assert got.payload == {"artifact_id": "a1"}
    assert got.from_stage == "s0" and got.to_stage == "s1"


async def test_duplicate_dedup_key_returns_original(store):
    """重复通知不得入队第二条，也不得触发第二次副作用。"""
    first, c1 = await store.messages.send(_msg(message_id="m1", dedup_key="k1"))
    second, c2 = await store.messages.send(_msg(message_id="m2", dedup_key="k1"))

    assert (c1, c2) == (True, False)
    assert second.message_id == first.message_id
    assert await store.messages.get("m2") is None
    assert len(await store.messages.inbox("s1")) == 1


async def test_null_dedup_key_allows_many(store):
    for i in range(3):
        _, created = await store.messages.send(
            _msg(message_id=f"m{i}", dedup_key=None)
        )
        assert created is True
    assert len(await store.messages.inbox("s1")) == 3


async def test_exists(store):
    assert await store.messages.exists("k1") is False
    await store.messages.send(_msg(dedup_key="k1"))
    assert await store.messages.exists("k1") is True


async def test_claim_is_idempotent(store):
    """消费端幂等：同一个 dedup_key 只被认领一次，重放不会重复生效。"""
    await store.messages.send(_msg(dedup_key="k1"))

    assert await store.messages.claim("k1") is True
    assert await store.messages.claim("k1") is False
    assert await store.messages.claim("k1") is False


async def test_claimed_message_leaves_inbox(store):
    await store.messages.send(_msg(dedup_key="k1"))
    assert len(await store.messages.inbox("s1")) == 1

    await store.messages.claim("k1")
    assert await store.messages.inbox("s1") == []


async def test_mark_delivered_then_consumed(store):
    sent, _ = await store.messages.send(_msg(dedup_key="k1"))
    await store.messages.mark_delivered(sent.message_id)
    assert await store.messages.inbox("s1") == []  # 只在 pending 里找
    assert len(await store.messages.inbox("s1", states=("delivered",))) == 1

    await store.messages.mark_consumed(sent.message_id)
    assert await store.messages.inbox("s1", states=("delivered",)) == []


async def test_claim_only_matches_pending(store):
    """已投递但未消费的消息不能被 claim 抢走（状态机守卫）。"""
    sent, _ = await store.messages.send(_msg(dedup_key="k1"))
    await store.messages.mark_delivered(sent.message_id)
    assert await store.messages.claim("k1") is False


async def test_inbox_for_task_and_by_type(store):
    await store.messages.send(_msg(message_id="m1", dedup_key="k1"))
    await store.messages.send(
        _msg(message_id="m2", dedup_key="k2", type_=MessageType.FEEDBACK, task_id="t2")
    )

    assert len(await store.messages.inbox_for_task("t1")) == 1
    assert len(await store.messages.by_type(MessageType.FEEDBACK)) == 1
    assert await store.messages.by_type(MessageType.CANCEL) == []


async def test_purge_stage_marks_pending_dead(store):
    """阶段被清理后，未消费的投递要作废，避免迟到消息唤醒已结束的阶段。"""
    await store.messages.send(_msg(message_id="m1", dedup_key="k1", to_stage="s1"))
    await store.messages.send(_msg(message_id="m2", dedup_key="k2", to_stage="s2"))

    assert await store.messages.purge_stage("s1") == 1
    assert await store.messages.inbox("s1") == []
    assert len(await store.messages.inbox("s2")) == 1


async def test_purge_task(store):
    await store.messages.send(
        _msg(message_id="m1", dedup_key="k1", task_id="t1", to_stage="s1")
    )
    await store.messages.send(
        _msg(message_id="m2", dedup_key="k2", task_id="t2", to_stage="s2")
    )

    assert await store.messages.purge_task("t1") == 1
    # 消息记录本身保留（历史可回看），但状态已置 dead，不再被消费
    assert await store.messages.get("m1") is not None
    assert await store.messages.inbox("s1") == []
    assert len(await store.messages.inbox("s2")) == 1


async def test_prune_keeps_pending(store):
    sent1, _ = await store.messages.send(_msg(message_id="m1", dedup_key="k1"))
    sent2, _ = await store.messages.send(_msg(message_id="m2", dedup_key="k2"))
    await store.messages.mark_consumed(sent1.message_id)

    assert await store.messages.prune() == 1
    assert await store.messages.get(sent1.message_id) is None
    assert await store.messages.get(sent2.message_id) is not None


async def test_message_dedup_default(store):
    m = _msg(dedup_key=None)
    assert m.dedup() == f"{m.type.value}:{m.message_id}"
    m2 = _msg(dedup_key="k1")
    assert m2.dedup() == "k1"
