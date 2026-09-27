"""真实 Claude Code 的权限钩子验收（HUM-03/04）。

默认不跑：

    .venv/bin/python -m pytest -m interactive tests/interactive -s

**会消耗你的账号配额。**

这条测试存在的理由：审批闭环此前只能在 mock 适配器上验——因为适配层把 claude
的 ``permission_hook`` 声明成了 ``False``（「外部进程拿不到标准权限钩子」）。
那个判断是错的：钩子不在 CLI 的 flag 列表里，在 host 控制协议里。
所以这里用真 harness 把「提问 → 批准 → 命令真的执行」和「拒绝 → 命令不执行」
两条路都走一遍。只测 mock 是测不出这个的——mock 的行为是我自己写的，
恰好会绕开真实协议里那些坑。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from workerbee.core.domain import (
    ExecutionProfile,
    GraphSpec,
    HarnessRegistration,
    NodeDefinition,
    WorkflowDefinition,
    WorkflowRevision,
)
from workerbee.core.domain.approval import ApprovalStatus
from workerbee.core.domain.task import StageState, TaskState
from workerbee.core.runtime.launch import launch_task

pytestmark = pytest.mark.interactive

CLAUDE = "claude-local"
#: 控制通道由环境变量开启——钩子依赖它，没有控制通道就没有 host 握手。
INPUT_FORMAT_ENV = "WORKERBEE_CLAUDE_CODE_INPUT_FORMAT"


def _require_claude() -> None:
    if shutil.which("claude") is None:
        pytest.skip("本机没有 claude")


async def _setup(store, engine_factory, target: Path):
    _require_claude()
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=CLAUDE,
            name="Claude Code",
            adapter_id="claude_code",
            enabled=True,
            env_template={INPUT_FORMAT_ENV: "stream-json"},
        )
    )
    engine = await engine_factory()

    wf = WorkflowDefinition(name="perm-hook", max_concurrent_tasks=2)
    await store.workflows.create(wf)
    await store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=GraphSpec(
                nodes=[
                    NodeDefinition(
                        node_id="H1", name="危险操作", role="执行者",
                        system_prompt="你只做用户要求的事，不要解释。",
                        profiles=[
                            ExecutionProfile(
                                profile_id="p1", model_name="", harness_ref=CLAUDE,
                                # default = 遇事就问我 —— 这正是要验的模式
                                permission_mode="default",
                            )
                        ],
                    )
                ],
                edges=[],
            ),
            is_published=True,
        ),
        publish=True,
        expected_revision_seq=0,
    )
    return engine, wf


async def _wait_for(predicate, *, timeout: float = 180.0, interval: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def _run_with_decision(
    store, engine_factory, tmp_path: Path, *, approve: bool, target: Path
) -> str:
    engine, wf = await _setup(store, engine_factory, target)
    result = await launch_task(
        store=store,
        workflow_id=wf.workflow_id,
        input_payload={
            "task": f"用 Bash 工具执行：echo probe > {target} 。只做这一件事，不要解释。"
        },
    )
    task_id = result.task.task_id

    async def _approval_ready():
        items = await engine.approvals.open_items()
        return items[0] if items else None

    approval = await _wait_for(_approval_ready, timeout=180.0)
    assert approval is not None, (
        "真实 claude 没有发出权限请求——要么握手没生效，要么这条操作没触发询问"
    )
    # harness 给的动作描述里必须能看到那条命令，否则用户不知道该不该批
    assert "echo probe" in approval.action, f"动作描述不完整：{approval.action}"

    delivery = await engine.approve(approval.approval_id, approve=approve)
    assert delivery.delivered, f"决定没送达 harness：{delivery.detail}"
    assert delivery.status == (
        ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
    )

    async def _terminal():
        t = await store.tasks.get_task(task_id)
        return t if t.observed_state in (
            TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED
        ) else None

    task = await _wait_for(_terminal, timeout=240.0)
    assert task is not None, "答复之后任务没有走到终态"
    stages = await store.tasks.list_stages(task_id)
    assert stages[0].observed_state == StageState.SUCCEEDED, (
        f"阶段终态 {stages[0].observed_state.value}；"
        f"原因 {stages[0].blocked_reason or stages[0].status_reason}"
    )
    return task_id


@pytest.mark.timeout(600)
async def test_approved_command_actually_runs(store, engine_factory, tmp_path):
    """批准之后命令必须真的执行。

    这条曾经是坏的：审批收到了、用户也批了、``delivered`` 也是 true，
    **命令却没执行**——因为 argv 里硬编码了 ``--permission-prompts none``，
    CLI 早已自行拒绝；而且答复帧的形状也不对（少了 hookSpecificOutput 包装）。
    两个 bug 的现象一模一样，都只能靠「文件到底建没建」这个外部事实区分出来。
    """
    target = tmp_path / "approved.txt"
    await _run_with_decision(store, engine_factory, tmp_path, approve=True, target=target)
    assert target.exists(), "批准之后命令没有执行——权限答复没有真正生效"
    assert "probe" in target.read_text()


@pytest.mark.timeout(600)
async def test_denied_command_does_not_run(store, engine_factory, tmp_path):
    """拒绝之后命令必须**不**执行。

    这条是安全底线：拒绝若拦不住，整套审批就是装饰。
    """
    target = tmp_path / "denied.txt"
    await _run_with_decision(store, engine_factory, tmp_path, approve=False, target=target)
    assert not target.exists(), (
        "拒绝之后命令仍然执行了——审批没有约束力，这比没有审批更危险"
    )


@pytest.mark.timeout(300)
async def test_probe_reports_the_hook_only_on_the_control_channel(store, engine_factory, tmp_path):
    """能力探测必须如实反映当前通道：只有流式输入通道下才声明有钩子。"""
    _require_claude()
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=CLAUDE, name="Claude Code", adapter_id="claude_code",
            enabled=True, env_template={INPUT_FORMAT_ENV: "stream-json"},
        )
    )
    engine = await engine_factory()
    caps = await engine.harness.capabilities(CLAUDE)
    assert caps.permission_hook is True, "控制通道下必须声明有权限钩子"
    assert caps.interact is True
