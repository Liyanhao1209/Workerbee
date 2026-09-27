"""适配器协议（架构设计 v0.02 §3、§8.1、D-09）。

传输：**子进程插件 + 换行分隔的 JSON-RPC 2.0 over stdio**。
选择子进程而非同进程插件，是为了崩溃隔离——第三方适配器崩溃不拖垮内核，
权限事件与心跳走同一通道。

两个方向的消息：
- core → adapter：``request``（要回包）与 ``notification``（不要回包）
- adapter → core：``response``，以及**异步通知**——事件流、权限请求、心跳。
  权限请求必须是通知而不是请求，因为它是 harness 主动发起的，core 不能用
  同步往返去「问」一个还没发生的事。

协议版本化：握手时双方交换 ``protocol_version``，不兼容时给出明确错误而非挂起
（D-09「不能声称支持」比「声称但卡死」重要）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PROTOCOL_VERSION",
    "METHODS",
    "NOTIFICATIONS",
    "ErrorCode",
    "AdapterError",
    "JsonRpcError",
]

#: 适配器协议版本。主版本不同即不兼容；次版本向后兼容。
PROTOCOL_VERSION = "1.0"

#: 主版本号——握手时只比它。
PROTOCOL_MAJOR = PROTOCOL_VERSION.split(".")[0]


class METHODS:
    """core → adapter 的请求方法名。六组契约一一对应。"""

    # --- health / version（§8.1 第 6 组）---
    HANDSHAKE = "handshake"
    HEARTBEAT = "health.heartbeat"
    COMPAT_CHECK = "health.compat_check"

    # --- capabilities（第 1 组）---
    DECLARE = "capabilities.declare"
    PROBE = "capabilities.probe"

    # --- session lifecycle（第 2 组）---
    SESSION_CREATE = "session.create"
    SESSION_RESUME = "session.resume"
    SESSION_DISPOSE = "session.dispose"
    SESSION_LIST = "session.list"

    # --- io（第 3 组）---
    SEND_INPUT = "io.send_input"
    READ_OUTPUT = "io.read_output"

    # --- permission hook（第 4 组）---
    PERMISSION_RESPOND = "permission.respond"

    # --- event stream（第 5 组）---
    SUBSCRIBE = "stream.subscribe"
    UNSUBSCRIBE = "stream.unsubscribe"

    # --- 控制（取消链需要，§10.4）---
    INTERRUPT = "control.interrupt"
    TERMINATE = "control.terminate"
    PAUSE = "control.pause"
    CHECKPOINT = "control.checkpoint"
    ABORT_STREAM = "control.abort_stream"
    COMPACT = "control.compact"

    # --- 供上下文组装使用的会话内操作 ---
    SESSION_STAT = "session.stat"


class NOTIFICATIONS:
    """adapter → core 的异步通知。"""

    EVENT = "event"
    """统一事件流。``kind`` 决定语义，见 EventKind。"""

    PERMISSION_REQUEST = "permission_request"
    """harness 请求授权。必须转译为 Approval 实体（HUM-03）。"""

    HEARTBEAT = "heartbeat"
    """会话心跳，用于租约续期与存活判定。"""

    LOG = "log"
    """适配器自身的日志。不得携带凭据。"""


class EventKind(StrEnum):
    """事件流语义（§8.1 第 5 组：状态变化、后台工作、compact、错误事件）。"""

    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    SESSION_RESUMED = "session_resumed"

    OUTPUT = "output"
    """一段增量输出。"""

    TURN_END = "turn_end"
    """agent 完成一轮响应。**不等于阶段成功**（RUN-06）。"""

    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"

    BACKGROUND_TASK_STARTED = "background_task_started"
    BACKGROUND_TASK_ENDED = "background_task_ended"
    """决定结果的后台工作未结束时不得判定阶段成功（RUN-06、AC-22）。"""

    COMPACT = "compact"
    """上下文整理发生。记录触发阈值与前后用量（CFG-04）。"""

    USAGE = "usage"
    """用量上报。不可取得时**不上报**，而不是上报 0（OBS-04）。"""

    STATE_CHANGE = "state_change"
    ERROR = "error"


class ErrorCode:
    """JSON-RPC 错误码。业务错误用 -32000 起的区间，避免与协议错误混淆。"""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # 业务错误
    NOT_SUPPORTED = -32001
    """该 harness 或该适配器不支持此操作。**必须如实返回**，不得静默降级。"""

    SESSION_NOT_FOUND = -32002
    SESSION_DEAD = -32003
    HARNESS_UNAVAILABLE = -32004
    AUTH_FAILED = -32005
    PROTOCOL_MISMATCH = -32006
    ADAPTER_BUSY = -32007
    RATE_LIMITED = -32008
    CONTEXT_LIMIT = -32009
    CHECKPOINT_UNAVAILABLE = -32010


#: 业务错误码 → 领域错误分类（D-05 的重试决策依据）。
ERROR_CLASS_MAP: dict[int, str] = {
    ErrorCode.NOT_SUPPORTED: "fatal_error",
    ErrorCode.SESSION_NOT_FOUND: "fatal_error",
    ErrorCode.SESSION_DEAD: "retryable_error",
    ErrorCode.HARNESS_UNAVAILABLE: "retryable_error",
    ErrorCode.AUTH_FAILED: "fatal_error",
    ErrorCode.PROTOCOL_MISMATCH: "fatal_error",
    ErrorCode.ADAPTER_BUSY: "retryable_error",
    ErrorCode.RATE_LIMITED: "retryable_error",
    ErrorCode.CONTEXT_LIMIT: "fatal_error",
    ErrorCode.CHECKPOINT_UNAVAILABLE: "fatal_error",
    ErrorCode.INTERNAL_ERROR: "retryable_error",
}

#: 错误码 → RetryPolicy.retryable_errors 中使用的类别名。
ERROR_KIND_MAP: dict[int, str] = {
    ErrorCode.SESSION_DEAD: "session_dead",
    ErrorCode.HARNESS_UNAVAILABLE: "network",
    ErrorCode.ADAPTER_BUSY: "adapter_busy",
    ErrorCode.RATE_LIMITED: "rate_limit",
    ErrorCode.INTERNAL_ERROR: "server_error",
}


class AdapterError(Exception):
    """适配器侧的业务错误。"""

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def error_class(self) -> str:
        return ERROR_CLASS_MAP.get(self.code, "fatal_error")

    def error_kind(self) -> str | None:
        return ERROR_KIND_MAP.get(self.code)

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


class JsonRpcError(Exception):
    """传输层／核心侧的 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def error_class(self) -> str:
        return ERROR_CLASS_MAP.get(self.code, "fatal_error")

    def error_kind(self) -> str | None:
        return ERROR_KIND_MAP.get(self.code)
