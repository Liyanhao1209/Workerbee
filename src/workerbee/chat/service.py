"""Web Chat 的核心编排（v0.03 §5，D-D/D-E）。

与只读助手（assistant/）**分域并存**：新表（chat_session/chat_node）、
新路由、可经工具写文件系统——权限模型与 system prompt 完全不同，互不混库。

一次发送的完整路径：

1. 落用户节点（parent 默认当前分支叶；``refs`` 里的 ``@路径`` 引用按
   read 同款 confinement 读入并拼进节点正文——模型所见即所存）；
2. 上下文重建：分支路径（根→叶）→ 滑窗截断 → 工具序列清理；
3. 后端解析：会话自带 ``credential_ref`` 优先，否则复用助手配置；
   后端不支持 tools（如 harness_cli）时**显式降级**——不带 tools 跑，
   降级原因如实返回并可推送；
4. 工具循环（``tool_loop.py``）：流式 chunk 推 ``chat_chunk``，
   assistant/tool 节点即时落库（REST 是事实源，推送只是加速器）；
5. 写/执行类工具过 ApprovalGateway（合成绑定 ``chat:<session_id>``），
   等待期间推 ``chat_status``；审批通过才执行。会话级临时授权（D-G：
   「本会话不再询问此类操作」）按 write / run 类别跳过逐次审批，
   危险命令模式不豁免；
6. 回复、推理、工具结果落库前过 SecretRedactor；事件日志只记元信息。

本模块不 import server 层（组装范本 §0.5）：审批网关、推送、凭据解析
都由组合根（app.py）注入。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Sequence

from ..assistant.memory import DEFAULT_WINDOW_CHARS, DEFAULT_WINDOW_ROUNDS
from ..assistant.service import AssistantConfig, load_config
from ..core.domain.approval import ApprovalStatus
from ..core.domain.base import new_id
from ..core.runtime.notifier import Notification
from ..data.event_log import EventActor, EventScope, EventType
from ..data.llm import LLMBackend, LLMError, LLMRouter
from ..data.llm.backend import LLMToolCall
from ..data.llm.router import BackendConfig
from ..data.store import Store
from . import fs
from .context import branch_path, build_context
from .run import DEFAULT_RUN_TIMEOUT_S, dangerous_reason
from .tool_loop import DEFAULT_MAX_TOOL_ROUNDS, run_tool_loop
from .tools import (
    APPROVAL_TOOLS,
    CHAT_TOOL_SPECS,
    RUN_TIMEOUT_CAP_S,
    RUN_TOOLS,
    ToolOutcome,
    execute_tool,
)

__all__ = [
    "ChatService",
    "ChatError",
    "ChatNotConfigured",
    "ChatLocked",
    "ChatCallFailed",
    "SESSION_TITLE_MAX_CHARS",
    "AUTO_TITLE_CHARS",
    "MAX_REFS",
    "GRANT_CATEGORIES",
    "tool_grant_category",
]

#: 会话名长度上限（重命名与自动标题共用同一口径，与助手域一致）。
SESSION_TITLE_MAX_CHARS = 100

#: 自动标题取首条用户消息的前 N 个字符。
AUTO_TITLE_CHARS = 20

#: 一条消息最多携带的 @引用 文件数。
MAX_REFS = 10

#: 发给模型的单次调用超时（秒）。同步等待的问答不能无限挂住。
CALL_TIMEOUT_S = 120.0

#: 会话级临时授权的操作类别（D-G）：write = 写类工具（write/mkdir/move/delete），
#: run = 命令执行（fs_run）。两类分开授权。
GRANT_CATEGORIES: frozenset[str] = frozenset({"write", "run"})


def tool_grant_category(tool_name: str) -> str:
    """工具所属的授权类别。只应对需审批工具调用（读类工具不参与授权）。"""
    return "run" if tool_name in RUN_TOOLS else "write"


async def _always_allow(_tool: str, _action: str, _target: str) -> bool:
    """会话级授权生效时的审批回调：直接放行（审批已被用户按类别预先给出）。"""
    return True

_ROLE_PROMPT = """你是 Workerbee（本机多智能体工作流编排工具）对话页的助手。

你可以使用的工具：
- 列目录用 fs_list，读文件用 fs_read（单文件最多读 100KB，超出会截断并标注）。
- 写文件 fs_write、建目录 fs_mkdir、移动/改名 fs_move、删除 fs_delete
  （只能删文件或空目录，没有递归删除）。
- 执行命令用 fs_run，可指定工作目录与超时（默认 60 秒，上限 300 秒），
  标准输出与标准错误合并返回，过长会截断并标注。

规则：
- 所有路径都是工作区内的相对路径；越出工作区的路径会被直接拒绝，不要尝试。
- 写、建目录、移动、删除和执行命令在执行前会请用户批准；被拒绝或等待超时
  时如实告知用户，不要假装操作成功。用户可能已对本会话的某类操作勾选
  「不再询问」，那时同类操作会直接执行，结果照样如实返回。
- 命令经 shell 执行，威力与用户在终端里亲手输入相同（能看到工作区之外的
  文件）。危险命令（rm、sudo、dd、mkfs、向工作区外重定向等）任何时候都
  需要逐次批准，会话授权也不豁免——主动避开这类命令，确有必要时先向用户
  说明风险再发起。
- 覆盖已存在的文件前，先用 fs_read 读取它，把返回的 mtime 作为
  expected_mtime 传给 fs_write。
- 不要编造你没读过的文件内容或没执行过的命令结果；操作失败时把失败原因
  如实告诉用户。
- 用大白话中文回答，先给结论。"""

_ROLE_PROMPT_NO_TOOLS = """你是 Workerbee（本机多智能体工作流编排工具）对话页的助手。

当前后端不支持文件操作工具，你只能纯对话：回答问题、解释概念、
按用户贴出的内容分析与建议。不要声称你读过或改过任何文件。
用大白话中文回答，先给结论。"""

_NO_TOOLS_NOTE = "当前后端不支持文件操作，仅纯对话"


# ---------------------------------------------------------------------------
# 错误：服务层据此翻译成 HTTP，文案给最终用户看
# ---------------------------------------------------------------------------


class ChatError(RuntimeError):
    """chat 用例失败的基类。消息是大白话中文，不含内部类型名。"""

    def __init__(self, detail: str, *, hint: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class ChatNotConfigured(ChatError):
    """对话没有可用的模型后端（未配凭据、凭据失效、助手也未启用等）。"""


class ChatLocked(ChatError):
    """凭据库未解锁，读不到模型密钥。"""


class ChatCallFailed(ChatError):
    """模型调用失败（含超时与全部后端耗尽）。"""


# ---------------------------------------------------------------------------
# 后端装配
# ---------------------------------------------------------------------------

#: 与助手同一签名：给定配置与凭据引用，产出一个后端实例。测试用它注入假后端。
BackendFactory = Callable[[AssistantConfig, Any, Any], LLMBackend]


def _default_backend_factory(
    config: AssistantConfig, credential: Any, secrets: Any
) -> LLMBackend:
    """生产路径：按（助手配置的）``api_protocol`` 选 openai_compat 或
    anthropic 后端，密钥走 secret_locator（AUTH-02）。"""
    kind = "anthropic" if config.api_protocol == "anthropic" else "openai_compat"
    return BackendConfig(
        kind=kind,
        name="chat",
        model=config.model_override or credential.default_model or "",
        base_url=credential.base_url,
        secret_locator=credential.secret_locator,
    ).build(secrets=secrets)


class ChatService:
    """chat 用例的编排核心。

    ``secret_resolver`` 是「取当前凭据库」的回调而不是凭据库本身：
    解锁／锁定发生在引擎生命周期中段，持死引用会看不到解锁后的库。
    ``approval_gateway`` 未注入时，写类工具一律拒绝执行（deny by default）。
    """

    def __init__(
        self,
        *,
        store: Store,
        notifier: Any = None,
        secret_resolver: Callable[[], Any] | None = None,
        redactor: Callable[[Any], Any] | None = None,
        approval_gateway: Any = None,
        backend_factory: BackendFactory | None = None,
        call_timeout: float = CALL_TIMEOUT_S,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        approval_poll_interval: float = 0.5,
        window_rounds: int = DEFAULT_WINDOW_ROUNDS,
        window_chars: int = DEFAULT_WINDOW_CHARS,
        run_timeout_cap: float = RUN_TIMEOUT_CAP_S,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self._secret_resolver = secret_resolver or (lambda: None)
        self.redactor = redactor
        self.approval_gateway = approval_gateway
        self.backend_factory = backend_factory
        self.call_timeout = call_timeout
        self.max_tool_rounds = max_tool_rounds
        self.approval_poll_interval = approval_poll_interval
        self.window_rounds = window_rounds
        self.window_chars = window_chars
        self.run_timeout_cap = run_timeout_cap
        self._router_cache: tuple[str, LLMRouter] | None = None

    # ------------------------------------------------------------------
    # 会话 CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        title: str = "",
        workspace_id: str,
        credential_ref: str | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self.store.workspaces.get(workspace_id)
        if workspace is None:
            raise ChatError(
                f"工作区不存在：{workspace_id}",
                hint="先在工作区切换器里确认当前工作区，或刷新页面",
            )
        if credential_ref:
            credential = await self.store.registry.get_credential(credential_ref)
            if credential is None:
                raise ChatError("指定的模型凭据不存在", hint="到 注册表 → 凭据 里确认它还在")
        session_id = new_id()
        return await self.store.chat.create_session(
            session_id,
            workspace_id=workspace_id,
            title=title.strip()[:SESSION_TITLE_MAX_CHARS],
            credential_ref=credential_ref or None,
            model_override=(model_override or "").strip() or None,
        )

    async def list_sessions(
        self, *, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.store.chat.list_sessions(workspace_id=workspace_id)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._require_session(session_id)

    async def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        await self._require_session(session_id)
        title = title.strip()
        if not title:
            raise ChatError("会话名不能为空")
        if len(title) > SESSION_TITLE_MAX_CHARS:
            raise ChatError(f"会话名最长 {SESSION_TITLE_MAX_CHARS} 个字符")
        await self.store.chat.update_session(session_id, title=title)
        session = await self.store.chat.get_session(session_id)
        assert session is not None  # _require_session 刚查过
        return session

    async def delete_session(self, session_id: str) -> None:
        await self._require_session(session_id)
        await self.store.chat.delete_session(session_id)

    # ------------------------------------------------------------------
    # 会话级临时授权（D-G）
    # ------------------------------------------------------------------

    async def grant_session(self, session_id: str, category: str) -> dict[str, Any]:
        """授予本会话某类操作的临时授权（之后同类操作跳过逐次审批）。

        授权只对当前会话有效（落在 chat_session.grants 列，删会话即消失）；
        命中危险模式的命令不受豁免（见 ``chat.run.dangerous_reason``）。
        """
        return await self._set_grant(session_id, category, granted=True)

    async def revoke_session_grant(self, session_id: str, category: str) -> dict[str, Any]:
        """撤销本会话某类操作的临时授权（恢复逐次审批）。"""
        return await self._set_grant(session_id, category, granted=False)

    async def _set_grant(
        self, session_id: str, category: str, *, granted: bool
    ) -> dict[str, Any]:
        category = (category or "").strip()
        if category not in GRANT_CATEGORIES:
            raise ChatError(
                "不认识的操作类别",
                hint="只有 write（写文件类）和 run（执行命令）两类授权",
            )
        session = await self._require_session(session_id)
        grants = set(session.get("grants") or [])
        if (category in grants) == granted:
            return session  # 幂等：状态已是目标态，不重复留痕
        if granted:
            grants.add(category)
        else:
            grants.discard(category)
        await self.store.chat.set_grants(session_id, sorted(grants))
        await self.store.events.append(
            scope=EventScope.CHAT,
            type=EventType.CHAT_GRANT_CHANGED,
            actor=EventActor.USER,
            scope_id=session_id,
            payload={"category": category, "granted": granted, "grants": sorted(grants)},
        )
        # 授权状态在 Chat 页工具栏可见：推一下让别的页面（审批中心）的改动同步过去。
        self._publish("chat_session", {"session_id": session_id})
        updated = await self.store.chat.get_session(session_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # 消息读取（线性视图：根 → 叶的分支路径）
    # ------------------------------------------------------------------

    async def list_messages(
        self, session_id: str, *, leaf_id: str | None = None
    ) -> dict[str, Any]:
        """当前分支的线性消息序列。``leaf_id`` 缺省时取最新叶节点。"""
        await self._require_session(session_id)
        nodes = await self.store.chat.list_nodes(session_id)
        if not nodes:
            return {"messages": [], "leaf_id": None}
        if leaf_id is None:
            leaf = await self.store.chat.latest_leaf(session_id)
            leaf_id = leaf["node_id"] if leaf else None
        if leaf_id is None:
            return {"messages": [], "leaf_id": None}
        try:
            path = branch_path(nodes, leaf_id)
        except KeyError:
            raise ChatError(
                "指定的分支末端不存在（可能已被删除）", hint="刷新对话后重试"
            ) from None
        return {"messages": path, "leaf_id": leaf_id}

    # ------------------------------------------------------------------
    # 发送（流式 + 工具循环）
    # ------------------------------------------------------------------

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        parent_id: str | None = None,
        refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        session = await self._require_session(session_id)
        if session.get("closed"):
            raise ChatError("这个会话已关闭，不能再发消息")
        workspace = await self.store.workspaces.get(session["workspace_id"])
        if workspace is None:
            raise ChatError(
                "会话所属的工作区已不存在",
                hint="新建一个属于现有工作区的会话",
            )
        root_dir = str(workspace["root_dir"])
        # 会话级临时授权（D-G）：发送开始时的快照；授权变更走 grant/revoke 方法。
        grants = set(session.get("grants") or [])

        # 用户消息先脱敏再入库、再发给模型（可能粘贴含密钥的内容，AUTH-02）。
        safe_text = self._redact(content)
        full_text = self._inject_refs(root_dir, safe_text, refs)
        safe_full = self._redact(full_text) if full_text != safe_text else safe_text

        parent = await self._resolve_parent(session_id, parent_id)
        user_node = await self.store.chat.append_node(
            node_id=new_id(),
            session_id=session_id,
            parent_id=parent,
            role="user",
            content=safe_full,
        )

        router, degraded_notes = await self._require_backend(session)
        supports_tools = router.supports_tools()
        tools = CHAT_TOOL_SPECS if supports_tools else None
        if not supports_tools:
            degraded_notes.append(_NO_TOOLS_NOTE)

        # 上下文：根→叶路径（含刚落下的用户节点）→ 滑窗 → 工具序列清理。
        nodes = await self.store.chat.list_nodes(session_id)
        path = branch_path(nodes, user_node["node_id"])
        context_messages, dropped = build_context(
            path, max_rounds=self.window_rounds, max_chars=self.window_chars
        )
        system = self._system_prompt(workspace, supports_tools)
        from ..data.llm import LLMMessage

        messages = [LLMMessage(role="system", content=system), *context_messages]

        leaf_holder = [user_node["node_id"]]

        async def persist(**kwargs: Any) -> dict[str, Any]:
            content = kwargs.get("content")
            if isinstance(content, str):
                kwargs["content"] = self._redact(content)
            reasoning = kwargs.get("reasoning")
            if isinstance(reasoning, str):
                kwargs["reasoning"] = self._redact(reasoning)
            node = await self.store.chat.append_node(
                session_id=session_id, parent_id=leaf_holder[0], **kwargs
            )
            leaf_holder[0] = node["node_id"]
            return node

        async def execute(call: LLMToolCall, via_node_id: str) -> ToolOutcome:
            gate = None
            approval_path = "approval"
            if call.name in APPROVAL_TOOLS:
                category = tool_grant_category(call.name)
                # 危险命令永远逐次审批，会话级授权不豁免（D-G）。
                dangerous = False
                if call.name in RUN_TOOLS:
                    reason = dangerous_reason(str(call.arguments.get("command", "")))
                    dangerous = reason is not None
                if category in grants and not dangerous:
                    gate = _always_allow
                    approval_path = "session_grant"
                elif self.approval_gateway is not None:
                    note = f"危险命令：{reason}" if dangerous else None
                    gate = self._make_approval_gate(session_id, via_node_id, note=note)
                # 无审批通道且无会话授权：gate=None → 工具层如实拒绝（deny by default）
            outcome = await execute_tool(
                call,
                workspace_root=root_dir,
                approval_gate=gate,
                run_timeout_cap=self.run_timeout_cap,
            )
            if outcome.effect is not None:
                payload = {
                    "path": outcome.effect["path"],
                    "detail": outcome.effect.get("detail"),
                    "tool": call.name,
                    "node_id": via_node_id,
                }
                payload.update(outcome.effect.get("extra") or {})
                if call.name in APPROVAL_TOOLS:
                    # 授权路径留痕：逐次批准还是会话级授权，事后可查。
                    payload["approval"] = approval_path
                await self.store.events.append(
                    scope=EventScope.CHAT,
                    type=EventType(outcome.effect["type"]),
                    actor=EventActor.AI,
                    scope_id=session_id,
                    payload=payload,
                )
            return outcome

        try:
            outcome = await run_tool_loop(
                router=router,
                messages=messages,
                tools=tools,
                timeout=self.call_timeout,
                max_rounds=self.max_tool_rounds,
                persist=persist,
                execute=execute,
                on_chunk=lambda node_id, kind, text: self._publish_chunk(
                    session_id, node_id, kind, text
                ),
            )
        except LLMError as exc:
            raise ChatCallFailed(
                f"模型调用失败：{exc}",
                hint="模型服务可能暂时不可用；稍后再试，或检查凭据与 Base URL 是否还有效",
            ) from exc

        final = outcome.final
        assert final is not None  # run_tool_loop 缺少终帧时会抛错
        backend_name = final.backend or "unknown"
        fell_back = final.fallback_from is not None
        if not final.streamed:
            degraded_notes.append("该后端不支持流式输出，本次回复是一次性完整返回的")
        degraded = fell_back or bool(degraded_notes)

        # 自动标题：会话还没有名字时，取首条用户消息的前 20 字。
        if not session.get("title"):
            await self.store.chat.update_session(
                session_id, title=safe_text[:AUTO_TITLE_CHARS]
            )

        if fell_back:
            await self.store.events.append(
                scope=EventScope.CHAT,
                type=EventType.CHAT_BACKEND_DEGRADED,
                actor=EventActor.AI,
                scope_id=session_id,
                payload={
                    "fallback_from": final.fallback_from,
                    "used": backend_name,
                    "reasons": list(final.degraded_reasons),
                },
            )
        reply = outcome.reply or outcome.nodes[-1]
        usage = final.usage
        await self.store.events.append(
            scope=EventScope.CHAT,
            type=EventType.CHAT_MESSAGE,
            actor=EventActor.AI,
            scope_id=session_id,
            payload={
                "node_id": reply["node_id"],
                "backend": backend_name,
                "model": final.model,
                "tokens_in": usage.input_tokens if usage else None,
                "tokens_out": usage.output_tokens if usage else None,
                "degraded": degraded,
                "supports_tools": supports_tools,
                "tool_rounds": outcome.tool_rounds,
                "hit_round_limit": outcome.hit_round_limit,
                "window_dropped": dropped,
            },
        )

        if self.notifier is not None:
            self._publish(
                "chat_message",
                {"session_id": session_id, "node_id": reply["node_id"]},
            )

        return {
            "user_node": user_node,
            "nodes": outcome.nodes,
            "reply": reply,
            "dropped": dropped,
            "degraded": degraded,
            "degraded_reasons": degraded_notes + list(final.degraded_reasons),
            "supports_tools": supports_tools,
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

    async def _require_session(self, session_id: str) -> dict[str, Any]:
        session = await self.store.chat.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def _resolve_parent(self, session_id: str, parent_id: str | None) -> str | None:
        if parent_id is None:
            leaf = await self.store.chat.latest_leaf(session_id)
            return leaf["node_id"] if leaf else None
        if parent_id == "":
            # 显式挂在树根（第一条消息的「改问」）：None 已被「缺省挂最新叶」占用，
            # 空串是显式表达「从头分叉」的约定。
            return None
        node = await self.store.chat.get_node(parent_id)
        if node is None or node["session_id"] != session_id or node.get("deleted_at"):
            raise ChatError(
                "指定的 parent_id 不是这个会话里的有效节点",
                hint="刷新对话后重试；分叉操作请从当前可见的消息发起",
            )
        return parent_id

    def _inject_refs(
        self, root_dir: str, content: str, refs: Sequence[str]
    ) -> str:
        """把 ``@路径`` 引用的文件内容拼进用户消息（read 同款 confinement 与截断）。

        读不到的引用**如实写在消息里**（「读取失败：原因」），不静默丢弃——
        模型与用户看到的是同一份正文。
        """
        unique = [r for r in dict.fromkeys(refs) if isinstance(r, str) and r.strip()]
        if not unique:
            return content
        blocks = [content]
        for ref in unique[:MAX_REFS]:
            ref = ref.strip()
            try:
                result = fs.read_file(root_dir, ref)
            except fs.FSError as exc:
                blocks.append(f"\n\n【引用文件 {ref}】读取失败：{exc.detail}")
                continue
            note = "（文件过大，只注入前 100KB）" if result["truncated"] else ""
            blocks.append(
                f"\n\n【引用文件 {result['path']}】{note}\n{result['content']}"
            )
        return "".join(blocks)

    def _system_prompt(self, workspace: dict[str, Any], supports_tools: bool) -> str:
        base = _ROLE_PROMPT if supports_tools else _ROLE_PROMPT_NO_TOOLS
        return (
            f"{base}\n\n当前工作区：「{workspace['name']}」"
            f"（根目录 {workspace['root_dir']}）。"
        )

    async def _require_backend(
        self, session: dict[str, Any]
    ) -> tuple[LLMRouter, list[str]]:
        """解析本会话的后端链。各种「不可用」都在这里明确报出。

        会话自带 ``credential_ref`` 优先；为空则回落到助手配置
        （此时要求助手已启用——否则等于绕过助手的开关）。
        """
        config = await load_config(self.store.db)
        session_ref = session.get("credential_ref") or None
        cred_id = session_ref or config.credential_ref
        if session_ref is None and not config.enabled:
            raise ChatNotConfigured(
                "对话还没有配置模型：既没有为会话选凭据，内置助手也未启用",
                hint="新建会话时选择一条模型凭据；或到助手面板的设置里启用助手并选一条凭据",
            )
        if not cred_id:
            raise ChatNotConfigured(
                "还没有给对话选模型凭据",
                hint="到 注册表 → 凭据 建一条含 Base URL 和 Key 的凭据，"
                "再在会话或助手设置里选它",
            )
        credential = await self.store.registry.get_credential(cred_id)
        if credential is None:
            raise ChatNotConfigured(
                "配置指向的凭据已经不存在了", hint="重新选择一条凭据"
            )
        if credential.revoked:
            raise ChatNotConfigured(
                f"凭据「{credential.label}」已被撤销",
                hint="恢复该凭据，或换一条",
            )
        if not credential.secret_locator:
            raise ChatNotConfigured(
                f"凭据「{credential.label}」没有密钥内容（harness 登录态不能给对话用）",
                hint="建一条含 Base URL 和 Key 的凭据再选它",
            )
        model = session.get("model_override") or config.model_override or credential.default_model
        if not model:
            raise ChatNotConfigured(
                f"凭据「{credential.label}」没有默认模型名，不知道该用哪个模型",
                hint="给会话或凭据补一个模型名",
            )
        secrets = self._secret_resolver()
        if secrets is None:
            raise ChatLocked(
                "凭据库还没有解锁，读不到模型密钥",
                hint="用带口令的方式重启内核（--passphrase 或 WORKERBEE_PASSPHRASE），"
                "解锁后再试",
            )

        effective = config.model_copy(
            update={"credential_ref": cred_id, "model_override": model}
        )
        cache_key = json.dumps(
            {
                "ref": cred_id,
                "locator": credential.secret_locator,
                "base_url": credential.base_url,
                "model": model,
                "api_protocol": config.api_protocol,
                "factory": id(self.backend_factory),
            },
            sort_keys=True,
        )
        if self._router_cache is not None and self._router_cache[0] == cache_key:
            return self._router_cache[1], []

        await self.aclose()
        factory = self.backend_factory or _default_backend_factory
        backend = factory(effective, credential, secrets)
        router = (
            backend if isinstance(backend, LLMRouter) else LLMRouter([backend], name="chat")
        )
        self._router_cache = (cache_key, router)
        return router, []

    def _make_approval_gate(
        self, session_id: str, via_node_id: str, *, note: str | None = None
    ):
        """写/执行类工具的审批回调：登记审批 → 推「等待审批」状态 → 轮询决定。

        合成绑定（``chat:<session_id>``）：chat 的审批不挂在任何任务/尝试上，
        决定由本循环直接从审批表读取，不需要回注 harness 会话
        （``Engine._deliver_approval`` 对这个前缀直通返回）。
        ``note`` 是给审批卡片看的补充说明（如危险命令的威胁原因）。
        """
        gateway = self.approval_gateway

        async def gate(tool_name: str, action: str, target: str) -> bool:
            shown_action = f"{action}（{tool_name}）"
            if note:
                shown_action = f"{shown_action} —— {note}"
            approval = await gateway.request(
                approval_id=new_id(),
                task_id=f"chat:{session_id}",
                stage_id=f"chat:{session_id}",
                attempt_id=f"chat:{session_id}",
                revision_seq=0,
                node_id=None,
                action=shown_action,
                target=target,
                risk="write" if tool_name not in RUN_TOOLS else "execute",
                tool_name=tool_name,
            )
            self._publish_status(
                session_id,
                via_node_id,
                "waiting_approval",
                f"{action}：{target}",
                approval_id=approval.approval_id,
            )
            deadline = time.monotonic() + float(gateway.timeout_seconds)
            while True:
                row = await self.store.approvals.get(approval.approval_id)
                if row is not None and not row.is_pending():
                    approved = row.status == ApprovalStatus.APPROVED
                    self._publish_status(
                        session_id,
                        via_node_id,
                        "approval_decided",
                        "已批准" if approved else "已拒绝",
                        approval_id=approval.approval_id,
                    )
                    return approved
                if time.monotonic() >= deadline:
                    # 超时按拒绝处理（与网关 deny_pause 同一语义）。
                    self._publish_status(
                        session_id,
                        via_node_id,
                        "approval_decided",
                        "等待超时，按拒绝处理",
                        approval_id=approval.approval_id,
                    )
                    return False
                await asyncio.sleep(self.approval_poll_interval)

        return gate

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.publish(Notification(kind=kind, payload=payload))
        except Exception:  # noqa: BLE001 - 推送通道的故障不该打断一次问答
            pass

    def _publish_chunk(self, session_id: str, node_id: str, kind: str, text: str) -> None:
        """流式增量推送。推送是加速器不是事实源：失败不阻断主流程。"""
        self._publish(
            "chat_chunk",
            {
                "session_id": session_id,
                "node_id": node_id,
                "kind": kind,
                "text": self._redact(text),
            },
        )

    def _publish_status(
        self,
        session_id: str,
        node_id: str,
        status: str,
        detail: str,
        *,
        approval_id: str | None = None,
    ) -> None:
        self._publish(
            "chat_status",
            {
                "session_id": session_id,
                "node_id": node_id,
                "status": status,
                "detail": detail,
                "approval_id": approval_id,
            },
        )

    def _redact(self, value: Any) -> Any:
        if self.redactor is None or value is None:
            return value
        return self.redactor(value)
