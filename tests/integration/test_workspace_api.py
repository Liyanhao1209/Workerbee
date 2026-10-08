"""工作区 API 的端到端测试（v0.03 §3、D-B）。

路由 → 服务 → 真实内核：CRUD、根目录唯一冲突（409）、归档冻结发射（409）、
删除前先迁移（409）、改归属（move）、按工作区过滤列表。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from workerbee.app import Engine, EngineConfig
from workerbee.core.domain import (
    HarnessRegistration,
    RevisionSource,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from workerbee.server.app import create_app
from workerbee.server.auth import TOKEN_HEADER

from tests.fakes import FakeHarness
from tests.helpers import graph

pytestmark = pytest.mark.integration

TOKEN = "test-token-8f3c-not-a-secret"


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
async def client(engine: Engine) -> Any:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app(engine, token=TOKEN)),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as c:
        yield c


async def _publish(engine: Engine, *, name: str = "wf") -> WorkflowDefinition:
    """直接经仓储发布一个单节点 Workflow（绕开修订端点）。"""
    await engine.store.registry.upsert_harness(
        HarnessRegistration(harness_id="h1", name="h1", adapter_id="fake", last_probe_ok=True)
    )
    wf = WorkflowDefinition(name=name, status=WorkflowStatus.DRAFT)
    await engine.store.workflows.create(wf)
    rev = WorkflowRevision(
        workflow_id=wf.workflow_id,
        revision_seq=1,
        graph=graph({"A": []}),
        source=RevisionSource.MANUAL,
        is_published=True,
    )
    await engine.store.workflows.save_revision(rev, publish=True, expected_revision_seq=0)
    return wf


async def _create_ws(client: Any, root: Path, name: str = "项目") -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    resp = await client.post(
        "/api/workspaces", json={"name": name, "root_dir": str(root)}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===========================================================================
# CRUD
# ===========================================================================


class TestWorkspaceCrud:
    async def test_默认工作区永远在列表里(self, client: Any) -> None:
        resp = await client.get("/api/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert any(w["workspace_id"] == "default" for w in body["workspaces"])
        # serve 没有运行过：没有「当前工作区」记录，字段如实为 None
        assert body["current_workspace_id"] is None

    async def test_创建与详情(self, client: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        assert ws["name"] == "项目"
        assert ws["root_dir"] == str((tmp_path / "proj").resolve())
        assert ws["archived"] is False

        resp = await client.get(f"/api/workspaces/{ws['workspace_id']}")
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == ws["workspace_id"]

    async def test_根目录必须已存在(self, client: Any, tmp_path: Path) -> None:
        resp = await client.post(
            "/api/workspaces",
            json={"name": "幽灵", "root_dir": str(tmp_path / "no-such-dir")},
        )
        assert resp.status_code == 400

    async def test_根目录冲突返回409(self, client: Any, tmp_path: Path) -> None:
        await _create_ws(client, tmp_path / "proj")
        resp = await client.post(
            "/api/workspaces",
            json={"name": "重复", "root_dir": str(tmp_path / "proj")},
        )
        assert resp.status_code == 409

    async def test_改名与归档(self, client: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        wid = ws["workspace_id"]

        resp = await client.patch(f"/api/workspaces/{wid}", json={"name": "新名"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "新名"

        resp = await client.post(f"/api/workspaces/{wid}/archive")
        assert resp.status_code == 200
        assert resp.json()["archived"] is True

        # 取消归档
        resp = await client.patch(f"/api/workspaces/{wid}", json={"archived": False})
        assert resp.status_code == 200
        assert resp.json()["archived"] is False

    async def test_不存在的工作区返回404(self, client: Any) -> None:
        assert (await client.get("/api/workspaces/ghost")).status_code == 404
        assert (await client.patch("/api/workspaces/ghost", json={"name": "x"})).status_code == 404


# ===========================================================================
# 删除安全边界
# ===========================================================================


class TestWorkspaceDelete:
    async def test_默认工作区不可删除(self, client: Any) -> None:
        resp = await client.delete("/api/workspaces/default")
        assert resp.status_code == 400

    async def test_仍有流程的工作区不可删除(self, client: Any, engine: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        wf = await _publish(engine)
        resp = await client.post(
            f"/api/workflows/{wf.workflow_id}/move",
            json={"workspace_id": ws["workspace_id"]},
        )
        assert resp.status_code == 200

        resp = await client.delete(f"/api/workspaces/{ws['workspace_id']}")
        assert resp.status_code == 409
        assert "迁移" in (resp.json().get("hint") or "")

        # 迁走之后就能删
        await client.post(
            f"/api/workflows/{wf.workflow_id}/move", json={"workspace_id": "default"}
        )
        resp = await client.delete(f"/api/workspaces/{ws['workspace_id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True


# ===========================================================================
# 流程归属与过滤
# ===========================================================================


class TestWorkflowOwnership:
    async def test_新建流程归入指定工作区(self, client: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        resp = await client.post(
            "/api/workflows", json={"name": "归我", "workspace_id": ws["workspace_id"]}
        )
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == ws["workspace_id"]

    async def test_省略时归默认工作区(self, client: Any) -> None:
        resp = await client.post("/api/workflows", json={"name": "默认"})
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == "default"

    async def test_不存在或已归档的工作区拒绝新建(self, client: Any, tmp_path: Path) -> None:
        resp = await client.post(
            "/api/workflows", json={"name": "x", "workspace_id": "ghost"}
        )
        assert resp.status_code == 404

        ws = await _create_ws(client, tmp_path / "proj")
        await client.post(f"/api/workspaces/{ws['workspace_id']}/archive")
        resp = await client.post(
            "/api/workflows", json={"name": "x", "workspace_id": ws["workspace_id"]}
        )
        assert resp.status_code == 409

    async def test_按工作区过滤流程列表(self, client: Any, engine: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        wf_keep = await _publish(engine, name="留下")
        await _publish(engine, name="过滤掉")
        await client.post(
            f"/api/workflows/{wf_keep.workflow_id}/move",
            json={"workspace_id": ws["workspace_id"]},
        )

        resp = await client.get(f"/api/workflows?workspace_id={ws['workspace_id']}")
        assert resp.status_code == 200
        names = [w["name"] for w in resp.json()["workflows"]]
        assert names == ["留下"]

        resp = await client.get("/api/workflows")
        assert len(resp.json()["workflows"]) == 2

    async def test_按工作区过滤任务列表(self, client: Any, engine: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        wf_a = await _publish(engine, name="A组")
        wf_b = await _publish(engine, name="B组")
        await client.post(
            f"/api/workflows/{wf_a.workflow_id}/move",
            json={"workspace_id": ws["workspace_id"]},
        )
        r1 = await client.post(f"/api/workflows/{wf_a.workflow_id}/tasks", json={})
        r2 = await client.post(f"/api/workflows/{wf_b.workflow_id}/tasks", json={})
        assert r1.status_code == 202 and r2.status_code == 202

        resp = await client.get(f"/api/tasks?workspace_id={ws['workspace_id']}")
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["workflow_id"] == wf_a.workflow_id

    async def test_迁移到已归档工作区被拒绝(self, client: Any, engine: Any, tmp_path: Path) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        await client.post(f"/api/workspaces/{ws['workspace_id']}/archive")
        wf = await _publish(engine)
        resp = await client.post(
            f"/api/workflows/{wf.workflow_id}/move",
            json={"workspace_id": ws["workspace_id"]},
        )
        assert resp.status_code == 409


# ===========================================================================
# 归档冻结发射（409）
# ===========================================================================


class TestArchivedSubmission:
    async def test_归档工作区的流程提交返回409(
        self, client: Any, engine: Any, tmp_path: Path
    ) -> None:
        ws = await _create_ws(client, tmp_path / "proj")
        wf = await _publish(engine)
        await client.post(
            f"/api/workflows/{wf.workflow_id}/move",
            json={"workspace_id": ws["workspace_id"]},
        )
        # 归档前能发射
        resp = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
        assert resp.status_code == 202

        await client.post(f"/api/workspaces/{ws['workspace_id']}/archive")
        resp = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
        assert resp.status_code == 409
        assert "已归档" in resp.json()["detail"]

        # 取消归档后恢复
        await client.patch(f"/api/workspaces/{ws['workspace_id']}", json={"archived": False})
        resp = await client.post(f"/api/workflows/{wf.workflow_id}/tasks", json={})
        assert resp.status_code == 202
