"""Web Chat 核心服务的集成测试（v0.03 §5，D-D/D-E）。

不经过 HTTP，直接驱动 ``ChatService``（核心编排）+ 真实 Store + 假 LLM 后端。
覆盖：纯对话、工具循环端到端（审批通过/拒绝）、轮次上限、不支持 tools 的
显式降级、@引用注入、上下文跨轮携带、会话 CRUD 与错误语义。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from workerbee.assistant.service import AssistantConfig, save_config
from workerbee.chat.service import (
    ChatError,
    ChatNotConfigured,
    ChatService,
)
from workerbee.core.domain.approval import ApprovalDecision, ApprovalStatus
from workerbee.core.domain.registry import CredentialKind, CredentialRef
from workerbee.data.event_log import EventScope, EventType
from workerbee.data.llm import LLMToolCall
from workerbee.data.store import DEFAULT_WORKSPACE_ID, Store
from workerbee.security.approval_gateway import ApprovalGateway

from tests.fakes import FakeLLMBackend

pytestmark = pytest.mark.integration


class _Notifier:
    """收集推送通知的替身。"""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def publish(self, notification: Any) -> None:
        self.items.append(notification)

    def kinds(self) -> list[str]:
        return [n.kind for n in self.items]


@pytest.fixture
def workspace_root(store: Store, tmp_path: Path) -> Path:
    """store 夹具的默认工作区根目录落到磁盘上。"""
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
async def credential(store: Store) -> str:
    cred = CredentialRef(
        credential_id="cred-1",
        label="测试 API",
        kind=CredentialKind.BASE_URL_PAIR,
        secret_locator="secret:cred-1",
        base_url="https://llm.example.com/v1",
        default_model="gpt-test",
    )
    await store.registry.upsert_credential(cred)
    await save_config(
        store.db, AssistantConfig(enabled=True, credential_ref="cred-1")
    )
    return "cred-1"


def _make_service(
    store: Store,
    backend: FakeLLMBackend,
    *,
    notifier: _Notifier | None = None,
    approval_gateway: ApprovalGateway | None = None,
    max_tool_rounds: int = 25,
) -> ChatService:
    return ChatService(
        store=store,
        notifier=notifier,
        secret_resolver=lambda: object(),  # 已解锁的凭据库的替身
        approval_gateway=approval_gateway,
        backend_factory=lambda cfg, cred, secrets: backend,
        max_tool_rounds=max_tool_rounds,
        approval_poll_interval=0.02,
    )


async def _create_session(service: ChatService, **kwargs: Any) -> dict[str, Any]:
    return await service.create_session(
        workspace_id=kwargs.pop("workspace_id", DEFAULT_WORKSPACE_ID), **kwargs
    )


# ===========================================================================
# 会话 CRUD
# ===========================================================================


class TestSessionCrud:
    async def test_创建与列表与详情(
        self, store: Store, workspace_root: Path
    ) -> None:
        service = _make_service(store, FakeLLMBackend("你好"))
        session = await _create_session(service, title="第一个对话")
        assert session["title"] == "第一个对话"
        assert session["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert session["closed"] is False

        sessions = await service.list_sessions()
        assert [s["session_id"] for s in sessions] == [session["session_id"]]
        assert (await service.get_session(session["session_id"]))["title"] == "第一个对话"

    async def test_自动标题取首条消息(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("回答")
        service = _make_service(store, backend)
        session = await _create_session(service)
        assert session["title"] == ""
        await service.send_message(session["session_id"], "请介绍一下这个工作区的情况")
        updated = await service.get_session(session["session_id"])
        assert updated["title"] == "请介绍一下这个工作区的情况"[:20]

    async def test_重命名校验(self, store: Store, workspace_root: Path) -> None:
        service = _make_service(store, FakeLLMBackend("x"))
        session = await _create_session(service)
        with pytest.raises(ChatError, match="不能为空"):
            await service.rename_session(session["session_id"], "   ")
        renamed = await service.rename_session(session["session_id"], "新名字")
        assert renamed["title"] == "新名字"

    async def test_删除级联节点(self, store: Store, workspace_root: Path, credential: str) -> None:
        service = _make_service(store, FakeLLMBackend("回答"))
        session = await _create_session(service)
        await service.send_message(session["session_id"], "问")
        await service.delete_session(session["session_id"])
        with pytest.raises(KeyError):
            await service.get_session(session["session_id"])
        assert await store.chat.list_nodes(session["session_id"]) == []

    async def test_工作区不存在显式失败(self, store: Store) -> None:
        service = _make_service(store, FakeLLMBackend("x"))
        with pytest.raises(ChatError, match="工作区不存在"):
            await service.create_session(workspace_id="ghost-ws")

    async def test_会话不存在抛KeyError(self, store: Store) -> None:
        service = _make_service(store, FakeLLMBackend("x"))
        with pytest.raises(KeyError):
            await service.send_message("ghost", "问")
        with pytest.raises(KeyError):
            await service.list_messages("ghost")


# ===========================================================================
# 纯对话（后端不支持工具 → 显式降级）
# ===========================================================================


class TestPlainChat:
    async def test_一问一答(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("这是回答。")  # supports_tools 默认 False
        notifier = _Notifier()
        service = _make_service(store, backend, notifier=notifier)
        session = await _create_session(service)

        result = await service.send_message(session["session_id"], "你好")

        assert result["reply"]["content"] == "这是回答。"
        assert result["user_node"]["content"] == "你好"
        assert result["supports_tools"] is False
        assert result["degraded"] is True
        assert any("仅纯对话" in r for r in result["degraded_reasons"])
        # 推送：chat_message 送达；chunk 推送是加速器，有就行
        assert "chat_message" in notifier.kinds()

    async def test_上下文跨轮携带(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("第一答", "第二答")
        service = _make_service(store, backend)
        session = await _create_session(service)
        await service.send_message(session["session_id"], "第一问")
        await service.send_message(session["session_id"], "第二问")

        # 第二轮调用应携带完整历史：system + u1 + a1 + u2
        second_call = backend.calls[-1]
        roles = [m.role for m in second_call]
        assert roles == ["system", "user", "assistant", "user"]
        assert second_call[1].content == "第一问"
        assert second_call[2].content == "第一答"

    async def test_未配置凭据时显式失败(
        self, store: Store, workspace_root: Path
    ) -> None:
        service = _make_service(store, FakeLLMBackend("x"))
        session = await _create_session(service)
        with pytest.raises(ChatNotConfigured):
            await service.send_message(session["session_id"], "问")

    async def test_凭据库未解锁显式失败(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        service = ChatService(
            store=store,
            secret_resolver=lambda: None,  # 锁定
            backend_factory=lambda cfg, cred, secrets: FakeLLMBackend("x"),
        )
        session = await _create_session(service)
        with pytest.raises(ChatError, match="解锁"):
            await service.send_message(session["session_id"], "问")

    async def test_关闭的会话不能再发(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        service = _make_service(store, FakeLLMBackend("x"))
        session = await _create_session(service)
        await store.chat.update_session(session["session_id"], closed=True)
        with pytest.raises(ChatError, match="已关闭"):
            await service.send_message(session["session_id"], "问")

    async def test_消息读取与分叉叶(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("答A", "答B")
        service = _make_service(store, backend)
        session = await _create_session(service)
        first = await service.send_message(session["session_id"], "问")
        # 改问第一条消息：显式挂树根（parent_id=""），与旧问句成为兄弟分支
        await service.send_message(session["session_id"], "问", parent_id="")

        listed = await service.list_messages(session["session_id"])
        assert listed["leaf_id"] is not None
        # 最新分支：user → 答B
        assert [m["content"] for m in listed["messages"]] == ["问", "答B"]
        # 显式取旧分支的叶
        old = await service.list_messages(
            session["session_id"], leaf_id=first["reply"]["node_id"]
        )
        assert [m["content"] for m in old["messages"]] == ["问", "答A"]


# ===========================================================================
# @引用注入
# ===========================================================================


class TestRefs:
    async def test_引用内容注入消息与上下文(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        (workspace_root / "note.txt").write_text("文件里的关键内容", encoding="utf-8")
        backend = FakeLLMBackend("看到了")
        service = _make_service(store, backend)
        session = await _create_session(service)

        result = await service.send_message(
            session["session_id"], "看看这个", refs=["note.txt"]
        )
        assert "文件里的关键内容" in result["user_node"]["content"]
        assert "【引用文件 note.txt】" in result["user_node"]["content"]
        # 模型所见即所存
        assert "文件里的关键内容" in backend.calls[-1][-1].content

    async def test_读不到的引用如实写进消息(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("明白")
        service = _make_service(store, backend)
        session = await _create_session(service)
        result = await service.send_message(
            session["session_id"], "看引用", refs=["../外面.txt", "ghost.txt"]
        )
        content = result["user_node"]["content"]
        assert "读取失败" in content
        assert "ghost.txt" in content


# ===========================================================================
# 工具循环
# ===========================================================================


def _write_call(path: str, content: str) -> dict[str, Any]:
    return {
        "tool_calls": [
            LLMToolCall(id="call-1", name="fs_write", arguments={"path": path, "content": content})
        ]
    }


async def _decide_pending(store: Store, approved: bool) -> None:
    """等 pending 审批出现并做决定（模拟用户在审批中心点按钮）。"""
    for _ in range(200):
        pending = await store.approvals.list_open()
        if pending:
            await store.approvals.decide(
                pending[0].approval_id,
                status=ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
                decision=ApprovalDecision(by="user", approved=approved),
            )
            return
        await asyncio.sleep(0.01)
    raise AssertionError("审批没有出现")


class TestToolLoop:
    async def test_写文件经审批通过(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend(
            _write_call("out.txt", "模型写的内容"),
            "文件已经写好了。",
            supports_tools=True,
        )
        gateway = ApprovalGateway(store=store, timeout_seconds=10.0)
        notifier = _Notifier()
        service = _make_service(
            store, backend, notifier=notifier, approval_gateway=gateway
        )
        session = await _create_session(service)

        send = asyncio.create_task(service.send_message(session["session_id"], "写一个文件"))
        await _decide_pending(store, approved=True)
        result = await send

        assert result["reply"]["content"] == "文件已经写好了。"
        assert result["supports_tools"] is True
        assert (workspace_root / "out.txt").read_text(encoding="utf-8") == "模型写的内容"
        # 节点链：assistant(tool_calls) → tool(结果) → assistant(终答)
        roles = [n["role"] for n in result["nodes"]]
        assert roles == ["assistant", "tool", "assistant"]
        assert result["nodes"][0]["tool_calls"][0]["name"] == "fs_write"
        assert result["nodes"][1]["tool_name"] == "fs_write"
        assert result["nodes"][1]["tool_call_id"] == "call-1"
        # 写操作留痕（actor=ai，scope=chat）
        rows = await store.db.fetch_all(
            "SELECT type, actor, scope FROM event_log WHERE scope='chat'"
        )
        types = {r[0] for r in rows}
        assert "fs.write" in types
        assert any(r[1] == "ai" for r in rows)
        # 审批绑定合成任务 chat:<session_id>
        approvals = await store.approvals.list_for_task(f"chat:{session['session_id']}")
        assert len(approvals) == 1
        assert approvals[0].status == ApprovalStatus.APPROVED
        # 状态推送如实：waiting_approval → approval_decided
        statuses = [
            n.payload["status"]
            for n in notifier.items
            if n.kind == "chat_status"
        ]
        assert "waiting_approval" in statuses
        assert "approval_decided" in statuses

    async def test_审批拒绝则文件未写入且结果如实(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend(
            _write_call("refused.txt", "不该落盘"),
            "用户拒绝了这次写入。",
            supports_tools=True,
        )
        gateway = ApprovalGateway(store=store, timeout_seconds=10.0)
        service = _make_service(store, backend, approval_gateway=gateway)
        session = await _create_session(service)

        send = asyncio.create_task(service.send_message(session["session_id"], "写吧"))
        await _decide_pending(store, approved=False)
        result = await send

        assert not (workspace_root / "refused.txt").exists()
        tool_node = result["nodes"][1]
        assert "未获用户批准" in tool_node["content"]
        assert result["reply"]["content"] == "用户拒绝了这次写入。"

    async def test_无审批网关时写类工具一律拒绝(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend(
            _write_call("nope.txt", "x"), "没写成。", supports_tools=True
        )
        service = _make_service(store, backend, approval_gateway=None)
        session = await _create_session(service)
        result = await service.send_message(session["session_id"], "写文件")
        assert not (workspace_root / "nope.txt").exists()
        assert "未获用户批准" in result["nodes"][1]["content"]

    async def test_读类工具不过审批(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        (workspace_root / "a.txt").write_text("内容甲", encoding="utf-8")
        backend = FakeLLMBackend(
            {
                "tool_calls": [
                    LLMToolCall(id="c1", name="fs_read", arguments={"path": "a.txt"})
                ]
            },
            "文件里是：内容甲",
            supports_tools=True,
        )
        # 不给审批网关：读类工具照常执行
        service = _make_service(store, backend, approval_gateway=None)
        session = await _create_session(service)
        result = await service.send_message(session["session_id"], "读一下 a.txt")
        tool_node = result["nodes"][1]
        assert "内容甲" in tool_node["content"]
        assert result["reply"]["content"] == "文件里是：内容甲"

    async def test_轮次上限显式收尾(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        # 脚本最后一项会无限重复 → 每轮都发起工具调用
        backend = FakeLLMBackend(
            {"tool_calls": [LLMToolCall(id="c", name="fs_list", arguments={"path": ""})]},
            supports_tools=True,
        )
        service = _make_service(store, backend, max_tool_rounds=3)
        session = await _create_session(service)
        result = await service.send_message(session["session_id"], "一直列目录")
        assert "上限" in result["reply"]["content"]
        # 3 轮工具往返 + 3 个 assistant + 1 条上限说明
        tool_nodes = [n for n in result["nodes"] if n["role"] == "tool"]
        assert len(tool_nodes) == 3

    async def test_支持工具的后端收到工具清单(
        self, store: Store, workspace_root: Path, credential: str
    ) -> None:
        backend = FakeLLMBackend("纯文本回复", supports_tools=True)
        service = _make_service(store, backend)
        session = await _create_session(service)
        result = await service.send_message(session["session_id"], "问")
        assert result["supports_tools"] is True
        tools_seen = backend.tools_seen[-1]
        assert tools_seen is not None
        names = {t.name for t in tools_seen}
        assert {"fs_list", "fs_read", "fs_write", "fs_mkdir", "fs_move", "fs_delete"} == names
