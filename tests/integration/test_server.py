"""API 网关集成测试：路由 → 服务 → 真实内核。

对应需求：UI-02、AUTH-02、REC-01、REC-03、RUN-02/04、ACT-02、AC-03/14、WF-05、
D-01/D-02、LIFE-06、HUM-04、RES-03、OBS-01/05。

刻意用 ``httpx.ASGITransport`` + 手写的 ASGI WebSocket 驱动，而不是
``starlette.testclient``：后者把应用跑在**另一个线程的事件循环**里，而 aiosqlite
的连接绑在创建它的循环上，跨循环使用会直接炸。这里全部在同一个事件循环内完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from httpx import ASGITransport

from workerbee.adapters.host.remote import SupervisorClient
from workerbee.app import Engine, EngineConfig
from workerbee.core.domain import (
    HarnessRegistration,
    RevisionSource,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from workerbee.core.domain.registry import CredentialKind
from workerbee.data.event_log import EventScope, EventType
from workerbee.security.secret_store import SecretRedactor, SecretStore
from workerbee.server.app import create_app
from workerbee.server.auth import TOKEN_HEADER

from tests.fakes import FakeHarness
from tests.helpers import graph, node

pytestmark = pytest.mark.integration

TOKEN = "test-token-8f3c-not-a-secret"

#: 测试用密值。断言它不出现在任何响应体里。
SECRET = "sk-live-DEADBEEFdeadbeef0123456789"

#: 形状上不像任何已知密钥的密值：只有「已登记的已知值」这条脱敏路径能拦住它。
PLAIN_SECRET = "zzq-9137-unpatterned-value-7741"

#: 测试用凭据库口令。
PASSPHRASE = "test-passphrase-please-change"

#: 一个固定的时间戳，用于直接写库的夹具。
NOW = "2026-01-02T03:04:05Z"

#: 图 A → B，两个节点都用已登记的 harness h1。
GRAPH = graph({"A": ["B"], "B": []})


# ===========================================================================
# 夹具
# ===========================================================================


@pytest.fixture
async def engine(tmp_path: Path) -> Any:
    """一个真实的 Engine：真数据库、真仓储，harness 用替身。"""
    config = EngineConfig(
        data_dir=tmp_path / ".workerbee",
        workspace_dir=tmp_path / "workspace",
        poll_interval=3600.0,  # 测试不跑调度循环，手动驱动
        reaper_interval=3600.0,
        use_summarizer=False,
        use_context_assembler=False,
    )
    eng = await Engine.create(config, harness=FakeHarness())
    yield eng
    await eng.store.close()


@pytest.fixture
async def app(engine: Engine) -> Any:
    return create_app(engine, token=TOKEN)


@pytest.fixture
async def client(app: Any) -> Any:
    """带正确令牌的客户端。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as c:
        yield c


@pytest.fixture
async def publish(engine: Engine):
    """直接经仓储发布一个 Workflow（网关的修订端点在别处单独测）。"""

    async def _publish(graph_spec: Any = GRAPH, *, name: str = "wf", harness: str = "h1"):
        await engine.store.registry.upsert_harness(
            HarnessRegistration(
                harness_id=harness, name=harness, adapter_id="fake", last_probe_ok=True
            )
        )
        wf = WorkflowDefinition(name=name, status=WorkflowStatus.DRAFT)
        await engine.store.workflows.create(wf)
        rev = WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=graph_spec,
            source=RevisionSource.MANUAL,
            is_published=True,
        )
        await engine.store.workflows.save_revision(
            rev, publish=True, expected_revision_seq=0
        )
        return wf, rev

    return _publish


# ===========================================================================
# ASGI WebSocket 驱动
# ===========================================================================


class RawWebSocket:
    """在同一个事件循环里驱动 ASGI WebSocket scope。"""

    def __init__(
        self,
        app: Any,
        *,
        token: str | None = TOKEN,
        path: str = "/api/ws",
        client: tuple[str, int] = ("127.0.0.1", 55123),
    ) -> None:
        self._app = app
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[Any] | None = None

        query = f"?token={token}" if token is not None else ""
        self._scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.lstrip("?").encode(),
            "root_path": "",
            "headers": [(b"host", b"localhost")],
            "client": client,
            "server": ("127.0.0.1", 8765),
            "subprotocols": [],
            "state": {},
        }

    async def _receive(self) -> dict[str, Any]:
        return await self._outbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._inbox.put(message)

    async def __aenter__(self) -> "RawWebSocket":
        # 握手消息必须先于应用读取时就位
        await self._outbox.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await self.expect({"type": "websocket.accept"}, {"type": "websocket.close"})
        return self

    async def __aexit__(self, *exc: Any) -> None:
        # 应用侧收到 disconnect 后会自行收束；它退出时抛什么都与断言无关
        await self.send({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=2.0)

    # ---- 收发 ----

    async def next_frame(self, timeout: float = 3.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._inbox.get(), timeout)

    async def expect(self, *allowed: dict[str, Any]) -> dict[str, Any]:
        frame = await self.next_frame()
        assert frame["type"] in {a["type"] for a in allowed}, frame
        return frame

    async def receive_json(self, timeout: float = 3.0) -> dict[str, Any]:
        frame = await self.next_frame(timeout)
        assert frame["type"] == "websocket.send", frame
        return json.loads(frame["text"])

    async def receive_until(
        self, predicate: Callable[[dict[str, Any]], bool], *, timeout: float = 3.0
    ) -> dict[str, Any]:
        async def _loop() -> dict[str, Any]:
            while True:
                message = await self.receive_json(timeout)
                if predicate(message):
                    return message

        return await asyncio.wait_for(_loop(), timeout)

    async def send(self, payload: dict[str, Any]) -> None:
        await self._outbox.put(
            {"type": "websocket.receive", "text": json.dumps(payload)}
        )

    async def close_frame(self) -> dict[str, Any]:
        return await self.next_frame()


# ===========================================================================
# 鉴权（UI-02）
# ===========================================================================


async def test_health_is_public(app: Any) -> None:
    """``/api/health`` 免鉴权：它不含任何数据，只说明服务在。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as anon:
        resp = await anon.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_missing_token_is_401(app: Any) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as anon:
        resp = await anon.get("/api/system/status")
    assert resp.status_code == 401
    assert TOKEN not in resp.text, "401 响应不得回显任何令牌"


async def test_wrong_token_is_401_and_never_echoed(app: Any) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as anon:
        resp = await anon.get(
            "/api/credentials", headers={TOKEN_HEADER: "wrong-token-value"}
        )
        via_query = await anon.get("/api/credentials?token=wrong-token-value")
    assert resp.status_code == 401
    assert via_query.status_code == 401
    assert "wrong-token-value" not in resp.text
    assert TOKEN not in resp.text


async def test_correct_token_via_header_and_query(client: Any, app: Any) -> None:
    assert (await client.get("/api/system/status")).status_code == 200
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as anon:
        resp = await anon.get(f"/api/health?token={TOKEN}")
        listed = await anon.get(f"/api/workflows?token={TOKEN}")
    assert resp.status_code == 200
    assert listed.status_code == 200


async def test_non_loopback_client_is_refused_even_with_token(app: Any) -> None:
    """默认只服务本机（UI-02）：令牌正确也不能让外部来源进来，除非显式放开。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("10.1.2.3", 40123)),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as remote:
        refused = await remote.get("/api/tasks")
    assert refused.status_code == 403
    assert "allow_remote" in refused.text or "本机" in refused.text

    allow_app = create_app(app.state.engine, token=TOKEN, allow_remote=True)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=allow_app, client=("10.1.2.3", 40123)),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as remote:
        allowed = await remote.get("/api/tasks")
        status = await remote.get("/api/system/status")
    assert allowed.status_code == 200
    assert status.json()["allow_remote"] is True
    assert status.json()["loopback_only"] is False


def test_cli_defaults_are_loopback_only() -> None:
    """默认绑定必须是 127.0.0.1:8765，开放访问只能是显式选择（UI-02）。"""
    from workerbee.server.main import build_parser

    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.allow_remote is False
    assert args.use_supervisor is False

    remote = build_parser().parse_args(["--allow-remote", "--use-supervisor"])
    assert remote.allow_remote is True
    assert remote.use_supervisor is True


def test_cli_refuses_non_loopback_without_flag() -> None:
    from workerbee.server.main import main

    assert main(["--host", "0.0.0.0"]) == 2


# ===========================================================================
# 发射（WF-05、RUN-02）
# ===========================================================================


async def test_submit_invalid_graph_returns_422_with_locator(
    client: Any, engine: Engine
) -> None:
    """校验失败是 422 + 可定位的完整报告，不是笼统的 400。"""
    bad = graph({"A": []}, nodes={"A": node("A", harness="ghost")})
    wf = WorkflowDefinition(name="bad", status=WorkflowStatus.DRAFT)
    await engine.store.workflows.create(wf)
    await engine.store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id, revision_seq=1, graph=bad, is_published=True
        ),
        publish=True,
        expected_revision_seq=0,
    )

    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/tasks", json={"input_payload": {"goal": "x"}}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["accepted"] is False

    diagnostics = body["report"]["diagnostics"]
    assert diagnostics, "422 必须携带完整校验报告"
    locatable = [d for d in diagnostics if d.get("node_id")]
    assert locatable, "报告里必须有一条能定位到节点的结论（WF-05）"
    hit = [d for d in locatable if d["code"] == "harness_not_registered"]
    assert hit and hit[0]["slot"] == "profiles[0].harness_ref"
    assert hit[0]["hint"], "诊断必须给出可执行的修复方向"


async def test_submit_idempotency_key_returns_same_task(
    client: Any, publish: Any
) -> None:
    wf, _ = await publish()
    payload = {"input_payload": {"goal": "写一个测试"}, "idempotency_key": "k-1"}

    first = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json=payload)
    second = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert second.json()["created"] is False
    assert len((await client.get("/api/tasks")).json()["tasks"]) == 1


async def test_attempt_work_and_artifact_content_endpoints(
    client: Any, engine: Engine, publish: Any
) -> None:
    """任务详情要能回看单次尝试的输入/推理/工具调用/涉及文件，以及产物正文。"""
    from workerbee.core.domain.artifact import ArtifactProducer
    from workerbee.core.domain.task import Attempt

    wf, _ = await publish()
    task_id = (
        await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
    ).json()["task_id"]
    stage = (await engine.store.tasks.list_stages(task_id))[0]
    attempt = await engine.store.tasks.create_attempt(
        Attempt(
            stage_id=stage.stage_id,
            task_id=task_id,
            node_id=stage.node_id,
            attempt_seq=1,
            profile_id="p1",
        )
    )

    base = dict(scope=EventScope.ATTEMPT, scope_id=attempt.attempt_id,
                task_id=task_id, stage_id=stage.stage_id)
    await engine.store.events.append(
        type=EventType.ATTEMPT_INPUT,
        payload={
            "system_prompt": "你是编码者",
            "system_prompt_truncated": False,
            "user_input": "修改 a.py",
            "user_input_truncated": False,
        },
        **base,
    )
    await engine.store.events.append(
        type=EventType.ATTEMPT_REASONING,
        payload={"text": "想清楚了", "truncated": False},
        **base,
    )
    await engine.store.events.append(
        type=EventType.ATTEMPT_TOOL_USE,
        payload={
            "tool_name": "Edit",
            "tool_use_id": "tu-1",
            "target": "/tmp/a.py",
            "input_preview": '{"file_path":"/tmp/a.py"}',
            "truncated": False,
        },
        **base,
    )
    await engine.store.events.append(
        type=EventType.ATTEMPT_TOOL_RESULT,
        payload={"tool_use_id": "tu-1", "is_error": False, "content": "done", "truncated": False},
        **base,
    )
    art = await engine.store.artifacts.put(
        "产物正文",
        producer=ArtifactProducer(
            task_id=task_id,
            stage_id=stage.stage_id,
            attempt_seq=1,
            node_id=stage.node_id,
            attempt_id=attempt.attempt_id,
        ),
    )

    work = await client.get(f"/api/tasks/{task_id}/attempts/{attempt.attempt_id}/work")
    assert work.status_code == 200
    body = work.json()
    assert body["node_id"] == stage.node_id
    assert body["input"]["user_input"] == "修改 a.py"
    assert body["reasoning"] == "想清楚了"
    assert body["tool_calls"][0]["name"] == "Edit"
    assert body["tool_calls"][0]["is_error"] is False
    assert body["files_written"] == ["/tmp/a.py"]
    assert body["artifact_ids"] == [art.artifact_id]

    content = await client.get(f"/api/tasks/{task_id}/artifacts/{art.artifact_id}/content")
    assert content.status_code == 200
    assert content.json()["text"] == "产物正文"

    missing = await client.get(f"/api/tasks/{task_id}/attempts/nope/work")
    assert missing.status_code == 404
    gone = await client.get(f"/api/tasks/{task_id}/artifacts/nope/content")
    assert gone.status_code == 404


# ===========================================================================
# 生命周期三态（LIFE-06）
# ===========================================================================


async def test_pause_and_delete_report_three_conclusions(
    client: Any, publish: Any
) -> None:
    """「已接受 / 执行已停止 / 资源清理完成」三个结论不得合成一个布尔值。"""
    wf, _ = await publish()
    task_id = (
        await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
    ).json()["task_id"]

    paused = await client.post(
        f"/api/tasks/{task_id}/pause", json={"reason": "用户手动暂停"}
    )
    assert paused.status_code == 200
    pbody = paused.json()
    for key in ("accepted", "execution_stopped", "resources", "warnings"):
        assert key in pbody, f"暂停响应必须独立给出 {key}"
    assert pbody["accepted"] is True
    assert pbody["execution_stopped"] is True
    assert isinstance(pbody["resources"], dict)
    assert "fully_complete" not in pbody, "三态不得被折叠成单一布尔"

    deleted = await client.request(
        "DELETE", f"/api/tasks/{task_id}", json={"reason": "用户删除"}
    )
    assert deleted.status_code == 200
    dbody = deleted.json()
    for key in ("accepted", "execution_stopped", "resources", "warnings"):
        assert key in dbody
    assert dbody["accepted"] is True
    assert dbody["cancelled_stages"] or dbody["preserved_succeeded"]


async def test_pause_missing_task_is_404(client: Any) -> None:
    resp = await client.post("/api/tasks/nope/pause", json={})
    assert resp.status_code == 404


# ===========================================================================
# 修订 CAS（D-02）
# ===========================================================================


async def test_revision_cas_conflict_returns_409_with_latest(
    client: Any, publish: Any
) -> None:
    wf, _ = await publish()

    ok = await client.post(
        f"/api/workflows/{wf.workflow_id}/revisions",
        json={
            "graph": GRAPH.model_dump(mode="json"),
            "publish": True,
            "base_revision_seq": 1,
            "note": "第一次编辑",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["revision_seq"] == 2
    assert ok.json()["cas_checked"] is True

    stale = await client.post(
        f"/api/workflows/{wf.workflow_id}/revisions",
        json={
            "graph": GRAPH.model_dump(mode="json"),
            "publish": True,
            "base_revision_seq": 1,  # 已过期，别人改过了
            "note": "基于旧版本的编辑",
        },
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["latest_revision_seq"] == 2, "冲突响应必须附最新版本（D-02）"
    assert body["latest_revision"]["revision_seq"] == 2
    assert body["workflow_id"] == wf.workflow_id
    assert body["hint"]


async def test_validate_endpoint_locates_problems(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/validate", json={"mode": "publish"}
    )
    assert resp.status_code == 200
    report = resp.json()
    assert report["mode"] == "publish"
    assert all("code" in d for d in report["diagnostics"])


# ===========================================================================
# 启停预览（ACT-02、D-01）
# ===========================================================================


async def test_toggle_preview_changes_nothing(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    before = await client.get(f"/api/workflows/{wf.workflow_id}")
    before_rev = await client.get(
        f"/api/workflows/{wf.workflow_id}/revisions/{before.json()['current_revision_seq']}"
    )

    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/nodes/A/toggle?preview=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "delta" in body and "report" in body and "affected_tasks" in body
    assert body["node_id"] == "A"
    assert body["enabling"] is False, "A 当前启用，预览的是停用它"

    after = await client.get(f"/api/workflows/{wf.workflow_id}")
    after_rev = await client.get(
        f"/api/workflows/{wf.workflow_id}/revisions/{after.json()['current_revision_seq']}"
    )
    assert before.json() == after.json(), "预览不得改变 Workflow 定义"
    assert before_rev.json() == after_rev.json(), "预览不得产生新修订（ACT-02）"


async def test_toggle_preview_uses_proposed_enabled_set(client: Any, publish: Any) -> None:
    """预览按**拟议状态**校验：停用唯一节点后，「没有任何已启用节点」必须被提前看见。

    按当前状态校验的话这里一条诊断都不会有——它恰好能区分「校验的是拟议状态」
    与「校验的是现状」。
    """
    wf, _ = await publish(graph({"A": []}))
    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/nodes/A/toggle?preview=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    codes = {d["code"] for d in body["report"]["diagnostics"]}
    assert "no_enabled_node" in codes, body["report"]
    # 差异必须说清楚「原来的入口将不复存在」，而不只是给个布尔值
    assert body["delta"]["entry_nodes_before"] == ["A"]
    assert body["delta"]["entry_nodes_after"] == []


async def test_toggle_execute_needs_body(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/nodes/B/toggle?preview=false"
    )
    assert resp.status_code == 400
    assert "preview=false" in resp.json()["detail"]


async def test_toggle_execute_produces_revision(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    resp = await client.post(
        f"/api/workflows/{wf.workflow_id}/nodes/B/toggle?preview=false",
        json={"enable": False, "mode": "drain"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["new_revision_seq"] == 2
    assert body["removed_edges"] == [["A", "B"]], "停用 B 后 A→B 从有效图移除"


# ===========================================================================
# 队列与调序（RUN-04、AC-03）
# ===========================================================================


async def test_reorder_returns_effective_order(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    for key in ("k1", "k2"):
        await client.post(
            f"/api/workflows/{wf.workflow_id}/tasks", json={"idempotency_key": key}
        )

    queue = await client.get("/api/nodes/A/queue")
    assert queue.status_code == 200
    stage_ids = [s["stage_id"] for s in queue.json()["pending"]]
    assert len(stage_ids) == 2

    wanted = list(reversed(stage_ids))
    resp = await client.post("/api/nodes/A/reorder", json={"stage_ids": wanted})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["effective_order"] == wanted, "返回的必须是实际生效顺序（AC-03）"
    assert body["applied"] is True
    assert body["rejected"] == []

    reread = await client.get("/api/nodes/A/queue")
    assert [s["stage_id"] for s in reread.json()["pending"]] == wanted


async def test_reorder_reports_rejected_items(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
    resp = await client.post(
        "/api/nodes/A/reorder", json={"stage_ids": ["ghost-stage", "also-not-real"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rejected"]) == 2
    assert all("reason" in item for item in body["rejected"])


# ===========================================================================
# 审批（AC-14、HUM-04）
# ===========================================================================


async def _raise_approval(engine: Engine, *, delivered: bool = True) -> str:
    """造一个待决审批，并装一条回注通道（真实回注由适配层提供）。"""
    seen: list[tuple[str, str]] = []

    async def _deliver(approval: Any, status: Any, modified: str | None) -> bool:
        seen.append((approval.approval_id, status.value))
        return delivered

    engine.approvals.set_deliver(_deliver)
    approval = await engine.approvals.request(
        approval_id="ap-1",
        task_id="t1",
        stage_id="s1",
        attempt_id="at1",
        revision_seq=1,
        node_id="A",
        action="Bash(rm -rf build/)",
        target="build/",
        risk="high",
    )
    return approval.approval_id


async def test_approval_decide_twice_is_not_an_error(client: Any, engine: Engine) -> None:
    """重复决定不报错，返回的是「实际生效结果」（AC-14）。"""
    approval_id = await _raise_approval(engine)

    listed = await client.get("/api/approvals")
    assert listed.status_code == 200
    assert [a["approval_id"] for a in listed.json()["approvals"]] == [approval_id]

    first = await client.post(
        f"/api/approvals/{approval_id}/decide", json={"approve": True, "by": "alice"}
    )
    assert first.status_code == 200
    assert first.json()["delivered"] is True
    assert first.json()["status"] == "approved"

    second = await client.post(
        f"/api/approvals/{approval_id}/decide", json={"approve": True, "by": "alice"}
    )
    assert second.status_code == 200, "重复决定不得变成 500"
    body = second.json()
    assert body["status"] == "approved", "返回的必须是实际生效结果"
    assert body["detail"] and "未生效" in body["detail"]


async def test_approval_undeliverable_stays_visible_and_retryable(
    client: Any, engine: Engine
) -> None:
    """回注失败必须可见、可重试，且重试沿用原决定（HUM-04）。"""
    approval_id = await _raise_approval(engine, delivered=False)

    decided = await client.post(
        f"/api/approvals/{approval_id}/decide", json={"approve": False}
    )
    assert decided.status_code == 200
    assert decided.json()["delivered"] is False
    assert decided.json()["status"] == "undeliverable"

    still_listed = await client.get("/api/approvals")
    assert approval_id in [a["approval_id"] for a in still_listed.json()["approvals"]]

    retried = await client.post(f"/api/approvals/{approval_id}/retry-delivery")
    assert retried.status_code == 200
    assert retried.json()["delivered"] is False, "回注通道仍不可用，重试也应如实报失败"


# ===========================================================================
# 系统与存储（OBS-01/05、RES-03）
# ===========================================================================


async def test_system_status_exposes_degradation(client: Any, engine: Engine) -> None:
    engine.startup_notes.append("摘要器不可用，已退化为截断式摘要")
    status = (await client.get("/api/system/status")).json()
    assert status["session_hosting"] == "in_process", "夹具用的是 core 自托管的替身"
    assert status["supervisor_connected"] is False  # 测试用 in-process 替身
    assert status["loopback_only"] is True
    assert status["startup_notes"], "降级说明必须透出，不能吞掉"

    attention = (await client.get("/api/attention")).json()
    assert attention["startup_notes"] == status["startup_notes"]


async def test_session_hosting_follows_config(tmp_path: Path) -> None:
    """托管方式是**配置的运行方式**，连接状态是**此刻的事实**，两者不得混为一谈。"""
    config = EngineConfig(
        data_dir=tmp_path / "wb",
        use_supervisor=True,
        supervisor_socket=tmp_path / "no-such.sock",
        poll_interval=3600.0,
        use_summarizer=False,
        use_context_assembler=False,
    )
    eng = await Engine.create(config, harness=FakeHarness())
    try:
        async with _client_for(eng) as c:
            status = (await c.get("/api/system/status")).json()
            # 要求了 supervisor 托管，但手里这个 harness 不是 supervisor 客户端：
            # 如实说「托管方式=supervisor，此刻没连上」，而不是把配置当成事实
            assert status["session_hosting"] == "supervisor"
            assert status["supervisor_connected"] is False

            # 真正接上 supervisor 之后，连接状态才翻真
            client_impl = SupervisorClient(tmp_path / "no-such.sock")
            client_impl._writer = _OpenWriter()  # type: ignore[assignment]
            eng.harness = client_impl  # type: ignore[assignment]
            status = (await c.get("/api/system/status")).json()
            assert status["session_hosting"] == "supervisor"
            assert status["supervisor_connected"] is True

            # 反过来：不要 supervisor 托管时，报告的是 in_process
            eng.config.use_supervisor = False
            status = (await c.get("/api/system/status")).json()
            assert status["session_hosting"] == "in_process"
            assert status["supervisor_connected"] is False
    finally:
        await eng.store.close()


async def test_sessions_in_process_mode_says_why_it_is_empty(client: Any) -> None:
    """in-process 模式下内核不维护会话台账：空列表必须带上这句话，不能装作「没有会话」。"""
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "session_handle"
    assert body["reachable"] is True
    assert body["sessions"] == []
    assert body["note"] and "不单独维护会话台账" in body["note"]


async def test_sessions_reads_local_table_when_present(client: Any, engine: Engine) -> None:
    """本地表里有记录时如实列出（REC-03）。"""
    await engine.store.db.execute(
        """INSERT INTO session_handle(session_ref, harness_id, owner_task_id, owner_stage_id,
               owner_attempt_id, state, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("sess-1", "h1", "t1", "A", "at1", "alive", NOW, NOW),
    )
    body = (await client.get("/api/sessions")).json()
    assert body["returned"] == 1
    assert body["note"] is None, "有记录就不需要那句说明"
    session = body["sessions"][0]
    assert session["session_ref"] == "sess-1"
    assert session["harness_id"] == "h1"
    assert session["owner_task_id"] == "t1"
    assert session["state"] == "alive"
    assert "persist_locator" not in session, "只出台账字段，不出内部记账细节"


async def test_sessions_from_supervisor_are_honest_about_unreachable(
    client: Any, engine: Engine
) -> None:
    """supervisor 托管但连不上：reachable=False + 说明，绝不回一个空列表了事。"""
    engine.config.use_supervisor = True
    engine.harness = SupervisorClient(engine.config.resolved_supervisor_socket())
    body = (await client.get("/api/sessions")).json()
    assert body["source"] == "supervisor"
    assert body["reachable"] is False
    assert body["sessions"] == []
    assert body["note"] and "不等于「没有会话」" in body["note"]


async def test_sessions_from_supervisor_lists_ledger(client: Any, engine: Engine) -> None:
    """接上 supervisor 后，台账以它为准（会话的持有者是 supervisor）。"""

    class _Supervisor(SupervisorClient):
        def __init__(self) -> None:  # 不碰真实 socket
            super().__init__("/nonexistent.sock")

        async def list_sessions(self) -> list[dict[str, Any]]:
            return [
                {
                    "session_ref": "sess-9",
                    "harness_id": "h1",
                    "owner_task_id": "t9",
                    "owner_stage_id": "A",
                    "owner_attempt_id": "at9",
                    "state": "alive",
                    "created_at": NOW,
                    "last_heartbeat": NOW,
                    "pid": 4242,
                }
            ]

    engine.config.use_supervisor = True
    engine.harness = _Supervisor()
    engine.harness._writer = _OpenWriter()  # type: ignore[assignment]

    body = (await client.get("/api/sessions")).json()
    assert body["source"] == "supervisor"
    assert body["reachable"] is True
    assert body["returned"] == 1
    assert body["sessions"][0]["session_ref"] == "sess-9"
    assert body["sessions"][0]["last_heartbeat"] == NOW
    assert "pid" not in body["sessions"][0]


class _OpenWriter:
    """只为让 ``SupervisorClient.connected`` 为真，不发任何一帧。"""

    def is_closing(self) -> bool:
        return False


def _client_for(eng: Engine) -> Any:
    app = create_app(eng, token=TOKEN)
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    )


async def test_storage_and_prune_report_what_was_deleted(
    client: Any, engine: Engine, publish: Any
) -> None:
    """RES-03：先看清楚要删什么，再动手；动手之后也要说清删了什么。"""
    wf, _ = await publish()
    task_id = (
        await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
    ).json()["task_id"]
    for _ in range(3):
        await engine.store.events.append(
            scope=EventScope.TASK, type=EventType.TASK_SUBMITTED, task_id=task_id
        )
    total = await engine.store.events.count_for_task(task_id)
    assert total >= 3

    storage = await client.get("/api/storage")
    assert storage.status_code == 200
    assert storage.json()["database"]["row_counts"]["task"] == 1

    dry = await client.post(
        "/api/storage/prune", json={"task_id": task_id, "dry_run": True, "keep_last": 1}
    )
    assert dry.status_code == 200
    preview = dry.json()
    assert preview["dry_run"] is True
    assert preview["actions"], "清理必须说明**要删什么**，不是只回一个数字"
    action = preview["actions"][0]
    assert action["kind"] == "events"
    assert action["scope"] == f"task={task_id}", "要指出作用在哪个范围"
    assert action["deleted"] == total - 1, "keep_last=1：应报告将删到只剩 1 条"
    assert action["detail"]["total_before"] == total
    assert await engine.store.events.count_for_task(task_id) == total, "预演不得真的删"

    real = await client.post(
        "/api/storage/prune",
        json={"task_id": task_id, "dry_run": False, "keep_last": 1},
    )
    assert real.status_code == 200
    body = real.json()
    assert body["dry_run"] is False
    assert body["actions"][0]["deleted"] == total - 1
    assert await engine.store.events.count_for_task(task_id) == 1

    # 清理本身也要进历史：事后能回答「谁在什么时候删掉了什么」
    after = (await client.get("/api/system/events?limit=200")).json()["events"]
    pruned = [e for e in after if e["type"] == "storage.pruned"]
    assert pruned and pruned[-1]["payload"]["actions"][0]["deleted"] == total - 1


# ===========================================================================
# 事件与推送（OBS-02、REC-01）
# ===========================================================================


async def test_events_endpoint_supports_incremental_pull(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    first = (await client.get("/api/system/events?limit=3")).json()
    assert first["returned"] <= 3
    assert first["latest_event_id"] >= first["events"][-1]["event_id"] if first["events"] else True

    watermark = first["events"][-1]["event_id"] if first["events"] else 0
    await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})

    later = (await client.get(f"/api/system/events?after_id={watermark}")).json()
    assert later["events"], "after_id 之后应有新事件"
    assert all(e["event_id"] > watermark for e in later["events"])


async def test_task_events_are_scoped(client: Any, publish: Any) -> None:
    wf, _ = await publish()
    task_id = (
        await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
    ).json()["task_id"]
    page = (await client.get(f"/api/tasks/{task_id}/events")).json()
    assert page["events"]
    assert all(e["task_id"] == task_id for e in page["events"])


async def test_entry_point_binds_secrets_before_serving(engine: Engine) -> None:
    """启动入口在开始服务之前，必须把库里的密值真正登记进脱敏器。

    这里单独测一次，是因为「凭据不进事件历史」这条保证依赖它真的发生过——
    仅仅是调用了解锁接口并不算数。
    """
    from workerbee.server.main import _bind_secrets_for_redaction

    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    leftover = await engine.unlock_secrets(PASSPHRASE)
    if asyncio.iscoroutine(leftover):  # 内核漏了 await，见凭据测试里的说明
        leftover.close()
    await engine.secret_store.put("secret://bound", {"token": PLAIN_SECRET})

    assert await _bind_secrets_for_redaction(engine) >= 1

    await engine.store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.SYSTEM_START,
        payload={"note": PLAIN_SECRET},
    )
    rows = await engine.store.events.tail(after_id=0, limit=50)
    assert PLAIN_SECRET not in json.dumps(rows), "登记之后密值不应再进历史"


async def test_websocket_pushes_notification_after_submit(app: Any, client: Any, publish: Any) -> None:
    """订阅之后再有状态变化，必须收到推送。"""
    wf, _ = await publish()

    async with RawWebSocket(app) as ws:
        hello = await ws.receive_json()
        assert hello["type"] == "hello"
        assert "latest_event_id" in hello

        created = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
        task_id = created.json()["task_id"]

        frame = await ws.receive_until(
            lambda m: m["type"] == "notification" and m.get("task_id") == task_id
        )
        assert frame["kind"] == "state_changed"


async def test_websocket_resume_backfills_events(app: Any, engine: Engine) -> None:
    """REC-01：断连后按 after_event_id 补齐事件。"""
    for i in range(3):
        await engine.store.events.append(
            scope=EventScope.SYSTEM,
            type=EventType.SYSTEM_START,
            payload={"seq": i},
        )

    async with RawWebSocket(app) as ws:
        hello = await ws.receive_json()
        assert hello["latest_event_id"] >= 3

        await ws.send({"type": "resume", "after_event_id": 1})
        first = await ws.receive_json()
        assert first["type"] == "event"
        assert first["event"]["event_id"] == 2

        done = await ws.receive_until(lambda m: m["type"] == "resume_complete")
        assert done["after_event_id"] == 1
        assert done["returned"] == 2
        assert done["latest_event_id"] >= 3
        assert "至少一次" in done["note"], "补拉必须说明投递语义，前端才能去重"


async def test_websocket_rejects_bad_token(app: Any) -> None:
    """浏览器只能在查询参数里带令牌；不匹配时以 4401 关闭并说明原因（UI-02）。"""
    async with RawWebSocket(app, token="nope") as ws:
        frame = await ws.close_frame()
        assert frame["type"] == "websocket.close"
        assert frame["code"] == 4401
        assert "nope" not in json.dumps(frame.get("reason", ""))


async def test_websocket_ping_pong(app: Any) -> None:
    async with RawWebSocket(app) as ws:
        await ws.receive_json()  # hello
        await ws.send({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"
        await ws.send({"type": "nonsense"})
        assert (await ws.receive_until(lambda m: m["type"] == "error"))["detail"]


# ===========================================================================
# 凭据不外泄（AUTH-02）
# ===========================================================================


async def test_credentials_never_appear_in_any_response(
    client: Any, engine: Engine, publish: Any, tmp_path: Path
) -> None:
    """凭据只以引用出入：任何响应体里都不允许出现密值。"""
    # 首次运行没有库文件，先建一个空库（等价于第一次带 --passphrase 启动）
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    leftover = await engine.unlock_secrets(PASSPHRASE)
    if asyncio.iscoroutine(leftover):  # 内核漏了 await bind_store，见下
        leftover.close()
    await engine.secret_store.put(
        "secret://openai",
        {"api_key": SECRET, "note": PLAIN_SECRET},
        label="openai",
    )
    # 把库里的值登记进脱敏器（组合根解锁后要做的事）。
    # 这里显式绑定而不是再调一次 unlock_secrets：后者目前没有 await
    # SecretRedactor.bind_store，登记不会真的发生——那属于内核侧的缺陷，
    # 本测试只对「网关不把密值送出去」负责，不替它掩盖。
    redactor = SecretRedactor()
    assert await redactor.bind_store(engine.secret_store) == 2
    engine.store.events.set_redactor(redactor)

    created = await client.post(
        "/api/credentials",
        json={
            "label": "openai",
            "kind": CredentialKind.API_KEY.value,
            "secret_locator": "secret://openai",
        },
    )
    assert created.status_code == 200
    assert created.json()["secret_locator"] == "secret://openai"
    # 只回引用：响应字段不许超出 CredentialRef 的元数据字段（AUTH-02）
    assert set(created.json()) <= {
        "credential_id",
        "label",
        "kind",
        "secret_locator",
        "base_url",
        "revoked",
        "created_at",
        "updated_at",
    }, created.json()
    assert SECRET not in created.text
    assert PLAIN_SECRET not in created.text

    # 试图把密值塞进事件历史：脱敏器必须拦住（AUTH-02）。
    # PLAIN_SECRET 刻意不含任何可被规则识别的形状，只有「已知值」这条路径能拦住它。
    await engine.store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.SYSTEM_START,
        payload={"note": f"key={SECRET}", "OPENAI_API_KEY": PLAIN_SECRET},
    )
    # 试图把密值塞进 harness 配置：写入时就该失败，而不是等它进了历史
    rejected = await client.post(
        "/api/harnesses",
        json={
            "name": "leaky",
            "adapter_id": "fake",
            "env_template": {"OPENAI_API_KEY": SECRET},
        },
    )
    assert rejected.status_code == 400
    assert SECRET not in rejected.text

    wf, _ = await publish()
    # 注意：密值刻意不塞进 input_payload。任务入参是用户自己写下的正文，网关原样回显
    # 才不撒谎；它要出系统时会过安全层的出站扫描。这里测的是**凭据本体**不外泄——
    # 即凭据库里的值不得因为某个接口顺带把它带出去。
    await client.post(
        f"/api/workflows/{wf.workflow_id}/tasks",
        json={"input_payload": {"goal": "x"}},
    )
    for path in (
        "/api/credentials",
        "/api/harnesses",
        "/api/workflows",
        "/api/tasks",
        "/api/attention",
        "/api/storage",
        "/api/system/status",
        "/api/system/events?limit=500",
        "/api/templates",
    ):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert SECRET not in resp.text, f"{path} 的响应体里出现了密值"
        assert PLAIN_SECRET not in resp.text, f"{path} 的响应体里出现了密值（已知值路径）"
        assert "sk-live-" not in resp.text, f"{path} 的响应体里出现了密钥片段"

    # 事件列表本身必须真的包含那条被脱敏的历史，否则上面的断言等于没测
    events = (await client.get("/api/system/events?limit=500")).json()["events"]
    assert any(e["payload"] for e in events), "脱敏后的事件应当仍在历史里，只是值被遮掉"


async def test_template_instantiate_reports_missing_bindings(
    client: Any, engine: Engine, publish: Any
) -> None:
    """未绑定的槽位如实列出，绝不编造一个凭据顶上（TPL-03）。"""
    wf, _ = await publish()
    await engine.store.registry.upsert_credential(
        _credential("cred-1", label="openai")
    )

    made = await client.post(
        "/api/templates",
        json={"name": "tpl", "kind": "workflow", "from_workflow_id": wf.workflow_id},
    )
    assert made.status_code == 200, made.text
    template_id = made.json()["template_id"]

    resp = await client.post(f"/api/templates/{template_id}/instantiate", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["usable"] is True, "该流程没有凭据槽位，理应可直接实例化"
    assert [n["node_id"] for n in body["nodes"]]
    assert SECRET not in resp.text


def _credential(credential_id: str, *, label: str) -> Any:
    from workerbee.core.domain import CredentialRef

    return CredentialRef(
        credential_id=credential_id,
        label=label,
        kind=CredentialKind.API_KEY,
        secret_locator=f"secret://{label}",
    )


async def test_credential_revoke_is_a_post_action(client: Any, engine: Engine) -> None:
    await engine.store.registry.upsert_credential(_credential("cred-9", label="kimi"))

    revoked = await client.post("/api/credentials/cred-9/revoke", json={"revoked": True})
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert revoked.json()["secret_locator"] == "secret://kimi"

    restored = await client.post("/api/credentials/cred-9/revoke")
    assert restored.json()["revoked"] is True, "缺省即撤销；传 revoked=false 才恢复"


async def test_credential_created_with_secret_material(client: Any, engine: Engine) -> None:
    """随请求提交密钥本体：内核写入凭据库、自动建 locator、立即登记脱敏。"""
    # 凭据库未解锁（内核没带口令启动）：明确拒绝，不悄悄只存引用
    rejected = await client.post(
        "/api/credentials",
        json={"label": "k1", "kind": "api_key", "secret": {"api_key": SECRET}},
    )
    assert rejected.status_code == 400
    assert "凭据库未解锁" in rejected.json()["detail"]
    assert SECRET not in rejected.text

    # harness_login 没有密钥本体，带 secret 是配置错误
    wrong = await client.post(
        "/api/credentials",
        json={"label": "k2", "kind": "harness_login", "secret": {"api_key": SECRET}},
    )
    assert wrong.status_code == 400
    assert SECRET not in wrong.text

    # 解锁后：正常写入
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    await engine.unlock_secrets(PASSPHRASE)

    created = await client.post(
        "/api/credentials",
        json={"label": "k1", "kind": "api_key", "secret": {"api_key": SECRET}},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["secret_locator"], "带密钥创建时必须自动生成 locator"
    assert SECRET not in created.text

    # 密钥真的进了凭据库，且能被解析路径读到
    stored = await engine.secret_store.get(body["secret_locator"])
    assert stored == {"api_key": SECRET}

    # 新写入的密值立即纳入脱敏：不需要重启内核才生效
    await engine.store.events.append(
        scope=EventScope.SYSTEM,
        type=EventType.SYSTEM_START,
        payload={"note": f"key={SECRET}"},
    )
    events = (await client.get("/api/system/events?limit=50")).json()["events"]
    assert SECRET not in json.dumps(events, ensure_ascii=False)


async def test_credential_secret_can_pair_with_base_url(client: Any, engine: Engine) -> None:
    """base_url_pair：url 与 key 一起进凭据库，url 同时留在引用上供展示。"""
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    await engine.unlock_secrets(PASSPHRASE)

    created = await client.post(
        "/api/credentials",
        json={
            "label": "self-hosted",
            "kind": "base_url_pair",
            "base_url": "https://llm.example.com/v1",
            "secret": {"api_key": SECRET, "base_url": "https://llm.example.com/v1"},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["base_url"] == "https://llm.example.com/v1"
    stored = await engine.secret_store.get(body["secret_locator"])
    assert stored["api_key"] == SECRET
    assert stored["base_url"] == "https://llm.example.com/v1"
    assert SECRET not in created.text


# ===========================================================================
# 注册表与模板的增删
# ===========================================================================


async def test_harness_lifecycle_and_probe(client: Any) -> None:
    created = await client.post(
        "/api/harnesses", json={"name": "kimi", "adapter_id": "fake"}
    )
    assert created.status_code == 200
    harness_id = created.json()["harness_id"]

    probed = await client.post(f"/api/harnesses/{harness_id}/probe")
    assert probed.status_code == 200
    assert probed.json()["source"] in ("probe", "capabilities")
    assert probed.json()["ok"] is True

    patched = await client.patch(
        f"/api/harnesses/{harness_id}", json={"exec_path": "/usr/bin/true"}
    )
    assert patched.json()["exec_path"] == "/usr/bin/true"

    assert (await client.get("/api/harnesses")).json()[0]["harness_id"] == harness_id
    assert (await client.delete(f"/api/harnesses/{harness_id}")).json() == 1
    assert (await client.get("/api/harnesses")).json() == []


async def test_unknown_objects_are_404(client: Any) -> None:
    assert (await client.get("/api/workflows/nope")).status_code == 404
    assert (await client.get("/api/tasks/nope")).status_code == 404
    assert (await client.get("/api/credentials/nope")).status_code == 404
    assert (await client.get("/api/templates/nope")).status_code == 404
    assert (
        await client.post("/api/approvals/nope/decide", json={"approve": True})
    ).status_code == 404


async def test_request_level_validation_is_422(client: Any, publish: Any) -> None:
    """字段拼错、取值越界在请求层就失败（严格模式），不进业务代码。"""
    wf, _ = await publish()
    typo = await client.post(
        f"/api/workflows/{wf.workflow_id}/tasks", json={"input_paylod": {}}
    )
    assert typo.status_code == 422
    assert typo.json()["detail"]

    out_of_range = await client.post(
        f"/api/workflows/{wf.workflow_id}/tasks", json={"priority": 999}
    )
    assert out_of_range.status_code == 422
