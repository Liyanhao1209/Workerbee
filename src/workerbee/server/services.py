"""Service 层：路由与内核之间的**唯一**一层。

边界怎么划：

- **路由层**只做三件事——解析参数（pydantic）、调本层的某个方法、返回已声明的响应模型。
  路由里没有 if 分支的业务判断，也不 import 任何仓储或领域实体。
- **本层**承载 API 独有的用例编排。运行期的用例（发射、暂停、恢复、删除、启停、调序、
  审批、存储报告）一行都不重复实现，直接转发给 ``Engine`` —— 内核才是它们的权威实现；
  内核没有的用例（Workflow 定义与修订、注册表、模板、历史清理入口）在这里落地，
  且只经仓储公开接口读写。
- 领域规则不下沉到网关：这里不判断「阶段能不能迁移」，只把内核返回的结论原样搬运，
  并在必要处把异常翻译成 HTTP 语义（``NotFound`` / ``BadRequest`` / ``RevisionConflict``）。

**凭据不出网关**：本层从不读取 Secret Store 的值，也不把 ``CredentialRef`` 之外的
任何凭据材料塞进响应。响应模型在 ``schemas`` 里，形状本来就是「只有引用」。
创建凭据时随请求提交上来的密钥本体只做一次转手（请求 → ``Engine.store_secret``），
不进事件、不落日志、不在响应里回显。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from ..app import Engine
from ..core.domain import (
    ApprovalStatus,
    CredentialRef,
    HarnessRegistration,
    InstantiationReport,
    SkillDoc,
    TaskState,
    ToolLaunch,
    ToolSpec,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
    new_id,
    now_iso,
)
from ..core.domain.task import StageState
from ..core.domain.template import MissingBinding, Template, TemplateKind
from ..core.graph.validate import ValidationReport, validate
from ..assistant import AssistantConfig, AssistantError
from ..data.db import ConflictError
from ..data.event_log import EventActor, EventScope, EventType
from . import schemas as S

__all__ = [
    "ServiceError",
    "NotFound",
    "BadRequest",
    "RevisionConflict",
    "SubmissionRejected",
    "Services",
]


# ===========================================================================
# 异常：网关的三种失败语义
# ===========================================================================


class ServiceError(Exception):
    """服务层失败的公共基类。``status_code`` 由 app.py 统一映射为响应。"""

    status_code = 400
    hint: str | None = None

    def __init__(self, detail: str, *, hint: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if hint:
            self.hint = hint

    def to_response(self) -> S.ErrorResponse:
        return S.ErrorResponse(detail=self.detail, hint=self.hint)


class NotFound(ServiceError):
    status_code = 404


class BadRequest(ServiceError):
    status_code = 400


class SubmissionRejected(ServiceError):
    """发射校验未通过（WF-05）。

    **422 而不是 400**：请求本身合法（字段类型、必填项都对），是「内容不可执行」。
    响应体是**完整校验报告**——每条结论都能定位到节点／连线／槽位，
    而不是一句笼统的「参数错误」。
    """

    status_code = 422

    def __init__(self, report: ValidationReport) -> None:
        errors = report.errors()
        super().__init__(
            f"发射被拒绝：{len(errors)} 项错误使该流程当前不可执行",
            hint="按报告中的 node_id / edge / slot 逐项修正后重试",
        )
        self.report = report

    def to_response(self) -> S.SubmitResponse:
        return S.SubmitResponse(accepted=False, report=self.report)


class RevisionConflict(ServiceError):
    """CAS 冲突（D-02）：附最新版本，让用户看到差异而不是干猜。"""

    status_code = 409

    def __init__(
        self,
        detail: str,
        *,
        workflow_id: str,
        latest_revision_seq: int,
        latest_revision: WorkflowRevision | None = None,
    ) -> None:
        super().__init__(
            detail, hint="拉取最新修订，合并后带上新的 base_revision_seq 重试"
        )
        self.workflow_id = workflow_id
        self.latest_revision_seq = latest_revision_seq
        self.latest_revision = latest_revision

    def to_response(self) -> S.ConflictResponse:
        return S.ConflictResponse(
            detail=self.detail,
            hint=self.hint,
            workflow_id=self.workflow_id,
            latest_revision_seq=self.latest_revision_seq,
            latest_revision=_revision_out(self.latest_revision)
            if self.latest_revision
            else None,
        )


# ===========================================================================
# 公共基类
# ===========================================================================


class _Service:
    """各服务的公共依赖。持有 Engine，也就持有 Store。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @property
    def store(self) -> Any:
        return self.engine.store

    # ---- 公共取数 ----

    async def _require_workflow(self, workflow_id: str) -> WorkflowDefinition:
        wf = await self.store.workflows.get(workflow_id)
        if wf is None:
            raise NotFound(f"Workflow 不存在: {workflow_id}")
        return wf

    async def _require_revision(self, workflow_id: str) -> WorkflowRevision:
        rev = await self.store.workflows.get_current_revision(workflow_id)
        if rev is None:
            raise NotFound(f"Workflow「{workflow_id}」没有可用的修订版本")
        return rev

    async def _require_task(self, task_id: str) -> Any:
        task = await self.store.tasks.get_task(task_id)
        if task is None:
            raise NotFound(f"任务不存在: {task_id}")
        return task

    async def _require_harness(self, harness_id: str) -> HarnessRegistration:
        h = await self.store.registry.get_harness(harness_id)
        if h is None:
            raise NotFound(f"harness 未登记: {harness_id}")
        return h

    async def _event(
        self,
        type_: EventType,
        *,
        scope: EventScope,
        scope_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        """网关侧动作留痕。写前日志的纪律对网关同样成立（D-08）。"""
        await self.store.events.append(
            scope=scope,
            type=type_,
            actor=EventActor.USER,
            scope_id=scope_id,
            payload=payload,
        )


# ===========================================================================
# 系统
# ===========================================================================


class SystemService(_Service):
    """健康、状态、事件增量、「需处理」、存储。"""

    async def health(self) -> S.HealthResponse:
        from .. import __version__

        return S.HealthResponse(ok=True, version=__version__)

    async def status(self, *, allow_remote: bool) -> S.SystemStatusResponse:
        from .. import __version__

        store = self.store
        engine = self.engine

        counts = S.CountsResponse(
            live_tasks=len(await store.tasks.list_live_tasks()),
            running_stages=len(
                await store.tasks.list_stages_in_states(
                    [StageState.DISPATCHING, StageState.RUNNING]
                )
            ),
            pending_stages=len(
                await store.tasks.list_stages_in_states(
                    [StageState.WAITING_DEPS, StageState.READY]
                )
            ),
            open_approvals=len(await engine.approvals.open_items()),
            workflows=len(await store.workflows.list()),
        )

        hosting, connected = _session_hosting(engine)
        reaper = engine.reaper
        last = reaper.last_report if reaper is not None else None
        storage = S.StorageReportResponse.model_validate(await engine.storage_report())

        return S.SystemStatusResponse(
            version=__version__,
            startup_notes=list(engine.startup_notes),
            data_dir=str(engine.config.data_dir),
            workspace_dir=str(engine.config.resolved_workspace()),
            secrets_unlocked=engine.secret_store is not None,
            harness_attached=engine.harness is not None,
            allow_remote=allow_remote,
            loopback_only=not allow_remote,
            scheduler=S.SchedulerStats(
                enabled=engine.scheduler is not None,
                poll_interval=float(engine.config.poll_interval),
            ),
            reaper=S.ReaperStats(
                interval_seconds=float(engine.config.reaper_interval),
                artifact_gc_enabled=bool(engine.config.artifact_gc_enabled),
                last_run=last.model_dump(mode="json") if last is not None else None,
            ),
            counts=counts,
            session_hosting=hosting,
            supervisor_connected=connected,
            notifier_subscribers=engine.notifier.subscriber_count,
            notifier_dropped=engine.notifier.dropped,
            latest_event_id=await store.events.latest_id(),
            storage=storage,
        )

    async def events(
        self, *, after_id: int = 0, limit: int = 200, task_id: str | None = None
    ) -> S.EventPage:
        store = self.store
        if task_id:
            rows = await store.events.for_task(task_id, limit=limit, after_id=after_id)
        else:
            rows = await store.events.tail(after_id=after_id, limit=limit)
        return S.EventPage(
            events=[S.EventRecord.model_validate(r) for r in rows],
            latest_event_id=await store.events.latest_id(),
            returned=len(rows),
            has_more=len(rows) >= limit,
        )

    async def attention(self) -> S.AttentionResponse:
        """OBS-05 的「需处理」入口。空列表是**如实的空**，不是「还没查」。"""
        return S.AttentionResponse.model_validate(await self.engine.attention_items())

    async def storage(self) -> S.StorageReportResponse:
        return S.StorageReportResponse.model_validate(await self.engine.storage_report())

    async def sessions(self) -> S.SessionListResponse:
        """会话台账（REC-03）。

        按部署形态取数据，且**如实反映取到了没有**：

        - ``supervisor`` 托管：会话的持有者是 supervisor，向它要（``session.list``）。
          问不到时返回 ``reachable=False`` 与一句说明，而不是空列表——空列表会被
          读成「没有会话」，而事实是「不知道」（这两件事在本项目里必须分开）。
        - ``in_process``：读内核本地的 ``session_handle`` 表。该表在 in-process 模式下
          内核并不单独维护（会话就是进程里的对象），查不到时要说明这一点。
        """
        hosting, connected = _session_hosting(self.engine)
        if hosting == "supervisor":
            client = self.engine.harness
            if not connected or not hasattr(client, "list_sessions"):
                return S.SessionListResponse(
                    source="supervisor",
                    reachable=False,
                    note="会话由 supervisor 托管，但此刻未连上它：查不到会话台账。"
                    "这不等于「没有会话」——supervisor 起来后重新查询即可",
                )
            try:
                rows = await client.list_sessions()
            except Exception as exc:  # noqa: BLE001 - 查不到就如实说查不到
                return S.SessionListResponse(
                    source="supervisor",
                    reachable=False,
                    note=f"向 supervisor 查询会话失败（{type(exc).__name__}）：{exc}",
                )
            sessions = [_session_record(r) for r in rows]
            return S.SessionListResponse(
                sessions=sessions,
                returned=len(sessions),
                source="supervisor",
                reachable=True,
            )

        rows = await self.store.db.fetch_all(
            "SELECT * FROM session_handle ORDER BY created_at"
        )
        sessions = [_session_record(dict(r)) for r in rows]
        note = None
        if not sessions:
            note = (
                "in_process 模式下内核不单独维护会话台账；"
                "会话生命周期与内核进程绑定"
            )
        return S.SessionListResponse(
            sessions=sessions,
            returned=len(sessions),
            source="session_handle",
            reachable=True,
            note=note,
        )

    # ---- 接入运行中的会话（HUM-01/02） ----

    async def attach(self, session_ref: str) -> S.SessionAttachResponse:
        """取一个会话的可看输出与可做操作。"""
        return S.SessionAttachResponse(**await self.engine.attach_session(session_ref))

    async def send_input(
        self, session_ref: str, req: S.SessionInputRequest
    ) -> S.SessionInputResponse:
        """向运行中的会话注入一条消息。

        空白内容在这里就拒绝，不往下传：一条空消息到了 agent 那头可能被当成
        一次「继续」，白烧一轮 token，而用户以为什么都没发。
        """
        text = req.text.strip()
        if not text:
            raise BadRequest("注入的内容不能为空。")
        return S.SessionInputResponse(
            **await self.engine.send_to_session(session_ref, text)
        )

    # ---- 手动清理（RES-03） ----

    async def prune(self, req: S.PruneRequest) -> S.PruneResponse:
        if req.task_id and req.older_than:
            raise BadRequest("task_id 与 older_than 互斥：一次只做一种范围的清理")
        if req.keep_last < 0:
            raise BadRequest("keep_last 不能为负")

        actions: list[S.PruneAction] = []
        notes: list[str] = []

        # 1) 事件历史
        if req.task_id:
            total = await self.store.events.count_for_task(req.task_id)
            pending = max(0, total - req.keep_last)
            deleted = 0 if req.dry_run else await self.store.events.prune_task(
                req.task_id, keep_last=req.keep_last
            )
            actions.append(
                S.PruneAction(
                    kind="events",
                    scope=f"task={req.task_id}",
                    deleted=pending if req.dry_run else deleted,
                    detail={"total_before": total, "keep_last": req.keep_last},
                )
            )
        elif req.older_than:
            total = int(
                await self.store.db.fetch_value(
                    "SELECT COUNT(*) FROM event_log WHERE ts<?", (req.older_than,), default=0
                )
            )
            deleted = 0
            if not req.dry_run:
                deleted = await self.store.events.prune_older_than(req.older_than)
            actions.append(
                S.PruneAction(
                    kind="events",
                    scope=f"older_than={req.older_than}",
                    deleted=total if req.dry_run else deleted,
                    detail={"matched": total},
                )
            )

        # 2) 产物物理回收。仍被活跃或可恢复任务引用的产物一律不动（RES-03）
        if req.include_artifacts or req.include_unreferenced_artifacts:
            result = await self.store.artifacts.gc(
                dry_run=req.dry_run,
                include_tombstoned=req.include_unreferenced_artifacts,
            )
            actions.append(
                S.PruneAction(
                    kind="artifacts",
                    scope="tombstoned+unreferenced"
                    if req.include_unreferenced_artifacts
                    else "tombstoned",
                    deleted=result.get("removed_rows", 0),
                    detail={
                        "removed_files": len(result.get("removed_files", [])),
                        "candidate_rows": result.get("candidate_rows", 0),
                        "skipped": result.get("skipped", []),
                    },
                )
            )
            if result.get("skipped"):
                notes.append(
                    f"{len(result['skipped'])} 份产物因同摘要仍有活跃引用被跳过，未回收"
                )

        # 3) 孤儿资源对账（只清理能确认归属的对象）
        if req.include_orphans:
            if req.dry_run:
                scan = await self.engine.ledger.scan_orphans()
                actions.append(
                    S.PruneAction(
                        kind="orphan_resources",
                        scope="台账对账（预览）",
                        deleted=len(scan.get("orphans", [])),
                        detail={"open_total": scan.get("open_total", 0)},
                    )
                )
            else:
                report = await self.engine.reaper.run_once()
                actions.append(
                    S.PruneAction(
                        kind="orphan_resources",
                        scope="台账对账",
                        deleted=report.orphans_closed,
                        detail=report.model_dump(mode="json"),
                    )
                )
                notes.extend(report.notes)
                if report.still_unresolved:
                    notes.append(
                        f"{len(report.still_unresolved)} 项资源无法确认归属，已保持可见待人工处理"
                    )

        if not actions:
            notes.append(
                "没有匹配任何清理范围：请给出 task_id 或 older_than，"
                "或显式打开 include_artifacts / include_orphans"
            )

        if not req.dry_run and actions:
            await self._event(
                EventType.STORAGE_PRUNED,
                scope=EventScope.SYSTEM,
                scope_id=None,
                payload={
                    "actions": [a.model_dump(mode="json") for a in actions],
                    "request": req.model_dump(mode="json"),
                },
            )

        return S.PruneResponse(
            dry_run=req.dry_run,
            actions=actions,
            notes=notes,
            storage=await self.storage(),
        )


# ===========================================================================
# Workflow 定义与修订
# ===========================================================================


class WorkflowService(_Service):
    """定义层用例。发明确不在内核，故在此实现；修订的不可变性由仓储保证。"""

    async def list_workflows(self, *, include_deleted: bool = False) -> S.WorkflowListResponse:
        items = await self.store.workflows.list(include_deleted=include_deleted)
        return S.WorkflowListResponse(
            workflows=[_workflow_out(w) for w in items], returned=len(items)
        )

    async def create_workflow(self, req: S.WorkflowCreateRequest) -> S.WorkflowResponse:
        wf = WorkflowDefinition(
            name=req.name,
            description=req.description,
            max_concurrent_tasks=req.max_concurrent_tasks,
            status=WorkflowStatus.DRAFT,
        )
        await self.store.workflows.create(wf)
        await self._event(
            EventType.WORKFLOW_CREATED,
            scope=EventScope.WORKFLOW,
            scope_id=wf.workflow_id,
            payload={"name": wf.name, "max_concurrent_tasks": wf.max_concurrent_tasks},
        )
        return _workflow_out(wf)

    async def get_workflow(self, workflow_id: str) -> S.WorkflowResponse:
        return _workflow_out(await self._require_workflow(workflow_id))

    async def patch_workflow(
        self, workflow_id: str, req: S.WorkflowPatchRequest
    ) -> S.WorkflowResponse:
        await self._require_workflow(workflow_id)
        fields = req.model_dump(exclude_unset=True, exclude_none=True)
        if not fields:
            raise BadRequest("没有需要更新的字段")
        ok = await self.store.workflows.update(workflow_id, **fields)
        if not ok:
            raise NotFound(f"Workflow 不存在: {workflow_id}")
        await self._event(
            EventType.WORKFLOW_UPDATED,
            scope=EventScope.WORKFLOW,
            scope_id=workflow_id,
            payload={"fields": sorted(fields)},
        )
        return _workflow_out(await self._require_workflow(workflow_id))

    async def delete_workflow(self, workflow_id: str) -> S.WorkflowDeleteResponse:
        await self._require_workflow(workflow_id)
        try:
            report = await self.engine.delete_workflow(workflow_id)
        except ValueError as exc:  # noqa: BLE001 - 内核的显式拒绝，原样转成 400
            raise BadRequest(str(exc)) from exc
        return S.WorkflowDeleteResponse.model_validate(report.model_dump(mode="json"))

    # ---- 修订 ----

    async def list_revisions(self, workflow_id: str, *, limit: int = 50) -> S.RevisionListResponse:
        await self._require_workflow(workflow_id)
        revs = await self.store.workflows.list_revisions(workflow_id, limit=limit)
        return S.RevisionListResponse(
            workflow_id=workflow_id, revisions=[_revision_out(r) for r in revs]  # type: ignore[arg-type]
        )

    async def get_revision(self, workflow_id: str, revision_seq: int) -> S.RevisionResponse:
        rev = await self.store.workflows.get_revision(workflow_id, revision_seq)
        if rev is None:
            raise NotFound(f"修订不存在: {workflow_id}#{revision_seq}")
        return _revision_out(rev)

    async def save_revision(
        self, workflow_id: str, req: S.RevisionSaveRequest
    ) -> S.RevisionSaveResponse:
        """保存新修订（修订不可变：编辑即新建，WF-06）。

        ``base_revision_seq`` 是乐观并发 CAS（D-02）：与当前 ``current_revision_seq``
        不一致时抛 :class:`RevisionConflict`，由 app 层返回 409 + 最新版本。
        """
        await self._require_workflow(workflow_id)
        store = self.store

        # next_revision_seq 与插入必须在同一个写事务里，否则并发下会算出同一个序号
        async with store.db.transaction():
            seq = await store.workflows.next_revision_seq(workflow_id)
            rev = WorkflowRevision(
                workflow_id=workflow_id,
                revision_seq=seq,
                graph=req.graph,
                source=req.source,
                draft_of=req.base_revision_seq,
                is_published=req.publish,
                note=req.note,
            )
            try:
                await store.workflows.save_revision(
                    rev, publish=req.publish, expected_revision_seq=req.base_revision_seq
                )
            except ConflictError as exc:
                latest = await store.workflows.get_current_revision(workflow_id)
                raise RevisionConflict(
                    f"修订冲突：{exc}",
                    workflow_id=workflow_id,
                    latest_revision_seq=latest.revision_seq if latest else 0,
                    latest_revision=latest,
                ) from exc

        await self._event(
            EventType.REVISION_PUBLISHED if req.publish else EventType.REVISION_SAVED,
            scope=EventScope.WORKFLOW,
            scope_id=workflow_id,
            payload={
                "revision_seq": seq,
                "published": req.publish,
                "base_revision_seq": req.base_revision_seq,
                "node_count": len(req.graph.nodes),
                "edge_count": len(req.graph.edges),
                "note": req.note,
            },
        )
        return S.RevisionSaveResponse(
            accepted=True,
            workflow_id=workflow_id,
            revision_seq=seq,
            published=req.publish,
            effective_graph_version=rev.effective_graph_version(),
            note=req.note,
            cas_checked=req.base_revision_seq is not None,
            base_revision_seq=req.base_revision_seq,
        )

    # ---- 校验与启停 ----

    async def validate_graph(
        self, workflow_id: str, req: S.ValidateRequest | None
    ) -> ValidationReport:
        """WF-05 校验：草稿、发布、发射三档共用同一条管线。

        ``graph`` 留空时校验该 Workflow 当前的修订（前端保存前预检最常用）。
        """
        await self._require_workflow(workflow_id)
        req = req or S.ValidateRequest()
        graph = req.graph
        if graph is None:
            graph = (await self._require_revision(workflow_id)).graph
        registry = await self.store.registry.snapshot()
        return validate(graph, registry, mode=req.mode)

    async def toggle_preview(
        self, workflow_id: str, node_id: str
    ) -> S.TogglePreviewResponse:
        """ACT-02：操作前的路径可见。**不改变任何状态。**

        预览的是「把当前节点翻到反面」的后果——用户看到的正是点下去会发生的事。
        """
        await self._require_workflow(workflow_id)
        rev = await self._require_revision(workflow_id)
        node = rev.graph.node(node_id)
        if node is None:
            raise NotFound(f"节点不存在于当前修订: {node_id}")
        target = not node.enabled

        result = await self.engine.preview_toggle(workflow_id, node_id, target)
        return S.TogglePreviewResponse.model_validate(
            {"node_id": node_id, "enabling": target, **result}
        )

    async def toggle_node(
        self, workflow_id: str, node_id: str, req: S.ToggleRequest | None
    ) -> S.NodeToggleResponse:
        """执行启停（D-01）。在途任务按发射时钉扎的有效图执行至结束。"""
        if req is None:
            raise BadRequest(
                "preview=false 需要请求体 {enable, mode}",
                hint="先带 ?preview=true 看一遍路径与受影响任务，再决定是否执行（ACT-02）",
            )
        await self._require_workflow(workflow_id)
        try:
            report = await self.engine.set_node_enabled(
                workflow_id, node_id, req.enable, mode=req.mode
            )
        except ValueError as exc:  # noqa: BLE001
            raise BadRequest(str(exc)) from exc
        return S.NodeToggleResponse.model_validate(report.model_dump(mode="json"))


class NodeService(_Service):
    """节点队列投影与调序（RUN-04、AC-03）。"""

    async def queue(self, node_id: str) -> S.NodeQueueResponse:
        return S.NodeQueueResponse.model_validate(await self.engine.node_queue(node_id))

    async def reorder(self, node_id: str, req: S.ReorderRequest) -> S.ReorderResponse:
        if not req.stage_ids:
            raise BadRequest("stage_ids 不能为空",
                             hint="给出本节点待执行阶段的期望顺序（从前往后）")
        report = await self.engine.reorder(node_id, req.stage_ids)
        return S.ReorderResponse.model_validate(report.model_dump(mode="json"))


# ===========================================================================
# 任务
# ===========================================================================


class TaskService(_Service):
    """发射、查询与控制。控制操作一律转发内核，**不在网关里合成结论**。"""

    async def submit(self, workflow_id: str, req: S.TaskSubmitRequest) -> S.SubmitResponse:
        await self._require_workflow(workflow_id)
        try:
            result = await self.engine.submit(
                workflow_id=workflow_id,
                input_payload=req.input_payload,
                idempotency_key=req.idempotency_key,
                priority=req.priority,
            )
        except ValueError as exc:  # noqa: BLE001 - 未发布／已删除等显式拒绝
            raise BadRequest(str(exc)) from exc

        if not result.get("accepted"):
            # 校验失败：把**完整报告**交回去（含 node_id / edge / slot），不是一句「参数错误」
            raise SubmissionRejected(
                ValidationReport.model_validate(result["report"])
            )
        return S.SubmitResponse(
            accepted=True,
            task_id=result.get("task_id"),
            created=bool(result.get("created")),
        )

    async def list_tasks(
        self,
        *,
        workflow_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> S.TaskListResponse:
        tasks = await self.store.tasks.list_tasks(
            workflow_id=workflow_id,
            states=[state] if state else None,
            limit=limit,
            offset=offset,
        )
        return S.TaskListResponse(
            tasks=tasks,
            returned=len(tasks),
            limit=limit,
            offset=offset,
            has_more=len(tasks) >= limit,
        )

    async def detail(self, task_id: str) -> S.TaskDetailResponse:
        try:
            detail = await self.engine.task_detail(task_id)
        except KeyError as exc:
            raise NotFound(f"任务不存在: {task_id}") from exc
        return S.TaskDetailResponse.model_validate(detail)

    async def pause(self, task_id: str, req: S.PauseRequest | None) -> S.PauseResponse:
        req = req or S.PauseRequest()
        await self._require_task(task_id)
        try:
            report = await self.engine.pause(
                task_id, from_node_id=req.from_node_id, reason=req.reason
            )
        except ValueError as exc:  # noqa: BLE001
            raise BadRequest(str(exc)) from exc
        return S.PauseResponse.model_validate(report.model_dump(mode="json"))

    async def resume(self, task_id: str, req: S.ResumeRequest | None) -> S.ResumeResponse:
        req = req or S.ResumeRequest()
        await self._require_task(task_id)
        try:
            report = await self.engine.resume(task_id, restart_failed=req.restart_failed)
        except ValueError as exc:  # noqa: BLE001
            raise BadRequest(str(exc)) from exc
        return S.ResumeResponse.model_validate(report.model_dump(mode="json"))

    async def delete(
        self, task_id: str, req: S.DeleteTaskRequest | None
    ) -> S.DeleteTaskResponse:
        req = req or S.DeleteTaskRequest()
        await self._require_task(task_id)
        try:
            report = await self.engine.delete_task(
                task_id, from_node_id=req.from_node_id, reason=req.reason
            )
        except ValueError as exc:  # noqa: BLE001
            raise BadRequest(str(exc)) from exc
        return S.DeleteTaskResponse.model_validate(report.model_dump(mode="json"))

    async def resume_stage(self, task_id: str, node_id: str) -> S.StageResumeResponse:
        """断点续跑（§11.3）。已主动删除的任务没有这个入口（LIFE-04）。"""
        await self._require_task(task_id)
        try:
            result = await self.engine.resume_from_stage(task_id, node_id)
        except ValueError as exc:  # noqa: BLE001
            raise BadRequest(str(exc)) from exc
        return S.StageResumeResponse.model_validate(result)

    async def events(
        self, task_id: str, *, after_id: int = 0, limit: int = 200
    ) -> S.EventPage:
        await self._require_task(task_id)
        return await SystemService(self.engine).events(
            after_id=after_id, limit=limit, task_id=task_id
        )

    async def attempt_work(self, task_id: str, attempt_id: str) -> S.AttemptWorkResponse:
        """一次执行尝试的工作细节：输入、推理、工具调用、涉及的文件（OBS-03）。"""
        await self._require_task(task_id)
        attempt = await self.engine.store.tasks.get_attempt(attempt_id)
        if attempt is None or attempt.task_id != task_id:
            raise NotFound(f"执行尝试不存在: {attempt_id}")
        rows = await self.engine.store.events.by_scope(
            EventScope.ATTEMPT, attempt_id, limit=2000
        )
        stage_artifacts = await self.engine.store.artifacts.list_by_stage(attempt.stage_id)
        artifact_ids = [
            a.artifact_id
            for a in stage_artifacts
            if a.producer is not None and a.producer.attempt_id == attempt_id
        ]
        return _assemble_attempt_work(task_id, attempt, rows, artifact_ids)

    async def artifact_content(self, task_id: str, artifact_id: str) -> S.ArtifactContentResponse:
        await self._require_task(task_id)
        try:
            result = await self.engine.artifact_content(task_id, artifact_id)
        except KeyError as exc:
            raise NotFound(f"产物不存在或已被清理: {artifact_id}") from exc
        return S.ArtifactContentResponse.model_validate(result)


#: 按工具名推断「这个调用对文件做了什么」。只是展示层归类，不影响任何状态。
_WRITE_TOOL_HINTS = ("write", "edit", "create", "notebook", "delete", "remove")
_READ_TOOL_HINTS = ("read", "view", "glob", "grep", "ls")
_COMMAND_TOOL_HINTS = ("bash", "shell", "cmd", "run")


def _assemble_attempt_work(
    task_id: str,
    attempt: Any,
    rows: list[dict[str, Any]],
    artifact_ids: list[str],
) -> S.AttemptWorkResponse:
    """把一次尝试的事件流水组装成「它干了什么」的视图。

    只呈现事件里真实记录的内容：没有推理记录就是 None，工具结果没回来
    （尝试还在跑或被中断）就是 is_error=None。
    """
    input_payload: dict[str, Any] | None = None
    reasoning_parts: list[str] = []
    reasoning_truncated = False
    calls: list[dict[str, Any]] = []
    by_use_id: dict[str, dict[str, Any]] = {}

    for row in rows:
        type_ = row["type"]
        payload = row["payload"]
        if type_ == EventType.ATTEMPT_INPUT.value:
            input_payload = payload
        elif type_ == EventType.ATTEMPT_REASONING.value:
            reasoning_parts.append(str(payload.get("text") or ""))
            reasoning_truncated = reasoning_truncated or bool(payload.get("truncated"))
        elif type_ == EventType.ATTEMPT_TOOL_USE.value:
            call = {
                "tool_use_id": payload.get("tool_use_id"),
                "name": payload.get("tool_name"),
                "target": payload.get("target"),
                "input_preview": payload.get("input_preview") or "",
                "input_truncated": bool(payload.get("truncated")),
                "is_error": None,
                "result_preview": None,
                "result_truncated": False,
            }
            calls.append(call)
            if call["tool_use_id"]:
                by_use_id[call["tool_use_id"]] = call
        elif type_ == EventType.ATTEMPT_TOOL_RESULT.value:
            call = by_use_id.get(payload.get("tool_use_id") or "")
            if call is None:
                call = {
                    "tool_use_id": payload.get("tool_use_id"),
                    "name": None,
                    "target": None,
                    "input_preview": "",
                    "input_truncated": False,
                    "is_error": None,
                    "result_preview": None,
                    "result_truncated": False,
                }
                calls.append(call)
            call["is_error"] = bool(payload.get("is_error"))
            call["result_preview"] = payload.get("content") or ""
            call["result_truncated"] = bool(payload.get("truncated"))

    files_written: list[str] = []
    files_read: list[str] = []
    commands: list[str] = []
    for call in calls:
        name = (call["name"] or "").lower()
        target = call["target"]
        if not target:
            continue
        if any(h in name for h in _COMMAND_TOOL_HINTS):
            commands.append(target)
        elif any(h in name for h in _WRITE_TOOL_HINTS):
            files_written.append(target)
        elif any(h in name for h in _READ_TOOL_HINTS):
            files_read.append(target)

    return S.AttemptWorkResponse(
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        stage_id=attempt.stage_id,
        node_id=attempt.node_id,
        input=input_payload,
        reasoning="".join(reasoning_parts) or None,
        reasoning_truncated=reasoning_truncated,
        tool_calls=[S.ToolCallView.model_validate(c) for c in calls],
        files_written=list(dict.fromkeys(files_written)),
        files_read=list(dict.fromkeys(files_read)),
        commands=list(dict.fromkeys(commands)),
        artifact_ids=artifact_ids,
    )


# ===========================================================================
# 注册表
# ===========================================================================
#: 明显像凭据的环境变量名。与 ``ToolLaunch`` 的构造期防线同一条规则（AUTH-02）：
#: 顺手粘一个 key 进配置应当在**写入时**失败，而不是等它进了日志、模板和事件历史。
_SUSPICIOUS_ENV = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _screen_env(env: dict[str, str], *, where: str) -> None:
    for name, value in (env or {}).items():
        if any(s in name.upper() for s in _SUSPICIOUS_ENV) and value and not value.startswith("$"):
            raise BadRequest(
                f"{where} 的环境变量 {name} 看起来包含凭据",
                hint="改用 auth_binding 引用 Secret Store 中的凭据，"
                "或使用 ${name} 形式的运行时占位符（AUTH-02）",
            )


class RegistryService(_Service):
    """Harness / 凭据 / Skill / 工具。

    **凭据永远只有引用**：本服务不读写 Secret Store 的值，``CredentialRef``
    也只携带 ``secret_locator``；密钥本体不出 L5。
    """

    # ---- harness ----

    async def list_harnesses(self) -> list[HarnessRegistration]:
        return await self.store.registry.list_harnesses()

    async def create_harness(self, req: S.HarnessCreateRequest) -> HarnessRegistration:
        _screen_env(req.env_template, where=f"harness「{req.name}」")
        h = HarnessRegistration(
            harness_id=req.harness_id or new_id(),
            name=req.name,
            adapter_id=req.adapter_id,
            adapter_version=req.adapter_version,
            exec_path=req.exec_path,
            env_template=req.env_template,
            cwd=req.cwd,
            auth_binding=req.auth_binding,
            auth_mode=req.auth_mode,
            enabled=req.enabled,
        )
        await self.store.registry.upsert_harness(h)
        return h

    async def get_harness(self, harness_id: str) -> HarnessRegistration:
        return await self._require_harness(harness_id)

    async def patch_harness(
        self, harness_id: str, req: S.HarnessPatchRequest
    ) -> HarnessRegistration:
        current = await self._require_harness(harness_id)
        fields = req.model_dump(exclude_unset=True)
        if "env_template" in fields and fields["env_template"] is not None:
            _screen_env(fields["env_template"], where=f"harness「{current.name}」")
        updated = current.model_copy(update={k: v for k, v in fields.items() if v is not None})
        # model_copy 不校验：显式重跑一次构造，非法组合在这里就失败而不是等到派发
        updated = HarnessRegistration.model_validate(updated.model_dump())
        await self.store.registry.upsert_harness(updated)
        return updated

    async def delete_harness(self, harness_id: str) -> int:
        """从本机注册表移除一台 harness。

        引用它的节点会在校验管线里报 ``harness_not_registered`` —— 影响面可见，
        而不是静默改写既有作业（EXT-03）。仓储没有删除方法，故在服务层直接执行。
        """
        await self._require_harness(harness_id)
        return await self.store.db.execute_rowcount(
            "DELETE FROM harness_registration WHERE harness_id=?", (harness_id,)
        )

    async def probe_harness(self, harness_id: str) -> S.ProbeResponse:
        """能力探测（HAR-02）。

        适配层提供 ``probe`` 时用它——那才是**实测**；没有时退回 ``capabilities``，
        并在 ``source``/``note`` 里如实说明结论来自接口声明而非实测（HAR-03）。
        """
        await self._require_harness(harness_id)
        h = self.engine.harness
        ok = True
        caps: dict[str, Any] | None = None
        error: str | None = None
        source = "probe"
        note: str | None = None

        probe = getattr(h, "probe", None)
        try:
            if callable(probe):
                caps = _as_dict(await probe(harness_id))
            elif h is not None and hasattr(h, "capabilities"):
                caps = _as_dict(await h.capabilities(harness_id))
                source = "capabilities"
                note = (
                    "适配层未提供实测 probe，本次结论来自接口声明的能力；"
                    "在接入真实 harness 前不应视为已验证（HAR-02）"
                )
            else:
                ok = False
                error = "适配层未装配，无法探测"
        except Exception as exc:  # noqa: BLE001 - 探测失败如实记录，不伪造能力
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        await self.store.registry.record_probe(
            harness_id, ok=ok, capabilities=caps, error=error
        )
        return S.ProbeResponse(
            harness_id=harness_id,
            ok=ok,
            capabilities=caps,
            error=error,
            probed_at=now_iso(),
            source=source,
            note=note,
        )

    # ---- 凭据（只有引用） ----

    async def list_credentials(self) -> list[CredentialRef]:
        return await self.store.registry.list_credentials()

    async def create_credential(self, req: S.CredentialCreateRequest) -> CredentialRef:
        if req.kind.value != "harness_login" and not req.secret_locator and not req.secret:
            raise BadRequest(
                "该凭据类型需要密钥：直接提交密钥内容，或提供 secret_locator 指向凭据库中已有条目",
                hint="若使用 harness 自身的登录态，请选 kind=harness_login",
            )
        if req.kind.value == "harness_login" and req.secret:
            raise BadRequest(
                "harness_login 没有密钥本体可存（凭据由 harness 自身的登录态提供）"
            )

        cred_id = req.credential_id or new_id()
        locator = req.secret_locator
        if req.secret:
            if self.engine.secret_store is None:
                raise BadRequest(
                    "凭据库未解锁，无法保存密钥",
                    hint="内核启动时提供 --passphrase（或 WORKERBEE_PASSPHRASE）后重试；"
                    "也可以只登记指向凭据库已有条目的 secret_locator",
                )
            locator = locator or f"secret://cred-{cred_id}"
            # 写库与登记脱敏必须成对（store_secret 内部保证）；密钥本体不进事件载荷。
            await self.engine.store_secret(locator, dict(req.secret), label=req.label)

        cred = CredentialRef(
            credential_id=cred_id,
            label=req.label,
            kind=req.kind,
            secret_locator=locator,
            base_url=req.base_url,
            default_model=req.default_model,
        )
        await self.store.registry.upsert_credential(cred)
        await self._event(
            EventType.SECRET_BOUND,
            scope=EventScope.SYSTEM,
            scope_id=cred.credential_id,
            payload={"label": cred.label, "kind": cred.kind.value},  # 不含 locator 之外的材料
        )
        return cred

    async def get_credential(self, credential_id: str) -> CredentialRef:
        cred = await self.store.registry.get_credential(credential_id)
        if cred is None:
            raise NotFound(f"凭据不存在: {credential_id}")
        return cred

    async def set_credential_revoked(
        self, credential_id: str, *, revoked: bool
    ) -> CredentialRef:
        """撤销／恢复一份凭据的可用性（§9.1）。

        撤销的是**本机引用**：密钥本体在 Secret Store 里，此处不动它——
        需要连带销毁密值请走 L5 的 revoke。
        """
        cred = await self.get_credential(credential_id)
        await self.store.registry.set_credential_revoked(credential_id, revoked)
        if revoked:
            await self._event(
                EventType.SECRET_REVOKED,
                scope=EventScope.SYSTEM,
                scope_id=credential_id,
                payload={"label": cred.label},
            )
        return await self.get_credential(credential_id)

    async def delete_credential(self, credential_id: str) -> int:
        await self.get_credential(credential_id)
        return await self.store.db.execute_rowcount(
            "DELETE FROM credential_ref WHERE credential_id=?", (credential_id,)
        )

    # ---- Skill ----

    async def list_skills(self) -> list[SkillDoc]:
        return await self.store.registry.list_skills()

    async def create_skill(self, req: S.SkillCreateRequest) -> SkillDoc:
        skill = SkillDoc(
            skill_id=req.skill_id or new_id(),
            name=req.name,
            content=req.content,
            version=req.version,
            scope=req.scope,
            enabled=req.enabled,
        )
        await self.store.registry.upsert_skill(skill)
        return skill

    async def delete_skill(self, skill_id: str) -> int:
        if await self.store.registry.get_skill(skill_id) is None:
            raise NotFound(f"Skill 不存在: {skill_id}")
        return await self.store.db.execute_rowcount(
            "DELETE FROM skill_doc WHERE skill_id=?", (skill_id,)
        )

    # ---- 工具 ----

    async def list_tools(self) -> list[ToolSpec]:
        return await self.store.registry.list_tools()

    async def create_tool(self, req: S.ToolCreateRequest) -> ToolSpec:
        # ToolLaunch 的构造期校验会拦下明显是凭据的 env（AUTH-02 的早失败防线）
        tool = ToolSpec(
            tool_id=req.tool_id or new_id(),
            name=req.name,
            launch=ToolLaunch.model_validate(req.launch or {}),
            description=req.description,
            io_schema=req.io_schema,
            risk_level=req.risk_level,
            approval_policy=req.approval_policy,
            version=req.version,
            health_check=req.health_check,
            enabled=req.enabled,
        )
        await self.store.registry.upsert_tool(tool)
        return tool

    async def delete_tool(self, tool_id: str) -> int:
        if await self.store.registry.get_tool(tool_id) is None:
            raise NotFound(f"工具不存在: {tool_id}")
        return await self.store.db.execute_rowcount(
            "DELETE FROM tool_spec WHERE tool_id=?", (tool_id,)
        )


# ===========================================================================
# 模板
# ===========================================================================


class TemplateService(_Service):
    """模板（TPL-01/02/03）。密钥本体被**结构性**挡在模板外：载荷装不下它；
    本机凭据的引用（只是 id）默认随模板保留，同机实例化自动绑定。"""

    async def list_templates(self, *, kind: TemplateKind | None = None) -> S.TemplateListResponse:
        items = await self.store.registry.list_templates(kind=kind)
        return S.TemplateListResponse(templates=items, returned=len(items))

    async def create_template(self, req: S.TemplateCreateRequest) -> Template:
        if (req.payload is None) == (req.from_workflow_id is None):
            raise BadRequest(
                "payload 与 from_workflow_id 必须二选一",
                hint="给 payload 直接落一个模板；给 from_workflow_id 从既有流程生成"
                "（密钥永远不进模板；本机凭据的引用默认保留，可用 keep_credential_refs=False 剥离）",
            )

        if req.from_workflow_id:
            await self._require_workflow(req.from_workflow_id)
            rev = (
                await self.store.workflows.get_revision(
                    req.from_workflow_id, req.from_revision_seq
                )
                if req.from_revision_seq
                else await self._require_revision(req.from_workflow_id)
            )
            if rev is None:
                raise NotFound(
                    f"修订不存在: {req.from_workflow_id}#{req.from_revision_seq}"
                )
            if req.kind == TemplateKind.NODE and len(rev.graph.nodes) != 1:
                raise BadRequest("节点模板只能从单节点流程生成")
            template = Template.from_nodes(
                rev.graph.nodes,
                rev.graph.edges,
                name=req.name,
                kind=req.kind,
                description=req.description,
                source_workflow_id=req.from_workflow_id,
                source_revision=rev.revision_seq,
                keep_credential_refs=req.keep_credential_refs,
            )
        else:
            template = Template(
                name=req.name,
                description=req.description,
                kind=req.kind,
                payload=req.payload,
            )

        await self.store.registry.upsert_template(template)
        return template

    async def get_template(self, template_id: str) -> Template:
        t = await self.store.registry.get_template(template_id)
        if t is None:
            raise NotFound(f"模板不存在: {template_id}")
        return t

    async def delete_template(self, template_id: str) -> int:
        await self.get_template(template_id)
        return await self.store.db.execute_rowcount(
            "DELETE FROM template WHERE template_id=?", (template_id,)
        )

    async def instantiate(
        self, template_id: str, req: S.TemplateInstantiateRequest
    ) -> S.TemplateInstantiateResponse:
        """实例化。未绑定的必填槽位**不编造**，如实进入报告要求补齐（TPL-03）。

        模板保留的凭据引用在此刻对本机注册表做一次校验：引用在本机解析不到
        （模板来自他机、或凭据已撤销/删除）时不能带病进入流程定义——清掉引用
        并转为 missing binding，让界面要求用户显式重选。
        """
        template = await self.get_template(template_id)
        nodes, edges, report = template.instantiate(
            dict(req.bindings), node_id_map=dict(req.node_id_map)
        )
        missing = list(report.missing_bindings)
        for node in nodes:
            for idx, profile in enumerate(node.profiles):
                if not profile.credential_ref:
                    continue
                cred = await self.store.registry.get_credential(profile.credential_ref)
                if cred is not None and cred.is_usable():
                    continue
                missing.append(
                    MissingBinding(
                        slot=f"{node.name}.profiles[{idx}].credential_ref",
                        label=profile.credential_ref,
                        reason="模板携带的凭据引用在本机不存在或已撤销，请重新绑定本机凭据",
                    )
                )
                profile.credential_ref = None
        report = InstantiationReport(
            missing_bindings=missing, usable=not missing, notes=report.notes
        )
        return S.TemplateInstantiateResponse(
            template_id=template_id,
            usable=report.usable,
            nodes=nodes,
            edges=edges,
            report=InstantiationReport.model_validate(report.model_dump(mode="json")),
        )


# ===========================================================================
# 审批
# ===========================================================================


class ApprovalService(_Service):
    """审批闭环（HUM-03/04、AC-12/14）。"""

    async def list_open(self) -> S.ApprovalListResponse:
        """待处理（含 undeliverable：决定已产生但没送达，必须留在列表里）。"""
        items = await self.engine.approvals.open_items()
        return S.ApprovalListResponse(approvals=items, returned=len(items))

    async def list_for_task(self, task_id: str) -> S.ApprovalListResponse:
        items = await self.store.approvals.list_for_task(task_id)
        return S.ApprovalListResponse(approvals=items, returned=len(items))

    async def decide(self, approval_id: str, req: S.ApprovalDecideRequest) -> S.DeliveryResponse:
        """做决定并回注。

        **重复决定不报错**：内核返回「实际生效结果」，网关原样呈现（AC-14）。
        """
        try:
            result = await self.engine.approve(
                approval_id,
                approve=req.approve,
                by=req.by,
                modified_action=req.modified_action,
            )
        except KeyError as exc:
            raise NotFound(f"审批不存在: {approval_id}") from exc

        # 决定已产生但没送达时，库里存的是 undeliverable；返回的 status 必须与之一致，
        # 否则前端会以为这条已经不欠处理，而它其实还挂在「需处理」列表里（HUM-04）。
        stored = await self.store.approvals.get(approval_id)
        status = stored.status if stored is not None else result.status
        detail = result.detail
        if not result.delivered and status == ApprovalStatus.UNDELIVERABLE:
            detail = detail or "决定已记录，但未送达原会话；该审批已标记为 undeliverable，可重试回注"
        return S.DeliveryResponse(
            approval_id=approval_id,
            delivered=result.delivered,
            status=status.value,
            detail=detail,
        )

    async def retry_delivery(self, approval_id: str) -> S.DeliveryResponse:
        """undeliverable 的重试（HUM-04）。沿用原决定，不重新征求同意——
        重复征求会训练用户盲目点同意。"""
        try:
            result = await self.engine.approvals.retry_delivery(approval_id)
        except KeyError as exc:
            raise NotFound(f"审批不存在: {approval_id}") from exc
        return S.DeliveryResponse(
            approval_id=approval_id,
            delivered=result.delivered,
            status=result.status.value,
            detail=result.detail,
        )


# ===========================================================================
# 基础助手（AI-01）
# ===========================================================================

#: 快照「最近错误事件」分区收录的事件类型。
_ERROR_EVENT_TYPES = frozenset(
    {
        EventType.HANDOFF_FAILED.value,
        EventType.VALIDATION_FAILED.value,
        EventType.SESSION_LOST.value,
        EventType.RESOURCE_TEARDOWN_FAILED.value,
        EventType.RESOURCE_ORPHANED.value,
        EventType.APPROVAL_UNDELIVERABLE.value,
    }
)


class AssistantSnapshotSource:
    """把 Services 的只读方法适配给 ``assistant.snapshot.SnapshotSource`` 协议。

    分层纪律：``assistant/`` 不 import server 层，所以由本侧做适配。
    全部方法只读；取到的数据已经过各服务的既有脱敏口径。
    """

    def __init__(self, services: "Services") -> None:
        self._services = services

    async def system_status(self) -> dict[str, Any]:
        status = await self._services.system.status(allow_remote=False)
        data = status.model_dump(mode="json")
        return {
            "secrets_unlocked": data.get("secrets_unlocked"),
            "counts": data.get("counts"),
            "startup_notes": data.get("startup_notes"),
        }

    async def attention_items(self) -> dict[str, Any]:
        attention = await self._services.system.attention()
        return attention.model_dump(mode="json")

    async def recent_tasks(self, limit: int) -> list[dict[str, Any]]:
        tasks = await self._services.engine.store.tasks.list_tasks(limit=limit)
        return [
            {
                "task_id": t.task_id,
                "workflow_id": t.workflow_id,
                "workflow_name": t.workflow_name,
                "observed_state": t.observed_state.value,
                "failure_summary": t.failure_summary,
                "blocked_reason": t.blocked_reason,
            }
            for t in tasks
        ]

    async def registry_overview(self) -> dict[str, Any]:
        registry = self._services.engine.store.registry
        harnesses = await registry.list_harnesses()
        return {
            "harnesses": [
                {
                    "harness_id": h.harness_id,
                    "name": h.name,
                    "enabled": h.enabled,
                    "last_probe_ok": h.last_probe_ok,
                }
                for h in harnesses
            ],
            "credential_count": len(await registry.list_credentials()),
            "skill_count": len(await registry.list_skills()),
            "tool_count": len(await registry.list_tools()),
        }

    async def recent_error_events(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._services.engine.store.events.tail(limit=500)
        errors = [r for r in rows if r["type"] in _ERROR_EVENT_TYPES]
        return [
            {"ts": r["ts"], "type": r["type"], "note": r["payload"].get("note")}
            for r in errors[-limit:]
        ]


class AssistantService(_Service):
    """基础助手的网关门面。核心编排在 ``engine.assistant``（assistant/service.py），
    本层只做参数校验与错误翻译——判断分支不下沉到路由，也不上移到内核。"""

    def _core(self) -> Any:
        svc = getattr(self.engine, "assistant", None)
        if svc is None:
            raise BadRequest("助手服务未装配，本轮内核不支持助手功能")
        return svc

    # ---- 线程与消息 ----

    async def list_threads(self) -> S.AssistantThreadListResponse:
        threads = await self._core().list_threads()
        return S.AssistantThreadListResponse(
            threads=[S.AssistantThreadResponse.model_validate(t) for t in threads],
            returned=len(threads),
        )

    async def create_thread(
        self, req: S.AssistantThreadCreateRequest
    ) -> S.AssistantThreadResponse:
        thread = await self._core().create_thread(title=(req.title or "").strip())
        return S.AssistantThreadResponse.model_validate(thread)

    async def rename_thread(
        self, thread_id: str, req: S.AssistantThreadRenameRequest
    ) -> S.AssistantThreadResponse:
        title = req.title.strip()
        if not title:
            raise BadRequest("对话名不能为空。")
        if len(title) > 100:
            raise BadRequest("对话名太长了，最多 100 个字符。")
        try:
            thread = await self._core().rename_thread(thread_id, title)
        except KeyError:
            raise NotFound(f"对话不存在: {thread_id}") from None
        except AssistantError as exc:
            raise BadRequest(exc.detail, hint=exc.hint) from exc
        return S.AssistantThreadResponse.model_validate(thread)

    async def list_messages(self, thread_id: str) -> S.AssistantMessageListResponse:
        try:
            messages = await self._core().list_messages(thread_id)
        except KeyError:
            raise NotFound(f"对话不存在: {thread_id}") from None
        return S.AssistantMessageListResponse(
            messages=[S.AssistantMessageResponse.model_validate(m) for m in messages],
            returned=len(messages),
        )

    async def send_message(
        self, thread_id: str, req: S.AssistantSendRequest
    ) -> S.AssistantSendResponse:
        text = req.content.strip()
        if not text:
            raise BadRequest("消息内容不能为空。")
        try:
            result = await self._core().send_message(thread_id, text)
        except KeyError:
            raise NotFound(f"对话不存在: {thread_id}") from None
        except AssistantError as exc:
            raise BadRequest(exc.detail, hint=exc.hint) from exc
        return S.AssistantSendResponse(
            message=S.AssistantMessageResponse.model_validate(result["message"]),
            user_message=S.AssistantMessageResponse.model_validate(result["user_message"]),
            dropped=result["dropped"],
            degraded=result["degraded"],
            degraded_reasons=result["degraded_reasons"],
        )

    async def compact(self, thread_id: str) -> S.AssistantCompactResponse:
        """手动「整理前文」。这是唯一会额外调用一次模型的记忆操作（用户显式触发）。"""
        try:
            result = await self._core().compact_thread(thread_id)
        except KeyError:
            raise NotFound(f"对话不存在: {thread_id}") from None
        except AssistantError as exc:
            raise BadRequest(exc.detail, hint=exc.hint) from exc
        return S.AssistantCompactResponse.model_validate(result)

    # ---- 配置（secret 只进不出：只接受引用） ----

    async def get_config(self) -> S.AssistantConfigResponse:
        config = await self._core().get_config()
        return S.AssistantConfigResponse(
            **config.model_dump(),
            secrets_unlocked=self.engine.secret_store is not None,
        )

    async def update_config(
        self, req: S.AssistantConfigUpdateRequest
    ) -> S.AssistantConfigResponse:
        current = await self._core().get_config()
        fields = req.model_dump(exclude_unset=True)
        if not fields:
            raise BadRequest("没有需要更新的字段")
        if fields.get("credential_ref"):
            cred = await self.store.registry.get_credential(fields["credential_ref"])
            if cred is None:
                raise BadRequest(
                    "这条凭据不存在",
                    hint="到 注册表 → 凭据 先建好凭据，再回到这里选择",
                )
        if fields.get("api_protocol") is not None and fields["api_protocol"] not in (
            "openai",
            "anthropic",
        ):
            raise BadRequest(
                f"接口协议只支持 openai（OpenAI 兼容）和 anthropic（Anthropic 兼容），"
                f"不认识「{fields['api_protocol']}」",
                hint="Base URL 路径里含 /anthropic 时选 anthropic，其余情况选 openai",
            )
        updated = AssistantConfig.model_validate(
            {**current.model_dump(), **fields}
        )
        await self._core().save_config(updated)
        return await self.get_config()


# ===========================================================================
# 容器
# ===========================================================================


class Services:
    """路由层的唯一入口。每个子服务只覆盖一类资源。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.system = SystemService(engine)
        self.workflows = WorkflowService(engine)
        self.nodes = NodeService(engine)
        self.tasks = TaskService(engine)
        self.registry = RegistryService(engine)
        self.templates = TemplateService(engine)
        self.approvals = ApprovalService(engine)
        self.assistant = AssistantService(engine)


# ===========================================================================
# 映射辅助
# ===========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _session_hosting(engine: Engine) -> tuple[str, bool]:
    """会话托管方式与 supervisor 连接状态（§3 的核心承诺要能被看见）。

    ``session_hosting`` 是**配置的运行方式**：``supervisor`` 表示 harness 子进程由
    独立进程托管，core 重启不打断在跑的任务；``in_process`` 表示子进程由 core 自己
    持有，core 一重启它们就没了。

    ``supervisor_connected`` 才说明它此刻是否真的立起来了——请求了 supervisor 却
    连不上时，配置说「能扛重启」而事实是不能，两个字段必须一起看（界面同理）。
    """
    if not engine.config.use_supervisor:
        return "in_process", False
    try:
        from ..adapters.host.remote import SupervisorClient
    except ImportError:  # pragma: no cover - 适配层缺失时的显式降级
        return "supervisor", False
    client = engine.harness
    if isinstance(client, SupervisorClient):
        return "supervisor", bool(client.connected)
    return "supervisor", False


def _session_record(row: Any) -> S.SessionRecord:
    """把一行会话台账（supervisor 的 session_ledger 或本地 session_handle）映射成响应。

    只取台账字段：多出来的列（``pid`` / ``generation`` / 能力快照）不进响应，
    免得把内部记账细节当成对外契约。
    """
    data = _as_dict(row) or {}

    def pick(*names: str) -> Any:
        for name in names:
            value = data.get(name)
            if value is not None:
                return value
        return None

    return S.SessionRecord(
        session_ref=str(pick("session_ref") or ""),
        harness_id=str(pick("harness_id") or ""),
        owner_task_id=pick("owner_task_id"),
        owner_stage_id=pick("owner_stage_id"),
        owner_attempt_id=pick("owner_attempt_id"),
        state=str(pick("state") or "unknown"),
        created_at=pick("created_at"),
        last_heartbeat=pick("last_heartbeat"),
    )


def _workflow_out(wf: WorkflowDefinition) -> S.WorkflowResponse:
    return S.WorkflowResponse(
        workflow_id=wf.workflow_id,
        name=wf.name,
        description=wf.description,
        current_revision_seq=wf.current_revision_seq,
        status=wf.status,
        max_concurrent_tasks=wf.max_concurrent_tasks,
        created_at=_iso(wf.created_at),
        updated_at=_iso(wf.updated_at),
    )


def _revision_out(rev: WorkflowRevision) -> S.RevisionResponse:
    return S.RevisionResponse(
        workflow_id=rev.workflow_id,
        revision_seq=rev.revision_seq,
        graph=rev.graph,
        source=rev.source,
        draft_of=rev.draft_of,
        is_published=rev.is_published,
        note=rev.note,
        effective_graph_version=rev.effective_graph_version(),
        created_at=_iso(rev.created_at),
        updated_at=_iso(rev.updated_at),
    )


def _as_dict(value: Any) -> dict[str, Any] | None:
    """把能力声明收敛成普通字典（dataclass / pydantic / mapping 都吃）。"""
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json.loads(json.dumps(dataclasses.asdict(value), default=str))
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}
