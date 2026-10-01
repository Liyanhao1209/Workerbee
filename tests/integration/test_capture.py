"""流程捕获（Graph Capture，WF-03、AC-16、D-12）的端到端集成测试。

链路：POST /api/capture/runs 建临时 Workflow 并真实发射 → 手工驱动调度器跑完
（FakeHarness 会话 + output/tool_use 事件）→ GET 详情（材料摘要、任务状态收敛）
→ POST drafts 触发合成（FakeLLMBackend 回 workerbee-draft 块）→ 复核降级留痕
→ 采用（workflow 草稿修订 source=graph_capture / as_template 存模板）/ 拒绝 →
默认流程列表过滤临时 Workflow。

纪律与 test_assistant_drafts.py 相同：httpx.ASGITransport 全程同一事件循环，
绝不发真实 LLM 请求；调度不靠山后台循环，手工 tick 保证时序确定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from workerbee.app import Engine, EngineConfig
from workerbee.capture import assemble_material
from workerbee.core.domain.task import TaskState
from workerbee.data.event_log import EventType
from workerbee.security.secret_store import SecretStore
from workerbee.server.app import create_app
from workerbee.server.auth import TOKEN_HEADER

from tests.fakes import FakeHarness, FakeLLMBackend

pytestmark = pytest.mark.integration

TOKEN = "test-token-8f3c-not-a-secret"
PASSPHRASE = "test-passphrase-please-change"

#: 捕获执行的输出正文：带一个标题含「计划」的小节，材料汇编才能取到显式计划。
PLAN_OUTPUT = "## 执行计划\n1. 扫描目录\n2. 汇总结果\n\n## 结果\n已完成扫描与汇总。"


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
    )
    eng = await Engine.create(config, harness=FakeHarness())
    yield eng
    await eng.store.close()


@pytest.fixture
async def backend(engine: Engine) -> FakeLLMBackend:
    fake = FakeLLMBackend("占位回答")
    engine.capture.backend_factory = lambda cfg, cred, secrets: fake
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
    """解锁凭据库、登记合成凭据、启用助手（捕获合成复用助手配置）、登记 harness。"""
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


async def _create_and_finish_run(client: Any, engine: Engine) -> dict[str, Any]:
    """建一个捕获任务并手工驱动它跑到 SUCCEEDED，返回 run 台账。"""
    created = await client.post(
        "/api/capture/runs",
        json={
            "name": "扫描仓库",
            "instructions": "列出目录并汇总结构",
            "harness_ref": "h-claude",
        },
    )
    assert created.status_code == 200, created.text
    run = created.json()
    assert run["status"] == "running"
    assert run["task_id"], "发射成功后 run 必须绑上真实任务 id"
    assert run["workflow_id"]
    assert run["profile"]["harness_ref"] == "h-claude"

    # 派发 → 产出（计划正文 + 一次工具调用）→ 会话正常结束 → 收敛
    await engine.scheduler.tick()
    rt = next(iter(engine.scheduler._runtimes.values()), None)
    assert rt is not None, "捕获任务没有被派发，测试前提不成立"
    await engine.scheduler.on_event(
        session_ref=rt.session_ref, kind="output", payload={"text": PLAN_OUTPUT}
    )
    await engine.scheduler.on_event(
        session_ref=rt.session_ref,
        kind="tool_use",
        payload={"tool_name": "Bash", "tool_use_id": "tu-1", "input": {"command": "ls"}},
    )
    await engine.scheduler.on_event(
        session_ref=rt.session_ref,
        kind="tool_result",
        payload={"tool_use_id": "tu-1", "is_error": False},
    )
    await engine.scheduler.on_session_ended(session_ref=rt.session_ref, ok=True)
    await engine.scheduler.tick()

    task = await engine.store.tasks.get_task(run["task_id"])
    assert task.observed_state == TaskState.SUCCEEDED
    return run


def _proposal_reply(payload: dict[str, Any]) -> str:
    return "这是分析。\n\n```workerbee-draft\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"


async def _generate_draft(
    client: Any, engine: Engine, backend: FakeLLMBackend, run: dict[str, Any]
) -> dict[str, Any]:
    """编排一次合成：取真实 evidence 编一份提案，触发生成，返回草案。"""
    material = await assemble_material(engine.store, run["task_id"])
    real_evidence = sorted(e for e in material.evidence_set() if e.startswith("E"))
    assert real_evidence, "材料里应该有可引用的事件证据，测试前提不成立"

    payload: dict[str, Any] = {
        "kind": "workflow",
        "name": "仓库扫描流程",
        "description": "先扫描再汇总",
        "nodes": [
            {
                "node_id": "scan",
                "name": "扫描",
                "role": "扫描者",
                "profiles": [{"harness_ref": "h-claude"}],
                "basis": "observed",
                "evidence": [real_evidence[0]],
            },
            {
                "node_id": "summarize",
                "name": "汇总",
                "role": "汇总者",
                "profiles": [{"harness_ref": "h-claude"}],
                "basis": "observed",
                "evidence": ["E999999"],  # 编造的佐证 → 必须被强制降级
            },
        ],
        "edges": [
            {"from_node": "scan", "to_node": "summarize", "basis": "inferred", "evidence": []}
        ],
    }
    backend._responses = [_proposal_reply(payload)]
    resp = await client.post(f"/api/capture/runs/{run['run_id']}/drafts")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _events(client: Any) -> list[dict[str, Any]]:
    return (await client.get("/api/system/events?limit=500")).json()["events"]


# ===========================================================================
# 捕获任务的创建与执行
# ===========================================================================


async def test_create_run_launches_real_task_and_detail_reflects_material(
    client: Any, engine: Engine
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)

    # 临时 Workflow 的名字带「捕获·」前缀（用户能一眼认出它）
    wf = (await client.get(f"/api/workflows/{run['workflow_id']}")).json()
    assert wf["name"].startswith("捕获·")

    # 详情：任务状态收敛为 succeeded，材料摘要如实反映这次执行
    detail = (await client.get(f"/api/capture/runs/{run['run_id']}")).json()
    assert detail["run"]["status"] == "completed"
    assert detail["task_state"] == "succeeded"
    material = detail["material"]
    assert material is not None
    assert material["tool_calls"] == 1
    assert material["has_plan"] is True
    assert material["chars"] > 0
    assert material["trimmed"] == []

    # 材料明确不含私有推理链：事件里即使出现 reasoning 也不能进材料
    text = (await assemble_material(engine.store, run["task_id"])).text
    assert "思考" not in text

    # 列表里状态同样收敛
    runs = (await client.get("/api/capture/runs")).json()["runs"]
    assert [r["run_id"] for r in runs] == [run["run_id"]]
    assert runs[0]["status"] == "completed"

    # 事件留痕：CAPTURE_RUN_CREATED 带任务与基础候选
    events = await _events(client)
    created_events = [e for e in events if e["type"] == EventType.CAPTURE_RUN_CREATED.value]
    assert len(created_events) == 1
    assert created_events[0]["payload"]["task_id"] == run["task_id"]
    assert created_events[0]["payload"]["harness_ref"] == "h-claude"


async def test_create_run_with_unknown_harness_is_400(client: Any, engine: Engine) -> None:
    await _setup(engine, client)
    resp = await client.post(
        "/api/capture/runs",
        json={"name": "x", "instructions": "做点什么", "harness_ref": "不存在的"},
    )
    assert resp.status_code == 400
    assert "没有登记" in resp.json()["detail"]
    # 失败不产生任何台账记录
    assert (await client.get("/api/capture/runs")).json()["runs"] == []


async def test_capture_workflow_hidden_from_default_listing(
    client: Any, engine: Engine
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)

    default = (await client.get("/api/workflows")).json()["workflows"]
    assert not [w for w in default if w["name"].startswith("捕获·")]

    included = (await client.get("/api/workflows?include_capture=true")).json()["workflows"]
    assert run["workflow_id"] in [w["workflow_id"] for w in included]


# ===========================================================================
# 草案合成（observed 复核）
# ===========================================================================


async def test_generate_draft_reviews_observed_basis(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)
    draft = await _generate_draft(client, engine, backend, run)

    assert draft["status"] == "pending"
    assert draft["name"] == "仓库扫描流程"
    nodes = {n["node_id"]: n for n in draft["payload"]["nodes"]}
    # 有真实佐证的标注保留；编造佐证的被强制降级为 inferred，且降级记录在案
    assert nodes["scan"]["basis"] == "observed"
    assert nodes["scan"]["evidence"], "真实佐证不应被清掉"
    assert nodes["summarize"]["basis"] == "inferred"
    assert nodes["summarize"]["evidence"] == []
    downgrades = draft["validation"]["downgrades"]
    assert len(downgrades) == 1
    assert "汇总" in downgrades[0]["target"]
    assert any("已按「推断的」处理" in note for note in draft["payload"]["notes"])
    # 引用的 harness 真实存在：没有「待配置」项
    assert draft["validation"]["pending_config"] == []

    # 事件留痕：CAPTURE_DRAFT_GENERATED 带降级计数与材料概况
    events = await _events(client)
    generated = [e for e in events if e["type"] == EventType.CAPTURE_DRAFT_GENERATED.value]
    assert len(generated) == 1
    assert generated[0]["actor"] == "ai"
    assert generated[0]["payload"]["draft_id"] == draft["draft_id"]
    assert generated[0]["payload"]["downgrade_count"] == 1

    # 草案随 run 详情列出
    detail = (await client.get(f"/api/capture/runs/{run['run_id']}")).json()
    assert [d["draft_id"] for d in detail["drafts"]] == [draft["draft_id"]]


async def test_generate_draft_requires_finished_task(client: Any, engine: Engine) -> None:
    """任务还在跑时触发生成：如实拒绝，不产生半截草案。"""
    await _setup(engine, client)
    created = await client.post(
        "/api/capture/runs",
        json={"name": "跑着", "instructions": "慢慢做", "harness_ref": "h-claude"},
    )
    assert created.status_code == 200, created.text
    run = created.json()
    await engine.scheduler.tick()
    assert engine.scheduler._runtimes, "任务应当已在运行"

    resp = await client.post(f"/api/capture/runs/{run['run_id']}/drafts")
    assert resp.status_code == 400
    assert "跑完" in resp.json()["detail"]


async def test_generate_draft_with_unparseable_reply_is_retryable(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    """模型没按约定输出提案块：400 且可重试；任务结果不受影响。"""
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)

    backend._responses = ["这次没有提案块。"]
    bad = await client.post(f"/api/capture/runs/{run['run_id']}/drafts")
    assert bad.status_code == 400
    assert "workerbee-draft" in bad.json()["detail"]

    task = await engine.store.tasks.get_task(run["task_id"])
    assert task.observed_state == TaskState.SUCCEEDED, "合成失败不得影响已完成的执行"

    draft = await _generate_draft(client, engine, backend, run)
    assert draft["status"] == "pending"


async def test_generate_draft_without_assistant_config_is_400(
    client: Any, engine: Engine
) -> None:
    """合成复用助手配置；没配置时如实报出并指向助手设置。"""
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    await engine.unlock_secrets(PASSPHRASE)
    harness = await client.post(
        "/api/harnesses",
        json={"harness_id": "h-claude", "name": "Claude", "adapter_id": "mock"},
    )
    assert harness.status_code == 200, harness.text
    run = await _create_and_finish_run(client, engine)

    resp = await client.post(f"/api/capture/runs/{run['run_id']}/drafts")
    assert resp.status_code == 400
    assert "还没有配置合成用的模型" in resp.json()["detail"]
    assert "助手" in resp.json()["hint"], "指引必须指向助手设置——配置就是同一份"


# ===========================================================================
# 采用与拒绝
# ===========================================================================


async def test_adopt_creates_draft_revision_via_same_service_path(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)
    draft = await _generate_draft(client, engine, backend, run)

    adopted = await client.post(f"/api/capture/drafts/{draft['draft_id']}/adopt")
    assert adopted.status_code == 200, adopted.text
    result = adopted.json()
    assert result["status"] == "adopted"
    workflow_id = result["adopted_ref"]
    assert workflow_id

    # 流程真的建成了：草稿修订、未发布、来源 graph_capture——与助手提案同一纪律
    wf = (await client.get(f"/api/workflows/{workflow_id}")).json()
    assert wf["name"] == "仓库扫描流程"
    assert wf["status"] == "draft"
    revisions = (await client.get(f"/api/workflows/{workflow_id}/revisions")).json()
    rev = revisions["revisions"][0]
    assert rev["source"] == "graph_capture"
    assert rev["is_published"] is False
    assert [n["node_id"] for n in rev["graph"]["nodes"]] == ["scan", "summarize"]
    # basis/evidence 是草案级的溯源标注，不进核心图模型；复核结论留在草案的
    # payload/validation 里（采用后的流程就是一条普通流程）

    # 采用的流程是普通流程，出现在默认列表里
    default = (await client.get("/api/workflows")).json()["workflows"]
    assert workflow_id in [w["workflow_id"] for w in default]

    # 事件：CAPTURE_DRAFT_ADOPTED（actor=user——这是用户的决定）
    events = await _events(client)
    adopted_events = [e for e in events if e["type"] == EventType.CAPTURE_DRAFT_ADOPTED.value]
    assert len(adopted_events) == 1
    assert adopted_events[0]["actor"] == "user"
    assert adopted_events[0]["payload"]["adopted_ref"] == workflow_id

    # 重复采用被拒，且不产生第二个流程
    again = await client.post(f"/api/capture/drafts/{draft['draft_id']}/adopt")
    assert again.status_code == 400
    assert "已经采用过" in again.json()["detail"]
    names = [w["name"] for w in (await client.get("/api/workflows")).json()["workflows"]]
    assert names.count("仓库扫描流程") == 1


async def test_adopt_as_template_creates_template(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)
    draft = await _generate_draft(client, engine, backend, run)

    adopted = await client.post(
        f"/api/capture/drafts/{draft['draft_id']}/adopt", json={"as_template": True}
    )
    assert adopted.status_code == 200, adopted.text
    template_id = adopted.json()["adopted_ref"]

    template = (await client.get(f"/api/templates/{template_id}")).json()
    assert template["kind"] == "workflow"
    assert template["name"] == "仓库扫描流程"
    assert len(template["payload"]["nodes"]) == 2

    # 采用为模板不产生新流程
    names = [w["name"] for w in (await client.get("/api/workflows")).json()["workflows"]]
    assert "仓库扫描流程" not in names


async def test_reject_leaves_trace_and_blocks_adopt(
    client: Any, engine: Engine, backend: FakeLLMBackend
) -> None:
    await _setup(engine, client)
    run = await _create_and_finish_run(client, engine)
    draft = await _generate_draft(client, engine, backend, run)

    rejected = await client.post(f"/api/capture/drafts/{draft['draft_id']}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["adopted_ref"] is None

    adopt = await client.post(f"/api/capture/drafts/{draft['draft_id']}/adopt")
    assert adopt.status_code == 400
    assert "已被拒绝" in adopt.json()["detail"]

    events = await _events(client)
    rejected_events = [e for e in events if e["type"] == EventType.CAPTURE_DRAFT_REJECTED.value]
    assert len(rejected_events) == 1
    assert rejected_events[0]["payload"]["draft_id"] == draft["draft_id"]


async def test_unknown_draft_is_404(client: Any) -> None:
    resp = await client.post("/api/capture/drafts/不存在的草案/adopt")
    assert resp.status_code == 404
    assert (await client.post("/api/capture/drafts/不存在的草案/reject")).status_code == 404
    assert (await client.get("/api/capture/drafts/不存在的草案")).status_code == 404
    assert (await client.get("/api/capture/runs/不存在的记录")).status_code == 404
