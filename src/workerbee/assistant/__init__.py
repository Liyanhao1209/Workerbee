"""基础助手子包（AI-01，功能清单 §3.12）。

系统内嵌的 AI 客服式问答：回答「怎么用 / 现在什么状态 / 这个报错什么意思」，
并能为用户**提议**流程或节点模板的草稿（``workerbee-draft`` 提案块）。
第一版的明确边界（计划 §2）：

- **不直接写库**：助手只产出提案卡片；真正的创建发生在用户点「采用」之后，
  走与手动建图完全相同的服务层入口（发布仍要用户去编辑器里做）；
- **流式输出**：发问后逐 chunk 经推送通道下发（正文与推理过程分开），
  完整回复仍同步返回并落库，推送只是加速器；
- **无 function calling**：系统感知靠每次请求注入的快照，不给助手装工具。

模块分工：

- :mod:`.guidebook`：静态使用指引的加载与预算截断；
- :mod:`.snapshot`：动态系统快照组装（数据源协议由 server 层注入实现）；
- :mod:`.memory`：滑动窗口与手动「整理前文」；
- :mod:`.draft`：草稿提案的协议（``workerbee-draft`` 块）、解析与待配置汇总；
- :mod:`.service`：AssistantService 核心编排（取历史 → 截断 → 拼 prompt →
  调模型 → 脱敏落库 → 提案解析校验 → 留痕 → 推送）。
"""

from .draft import DraftInvalid, DraftProposal, extract_proposal
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
    "DraftInvalid",
    "DraftProposal",
    "extract_proposal",
]
