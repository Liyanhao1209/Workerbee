"""状态存储（架构设计 v0.02 D-08、§5）。

一份关系表（当前态）+ append-only 事件日志（迁移与操作留痕）。控制操作先写事件
日志再改状态（写前日志）；状态迁移一律以 ``(entity_id, control_epoch)`` CAS 提交，
失败方拿到「实际生效结果」。

本模块只负责持久化与原子性，不承载领域规则——合法状态迁移的判定在
``core/runtime/state.py``。这样「存了什么」与「允许发生什么」可以分别测试。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..core.domain import (    Approval,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTimeoutPolicy,
    Attempt,
    AttemptOutcome,
    CompactEvent,
    CredentialKind,
    CredentialRef,
    DesiredState,
    GraphSpec,
    HarnessRegistration,
    OriginOfControl,
    PinnedGraph,
    SkillDoc,
    SkillScope,
    StageState,
    Task,
    TaskStage,
    TaskState,
    ToolLaunch,
    ToolSpec,
    Usage,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
    utcnow,
)
from ..core.domain.registry import ApprovalPolicy, AuthMode, RiskLevel
from ..core.domain.template import Template, TemplateKind, TemplatePayload
from ..core.domain.workflow import RevisionSource
from ..core.graph.validate import InMemoryRegistry
from .db import ConflictError, Database, dumps, loads

__all__ = [
    "Store",
    "WorkflowRepository",
    "RegistryRepository",
    "TaskRepository",
    "ResourceRepository",
    "ApprovalRepository",
    "AssistantRepository",
]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


# ===========================================================================
# 定义层
# ===========================================================================


class WorkflowRepository:
    """WorkflowDefinition 与不可变 WorkflowRevision。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- workflow ----

    async def create(self, wf: WorkflowDefinition) -> WorkflowDefinition:
        await self.db.execute(
            """INSERT INTO workflow(workflow_id, name, description, current_revision_seq,
                   status, max_concurrent_tasks, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                wf.workflow_id,
                wf.name,
                wf.description,
                wf.current_revision_seq,
                wf.status.value,
                wf.max_concurrent_tasks,
                wf.created_at.isoformat(),
                wf.updated_at.isoformat(),
            ),
        )
        return wf

    async def get(self, workflow_id: str) -> WorkflowDefinition | None:
        row = await self.db.fetch_one(
            "SELECT * FROM workflow WHERE workflow_id=?", (workflow_id,)
        )
        return self._to_workflow(row) if row else None

    async def list(self, *, include_deleted: bool = False) -> list[WorkflowDefinition]:
        if include_deleted:
            rows = await self.db.fetch_all("SELECT * FROM workflow ORDER BY created_at")
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM workflow WHERE status != 'deleted' ORDER BY created_at"
            )
        return [self._to_workflow(r) for r in rows]

    async def update(
        self, workflow_id: str, *, expected_revision_seq: int | None = None, **fields: Any
    ) -> bool:
        """更新定义。带 ``expected_revision_seq`` 时是乐观并发 CAS（D-02）。"""
        allowed = {"name", "description", "current_revision_seq", "status", "max_concurrent_tasks"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"不可更新的字段: {k}")
            sets.append(f"{k}=?")
            params.append(v.value if hasattr(v, "value") else v)
        if not sets:
            return True
        sets.append("updated_at=?")
        params.append(datetime.now().astimezone().isoformat())

        sql = f"UPDATE workflow SET {', '.join(sets)} WHERE workflow_id=?"
        params.append(workflow_id)
        if expected_revision_seq is not None:
            sql += " AND current_revision_seq=?"
            params.append(expected_revision_seq)
        return await self.db.execute_rowcount(sql, params) > 0

    async def get_name(self, workflow_id: str) -> str | None:
        return await self.db.fetch_value(
            "SELECT name FROM workflow WHERE workflow_id=?", (workflow_id,)
        )

    # ---- revision ----

    async def get_revision(self, workflow_id: str, revision_seq: int) -> WorkflowRevision | None:
        row = await self.db.fetch_one(
            "SELECT * FROM workflow_revision WHERE workflow_id=? AND revision_seq=?",
            (workflow_id, revision_seq),
        )
        return self._to_revision(row) if row else None

    async def get_current_revision(self, workflow_id: str) -> WorkflowRevision | None:
        seq = await self.db.fetch_value(
            "SELECT current_revision_seq FROM workflow WHERE workflow_id=?", (workflow_id,),
            default=0,
        )
        if not seq:
            return None
        return await self.get_revision(workflow_id, int(seq))

    async def list_revisions(self, workflow_id: str, *, limit: int = 50) -> list[WorkflowRevision]:
        rows = await self.db.fetch_all(
            """SELECT * FROM workflow_revision WHERE workflow_id=?
               ORDER BY revision_seq DESC LIMIT ?""",
            (workflow_id, limit),
        )
        return [self._to_revision(r) for r in rows]

    async def next_revision_seq(self, workflow_id: str) -> int:
        """在事务内取得下一个序号。调用方应处于写事务中以避免竞态。"""
        cur = await self.db.fetch_value(
            "SELECT COALESCE(MAX(revision_seq),0) FROM workflow_revision WHERE workflow_id=?",
            (workflow_id,),
            default=0,
        )
        return int(cur) + 1

    async def save_revision(
        self,
        rev: WorkflowRevision,
        *,
        publish: bool,
        expected_revision_seq: int | None = None,
    ) -> WorkflowRevision:
        """插入新修订，并（可选）把它设为当前已发布版本。

        修订本身**不可变**：只 INSERT，不 UPDATE graph_json。
        """
        async with self.db.transaction():
            # 唯一约束即 CAS：同一 (workflow_id, revision_seq) 不能写两次。
            try:
                await self.db.execute(
                    """INSERT INTO workflow_revision(workflow_id, revision_seq, source, draft_of,
                           is_published, note, graph_json, effective_graph_version,
                           created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rev.workflow_id,
                        rev.revision_seq,
                        rev.source.value,
                        rev.draft_of,
                        1 if (publish or rev.is_published) else 0,
                        rev.note,
                        rev.graph.model_dump_json(),
                        rev.graph.effective_graph_version(),
                        rev.created_at.isoformat(),
                        rev.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"修订 {rev.workflow_id}#{rev.revision_seq} 已存在（修订不可变）"
                ) from exc

            if publish:
                ok = await self.update(
                    rev.workflow_id,
                    current_revision_seq=rev.revision_seq,
                    status=WorkflowStatus.PUBLISHED.value,
                    expected_revision_seq=expected_revision_seq,
                )
                if not ok:
                    raise ConflictError(
                        "工作流已被他人修改（current_revision_seq 不匹配）；"
                        "请拉取最新版本后重试"
                    )
        return rev

    async def mark_revision_published(self, workflow_id: str, revision_seq: int) -> None:
        await self.db.execute(
            "UPDATE workflow_revision SET is_published=1, updated_at=? "
            "WHERE workflow_id=? AND revision_seq=?",
            (datetime.now().astimezone().isoformat(), workflow_id, revision_seq),
        )

    # ---- mappers ----

    @staticmethod
    def _to_workflow(row: sqlite3.Row) -> WorkflowDefinition:
        return WorkflowDefinition(
            workflow_id=row["workflow_id"],
            name=row["name"],
            description=row["description"],
            current_revision_seq=row["current_revision_seq"],
            status=WorkflowStatus(row["status"]),
            max_concurrent_tasks=row["max_concurrent_tasks"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_revision(row: sqlite3.Row) -> WorkflowRevision:
        return WorkflowRevision(
            workflow_id=row["workflow_id"],
            revision_seq=row["revision_seq"],
            graph=GraphSpec.model_validate_json(row["graph_json"]),
            source=RevisionSource(row["source"]),
            draft_of=row["draft_of"],
            is_published=bool(row["is_published"]),
            note=row["note"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# ===========================================================================
# 注册表
# ===========================================================================


class RegistryRepository:
    """Harness / 凭据 / Skill / 工具 / 模板。

    ``snapshot()`` 一次性载入全部注册项为进程内视图，供纯函数校验管线使用：
    校验期间的并发撤销不会产生「检查到一半」的报告（读一致性快照）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- harness ----

    async def upsert_harness(self, h: HarnessRegistration) -> None:
        await self.db.execute(
            """INSERT INTO harness_registration(
                   harness_id, name, adapter_id, adapter_version, exec_path, env_template, cwd,
                   auth_binding, auth_mode, capabilities_snapshot, last_probe_at, last_probe_ok,
                   last_probe_error, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(harness_id) DO UPDATE SET
                   name=excluded.name, adapter_id=excluded.adapter_id,
                   adapter_version=excluded.adapter_version, exec_path=excluded.exec_path,
                   env_template=excluded.env_template, cwd=excluded.cwd,
                   auth_binding=excluded.auth_binding, auth_mode=excluded.auth_mode,
                   capabilities_snapshot=excluded.capabilities_snapshot,
                   last_probe_at=excluded.last_probe_at, last_probe_ok=excluded.last_probe_ok,
                   last_probe_error=excluded.last_probe_error, enabled=excluded.enabled,
                   updated_at=excluded.updated_at""",
            (
                h.harness_id,
                h.name,
                h.adapter_id,
                h.adapter_version,
                h.exec_path,
                dumps(h.env_template),
                h.cwd,
                h.auth_binding,
                h.auth_mode.value,
                dumps(h.capabilities_snapshot) if h.capabilities_snapshot is not None else None,
                h.last_probe_at,
                None if h.last_probe_ok is None else (1 if h.last_probe_ok else 0),
                h.last_probe_error,
                1 if h.enabled else 0,
                h.created_at.isoformat(),
                h.updated_at.isoformat(),
            ),
        )

    async def get_harness(self, harness_id: str) -> HarnessRegistration | None:
        row = await self.db.fetch_one(
            "SELECT * FROM harness_registration WHERE harness_id=?", (harness_id,)
        )
        return self._to_harness(row) if row else None

    async def list_harnesses(self) -> list[HarnessRegistration]:
        rows = await self.db.fetch_all(
            "SELECT * FROM harness_registration ORDER BY created_at"
        )
        return [self._to_harness(r) for r in rows]

    async def record_probe(
        self, harness_id: str, *, ok: bool | None, capabilities: dict | None, error: str | None
    ) -> None:
        """记录一次能力探测结论。

        ``ok`` 有**三态**，不能压成布尔：
        - ``True``：探测成功且声明完整；
        - ``False``：探测失败（连不上、报错）；
        - ``None``：探测跑了但没拿到可用的声明（未知）。

        「未知」与「失败」必须分开：前者说明我们不知道，后者说明它坏了。
        把「未知」记成「失败」会让校验管线拒绝一次本应合法的发射——
        把不知道伪装成确定，无论朝哪个方向都是错的。
        """
        await self.db.execute(
            """UPDATE harness_registration
               SET last_probe_at=?, last_probe_ok=?, last_probe_error=?,
                   capabilities_snapshot=COALESCE(?, capabilities_snapshot), updated_at=?
               WHERE harness_id=?""",
            (
                datetime.now().astimezone().isoformat(),
                None if ok is None else (1 if ok else 0),
                error,
                dumps(capabilities) if capabilities is not None else None,
                datetime.now().astimezone().isoformat(),
                harness_id,
            ),
        )

    # ---- credential ----

    async def upsert_credential(self, c: CredentialRef) -> None:
        await self.db.execute(
            """INSERT INTO credential_ref(credential_id, label, kind, secret_locator,
                   base_url, default_model, revoked, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(credential_id) DO UPDATE SET
                   label=excluded.label, kind=excluded.kind,
                   secret_locator=excluded.secret_locator, base_url=excluded.base_url,
                   default_model=excluded.default_model,
                   revoked=excluded.revoked, updated_at=excluded.updated_at""",
            (
                c.credential_id,
                c.label,
                c.kind.value,
                c.secret_locator,
                c.base_url,
                c.default_model,
                1 if c.revoked else 0,
                c.created_at.isoformat(),
                c.updated_at.isoformat(),
            ),
        )

    async def get_credential(self, credential_id: str) -> CredentialRef | None:
        row = await self.db.fetch_one(
            "SELECT * FROM credential_ref WHERE credential_id=?", (credential_id,)
        )
        return self._to_credential(row) if row else None

    async def list_credentials(self) -> list[CredentialRef]:
        rows = await self.db.fetch_all("SELECT * FROM credential_ref ORDER BY created_at")
        return [self._to_credential(r) for r in rows]

    async def set_credential_revoked(self, credential_id: str, revoked: bool) -> bool:
        return (
            await self.db.execute_rowcount(
                "UPDATE credential_ref SET revoked=?, updated_at=? WHERE credential_id=?",
                (
                    1 if revoked else 0,
                    datetime.now().astimezone().isoformat(),
                    credential_id,
                ),
            )
            > 0
        )

    # ---- skill ----

    async def upsert_skill(self, s: SkillDoc) -> None:
        await self.db.execute(
            """INSERT INTO skill_doc(skill_id, name, content, version, scope, enabled,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(skill_id) DO UPDATE SET
                   name=excluded.name, content=excluded.content, version=excluded.version,
                   scope=excluded.scope, enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (
                s.skill_id,
                s.name,
                s.content,
                s.version,
                s.scope.value,
                1 if s.enabled else 0,
                s.created_at.isoformat(),
                s.updated_at.isoformat(),
            ),
        )

    async def get_skill(self, skill_id: str) -> SkillDoc | None:
        row = await self.db.fetch_one("SELECT * FROM skill_doc WHERE skill_id=?", (skill_id,))
        return self._to_skill(row) if row else None

    async def list_skills(self) -> list[SkillDoc]:
        rows = await self.db.fetch_all("SELECT * FROM skill_doc ORDER BY created_at")
        return [self._to_skill(r) for r in rows]

    async def delete_skill(self, skill_id: str) -> bool:
        return await self.db.execute_rowcount(
            "DELETE FROM skill_doc WHERE skill_id=?", (skill_id,)
        ) > 0

    # ---- tool ----

    async def upsert_tool(self, t: ToolSpec) -> None:
        await self.db.execute(
            """INSERT INTO tool_spec(tool_id, name, kind, launch, description, io_schema,
                   risk_level, approval_policy, version, health_check, enabled,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tool_id) DO UPDATE SET
                   name=excluded.name, kind=excluded.kind, launch=excluded.launch,
                   description=excluded.description, io_schema=excluded.io_schema,
                   risk_level=excluded.risk_level, approval_policy=excluded.approval_policy,
                   version=excluded.version, health_check=excluded.health_check,
                   enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (
                t.tool_id,
                t.name,
                t.kind,
                t.launch.model_dump_json(),
                t.description,
                dumps(t.io_schema),
                t.risk_level.value,
                t.approval_policy.value,
                t.version,
                1 if t.health_check else 0,
                1 if t.enabled else 0,
                t.created_at.isoformat(),
                t.updated_at.isoformat(),
            ),
        )

    async def get_tool(self, tool_id: str) -> ToolSpec | None:
        row = await self.db.fetch_one("SELECT * FROM tool_spec WHERE tool_id=?", (tool_id,))
        return self._to_tool(row) if row else None

    async def list_tools(self) -> list[ToolSpec]:
        rows = await self.db.fetch_all("SELECT * FROM tool_spec ORDER BY created_at")
        return [self._to_tool(r) for r in rows]

    async def delete_tool(self, tool_id: str) -> bool:
        return await self.db.execute_rowcount(
            "DELETE FROM tool_spec WHERE tool_id=?", (tool_id,)
        ) > 0

    # ---- template ----

    async def upsert_template(self, t: Template) -> None:
        await self.db.execute(
            """INSERT INTO template(template_id, name, description, kind, payload, version,
                   source_revision, source_workflow_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(template_id) DO UPDATE SET
                   name=excluded.name, description=excluded.description, kind=excluded.kind,
                   payload=excluded.payload, version=excluded.version,
                   source_revision=excluded.source_revision,
                   source_workflow_id=excluded.source_workflow_id,
                   updated_at=excluded.updated_at""",
            (
                t.template_id,
                t.name,
                t.description,
                t.kind.value,
                t.payload.model_dump_json(),
                t.version,
                t.source_revision,
                t.source_workflow_id,
                t.created_at.isoformat(),
                t.updated_at.isoformat(),
            ),
        )

    async def get_template(self, template_id: str) -> Template | None:
        row = await self.db.fetch_one(
            "SELECT * FROM template WHERE template_id=?", (template_id,)
        )
        return self._to_template(row) if row else None

    async def list_templates(self, *, kind: TemplateKind | None = None) -> list[Template]:
        if kind is None:
            rows = await self.db.fetch_all("SELECT * FROM template ORDER BY created_at")
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM template WHERE kind=? ORDER BY created_at", (kind.value,)
            )
        return [self._to_template(r) for r in rows]

    async def delete_template(self, template_id: str) -> bool:
        return await self.db.execute_rowcount(
            "DELETE FROM template WHERE template_id=?", (template_id,)
        ) > 0

    # ---- 一致性快照 ----

    async def snapshot(self) -> InMemoryRegistry:
        """载入全部注册项，供纯函数校验管线使用。"""
        return InMemoryRegistry(
            harnesses=await self.list_harnesses(),
            credentials=await self.list_credentials(),
            skills=await self.list_skills(),
            tools=await self.list_tools(),
        )

    # ---- mappers ----

    @staticmethod
    def _to_harness(row: sqlite3.Row) -> HarnessRegistration:
        return HarnessRegistration(
            harness_id=row["harness_id"],
            name=row["name"],
            adapter_id=row["adapter_id"],
            adapter_version=row["adapter_version"],
            exec_path=row["exec_path"],
            env_template=loads(row["env_template"], {}),
            cwd=row["cwd"],
            auth_binding=row["auth_binding"],
            auth_mode=AuthMode(row["auth_mode"]),
            capabilities_snapshot=loads(row["capabilities_snapshot"]),
            last_probe_at=row["last_probe_at"],
            last_probe_ok=None if row["last_probe_ok"] is None else bool(row["last_probe_ok"]),
            last_probe_error=row["last_probe_error"],
            enabled=bool(row["enabled"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_credential(row: sqlite3.Row) -> CredentialRef:
        return CredentialRef(
            credential_id=row["credential_id"],
            label=row["label"],
            kind=CredentialKind(row["kind"]),
            secret_locator=row["secret_locator"],
            base_url=row["base_url"],
            default_model=row["default_model"],
            revoked=bool(row["revoked"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_skill(row: sqlite3.Row) -> SkillDoc:
        return SkillDoc(
            skill_id=row["skill_id"],
            name=row["name"],
            content=row["content"],
            version=row["version"],
            scope=SkillScope(row["scope"]),
            enabled=bool(row["enabled"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_tool(row: sqlite3.Row) -> ToolSpec:
        launch = ToolLaunch.model_validate_json(row["launch"])
        return ToolSpec(
            tool_id=row["tool_id"],
            name=row["name"],
            kind=row["kind"],
            launch=launch,
            description=row["description"],
            io_schema=loads(row["io_schema"], {}),
            risk_level=RiskLevel(row["risk_level"]),
            approval_policy=ApprovalPolicy(row["approval_policy"]),
            version=row["version"],
            health_check=bool(row["health_check"]),
            enabled=bool(row["enabled"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_template(row: sqlite3.Row) -> Template:
        return Template(
            template_id=row["template_id"],
            name=row["name"],
            description=row["description"],
            kind=TemplateKind(row["kind"]),
            payload=TemplatePayload.model_validate_json(row["payload"]),
            version=row["version"],
            source_revision=row["source_revision"],
            source_workflow_id=row["source_workflow_id"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# ===========================================================================
# 运行时
# ===========================================================================


class TaskRepository:
    """Task / TaskStage / Attempt。所有状态迁移都是带守卫的 CAS 更新。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- task ----

    async def create_task_with_stages(
        self, task: Task, stages: Sequence[TaskStage]
    ) -> tuple[Task, bool]:
        """原子地创建任务与其全部阶段。

        返回 ``(task, created)``。``created=False`` 表示命中幂等键，
        返回的是**已存在**的那次提交（RUN-02：网络重送不产生额外任务）。
        """
        async with self.db.transaction():
            try:
                await self.db.execute(
                    """INSERT INTO task(task_id, workflow_id, workflow_name, idempotency_key,
                           revision_seq, effective_graph_version, graph_snapshot, input_payload,
                           desired_state, observed_state, control_epoch, priority,
                           failure_summary, blocked_reason, last_origin, submitted_by,
                           created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task.task_id,
                        task.workflow_id,
                        task.workflow_name,
                        task.idempotency_key,
                        task.revision_seq,
                        task.effective_graph_version,
                        task.graph_snapshot.model_dump_json(),
                        dumps(task.input_payload),
                        task.desired_state.value,
                        task.observed_state.value,
                        task.control_epoch,
                        task.priority,
                        dumps(task.failure_summary) if task.failure_summary else None,
                        task.blocked_reason,
                        task.last_origin.model_dump_json() if task.last_origin else None,
                        task.submitted_by,
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = await self.find_by_idempotency(
                    task.workflow_id, task.idempotency_key
                )
                if existing is None:
                    raise
                return existing, False

            for st in stages:
                await self._insert_stage(st)
        return task, True

    async def find_by_idempotency(
        self, workflow_id: str, idempotency_key: str | None
    ) -> Task | None:
        if not idempotency_key:
            return None
        row = await self.db.fetch_one(
            "SELECT * FROM task WHERE workflow_id=? AND idempotency_key=?",
            (workflow_id, idempotency_key),
        )
        return self._to_task(row) if row else None

    async def get_task(self, task_id: str) -> Task | None:
        row = await self.db.fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
        return self._to_task(row) if row else None

    async def list_tasks(
        self,
        *,
        workflow_id: str | None = None,
        states: Iterable[TaskState] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Task]:
        sql = "SELECT * FROM task"
        params: list[Any] = []
        clauses: list[str] = []
        if workflow_id:
            clauses.append("workflow_id=?")
            params.append(workflow_id)
        if states:
            state_list = list(states)
            clauses.append(f"observed_state IN ({','.join('?' * len(state_list))})")
            params.extend(s.value for s in state_list)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self.db.fetch_all(sql, params)
        return [self._to_task(r) for r in rows]

    async def list_live_tasks(self) -> list[Task]:
        """非终态任务。启动对账与 Reaper 的输入。"""
        placeholders = ",".join("?" * len(_TASK_TERMINAL))
        rows = await self.db.fetch_all(
            f"SELECT * FROM task WHERE observed_state NOT IN ({placeholders})",
            tuple(s.value for s in _TASK_TERMINAL),
        )
        return [self._to_task(r) for r in rows]

    async def count_active_tasks(self, workflow_id: str) -> int:
        """占用并发额度的任务数。终态与已暂停的不计入（D-03 背压入口）。"""
        placeholders = ",".join("?" * len(_TASK_TERMINAL))
        return int(
            await self.db.fetch_value(
                f"""SELECT COUNT(*) FROM task
                    WHERE workflow_id=? AND observed_state NOT IN ({placeholders})""",
                (workflow_id, *(s.value for s in _TASK_TERMINAL)),
                default=0,
            )
        )

    async def update_task(
        self,
        task_id: str,
        *,
        to_state: TaskState | None = None,
        from_states: Iterable[TaskState] | None = None,
        expected_epoch: int | None = None,
        bump_epoch: bool = False,
        **fields: Any,
    ) -> bool:
        """带守卫的 CAS 更新。返回 False 表示守卫未通过（并发方已改过）。"""
        sets: list[str] = []
        params: list[Any] = []

        for k, v in fields.items():
            if k not in _TASK_UPDATABLE:
                raise ValueError(f"不可更新的字段: {k}")
            sets.append(f"{k}=?")
            params.append(_encode_field(k, v))

        if to_state is not None:
            sets.append("observed_state=?")
            params.append(to_state.value)
        if bump_epoch:
            sets.append("control_epoch=control_epoch+1")
        sets.append("updated_at=?")
        params.append(datetime.now().astimezone().isoformat())

        sql = f"UPDATE task SET {', '.join(sets)} WHERE task_id=?"
        params.append(task_id)
        if from_states is not None:
            states = list(from_states)
            sql += f" AND observed_state IN ({','.join('?' * len(states))})"
            params.extend(s.value for s in states)
        if expected_epoch is not None:
            sql += " AND control_epoch=?"
            params.append(expected_epoch)
        return await self.db.execute_rowcount(sql, params) > 0

    # ---- stage ----

    async def _insert_stage(self, st: TaskStage) -> None:
        await self.db.execute(
            """INSERT INTO task_stage(stage_id, task_id, node_id, node_name, desired_state,
                   observed_state, control_epoch, node_priority, task_priority, enqueued_at,
                   current_attempt_seq, attempt_count, profile_cursor, blocked_reason,
                   status_reason, origin_of_control, upstream_pins, checkpoint_ref,
                   requires_reconcile, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            self._stage_params(st),
        )

    async def create_stages(self, stages: Sequence[TaskStage]) -> None:
        async with self.db.transaction():
            for st in stages:
                await self._insert_stage(st)

    async def get_stage(self, stage_id: str) -> TaskStage | None:
        row = await self.db.fetch_one("SELECT * FROM task_stage WHERE stage_id=?", (stage_id,))
        return self._to_stage(row) if row else None

    async def list_stages(self, task_id: str) -> list[TaskStage]:
        rows = await self.db.fetch_all(
            "SELECT * FROM task_stage WHERE task_id=? ORDER BY enqueued_at", (task_id,)
        )
        return [self._to_stage(r) for r in rows]

    async def list_stages_by_ids(self, stage_ids: Sequence[str]) -> list[TaskStage]:
        if not stage_ids:
            return []
        rows = await self.db.fetch_all(
            f"SELECT * FROM task_stage WHERE stage_id IN ({','.join('?' * len(stage_ids))})",
            list(stage_ids),
        )
        return [self._to_stage(r) for r in rows]

    async def list_stages_by_node(
        self, node_id: str, *, states: Iterable[StageState] | None = None, limit: int = 500
    ) -> list[TaskStage]:
        """节点队列投影：按 queue_order 排序的只读视图（RUN-04）。"""
        sql = "SELECT * FROM task_stage WHERE node_id=?"
        params: list[Any] = [node_id]
        if states:
            state_list = list(states)
            sql += f" AND observed_state IN ({','.join('?' * len(state_list))})"
            params.extend(s.value for s in state_list)
        sql += " ORDER BY node_priority DESC, task_priority DESC, enqueued_at ASC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetch_all(sql, params)
        return [self._to_stage(r) for r in rows]

    async def list_all_stages_by_node(self, node_id: str, *, limit: int = 1000) -> list[TaskStage]:
        """含全部状态，供历史与「节点关联的 session」展示（RUN-03）。"""
        rows = await self.db.fetch_all(
            """SELECT * FROM task_stage WHERE node_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (node_id, limit),
        )
        return [self._to_stage(r) for r in rows]

    async def list_ready_stages(self, limit: int = 500) -> list[TaskStage]:
        rows = await self.db.fetch_all(
            """SELECT * FROM task_stage WHERE observed_state='ready'
               ORDER BY node_priority DESC, task_priority DESC, enqueued_at ASC LIMIT ?""",
            (limit,),
        )
        return [self._to_stage(r) for r in rows]

    async def list_stages_in_states(self, states: Iterable[StageState]) -> list[TaskStage]:
        state_list = list(states)
        if not state_list:
            return []
        rows = await self.db.fetch_all(
            f"""SELECT * FROM task_stage
                WHERE observed_state IN ({','.join('?' * len(state_list))})""",
            [s.value for s in state_list],
        )
        return [self._to_stage(r) for r in rows]

    async def node_slot_taken(self, node_id: str) -> bool:
        """节点执行槽是否被占用（D-03：只有 DISPATCHING/RUNNING 占槽）。"""
        n = await self.db.fetch_value(
            """SELECT COUNT(*) FROM task_stage
               WHERE node_id=? AND observed_state IN ('dispatching','running')""",
            (node_id,),
            default=0,
        )
        return int(n) > 0

    async def update_stage(
        self,
        stage_id: str,
        *,
        to_state: StageState | None = None,
        from_states: Iterable[StageState] | None = None,
        expected_epoch: int | None = None,
        bump_epoch: bool = False,
        **fields: Any,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []

        for k, v in fields.items():
            if k not in _STAGE_UPDATABLE:
                raise ValueError(f"不可更新的阶段字段: {k}")
            sets.append(f"{k}=?")
            params.append(_encode_field(k, v))

        if to_state is not None:
            sets.append("observed_state=?")
            params.append(to_state.value)
        if bump_epoch:
            sets.append("control_epoch=control_epoch+1")
        sets.append("updated_at=?")
        params.append(datetime.now().astimezone().isoformat())

        sql = f"UPDATE task_stage SET {', '.join(sets)} WHERE stage_id=?"
        params.append(stage_id)
        if from_states is not None:
            states = list(from_states)
            sql += f" AND observed_state IN ({','.join('?' * len(states))})"
            params.extend(s.value for s in states)
        if expected_epoch is not None:
            sql += " AND control_epoch=?"
            params.append(expected_epoch)
        return await self.db.execute_rowcount(sql, params) > 0

    async def reorder_stage(self, stage_id: str, *, node_priority: int) -> bool:
        """调序：只改 node_priority，不碰状态、不碰依赖（RUN-04）。"""
        return (
            await self.db.execute_rowcount(
                "UPDATE task_stage SET node_priority=?, updated_at=? WHERE stage_id=?",
                (node_priority, datetime.now().astimezone().isoformat(), stage_id),
            )
            > 0
        )

    # ---- attempt ----

    async def create_attempt(self, at: Attempt) -> Attempt:
        await self.db.execute(
            """INSERT INTO attempt(attempt_id, stage_id, task_id, node_id, attempt_seq,
                   profile_id, profile_snapshot, session_ref, lease_id, lease_expires_at,
                   generation, usage, outcome, compact_events, started_at, ended_at,
                   resume_from_checkpoint, reattached, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                at.attempt_id,
                at.stage_id,
                at.task_id,
                at.node_id,
                at.attempt_seq,
                at.profile_id,
                dumps(at.profile_snapshot),
                at.session_ref,
                at.lease_id,
                _iso(at.lease_expires_at),
                at.generation,
                at.usage.model_dump_json() if at.usage else None,
                at.outcome.model_dump_json() if at.outcome else None,
                dumps([e.model_dump(mode="json") for e in at.compact_events]),
                _iso(at.started_at),
                _iso(at.ended_at),
                at.resume_from_checkpoint,
                1 if at.reattached else 0,
                at.created_at.isoformat(),
                at.updated_at.isoformat(),
            ),
        )
        return at

    async def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = await self.db.fetch_one("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,))
        return self._to_attempt(row) if row else None

    async def list_attempts(
        self, stage_id: str, *, descending: bool = True
    ) -> list[Attempt]:
        order = "DESC" if descending else "ASC"
        rows = await self.db.fetch_all(
            f"SELECT * FROM attempt WHERE stage_id=? ORDER BY attempt_seq {order}",
            (stage_id,),
        )
        return [self._to_attempt(r) for r in rows]

    async def list_attempts_for_task(self, task_id: str) -> list[Attempt]:
        rows = await self.db.fetch_all(
            "SELECT * FROM attempt WHERE task_id=? ORDER BY created_at", (task_id,)
        )
        return [self._to_attempt(r) for r in rows]

    async def update_attempt(self, attempt_id: str, **fields: Any) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in _ATTEMPT_UPDATABLE:
                raise ValueError(f"不可更新的尝试字段: {k}")
            sets.append(f"{k}=?")
            params.append(_encode_field(k, v))
        if not sets:
            return True
        sets.append("updated_at=?")
        params.append(datetime.now().astimezone().isoformat())
        params.append(attempt_id)
        return (
            await self.db.execute_rowcount(
                f"UPDATE attempt SET {', '.join(sets)} WHERE attempt_id=?", params
            )
            > 0
        )

    async def complete_attempt(
        self,
        attempt_id: str,
        *,
        outcome: AttemptOutcome,
        usage: Usage | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """完成回调。代次过期的回调被丢弃（REC-05）。"""
        sql = (
            "UPDATE attempt SET outcome=?, usage=COALESCE(?, usage), ended_at=?, updated_at=? "
            "WHERE attempt_id=? AND outcome IS NULL"
        )
        params: list[Any] = [
            outcome.model_dump_json(),
            usage.model_dump_json() if usage else None,
            datetime.now().astimezone().isoformat(),
            datetime.now().astimezone().isoformat(),
            attempt_id,
        ]
        if expected_generation is not None:
            sql += " AND generation=?"
            params.append(expected_generation)
        return await self.db.execute_rowcount(sql, params) > 0

    async def bump_attempt_generation(self, attempt_id: str) -> None:
        """清理后递增代次：迟到回调按代次丢弃（REC-05、§10.4）。"""
        await self.db.execute(
            "UPDATE attempt SET generation=generation+1, updated_at=? WHERE attempt_id=?",
            (datetime.now().astimezone().isoformat(), attempt_id),
        )

    # ---- mappers ----

    @staticmethod
    def _stage_params(st: TaskStage) -> tuple:
        return (
            st.stage_id,
            st.task_id,
            st.node_id,
            st.node_name,
            st.desired_state.value,
            st.observed_state.value,
            st.control_epoch,
            st.node_priority,
            st.task_priority,
            st.enqueued_at.isoformat(),
            st.current_attempt_seq,
            st.attempt_count,
            st.profile_cursor,
            st.blocked_reason,
            st.status_reason,
            st.origin_of_control.model_dump_json() if st.origin_of_control else None,
            dumps(st.upstream_pins),
            st.checkpoint_ref,
            1 if st.requires_reconcile else 0,
            st.created_at.isoformat(),
            st.updated_at.isoformat(),
        )

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            workflow_id=row["workflow_id"],
            workflow_name=row["workflow_name"],
            idempotency_key=row["idempotency_key"],
            revision_seq=row["revision_seq"],
            effective_graph_version=row["effective_graph_version"],
            graph_snapshot=PinnedGraph.model_validate_json(row["graph_snapshot"]),
            input_payload=loads(row["input_payload"], {}),
            desired_state=DesiredState(row["desired_state"]),
            observed_state=TaskState(row["observed_state"]),
            control_epoch=row["control_epoch"],
            priority=row["priority"],
            failure_summary=loads(row["failure_summary"]),
            blocked_reason=row["blocked_reason"],
            last_origin=OriginOfControl.model_validate_json(row["last_origin"])
            if row["last_origin"]
            else None,
            submitted_by=row["submitted_by"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_stage(row: sqlite3.Row) -> TaskStage:
        return TaskStage(
            stage_id=row["stage_id"],
            task_id=row["task_id"],
            node_id=row["node_id"],
            node_name=row["node_name"],
            desired_state=DesiredState(row["desired_state"]),
            observed_state=StageState(row["observed_state"]),
            control_epoch=row["control_epoch"],
            node_priority=row["node_priority"],
            task_priority=row["task_priority"],
            enqueued_at=_dt(row["enqueued_at"]),
            current_attempt_seq=row["current_attempt_seq"],
            attempt_count=row["attempt_count"],
            profile_cursor=row["profile_cursor"],
            blocked_reason=row["blocked_reason"],
            status_reason=row["status_reason"],
            origin_of_control=OriginOfControl.model_validate_json(row["origin_of_control"])
            if row["origin_of_control"]
            else None,
            upstream_pins=loads(row["upstream_pins"], {}),
            checkpoint_ref=row["checkpoint_ref"],
            requires_reconcile=bool(row["requires_reconcile"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(
            attempt_id=row["attempt_id"],
            stage_id=row["stage_id"],
            task_id=row["task_id"],
            node_id=row["node_id"],
            attempt_seq=row["attempt_seq"],
            profile_id=row["profile_id"],
            profile_snapshot=loads(row["profile_snapshot"], {}),
            session_ref=row["session_ref"],
            lease_id=row["lease_id"],
            lease_expires_at=_dt(row["lease_expires_at"]),
            generation=row["generation"],
            usage=Usage.model_validate_json(row["usage"]) if row["usage"] else None,
            outcome=AttemptOutcome.model_validate_json(row["outcome"])
            if row["outcome"]
            else None,
            compact_events=[
                CompactEvent.model_validate(e) for e in loads(row["compact_events"], [])
            ],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            resume_from_checkpoint=row["resume_from_checkpoint"],
            reattached=bool(row["reattached"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# ===========================================================================
# 资源台账与审批
# ===========================================================================


class ResourceRepository:
    """资源台账（RES-01/02）。清理路径只信这本账。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def register(
        self,
        *,
        resource_id: str,
        kind: str,
        locator: dict[str, Any],
        owner_task_id: str | None = None,
        owner_stage_id: str | None = None,
        owner_attempt_id: str | None = None,
        owner_node_id: str | None = None,
        teardown: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        await self.db.execute(
            """INSERT INTO resource_record(resource_id, owner_task_id, owner_stage_id,
                   owner_attempt_id, owner_node_id, kind, locator, state, teardown,
                   ref_count, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'open', ?,1,?,?)""",
            (
                resource_id,
                owner_task_id,
                owner_stage_id,
                owner_attempt_id,
                owner_node_id,
                kind,
                dumps(locator),
                dumps(teardown or {}),
                now,
                now,
            ),
        )

    async def list_open(self, limit: int = 5000) -> list[sqlite3.Row]:
        return await self.db.fetch_all(
            "SELECT * FROM resource_record WHERE state IN ('open','closing','teardown_failed') "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        )

    async def list_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        return await self.db.fetch_all(
            "SELECT * FROM resource_record WHERE owner_attempt_id=?", (attempt_id,)
        )

    async def list_for_task(self, task_id: str) -> list[sqlite3.Row]:
        return await self.db.fetch_all(
            "SELECT * FROM resource_record WHERE owner_task_id=?", (task_id,)
        )

    async def list_by_state(self, states: Iterable[str], limit: int = 1000) -> list[sqlite3.Row]:
        state_list = list(states)
        if not state_list:
            return []
        return await self.db.fetch_all(
            f"""SELECT * FROM resource_record
                WHERE state IN ({','.join('?' * len(state_list))}) LIMIT ?""",
            [*state_list, limit],
        )

    async def mark_state(
        self, resource_id: str, state: str, *, error: str | None = None
    ) -> bool:
        return (
            await self.db.execute_rowcount(
                "UPDATE resource_record SET state=?, last_error=?, updated_at=? "
                "WHERE resource_id=?",
                (state, error, datetime.now().astimezone().isoformat(), resource_id),
            )
            > 0
        )

    async def close_all_for_attempt(self, attempt_id: str) -> list[str]:
        """把某次尝试名下的全部句柄标记为 closing，返回其 resource_id。

        「标记为 closing」与「真的关掉了」是两件事——后者由调用方逐个 teardown
        并回写 closed / teardown_failed（LIFE-06 的三态分离）。
        """
        rows = await self.list_for_attempt(attempt_id)
        ids = [r["resource_id"] for r in rows if r["state"] in ("open", "teardown_failed")]
        for rid in ids:
            await self.mark_state(rid, "closing")
        return ids


class ApprovalRepository:
    """审批持久化。断连期间审批保持 pending，重连后找回（AC-12）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, a: Approval) -> Approval:
        await self.db.execute(
            """INSERT INTO approval(approval_id, task_id, stage_id, attempt_id, revision_seq,
                   node_id, tool_name, action_fingerprint, action, target, risk, status,
                   timeout_policy, decision, detail, expires_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a.approval_id,
                a.bound_to.task_id,
                a.bound_to.stage_id,
                a.bound_to.attempt_id,
                a.bound_to.revision_seq,
                a.bound_to.node_id,
                a.tool_name,
                a.bound_to.action_fingerprint,
                a.action,
                a.target,
                a.risk,
                a.status.value,
                a.timeout_policy.value,
                a.decision.model_dump_json() if a.decision else None,
                a.detail,
                _iso(a.expires_at),
                a.created_at.isoformat(),
                a.updated_at.isoformat(),
            ),
        )
        return a

    async def get(self, approval_id: str) -> Approval | None:
        row = await self.db.fetch_one(
            "SELECT * FROM approval WHERE approval_id=?", (approval_id,)
        )
        return self._to_approval(row) if row else None

    async def list_open(self, limit: int = 500) -> list[Approval]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approval WHERE status IN ('pending','undeliverable') "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return [self._to_approval(r) for r in rows]

    async def list_for_task(self, task_id: str) -> list[Approval]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approval WHERE task_id=? ORDER BY created_at", (task_id,)
        )
        return [self._to_approval(r) for r in rows]

    async def list_for_attempt(self, attempt_id: str) -> list[Approval]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approval WHERE attempt_id=? ORDER BY created_at", (attempt_id,)
        )
        return [self._to_approval(r) for r in rows]

    async def decide(self, approval_id: str, *, status: ApprovalStatus, decision: ApprovalDecision,
                     detail: str | None = None) -> bool:
        """写入决定。**只允许从 pending 迁移**——重复通知不重复授权（HUM-04）。"""
        return (
            await self.db.execute_rowcount(
                """UPDATE approval SET status=?, decision=?, detail=COALESCE(?, detail),
                       updated_at=?
                   WHERE approval_id=? AND status='pending'""",
                (
                    status.value,
                    decision.model_dump_json(),
                    detail,
                    datetime.now().astimezone().isoformat(),
                    approval_id,
                ),
            )
            > 0
        )

    async def invalidate_for_attempt(self, attempt_id: str, why: str) -> int:
        """尝试失效 → 旧批准作废（AC-14）。"""
        return await self.db.execute_rowcount(
            """UPDATE approval SET status='superseded', detail=?, updated_at=?
               WHERE attempt_id=? AND status IN ('pending','undeliverable')""",
            (why, datetime.now().astimezone().isoformat(), attempt_id),
        )

    async def invalidate_for_task(self, task_id: str, why: str) -> int:
        return await self.db.execute_rowcount(
            """UPDATE approval SET status='superseded', detail=?, updated_at=?
               WHERE task_id=? AND status IN ('pending','undeliverable')""",
            (why, datetime.now().astimezone().isoformat(), task_id),
        )

    @staticmethod
    def _to_approval(row: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            bound_to=ApprovalBinding(
                task_id=row["task_id"],
                stage_id=row["stage_id"],
                attempt_id=row["attempt_id"],
                revision_seq=row["revision_seq"] or 0,
                node_id=row["node_id"],
                # 指纹必须回来：AC-14 的「命令内容变化即旧批准作废」靠它比较，
                # 丢了它这条规则就永远判成「没变」。
                action_fingerprint=row["action_fingerprint"],
            ),
            action=row["action"],
            tool_name=row["tool_name"],
            target=row["target"],
            risk=row["risk"],
            status=ApprovalStatus(row["status"]),
            timeout_policy=ApprovalTimeoutPolicy(row["timeout_policy"]),
            decision=ApprovalDecision.model_validate_json(row["decision"])
            if row["decision"]
            else None,
            detail=row["detail"],
            expires_at=_dt(row["expires_at"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# ===========================================================================
# 基础助手（AI-01）
# ===========================================================================


class AssistantRepository:
    """助手线程与消息。返回的是普通字典——它们没有对应的领域实体，
    字段集就是表结构本身（schema 迁移 7/8）。

    对话历史只增不改：``update_message`` 只放行元信息列（backend/用量/degraded），
    不提供改写正文或删除的路径——「整理前文」是追加一条 memory 消息，不是篡改历史。
    线程元信息同理走 ``update_thread`` 的白名单（title/closed）。
    """

    _MESSAGE_UPDATABLE = {"backend", "tokens_in", "tokens_out", "degraded"}
    _THREAD_UPDATABLE = {"title", "closed"}

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- thread ----

    async def create_thread(self, thread_id: str, title: str = "") -> dict[str, Any]:
        now = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO assistant_thread(thread_id, title, closed, created_at, updated_at)
               VALUES (?,?,0,?,?)""",
            (thread_id, title, now, now),
        )
        return {
            "thread_id": thread_id,
            "title": title,
            "closed": False,
            "created_at": now,
            "updated_at": now,
        }

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM assistant_thread WHERE thread_id=?", (thread_id,)
        )
        return self._to_thread(row) if row else None

    async def list_threads(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM assistant_thread ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [self._to_thread(r) for r in rows]

    async def update_thread(self, thread_id: str, **fields: Any) -> bool:
        """白名单元信息更新（title/closed）。改名不触碰任何消息。"""
        sets: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in self._THREAD_UPDATABLE:
                raise ValueError(f"不可更新的助手线程字段: {k}")
            sets.append(f"{k}=?")
            params.append(1 if isinstance(v, bool) else v)
        if not sets:
            return True
        sets.append("updated_at=?")
        params.append(utcnow().isoformat())
        params.append(thread_id)
        return (
            await self.db.execute_rowcount(
                f"UPDATE assistant_thread SET {', '.join(sets)} WHERE thread_id=?",
                params,
            )
            > 0
        )

    # ---- message ----

    async def append_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        role: str,
        content: str,
        backend: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        degraded: bool = False,
        reasoning: str | None = None,
    ) -> dict[str, Any]:
        """追加一条消息，并把线程的 updated_at 顶到最新（列表按它排序）。

        ``reasoning`` 是助手回复的推理过程（迁移 8 新增列）：与正文分开存，
        没有推理内容时保持 None（「没有」≠ 空串）。
        """
        now = utcnow().isoformat()
        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO assistant_message(message_id, thread_id, role, content,
                       backend, tokens_in, tokens_out, degraded, reasoning, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    message_id,
                    thread_id,
                    role,
                    content,
                    backend,
                    tokens_in,
                    tokens_out,
                    1 if degraded else 0,
                    reasoning,
                    now,
                ),
            )
            await self.db.execute(
                "UPDATE assistant_thread SET updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "backend": backend,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "degraded": degraded,
            "reasoning": reasoning,
            "created_at": now,
        }

    async def list_messages(
        self, thread_id: str, *, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """按时间正序取回。同刻消息以 rowid 定序（插入顺序即对话顺序）。"""
        rows = await self.db.fetch_all(
            """SELECT * FROM assistant_message WHERE thread_id=?
               ORDER BY created_at, rowid LIMIT ?""",
            (thread_id, limit),
        )
        return [self._to_message(r) for r in rows]

    async def update_message(self, message_id: str, **fields: Any) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in self._MESSAGE_UPDATABLE:
                raise ValueError(f"不可更新的助手消息字段: {k}")
            sets.append(f"{k}=?")
            params.append(1 if isinstance(v, bool) else v)
        if not sets:
            return True
        params.append(message_id)
        return (
            await self.db.execute_rowcount(
                f"UPDATE assistant_message SET {', '.join(sets)} WHERE message_id=?",
                params,
            )
            > 0
        )

    # ---- 草稿提案（assistant_draft，迁移 9） ----

    async def create_draft(
        self,
        *,
        draft_id: str,
        message_id: str,
        thread_id: str,
        kind: str,
        payload: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        """落一份草稿提案。状态恒为 pending：采用/拒绝是之后用户的显式决定。"""
        now = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO assistant_draft(draft_id, message_id, thread_id, kind,
                   payload, validation, status, adopted_ref, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'pending', NULL, ?,?)""",
            (draft_id, message_id, thread_id, kind, dumps(payload), dumps(validation), now, now),
        )
        return {
            "draft_id": draft_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "kind": kind,
            "payload": payload,
            "validation": validation,
            "status": "pending",
            "adopted_ref": None,
            "created_at": now,
            "updated_at": now,
        }

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM assistant_draft WHERE draft_id=?", (draft_id,)
        )
        return self._to_draft(row) if row else None

    async def list_drafts_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM assistant_draft WHERE thread_id=? ORDER BY created_at, rowid",
            (thread_id,),
        )
        return [self._to_draft(r) for r in rows]

    async def decide_draft(
        self,
        draft_id: str,
        *,
        to_status: str,
        adopted_ref: str | None = None,
        validation: dict[str, Any] | None = None,
    ) -> bool:
        """采用/拒绝的 CAS：只允许 pending → adopted|rejected。

        并发或重复点击时只有一个请求能迁移状态；失败方拿到 False，
        由服务层回 400 大白话说明（不产生第二份采用产物）。
        """
        assert to_status in ("adopted", "rejected")
        return (
            await self.db.execute_rowcount(
                """UPDATE assistant_draft
                   SET status=?, adopted_ref=?, validation=COALESCE(?, validation), updated_at=?
                   WHERE draft_id=? AND status='pending'""",
                (
                    to_status,
                    adopted_ref,
                    dumps(validation) if validation is not None else None,
                    utcnow().isoformat(),
                    draft_id,
                ),
            )
            > 0
        )

    # ---- mappers ----

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": row["draft_id"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "kind": row["kind"],
            "payload": loads(row["payload"], {}),
            "validation": loads(row["validation"], {}),
            "status": row["status"],
            "adopted_ref": row["adopted_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _to_thread(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "thread_id": row["thread_id"],
            "title": row["title"],
            "closed": bool(row["closed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _to_message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "role": row["role"],
            "content": row["content"],
            "backend": row["backend"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "degraded": bool(row["degraded"]),
            "reasoning": row["reasoning"],
            "created_at": row["created_at"],
        }


# ===========================================================================
# 可更新字段白名单与编码
# ===========================================================================

_TASK_UPDATABLE = {
    "input_payload",
    "desired_state",
    "priority",
    "failure_summary",
    "blocked_reason",
    "last_origin",
    "workflow_name",
    "revision_seq",
    "effective_graph_version",
    "graph_snapshot",
    "idempotency_key",
}

_STAGE_UPDATABLE = {
    "desired_state",
    "node_priority",
    "task_priority",
    "current_attempt_seq",
    "attempt_count",
    "profile_cursor",
    "blocked_reason",
    "status_reason",
    "origin_of_control",
    "upstream_pins",
    "checkpoint_ref",
    "requires_reconcile",
    "node_name",
    "enqueued_at",
}

_ATTEMPT_UPDATABLE = {
    "session_ref",
    "lease_id",
    "lease_expires_at",
    "generation",
    "usage",
    "outcome",
    "compact_events",
    "started_at",
    "ended_at",
    "profile_snapshot",
    "resume_from_checkpoint",
    "reattached",
}

_TASK_TERMINAL = (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)


def _encode_field(name: str, value: Any) -> Any:
    """把领域值编码成 SQLite 可存的标量。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    if isinstance(value, dict):
        return dumps(value)
    if isinstance(value, (list, tuple)):
        return dumps(value)
    return value


# ===========================================================================
# 门面
# ===========================================================================


class Store:
    """数据库与各仓储的门面。

    同时构造 L4 的三个数据服务（事件日志／产物／消息总线），它们共享同一连接，
    因此「写状态 + 写事件」可以在同一个事务里完成。
    """

    def __init__(self, db: Database) -> None:
        from .artifact_store import ArtifactStore
        from .event_log import EventLog
        from .message_bus import MessageBus

        self.db = db
        self.workflows = WorkflowRepository(db)
        self.registry = RegistryRepository(db)
        self.tasks = TaskRepository(db)
        self.resources = ResourceRepository(db)
        self.approvals = ApprovalRepository(db)
        self.assistant = AssistantRepository(db)
        self.events = EventLog(db)
        self.artifacts = ArtifactStore(db)
        self.messages = MessageBus(db)

    @classmethod
    async def open(cls, path: str) -> "Store":
        db = Database(path)
        await db.connect()
        return cls(db)

    async def close(self) -> None:
        await self.db.close()
