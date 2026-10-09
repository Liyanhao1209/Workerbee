"""chat 的上下文重建（v0.03 §5.2、§6.2）。

一条消息的上下文 = 它所在分支的「根 → 叶」路径。重建三步：

1. **沿 parent_id 上溯**：O(深度)，无需物化路径；带深度上限防环
   （数据损坏时显式失败，不无限转圈）。
2. **滑窗截断**：复用 ``assistant/memory.py`` 的 ``sliding_window``——
   截断不产生额外调用，``dropped`` 如实返回。
3. **工具序列清理**（截断之后必须做）：截断可能从一轮工具往返的中间切开，
   留下「assistant 声明了调用但没有结果」或「结果找不到调用」的残缺序列，
   两家 API 都会因此拒绝请求。清理规则：结果不全的 assistant 节点剥掉
   tool_calls（保留正文）；孤儿 tool 节点丢弃；开头的非 user 消息丢弃。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..assistant.memory import DEFAULT_WINDOW_CHARS, DEFAULT_WINDOW_ROUNDS, sliding_window
from ..data.llm import LLMMessage, LLMToolCall

__all__ = [
    "branch_path",
    "sanitize_tool_sequence",
    "to_llm_messages",
    "build_context",
    "MAX_PATH_DEPTH",
]

#: 上溯深度上限：超过即视为数据损坏（环），显式报错而不是无限循环。
MAX_PATH_DEPTH = 4096


def branch_path(nodes: Sequence[dict[str, Any]], leaf_id: str) -> list[dict[str, Any]]:
    """从会话的全部节点里取「根 → leaf」的分支路径（插入顺序的列表切片）。

    ``nodes`` 须为同一会话的节点（``ChatRepository.list_nodes`` 的返回）。
    叶节点不存在抛 KeyError；父链断裂或有环抛 ValueError。
    """
    by_id = {n["node_id"]: n for n in nodes}
    leaf = by_id.get(leaf_id)
    if leaf is None:
        raise KeyError(leaf_id)
    chain: list[dict[str, Any]] = []
    current: dict[str, Any] | None = leaf
    while current is not None:
        chain.append(current)
        parent_id = current.get("parent_id")
        current = by_id.get(parent_id) if parent_id else None
        if len(chain) > MAX_PATH_DEPTH:
            raise ValueError(f"chat 节点树存在环或断裂（session={leaf.get('session_id')}）")
    chain.reverse()
    return chain


def sanitize_tool_sequence(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理截断后的工具往返序列（规则见模块文档）。不改动入参节点。"""
    answered = {
        n.get("tool_call_id") for n in nodes if n.get("role") == "tool" and n.get("tool_call_id")
    }
    out: list[dict[str, Any]] = []
    for node in nodes:
        role = node.get("role")
        if role == "assistant":
            calls = list(node.get("tool_calls") or [])
            if calls and not all(c.get("id") in answered for c in calls):
                # 结果不全（通常是被窗口切掉）：剥掉调用清单，只留正文——
                # 协议不接受「有调用没结果」的 assistant 消息。
                out.append({**node, "tool_calls": None})
            else:
                out.append(node)
        elif role == "tool":
            # 只保留「路径上最近的 assistant 节点声明过这次调用」的结果。
            declared = next(
                (m for m in reversed(out) if m.get("role") == "assistant"), None
            )
            declared_ids = {
                c.get("id") for c in (declared or {}).get("tool_calls") or []
            }
            if node.get("tool_call_id") in declared_ids:
                out.append(node)
            # 孤儿结果丢弃：模型不知道这次调用存在过，结果只会让它困惑。
        else:
            out.append(node)
    # 协议要求首条非 system 消息是 user：截断从中间切开时丢弃开头的残段。
    while out and out[0].get("role") != "user":
        out.pop(0)
    return out


def to_llm_messages(nodes: Sequence[dict[str, Any]]) -> list[LLMMessage]:
    """节点路径 → 协议层消息。system 角色（预留）跳过：system prompt 每次新鲜组装。"""
    messages: list[LLMMessage] = []
    for node in nodes:
        role = node.get("role")
        if role == "user":
            messages.append(LLMMessage(role="user", content=str(node.get("content") or "")))
        elif role == "assistant":
            raw_calls = node.get("tool_calls") or []
            calls = [
                LLMToolCall(
                    id=str(c.get("id") or ""),
                    name=str(c.get("name") or ""),
                    arguments=c.get("arguments") if isinstance(c.get("arguments"), dict) else {},
                )
                for c in raw_calls
            ]
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=str(node.get("content") or ""),
                    tool_calls=calls or None,
                )
            )
        elif role == "tool":
            messages.append(
                LLMMessage(
                    role="tool",
                    content=str(node.get("content") or ""),
                    tool_call_id=node.get("tool_call_id"),
                    name=node.get("tool_name"),
                )
            )
    return messages


def build_context(
    path: Sequence[dict[str, Any]],
    *,
    max_rounds: int = DEFAULT_WINDOW_ROUNDS,
    max_chars: int = DEFAULT_WINDOW_CHARS,
) -> tuple[list[LLMMessage], int]:
    """完整管线：滑窗截断 → 工具序列清理 → 协议层消息。返回 ``(messages, dropped)``。"""
    window = sliding_window(list(path), max_rounds=max_rounds, max_chars=max_chars)
    clean = sanitize_tool_sequence(window.messages)
    return to_llm_messages(clean), window.dropped
