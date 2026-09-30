"""使用指引（guidebook）的加载与预算截断（AI-01 静态知识层）。

guidebook.md 是助手回答「怎么用」的唯一依据，随代码版本维护。两条纪律：

- **文件缺失不报错**：记 ``missing=True``，由上层如实标注降级（小窗会显示
  「指引文档还没写好」），而不是让助手整个不可用——问答快照那一半能力仍在。
- **有字符预算**：超出即截断并记 ``truncated=True``，截断事实进事件日志，
  不允许「悄悄少喂了半份文档」。
"""

from __future__ import annotations

from pathlib import Path

from ..core.domain.base import DomainModel

__all__ = ["Guidebook", "load_guidebook", "DEFAULT_GUIDEBOOK_BUDGET", "DEFAULT_PATH"]

#: 注入 system prompt 的字符预算（默认值）。未经实测标定：字符数是 token 的近似。
DEFAULT_GUIDEBOOK_BUDGET = 8000

#: 与本模块同目录的指引文档。
DEFAULT_PATH = Path(__file__).with_name("guidebook.md")


class Guidebook(DomainModel):
    """一次加载的结果。各字段如实说明「拿到了多少」，不粉饰。"""

    text: str = ""
    missing: bool = False
    """文件不存在。上层据此标注降级，而不是把空指引当成「没有可说的」。"""

    truncated: bool = False
    """内容超出预算被截断。"""

    total_chars: int = 0
    """原文总字符数。截断时让「喂了多少 / 共有多少」可核对。"""


def load_guidebook(
    path: Path | None = None, *, budget: int = DEFAULT_GUIDEBOOK_BUDGET
) -> Guidebook:
    """读取指引文档并按预算截断。任何读失败都落到 ``missing``，不抛异常。"""
    source = path or DEFAULT_PATH
    try:
        text = source.read_text(encoding="utf-8").strip()
    except OSError:
        return Guidebook(missing=True)
    if not text:
        return Guidebook(missing=True)

    total = len(text)
    if total > budget:
        return Guidebook(
            text=text[:budget].rstrip() + "\n\n（指引内容过长，此处截断）",
            truncated=True,
            total_chars=total,
        )
    return Guidebook(text=text, total_chars=total)
