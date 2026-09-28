"""框架自身 LLM 能力的后端抽象（架构设计 v0.02 §7.2、§7.4、§9.1、D-06）。

**边界（本子包最重要的一条纪律）**：这里的后端只服务**框架自身**的能力——
生成交接摘要（§7.2）、AI 建图（§9.3）。它们**不参与 workflow 节点的执行**。
节点执行一律走 L3 适配器（``adapters/``），由用户配置的候选（ExecutionProfile）
与凭据驱动。把两者混起来会让「框架替用户干活」与「框架替自己干活」的凭据、配额、
审计边界同时失效——这条边界必须由代码结构保证，而不是靠调用方自觉。

凭据纪律（AUTH-02、§9.1）：
- 凭据从 Secret Store 取，接口签名为 ``async get(locator) -> dict[str, str] | None``
  （``security/secret_store.py``）。本模块只依赖该签名（结构化 Protocol），
  不 import 其实现，也不感知其存储细节。
- 凭据以 :class:`~workerbee.data.redact.Secret` 持有，``repr`` 一律脱敏；
  任何要进入异常消息或日志的文本先过 ``redact_text``。
- **凭据不进 prompt**：调用方只传 :class:`LLMMessage`；本模块只把凭据放进
  传输层头部（``Authorization`` / ``x-api-key``）。

「未知 ≠ 零」同样适用：CLI 后端拿不到精确用量时 ``usage`` 为 ``None``（未知），
不填 0；模型名取不到时写 ``"unknown"``，不假装知道。
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import Field

from ...core.domain.base import DomainModel
from ...core.domain.task import Usage
from ..redact import Secret, redact_text

__all__ = [
    "LLMMessage",
    "LLMResponse",
    "LLMBackend",
    "SecretResolver",
    "LLMError",
    "LLMBackendError",
    "LLMTimeoutError",
    "LLMResponseError",
    "LLMConfigError",
    "LLMAuthError",
    "LLMUnavailableError",
    "LLMAllBackendsFailed",
    "resolve_credentials",
    "extract_credentials",
    "system_text",
    "non_system_messages",
    "SECRET_KEY_ALIASES",
    "BASE_URL_ALIASES",
]

#: Secret Store 返回字典中「密钥」字段的候选键名。规范键是 ``api_key``，
#: 其余为兼容别名——Secret Store 的实现由另一端负责，接口只有签名没有字段表。
SECRET_KEY_ALIASES: tuple[str, ...] = ("api_key", "apikey", "key", "token", "secret")

#: base_url 的候选键名。规范键是 ``base_url``。
BASE_URL_ALIASES: tuple[str, ...] = ("base_url", "baseurl", "endpoint", "url")


# ---------------------------------------------------------------------------
# 消息与响应
# ---------------------------------------------------------------------------


class LLMMessage(DomainModel):
    """一次补全消息。角色集合与主流 API 一致，不引入额外的角色语义。"""

    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(DomainModel):
    """一次补全的结果。

    ``backend`` 是**实际**执行这次补全的后端名，不是期望的后端名——降级必须可见
    （见 :class:`~workerbee.data.llm.router.LLMRouter`），调用方据此写入事件日志。
    """

    text: str
    usage: Usage | None = None
    """用量。``None`` 表示不可取得（未知），不是 0（OBS-04）。"""

    model: str
    backend: str

    # ---- 增列字段：降级可见性（§7、DATA-03 不得静默降级） ----

    fallback_from: str | None = None
    """非 None 表示「主后端失败，实际用了这个后端」。由 router 填写。"""

    degraded_reasons: list[str] = Field(default_factory=list)
    """降级原因链，形如 ``["openai-compat:rate_limit", ...]``。已过脱敏。"""

    def degraded(self) -> bool:
        return self.fallback_from is not None


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """框架自身 LLM 调用失败的基类。

    分类口径对齐 D-05 的错误分类：网络／限流／5xx 可重试；认证失败、4xx 配置错误、
    契约校验失败不可重试。**消息在构造时即脱敏**，因此可以安全地进日志与事件。

    ``retryable`` 只表达「重试同一个后端是否可能成功」；降级到备用后端是与它
    正交的决策——router 对两类错误都会尝试下一个后端，只是把分类如实记下来。
    """

    kind: str = "llm_error"

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        kind: str | None = None,
        retryable: bool | None = None,
        secrets: Iterable[Secret | str] | None = None,
    ) -> None:
        safe = redact_text(str(message), secrets)
        self.backend = backend
        self.secrets_scrubbed = safe != str(message)
        if kind is not None:
            self.kind = kind
        if retryable is not None:
            self.retryable = retryable
        prefix = f"[{self.kind}]" if backend is None else f"[{self.kind}/{backend}]"
        super().__init__(f"{prefix} {safe}")

    retryable: bool = True

    def as_reason(self) -> str:
        """供事件日志／降级链使用的短标签（只含分类，不含正文）。"""
        return f"{self.backend or '?'}:{self.kind}"


class LLMBackendError(LLMError):
    """传输层或后端进程失败（网络错误、429、5xx、非零退出码）。可重试。"""

    kind = "backend_error"


class LLMTimeoutError(LLMError):
    """超时。可重试（对端慢或被限流）。"""

    kind = "timeout"


class LLMResponseError(LLMError):
    """响应不可解析、缺字段，或输出被 max_tokens 截断。

    不可重试：同样的输入很可能得到同样的坏输出（D-05 把契约校验失败归为不可重试）。
    截断是**显式失败**——框架不得把截断的半截文本当作完整摘要（DATA-03）。
    """

    kind = "bad_response"
    retryable = False


class LLMConfigError(LLMError):
    """配置或凭据问题（缺 locator、缺字段、4xx 配置错误）。不可重试。"""

    kind = "config_error"
    retryable = False


class LLMAuthError(LLMConfigError):
    """认证失败（401/403）。不可重试，需人工重新绑定凭据。"""

    kind = "auth"


class LLMUnavailableError(LLMError):
    """后端在当前环境不可用（CLI 未安装、健康检查失败）。不可重试。"""

    kind = "unavailable"
    retryable = False


class LLMAllBackendsFailed(LLMError):
    """全部后端耗尽。携带完整的尝试链，便于用户定位（D-05：展示各次原因）。"""

    kind = "all_failed"
    retryable = False

    def __init__(self, attempts: Sequence[str], last: LLMError | None = None) -> None:
        chain = "、".join(attempts) if attempts else "（无已配置后端）"
        message = f"全部 {len(attempts)} 个 LLM 后端均失败：{chain}"
        if last is not None:
            message += f"；最后一个错误：{last}"
        super().__init__(message, kind=self.kind, retryable=False)
        self.attempts = list(attempts)


# ---------------------------------------------------------------------------
# 凭据解析（只依赖 Secret Store 的签名）
# ---------------------------------------------------------------------------


@runtime_checkable
class SecretResolver(Protocol):
    """Secret Store 的最小接口面（``security/secret_store.py``）。

    本模块只依赖这一个方法：取不到或 locator 无效时返回 ``None``，
    不抛异常、不返回空字典——「没有」与「有但是空的」必须可区分。
    """

    async def get(self, locator: str) -> dict[str, str] | None: ...


async def resolve_credentials(
    resolver: SecretResolver | None, locator: str | None
) -> dict[str, str] | None:
    """按 locator 取凭据。locator 为 None 或解析器缺失时返回 None（不是空字典）。"""
    if resolver is None or not locator:
        return None
    fields = await resolver.get(locator)
    if not fields:
        return None
    return {str(k): str(v) for k, v in dict(fields).items()}


def extract_credentials(
    fields: Mapping[str, str] | None,
    *,
    locator: str | None = None,
    require_key: bool = True,
    backend: str | None = None,
) -> tuple[str | None, Secret | None]:
    """从凭据字典里取出 ``(base_url, Secret)``。

    找不到密钥时按 ``require_key`` 决定是否抛 :class:`LLMConfigError`。
    报错信息只提到 locator 与缺哪个字段，**绝不含凭据本体**（此处也不可能含）。
    """
    if not fields:
        if require_key:
            raise LLMConfigError(
                f"凭据不可用：locator={locator or '<未配置>'}（Secret Store 无此条目）",
                backend=backend,
                kind="no_credential",
            )
        return None, None

    lowered = {str(k).lower(): str(v) for k, v in fields.items()}
    base_url = None
    for alias in BASE_URL_ALIASES:
        if lowered.get(alias):
            base_url = lowered[alias]
            break

    key_value = None
    for alias in SECRET_KEY_ALIASES:
        if lowered.get(alias):
            key_value = lowered[alias]
            break

    if key_value is None:
        if require_key:
            raise LLMConfigError(
                f"凭据缺少 api_key 字段：locator={locator or '<未配置>'} "
                f"（已提供字段：{sorted(lowered)}）",
                backend=backend,
                kind="no_credential",
            )
        return base_url, None
    return base_url, Secret(key_value, label=locator or None)


# ---------------------------------------------------------------------------
# 消息切分小工具（各后端共用，避免三处各写一遍而行为不一致）
# ---------------------------------------------------------------------------


def system_text(messages: Sequence[LLMMessage]) -> str:
    """把 system 消息拼成单段文本。无 system 消息时返回空串。"""
    parts = [m.content for m in messages if m.role == "system" and m.content]
    return "\n\n".join(parts)


def non_system_messages(messages: Sequence[LLMMessage]) -> list[LLMMessage]:
    """取出非 system 消息，保持原顺序。"""
    return [m for m in messages if m.role != "system"]


def transcript(messages: Sequence[LLMMessage]) -> str:
    """把多轮对话压成单段文本，供只接受「一次文本」的 CLI harness 使用。

    CLI 后端是单次文本进、文本出的形态，多轮是**压平**而不是真多轮——这一点必须
    在文档里说清楚，避免调用方以为得到了会话能力。框架自身的用途（摘要、AI 建图）
    都是单轮，压平不损失能力。
    """
    turns = non_system_messages(messages)
    if not turns:
        return ""
    if len(turns) == 1 and turns[0].role == "user":
        return turns[0].content
    blocks = [f"<{m.role}>\n{m.content}\n</{m.role}>" for m in turns]
    return "（以下为多轮对话转写）\n" + "\n".join(blocks)


# ---------------------------------------------------------------------------
# 后端协议
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMBackend(Protocol):
    """框架自身 LLM 能力的后端。

    实现必须满足：
    - ``complete`` 失败时抛 :class:`LLMError` 的子类（分类如实），不要抛裸异常；
      消息里不得含凭据。
    - ``health`` 不产生副作用、不发补全请求（只做可用性探测）。
    """

    name: str

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> LLMResponse: ...

    async def health(self) -> tuple[bool, str | None]:
        """``(是否可用, 说明)``。

        不可用时第二项是**原因**；可用时可以是一句补充说明（如「登录态未验证」）——
        能力边界如实标注比返回一个干净的 ``None`` 更有用。两种情况下都不得含凭据。
        """
        ...


def usage_from_counts(
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    extra: dict[str, Any] | None = None,
    cost_estimate: float | None = None,
    cost_basis: str | None = None,
) -> Usage | None:
    """构造 ``Usage``。两项都不可得时返回 ``None``（未知，不是 0）。"""
    if input_tokens is None and output_tokens is None:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        cost_basis=cost_basis,
        notes=None,
        **{k: v for k, v in (extra or {}).items() if k in {"cache_read_tokens",
                                                          "cache_write_tokens"}},
    )
