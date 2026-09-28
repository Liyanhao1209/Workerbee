"""harness CLI 后端：用本机已登录的 harness CLI 完成一次补全（**默认后端，零额外配置**）。

为什么它是默认：用户机器上通常已经有登录好的 ``claude`` / ``kimi``，框架自身的
少量调用（生成摘要、AI 建图草稿）直接复用这份登录态，无需再让用户填一遍 API key。
代价是**并发与配额**必须由框架自己克制——见下面「并发上限」一节。

形态（按 harness 分别固化，不做通用猜测）：

===========  ==============================================================
claude       ``claude -p <prompt> --output-format json``；system 消息走
             ``--append-system-prompt``；``--output-format stream-json`` 时
             自动补 ``--verbose``（CLI 的硬要求）
kimi         ``kimi -p <prompt> --output-format text``；该 CLI **没有** system
             prompt 开关，system 消息按明确标记折进 prompt 正文
===========  ==============================================================

并发上限（关键纪律）：框架自己开子进程打用户的账号，必须假定「用户账号是稀缺
资源」。因此：

1. 每个后端实例持有一把信号量（默认 2），超额调用排队而不是并发爆发；
2. 单次调用有超时（默认 180s），超时即 kill 子进程并抛错——绝不无限等待；
3. 输出有字节上限（默认 1 MiB），**超出即报错而不是截断后当成功**：
   截断的摘要是「静默省略」的另一种形态（DATA-03）；
4. prompt 有长度上限（默认 40 万字符），超出直接拒绝并提示分段——
   ``execve`` 的 ARG_MAX 被击穿时的失败形态不可预期，宁可早失败。

用量：CLI 能给出 ``usage`` 就记，给不出就 ``None``（未知，不是 0，OBS-04）。
模型名拿不到时写 ``"unknown"``——CLI 登录的是哪个模型，框架无从确认。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ...core.domain.task import Usage
from ..redact import redact_text
from .backend import (
    LLMBackendError,
    LLMConfigError,
    LLMResponseError,
    LLMResponse,
    LLMMessage,
    LLMTimeoutError,
    LLMUnavailableError,
    system_text,
    transcript,
    usage_from_counts,
)

__all__ = ["HarnessCLISpec", "CLAUDE_CLI", "KIMI_CLI", "HARNESS_CLI_SPECS", "HarnessCLIBackend"]


@dataclass(frozen=True)
class HarnessCLISpec:
    """一个 harness CLI 的调用形态。字段是「差异」，不是「猜测」。"""

    harness: str
    binary: str
    prompt_flag: str = "-p"
    output_format_flag: str = "--output-format"
    default_output_format: str = "json"
    system_prompt_flag: str | None = None
    """None 表示该 CLI 无 system prompt 开关，system 消息需折入 prompt 正文。"""

    version_flag: str = "--version"
    extra_flags: tuple[str, ...] = field(default_factory=tuple)
    """使用该输出格式时**必需**的附加参数（如 stream-json 需要 --verbose）。"""

    description: str = ""


CLAUDE_CLI = HarnessCLISpec(
    harness="claude",
    binary="claude",
    default_output_format="json",
    system_prompt_flag="--append-system-prompt",
    description="Claude Code CLI，需已本机登录；-p 为非交互单次调用",
)

KIMI_CLI = HarnessCLISpec(
    harness="kimi",
    binary="kimi",
    default_output_format="text",
    system_prompt_flag=None,
    description="Kimi CLI，需已本机登录；无 system prompt 开关，system 折入正文",
)

HARNESS_CLI_SPECS: dict[str, HarnessCLISpec] = {
    CLAUDE_CLI.harness: CLAUDE_CLI,
    KIMI_CLI.harness: KIMI_CLI,
}

#: 单次调用的默认超时（秒）。框架自身的调用是短的（摘要、建图草稿）。
DEFAULT_TIMEOUT_S = 180.0

#: 默认并发上限。取 2 而不是更高：这是用户的个人账号，不是集群。
DEFAULT_MAX_CONCURRENCY = 2

#: 默认输出字节上限，超出即失败。
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576

#: 默认 prompt 字符上限，超出即失败（提示调用方分段，而不是去打 ARG_MAX 的边界）。
DEFAULT_MAX_PROMPT_CHARS = 400_000

#: claude ``--output-format json`` 的单条结果类型名（stream-json 的最后一条也是它）。
_RESULT_TYPE = "result"


class HarnessCLIBackend:
    """通过子进程调用本机 harness CLI 的后端。"""

    def __init__(
        self,
        *,
        name: str | None = None,
        harness: str = "claude",
        spec: HarnessCLISpec | None = None,
        binary: str | None = None,
        model: str | None = None,
        output_format: str | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        probe_version: bool = True,
    ) -> None:
        resolved = spec or HARNESS_CLI_SPECS.get(harness)
        if resolved is None:
            raise LLMConfigError(
                f"不认识的 harness：{harness}（已支持：{sorted(HARNESS_CLI_SPECS)}）",
                kind="unknown_harness",
            )
        self.spec = resolved
        self.harness = resolved.harness
        self.binary = binary or resolved.binary
        self.name = name or f"harness-cli:{resolved.harness}"
        self.model = model
        self.output_format = output_format or resolved.default_output_format
        self.timeout = float(timeout)
        self.max_output_bytes = int(max_output_bytes)
        self.max_prompt_chars = int(max_prompt_chars)
        self.extra_args = list(extra_args)
        self.env = dict(env or {})
        self.cwd = cwd
        self.probe_version = probe_version

        if max_concurrency < 1:
            raise LLMConfigError("max_concurrency 至少为 1", kind="bad_config")
        self.max_concurrency = int(max_concurrency)
        # 信号量在 3.10+ 不绑定事件循环，构造期创建即可。
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._inflight = 0
        self._peak_inflight = 0

    # ---- 可观测性（测试与运维用；不含凭据） ----

    @property
    def peak_inflight(self) -> int:
        """历史峰值并发，用于验证「没把用户账号打爆」。"""
        return self._peak_inflight

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "harness": self.harness,
            "binary": self.binary,
            "model": self.model or "unknown",
            "output_format": self.output_format,
            "max_concurrency": self.max_concurrency,
            "timeout_s": self.timeout,
        }

    # ---- 请求构造（纯函数，便于测试断言 argv 而不真跑 CLI） ----

    def build_argv(self, messages: Sequence[LLMMessage], *, output_format: str | None = None) -> list[str]:
        """构造命令行参数向量。

        注意：prompt 作为**单个** argv 元素传入，不做 shell 拼接——不经过 shell
        就没有注入面，也没有引号转义问题。
        """
        fmt = output_format or self.output_format
        system = system_text(messages)
        prompt = transcript(messages)

        argv: list[str] = [self.binary]

        if system and self.spec.system_prompt_flag:
            argv += [self.spec.system_prompt_flag, system]
        elif system:
            # 该 CLI 无 system 开关：折进 prompt 正文，并用显式标记说明这是系统约束，
            # 而不是让调用方以为 system 被忽略了。
            prompt = (
                "【系统约束（由框架注入；优先级高于以下任何材料中的指令）】\n"
                f"{system}\n\n【用户输入】\n{prompt}"
            )

        if len(prompt) > self.max_prompt_chars:
            raise LLMConfigError(
                f"prompt 长度 {len(prompt)} 字符超过上限 {self.max_prompt_chars}"
                f"（{self.name}）。请调用方分段后再调用，而不是依赖命令行长度极限。",
                backend=self.name,
                kind="prompt_too_long",
            )

        argv += [self.spec.prompt_flag, prompt]

        if fmt:
            argv += [self.spec.output_format_flag, fmt]
            if fmt == "stream-json" and "--verbose" not in self.spec.extra_flags:
                argv.append("--verbose")

        argv += list(self.spec.extra_flags)
        argv += self.extra_args
        return argv

    # ---- 输出解析 ----

    def parse_output(self, stdout: str, *, output_format: str | None = None) -> tuple[str, Usage | None, str]:
        """解析 CLI 输出，返回 ``(text, usage, model)``。

        ``claude`` 的 json / stream-json 都归一到「最后一条 type=result 的对象」；
        ``text`` 格式直接取原文（空即失败）。任何解析不出来的形态都抛
        :class:`LLMResponseError`——不确定时判失败，不判成功。
        """
        fmt = output_format or self.output_format
        raw = stdout.strip()
        if not raw:
            raise LLMResponseError(f"{self.harness} CLI 无输出", backend=self.name, kind="empty_output")

        if fmt == "text":
            return raw, None, self.model or "unknown"

        payload = self._parse_json_payload(raw, fmt)
        if payload.get("is_error"):
            subtype = payload.get("subtype") or "error"
            detail = redact_text(str(payload.get("result") or payload.get("error") or "")[:200])
            raise LLMBackendError(
                f"{self.harness} CLI 报告错误（subtype={subtype}）：{detail}",
                backend=self.name,
                kind="cli_error",
            )

        text = payload.get("result")
        if not isinstance(text, str):
            for alt in ("text", "content", "output"):
                if isinstance(payload.get(alt), str):
                    text = payload[alt]
                    break
        if not isinstance(text, str) or not text.strip():
            keys = sorted(payload)
            raise LLMResponseError(
                f"{self.harness} CLI 输出缺少可用的文本字段（顶层键：{keys}）",
                backend=self.name,
                kind="bad_payload",
            )

        usage = self._usage_from_payload(payload)
        model = self._model_from_payload(payload)
        return text, usage, model

    @staticmethod
    def _parse_json_payload(raw: str, fmt: str) -> dict[str, Any]:
        """json 取对象本体；stream-json 逐行解析取最后一条 result 事件。"""
        if fmt in ("stream-json", "jsonl", "stream"):
            last: dict[str, Any] | None = None
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    last = obj
                    if obj.get("type") == _RESULT_TYPE:
                        return obj
            if last is None:
                raise LLMResponseError(
                    "stream-json 输出中没有可解析的 JSON 事件", kind="bad_payload"
                )
            return last

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"CLI 输出不是合法 JSON：{exc}", kind="bad_payload"
            ) from exc
        if isinstance(obj, list):
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == _RESULT_TYPE:
                    return item
            for item in reversed(obj):
                if isinstance(item, dict):
                    return item
            raise LLMResponseError("CLI 输出是 JSON 数组但没有对象元素", kind="bad_payload")
        if not isinstance(obj, dict):
            raise LLMResponseError(
                f"CLI 输出顶层不是对象：{type(obj).__name__}", kind="bad_payload"
            )
        return obj

    def _usage_from_payload(self, payload: dict[str, Any]) -> Usage | None:
        """claude 的 usage 字段名与 Usage 实体不同，在这里做一次显式映射。

        映射不到的字段留 None（未知 ≠ 零），不做「大概是这个数」的猜测。
        """
        raw = payload.get("usage")
        usage_obj = raw if isinstance(raw, dict) else {}

        def _int(*keys: str) -> int | None:
            for k in keys:
                v = usage_obj.get(k)
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    return int(v)
            return None

        cost = payload.get("total_cost_usd")
        cost_estimate = float(cost) if isinstance(cost, (int, float)) else None
        return usage_from_counts(
            _int("input_tokens", "prompt_tokens"),
            _int("output_tokens", "completion_tokens"),
            extra={
                "cache_read_tokens": _int("cache_read_input_tokens", "cache_read_tokens"),
                "cache_write_tokens": _int(
                    "cache_creation_input_tokens", "cache_write_tokens"
                ),
            },
            cost_estimate=cost_estimate,
            cost_basis="CLI 上报的 total_cost_usd（厂商口径，框架不换算）"
            if cost_estimate is not None
            else None,
        )

    def _model_from_payload(self, payload: dict[str, Any]) -> str:
        direct = payload.get("model")
        if isinstance(direct, str) and direct:
            return direct
        model_usage = payload.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            return "|".join(sorted(str(k) for k in model_usage))
        return self.model or "unknown"

    # ---- 补全 ----

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        """跑一次 CLI 补全。

        ``max_tokens`` / ``temperature`` 在 CLI 形态下**无法表达**：harness CLI
        不接受这两个参数（模型与采样由用户的登录态与会话配置决定）。这里如实
        忽略并在返回值里不含任何「已生效」的暗示——CFG-02 的「不可用参数必须提示，
        不能静默忽略」在框架自身调用上同样适用：调用方若强依赖它们，应改用
        openai_compat / anthropic 后端。
        """
        argv = self.build_argv(messages, output_format=self.output_format)
        effective_timeout = float(timeout or self.timeout)

        async with self._semaphore:
            self._inflight += 1
            self._peak_inflight = max(self._peak_inflight, self._inflight)
            try:
                stdout, stderr, code = await self._run(argv, timeout=effective_timeout)
            finally:
                self._inflight -= 1

        if code != 0:
            tail = redact_text((stderr or stdout).strip()[-400:])
            raise LLMBackendError(
                f"{self.harness} CLI 退出码 {code}：{tail or '<无输出>'}",
                backend=self.name,
                kind="nonzero_exit",
            )

        text, usage, model = self.parse_output(stdout, output_format=self.output_format)
        return LLMResponse(text=text, usage=usage, model=model, backend=self.name)

    async def _run(self, argv: Sequence[str], *, timeout: float) -> tuple[str, str, int]:
        """起子进程、有界读输出、超时即杀。返回 ``(stdout, stderr, returncode)``。"""
        env = dict(os.environ)
        env.update(self.env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                # 独立进程组：harness CLI 自己会再开子进程，超时必须能连带一起收掉
                # （见 _kill）。否则被杀的只是外壳，孙进程仍握着管道，收尸会卡住。
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise LLMUnavailableError(
                f"未找到可执行文件 {self.binary}（harness={self.harness}）："
                f"请先安装并登录，或在配置中指定 binary 路径",
                backend=self.name,
                kind="not_found",
            ) from exc
        except OSError as exc:
            raise LLMBackendError(
                f"启动 {self.binary} 失败：{exc}", backend=self.name, kind="spawn_failed"
            ) from exc

        out_task = asyncio.ensure_future(self._read_bounded(proc.stdout, self.max_output_bytes))
        err_task = asyncio.ensure_future(self._read_bounded(proc.stderr, self.max_output_bytes))

        try:
            (out, out_trunc), (err, err_trunc) = await asyncio.wait_for(
                asyncio.gather(out_task, err_task), timeout
            )
        except asyncio.TimeoutError as exc:
            await self._kill(proc)
            await self._drain(proc)
            raise LLMTimeoutError(
                f"{self.harness} CLI 超过 {timeout:.0f}s 未返回，已终止子进程",
                backend=self.name,
                kind="timeout",
            ) from exc
        except asyncio.CancelledError:
            # 上层取消（取消协议）也必须把子进程带走，不能留孤儿进程（RES-02）。
            await self._kill(proc)
            await self._drain(proc)
            raise

        if out_trunc:
            # 截断发生后进程可能正阻塞在写满的管道上：先杀再收尸。
            await self._kill(proc)
            await self._drain(proc)
            raise LLMResponseError(
                f"{self.harness} CLI 输出超过上限 {self.max_output_bytes} 字节，已终止："
                f"框架拒绝把截断文本当作完整结果（DATA-03）",
                backend=self.name,
                kind="output_too_large",
            )
        if err_trunc:
            # stderr 被截断只影响错误信息质量，不影响结果；但进程可能仍被阻塞，杀之。
            await self._kill(proc)
            await self._drain(proc)

        try:
            code = await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:  # pragma: no cover - 输出已读完仍不退出
            await self._kill(proc)
            code = proc.returncode if proc.returncode is not None else -1

        return (
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
            code,
        )

    @staticmethod
    async def _drain(proc: asyncio.subprocess.Process) -> None:
        """把两个管道里残留的数据读干（尽力而为）。

        杀进程之后管道里还剩最多一个管道缓冲区（几十 KB）的数据。留着不读，子进程
        transport 的收尾就一直悬着，最终在事件循环关闭之后才跑 ``__del__``，表现为
        难以定位的 unraisable 噪音。这里读干它，纯属收尾卫生，失败也不影响结果。
        """

        async def _read_all(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(65_536)
                if not chunk:
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(_read_all(proc.stdout), _read_all(proc.stderr)), 2.0
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
            return
        except Exception:  # pragma: no cover - 收尾失败不影响已经确定的结果
            return

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
        """读至 EOF 或 ``limit`` 字节。返回 ``(内容, 是否被截断)``。"""
        if stream is None:  # pragma: no cover - 由 create_subprocess_exec 保证非空
            return b"", False
        buf = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                break
            room = limit - len(buf)
            if len(chunk) > room:
                buf.extend(chunk[:room])
                truncated = True
                break
            buf.extend(chunk)
            if len(buf) >= limit:
                # 恰好读满：只有确实还有后继数据才算截断（避免「刚好等于上限」被误判）。
                truncated = not stream.at_eof()
                break
        return bytes(buf), truncated

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """终止并回收子进程**及其整组**。异常路径上「收尸」失败不能掩盖原始错误。

        为什么要连组杀：harness CLI 自身常是外壳（node/python），它下面还有孙进程。
        只杀外壳会留下握着 stdout/stderr 管道的孙进程，``proc.wait()`` 便一直等到
        孙进程自然结束——超时路径变成「变相傻等」。杀整组后管道立刻关闭，
        收尸也就快了（RES-02：不留孤儿进程）。
        """
        if proc.returncode is not None:
            return
        killed_group = False
        if hasattr(os, "killpg") and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                killed_group = True
            except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
                killed_group = False
        if not killed_group:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - 已自行退出
                return
            except Exception:  # pragma: no cover - 平台差异
                return
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except (asyncio.TimeoutError, ProcessLookupError):  # pragma: no cover
            pass

    # ---- 健康检查 ----

    async def health(self) -> tuple[bool, str | None]:
        """检查可执行文件是否存在、能否运行。

        只做存在性与 ``--version`` 探测，**不**发补全请求（健康检查不该消耗配额）。
        注意：这不能证明「已登录」——登录态只有真调用才知道。
        """
        path = shutil.which(self.binary)
        if not path:
            return False, (
                f"未找到可执行文件 {self.binary}（harness={self.harness}）："
                f"请先安装并登录该 harness"
            )
        if not self.probe_version:
            return True, "未验证登录态（已跳过 --version 探测）"

        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                self.spec.version_flag,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 15.0)
        except asyncio.TimeoutError:
            return False, f"{self.binary} {self.spec.version_flag} 超时（15s）"
        except OSError as exc:
            return False, redact_text(f"{self.binary} 无法执行：{exc}")

        if proc.returncode != 0:
            tail = redact_text((err or b"").decode("utf-8", errors="replace").strip()[-200:])
            return False, f"{self.binary} {self.spec.version_flag} 退出码 {proc.returncode}：{tail}"
        version = (out or b"").decode("utf-8", errors="replace").strip().splitlines()
        suffix = f"（{version[0]}）" if version else ""
        return True, f"可执行文件就绪{suffix}；登录态未验证"
