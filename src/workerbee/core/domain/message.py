"""统一消息信封（架构设计 v0.02 §5.4 Message）。

投递语义：**至少一次 + 消费端按 dedup_key 幂等**（REC-05）。
因此消费方的正确性不能依赖「只收到一次」，只能依赖 dedup_key。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import Entity, new_id

__all__ = ["MessageType", "Message"]


class MessageType(StrEnum):
    DATA_READY = "data_ready"
    """上游阶段完成，负载只携带产物引用与元数据；正文不随通知传输（§7.1）。"""

    FEEDBACK = "feedback"
    """下游发现输入不足或需返工，结构化携带问题描述与对应上游产物引用。
    第一版只呈现给用户，不自动唤醒已完成上游（DATA-06、D-06）。"""

    APPROVAL_REQ = "approval_req"
    APPROVAL_RESP = "approval_resp"

    STATE_NOTIFY = "state_notify"
    """状态变化通知，驱动 WebSocket 推送（OBS-05）。"""

    CANCEL = "cancel"

    BTW_INPUT = "btw_input"
    """用户在运行中补充的指示（HUM-01）。必须路由到**当时那个**会话，
    不能因节点已开始下一任务而被错误投递（AC-14）。"""


class Message(Entity):
    """一条消息。``payload`` 与 ``payload_ref`` 二选一：

    - 小负载内联进 ``payload``；
    - 大负载只携带 ``payload_ref``（产物引用），下游按需拉取全文。

    两条路径共用同一份校验，避免「内联的能过、引用的被漏掉」（§7.1）。
    """

    message_id: str = Field(default_factory=new_id)
    type: MessageType

    task_id: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None

    dedup_key: str | None = None
    """消费端幂等的唯一依据。同一完成通知重复到达时，靠它避免重复启动下游。"""

    causation_id: str | None = None
    """触发本消息的上一条消息／事件 id，用于追踪因果链。"""

    generation: int = 1
    """代次号；清理后递增，迟到消息按代次丢弃（REC-05）。"""

    payload_ref: str | None = None
    payload: dict | None = None

    def dedup(self) -> str:
        if self.dedup_key:
            return self.dedup_key
        return f"{self.type.value}:{self.message_id}"
