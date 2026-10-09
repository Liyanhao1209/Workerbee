"""chat 的工具循环驱动器（v0.03 §5.3，D-D）。

一轮的结构：发消息 + tools → 流式收集（正文/推理/tool_call）→ 落 assistant
节点 → 无 tool_call 即结束；有则逐个分发执行（写类工具在执行前等审批），
每个结果落一个 tool 节点（链式挂在路径末尾），回注后继续下一轮。
到轮次上限（默认 25，防爆走）落一条可见的说明节点收尾——上限命中是
**显式的**，不是静默停住。

节点链保持线性：assistant 节点 → 它的工具结果节点（依次串联）→ 下一轮
assistant 节点。这样「根→叶路径」永远包含完整的工具往返，fork（Phase 4）
也只发生在 user 节点上。

本模块只做编排：持久化、推送、审批都经注入的回调完成，不 import store、
notifier 或 security 层。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

from pydantic import Field

from ..core.domain.base import DomainModel, new_id
from ..data.llm import LLMChunk, LLMMessage, LLMToolCall, LLMToolSpec

__all__ = ["ToolLoopOutcome", "run_tool_loop", "DEFAULT_MAX_TOOL_ROUNDS"]

#: 单次发送里的工具调用轮次上限（防爆走）。
DEFAULT_MAX_TOOL_ROUNDS = 25

#: 流式增量回调：``(node_id, kind, text)``——kind 为 "text" / "reasoning"。
ChunkHook = Callable[[str, str, str], None]

#: 节点落库回调：kwargs 同 ``ChatRepository.append_node``，返回落库后的节点字典。
PersistFn = Callable[..., Awaitable[dict[str, Any]]]

#: 工具执行回调：``(调用, 发起它的 assistant 节点 id)`` → 结果。
ExecuteFn = Callable[[LLMToolCall, str], Awaitable[Any]]


class ToolLoopOutcome(DomainModel):
    """一轮发送的完整产物。"""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    """本轮新落的 assistant/tool 节点（按落库顺序，含上限说明节点）。"""

    reply: dict[str, Any] | None = None
    """本轮的最终回复节点（最后一个 assistant 节点）。"""

    final: LLMChunk | None = None
    """最后一次模型调用的终帧（usage/backend/降级标注的载体）。"""

    tool_rounds: int = 0
    """实际执行的工具轮次数。"""

    hit_round_limit: bool = False
    """True 表示因到达轮次上限而收尾（reply 是那条可见的说明节点）。"""


async def _stream_once(
    router: Any,
    messages: list[LLMMessage],
    *,
    tools: Sequence[LLMToolSpec] | None,
    timeout: float | None,
    reply_id: str,
    on_chunk: ChunkHook | None,
) -> tuple[str, str, list[LLMToolCall], LLMChunk | None]:
    """跑一轮流式补全。返回 (正文, 推理, 工具调用清单, 终帧)。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    final: LLMChunk | None = None
    async for chunk in router.stream(messages, timeout=timeout, tools=tools):
        if chunk.final:
            final = chunk
            continue
        if chunk.kind == "tool_call":
            if chunk.tool_call is not None:
                tool_calls.append(chunk.tool_call)
            continue
        if not chunk.text:
            continue
        if chunk.kind == "reasoning":
            reasoning_parts.append(chunk.text)
        else:
            text_parts.append(chunk.text)
        if on_chunk is not None:
            on_chunk(reply_id, chunk.kind, chunk.text)
    return "".join(text_parts), "".join(reasoning_parts), tool_calls, final


async def run_tool_loop(
    *,
    router: Any,
    messages: list[LLMMessage],
    tools: Sequence[LLMToolSpec] | None,
    timeout: float | None,
    max_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    persist: PersistFn,
    execute: ExecuteFn,
    on_chunk: ChunkHook | None = None,
) -> ToolLoopOutcome:
    """驱动工具循环，直到模型不再发起工具调用或到达轮次上限。

    ``messages`` 会被就地追加本轮产生的 assistant/tool 消息（调用方复用
    同一份列表做下一轮上下文）。
    """
    outcome = ToolLoopOutcome()
    while True:
        reply_id = new_id()
        text, reasoning, tool_calls, final = await _stream_once(
            router, messages,
            tools=tools, timeout=timeout, reply_id=reply_id, on_chunk=on_chunk,
        )
        if final is None:
            # 后端违反协议（流结束却没有终帧）：如实失败，不拿半截内容凑数。
            from ..data.llm import LLMResponseError

            raise LLMResponseError(
                "模型的流式响应没有正常结束（缺少收尾帧）", kind="bad_payload"
            )

        usage = final.usage
        assistant_node = await persist(
            node_id=reply_id,
            role="assistant",
            content=text,
            reasoning=reasoning or None,
            backend=final.backend,
            tokens_in=usage.input_tokens if usage else None,
            tokens_out=usage.output_tokens if usage else None,
            tool_calls=[tc.model_dump(mode="json") for tc in tool_calls] or None,
        )
        outcome.nodes.append(assistant_node)
        outcome.final = final
        outcome.reply = assistant_node
        messages.append(
            LLMMessage(
                role="assistant", content=text, tool_calls=tool_calls or None
            )
        )

        if not tool_calls:
            return outcome

        if outcome.tool_rounds >= max_rounds:
            note = await persist(
                node_id=new_id(),
                role="assistant",
                content=(
                    f"本轮对话已连续调用了 {max_rounds} 轮工具，达到单次发送的上限，"
                    "已停止继续操作。如果事情还没做完，请再发一条消息让我继续。"
                ),
            )
            outcome.nodes.append(note)
            outcome.reply = note
            outcome.hit_round_limit = True
            return outcome
        outcome.tool_rounds += 1

        for call in tool_calls:
            result = await execute(call, reply_id)
            text_result = str(getattr(result, "text", result))
            tool_node = await persist(
                node_id=new_id(),
                role="tool",
                content=text_result,
                tool_name=call.name,
                tool_call_id=call.id,
            )
            outcome.nodes.append(tool_node)
            messages.append(
                LLMMessage(
                    role="tool",
                    content=text_result,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
