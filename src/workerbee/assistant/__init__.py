"""基础助手子包（AI-01，功能清单 §3.12）。

系统内嵌的 AI 客服式问答：回答「怎么用 / 现在什么状态 / 这个报错什么意思」。
第一版的明确边界（计划 §2）：

- **只读**：不建/改 workflow、不改配置、不碰凭据、不提交任务；
- **非流式**：LLMBackend 没有 stream 接口，一问一答同步返回；
- **无 function calling**：系统感知靠每次请求注入的快照，不给助手装工具。

模块分工：

- :mod:`.guidebook`：静态使用指引的加载与预算截断；
- :mod:`.snapshot`：动态系统快照组装（数据源协议由 server 层注入实现）；
- :mod:`.memory`：滑动窗口与手动「整理前文」；
- :mod:`.service`：AssistantService 核心编排（取历史 → 截断 → 拼 prompt →
  调模型 → 脱敏落库 → 留痕 → 推送）。
"""

from .service import (
    AssistantCallFailed,
    AssistantConfig,
    AssistantError,
    AssistantLocked,
    AssistantNotConfigured,
    AssistantService,
)

__all__ = [
    "AssistantService",
    "AssistantConfig",
    "AssistantError",
    "AssistantNotConfigured",
    "AssistantLocked",
    "AssistantCallFailed",
]
