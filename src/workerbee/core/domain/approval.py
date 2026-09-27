"""审批实体（架构设计 v0.02 §5.5、§9.2、HUM-03/04）。

关键语义：
- 审批**精确绑定执行尝试与配置版本**（``bound_to``）。尝试失效或命令内容改变后，
  旧批准自动作废——「任务删除、尝试失效、命令内容变化均使旧批准作废；
  重复通知不重复授权」（AC-14）。
- 断连、超时、重复通知**都不构成批准**（HUM-04）。回传失败必须可见。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from .base import DomainModel, Entity, new_id, utcnow

__all__ = [
    "ApprovalStatus",
    "ApprovalTimeoutPolicy",
    "ApprovalDecision",
    "ApprovalBinding",
    "Approval",
]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    UNDELIVERABLE = "undeliverable"
    """决定已产生但回注原会话失败。必须可见并可追溯（HUM-04）。"""

    SUPERSEDED = "superseded"
    """被绑定的尝试失效或命令内容变化而作废（AC-14）。"""


class ApprovalTimeoutPolicy(StrEnum):
    DENY_PAUSE = "deny_pause"
    """默认：超时拒绝该动作，并把阶段置为显式等待态，释放 stream 资源。"""


class ApprovalBinding(DomainModel):
    """审批精确绑定到一次执行尝试与配置版本。

    这是 AC-14 的机器保证：节点开始下一任务后，旧任务的批准不可能作用到新 session。
    """

    task_id: str
    stage_id: str
    attempt_id: str
    revision_seq: int
    node_id: str | None = None

    action_fingerprint: str | None = None
    """被授权动作内容的指纹。内容变化即作废，防止「改了命令仍复用旧批准」。"""


class ApprovalDecision(DomainModel):
    by: str = "user"
    at: datetime = Field(default_factory=utcnow)
    modified_action: str | None = None
    """用户可修改后批准；回注原请求方的是修改后的动作。"""

    note: str | None = None


class Approval(Entity):
    """一次人工审批请求。"""

    approval_id: str = Field(default_factory=new_id)

    bound_to: ApprovalBinding

    action: str = ""
    """操作内容（如 ``Bash(rm -rf build/)``）。"""

    target: str | None = None
    """操作目标。"""

    risk: str | None = None
    """可取得的权限与风险信息。不要求另造风险分类器（清单 §3.8 注记）。"""

    tool_name: str | None = None
    workflow_name: str | None = None

    status: ApprovalStatus = ApprovalStatus.PENDING
    timeout_policy: ApprovalTimeoutPolicy = ApprovalTimeoutPolicy.DENY_PAUSE

    decision: ApprovalDecision | None = None

    expires_at: datetime | None = None
    detail: str | None = None
    """失败原因、超时说明、回传失败的技术细节——都必须对用户可见。"""

    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    def is_open(self) -> bool:
        """仍在等待处理（用于「需处理」入口 OBS-05）。"""
        return self.status in (ApprovalStatus.PENDING, ApprovalStatus.UNDELIVERABLE)

    def invalidate(self, why: str) -> None:
        self.status = ApprovalStatus.SUPERSEDED
        self.detail = why
        self.touch()

    def action_changed(self, new_fingerprint: str) -> bool:
        return (
            self.bound_to.action_fingerprint is not None
            and self.bound_to.action_fingerprint != new_fingerprint
        )
