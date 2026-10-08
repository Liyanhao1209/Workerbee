"""工作区（v0.03 §3、D-B）的集成测试：迁移、仓储、归档拦截、cwd 解析、删除边界。

覆盖的红线：
- 存量库升级：旧 workflow 自动归 ``default``，迁移幂等；
- 归档冻结**新发射**（``launch_task`` 显式拒绝），不硬删数据，在途不受影响；
- 派发时按 ``workflow.workspace_id → workspace.root_dir`` 解析 cwd，
  解析失败显式判阶段失败，不静默落到进程目录；
- 删除边界（``managed_roots``）覆盖全部未归档工作区的根。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workerbee.core.domain.task import StageState, TaskState
from workerbee.core.runtime.launch import WorkspaceArchived, launch_task
from workerbee.data.db import ConflictError
from workerbee.data.schema import MIGRATIONS, SCHEMA_VERSION
from workerbee.data.store import DEFAULT_WORKSPACE_ID, Store
from workerbee.serve import WORKSPACE_CURRENT_KEY, resolve_serve_workspace

from tests.helpers import graph

pytestmark = pytest.mark.integration


# ===========================================================================
# 迁移：v11 → v12
# ===========================================================================


def _build_v11_db(path: Path) -> None:
    """手工建一个 v11 库：应用前 11 条迁移，并写入一行旧格式的 workflow。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            "CREATE TABLE schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, sql in MIGRATIONS:
            if version > 11:
                break
            con.executescript(sql)
            con.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
        # v11 的 workflow 表没有 workspace_id 列——这正是迁移要补的东西
        con.execute(
            "INSERT INTO workflow(workflow_id, name, status, created_at, updated_at)"
            " VALUES ('w-legacy', '旧流程', 'published', datetime('now'), datetime('now'))"
        )
        con.commit()
    finally:
        con.close()


class TestMigration:
    async def test_v11库升级到v12_旧流程归默认工作区(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        _build_v11_db(db_path)

        store = await Store.open(str(db_path))
        try:
            version = await store.db.fetch_value(
                "SELECT MAX(version) FROM schema_version", default=0
            )
            assert int(version) == SCHEMA_VERSION == 12

            # 旧数据自动归 default（列默认值），不需要任何数据搬运
            wf = await store.workflows.get("w-legacy")
            assert wf is not None
            assert wf.workspace_id == DEFAULT_WORKSPACE_ID

            # default 行由仓储幂等补齐（root_dir 依赖运行时配置，不进 SQL 迁移）
            root = str((tmp_path / "workspace").resolve())
            row = await store.workspaces.ensure_default(root_dir=root)
            assert row["workspace_id"] == DEFAULT_WORKSPACE_ID
            # 幂等：再调一次还是同一行，不产生第二条
            again = await store.workspaces.ensure_default(root_dir=root)
            assert again["root_dir"] == root
            assert len(await store.workspaces.list(include_archived=True)) == 1
        finally:
            await store.close()

    async def test_迁移幂等_重复打开不报错(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "wb.db")
        store = await Store.open(db_path)
        await store.close()
        store = await Store.open(db_path)  # 第二次打开：全部迁移应跳过
        try:
            version = await store.db.fetch_value(
                "SELECT MAX(version) FROM schema_version", default=0
            )
            assert int(version) == 12
        finally:
            await store.close()

    async def test_默认工作区根目录被占用时显式失败(self, store: Store) -> None:
        """另一个工作区已注册同一根目录时，ensure_default 不能另指一个根。"""
        default = await store.workspaces.get(DEFAULT_WORKSPACE_ID)
        assert default is not None
        # 先删掉 default 行，再造一个占用它根目录的工作区
        await store.workspaces.delete(DEFAULT_WORKSPACE_ID)
        await store.workspaces.create(name="占用者", root_dir=default["root_dir"])
        with pytest.raises(ConflictError):
            await store.workspaces.ensure_default(root_dir=default["root_dir"])


# ===========================================================================
# 仓储 CRUD
# ===========================================================================


class TestWorkspaceRepository:
    async def test_创建与按根目录查询(self, store: Store, tmp_path: Path) -> None:
        root = str((tmp_path / "proj").resolve())
        row = await store.workspaces.create(name="项目", root_dir=root)
        assert row["archived"] is False

        assert (await store.workspaces.get(row["workspace_id"]))["name"] == "项目"
        assert (await store.workspaces.get_by_root(root))["workspace_id"] == row[
            "workspace_id"
        ]
        assert await store.workspaces.get_by_root("/no/such/dir") is None

    async def test_根目录全局唯一(self, store: Store, tmp_path: Path) -> None:
        root = str((tmp_path / "proj").resolve())
        await store.workspaces.create(name="甲", root_dir=root)
        with pytest.raises(ConflictError):
            await store.workspaces.create(name="乙", root_dir=root)

    async def test_更新白名单(self, store: Store, tmp_path: Path) -> None:
        row = await store.workspaces.create(
            name="旧名", root_dir=str((tmp_path / "p").resolve())
        )
        assert await store.workspaces.update(row["workspace_id"], name="新名")
        assert (await store.workspaces.get(row["workspace_id"]))["name"] == "新名"
        with pytest.raises(ValueError, match="不可更新"):
            await store.workspaces.update(row["workspace_id"], root_dir="/elsewhere")

    async def test_列表默认不含已归档(self, store: Store, tmp_path: Path) -> None:
        row = await store.workspaces.create(
            name="旧", root_dir=str((tmp_path / "old").resolve())
        )
        await store.workspaces.update(row["workspace_id"], archived=True)
        ids = {r["workspace_id"] for r in await store.workspaces.list()}
        assert row["workspace_id"] not in ids
        ids_all = {r["workspace_id"] for r in await store.workspaces.list(include_archived=True)}
        assert row["workspace_id"] in ids_all

    async def test_流程计数与改归属(self, store: Store, make_workflow) -> None:
        ws = await store.workspaces.create(name="目标", root_dir="/tmp/wb-test-move")
        wf, _ = await make_workflow(graph({"A": []}))
        assert await store.workspaces.count_workflows(DEFAULT_WORKSPACE_ID) == 1

        assert await store.workflows.move_to_workspace(wf.workflow_id, ws["workspace_id"])
        assert await store.workspaces.count_workflows(DEFAULT_WORKSPACE_ID) == 0
        assert await store.workspaces.count_workflows(ws["workspace_id"]) == 1
        moved = await store.workflows.get(wf.workflow_id)
        assert moved is not None and moved.workspace_id == ws["workspace_id"]

    async def test_活动根目录排除已归档(self, store: Store, tmp_path: Path) -> None:
        active = await store.workspaces.create(
            name="活", root_dir=str((tmp_path / "active").resolve())
        )
        archived = await store.workspaces.create(
            name="档", root_dir=str((tmp_path / "archived").resolve())
        )
        await store.workspaces.update(archived["workspace_id"], archived=True)
        roots = await store.workspaces.list_active_roots()
        assert active["root_dir"] in roots
        assert archived["root_dir"] not in roots


# ===========================================================================
# 归档拦截：冻结新发射，不删数据
# ===========================================================================


class TestArchivedWorkspace:
    async def test_归档工作区的流程不可发射(self, store: Store, make_workflow) -> None:
        await _register_harness(store)  # 取消归档后的那次发射要走完整校验
        ws = await store.workspaces.create(name="旧项目", root_dir="/tmp/wb-test-arch")
        wf, _ = await make_workflow(graph({"A": []}))
        await store.workflows.move_to_workspace(wf.workflow_id, ws["workspace_id"])

        await store.workspaces.update(ws["workspace_id"], archived=True)
        with pytest.raises(WorkspaceArchived, match="已归档"):
            await launch_task(store=store, workflow_id=wf.workflow_id)

        # 归档不删数据：流程与修订原样还在
        kept = await store.workflows.get(wf.workflow_id)
        assert kept is not None
        assert await store.workflows.get_current_revision(wf.workflow_id) is not None

        # 取消归档后恢复可发射
        await store.workspaces.update(ws["workspace_id"], archived=False)
        result = await launch_task(store=store, workflow_id=wf.workflow_id)
        assert result.created

    async def test_引用不存在工作区的流程显式失败(self, store: Store, make_workflow) -> None:
        wf, _ = await make_workflow(graph({"A": []}))
        await store.db.execute(
            "UPDATE workflow SET workspace_id='ghost' WHERE workflow_id=?",
            (wf.workflow_id,),
        )
        with pytest.raises(ValueError, match="工作区不存在"):
            await launch_task(store=store, workflow_id=wf.workflow_id)


# ===========================================================================
# 派发时的 cwd 解析
# ===========================================================================


async def _register_harness(store: Store, harness_id: str = "h1") -> None:
    from workerbee.core.domain.registry import HarnessRegistration

    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id=harness_id, name=harness_id, adapter_id="fake", last_probe_ok=True
        )
    )


class TestCwdResolution:
    async def test_按工作区根目录派发(
        self, store, harness, scheduler, make_workflow, tick, tmp_path: Path
    ) -> None:
        """调度时按 workflow → workspace.root_dir 解析节点工作目录。"""
        await _register_harness(store)
        ws_root = str((tmp_path / "proj").resolve())
        ws = await store.workspaces.create(name="项目", root_dir=ws_root)
        wf, _ = await make_workflow(graph({"A": []}))
        await store.workflows.move_to_workspace(wf.workflow_id, ws["workspace_id"])

        async def resolver(workflow_id: str) -> str | None:
            w = await store.workflows.get(workflow_id)
            assert w is not None
            row = await store.workspaces.get(w.workspace_id)
            return row["root_dir"] if row else None

        scheduler.cwd_resolver = resolver
        await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler, rounds=1)

        assert len(harness.created) == 1
        assert harness.created[0]["cwd"] == ws_root

    async def test_解析失败显式判阶段失败(
        self, store, harness, scheduler, make_workflow, tick
    ) -> None:
        """cwd 解析不出来必须失败并写明原因——不能悄悄在进程目录里跑。"""
        await _register_harness(store)
        wf, _ = await make_workflow(graph({"A": []}))

        async def broken(workflow_id: str) -> str:
            raise LookupError("Workflow 引用的工作区不存在: ghost")

        scheduler.cwd_resolver = broken
        result = await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler, rounds=2)

        stages = await store.tasks.list_stages(result.task.task_id)
        assert [s.observed_state for s in stages] == [StageState.FAILED]
        assert "工作目录解析失败" in (stages[0].blocked_reason or "")
        # 没有创建任何会话：失败发生在建会话之前
        assert harness.created == []

        task = await store.tasks.get_task(result.task.task_id)
        assert task is not None and task.observed_state == TaskState.FAILED

    async def test_未装解析器时沿用旧行为(
        self, store, harness, scheduler, make_workflow, tick
    ) -> None:
        """cwd_resolver 为 None（旧装配）时派发不受影响，cwd 为 None。"""
        await _register_harness(store)
        wf, _ = await make_workflow(graph({"A": []}))
        assert scheduler.cwd_resolver is None
        await launch_task(store=store, workflow_id=wf.workflow_id)
        await tick(scheduler, rounds=1)
        assert len(harness.created) == 1
        assert harness.created[0]["cwd"] is None


# ===========================================================================
# 删除边界：managed_roots 覆盖全部未归档工作区
# ===========================================================================


class TestManagedRoots:
    async def test_台账边界随工作区变化(self, engine_factory, store, tmp_path: Path) -> None:
        engine = await engine_factory()
        # 托管目录与默认工作区都在边界内
        assert (tmp_path / "workspace").resolve() in engine.ledger.managed_roots

        ws_root = tmp_path / "proj"
        ws = await store.workspaces.create(name="项目", root_dir=str(ws_root.resolve()))
        await engine.refresh_managed_roots()
        assert ws_root.resolve() in engine.ledger.managed_roots

        # 归档后移出删除边界——归档目录里的东西不再属于清理范围（RES-01）
        await store.workspaces.update(ws["workspace_id"], archived=True)
        await engine.refresh_managed_roots()
        assert ws_root.resolve() not in engine.ledger.managed_roots


# ===========================================================================
# serve 启动归位：匹配 / 自动注册 / 幂等
# ===========================================================================


class TestResolveServeWorkspace:
    async def test_全新数据目录注册新工作区(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        proj = tmp_path / "我的项目"
        proj.mkdir()

        info = await resolve_serve_workspace(data_dir, proj)
        assert info["created"] is True
        ws = info["workspace"]
        assert ws["root_dir"] == str(proj.resolve())
        assert ws["name"] == proj.name

        # meta_kv 记下了当前工作区，供 API 层的 current_workspace_id 使用
        store = await Store.open(str(data_dir / "workerbee.db"))
        try:
            current = await store.db.fetch_value(
                "SELECT v FROM meta_kv WHERE k=?", (WORKSPACE_CURRENT_KEY,)
            )
            assert current == ws["workspace_id"]
            # 默认工作区也已补齐
            assert await store.workspaces.get(DEFAULT_WORKSPACE_ID) is not None
        finally:
            await store.close()

    async def test_重复调用幂等匹配同一行(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()

        first = await resolve_serve_workspace(data_dir, proj)
        second = await resolve_serve_workspace(data_dir, proj)
        assert second["created"] is False
        assert second["workspace"]["workspace_id"] == first["workspace"]["workspace_id"]

    async def test_子目录最长前缀匹配(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        proj = tmp_path / "proj"
        (proj / "sub" / "deep").mkdir(parents=True)

        registered = await resolve_serve_workspace(data_dir, proj)
        # 再从它的子目录启动：命中同一个工作区，不再注册新的
        hit = await resolve_serve_workspace(data_dir, proj / "sub" / "deep")
        assert hit["created"] is False
        assert hit["workspace"]["workspace_id"] == registered["workspace"]["workspace_id"]

    async def test_相邻目录撞名时追加序号(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "a" / "proj").mkdir(parents=True)
        (tmp_path / "b" / "proj").mkdir(parents=True)

        first = await resolve_serve_workspace(data_dir, tmp_path / "a" / "proj")
        second = await resolve_serve_workspace(data_dir, tmp_path / "b" / "proj")
        assert first["workspace"]["name"] == "proj"
        assert second["workspace"]["name"] == "proj 2"
        assert second["created"] is True
