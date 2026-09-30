"""AssistantService 全流程（AI-01，计划 §6/§9）。

最重要的纪律：**断言模型实际收到的 messages 内容**——guidebook、系统快照、
对话历史都必须真的进了 prompt，只断言「返回了回复」不算数（这个项目最怕
「不报错但悄悄不生效」）。

假后端是 tests/fakes.py 的 FakeLLMBackend，绝不发真实网络请求。
"""

from __future__ import annotations

import pytest

from workerbee.assistant import (
    AssistantCallFailed,
    AssistantConfig,
    AssistantLocked,
    AssistantNotConfigured,
    AssistantService,
)
from workerbee.core.domain.registry import CredentialKind, CredentialRef
from workerbee.core.runtime.notifier import BroadcastNotifier
from workerbee.data.event_log import EventType
from workerbee.data.llm import LLMRouter, LLMTimeoutError

from tests.fakes import FakeLLMBackend

pytestmark = pytest.mark.unit


class FakeSecrets:
    """最小的 SecretResolver：按 locator 返回固定字段。"""

    def __init__(self, fields: dict | None = None) -> None:
        self.fields = fields if fields is not None else {"api_key": "sk-test"}

    async def get(self, locator: str):
        return self.fields


class FakeSnapshotSource:
    async def system_status(self):
        return {"secrets_unlocked": True, "counts": {}, "startup_notes": []}

    async def attention_items(self):
        return {"approvals": [], "failed_tasks": []}

    async def recent_tasks(self, limit):
        return [{"task_id": "task-001", "workflow_id": "w1", "workflow_name": "发版",
                 "observed_state": "failed", "failure_summary": {"summary": "节点超时"},
                 "blocked_reason": None}]

    async def registry_overview(self):
        return {"harnesses": [{"harness_id": "h1", "name": "Claude", "enabled": True,
                               "last_probe_ok": True}],
                "credential_count": 1, "skill_count": 0, "tool_count": 0}

    async def recent_error_events(self, limit):
        return []


async def make_service(
    store,
    *backends: FakeLLMBackend,
    secrets=None,
    credential: bool = True,
    enabled: bool = True,
    notifier=None,
) -> AssistantService:
    """装配一个可用的 AssistantService：凭据已登记、配置已写好、后端是替身。"""
    if credential:
        await store.registry.upsert_credential(
            CredentialRef(
                credential_id="cred-1",
                label="测试 API",
                kind=CredentialKind.BASE_URL_PAIR,
                secret_locator="secret://cred-1",
                base_url="https://llm.example.com/v1",
                default_model="gpt-test",
            )
        )
    service = AssistantService(
        store=store,
        notifier=notifier,
        secret_resolver=(lambda: secrets) if secrets is not None else (lambda: None),
        snapshot_source=FakeSnapshotSource(),
        backend_factory=(lambda cfg, cred, sec: backends[0] if len(backends) == 1
                         else LLMRouter(list(backends))),
    )
    if credential:
        await service.save_config(AssistantConfig(enabled=enabled, credential_ref="cred-1"))
    return service


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_full_flow_and_prompt_contents(store) -> None:
    backend = FakeLLMBackend("这是回答。")
    notifier = BroadcastNotifier()
    queue = notifier.subscribe()
    service = await make_service(store, backend, secrets=FakeSecrets(), notifier=notifier)

    thread = await service.create_thread(title="试用")
    result = await service.send_message(thread["thread_id"], "现在有什么任务失败了？")

    assert result["message"]["content"] == "这是回答。"
    assert result["message"]["backend"] == "fake-llm"
    assert result["dropped"] == 0

    # —— 关键断言：模型实际收到的 messages ——
    assert len(backend.calls) == 1
    messages = backend.calls[0]
    assert messages[0].role == "system"
    system_text = messages[0].content
    assert "使用指引" in system_text
    assert "Workerbee" in system_text  # guidebook 内容真的进去了
    assert "系统当前状态快照" in system_text
    assert "节点超时" in system_text  # 快照里的失败任务真的进去了
    assert messages[-1].role == "user"
    assert messages[-1].content == "现在有什么任务失败了？"

    # 历史落库：user + assistant 各一条
    history = await service.list_messages(thread["thread_id"])
    assert [m["role"] for m in history] == ["user", "assistant"]

    # 事件留痕（正文不进事件）
    events = await store.events.by_scope(*_assistant_scope(thread["thread_id"]))
    message_events = [e for e in events if e["type"] == EventType.ASSISTANT_MESSAGE.value]
    assert len(message_events) == 1
    assert message_events[0]["payload"]["backend"] == "fake-llm"
    assert "这是回答" not in str(message_events)

    # 推送
    notification = queue.get_nowait()
    assert notification.kind == "assistant_message"
    assert notification.payload["thread_id"] == thread["thread_id"]


async def test_history_is_sent_back_to_model(store) -> None:
    backend = FakeLLMBackend("第一答", "第二答")
    service = await make_service(store, backend, secrets=FakeSecrets())
    thread = await service.create_thread()

    await service.send_message(thread["thread_id"], "第一个问题")
    await service.send_message(thread["thread_id"], "第二个问题")

    second_call = backend.calls[1]
    contents = [(m.role, m.content) for m in second_call if m.role != "system"]
    assert contents == [
        ("user", "第一个问题"),
        ("assistant", "第一答"),
        ("user", "第二个问题"),
    ]


# ---------------------------------------------------------------------------
# 降级与失败
# ---------------------------------------------------------------------------


async def test_backend_fallback_is_visible(store) -> None:
    primary = FakeLLMBackend(LLMTimeoutError("主后端超时", backend="primary"), name="primary")
    backup = FakeLLMBackend("备用后端的回答", name="backup")
    service = await make_service(store, primary, backup, secrets=FakeSecrets())
    thread = await service.create_thread()

    result = await service.send_message(thread["thread_id"], "你好")
    assert result["message"]["backend"] == "backup"
    assert result["message"]["degraded"] is True
    assert result["degraded"] is True
    assert any("primary" in r for r in result["degraded_reasons"])

    events = await store.events.by_scope(*_assistant_scope(thread["thread_id"]))
    degraded = [e for e in events
                if e["type"] == EventType.ASSISTANT_BACKEND_DEGRADED.value]
    assert len(degraded) == 1
    assert degraded[0]["payload"]["fallback_from"] == "primary"


async def test_timeout_becomes_plain_error(store) -> None:
    backend = FakeLLMBackend(LLMTimeoutError("超时", backend="fake-llm"))
    service = await make_service(store, backend, secrets=FakeSecrets())
    thread = await service.create_thread()

    with pytest.raises(AssistantCallFailed) as info:
        await service.send_message(thread["thread_id"], "你好")
    assert info.value.hint  # 给出可操作的引导


async def test_not_enabled(store) -> None:
    service = await make_service(store, FakeLLMBackend("x"), secrets=FakeSecrets(),
                                 enabled=False)
    thread = await service.create_thread()
    with pytest.raises(AssistantNotConfigured, match="还没有启用"):
        await service.send_message(thread["thread_id"], "你好")


async def test_no_credential_ref(store) -> None:
    service = await make_service(store, FakeLLMBackend("x"), secrets=FakeSecrets())
    await service.save_config(AssistantConfig(enabled=True, credential_ref=None))
    thread = await service.create_thread()
    with pytest.raises(AssistantNotConfigured, match="凭据"):
        await service.send_message(thread["thread_id"], "你好")


async def test_locked_secret_store(store) -> None:
    """凭据库未解锁：明确报错引导解锁，不静默匿名调用。"""
    service = await make_service(store, FakeLLMBackend("x"), secrets=None)
    thread = await service.create_thread()
    with pytest.raises(AssistantLocked, match="解锁"):
        await service.send_message(thread["thread_id"], "你好")


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


async def test_user_message_is_redacted_before_store_and_send(store) -> None:
    from workerbee.security.secret_store import SecretRedactor

    secret = "sk-live-redactme0123456789"
    redactor = SecretRedactor([secret])
    backend = FakeLLMBackend(f"回答里也不该有 {secret}")
    service = await make_service(store, backend, secrets=FakeSecrets())
    service.redactor = redactor
    thread = await service.create_thread()

    result = await service.send_message(thread["thread_id"], f"我的 key 是 {secret}")

    # 模型收到的、库里存的、返回的——三处都不见明文
    assert secret not in str(backend.calls[0][-1].content)
    history = await service.list_messages(thread["thread_id"])
    assert secret not in str(history)
    assert secret not in result["message"]["content"]


# ---------------------------------------------------------------------------
# 整理前文
# ---------------------------------------------------------------------------


async def test_compact_summarizes_outside_window(store) -> None:
    backend = FakeLLMBackend("早答0", "早答1", "近答", "整理：用户问了早问题")
    service = await make_service(store, backend, secrets=FakeSecrets())
    await service.save_config(AssistantConfig(
        enabled=True, credential_ref="cred-1", window_rounds=1
    ))
    thread = await service.create_thread()

    await service.send_message(thread["thread_id"], "早问题0")
    await service.send_message(thread["thread_id"], "早问题1")
    await service.send_message(thread["thread_id"], "近问题")

    result = await service.compact_thread(thread["thread_id"])
    assert result["compacted"] is True
    assert result["summarized"] == 4  # 窗口外：早问题0/早答0/早问题1/早答1

    # 整理材料里真的有窗口外的原文
    compact_call = backend.calls[-1]
    assert "早问题0" in compact_call[-1].content
    assert "早问题1" in compact_call[-1].content

    # 整理后再发问：system prompt 带上摘要，窗口外原文不再出现
    backend._responses.append("最新答")
    await service.send_message(thread["thread_id"], "最新问题")
    last_call = backend.calls[-1]
    assert "整理：用户问了早问题" in last_call[0].content
    assert not any(m.content == "早问题0" for m in last_call if m.role == "user")

    events = await store.events.by_scope(*_assistant_scope(thread["thread_id"]))
    compacted = [e for e in events if e["type"] == EventType.ASSISTANT_COMPACTED.value]
    assert len(compacted) == 1
    assert compacted[0]["payload"]["summarized"] == 4


async def test_compact_without_outside_messages_is_noop(store) -> None:
    backend = FakeLLMBackend("答")
    service = await make_service(store, backend, secrets=FakeSecrets())
    thread = await service.create_thread()
    await service.send_message(thread["thread_id"], "唯一的问题")

    result = await service.compact_thread(thread["thread_id"])
    assert result["compacted"] is False
    assert result["note"]
    assert len(backend.calls) == 1  # 没有产生额外调用


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _assistant_scope(thread_id: str):
    from workerbee.data.event_log import EventScope

    return EventScope.ASSISTANT, thread_id
