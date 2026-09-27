"""supervisor 与 core 之间的协议（架构设计 v0.02 §3）。

与适配器协议同构：换行分隔的 JSON-RPC 2.0。复用同一套做法是有意的——
两处都是「一个长驻进程通过管道/套接字被另一个进程驱动」，
用同一套心智模型可以减少一类「这里怎么发通知来着」的犹豫。

**与适配器协议的关键差别**：core 会重启，适配器不会。因此 supervisor 必须
- 按会话缓冲事件并编号，使 core 重连后能补齐断连期间的事件；
- 自己持久化会话台账，使 core 重启后能据此重接管。

没有这两条，「重启 core 不影响在跑的 session」就只是句口号。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SUPERVISOR_PROTOCOL_VERSION", "METHODS", "NOTIFICATIONS", "ErrorCode", "SupervisorError"]

SUPERVISOR_PROTOCOL_VERSION = "1.0"


class METHODS:
    # --- 连接 ---
    HELLO = "hello"
    PING = "ping"

    # --- harness 管理 ---
    HARNESS_ENSURE = "harness.ensure"
    HARNESS_CAPABILITIES = "harness.capabilities"
    HARNESS_LIST = "harness.list"
    HARNESS_STOP = "harness.stop"

    # --- 会话（与 HarnessPort 一一对应）---
    SESSION_CREATE = "session.create"
    SESSION_RESUME = "session.resume"
    SESSION_SEND = "session.send_input"
    SESSION_INTERRUPT = "session.interrupt"
    SESSION_TERMINATE = "session.terminate"
    SESSION_ABORT_STREAM = "session.abort_stream"
    SESSION_PAUSE = "session.pause"
    SESSION_CHECKPOINT = "session.checkpoint"
    SESSION_COMPACT = "session.compact"
    SESSION_ALIVE = "session.alive"
    SESSION_DISPOSE = "session.dispose"
    SESSION_LIST = "session.list"
    SESSION_EVENTS = "session.events"

    # --- 订阅 ---
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"

    # --- 系统 ---
    SHUTDOWN = "system.shutdown"
    STATUS = "system.status"


class NOTIFICATIONS:
    EVENT = "event"
    """会话事件。带 ``seq`` 与 ``session_ref``，core 可据此去重与补齐。"""

    SESSION_DIED = "session_died"
    """会话非正常终止（harness 崩溃）。core 据此走 REC-02 的恢复路径。"""

    HARNESS_DIED = "harness_died"
    """适配器子进程退出。"""

    HEARTBEAT = "heartbeat"


class ErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    SESSION_NOT_FOUND = -32001
    HARNESS_NOT_FOUND = -32002
    HARNESS_UNAVAILABLE = -32003
    NOT_SUPPORTED = -32004
    EVENTS_GONE = -32005
    """请求的事件序号已被环形缓冲丢弃。core 必须据此走对账而不是假装补上了。"""


class SupervisorError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}
