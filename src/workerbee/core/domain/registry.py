"""共享配置注册表（架构设计 v0.02 §5.1）。

四类实体：SkillDoc / ToolSpec / CredentialRef / HarnessRegistration。
共同约束：
- 被节点引用（``NodeDefinition.skill_refs`` / ``tool_refs`` / ``profile.*_ref``），
  因此修改与删除都必须给出影响面并按 EXT-03 处理，不得静默改写历史任务采用的配置。
- ``CredentialRef`` 只持有 ``secret_locator``；凭据本体只存在于 L5 Secret Store，
  其余任何位置（节点、模板、事件、日志、摘要、Graph Capture 材料）只出现引用（AUTH-02）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from .base import Entity, new_id

__all__ = [
    "SkillScope",
    "SkillDoc",
    "MCPTransport",
    "ToolLaunch",
    "RiskLevel",
    "ApprovalPolicy",
    "ToolSpec",
    "CredentialKind",
    "CredentialRef",
    "AuthMode",
    "HarnessRegistration",
]


class SkillScope(StrEnum):
    GLOBAL = "global"
    """工具库中的可复用 Skill。"""

    NODE_LOCAL = "node_local"
    """仅为当前节点临时定义（EXT-01）。"""


class SkillDoc(Entity):
    """执行指导文档。

    边界（RES-04、AC-22）：Skill 是**指导**，UI 与文档中不得显示为
    「框架已强制的资源限制」。框架强制的只有资源归属与释放义务。
    """

    skill_id: str = Field(default_factory=new_id)
    name: str
    content: str = ""
    version: int = 1
    scope: SkillScope = SkillScope.GLOBAL

    enabled: bool = True
    """停用后不再出现在可选列表；已引用它的历史任务记录不变（EXT-03）。"""


class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"
    """具体协议实现按 EXT-02 在设计阶段调研确定；此处声明传输类别。"""


class ToolLaunch(Entity):
    """MCP 工具的启动与连接信息。"""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    """只能放非凭据型环境变量；需要认证时用 ``credential_ref``。"""

    cwd: str | None = None
    url: str | None = None
    """http / sse 传输的端点。"""

    transport: MCPTransport = MCPTransport.STDIO

    credential_ref: str | None = None
    """该工具调用所需凭据的引用；凭据本体不入此表。"""

    @field_validator("env")
    @classmethod
    def _reject_obvious_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        """拒绝把明显是凭据的环境变量直接写进工具定义（AUTH-02 的早失败防线）。

        这里拦不住所有形态，但能让最常见的「顺手粘一个 key 进去」在构造点失败，
        而不是等到它进了日志、模板和事件历史之后才发现。
        """
        suspicious = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        for name, value in v.items():
            if any(s in name.upper() for s in suspicious) and value and not value.startswith("$"):
                raise ValueError(
                    f"环境变量 {name} 看起来包含凭据。请改用 credential_ref 引用 "
                    f"Secret Store 中的凭据，或使用 ${name} 形式的运行时占位符。"
                )
        return v


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalPolicy(StrEnum):
    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


class ToolSpec(Entity):
    """MCP 工具登记项（EXT-02）。

    风险分级与审批策略挂在工具级（§9.4）。本设计不引入覆盖所有危险命令的
    独立风险分类器（遵从清单 §3.8 注记）。
    """

    tool_id: str = Field(default_factory=new_id)
    name: str
    kind: Literal["mcp"] = "mcp"

    launch: ToolLaunch = Field(default_factory=ToolLaunch)

    description: str | None = None
    """工具的输入、输出、适用场景说明——会进入 ContextPackage 的 P4 分区。"""

    io_schema: dict[str, Any] = Field(default_factory=dict)

    risk_level: RiskLevel = RiskLevel.MEDIUM
    approval_policy: ApprovalPolicy = ApprovalPolicy.ASK

    version: int = 1
    health_check: bool = True
    """登记时是否执行连通性检查。"""

    enabled: bool = True


class CredentialKind(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"
    BASE_URL_PAIR = "base_url_pair"
    """base_url + api_key 成对提供（AUTH-01）。"""

    HARNESS_LOGIN = "harness_login"
    """使用 harness 已有的本机登录态，框架不持有密钥（如 Kimi Code 的 OAuth）。"""


class CredentialRef(Entity):
    """凭据**引用**。凭据本体不出 L5（AUTH-02）。"""

    credential_id: str = Field(default_factory=new_id)
    label: str
    kind: CredentialKind

    secret_locator: str | None = None
    """指向 Secret Store 中的条目。``harness_login`` 类型为 None——
    此时凭据由 harness 自身的登录态提供，框架无从也无权读取。"""

    base_url: str | None = None
    """非敏感元数据，可与 ``base_url_pair`` 搭配；不含密钥。"""

    revoked: bool = False
    """撤销后影响面可见（引用它的 Workflow／节点／在途任务清单），
    且新 Attempt 必须重新绑定有效凭据，不得以钉扎快照绕过（§9.1、D-02）。"""

    def is_usable(self) -> bool:
        return not self.revoked


class AuthMode(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"
    BASE_URL_PAIR = "base_url_pair"
    NATIVE_LOGIN = "native_login"


class HarnessRegistration(Entity):
    """一个已登记的本机 harness（HAR-01）。

    ``capabilities_snapshot`` 是接口支持情况的声明，**不是模型能力评级**
    （清单 §3.4 来源注记）。真实值以适配器 probe() 的实测结果为准（HAR-02），
    此处缓存供配置期做兼容性预检。
    """

    harness_id: str = Field(default_factory=new_id)
    name: str
    adapter_id: str
    adapter_version: str | None = None

    exec_path: str | None = None
    """可执行文件位置。None 表示由适配器自行解析（如从 PATH 查找）。"""

    env_template: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    auth_binding: str | None = None
    """CredentialRef.credential_id；None 表示依赖本机登录态。"""

    auth_mode: AuthMode = AuthMode.NATIVE_LOGIN

    capabilities_snapshot: dict[str, Any] | None = None
    """最近一次 probe() 的能力声明缓存。"""

    last_probe_at: str | None = None
    last_probe_ok: bool | None = None
    last_probe_error: str | None = None

    enabled: bool = True

    def is_usable(self) -> bool:
        return self.enabled and self.last_probe_ok is not False
