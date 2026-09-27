"""可脚本化的假 harness——本项目全部集成测试的地基。

它存在的理由不是「省事」，而是：真实 harness 的行为不可控（网络、模型、
权限弹窗），而内核要验证的东西是**确定性的**——事件是否按序到达、权限请求
是否真的阻塞到用户答复、终止是否在有界时间内收掉子进程、适配器崩溃时在途
请求是否拿到明确错误。这些只可能用一个「我说什么它就演什么」的 harness 来测。

剧本（script）是一份 JSON，可由构造参数或环境变量给出：

- ``WORKERBEE_MOCK_SCRIPT``：剧本 JSON 字面量；
- ``WORKERBEE_MOCK_SCRIPT_FILE``：剧本文件路径（优先级低于字面量？不——文件优先，
  便于把剧本放在仓库里用编辑器改）。

剧本结构（除 ``steps`` 外全部可选）::

    {
      "manifest": {"protocol_version": "1.0", "harness_family": "mock", ...},
      "handshake": {"skip_version_check": false},
      "drop_methods": ["session.list"],          # 收下但永不回包（测在途崩溃）
      "unsupported_methods": ["control.compact"],  # 回 NOT_SUPPORTED（测降级路径）
      "methods_delay_ms": {"session.create": 300},  # 回包前先拖一会儿（测超时）
      "exit_on_terminate": true,                 # terminate 后进程退出（取消链）
      "steps": [ ... ],
      "on_input": [ ... ]                        # 收到 io.send_input 后追加执行
    }

``unsupported_methods`` 与 ``manifest.capabilities`` 里的 False 是一件事的两面：
剧本要测降级时，两边都得写，否则就成了「声明不支持却做得到」的假替身。

步骤（``{"do": ...}``）：

===================  ==================================================
``output``           发一个 OUTPUT 事件（``text``）
``wait``             等 ``ms`` 毫秒
``permission``       发 ``permission_request`` 并**阻塞**直到 ``permission.respond``
``compact``          发 COMPACT（``pre_tokens`` / ``post_tokens`` / ``trigger``）
``background_start`` 发 BACKGROUND_TASK_STARTED（``task_id`` / ``label``）
``background_end``   发 BACKGROUND_TASK_ENDED（``task_id`` / ``status``）
``error``            发 ERROR（``message`` / ``error_class`` / ``error_kind``）
``usage``            发 USAGE（``input_tokens`` / ``output_tokens`` / ``cost_usd``）
``state``            发 STATE_CHANGE（``state`` / ``detail``）
``tool_use``         发 TOOL_USE（``name`` / ``input``）
``tool_result``      发 TOOL_RESULT（``name`` / ``content``）
``turn_end``         发 TURN_END（``text``）
``heartbeat``        发一条心跳
``stderr``           往 stderr 写一行（不进协议通道）
``log``              发一条 LOG 通知
``sleep_forever``    挂住直到被终止（测取消链）
``exit``             退出进程：默认 ``os._exit``（模拟崩溃），``graceful=true`` 走清理路径
===================  ==================================================

设计纪律：mock 的能力**全开**且每一项都真实实现——它是我们自己的测试替身，
声明 ``permission_hook=True`` 是因为它确实能发权限请求并阻塞等答复；
真实 harness 做不到的（见 claude_code / kimi_code 的 manifest）不会在这里被「补上」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..sdk.base import AdapterBase
from ..sdk.contract import (
    AdapterCapabilities,
    AdapterManifest,
    CreateSessionRequest,
    InputKind,
    PermissionRequest,
    SessionInfo,
)
from ..sdk.protocol import (
    PROTOCOL_VERSION,
    AdapterError,
    ErrorCode,
    EventKind,
    NOTIFICATIONS,
)

__all__ = ["MockAdapter", "MockScript", "MockSession", "load_script_from_env"]

#: 剧本字面量与剧本文件的环境变量名（适配器进程启动前由测试设置）。
SCRIPT_ENV = "WORKERBEE_MOCK_SCRIPT"
SCRIPT_FILE_ENV = "WORKERBEE_MOCK_SCRIPT_FILE"


class MockScript(BaseModel):
    """一份剧本。未知键一律拒绝——剧本拼错一个键却静默不生效，最难查。"""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any] | None = None
    """覆盖 manifest 字段（如把 protocol_version 改成不兼容的值来测握手失败）。"""

    handshake: dict[str, Any] = Field(default_factory=dict)
    """``skip_version_check``：跳过适配器侧的版本比对，用于测**内核侧**的校验。"""

    drop_methods: list[str] = Field(default_factory=list)
    """收下这些方法但永不回包——制造「在途请求」用。"""

    unsupported_methods: list[str] = Field(default_factory=list)
    """对这些方法回 NOT_SUPPORTED——测内核的降级路径（D-07、D-09）。

    用它与 ``manifest.capabilities`` 里对应的 False 配对写：声明不支持，
    协议上就真的做不到，而不是「声明 False 却样样都行」的假替身。
    """

    methods_delay_ms: dict[str, int] = Field(default_factory=dict)
    """这些方法回包前先睡一会儿——测内核侧超时。"""

    exit_on_terminate: bool = True
    """``control.terminate`` 之后是否让适配器进程退出（模拟 harness 真的死了）。"""

    steps: list[dict[str, Any]] = Field(default_factory=list)
    on_input: list[dict[str, Any]] = Field(default_factory=list)


def load_script_from_env(env: dict[str, str] | None = None) -> MockScript:
    """从环境变量装载剧本；两者皆无则得到一份空剧本（会话起来后什么都不做）。"""
    src = os.environ if env is None else env
    path = src.get(SCRIPT_FILE_ENV)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return MockScript.model_validate(json.load(fh))
        except (OSError, ValueError) as exc:
            raise AdapterError(
                ErrorCode.INVALID_PARAMS, f"无法读取剧本文件 {path}: {exc}"
            ) from exc
    raw = src.get(SCRIPT_ENV)
    if raw:
        try:
            return MockScript.model_validate(json.loads(raw))
        except ValueError as exc:
            raise AdapterError(ErrorCode.INVALID_PARAMS, f"剧本 JSON 非法: {exc}") from exc
    return MockScript()


@dataclass
class MockSession:
    """一个假 harness 会话。"""

    session_ref: str
    persist_locator: str
    harness_id: str
    attempt_id: str | None = None
    model_name: str | None = None
    state: str = "alive"
    created_at: str = ""
    checkpoint_seq: int = 0
    steps_done: int = 0
    script_index: int = 0
    """下一条要执行的步骤下标；checkpoint 恢复时从这里继续。"""

    output_log: list[str] = field(default_factory=list)
    received_inputs: list[dict[str, Any]] = field(default_factory=list)
    permission_decisions: list[dict[str, Any]] = field(default_factory=list)
    events_emitted: int = 0
    task: asyncio.Task | None = None
    pause_requested: bool = False

    credential_keys: list[str] = field(default_factory=list)
    """收到的凭据**键名**，供测试断言「凭据确实传到了适配器」。
    只记键名不记值——AUTH-02 允许指代，不允许留存。"""

    def alive(self) -> bool:
        return self.state in ("alive", "paused")

    def to_info(self) -> SessionInfo:
        return SessionInfo(
            session_ref=self.session_ref,
            harness_id=self.harness_id,
            state="alive" if self.alive() else "ended",
            persist_locator=self.persist_locator,
            model_name=self.model_name,
            created_at=self.created_at,
            pid=os.getpid(),
            capabilities_used=sorted(
                {"permission_hook", "background_tasks", "compact", "token_usage"}
            ),
        )


class _SessionEnded(Exception):
    """剧本要求结束会话（不是错误，只是停止执行后续步骤）。"""


class MockAdapter(AdapterBase):
    """假 harness 适配器。一个进程可以托管多个会话，每个会话跑自己的剧本。"""

    def __init__(self, script: MockScript | dict | None = None) -> None:
        super().__init__()
        if isinstance(script, MockScript):
            self.script = script
        else:
            self.script = MockScript.model_validate(script or {})
        self.manifest = _build_manifest(self.script.manifest)
        self._sessions: dict[str, MockSession] = {}
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._calls: list[dict[str, Any]] = []
        self._main_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        """自行发起的后台任务（如延时退出）。必须持引用，否则可能被 GC 掉。"""
        self._skip_version_check = bool(self.script.handshake.get("skip_version_check"))

    # ------------------------------------------------------------------
    # 进程入口
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MockAdapter":
        return cls(load_script_from_env(env))

    async def run(self) -> None:
        """在 SDK 事件循环外再包一层，让剧本能请求「正常退出」。

        ``AdapterBase.run()`` 阻塞在 stdin 队列上，外部无法唤醒；要正常退出
        只能取消它。取消后 ``finally: on_shutdown()`` 仍会执行，所以清理路径
        是真的走过，而不是靠 ``os._exit`` 跳过。
        """
        self._main_task = asyncio.create_task(super().run())
        try:
            await self._main_task
        except asyncio.CancelledError:
            pass

    async def request_exit(self) -> None:
        """请进程正常退出（剧本 ``exit`` 步骤的 graceful 分支）。"""
        if self._main_task is not None:
            self._main_task.cancel()
        else:  # pragma: no cover - run() 未被调用时的兜底
            await self.stop()

    # ------------------------------------------------------------------
    # 协议层：握手 / 方法拦截
    # ------------------------------------------------------------------

    async def on_handshake(self, params: dict) -> dict:
        mine = self.manifest.protocol_version
        core_version = str(params.get("protocol_version", ""))
        core_major = core_version.split(".")[0] if core_version else ""
        if core_major and not self._skip_version_check and core_major != mine.split(".")[0]:
            raise AdapterError(
                ErrorCode.PROTOCOL_MISMATCH,
                f"协议主版本不兼容：适配器 {mine}，内核 {core_version}",
                {"adapter_protocol": mine, "core_protocol": core_version},
            )
        return {
            "ok": True,
            "protocol_version": mine,
            "adapter_id": self.manifest.adapter_id,
            "adapter_version": self.manifest.version,
            "harness_family": self.manifest.harness_family,
            "capabilities": self.manifest.capabilities.model_dump(mode="json"),
        }

    async def _dispatch(self, method: str, params: dict) -> Any:
        self._calls.append({"method": method, "params": params})
        if method in self.script.unsupported_methods:
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                f"剧本声明本替身不支持 {method}",
                {"mock_unsupported": method},
            )
        if method in self.script.drop_methods:
            # 永不回包：内核侧的请求会一直挂着，直到本进程退出或调用超时。
            await asyncio.Event().wait()
        delay = self.script.methods_delay_ms.get(method)
        if delay:
            await asyncio.sleep(delay / 1000)
        return await super()._dispatch(method, params)

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def on_session_create(self, params: dict) -> dict:
        request, extras = _parse_request(params)
        session_ref = str(
            extras.get("session_ref") or request.session_ref_hint or uuid.uuid4()
        )
        session = MockSession(
            session_ref=session_ref,
            persist_locator=str(uuid.uuid4()),
            harness_id=request.harness.harness_id,
            attempt_id=request.attempt_id,
            model_name=request.model_name or None,
            created_at=_now_iso(),
            credential_keys=sorted((request.harness.credential or {}).keys()),
        )
        self._sessions[session_ref] = session
        # 会话真的起来了才发 session_started；真实适配器是在 init 行到达时报的，
        # 这里创建即就绪，所以在脚本第一步之前发，保证事件顺序与真实情形一致。
        await self.emit_event(
            EventKind.SESSION_STARTED,
            session_ref=session_ref,
            attempt_id=session.attempt_id,
            data={
                "persist_locator": session.persist_locator,
                "harness_id": session.harness_id,
                "model": session.model_name,
            },
        )
        await self._start_script(session, self.script.steps, start_index=0)
        return {"session": session.to_info().model_dump(mode="json"), "created": True}

    async def on_session_resume(self, params: dict) -> dict:
        request, extras = _parse_request(params)
        locator = extras.get("persist_locator")
        if not locator:
            raise AdapterError(
                ErrorCode.INVALID_PARAMS, "session.resume 需要 persist_locator"
            )
        session_ref = str(extras.get("session_ref") or request.session_ref_hint or uuid.uuid4())
        checkpoint = extras.get("checkpoint") or {}
        start_index = int(checkpoint.get("next_index", 0)) if isinstance(checkpoint, dict) else 0
        session = MockSession(
            session_ref=session_ref,
            persist_locator=str(locator),
            harness_id=request.harness.harness_id,
            attempt_id=request.attempt_id,
            model_name=request.model_name or None,
            created_at=_now_iso(),
            credential_keys=sorted((request.harness.credential or {}).keys()),
        )
        self._sessions[session_ref] = session
        await self.emit_event(
            EventKind.SESSION_RESUMED,
            session_ref=session_ref,
            attempt_id=session.attempt_id,
            data={"persist_locator": locator, "next_index": start_index},
        )
        # 从 checkpoint 恢复：续跑未完成的步骤，而不是从头发一遍。
        await self._start_script(session, self.script.steps, start_index=start_index)
        return {"session": session.to_info().model_dump(mode="json"), "resumed": True}

    async def on_session_list(self, params: dict) -> dict:
        infos = [s.to_info().model_dump(mode="json") for s in self._sessions.values()]
        return {"sessions": infos, "count": len(infos)}

    async def on_session_stat(self, params: dict) -> dict:
        session = self._get_session(params)
        return {
            "session": session.to_info().model_dump(mode="json"),
            "diagnostics": {
                "steps_done": session.steps_done,
                "script_index": session.script_index,
                "output_chunks": len(session.output_log),
                "received_inputs": list(session.received_inputs),
                "permission_decisions": list(session.permission_decisions),
                "events_emitted": session.events_emitted,
                "calls_seen": len(self._calls),
                "pending_permissions": sorted(self._pending_permissions),
                # 只回键名：让测试能断言「凭据确实到了适配器」，又不把值带出子进程。
                "credential_keys": list(session.credential_keys),
            },
        }

    async def on_session_dispose(self, params: dict) -> dict:
        session = self._get_session(params)
        await self._stop_script(session)
        session.state = "ended"
        await self.emit_event(
            EventKind.SESSION_ENDED,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"reason": "dispose"},
        )
        if params.get("forget", False):
            self._sessions.pop(session.session_ref, None)
        return {"ok": True, "disposed": True, "session_ref": session.session_ref}

    # ------------------------------------------------------------------
    # io
    # ------------------------------------------------------------------

    async def on_send_input(self, params: dict) -> dict:
        session = self._get_session(params)
        kind = str(params.get("kind") or InputKind.USER)
        text = str(params.get("text") or "")
        session.received_inputs.append({"kind": kind, "text": text})
        if kind == InputKind.INTERRUPT:
            await self._interrupt(session)
        elif self.script.on_input:
            await self._start_script(session, self.script.on_input, start_index=0)
        return {
            "ok": True,
            "delivered": True,
            "session_ref": session.session_ref,
            "kind": kind,
            "text_echo": text,
        }

    async def on_read_output(self, params: dict) -> dict:
        session = self._get_session(params)
        since = int(params.get("since_seq") or 0)
        chunks = session.output_log[since:]
        return {
            "session_ref": session.session_ref,
            "chunks": chunks,
            "text": "".join(chunks),
            "next_seq": len(session.output_log),
            "eof": not session.alive(),
        }

    # ------------------------------------------------------------------
    # 权限（HUM-03 / AC-14 的唯一测试手段）
    # ------------------------------------------------------------------

    async def on_permission_respond(self, params: dict) -> dict:
        approval_id = str(params.get("approval_id") or "")
        if not approval_id:
            raise AdapterError(ErrorCode.INVALID_PARAMS, "permission.respond 需要 approval_id")
        fut = self._pending_permissions.pop(approval_id, None)
        if fut is None or fut.done():
            # 重复通知不重复授权：这里显式拒绝，而不是静默吞掉（AC-14）。
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                f"未知或已答复的 approval_id：{approval_id}（重复通知不构成再次授权）",
                {"approval_id": approval_id},
            )
        decision = str(params.get("decision") or "").lower()
        if decision not in ("approve", "deny"):
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                f"decision 必须是 approve/deny，收到 {decision!r}",
            )
        fut.set_result(
            {
                "decision": decision,
                "by": params.get("by"),
                "modified_action": params.get("modified_action"),
            }
        )
        return {"ok": True, "approval_id": approval_id, "decision": decision}

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    async def on_interrupt(self, params: dict) -> dict:
        session = self._get_session(params)
        await self._interrupt(session)
        return {"ok": True, "interrupted": True, "session_ref": session.session_ref}

    async def on_terminate(self, params: dict) -> dict:
        session = self._get_session(params)
        signal = str(params.get("signal") or "TERM").upper()
        if session.state == "ended":
            # 已经结束了就照实说，不要表演一次「成功终止」（§8.3）。
            return {
                "ok": True,
                "session_ref": session.session_ref,
                "terminated": False,
                "already_dead": True,
                "signal": signal,
            }
        await self._stop_script(session)
        session.state = "ended"
        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"state": "ended", "reason": "terminate", "signal": signal},
        )
        await self.emit_event(
            EventKind.SESSION_ENDED,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"reason": "terminate", "signal": signal},
        )
        if self.script.exit_on_terminate:
            # 模拟「harness 进程真的死了」；但先让本次回包落盘，否则内核只会
            # 看到一个莫名其妙的断连——那测不出取消链，只测出竞态。
            self._spawn_bg(self._delayed_exit(0.1, 0))
        return {
            "ok": True,
            "session_ref": session.session_ref,
            "terminated": True,
            "already_dead": False,
            "signal": signal,
            "exit_on_terminate": self.script.exit_on_terminate,
        }

    async def on_pause(self, params: dict) -> dict:
        """协作停止（不是原位暂停）。

        协议里没有 control.resume，原位冻结会被兑现成「永远醒不来」的承诺。
        因此 mock 声明的暂停档位是 checkpoint 重建：停下 → 取 checkpoint →
        恢复时按 checkpoint 续跑（D-07 的第二档）。
        """
        session = self._get_session(params)
        await self._stop_script(session)
        session.state = "paused"
        session.pause_requested = True
        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"state": "paused", "mode": "cooperative_stop"},
        )
        return {
            "ok": True,
            "paused": True,
            "mode": "cooperative_stop",
            "next_index": session.script_index,
        }

    async def on_checkpoint(self, params: dict) -> dict:
        session = self._get_session(params)
        session.checkpoint_seq += 1
        token = f"mock-ckpt-{session.session_ref[:8]}-{session.checkpoint_seq}"
        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"state": "checkpointed", "token": token, "next_index": session.script_index},
        )
        return {
            "ok": True,
            "checkpoint": {
                "token": token,
                "next_index": session.script_index,
                "steps_done": session.steps_done,
                "persist_locator": session.persist_locator,
            },
        }

    async def on_abort_stream(self, params: dict) -> dict:
        session = self._get_session(params)
        await self._interrupt(session)
        return {"ok": True, "aborted": True, "session_ref": session.session_ref}

    async def on_compact(self, params: dict) -> dict:
        session = self._get_session(params)
        pre = int(params.get("pre_tokens") or 0)
        post = int(params.get("post_tokens") or max(pre // 4, 1))
        await self.emit_event(
            EventKind.COMPACT,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"trigger": "explicit", "pre_tokens": pre, "post_tokens": post},
        )
        return {"ok": True, "compacted": True, "pre_tokens": pre, "post_tokens": post}

    async def on_heartbeat(self, params: dict) -> dict:
        for session in self._sessions.values():
            await self.emit_heartbeat(session.session_ref, alive=session.alive())
        return {"ok": True, "sessions": list(self._sessions)}

    async def on_probe(self, params: dict) -> dict:
        return {
            "ok": True,
            "adapter_id": self.manifest.adapter_id,
            "harness_family": self.manifest.harness_family,
            "note": "mock 适配器不做实测探测：它本身就是测试替身",
            "capabilities": self.manifest.capabilities.model_dump(mode="json"),
        }

    async def on_shutdown(self) -> None:
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await self._stop_script(session)
                session.state = "ended"

    # ------------------------------------------------------------------
    # 剧本执行
    # ------------------------------------------------------------------

    async def _start_script(
        self, session: MockSession, steps: list[dict], *, start_index: int
    ) -> None:
        await self._stop_script(session)
        if session.state == "ended":
            session.state = "alive"
        session.task = asyncio.create_task(
            self._run_steps(session, steps, start_index=start_index),
            name=f"mock-script:{session.session_ref[:8]}",
        )

    async def _stop_script(self, session: MockSession) -> None:
        task = session.task
        session.task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _interrupt(self, session: MockSession) -> None:
        """打断 = 停掉当前剧本并如实记一笔；会话本身仍然活着（HUM-02）。"""
        await self._stop_script(session)
        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"state": "interrupted", "at_index": session.script_index},
        )

    async def _run_steps(
        self, session: MockSession, steps: list[dict], *, start_index: int
    ) -> None:
        try:
            for index in range(start_index, len(steps)):
                session.script_index = index
                await self._do_step(session, steps[index])
                session.steps_done += 1
                session.script_index = index + 1
        except asyncio.CancelledError:
            raise
        except _SessionEnded:
            return
        except Exception as exc:  # noqa: BLE001 - 剧本写错了要看得见，不能静默停摆
            await self.emit_event(
                EventKind.ERROR,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=f"剧本执行失败：{type(exc).__name__}: {exc}",
                data={"error_class": "fatal_error", "error_kind": "mock_script"},
            )

    async def _do_step(self, session: MockSession, step: dict) -> None:
        if not isinstance(step, dict):
            raise AdapterError(ErrorCode.INVALID_PARAMS, f"剧本步骤必须是对象：{step!r}")
        do = str(step.get("do") or "")
        handler = _STEPS.get(do)
        if handler is None:
            raise AdapterError(
                ErrorCode.INVALID_PARAMS,
                f"未知剧本步骤 do={do!r}；可用：{sorted(_STEPS)}",
            )
        await handler(self, session, step)

    # --- 各步骤 ---

    async def _step_output(self, session: MockSession, step: dict) -> None:
        text = str(step.get("text", ""))
        session.output_log.append(text)
        await self.emit_event(
            EventKind.OUTPUT,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            text=text,
        )

    async def _step_wait(self, session: MockSession, step: dict) -> None:
        await asyncio.sleep(float(step.get("ms", 0)) / 1000)

    async def _step_permission(self, session: MockSession, step: dict) -> None:
        """发权限请求并**阻塞**到收到答复——HUM-03 / AC-14 的测试支点。"""
        approval_id = str(step.get("approval_id") or f"ap-{uuid.uuid4().hex[:8]}")
        request = PermissionRequest(
            approval_id=approval_id,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            action=str(step.get("action") or "Bash(echo)"),
            target=step.get("target"),
            risk=step.get("risk"),
            tool_name=step.get("tool_name"),
            raw=step.get("raw") or {},
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[approval_id] = fut
        await self.emit_permission_request(request)

        timeout = float(step.get("timeout_ms") or 0) / 1000
        try:
            if timeout:
                decision = await asyncio.wait_for(fut, timeout)
            else:
                decision = await fut
        except asyncio.TimeoutError:
            # 超时默认 deny（HUM-03：断连、超时、重复通知都不构成批准）。
            session.permission_decisions.append(
                {"approval_id": approval_id, "decision": "denied_by_timeout",
                 "fingerprint": PermissionRequest.fingerprint(
                     request.action, request.target, request.tool_name)}
            )
            await self.emit_event(
                EventKind.ERROR,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                text=f"权限请求 {approval_id} 超时未答复，按 deny_pause 处理",
                data={"error_class": "fatal_error", "error_kind": "approval_timeout",
                      "approval_id": approval_id},
            )
            return
        finally:
            self._pending_permissions.pop(approval_id, None)

        session.permission_decisions.append(
            {
                "approval_id": approval_id,
                "decision": decision["decision"],
                "by": decision.get("by"),
                "modified_action": decision.get("modified_action"),
                "fingerprint": PermissionRequest.fingerprint(
                    request.action, request.target, request.tool_name
                ),
            }
        )
        if step.get("echo_decision", True):
            await self.emit_event(
                EventKind.STATE_CHANGE,
                session_ref=session.session_ref,
                attempt_id=session.attempt_id,
                data={"state": "permission_settled", "approval_id": approval_id,
                      "decision": decision["decision"]},
            )

    async def _step_compact(self, session: MockSession, step: dict) -> None:
        pre = int(step.get("pre_tokens", 0))
        post = int(step.get("post_tokens", max(pre // 4, 1)))
        await self.emit_event(
            EventKind.COMPACT,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={
                "trigger": str(step.get("trigger") or "auto"),
                "pre_tokens": pre,
                "post_tokens": post,
            },
        )

    async def _step_background_start(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.BACKGROUND_TASK_STARTED,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={
                "task_id": str(step.get("task_id") or "bg-1"),
                "label": str(step.get("label") or ""),
            },
        )

    async def _step_background_end(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.BACKGROUND_TASK_ENDED,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={
                "task_id": str(step.get("task_id") or "bg-1"),
                "status": str(step.get("status") or "ok"),
            },
        )

    async def _step_error(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.ERROR,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            text=str(step.get("message") or "mock error"),
            data={
                "error_class": str(step.get("error_class") or "retryable_error"),
                "error_kind": step.get("error_kind"),
            },
        )

    async def _step_usage(self, session: MockSession, step: dict) -> None:
        data = {
            k: step[k]
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cost_usd")
            if k in step
        }
        data["source"] = str(step.get("source") or "mock")
        await self.emit_event(
            EventKind.USAGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data=data,
        )

    async def _step_state(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.STATE_CHANGE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"state": str(step.get("state") or "running"),
                  "detail": step.get("detail")},
        )

    async def _step_tool_use(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.TOOL_USE,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            data={"tool_name": str(step.get("name") or "Bash"),
                  "tool_use_id": str(step.get("id") or uuid.uuid4().hex[:12]),
                  "input": step.get("input") or {}},
        )

    async def _step_tool_result(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.TOOL_RESULT,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            text=str(step.get("content") or ""),
            data={"tool_name": str(step.get("name") or "Bash"),
                  "is_error": bool(step.get("is_error", False))},
        )

    async def _step_turn_end(self, session: MockSession, step: dict) -> None:
        await self.emit_event(
            EventKind.TURN_END,
            session_ref=session.session_ref,
            attempt_id=session.attempt_id,
            text=step.get("text"),
            data={"implied_by": "mock_step"},
        )

    async def _step_heartbeat(self, session: MockSession, step: dict) -> None:
        await self.emit_heartbeat(
            session.session_ref, alive=bool(step.get("alive", True))
        )

    async def _step_stderr(self, session: MockSession, step: dict) -> None:
        print(str(step.get("text", "")), file=sys.stderr, flush=True)

    async def _step_log(self, session: MockSession, step: dict) -> None:
        await self.notify(
            NOTIFICATIONS.LOG,
            {"message": str(step.get("text", "")), "session_ref": session.session_ref,
             "level": str(step.get("level") or "info")},
        )

    async def _step_sleep_forever(self, session: MockSession, step: dict) -> None:
        await asyncio.Event().wait()

    async def _step_exit(self, session: MockSession, step: dict) -> None:
        code = int(step.get("code", 0))
        if step.get("graceful", False):
            # 先让「我要退出了」这件事落到 stderr，再走 SDK 的清理路径。
            print(f"[mock] graceful exit code={code}", file=sys.stderr, flush=True)
            self._spawn_bg(self._delayed_exit(0.05, code, graceful=True))
            raise _SessionEnded
        self._flush_protocol()
        os._exit(code)

    # ------------------------------------------------------------------

    def _spawn_bg(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _flush_protocol(self) -> None:
        """``os._exit`` 不走 atexit/flush，而协议通道是启动时捕获的原始 stdout。"""
        with contextlib.suppress(Exception):
            self._out.flush()

    async def _delayed_exit(self, delay: float, code: int, *, graceful: bool = False) -> None:
        await asyncio.sleep(delay)
        if graceful:
            await self.request_exit()
        else:
            self._flush_protocol()
            os._exit(code)

    def _get_session(self, params: dict) -> MockSession:
        session_ref = params.get("session_ref")
        if not session_ref:
            raise AdapterError(ErrorCode.INVALID_PARAMS, "缺少 session_ref")
        session = self._sessions.get(str(session_ref))
        if session is None:
            raise AdapterError(
                ErrorCode.SESSION_NOT_FOUND,
                f"未知会话 {session_ref}",
                {"session_ref": str(session_ref), "known": list(self._sessions)},
            )
        return session

    async def emit_event(self, kind: str, **kwargs: Any) -> None:
        session_ref = kwargs.get("session_ref")
        session = self._sessions.get(str(session_ref)) if session_ref else None
        if session is not None:
            session.events_emitted += 1
        await super().emit_event(kind, **kwargs)


# ----------------------------------------------------------------------
# 步骤表（放在类外，便于 __init__ 之前引用）
# ----------------------------------------------------------------------

_STEPS: dict[str, Any] = {
    "output": MockAdapter._step_output,
    "wait": MockAdapter._step_wait,
    "permission": MockAdapter._step_permission,
    "compact": MockAdapter._step_compact,
    "background_start": MockAdapter._step_background_start,
    "background_end": MockAdapter._step_background_end,
    "error": MockAdapter._step_error,
    "usage": MockAdapter._step_usage,
    "state": MockAdapter._step_state,
    "tool_use": MockAdapter._step_tool_use,
    "tool_result": MockAdapter._step_tool_result,
    "turn_end": MockAdapter._step_turn_end,
    "heartbeat": MockAdapter._step_heartbeat,
    "stderr": MockAdapter._step_stderr,
    "log": MockAdapter._step_log,
    "sleep_forever": MockAdapter._step_sleep_forever,
    "exit": MockAdapter._step_exit,
}


def _build_manifest(overrides: dict | None) -> AdapterManifest:
    """mock 的能力全开，且每一项都真的实现（见模块 docstring 的纪律说明）。"""
    base: dict[str, Any] = {
        "adapter_id": "mock",
        "version": "0.1.0",
        "protocol_version": PROTOCOL_VERSION,
        "harness_family": "mock",
        "display_name": "Mock Harness（测试替身）",
        "capabilities": AdapterCapabilities(
            create_session=True,
            resume_session=True,
            read_output=True,
            interact=True,
            interrupt=True,
            stop=True,
            compact=True,
            permission_hook=True,
            background_tasks=True,
            # 协议里没有 control.resume；原位冻结会被兑现成「永远醒不来」的承诺，
            # 因此如实声明 False，暂停档位取 D-07 的第二档（协作停止 + checkpoint）。
            pause_in_place=False,
            checkpoint_resume=True,
            keep_checkpoint_on_stop=True,
            reasoning_efforts=["low", "medium", "high"],
            models=["mock-model"],
            auth_modes=["native_login"],
            token_usage=True,
            structured_output=True,
        ),
        "auth_modes": ["native_login"],
        "notes": [
            "测试替身：行为完全由剧本 JSON 决定，不做任何真实模型调用。",
            "permission_hook=True 表示它会真实发出 permission_request 并阻塞等待答复。",
            "pause 档位为「协作停止 + checkpoint 重建」（D-07 第二档）。",
        ],
    }
    if overrides:
        overlay = dict(overrides)
        caps = overlay.pop("capabilities", None)
        base.update(overlay)
        if caps:
            merged = base["capabilities"].model_dump(mode="json")
            merged.update(caps)
            base["capabilities"] = AdapterCapabilities.model_validate(merged)
    return AdapterManifest.model_validate(base)


def _parse_request(params: dict) -> tuple[CreateSessionRequest, dict]:
    from ..sdk.cli import parse_session_request

    return parse_session_request(params)


def _now_iso() -> str:
    from ...core.domain.base import now_iso

    return now_iso()


# 便于测试直接断言「剧本步骤名与实现对得上」
STEP_NAMES = frozenset(_STEPS)
