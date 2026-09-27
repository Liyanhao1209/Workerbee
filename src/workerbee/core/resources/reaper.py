"""周期清理器 Reaper（架构设计 v0.02 §10.5、RES-02/03）。

三项职责，周期运行 + 服务启动时运行：

(a) 台账与实际进程表对账，回收孤儿进程、端口、连接；
(b) 处理 ``teardown_failed`` 句柄的重试与告警；
(c) 超龄 tombstone 产物在「无活跃任务或可恢复任务引用」检查后物理回收。

**历史数据本身不强制自动 TTL**（清单 §4 的澄清：历史≠泄漏）。因此 (c) 只在
显式启用且对象已被 tombstone 时才动手，并提供存储占用查看与手动清理入口。

**只清理能确认归属的对象；无法确认归属时报告而非隐藏。**
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from pydantic import Field

from ..domain.base import DomainModel, utcnow
from ...data.event_log import EventActor, EventScope, EventType

__all__ = ["ReaperConfig", "ReaperReport", "Reaper"]


class ReaperConfig(DomainModel):
    interval_seconds: float = 300.0
    """周期。默认 5 分钟——清理不是热路径，过于频繁只会浪费。"""

    retry_teardown_limit: int = 3
    artifact_gc_enabled: bool = False
    """默认关闭。物理回收会永久丢失内容，必须是用户显式开启的动作。"""

    artifact_gc_min_age_hours: float = 24.0
    run_on_start: bool = True


class ReaperReport(DomainModel):
    started_at: str = ""
    orphans_found: int = 0
    orphans_closed: int = 0
    teardown_retried: int = 0
    teardown_recovered: int = 0
    teardown_still_failing: int = 0
    artifacts_removed: int = 0
    bytes_reclaimed: int = 0
    still_unresolved: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Reaper:
    def __init__(
        self,
        *,
        store: Any,
        ledger: Any,
        config: ReaperConfig | None = None,
        notifier: Any | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.config = config or ReaperConfig()
        self.notifier = notifier
        self._stop = asyncio.Event()
        self._last: ReaperReport | None = None

    async def run_forever(self) -> None:
        if self.config.run_on_start:
            with contextlib.suppress(Exception):
                await self.run_once()
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.interval_seconds)
            if self._stop.is_set():
                break
            with contextlib.suppress(Exception):
                await self.run_once()

    def stop(self) -> None:
        self._stop.set()

    @property
    def last_report(self) -> ReaperReport | None:
        return self._last

    # ------------------------------------------------------------------

    async def run_once(self) -> ReaperReport:
        report = ReaperReport(started_at=utcnow().isoformat())

        await self._sweep_orphans(report)
        await self._retry_failed_teardowns(report)
        if self.config.artifact_gc_enabled:
            await self._collect_artifacts(report)

        self._last = report
        await self.store.events.append(
            scope=EventScope.SYSTEM,
            type=EventType.REAPER_RUN,
            actor=EventActor.SYSTEM,
            payload=report.model_dump(mode="json"),
        )
        if self.notifier is not None and report.still_unresolved:
            with contextlib.suppress(Exception):
                await self.notifier.attention_required(
                    kind="cleanup_incomplete",
                    task_id=None,
                    payload={
                        "count": len(report.still_unresolved),
                        "items": report.still_unresolved[:20],
                        "note": "清理未完成，可继续处理",
                    },
                )
        return report

    # ------------------------------------------------------------------
    # (a) 孤儿回收
    # ------------------------------------------------------------------

    async def _sweep_orphans(self, report: ReaperReport) -> None:
        scan = await self.ledger.scan_orphans()
        report.orphans_found = len(scan.get("orphans", []))

        by_task: dict[str, list[dict]] = {}
        for item in scan.get("orphans", []):
            # 归属任务不存在或已终结，但资源还开着 —— 逐个走台账清理。
            by_task.setdefault(item.get("resource_id", ""), []).append(item)

        for item in scan.get("orphans", []):
            rid = item.get("resource_id")
            row = await self.store.db.fetch_one(
                "SELECT * FROM resource_record WHERE resource_id=?", (rid,)
            )
            if row is None:
                continue
            result = await self.ledger._close_rows([row])
            if result.get("closed"):
                report.orphans_closed += 1
            else:
                report.still_unresolved.append(
                    {
                        "resource_id": rid,
                        "kind": item.get("kind"),
                        "reason": item.get("reason"),
                        "state": item.get("state"),
                    }
                )

    # ------------------------------------------------------------------
    # (b) 失败句柄重试
    # ------------------------------------------------------------------

    async def _retry_failed_teardowns(self, report: ReaperReport) -> None:
        rows = await self.store.resources.list_by_state(["teardown_failed"])
        report.teardown_retried = len(rows)

        for row in rows:
            result = await self.ledger._close_rows([row])
            if result.get("closed"):
                report.teardown_recovered += 1
                continue

            report.teardown_still_failing += 1
            report.still_unresolved.append(
                {
                    "resource_id": row["resource_id"],
                    "kind": row["kind"],
                    "reason": row["last_error"] or "清理仍然失败",
                    "state": "teardown_failed",
                }
            )

    # ------------------------------------------------------------------
    # (c) 产物回收
    # ------------------------------------------------------------------

    async def _collect_artifacts(self, report: ReaperReport) -> None:
        cutoff = utcnow() - timedelta(hours=self.config.artifact_gc_min_age_hours)

        # 仍被活跃或可恢复任务引用的产物一律不动（RES-03：「不能静默清除」）
        protected: set[str] = set()
        for task in await self.store.tasks.list_live_tasks():
            for art in await self.store.artifacts.list_by_task(task.task_id):
                protected.add(art.artifact_id)

        rows = await self.store.db.fetch_all(
            """SELECT * FROM artifact
               WHERE tombstoned=1 AND ref_count=0 AND created_at < ?""",
            (cutoff.isoformat(),),
        )
        candidates = [r for r in rows if r["artifact_id"] not in protected]

        for row in candidates:
            art = await self.store.artifacts.get(row["artifact_id"])
            if art is None:
                continue
            from pathlib import Path

            path = Path(art.storage_path or "")
            size = art.size_bytes or 0
            if path and path.exists():
                path.unlink(missing_ok=True)
            await self.store.db.execute(
                "DELETE FROM artifact WHERE artifact_id=?", (row["artifact_id"],)
            )
            report.artifacts_removed += 1
            report.bytes_reclaimed += size

        held = len(rows) - len(candidates)
        if held:
            report.notes.append(
                f"{held} 份已标记删除的产物仍被活跃或可恢复任务引用，未回收"
            )

    # ------------------------------------------------------------------

    async def storage_report(self) -> dict[str, Any]:
        """存储占用查看入口（RES-03）——提供查看，不强制自动清理。"""
        from ...data.db import Database

        db: Database = self.store.db
        return {
            "database": await db.storage_report(),
            "artifacts": await self.store.artifacts.storage_report(),
            "unresolved_resources": len(await self.ledger.teardown_failed()),
            "last_run": self._last.model_dump(mode="json") if self._last else None,
        }
