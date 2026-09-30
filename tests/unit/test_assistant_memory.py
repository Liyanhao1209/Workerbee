"""对话记忆的滑动窗口（AI-01，计划 §5）。

窗口纪律是本文件的断言重点：

- 轮数上限与字符上限各自独立生效，超出丢弃**最旧**的消息；
- 截断条数如实返回（``dropped``），不允许「悄悄少发了」；
- memory 消息（整理前文的摘要）不参与窗口，由 ``latest_memory`` 钉扎最新一条。
"""

from __future__ import annotations

import pytest

from workerbee.assistant.memory import latest_memory, sliding_window

pytestmark = pytest.mark.unit


def msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def dialogue(rounds: int) -> list[dict]:
    out: list[dict] = []
    for i in range(rounds):
        out.append(msg("user", f"问题{i}"))
        out.append(msg("assistant", f"回答{i}"))
    return out


async def test_window_keeps_everything_under_limits() -> None:
    window = sliding_window(dialogue(3), max_rounds=12, max_chars=12000)
    assert window.dropped == 0
    assert len(window.messages) == 6


async def test_window_drops_oldest_rounds() -> None:
    window = sliding_window(dialogue(5), max_rounds=2, max_chars=12000)
    # 保留最近 2 轮 = 2 条 user + 2 条 assistant
    assert [m["content"] for m in window.messages] == ["问题3", "回答3", "问题4", "回答4"]
    assert window.dropped == 6


async def test_window_drops_oldest_by_chars() -> None:
    history = [msg("user", "x" * 100), msg("assistant", "y" * 100), msg("user", "短")]
    # 上限 120：丢一条（100+1 ≤ 120）就够，只丢最旧的一条
    window = sliding_window(history, max_rounds=12, max_chars=120)
    assert window.messages == [msg("assistant", "y" * 100), msg("user", "短")]
    assert window.dropped == 1
    # 上限 60：继续丢到只剩最新一条
    window = sliding_window(history, max_rounds=12, max_chars=60)
    assert window.messages == [msg("user", "短")]
    assert window.dropped == 2


async def test_window_drops_whole_messages_not_partial() -> None:
    """字符上限按整条消息丢弃，不把一条消息拦腰截断。"""
    history = [msg("user", "a" * 50), msg("assistant", "b" * 50)]
    window = sliding_window(history, max_rounds=12, max_chars=60)
    assert window.messages == [msg("assistant", "b" * 50)]
    assert window.dropped == 1


async def test_memory_messages_do_not_enter_window() -> None:
    history = [msg("user", "问题0"), msg("memory", "旧摘要"), msg("assistant", "回答0")]
    window = sliding_window(history, max_rounds=12, max_chars=12000)
    assert all(m["role"] != "memory" for m in window.messages)
    # memory 不算被「截断」——它根本不在窗口语义里
    assert window.dropped == 0


async def test_latest_memory_pins_the_newest() -> None:
    history = [
        msg("memory", "更早的摘要"),
        msg("user", "问题"),
        msg("memory", "最新的摘要"),
    ]
    assert latest_memory(history)["content"] == "最新的摘要"
    assert latest_memory(dialogue(2)) is None
