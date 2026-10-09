"""Web Chat 域（v0.03 §5）：对话 + 文件系统工具，与只读助手分域并存（D-E）。

公开面：

- :class:`ChatService` —— 会话 CRUD、消息读取（线性分支路径）、发送
  （流式 + 工具循环 + 审批）；
- ``fs`` —— workspace 文件系统 confinement 与读写操作（REST 端点与
  LLM 工具共用同一实现）；
- ``tools`` —— LLM 工具清单（CHAT_TOOL_SPECS）与执行器；
- ``context`` —— 分支路径重建、滑窗截断、工具序列清理；
- ``tool_loop`` —— 工具循环驱动器。
"""

from . import context, fs, tool_loop, tools
from .service import (
    ChatCallFailed,
    ChatError,
    ChatLocked,
    ChatNotConfigured,
    ChatService,
)

__all__ = [
    "ChatService",
    "ChatError",
    "ChatNotConfigured",
    "ChatLocked",
    "ChatCallFailed",
    "context",
    "fs",
    "tool_loop",
    "tools",
]
