"""L1 定义层 + L2 运行时内核的领域实体与值对象（架构设计 v0.02 §5）。"""

from .approval import (
    Approval,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTimeoutPolicy,
)
from .base import DomainModel, Entity, new_id, now_iso, parse_ts, utcnow
from .edge import ContractWaiver, Edge, EdgeContract, Sensitivity
from .node import ExecutionProfile, NodeDefinition, RetryPolicy, VersionedRef
from .registry import (
    ApprovalPolicy,
    AuthMode,
    CredentialKind,
    CredentialRef,
    HarnessRegistration,
    MCPTransport,
    RiskLevel,
    SkillDoc,
    SkillScope,
    ToolLaunch,
    ToolSpec,
)
from .task import (
    STAGE_TERMINAL_STATES,
    STAGE_TRANSITIONS,
    TASK_TERMINAL_STATES,
    TASK_TRANSITIONS,
    Attempt,
    AttemptOutcome,
    CompactEvent,
    DesiredState,
    ErrorClass,
    OriginOfControl,
    PinnedGraph,
    StageState,
    Task,
    TaskStage,
    TaskState,
    Usage,
    stage_can_transition,
    task_can_transition,
)
from .template import (
    CredentialPlaceholder,
    InstantiationReport,
    MissingBinding,
    Template,
    TemplateDiff,
    TemplateKind,
    TemplateNodeConfig,
    TemplatePayload,
)
from .workflow import (
    GraphSpec,
    RevisionSource,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)

__all__ = [
    # approval
    "Approval",
    "ApprovalBinding",
    "ApprovalDecision",
    "ApprovalStatus",
    "ApprovalTimeoutPolicy",
    # base
    "DomainModel",
    "Entity",
    "new_id",
    "now_iso",
    "parse_ts",
    "utcnow",
    # edge
    "ContractWaiver",
    "Edge",
    "EdgeContract",
    "Sensitivity",
    # node
    "ExecutionProfile",
    "NodeDefinition",
    "RetryPolicy",
    "VersionedRef",
    # registry
    "ApprovalPolicy",
    "AuthMode",
    "CredentialKind",
    "CredentialRef",
    "HarnessRegistration",
    "MCPTransport",
    "RiskLevel",
    "SkillDoc",
    "SkillScope",
    "ToolLaunch",
    "ToolSpec",
    # task
    "Attempt",
    "AttemptOutcome",
    "CompactEvent",
    "DesiredState",
    "ErrorClass",
    "OriginOfControl",
    "PinnedGraph",
    "STAGE_TERMINAL_STATES",
    "STAGE_TRANSITIONS",
    "StageState",
    "TASK_TERMINAL_STATES",
    "TASK_TRANSITIONS",
    "Task",
    "TaskStage",
    "TaskState",
    "Usage",
    "stage_can_transition",
    "task_can_transition",
    # template
    "CredentialPlaceholder",
    "InstantiationReport",
    "MissingBinding",
    "Template",
    "TemplateDiff",
    "TemplateKind",
    "TemplateNodeConfig",
    "TemplatePayload",
    # workflow
    "GraphSpec",
    "RevisionSource",
    "WorkflowDefinition",
    "WorkflowRevision",
    "WorkflowStatus",
]
