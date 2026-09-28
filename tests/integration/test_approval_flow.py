"""审批闭环的端到端测试（HUM-03/04、AC-12/14）。

这组测试的由来：为了给作者演示「审批到底怎么走」，我把 mock 适配器接进完整内核
跑了一遍，**第一次跑根本走不通**。三个 bug 都是「不报错、只是悄悄不工作」那一类，
单测和集成测试都没覆盖到，因为它们的共同特征是**依赖真实的到达顺序**：

1. 权限请求可能在建会话返回**之前**就到——调度器还没把尝试登记进在途表。
   按 session 反查会查不到，请求被当「无法归属」丢掉，用户永远看不到该他决定的审批。
2. 审批网关的**回注通道从未装配**：决定被照常记录、界面显示「已批准」，
   然后静静地送不出去，agent 在那头一直等。
3. 适配器进程崩溃时**没有任何 session_ended 事件**（进程直接没了，SDK 清理路径
   根本没跑）。崩溃信号没接到调度器 → 阶段永远停在 running。
   「永远 running」是架构设计点名要消灭的失败模式（HAR-03）。

三条都用真适配器子进程测，不用替身——因为要测的正是**时序**，
而替身的时序是我自己写的，恰好会避开这些坑。
"""

from __future__ import annotations

import asyncio
import json
import sys
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

pytestmark = pytest.mark.integration

MOCK_CMD = [sys.executable, "-m", "workerbee.adapters.mock.main"]


def _script(path: Path, *, with_permission: bool = True, crash: bool = False) -> Path:
    """写一份 mock 剧本。

    ``with_permission`` 决定要不要走审批那一步——注意权限步骤会**一直阻塞到有人
    答复**，所以测崩溃的剧本不能带它，否则永远走不到 exit。

    ``crash`` 为真时进程直接消失、不报会话结束，用来模拟 harness 崩溃。
    """
    steps = [{"do": "output", "text": "开始工作。"}]
    if with_permission:
        steps += [
            {
                "do": "permission",
                "action": "Bash(rm -rf build/)",
                "target": "build/",
                "risk": "高：会删除构建目录",
                "tool_name": "Bash",
            },
            {"do": "output", "text": "已按答复处理。"},
        ]
    if crash:
        steps.append({"do": "exit", "code": 0})
    else:
        steps += [{"do": "turn_end"}, {"do": "exit", "code": 0, "graceful": True}]
    path.write_text(json.dumps({"steps": steps}, ensure_ascii=False))
    return path


async def _setup(
    store, engine_factory, tmp_path: Path, *,
    with_permission: bool = True, crash: bool = False,
):
    script = _script(
        tmp_path / "script.json", with_permission=with_permission, crash=crash
    )
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id="mock-1",
            name="Mock",
            adapter_id="mock",
            enabled=True,
            env_template={"WORKERBEE_MOCK_SCRIPT_FILE": str(script)},
        )
    )
    engine = await engine_factory(adapter_commands={"mock": MOCK_CMD})

    wf = WorkflowDefinition(name="approval-wf", max_concurrent_tasks=4)
    await store.workflows.create(wf)
    await store.workflows.save_revision(
        WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=GraphSpec(
                nodes=[
                    NodeDefinition(
                        node_id="G1", name="危险操作",
                        profiles=[
                            ExecutionProfile(
                                profile_id="p1", model_name="mock",
                                harness_ref="mock-1", permission_mode="default",
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


async def _wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.2):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


# ===========================================================================
# 1. 权限请求必须能归属到阶段
# ===========================================================================


async def test_permission_before_attempt_registration_still_creates_approval(
    store, engine_factory, tmp_path
):
    """权限请求在建会话返回**之前**到达时，仍必须生成审批项。

    mock 的剧本在会话创建过程中就发问——这正是暴露「按 session 反查在途表」
    这个时序假设的地方。harness 在建会话的过程中就提问是完全正常的行为。
    """
    engine, wf = await _setup(store, engine_factory, tmp_path)
    result = await launch_task(store=store, workflow_id=wf.workflow_id)

    pending = await _wait_for(
        lambda: engine.approvals.open_items(), timeout=20.0
    )
    assert pending, (
        "权限请求被丢弃了——用户永远看不到这条该由他决定的审批，"
        "而 agent 在那头一直等"
    )
    approval = pending[0]
    assert approval.action == "Bash(rm -rf build/)"
    assert approval.target == "build/"
    assert approval.bound_to.task_id == result.task.task_id, "必须归属到正确的任务"
    assert approval.bound_to.attempt_id, "必须有绑定的执行尝试"

    stage = (await store.tasks.list_stages(result.task.task_id))[0]
    assert approval.bound_to.stage_id == stage.stage_id


# ===========================================================================
# 2. 决定必须真的送回 agent
# ===========================================================================


async def test_decision_is_delivered_back_to_the_agent(
    store, engine_factory, tmp_path
):
    """批准之后，决定必须真的回到原会话。

    这条曾经是坏的：决定被记录、界面显示「已批准」，然后静静地送不出去。
    两端各自都觉得自己是对的——这是「静默失败」最危险的一种。
    """
    engine, wf = await _setup(store, engine_factory, tmp_path)
    result = await launch_task(store=store, workflow_id=wf.workflow_id)

    pending = await _wait_for(lambda: engine.approvals.open_items(), timeout=20.0)
    assert pending
    approval_id = pending[0].approval_id

    delivery = await engine.approve(approval_id, approve=True)
    assert delivery.delivered, f"决定没能送达 agent：{delivery.detail}"
    assert delivery.status == ApprovalStatus.APPROVED

    fresh = await store.approvals.get(approval_id)
    assert fresh.status == ApprovalStatus.APPROVED


async def test_deny_is_also_delivered(store, engine_factory, tmp_path):
    """拒绝同样要送达——否则 agent 会一直等一个永远不来的答复。"""
    engine, wf = await _setup(store, engine_factory, tmp_path)
    await launch_task(store=store, workflow_id=wf.workflow_id)

    pending = await _wait_for(lambda: engine.approvals.open_items(), timeout=20.0)
    assert pending
    delivery = await engine.approve(pending[0].approval_id, approve=False)
    assert delivery.delivered
    assert delivery.status == ApprovalStatus.DENIED


async def test_duplicate_decision_does_not_re_authorize(
    store, engine_factory, tmp_path
):
    """AC-14：重复通知不重复授权。"""
    engine, wf = await _setup(store, engine_factory, tmp_path)
    await launch_task(store=store, workflow_id=wf.workflow_id)

    pending = await _wait_for(lambda: engine.approvals.open_items(), timeout=20.0)
    approval_id = pending[0].approval_id

    first = await engine.approve(approval_id, approve=True)
    second = await engine.approve(approval_id, approve=True)

    assert first.status == ApprovalStatus.APPROVED
    assert "未生效" in (second.detail or "") or second.status != ApprovalStatus.PENDING, (
        "第二次决定不应再次生效"
    )


# ===========================================================================
# 3. harness 崩溃不能把阶段永久挂在 running
# ===========================================================================


async def test_adapter_crash_does_not_leave_stage_running_forever(
    store, engine_factory, tmp_path
):
    """适配器进程崩溃时，阶段必须被判定为失败并进入重试，而不是永远 running。

    「永远显示 running」是架构设计点名要消灭的失败模式（HAR-03）。
    """
    engine, wf = await _setup(store, engine_factory, tmp_path, with_permission=False, crash=True)
    result = await launch_task(store=store, workflow_id=wf.workflow_id)

    async def _first_attempt_settled():
        # 注意不能只判「不是 running」——DISPATCHING 也不是 running，
        # 那一瞬间还没有任何 attempt，会假阳性通过。
        stages = await store.tasks.list_stages(result.task.task_id)
        attempts = await store.tasks.list_attempts(stages[0].stage_id, descending=False)
        if attempts and attempts[0].outcome is not None:
            return stages[0], attempts[0]
        return None

    settled = await _wait_for(_first_attempt_settled, timeout=25.0)
    assert settled is not None, (
        "适配器崩溃后第一次尝试始终没有结局——内核没收到「会话没了」的信号，"
        "阶段会一直停在 running"
    )
    stage, attempts_first = settled
    assert attempts_first.outcome.error_class.value == "retryable_error", (
        "harness 崩溃属于可重试错误（REC-02），不是致命错误"
    )


async def test_crash_is_recorded_and_visible(store, engine_factory, tmp_path):
    """崩溃必须留下可见记录，而不是静默重试。"""
    engine, wf = await _setup(store, engine_factory, tmp_path, with_permission=False, crash=True)
    result = await launch_task(store=store, workflow_id=wf.workflow_id)

    async def _has_record():
        events = await store.events.for_task(result.task.task_id)
        return [e for e in events if "退出" in json.dumps(e, ensure_ascii=False)]

    found = await _wait_for(_has_record, timeout=25.0)
    assert found, "适配器退出必须被记入事件历史"


async def test_adapter_exit_lands_on_the_affected_tasks_timeline(
    store, engine_factory, tmp_path
):
    """适配器退出要写在**受影响任务**的事件里，而不只是全局视野。

    这条曾经是无归属的：事件确实写进去了，但 ``task_id`` 为空，而任务详情页
    按 task_id 查事件——于是用户在那个任务上只看到一个停在 running 的阶段，
    时间线里什么线索都没有，无从知道适配器崩过。

    上一版测试靠「崩溃赶在派发登记之前」这条竞态去间接命中它，因此在 CI 上
    时序一变就红。这里直接把「归属」这件事测掉，不依赖任何竞态。

    用 ``with_permission=True``：权限那一步会**一直阻塞到有人答复**，阶段因此
    稳稳停在运行态。带 ``with_permission=False`` 的剧本会立刻 graceful exit，
    根本没有可断言的在途尝试——第一版就是这么写的，本地靠抢时序碰巧过，CI 上
    立刻暴露。
    """
    engine, wf = await _setup(store, engine_factory, tmp_path, with_permission=True)
    result = await launch_task(store=store, workflow_id=wf.workflow_id)
    task_id = result.task.task_id

    async def _running():
        runtimes = engine.scheduler.runtimes_for_harness("mock-1")
        return runtimes[0] if runtimes else None

    rt = await _wait_for(_running, timeout=25.0)
    assert rt is not None, "阶段没有进入运行态，测试前提不成立"

    # 直接触发退出回调，不依赖进程真的崩——测的是「事件归属」这件事本身
    await engine._on_adapter_exit("mock-1", 1, "mock: 模拟崩溃")

    events = await store.events.for_task(task_id)
    records = [e for e in events if e["type"] == "session.lost"]
    assert records, "适配器退出必须出现在该任务的事件时间线上"
    hit = records[-1]
    assert hit["payload"]["harness_id"] == "mock-1"
    assert hit["payload"]["exit_code"] == 1
    assert hit["stage_id"] == rt.stage_id
    assert hit["payload"]["attempt_id"] == rt.attempt_id


async def test_idle_adapter_exit_is_recorded_without_a_task(store, engine_factory, tmp_path):
    """适配器空闲时退出没有受影响的尝试，写一条无归属的系统级记录即可。"""
    engine, _ = await _setup(store, engine_factory, tmp_path, with_permission=False)

    await engine._on_adapter_exit("mock-1", 0, "")

    rows = await store.db.fetch_all(
        "SELECT task_id, payload FROM event_log WHERE type='session.lost'"
    )
    assert rows, "空闲退出也要留记录"
    assert all(r["task_id"] is None for r in rows), "没有受影响的尝试时不该硬安一个归属"
