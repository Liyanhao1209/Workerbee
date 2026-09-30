"""SQLite 表结构与迁移（架构设计 v0.02 D-08）。

设计口径：
- **当前态用关系表**，嵌套结构（图、候选、台账 locator 等）以 JSON 列承载，
  只把需要检索／排序／CAS 的字段提升为列。
- **迁移与操作留痕用 append-only 事件日志**（``event_log``）。控制操作先写事件
  日志再改状态（写前日志），使崩溃后的对账有据可依。
- 事件日志**不设自动 TTL**（清单 §4：历史≠泄漏），只提供手动清理与按任务归档。

迁移约定：``MIGRATIONS`` 顺序执行，每项记录版本号；已应用的版本记在
``schema_version``。新增迁移只能追加，不得修改已发布条目。
"""

from __future__ import annotations

SCHEMA_VERSION = 5

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
-- =====================================================================
-- L1 定义层
-- =====================================================================

CREATE TABLE IF NOT EXISTS workflow (
    workflow_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    description          TEXT,
    current_revision_seq INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'draft',
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 8,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_status ON workflow(status);

-- 修订不可变：只插入，不更新 graph_json。is_published 之外的列也只在发布时写一次。
CREATE TABLE IF NOT EXISTS workflow_revision (
    workflow_id            TEXT NOT NULL,
    revision_seq           INTEGER NOT NULL,
    source                 TEXT NOT NULL DEFAULT 'manual',
    draft_of               INTEGER,
    is_published           INTEGER NOT NULL DEFAULT 0,
    note                   TEXT,
    graph_json             TEXT NOT NULL,
    effective_graph_version INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (workflow_id, revision_seq),
    FOREIGN KEY (workflow_id) REFERENCES workflow(workflow_id) ON DELETE CASCADE
);

-- =====================================================================
-- 共享配置注册表（EXT-01/02/03、AUTH-01、HAR-01）
-- =====================================================================

CREATE TABLE IF NOT EXISTS harness_registration (
    harness_id            TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    adapter_id            TEXT NOT NULL,
    adapter_version       TEXT,
    exec_path             TEXT,
    env_template          TEXT NOT NULL DEFAULT '{}',
    cwd                   TEXT,
    auth_binding          TEXT,
    auth_mode             TEXT NOT NULL DEFAULT 'native_login',
    capabilities_snapshot TEXT,
    last_probe_at         TEXT,
    last_probe_ok         INTEGER,
    last_probe_error      TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credential_ref (
    credential_id  TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    secret_locator TEXT,
    base_url       TEXT,
    revoked        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_doc (
    skill_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    scope      TEXT NOT NULL DEFAULT 'global',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_spec (
    tool_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'mcp',
    launch    TEXT NOT NULL DEFAULT '{}',
    description TEXT,
    io_schema TEXT NOT NULL DEFAULT '{}',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    approval_policy TEXT NOT NULL DEFAULT 'ask',
    version   INTEGER NOT NULL DEFAULT 1,
    health_check INTEGER NOT NULL DEFAULT 1,
    enabled   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template (
    template_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    description          TEXT,
    kind                 TEXT NOT NULL DEFAULT 'workflow',
    payload              TEXT NOT NULL DEFAULT '{}',
    version              INTEGER NOT NULL DEFAULT 1,
    source_revision      INTEGER,
    source_workflow_id   TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- =====================================================================
-- L2 运行时内核
-- =====================================================================

CREATE TABLE IF NOT EXISTS task (
    task_id                TEXT PRIMARY KEY,
    workflow_id            TEXT NOT NULL,
    workflow_name          TEXT,
    idempotency_key        TEXT,
    revision_seq           INTEGER NOT NULL,
    effective_graph_version INTEGER NOT NULL,
    graph_snapshot         TEXT NOT NULL,
    input_payload          TEXT NOT NULL DEFAULT '{}',
    desired_state          TEXT NOT NULL DEFAULT 'active',
    observed_state         TEXT NOT NULL DEFAULT 'queued',
    control_epoch          INTEGER NOT NULL DEFAULT 0,
    priority               INTEGER NOT NULL DEFAULT 50,
    failure_summary        TEXT,
    blocked_reason         TEXT,
    last_origin            TEXT,
    submitted_by           TEXT NOT NULL DEFAULT 'user',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_workflow ON task(workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_state ON task(observed_state);
-- RUN-02：同一次提交因网络重送不得创建额外任务；不同 idempotency_key 才产生新任务。
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_idempotency
    ON task(workflow_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_stage (
    stage_id            TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    node_name           TEXT,
    desired_state       TEXT NOT NULL DEFAULT 'active',
    observed_state      TEXT NOT NULL DEFAULT 'waiting_deps',
    control_epoch       INTEGER NOT NULL DEFAULT 0,
    node_priority       INTEGER NOT NULL DEFAULT 50,
    task_priority       INTEGER NOT NULL DEFAULT 50,
    enqueued_at         TEXT NOT NULL,
    current_attempt_seq INTEGER NOT NULL DEFAULT 0,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    profile_cursor      INTEGER NOT NULL DEFAULT 0,
    blocked_reason      TEXT,
    status_reason       TEXT,
    origin_of_control   TEXT,
    upstream_pins       TEXT NOT NULL DEFAULT '{}',
    checkpoint_ref      TEXT,
    requires_reconcile  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stage_task ON task_stage(task_id);
-- 调度循环的热路径：按节点取 READY 阶段（D-03 的执行槽判定）。
CREATE INDEX IF NOT EXISTS idx_stage_node_state ON task_stage(node_id, observed_state);
-- 节点投影的只读队列视图（RUN-04）。
CREATE INDEX IF NOT EXISTS idx_stage_queue ON task_stage(
    node_id, observed_state, node_priority DESC, task_priority DESC, enqueued_at ASC
);

CREATE TABLE IF NOT EXISTS attempt (
    attempt_id          TEXT PRIMARY KEY,
    stage_id            TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    attempt_seq         INTEGER NOT NULL,
    profile_id          TEXT NOT NULL,
    profile_snapshot    TEXT NOT NULL DEFAULT '{}',
    session_ref         TEXT,
    lease_id            TEXT,
    lease_expires_at    TEXT,
    generation          INTEGER NOT NULL DEFAULT 1,
    usage               TEXT,
    outcome             TEXT,
    compact_events      TEXT NOT NULL DEFAULT '[]',
    started_at          TEXT,
    ended_at            TEXT,
    resume_from_checkpoint TEXT,
    reattached          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (stage_id) REFERENCES task_stage(stage_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attempt_stage ON attempt(stage_id, attempt_seq DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_task ON attempt(task_id);
CREATE INDEX IF NOT EXISTS idx_attempt_lease ON attempt(lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_seq ON attempt(stage_id, attempt_seq);

-- 资源台账（RES-01）。所有清理路径只信这本账，不依赖内存状态。
CREATE TABLE IF NOT EXISTS resource_record (
    resource_id      TEXT PRIMARY KEY,
    owner_task_id    TEXT,
    owner_stage_id   TEXT,
    owner_attempt_id TEXT,
    owner_node_id    TEXT,
    kind             TEXT NOT NULL,
    locator          TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL DEFAULT 'open',
    teardown         TEXT NOT NULL DEFAULT '{}',
    ref_count        INTEGER NOT NULL DEFAULT 1,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_attempt ON resource_record(owner_attempt_id);
CREATE INDEX IF NOT EXISTS idx_resource_task ON resource_record(owner_task_id);
CREATE INDEX IF NOT EXISTS idx_resource_state ON resource_record(state);
CREATE INDEX IF NOT EXISTS idx_resource_kind ON resource_record(kind, state);

-- Session 台账（L3）。core 重启后据此重接管（REC-03）。
CREATE TABLE IF NOT EXISTS session_handle (
    session_ref       TEXT PRIMARY KEY,
    harness_id        TEXT NOT NULL,
    owner_attempt_id  TEXT,
    owner_stage_id    TEXT,
    owner_task_id     TEXT,
    state             TEXT NOT NULL DEFAULT 'alive',
    persist_locator   TEXT,
    capabilities_used TEXT NOT NULL DEFAULT '[]',
    last_heartbeat    TEXT,
    generation        INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_attempt ON session_handle(owner_attempt_id);
CREATE INDEX IF NOT EXISTS idx_session_state ON session_handle(state);

-- 节点启停操作（D-01）：排水是有状态的过渡过程，必须可查、可续、可见。
CREATE TABLE IF NOT EXISTS node_activation_op (
    op_id        TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    revision_seq INTEGER NOT NULL,
    node_id      TEXT NOT NULL,
    enabling     INTEGER NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'drain',
    state        TEXT NOT NULL DEFAULT 'pending',
    pending_stage_ids TEXT NOT NULL DEFAULT '[]',
    detail       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activation_state ON node_activation_op(state);
CREATE INDEX IF NOT EXISTS idx_activation_node ON node_activation_op(workflow_id, node_id);

-- =====================================================================
-- L4 数据与事件层
-- =====================================================================

-- 内容寻址、落地即不可变；「修改」= 派生新版本（DATA-04）。
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id        TEXT PRIMARY KEY,
    digest             TEXT NOT NULL,
    producer_task_id   TEXT,
    producer_stage_id  TEXT,
    producer_attempt_seq INTEGER,
    kind               TEXT NOT NULL DEFAULT 'text',
    summary            TEXT,
    token_estimate     INTEGER,
    sensitivity        TEXT NOT NULL DEFAULT 'internal',
    lineage            TEXT NOT NULL DEFAULT '[]',
    ref_count          INTEGER NOT NULL DEFAULT 0,
    tombstoned         INTEGER NOT NULL DEFAULT 0,
    size_bytes         INTEGER,
    storage_path       TEXT,
    media_type         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_digest ON artifact(digest);
CREATE INDEX IF NOT EXISTS idx_artifact_producer ON artifact(producer_task_id, producer_stage_id);
CREATE INDEX IF NOT EXISTS idx_artifact_gc ON artifact(tombstoned, ref_count);

-- 统一信封；至少一次投递 + 消费端按 dedup_key 幂等（REC-05）。
CREATE TABLE IF NOT EXISTS message (
    message_id   TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    task_id      TEXT,
    from_stage   TEXT,
    to_stage     TEXT,
    dedup_key    TEXT,
    causation_id TEXT,
    generation   INTEGER NOT NULL DEFAULT 1,
    payload_ref  TEXT,
    payload      TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_dedup ON message(dedup_key)
    WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_inbox ON message(to_stage, state, created_at);

-- append-only：只有 INSERT 与（清理时）批量 DELETE。
CREATE TABLE IF NOT EXISTS event_log (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    scope_id   TEXT,
    type       TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT 'system',
    task_id    TEXT,
    stage_id   TEXT,
    payload    TEXT NOT NULL DEFAULT '{}',
    refs       TEXT NOT NULL DEFAULT '[]',
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_task ON event_log(task_id, event_id);
CREATE INDEX IF NOT EXISTS idx_event_scope ON event_log(scope, scope_id, event_id);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_log(type, event_id);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);

-- =====================================================================
-- L5 安全治理层
-- =====================================================================

CREATE TABLE IF NOT EXISTS approval (
    approval_id    TEXT PRIMARY KEY,
    task_id        TEXT,
    stage_id       TEXT,
    attempt_id     TEXT,
    revision_seq   INTEGER,
    node_id        TEXT,
    action         TEXT NOT NULL DEFAULT '',
    target         TEXT,
    risk           TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    timeout_policy TEXT NOT NULL DEFAULT 'deny_pause',
    decision       TEXT,
    detail         TEXT,
    expires_at     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_status ON approval(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_attempt ON approval(attempt_id);
CREATE INDEX IF NOT EXISTS idx_approval_task ON approval(task_id);

-- =====================================================================
-- 元
-- =====================================================================

CREATE TABLE IF NOT EXISTS meta_kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    ),
    (
        2,
        """
-- 产物的契约覆盖字段（D-06 摘要质量门禁 / RUN-06 完成判据的共用载体）。
-- 单独一条迁移而不是改 migration 1：已应用的迁移不可改写。
ALTER TABLE artifact ADD COLUMN covered_fields TEXT NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_artifact_covered ON artifact(producer_stage_id, tombstoned);
""",
    ),
    (
        3,
        """
-- 摘要质量门禁的结果必须持久化：不落库的话，一次「摘要未覆盖契约字段」的交接
-- 失败在回读时会退回默认值 True，下游据此放行——DATA-03 要求它显式报出。
ALTER TABLE artifact ADD COLUMN summary_ok INTEGER NOT NULL DEFAULT 1;
""",
    ),
    (
        4,
        """
-- 审批的两个字段此前只活在内存里，回读就丢：
--   tool_name          —— 用户要看到「是哪个工具在请求授权」
--   action_fingerprint —— AC-14 的「命令内容变化即旧批准作废」全靠它，
--                         不落库的话这条规则永远不生效（回读后指纹为 None，
--                         比较结果恒为「没变」）。
ALTER TABLE approval ADD COLUMN tool_name TEXT;
ALTER TABLE approval ADD COLUMN action_fingerprint TEXT;
""",
    ),
    (
        5,
        """
-- 产出者的 node_id / attempt_id 此前只在内存里，落库时被丢掉，回读后界面
-- 只能显示「未知节点」（DATA-02 的可辨认性在重启后失效）。
ALTER TABLE artifact ADD COLUMN producer_node_id TEXT;
ALTER TABLE artifact ADD COLUMN producer_attempt_id TEXT;
""",
    ),
]


def migration_sql(version: int) -> str:
    for v, sql in MIGRATIONS:
        if v == version:
            return sql
    raise KeyError(f"未定义的迁移版本: {version}")
