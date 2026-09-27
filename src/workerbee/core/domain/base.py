"""领域模型共用基类与工具。

设计约束（架构设计 v0.02 §5）：
- 所有实体含 created_at / updated_at。
- 字段表是设计基线而非穷举：可增列内部字段，不得删除声明语义的字段。
- ``extra="forbid"`` 让拼写错误在构造点即失败，而不是静默丢失配置。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DomainModel", "Entity", "new_id", "utcnow", "parse_ts", "now_iso"]


def new_id() -> str:
    """生成实体主键。系统生成的标识（Task/TaskStage/Attempt 等）一律走这里。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """带时区的当前时间。全系统不使用 naive datetime。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def parse_ts(value: str | datetime) -> datetime:
    """解析存储层回读的时间戳，始终返回 aware datetime。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DomainModel(BaseModel):
    """领域模型基类：禁止未知字段。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    def model_copy_update(self, **changes: Any) -> "DomainModel":
        """返回带更新的浅拷贝（不改动原对象）。"""
        return self.model_copy(update=changes)


class Entity(DomainModel):
    """带生命周期时间戳的实体。"""

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()
