"""首次启动时的 harness 自动登记。

目的是开箱即用：用户装了 claude / kimi，首次启动就能直接建流程，不必先去
注册表填表。但**只在注册表为空时动手**——里面只要已经有任何东西就完全不动，
因为用户可能是有意只登记一部分，凭空补记录会打乱他的配置，而且事后要删还得
先弄清哪条是自己建的。

这里也钉住 claude 那个开关：不带 INPUT_FORMAT=stream-json 的话，注册会成功、
探测也会成功，但审批转发与运行中交互**静默不可用**——只有真去点审批时才发现，
是最难查的一类。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.registry import HarnessRegistration

pytestmark = pytest.mark.integration


def _fake_resolver(monkeypatch, mapping: dict[str, str]) -> None:
    """把解析换成受控结果，测试不依赖本机装了什么。"""
    monkeypatch.setattr(
        "workerbee.app.resolve_executable",
        lambda _path, name: mapping.get(name),
    )


async def test_first_start_registers_what_it_finds(engine_factory, store, monkeypatch):
    _fake_resolver(monkeypatch, {"claude": "/opt/x/claude", "kimi": "/opt/x/kimi"})

    engine = await engine_factory()

    regs = {r.harness_id: r for r in await store.registry.list_harnesses()}
    assert set(regs) == {"claude", "kimi"}
    assert regs["claude"].adapter_id == "claude_code"
    assert regs["kimi"].adapter_id == "kimi_code"
    # 存的是解析后的绝对路径，不是留空靠每次再解析——路径要能看见、能改
    assert regs["claude"].exec_path == "/opt/x/claude"
    assert regs["kimi"].exec_path == "/opt/x/kimi"
    assert all(r.enabled for r in regs.values())
    assert any("自动登记" in n for n in engine.startup_notes), "要留痕，不能悄悄建记录"


async def test_claude_gets_the_stream_json_switch(engine_factory, store, monkeypatch):
    """漏掉这个开关的代价是「注册好了但审批和 attach 用不了」。"""
    _fake_resolver(monkeypatch, {"claude": "/opt/x/claude", "kimi": "/opt/x/kimi"})

    await engine_factory()

    regs = {r.harness_id: r for r in await store.registry.list_harnesses()}
    assert regs["claude"].env_template.get("WORKERBEE_CLAUDE_CODE_INPUT_FORMAT") == "stream-json"
    assert regs["kimi"].env_template == {}, "kimi 没有这个开关，不该平白塞东西"


async def test_only_the_harnesses_that_resolve_are_registered(engine_factory, store, monkeypatch):
    _fake_resolver(monkeypatch, {"kimi": "/opt/x/kimi"})

    await engine_factory()

    regs = {r.harness_id for r in await store.registry.list_harnesses()}
    assert regs == {"kimi"}, "只找得到 kimi 时就只登记 kimi"


async def test_nothing_resolvable_registers_nothing(engine_factory, store, monkeypatch):
    """CI 上就是这个情形：两个 harness 都没装。不该建空记录，也不该报错。"""
    _fake_resolver(monkeypatch, {})

    engine = await engine_factory()

    assert await store.registry.list_harnesses() == []
    assert not any("自动登记" in n for n in engine.startup_notes)


async def test_an_existing_registry_is_left_alone(engine_factory, store, monkeypatch):
    """注册表非空就完全不碰——哪怕本机还装着别的 harness。"""
    _fake_resolver(monkeypatch, {"claude": "/opt/x/claude", "kimi": "/opt/x/kimi"})
    await store.registry.upsert_harness(
        HarnessRegistration(
            harness_id="my-own",
            name="我自己配的",
            adapter_id="claude_code",
            exec_path="/custom/claude",
        )
    )

    engine = await engine_factory()

    regs = {r.harness_id for r in await store.registry.list_harnesses()}
    assert regs == {"my-own"}, "不该往已有配置里补记录"
    assert not any("自动登记" in n for n in engine.startup_notes)


async def test_a_partial_registry_is_also_left_alone(engine_factory, store, monkeypatch):
    """只登记了 claude、故意没登记 kimi 也是合法的意图，不该被「补齐」。"""
    _fake_resolver(monkeypatch, {"claude": "/opt/x/claude", "kimi": "/opt/x/kimi"})
    await store.registry.upsert_harness(
        HarnessRegistration(harness_id="claude", name="Claude Code", adapter_id="claude_code")
    )

    await engine_factory()

    regs = {r.harness_id for r in await store.registry.list_harnesses()}
    assert regs == {"claude"}
