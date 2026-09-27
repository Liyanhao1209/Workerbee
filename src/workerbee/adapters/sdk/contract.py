"""适配器六组契约的数据类型（架构设计 v0.02 §5.3、§8.1、D-09）。

能力声明是**接口支持情况的声明，不是模型能力评级**（清单 §3.4 来源注记）。
适配器作者必须如实声明；内核据此做配置期与启动期的兼容性判定，
「不支持关键能力的组合不能以完整支持状态接受」（HAR-02）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AdapterCapabilities",
    "PauseSupport",
    "AdapterManifest",
    "HarnessConfig",
    "CreateSessionRequest",
    "SessionInfo",
    "AdapterEvent",
    "PermissionRequest",
    "Heartbeat",
    "InputKind",
    "TerminateSignal",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputKind(StrEnum):
    USER = "user"
    """普通输入。"""

    BTW = "btw"
    """运行中补充指示（HUM-01）。必须到达**所选的那个会话**。"""

    INTERRUPT = "interrupt"
    """打断当前执行并与 agent 交互（HUM-02）。"""


class TerminateSignal(StrEnum):
    TERM = "TERM"
    KILL = "KILL"


class PauseSupport(StrEnum):
    """D-07 逐 harness 声明的暂停能力四档。"""

    IN_PLACE = "in_place"
    """原位暂停：会话冻结，恢复后从原处继续。"""

    CHECKPOINT = "checkpoint"
    """协作停止 + checkpoint 重建。恢复时新 session 从断点继续。"""

    RESTART = "restart"
    """协作停止 + 从头重跑本阶段。恢复代价最高，但语义仍诚实。"""

    NONE = "none"
    """不支持暂停；收到暂停请求如实拒绝并说明，不得显示为已暂停。"""


class AdapterCapabilities(_Model):
    """接口支持情况。字段名刻意与架构设计 §5.3 的清单一致。"""

    # --- 与 §5.3 逐项对齐 ---
    create_session: bool = True
    resume_session: bool = False
    read_output: bool = True
    interact: bool = False
    """是否支持运行中交互（HUM-01/02）。"""

    interrupt: bool = False
    stop: bool = True
    compact: bool = False
    """是否支持上下文整理（CFG-04）。False 时内核不得声称已执行整理。"""

    permission_hook: bool = False
    """是否能把 harness 的权限确认事件转译为 Approval。
    False 时该 harness 不得被声明为支持非自动权限模式（HUM-03）。"""

    background_tasks: bool = False
    """是否能上报后台工作状态。True 才能满足 RUN-06 的完成判据。"""

    # --- 暂停能力（D-07）---
    pause_in_place: bool = False
    checkpoint_resume: bool = False
    keep_checkpoint_on_stop: bool = False

    # --- 配置兼容性判定所需 ---
    reasoning_efforts: list[str] = Field(default_factory=list)
    """该 harness 实际支持的 effort 取值。空列表表示此维度不适用。
    CFG-02：用户填了不受支持的取值必须提示，不能静默忽略。"""

    models: list[str] = Field(default_factory=list)
    """已知可用的模型名。空表示不限定（由用户自行填写）。"""

    auth_modes: list[str] = Field(default_factory=list)
    """该适配器支持的认证方式，取值见 AuthMode。"""

    token_usage: bool = False
    """是否能上报用量。False 时内核把 usage 记为「未知」而非 0（OBS-04）。"""

    structured_output: bool = False
    """是否能产出可解析为结构化事件的输出流。"""

    def pause_support(self) -> PauseSupport:
        if self.pause_in_place:
            return PauseSupport.IN_PLACE
        if self.checkpoint_resume:
            return PauseSupport.CHECKPOINT
        if self.stop:
            return PauseSupport.RESTART
        return PauseSupport.NONE


class AdapterManifest(_Model):
    """适配器自述。握手与 declare() 都返回它。"""

    adapter_id: str
    version: str
    protocol_version: str

    harness_family: str
    """harness 家族名，如 claude-code / kimi-code。"""

    display_name: str | None = None
    capabilities: AdapterCapabilities = Field(default_factory=AdapterCapabilities)

    platform_matrix: dict[str, Any] = Field(default_factory=dict)
    """各平台的支持情况与限制（PLAT-01）。示例：
    ``{"linux": {"supported": true, "notes": "..."}}``"""

    auth_modes: list[str] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)
    """适配器作者如实声明的限制。会展示在配置界面（HAR-02）。"""

    def missing_key_capabilities(self) -> list[str]:
        """关键能力缺失清单，供 UI 与校验管线提示。"""
        missing: list[str] = []
        if not self.capabilities.permission_hook:
            missing.append("审批（permission_hook）")
        if not self.capabilities.compact:
            missing.append("自动上下文整理（compact）")
        if not self.capabilities.background_tasks:
            missing.append("后台工作状态（background_tasks）")
        if not self.capabilities.resume_session:
            missing.append("会话恢复（resume_session）")
        return missing


class HarnessConfig(_Model):
    """把「怎么启动这个 harness」从节点定义中解耦出来。

    注意：``credential`` 是**已解析的凭据明文**，只在派发时向内传给适配器子进程。
    它不进事件日志、不进 prompt、不进摘要（AUTH-02）。
    """

    harness_id: str
    exec_path: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    adapter_id: str | None = None
    adapter_options: dict[str, Any] = Field(default_factory=dict)

    credential: dict[str, str] | None = None
    """解析后的凭据（如 ``{"api_key": "...", "base_url": "..."}``）。
    为 None 表示依赖 harness 的本机登录态。"""

    auth_mode: str = "native_login"


class CreateSessionRequest(_Model):
    """创建会话。一次 Attempt 一份。"""

    harness: HarnessConfig
    model_name: str
    reasoning_effort: str | None = None
    system_prompt: str | None = None

    session_ref_hint: str | None = None
    """内核期望的会话标识。适配器可采纳，也可另生成并如实回报。"""

    attempt_id: str | None = None
    permission_mode: str | None = None
    """如 ``default`` / ``acceptEdits`` / ``bypassPermissions``。
    适配器不支持时返回 NOT_SUPPORTED，不得静默替换。"""

    extra: dict[str, Any] = Field(default_factory=dict)


class SessionInfo(_Model):
    """会话台账项（§5.3 SessionHandle 的适配器侧视图）。"""

    session_ref: str
    harness_id: str
    state: Literal["alive", "ended", "lost", "starting"] = "alive"

    persist_locator: str | None = None
    """harness 自己的会话持久化标识（如 Claude Code 的 session id、
    Kimi Code 的 session 名）。恢复会话时用它。"""

    capabilities_used: list[str] = Field(default_factory=list)
    model_name: str | None = None
    created_at: str | None = None
    last_heartbeat: str | None = None

    pid: int | None = None
    """子进程 pid，登记进资源台账供 Reaper 对账（RES-02）。"""

    cwd: str | None = None
    transcript_path: str | None = None

    def is_alive(self) -> bool:
        return self.state == "alive"


class AdapterEvent(_Model):
    """统一事件流的单条事件。"""

    kind: str
    """见 protocol.EventKind。"""

    session_ref: str | None = None
    attempt_id: str | None = None

    seq: int | None = None
    """适配器侧单调序号，供断线重连后补齐。"""

    text: str | None = None
    """输出类事件的文本内容。"""

    data: dict[str, Any] = Field(default_factory=dict)
    """事件附加数据：tool 名、后台工作 id、compact 前后用量等。"""

    ts: str | None = None


class PermissionRequest(_Model):
    """harness 的权限确认事件（HUM-03）。

    ``action_fingerprint`` 是动作内容的指纹：内容一变，旧批准自动作废（AC-14）。
    """

    approval_id: str
    session_ref: str
    attempt_id: str | None = None

    action: str
    target: str | None = None
    risk: str | None = None

    tool_name: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def fingerprint(action: str, target: str | None, tool_name: str | None) -> str:
        import hashlib

        payload = f"{tool_name or ''}\x00{action}\x00{target or ''}"
        return hashlib.sha256(payload.encode()).hexdigest()[:32]


class Heartbeat(_Model):
    session_ref: str
    ts: str
    alive: bool = True
    detail: str | None = None
