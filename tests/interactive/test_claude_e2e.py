"""真实 Claude Code 走**完整内核**的端到端测试（慢、要登录、要花 token）。

**默认不跑**：pyproject 的 addopts 里有 ``-m "not interactive"``。手动跑：

    .venv/bin/python -m pytest tests/interactive/test_claude_e2e.py -m interactive -v -s

与 ``test_real_harnesses.py`` 的分工：那一个测**适配器**（协议往返、能力声明、
取消链），这一个测**整条产品链路**——建 workflow → launch → 等终态 → 产物与用量，
也就是用户真实使用时走的那条路。这条链路此前是断的：内核支持把首轮输入随
``create_session`` 一起交付，但适配层的 ``initial_input`` 通道没接上，于是真 claude
在 text 模式下直接报「prompt 必须随会话创建一起给出」。

第三组用例覆盖**交互会话**（``-p --input-format stream-json``）：首轮输入走 stdin、
运行中还能真的再注入一条（HUM-01 的 BTW）、打断有回执且被如实归类（HUM-02）、
静默一段时间后如实结束（RUN-06）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import sys
import time
from typing import Any, Callable

import pytest

from workerbee.adapters.host.client import AdapterProcess
from workerbee.adapters.sdk.protocol import METHODS
from workerbee.app import Engine, EngineConfig
from workerbee.core.domain import (
    ExecutionProfile,
    GraphSpec,
    NodeDefinition,
    WorkflowDefinition,
    WorkflowRevision,
)
from workerbee.core.domain.registry import AuthMode, HarnessRegistration

pytestmark = pytest.mark.interactive

#: 真实模型调用慢（冷启动 + 首 token + 可能的工具往返），给足时间。
TERMINAL_TIMEOUT = 300.0
HARNESS_ID = "claude-local"
ADAPTER_ID = "claude_code"
ADAPTER_ARGV = [sys.executable, "-m", "workerbee.adapters.claude_code.main"]

#: 一句话任务：明确要求不用工具，避免模型去写文件（真实模型输出不稳定，
#: 断言只看「该出现的内容在不在」，不比对完整文案）。
ONE_LINER = "只回复四个字：你好世界。不要做任何其他事，不要用任何工具。"
EXPECTED = "你好世界"

#: 交互模式下用的长任务：靠一个慢命令把这一轮**撑住**，好让我们在轮次中间打断。
#: 用时长而不是「字数多」是因为 claude 的消息块是整块到达的（没有
#: --include-partial-messages），按字数等的话，第一块正文到手时这一轮往往已经结束了。
BUSY_PROMPT = (
    "请先用 Bash 工具执行一条命令：sleep 25。只执行这一条命令，"
    "不要做别的事；等它结束后回复四个字：完成了。"
)

#: busy 任务里那条命令的实际时长；打断后本轮必须在远早于它结束时就收尾。
BUSY_SECONDS = 25.0


def _model() -> str:
    """模型名从环境取，默认用本机已配好的那个（换机器只改环境，不改测试）。"""
    return os.environ.get("WORKERBEE_E2E_MODEL", "deepseek-flash[1m]")


def _has_claude() -> bool:
    return shutil.which("claude") is not None


# ----------------------------------------------------------------------
# 组装：登记 harness、探测能力、建 workflow
# ----------------------------------------------------------------------


async def _register_and_probe(eng: Engine, *, env_template: dict[str, str] | None = None) -> Any:
    """登记 harness，并把探测结论写进 ``capabilities_snapshot``。

    这一步与 API 的 ``probe_harness`` 是同一件事（HAR-02）：HUM-03 的校验读的是
    注册表里的 ``capabilities_snapshot``，不探测就等于没声明，权限模式那几条
    检查根本不会跑——绕过它「看起来能跑」，但也不是被测的东西了。
    """
    await eng.store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=HARNESS_ID,
            name="Claude Code",
            adapter_id=ADAPTER_ID,
            auth_mode=AuthMode.NATIVE_LOGIN,
            enabled=True,
            env_template=env_template or {},
        )
    )
    caps = await eng.harness.capabilities(HARNESS_ID)
    await eng.store.registry.record_probe(
        HARNESS_ID, ok=True, capabilities=dataclasses.asdict(caps), error=None
    )
    return caps


async def _new_engine(tmp_path, *, env_template: dict[str, str] | None = None) -> Engine:
    if not _has_claude():
        pytest.skip("本机没有 claude CLI")
    eng = await Engine.create(
        EngineConfig(
            data_dir=tmp_path / "data",
            use_summarizer=False,
            use_context_assembler=False,
            node_cwd=tmp_path,
            poll_interval=0.5,
        )
    )
    await _register_and_probe(eng, env_template=env_template)
    return eng


@pytest.fixture
async def engine(tmp_path):
    """默认（text）通道：一次性会话，阶段靠进程退出收口。"""
    eng = await _new_engine(tmp_path)
    try:
        yield eng
    finally:
        await eng.stop()


@pytest.fixture
async def stream_engine(tmp_path):
    """流式输入通道（常驻会话），静默结束窗口压到 5 秒。

    静默窗口是「常驻会话怎么结束」的答案：内核按 ``session_ended`` 判阶段完成
    （RUN-06），而常驻会话跑完一轮不会自己退，所以适配器在**一轮结束后**静默
    超过窗口时关掉输入通道让 harness 退出——结束事件仍由进程真的退出触发。
    """
    eng = await _new_engine(
        tmp_path,
        env_template={
            "WORKERBEE_CLAUDE_CODE_INPUT_FORMAT": "stream-json",
            "WORKERBEE_CLAUDE_CODE_IDLE_END_SECONDS": "5",
        },
    )
    try:
        yield eng
    finally:
        await eng.stop()


def _workflow(model: str, *, permission_mode: str | None = "bypassPermissions") -> GraphSpec:
    return GraphSpec(
        nodes=[
            NodeDefinition(
                node_id="N1",
                name="写一句话",
                role="执行者",
                profiles=[
                    ExecutionProfile(
                        profile_id="p1",
                        model_name=model,
                        harness_ref=HARNESS_ID,
                        permission_mode=permission_mode,
                    )
                ],
            )
        ],
        edges=[],
    )


async def _publish(eng: Engine, graph: GraphSpec) -> str:
    wf = WorkflowDefinition(name="claude-e2e")
    await eng.store.workflows.create(wf)
    await eng.store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id, revision_seq=1, graph=graph, is_published=True
        ),
        publish=True,
        expected_revision_seq=0,
    )
    return wf.workflow_id


async def _wait_terminal(eng: Engine, task_id: str, *, timeout: float = TERMINAL_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await eng.task_detail(task_id)
        if detail["task"]["observed_state"] in ("succeeded", "failed", "cancelled"):
            return detail
        await asyncio.sleep(1.0)
    raise AssertionError(
        f"任务 {timeout:.0f}s 内没有走到终态：{detail.get('task', {}).get('observed_state')}；"
        f"阶段={[(s['node_name'], s['observed_state']) for s in detail.get('stages', [])]}；"
        f"尝试={[(a.get('outcome') or {}).get('error_class') for a in detail.get('attempts', [])]}"
    )


async def _artifact_text(eng: Engine, detail: dict) -> str:
    parts = [
        await eng.store.artifacts.read_text(art["artifact_id"]) for art in detail["artifacts"]
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 1. 走完整内核：建 workflow → launch → 等终态 → 产物与用量
# ----------------------------------------------------------------------


async def test_one_sentence_task_succeeds_with_artifact_and_usage(engine):
    """验收用例：单节点、一句话任务 → succeeded，产物里有预期内容、用量非空。"""
    await engine.start(reconcile=True)
    workflow_id = await _publish(engine, _workflow(_model()))

    submitted = await engine.submit(workflow_id=workflow_id, input_payload={"task": ONE_LINER})
    assert submitted["accepted"] is True, submitted

    detail = await _wait_terminal(engine, submitted["task_id"])
    assert detail["task"]["observed_state"] == "succeeded", (
        f"阶段={[(s['observed_state'], s.get('blocked_reason')) for s in detail['stages']]}；"
        f"尝试={[(a.get('outcome') or {}).get('error_class') for a in detail['attempts']]}"
    )

    text = await _artifact_text(engine, detail)
    assert EXPECTED in text, f"产物里没有预期内容：{text[:200]!r}"

    assert detail["attempts"], "至少要有一条尝试记录"
    usage = detail["attempts"][0]["usage"]
    # OBS-04：拿不到就记「未知」；但这条链路必须真的拿到——claude 的 result 行
    # 带 usage，丢在转发里等于用量面板永远显示「未知」。
    assert usage, f"attempt.usage 是空的：{usage}"
    assert (usage.get("input_tokens") or 0) > 0
    assert (usage.get("output_tokens") or 0) >= 0


async def test_unset_permission_mode_is_rejected_before_anything_runs(engine):
    """HUM-03 在真实登记上的样子：没有权限钩子 + 没给模式 → 发射前就拒。

    「框架不替你默认放行」的可执行证据：拒绝发生在 launch，一个 token 都没花，
    而且报告里必须给出可选项，不能只说一句「不行」。
    """
    await engine.start(reconcile=True)
    workflow_id = await _publish(engine, _workflow(_model(), permission_mode=None))

    result = await engine.submit(workflow_id=workflow_id, input_payload={"task": ONE_LINER})
    assert result["accepted"] is False, result
    diags = {d["code"]: d for d in result["report"]["diagnostics"]}
    assert "permission_mode_unset" in diags, result["report"]
    assert diags["permission_mode_unset"]["severity"] == "error"
    # 提示里要能直接看到「该填什么」，否则用户只能猜
    assert "bypassPermissions" in diags["permission_mode_unset"]["hint"]


# ----------------------------------------------------------------------
# 2. 交互会话：走完整内核也要能收口
# ----------------------------------------------------------------------


async def test_interactive_session_still_completes_the_stage(stream_engine):
    """流式输入通道下的一轮：能力声明为可交互，阶段照样走到 succeeded。"""
    caps = await stream_engine.harness.capabilities(HARNESS_ID)
    assert caps.interact is True and caps.interrupt is True, "流式通道下应当声明可交互"

    await stream_engine.start(reconcile=True)
    workflow_id = await _publish(stream_engine, _workflow(_model()))
    submitted = await stream_engine.submit(workflow_id=workflow_id, input_payload={"task": ONE_LINER})
    assert submitted["accepted"] is True, submitted

    detail = await _wait_terminal(stream_engine, submitted["task_id"])
    assert detail["task"]["observed_state"] == "succeeded", (
        f"阶段={[(s['observed_state'], s.get('blocked_reason')) for s in detail['stages']]}；"
        f"尝试={[(a.get('outcome') or {}).get('error_class') for a in detail['attempts']]}"
    )
    assert EXPECTED in await _artifact_text(stream_engine, detail)
    usage = detail["attempts"][0]["usage"]
    assert usage and (usage.get("input_tokens") or 0) > 0


# ----------------------------------------------------------------------
# 3. 直接驱动适配器：三件只有真跑才能证明的事
# ----------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def on_event(self, event: Any) -> None:
        self.events.append(event)

    def turns(self, ref: str) -> int:
        return sum(1 for e in self.events if str(e.kind) == "turn_end" and e.session_ref == ref)

    def text(self, ref: str) -> str:
        """harness 的正文（只看 block=text）——思考链与用户回显都不算。"""
        return "".join(
            e.text or ""
            for e in self.events
            if e.session_ref == ref
            and str(e.kind) == "output"
            and (e.data or {}).get("block") == "text"
        )

    def echoed_user_text(self, ref: str) -> list[Any]:
        return [
            e
            for e in self.events
            if e.session_ref == ref
            and str(e.kind) == "output"
            and (e.data or {}).get("block") == "user_text"
        ]

    def errors(self, ref: str) -> list[Any]:
        return [e for e in self.events if str(e.kind) == "error" and e.session_ref == ref]

    def tool_names(self, ref: str) -> list[Any]:
        return [
            (e.data or {}).get("tool_name")
            for e in self.events
            if str(e.kind) == "tool_use" and e.session_ref == ref
        ]


async def _wait_until(pred: Callable[[], bool], *, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.2)
    return pred()


async def _start_interactive(rec: _Recorder, *, idle_end_seconds: str = "0") -> AdapterProcess:
    """常驻（不自动结束）的流式输入适配器，结束由测试自己触发。"""
    ap = await AdapterProcess.start(
        ADAPTER_ARGV,
        env={
            "WORKERBEE_CLAUDE_CODE_INPUT_FORMAT": "stream-json",
            "WORKERBEE_CLAUDE_CODE_IDLE_END_SECONDS": idle_end_seconds,
        },
        label="claude-code-interactive",
    )
    ap.on_event = rec.on_event
    return ap


def _create_params(cwd: str, prompt: str) -> dict:
    return {
        "harness": {"harness_id": HARNESS_ID, "cwd": cwd},
        "model_name": _model(),
        # 显式给一个不询问的模式：没有钩子时，框架要求用户自己选（HUM-03）。
        "permission_mode": "bypassPermissions",
        "extra": {"prompt": prompt},
    }


async def test_io_send_input_really_injects_a_second_turn(tmp_path):
    """HUM-01 的地基：``io.send_input`` 必须真的把消息投进那个会话。

    直接驱动适配器（不经内核），因为要验证的就是「第二条消息到达了 harness」
    这件事：投递前一轮已结束、进程仍在，投递后 harness 回了新内容。
    """
    if not _has_claude():
        pytest.skip("本机没有 claude CLI")
    rec = _Recorder()
    ap = await _start_interactive(rec)
    try:
        assert ap.manifest.capabilities.interact is True

        created = await ap.call(
            METHODS.SESSION_CREATE,
            _create_params(str(tmp_path), "Reply with exactly: FIRST. Do not use any tools."),
            timeout=120.0,
        )
        ref = created["session"]["session_ref"]
        # 首轮输入随建会话交付：内核据此**不会**再投一次同样的输入
        # （同一条指令执行两遍会损坏结果）。
        assert created["accepted_initial_input"] is True

        assert await _wait_until(lambda: rec.turns(ref) >= 1), (
            f"首轮没跑完；事件={[str(e.kind) for e in rec.events]}"
        )
        assert "FIRST" in rec.text(ref)

        # 运行中注入第二条：真的到达 harness，而不是「已送达」的空话
        injected = await ap.call(
            METHODS.SEND_INPUT,
            {"session_ref": ref, "kind": "btw", "text": "Now reply with exactly: SECOND"},
        )
        assert injected["delivered"] is True
        assert await _wait_until(lambda: rec.turns(ref) >= 2), (
            f"注入的输入没有换来第二轮；事件={[str(e.kind) for e in rec.events]}"
        )
        assert "SECOND" in rec.text(ref)
        # 自己发出去的话不能被当成 harness 的输出（实测 --replay-user-messages
        # 会把输入原样回显，适配器必须按 isReplay 过滤掉）
        assert not rec.echoed_user_text(ref), f"用户回显被当成了输出：{rec.echoed_user_text(ref)}"
        assert rec.text(ref).count("FIRST") == 1, f"正文={rec.text(ref)!r}"

        # 取消链：常驻会话必须能被收掉，且如实报告已回收
        terminated = await ap.call(METHODS.TERMINATE, {"session_ref": ref, "grace_ms": 5000})
        assert terminated["reclaimed"] is True
        assert await _wait_until(
            lambda: any(str(e.kind) == "session_ended" for e in rec.events), timeout=30.0
        ), "终止之后必须上报 session_ended"
    finally:
        await ap.close(grace=5.0)


async def test_interrupt_ends_the_turn_and_is_reported_as_user_cancelled(tmp_path):
    """HUM-02：打断要真的打断，并且如实说成「用户取消」而不是 harness 故障。

    为了让「打断」真的落在轮次中间，这一轮被一个 ``sleep 25`` 撑住：看到 Bash
    工具调用就说明模型正在执行中。实测 2.1.283：打断有回执
    （``control_response.success``），本轮以 ``result/error_during_execution``
    收尾、进程继续存活。报成 harness 故障会触发一次毫无意义的重试，所以分类必须对。
    """
    if not _has_claude():
        pytest.skip("本机没有 claude CLI")
    rec = _Recorder()
    ap = await _start_interactive(rec)
    try:
        created = await ap.call(
            METHODS.SESSION_CREATE, _create_params(str(tmp_path), BUSY_PROMPT), timeout=120.0
        )
        ref = created["session"]["session_ref"]
        assert await _wait_until(lambda: "Bash" in rec.tool_names(ref), timeout=120.0), (
            f"这一轮没有走到工具调用，无法保证打断落在轮次中间；"
            f"事件={[str(e.kind) for e in rec.events]}；正文={rec.text(ref)[:200]!r}"
        )

        started = time.monotonic()
        await ap.call(METHODS.INTERRUPT, {"session_ref": ref})
        assert await _wait_until(lambda: rec.turns(ref) >= 1, timeout=30.0), "打断后本轮没有结束"
        elapsed = time.monotonic() - started

        # 那条命令还在跑（25s）：这么快就收尾，只可能是真的被打断了
        assert elapsed < BUSY_SECONDS / 2, (
            f"打断后本轮用了 {elapsed:.1f}s 才结束，看起来并没有真的被打断"
        )

        errors = rec.errors(ref)
        assert errors, "被打断的一轮应当留下一条记录（不能静默吞掉）"
        assert errors[-1].data.get("error_class") == "user_cancelled", errors[-1].data

        # 打断 ≠ 终止：进程还在，仍可继续用
        stat = await ap.call(METHODS.SESSION_STAT, {"session_ref": ref})
        assert stat["session"]["state"] == "alive", stat["session"]
        terminated = await ap.call(METHODS.TERMINATE, {"session_ref": ref, "grace_ms": 5000})
        assert terminated["reclaimed"] is True
    finally:
        await ap.close(grace=5.0)


async def test_capabilities_declare_what_was_measured(tmp_path):
    """能力声明表：交互通道下 interact/interrupt 为 True，权限模式分类照实。"""
    if not _has_claude():
        pytest.skip("本机没有 claude CLI")
    rec = _Recorder()
    ap = await _start_interactive(rec)
    try:
        caps = ap.manifest.capabilities
        assert caps.interact is True and caps.interrupt is True
        # 可交互不等于有权限钩子：这两件事互不推导（D-09）
        assert caps.permission_hook is False
        assert set(caps.non_interactive_modes) == {"auto", "bypassPermissions", "dontAsk", "plan"}
        assert "manual" not in caps.non_interactive_modes
        assert set(caps.non_interactive_modes) <= set(caps.permission_modes)
    finally:
        await ap.close(grace=5.0)


async def test_text_channel_refuses_a_session_without_prompt(tmp_path):
    """默认（text）通道：能力如实为 False，且缺 prompt 时**大声报错**而不是干等。

    这条断言的就是当初那次真实失败（「prompt 必须随会话创建一起给出」）——
    只有它能保证内核不会把一个永远不会收到输入的空会话派给用户。
    """
    if not _has_claude():
        pytest.skip("本机没有 claude CLI")
    rec = _Recorder()
    ap = await AdapterProcess.start(ADAPTER_ARGV, label="claude-code-text")
    ap.on_event = rec.on_event
    try:
        caps = ap.manifest.capabilities
        assert caps.interact is False and caps.interrupt is False

        with pytest.raises(Exception) as ei:
            await ap.call(
                METHODS.SESSION_CREATE,
                {"harness": {"harness_id": HARNESS_ID, "cwd": str(tmp_path)}, "model_name": _model()},
                timeout=60.0,
            )
        assert "prompt" in str(ei.value), str(ei.value)
        # 报错之后适配器仍然可用（一个坏请求不能把进程带走）
        assert ap.proc.returncode is None
    finally:
        await ap.close(grace=5.0)
