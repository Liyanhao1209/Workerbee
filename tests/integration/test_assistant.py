"""基础助手 REST 端点集成测试（AI-01，计划 §9）。

覆盖：配置读写 → 建线程 → 发消息 → 历史 → 整理前文 → 事件留痕 → 重启恢复。

纪律与 test_server.py 相同：httpx.ASGITransport 全程同一个事件循环；
**断言模型实际收到的 messages**（guidebook / 快照 / 历史都在里面），
密值不出现在任何响应体与事件里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from workerbee.app import Engine, EngineConfig
from workerbee.data.event_log import EventType
from workerbee.security.secret_store import SecretStore
from workerbee.server.app import create_app
from workerbee.server.auth import TOKEN_HEADER

from tests.fakes import FakeHarness, FakeLLMBackend

pytestmark = pytest.mark.integration

TOKEN = "test-token-8f3c-not-a-secret"
PASSPHRASE = "test-passphrase-please-change"
SECRET = "sk-live-DEADBEEFdeadbeef0123456789"


# ===========================================================================
# 夹具
# ===========================================================================


@pytest.fixture
async def engine(tmp_path: Path) -> Any:
    config = EngineConfig(
        data_dir=tmp_path / ".workerbee",
        workspace_dir=tmp_path / "workspace",
        poll_interval=3600.0,
        reaper_interval=3600.0,
        use_summarizer=False,
        use_context_assembler=False,
    )
    eng = await Engine.create(config, harness=FakeHarness())
    yield eng
    await eng.store.close()


@pytest.fixture
async def backend(engine: Engine) -> FakeLLMBackend:
    """把助手的后端换成替身：绝不在集成测试里发真实 LLM 请求。"""
    fake = FakeLLMBackend("这是助手的回答。")
    engine.assistant.backend_factory = lambda cfg, cred, secrets: fake
    return fake


@pytest.fixture
async def client(engine: Engine) -> Any:
    app = create_app(engine, token=TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as c:
        yield c


async def _setup_credential_and_config(engine: Engine, client: Any) -> str:
    """解锁凭据库、建一条 base_url_pair 凭据、把它配给助手。返回 credential_id。"""
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    await engine.unlock_secrets(PASSPHRASE)

    created = await client.post(
        "/api/credentials",
        json={
            "label": "助手 API",
            "kind": "base_url_pair",
            "base_url": "https://llm.example.com/v1",
            "default_model": "gpt-test",
            "secret": {"api_key": SECRET, "base_url": "https://llm.example.com/v1"},
        },
    )
    assert created.status_code == 200, created.text
    credential_id = created.json()["credential_id"]

    updated = await client.put(
        "/api/assistant/config",
        json={"enabled": True, "credential_ref": credential_id},
    )
    assert updated.status_code == 200, updated.text
    assert SECRET not in updated.text
    return credential_id


# ===========================================================================
# 配置
# ===========================================================================


async def test_config_defaults_and_update(client: Any, engine: Engine) -> None:
    got = await client.get("/api/assistant/config")
    assert got.status_code == 200
    body = got.json()
    assert body["enabled"] is False
    assert body["credential_ref"] is None
    assert body["secrets_unlocked"] is False

    credential_id = await _setup_credential_and_config(engine, client)
    got = await client.get("/api/assistant/config")
    body = got.json()
    assert body["enabled"] is True
    assert body["credential_ref"] == credential_id
    assert body["secrets_unlocked"] is True
    assert SECRET not in got.text  # 密值只进不出


async def test_config_rejects_unknown_credential(client: Any) -> None:
    updated = await client.put(
        "/api/assistant/config", json={"credential_ref": "不存在的凭据"}
    )
    assert updated.status_code == 400
    assert "凭据" in updated.json()["detail"]


# ===========================================================================
# 问答全路径
# ===========================================================================


async def test_send_message_full_path(client: Any, engine: Engine, backend: FakeLLMBackend) -> None:
    await _setup_credential_and_config(engine, client)

    thread = (await client.post("/api/assistant/threads", json={"title": "试用"})).json()
    resp = await client.post(
        f"/api/assistant/threads/{thread['thread_id']}/messages",
        json={"content": "现在有什么要处理的？"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"]["content"] == "这是助手的回答。"
    assert body["message"]["backend"] == "fake-llm"
    assert body["message"]["degraded"] is False
    assert body["dropped"] == 0

    # —— 模型实际收到的 messages：角色设定 + guidebook + 快照 + 问题 ——
    assert len(backend.calls) == 1
    messages = backend.calls[0]
    assert messages[0].role == "system"
    system_text = messages[0].content
    assert "使用指引" in system_text
    assert "Workerbee" in system_text
    assert "系统当前状态快照" in system_text
    assert "凭据库已解锁：是" in system_text  # 快照真的反映了此刻状态
    assert messages[-1].role == "user"
    assert messages[-1].content == "现在有什么要处理的？"

    # 历史读回
    history = (
        await client.get(f"/api/assistant/threads/{thread['thread_id']}/messages")
    ).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]

    # 事件留痕：元信息在，正文不在
    events = (await client.get("/api/system/events?limit=200")).json()["events"]
    assistant_events = [
        e for e in events if e["type"] == EventType.ASSISTANT_MESSAGE.value
    ]
    assert len(assistant_events) == 1
    assert assistant_events[0]["payload"]["backend"] == "fake-llm"
    assert "这是助手的回答" not in json.dumps(assistant_events, ensure_ascii=False)


async def test_thread_listing(client: Any, engine: Engine) -> None:
    tid1 = (await client.post("/api/assistant/threads", json={"title": "一"})).json()
    tid2 = (await client.post("/api/assistant/threads", json={})).json()
    listing = (await client.get("/api/assistant/threads")).json()
    assert listing["returned"] == 2
    ids = {t["thread_id"] for t in listing["threads"]}
    assert ids == {tid1["thread_id"], tid2["thread_id"]}


async def test_missing_thread_is_404(client: Any) -> None:
    resp = await client.get("/api/assistant/threads/不存在的线程/messages")
    assert resp.status_code == 404
    resp = await client.post(
        "/api/assistant/threads/不存在的线程/messages", json={"content": "你好"}
    )
    assert resp.status_code == 404


async def test_empty_message_rejected(client: Any, engine: Engine) -> None:
    thread = (await client.post("/api/assistant/threads", json={})).json()
    resp = await client.post(
        f"/api/assistant/threads/{thread['thread_id']}/messages",
        json={"content": "   "},
    )
    assert resp.status_code == 400
    assert "空" in resp.json()["detail"]


# ===========================================================================
# 不可用的明确报错（不静默）
# ===========================================================================


async def test_not_enabled_gives_guidance(client: Any, engine: Engine) -> None:
    thread = (await client.post("/api/assistant/threads", json={})).json()
    resp = await client.post(
        f"/api/assistant/threads/{thread['thread_id']}/messages",
        json={"content": "你好"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "启用" in body["detail"]
    assert body["hint"]  # 有下一步指引


async def test_locked_vault_gives_guidance(client: Any, engine: Engine) -> None:
    """配置齐全但凭据库未解锁：明确引导解锁，不静默匿名调用。"""
    # 不经凭据库直接登记一条带 locator 的引用（模拟「上次解锁时建的」）
    from workerbee.core.domain.registry import CredentialKind, CredentialRef

    cred = CredentialRef(
        credential_id="cred-locked",
        label="助手 API",
        kind=CredentialKind.BASE_URL_PAIR,
        secret_locator="secret://cred-locked",
        base_url="https://llm.example.com/v1",
        default_model="gpt-test",
    )
    await engine.store.registry.upsert_credential(cred)
    await client.put(
        "/api/assistant/config", json={"enabled": True, "credential_ref": "cred-locked"}
    )

    thread = (await client.post("/api/assistant/threads", json={})).json()
    resp = await client.post(
        f"/api/assistant/threads/{thread['thread_id']}/messages",
        json={"content": "你好"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "解锁" in body["detail"]
    assert "passphrase" in body["hint"]


# ===========================================================================
# 整理前文
# ===========================================================================


async def test_compact_via_api(client: Any, engine: Engine, backend: FakeLLMBackend) -> None:
    await _setup_credential_and_config(engine, client)
    # 窗口调成 1 轮，好让旧消息落到窗口外
    await client.put("/api/assistant/config", json={"window_rounds": 1})

    backend._responses.extend(["早答", "近答", "整理：用户问了早问题"])
    thread = (await client.post("/api/assistant/threads", json={})).json()
    tid = thread["thread_id"]
    await client.post(f"/api/assistant/threads/{tid}/messages", json={"content": "早问题"})
    await client.post(f"/api/assistant/threads/{tid}/messages", json={"content": "近问题"})

    resp = await client.post(f"/api/assistant/threads/{tid}/compact")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["compacted"] is True
    assert body["summarized"] == 2

    # 整理材料里真的有窗口外原文
    assert "早问题" in backend.calls[-1][-1].content

    history = (await client.get(f"/api/assistant/threads/{tid}/messages")).json()
    roles = [m["role"] for m in history["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "memory"]

    events = (await client.get("/api/system/events?limit=200")).json()["events"]
    assert any(e["type"] == EventType.ASSISTANT_COMPACTED.value for e in events)


# ===========================================================================
# 密值不泄漏
# ===========================================================================


async def test_secret_never_leaks(client: Any, engine: Engine, backend: FakeLLMBackend) -> None:
    await _setup_credential_and_config(engine, client)
    thread = (await client.post("/api/assistant/threads", json={})).json()
    tid = thread["thread_id"]
    resp = await client.post(
        f"/api/assistant/threads/{tid}/messages",
        json={"content": f"我的密钥 {SECRET} 是不是配错了？"},
    )
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text

    # 模型收到的与库里存的都是脱敏后的
    assert SECRET not in backend.calls[0][-1].content
    history = (await client.get(f"/api/assistant/threads/{tid}/messages")).json()
    assert SECRET not in json.dumps(history, ensure_ascii=False)

    events = (await client.get("/api/system/events?limit=200")).json()
    assert SECRET not in json.dumps(events, ensure_ascii=False)


# ===========================================================================
# 重启恢复
# ===========================================================================


async def test_history_survives_restart(tmp_path: Path) -> None:
    """临时 data-dir 起 Engine、发消息、停掉、再起——历史原样读回。"""
    data_dir = tmp_path / ".workerbee"

    def make_config() -> EngineConfig:
        return EngineConfig(
            data_dir=data_dir,
            workspace_dir=tmp_path / "workspace",
            poll_interval=3600.0,
            reaper_interval=3600.0,
            use_summarizer=False,
            use_context_assembler=False,
        )

    engine1 = await Engine.create(make_config(), harness=FakeHarness())
    fake = FakeLLMBackend("重启前的回答。")
    engine1.assistant.backend_factory = lambda cfg, cred, secrets: fake
    app1 = create_app(engine1, token=TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app1),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as client1:
        # 助手 API 调用的 URL 形如 /chat/completions，fake 后端不会发真实请求。
        credential_id = await _setup_credential_and_config(engine1, client1)
        thread = (await client1.post("/api/assistant/threads", json={"title": "跨重启"})).json()
        sent = await client1.post(
            f"/api/assistant/threads/{thread['thread_id']}/messages",
            json={"content": "重启前的消息"},
        )
        assert sent.status_code == 200, sent.text

    await engine1.stop()  # 连库一起关，模拟真重启

    engine2 = await Engine.create(make_config(), harness=FakeHarness())
    fake2 = FakeLLMBackend("重启后的回答。")
    engine2.assistant.backend_factory = lambda cfg, cred, secrets: fake2
    app2 = create_app(engine2, token=TOKEN)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app2),
            base_url="http://localhost",
            headers={TOKEN_HEADER: TOKEN},
        ) as client2:
            # 配置跨重启保留
            config = (await client2.get("/api/assistant/config")).json()
            assert config["enabled"] is True
            assert config["credential_ref"] == credential_id

            # 历史原样读回
            history = (
                await client2.get(f"/api/assistant/threads/{thread['thread_id']}/messages")
            ).json()
            assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
            assert history["messages"][0]["content"] == "重启前的消息"
            assert history["messages"][1]["content"] == "重启前的回答。"

            # 重启后未解锁：发消息报明确错误，不静默匿名调用
            assert engine2.secret_store is None
            locked = await client2.post(
                f"/api/assistant/threads/{thread['thread_id']}/messages",
                json={"content": "重启后的消息"},
            )
            assert locked.status_code == 400
            assert "解锁" in locked.json()["detail"]

            # 解锁后继续聊，且带上了重启前的历史
            await engine2.unlock_secrets(PASSPHRASE)
            sent = await client2.post(
                f"/api/assistant/threads/{thread['thread_id']}/messages",
                json={"content": "重启后的消息"},
            )
            assert sent.status_code == 200, sent.text
            sent_contents = [m.content for m in fake2.calls[0] if m.role == "user"]
            assert sent_contents == ["重启前的消息", "重启后的消息"]
    finally:
        await engine2.stop()
