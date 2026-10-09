"""Web Chat 与文件系统 REST 端点的集成测试（v0.03 §5）。

路由 → 服务 → 真实内核：会话 CRUD、消息收发（假 LLM 后端）、fs 六个端点的
confinement/乐观并发/敏感名语义、写类工具经 REST 审批的端到端闭环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from workerbee.app import Engine, EngineConfig
from workerbee.data.llm import LLMToolCall
from workerbee.security.secret_store import SecretStore
from workerbee.server.app import create_app
from workerbee.server.auth import TOKEN_HEADER

from tests.fakes import FakeHarness, FakeLLMBackend

pytestmark = pytest.mark.integration

TOKEN = "test-token-8f3c-not-a-secret"
PASSPHRASE = "test-passphrase-please-change"
SECRET = "sk-live-DEADBEEFdeadbeef0123456789"


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
    (tmp_path / "workspace").mkdir(exist_ok=True)
    yield eng
    await eng.store.close()


@pytest.fixture
async def client(engine: Engine) -> Any:
    app = create_app(engine, token=TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
        headers={TOKEN_HEADER: TOKEN},
    ) as c:
        yield c


@pytest.fixture
def workspace_root(engine: Engine, tmp_path: Path) -> Path:
    return tmp_path / "workspace"


async def _setup_credential(engine: Engine, client: Any) -> str:
    """解锁凭据库、建凭据、启用助手配置（chat 的后端回落到它）。"""
    vault = engine.config.data_dir / "secrets.vault"
    await SecretStore.create(PASSPHRASE, vault)
    await engine.unlock_secrets(PASSPHRASE)
    created = await client.post(
        "/api/credentials",
        json={
            "label": "Chat API",
            "kind": "base_url_pair",
            "base_url": "https://llm.example.com/v1",
            "default_model": "gpt-test",
            "secret": {"api_key": SECRET, "base_url": "https://llm.example.com/v1"},
        },
    )
    assert created.status_code == 200, created.text
    credential_id = created.json()["credential_id"]
    updated = await client.put(
        "/api/assistant/config", json={"enabled": True, "credential_ref": credential_id}
    )
    assert updated.status_code == 200, updated.text
    return credential_id


def _plug_backend(engine: Engine, backend: FakeLLMBackend) -> None:
    engine.chat.backend_factory = lambda cfg, cred, secrets: backend


# ===========================================================================
# 会话 CRUD
# ===========================================================================


class TestSessionApi:
    async def test_创建列表详情改名删除(self, client: Any) -> None:
        created = await client.post("/api/chat/sessions", json={"title": "调研"})
        assert created.status_code == 200, created.text
        session = created.json()
        assert session["title"] == "调研"
        assert session["workspace_id"] == "default"

        listed = await client.get("/api/chat/sessions")
        assert listed.status_code == 200
        assert listed.json()["returned"] == 1

        got = await client.get(f"/api/chat/sessions/{session['session_id']}")
        assert got.status_code == 200

        renamed = await client.patch(
            f"/api/chat/sessions/{session['session_id']}", json={"title": "改名了"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "改名了"

        deleted = await client.delete(f"/api/chat/sessions/{session['session_id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert (await client.get(f"/api/chat/sessions/{session['session_id']}")).status_code == 404

    async def test_按工作区过滤(self, client: Any, tmp_path: Path) -> None:
        (tmp_path / "proj").mkdir()
        ws = await client.post(
            "/api/workspaces", json={"name": "项目", "root_dir": str(tmp_path / "proj")}
        )
        assert ws.status_code == 200
        ws_id = ws.json()["workspace_id"]
        await client.post("/api/chat/sessions", json={"title": "默认区的"})
        await client.post("/api/chat/sessions", json={"title": "项目区的", "workspace_id": ws_id})

        all_sessions = await client.get("/api/chat/sessions")
        assert all_sessions.json()["returned"] == 2
        filtered = await client.get(f"/api/chat/sessions?workspace_id={ws_id}")
        body = filtered.json()
        assert body["returned"] == 1
        assert body["sessions"][0]["title"] == "项目区的"

    async def test_改名校验是大白话400(self, client: Any) -> None:
        session = (await client.post("/api/chat/sessions", json={})).json()
        resp = await client.patch(
            f"/api/chat/sessions/{session['session_id']}", json={"title": "  "}
        )
        assert resp.status_code == 400
        assert "不能为空" in resp.json()["detail"]

    async def test_幽灵会话404(self, client: Any) -> None:
        assert (await client.get("/api/chat/sessions/ghost")).status_code == 404
        assert (
            await client.get("/api/chat/sessions/ghost/messages")
        ).status_code == 404
        resp = await client.post(
            "/api/chat/sessions/ghost/messages", json={"content": "问"}
        )
        assert resp.status_code == 404


class TestGrantApi:
    """会话级临时授权（D-G）：授予/撤销/幂等/非法类别。"""

    async def test_授予与撤销(self, client: Any) -> None:
        session = (await client.post("/api/chat/sessions", json={})).json()
        sid = session["session_id"]
        assert session["grants"] == []

        granted = await client.put(
            f"/api/chat/sessions/{sid}/grants", json={"category": "run"}
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["grants"] == ["run"]

        # 列表与详情也透出授权状态
        got = await client.get(f"/api/chat/sessions/{sid}")
        assert got.json()["grants"] == ["run"]

        revoked = await client.delete(f"/api/chat/sessions/{sid}/grants/run")
        assert revoked.status_code == 200
        assert revoked.json()["grants"] == []

    async def test_非法类别400(self, client: Any) -> None:
        session = (await client.post("/api/chat/sessions", json={})).json()
        resp = await client.put(
            f"/api/chat/sessions/{session['session_id']}/grants",
            json={"category": "everything"},
        )
        assert resp.status_code == 400
        assert "类别" in resp.json()["detail"]

    async def test_幽灵会话404(self, client: Any) -> None:
        resp = await client.put(
            "/api/chat/sessions/ghost/grants", json={"category": "run"}
        )
        assert resp.status_code == 404

    async def test_审批决定附带会话授权(
        self, client: Any, engine: Engine, workspace_root: Path
    ) -> None:
        """批准时勾选「本会话不再询问」→ 该会话的 run 类别授权落库。"""
        await _setup_credential(engine, client)
        backend = FakeLLMBackend(
            {
                "tool_calls": [
                    LLMToolCall(id="c-run", name="fs_run", arguments={"command": "echo ok"})
                ]
            },
            "跑完了。",
            supports_tools=True,
        )
        _plug_backend(engine, backend)
        session = (await client.post("/api/chat/sessions", json={})).json()

        send = asyncio.create_task(
            client.post(
                f"/api/chat/sessions/{session['session_id']}/messages",
                json={"content": "跑个命令"},
            )
        )
        approval_id = None
        for _ in range(200):
            approvals = (await client.get("/api/approvals")).json()["approvals"]
            if approvals:
                approval_id = approvals[0]["approval_id"]
                break
            await asyncio.sleep(0.02)
        assert approval_id is not None, "run 工具没有触发审批"

        decided = await client.post(
            f"/api/approvals/{approval_id}/decide",
            json={"approve": True, "grant_session": True},
        )
        assert decided.status_code == 200, decided.text
        assert "不再询问" in (decided.json()["detail"] or "")
        resp = await send
        assert resp.status_code == 200, resp.text

        got = await client.get(f"/api/chat/sessions/{session['session_id']}")
        assert got.json()["grants"] == ["run"]

    async def test_非chat审批的授权标志如实告知未生效(self, client: Any, engine: Engine) -> None:
        """grant_session 对非 chat 域审批不生效，返回里要如实说明。"""
        approval = await engine.approvals.request(
            approval_id="ap-non-chat",
            task_id="task-x",
            stage_id="stage-x",
            attempt_id="attempt-x",
            revision_seq=1,
            node_id=None,
            action="执行 harness 命令",
            tool_name="Bash",
        )
        decided = await client.post(
            f"/api/approvals/{approval.approval_id}/decide",
            json={"approve": True, "grant_session": True},
        )
        assert decided.status_code == 200, decided.text
        assert "只对对话里的操作生效" in (decided.json()["detail"] or "")


# ===========================================================================
# 消息收发
# ===========================================================================


class TestMessageApi:
    async def test_发送与读回(self, client: Any, engine: Engine) -> None:
        await _setup_credential(engine, client)
        _plug_backend(engine, FakeLLMBackend("接口层回答。"))
        session = (await client.post("/api/chat/sessions", json={})).json()

        sent = await client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"content": "你好"},
        )
        assert sent.status_code == 200, sent.text
        body = sent.json()
        assert body["reply"]["content"] == "接口层回答。"
        assert body["supports_tools"] is False
        assert any("仅纯对话" in r for r in body["degraded_reasons"])

        history = await client.get(f"/api/chat/sessions/{session['session_id']}/messages")
        assert history.status_code == 200
        messages = history.json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "接口层回答。"

    async def test_空白内容被拒(self, client: Any, engine: Engine) -> None:
        session = (await client.post("/api/chat/sessions", json={})).json()
        resp = await client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"content": "   "},
        )
        assert resp.status_code == 400

    async def test_未配置凭据时400带指引(self, client: Any, engine: Engine) -> None:
        session = (await client.post("/api/chat/sessions", json={})).json()
        resp = await client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"content": "问"},
        )
        assert resp.status_code == 400
        assert resp.json()["hint"]

    async def test_引用注入经REST(
        self, client: Any, engine: Engine, workspace_root: Path
    ) -> None:
        (workspace_root / "readme.txt").write_text("关键内容甲乙丙", encoding="utf-8")
        await _setup_credential(engine, client)
        backend = FakeLLMBackend("读到了")
        _plug_backend(engine, backend)
        session = (await client.post("/api/chat/sessions", json={})).json()
        resp = await client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"content": "看下引用", "refs": ["readme.txt"]},
        )
        assert resp.status_code == 200, resp.text
        assert "关键内容甲乙丙" in resp.json()["user_node"]["content"]

    async def test_写类工具经REST审批闭环(
        self, client: Any, engine: Engine, workspace_root: Path
    ) -> None:
        """模型发起 fs_write → 审批出现在 /api/approvals → 批准 → 文件落盘。"""
        await _setup_credential(engine, client)
        backend = FakeLLMBackend(
            {
                "tool_calls": [
                    LLMToolCall(
                        id="c1",
                        name="fs_write",
                        arguments={"path": "api-out.txt", "content": "经审批的内容"},
                    )
                ]
            },
            "写好了。",
            supports_tools=True,
        )
        _plug_backend(engine, backend)
        session = (await client.post("/api/chat/sessions", json={})).json()

        send = asyncio.create_task(
            client.post(
                f"/api/chat/sessions/{session['session_id']}/messages",
                json={"content": "写一个文件"},
            )
        )
        # 等审批出现并批准（模拟审批中心的用户操作）
        approval_id = None
        for _ in range(200):
            approvals = (await client.get("/api/approvals")).json()["approvals"]
            if approvals:
                approval_id = approvals[0]["approval_id"]
                break
            await asyncio.sleep(0.02)
        assert approval_id is not None, "写类工具没有触发审批"
        decided = await client.post(
            f"/api/approvals/{approval_id}/decide", json={"approve": True}
        )
        assert decided.status_code == 200, decided.text

        resp = await send
        assert resp.status_code == 200, resp.text
        assert resp.json()["reply"]["content"] == "写好了。"
        assert (workspace_root / "api-out.txt").read_text(encoding="utf-8") == "经审批的内容"


# ===========================================================================
# 文件系统端点
# ===========================================================================


class TestFsApi:
    async def test_列表与读取(self, client: Any, workspace_root: Path) -> None:
        (workspace_root / "sub").mkdir()
        (workspace_root / "sub" / "f.txt").write_text("内容", encoding="utf-8")
        (workspace_root / "top.txt").write_text("顶层", encoding="utf-8")

        listed = await client.get("/api/fs/list", params={"path": ""})
        assert listed.status_code == 200, listed.text
        names = [e["name"] for e in listed.json()["entries"]]
        assert names == ["sub", "top.txt"]  # 目录在前

        read = await client.get("/api/fs/read", params={"path": "sub/f.txt"})
        assert read.status_code == 200
        body = read.json()
        assert body["content"] == "内容"
        assert body["truncated"] is False
        assert body["mtime"]

    async def test_越界403(self, client: Any) -> None:
        for params in ({"path": "../x"}, {"path": "/etc/passwd"}):
            resp = await client.get("/api/fs/read", params=params)
            assert resp.status_code == 403, resp.text
        resp = await client.put(
            "/api/fs/write", json={"path": "../evil.txt", "content": "x"}
        )
        assert resp.status_code == 403

    async def test_敏感文件403(self, client: Any, workspace_root: Path) -> None:
        (workspace_root / ".env").write_text("KEY=x", encoding="utf-8")
        resp = await client.get("/api/fs/read", params={"path": ".env"})
        assert resp.status_code == 403
        # 但列表里如实可见（sensitive 标注）
        listed = await client.get("/api/fs/list", params={"path": ""})
        entry = next(e for e in listed.json()["entries"] if e["name"] == ".env")
        assert entry["sensitive"] is True

    async def test_写入与乐观并发409(self, client: Any, workspace_root: Path) -> None:
        created = await client.put(
            "/api/fs/write", json={"path": "f.txt", "content": "v1"}
        )
        assert created.status_code == 200, created.text

        # 覆盖不带 expected_mtime → 409
        conflict = await client.put(
            "/api/fs/write", json={"path": "f.txt", "content": "v2"}
        )
        assert conflict.status_code == 409
        assert "mtime" in conflict.json()["detail"]

        # 带上读到的 mtime → 成功
        mtime = (
            await client.get("/api/fs/read", params={"path": "f.txt"})
        ).json()["mtime"]
        ok = await client.put(
            "/api/fs/write",
            json={"path": "f.txt", "content": "v2", "expected_mtime": mtime},
        )
        assert ok.status_code == 200, ok.text
        assert (workspace_root / "f.txt").read_text(encoding="utf-8") == "v2"

        # 用旧 mtime 再写 → 409
        stale = await client.put(
            "/api/fs/write",
            json={"path": "f.txt", "content": "v3", "expected_mtime": mtime},
        )
        assert stale.status_code == 409

    async def test_mkdir_move_delete(self, client: Any, workspace_root: Path) -> None:
        resp = await client.post("/api/fs/mkdir", json={"path": "d1"})
        assert resp.status_code == 200, resp.text
        again = await client.post("/api/fs/mkdir", json={"path": "d1"})
        assert again.json()["detail"]  # 幂等：如实说已存在

        await client.put("/api/fs/write", json={"path": "d1/a.txt", "content": "x"})
        moved = await client.post("/api/fs/move", json={"src": "d1/a.txt", "dst": "b.txt"})
        assert moved.status_code == 200
        assert (workspace_root / "b.txt").exists()

        # 非空目录拒绝删除
        await client.put("/api/fs/write", json={"path": "d1/c.txt", "content": "x"})
        denied = await client.request("DELETE", "/api/fs/delete", json={"path": "d1"})
        assert denied.status_code == 400
        deleted = await client.request("DELETE", "/api/fs/delete", json={"path": "d1/c.txt"})
        assert deleted.status_code == 200
        assert (await client.request("DELETE", "/api/fs/delete", json={"path": "d1"})).status_code == 200

    async def test_不存在404(self, client: Any) -> None:
        resp = await client.get("/api/fs/read", params={"path": "ghost.txt"})
        assert resp.status_code == 404

    async def test_写操作留痕(self, client: Any, engine: Engine) -> None:
        await client.put("/api/fs/write", json={"path": "traced.txt", "content": "x"})
        rows = await engine.store.db.fetch_all(
            "SELECT type, actor, scope FROM event_log WHERE type='fs.write'"
        )
        assert rows
        assert all(r[1] == "user" and r[2] == "chat" for r in rows)

    async def test_越界URL编码变体403(self, client: Any) -> None:
        # %2e%2e%2f 解码后就是 ../——编码绕不过 confinement
        resp = await client.get("/api/fs/read?path=%2e%2e%2f%2e%2e%2fetc%2fpasswd")
        assert resp.status_code == 403, resp.text

    async def test_symlink逃逸403(self, client: Any, workspace_root: Path) -> None:
        (workspace_root / "link.txt").symlink_to("/etc/hostname")
        resp = await client.get("/api/fs/read", params={"path": "link.txt"})
        assert resp.status_code == 403

    async def test_run端点(self, client: Any, engine: Engine, workspace_root: Path) -> None:
        resp = await client.post("/api/fs/run", json={"command": "echo 你好 && pwd"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["exit_code"] == 0
        assert body["timed_out"] is False
        assert "你好" in body["output"]
        assert body["cwd"] == ""

        # 非零退出码如实返回，不是 HTTP 错误
        failed = await client.post("/api/fs/run", json={"command": "exit 7"})
        assert failed.status_code == 200
        assert failed.json()["exit_code"] == 7

        # 用户发起的执行也留痕（actor=user）
        rows = await engine.store.db.fetch_all(
            "SELECT actor, scope, payload FROM event_log WHERE type='fs.run'"
        )
        assert rows
        assert all(r[0] == "user" and r[1] == "chat" for r in rows)

    async def test_run的cwd越界403(self, client: Any) -> None:
        resp = await client.post("/api/fs/run", json={"command": "ls", "cwd": "../"})
        assert resp.status_code == 403

    async def test_run超时如实标注(self, client: Any) -> None:
        resp = await client.post(
            "/api/fs/run", json={"command": "sleep 30", "timeout_seconds": 0.3}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["timed_out"] is True
        assert body["exit_code"] is None
