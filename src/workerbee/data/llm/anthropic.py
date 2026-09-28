"""Anthropic Messages 后端：``POST {base_url}/v1/messages``。

协议差异（与 openai_compat 并列，不是能力评级）：

- 认证头是 ``x-api-key``（不是 Bearer），外加必需的 ``anthropic-version``。
- 顶层 **没有** ``messages`` 里的 system 角色：system 走顶层 ``system`` 字段，
  ``messages`` 只接受 user/assistant。这里由框架做这个切分，调用方不必感知。
- ``max_tokens`` 是**必填**字段。未传时取本后端的 ``default_max_tokens``，
  避免把「没配」变成对端的 400 配置错误。
- 响应正文是 ``content`` 数组，需要把 ``type == "text"`` 的块拼起来。
- ``stop_reason == "max_tokens"`` 表示输出被截断——**判失败**，不返回半截文本
  （DATA-03：不允许以静默省略换取「成功」）。

凭据与错误映射同 :mod:`openai_compat`：429/5xx 可重试，401/403 为认证失败，
其余 4xx 为配置错误；异常消息里的响应正文先过 ``redact_text``。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx

from ..redact import Secret, redact_text
from .backend import (
    LLMBackendError,
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMResponse,
    LLMResponseError,
    LLMMessage,
    LLMTimeoutError,
    SecretResolver,
    extract_credentials,
    non_system_messages,
    resolve_credentials,
    system_text,
    usage_from_counts,
)

__all__ = ["AnthropicBackend", "ANTHROPIC_VERSION", "DEFAULT_MAX_TOKENS", "DEFAULT_TIMEOUT_S"]

#: Messages API 的版本头。升级时需要同步复核请求／响应字段（HAR-03 的兼容定位）。
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_TIMEOUT_S = 60.0

#: ``max_tokens`` 缺失时的默认值。这是**本后端的兜底值，未经实测标定**：
#: 它只影响单次补全的长度上限，不影响上下文窗口（窗口是模型属性，由候选配置）。
DEFAULT_MAX_TOKENS = 4096

_ERROR_SNIPPET_CHARS = 400


class AnthropicBackend:
    """``/v1/messages`` 形态的后端。"""

    def __init__(
        self,
        *,
        name: str = "anthropic",
        model: str,
        base_url: str | None = None,
        api_key: str | Secret | None = None,
        secret_locator: str | None = None,
        secrets: SecretResolver | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        version: str = ANTHROPIC_VERSION,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.secret_locator = secret_locator
        self.secrets = secrets
        self.timeout = float(timeout)
        self.default_max_tokens = int(default_max_tokens)
        self.version = version
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})

        if isinstance(api_key, Secret):
            self._api_key: Secret | None = api_key
        elif api_key:
            self._api_key = Secret(str(api_key), label=secret_locator or "inline")
        else:
            self._api_key = None

        self._client = client
        self._owns_client = client is None

    # ---- 生命周期 ----

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self._client

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "anthropic",
            "model": self.model,
            "base_url": self.base_url or "<未配置>",
            "secret_locator": self.secret_locator or "<inline/未配置>",
            "anthropic_version": self.version,
        }

    # ---- 凭据 ----

    async def _credentials(self) -> tuple[str, Secret]:
        base_url = self.base_url
        key = self._api_key

        if key is None or not base_url:
            fields = await resolve_credentials(self.secrets, self.secret_locator)
            from_store_url, from_store_key = extract_credentials(
                fields,
                locator=self.secret_locator,
                require_key=key is None,
                backend=self.name,
            )
            base_url = base_url or (from_store_url or "")
            key = key or from_store_key

        if key is None:
            raise LLMConfigError(
                f"缺少 api_key（backend={self.name}，"
                f"locator={self.secret_locator or '<未配置>'}）",
                backend=self.name,
                kind="no_credential",
            )
        if not base_url:
            raise LLMConfigError(
                f"缺少 base_url（backend={self.name}）", backend=self.name, kind="no_base_url"
            )
        return base_url.rstrip("/"), key

    # ---- 请求构造（纯函数，便于断言而不发网络请求） ----

    def build_request(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        """构造请求体。system 与 user/assistant 在此完成分流。"""
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens) if max_tokens is not None else self.default_max_tokens,
            "messages": [
                {"role": m.role, "content": m.content} for m in non_system_messages(messages)
            ],
        }
        system = system_text(messages)
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = float(temperature)
        body.update(self.extra_body)
        return body

    def build_headers(self, key: Secret) -> dict[str, str]:
        """认证头。 ``x-api-key`` 只在此处 reveal()。"""
        return {
            "x-api-key": key.reveal(),
            "anthropic-version": self.version,
            "content-type": "application/json",
            **self.extra_headers,
        }

    # ---- 补全 ----

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        base_url, key = await self._credentials()
        url = f"{base_url}/v1/messages"
        body = self.build_request(messages, max_tokens=max_tokens, temperature=temperature)

        client = self._ensure_client()
        try:
            resp = await client.post(
                url,
                json=body,
                headers=self.build_headers(key),
                timeout=float(timeout or self.timeout),
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"请求 {url} 超时（backend={self.name}）", backend=self.name
            ) from exc
        except httpx.TransportError as exc:
            raise LLMBackendError(
                f"请求 {url} 失败：{type(exc).__name__}（backend={self.name}）",
                backend=self.name,
                kind="network",
            ) from exc

        self._raise_for_status(resp, target=url)
        payload = self._decode_json(resp, target=url)
        return self._to_response(payload)

    def _raise_for_status(self, resp: httpx.Response, *, target: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        snippet = redact_text(
            resp.text[:_ERROR_SNIPPET_CHARS], [self._api_key] if self._api_key else None
        )
        # Anthropic 的错误分类在正文里（type 字段），比 HTTP 码更细。
        error_type = ""
        try:
            parsed = resp.json()
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error_type = str(parsed["error"].get("type") or "")
        except ValueError:
            error_type = ""

        message = (
            f"HTTP {status}（backend={self.name} model={self.model} "
            f"error_type={error_type or 'unknown'}）：{snippet}"
        )
        if error_type in ("overloaded_error", "rate_limit_error") or status == 429:
            raise LLMBackendError(message, backend=self.name, kind="rate_limit", retryable=True)
        if status >= 500:
            raise LLMBackendError(message, backend=self.name, kind="server_error", retryable=True)
        if error_type == "authentication_error" or status in (401, 403):
            raise LLMAuthError(message, backend=self.name, kind="auth")
        if error_type == "not_found_error" or status == 404:
            raise LLMConfigError(message, backend=self.name, kind="not_found")
        raise LLMConfigError(message, backend=self.name, kind="bad_request")

    def _decode_json(self, resp: httpx.Response, *, target: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            snippet = redact_text(resp.text[:200], [self._api_key] if self._api_key else None)
            raise LLMResponseError(
                f"响应不是合法 JSON（backend={self.name} url={target}）："
                f"{type(exc).__name__}；正文片段：{snippet}",
                backend=self.name,
                kind="bad_json",
            ) from exc
        if not isinstance(payload, dict):
            raise LLMResponseError(
                f"响应顶层不是对象（backend={self.name}）", backend=self.name, kind="bad_payload"
            )
        return payload

    def _to_response(self, payload: dict[str, Any]) -> LLMResponse:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise LLMResponseError(
                f"响应缺少 content 数组（backend={self.name}）",
                backend=self.name,
                kind="bad_payload",
            )
        texts = [
            b.get("text")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if not texts:
            types = [b.get("type") for b in blocks if isinstance(b, dict)]
            raise LLMResponseError(
                f"响应中没有 text 内容块（块类型：{types}，backend={self.name}）",
                backend=self.name,
                kind="bad_payload",
            )
        text = "".join(texts)

        stop_reason = payload.get("stop_reason")
        if stop_reason == "max_tokens":
            raise LLMResponseError(
                f"补全被 max_tokens 截断（stop_reason=max_tokens，backend={self.name}）："
                f"框架拒绝把截断文本当作完整结果（DATA-03）",
                backend=self.name,
                kind="output_truncated",
            )

        usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

        def _int(key: str) -> int | None:
            v = usage_raw.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        usage = usage_from_counts(_int("input_tokens"), _int("output_tokens"))
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.model
        return LLMResponse(text=text, usage=usage, model=model or self.model, backend=self.name)

    # ---- 健康检查 ----

    async def health(self) -> tuple[bool, str | None]:
        try:
            base_url, _key = await self._credentials()
        except LLMError as exc:
            return False, str(exc)
        return True, f"配置就绪（{base_url}，连通性未验证）"
