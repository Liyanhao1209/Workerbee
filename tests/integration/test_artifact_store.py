"""内容寻址产物存储（DATA-01–04、RES-03、§7）。

重点钉住三条语义：不可变 + 内容去重、敏感级沿血缘取最高、引用未释放不得回收。
"""

from __future__ import annotations

import pytest

from workerbee.core.domain.artifact import ArtifactKind, ArtifactProducer, estimate_tokens
from workerbee.data.artifact_store import ArtifactNotFound

pytestmark = pytest.mark.integration


def _producer(seq: int = 1) -> ArtifactProducer:
    return ArtifactProducer(task_id="t1", stage_id="s1", attempt_seq=seq, node_id="A")


async def test_identical_content_dedups_physical_file(artifacts):
    """内容寻址：相同内容只有一份物理文件，但每条记录各自独立。"""
    a1 = await artifacts.put("同一个结果", producer=_producer(1))
    a2 = await artifacts.put("同一个结果", producer=_producer(2))

    assert a1.digest == a2.digest
    assert a1.artifact_id != a2.artifact_id
    assert a1.storage_path == a2.storage_path

    files = [p for p in artifacts.root.rglob("*") if p.is_file()]
    assert len(files) == 1, "相同内容不应落两份文件"

    assert await artifacts.read_text(a1.artifact_id) == "同一个结果"
    assert await artifacts.read_text(a2.artifact_id) == "同一个结果"


async def test_different_content_distinct_files(artifacts):
    a1 = await artifacts.put("A", producer=_producer(1))
    a2 = await artifacts.put("B", producer=_producer(1))
    assert a1.digest != a2.digest
    assert len([p for p in artifacts.root.rglob("*") if p.is_file()]) == 2


async def test_artifact_is_immutable_via_derive(artifacts):
    """DATA-04：「修改」= 派生新版本，父产物内容不变。"""
    parent = await artifacts.put("v1 内容", producer=_producer(1), summary="初版")
    child = await artifacts.derive(parent, "v2 内容", summary="二版")

    assert child.artifact_id != parent.artifact_id
    assert child.lineage == [parent.artifact_id]
    assert await artifacts.read_text(parent.artifact_id) == "v1 内容"
    assert await artifacts.read_text(child.artifact_id) == "v2 内容"


async def test_derive_inherits_kind_and_sensitivity(artifacts):
    parent = await artifacts.put(
        "s", producer=_producer(1), kind=ArtifactKind.CODE, sensitivity="sensitive"
    )
    child = await artifacts.derive(parent, "s2")
    assert child.kind == ArtifactKind.CODE
    assert child.sensitivity == "sensitive"


async def test_sensitivity_propagates_to_max_along_lineage(artifacts):
    """§9.4：敏感级沿血缘取最高级。"""
    public_parent = await artifacts.put("p", producer=_producer(1), sensitivity="public")
    sensitive_parent = await artifacts.put("q", producer=_producer(1), sensitivity="sensitive")

    merged = await artifacts.put(
        "merged",
        producer=_producer(2),
        lineage=[public_parent.artifact_id, sensitive_parent.artifact_id],
    )
    assert merged.sensitivity == "sensitive"

    derived = await artifacts.derive(merged, "child")
    assert derived.sensitivity == "sensitive"


async def test_sensitivity_defaults_to_internal(artifacts):
    art = await artifacts.put("x", producer=_producer(1))
    assert art.sensitivity == "internal"


async def test_explicit_sensitivity_wins(artifacts):
    art = await artifacts.put(
        "x", producer=_producer(1), sensitivity="public",
        lineage=[(await artifacts.put("p", producer=_producer(1),
                                      sensitivity="sensitive")).artifact_id],
    )
    assert art.sensitivity == "public"


async def test_producer_triple_is_recorded(artifacts):
    """DATA-02：下游可辨认结果来自哪个任务、上游阶段和执行尝试。"""
    art = await artifacts.put("r", producer=ArtifactProducer(
        task_id="t9", stage_id="s9", attempt_seq=3))
    got = await artifacts.get(art.artifact_id)
    assert got.producer.task_id == "t9"
    assert got.producer.stage_id == "s9"
    assert got.producer.attempt_seq == 3


async def test_resolve_pin_by_stage_and_attempt(artifacts):
    """D-06：下游钉扎上游「该任务内当前成功」的产物版本。"""
    p1 = ArtifactProducer(task_id="t1", stage_id="s1", attempt_seq=1)
    p2 = ArtifactProducer(task_id="t1", stage_id="s1", attempt_seq=2)
    await artifacts.put("失败尝试的产物", producer=p1)
    good = await artifacts.put("成功尝试的产物", producer=p2)

    pinned = await artifacts.resolve_pin("s1", attempt_seq=2)
    assert [a.artifact_id for a in pinned] == [good.artifact_id]

    all_versions = await artifacts.resolve_pin("s1")
    assert len(all_versions) == 2


async def test_missing_content_raises_not_returns_empty(artifacts):
    """DATA-03：引用失效属交接失败，必须报出，不允许以静默空内容换取「成功」。"""
    art = await artifacts.put("内容", producer=_producer(1))
    from pathlib import Path

    Path(art.storage_path).unlink()

    with pytest.raises(ArtifactNotFound):
        await artifacts.read_bytes(art.artifact_id)


async def test_read_unknown_artifact_raises(artifacts):
    with pytest.raises(ArtifactNotFound):
        await artifacts.read_bytes("nope")


async def test_tombstone_refuses_while_referenced(artifacts):
    """RES-03：仍被活跃任务引用的数据不能静默清除。"""
    art = await artifacts.put("x", producer=_producer(1))
    assert art.ref_count == 1

    assert await artifacts.tombstone(art.artifact_id) is False
    assert (await artifacts.get(art.artifact_id)).tombstoned is False

    await artifacts.release(art.artifact_id)
    assert await artifacts.tombstone(art.artifact_id) is True
    assert (await artifacts.get(art.artifact_id)).tombstoned is True


async def test_tombstoned_artifact_is_not_readable(artifacts):
    art = await artifacts.put("x", producer=_producer(1))
    await artifacts.mark_unreferenced(art.artifact_id)
    await artifacts.tombstone(art.artifact_id)

    with pytest.raises(ArtifactNotFound):
        await artifacts.read_bytes(art.artifact_id)


async def test_reference_counting_round_trip(artifacts):
    art = await artifacts.put("x", producer=_producer(1))
    await artifacts.add_reference(art.artifact_id, 2)
    assert (await artifacts.get(art.artifact_id)).ref_count == 3

    await artifacts.release(art.artifact_id, 10)
    assert (await artifacts.get(art.artifact_id)).ref_count == 0


async def test_gc_dry_run_keeps_files(artifacts):
    art = await artifacts.put("x", producer=_producer(1))
    await artifacts.mark_unreferenced(art.artifact_id)
    await artifacts.tombstone(art.artifact_id)

    report = await artifacts.gc(dry_run=True)
    assert report["removed_rows"] == 1
    from pathlib import Path

    assert Path(art.storage_path).exists(), "dry run 不得删除任何文件"
    assert await artifacts.get(art.artifact_id) is not None


async def test_gc_deletes_only_unreferenced(artifacts):
    """RES-03：物理回收只在显式调用时发生，且只动无引用、已标记的那些。"""
    doomed = await artifacts.put("要回收", producer=_producer(1))
    await artifacts.mark_unreferenced(doomed.artifact_id)
    await artifacts.tombstone(doomed.artifact_id)

    live = await artifacts.put("还要用", producer=_producer(2))

    report = await artifacts.gc(dry_run=False)
    assert report["removed_rows"] == 1
    assert report["removed_files"]

    assert await artifacts.get(doomed.artifact_id) is None
    assert await artifacts.get(live.artifact_id) is not None
    assert await artifacts.read_text(live.artifact_id) == "还要用"


async def test_gc_keeps_shared_blob_for_live_sibling(artifacts):
    """同一份内容被两条记录引用时，删掉一条不得让另一条的内容消失。"""
    a1 = await artifacts.put("共享内容", producer=_producer(1))
    a2 = await artifacts.put("共享内容", producer=_producer(2))

    await artifacts.mark_unreferenced(a1.artifact_id)
    await artifacts.tombstone(a1.artifact_id)

    report = await artifacts.gc(dry_run=False)
    assert report["removed_rows"] == 0
    assert report["skipped"], "应报告因同摘要仍有活跃引用而跳过"
    assert await artifacts.read_text(a2.artifact_id) == "共享内容"


async def test_storage_report(artifacts):
    await artifacts.put("hello", producer=_producer(1))
    report = await artifacts.storage_report()
    assert report["records"] == 1
    assert report["recorded_bytes"] == 5
    assert report["disk_bytes"] == 5


async def test_put_records_summary_and_tokens(artifacts):
    art = await artifacts.put(
        "一段中文内容", producer=_producer(1), summary="摘要", media_type="text/plain"
    )
    assert art.summary == "摘要"
    assert art.media_type == "text/plain"
    assert art.token_estimate == estimate_tokens("一段中文内容")
    assert art.size_bytes == len("一段中文内容".encode())


async def test_estimate_tokens_counts_cjk_denser_than_latin():
    assert estimate_tokens("中文内容") > estimate_tokens("abcd")
    assert estimate_tokens("") == 0
