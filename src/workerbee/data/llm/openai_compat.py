"""OpenAI 兼容后端：``POST {base_url}/chat/completions`` + Bearer 认证。

适用于一切「OpenAI 兼容」的网关（自建代理、厂商兼容层等）。与 anthropic 后端的
分工是**协议差异**，不是能力评级（清单 §1.3：系统不评价模型强弱）。

凭据（AUTH-02）：
- 密钥来自 Secret Store（``secret_locator``）或构造参数（主要供测试与嵌入使用）。
  构造参数形式下调用方必须自己保证它不进日志。
- 凭据只出现在 ``Authorization`` 头里；异常消息里的响应正文先过 ``redact_text``。
- ``base_url`` 属于非敏感元数据，可以来自 ``CredentialRef.base_url``。

错误映射对齐 D-05：429 与 5xx 可重试；401/403 是认证问题（不可重试，需重新绑定
凭据）；其余 4xx 是配置错误（不可重试，直接切候选或失败）。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx

from ..redact import Secret, redact_text
from .backend import (
    LLMBackendError,
    LLMAuthError,
    LLMChunk,
    LLMConfigError,
    LLMError,
    LLMResponse,
    LLMResponseError,
    LLMMessage,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolSpec,
    SecretResolver,
    extract_credentials,
    resolve_credentials,
    usage_from_counts,
)

__all__ = ["OpenAICompatBackend", "DEFAULT_TIMEOUT_S"]

DEFAULT_TIMEOUT_S = 60.0

#: 错误正文进入异常消息前截断的长度（避免把整页 HTML 打进日志）。
_ERROR_SNIPPET_CHARS = 400


def _message_wire(m: LLMMessage) -> dict[str, Any]:
    """协议层消息 → OpenAI 线格式。None 字段一律不落，无工具的历史消息形状不变。"""
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in m.tool_calls
        ]
    if m.role == "tool":
        out["tool_call_id"] = m.tool_call_id
        if m.name:
            out["name"] = m.name
    return out


def _parse_tool_calls(raw: Any, *, backend: str) -> list[LLMToolCall]:
    """把线格式 tool_calls 解析成协议层对象。arguments 不是合法 JSON 即坏响应。"""
    if not isinstance(raw, list):
        return []
    calls: list[LLMToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = fn.get("name")
        call_id = item.get("id")
        if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
            raise LLMResponseError(
                f"工具调用缺少 id 或 name（backend={backend}）",
                backend=backend,
                kind="bad_payload",
            )
        args_raw = fn.get("arguments")
        if isinstance(args_raw, dict):
            arguments = args_raw
        elif isinstance(args_raw, str):
            try:
                arguments = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise LLMResponseError(
                    f"工具调用 {name} 的参数不是合法 JSON（backend={backend}）",
                    backend=backend,
                    kind="bad_payload",
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMResponseError(
                    f"工具调用 {name} 的参数不是 JSON 对象（backend={backend}）",
                    backend=backend,
                    kind="bad_payload",
                )
        else:
            arguments = {}
        calls.append(LLMToolCall(id=call_id, name=name, arguments=arguments))
    return calls


class OpenAICompatBackend:
    """``/chat/completions`` 形态的后端。"""

    #: 本后端支持 OpenAI tools/function-calling 协议。
    supports_tools = True

    def __init__(
        self,
        *,
        name: str = "openai-compat",
        model: str,
        base_url: str | None = None,
        api_key: str | Secret | None = None,
        secret_locator: str | None = None,
        secrets: SecretResolver | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        default_max_tokens: int | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.secret_locator = secret_locator
        self.secrets = secrets
        self.timeout = float(timeout)
        self.default_max_tokens = default_max_tokens
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
        """只关闭自己创建的 client。外部注入的 client 由注入方管理（测试友好）。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self._client

    def describe(self) -> dict[str, Any]:
        """可写日志的描述。凭据只以 locator 出现，不含本体。"""
        return {
            "name": self.name,
            "kind": "openai_compat",
            "model": self.model,
            "base_url": self.base_url or "<未配置>",
            "secret_locator": self.secret_locator or "<inline/未配置>",
        }

    # ---- 凭据 ----

    async def _credentials(self) -> tuple[str, Secret]:
        """解析 ``(base_url, key)``。优先使用构造参数，其次 Secret Store。"""
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

    # ---- 补全 ----

    def _build_body(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool = False,
        tools: Sequence[LLMToolSpec] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_wire(m) for m in messages],
        }
        effective_max = max_tokens if max_tokens is not None else self.default_max_tokens
        if effective_max is not None:
            body["max_tokens"] = int(effective_max)
        if temperature is not None:
            body["temperature"] = float(temperature)
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if stream:
            body["stream"] = True
            # 流式下 usage 默认不回传：显式要求带，否则终帧只能记「未知」。
            body["stream_options"] = {"include_usage": True}
        body.update(self.extra_body)
        return body

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        tools: Sequence[LLMToolSpec] | None = None,
    ) -> LLMResponse:
        base_url, key = await self._credentials()
        url = f"{base_url}/chat/completions"

        body = self._build_body(
            messages, max_tokens=max_tokens, temperature=temperature, tools=tools
        )

        headers = {
            "Authorization": f"Bearer {key.reveal()}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        client = self._ensure_client()
        try:
            resp = await client.post(
                url, json=body, headers=headers, timeout=float(timeout or self.timeout)
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

    # ---- 流式补全（SSE） ----

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        tools: Sequence[LLMToolSpec] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """SSE 流式补全。逐 delta yield，恰以一个 ``final=True`` 的终帧结束。

        增量映射：``delta.content`` → text chunk；``delta.reasoning_content``
        → reasoning chunk（DeepSeek 等兼容服务用这个字段名回传思维链）；
        ``delta.tool_calls`` 按 ``index`` 累积 id/name/arguments 片段，
        在收尾时按序产出 ``kind="tool_call"`` 的完整调用 chunk。
        usage 只在 ``stream_options.include_usage`` 换来的最后一个空 choices
        数据帧里出现，由终帧携带；对端不给时终帧 ``usage=None``（未知 ≠ 0）。

        第一个 chunk 到达之前的失败（连不上、HTTP 错误、首帧解析失败）抛
        :class:`LLMError` 子类，router 据此换后端；开始产出之后再失败原样抛出。
        """
        base_url, key = await self._credentials()
        url = f"{base_url}/chat/completions"
        body = self._build_body(
            messages, max_tokens=max_tokens, temperature=temperature, stream=True,
            tools=tools,
        )
        headers = {
            "Authorization": f"Bearer {key.reveal()}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        client = self._ensure_client()
        try:
            async with client.stream(
                "POST", url, json=body, headers=headers,
                timeout=float(timeout or self.timeout),
            ) as resp:
                if resp.status_code >= 400:
                    # 流式响应的错误体要先读出来才能复用既有的分类逻辑。
                    await resp.aread()
                    self._raise_for_status(resp, target=url)
                async for chunk in self._consume_sse(resp):
                    yield chunk
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

    async def _consume_sse(self, resp: httpx.Response) -> AsyncIterator[LLMChunk]:
        """解析 SSE 数据流：``data: <json>`` 逐行来，``data: [DONE]`` 终止。"""
        model: str | None = None
        usage = None
        truncated = False
        # index → [id, name, arguments 片段]：工具调用的增量按 index 累积。
        pending_calls: dict[int, list[Any]] = {}
        async for line in resp.aiter_lines():
            if not line or line.startswith(":"):
                continue  # 空行与 SSE 注释（keep-alive）
            if not line.startswith("data:"):
                continue  # event:/id:/retry: 等行对本协议没有意义
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LLMResponseError(
                    f"流式数据帧不是合法 JSON（backend={self.name}）：{data[:200]}",
                    backend=self.name,
                    kind="bad_json",
                ) from exc
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("model"), str):
                model = payload["model"]

            usage_raw = payload.get("usage")
            if isinstance(usage_raw, dict):
                # include_usage 换来的终帧：choices 为空、只有 usage。
                def _int(key: str) -> int | None:
                    v = usage_raw.get(key)
                    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

                usage = usage_from_counts(_int("prompt_tokens"), _int("completion_tokens"))

            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0] if isinstance(choices[0], dict) else {}
            delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield LLMChunk(kind="reasoning", text=reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield LLMChunk(kind="text", text=content)
            tool_deltas = delta.get("tool_calls")
            if isinstance(tool_deltas, list):
                for item in tool_deltas:
                    if not isinstance(item, dict):
                        continue
                    index = item.get("index")
                    if not isinstance(index, int):
                        index = len(pending_calls)
                    slot = pending_calls.setdefault(index, ["", "", []])
                    if isinstance(item.get("id"), str):
                        slot[0] = item["id"]
                    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                    if isinstance(fn.get("name"), str):
                        slot[1] = fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        slot[2].append(fn["arguments"])
            if first.get("finish_reason") == "length":
                # 与 complete 同一口径：截断即失败，不把半截文本当完整回复。
                truncated = True

        if truncated:
            raise LLMResponseError(
                f"补全被 max_tokens 截断（finish_reason=length，backend={self.name}）",
                backend=self.name,
                kind="output_truncated",
            )
        for index in sorted(pending_calls):
            call_id, name, fragments = pending_calls[index]
            for call in _parse_tool_calls(
                [{"id": call_id, "function": {"name": name, "arguments": "".join(fragments)}}],
                backend=self.name,
            ):
                yield LLMChunk(kind="tool_call", tool_call=call)
        yield LLMChunk(
            kind="text", final=True, usage=usage, model=model or self.model,
            backend=self.name,
        )

    def _raise_for_status(self, resp: httpx.Response, *, target: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        snippet = redact_text(resp.text[:_ERROR_SNIPPET_CHARS], [self._api_key] if self._api_key else None)
        message = f"HTTP {status}（backend={self.name} model={self.model} url={target}）：{snippet}"
        if status == 429:
            raise LLMBackendError(message, backend=self.name, kind="rate_limit", retryable=True)
        if status >= 500:
            raise LLMBackendError(message, backend=self.name, kind="server_error", retryable=True)
        if status in (401, 403):
            raise LLMAuthError(message, backend=self.name, kind="auth")
        if status == 404:
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
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                f"响应缺少 choices（backend={self.name}）", backend=self.name, kind="bad_payload"
            )
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        tool_calls = _parse_tool_calls(message.get("tool_calls"), backend=self.name)
        text = message.get("content")
        if not isinstance(text, str):
            # 兼容少数网关把正文放在 text 字段的形态；都不满足即失败。
            text = first.get("text") if isinstance(first.get("text"), str) else None
        if not isinstance(text, str):
            if tool_calls:
                # 纯工具调用轮：content 为 null 是协议的正常形态，不是缺内容。
                text = ""
            else:
                raise LLMResponseError(
                    f"响应中取不到文本内容（backend={self.name}）",
                    backend=self.name,
                    kind="bad_payload",
                )

        finish_reason = first.get("finish_reason")
        if finish_reason == "length":
            # 截断即失败：半截摘要冒充完整摘要正是 DATA-03 要禁止的静默省略。
            raise LLMResponseError(
                f"补全被 max_tokens 截断（finish_reason=length，backend={self.name}）",
                backend=self.name,
                kind="output_truncated",
            )

        usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

        def _int(key: str) -> int | None:
            v = usage_raw.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        usage = usage_from_counts(_int("prompt_tokens"), _int("completion_tokens"))
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.model
        return LLMResponse(
            text=text, usage=usage, model=model or self.model, backend=self.name,
            tool_calls=tool_calls,
        )

    # ---- 健康检查 ----

    async def health(self) -> tuple[bool, str | None]:
        """只做配置完备性检查（够不够发一次请求），不发真实补全请求。

        真实连通性只有 ``complete`` 才知道；用健康检查偷偷发请求会消耗配额，
        也会在用户没打算跑任务时打扰对端。
        """
        try:
            base_url, _key = await self._credentials()
        except LLMError as exc:
            return False, str(exc)
        return True, f"配置就绪（{base_url}，连通性未验证）"
