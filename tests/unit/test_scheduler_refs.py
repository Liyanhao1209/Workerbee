"""节点 Skill / 工具引用在派发路径上的解析与注入（EXT-01/02、CFG-07）。

这里是历史上「配了不生效、全程不报错」那根线的测试：
调度器必须把节点的 skill_refs / tool_refs 解析成注册表实体，同时
注入上下文组装器（P4）与 create_session 的 extra；引用缺失或停用时
进 degraded，而不是静默跳过。

适配器侧（kimi 落 skills 目录、claude 渲染 --mcp-config）的用例在
tests/integration/test_adapters.py。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain import SkillDoc, ToolLaunch, ToolSpec, VersionedRef
from workerbee.core.domain.base import utcnow
from workerbee.core.domain.task import Attempt
from workerbee.core.runtime.scheduler import Scheduler, _session_extra
from workerbee.data.context_assembler import ContextAssembler

from tests.conftest import make_stage, make_task
from tests.fakes import FakeContextBuilder
from tests.helpers import node

pytestmark = pytest.mark.unit


def _attempt() -> Attempt:
    return Attempt(
        stage_id="s1", task_id="t1", node_id="A", attempt_seq=1,
        profile_id="p1", started_at=utcnow(),
    )


def _node(**overrides):
    base = node("A", system_prompt="你是节点 A")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


async def _seed_registry(store) -> None:
    await store.registry.upsert_skill(
        SkillDoc(skill_id="sk1", name="代码审查", content="先跑测试再下结论", version=3)
    )
    await store.registry.upsert_skill(
        SkillDoc(skill_id="sk2", name="已停用", content="不应出现", enabled=False)
    )
    await store.registry.upsert_tool(
        ToolSpec(
            tool_id="tl1",
            name="pgsql",
            description="查询只读库",
            launch=ToolLaunch(command="mcp-pgsql", args=["--ro"], env={"PGHOST": "db"}),
        )
    )


async def test_refs_resolved_and_passed_to_builder(store, sm, harness, ledger):
    """有组装器：解析出的 Skill / 工具必须真的到达 context_builder.build()。"""
    await _seed_registry(store)
    builder = FakeContextBuilder()
    scheduler = Scheduler(
        store=store, sm=sm, harness=harness, ledger=ledger, context_builder=builder
    )
    n = _node(
        skill_refs=[VersionedRef(ref_id="sk1")],
        tool_refs=[VersionedRef(ref_id="tl1")],
    )
    refs = await scheduler._resolve_node_refs(n)
    context = await scheduler._build_context(make_task(), make_stage(), _attempt(), n, refs=refs)

    call = builder.calls[0]
    assert [s.name for s in call["skills"]] == ["代码审查"]
    assert [t.name for t in call["tools"]] == ["pgsql"]
    assert context.degraded == []


async def test_missing_and_disabled_refs_go_to_degraded(store, sm, harness, ledger):
    """找不到 / 已停用的引用：跳过 + degraded 留痕，不影响其余引用注入。"""
    await _seed_registry(store)
    builder = FakeContextBuilder()
    scheduler = Scheduler(
        store=store, sm=sm, harness=harness, ledger=ledger, context_builder=builder
    )
    n = _node(
        skill_refs=[
            VersionedRef(ref_id="sk1"),
            VersionedRef(ref_id="sk2"),      # 已停用
            VersionedRef(ref_id="ghost"),    # 不存在
        ],
        tool_refs=[VersionedRef(ref_id="ghost-tool")],
    )
    refs = await scheduler._resolve_node_refs(n)
    context = await scheduler._build_context(make_task(), make_stage(), _attempt(), n, refs=refs)

    # 有效的引用照常注入
    assert [s.name for s in builder.calls[0]["skills"]] == ["代码审查"]
    # 三条无效引用各有说明，且在组装器返回的 degraded 之前
    assert any("ghost" in d and "不存在" in d for d in context.degraded)
    assert any("已停用" in d and "未注入" in d for d in context.degraded)
    assert any("ghost-tool" in d for d in context.degraded)
    assert len(context.degraded) == 3


async def test_pinned_version_note_when_only_latest_stored(store, sm, harness, ledger):
    """version 钉扎拿不到旧版本：照常注入最新版，并如实标注实际注入的版本。"""
    await _seed_registry(store)
    scheduler = Scheduler(
        store=store, sm=sm, harness=harness, ledger=ledger,
        context_builder=FakeContextBuilder(),
    )
    n = _node(skill_refs=[VersionedRef(ref_id="sk1", version=1)])
    refs = await scheduler._resolve_node_refs(n)
    skills, _, notes = refs
    assert skills[0].version == 3  # 存储只保留最新版
    assert any("v1" in note and "v3" in note for note in notes)


async def test_fallback_path_injects_refs_into_system_prompt(store, sm, harness, ledger):
    """无组装器：Skill 正文与工具说明追加进 system_prompt，语义一致。"""
    await _seed_registry(store)
    scheduler = Scheduler(store=store, sm=sm, harness=harness, ledger=ledger)
    n = _node(
        skill_refs=[VersionedRef(ref_id="sk1"), VersionedRef(ref_id="ghost")],
        tool_refs=[VersionedRef(ref_id="tl1")],
    )
    refs = await scheduler._resolve_node_refs(n)
    context = await scheduler._build_context(make_task(), make_stage(), _attempt(), n, refs=refs)

    assert "你是节点 A" in context.system_prompt
    assert "代码审查" in context.system_prompt
    assert "先跑测试再下结论" in context.system_prompt
    assert "pgsql" in context.system_prompt
    assert "查询只读库" in context.system_prompt
    assert any("ghost" in d for d in context.degraded)


async def test_real_assembler_renders_refs_into_p4(store, sm, harness, ledger):
    """接真组装器再走一遍：Skill / 工具要出现在 P4 的 system_prompt 里。

    FakeContextBuilder 只能证明「传过去了」；这一条证明组装器真的渲染了它。
    """
    await _seed_registry(store)
    scheduler = Scheduler(
        store=store, sm=sm, harness=harness, ledger=ledger,
        context_builder=ContextAssembler(),
    )
    n = _node(
        skill_refs=[VersionedRef(ref_id="sk1")],
        tool_refs=[VersionedRef(ref_id="tl1")],
    )
    refs = await scheduler._resolve_node_refs(n)
    context = await scheduler._build_context(make_task(), make_stage(), _attempt(), n, refs=refs)

    assert "代码审查" in context.system_prompt
    assert "pgsql" in context.system_prompt
    assert "查询只读库" in context.system_prompt


def test_session_extra_shape_and_none_when_no_refs():
    skills, tools = [], []
    assert _session_extra(skills, tools) is None

    skill = SkillDoc(skill_id="sk1", name="代码审查", content="正文", version=2)
    tool = ToolSpec(
        tool_id="tl1", name="pgsql",
        launch=ToolLaunch(command="mcp-pgsql", args=["--ro"], env={"PGHOST": "db"}),
    )
    extra = _session_extra([skill], [tool])
    assert extra is not None
    assert extra["skills"] == [{"name": "代码审查", "content": "正文", "version": 2}]
    assert extra["mcp_tools"] == [
        {
            "name": "pgsql",
            "transport": "stdio",
            "command": "mcp-pgsql",
            "args": ["--ro"],
            "env": {"PGHOST": "db"},
            "cwd": None,
            "url": None,
        }
    ]
    # 只有 Skill 没有工具时不出 mcp_tools 键
    only_skills = _session_extra([skill], [])
    assert only_skills is not None and "mcp_tools" not in only_skills
