"""Session Fork 树操作的集成测试（v0.03 §6.2，D-F）。

不经过 HTTP，直接驱动 ``ChatService`` 的树操作 + 真实 Store。树结构经
``store.chat.append_node`` 直接铺设（树操作本身不依赖 LLM），另有一组用例
用假后端走完整发送链路验证分叉后两条分支各自的上下文。

覆盖 AC：分叉上下文正确（含中间 tool 节点）、删除不误删兄弟、按批次恢复、
移动成环拒绝、跨树合并、清空后不可恢复、树变更推送与 REST 重拉一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from workerbee.chat.service import ChatConflict, ChatError, ChatService
from workerbee.data.event_log import EventScope, EventType
from workerbee.data.store import DEFAULT_WORKSPACE_ID, Store

from tests.fakes import FakeLLMBackend
from tests.integration.test_chat_service import _Notifier, _make_service

pytestmark = pytest.mark.integration


@pytest.fixture
async def credential(store: Store) -> str:
    """启用助手配置 + 一条凭据（chat 的后端回落到它），供走发送链路的用例使用。"""
    from workerbee.assistant.service import AssistantConfig, save_config
    from workerbee.core.domain.registry import CredentialKind, CredentialRef

    cred = CredentialRef(
        credential_id="cred-1",
        label="测试 API",
        kind=CredentialKind.BASE_URL_PAIR,
        secret_locator="secret:cred-1",
        base_url="https://llm.example.com/v1",
        default_model="gpt-test",
    )
    await store.registry.upsert_credential(cred)
    await save_config(store.db, AssistantConfig(enabled=True, credential_ref="cred-1"))
    return "cred-1"


async def _make_session(store: Store) -> tuple[ChatService, str]:
    service = _make_service(store, FakeLLMBackend("回答"))
    session = await service.create_session(workspace_id=DEFAULT_WORKSPACE_ID)
    return service, session["session_id"]


async def _add(
    store: Store,
    session_id: str,
    node_id: str,
    parent_id: str | None,
    role: str = "user",
    content: str = "",
) -> dict[str, Any]:
    return await store.chat.append_node(
        node_id=node_id,
        session_id=session_id,
        parent_id=parent_id,
        role=role,
        content=content or node_id,
    )


async def _linear(store: Store, session_id: str, ids: list[str]) -> None:
    """铺一条链：ids[0] 为根，后续依次串联。"""
    parent: str | None = None
    for nid in ids:
        await _add(store, session_id, nid, parent)
        parent = nid


async def _live_ids(store: Store, session_id: str) -> set[str]:
    return {n["node_id"] for n in await store.chat.list_nodes(session_id)}


async def _deleted_ids(store: Store, session_id: str) -> set[str]:
    nodes = await store.chat.list_nodes(session_id, include_deleted=True)
    return {n["node_id"] for n in nodes if n["deleted_at"]}


# ===========================================================================
# 分叉（fork）
# ===========================================================================


class TestFork:
    async def test_分叉上下文含工具往返节点(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _add(store, sid, "u1", None, role="user", content="读一下配置")
        await _add(store, sid, "a1", "u1", role="assistant", content="")
        await store.chat.append_node(
            node_id="t1", session_id=sid, parent_id="a1",
            role="tool", content="配置内容", tool_name="fs_read", tool_call_id="c1",
        )
        await _add(store, sid, "a2", "t1", role="assistant", content="配置是这样的")

        result = await service.fork_node("a2")
        assert result["leaf_id"] == "a2"
        assert [n["node_id"] for n in result["messages"]] == ["u1", "a1", "t1", "a2"]

    async def test_分叉点不存在或已删除显式失败(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["n1", "n2"])
        with pytest.raises(KeyError):
            await service.fork_node("ghost")
        await service.delete_subtree("n2")
        with pytest.raises(ChatError, match="已被删除"):
            await service.fork_node("n2")

    async def test_分叉后两条分支各自上下文正确(
        self, store: Store, credential: Any
    ) -> None:
        """走完整发送链路：从中间节点分叉后发消息，两分支路径互不串扰。"""
        backend = FakeLLMBackend("回答")
        service = _make_service(store, backend)
        session = await service.create_session(workspace_id=DEFAULT_WORKSPACE_ID)
        sid = session["session_id"]

        r1 = await service.send_message(sid, "第一问")
        a1 = r1["reply"]["node_id"]
        await service.send_message(sid, "第二问")
        # 从第一轮的 assistant 节点分叉：新分支的上下文只有第一问。
        r3 = await service.send_message(sid, "改问别的", parent_id=a1)

        nodes = await store.chat.list_nodes(sid)
        from workerbee.chat.context import branch_path

        path_a = [n["content"] for n in branch_path(nodes, r3["reply"]["node_id"])]
        assert path_a == ["第一问", "回答", "改问别的", "回答"]
        leaf = await store.chat.latest_leaf(sid)
        assert leaf is not None and leaf["node_id"] == r3["reply"]["node_id"]
        # 原分支仍然完整可读。
        r2_leaf = r3["user_node"]["parent_id"]
        assert r2_leaf == a1
        msgs = await service.list_messages(sid)
        assert [m["content"] for m in msgs["messages"]] == path_a


# ===========================================================================
# 删除子树（级联软删，D-F）
# ===========================================================================


class TestDeleteSubtree:
    async def test_级联删除不误删兄弟(self, store: Store) -> None:
        service, sid = await _make_session(store)
        notifier = _Notifier()
        service.notifier = notifier
        # 根 r 下两分支：a 支与 b 支。
        await _linear(store, sid, ["r", "a1", "a2"])
        await _add(store, sid, "b1", "r")
        await _add(store, sid, "b2", "b1")

        result = await service.delete_subtree("a1")
        assert result["count"] == 2
        assert set(result["deleted"]) == {"a1", "a2"}
        assert result["deleted_at"]
        assert await _deleted_ids(store, sid) == {"a1", "a2"}
        assert await _live_ids(store, sid) == {"r", "b1", "b2"}
        # 删除批次留痕 + 推送（前端据此 REST 重拉对账）。
        events = await store.events.by_scope(EventScope.CHAT, sid)
        deleted_events = [e for e in events if e["type"] == EventType.CHAT_TREE_DELETED]
        assert len(deleted_events) == 1
        assert deleted_events[0]["payload"]["count"] == 2
        assert "chat_tree_changed" in notifier.kinds()

    async def test_删除后线性视图与latest_leaf自动避开(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1"])
        await _add(store, sid, "b1", "r")
        await service.delete_subtree("b1")
        leaf = await store.chat.latest_leaf(sid)
        assert leaf is not None and leaf["node_id"] == "a1"
        msgs = await service.list_messages(sid)
        assert [m["node_id"] for m in msgs["messages"]] == ["r", "a1"]

    async def test_重复删除显式失败(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["n1", "n2"])
        await service.delete_subtree("n1")
        with pytest.raises(ChatError, match="回收站"):
            await service.delete_subtree("n1")
        with pytest.raises(KeyError):
            await service.delete_subtree("ghost")


# ===========================================================================
# 恢复（按删除批次还原）
# ===========================================================================


class TestRestore:
    async def test_恢复整棵子树(self, store: Store) -> None:
        service, sid = await _make_session(store)
        notifier = _Notifier()
        service.notifier = notifier
        await _linear(store, sid, ["r", "a1", "a2"])
        await service.delete_subtree("a1")
        assert await _live_ids(store, sid) == {"r"}

        result = await service.restore_subtree("a1")
        assert result["restored"] == 2
        assert await _deleted_ids(store, sid) == set()
        assert await _live_ids(store, sid) == {"r", "a1", "a2"}
        events = await store.events.by_scope(EventScope.CHAT, sid)
        assert any(e["type"] == EventType.CHAT_TREE_RESTORED for e in events)
        assert notifier.kinds().count("chat_tree_changed") == 2

    async def test_恢复不复活更早单独删除的节点(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1", "a2"])
        # 先单独删 a2（批次一），再删 a1（批次二，只含 a1——a2 已有标记不重打）。
        first = await service.delete_subtree("a2")
        second = await service.delete_subtree("a1")
        assert first["deleted_at"] != second["deleted_at"]
        assert set(second["deleted"]) == {"a1"}

        result = await service.restore_subtree("a1")
        assert result["restored"] == 1
        assert await _live_ids(store, sid) == {"r", "a1"}
        assert await _deleted_ids(store, sid) == {"a2"}

    async def test_未删除的节点恢复显式失败(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["n1"])
        with pytest.raises(ChatError, match="无需恢复"):
            await service.restore_subtree("n1")
        with pytest.raises(KeyError):
            await service.restore_subtree("ghost")


# ===========================================================================
# 移动 / 合并子树（环检测）
# ===========================================================================


class TestMove:
    async def test_移动子树并留痕与推送(self, store: Store) -> None:
        service, sid = await _make_session(store)
        notifier = _Notifier()
        service.notifier = notifier
        await _linear(store, sid, ["r", "a1", "a2"])
        await _add(store, sid, "b1", "r")

        result = await service.move_node("a2", "b1")
        assert result["previous_parent_id"] == "a1"
        assert result["node"]["parent_id"] == "b1"
        node = await store.chat.get_node("a2")
        assert node is not None and node["parent_id"] == "b1"
        events = await store.events.by_scope(EventScope.CHAT, sid)
        moved = [e for e in events if e["type"] == EventType.CHAT_TREE_MOVED]
        assert len(moved) == 1
        assert moved[0]["payload"]["previous_parent_id"] == "a1"
        assert moved[0]["payload"]["new_parent_id"] == "b1"
        assert "chat_tree_changed" in notifier.kinds()

    async def test_撤销移动等于移回旧父节点(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1", "a2"])
        await _add(store, sid, "b1", "r")
        result = await service.move_node("a2", "b1")
        back = await service.move_node("a2", result["previous_parent_id"] or "")
        assert back["node"]["parent_id"] == "a1"

    async def test_跨树移动即合并(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["t1", "t1a"])
        await _linear(store, sid, ["t2", "t2a"])
        await service.move_node("t2", "t1a")
        nodes = await store.chat.list_nodes(sid)
        roots = [n["node_id"] for n in nodes if not n["parent_id"]]
        assert roots == ["t1"]

    async def test_移动到自己或后代下成环拒绝(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1", "a2", "a3"])
        with pytest.raises(ChatConflict, match="循环"):
            await service.move_node("a1", "a1")
        with pytest.raises(ChatConflict, match="循环"):
            await service.move_node("a1", "a3")
        # 拒绝后原结构不变。
        node = await store.chat.get_node("a1")
        assert node is not None and node["parent_id"] == "r"

    async def test_非法目标显式失败(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1"])
        await _add(store, sid, "gone", "r")
        await service.delete_subtree("gone")
        with pytest.raises(ChatError, match="有效节点"):
            await service.move_node("a1", "ghost")
        with pytest.raises(ChatError, match="已删除"):
            await service.move_node("a1", "gone")
        with pytest.raises(ChatError, match="不能移动"):
            await service.move_node("gone", "r")

    async def test_挂到森林根与同位移动幂等(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1"])
        moved = await service.move_node("a1", "")
        assert moved["node"]["parent_id"] is None
        same = await service.move_node("a1", "")
        assert same["node"]["parent_id"] is None


# ===========================================================================
# 清空（硬删，不可恢复）
# ===========================================================================


class TestPurge:
    async def test_清空硬删软删节点且不伤活节点(self, store: Store) -> None:
        service, sid = await _make_session(store)
        notifier = _Notifier()
        service.notifier = notifier
        await _linear(store, sid, ["r", "a1", "a2"])
        await _add(store, sid, "b1", "r")
        await service.delete_subtree("a1")

        result = await service.purge_deleted(sid)
        assert result["purged"] == 2
        all_nodes = await store.chat.list_nodes(sid, include_deleted=True)
        assert {n["node_id"] for n in all_nodes} == {"r", "b1"}
        events = await store.events.by_scope(EventScope.CHAT, sid)
        assert any(e["type"] == EventType.CHAT_TREE_PURGED for e in events)
        assert "chat_tree_changed" in notifier.kinds()

    async def test_清空后不可恢复(self, store: Store) -> None:
        service, sid = await _make_session(store)
        await _linear(store, sid, ["r", "a1"])
        await service.delete_subtree("a1")
        await service.purge_deleted(sid)
        with pytest.raises(KeyError):
            await service.restore_subtree("a1")
        # 无可清内容时幂等返回 0，不重复留痕。
        again = await service.purge_deleted(sid)
        assert again["purged"] == 0

    async def test_清空不影响别的会话(self, store: Store) -> None:
        service, sid = await _make_session(store)
        other = await service.create_session(workspace_id=DEFAULT_WORKSPACE_ID)
        oid = other["session_id"]
        await _linear(store, sid, ["s1", "s2"])
        await _linear(store, oid, ["o1", "o2"])
        await service.delete_subtree("s2")
        await service.delete_subtree("o2")
        await service.purge_deleted(sid)
        assert await _deleted_ids(store, oid) == {"o2"}
