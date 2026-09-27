"""凭据库 → 脱敏器的接线（AUTH-02）。

这组测试盯的是**一类不会报错的 bug**：`bind_store` 是协程，漏掉 await 不会抛异常，
只会让脱敏器永远登记不到密值——表面一切正常，直到某天一个**不含可识别前缀**的
密钥原样出现在事件历史里。所以这里刻意用一个「长得完全不像密钥」的随机串：
只靠正则兜底是抓不住它的，必须真的登记过才会被替换。

同类风险还有一处：库不存在时抛的是 `StoreNotFoundError`（不是 `FileNotFoundError`），
捕错异常会让**首次带口令启动**直接失败。也在下面钉住。
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from workerbee.app import Engine, EngineConfig
from workerbee.data.event_log import EventActor, EventScope, EventType
from workerbee.security.secret_store import SecretStore

pytestmark = pytest.mark.integration

RANDOM_SECRET = "Kx7" + secrets.token_hex(16)  # 无前缀、无形状，正则抓不到


async def _engine(tmp_path: Path) -> Engine:
    return await Engine.create(
        EngineConfig(data_dir=tmp_path, use_summarizer=False)
    )


async def test_first_unlock_creates_vault_instead_of_failing(tmp_path: Path):
    """首次启动：库不存在应当**建库**，而不是抛异常。

    这条曾经是坏的：捕 `FileNotFoundError` 而实际抛的是 `StoreNotFoundError`，
    于是第一次带 `--passphrase` 启动必然失败。
    """
    engine = await _engine(tmp_path)
    n = await engine.unlock_secrets("correct horse battery staple")
    assert n == 0
    assert (tmp_path / "secrets.vault").exists()
    await engine.stop()


async def test_random_shaped_secret_is_actually_registered(tmp_path: Path):
    """无前缀的密值必须被真正登记，而不是靠正则兜底。

    这是「漏 await」唯一能被抓住的地方：一个不像密钥的串，只有登记过才会被替换。
    """
    engine = await _engine(tmp_path)
    await engine.unlock_secrets("pw-1")

    await engine.secret_store.put("svc-a", {"api_key": RANDOM_SECRET})

    # 重新解锁一次，让新写入的值进入脱敏器（写入 → 登记是两步）
    await engine.lock_secrets()
    n = await engine.unlock_secrets("pw-1")
    assert n >= 1, "重新解锁后必须登记到至少一个密值"

    await engine.store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.SYSTEM_START,
        actor=EventActor.USER,
        payload={"diagnostic": f"密钥是 {RANDOM_SECRET}，不该出现在历史里"},
    )
    tail = await engine.store.events.tail(limit=50)
    blob = repr(tail)
    assert RANDOM_SECRET not in blob, "密值原样进入了事件历史——脱敏器没登记到它"
    assert "***" in blob, "敏感位置应当被替换成掩码"
    await engine.stop()


async def test_lock_clears_registered_values(tmp_path: Path):
    """锁定后脱敏器不得再持有明文（内存里也不该留）。"""
    engine = await _engine(tmp_path)
    await engine.unlock_secrets("pw-2")
    await engine.secret_store.put("svc-b", {"token": RANDOM_SECRET})
    await engine.lock_secrets()

    assert engine.secret_store is None
    redactor = engine._redactor
    assert redactor is not None
    assert redactor.bound_count == 0, "锁库后登记的密值必须清空"

    await engine.store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.SYSTEM_START,
        actor=EventActor.USER,
        payload={"diagnostic": RANDOM_SECRET},
    )
    tail = await engine.store.events.tail(limit=50)
    # 锁库后不再登记，因此这条字符串会原样出现——这不是漏洞，是「未解锁就不脱敏」
    # 的如实结果。真正要保证的是：解锁时必须登记（上一条测试），锁定时必须清空（本条）。
    assert engine._redactor.bound_count == 0
    _ = tail
    await engine.stop()


async def test_wrong_passphrase_raises(tmp_path: Path):
    """口令错误必须原样上抛——那不是「降级」，是用户必须知道的事。"""
    engine = await _engine(tmp_path)
    await engine.unlock_secrets("right")
    await engine.lock_secrets()
    await engine.stop()

    engine2 = await _engine(tmp_path)
    from workerbee.security.secret_store import PassphraseError

    with pytest.raises(PassphraseError):
        await engine2.unlock_secrets("wrong")
    await engine2.stop()


async def test_task_input_is_not_silently_redacted(tmp_path: Path):
    """任务输入**不**做脱敏——那是模型要用的正文，改了就改变了用户的任务。

    这条记录一个刻意的取舍：入参原样保存与原样回显（用户自己写的东西回显给
    同一个人不构成泄漏），凭据不外泄靠的是「不把凭据塞进入参」这条使用纪律
    与出站扫描，而不是偷偷改写用户的输入。
    """
    from workerbee.core.domain import (
        ExecutionProfile,
        GraphSpec,
        HarnessRegistration,
        NodeDefinition,
        WorkflowDefinition,
        WorkflowRevision,
    )
    from workerbee.core.runtime.launch import launch_task

    engine = await _engine(tmp_path)
    await engine.store.registry.upsert_harness(
        HarnessRegistration(harness_id="h1", name="h1", adapter_id="mock", last_probe_ok=True)
    )
    wf = WorkflowDefinition(name="wf")
    await engine.store.workflows.create(wf)
    await engine.store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=GraphSpec(
                nodes=[
                    NodeDefinition(
                        node_id="A", name="A",
                        profiles=[ExecutionProfile(profile_id="p1", harness_ref="h1")],
                    )
                ],
                edges=[],
            ),
            is_published=True,
        ),
        publish=True,
        expected_revision_seq=0,
    )

    payload = {"task": f"用户自己写的正文，含 {RANDOM_SECRET} 这样的串"}
    result = await launch_task(
        store=engine.store, workflow_id=wf.workflow_id, input_payload=payload
    )
    task = await engine.store.tasks.get_task(result.task.task_id)
    assert task.input_payload == payload, "入参必须原样保留，框架不改写用户的任务"

    # 但它**不能**被写进事件日志——那是「普通日志」的范畴（AUTH-02）
    events = await engine.store.events.for_task(result.task.task_id)
    assert RANDOM_SECRET not in repr(events), "入参正文不得进入事件历史"
    await engine.stop()


async def test_secret_store_round_trip_through_engine(tmp_path: Path):
    """经组合根拿到的凭据库能正常读写——证明接线没有半途断掉。"""
    engine = await _engine(tmp_path)
    await engine.unlock_secrets("pw-3")
    await engine.secret_store.put("svc-c", {"api_key": RANDOM_SECRET})
    got = await engine.secret_store.get("svc-c")
    assert got == {"api_key": RANDOM_SECRET}
    await engine.stop()

    # 重新打开同一个库
    store = await SecretStore.open("pw-3", str(tmp_path / "secrets.vault"))
    assert await store.get("svc-c") == {"api_key": RANDOM_SECRET}
    await store.lock()
