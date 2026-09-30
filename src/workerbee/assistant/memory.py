"""对话记忆的滑动窗口与手动整理（AI-01，记忆策略见计划 §5）。

两条纪律：

- **截断不产生任何额外调用**：超出窗口的旧消息直接丢弃，``dropped`` 如实返回
  截断条数，对小窗可见（「更早的 N 条消息未随本次发送」）。不做自动摘要——
  自动摘要会静默产生费用。
- **memory 消息钉扎**：「整理前文」产出的摘要消息（role=memory）不参与窗口计数，
  组装上下文时永远带上**最新一条**；更旧的 memory 消息已被新摘要吸收，
  留在库里只是历史，不再注入。
"""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import Field

from ..core.domain.base import DomainModel

__all__ = [
    "Window",
    "sliding_window",
    "latest_memory",
    "DEFAULT_WINDOW_ROUNDS",
    "DEFAULT_WINDOW_CHARS",
]

#: 默认窗口：最近 12 轮（一轮 = 一条用户消息及其后的助手回复）。
DEFAULT_WINDOW_ROUNDS = 12

#: 默认窗口字符上限（对话部分，不含钉扎的 memory 消息）。未经实测标定。
DEFAULT_WINDOW_CHARS = 12000


class Window(DomainModel):
    """一次窗口截断的结果。"""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    """随本次发送的对话消息（不含 memory 消息），时间正序。"""

    dropped: int = 0
    """因窗口限制未随本次发送的对话消息条数。0 表示完整发送。"""


def latest_memory(messages: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """取最新一条 memory 消息。更旧的 memory 已被它吸收，不再注入。"""
    for message in reversed(messages):
        if message.get("role") == "memory":
            return message
    return None


def sliding_window(
    messages: Sequence[dict[str, Any]],
    *,
    max_rounds: int = DEFAULT_WINDOW_ROUNDS,
    max_chars: int = DEFAULT_WINDOW_CHARS,
) -> Window:
    """对对话历史做窗口截断。

    先按轮数截（保留最近 ``max_rounds`` 条用户消息及其后的一切），再按字符上限
    从最旧的消息起逐条丢弃。memory 消息不进入返回结果（由 :func:`latest_memory`
    单独钉扎）。
    """
    conversation = [m for m in messages if m.get("role") != "memory"]

    # 按轮数截：保留最近 max_rounds 条用户消息及其后的一切。一轮从用户消息开始，
    # 所以起点要落在一条用户消息上（不能从上一条助手回复中间切开）。
    if max_rounds > 0:
        seen = 0
        start = 0
        for index in range(len(conversation) - 1, -1, -1):
            if conversation[index].get("role") == "user":
                seen += 1
                if seen > max_rounds:
                    start = index + 1
                    while start < len(conversation) and conversation[start].get("role") != "user":
                        start += 1
                    break
        kept = conversation[start:]
    else:
        kept = list(conversation)

    # 按字符截：超出上限时丢弃最旧。
    if max_chars > 0:
        total = sum(len(str(m.get("content") or "")) for m in kept)
        while kept and total > max_chars:
            total -= len(str(kept[0].get("content") or ""))
            kept.pop(0)

    return Window(messages=kept, dropped=len(conversation) - len(kept))
