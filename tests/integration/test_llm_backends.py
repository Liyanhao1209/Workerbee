"""LLM 后端的请求构造、错误分类与凭据纪律（架构设计 v0.02 §7.2、§9.1、D-05、DATA-03）。

测试纪律（本文件全部用例都遵守）：

- **不发真实网络请求**：``openai_compat`` 与 ``anthropic`` 用 ``httpx.MockTransport``
  拦截，断言 URL／header／body 与错误映射；
- **不跑真实 harness**：``harness_cli`` 用写进 ``tmp_path`` 的 ``#!/bin/sh`` 假脚本，
  脚本只做「按需输出 + 睡觉 + 记录参数」三件事；
- **凭据不出现在任何可见文本里**：异常消息、``describe()``、``repr`` 都要验一遍。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from workerbee.data.llm import (
    AnthropicBackend,
    BackendConfig,
    HarnessCLIBackend,
    LLMAllBackendsFailed,
    LLMAuthError,
    LLMBackendError,
    LLMChunk,
    LLMConfigError,
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMResponseError,
    LLMRouter,
    LLMRouterConfig,
    LLMTimeoutError,
    LLMUnavailableError,
    OpenAICompatBackend,
    resolve_credentials,
)
from workerbee.data.redact import REDACTED, Secret, looks_secret_like, redact_text, scrub
from workerbee.data.summarizer import Summarizer

pytestmark = pytest.mark.integration

KEY = "sk-live-abcdefghijklmnop0123"


# ---------------------------------------------------------------------------
# 假传输层
# ---------------------------------------------------------------------------


class Recorder:
    """记录 MockTransport 收到的请求，并按时序返回预置响应。"""

    def __init__(self, *responses: httpx.Response):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"未预置响应，却收到了第 {len(self.requests)} 个请求")
        return self._responses.pop(0)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def json_body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content.decode("utf-8"))


def make_client(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=recorder.transport(), timeout=5.0)


def openai_ok(text: str = "摘要正文", **usage) -> httpx.Response:
    payload: dict = {
        "model": "gpt-test-1",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    if usage:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def anthropic_ok(text: str = "摘要正文", **usage) -> httpx.Response:
    payload: dict = {
        "model": "claude-test-1",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }
    if usage:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


MSGS = [
    LLMMessage(role="system", content="你是摘要器"),
    LLMMessage(role="user", content="把这段材料摘一下"),
]


# ===========================================================================
# 流式补全（stream）：SSE 解析、reasoning 透传、降级边界
#
# 纪律同非流式：不发真实网络请求（MockTransport + 构造好的 SSE 字节流），
# 断言**逐 chunk 到达的内容与顺序**——只断言「最终拼出来对」不算数。
# ===========================================================================


class _SSEStream(httpx.AsyncByteStream):
    """把预置字节块按序吐给响应体（MockTransport 的流式响应夹具）。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def sse_response(*data_lines: str) -> httpx.Response:
    """把若干 SSE data 载荷拼成一个 text/event-stream 响应。"""
    body = "".join(f"data: {line}\n\n" for line in data_lines)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_SSEStream([body.encode("utf-8")]),
    )


async def collect(agen) -> list:
    return [chunk async for chunk in agen]


def openai_sse_delta(content=None, reasoning=None, finish=None, model="gpt-test-1"):
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return json.dumps(
        {"model": model, "choices": [{"delta": delta, "finish_reason": finish}]},
        ensure_ascii=False,
    )


async def test_openai_stream_request_body_and_chunk_order():
    rec = Recorder(
        sse_response(
            openai_sse_delta(reasoning="先想一"),
            openai_sse_delta(reasoning="想二"),
            openai_sse_delta(content="答"),
            openai_sse_delta(content="案", finish="stop"),
            json.dumps({"model": "gpt-test-1", "choices": [],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4}}),
            "[DONE]",
        )
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="gpt-test-1", base_url="https://x/v1", api_key=KEY, client=client
        )
        chunks = await collect(backend.stream(MSGS, max_tokens=64, temperature=0.1))

    body = rec.json_body()
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 64

    # 逐 chunk 的内容与顺序：reasoning 在前、正文在后、终帧收尾
    assert [(c.kind, c.text) for c in chunks[:-1]] == [
        ("reasoning", "先想一"),
        ("reasoning", "想二"),
        ("text", "答"),
        ("text", "案"),
    ]
    final = chunks[-1]
    assert final.final is True
    assert final.backend == "openai-compat"
    assert final.model == "gpt-test-1"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (10, 4)
    assert final.streamed is True
    assert final.fallback_from is None


async def test_openai_stream_without_usage_frame_reports_unknown():
    """对端不回 usage 帧时，终帧 usage 是 None（未知），不是 0。"""
    rec = Recorder(
        sse_response(openai_sse_delta(content="好", finish="stop", model=None), "[DONE]")
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        chunks = await collect(backend.stream(MSGS))

    assert chunks[-1].final is True
    assert chunks[-1].usage is None
    assert chunks[-1].model == "m"  # 帧里没带 model 时回退到配置值


async def test_openai_stream_http_error_before_first_chunk_is_classified():
    rec = Recorder(httpx.Response(429, json={"error": {"message": "slow down"}}))
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(LLMBackendError) as info:
            await collect(backend.stream(MSGS))

    assert info.value.kind == "rate_limit"


async def test_openai_stream_truncated_output_is_an_error():
    rec = Recorder(
        sse_response(openai_sse_delta(content="半截", finish="length"), "[DONE]")
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError) as info:
            await collect(backend.stream(MSGS))

    assert info.value.kind == "output_truncated"


def anthropic_sse(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def test_anthropic_stream_thinking_text_usage_and_no_thinking_param():
    rec = Recorder(
        sse_response(
            anthropic_sse({"type": "message_start", "message": {
                "model": "claude-test-1", "usage": {"input_tokens": 21}}}),
            anthropic_sse({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "thinking", "thinking": ""}}),
            anthropic_sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": "推一"}}),
            anthropic_sse({"type": "content_block_stop", "index": 0}),
            anthropic_sse({"type": "content_block_start", "index": 1,
                           "content_block": {"type": "text", "text": ""}}),
            anthropic_sse({"type": "content_block_delta", "index": 1,
                           "delta": {"type": "text_delta", "text": "正"}}),
            anthropic_sse({"type": "content_block_delta", "index": 1,
                           "delta": {"type": "text_delta", "text": "文"}}),
            anthropic_sse({"type": "message_delta",
                           "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 9}}),
            anthropic_sse({"type": "message_stop"}),
        )
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="claude-test-1", base_url="https://api.anthropic.com", api_key=KEY,
            client=client,
        )
        chunks = await collect(backend.stream(MSGS))

    body = rec.json_body()
    assert body["stream"] is True
    # 不主动开启 extended thinking：非思考模型收到 thinking 参数会报错；
    # 这里只透传服务端自然发出的 thinking。
    assert "thinking" not in body

    assert [(c.kind, c.text) for c in chunks[:-1]] == [
        ("reasoning", "推一"),
        ("text", "正"),
        ("text", "文"),
    ]
    final = chunks[-1]
    assert final.final is True
    assert final.backend == "anthropic"
    assert final.model == "claude-test-1"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (21, 9)


async def test_anthropic_stream_truncated_is_a_failure():
    rec = Recorder(
        sse_response(
            anthropic_sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": "半截"}}),
            anthropic_sse({"type": "message_delta",
                           "delta": {"stop_reason": "max_tokens"},
                           "usage": {"output_tokens": 5}}),
            anthropic_sse({"type": "message_stop"}),
        )
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError) as info:
            await collect(backend.stream(MSGS))

    assert info.value.kind == "output_truncated"


async def test_anthropic_stream_http_error_before_first_chunk_is_classified():
    rec = Recorder(
        httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        with pytest.raises(LLMAuthError):
            await collect(backend.stream(MSGS))


async def test_harness_cli_stream_is_an_honest_one_shot(tmp_path):
    """CLI 后端不支持真流式：一次性给全，且终帧如实标 streamed=False。"""
    payload = json.dumps({"type": "result", "subtype": "success", "result": "完整回答"})
    binary = write_script(tmp_path, f"cat <<'EOF'\n{payload}\nEOF\n")
    backend = HarnessCLIBackend(harness="claude", binary=binary)

    chunks = await collect(backend.stream(MSGS))

    assert len(chunks) == 1
    assert chunks[0].final is True
    assert chunks[0].text == "完整回答"
    assert chunks[0].streamed is False
    assert chunks[0].backend == "harness-cli:claude"


class StreamFailBackend:
    """第一个 chunk 之前就失败的流式假后端（允许降级的形态）。"""

    def __init__(self, name: str, *, error: Exception):
        self.name = name
        self._error = error
        self.started = 0

    async def stream(self, messages, *, max_tokens=None, temperature=None, timeout=None):
        self.started += 1
        raise self._error
        yield  # pragma: no cover - 让本方法成为异步生成器

    async def complete(self, messages, **kwargs):
        raise self._error

    async def health(self):
        return True, None


class StreamScriptBackend:
    """按脚本逐 chunk 产出（可指定中途失败）的流式假后端。"""

    def __init__(
        self, name: str, chunks: list[str], *, fail_after: int | None = None
    ):
        self.name = name
        self._chunks = chunks
        self._fail_after = fail_after
        self.started = 0

    async def stream(self, messages, *, max_tokens=None, temperature=None, timeout=None):
        self.started += 1
        for index, text in enumerate(self._chunks):
            yield LLMChunk(kind="text", text=text)
            if self._fail_after is not None and index + 1 == self._fail_after:
                raise LLMBackendError("连接中断", backend=self.name, kind="network")
        yield LLMChunk(kind="text", final=True, model="fake-model", backend=self.name)

    async def complete(self, messages, **kwargs):  # pragma: no cover - 本测试只用 stream
        raise AssertionError("不该被调用")

    async def health(self):
        return True, None


async def test_router_stream_falls_back_only_before_the_first_chunk():
    """第一个 chunk 之前失败：换后端，终帧带降级标注。"""
    primary = StreamFailBackend(
        "primary", error=LLMTimeoutError("超时", backend="primary")
    )
    backup = StreamScriptBackend("backup", ["备", "用"])

    chunks = await collect(LLMRouter([primary, backup]).stream(MSGS))

    assert [(c.kind, c.text) for c in chunks[:-1]] == [("text", "备"), ("text", "用")]
    final = chunks[-1]
    assert final.backend == "backup"
    assert final.fallback_from == "primary"
    assert final.degraded_reasons == ["primary:timeout"]
    assert primary.started == 1 and backup.started == 1


async def test_router_stream_never_falls_back_after_the_first_chunk():
    """第一个 chunk 已发出后失败：错误原样抛出，**不**换后端重发。"""
    primary = StreamScriptBackend("primary", ["已发出的开头"], fail_after=1)
    backup = StreamScriptBackend("backup", ["备用"])

    with pytest.raises(LLMBackendError, match="连接中断"):
        await collect(LLMRouter([primary, backup]).stream(MSGS))

    assert backup.started == 0  # 内容已发出，绝不能换后端重来


async def test_router_stream_all_backends_failed_carries_attempt_chain():
    router = LLMRouter([
        StreamFailBackend("a", error=LLMTimeoutError("超时", backend="a")),
        StreamFailBackend(
            "b", error=LLMBackendError("500", backend="b", kind="server_error")
        ),
    ])
    with pytest.raises(LLMAllBackendsFailed) as info:
        await collect(router.stream(MSGS))

    assert info.value.attempts == ["a:timeout", "b:server_error"]


async def test_router_stream_marks_pseudo_streaming_honestly(tmp_path):
    """harness_cli 兜底路径：路由透传终帧的 streamed=False，不包装成真流式。"""
    payload = json.dumps({"type": "result", "subtype": "success", "result": "好"})
    binary = write_script(tmp_path, f"cat <<'EOF'\n{payload}\nEOF\n")
    router = LLMRouter([HarnessCLIBackend(harness="claude", binary=binary)])

    chunks = await collect(router.stream(MSGS))

    assert chunks[-1].final is True
    assert chunks[-1].streamed is False
    assert chunks[-1].text == "好"


# ===========================================================================
# openai_compat：请求构造
# ===========================================================================


async def test_openai_request_url_headers_and_body():
    rec = Recorder(openai_ok())
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="gpt-test-1",
            base_url="https://api.example.com/v1/",
            api_key=KEY,
            client=client,
            default_max_tokens=256,
        )
        resp = await backend.complete(MSGS, temperature=0.2)

    req = rec.requests[0]
    assert str(req.url) == "https://api.example.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["content-type"] == "application/json"

    body = rec.json_body()
    assert body["model"] == "gpt-test-1"
    assert body["messages"] == [
        {"role": "system", "content": "你是摘要器"},
        {"role": "user", "content": "把这段材料摘一下"},
    ]
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.2

    assert resp.text == "摘要正文"
    assert resp.model == "gpt-test-1"
    assert resp.backend == "openai-compat"
    assert resp.fallback_from is None


async def test_openai_explicit_max_tokens_overrides_default():
    rec = Recorder(openai_ok())
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client,
            default_max_tokens=256,
        )
        await backend.complete(MSGS, max_tokens=7)

    assert rec.json_body()["max_tokens"] == 7


async def test_openai_usage_and_missing_usage():
    rec = Recorder(openai_ok(prompt_tokens=11, completion_tokens=7), openai_ok())
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with_usage = await backend.complete(MSGS)
        without_usage = await backend.complete(MSGS)

    assert with_usage.usage is not None
    assert (with_usage.usage.input_tokens, with_usage.usage.output_tokens) == (11, 7)
    # 未知 ≠ 零：给不出用量时是 None，不是 0（OBS-04）
    assert without_usage.usage is None


# ===========================================================================
# openai_compat：错误分类
# ===========================================================================


@pytest.mark.parametrize(
    "status,kind,retryable,exc_type",
    [
        (429, "rate_limit", True, LLMBackendError),
        (500, "server_error", True, LLMBackendError),
        (503, "server_error", True, LLMBackendError),
        (401, "auth", False, LLMAuthError),
        (403, "auth", False, LLMAuthError),
        (404, "not_found", False, LLMConfigError),
        (400, "bad_request", False, LLMConfigError),
    ],
)
async def test_openai_http_errors_are_classified(status, kind, retryable, exc_type):
    rec = Recorder(httpx.Response(status, json={"error": {"message": "boom"}}))
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(exc_type) as info:
            await backend.complete(MSGS)

    assert info.value.kind == kind
    assert info.value.retryable is retryable
    assert f"HTTP {status}" in str(info.value)


async def test_openai_error_body_never_leaks_the_key():
    """对端把 key 回显在错误正文里时，异常消息必须已经脱敏。"""
    rec = Recorder(
        httpx.Response(401, json={"error": {"message": f"invalid key: {KEY}"}})
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=Secret(KEY, label="llm/primary"),
            client=client,
        )
        with pytest.raises(LLMAuthError) as info:
            await backend.complete(MSGS)

    assert KEY not in str(info.value)
    assert "<redacted" in str(info.value)


async def test_openai_malformed_json_and_missing_choices_fail():
    rec = Recorder(
        httpx.Response(200, text="<html>不是 JSON</html>"),
        httpx.Response(200, json={"model": "m"}),
        httpx.Response(200, json={"choices": []}),
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError, match="不是合法 JSON"):
            await backend.complete(MSGS)
        with pytest.raises(LLMResponseError, match="缺少 choices"):
            await backend.complete(MSGS)
        with pytest.raises(LLMResponseError, match="缺少 choices"):
            await backend.complete(MSGS)


async def test_openai_truncated_output_is_an_error_not_a_result():
    """finish_reason=length：半截摘要冒充完整摘要正是 DATA-03 禁止的静默省略。"""
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "半截"}, "finish_reason": "length"}
                ]
            },
        )
    )
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError) as info:
            await backend.complete(MSGS)

    assert info.value.kind == "output_truncated"
    assert info.value.retryable is False


async def test_openai_transport_error_is_retryable_backend_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(boom), timeout=5.0
    ) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        with pytest.raises(LLMBackendError) as info:
            await backend.complete(MSGS)

    assert info.value.kind == "network"
    assert info.value.retryable is True


# ===========================================================================
# 凭据解析（Secret Store 只依赖签名）
# ===========================================================================


class StubResolver:
    """按签名实现的假 Secret Store，用来验证「缺凭据即失败」的路径。"""

    def __init__(self, fields: dict[str, str] | None):
        self.fields = fields
        self.calls: list[str] = []

    async def get(self, locator: str) -> dict[str, str] | None:
        self.calls.append(locator)
        return self.fields


async def test_openai_takes_url_and_key_from_secret_store():
    resolver = StubResolver({"base_url": "https://gateway.internal/v1", "api_key": KEY})
    rec = Recorder(openai_ok())
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", secret_locator="llm/primary", secrets=resolver, client=client
        )
        assert (await backend.health())[0] is True
        await backend.complete(MSGS)

    assert str(rec.requests[0].url) == "https://gateway.internal/v1/chat/completions"
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert resolver.calls == ["llm/primary", "llm/primary"]
    # 描述与 repr 里只有 locator，没有值
    assert KEY not in json.dumps(backend.describe(), ensure_ascii=False)
    assert KEY not in repr(backend)


async def test_missing_secret_fails_before_any_request():
    rec = Recorder()
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m",
            secret_locator="llm/absent",
            secrets=StubResolver(None),
            client=client,
        )
        with pytest.raises(LLMConfigError) as info:
            await backend.complete(MSGS)

    assert info.value.kind == "no_credential"
    assert "llm/absent" in str(info.value)
    assert rec.requests == []  # 没凭据就不该发出请求


async def test_secret_without_key_field_is_reported_with_field_list():
    resolver = StubResolver({"base_url": "https://x/v1"})
    async with make_client(Recorder()) as client:
        backend = AnthropicBackend(
            model="m", secret_locator="llm/x", secrets=resolver, client=client
        )
        with pytest.raises(LLMConfigError, match="缺少 api_key"):
            await backend.complete(MSGS)


async def test_resolve_credentials_returns_none_not_empty_dict():
    assert await resolve_credentials(StubResolver(None), "a") is None
    assert await resolve_credentials(None, "a") is None
    assert await resolve_credentials(StubResolver({}), "a") is None  # 空字典 = 没有
    assert await resolve_credentials(StubResolver({"api_key": "k"}), "a") == {"api_key": "k"}


async def test_real_secret_store_is_a_valid_resolver(tmp_path):
    """与 ``security/secret_store.py`` 对接一次：签名一致即插得进去（AUTH-02）。"""
    from workerbee.security.secret_store import SecretStore

    store = await SecretStore.create("passphrase-for-test", tmp_path / "vault.bin")
    await store.put("llm/primary", {"base_url": "https://gw/v1", "api_key": KEY})

    rec = Recorder(openai_ok())
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", secret_locator="llm/primary", secrets=store, client=client
        )
        resp = await backend.complete(MSGS)

    assert resp.text == "摘要正文"
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"


# ===========================================================================
# anthropic：请求构造与错误映射
# ===========================================================================


async def test_anthropic_request_shape_and_system_extraction():
    rec = Recorder(anthropic_ok())
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="claude-test-1", base_url="https://api.anthropic.com", api_key=KEY,
            client=client,
        )
        resp = await backend.complete(MSGS, max_tokens=512, temperature=0.0)

    req = rec.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers  # 不用 Bearer，别把两套认证混起来

    body = rec.json_body()
    assert body["system"] == "你是摘要器"  # system 提到顶层
    assert body["messages"] == [{"role": "user", "content": "把这段材料摘一下"}]
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.0

    assert resp.text == "摘要正文"
    assert resp.backend == "anthropic"


async def test_anthropic_requires_max_tokens_and_fills_the_default():
    """``max_tokens`` 在 anthropic 是必填：调用方不给就填默认值，绝不发一个会被拒的请求。"""
    rec = Recorder(anthropic_ok())
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        await backend.complete(MSGS)

    assert rec.json_body()["max_tokens"] == AnthropicBackend(
        model="m"
    ).default_max_tokens


async def test_anthropic_concatenates_text_blocks_and_reports_usage():
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "model": "claude-test-1",
                "content": [
                    {"type": "text", "text": "第一段。"},
                    {"type": "tool_use", "name": "x", "input": {}},
                    {"type": "text", "text": "第二段。"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 21, "output_tokens": 9},
            },
        )
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        resp = await backend.complete(MSGS)

    assert resp.text == "第一段。第二段。"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (21, 9)


async def test_anthropic_no_text_block_fails():
    rec = Recorder(
        httpx.Response(200, json={"content": [{"type": "tool_use", "name": "x"}]})
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError, match="没有 text 内容块"):
            await backend.complete(MSGS)


async def test_anthropic_max_tokens_stop_reason_is_a_failure():
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "半截"}],
                "stop_reason": "max_tokens",
            },
        )
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        with pytest.raises(LLMResponseError) as info:
            await backend.complete(MSGS)

    assert info.value.kind == "output_truncated"


@pytest.mark.parametrize(
    "status,error_type,kind,retryable,exc_type",
    [
        (529, "overloaded_error", "rate_limit", True, LLMBackendError),
        (429, "rate_limit_error", "rate_limit", True, LLMBackendError),
        (500, "api_error", "server_error", True, LLMBackendError),
        (401, "authentication_error", "auth", False, LLMAuthError),
        (404, "not_found_error", "not_found", False, LLMConfigError),
        (400, "invalid_request_error", "bad_request", False, LLMConfigError),
    ],
)
async def test_anthropic_error_body_drives_the_classification(
    status, error_type, kind, retryable, exc_type
):
    rec = Recorder(
        httpx.Response(status, json={"type": "error", "error": {"type": error_type}})
    )
    async with make_client(rec) as client:
        backend = AnthropicBackend(
            model="m", base_url="https://api.anthropic.com", api_key=KEY, client=client
        )
        with pytest.raises(exc_type) as info:
            await backend.complete(MSGS)

    assert info.value.kind == kind
    assert info.value.retryable is retryable
    assert error_type in str(info.value)


# ===========================================================================
# 凭据纪律（脱敏是 L4 自己的兜底）
# ===========================================================================


def test_secret_repr_str_and_bool_never_reveal():
    s = Secret(KEY, label="llm/primary")
    assert KEY not in repr(s)
    assert KEY not in str(s)
    assert KEY not in f"{s}"
    assert "llm/primary" in repr(s)
    assert s.reveal() == KEY
    assert bool(Secret(KEY)) is True
    assert bool(Secret("")) is False


def test_redact_text_removes_known_values_and_known_shapes():
    text = (
        f"Authorization: Bearer {KEY}\n"
        "openai sk-proj-AAAABBBBCCCCDDDD\n"
        "aws AKIAIOSFODNN7EXAMPLE\n"
        'config: {"api_key": "hunter2hunter2"}\n'
    )
    cleaned = redact_text(text)
    for leaked in (KEY, "sk-proj-AAAABBBBCCCCDDDD", "AKIAIOSFODNN7EXAMPLE", "hunter2hunter2"):
        assert leaked not in cleaned
    assert REDACTED in cleaned

    # 已知值优先于形状规则
    assert KEY not in redact_text(f"key={KEY}", [Secret(KEY)])


def test_scrub_reports_hits_and_is_idempotent():
    once, hits = scrub(f"k={KEY} k={KEY}", [KEY])
    assert hits == 2
    assert scrub(once, [KEY]) == (once, 0)  # 二次脱敏不改变文本


def test_sha256_digest_is_not_mistaken_for_a_secret():
    """形状规则刻意不做「长随机串」的泛化匹配，免得把产物 digest 也毁掉。"""
    digest = "a" * 64
    assert redact_text(f"digest={digest}") == f"digest={digest}"
    assert looks_secret_like(digest) is False
    assert looks_secret_like(KEY) is True


def test_llm_error_message_is_scrubbed_on_construction():
    err = LLMError(f"请求失败，key={KEY}", backend="b", kind="x", secrets=[KEY])
    assert KEY not in str(err)
    assert err.secrets_scrubbed is True
    assert err.as_reason() == "b:x"  # 降级链只用分类，不带正文


# ===========================================================================
# harness_cli：假脚本，不跑真 CLI
# ===========================================================================


def write_script(tmp_path, body: str, name: str = "fake-harness") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_claude_argv_puts_prompt_in_one_element_and_system_in_its_own_flag(tmp_path):
    binary = write_script(tmp_path, 'printf "{}"\n')
    backend = HarnessCLIBackend(harness="claude", binary=binary, model="haiku")

    argv = backend.build_argv(MSGS)

    assert argv[0] == binary
    assert argv[1:3] == ["--append-system-prompt", "你是摘要器"]
    assert argv[3] == "-p"
    assert argv[4] == "把这段材料摘一下"  # prompt 是单个 argv 元素，没有 shell 拼接
    assert argv[5:] == ["--output-format", "json"]


def test_stream_json_adds_verbose(tmp_path):
    binary = write_script(tmp_path, 'printf "{}"\n')
    backend = HarnessCLIBackend(harness="claude", binary=binary, output_format="stream-json")
    assert "--verbose" in backend.build_argv(MSGS)


def test_kimi_folds_system_into_prompt_with_an_explicit_marker(tmp_path):
    binary = write_script(tmp_path, 'printf "ok"\n')
    backend = HarnessCLIBackend(harness="kimi", binary=binary)

    argv = backend.build_argv(MSGS)

    assert "--append-system-prompt" not in argv
    assert argv[1] == "-p"
    prompt = argv[2]
    assert "【系统约束" in prompt and "你是摘要器" in prompt
    assert "把这段材料摘一下" in prompt
    assert argv[3:] == ["--output-format", "text"]


def test_prompt_over_the_length_limit_is_refused_not_truncated(tmp_path):
    binary = write_script(tmp_path, 'printf "ok"\n')
    backend = HarnessCLIBackend(harness="kimi", binary=binary, max_prompt_chars=50)
    with pytest.raises(LLMConfigError) as info:
        backend.build_argv([LLMMessage(role="user", content="x" * 100)])
    assert info.value.kind == "prompt_too_long"


def test_unknown_harness_is_refused():
    with pytest.raises(LLMConfigError, match="不认识的 harness"):
        HarnessCLIBackend(harness="gemini")


async def test_claude_json_output_is_parsed_with_usage_and_cost(tmp_path):
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "这是摘要",
            "total_cost_usd": 0.0123,
            "model": "claude-sonnet-4",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 2,
            },
        },
        ensure_ascii=False,
    )
    binary = write_script(tmp_path, f"cat <<'EOF'\n{payload}\nEOF\n")
    backend = HarnessCLIBackend(harness="claude", binary=binary)

    resp = await backend.complete(MSGS)

    assert resp.text == "这是摘要"
    assert resp.model == "claude-sonnet-4"
    assert resp.backend == "harness-cli:claude"
    assert resp.usage.input_tokens == 120
    assert resp.usage.output_tokens == 30
    assert resp.usage.cache_read_tokens == 5
    assert resp.usage.cost_estimate == pytest.approx(0.0123)


async def test_stream_json_takes_the_last_result_event(tmp_path):
    lines = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps({"type": "result", "subtype": "success", "result": "最终答案"}),
        ]
    )
    binary = write_script(tmp_path, f"cat <<'EOF'\n{lines}\nEOF\n")
    backend = HarnessCLIBackend(harness="claude", binary=binary, output_format="stream-json")

    resp = await backend.complete(MSGS)
    assert resp.text == "最终答案"
    assert resp.usage is None  # 给不出用量就是未知，不是 0


async def test_cli_error_payload_is_reported_as_backend_error(tmp_path):
    payload = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"})
    binary = write_script(tmp_path, f"cat <<'EOF'\n{payload}\nEOF\n")
    backend = HarnessCLIBackend(harness="claude", binary=binary)

    with pytest.raises(LLMBackendError, match="error_max_turns"):
        await backend.complete(MSGS)


async def test_garbage_and_empty_output_fail(tmp_path):
    garbage = write_script(tmp_path, 'printf "这不是 JSON"\n', name="garbage")
    empty = write_script(tmp_path, "exit 0\n", name="empty")
    for binary in (garbage, empty):
        backend = HarnessCLIBackend(harness="claude", binary=binary)
        with pytest.raises(LLMResponseError):
            await backend.complete(MSGS)


async def test_nonzero_exit_is_a_backend_error_carrying_stderr(tmp_path):
    binary = write_script(tmp_path, 'echo "boom: not logged in" >&2\nexit 3\n')
    backend = HarnessCLIBackend(harness="claude", binary=binary)

    with pytest.raises(LLMBackendError) as info:
        await backend.complete(MSGS)

    assert info.value.kind == "nonzero_exit"
    assert "退出码 3" in str(info.value)
    assert "not logged in" in str(info.value)


async def test_timeout_kills_the_child_instead_of_waiting(tmp_path):
    marker = tmp_path / "finished"
    binary = write_script(
        tmp_path, f'sleep 5\nprintf "done" > "{marker}"\nprintf "晚到的输出"\n'
    )
    backend = HarnessCLIBackend(harness="claude", binary=binary, timeout=0.3)

    started = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        await backend.complete(MSGS)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0  # 没有傻等那 5 秒
    await asyncio.sleep(0.2)
    assert not marker.exists()  # 子进程确实被杀了，没有留下来继续跑（RES-02）


async def test_oversized_output_fails_instead_of_truncating(tmp_path):
    binary = write_script(tmp_path, "head -c 200000 /dev/zero | tr '\\0' 'x'\n")
    backend = HarnessCLIBackend(harness="kimi", binary=binary, max_output_bytes=1024)

    with pytest.raises(LLMResponseError) as info:
        await backend.complete(MSGS)

    assert info.value.kind == "output_too_large"
    assert "拒绝把截断文本当作完整结果" in str(info.value)


async def test_read_bounded_does_not_flag_an_exact_fit_as_truncated():
    """恰好等于上限不该被误判为截断（否则正常输出会被当成失败）。"""

    exact = asyncio.StreamReader()
    exact.feed_data(b"x" * 100)
    exact.feed_eof()
    assert await HarnessCLIBackend._read_bounded(exact, 100) == (b"x" * 100, False)

    over = asyncio.StreamReader()
    over.feed_data(b"x" * 101)
    over.feed_eof()
    data, truncated = await HarnessCLIBackend._read_bounded(over, 100)
    assert data == b"x" * 100
    assert truncated is True


async def test_concurrency_is_capped_by_the_semaphore(tmp_path):
    """框架自己开子进程打用户账号：并发必须有上限，超额排队。"""
    binary = write_script(tmp_path, "sleep 0.3\nprintf '{\"result\": \"ok\"}'\n")
    backend = HarnessCLIBackend(
        harness="claude", binary=binary, max_concurrency=2, timeout=10.0
    )

    results = await asyncio.gather(*[backend.complete(MSGS) for _ in range(5)])

    assert [r.text for r in results] == ["ok"] * 5
    assert backend.peak_inflight == 2


async def test_missing_binary_is_unavailable_not_a_crash(tmp_path):
    backend = HarnessCLIBackend(harness="claude", binary=str(tmp_path / "nope"), probe_version=False)

    ok, reason = await backend.health()
    assert ok is False
    assert "未找到可执行文件" in reason

    with pytest.raises(LLMUnavailableError):
        await backend.complete(MSGS)


async def test_health_probes_version_and_states_the_login_caveat(tmp_path):
    binary = write_script(tmp_path, 'if [ "$1" = "--version" ]; then echo "1.2.3"; exit 0; fi\n')
    backend = HarnessCLIBackend(harness="claude", binary=binary)

    ok, reason = await backend.health()
    assert ok is True
    assert "1.2.3" in reason
    assert "登录态未验证" in reason  # 健康检查不谎报「已登录」


async def test_cli_receives_extra_env_and_args(tmp_path):
    args_file = tmp_path / "args.txt"
    env_file = tmp_path / "env.txt"
    binary = write_script(
        tmp_path,
        f'printf "%s\\n" "$@" > "{args_file}"\n'
        f'printf "%s" "$MY_ENV" > "{env_file}"\n'
        'printf \'{"result": "ok"}\'\n',
    )
    backend = HarnessCLIBackend(
        harness="claude",
        binary=binary,
        env={"MY_ENV": "hello"},
        extra_args=["--permission-mode", "plan"],
    )

    assert (await backend.complete(MSGS)).text == "ok"
    assert env_file.read_text(encoding="utf-8") == "hello"  # 环境变量传给了子进程
    recorded = args_file.read_text(encoding="utf-8").splitlines()
    assert "--permission-mode" in recorded
    assert recorded[recorded.index("-p") + 1] == "把这段材料摘一下"


# ===========================================================================
# router：降级可见
# ===========================================================================


class FakeBackend:
    def __init__(self, name: str, *, text: str = "ok", error: Exception | None = None):
        self.name = name
        self._text = text
        self._error = error
        self.calls = 0

    async def complete(self, messages, *, max_tokens=None, temperature=None, timeout=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return LLMResponse(text=self._text, model="fake-model", backend=self.name)

    async def health(self) -> tuple[bool, str | None]:
        if self._error is not None:
            return False, type(self._error).__name__
        return True, None


def make_router(*backends):
    return LLMRouter(list(backends))


async def test_primary_success_is_not_marked_as_degraded():
    primary = FakeBackend("primary", text="一次成功")
    resp = await make_router(primary, FakeBackend("backup")).complete(MSGS)

    assert resp.text == "一次成功"
    assert resp.backend == "primary"
    assert resp.fallback_from is None
    assert resp.degraded() is False


async def test_fallback_is_visible_and_reported_by_hook():
    primary = FakeBackend(
        "primary", error=LLMBackendError("429", backend="primary", kind="rate_limit", retryable=True)
    )
    backup = FakeBackend("backup", text="备用后端的结果")
    seen: list[tuple[list[str], str]] = []

    resp = await LLMRouter([primary, backup], on_fallback=lambda a, b: seen.append((a, b))).complete(
        MSGS
    )

    assert resp.text == "备用后端的结果"
    assert resp.backend == "backup"  # 实际执行者，不是期望者
    assert resp.fallback_from == "primary"
    assert resp.degraded() is True
    assert resp.degraded_reasons == ["primary:rate_limit"]
    assert seen == [(["primary:rate_limit"], "backup")]


async def test_allow_fallback_false_surfaces_the_primary_error():
    primary = FakeBackend("primary", error=LLMConfigError("缺 key", backend="primary"))
    backup = FakeBackend("backup")
    with pytest.raises(LLMConfigError):
        await make_router(primary, backup).complete(MSGS, allow_fallback=False)

    assert backup.calls == 0  # 用户说「就用这个」时不许悄悄换人


async def test_all_backends_failed_carries_the_attempt_chain():
    router = make_router(
        FakeBackend("a", error=LLMTimeoutError("超时", backend="a")),
        FakeBackend("b", error=LLMBackendError("500", backend="b", kind="server_error")),
    )
    with pytest.raises(LLMAllBackendsFailed) as info:
        await router.complete(MSGS)

    assert info.value.attempts == ["a:timeout", "b:server_error"]
    assert "a:timeout" in str(info.value)


async def test_unexpected_exception_still_degrades_but_is_labelled():
    class Exploding(FakeBackend):
        async def complete(self, messages, **kwargs):
            raise ValueError("内部 bug")

    resp = await make_router(Exploding("broken"), FakeBackend("backup")).complete(MSGS)

    assert resp.backend == "backup"
    assert resp.degraded_reasons == ["broken:unexpected"]


async def test_prefer_moves_a_backend_to_the_front_for_one_call():
    primary = FakeBackend("primary", text="主")
    backup = FakeBackend("backup", text="备")
    router = make_router(primary, backup)

    assert (await router.complete(MSGS, prefer="backup")).text == "备"
    assert (await router.complete(MSGS)).text == "主"
    assert router.backend_names == ["primary", "backup"]


async def test_health_aggregates_across_backends():
    ok_router = make_router(FakeBackend("a"), FakeBackend("b"))
    assert (await ok_router.health())[0] is True

    bad_router = make_router(
        FakeBackend("a", error=LLMConfigError("没配", backend="a")),
        FakeBackend("b", error=LLMConfigError("也没配", backend="b")),
    )
    ok, reason = await bad_router.health()
    assert ok is False
    assert "a:" in reason and "b:" in reason


# ===========================================================================
# 配置 → 后端
# ===========================================================================


def test_default_config_is_a_zero_setup_harness_cli():
    cfg = LLMRouterConfig.default()
    assert [(b.kind, b.harness) for b in cfg.backends] == [("harness_cli", "claude")]

    router = LLMRouterConfig.default().build()
    assert router.backend_names == ["harness-cli:claude"]


def test_config_refuses_inline_credentials():
    with pytest.raises(ValueError, match="疑似凭据字段"):
        BackendConfig(kind="openai_compat", model="m", options={"api_key": KEY})


def test_empty_config_is_refused_at_build_time():
    with pytest.raises(ValueError, match="未配置任何 LLM 后端"):
        LLMRouterConfig().build()


def test_backend_config_builds_each_kind():
    router = LLMRouterConfig(
        backends=[
            BackendConfig(kind="harness_cli", harness="kimi"),
            BackendConfig(
                kind="openai_compat", model="m", base_url="https://x/v1", secret_locator="k"
            ),
            BackendConfig(
                kind="anthropic", model="m", base_url="https://a", secret_locator="k"
            ),
        ]
    ).build(secrets=StubResolver(None))

    assert router.backend_names == ["harness-cli:kimi", "openai-compat", "anthropic"]


# ===========================================================================
# 端到端：摘要器 + 真后端 + 假传输层
# ===========================================================================


async def test_summarizer_over_a_real_backend_and_mocked_transport():
    """摘要质量门禁走真实 HTTP 构造路径（只是传输层被 mock 掉）。"""
    payload = json.dumps(
        {"summary": "改动 3 个文件，测试通过", "covered_fields": ["diff", "test_result"]},
        ensure_ascii=False,
    )
    rec = Recorder(openai_ok(payload, prompt_tokens=42, completion_tokens=12))
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        result = await Summarizer(backend).summarize(
            "完整材料", contract_fields=["diff", "test_result"], max_chars=1000
        )

    assert result.ok is True
    assert result.backend == "openai-compat"
    assert result.missing_fields == []
    # 材料作为数据进入 user 消息；凭据只在 header 里
    body = rec.json_body()
    assert "完整材料" in body["messages"][-1]["content"]
    assert KEY not in json.dumps(body, ensure_ascii=False)


async def test_summarizer_reports_backend_failure_as_a_failed_summary():
    rec = Recorder(httpx.Response(500, json={"error": "boom"}))
    async with make_client(rec) as client:
        backend = OpenAICompatBackend(
            model="m", base_url="https://x/v1", api_key=KEY, client=client
        )
        result = await Summarizer(backend).summarize(
            "材料", contract_fields=["diff"], max_chars=1000
        )

    assert result.ok is False
    assert result.missing_fields == ["diff"]
    assert "LLM 调用失败" in (result.reason or "")
