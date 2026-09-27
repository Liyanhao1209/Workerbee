"""节点定义与执行候选（架构设计 v0.02 §5.1 NodeDefinition / ExecutionProfile）。

要点：
- 执行候选是**有序列表**，取代 v0.01 三个 Preferences 数组的下标对齐。
  对齐键是 ``profile_id``：重排、删除候选项不会造成模型／harness／凭据错配（CFG-01）。
- ``enabled`` 是节点启停的唯一事实源（§1.2 原则 1）。启停操作只翻转此字段并 bump revision。
- ``required_inputs`` 是对 §5.1 字段表的增列（§5 允许增列内部字段）：ACT-03 的
  「下游声明的必需输入」必须有落点，放在边上的话，节点被停用后需求会随之消失。
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from .base import DomainModel, Entity, new_id

__all__ = ["RetryPolicy", "ExecutionProfile", "VersionedRef", "NodeDefinition"]


class RetryPolicy(DomainModel):
    """有界重试（CFG-03、D-05）。自动尝试必须有终点。"""

    max_attempts: int = 3
    """单个候选上的最大尝试次数（含首次）。耗尽后转向下一候选。"""

    backoff_base_ms: int = 1000
    backoff_cap_ms: int = 60_000

    retryable_errors: list[str] = Field(
        default_factory=lambda: ["network", "rate_limit", "server_error"]
    )
    """适配器可覆盖声明。默认对应 D-05：网络错误 / 429 / 5xx 可重试；
    认证失败、4xx 配置错误、契约校验失败不可重试。"""

    jitter_ratio: float = 0.2
    """抖动比例。退避 = min(base × 2^n, cap) ± jitter_ratio，避免同刻重试叠加。"""

    @field_validator("max_attempts")
    @classmethod
    def _at_least_one(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_attempts 至少为 1（首次执行本身算一次尝试）")
        return v

    @field_validator("backoff_base_ms", "backoff_cap_ms")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("退避时长不能为负")
        return v

    @model_validator(mode="after")
    def _cap_ge_base(self) -> "RetryPolicy":
        if self.backoff_cap_ms < self.backoff_base_ms:
            raise ValueError("backoff_cap_ms 不能小于 backoff_base_ms")
        return self


class ExecutionProfile(DomainModel):
    """一组执行候选：模型 + harness + 凭据 + 运行参数。

    系统只做兼容性检查，不评价模型主观强弱（清单 §1.3）。
    """

    profile_id: str = Field(default_factory=new_id)

    model_name: str = ""
    """模型名。**留空表示使用该 harness 的默认模型。**

    CFG-01 要求候选「能明确关联模型」，留空并不意味着不明确：它明确表达了
    「由 harness 自己决定」。强迫用户为每个 harness 猜一个模型别名只会
    制造一行假配置。实际使用的模型由适配器在会话建立后如实上报，
    记进 ``Attempt.profile_snapshot``，CFG-07 的可追溯性由此保证。
    """

    harness_ref: str | None = None
    """引用 HarnessRegistration.harness_id。模型与 harness 解耦，多对多可表达。"""

    credential_ref: str | None = None
    """引用 CredentialRef.credential_id。永不内嵌凭据本体（AUTH-02）。"""

    reasoning_effort: str | None = None
    """以适配器验证的能力为准。不可用的取值必须提示，不能静默忽略（CFG-02）。
    仅在 harness 未声明该维度（harness 无此概念）时允许为 None。"""

    permission_mode: str | None = None
    """该候选运行的权限模式（如 ``default`` / ``acceptEdits`` / ``bypassPermissions``）。

    这个字段存在的理由（HUM-03、§8.1）：**harness 是否会向用户请求授权**取决于它，
    而不是取决于框架。当 harness 没有权限钩子（框架无法代你拦截审批）时，
    必须由用户**显式**选定一个不询问的模式，框架才允许执行——而不是替用户
    默认成「自动放行」。权限相关的事不做隐式默认。

    None 表示未指定。适配器会如实拒绝，而不是替用户挑一个。
    """

    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    compact_threshold: int | None = None
    """用户**期望**触发上下文整理的阈值，不是模型最大窗口（R §1.3.2 澄清）。
    实际触发点为 min(用户阈值, harness 实际上限) − 安全余量（CFG-04、§7.4）。"""

    extra: dict[str, str] = Field(default_factory=dict)
    """透传给适配器的附加参数（如 API base url 覆盖、model 别名）。
    不进 prompt、不进普通日志；含凭据性内容时必须走 credential_ref。"""

    @field_validator("compact_threshold")
    @classmethod
    def _positive_threshold(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("compact_threshold 必须为正数；留空表示不主动整理")
        return v

    def display(self) -> str:
        bits = [self.model_name or "(harness 默认)"]
        if self.harness_ref:
            bits.append(f"@{self.harness_ref}")
        if self.reasoning_effort:
            bits.append(f"effort={self.reasoning_effort}")
        return " ".join(bits)


class VersionedRef(DomainModel):
    """指向带版本的共享配置（Skill / Tool）。历史任务据此复现实际采用的内容。"""

    ref_id: str
    version: int | None = None
    """None 表示「跟随最新」。钉扎到具体版本才能满足 CFG-07 的可追溯要求。"""

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return f"{self.ref_id}@{self.version}" if self.version is not None else self.ref_id


class NodeDefinition(Entity):
    """流程中的一个逻辑阶段。"""

    node_id: str = Field(default_factory=new_id)
    """跨 revision 稳定，供历史归因（§5.1）。"""

    name: str
    role: str | None = None
    """用于上下文组装 P1 分区（§7.3）。如「规划者」「编码者」「审计者」。"""

    description: str | None = None

    enabled: bool = True
    """启停唯一事实源。启停操作只翻转此字段并 bump revision（ACT-01）。"""

    system_prompt: str | None = None
    """可选节点系统 prompt（CFG-06）。留空时仍能以 P1/P2 执行。"""

    profiles: list[ExecutionProfile] = Field(default_factory=list)
    """执行候选，有序即优先级。CFG-01 要求可运行节点至少一组。"""

    skill_refs: list[VersionedRef] = Field(default_factory=list)
    tool_refs: list[VersionedRef] = Field(default_factory=list)

    required_inputs: list[str] = Field(default_factory=list)
    """下游声明的必需输入字段名（ACT-03）。空表示不参与输入衔接的机器校验。"""

    ui_position: tuple[float, float] | None = None
    """前端布局，纯展示。"""

    @model_validator(mode="after")
    def _unique_profile_ids(self) -> "NodeDefinition":
        seen: set[str] = set()
        for p in self.profiles:
            if p.profile_id in seen:
                raise ValueError(f"节点 {self.name} 内存在重复的 profile_id: {p.profile_id}")
            seen.add(p.profile_id)
        return self

    def profile(self, profile_id: str) -> ExecutionProfile | None:
        for p in self.profiles:
            if p.profile_id == profile_id:
                return p
        return None

    def profile_by_index(self, index: int) -> ExecutionProfile | None:
        if 0 <= index < len(self.profiles):
            return self.profiles[index]
        return None

    def is_runnable(self) -> bool:
        """可运行 = 已启用且至少有一组执行候选。"""
        return self.enabled and len(self.profiles) > 0
