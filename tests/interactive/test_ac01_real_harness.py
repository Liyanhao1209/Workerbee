"""AC-01 的真实 harness 版本：三阶段、至少两种 harness、一次提交自动跑完。

默认不跑（`-m "not interactive"`）。要跑：

    .venv/bin/python -m pytest -m interactive tests/interactive -s

**会消耗你的账号配额**，所以提示词刻意压到最短。

这项测试与其他所有测试的区别：它验证的是产品**声称要解决的问题本身**——
「用户不必逐阶段创建 session、查找命令和复制摘要」。其他测试都在测机制，
只有这条在测那个承诺。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from workerbee.core.domain import (
    Edge,
    ExecutionProfile,
    GraphSpec,
    NodeDefinition,
    WorkflowDefinition,
    WorkflowRevision,
)
from workerbee.core.domain.registry import AuthMode, HarnessRegistration
from workerbee.core.domain.task import StageState, TaskState
from workerbee.core.runtime.launch import launch_task

pytestmark = pytest.mark.interactive

CLAUDE = "claude-local"
KIMI = "kimi-local"


def _available() -> dict[str, bool]:
    return {
        "claude": shutil.which("claude") is not None,
        "kimi": shutil.which("kimi") is not None,
    }


async def _harness_setup(store) -> list[str]:
    have = _available()
    registered: list[str] = []
    if have["claude"]:
        await store.registry.upsert_harness(
            HarnessRegistration(
                harness_id=CLAUDE, name="Claude Code", adapter_id="claude_code",
                auth_mode=AuthMode.NATIVE_LOGIN, enabled=True,
            )
        )
        registered.append(CLAUDE)
    if have["kimi"]:
        await store.registry.upsert_harness(
            HarnessRegistration(
                harness_id=KIMI, name="Kimi Code", adapter_id="kimi_code",
                auth_mode=AuthMode.NATIVE_LOGIN, enabled=True,
            )
        )
        registered.append(KIMI)
    return registered


async def _wait_terminal(store, task_id: str, *, timeout: float = 420.0) -> str:
    """等任务到终态。用轮询而不是事件，避免测试与被测系统共享一条可能出错的路径。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = await store.tasks.get_task(task_id)
        if task is not None and task.observed_state in (
            TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED
        ):
            return task.observed_state.value
        await asyncio.sleep(2.0)
    return "timeout"


@pytest.mark.timeout(600)
async def test_ac01_cross_harness_three_stage_run(store, engine_factory, tmp_path):
    """AC-01：三阶段、至少两种已支持的 harness，一次提交自动建会话、交接并结束。

    覆盖 WF-01、HAR-01–03、RUN-01、DATA-01、OBS-03。
    """
    harnesses = await _harness_setup(store)
    if len(harnesses) < 2:
        pytest.skip(f"需要至少两种 harness，本机可用：{harnesses}")

    engine = await engine_factory()
    wf = WorkflowDefinition(name="ac01", max_concurrent_tasks=4)
    await store.workflows.create(wf)

    # 三个节点：Claude 规划 → Kimi 执行 → Claude 复核。
    # 提示词刻意极短，避免烧配额；测的是链路，不是模型能力。
    spec = GraphSpec(
        nodes=[
            NodeDefinition(
                node_id="P", name="规划", role="规划者",
                system_prompt="你只回复用户要求的内容，不要使用任何工具，不要解释。",
                profiles=[
                    ExecutionProfile(
                        profile_id="p1", model_name="", harness_ref=CLAUDE,
                        permission_mode="bypassPermissions",
                    )
                ],
            ),
            NodeDefinition(
                node_id="E", name="执行", role="执行者",
                system_prompt="你只回复用户要求的内容，不要使用任何工具，不要解释。",
                profiles=[
                    ExecutionProfile(
                        profile_id="p2", model_name="", harness_ref=KIMI,
                        permission_mode="default",
                    )
                ],
            ),
            NodeDefinition(
                node_id="R", name="复核", role="审计者",
                system_prompt="你只回复用户要求的内容，不要使用任何工具，不要解释。",
                profiles=[
                    ExecutionProfile(
                        profile_id="p3", model_name="", harness_ref=CLAUDE,
                        permission_mode="bypassPermissions",
                    )
                ],
            ),
        ],
        edges=[Edge(from_node="P", to_node="E"), Edge(from_node="E", to_node="R")],
    )
    await store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id, revision_seq=1, graph=spec, is_published=True
        ),
        publish=True,
        expected_revision_seq=0,
    )

    result = await launch_task(
        store=store,
        workflow_id=wf.workflow_id,
        input_payload={"task": "只回复两个字：收到"},
    )
    assert result.created

    final = await _wait_terminal(store, result.task.task_id)
    detail_parts = []
    for s in await store.tasks.list_stages(result.task.task_id):
        detail_parts.append(f"{s.node_name}={s.observed_state.value}")

    assert final == "succeeded", f"任务终态 {final}；阶段：{', '.join(detail_parts)}"

    stages = {s.node_id: s for s in await store.tasks.list_stages(result.task.task_id)}
    assert all(s.observed_state == StageState.SUCCEEDED for s in stages.values())

    # DATA-01：框架自动完成了交接——下游拿到了上游产物的引用
    assert stages["E"].upstream_pins.get("P"), "下游必须钉扎上游产物（自动交接）"
    artifacts = await store.artifacts.list_by_task(result.task.task_id)
    assert artifacts, "每个阶段都应产出可交接的产物"

    # OBS-03 / CFG-07：实际配置与路径可追溯
    attempts = await store.tasks.list_attempts_for_task(result.task.task_id)
    by_node = {a.node_id: a for a in attempts}
    assert by_node["E"].profile_snapshot["harness_ref"] == KIMI, "第二段应确实换到了 kimi"
    assert by_node["R"].profile_snapshot["harness_ref"] == CLAUDE

    events = await store.events.for_task(result.task.task_id)
    assert any(e["type"] == "context.assembled" for e in events), "交接内容必须可检查"
