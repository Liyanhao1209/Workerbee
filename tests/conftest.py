"""共享测试夹具与构造器。

每个用例一个独立的临时数据库与产物目录——测试之间不共享状态，
因此可以放心并行与乱序执行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from workerbee.core.domain import (
    Approval,
    ApprovalBinding,
    ApprovalStatus,
    PinnedGraph,
    Task,
    TaskStage,
    WorkflowDefinition,
    WorkflowRevision,
    WorkflowStatus,
)
from workerbee.core.domain.workflow import GraphSpec, RevisionSource
from workerbee.core.resources.ledger import ResourceLedger
from workerbee.core.runtime.scheduler import Scheduler, SchedulerConfig
from workerbee.core.runtime.state import StateMachine
from workerbee.data.store import Store

from tests.fakes import FakeContextBuilder, FakeHarness, FakeSummarizer
from tests.helpers import edge, graph, node

# ===========================================================================
# 实体构造器（供仓储层与调度层测试共用）
# ===========================================================================


def make_graph() -> GraphSpec:
    """一个最小的 DAG：A → B。"""
    return graph({"A": ["B"], "B": []})


def make_task(**overrides: Any) -> Task:
    g = make_graph()
    pinned = PinnedGraph(
        graph=g,
        effective_edges=[("A", "B")],
        effective_graph_version=g.effective_graph_version(),
    )
    defaults: dict[str, Any] = {
        "task_id": "t1",
        "workflow_id": "w1",
        "revision_seq": 1,
        "effective_graph_version": pinned.effective_graph_version,
        "graph_snapshot": pinned,
        "input_payload": {"goal": "写一个测试"},
    }
    defaults.update(overrides)
    return Task(**defaults)


def make_stage(**overrides: Any) -> TaskStage:
    defaults: dict[str, Any] = {
        "stage_id": "s1",
        "task_id": "t1",
        "node_id": "A",
        "node_name": "A",
    }
    defaults.update(overrides)
    return TaskStage(**defaults)


def make_approval(**overrides: Any) -> Approval:
    """审批的绑定字段是扁平的，构造器把它们折进 ``bound_to``。"""
    bound = ApprovalBinding(
        task_id=overrides.pop("task_id", "t1"),
        stage_id=overrides.pop("stage_id", "s1"),
        attempt_id=overrides.pop("attempt_id", "at1"),
        revision_seq=overrides.pop("revision_seq", 1),
        node_id=overrides.pop("node_id", "A"),
    )
    defaults: dict[str, Any] = {
        "approval_id": "ap1",
        "bound_to": bound,
        "action": "Bash(rm -rf build/)",
        "target": "build/",
        "status": ApprovalStatus.PENDING,
    }
    defaults.update(overrides)
    return Approval(**defaults)


# ===========================================================================
# 夹具
# ===========================================================================


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = await Store.open(str(tmp_path / "workerbee.db"))
    s.artifacts.root = tmp_path / "artifacts"
    # 默认工作区必须在场：发射路径会校验 workflow 的归属（迁移 12 之后
    # workflow.workspace_id 一律指向某个工作区，缺行是显式失败而不是放行）。
    await s.workspaces.ensure_default(root_dir=str((tmp_path / "workspace").resolve()))
    yield s
    await s.close()


@pytest.fixture
async def db(tmp_path: Path) -> Any:
    """裸数据库连接，供只测事务与迁移的用例使用。"""
    from workerbee.data.db import Database

    d = Database(str(tmp_path / "raw.db"))
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
async def artifacts(tmp_path: Path) -> Any:
    """独立的产物存储，不依赖完整 Store。"""
    from workerbee.data.artifact_store import ArtifactStore
    from workerbee.data.db import Database

    d = Database(str(tmp_path / "artifact.db"))
    await d.connect()
    yield ArtifactStore(d, root=tmp_path / "artifacts")
    await d.close()


@pytest.fixture
def sm(store: Store) -> StateMachine:
    return StateMachine(store)


@pytest.fixture
def harness() -> FakeHarness:
    return FakeHarness()


async def _fake_harness_teardown(spec: dict) -> tuple[bool, str | None]:
    return True, None


@pytest.fixture
def ledger(store: Store, tmp_path: Path) -> ResourceLedger:
    return ResourceLedger(
        store,
        managed_roots=[tmp_path / "workspace"],
        harness_teardown=_fake_harness_teardown,
        default_grace_ms=200,
    )


@pytest.fixture
def context_builder() -> FakeContextBuilder:
    return FakeContextBuilder()


@pytest.fixture
def summarizer() -> FakeSummarizer:
    return FakeSummarizer()


@pytest.fixture
def scheduler(
    store: Store,
    sm: StateMachine,
    harness: FakeHarness,
    ledger: ResourceLedger,
    context_builder: FakeContextBuilder,
    summarizer: FakeSummarizer,
) -> Scheduler:
    return Scheduler(
        store=store,
        sm=sm,
        harness=harness,
        ledger=ledger,
        context_builder=context_builder,
        summarizer=summarizer,
        config=SchedulerConfig(poll_interval=60.0, max_dispatch_per_tick=4),
    )


@pytest.fixture
async def engine_factory(store: Store, tmp_path: Path):
    """在测试已有的 store 之上装配一个真 Engine（含真实适配层）。

    给需要走完整链路的集成／验收测试用——它们验证的是「产品声称解决的问题」，
    所以不能只测内核，必须有真的适配层与真的 harness。
    """
    from workerbee.app import Engine, EngineConfig

    engines: list[Any] = []

    async def _make(**overrides: Any) -> Any:
        cfg = EngineConfig(
            data_dir=tmp_path,
            workspace_dir=tmp_path / "workspace",
            # 摘要器要调 LLM，测试里默认关掉（交接失败会被显式标记，不会假装成功）。
            # 上下文组装器是纯逻辑、无外部依赖，**保持开启**——DATA-05 要求
            # 「交接了什么及其来源」可检查，关掉它就把这条能力测没了。
            use_summarizer=False,
            **overrides,
        )
        engine = await Engine.create(cfg, store=store)
        engines.append(engine)
        await engine.start(reconcile=False)
        return engine

    yield _make

    # 必须走 engine.stop()，不能只停 scheduler/reaper：
    # Engine.start() 还起了「审批超时」与「排水推进」两个循环，它们的退出条件是
    # 一个只有 stop() 才会清的标志。只停前两个的话，这两个循环会活到事件循环关闭，
    # 变成「Task was destroyed but it is pending」，极端情况下把测试挂住。
    # 用 close() 而不是 stop()：测试里的 store 由 store 夹具持有并关闭，
    # 这里再关一次会让夹具的清理报错。
    for engine in engines:
        try:
            await engine.stop(close_store=False)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def make_workflow(store: Store):
    """创建一个已发布的 Workflow，返回 ``(workflow, revision)``。"""

    async def _make(graph_spec: GraphSpec, *, name: str = "wf", max_concurrent: int = 8):
        wf = WorkflowDefinition(
            name=name,
            status=WorkflowStatus.DRAFT,
            max_concurrent_tasks=max_concurrent,
        )
        await store.workflows.create(wf)
        rev = WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=graph_spec,
            source=RevisionSource.MANUAL,
            is_published=True,
        )
        await store.workflows.save_revision(rev, publish=True, expected_revision_seq=0)
        wf.current_revision_seq = 1
        wf.status = WorkflowStatus.PUBLISHED
        return wf, rev

    return _make


async def drain(scheduler: Scheduler, *, rounds: int = 4) -> None:
    """跑若干轮调度。测试里用它替代真实的时间等待。"""
    for _ in range(rounds):
        await scheduler.tick()
        await asyncio.sleep(0)


@pytest.fixture
def tick():
    return drain
