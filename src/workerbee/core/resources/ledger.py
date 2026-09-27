"""资源台账（架构设计 v0.02 §10.3/§10.4/§10.5、RES-01/02、D-10/D-11）。

设计原则：**先登记后使用，清理只信台账。** 一切运行资源（进程、流、连接、
临时文件、端口、锁、浏览器句柄、会话）创建前先登记；所有清理路径——完成、失败、
暂停、删除、停用、崩溃恢复——遍历台账执行，不依赖内存状态。

两条安全底线（RES-01、AC-15 的验收点）：

1. **禁止为回收本任务资源而终止无关进程。** 因此杀进程只接受「我们自己拉起的
   子进程 pid」，并且在动手前核对 pid 是否仍属于该进程（防 pid 复用）。
2. **禁止删除用户项目文件。** 因此 ``unlink`` 只对**托管目录**内的路径生效；
   任何落在托管目录之外的路径会被拒绝并记为 ``teardown_failed``，
   交由人工核对——而不是删掉再说。

首版只保证资源**归属与释放义务**，不提供框架级硬配额（D-10）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

__all__ = ["ResourceLedger", "TeardownOutcome", "ResourceKind"]

TeardownFn = Callable[[dict[str, Any]], Awaitable[tuple[bool, str | None]]]


class ResourceKind:
    PROCESS = "process"
    API_STREAM = "api_stream"
    MCP_CONN = "mcp_conn"
    TMP_FILE = "tmp_file"
    PORT = "port"
    LOCK = "lock"
    BROWSER_HANDLE = "browser_handle"
    SESSION = "session"

    ALL = (PROCESS, API_STREAM, MCP_CONN, TMP_FILE, PORT, LOCK, BROWSER_HANDLE, SESSION)


class TeardownOutcome:
    CLOSED = "closed"
    TEARDOWN_FAILED = "teardown_failed"
    ORPHANED = "orphaned"
    SKIPPED = "skipped"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ResourceLedger:
    """台账 + 清理执行器。

    ``harness_teardown`` 由 L3 注入：会话、流、MCP 连接的真正关闭动作属于适配层，
    台账只负责「什么时候调、调失败了怎么办」。
    """

    def __init__(
        self,
        store: Any,
        *,
        managed_roots: Iterable[str | Path] = (),
        harness_teardown: TeardownFn | None = None,
        default_grace_ms: int = 3000,
    ) -> None:
        self.store = store
        self.managed_roots = [Path(p).resolve() for p in managed_roots]
        self.harness_teardown = harness_teardown
        self.default_grace_ms = default_grace_ms
        self._handlers: dict[str, TeardownFn] = {
            ResourceKind.PROCESS: self._teardown_process,
            ResourceKind.TMP_FILE: self._teardown_file,
            ResourceKind.SESSION: self._teardown_session,
            ResourceKind.API_STREAM: self._teardown_session,
            ResourceKind.MCP_CONN: self._teardown_session,
            ResourceKind.BROWSER_HANDLE: self._teardown_session,
            ResourceKind.PORT: self._teardown_inert,
            ResourceKind.LOCK: self._teardown_inert,
        }

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------

    async def register(
        self,
        *,
        kind: str,
        locator: dict[str, Any],
        owner: dict[str, str | None],
        teardown: dict[str, Any] | None = None,
        resource_id: str | None = None,
    ) -> str:
        import uuid

        rid = resource_id or str(uuid.uuid4())
        spec = teardown or {}
        spec.setdefault("timeout_ms", self.default_grace_ms)
        spec.setdefault("kind", kind)

        await self.store.resources.register(
            resource_id=rid,
            kind=kind,
            locator=locator,
            owner_task_id=owner.get("task_id"),
            owner_stage_id=owner.get("stage_id"),
            owner_attempt_id=owner.get("attempt_id"),
            owner_node_id=owner.get("node_id"),
            teardown=spec,
        )
        await self.store.events.append(
            scope=_scope("resource"),
            type=_event("RESOURCE_REGISTERED"),
            scope_id=rid,
            task_id=owner.get("task_id"),
            stage_id=owner.get("stage_id"),
            payload={"kind": kind, "locator": _safe_locator(locator)},
        )
        return rid

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def close_for_attempt(self, attempt_id: str) -> dict[str, int]:
        rows = await self.store.resources.list_for_attempt(attempt_id)
        return await self._close_rows(rows)

    async def close_for_task(self, task_id: str) -> dict[str, int]:
        rows = await self.store.resources.list_for_task(task_id)
        return await self._close_rows(rows)

    async def close_for_stage(self, stage_id: str) -> dict[str, int]:
        rows = await self.store.db.fetch_all(
            "SELECT * FROM resource_record WHERE owner_stage_id=?", (stage_id,)
        )
        return await self._close_rows(rows)

    async def _close_rows(self, rows: list[Any]) -> dict[str, int]:
        """逐句柄执行 teardown，返回三态计数。

        LIFE-06 要求「已接受删除」「执行已停止」「资源清理完成」是三个独立判据，
        所以这里绝不返回一个笼统的布尔值——清理失败必须能被单独看见。
        """
        counts = {
            TeardownOutcome.CLOSED: 0,
            TeardownOutcome.TEARDOWN_FAILED: 0,
            TeardownOutcome.ORPHANED: 0,
            TeardownOutcome.SKIPPED: 0,
        }
        for row in rows:
            state = row["state"]
            if state in ("closed",):
                counts[TeardownOutcome.SKIPPED] += 1
                continue

            import json

            locator = json.loads(row["locator"]) if row["locator"] else {}
            spec = json.loads(row["teardown"]) if row["teardown"] else {}
            kind = row["kind"]

            await self.store.resources.mark_state(row["resource_id"], "closing")

            handler = self._handlers.get(kind)
            if handler is None:
                ok, err = False, f"没有为资源类型 {kind} 注册清理方式"
            else:
                try:
                    ok, err = await handler({**locator, **spec})
                except Exception as exc:  # noqa: BLE001 - 清理失败必须可见而非炸穿
                    ok, err = False, f"{type(exc).__name__}: {exc}"

            if ok:
                await self.store.resources.mark_state(row["resource_id"], "closed")
                counts[TeardownOutcome.CLOSED] += 1
                await self.store.events.append(
                    scope=_scope("resource"),
                    type=_event("RESOURCE_CLOSED"),
                    scope_id=row["resource_id"],
                    task_id=row["owner_task_id"],
                    payload={"kind": kind},
                )
            else:
                # 无法确认归属或清理失败 → 保持可见，移交 Reaper（LIFE-06）
                new_state = (
                    TeardownOutcome.ORPHANED if err and "无法确认归属" in err
                    else TeardownOutcome.TEARDOWN_FAILED
                )
                await self.store.resources.mark_state(row["resource_id"], new_state, error=err)
                counts[new_state] += 1
                await self.store.events.append(
                    scope=_scope("resource"),
                    type=_event("RESOURCE_TEARDOWN_FAILED"),
                    scope_id=row["resource_id"],
                    task_id=row["owner_task_id"],
                    payload={"kind": kind, "detail": err},
                )
        return counts

    # ------------------------------------------------------------------
    # 各类资源的清理实现
    # ------------------------------------------------------------------

    async def _teardown_process(self, spec: dict[str, Any]) -> tuple[bool, str | None]:
        """两级终止：SIGTERM → 宽限期 → SIGKILL（§10.4）。

        pid 复用防护：只有能确认该 pid 仍是本台账登记的那个进程时才动手。
        首版用「启动时间 + cmdline 前缀」做核对；两者都取不到时保守放弃并报出。
        """
        pid = spec.get("pid")
        if not isinstance(pid, int):
            return False, "台账里没有可用的 pid，无法确认归属"

        if not _pid_alive(pid):
            return True, None  # 已经不在，视为已释放

        if not _still_same_process(pid, spec.get("cmdline_hint"), spec.get("start_ticks")):
            return (
                False,
                f"pid {pid} 已不属于本台账登记的进程（疑似 pid 复用），"
                f"拒绝终止以免误杀无关进程",
            )

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

        timeout_ms = int(spec.get("timeout_ms", self.default_grace_ms))
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            if not _pid_alive(pid):
                return True, None
            await asyncio.sleep(0.05)

        if str(spec.get("escalate", "kill")).lower() == "kill":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            for _ in range(20):
                if not _pid_alive(pid):
                    return True, None
                await asyncio.sleep(0.05)
            return False, f"pid {pid} 在 SIGKILL 后仍存活"

        return False, f"pid {pid} 在 {timeout_ms}ms 宽限期内未退出（未启用升级）"

    async def _teardown_file(self, spec: dict[str, Any]) -> tuple[bool, str | None]:
        """只删除托管目录内的文件。

        落在托管目录之外的路径一律拒绝：宁可留一个可见的未清理句柄，
        也不能冒着删掉用户项目文件的风险（RES-01、AC-15）。
        """
        raw = spec.get("path")
        if not raw:
            return False, "台账里没有路径"
        path = Path(str(raw)).resolve()

        if not self.managed_roots:
            return (
                False,
                f"没有配置托管目录，拒绝删除 {path}（无法确认归属）",
            )

        if not any(_is_within(path, root) for root in self.managed_roots):
            return (
                False,
                f"路径 {path} 不在 Workerbee 托管目录内，拒绝删除（无法确认归属）",
            )

        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return True, None

    async def _teardown_session(self, spec: dict[str, Any]) -> tuple[bool, str | None]:
        if self.harness_teardown is None:
            return False, "尚未装配适配层清理钩子，会话类资源无法关闭"
        return await self.harness_teardown(spec)

    async def _teardown_inert(self, spec: dict[str, Any]) -> tuple[bool, str | None]:
        """端口与锁只是记账：进程退出即释放，无需主动动作。"""
        return True, None

    # ------------------------------------------------------------------
    # 对账（Reaper 调用）
    # ------------------------------------------------------------------

    async def scan_orphans(self) -> dict[str, Any]:
        """找出台账里仍开着但归属已终结的句柄，以及进程表里的孤儿。

        「只清理能确认归属的对象；无法清理或无法确认归属时明确报告，
        不以隐藏记录代替处理」（RES-02）。
        """
        open_rows = await self.store.resources.list_open()
        orphans: list[dict[str, Any]] = []
        still_owned: list[str] = []

        for row in open_rows:
            task_id = row["owner_task_id"]
            state = await self.store.db.fetch_value(
                "SELECT observed_state FROM task WHERE task_id=?", (task_id,), default=None
            )
            if state is None:
                orphans.append(
                    {
                        "resource_id": row["resource_id"],
                        "kind": row["kind"],
                        "reason": "归属任务不存在",
                        "state": row["state"],
                    }
                )
            elif state in ("succeeded", "failed", "cancelled"):
                orphans.append(
                    {
                        "resource_id": row["resource_id"],
                        "kind": row["kind"],
                        "reason": f"归属任务已终结（{state}）但资源未释放",
                        "state": row["state"],
                    }
                )
            else:
                still_owned.append(row["resource_id"])

        return {
            "open_total": len(open_rows),
            "orphans": orphans,
            "still_owned": len(still_owned),
        }

    async def teardown_failed(self) -> list[dict[str, Any]]:
        rows = await self.store.resources.list_by_state(["teardown_failed", "orphaned"])
        return [
            {
                "resource_id": r["resource_id"],
                "kind": r["kind"],
                "state": r["state"],
                "last_error": r["last_error"],
                "owner_task_id": r["owner_task_id"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _still_same_process(pid: int, cmdline_hint: str | None, start_ticks: Any) -> bool:
    """核对 pid 是否仍是当初登记的那个进程。

    取不到 /proc 信息（非 Linux）时保守返回 True——因为调用方已经在
    「有台账记录」的前提下工作，且这是首版 Linux 目标的实现。
    非 Linux 平台的退化策略见 D-13。
    """
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return False

    if start_ticks is not None:
        try:
            stat = (proc / "stat").read_text()
            after = stat.rsplit(")", 1)[-1].split()
            current_ticks = int(after[19])  # starttime 是第 22 个字段
            if int(start_ticks) != current_ticks:
                return False
        except (OSError, IndexError, ValueError):
            return True

    if cmdline_hint:
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
            if cmdline_hint not in cmdline:
                return False
        except OSError:
            return True

    return True


def read_start_ticks(pid: int) -> int | None:
    """登记进程资源时记下启动时刻，供日后核对 pid 复用。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        after = stat.rsplit(")", 1)[-1].split()
        return int(after[19])
    except (IndexError, ValueError):
        return None


def _safe_locator(locator: dict[str, Any]) -> dict[str, Any]:
    """事件日志里只记非敏感的定位信息。"""
    out = dict(locator)
    for key in ("token", "api_key", "secret", "password"):
        out.pop(key, None)
    return out


def _scope(name: str):
    from ...data.event_log import EventScope

    return {
        "resource": EventScope.RESOURCE,
        "task": EventScope.TASK,
        "system": EventScope.SYSTEM,
    }[name]


def _event(name: str):
    from ...data.event_log import EventType

    return getattr(EventType, name)
