"""LLM 后端路由与降级（架构设计 v0.02 §7.2、D-05、DATA-03）。

职责只有两件：

1. **按配置选择后端**：后端列表有序即优先级（与 ``ExecutionProfile`` 的有序候选
   同构）。用户的配置可以只写一个 ``harness_cli``（零额外配置的默认路径）。
2. **主后端失败时降级到备用后端**——并且降级**必须可见**：
   - 返回值里 ``LLMResponse.backend`` 是**实际**使用的后端名；
   - ``fallback_from`` 非 None 表示发生过降级；
   - ``degraded_reasons`` 逐个列出失败的后端与错误分类（已脱敏）。

   调用方据此写入事件日志（``EventType.HANDOFF_FAILED`` / 摘要结果里的 ``backend``）。
   「静默降级」在这里是被代码结构排除的：降级路径上返回的必定是带标记的对象。

D-05 的单向推进纪律同样适用：候选按顺序推进，**已失败的后端在同一次调用中不再
回访**，避免 A↔B 震荡；全部耗尽即抛 :class:`LLMAllBackendsFailed`，携带完整尝试链。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Literal, Sequence

from pydantic import Field, model_validator

from ...core.domain.base import DomainModel
from ..redact import redact_text
from .anthropic import AnthropicBackend
from .backend import (
    LLMAllBackendsFailed,
    LLMBackend,
    LLMChunk,
    LLMError,
    LLMResponse,
    LLMMessage,
    LLMResponseError,
    LLMToolSpec,
    SecretResolver,
)
from .harness_cli import HarnessCLIBackend
from .openai_compat import OpenAICompatBackend

__all__ = ["LLMRouter", "BackendConfig", "LLMRouterConfig"]

#: 降级发生的回调签名：(尝试链, 实际使用的后端名)。供调用方写事件日志。
FallbackHook = Callable[[list[str], str], None]


class BackendConfig(DomainModel):
    """一个后端的声明式配置。

    ``kind`` 决定用哪个实现；其余字段按实现取用（未用的字段留空即可）。
    凭据只写 ``secret_locator``——**绝不在配置里写明文**（AUTH-02）。
    """

    kind: Literal["harness_cli", "openai_compat", "anthropic"]
    name: str | None = None

    model: str | None = None
    base_url: str | None = None
    secret_locator: str | None = None

    harness: str | None = None
    """``harness_cli`` 用：``claude`` / ``kimi``。"""

    binary: str | None = None
    """``harness_cli`` 用：可执行文件路径覆盖（默认从 PATH 查找）。"""

    output_format: str | None = None

    options: dict[str, Any] = Field(default_factory=dict)
    """透传给实现的其它参数（如 max_concurrency、timeout、default_max_tokens）。
    不得放凭据；含凭据性内容时走 ``secret_locator``。"""

    @model_validator(mode="after")
    def _reject_inline_secret(self) -> "BackendConfig":
        lowered = {k.lower() for k in self.options}
        for bad in ("api_key", "apikey", "token", "secret", "password"):
            if bad in lowered:
                raise ValueError(
                    f"options 中出现疑似凭据字段 {bad!r}：请改用 secret_locator 引用 Secret Store"
                )
        return self

    def build(self, *, secrets: SecretResolver | None = None) -> LLMBackend:
        """按配置实例化后端。"""
        opts = dict(self.options)
        if self.kind == "harness_cli":
            return HarnessCLIBackend(
                name=self.name,
                harness=self.harness or "claude",
                binary=self.binary,
                model=self.model,
                output_format=self.output_format,
                **opts,
            )
        if self.kind == "openai_compat":
            return OpenAICompatBackend(
                name=self.name or "openai-compat",
                model=self.model or "unknown",
                base_url=self.base_url,
                secret_locator=self.secret_locator,
                secrets=secrets,
                **opts,
            )
        return AnthropicBackend(
            name=self.name or "anthropic",
            model=self.model or "unknown",
            base_url=self.base_url,
            secret_locator=self.secret_locator,
            secrets=secrets,
            **opts,
        )


class LLMRouterConfig(DomainModel):
    """框架自身 LLM 能力的配置。

    默认值 = 「一个 harness CLI 后端」：复用本机已登录的 harness，用户零额外配置。
    需要更强能力或确定性时，再显式加 openai_compat / anthropic 作为主后端或备用。
    """

    backends: list[BackendConfig] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.backends

    @classmethod
    def default(cls) -> "LLMRouterConfig":
        """零额外配置的默认：claude CLI。"""
        return cls(backends=[BackendConfig(kind="harness_cli", harness="claude")])

    def build(self, *, secrets: SecretResolver | None = None) -> "LLMRouter":
        if not self.backends:
            raise ValueError("未配置任何 LLM 后端：请显式声明，或使用 LLMRouterConfig.default()")
        backends = [cfg.build(secrets=secrets) for cfg in self.backends]
        return LLMRouter(backends)


class LLMRouter:
    """有序后端链。第 0 个是主后端，其余按顺序降级。"""

    def __init__(
        self,
        backends: Sequence[LLMBackend],
        *,
        name: str = "llm-router",
        on_fallback: FallbackHook | None = None,
    ) -> None:
        if not backends:
            raise ValueError("LLMRouter 至少需要一个后端")
        self._backends: list[LLMBackend] = list(backends)
        self.name = name
        self.on_fallback = on_fallback

    # ---- 元信息 ----

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]

    def supports_tools(self) -> bool:
        """主后端（链上第一个）是否支持工具调用。降级到不支持的备用后端时，
        工具调用会如实落空——调用方据此做显式降级提示。"""
        return bool(getattr(self._backends[0], "supports_tools", False))

    def describe(self) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for i, b in enumerate(self._backends):
            detail = b.describe() if hasattr(b, "describe") else {"name": b.name}
            out.append({"index": i, "primary": i == 0, **detail})
        return {"name": self.name, "backends": out}

    def _order(self, prefer: str | None) -> list[LLMBackend]:
        """``prefer`` 指定的后端提到最前，其余保持原相对顺序（不做 A↔B 回访）。"""
        if not prefer:
            return list(self._backends)
        preferred = [b for b in self._backends if b.name == prefer]
        rest = [b for b in self._backends if b.name != prefer]
        return preferred + rest

    # ---- 补全（带可见降级） ----

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        prefer: str | None = None,
        allow_fallback: bool = True,
        tools: Sequence[LLMToolSpec] | None = None,
    ) -> LLMResponse:
        """依次尝试后端。返回的 ``backend`` 一定是实际执行者。

        ``allow_fallback=False`` 时只尝试首选后端（用户显式要求「就用这个」时，
        失败要直接报出，而不是悄悄换一个）。

        ``tools`` 只在非 None 时才透传给后端：不带工具的既有调用方与
        不感知 tools 的后端实现（如测试替身）都零改动。
        """
        order = self._order(prefer)
        if not allow_fallback:
            order = order[:1]
        primary_name = order[0].name
        attempts: list[str] = []
        last_error: LLMError | None = None

        for index, backend in enumerate(order):
            try:
                kwargs: dict[str, Any] = {
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "timeout": timeout,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                resp = await backend.complete(messages, **kwargs)
            except LLMError as exc:
                attempts.append(exc.as_reason())
                last_error = exc
                continue
            except Exception as exc:  # 未预期异常同样触发降级，但如实标注为 unexpected
                attempts.append(f"{backend.name}:unexpected")
                last_error = LLMError(
                    f"{type(exc).__name__}: {exc}", kind="unexpected"
                )
                last_error.backend = backend.name
                continue

            if index == 0 and not attempts:
                # 正常路径：只保证 backend 字段如实反映实际执行者。
                if resp.backend != backend.name:
                    return resp.model_copy(update={"backend": backend.name})
                return resp

            # 降级路径：显式标注来源与原因（绝不静默）。
            degraded = [redact_text(a) for a in attempts]
            result = resp.model_copy(
                update={
                    "backend": backend.name,
                    "fallback_from": primary_name,
                    "degraded_reasons": degraded,
                }
            )
            if self.on_fallback is not None:
                self.on_fallback(list(degraded), backend.name)
            return result

        if not allow_fallback and last_error is not None:
            # 用户显式要求「就用这个后端」：失败时抛出**原始分类**的错误，而不是
            # 包一层 all_failed——调用方需要按 D-05 的分类决定重试还是换配置。
            raise last_error
        raise LLMAllBackendsFailed(attempts, last_error)

    # ---- 流式补全（带可见降级） ----

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        prefer: str | None = None,
        allow_fallback: bool = True,
        tools: Sequence[LLMToolSpec] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """流式版本的有序降级。语义与 :meth:`complete` 一致，除了一条流式特有的
        硬约束：

        **只有第一个 chunk 到达之前的失败才允许换后端。** 第一个 chunk 一旦产出，
        内容可能已经推给了用户界面——此刻再失败绝不能换后端把回答重发一遍
        （用户会看到两份开头），只能把错误原样抛出。

        ``tools`` 只在非 None 时才透传给后端（与 :meth:`complete` 同一口径）。
        """
        order = self._order(prefer)
        if not allow_fallback:
            order = order[:1]
        primary_name = order[0].name
        attempts: list[str] = []
        last_error: LLMError | None = None

        for backend in order:
            kwargs: dict[str, Any] = {
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeout": timeout,
            }
            if tools is not None:
                kwargs["tools"] = tools
            agen = backend.stream(messages, **kwargs)
            try:
                first = await agen.__anext__()
            except StopAsyncIteration:
                # 空流 = 坏响应：什么都没产出，等价于取不到内容，允许换后端。
                attempts.append(f"{backend.name}:bad_response")
                last_error = LLMResponseError(
                    f"流式补全没有产出任何 chunk（backend={backend.name}）",
                    backend=backend.name,
                    kind="bad_payload",
                )
                continue
            except LLMError as exc:
                attempts.append(exc.as_reason())
                last_error = exc
                continue
            except Exception as exc:  # 未预期异常同样触发降级，但如实标注为 unexpected
                attempts.append(f"{backend.name}:unexpected")
                last_error = LLMError(f"{type(exc).__name__}: {exc}", kind="unexpected")
                last_error.backend = backend.name
                continue

            degraded = [redact_text(a) for a in attempts]
            fell_back = bool(attempts)
            if fell_back and self.on_fallback is not None:
                self.on_fallback(list(degraded), backend.name)

            def stamp(chunk: LLMChunk) -> LLMChunk:
                """降级标注只写在终帧上（降级可见性同 LLMResponse 的约定）。"""
                if not fell_back or not chunk.final:
                    return chunk
                return chunk.model_copy(
                    update={
                        "backend": chunk.backend or backend.name,
                        "fallback_from": primary_name,
                        "degraded_reasons": degraded + list(chunk.degraded_reasons),
                    }
                )

            yield stamp(first)
            async for chunk in agen:
                # 从这里开始的任何异常都原样向上抛：内容已发出，不能换后端重来。
                yield stamp(chunk)
            return

        if not allow_fallback and last_error is not None:
            raise last_error
        raise LLMAllBackendsFailed(attempts, last_error)

    # ---- 健康检查 ----

    async def health(self) -> tuple[bool, str | None]:
        """任一后端可用即算可用；全不可用时把各后端的原因拼出来。"""
        reasons: list[str] = []
        for backend in self._backends:
            try:
                ok, reason = await backend.health()
            except Exception as exc:  # 健康检查自身出错不能掩盖其它后端
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            if ok:
                return True, f"{backend.name}: {reason or '可用'}"
            reasons.append(f"{backend.name}: {reason or '不可用'}")
        return False, "；".join(reasons) if reasons else "未配置任何后端"
