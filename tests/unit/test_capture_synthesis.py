"""捕获草案合成的单测：提案解析扩展字段、observed 复核降级、合成失败可重试。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import make_task
from tests.fakes import FakeLLMBackend

from workerbee.assistant.draft import DraftProposal
from workerbee.assistant.service import AssistantConfig, save_config
from workerbee.capture import CaptureError, CaptureNotConfigured, CaptureService
from workerbee.capture.synthesis import build_synthesis_prompt, review_basis
from workerbee.core.domain.registry import CredentialKind, CredentialRef
from workerbee.core.domain.task import TaskState
from workerbee.data.event_log import EventScope, EventType

pytestmark = pytest.mark.unit


# ===========================================================================
# 提案协议的扩展字段
# ===========================================================================


def test_proposal_parses_basis_and_evidence():
    raw = {
        "kind": "workflow",
        "name": "x",
        "nodes": [
            {"node_id": "a", "name": "甲", "basis": "observed", "evidence": ["E1", "A2"]},
            {"node_id": "b", "name": "乙", "basis": "inferred"},
        ],
        "edges": [
            {"from_node": "a", "to_node": "b", "basis": "observed", "evidence": ["E3"]}
        ],
    }
    proposal = DraftProposal.model_validate(raw)
    assert proposal.nodes[0].basis == "observed"
    assert proposal.nodes[0].evidence == ["E1", "A2"]
    assert proposal.nodes[1].basis == "inferred"
    assert proposal.edges[0].basis == "observed"


def test_proposal_without_basis_still_parses():
    """助手提案不带这两个字段：扩展必须向后兼容。"""
    proposal = DraftProposal.model_validate(
        {"kind": "workflow", "name": "x", "nodes": [{"name": "甲"}], "edges": []}
    )
    assert proposal.nodes[0].basis is None
    assert proposal.nodes[0].evidence == []


# ===========================================================================
# observed 复核：模型不能给自己贴金
# ===========================================================================


def _proposal() -> DraftProposal:
    return DraftProposal.model_validate(
        {
            "kind": "workflow",
            "name": "x",
            "nodes": [
                {"node_id": "a", "name": "甲", "basis": "observed", "evidence": ["E99"]},
                {"node_id": "b", "name": "乙", "basis": "observed", "evidence": ["E1"]},
                {"node_id": "c", "name": "丙", "basis": "inferred"},
            ],
            "edges": [
                {"from_node": "a", "to_node": "b", "basis": "observed", "evidence": []}
            ],
        }
    )


def test_observed_without_real_evidence_is_downgraded():
    proposal = _proposal()
    downgrades = review_basis(proposal, {"E1", "1"})

    # E99 不在材料里 → 强制降级，且在草案说明里写明
    assert proposal.nodes[0].basis == "inferred"
    assert proposal.nodes[0].evidence == []
    # E1 在材料里 → 保持 observed
    assert proposal.nodes[1].basis == "observed"
    # 没标 observed 的不动
    assert proposal.nodes[2].basis == "inferred"
    # observed 但 evidence 为空 → 同样降级
    assert proposal.edges[0].basis == "inferred"

    assert len(downgrades) == 2
    note_text = "\n".join(proposal.notes)
    assert "甲" in note_text and "推断" in note_text


def test_fabricated_evidence_mixed_with_real_keeps_only_real():
    proposal = DraftProposal.model_validate(
        {
            "kind": "workflow",
            "name": "x",
            "nodes": [
                {"node_id": "a", "name": "甲", "basis": "observed",
                 "evidence": ["E1", "E99", "编造"]}
            ],
            "edges": [],
        }
    )
    downgrades = review_basis(proposal, {"E1", "1"})
    assert downgrades == [], "有至少一条真实佐证时不降级"
    assert proposal.nodes[0].basis == "observed"
    assert proposal.nodes[0].evidence == ["E1"], "编造的引用被清掉，只留下真的"


def test_synthesis_prompt_carries_material_and_base_profile():
    from workerbee.capture.material import CaptureMaterial

    material = CaptureMaterial(task_id="t1", text="材料正文内容", total_chars=6)
    prompt = build_synthesis_prompt(
        material,
        base_profile={"harness_ref": "h1", "model_name": "m1", "credential_ref": None},
        harness_ids=["h1"],
        credential_ids=[],
        skill_ids=[],
        tool_ids=[],
    )
    assert "材料正文内容" in prompt
    assert "h1" in prompt
    assert "observed" in prompt and "inferred" in prompt


# ===========================================================================
# 草案合成（端到端核心路径，不经 HTTP）
# ===========================================================================


class _Hooks:
    async def create_workflow(self, *, name, description):  # pragma: no cover
        raise NotImplementedError

    async def save_revision(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def submit(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


async def _finished_run(store, *, instructions: str = "整理日志") -> dict:
    """造一个已跑完的捕获 run：run 台账 + 成功任务 + 输入/工具事件。"""
    task = make_task(task_id="t1")
    await store.tasks.create_task_with_stages(task, [])
    await store.tasks.update_task("t1", to_state=TaskState.SUCCEEDED)
    await store.events.append(
        scope=EventScope.ATTEMPT, type=EventType.ATTEMPT_INPUT,
        scope_id="at1", task_id="t1", payload={"user_input": instructions},
    )
    await store.events.append(
        scope=EventScope.ATTEMPT, type=EventType.ATTEMPT_TOOL_USE,
        scope_id="at1", task_id="t1",
        payload={"tool_name": "Bash", "tool_use_id": "tu-1", "target": "ls /var/log"},
    )
    return await store.capture.create_run(
        run_id="r1", name="捕获A", workflow_id="w1", task_id="t1",
        profile={"harness_ref": "h1", "model_name": "m1", "credential_ref": None},
    )


async def _configure_llm(store, backend: FakeLLMBackend) -> CaptureService:
    """登记合成凭据 + 启用助手配置 + 注入假后端。"""
    await store.registry.upsert_credential(
        CredentialRef(
            credential_id="cred-1", label="合成", kind=CredentialKind.BASE_URL_PAIR,
            secret_locator="secret://llm", base_url="https://llm.example.com/v1",
            default_model="gpt-test",
        )
    )
    await save_config(
        store.db, AssistantConfig(enabled=True, credential_ref="cred-1")
    )
    return CaptureService(
        store=store,
        hooks=_Hooks(),
        secret_resolver=lambda: object(),  # 凭据库已解锁（内容无所谓，假后端不读）
        backend_factory=lambda cfg, cred, secrets: backend,
    )


def _draft_reply(**overrides) -> str:
    payload = {
        "kind": "workflow",
        "name": "捕获草案",
        "nodes": [
            {"node_id": "a", "name": "扫描", "basis": "observed", "evidence": ["E2"]},
            {"node_id": "b", "name": "汇总", "basis": "observed", "evidence": ["E999"]},
        ],
        "edges": [{"from_node": "a", "to_node": "b", "basis": "inferred"}],
    }
    payload.update(overrides)
    return "说明。\n\n```workerbee-draft\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"


async def test_generate_draft_happy_path_with_downgrade(store):
    await _finished_run(store)
    backend = FakeLLMBackend(_draft_reply())
    svc = await _configure_llm(store, backend)

    draft = await svc.generate_draft("r1")

    assert draft["status"] == "pending"
    # 模型真的收到了材料正文（只断言状态码不算数）
    assert backend.calls, "合成必须真的调用模型"
    user_message = backend.calls[0][-1].content
    assert "整理日志" in user_message
    assert "Bash" in user_message
    # E999 不在材料里 → 复核降级，记录进草案 validation 与说明
    nodes = {n["node_id"]: n for n in draft["payload"]["nodes"]}
    assert nodes["a"]["basis"] == "observed"
    assert nodes["b"]["basis"] == "inferred"
    assert draft["validation"]["downgrades"], "降级必须留痕"
    assert any("汇总" in d["target"] for d in draft["validation"]["downgrades"])
    # 校验走了 draft 档管线
    assert draft["validation"]["diagnostics"] is not None
    # 事件留痕：带材料裁剪与降级事实
    events = await store.events.tail()
    generated = [e for e in events if e["type"] == EventType.CAPTURE_DRAFT_GENERATED.value]
    assert len(generated) == 1
    assert generated[0]["payload"]["downgrade_count"] == 1
    assert generated[0]["payload"]["backend"] == "fake-llm"


async def test_generate_draft_requires_finished_task(store):
    task = make_task(task_id="t1")
    await store.tasks.create_task_with_stages(task, [])  # 还在 queued
    await store.capture.create_run(
        run_id="r1", name="x", workflow_id="w1", task_id="t1", profile={}
    )
    svc = await _configure_llm(store, FakeLLMBackend("x"))
    with pytest.raises(CaptureError, match="还在跑"):
        await svc.generate_draft("r1")


async def test_generate_draft_requires_llm_config(store):
    await _finished_run(store)
    svc = CaptureService(store=store, hooks=_Hooks())  # 没配置助手
    with pytest.raises(CaptureNotConfigured):
        await svc.generate_draft("r1")


async def test_broken_model_output_is_honest_error_and_retryable(store):
    """坏输出如实报错、不落草案、可重试；已跑完的任务不受影响。"""
    await _finished_run(store)
    backend = FakeLLMBackend("这次模型没有输出提案块")
    svc = await _configure_llm(store, backend)

    with pytest.raises(CaptureError, match="没有按约定输出"):
        await svc.generate_draft("r1")
    assert await store.capture.list_drafts_for_run("r1") == []
    task = await store.tasks.get_task("t1")
    assert task.observed_state == TaskState.SUCCEEDED, "合成失败不影响已跑完的任务"

    # 重试：换一份正常输出，成功落库
    backend._responses = [_draft_reply()]
    draft = await svc.generate_draft("r1")
    assert draft["status"] == "pending"
