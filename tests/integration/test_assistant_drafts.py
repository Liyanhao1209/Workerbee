"""助手草稿提案的完整链路集成测试（Base Assistant 建图提案）。

覆盖：fake LLM 返回含 workerbee-draft 块的回复 → 发消息 → draft 落库 +
AI_DRAFT_PROPOSED 事件 + 消息列表带 draft → 采用（workflow 真建成、修订
source=ai_generated、未发布）/ 重复采用被拒 / 拒绝留痕 / 节点模板路径 /
编造引用的「待配置」如实呈现。

纪律与 test_assistant.py 相同：httpx.ASGITransport 全程同一事件循环，
绝不发真实 LLM 请求。
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


# ===========================================================================
# 夹具与工具
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
    fake = FakeLLMBackend("占位回答")
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


async def _setup(engine: Engine, client: Any) -> None:
    """解锁凭据库、登记一条助手凭据、启用助手、登记一台可用的 harness。"""
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
            "secret": {"api_key": "sk-test", "base_url": "https://llm.example.com/v1"},
        },
    )
    assert created.status_code == 200, created.text
    updated = await client.put(
        "/api/assistant/config",
        json={"enabled": True, "credential_ref": created.json()["credential_id"]},
    )
    assert updated.status_code == 200, updated.text

    harness = await client.post(
        "/api/harnesses",
        json={"harness_id": "h-claude", "name": "Claude", "adapter_id": "mock"},
    )
    assert harness.status_code == 200, harness.text


def _proposal_reply(payload: dict[str, Any]) -> str:
    return "这是解释。\n\n```workerbee-draft\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"


def _workflow_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "workflow",
        "name": "AI 生成的流程",
        "description": "先规划再执行",
        "nodes": [
            {"node_id": "planner", "name": "规划",
             "profiles": [{"harness_ref": "h-claude", "model_name": "claude-sonnet"}]},
            {"node_id": "coder", "name": "执行", "profiles": [{"harness_ref": "h-claude"}]},
        ],
        "edges": [{"from_node": "planner", "to_node": "coder", "output_contract": ["plan"]}],
    }
    base.update(overrides)
    return base


async def _send_with_reply(client: Any, backend: FakeLLMBackend, reply: str) -> dict[str, Any]:
    """换 fake 后端的回答并发一条消息，返回 send 响应体。"""
    backend._responses = [reply]
    thread = (await client.post("/api/assistant/threads", json={})).json()
    resp = await client.post(
        f"/api/assistant/threads/{thread['thread_id']}/messages",
        json={"content": "帮我生成一个流程"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _events(client: Any) -> list[dict[str, Any]]:
    return (await client.get("/api/system/events?limit=500")).json()["events"]


# ===========================================================================
# 提案落库与呈现
# ===========================================================================


async def test_proposal_is_persisted_and_listed_with_message(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    body = await _send_with_reply(client, backend, _proposal_reply(_workflow_payload()))

    # send 响应里的消息就带 draft（前端无需二次查询才能渲染卡片）
    drafts = body["message"]["drafts"]
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["kind"] == "workflow"
    assert draft["name"] == "AI 生成的流程"
    assert draft["status"] == "pending"
    assert draft["adopted_ref"] is None
    # 引用全部有效：校验通过、没有「待配置」项
    assert draft["validation"]["ok"] is True
    assert draft["validation"]["pending_config"] == []

    # 消息列表同样带 draft（重启/刷新后的渲染路径）
    history = (
        await client.get(f"/api/assistant/threads/{draft['thread_id']}/messages")
    ).json()
    assistant_msg = [m for m in history["messages"] if m["role"] == "assistant"][0]
    assert [d["draft_id"] for d in assistant_msg["drafts"]] == [draft["draft_id"]]

    # 事件留痕：AI_DRAFT_PROPOSED 带 draft_id 与校验摘要
    events = await _events(client)
    proposed = [e for e in events if e["type"] == EventType.AI_DRAFT_PROPOSED.value]
    assert len(proposed) == 1
    assert proposed[0]["payload"]["draft_id"] == draft["draft_id"]
    assert proposed[0]["actor"] == "ai"


async def test_reply_without_proposal_has_no_draft(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    body = await _send_with_reply(client, backend, "这次没有提案。")
    assert body["message"]["drafts"] == []
    events = await _events(client)
    assert not [e for e in events if e["type"] == EventType.AI_DRAFT_PROPOSED.value]


async def test_broken_proposal_block_is_degraded_not_fatal(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    """块内 JSON 损坏：回复照常显示，只是没有卡片，且不阻断消息落库。"""
    await _setup(engine, client)
    body = await _send_with_reply(client, backend, "解释。\n\n```workerbee-draft\n{坏JSON\n```\n")
    assert body["message"]["drafts"] == []
    assert body["message"]["content"].startswith("解释。")


async def test_fabricated_refs_appear_in_pending_config(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    """模型编造清单外的实体 id：校验诊断与「待配置」项如实出现在 draft 里。"""
    await _setup(engine, client)
    payload = _workflow_payload(
        nodes=[
            {"node_id": "a", "name": "甲",
             "profiles": [{"harness_ref": "编造的harness", "credential_ref": "编造的凭据"}],
             "skill_refs": ["编造的skill"], "tool_refs": ["编造的工具"]},
        ],
        edges=[],
    )
    body = await _send_with_reply(client, backend, _proposal_reply(payload))
    draft = body["message"]["drafts"][0]

    pending_text = "\n".join(draft["validation"]["pending_config"])
    assert "编造的harness" in pending_text
    assert "编造的凭据" in pending_text
    assert "编造的skill" in pending_text
    assert "编造的工具" in pending_text
    codes = {d["code"] for d in draft["validation"]["diagnostics"]}
    assert "harness_not_registered" in codes
    assert "credential_not_registered" in codes


# ===========================================================================
# 采用
# ===========================================================================


async def test_adopt_workflow_creates_draft_revision(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    body = await _send_with_reply(client, backend, _proposal_reply(_workflow_payload()))
    draft = body["message"]["drafts"][0]

    adopted = await client.post(f"/api/assistant/drafts/{draft['draft_id']}/adopt")
    assert adopted.status_code == 200, adopted.text
    result = adopted.json()
    assert result["status"] == "adopted"
    workflow_id = result["adopted_ref"]
    assert workflow_id

    # 流程真的建成了，且走与手动建图相同的服务层入口
    wf = (await client.get(f"/api/workflows/{workflow_id}")).json()
    assert wf["name"] == "AI 生成的流程"
    assert wf["status"] == "draft"  # 采用只存为草稿，不自动发布

    revisions = (await client.get(f"/api/workflows/{workflow_id}/revisions")).json()
    assert revisions["revisions"][0]["source"] == "ai_generated"
    assert revisions["revisions"][0]["is_published"] is False
    graph = revisions["revisions"][0]["graph"]
    assert [n["node_id"] for n in graph["nodes"]] == ["planner", "coder"]
    assert len(graph["edges"]) == 1

    # 采用前复核的校验结论回写进了 draft
    assert result["validation"]["summary"]

    # 事件：AI_DRAFT_ACCEPTED（actor=user——这是用户的决定）
    events = await _events(client)
    accepted = [e for e in events if e["type"] == EventType.AI_DRAFT_ACCEPTED.value]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["adopted_ref"] == workflow_id
    assert accepted[0]["actor"] == "user"

    # 重复采用被拒，且不产生第二个流程
    again = await client.post(f"/api/assistant/drafts/{draft['draft_id']}/adopt")
    assert again.status_code == 400
    assert "已经采用过" in again.json()["detail"]
    workflows = (await client.get("/api/workflows")).json()["workflows"]
    assert len([w for w in workflows if w["name"] == "AI 生成的流程"]) == 1


async def test_adopt_unknown_draft_is_404(client: Any) -> None:
    resp = await client.post("/api/assistant/drafts/不存在的提案/adopt")
    assert resp.status_code == 404


async def test_adopt_node_template_creates_template(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    payload = {
        "kind": "node_template",
        "name": "审计节点",
        "description": "审查上游产出",
        "nodes": [
            {"name": "审计", "role": "审计者", "system_prompt": "你是审计者。",
             "profiles": [{"harness_ref": "h-claude"}]}
        ],
    }
    body = await _send_with_reply(client, backend, _proposal_reply(payload))
    draft = body["message"]["drafts"][0]
    assert draft["kind"] == "node_template"

    adopted = await client.post(f"/api/assistant/drafts/{draft['draft_id']}/adopt")
    assert adopted.status_code == 200, adopted.text
    template_id = adopted.json()["adopted_ref"]

    template = (await client.get(f"/api/templates/{template_id}")).json()
    assert template["kind"] == "node"
    assert template["name"] == "审计节点"
    assert template["payload"]["nodes"][0]["name"] == "审计"
    assert template["payload"]["nodes"][0]["system_prompt"] == "你是审计者。"


# ===========================================================================
# 拒绝
# ===========================================================================


async def test_reject_leaves_trace_and_blocks_adopt(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    body = await _send_with_reply(client, backend, _proposal_reply(_workflow_payload()))
    draft = body["message"]["drafts"][0]

    rejected = await client.post(f"/api/assistant/drafts/{draft['draft_id']}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["adopted_ref"] is None

    # 拒绝后不能再采用
    adopt = await client.post(f"/api/assistant/drafts/{draft['draft_id']}/adopt")
    assert adopt.status_code == 400
    assert "已被拒绝" in adopt.json()["detail"]

    # 拒绝本身留痕
    events = await _events(client)
    rejected_events = [e for e in events if e["type"] == EventType.AI_DRAFT_REJECTED.value]
    assert len(rejected_events) == 1
    assert rejected_events[0]["payload"]["draft_id"] == draft["draft_id"]

    # 没有任何流程被创建
    workflows = (await client.get("/api/workflows")).json()["workflows"]
    assert workflows == []
