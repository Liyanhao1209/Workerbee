"""捕获材料汇编的单测（WF-03、D-12）。

覆盖四条硬边界：排除推理链、预算裁剪按优先级且留痕、脱敏、显式计划提取；
外加捕获仓储（迁移 10 的两张表）的读写与 CAS。
"""

from __future__ import annotations

import pytest

from workerbee.capture import assemble_material
from workerbee.capture.material import CaptureMaterial
from workerbee.capture.service import CaptureError, CaptureService
from workerbee.core.domain.artifact import ArtifactKind, ArtifactProducer
from workerbee.data.event_log import EventScope, EventType
from workerbee.data.redact import redact_text

pytestmark = pytest.mark.unit


async def _append(store, type_: EventType, payload: dict, *, task_id: str = "t1") -> int:
    return await store.events.append(
        scope=EventScope.ATTEMPT,
        type=type_,
        scope_id="at1",
        task_id=task_id,
        payload=payload,
    )


async def _put_artifact(store, content: str, *, summary: str = "摘要", task_id: str = "t1"):
    return await store.artifacts.put(
        content,
        kind=ArtifactKind.TEXT,
        producer=ArtifactProducer(
            task_id=task_id, stage_id="s1", attempt_seq=1, node_id="A", attempt_id="at1"
        ),
        summary=summary,
        media_type="text/plain",
    )


async def test_reasoning_is_excluded(store):
    """ATTEMPT_REASONING 是私有推理链：连读都不读，内容不得出现在材料里。"""
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "写一个备份脚本"})
    await _append(store, EventType.ATTEMPT_REASONING, {"text": "这一段是私密思考链内容"})
    use_id = await _append(
        store,
        EventType.ATTEMPT_TOOL_USE,
        {"tool_name": "Write", "tool_use_id": "tu-1", "target": "/tmp/backup.sh"},
    )
    await _append(
        store, EventType.ATTEMPT_TOOL_RESULT,
        {"tool_use_id": "tu-1", "is_error": False, "content": "ok"},
    )

    material = await assemble_material(store, "t1")

    assert "私密思考链" not in material.text
    assert "写一个备份脚本" in material.text
    assert material.tool_call_count == 1
    assert "/tmp/backup.sh" in material.text
    assert "成功" in material.text
    # 输入与工具调用事件都是可引用的证据
    assert f"E{use_id}" in material.evidence_set()


async def test_material_is_redacted_again_before_synthesis(store):
    """事件落库时已脱敏，汇编时再脱敏一次——密钥形态不得进材料正文。"""
    await _append(
        store, EventType.ATTEMPT_INPUT,
        {"user_input": "用这个 key 部署：sk-livekey123456789"},
    )
    material = await assemble_material(store, "t1", redactor=redact_text)
    assert "sk-livekey123456789" not in material.text
    assert "<redacted:shape>" in material.text


async def test_plan_section_is_extracted_from_output(store):
    """模型按引导在输出正文里写了「计划」小节 → 提取为显式计划材料。"""
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "整理日志"})
    await _put_artifact(
        store,
        "做了一些事。\n\n## 执行计划\n1. 先扫描日志\n2. 再聚合\n\n## 结果\n完成了。",
    )
    material = await assemble_material(store, "t1")
    assert material.has_plan is True
    assert "先扫描日志" in material.text
    assert "## 结果" not in material.text.split("# 模型输出的执行计划")[-1]


async def test_no_plan_is_honest_none(store):
    """没有显式计划就如实没有（此时草案的节点划分几乎全是推断）。"""
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "随便做点啥"})
    await _put_artifact(store, "直接给了结果，没有计划段。")
    material = await assemble_material(store, "t1")
    assert material.has_plan is False
    assert "执行计划" not in material.text


async def test_budget_trimming_follows_priority_and_leaves_trace(store):
    """超预算时先裁产物摘要、再裁计划，工具调用序列最后才动；裁剪全部留痕。"""
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "任务"})
    for i in range(10):
        await _append(
            store, EventType.ATTEMPT_TOOL_USE,
            {"tool_name": f"Tool{i}", "tool_use_id": f"tu-{i}", "target": f"/f{i}"},
        )
    art = await _put_artifact(
        store, "## 执行计划\n" + "计划内容。" * 50, summary="产物摘要。" * 50
    )

    # 预算够装输入+工具调用+计划，装不下产物摘要 → 只裁产物
    material = await assemble_material(store, "t1", budget_chars=700)
    assert material.total_chars <= 700
    assert any("产物" in t for t in material.trimmed)
    assert material.has_plan is True, "产物优先于计划被裁"
    assert material.tool_call_count == 10
    # 被裁掉的分区不再贡献 evidence：复核不得采信「看见过」拿不到的材料
    assert art.artifact_id not in material.evidence_set()
    assert f"A{art.artifact_id}" not in material.evidence_set()

    # 预算连计划也装不下 → 计划也被裁，且 has_plan 如实回落
    tighter = await assemble_material(store, "t1", budget_chars=500)
    assert tighter.total_chars <= 500
    assert any("计划" in t for t in tighter.trimmed)
    assert tighter.has_plan is False
    # 工具调用序列是最高优先级：它还在
    assert "工具调用序列" in tighter.text


async def test_tool_sequence_is_trimmed_last_and_from_oldest(store):
    """工具调用序列本身超预算时，从最早的调用开始丢，并如实记笔数。"""
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "任务"})
    for i in range(40):
        await _append(
            store, EventType.ATTEMPT_TOOL_USE,
            {"tool_name": f"Tool{i}", "tool_use_id": f"tu-{i}", "target": f"/f{i}"},
        )
    material = await assemble_material(store, "t1", budget_chars=400)
    assert material.total_chars <= 400
    assert any("工具调用序列" in t for t in material.trimmed)
    assert "最早的" in material.text
    # 保住的是最近的行为
    assert "Tool39" in material.text


async def test_usage_is_summarized_without_zero_fill(store):
    """用量来自 attempt 记录；取不到的字段保持缺失（未知 ≠ 0）。"""
    from tests.conftest import make_stage, make_task
    from workerbee.core.domain.task import Attempt, Usage

    task = make_task()
    stage = make_stage()
    await store.tasks.create_task_with_stages(task, [stage])
    await store.tasks.create_attempt(
        Attempt(
            stage_id=stage.stage_id, task_id=task.task_id, node_id="A",
            attempt_seq=1, profile_id="p1",
            usage=Usage(input_tokens=100, output_tokens=50),
            started_at=task.created_at, ended_at=task.created_at,
        )
    )
    await _append(store, EventType.ATTEMPT_INPUT, {"user_input": "任务"})
    material = await assemble_material(store, "t1")
    assert material.usage is not None
    assert material.usage["input_tokens"] == 100
    assert material.usage["attempts"] == 1
    assert material.usage["duration_s"] == 0.0


# ===========================================================================
# 捕获仓储（迁移 10）
# ===========================================================================


async def test_capture_repository_roundtrip_and_cas(store):
    run = await store.capture.create_run(
        run_id="r1", name="捕获A", workflow_id="w1", task_id="t1",
        profile={"harness_ref": "h1"},
    )
    assert run["status"] == "running"
    assert await store.capture.get_run("r1") == run

    # 状态单向收敛：running → completed；之后再 failed 不再生效
    assert await store.capture.update_run_status("r1", "completed") is True
    assert await store.capture.update_run_status("r1", "failed") is False

    draft = await store.capture.create_draft(
        draft_id="d1", run_id="r1", payload={"name": "流程"}, validation={"ok": True}
    )
    assert draft["status"] == "pending"
    assert await store.capture.decide_draft("d1", to_status="adopted", adopted_ref="w2") is True
    # CAS：已决定的草案不能再被决定（重复采用不产生第二份产物）
    assert await store.capture.decide_draft("d1", to_status="rejected") is False
    stored = await store.capture.get_draft("d1")
    assert stored["status"] == "adopted"
    assert stored["adopted_ref"] == "w2"

    assert await store.capture.capture_workflow_ids() == {"w1"}


# ===========================================================================
# 捕获执行入口（临时 Workflow + 发射走注入的钩子）
# ===========================================================================


class _Hooks:
    """记录捕获服务发起的系统写，断言「真的建了流程、真的发射了」。"""

    def __init__(self, *, accepted: bool = True) -> None:
        self.workflows: list[dict] = []
        self.revisions: list[dict] = []
        self.submissions: list[dict] = []
        self.accepted = accepted

    async def create_workflow(self, *, name, description):
        self.workflows.append({"name": name, "description": description})
        return f"wf-{len(self.workflows)}"

    async def save_revision(self, *, workflow_id, graph, publish, source, note):
        self.revisions.append(
            {"workflow_id": workflow_id, "graph": graph, "publish": publish,
             "source": source, "note": note}
        )

    async def submit(self, *, workflow_id, input_payload, idempotency_key):
        self.submissions.append(
            {"workflow_id": workflow_id, "input_payload": input_payload,
             "idempotency_key": idempotency_key}
        )
        return {"accepted": self.accepted, "task_id": "task-1" if self.accepted else None}


async def _harness(store, harness_id: str = "h1") -> None:
    from workerbee.core.domain.registry import HarnessRegistration

    await store.registry.upsert_harness(
        HarnessRegistration(harness_id=harness_id, name=harness_id, adapter_id="fake")
    )


async def test_create_run_builds_temp_workflow_and_submits(store):
    await _harness(store)
    hooks = _Hooks()
    svc = CaptureService(store=store, hooks=hooks)

    run = await svc.create_run(
        name="备份日志", instructions="把 /var/log 打包", harness_ref="h1", model_name="m1"
    )

    # 真的建了单节点临时 Workflow，名称带「捕获·」前缀，修订已发布（否则发射不了）
    assert hooks.workflows[0]["name"] == "捕获·备份日志"
    rev = hooks.revisions[0]
    assert rev["publish"] is True
    assert rev["source"].value == "graph_capture"
    nodes = rev["graph"].nodes
    assert len(nodes) == 1
    assert "显式写出你的执行计划" in (nodes[0].system_prompt or "")
    assert nodes[0].profiles[0].harness_ref == "h1"
    assert nodes[0].profiles[0].model_name == "m1"

    # 真的经既有提交路径发射了，幂等键防止网络重送产生两次执行
    assert hooks.submissions[0]["input_payload"] == {"task": "把 /var/log 打包"}
    assert hooks.submissions[0]["idempotency_key"] == f"capture:{run['run_id']}"
    assert run["task_id"] == "task-1"
    assert run["status"] == "running"

    # 留痕：CAPTURE_RUN_CREATED
    events = await store.events.tail()
    created = [e for e in events if e["type"] == EventType.CAPTURE_RUN_CREATED.value]
    assert len(created) == 1
    assert created[0]["payload"]["workflow_id"] == run["workflow_id"]


async def test_create_run_rejects_unknown_harness(store):
    svc = CaptureService(store=store, hooks=_Hooks())
    with pytest.raises(CaptureError, match="没有登记"):
        await svc.create_run(name="x", instructions="y", harness_ref="ghost")


async def test_failed_submission_leaves_failed_run_not_stray_workflow(store):
    """发射被拒：run 如实标 failed（看得见「没跑起来」），不产生孤立的可见流程。"""
    await _harness(store)
    hooks = _Hooks(accepted=False)
    svc = CaptureService(store=store, hooks=hooks)
    with pytest.raises(CaptureError, match="发射校验"):
        await svc.create_run(name="x", instructions="y", harness_ref="h1")
    run = (await store.capture.list_runs())[0]
    assert run["status"] == "failed"
    assert run["task_id"] is None
    # 这个临时 Workflow 已关联进 capture_run，默认流程列表据此藏得住它
    assert run["workflow_id"] in await store.capture.capture_workflow_ids()


async def test_run_status_converges_from_task(store):
    """run 状态从任务的真实状态单向收敛。"""
    from tests.conftest import make_task
    from workerbee.core.domain.task import TaskState

    await _harness(store)
    hooks = _Hooks()
    svc = CaptureService(store=store, hooks=hooks)
    run = await svc.create_run(name="x", instructions="y", harness_ref="h1")

    task = make_task(task_id="task-1", workflow_id=run["workflow_id"])
    await store.tasks.create_task_with_stages(task, [])

    # 任务还在跑：run 仍是 running
    assert (await svc.get_run(run["run_id"]))["status"] == "running"
    # 任务成功：run 收敛为 completed
    await store.tasks.update_task("task-1", to_state=TaskState.SUCCEEDED)
    assert (await svc.get_run(run["run_id"]))["status"] == "completed"
