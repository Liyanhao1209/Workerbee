"""测试替身：假 harness、假上下文组装器、假摘要器。

这些替身的存在意义是让运行时内核可以脱离真实 harness 与 LLM 被**完整**测试。
它们刻意只实现 ``ports.py` 里的协议——如果内核开始依赖协议之外的东西，
这些替身会先报错，而不是等到接入真实适配器时才发现耦合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from workerbee.core.domain.artifact import ArtifactKind, ArtifactProducer
from workerbee.core.runtime.ports import AssembledContext, SessionCaps, SessionHandle

__all__ = ["FakeHarness", "FakeContextBuilder", "FakeSummarizer", "ScriptedFailure"]


class ScriptedFailure(Exception):
    """测试脚本要求这次派发失败。"""


@dataclass
class FakeSession:
    session_ref: str
    harness_id: str
    alive: bool = True
    checkpoint: str | None = None
    inputs: list[str] = field(default_factory=list)


class FakeHarness:
    """可脚本化的假 harness。"""

    def __init__(
        self,
        *,
        caps: SessionCaps | None = None,
        fail_create_for: set[str] | None = None,
        fail_send_for: set[str] | None = None,
    ) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.created: list[dict[str, Any]] = []
        self.disposed: list[str] = []
        self.terminated: list[tuple[str, str]] = []
        self._seq = 0
        self._caps = caps or SessionCaps(
            resume_session=True, interrupt=True, compact=True, token_usage=True,
            permission_hook=True, background_tasks=True, checkpoint_resume=True,
        )
        self.per_harness_caps: dict[str, SessionCaps] = {}
        self.fail_create_for = fail_create_for or set()
        self.fail_send_for = fail_send_for or set()

        #: 由测试设置的钩子：给定 (session_ref, attempt)，返回要抛出的异常或 None
        self.on_create: Callable[[str, Any], Exception | None] | None = None

    # ---- HarnessPort ----

    async def create_session(
        self, *, harness_id: str, attempt: Any, stage: Any, model_name: str,
        reasoning_effort: str | None, system_prompt: str | None,
        cwd: str | None = None, extra: dict | None = None,
    ) -> SessionHandle:
        self.created.append(
            {
                "harness_id": harness_id,
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
                "system_prompt": system_prompt,
                "stage_id": stage.stage_id,
                "node_id": stage.node_id,
            }
        )
        if harness_id in self.fail_create_for:
            raise ScriptedFailure(f"脚本要求 {harness_id} 建会话失败")
        if self.on_create is not None:
            exc = self.on_create(attempt.attempt_id, attempt)
            if exc is not None:
                raise exc

        self._seq += 1
        ref = f"sess-{self._seq}"
        self.sessions[ref] = FakeSession(session_ref=ref, harness_id=harness_id)
        return SessionHandle(
            session_ref=ref,
            harness_id=harness_id,
            state="alive",
            persist_locator=ref,
            pid=None,
            model_name=model_name,
        )

    async def resume_session(
        self, *, harness_id: str, persist_locator: str, attempt: Any, stage: Any
    ) -> SessionHandle:
        self._seq += 1
        ref = f"sess-{self._seq}"
        self.sessions[ref] = FakeSession(session_ref=ref, harness_id=harness_id)
        return SessionHandle(
            session_ref=ref,
            harness_id=harness_id,
            persist_locator=persist_locator,
            used_resume=True,
        )

    async def send_input(self, session_ref: str, text: str, *, kind: str = "user") -> bool:
        sess = self.sessions.get(session_ref)
        if sess is None:
            return False
        if session_ref in self.fail_send_for:
            return False
        sess.inputs.append(text)
        return True

    async def interrupt(self, session_ref: str) -> bool:
        return self._caps.interrupt

    async def terminate(self, session_ref: str, *, signal: str = "TERM") -> bool:
        self.terminated.append((session_ref, signal))
        sess = self.sessions.get(session_ref)
        if sess is not None:
            sess.alive = False
        return True

    async def abort_stream(self, session_ref: str) -> None:
        return None

    async def pause(self, session_ref: str) -> bool:
        return self._caps.pause_in_place

    async def checkpoint(self, session_ref: str) -> str | None:
        sess = self.sessions.get(session_ref)
        if sess is None:
            return None
        if not self._caps.checkpoint_resume:
            return None
        sess.checkpoint = f"ckpt-{session_ref}"
        return sess.checkpoint

    async def compact(self, session_ref: str, threshold: int | None) -> dict[str, Any]:
        if not self._caps.compact:
            return {"ok": False, "reason": "该 harness 不支持上下文整理"}
        return {"ok": True, "tokens_before": 100, "tokens_after": 40}

    async def session_alive(self, session_ref: str) -> bool:
        sess = self.sessions.get(session_ref)
        return bool(sess and sess.alive)

    async def capabilities(self, harness_id: str) -> SessionCaps:
        return self.per_harness_caps.get(harness_id, self._caps)

    async def dispose(self, session_ref: str) -> None:
        self.disposed.append(session_ref)
        sess = self.sessions.get(session_ref)
        if sess is not None:
            sess.alive = False

    # ---- 测试驱动 ----

    def last_session(self) -> FakeSession:
        if not self.sessions:
            raise AssertionError("还没有任何会话")
        return list(self.sessions.values())[-1]

    def session_inputs(self, index: int = -1) -> list[str]:
        return list(self.sessions.values())[index].inputs


class FakeContextBuilder:
    """记录被要求组装了什么，返回固定内容。"""

    def __init__(self, *, user_input: str = "请开始", degraded: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.user_input = user_input
        self.degraded = degraded or []

    async def build(self, *, task, stage, attempt, node, contracts, artifacts) -> AssembledContext:
        self.calls.append(
            {
                "task_id": task.task_id,
                "stage_id": stage.stage_id,
                "node_id": stage.node_id,
                "contracts": list(contracts),
                "artifact_ids": [a.artifact_id for a in artifacts],
                "node_name": getattr(node, "name", None),
            }
        )
        return AssembledContext(
            system_prompt=node.system_prompt or f"你是 {node.name}",
            user_input=self.user_input,
            partitions={"P1": {"tokens": 10}, "P2": {"tokens": 20}},
            degraded=list(self.degraded),
            token_estimate=30,
            log_summary={"partitions": ["P1", "P2"], "degraded": self.degraded},
        )


class FakeSummarizer:
    """按脚本返回覆盖结果。默认全部覆盖。"""

    def __init__(
        self,
        *,
        summary_text: str = "摘要：已完成",
        cover: list[str] | None = None,
        ok: bool = True,
        raise_on_call: bool = False,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.summary_text = summary_text
        self.cover = cover
        self.ok = ok
        self.raise_on_call = raise_on_call

    async def summarize(self, content: str, *, contract_fields, max_chars: int = 2000):
        from pydantic import BaseModel

        self.calls.append({"chars": len(content), "contract_fields": list(contract_fields)})
        if self.raise_on_call:
            raise RuntimeError("摘要后端不可用")

        covered = list(contract_fields) if self.cover is None else list(self.cover)
        missing = [f for f in contract_fields if f not in covered]

        class _Result(BaseModel):
            summary: str
            covered_fields: list[str]
            missing_fields: list[str]
            ok: bool
            reason: str | None = None

        overall_ok = self.ok and not missing
        return _Result(
            summary=self.summary_text,
            covered_fields=covered,
            missing_fields=missing,
            ok=overall_ok,
            reason=None if overall_ok else "摘要未覆盖全部契约字段",
        )
