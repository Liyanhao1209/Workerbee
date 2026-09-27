"""共享测试夹具。

每个用例一个独立的临时数据库与产物目录——测试之间不共享状态，
因此可以放心并行与乱序执行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from workerbee.core.domain import (
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

__all__ = []


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = await Store.open(str(tmp_path / "workerbee.db"))
    s.artifacts.root = tmp_path / "artifacts"
    yield s
    await s.close()


@pytest.fixture
def sm(store: Store) -> StateMachine:
    return StateMachine(store)


@pytest.fixture
def harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
def ledger(store: Store, tmp_path: Path) -> ResourceLedger:
    return ResourceLedger(
        store,
        managed_roots=[tmp_path / "workspace"],
        harness_teardown=_fake_harness_teardown,
        default_grace_ms=200,
    )


async def _fake_harness_teardown(spec: dict) -> tuple[bool, str | None]:
    return True, None


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
def make_workflow(store: Store):
    """创建一个已发布的 Workflow，返回 (workflow, revision)。"""

    async def _make(graph: GraphSpec, *, name: str = "wf", max_concurrent: int = 8):
        wf = WorkflowDefinition(
            name=name,
            status=WorkflowStatus.DRAFT,
            max_concurrent_tasks=max_concurrent,
        )
        await store.workflows.create(wf)
        rev = WorkflowRevision(
            workflow_id=wf.workflow_id,
            revision_seq=1,
            graph=graph,
            source=RevisionSource.MANUAL,
            is_published=True,
        )
        await store.workflows.save_revision(
            rev, publish=True, expected_revision_seq=0
        )
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
