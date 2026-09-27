"""内核与适配器之间**唯一**的接线点（架构设计 v0.02 §3、§8.1、§8.2、§8.3、§12）。

``core`` 只认 ``core/runtime/ports.py`` 里的 ``HarnessPort``；``adapters`` 只认
JSON-RPC 的六组契约。两边对「一次会话」的想象并不完全一样，差异全部收在这一个文件里：

映射表（三条，改动都要在这里看得见）
------------------------------------

1. **harness_id → 适配器进程**：``HarnessRegistration.adapter_id`` 查启动命令；
   一个 harness **一个** ``AdapterProcess``（懒启动、复用），会话是进程内的。
2. **session_ref → harness_id**：适配器回报的 ``SessionInfo`` 是会话状态的唯一权威；
   这里只留一张定位表，不复制状态机。
3. **适配器事件 → 内核 payload**：``AdapterEvent.kind`` 原样传（``output`` /
   ``background_task_started`` / ``usage`` / ``error`` …），但内核读的字段名与
   适配器上报的字段名有几处对不上，翻译如下（不改数据，只补别名）：

   ==========================  ============================  ==========================
   事件 kind                   适配器上报                    内核读取（Scheduler.on_event）
   ==========================  ============================  ==========================
   ``output``                  ``text``                      ``payload["text"]``
   ``background_task_*``       ``task_id``                   ``payload["id"]``
   ``usage``                   ``cache_read_input_tokens``   ``cache_read_tokens``
   ``usage``                   ``cache_creation_input_tokens`` ``cache_write_tokens``
   ``usage``                   ``cost_usd`` / ``total_cost_usd`` ``cost_estimate``
   ``error``                   ``text`` + ``error_kind``     ``payload["message"]`` / ``["kind"]``
   ==========================  ============================  ==========================

   不翻译的话，后台工作永远不会被登记、用量永远记为「未知」、错误详情永远为空——
   都是「看起来在工作」的静默失败，比报错更难查。

降级路径（D-07、D-09）
----------------------

适配器如实返回 ``NOT_SUPPORTED`` 时，这里**不抛异常**，而是给出内核能用的保守值，
由内核走降级：``pause`` → ``False``（协作停止）、``checkpoint`` → ``None``（从头重跑
本阶段）、``compact`` → ``{"ok": False, ...}``（不得声称已整理）、``session_alive`` →
``False``（查不到就不说活着）。

凭据（AUTH-02）
--------------

``auth_binding`` → ``CredentialRef`` → SecretStore 取出明文，**只**放进
``HarnessConfig.credential`` 随 ``session.create`` 传给子进程。本模块的任何日志、
异常消息、事件 payload 都不含凭据值；需要指代时只用 locator。
standalone 模式没有 core 的凭据注册表：绑定到凭据的 harness 在那里会明确失败，
而不是悄悄用「本机登录态」顶上（那等于换了一份身份，AUTH-02 不允许猜）。

独立进程（standalone）
----------------------

``supervisor`` 持有 harness 进程却**没有** core 的数据库，所以它要 ``store=None`` +
``standalone=True``：注册信息改从构造参数 ``registrations``（``harness_id`` →
``HarnessRegistration``）取。除了注册信息的来源，其余行为完全一致——同一个路由器、
同一套会话定位表、同一套降级与存活判据。
``standalone=False`` 却没给 ``store`` 会**当场抛错**：静默降级成 standalone 会让
「注册表里查不到」这种事故伪装成「配置没问题」，是最难查的一类错。

子进程环境（注入优先级）
------------------------

``{**os.environ, **adapter_env, **registration.env_template}``——系统环境打底（PATH、
HOME 等必须留着），``adapter_env`` 是组合根的全局注入（supervisor 用它给 mock 适配器
下发剧本文件路径），注册表里的显式配置优先级最高。

建会话时的四处翻译（``create_session``）
---------------------------------------

======================  ==========================================================
``HarnessPort`` 参数    适配器侧的位置
======================  ==========================================================
``initial_input``       ``CreateSessionRequest.extra["prompt"]``（claude/kimi/mock 都读它）
``permission_mode``     ``CreateSessionRequest.permission_mode`` → ``--permission-mode`` 等
``system_prompt``       ``CreateSessionRequest.system_prompt``
``cwd``                 ``HarnessConfig.cwd``（未给时取注册表里的值）
======================  ==========================================================

``accepted_initial_input`` 的结论来自适配器回包；老适配器不带该字段时，只在
「给了首轮输入」且「该适配器自述 ``interact=False``」时判 True，其余一律 False
（不猜：猜 True 而实际没交付，阶段会永远等一个不会开始的任务）。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ...core.domain.registry import AuthMode, HarnessRegistration
from ...core.domain.task import Attempt, TaskStage
from ...core.runtime.ports import InputKind, SessionCaps, SessionHandle
from ..sdk.contract import (
    AdapterEvent,
    CreateSessionRequest,
    HarnessConfig,
    PermissionRequest,
    SessionInfo,
)
from ..sdk.protocol import (
    METHODS,
    AdapterError,
    ErrorCode,
    EventKind,
    JsonRpcError,
)
from .client import AdapterProcess, AdapterSpawnError

__all__ = [
    "HarnessRouter",
    "BUILTIN_ADAPTERS",
    "ADAPTER_ALIASES",
    "child_env",
    "EventCallback",
    "PermissionCallback",
    "ExitCallback",
    "SessionEndedCallback",
    "LogCallback",
]

EventCallback = Callable[..., Awaitable[None]]
"""事件回调。两种形态都支持，按回调声明的参数个数分派：

- ``(event: AdapterEvent)``——**原始事件**。``host/remote.py`` 的 ``EventCallback``、
  ``app.py`` 与 ``supervisor/server.py`` 的回调都是这个形态，它们自己翻译字段；
- ``(session_ref, kind, payload)``——**内核形态**，``payload`` 已按模块文档的翻译表
  补好别名，可直接交给 ``Scheduler.on_event``。

两种都留是因为两边的消费者都已经存在：只认一种就会让另一条路径静默收不到事件
（「看起来在工作」的静默失败）。参数个数判不出来时（``*args``、内省失败）按
**原始事件**处理——那是本项目里更保守的一种约定（不替调用方翻译字段）。
"""

PermissionCallback = Callable[[PermissionRequest], Awaitable[None]]
ExitCallback = Callable[[str, int | None, str], Awaitable[None]]
"""(harness_id, exit_code, stderr_tail) → 上层走对账（REC-02/03）。"""

SessionEndedCallback = Callable[[str, bool, str | None], Awaitable[None]]
"""``(session_ref, ok, detail)`` → 组合根转给 ``Scheduler.on_session_ended``。

会话结束**不**走 ``on_event``：内核的完成判据（RUN-06）要的是这个专门回调。
"""

LogCallback = Callable[[str], None]


#: 内置适配器的启动命令。键是 ``HarnessRegistration.adapter_id``。
BUILTIN_ADAPTERS: dict[str, list[str]] = {
    "claude_code": [sys.executable, "-m", "workerbee.adapters.claude_code.main"],
    "kimi_code": [sys.executable, "-m", "workerbee.adapters.kimi_code.main"],
    "mock": [sys.executable, "-m", "workerbee.adapters.mock.main"],
}

#: 适配器自述（``manifest.adapter_id``）用的是连字符；注册表里两种写法都可能出现。
ADAPTER_ALIASES: dict[str, str] = {
    "claude-code": "claude_code",
    "kimi-code": "kimi_code",
}

_TERMINAL_STATES = frozenset({"ended", "lost"})

#: 看起来像凭据的环境变量名。命中且值不是 ``$`` 占位符时给出告警（AUTH-02）。
_SECRETISH_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _normalize_adapter_id(adapter_id: str) -> str:
    key = (adapter_id or "").strip().lower()
    return ADAPTER_ALIASES.get(key.replace("_", "-"), key.replace("-", "_"))


def child_env(
    adapter_env: dict[str, str] | None = None,
    registration_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """合成适配器子进程的环境变量（优先级见模块文档）。

    ``os.environ`` 打底 → ``adapter_env``（组合根全局注入）→ ``registration_env``
    （注册表里针对这个 harness 的显式配置，优先级最高）。
    """
    return {**os.environ, **(adapter_env or {}), **(registration_env or {})}


def _callback_shape(callback: Callable[..., Any] | None) -> str:
    """``"raw"``（``(event)``）还是 ``"kernel"``（``(session_ref, kind, payload)``）。"""
    if callback is None:
        return "raw"
    try:
        params = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - 内建/无法内省的可调用对象
        return "raw"
    positional = 0
    for param in params:
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            positional += 1
        elif param.kind is param.VAR_POSITIONAL:
            # *args：判不出个数。按原始事件处理，不做猜测性的翻译。
            return "raw"
    return "kernel" if positional >= 3 else "raw"


def _looks_like_credential(name: str, value: str | None) -> bool:
    """环境变量名像凭据、值又是字面量（不是 ``$`` 占位符）时给出告警（AUTH-02）。"""
    if not value or value.startswith("$"):
        return False
    return any(marker in name.upper() for marker in _SECRETISH_ENV_MARKERS)


def _with_initial_input(
    extra: dict[str, Any] | None, initial_input: str | None
) -> dict[str, Any] | None:
    """把首轮输入放进 ``extra["prompt"]``。

    键名是**协议约定**：``extra_prompt()``（sdk/cli.py）与各适配器的 build_argv
    都按 ``prompt | input | user_input`` 读取本轮输入，其中 claude 只认 prompt。
    调用方显式给了同名键时以调用方为准（不覆盖），避免把上层精心准备的内容顶掉。
    """
    if initial_input is None:
        return extra
    merged = dict(extra or {})
    merged.setdefault("prompt", initial_input)
    return merged


@dataclass
class _SessionRecord:
    """路由器的会话定位表。状态以适配器回报为准，这里不做推断。"""

    session_ref: str
    harness_id: str
    attempt_id: str | None = None
    persist_locator: str | None = None
    state: str = "alive"
    model_name: str | None = None
    used_resume: bool = False
    cwd: str | None = None
    ended_reported: bool = False
    """结束回调只发一次：进程退出的补报不得覆盖已经报过的正常结束。"""


class HarnessRouter:
    """``HarnessPort`` 的适配器侧实现。"""

    def __init__(
        self,
        store: Any | None,
        *,
        adapter_commands: dict[str, list[str]] | None = None,
        registrations: dict[str, HarnessRegistration] | None = None,
        adapter_env: dict[str, str] | None = None,
        secret_store: Any | None = None,
        on_event: EventCallback | None = None,
        on_permission: PermissionCallback | None = None,
        on_exit: ExitCallback | None = None,
        on_session_ended: SessionEndedCallback | None = None,
        log: LogCallback | None = None,
        call_timeout: float = 60.0,
        alive_timeout: float = 5.0,
        close_grace: float = 5.0,
        standalone: bool = False,
    ) -> None:
        if store is None and not standalone:
            # 静默把 store=None 当成 standalone，会让「注册表里查不到」伪装成
            # 「配置没问题」——错在装配，就该在装配时报错。
            raise ValueError(
                "HarnessRouter 需要 store（core 的数据库）才能查 harness 注册信息；"
                "独立进程（如 supervisor）请显式传 standalone=True，"
                "并用 registrations 参数提供注册表"
            )
        self.store = store
        """需要 ``store.registry.list_harnesses()`` / ``get_harness()`` /
        ``get_credential()``。这里不 import 数据层，由组合根注入。
        ``None`` 只在 ``standalone=True`` 时合法。"""

        self.standalone = standalone
        # 保留传入的那个 dict 对象本身（而不是拷一份）：组合根会在后面往里加
        # harness（supervisor 的 harness.ensure 就是这么做的），拷贝会让那些新增的
        # 注册信息永远到不了这里。
        self.registrations: dict[str, HarnessRegistration] = (
            registrations if registrations is not None else {}
        )
        """standalone 模式下的 harness 注册表（``harness_id`` → 注册信息），
        与 ``adapter_commands`` 并列：supervisor 把 core 带来的注册信息放在这里。
        可以改（同一个 dict 对象被组合根持有），查找时按需读取。"""

        self.adapter_env: dict[str, str] = dict(adapter_env or {})
        """额外注入给**所有**适配器子进程的环境变量，优先级见模块文档。"""

        self.secret_store = secret_store
        """``await get(locator) -> dict | None``。None 表示本机登录态，不需要凭据。"""

        self._commands = {**BUILTIN_ADAPTERS, **(adapter_commands or {})}
        self._on_event = on_event
        self._event_shape = _callback_shape(on_event)
        """``on_event`` 要原始事件还是内核三元组——见 ``EventCallback``。"""
        self._on_permission = on_permission
        self._on_exit = on_exit
        self._on_session_ended = on_session_ended
        self._log = log
        self._call_timeout = call_timeout
        self._alive_timeout = alive_timeout
        self._close_grace = close_grace

        self._procs: dict[str, AdapterProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _SessionRecord] = {}
        self._checkpoints: dict[str, dict[str, Any]] = {}
        """checkpoint token → 适配器给的完整断点。token 对内核是不透明字符串，
        恢复时要还原成适配器认的那个结构（D-07 第二档）。"""
        self._registrations: dict[str, HarnessRegistration] = {}
        self._start_errors: dict[str, str] = {}
        self._stopped = False

    @classmethod
    async def create(
        cls,
        store: Any | None = None,
        *,
        standalone: bool = False,
        registrations: dict[str, HarnessRegistration] | None = None,
        adapter_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> "HarnessRouter":
        """构造并启动。组合根统一走这个入口（签名与 ``HarnessPort`` 的装配对齐）。

        ``store=None`` 只在 ``standalone=True`` 时合法；``standalone=False`` 且
        ``store is None`` 会**明确抛错**，不会静默降级成 standalone。
        """
        router = cls(
            store,
            standalone=standalone,
            registrations=registrations,
            adapter_env=adapter_env,
            **kwargs,
        )
        await router.start()
        return router

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """按注册表拉起需要的适配器子进程。

        单个 harness 起不来**不**阻断启动：如实记在 ``_start_errors`` 里并告警，
        真要用到它时 ``create_session`` 会抛出明确的 ``HARNESS_UNAVAILABLE``。
        「一个坏适配器拖垮整机」比「一个坏适配器不可用」危险得多。
        """
        self._stopped = False
        if self._on_event is not None:
            self._emit_log(
                f"[router] on_event 回调形态："
                f"{'原始 AdapterEvent' if self._event_shape == 'raw' else '内核三元组 (session_ref, kind, payload)'}"
            )
        for reg in await self._registered_harnesses():
            self._registrations[reg.harness_id] = reg
            if not reg.enabled:
                self._emit_log(f"[router] harness {reg.harness_id} 已停用，跳过启动")
                continue
            try:
                await self._ensure_process(reg.harness_id)
            except Exception as exc:  # noqa: BLE001 - 逐个降级，不牵连其余 harness
                detail = f"{type(exc).__name__}: {exc}"
                self._start_errors[reg.harness_id] = detail
                self._emit_log(
                    f"[router] harness {reg.harness_id} 启动失败（该 harness 暂不可用）：{detail}"
                )

    async def _registered_harnesses(self) -> list[HarnessRegistration]:
        """启动时该看的那份注册表：常规模式查 store，standalone 用显式映射。"""
        if self.standalone:
            return list(self.registrations.values())
        return await self.store.registry.list_harnesses()

    async def stop(self) -> None:
        """关闭全部适配器子进程。

        这里**不**触发 ``on_exit``/``on_session_ended``：是内核主动停机，
        不是崩溃；把停机报成「会话异常结束」会让对账逻辑误判（REC-02）。
        """
        self._stopped = True
        for harness_id, proc in list(self._procs.items()):
            try:
                await proc.close(grace=self._close_grace)
            except Exception as exc:  # noqa: BLE001
                self._emit_log(
                    f"[router] 关闭适配器 {harness_id} 时出错：{type(exc).__name__}: {exc}"
                )
        self._procs.clear()
        self._sessions.clear()
        self._checkpoints.clear()

    async def ensure_harness(
        self,
        harness_id: str,
        *,
        adapter_id: str | None = None,
        exec_path: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> bool:
        """按需拉起某个 harness 的适配器进程（幂等）。

        返回值：``True`` = 这次调用**之前**它就已经在跑（幂等命中，什么都没做）；
        ``False`` = 这次真的把进程拉起来了。两种都表示「现在可用」——
        真的起不来会抛 ``HARNESS_UNAVAILABLE``，不用 ``False`` 冒充失败
        （那会让调用方把「不可用」和「刚启动」混为一谈）。

        显式传入的 ``adapter_id``/``exec_path``/``env``/``cwd`` 覆盖注册表里的同名字段：
        core 通过 supervisor 的 ``harness.ensure`` 把注册信息带过来时走的就是这条路
        （supervisor 不读 core 的库）。覆盖后的注册信息留在本进程内，
        随后的 ``create_session`` 不必再查一次数据库。
        """
        proc = self._procs.get(harness_id)
        if proc is not None and proc.alive:
            return True

        reg = await self._ensure_registration(
            harness_id, adapter_id=adapter_id, exec_path=exec_path, env=env, cwd=cwd
        )
        try:
            await self._ensure_process(harness_id)
        except Exception as exc:  # noqa: BLE001 - 如实记下并让调用方看见
            detail = f"{type(exc).__name__}: {exc}"
            self._start_errors[harness_id] = detail
            self._emit_log(f"[router] 按需拉起 harness {harness_id} 失败：{detail}")
            raise
        self._emit_log(f"[router] 按需拉起 harness {harness_id}（adapter={reg.adapter_id}）")
        return False

    async def _ensure_registration(
        self,
        harness_id: str,
        *,
        adapter_id: str | None,
        exec_path: str | None,
        env: dict[str, str] | None,
        cwd: str | None,
    ) -> HarnessRegistration:
        """取（或按显式参数补出）注册信息，并叠加显式覆盖。"""
        if not harness_id:
            raise AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "未指定 harness_id")
        reg = await self._registration_or_none(harness_id)
        if reg is None:
            if not adapter_id:
                raise AdapterError(
                    ErrorCode.HARNESS_UNAVAILABLE,
                    f"ensure_harness 拿不到 {harness_id} 的注册信息：注册表里没有它，"
                    f"调用时也没带 adapter_id"
                    + (
                        "（standalone 模式的注册表由构造参数 registrations 提供）"
                        if self.standalone
                        else ""
                    ),
                    {"harness_id": harness_id},
                )
            reg = HarnessRegistration(
                harness_id=harness_id,
                name=harness_id,
                adapter_id=adapter_id,
                auth_mode=AuthMode.NATIVE_LOGIN,
                enabled=True,
            )
        elif not reg.enabled:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness 已停用：{harness_id}",
                {"harness_id": harness_id},
            )

        overrides: dict[str, Any] = {}
        if adapter_id:
            overrides["adapter_id"] = adapter_id
        if exec_path is not None:
            overrides["exec_path"] = exec_path
        if env is not None:
            overrides["env_template"] = dict(env)
        if cwd is not None:
            overrides["cwd"] = cwd
        if overrides:
            reg = reg.model_copy(update=overrides)
        self._registrations[harness_id] = reg
        return reg

    @property
    def harness_ids(self) -> list[str]:
        """已经拉起适配器进程、且进程还活着的 harness（按 id 排序）。

        只报活着的进程：进程死了就不再算「已启动」——把「以为还在跑」报给运维，
        比报一个空的列表危险得多（LIFE-02）。
        """
        return sorted(hid for hid, proc in self._procs.items() if proc.alive)

    async def stop_harness(self, harness_id: str) -> None:
        """停掉一个 harness 的适配器进程，并把它名下的会话如实标成 lost。

        与 ``stop()`` 的区别只在范围：这里只动一个 harness。与崩溃的区别在归因：
        这是主动停机，所以**不**触发 ``on_exit``（REC-02：停机不是崩溃）；但那些会话
        确实没有进程在替它们干活了，必须走 ``on_session_ended(ok=False)``，
        否则内核会一直等一个永远不来的结束事件（LIFE-02）。
        """
        proc = self._procs.pop(harness_id, None)
        if proc is None:
            self._emit_log(f"[router] harness {harness_id} 没有在跑的适配器进程")
        else:
            try:
                await proc.close(grace=self._close_grace)
            except Exception as exc:  # noqa: BLE001
                self._emit_log(
                    f"[router] 关闭适配器 {harness_id} 时出错：{type(exc).__name__}: {exc}"
                )
            self._emit_log(f"[router] 已停止 harness {harness_id} 的适配器进程")

        for ref, rec in list(self._sessions.items()):
            if rec.harness_id == harness_id and rec.state not in _TERMINAL_STATES:
                await self._mark_ended(
                    ref, ok=False, detail=f"harness {harness_id} 的适配器已被主动停止"
                )

    # ------------------------------------------------------------------
    # 2. 会话生命周期
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        harness_id: str,
        attempt: Attempt,
        stage: TaskStage,
        model_name: str,
        reasoning_effort: str | None,
        system_prompt: str | None,
        initial_input: str | None = None,
        permission_mode: str | None = None,
        cwd: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SessionHandle:
        reg = await self._registration(harness_id)

        checkpoint_ref = getattr(attempt, "resume_from_checkpoint", None)
        if checkpoint_ref:
            # 内核把「从断点重建」放在 Attempt 上（§8.2）。那是一次 resume，
            # 不是新开会话——混为一谈会让历史里丢掉「本尝试复用/重建了会话」的痕迹。
            return await self._resume(
                reg,
                persist_locator=str(checkpoint_ref),
                attempt=attempt,
                stage=stage,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                system_prompt=system_prompt,
                initial_input=initial_input,
                permission_mode=permission_mode,
                cwd=cwd,
                extra=extra,
            )

        proc = await self._ensure_process(harness_id)
        credential = await self._resolve_credential(reg)
        request = self._build_request(
            reg,
            attempt=attempt,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            cwd=cwd,
            credential=credential,
            extra=_with_initial_input(extra, initial_input),
        )
        result = await self._call(proc, METHODS.SESSION_CREATE, self._session_params(request))
        return await self._adopt(
            reg,
            result,
            used_resume=False,
            attempt_id=attempt.attempt_id,
            initial_input=initial_input,
            proc=proc,
        )

    async def resume_session(
        self, *, harness_id: str, persist_locator: str, attempt: Attempt, stage: TaskStage
    ) -> SessionHandle:
        reg = await self._registration(harness_id)
        snapshot = dict(getattr(attempt, "profile_snapshot", None) or {})
        return await self._resume(
            reg,
            persist_locator=persist_locator,
            attempt=attempt,
            stage=stage,
            model_name=str(snapshot.get("model_name") or ""),
            reasoning_effort=snapshot.get("reasoning_effort"),
            system_prompt=None,
        )

    async def _resume(
        self,
        reg: HarnessRegistration,
        *,
        persist_locator: str,
        attempt: Attempt,
        stage: TaskStage,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        system_prompt: str | None = None,
        initial_input: str | None = None,
        permission_mode: str | None = None,
        cwd: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SessionHandle:
        proc = await self._ensure_process(reg.harness_id)
        credential = await self._resolve_credential(reg)
        request = self._build_request(
            reg,
            attempt=attempt,
            model_name=model_name or "",
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            cwd=cwd,
            credential=credential,
            extra=_with_initial_input(extra, initial_input),
        )

        params = self._session_params(request)
        known = self._checkpoints.get(persist_locator)
        if known is not None:
            # 内核只知道 token；适配器要的是它自己那套结构（含续跑位置）。
            params["persist_locator"] = known.get("persist_locator") or persist_locator
            params["checkpoint"] = known
        else:
            params["persist_locator"] = persist_locator
            self._emit_log(
                f"[router] 断点 {persist_locator[:12]}… 没有本进程内的展开记录，"
                f"按不透明定位符交给适配器；能否真的从断点续跑由适配器如实回报"
            )
        result = await self._call(proc, METHODS.SESSION_RESUME, params)
        return await self._adopt(
            reg,
            result,
            used_resume=True,
            attempt_id=attempt.attempt_id,
            cwd=request.harness.cwd,
            initial_input=initial_input,
            proc=proc,
        )

    async def dispose(self, session_ref: str) -> None:
        rec = self._sessions.get(session_ref)
        proc = self._proc_for(rec)
        if proc is None:
            # 进程都没了，没有可释放的远端资源；本地状态如实收束——
            # 若此前已经报过「丢失」，就不要在这里改口说「正常结束」。
            already_lost = rec is not None and rec.state == "lost"
            await self._mark_ended(
                session_ref, ok=not already_lost, detail="适配器进程已退出"
            )
            return
        try:
            await proc.call(
                METHODS.SESSION_DISPOSE, {"session_ref": session_ref}, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            self._emit_log(f"[router] dispose 会话 {session_ref} 失败：{exc}")
        await self._mark_ended(session_ref, ok=True, detail="已释放会话句柄")

    # ------------------------------------------------------------------
    # 3. io
    # ------------------------------------------------------------------

    async def send_input(
        self, session_ref: str, text: str, *, kind: str = InputKind.USER
    ) -> bool:
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            self._emit_log(f"[router] send_input 找不到会话 {session_ref} 的适配器进程")
            return False
        try:
            result = await proc.call(
                METHODS.SEND_INPUT,
                {"session_ref": session_ref, "text": text, "kind": kind},
                timeout=self._call_timeout,
            )
        except (AdapterError, JsonRpcError) as exc:
            # NOT_SUPPORTED：该 harness 的输入在 argv 里，运行中注入不了。
            # 如实返回 False 让内核走降级，不假装「已送达」（HUM-01）。
            self._emit_log(
                f"[router] 向会话 {session_ref} 注入输入失败（kind={kind}）："
                f"{getattr(exc, 'code', None)}: {exc}"
            )
            return False
        return bool(result.get("delivered", True))

    async def respond_permission(
        self,
        session_ref: str,
        *,
        approval_id: str,
        approved: bool,
        modified_action: str | None = None,
        note: str | None = None,
    ) -> bool:
        """把审批决定回注给提出请求的会话（HUM-04）。

        返回 False 表示**没送到**——调用方必须据此把审批标成 undeliverable 并保持可见，
        不能假装送达。回传失败与「用户没批准」是两回事：前者 agent 还在等，后者它已经
        拿到答复了。混为一谈会让用户以为事情处理完了，而 agent 那头其实还挂着。
        """
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            self._emit_log(
                f"[router] 回注审批 {approval_id} 失败：会话 {session_ref} 不在活着的适配器上"
            )
            return False
        # 参数形状按 §8.1 的 ``respond(decision)``：decision 是 "approve"/"deny" 字符串。
        # 不要再用布尔别名——两种形状并存会逼每个适配器作者写兼容分支，
        # 而写漏的那一支只会在运行时才暴露。
        params: dict[str, Any] = {
            "session_ref": session_ref,
            "approval_id": approval_id,
            "decision": "approve" if approved else "deny",
        }
        if modified_action is not None:
            params["modified_action"] = modified_action
        if note is not None:
            params["note"] = note
        try:
            result = await proc.call(
                METHODS.PERMISSION_RESPOND, params, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            self._emit_log(f"[router] 回注审批 {approval_id} 失败：{exc}")
            return False
        if isinstance(result, dict) and result.get("ok") is False:
            self._emit_log(
                f"[router] 适配器拒绝回注审批 {approval_id}：{result.get('detail') or result}"
            )
            return False
        return True

    async def interrupt(self, session_ref: str) -> bool:
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            return False
        try:
            await proc.call(
                METHODS.INTERRUPT, {"session_ref": session_ref}, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            self._emit_log(f"[router] 打断会话 {session_ref} 失败：{exc}")
            return False
        return True

    # ------------------------------------------------------------------
    # 取消链（§10.4）
    # ------------------------------------------------------------------

    async def terminate(self, session_ref: str, *, signal: str = "TERM") -> bool:
        rec = self._sessions.get(session_ref)
        proc = self._proc_for(rec)
        if proc is None:
            return False
        try:
            result = await proc.call(
                METHODS.TERMINATE,
                {"session_ref": session_ref, "signal": signal},
                timeout=self._call_timeout,
            )
        except (AdapterError, JsonRpcError) as exc:
            self._emit_log(f"[router] 终止会话 {session_ref} 失败：{exc}")
            return False

        rec.state = "ended"
        if result.get("terminated"):
            return True
        # already_dead：没终止任何东西。会话确实不在了，但**不是**这次终止的功劳，
        # 所以如实返回 False——取消链的判据靠 session_alive，不靠这个布尔值。
        self._emit_log(
            f"[router] 会话 {session_ref} 在终止前已经结束（already_dead="
            f"{result.get('already_dead')}）"
        )
        await self._mark_ended(session_ref, ok=True, detail="会话此前已结束")
        return False

    async def abort_stream(self, session_ref: str) -> None:
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            return
        try:
            await proc.call(
                METHODS.ABORT_STREAM, {"session_ref": session_ref}, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            # 取消链的第一步失败不阻断后续的 terminate；如实记录，不承诺计费已停。
            self._emit_log(f"[router] 中止会话 {session_ref} 的远端流失败：{exc}")

    # ------------------------------------------------------------------
    # 4. 暂停 / 断点 / 整理（D-07、CFG-04）
    # ------------------------------------------------------------------

    async def pause(self, session_ref: str) -> bool:
        """原位暂停。适配器不支持时返回 False，由内核走协作停止。"""
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            return False
        try:
            result = await proc.call(
                METHODS.PAUSE, {"session_ref": session_ref}, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            if getattr(exc, "code", None) == ErrorCode.NOT_SUPPORTED:
                self._emit_log(f"[router] 会话 {session_ref} 不支持原位暂停，降级为协作停止")
            else:
                self._emit_log(f"[router] 暂停会话 {session_ref} 失败：{exc}")
            return False
        return bool(result.get("paused", False))

    async def checkpoint(self, session_ref: str) -> str | None:
        """取断点。取不到返回 None——恢复只能从头重跑，必须如实告知（LIFE-02）。"""
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            return None
        try:
            result = await proc.call(
                METHODS.CHECKPOINT, {"session_ref": session_ref}, timeout=self._call_timeout
            )
        except (AdapterError, JsonRpcError) as exc:
            if getattr(exc, "code", None) == ErrorCode.NOT_SUPPORTED:
                self._emit_log(f"[router] 会话 {session_ref} 不支持断点，恢复将从头重跑")
            else:
                self._emit_log(f"[router] 保存会话 {session_ref} 的断点失败：{exc}")
            return None

        payload = result.get("checkpoint") if isinstance(result, dict) else None
        if not isinstance(payload, dict) or not payload.get("token"):
            return None
        token = str(payload["token"])
        self._checkpoints[token] = payload
        return token

    async def compact(self, session_ref: str, threshold: int | None) -> dict[str, Any]:
        proc = self._proc_for(self._sessions.get(session_ref))
        if proc is None:
            return {"ok": False, "reason": "会话所属的适配器进程已退出"}
        try:
            result = await proc.call(
                METHODS.COMPACT,
                {"session_ref": session_ref, "threshold": threshold},
                timeout=self._call_timeout,
            )
        except (AdapterError, JsonRpcError) as exc:
            # 不得声称已整理（CFG-04）。给内核一个明确的否，而不是抛异常。
            return {"ok": False, "reason": f"{getattr(exc, 'code', None)}: {exc}"}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "适配器未返回整理结果"}
        return result

    # ------------------------------------------------------------------
    # 5. 能力与存活
    # ------------------------------------------------------------------

    async def capabilities(self, harness_id: str) -> SessionCaps:
        """映射适配器自述的能力。

        没起来就**按需拉起**再问：能力必须来自适配器自己的自述（HAR-02），
        而「还没启动」不等于「不支持任何东西」——返回一份全 False 的默认值
        会被校验管线当成「该 harness 未声明任何不询问的权限模式」，于是
        claude / kimi 这类没有权限钩子的 harness 会永久不可用。拉不起来
        （未注册、可执行文件缺失……）才退回保守默认值，并如实记一笔。

        保守默认值不是「猜一个」，而是「尚未验证的都不算数」（D-09、HAR-03）。
        """
        proc = self._procs.get(harness_id)
        if proc is None or not proc.alive or proc.manifest is None:
            try:
                await self.ensure_harness(harness_id)
            except AdapterError as exc:
                self._emit_log(
                    f"[router] 查询 {harness_id} 的能力前需要拉起适配器，但未能拉起："
                    f"{exc.code}: {exc}；按「尚未验证」返回保守默认值"
                )
                return SessionCaps()
            proc = self._procs.get(harness_id)
        if proc is None or proc.manifest is None:  # pragma: no cover - 上面已兜住
            return SessionCaps()
        return SessionCaps.from_mapping(
            proc.manifest.capabilities.model_dump(mode="json")
        )

    async def session_alive(self, session_ref: str) -> bool:
        """存活判定：进程活着 **且** 适配器说会话是 alive。

        任何查不到、超时、异常都算**不存活**。反向的乐观猜测（「刚还在跑，
        现在应该也在跑」）会让运维看到「仍在运行」而进程早已消失——LIFE-02 明确禁止。
        """
        rec = self._sessions.get(session_ref)
        if rec is None:
            return False
        if rec.state in _TERMINAL_STATES:
            return False
        proc = self._procs.get(rec.harness_id)
        if proc is None or not proc.alive:
            return False
        try:
            stat = await proc.call(
                METHODS.SESSION_STAT, {"session_ref": session_ref}, timeout=self._alive_timeout
            )
        except Exception as exc:  # noqa: BLE001 - 查不到就不说活着
            self._emit_log(f"[router] 查询会话 {session_ref} 状态失败，按不存活处理：{exc}")
            return False

        state = str(((stat or {}).get("session") or {}).get("state") or "")
        if state in _TERMINAL_STATES:
            rec.state = state
            await self._mark_ended(
                session_ref, ok=(state == "ended"), detail=f"适配器报告会话状态：{state}"
            )
            return False
        if state != "alive":
            # 未知状态不推断成活着（state 为空串也走这里）
            self._emit_log(f"[router] 会话 {session_ref} 状态未知（state={state!r}），按不存活处理")
            return False
        return True

    # ------------------------------------------------------------------
    # 回调：适配器 → 内核
    # ------------------------------------------------------------------

    async def _handle_event(self, event: AdapterEvent) -> None:
        session_ref = event.session_ref
        kind = str(event.kind)

        if kind == EventKind.SESSION_ENDED:
            # 会话结束先走专用回调：内核的完成判据（RUN-06）认的是它。
            await self._mark_ended(
                session_ref or "", **self._ended_verdict(event.data or {})
            )
            if session_ref and self._event_shape == "raw" and self._on_event is not None:
                # 原始事件流的消费者（app.py、supervisor）自己从 data["ok"] 翻译结束语义，
                # 不给它这一条，它就会永远等一个不来的结束事件。
                await self._on_event(event)
            return

        if not session_ref:
            # 没有会话归属的事件内核无法路由。如实记一笔，不静默丢。
            self._emit_log(f"[router] 收到无会话归属的事件（kind={kind}），已忽略")
            return

        if kind == EventKind.ERROR:
            err = event.data.get("error_class")
            if err:
                self._emit_log(f"[router] 会话 {session_ref} 上报错误（{err}）：{event.text or ''}")

        if self._on_event is None:
            return
        if self._event_shape == "raw":
            await self._on_event(event)
        else:
            await self._on_event(session_ref, kind, self._to_payload(event))

    @staticmethod
    def _ended_verdict(data: dict[str, Any]) -> dict[str, Any]:
        """把「会话怎么结束的」翻译成内核的 ``ok`` 判据（RUN-06：结束 ≠ 成功）。

        - 被终止（``reason=terminate`` / 带 ``signal``）：不是正常完成；
        - harness 进程非零退出：不是正常完成（适配器随后还会补一条 ERROR）；
        - 其余（正常收尾、dispose）：按正常结束报。
        """
        exit_code = data.get("exit_code")
        reason = data.get("reason")
        if reason == "terminate" or data.get("signal"):
            return {
                "ok": False,
                "detail": f"会话被终止（{data.get('signal') or 'terminate'}），不是正常完成",
            }
        if exit_code not in (0, None):
            return {"ok": False, "detail": f"harness 进程非零退出（code={exit_code}）"}
        return {"ok": True, "detail": str(data.get("detail") or reason or "") or None}

    @staticmethod
    def _to_payload(event: AdapterEvent) -> dict[str, Any]:
        """``AdapterEvent`` → 内核 payload。只补别名，不改内容（见模块文档的翻译表）。"""
        data: dict[str, Any] = dict(event.data or {})
        kind = str(event.kind)

        if kind == EventKind.OUTPUT:
            return {"text": event.text or ""}

        if kind in (EventKind.BACKGROUND_TASK_STARTED, EventKind.BACKGROUND_TASK_ENDED):
            data.setdefault(
                "id",
                data.get("task_id") or data.get("background_id") or data.get("id") or "unknown",
            )
            return data

        if kind == EventKind.USAGE:
            if "cache_read_tokens" not in data and "cache_read_input_tokens" in data:
                data["cache_read_tokens"] = data["cache_read_input_tokens"]
            if "cache_write_tokens" not in data and "cache_creation_input_tokens" in data:
                data["cache_write_tokens"] = data["cache_creation_input_tokens"]
            if "cost_estimate" not in data:
                for key in ("cost_usd", "total_cost_usd"):
                    value = data.get(key)
                    if isinstance(value, (int, float)):
                        data["cost_estimate"] = value
                        data.setdefault("cost_basis", "provider_reported")
                        break
            return data

        if kind == EventKind.ERROR:
            if "message" not in data and event.text:
                data["message"] = event.text
            # 内核读 payload["kind"]；适配器上报的是 error_kind / error_class。
            if "kind" not in data:
                kind_hint = data.get("error_kind") or data.get("error_class")
                if kind_hint:
                    data["kind"] = kind_hint
            return data

        return data

    async def _handle_permission(self, request: PermissionRequest) -> None:
        """权限请求原样交给 L5 的审批网关。"""
        if self._on_permission is None:
            self._emit_log(
                f"[router] 收到权限请求 {request.approval_id}（会话 {request.session_ref}），"
                f"但未装配审批回调——该请求不会被送达"
            )
            return
        await self._on_permission(request)

    def _exit_handler(self, harness_id: str) -> Callable[[int | None, str], Awaitable[None]]:
        async def _on_exit(code: int | None, stderr: str) -> None:
            await self._handle_exit(harness_id, code, stderr)

        return _on_exit

    async def _handle_exit(self, harness_id: str, code: int | None, stderr: str) -> None:
        """适配器进程死了：会话全部标记为 lost，并让上层走对账（REC-02/03）。"""
        proc = self._procs.get(harness_id)
        if proc is not None and not proc.alive:
            self._procs.pop(harness_id, None)
        self._emit_log(f"[router] 适配器进程退出：harness={harness_id} code={code}")

        for ref, rec in list(self._sessions.items()):
            if rec.harness_id == harness_id and rec.state not in _TERMINAL_STATES:
                await self._mark_ended(
                    ref, ok=False, detail=f"适配器进程退出（code={code}），会话状态未知"
                )

        if self._on_exit is not None:
            await self._on_exit(harness_id, code, stderr)

    async def _mark_ended(self, session_ref: str, *, ok: bool, detail: str | None) -> None:
        """终结一个会话并回调一次（幂等：同一个会话只报一次结束）。"""
        rec = self._sessions.get(session_ref)
        if rec is not None:
            if rec.ended_reported:
                return
            rec.ended_reported = True
            rec.state = "ended" if ok else "lost"
        elif ok is False:
            # 未知会话的「丢失」不回调：内核没有它的记录，报了也只是噪音。
            return

        if self._on_session_ended is not None and session_ref:
            await self._on_session_ended(session_ref, ok, detail)

    # ------------------------------------------------------------------
    # 适配器进程与请求构造
    # ------------------------------------------------------------------

    async def _ensure_process(self, harness_id: str) -> AdapterProcess:
        lock = self._locks.get(harness_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[harness_id] = lock
        async with lock:
            proc = self._procs.get(harness_id)
            if proc is not None and proc.alive:
                return proc
            reg = await self._registration(harness_id)
            if proc is not None:
                # 上一个进程已经死了（崩溃或正常退出）。按需重新拉起是正常运营，
                # 但必须留下痕迹：崩溃本身已经在 _handle_exit 里报过一次了。
                self._emit_log(
                    f"[router] harness {harness_id} 的适配器进程已退出"
                    f"（code={proc.exit_code}），按需重新拉起"
                )
                self._procs.pop(harness_id, None)
            return await self._spawn(reg)

    async def _spawn(self, reg: HarnessRegistration) -> AdapterProcess:
        command = self._adapter_command(reg.adapter_id, reg.harness_id)
        reg_env = dict(reg.env_template or {})
        for source, values in (("adapter_env", self.adapter_env), ("env_template", reg_env)):
            for name, value in values.items():
                if _looks_like_credential(name, value):
                    # 与注册表的 ToolLaunch 校验同一条纪律（AUTH-02）：凭据走 credential_ref，
                    # 不写进环境变量。这里只告警不阻断——env_template 的治理归配置层。
                    self._emit_log(
                        f"[router] harness {reg.harness_id} 的环境变量 {name}"
                        f"（来自 {source}）看起来包含凭据，"
                        f"建议改用 auth_binding 引用 Secret Store"
                    )
        try:
            proc = await AdapterProcess.start(
                command,
                # 优先级：注册表显式配置 > adapter_env > 系统环境（见模块文档）
                env=child_env(self.adapter_env, reg_env),
                cwd=reg.cwd,
                label=f"{reg.adapter_id}:{reg.harness_id}",
            )
        except (AdapterSpawnError, AdapterError) as exc:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"拉起适配器失败（harness={reg.harness_id}，adapter={reg.adapter_id}）：{exc}",
                {"harness_id": reg.harness_id, "adapter_id": reg.adapter_id},
            ) from exc

        proc.on_event = self._handle_event
        proc.on_permission = self._handle_permission
        proc.on_log = self._emit_log
        proc.on_exit = self._exit_handler(reg.harness_id)
        self._procs[reg.harness_id] = proc
        self._start_errors.pop(reg.harness_id, None)
        self._emit_log(
            f"[router] 已拉起适配器 {reg.adapter_id}（harness={reg.harness_id}，"
            f"pid={proc.proc.pid}）"
        )
        return proc

    def _adapter_command(self, adapter_id: str, harness_id: str) -> list[str]:
        key = _normalize_adapter_id(adapter_id)
        command = self._commands.get(key) or self._commands.get(adapter_id)
        if not command:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"没有为 adapter_id={adapter_id!r} 注册启动命令"
                f"（harness={harness_id}）。内置可用：{sorted(self._commands)}",
                {"adapter_id": adapter_id, "harness_id": harness_id},
            )
        return list(command)

    async def _registration_or_none(self, harness_id: str) -> HarnessRegistration | None:
        """查注册信息。常规模式以 store 为准（配置改了要立刻生效），
        standalone 模式没有 store，取显式注册表或本进程内补出的那份。"""
        if self.store is not None:
            reg = await self.store.registry.get_harness(harness_id)
            if reg is not None:
                return reg
        return self._registrations.get(harness_id) or self.registrations.get(harness_id)

    async def _registration(self, harness_id: str) -> HarnessRegistration:
        if not harness_id:
            raise AdapterError(ErrorCode.HARNESS_UNAVAILABLE, "未指定 harness_id")
        reg = await self._registration_or_none(harness_id)
        if reg is None:
            hint = (
                "（standalone 模式：注册表来自构造参数 registrations，"
                "或先用 ensure_harness 把注册信息带进来）"
                if self.standalone
                else ""
            )
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness 未登记：{harness_id}{hint}",
                {"harness_id": harness_id},
            )
        if not reg.enabled:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness 已停用：{harness_id}",
                {"harness_id": harness_id},
            )
        if reg.last_probe_ok is False:
            self._emit_log(
                f"[router] harness {harness_id} 上次探测失败，仍按配置尝试；"
                f"失败原因：{reg.last_probe_error or '未记录'}"
            )
        self._registrations[harness_id] = reg
        return reg

    def _build_request(
        self,
        reg: HarnessRegistration,
        *,
        attempt: Attempt,
        model_name: str,
        reasoning_effort: str | None,
        system_prompt: str | None,
        cwd: str | None,
        credential: dict[str, str] | None,
        extra: dict[str, Any] | None,
        permission_mode: str | None = None,
    ) -> CreateSessionRequest:
        return CreateSessionRequest(
            harness=HarnessConfig(
                harness_id=reg.harness_id,
                exec_path=reg.exec_path,
                env=dict(reg.env_template or {}),
                cwd=cwd or reg.cwd,
                adapter_id=reg.adapter_id,
                # 凭据只在这里出现一次，随请求进子进程；不进日志、不进事件（AUTH-02）。
                credential=credential,
                auth_mode=reg.auth_mode.value,
            ),
            model_name=model_name or "",
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            attempt_id=attempt.attempt_id,
            extra=dict(extra or {}),
        )

    @staticmethod
    def _session_params(request: CreateSessionRequest) -> dict[str, Any]:
        """请求体。``extra`` 同时铺在顶层：适配器按「本轮输入」读它（extra.prompt）。"""
        params: dict[str, Any] = {"request": request.model_dump(mode="json")}
        for key, value in (request.extra or {}).items():
            params.setdefault(key, value)
        return params

    async def _resolve_credential(self, reg: HarnessRegistration) -> dict[str, str] | None:
        """按 ``auth_binding`` 从 Secret Store 取凭据明文。

        返回值**只**允许流向 ``HarnessConfig.credential``。任何日志与异常里
        最多出现 locator（AUTH-02）。
        """
        if not reg.auth_binding:
            return None  # 依赖本机登录态
        if self.store is None:
            # standalone（supervisor）没有 core 的凭据注册表。明确失败，不要拿
            # 「本机登录态」顶上——那等于换了一份身份（AUTH-02）。
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness {reg.harness_id} 绑定了凭据引用 {reg.auth_binding}，"
                f"但当前是 standalone 模式（没有 core 的凭据注册表），无法解析",
                {"harness_id": reg.harness_id, "credential_id": reg.auth_binding},
            )
        ref = await self.store.registry.get_credential(reg.auth_binding)
        if ref is None:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness {reg.harness_id} 绑定了不存在的凭据引用：{reg.auth_binding}",
                {"harness_id": reg.harness_id, "credential_id": reg.auth_binding},
            )
        if not ref.is_usable():
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness {reg.harness_id} 绑定的凭据已被撤销：{ref.credential_id}",
                {"harness_id": reg.harness_id, "credential_id": ref.credential_id},
            )
        locator = ref.secret_locator
        if not locator:
            # harness_login：凭据由 harness 自己的登录态提供，框架无从也无权读取。
            return None
        if self.secret_store is None:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"harness {reg.harness_id} 绑定了凭据（locator={locator}），"
                f"但路由器未配置 SecretStore，无法解析",
                {"harness_id": reg.harness_id, "locator": locator},
            )
        try:
            credential = await self.secret_store.get(locator)
        except Exception as exc:  # noqa: BLE001 - 消息里只含 locator，不含值
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"读取凭据失败（locator={locator}）：{type(exc).__name__}: {exc}",
                {"harness_id": reg.harness_id, "locator": locator},
            ) from exc
        if not credential:
            raise AdapterError(
                ErrorCode.HARNESS_UNAVAILABLE,
                f"凭据为空或不存在（locator={locator}）",
                {"harness_id": reg.harness_id, "locator": locator},
            )
        return dict(credential)

    async def _call(
        self, proc: AdapterProcess, method: str, params: dict[str, Any]
    ) -> Any:
        """调适配器，把传输层错误归一成 ``AdapterError``（内核按它分类重试）。"""
        try:
            return await proc.call(method, params, timeout=self._call_timeout)
        except AdapterSpawnError as exc:  # pragma: no cover - start 阶段已拦
            raise AdapterError(ErrorCode.HARNESS_UNAVAILABLE, str(exc)) from exc

    async def _adopt(
        self,
        reg: HarnessRegistration,
        result: Any,
        *,
        used_resume: bool,
        attempt_id: str | None,
        cwd: str | None = None,
        initial_input: str | None = None,
        proc: AdapterProcess | None = None,
    ) -> SessionHandle:
        """把适配器回报的 ``SessionInfo`` 映射成内核的 ``SessionHandle``。"""
        if not isinstance(result, dict) or not result.get("session"):
            raise AdapterError(
                ErrorCode.INTERNAL_ERROR,
                f"适配器未返回会话信息（harness={reg.harness_id}）",
                {"harness_id": reg.harness_id},
            )
        info = SessionInfo.model_validate(result["session"])
        rec = _SessionRecord(
            session_ref=info.session_ref,
            harness_id=reg.harness_id,
            attempt_id=attempt_id,
            persist_locator=info.persist_locator,
            state=info.state,
            model_name=info.model_name,
            used_resume=used_resume,
            cwd=info.cwd or cwd,
        )
        self._sessions[info.session_ref] = rec

        if info.state in _TERMINAL_STATES:
            # 适配器在返回前就把会话跑完了（一轮即完的 harness）。如实补报结束，
            # 否则内核会等一个永远不来的結束事件。
            await self._mark_ended(
                info.session_ref,
                ok=(info.state == "ended"),
                detail=f"会话在建立返回前已结束（state={info.state}）",
            )

        return SessionHandle(
            session_ref=info.session_ref,
            harness_id=reg.harness_id,
            state=info.state,
            persist_locator=info.persist_locator,
            pid=info.pid,
            model_name=info.model_name or None,
            used_resume=used_resume,
            accepted_initial_input=self._accepted_initial_input(
                result, initial_input=initial_input, proc=proc
            ),
            detail=info.transcript_path,
        )

    def _accepted_initial_input(
        self,
        result: dict[str, Any],
        *,
        initial_input: str | None,
        proc: AdapterProcess | None,
    ) -> bool:
        """首轮输入是否已随建会话交付：**如实**判定，不猜。

        顺序（HAR-02 的同一条纪律——先说清楚依据，再给结论）：

        1. 适配器回包里显式带了 ``accepted_initial_input`` → 用它。适配器是
           唯一知道自己真的把输入写进去了没有的一方。
        2. 没带（老适配器）→ 只在「本次确实给了首轮输入」且「该适配器自述
           ``interact=False``」时才判 True：交互不支持的 harness 只有建会话
           这一次机会，prompt 必然随 argv 给出。
        3. 其余情况一律 False（保守）——False 的代价是内核会再投一次输入，
           会被适配器以 NOT_SUPPORTED 拒绝并如实报错；True 猜错的代价是
           内核以为已交付而实际没交付，阶段会永远等一个不会开始的任务。
        """
        reported = result.get("accepted_initial_input") if isinstance(result, dict) else None
        if isinstance(reported, bool):
            return reported
        if initial_input is None:
            return False
        caps = proc.manifest.capabilities if (proc is not None and proc.manifest) else None
        if caps is None:
            return False
        return not bool(caps.interact)

    def _proc_for(self, rec: _SessionRecord | None) -> AdapterProcess | None:
        if rec is None:
            return None
        proc = self._procs.get(rec.harness_id)
        if proc is None or not proc.alive:
            return None
        return proc

    def _emit_log(self, message: str) -> None:
        if self._log is not None:
            self._log(message)
        else:  # pragma: no cover - 未装配日志回调时的兜底
            print(message, file=sys.stderr)
