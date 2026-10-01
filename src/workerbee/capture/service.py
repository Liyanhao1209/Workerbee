"""流程捕获（Graph Capture）的核心编排（WF-03、D-12、AC-16）。

分层纪律与 ``assistant/`` 相同：**本模块不 import server 层**。捕获执行需要的
两个系统写入口（建临时 Workflow、发射任务）以回调协议声明，由 server 层在
装配时注入既有服务/内核的真实实现——捕获跑的就是一次正常执行，调度、审批、
暂停、事件与产物全部免费获得，而不是另造一条会漂移的「捕获通道」。

三件事各自独立：
1. ``create_run``：建单节点临时 Workflow（名称前缀「捕获·」）+ 发射任务；
2. 材料汇编在 :mod:`.material`（只读）；
3. 草案合成在 :mod:`.synthesis`（显式触发的一次模型调用）。

「从既有任务补捕获」「多模型协作捕获」明确不做（计划 §9）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from ..core.domain.base import new_id
from ..core.domain.node import ExecutionProfile, NodeDefinition
from ..core.domain.task import TaskState
from ..core.domain.workflow import GraphSpec, RevisionSource
from ..data.event_log import EventActor, EventScope, EventType
from ..data.store import Store
from .material import CaptureMaterial, assemble_material

__all__ = [
    "CaptureService",
    "CaptureError",
    "CaptureRunHooks",
    "CAPTURE_NAME_PREFIX",
    "PLAN_GUIDANCE",
]

#: 捕获专用 Workflow 的名称前缀。用户能一眼认出它，流程列表默认把它藏起来。
CAPTURE_NAME_PREFIX = "捕获·"

#: 捕获节点的引导语：请模型**显式**输出执行计划。计划是 WF-03 允许使用的资料，
#: 前提是它出现在输出正文里，而不是私有推理链里。
PLAN_GUIDANCE = (
    "你在执行一个「流程捕获」任务：系统会把你这次的工作过程整理成可复用的流程草案。"
    "请在输出正文里用一个小节（标题含「计划」）显式写出你的执行计划与阶段划分，"
    "然后按计划执行。注意：只有正文里显式写出的内容会被采用，思考过程不会被使用。"
)


class CaptureError(RuntimeError):
    """捕获用例失败的基类。消息是大白话中文，由服务层翻译为 HTTP 400。"""

    def __init__(self, detail: str, *, hint: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class CaptureRunHooks(Protocol):
    """捕获执行的系统写入口。server 层注入既有实现，capture 模块不感知它们来自哪。"""

    async def create_workflow(self, *, name: str, description: str | None) -> str:
        """创建 Workflow 定义，返回 workflow_id。"""
        ...

    async def save_revision(
        self,
        *,
        workflow_id: str,
        graph: GraphSpec,
        publish: bool,
        source: RevisionSource,
        note: str | None,
    ) -> None:
        """保存修订（捕获执行用 publish=True：临时 Workflow 必须可发射）。"""
        ...

    async def submit(
        self,
        *,
        workflow_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """发射任务，返回 ``{"accepted": bool, "task_id": str|None, ...}``。"""
        ...


#: 任务的终态 → run 状态的映射。任务表才是执行状态的事实源，
#: run 的 status 只是读取时从它收敛出来的台账。
_FAILED_TASK_STATES = {TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED}


class CaptureService:
    """捕获用例的编排核心。"""

    def __init__(
        self,
        *,
        store: Store,
        hooks: CaptureRunHooks,
        redactor: Callable[[Any], Any] | None = None,
        material_budget: int = 12_000,
    ) -> None:
        self.store = store
        self.hooks = hooks
        self.redactor = redactor
        self.material_budget = material_budget

    # ------------------------------------------------------------------
    # 捕获任务
    # ------------------------------------------------------------------

    async def create_run(
        self,
        *,
        name: str,
        instructions: str,
        harness_ref: str,
        model_name: str | None = None,
        credential_ref: str | None = None,
    ) -> dict[str, Any]:
        """新建捕获任务：建临时 Workflow → 落 run 台账 → 发射任务。

        顺序里藏着一条纪律：**run 台账先落库，再发射**。发射失败时这条 run
        以 failed 状态留在列表里（用户看得到「这个捕获没跑起来」），而不是
        悄悄消失、只留下一个没人认领的临时 Workflow。
        """
        harness = await self.store.registry.get_harness(harness_ref)
        if harness is None:
            raise CaptureError(
                f"harness「{harness_ref}」没有登记",
                hint="到 注册表 → Harness 先登记，再回到这里选择",
            )
        if not harness.enabled:
            raise CaptureError(f"harness「{harness.name}」已停用，换个可用的")
        if credential_ref:
            cred = await self.store.registry.get_credential(credential_ref)
            if cred is None:
                raise CaptureError(f"凭据「{credential_ref}」不存在")
            if cred.revoked:
                raise CaptureError(f"凭据「{cred.label}」已被撤销，恢复它或换一条")

        node = NodeDefinition(
            name="捕获执行",
            role="被捕获的执行者",
            system_prompt=PLAN_GUIDANCE,
            profiles=[
                ExecutionProfile(
                    harness_ref=harness_ref,
                    model_name=model_name or "",
                    credential_ref=credential_ref,
                )
            ],
        )
        graph = GraphSpec(nodes=[node])

        workflow_id = await self.hooks.create_workflow(
            name=f"{CAPTURE_NAME_PREFIX}{name}",
            description=f"流程捕获的执行载体：{instructions[:200]}",
        )
        await self.hooks.save_revision(
            workflow_id=workflow_id,
            graph=graph,
            publish=True,
            source=RevisionSource.GRAPH_CAPTURE,
            note="捕获任务的临时单节点流程；跑完后保留供回看，不进默认流程列表",
        )

        profile_snapshot = {
            "harness_ref": harness_ref,
            "model_name": model_name or "",
            "credential_ref": credential_ref,
            "instructions": instructions,
        }
        run = await self.store.capture.create_run(
            run_id=new_id(),
            name=name,
            workflow_id=workflow_id,
            task_id=None,
            profile=profile_snapshot,
        )

        try:
            result = await self.hooks.submit(
                workflow_id=workflow_id,
                input_payload={"task": instructions},
                idempotency_key=f"capture:{run['run_id']}",
            )
        except ValueError as exc:
            await self.store.capture.update_run_status(run["run_id"], "failed")
            raise CaptureError(f"捕获任务发射失败：{exc}") from exc
        if not result.get("accepted"):
            await self.store.capture.update_run_status(run["run_id"], "failed")
            raise CaptureError(
                "捕获任务没有通过发射校验",
                hint="打开该捕获的临时流程看校验报告，逐项修正后换个候选重试",
            )

        task_id = result.get("task_id")
        run = await self._bind_task(run["run_id"], task_id)
        await self.store.events.append(
            scope=EventScope.CAPTURE,
            type=EventType.CAPTURE_RUN_CREATED,
            actor=EventActor.USER,
            scope_id=run["run_id"],
            task_id=task_id,
            payload={
                "name": name,
                "workflow_id": workflow_id,
                "task_id": task_id,
                "harness_ref": harness_ref,
                "model_name": model_name or None,
                "credential_ref": credential_ref,
            },
        )
        return run

    async def _bind_task(self, run_id: str, task_id: str | None) -> dict[str, Any]:
        await self.store.db.execute(
            "UPDATE capture_run SET task_id=?, updated_at=datetime('now') WHERE run_id=?",
            (task_id, run_id),
        )
        run = await self.store.capture.get_run(run_id)
        assert run is not None  # 刚创建
        return run

    async def list_runs(self) -> list[dict[str, Any]]:
        runs = await self.store.capture.list_runs()
        return [await self._sync_status(r) for r in runs]

    async def get_run(self, run_id: str) -> dict[str, Any]:
        run = await self.store.capture.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return await self._sync_status(run)

    async def run_detail(self, run_id: str) -> dict[str, Any]:
        """捕获记录详情：台账 + 任务实时状态 + 材料汇编摘要 + 已生成的草案。

        材料汇编是只读操作，每次详情请求都现算——材料跟着事件走，
        不落库就不会过期。
        """
        run = await self.get_run(run_id)
        task_state: str | None = None
        material_summary: dict[str, Any] | None = None
        if run["task_id"]:
            task = await self.store.tasks.get_task(run["task_id"])
            task_state = task.observed_state.value if task is not None else None
            material = await self.assemble(run["task_id"])
            material_summary = {
                "tool_calls": material.tool_call_count,
                "artifacts": material.artifact_count,
                "has_plan": material.has_plan,
                "chars": material.total_chars,
                "trimmed": material.trimmed,
            }
        drafts = await self.store.capture.list_drafts_for_run(run_id)
        return {
            "run": run,
            "task_state": task_state,
            "material": material_summary,
            "drafts": drafts,
        }

    async def assemble(self, task_id: str) -> CaptureMaterial:
        return await assemble_material(
            self.store,
            task_id,
            budget_chars=self.material_budget,
            redactor=self.redactor,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _sync_status(self, run: dict[str, Any]) -> dict[str, Any]:
        """把 run 状态向任务的真实状态收敛（单向：running → completed|failed）。"""
        if run["status"] != "running" or not run["task_id"]:
            return run
        task = await self.store.tasks.get_task(run["task_id"])
        if task is None:
            return run
        if task.observed_state == TaskState.SUCCEEDED:
            if await self.store.capture.update_run_status(run["run_id"], "completed"):
                run["status"] = "completed"
        elif task.observed_state in _FAILED_TASK_STATES:
            if await self.store.capture.update_run_status(run["run_id"], "failed"):
                run["status"] = "failed"
        return run
