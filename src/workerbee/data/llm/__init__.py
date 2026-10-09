"""框架自身 LLM 能力的后端子包（架构设计 v0.02 §7.2、§9.3）。

**边界**：这里只服务框架自身的能力——生成交接摘要、AI 建图草稿。workflow 节点的
执行由 L3 适配器负责，与本子包无关（见 :mod:`.backend` 的模块文档）。

对外公开面（其余为实现细节）：

- 协议与数据：:class:`LLMMessage` / :class:`LLMResponse` / :class:`LLMBackend`
  / :class:`SecretResolver`
- 后端：:class:`HarnessCLIBackend`（默认，零额外配置）、
  :class:`OpenAICompatBackend`、:class:`AnthropicBackend`
- 路由：:class:`LLMRouter` / :class:`LLMRouterConfig` / :class:`BackendConfig`
- 错误：:class:`LLMError` 及其子类（消息已脱敏，可安全进日志与事件）
"""

from .anthropic import AnthropicBackend
from .backend import (
    BASE_URL_ALIASES,
    SECRET_KEY_ALIASES,
    LLMAllBackendsFailed,
    LLMAuthError,
    LLMBackend,
    LLMBackendError,
    LLMChunk,
    LLMConfigError,
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolSpec,
    LLMUnavailableError,
    SecretResolver,
    extract_credentials,
    resolve_credentials,
)
from .harness_cli import (
    CLAUDE_CLI,
    HARNESS_CLI_SPECS,
    KIMI_CLI,
    HarnessCLIBackend,
    HarnessCLISpec,
)
from .openai_compat import OpenAICompatBackend
from .router import BackendConfig, LLMRouter, LLMRouterConfig

__all__ = [
    # 协议与数据
    "LLMMessage",
    "LLMResponse",
    "LLMChunk",
    "LLMToolCall",
    "LLMToolSpec",
    "LLMBackend",
    "SecretResolver",
    # 后端
    "HarnessCLIBackend",
    "HarnessCLISpec",
    "CLAUDE_CLI",
    "KIMI_CLI",
    "HARNESS_CLI_SPECS",
    "OpenAICompatBackend",
    "AnthropicBackend",
    # 路由与配置
    "LLMRouter",
    "LLMRouterConfig",
    "BackendConfig",
    # 错误
    "LLMError",
    "LLMBackendError",
    "LLMTimeoutError",
    "LLMResponseError",
    "LLMConfigError",
    "LLMAuthError",
    "LLMUnavailableError",
    "LLMAllBackendsFailed",
    # 凭据解析
    "resolve_credentials",
    "extract_credentials",
    "SECRET_KEY_ALIASES",
    "BASE_URL_ALIASES",
]
