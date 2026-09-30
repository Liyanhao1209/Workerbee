"""基础助手的核心编排（AI-01，计划 §6）。

一次问答的完整路径：

1. 读线程历史（assistant_message 表）→ 滑动窗口截断（不产生额外调用）；
2. 拼 system prompt = 角色设定 + 使用指引（guidebook）+ 系统快照（snapshot）
   + 钉扎的「整理前文」摘要；
3. LLMRouter.complete（后端链由配置里的 credential_ref 装配，openai_compat）；
4. 回复过脱敏 → 落库 → 写事件 → notifier 推送。

安全边界（计划 §8）：
- **只读**：本服务只调数据源的只读方法与仓储的助手表写入，没有任何系统写入口。
- **脱敏**：用户消息、助手回复在入库与发给模型之前各过一次 redactor
  （用户可能粘贴含密钥的报错；回复可能复述快照里的敏感片段）。
- **密值不出进程**：凭据只以 credential_ref → secret_locator 解析，
  密钥本体只进后端传输层；事件日志只记录后端名 / 用量 / 降级原因，不落正文。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from ..core.domain.base import DomainModel, new_id, utcnow
from ..core.runtime.notifier import Notification
from ..data.db import Database
from ..data.event_log import EventActor, EventScope, EventType
from ..data.llm.backend import LLMBackend, LLMError, LLMMessage
from ..data.llm.router import BackendConfig, LLMRouter
from ..data.store import Store
from .guidebook import DEFAULT_GUIDEBOOK_BUDGET, load_guidebook
from .memory import (
    DEFAULT_WINDOW_CHARS,
    DEFAULT_WINDOW_ROUNDS,
    latest_memory,
    sliding_window,
)
from .snapshot import DEFAULT_SNAPSHOT_BUDGET, SnapshotSource, build_snapshot

__all__ = [
    "AssistantService",
    "AssistantConfig",
    "AssistantError",
    "AssistantNotConfigured",
    "AssistantLocked",
    "AssistantCallFailed",
    "load_config",
    "save_config",
    "CONFIG_KEY",
]

#: 配置在 meta_kv 表里的键（免迁移，schema.py 的 meta_kv 现成）。
CONFIG_KEY = "assistant_config"

#: 发给模型的单次调用超时（秒）。同步等待的问答不能无限挂住。
CALL_TIMEOUT_S = 120.0

_ROLE_PROMPT = """你是 Workerbee（本机多智能体工作流编排工具）的内置助手。

规则：
- 你只能回答问题、解释概念、指引操作路径；你不能替用户改任何配置、建流程、
  提交任务或碰凭据。需要动手时，告诉用户到哪个页面、点哪里。
- 回答基于下面给出的「使用指引」和「系统当前状态快照」。快照是发消息那一刻的
  状态；快照里没有的信息就说不知道，不要编造。
- 用大白话中文回答，先给结论，再给操作步骤。不要暴露内部字段名与表名。"""

_COMPACT_PROMPT = """下面是用户与助手的一段较早对话，以及（可能有的）此前整理过的摘要。
请把它们压缩成一段简短的中文记忆，保留：用户关心的问题、已给出的结论、
尚未解决的悬念。只输出整理后的正文，不要加标题或解释。"""


# ---------------------------------------------------------------------------
# 错误：服务层据此翻译成 HTTP，文案给最终用户看
# ---------------------------------------------------------------------------


class AssistantError(RuntimeError):
    """助手用例失败的基类。消息是大白话中文，不含内部类型名。"""

    def __init__(self, detail: str, *, hint: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class AssistantNotConfigured(AssistantError):
    """助手未启用、未选凭据、凭据失效或缺模型名。"""


class AssistantLocked(AssistantError):
    """凭据库未解锁，读不到助手模型的密钥。"""


class AssistantCallFailed(AssistantError):
    """模型调用失败（含超时与全部后端耗尽）。"""


# ---------------------------------------------------------------------------
# 配置（meta_kv 表，密值只进不出：这里只存引用）
# ---------------------------------------------------------------------------


class AssistantConfig(DomainModel):
    """助手配置。``credential_ref`` 只是引用，密钥本体永远不进这份配置。"""

    enabled: bool = False
    credential_ref: str | None = None
    model_override: str | None = None
    window_rounds: int = DEFAULT_WINDOW_ROUNDS
    window_chars: int = DEFAULT_WINDOW_CHARS
    snapshot_budget: int = DEFAULT_SNAPSHOT_BUDGET


async def load_config(db: Database) -> AssistantConfig:
    raw = await db.fetch_value("SELECT v FROM meta_kv WHERE k=?", (CONFIG_KEY,))
    if not raw:
        return AssistantConfig()
    try:
        return AssistantConfig.model_validate(json.loads(raw))
    except (ValueError, TypeError):
        # 配置损坏时回退默认并如实可改（PUT 一次即修复），不抛无法理解的错误。
        return AssistantConfig()


async def save_config(db: Database, config: AssistantConfig) -> None:
    await db.execute(
        "INSERT INTO meta_kv(k, v, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (CONFIG_KEY, config.model_dump_json(), utcnow().isoformat()),
    )


# ---------------------------------------------------------------------------
# 核心服务
# ---------------------------------------------------------------------------

#: 后端工厂签名：给定配置与凭据引用，产出一个后端实例。测试用它注入假后端；
#: 生产实现是 ``_default_backend_factory``（openai_compat，经 LLMRouter）。
BackendFactory = Callable[[AssistantConfig, Any, Any], LLMBackend]


def _default_backend_factory(
    config: AssistantConfig, credential_ref: Any, secrets: Any
) -> LLMBackend:
    """生产路径：openai_compat 后端，密钥走 secret_locator（AUTH-02）。"""
    return BackendConfig(
        kind="openai_compat",
        name="assistant",
        model=config.model_override or credential_ref.default_model or "",
        base_url=credential_ref.base_url,
        secret_locator=credential_ref.secret_locator,
    ).build(secrets=secrets)


class AssistantService:
    """助手用例的编排核心。

    ``snapshot_source`` 由 server 层在装配时注入（assistant/ 不 import server）；
    未注入时快照为空并如实标注降级——线程与历史仍可用。

    ``secret_resolver`` 是「取当前凭据库」的回调而不是凭据库本身：
    解锁／锁定发生在引擎生命周期中段，持死引用会看不到解锁后的库。
    """

    def __init__(
        self,
        *,
        store: Store,
        notifier: Any = None,
        secret_resolver: Callable[[], Any] | None = None,
        redactor: Callable[[Any], Any] | None = None,
        snapshot_source: SnapshotSource | None = None,
        backend_factory: BackendFactory | None = None,
        guidebook_path: Any = None,
        guidebook_budget: int = DEFAULT_GUIDEBOOK_BUDGET,
        call_timeout: float = CALL_TIMEOUT_S,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self._secret_resolver = secret_resolver or (lambda: None)
        self.redactor = redactor
        self.snapshot_source = snapshot_source
        self.backend_factory = backend_factory
        self.guidebook_path = guidebook_path
        self.guidebook_budget = guidebook_budget
        self.call_timeout = call_timeout
        self._router_cache: tuple[str, LLMRouter] | None = None

    # ------------------------------------------------------------------
    # 线程与历史
    # ------------------------------------------------------------------

    async def create_thread(self, *, title: str = "") -> dict[str, Any]:
        return await self.store.assistant.create_thread(new_id(), title)

    async def list_threads(self) -> list[dict[str, Any]]:
        return await self.store.assistant.list_threads()

    async def list_messages(self, thread_id: str) -> list[dict[str, Any]]:
        await self._require_thread(thread_id)
        return await self.store.assistant.list_messages(thread_id)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    async def get_config(self) -> AssistantConfig:
        return await load_config(self.store.db)

    async def save_config(self, config: AssistantConfig) -> AssistantConfig:
        await save_config(self.store.db, config)
        self._router_cache = None  # 配置变了，缓存的后端链作废
        return config

    # ------------------------------------------------------------------
    # 问答
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, text: str) -> dict[str, Any]:
        """同步问答：落用户消息 → 调模型 → 落助手回复 → 留痕 → 推送。"""
        await self._require_thread(thread_id)
        router, config = await self._require_backend()

        # 用户消息先脱敏再入库、再发给模型：用户可能粘贴含密钥的报错（AUTH-02）。
        safe_text = self._redact(text)
        history = await self.store.assistant.list_messages(thread_id)
        window = sliding_window(
            history,
            max_rounds=config.window_rounds,
            max_chars=config.window_chars,
        )
        memory = latest_memory(history)

        guidebook = load_guidebook(self.guidebook_path, budget=self.guidebook_budget)
        snapshot = await build_snapshot(
            self.snapshot_source,
            budget=config.snapshot_budget,
            redactor=self._redact,
        )

        messages = self._build_prompt(
            guidebook=guidebook.text,
            snapshot=snapshot.text,
            memory=memory,
            window=window.messages,
            question=safe_text,
        )

        degraded_notes: list[str] = []
        if guidebook.missing:
            degraded_notes.append("使用指引文档缺失，本次回答只依据系统状态")
        if snapshot.degraded:
            degraded_notes.extend(snapshot.degraded)
        if snapshot.trimmed:
            degraded_notes.append(
                "系统快照因预算裁掉了分区：" + "、".join(snapshot.trimmed)
            )

        try:
            response = await router.complete(messages, timeout=self.call_timeout)
        except LLMError as exc:
            raise AssistantCallFailed(
                f"助手模型调用失败：{exc}",
                hint="模型服务可能暂时不可用；稍后再试，或检查凭据与 Base URL 是否还有效",
            ) from exc

        safe_reply = self._redact(response.text)
        degraded = response.degraded() or bool(degraded_notes)
        usage = response.usage

        user_message = await self.store.assistant.append_message(
            message_id=new_id(), thread_id=thread_id, role="user", content=safe_text
        )
        reply = await self.store.assistant.append_message(
            message_id=new_id(),
            thread_id=thread_id,
            role="assistant",
            content=safe_reply,
            backend=response.backend,
            tokens_in=usage.input_tokens if usage else None,
            tokens_out=usage.output_tokens if usage else None,
            degraded=degraded,
        )

        # 留痕：后端、用量、降级、截断条数都记；对话正文不进事件日志。
        if response.degraded():
            await self.store.events.append(
                scope=EventScope.ASSISTANT,
                type=EventType.ASSISTANT_BACKEND_DEGRADED,
                actor=EventActor.AI,
                scope_id=thread_id,
                payload={
                    "fallback_from": response.fallback_from,
                    "used": response.backend,
                    "reasons": list(response.degraded_reasons),
                },
            )
        await self.store.events.append(
            scope=EventScope.ASSISTANT,
            type=EventType.ASSISTANT_MESSAGE,
            actor=EventActor.AI,
            scope_id=thread_id,
            payload={
                "message_id": reply["message_id"],
                "backend": response.backend,
                "model": response.model,
                "tokens_in": usage.input_tokens if usage else None,
                "tokens_out": usage.output_tokens if usage else None,
                "degraded": degraded,
                "window_dropped": window.dropped,
                "guidebook_missing": guidebook.missing,
                "guidebook_truncated": guidebook.truncated,
                "snapshot_trimmed": snapshot.trimmed,
            },
        )

        if self.notifier is not None:
            self.notifier.publish(
                Notification(
                    kind="assistant_message",
                    payload={"thread_id": thread_id, "message_id": reply["message_id"]},
                )
            )

        return {
            "user_message": user_message,
            "message": reply,
            "dropped": window.dropped,
            "degraded": degraded,
            "degraded_reasons": degraded_notes + list(response.degraded_reasons),
        }

    # ------------------------------------------------------------------
    # 手动整理前文（唯一会产生额外调用的记忆操作，用户显式触发）
    # ------------------------------------------------------------------

    async def compact_thread(self, thread_id: str) -> dict[str, Any]:
        """把窗口外的旧消息（连同旧摘要）整理成一条 memory 消息入库。"""
        await self._require_thread(thread_id)
        router, config = await self._require_backend()

        history = await self.store.assistant.list_messages(thread_id)
        conversation = [m for m in history if m.get("role") != "memory"]
        window = sliding_window(
            conversation,
            max_rounds=config.window_rounds,
            max_chars=config.window_chars,
        )
        outside = conversation[: len(conversation) - len(window.messages)]
        previous = latest_memory(history)
        if not outside and previous is None:
            return {
                "compacted": False,
                "summarized": 0,
                "memory_message_id": None,
                "note": "没有窗口之外的旧消息，不需要整理",
            }

        material: list[str] = []
        if previous is not None:
            material.append(f"【此前的整理】\n{previous['content']}")
        for m in outside:
            who = "用户" if m.get("role") == "user" else "助手"
            material.append(f"【{who}】\n{m.get('content', '')}")

        messages = [
            LLMMessage(role="system", content=_COMPACT_PROMPT),
            LLMMessage(role="user", content=self._redact("\n\n".join(material))),
        ]
        try:
            response = await router.complete(messages, timeout=self.call_timeout)
        except LLMError as exc:
            raise AssistantCallFailed(f"整理前文失败：{exc}") from exc

        memory_message = await self.store.assistant.append_message(
            message_id=new_id(),
            thread_id=thread_id,
            role="memory",
            content=self._redact(response.text),
            backend=response.backend,
            tokens_in=response.usage.input_tokens if response.usage else None,
            tokens_out=response.usage.output_tokens if response.usage else None,
            degraded=response.degraded(),
        )
        await self.store.events.append(
            scope=EventScope.ASSISTANT,
            type=EventType.ASSISTANT_COMPACTED,
            actor=EventActor.USER,
            scope_id=thread_id,
            payload={
                "memory_message_id": memory_message["message_id"],
                "summarized": len(outside),
                "absorbed_previous_memory": previous is not None,
                "backend": response.backend,
            },
        )
        return {
            "compacted": True,
            "summarized": len(outside),
            "memory_message_id": memory_message["message_id"],
            "note": None,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """收束缓存的后端（关闭其持有的 HTTP 连接）。引擎 stop 时调用。"""
        if self._router_cache is not None:
            _key, router = self._router_cache
            for backend in getattr(router, "_backends", []):
                close = getattr(backend, "aclose", None)
                if close is not None:
                    await close()
            self._router_cache = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require_thread(self, thread_id: str) -> dict[str, Any]:
        thread = await self.store.assistant.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return thread

    async def _require_backend(self) -> tuple[LLMRouter, AssistantConfig]:
        """按当前配置装配（或复用缓存的）后端链。各种「不可用」都在这里明确报出。"""
        config = await self.get_config()
        if not config.enabled:
            raise AssistantNotConfigured(
                "助手还没有启用",
                hint="在助手面板的设置里打开开关，并选择一条模型凭据",
            )
        if not config.credential_ref:
            raise AssistantNotConfigured(
                "还没给助手选模型凭据",
                hint="到 注册表 → 凭据 建一条含 Base URL 和 Key 的凭据，"
                "再到助手面板的设置里选它",
            )
        credential = await self.store.registry.get_credential(config.credential_ref)
        if credential is None:
            raise AssistantNotConfigured(
                "助手配置指向的凭据已经不存在了",
                hint="到助手面板的设置里重新选择一条凭据",
            )
        if credential.revoked:
            raise AssistantNotConfigured(
                f"助手用的凭据「{credential.label}」已被撤销",
                hint="恢复该凭据，或到助手面板的设置里换一条",
            )
        if not credential.secret_locator:
            raise AssistantNotConfigured(
                f"凭据「{credential.label}」没有密钥内容（harness 登录态不能给助手用）",
                hint="建一条含 Base URL 和 Key 的凭据，再到设置里选它",
            )
        model = config.model_override or credential.default_model
        if not model:
            raise AssistantNotConfigured(
                f"凭据「{credential.label}」没有默认模型名，助手不知道该用哪个模型",
                hint="在助手面板的设置里填一个模型名，或给凭据补上默认模型",
            )
        secrets = self._secret_resolver()
        if secrets is None:
            raise AssistantLocked(
                "凭据库还没有解锁，读不到助手模型的密钥",
                hint="用带口令的方式重启内核（--passphrase 或 WORKERBEE_PASSPHRASE），"
                "解锁后再试",
            )

        cache_key = json.dumps(
            {
                "ref": config.credential_ref,
                "locator": credential.secret_locator,
                "base_url": credential.base_url,
                "model": model,
                "factory": id(self.backend_factory),
            },
            sort_keys=True,
        )
        if self._router_cache is not None and self._router_cache[0] == cache_key:
            return self._router_cache[1], config

        await self.aclose()
        factory = self.backend_factory or _default_backend_factory
        backend = factory(config, credential, secrets)
        # 工厂可以直接给一个装配好的 LLMRouter（测试注入多后端降级链），
        # 否则把单个后端包成「只有一个后端」的链。
        router = (
            backend if isinstance(backend, LLMRouter) else LLMRouter([backend], name="assistant")
        )
        self._router_cache = (cache_key, router)
        return router, config

    def _build_prompt(
        self,
        *,
        guidebook: str,
        snapshot: str,
        memory: dict[str, Any] | None,
        window: Sequence[dict[str, Any]],
        question: str,
    ) -> list[LLMMessage]:
        parts = [_ROLE_PROMPT]
        parts.append(
            "## 使用指引\n\n" + (guidebook or "（使用指引文档还没写好，这一部分为空）")
        )
        parts.append(
            "## 系统当前状态快照\n\n"
            + (snapshot or "（本次取不到系统状态快照，回答时不要编造系统状态）")
        )
        if memory is not None:
            parts.append(f"## 此前对话的整理\n\n{memory.get('content', '')}")
        messages = [LLMMessage(role="system", content="\n\n".join(parts))]
        for m in window:
            role = m.get("role")
            if role in ("user", "assistant"):
                messages.append(LLMMessage(role=role, content=str(m.get("content", ""))))
        messages.append(LLMMessage(role="user", content=question))
        return messages

    def _redact(self, value: Any) -> Any:
        if self.redactor is None or value is None:
            return value
        return self.redactor(value)
