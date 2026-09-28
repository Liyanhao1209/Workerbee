"""内容寻址产物存储（架构设计 v0.02 §5.4、§7、RES-03）。

磁盘布局：``<root>/<digest[:2]>/<digest>``。相同内容只落一份物理文件；
``artifact`` 表按生产者分别记行，引用计数与 tombstone 按行维护。

回收口径（RES-03）：**历史数据本身不强制自动 TTL**。物理回收只在显式调用
``gc`` 时发生，且必须同时满足「已 tombstone」「引用计数为 0」「无活跃或可恢复
任务引用」。无法确认归属时报出而不是隐藏。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Sequence

from ..core.domain.artifact import (
    Artifact,
    ArtifactKind,
    ArtifactProducer,
    estimate_tokens,
    max_sensitivity,
)
from ..core.domain.base import utcnow
from .db import Database, dumps, loads

__all__ = ["ArtifactStore", "ArtifactNotFound"]


class ArtifactNotFound(KeyError):
    pass


class ArtifactStore:
    """产物的读写、派生与回收。"""

    def __init__(self, db: Database, root: str | Path | None = None) -> None:
        self.db = db
        self.root = Path(root) if root is not None else Path(".workerbee/artifacts")

    # ---- 写入 ----

    async def put(
        self,
        content: bytes | str,
        *,
        kind: ArtifactKind = ArtifactKind.TEXT,
        producer: ArtifactProducer | None = None,
        sensitivity: str | None = None,
        media_type: str | None = None,
        summary: str | None = None,
        summary_ok: bool = True,
        covered_fields: Sequence[str] = (),
        lineage: Sequence[str] = (),
    ) -> Artifact:
        """落地一份新产物。内容已存在时复用物理文件，仍生成独立记录。"""
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()

        inherited = await self._inherit_sensitivity(lineage)
        effective = sensitivity or inherited or "internal"

        # 内容寻址：同摘要只落一份文件。先写临时文件再原子重命名，
        # 避免读到写了一半的内容。
        path = self._path_for(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{digest}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        text_for_estimate = (
            data.decode("utf-8", errors="replace") if kind != ArtifactKind.FILE else ""
        )

        art = Artifact(
            digest=digest,
            producer=producer,
            kind=kind,
            summary=summary,
            summary_ok=summary_ok,
            covered_fields=list(covered_fields),
            token_estimate=estimate_tokens(text_for_estimate) if text_for_estimate else None,
            size_bytes=len(data),
            media_type=media_type,
            sensitivity=effective,  # type: ignore[arg-type]
            lineage=list(lineage),
            storage_path=str(path),
            ref_count=1,
        )
        await self._insert(art)
        return art

    async def derive(
        self,
        parent: Artifact,
        content: bytes | str,
        *,
        kind: ArtifactKind | None = None,
        summary: str | None = None,
        producer: ArtifactProducer | None = None,
    ) -> Artifact:
        """「修改」= 派生新版本（DATA-04）。

        不覆盖父产物：已完成消费者持有的输入必须保持可追溯。
        """
        return await self.put(
            content,
            kind=kind or parent.kind,
            producer=producer or parent.producer,
            sensitivity=parent.sensitivity,
            media_type=parent.media_type,
            summary=summary,
            lineage=[parent.artifact_id],
        )

    async def _insert(self, art: Artifact) -> None:
        await self.db.execute(
            """INSERT INTO artifact(artifact_id, digest, producer_task_id, producer_stage_id,
                   producer_attempt_seq, kind, summary, summary_ok, covered_fields,
                   token_estimate, sensitivity, lineage, ref_count, tombstoned, size_bytes,
                   storage_path, media_type, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                art.artifact_id,
                art.digest,
                art.producer.task_id if art.producer else None,
                art.producer.stage_id if art.producer else None,
                art.producer.attempt_seq if art.producer else None,
                art.kind.value,
                art.summary,
                1 if art.summary_ok else 0,
                dumps(art.covered_fields),
                art.token_estimate,
                art.sensitivity,
                dumps(art.lineage),
                art.ref_count,
                1 if art.tombstoned else 0,
                art.size_bytes,
                art.storage_path,
                art.media_type,
                art.created_at.isoformat(),
                art.updated_at.isoformat(),
            ),
        )

    # ---- 读取 ----

    async def get(self, artifact_id: str) -> Artifact | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact WHERE artifact_id=?", (artifact_id,)
        )
        return self._to_artifact(row) if row else None

    async def require(self, artifact_id: str) -> Artifact:
        art = await self.get(artifact_id)
        if art is None:
            raise ArtifactNotFound(f"产物不存在: {artifact_id}")
        return art

    async def read_bytes(self, artifact_id: str) -> bytes:
        art = await self.require(artifact_id)
        if art.tombstoned:
            raise ArtifactNotFound(f"产物已被逻辑删除: {artifact_id}")
        path = Path(art.storage_path or self._path_for(art.digest))
        if not path.exists():
            # 引用失效属交接失败，必须报出而不是返回空内容（DATA-03）
            raise ArtifactNotFound(f"产物内容缺失（引用失效）: {artifact_id}")
        return path.read_bytes()

    async def read_text(self, artifact_id: str) -> str:
        return (await self.read_bytes(artifact_id)).decode("utf-8", errors="replace")

    async def list_by_task(self, task_id: str, *, include_tombstoned: bool = False) -> list[Artifact]:
        sql = "SELECT * FROM artifact WHERE producer_task_id=?"
        if not include_tombstoned:
            sql += " AND tombstoned=0"
        sql += " ORDER BY created_at"
        rows = await self.db.fetch_all(sql, (task_id,))
        return [self._to_artifact(r) for r in rows]

    async def list_by_stage(self, stage_id: str) -> list[Artifact]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifact WHERE producer_stage_id=? ORDER BY created_at",
            (stage_id,),
        )
        return [self._to_artifact(r) for r in rows]

    async def resolve_pin(self, stage_id: str, attempt_seq: int | None = None) -> list[Artifact]:
        """按钉扎条件取产物：某阶段（可选：某次尝试）成功产出的那批。

        D-06：下游在就绪判定时钉扎上游「该任务内当前成功」的产物版本，之后不变。
        """
        if attempt_seq is None:
            rows = await self.db.fetch_all(
                """SELECT * FROM artifact WHERE producer_stage_id=? AND tombstoned=0
                   ORDER BY created_at""",
                (stage_id,),
            )
        else:
            rows = await self.db.fetch_all(
                """SELECT * FROM artifact WHERE producer_stage_id=? AND producer_attempt_seq=?
                   AND tombstoned=0 ORDER BY created_at""",
                (stage_id, attempt_seq),
            )
        return [self._to_artifact(r) for r in rows]

    # ---- 引用计数 ----

    async def add_reference(self, artifact_id: str, n: int = 1) -> None:
        await self.db.execute(
            "UPDATE artifact SET ref_count=ref_count+?, updated_at=? WHERE artifact_id=?",
            (n, utcnow().isoformat(), artifact_id),
        )

    async def release(self, artifact_id: str, n: int = 1) -> None:
        await self.db.execute(
            """UPDATE artifact SET ref_count=MAX(0, ref_count-?), updated_at=?
               WHERE artifact_id=?""",
            (n, utcnow().isoformat(), artifact_id),
        )

    async def tombstone(self, artifact_id: str, *, force: bool = False) -> bool:
        """逻辑删除。默认拒绝删除仍被引用的产物（RES-03 的「不能静默清除」）。"""
        art = await self.get(artifact_id)
        if art is None:
            return False
        if art.ref_count > 0 and not force:
            return False
        return (
            await self.db.execute_rowcount(
                "UPDATE artifact SET tombstoned=1, updated_at=? WHERE artifact_id=?",
                (utcnow().isoformat(), artifact_id),
            )
            > 0
        )

    async def mark_unreferenced(self, artifact_id: str) -> None:
        await self.db.execute(
            "UPDATE artifact SET ref_count=0, updated_at=? WHERE artifact_id=?",
            (utcnow().isoformat(), artifact_id),
        )

    # ---- 回收（RES-03） ----

    async def gc(self, *, dry_run: bool = True, include_tombstoned: bool = False) -> dict:
        """回收不再需要的物理内容。

        只回收「引用计数为 0 且（已 tombstone 或显式要求）」的产物，
        且物理文件仅在其摘要不再被任何未回收行引用时删除。
        """
        condition = "ref_count=0"
        if not include_tombstoned:
            condition += " AND tombstoned=1"
        rows = await self.db.fetch_all(f"SELECT * FROM artifact WHERE {condition}")

        candidates = [self._to_artifact(r) for r in rows]
        removed_rows: list[str] = []
        removed_files: list[str] = []
        skipped: list[dict] = []

        for art in candidates:
            still_used = await self.db.fetch_value(
                "SELECT COUNT(*) FROM artifact WHERE digest=? AND ref_count>0 AND tombstoned=0",
                (art.digest,),
                default=0,
            )
            if int(still_used) > 0:
                skipped.append({"artifact_id": art.artifact_id, "reason": "同摘要仍有活跃引用"})
                continue

            path = Path(art.storage_path or self._path_for(art.digest))
            removed_rows.append(art.artifact_id)
            if path.exists():
                removed_files.append(str(path))
                if not dry_run:
                    path.unlink(missing_ok=True)
            if not dry_run:
                await self.db.execute(
                    "DELETE FROM artifact WHERE artifact_id=?", (art.artifact_id,)
                )

        return {
            "dry_run": dry_run,
            "candidate_rows": len(candidates),
            "removed_rows": len(removed_rows),
            "removed_files": removed_files,
            "skipped": skipped,
        }

    async def storage_report(self) -> dict:
        rows = await self.db.fetch_all(
            """SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes,
                      SUM(CASE WHEN tombstoned=1 THEN 1 ELSE 0 END) AS tombstoned,
                      SUM(CASE WHEN ref_count=0 THEN 1 ELSE 0 END) AS unreferenced
               FROM artifact"""
        )
        r = rows[0]
        disk = 0
        if self.root.exists():
            for p in self.root.rglob("*"):
                if p.is_file():
                    disk += p.stat().st_size
        return {
            "root": str(self.root),
            "records": r["n"] or 0,
            "recorded_bytes": r["bytes"] or 0,
            "tombstoned": r["tombstoned"] or 0,
            "unreferenced": r["unreferenced"] or 0,
            "disk_bytes": disk,
        }

    # ---- helpers ----

    def _path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    async def _inherit_sensitivity(self, lineage: Iterable[str]) -> str | None:
        """敏感级沿血缘取最高级（§9.4）。无父产物时返回 None，由调用方取默认值。"""
        values = []
        for parent_id in lineage:
            parent = await self.get(parent_id)
            if parent is not None:
                values.append(parent.sensitivity)
        if not values:
            return None
        return max_sensitivity(values)

    @staticmethod
    def _to_artifact(row) -> Artifact:
        producer = None
        if row["producer_task_id"]:
            producer = ArtifactProducer(
                task_id=row["producer_task_id"],
                stage_id=row["producer_stage_id"],
                attempt_seq=row["producer_attempt_seq"] or 0,
                attempt_id=None,
            )
        return Artifact(
            artifact_id=row["artifact_id"],
            digest=row["digest"],
            producer=producer,
            kind=ArtifactKind(row["kind"]),
            summary=row["summary"],
            summary_ok=bool(row["summary_ok"]),
            covered_fields=loads(row["covered_fields"], []),
            token_estimate=row["token_estimate"],
            sensitivity=row["sensitivity"],
            lineage=loads(row["lineage"], []),
            ref_count=row["ref_count"],
            tombstoned=bool(row["tombstoned"]),
            size_bytes=row["size_bytes"],
            storage_path=row["storage_path"],
            media_type=row["media_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
