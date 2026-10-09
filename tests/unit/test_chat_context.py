"""chat 上下文重建的单元测试（v0.03 §5.2、§6.2）。

三件事：分支路径（根→叶）、滑窗截断、工具序列清理。清理规则是
「两家 LLM API 都不接受残缺工具往返」的直接转译，每条都有用例钉住。
"""

from __future__ import annotations

from typing import Any

import pytest

from workerbee.chat.context import (
    branch_path,
    build_context,
    sanitize_tool_sequence,
    to_llm_messages,
)

pytestmark = pytest.mark.unit


def _node(
    node_id: str,
    role: str,
    content: str = "",
    parent_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    node = {
        "node_id": node_id,
        "session_id": "s1",
        "parent_id": parent_id,
        "role": role,
        "content": content,
        "tool_calls": None,
        "tool_name": None,
        "tool_call_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    node.update(extra)
    return node


# ===========================================================================
# branch_path：根 → 叶的线性路径
# ===========================================================================


class TestBranchPath:
    def test_线性链(self) -> None:
        nodes = [
            _node("n1", "user", "问"),
            _node("n2", "assistant", "答", parent_id="n1"),
            _node("n3", "user", "再问", parent_id="n2"),
        ]
        path = branch_path(nodes, "n3")
        assert [n["node_id"] for n in path] == ["n1", "n2", "n3"]

    def test_分叉只走所选分支(self) -> None:
        nodes = [
            _node("n1", "user", "问"),
            _node("n2", "assistant", "答A", parent_id="n1"),
            _node("n3", "assistant", "答B", parent_id="n1"),  # 兄弟分支
        ]
        path = branch_path(nodes, "n2")
        assert [n["node_id"] for n in path] == ["n1", "n2"]

    def test_叶不存在抛KeyError(self) -> None:
        with pytest.raises(KeyError):
            branch_path([_node("n1", "user")], "ghost")

    def test_环报ValueError(self) -> None:
        nodes = [
            _node("n1", "user", parent_id="n2"),
            _node("n2", "assistant", parent_id="n1"),
        ]
        with pytest.raises(ValueError):
            branch_path(nodes, "n1")


# ===========================================================================
# sanitize_tool_sequence：残缺工具往返的清理
# ===========================================================================


class TestSanitize:
    def test_完整往返原样保留(self) -> None:
        nodes = [
            _node("n1", "user", "列目录"),
            _node(
                "n2", "assistant", "", parent_id="n1",
                tool_calls=[{"id": "c1", "name": "fs_list", "arguments": {"path": ""}}],
            ),
            _node("n3", "tool", "（空目录）", parent_id="n2",
                  tool_name="fs_list", tool_call_id="c1"),
            _node("n4", "assistant", "目录是空的", parent_id="n3"),
        ]
        out = sanitize_tool_sequence(nodes)
        assert [n["node_id"] for n in out] == ["n1", "n2", "n3", "n4"]
        assert out[1]["tool_calls"]  # 结果齐全的调用保留

    def test_结果不全的调用被剥掉(self) -> None:
        nodes = [
            _node("n1", "user", "写文件"),
            _node(
                "n2", "assistant", "好的", parent_id="n1",
                tool_calls=[{"id": "c1", "name": "fs_write", "arguments": {}}],
            ),
            # 没有对应的 tool 结果节点（被窗口切掉了）
        ]
        out = sanitize_tool_sequence(nodes)
        assert out[1]["tool_calls"] is None
        assert out[1]["content"] == "好的"  # 正文保留

    def test_孤儿tool节点被丢弃(self) -> None:
        nodes = [
            _node("n1", "user", "问"),
            _node("n9", "tool", "结果", parent_id="n1",
                  tool_name="fs_read", tool_call_id="cX"),  # 没有 assistant 声明过 cX
            _node("n2", "assistant", "答", parent_id="n9"),
        ]
        out = sanitize_tool_sequence(nodes)
        assert [n["node_id"] for n in out] == ["n1", "n2"]

    def test_开头非user的残段被丢弃(self) -> None:
        nodes = [
            _node("n0", "assistant", "半截回复"),
            _node("n1", "user", "问"),
            _node("n2", "assistant", "答", parent_id="n1"),
        ]
        out = sanitize_tool_sequence(nodes)
        assert [n["node_id"] for n in out] == ["n1", "n2"]

    def test_部分结果不全时整个调用清单剥掉(self) -> None:
        nodes = [
            _node("n1", "user", "做两件事"),
            _node(
                "n2", "assistant", "", parent_id="n1",
                tool_calls=[
                    {"id": "c1", "name": "fs_list", "arguments": {}},
                    {"id": "c2", "name": "fs_read", "arguments": {}},
                ],
            ),
            _node("n3", "tool", "目录", parent_id="n2",
                  tool_name="fs_list", tool_call_id="c1"),
            # c2 的结果缺失
        ]
        out = sanitize_tool_sequence(nodes)
        # c2 没结果 → 整个 tool_calls 剥掉；c1 的结果随之成为孤儿被丢弃
        assert out[1]["tool_calls"] is None
        assert [n["node_id"] for n in out] == ["n1", "n2"]


# ===========================================================================
# to_llm_messages / build_context
# ===========================================================================


class TestToLlmMessages:
    def test_角色与工具字段映射(self) -> None:
        nodes = [
            _node("n1", "user", "读文件"),
            _node(
                "n2", "assistant", "", parent_id="n1",
                tool_calls=[{"id": "c1", "name": "fs_read", "arguments": {"path": "a"}}],
            ),
            _node("n3", "tool", "内容", parent_id="n2",
                  tool_name="fs_read", tool_call_id="c1"),
        ]
        msgs = to_llm_messages(nodes)
        assert [m.role for m in msgs] == ["user", "assistant", "tool"]
        assert msgs[1].tool_calls is not None
        assert msgs[1].tool_calls[0].name == "fs_read"
        assert msgs[1].tool_calls[0].arguments == {"path": "a"}
        assert msgs[2].tool_call_id == "c1"
        assert msgs[2].name == "fs_read"

    def test_system节点跳过(self) -> None:
        nodes = [
            _node("n0", "system", "旧提示词"),
            _node("n1", "user", "问", parent_id="n0"),
        ]
        msgs = to_llm_messages(nodes)
        assert [m.role for m in msgs] == ["user"]


class TestBuildContext:
    def test_窗口截断如实计数(self) -> None:
        nodes: list[dict[str, Any]] = []
        prev: str | None = None
        for i in range(5):
            u = _node(f"u{i}", "user", f"问题{i}" * 10, parent_id=prev)
            a = _node(f"a{i}", "assistant", f"回答{i}" * 10, parent_id=u["node_id"])
            nodes.extend([u, a])
            prev = a["node_id"]
        msgs, dropped = build_context(nodes, max_rounds=2, max_chars=0)
        assert dropped == 6  # 只保留最近 2 轮（4 条）
        assert len(msgs) == 4
        assert msgs[0].role == "user"

    def test_截断切开工具往返时序列仍合法(self) -> None:
        """字符截断从一轮工具往返中间切开：残留的 assistant/tool 段被清理。"""
        nodes = [
            _node("u0", "user", "早" * 100),
            _node(
                "a0", "assistant", "", parent_id="u0",
                tool_calls=[{"id": "c1", "name": "fs_read", "arguments": {}}],
            ),
            _node("t0", "tool", "结果" * 50, parent_id="a0",
                  tool_name="fs_read", tool_call_id="c1"),
            _node("a1", "assistant", "读完早文件了", parent_id="t0"),
            _node("u1", "user", "现在做什么", parent_id="a1"),
        ]
        # 窗口只放得下一部分：从中间切开
        msgs, dropped = build_context(nodes, max_rounds=0, max_chars=30)
        assert dropped > 0
        # 切完之后的序列必须协议合法：首条是 user，且没有孤儿 tool
        if msgs:
            assert msgs[0].role == "user"
        roles = [m.role for m in msgs]
        for i, role in enumerate(roles):
            if role == "tool":
                # 前面必有声明了该调用的 assistant
                assert any(
                    m.role == "assistant" and m.tool_calls for m in msgs[:i]
                )
