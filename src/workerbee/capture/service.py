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

import json
from typing import Any, Callable, Protocol

from ..assistant.draft import (
    DraftInvalid,
    DraftProposal,
    collect_pending,
    extract_proposal,
    proposal_graph,
)
from ..assistant.service import (
    AssistantConfig,
    BackendFactory,
    _default_backend_factory,
    load_config,
)
from ..core.domain.base import new_id
from ..core.domain.node import ExecutionProfile, NodeDefinition
from ..core.domain.task import TaskState
from ..core.domain.workflow import GraphSpec, RevisionSource
from ..core.graph.validate import ValidationMode, validate
from ..data.event_log import EventActor, EventScope, EventType
from ..data.llm.backend import LLMError, LLMMessage
from ..data.llm.router import LLMRouter
from ..data.store import Store
from .material import CaptureMaterial, assemble_material
from .synthesis import SYSTEM_PROMPT, build_synthesis_prompt, review_basis

__all__ = [
    "CaptureService",
    "CaptureError",
    "CaptureNotConfigured",
    "CaptureLocked",
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


class CaptureNotConfigured(CaptureError):
    """合成用的模型没配置。捕获合成复用助手配置（不另开一套凭据）。"""


class CaptureLocked(CaptureError):
    """凭据库未解锁，读不到合成模型的密钥。"""


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


#: 合成的单次调用超时（秒）。这是一次显式触发的同步调用，不能无限挂住。
SYNTHESIS_TIMEOUT_S = 120.0


class CaptureService:
    """捕获用例的编排核心。

    ``secret_resolver`` 是「取当前凭据库」的回调而不是凭据库本身
    （解锁发生在引擎生命周期中段，与助手同一条纪律）。
    ``backend_factory`` 测试注入假后端；生产默认走 assistant 的装配
    （按 assistant_config 的 api_protocol 选 openai_compat / anthropic）。
    """

    def __init__(
        self,
        *,
        store: Store,
        hooks: CaptureRunHooks,
        secret_resolver: Callable[[], Any] | None = None,
        redactor: Callable[[Any], Any] | None = None,
        backend_factory: BackendFactory | None = None,
        material_budget: int = 12_000,
        call_timeout: float = SYNTHESIS_TIMEOUT_S,
    ) -> None:
        self.store = store
        self.hooks = hooks
        self._secret_resolver = secret_resolver or (lambda: None)
        self.redactor = redactor
        self.backend_factory = backend_factory
        self.material_budget = material_budget
        self.call_timeout = call_timeout
        self._router_cache: tuple[str, LLMRouter] | None = None

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
    # 草案合成（显式触发的一次模型调用）
    # ------------------------------------------------------------------

    async def generate_draft(self, run_id: str) -> dict[str, Any]:
        """从捕获材料合成流程草案并落库。

        前置条件如实检查：run 存在、任务已跑完（终态）。失败一律抛
        :class:`CaptureError`（可重试），不产生半截草案。
        """
        run = await self.get_run(run_id)
        if not run["task_id"]:
            raise CaptureError("这次捕获的任务没有跑起来，没有材料可以合成")
        task = await self.store.tasks.get_task(run["task_id"])
        if task is None:
            raise CaptureError("捕获任务对应的执行记录不存在")
        if task.observed_state not in (
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.BLOCKED,
        ):
            raise CaptureError(
                "任务还在跑，跑完才能生成流程草案",
                hint="到任务详情页看实时进展；结束后再来点「生成流程草案」",
            )

        material = await self.assemble(run["task_id"])
        router, _config = await self._require_backend()

        registry_store = self.store.registry
        harnesses = await registry_store.list_harnesses()
        credentials = await registry_store.list_credentials()
        skills = await registry_store.list_skills()
        tools = await registry_store.list_tools()

        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_synthesis_prompt(
                    material,
                    base_profile=run["profile"],
                    harness_ids=[h.harness_id for h in harnesses],
                    credential_ids=[c.credential_id for c in credentials],
                    skill_ids=[s.skill_id for s in skills],
                    tool_ids=[t.tool_id for t in tools],
                ),
            ),
        ]
        try:
            response = await router.complete(messages, timeout=self.call_timeout)
        except LLMError as exc:
            raise CaptureError(
                f"草案合成的模型调用失败：{exc}",
                hint="模型服务可能暂时不可用；稍后再试。本次执行的结果不受影响",
            ) from exc

        reply = self._redact(response.text)
        proposal = extract_proposal(reply)
        if proposal is None:
            raise CaptureError(
                "模型没有按约定输出流程草案（没有可解析的 workerbee-draft 块）",
                hint="再试一次；反复失败说明当前模型不适合做合成，可在助手设置里换一个",
            )
        if proposal.kind != "workflow":
            raise CaptureError(
                "草案合成只产出整条流程（workflow）的提案，模型给了别的形态",
                hint="再试一次",
            )

        # observed 复核：evidence 不在本次材料里的标注强制降级为 inferred，
        # 降级事实进草案说明与事件——模型不能给自己贴金（D-12）。
        downgrades = review_basis(proposal, material.evidence_set())

        pending = collect_pending(
            proposal,
            harness_ids=[h.harness_id for h in harnesses],
            credential_ids=[c.credential_id for c in credentials],
            skill_ids=[s.skill_id for s in skills],
            tool_ids=[t.tool_id for t in tools],
        )
        # AC-16：与手动草稿、助手提案走同一条校验管线（draft 档）。
        try:
            graph = proposal_graph(proposal)
        except DraftInvalid as exc:
            raise CaptureError(f"模型给出的草案无法构造流程：{exc}", hint="再试一次") from exc
        registry = await registry_store.snapshot()
        report = validate(graph, registry, mode=ValidationMode.DRAFT)

        usage = response.usage
        validation = {
            "ok": report.ok(),
            "summary": report.summary(),
            "error": None,
            "diagnostics": [d.model_dump(mode="json") for d in report.diagnostics],
            "pending_config": pending,
            "downgrades": downgrades,
        }
        draft = await self.store.capture.create_draft(
            draft_id=new_id(),
            run_id=run_id,
            payload=proposal.model_dump(mode="json"),
            validation=validation,
        )
        await self.store.events.append(
            scope=EventScope.CAPTURE,
            type=EventType.CAPTURE_DRAFT_GENERATED,
            actor=EventActor.AI,
            scope_id=run_id,
            task_id=run["task_id"],
            payload={
                "draft_id": draft["draft_id"],
                "backend": response.backend,
                "model": response.model,
                "tokens_in": usage.input_tokens if usage else None,
                "tokens_out": usage.output_tokens if usage else None,
                "validation_summary": validation["summary"],
                "downgrade_count": len(downgrades),
                "material_chars": material.total_chars,
                "material_trimmed": material.trimmed,
                "material_has_plan": material.has_plan,
            },
        )
        return draft

    async def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft = await self.store.capture.get_draft(draft_id)
        if draft is None:
            raise KeyError(draft_id)
        return draft

    # ------------------------------------------------------------------
    # 后端装配（复用助手配置，不另开一套凭据）
    # ------------------------------------------------------------------

    async def _require_backend(self) -> tuple[LLMRouter, AssistantConfig]:
        """按助手的配置装配合成后端链。各种「不可用」都在这里明确报出，
        文案指向助手设置——配置就是同一份，UI 要如实说明这一点。"""
        config = await load_config(self.store.db)
        if not config.enabled:
            raise CaptureNotConfigured(
                "还没有配置合成用的模型",
                hint="捕获合成复用助手的模型配置：到助手面板的设置里打开开关并选择凭据",
            )
        if not config.credential_ref:
            raise CaptureNotConfigured(
                "还没有给助手选模型凭据",
                hint="捕获合成复用助手的模型配置：到助手面板的设置里选一条凭据",
            )
        credential = await self.store.registry.get_credential(config.credential_ref)
        if credential is None:
            raise CaptureNotConfigured(
                "助手配置指向的凭据已经不存在了",
                hint="到助手面板的设置里重新选择一条凭据",
            )
        if credential.revoked:
            raise CaptureNotConfigured(
                f"助手用的凭据「{credential.label}」已被撤销",
                hint="恢复该凭据，或到助手面板的设置里换一条",
            )
        if not credential.secret_locator:
            raise CaptureNotConfigured(
                f"凭据「{credential.label}」没有密钥内容（harness 登录态不能用于合成）",
                hint="建一条含 Base URL 和 Key 的凭据，再到助手设置里选它",
            )
        if not (config.model_override or credential.default_model):
            raise CaptureNotConfigured(
                f"凭据「{credential.label}」没有默认模型名，不知道该用哪个模型合成",
                hint="在助手面板的设置里填一个模型名，或给凭据补上默认模型",
            )
        secrets = self._secret_resolver()
        if secrets is None:
            raise CaptureLocked(
                "凭据库还没有解锁，读不到合成模型的密钥",
                hint="用带口令的方式重启内核（--passphrase 或 WORKERBEE_PASSPHRASE）后重试",
            )

        cache_key = json.dumps(
            {
                "ref": config.credential_ref,
                "locator": credential.secret_locator,
                "base_url": credential.base_url,
                "model": config.model_override or credential.default_model,
                "api_protocol": config.api_protocol,
                "factory": id(self.backend_factory),
            },
            sort_keys=True,
        )
        if self._router_cache is not None and self._router_cache[0] == cache_key:
            return self._router_cache[1], config

        await self.aclose()
        factory = self.backend_factory or _default_backend_factory
        backend = factory(config, credential, secrets)
        router = (
            backend if isinstance(backend, LLMRouter) else LLMRouter([backend], name="capture")
        )
        self._router_cache = (cache_key, router)
        return router, config

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

    def _redact(self, value: Any) -> Any:
        if self.redactor is None or value is None:
            return value
        return self.redactor(value)

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
