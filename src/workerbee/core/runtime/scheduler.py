"""调度循环与派发（架构设计 v0.02 §6.3、§6.4、D-03、D-04）。

事件驱动优先，轮询兜底。轮询的存在意义不是「发现新工作」（事件会做），而是
**收敛兜底**：事件丢了、回调晚到、或者某个阶段因为竞态停在中间态时，
周期性的一遍扫描能把它重新推起来。

派发路径上的每一步都可能失败，而且失败语义各不相同：
- **claim 失败**：竞态，跳过，下一轮再看（正常）。
- **组装/建会话失败**：可重试错误 → RETRYING；不可重试 → 切候选或 FAILED。
- **建会话成功但发输入失败**：会话已产生，必须走取消链回收，不能只是放弃。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Sequence

from pydantic import Field

from ..domain.base import DomainModel, new_id, utcnow
from ..domain.task import (
    Attempt,
    AttemptOutcome,
    CompactEvent,
    ErrorClass,
    StageState,
    Task,
    TaskStage,
    TaskState,
    Usage,
)
from ...data.event_log import EventActor, EventScope, EventType
from .advance import (
    ACTIVE_STAGE_STATES,
    block_downstream,
    contract_satisfied,
    deps_satisfied,
    evaluate_task_state,
    on_stage_succeeded,
    summarize_failure,
)
from .cancel import CancelOutcome, cancel_chain
from .ports import AssembledContext

__all__ = ["SchedulerConfig", "Scheduler", "TickReport", "AttemptRuntime"]


class SchedulerConfig(DomainModel):
    """调度参数。取值刻意保守：单机部署下宁可慢一点也不要打满本机资源。"""

    poll_interval: float = 1.0
    max_dispatch_per_tick: int = 8
    dispatch_timeout: float = 120.0
    """建会话 + 发输入的时限。超时按可重试错误处理。"""

    session_grace_seconds: float = 5.0
    default_max_output_chars: int = 200_000
    """单次尝试保留的输出上限。超出时截断并在事件里如实标注。"""


class TickReport(DomainModel):
    claimed: int = 0
    dispatched: int = 0
    advanced: int = 0
    blocked: int = 0
    skipped_capacity: int = 0
    skipped_slot: int = 0
    skipped_lease: int = 0
    errors: list[str] = Field(default_factory=list)


@dataclass
class AttemptRuntime:
    """一次在途尝试的内存态。

    只保存**运行期间**的信息（输出缓冲、后台工作计数、结束标记）。
    一旦尝试结束，权威事实全部落库；进程重启后由对账重建或判定（REC-03）。
    """

    attempt_id: str
    stage_id: str
    task_id: str
    node_id: str
    session_ref: str | None = None
    output: list[str] = field(default_factory=list)
    output_chars: int = 0
    truncated: bool = False
    background: set[str] = field(default_factory=set)
    compact_events: list[CompactEvent] = field(default_factory=list)
    usage: Usage | None = None
    session_ended: bool = False
    ended_ok: bool = False
    error_detail: str | None = None
    error_kind: str | None = None
    resume_from_checkpoint: str | None = None
    dispatched_at: float = 0.0

    def text(self, limit: int) -> str:
        joined = "".join(self.output)
        if len(joined) > limit:
            self.truncated = True
            return joined[:limit]
        return joined

    def has_blocking_background(self) -> bool:
        """RUN-06：仍有决定结果的后台工作时不得判定阶段成功。"""
        return len(self.background) > 0


class Scheduler:
    """调度循环 + 派发 + 完成处理。"""

    def __init__(
        self,
        *,
        store: Any,
        sm: Any,
        harness: Any,
        ledger: Any,
        context_builder: Any | None = None,
        summarizer: Any | None = None,
        notifier: Any | None = None,
        config: SchedulerConfig | None = None,
        node_cwd: str | None = None,
    ) -> None:
        self.store = store
        self.sm = sm
        self.harness = harness
        self.ledger = ledger
        self.context_builder = context_builder
        self.summarizer = summarizer
        self.notifier = notifier
        self.config = config or SchedulerConfig()
        self.node_cwd = node_cwd

        self._runtimes: dict[str, AttemptRuntime] = {}
        self._stop = asyncio.Event()
        #: 在派发完成之前就到达的「会话已结束」结论。
        #:
        #: 存在的理由是一处真实时序：harness 可能在 ``session.create`` 返回的
        #: 同一瞬间崩溃（或干脆就在建会话途中崩）。此时调度器还没把这次尝试
        #: 登记进 ``_runtimes``，按会话查不到归属，那条结束信号就会被丢掉——
        #: 结果是阶段**永远停在 running**，界面显示一切正常。
        #: 「永远 running」是架构设计点名要消灭的失败模式（HAR-03）。
        #: 缓冲它，等派发走到登记那一步再兑现。
        self._ended_early: dict[str, tuple[bool, str | None]] = {}
        self._ended_early_limit = 64
        # 退避中的阶段：到点后回队。放在内存里是有意的——重启后这些阶段
        # 由启动对账重新评估（REC-03），不需要为「还要等几秒」做持久化。
        self._retry_heap: list[tuple[float, str]] = []
        #: 会话存活巡检（`_sweep_dead_sessions`）的状态。
        #: 计数而非布尔，是因为 ``session_alive`` 在查不到时会返回 False——
        #: 适配器忙一次、连接抖一下都会走到那条分支。单次否定不足以判定死亡，
        #: 连续两次才收束，免得把健康的阶段误杀。
        self._liveness_misses: dict[str, int] = {}
        self._last_liveness_sweep = 0.0

    # ------------------------------------------------------------------
    # 循环
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 调度循环不能因单次异常退出
                await self._log_error(f"调度循环异常：{type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval)

    def stop(self) -> None:
        self._stop.set()

    def schedule_retry(self, stage_id: str, delay_seconds: float) -> None:
        """把退避中的阶段排进回队队列。"""
        import heapq

        heapq.heappush(
            self._retry_heap,
            (asyncio.get_running_loop().time() + max(0.0, delay_seconds), stage_id),
        )

    async def _promote_due_retries(self) -> int:
        """把退避到期的阶段放回就绪集。"""
        import heapq

        now = asyncio.get_running_loop().time()
        promoted = 0
        while self._retry_heap and self._retry_heap[0][0] <= now:
            _, stage_id = heapq.heappop(self._retry_heap)
            stage = await self.store.tasks.get_stage(stage_id)
            if stage is None or stage.observed_state != StageState.RETRYING:
                continue  # 退避期间被暂停／取消／对账接管了
            if await self.sm.set_stage_state(
                stage,
                StageState.READY,
                reason="退避结束，重新竞争执行槽",
                actor="system",
                status_reason=None,
            ):
                promoted += 1
        return promoted

    # ------------------------------------------------------------------
    # 一轮调度
    # ------------------------------------------------------------------

    #: 会话存活巡检的最小间隔（秒）。调度 tick 比这快得多，不节流会对着
    #: harness 狂发探测请求。
    LIVENESS_SWEEP_INTERVAL = 10.0

    async def _sweep_dead_sessions(self) -> int:
        """巡检运行中的阶段，收束那些会话已经死掉的。

        这是**兜底**，不是主路径：正常情况下由适配层发 SESSION_DIED 通知，走
        ``on_session_ended``。但只要那条通知丢一次——广播时某个订阅者写失败、
        core 重启错过了窗口、适配器进程被强杀——阶段就会永远停在 running，
        任务无限期挂起，而两侧日志都是干净的。所以不能只依赖通知，还得有人
        定期去问一句「它还活着吗」。

        连续两次探测为否定才判定死亡：``session_alive`` 取不到结果时返回 False，
        单次否定可能只是抖动。
        """
        swept = 0
        for rt in list(self._runtimes.values()):
            if rt.session_ended or not rt.session_ref:
                continue
            key = rt.attempt_id
            try:
                alive = await self.harness.session_alive(rt.session_ref)
            except Exception:  # noqa: BLE001 - 探测失败按否定计，靠连续两次兜底
                alive = False
            if alive:
                self._liveness_misses.pop(key, None)
                continue

            misses = self._liveness_misses.get(key, 0) + 1
            self._liveness_misses[key] = misses
            if misses < 2:
                continue

            self._liveness_misses.pop(key, None)
            swept += 1
            rt.session_ended = True
            rt.ended_ok = False
            rt.error_detail = rt.error_detail or "会话已不存在（存活巡检发现）"
            await self._log_error(
                f"存活巡检：会话 {rt.session_ref} 已不存在，收束阶段 {rt.stage_id}"
            )
            await self._maybe_complete(rt)
        return swept

    async def tick(self) -> TickReport:
        report = TickReport()
        now = asyncio.get_running_loop().time()
        if now - self._last_liveness_sweep >= self.LIVENESS_SWEEP_INTERVAL:
            self._last_liveness_sweep = now
            await self._sweep_dead_sessions()
        report.advanced += await self._promote_due_retries()
        ready = await self.store.tasks.list_ready_stages(
            limit=self.config.max_dispatch_per_tick * 4
        )

        # 按批次读取相关任务，避免 N+1 查询打满单连接
        task_ids = list({s.task_id for s in ready})
        tasks: dict[str, Task] = {}
        for tid in task_ids:
            t = await self.store.tasks.get_task(tid)
            if t is not None:
                tasks[tid] = t

        dispatched = 0
        for stage in ready:
            if dispatched >= self.config.max_dispatch_per_tick:
                break
            task = tasks.get(stage.task_id)
            if task is None:
                continue

            verdict = await self._precheck(task, stage)
            if verdict == "slot":
                report.skipped_slot += 1
                continue
            if verdict == "capacity":
                report.skipped_capacity += 1
                continue
            if verdict == "deps":
                # 依赖其实没满足：把它退回等待态，而不是让它一直假装 READY
                await self.sm.set_stage_state(
                    stage, StageState.WAITING_DEPS, reason="依赖尚未满足", actor="system"
                )
                continue
            if verdict != "ok":
                continue

            ok = await self.dispatch(task, stage)
            if ok:
                dispatched += 1
                report.dispatched += 1
            else:
                report.claimed += 1

        return report

    async def _precheck(self, task: Task, stage: TaskStage) -> str:
        """派发前的四项判定，顺序即优先级（便宜的检查放在前面）。"""
        if task.desired_state.value != "active" or task.blocks_new_dispatch():
            return "task"

        stages = {s.node_id: s for s in await self.store.tasks.list_stages(task.task_id)}
        if not deps_satisfied(task, stage, stages):
            return "deps"

        # 节点串行（R §5.2.2）：一个节点同一时刻至多一个占用执行槽的阶段。
        # AWAITING_APPROVAL / RETRYING 等不占槽，因此不阻塞同节点的后续任务（D-03）。
        if await self.store.tasks.node_slot_taken(stage.node_id):
            return "slot"

        workflow = await self.store.workflows.get(task.workflow_id)
        if workflow is not None:
            active = await self.store.tasks.count_active_tasks(task.workflow_id)
            # 已经在本 Workflow 名下的任务不重复计入背压，否则它会把自己堵死
            if active >= workflow.max_concurrent_tasks and not await self._task_has_active_stage(
                task.task_id
            ):
                return "capacity"

        return "ok"

    async def _task_has_active_stage(self, task_id: str) -> bool:
        stages = await self.store.tasks.list_stages(task_id)
        return any(s.observed_state in ACTIVE_STAGE_STATES for s in stages)

    # ------------------------------------------------------------------
    # 派发
    # ------------------------------------------------------------------

    async def dispatch(self, task: Task, stage: TaskStage) -> bool:
        """领取并启动一个阶段。返回 True 表示真的派发了。"""
        # claim：CAS 到 DISPATCHING。失败说明有人先动了它（调序、暂停、删除、对账）。
        claimed = await self.sm.set_stage_state(
            stage,
            StageState.DISPATCHING,
            reason="调度器领取",
            actor="system",
            expected_epoch=stage.control_epoch,
        )
        if not claimed:
            return False

        # 领取后必须重新读一次：从上面那次 CAS 成功到此刻，控制操作可能已经发生
        fresh_stage = await self.store.tasks.get_stage(stage.stage_id)
        fresh_task = await self.store.tasks.get_task(task.task_id)
        if fresh_stage is None or fresh_task is None:
            return False
        if fresh_task.desired_state.value != "active":
            await self.sm.set_stage_state(
                fresh_stage,
                StageState.PAUSED
                if fresh_task.desired_state.value == "paused"
                else StageState.CANCELLED,
                reason="控制意图在派发过程中发生变化",
                actor="system",
            )
            return False

        # 任务从排队转为运行。这一步必须在派发成功之后立刻做，否则：
        # 单节点图会在该节点成功后尝试 QUEUED → SUCCEEDED，而那不是合法迁移。
        if fresh_task.observed_state == TaskState.QUEUED:
            await self.sm.set_task_state(
                fresh_task, TaskState.RUNNING, reason="首个阶段已派发", actor="system"
            )
            fresh_task = await self.store.tasks.get_task(fresh_task.task_id) or fresh_task

        node = fresh_task.graph_snapshot.graph.node(stage.node_id)
        if node is None:
            await self._fail_stage(
                fresh_task, fresh_stage, "节点在钉扎快照中不存在", retryable=False
            )
            return True

        if fresh_stage.profile_cursor >= len(node.profiles):
            await self._fail_stage(
                fresh_task,
                fresh_stage,
                "全部候选都已尝试完，停止自动尝试",
                retryable=False,
            )
            return True

        profile = node.profiles[fresh_stage.profile_cursor]

        attempt = Attempt(
            stage_id=fresh_stage.stage_id,
            task_id=fresh_task.task_id,
            node_id=fresh_stage.node_id,
            attempt_seq=fresh_stage.current_attempt_seq + 1,
            profile_id=profile.profile_id,
            profile_snapshot={
                "model_name": profile.model_name,
                "harness_ref": profile.harness_ref,
                "reasoning_effort": profile.reasoning_effort,
                "compact_threshold": profile.compact_threshold,
                "credential_ref": profile.credential_ref,
                "candidate_index": fresh_stage.profile_cursor,
            },
            resume_from_checkpoint=fresh_stage.checkpoint_ref,
            started_at=utcnow(),
        )

        runtime = AttemptRuntime(
            attempt_id=attempt.attempt_id,
            stage_id=fresh_stage.stage_id,
            task_id=fresh_task.task_id,
            node_id=fresh_stage.node_id,
            resume_from_checkpoint=fresh_stage.checkpoint_ref,
            dispatched_at=asyncio.get_running_loop().time(),
        )

        try:
            context = await self._build_context(fresh_task, fresh_stage, attempt, node)
        except Exception as exc:  # noqa: BLE001
            await self._fail_stage(
                fresh_task,
                fresh_stage,
                f"上下文组装失败：{type(exc).__name__}: {exc}",
                retryable=True,
                error_kind="context",
            )
            return True

        await self.store.tasks.create_attempt(attempt)
        await self.store.tasks.update_stage(
            fresh_stage.stage_id,
            current_attempt_seq=attempt.attempt_seq,
            attempt_count=fresh_stage.attempt_count + 1,
            profile_cursor=fresh_stage.profile_cursor,
            blocked_reason=None,
        )

        session = None
        try:
            session = await asyncio.wait_for(
                self.harness.create_session(
                    harness_id=profile.harness_ref or "",
                    attempt=attempt,
                    stage=fresh_stage,
                    model_name=profile.model_name,
                    reasoning_effort=profile.reasoning_effort,
                    system_prompt=context.system_prompt,
                    # 首轮输入随建会话给出：一次性 `-p` 型 harness 只能这样拿到输入。
                    initial_input=context.user_input,
                    # 权限模式由用户在候选上显式指定；无钩子的 harness 靠它保证
                    # 不会中途停下来等人（HUM-03）。未指定时由适配器如实拒绝。
                    permission_mode=profile.permission_mode,
                    cwd=self.node_cwd,
                ),
                timeout=self.config.dispatch_timeout,
            )
        except asyncio.TimeoutError:
            await self._fail_stage(
                fresh_task,
                fresh_stage,
                f"建立会话超时（{self.config.dispatch_timeout}s）",
                retryable=True,
                error_kind="timeout",
                attempt=attempt,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            kind = _classify(exc)
            await self._fail_stage(
                fresh_task,
                fresh_stage,
                f"建立会话失败：{type(exc).__name__}: {exc}",
                retryable=(kind == ErrorClass.RETRYABLE_ERROR),
                error_kind=_error_kind(exc),
                attempt=attempt,
            )
            return True

        runtime.session_ref = session.session_ref

        # 先登记后使用：会话与其进程在发输入之前进入台账（RES-01）。
        await self.ledger.register(
            kind="session",
            locator={"session_ref": session.session_ref, "harness_id": profile.harness_ref},
            owner={
                "task_id": fresh_task.task_id,
                "stage_id": fresh_stage.stage_id,
                "attempt_id": attempt.attempt_id,
                "node_id": fresh_stage.node_id,
            },
            teardown={"method": "harness_teardown", "timeout_ms": 5000},
        )
        if session.pid:
            from ..resources.ledger import read_start_ticks

            await self.ledger.register(
                kind="process",
                locator={
                    "pid": session.pid,
                    "start_ticks": read_start_ticks(session.pid),
                    "cmdline_hint": profile.harness_ref,
                },
                owner={
                    "task_id": fresh_task.task_id,
                    "stage_id": fresh_stage.stage_id,
                    "attempt_id": attempt.attempt_id,
                    "node_id": fresh_stage.node_id,
                },
                teardown={"method": "kill", "timeout_ms": 3000, "escalate": "kill"},
            )

        await self.store.tasks.update_attempt(
            attempt.attempt_id,
            session_ref=session.session_ref,
            started_at=utcnow(),
            resume_from_checkpoint=None,
        )
        attempt.session_ref = session.session_ref

        if session.accepted_initial_input:
            # 输入已随建会话交付（一次性 `-p` 型 harness 只能这样）。
            # 绝不能再 send_input 一次——同一指令执行两遍会把结果搞坏。
            pass
        else:
            try:
                accepted = await asyncio.wait_for(
                    self.harness.send_input(session.session_ref, context.user_input),
                    timeout=self.config.dispatch_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                # 会话已经产生，必须回收——直接放弃会留下一个没人管的进程
                await self._abort_attempt(
                    fresh_task, fresh_stage, attempt, f"发送输入失败：{exc}"
                )
                return True

            if not accepted:
                await self._abort_attempt(
                    fresh_task, fresh_stage, attempt, "适配器拒绝了输入"
                )
                return True

        self._runtimes[attempt.attempt_id] = runtime

        await self.sm.set_stage_state(
            fresh_stage,
            StageState.RUNNING,
            reason=f"已派发到 {profile.display()}",
            actor="system",
        )

        # 会话可能在派发完成之前就没了（harness 建会话途中崩溃、或结束得太快）。
        # 两条独立的判据，缺一不可：
        #
        # 1. **心跳信号**：结束通知早于在途登记到达时，被 `on_session_ended` 缓冲下来，
        #    这里兑现。它快，但依赖通知真的到达。
        # 2. **主动查活**：直接问「这个会话还活着吗」。它不依赖任何信号的到达顺序，
        #    是最后一道兜底——没有它，一次丢掉的信号就会让阶段**永远停在 running**，
        #    界面显示一切正常。「永远 running」是架构设计点名要消灭的失败模式（HAR-03）。
        early = self._ended_early.pop(session.session_ref, None)
        if early is None:
            try:
                alive = await self.harness.session_alive(session.session_ref)
            except Exception:  # noqa: BLE001 - 查不到就当未知，走正常路径
                alive = True
            if not alive:
                early = (False, "会话在本阶段派发完成前即已结束")

        if early is not None:
            ended_ok, ended_detail = early
            await self.store.events.append(
                scope=EventScope.ATTEMPT,
                type=EventType.ATTEMPT_ENDED,
                actor=EventActor.ADAPTER,
                scope_id=attempt.attempt_id,
                task_id=fresh_task.task_id,
                stage_id=fresh_stage.stage_id,
                payload={
                    "note": "会话在本阶段派发完成前即已结束，按已结束处理",
                    "ok": ended_ok,
                    "detail": ended_detail,
                },
            )
            await self.on_session_ended(
                session_ref=session.session_ref, ok=ended_ok, detail=ended_detail
            )
            return True

        await self.store.events.append(
            scope=EventScope.ATTEMPT,
            type=EventType.ATTEMPT_STARTED,
            actor=EventActor.SYSTEM,
            scope_id=attempt.attempt_id,
            task_id=fresh_task.task_id,
            stage_id=fresh_stage.stage_id,
            payload={
                "attempt_seq": attempt.attempt_seq,
                "profile_id": attempt.profile_id,
                "model": profile.model_name,
                "harness": profile.harness_ref,
                "session_ref": session.session_ref,
                "resumed": session.used_resume,
                "context_degraded": context.degraded,
                "context_tokens": context.token_estimate,
            },
        )

        if self.context_builder is not None and context.log_summary:
            await self.store.events.append(
                scope=EventScope.ATTEMPT,
                type=EventType.CONTEXT_ASSEMBLED,
                actor=EventActor.SYSTEM,
                scope_id=attempt.attempt_id,
                task_id=fresh_task.task_id,
                stage_id=fresh_stage.stage_id,
                payload=context.log_summary,
            )

        await self._notify_state(fresh_task.task_id, fresh_stage.stage_id)
        return True

    async def _build_context(
        self, task: Task, stage: TaskStage, attempt: Attempt, node: Any
    ) -> AssembledContext:
        if self.context_builder is None:
            # 没有组装器时的最小可用上下文：仍然遵守 §7.3 的「留空仍能执行」
            return AssembledContext(
                system_prompt=node.system_prompt or "",
                user_input=_fallback_input(task, node),
                partitions={"P1": {"mode": "fallback"}},
            )

        contracts = [
            (task.graph_snapshot.graph.edge(p, stage.node_id) or _no_contract()).output_contract
            for p in task.graph_snapshot.effective_predecessors(stage.node_id)
        ]
        artifacts = []
        for artifact_ids in stage.upstream_pins.values():
            for aid in artifact_ids:
                art = await self.store.artifacts.get(aid)
                if art is not None and not art.tombstoned:
                    artifacts.append(art)

        return await self.context_builder.build(
            task=task,
            stage=stage,
            attempt=attempt,
            node=node,
            contracts=contracts,
            artifacts=artifacts,
        )

    # ------------------------------------------------------------------
    # 完成路径（由适配器事件驱动）
    # ------------------------------------------------------------------

    def runtime_for(self, attempt_id: str) -> AttemptRuntime | None:
        return self._runtimes.get(attempt_id)

    def runtime_for_session(self, session_ref: str) -> AttemptRuntime | None:
        for rt in self._runtimes.values():
            if rt.session_ref == session_ref:
                return rt
        return None

    async def on_session_ended(
        self, *, session_ref: str, ok: bool, detail: str | None = None
    ) -> bool:
        """适配器报告会话结束。**不等于阶段成功**（RUN-06）。"""
        rt = self.runtime_for_session(session_ref)
        if rt is None:
            # 派发还没走到登记那一步（或这次会话压根没归属）。
            # 先存下来——如果是前者，dispatch 登记完会回头兑现它。
            if len(self._ended_early) >= self._ended_early_limit:
                # 有界：极端情况下也不让这张表无限长。丢最旧的一条。
                self._ended_early.pop(next(iter(self._ended_early)), None)
            self._ended_early[session_ref] = (ok, detail)
            return False
        rt.session_ended = True
        rt.ended_ok = ok
        if not ok:
            rt.error_detail = detail or "会话异常结束"
        return await self._maybe_complete(rt)

    async def on_event(self, *, session_ref: str, kind: str, payload: dict[str, Any]) -> None:
        rt = self.runtime_for_session(session_ref)
        if rt is None:
            return
        if kind == "output":
            text = payload.get("text") or ""
            rt.output.append(text)
            rt.output_chars += len(text)
        elif kind == "background_task_started":
            rt.background.add(str(payload.get("id", "unknown")))
        elif kind == "background_task_ended":
            rt.background.discard(str(payload.get("id", "unknown")))
            await self._maybe_complete(rt)
        elif kind == "compact":
            rt.compact_events.append(
                CompactEvent(
                    trigger=str(payload.get("trigger", "threshold")),
                    effective_threshold=payload.get("effective_threshold"),
                    tokens_before=payload.get("tokens_before"),
                    tokens_after=payload.get("tokens_after"),
                    ok=bool(payload.get("ok", True)),
                    detail=payload.get("detail"),
                )
            )
        elif kind == "usage":
            rt.usage = _usage_from_payload(payload)
        elif kind == "error":
            rt.error_detail = str(payload.get("message", "未知错误"))
            rt.error_kind = payload.get("kind")

    async def _maybe_complete(self, rt: AttemptRuntime) -> bool:
        """三合一完成判据（RUN-06）。三者缺一，不发出完成事件。"""
        if not rt.session_ended:
            return False
        if rt.has_blocking_background():
            # 仍有决定结果的后台工作：显示其关联与状态，但不判成功
            await self.sm.set_stage_state(
                rt.stage_id,
                StageState.RUNNING,
                reason=f"仍有 {len(rt.background)} 项后台工作未结束",
                actor="system",
                status_reason="等待后台工作结束",
            )
            return False

        stage = await self.store.tasks.get_stage(rt.stage_id)
        task = await self.store.tasks.get_task(rt.task_id)
        if stage is None or task is None:
            return False
        if stage.observed_state not in (StageState.RUNNING, StageState.DISPATCHING):
            return False  # 已被暂停／取消／对账接管，完成回调不再生效

        attempt = await self._current_attempt(stage)
        if attempt is None:
            return False

        if not rt.ended_ok:
            await self.on_attempt_failed(
                attempt_id=attempt.attempt_id,
                detail=rt.error_detail or "会话异常结束",
                error_kind=rt.error_kind,
            )
            return True

        # 产出物 + 契约校验
        artifacts, contract_ok, missing, why = await self._materialize_output(task, stage, attempt, rt)
        if not contract_ok:
            await self._fail_stage(
                task,
                stage,
                f"产出物未通过边契约校验：{why}",
                retryable=False,
                error_kind="contract",
                attempt=attempt,
            )
            return True

        return await self._succeed_stage(task, stage, attempt, artifacts)

    async def _materialize_output(
        self, task: Task, stage: TaskStage, attempt: Attempt, rt: AttemptRuntime
    ) -> tuple[list[Any], bool, list[str], str | None]:
        """把会话输出落成产物，并做一次契约校验。

        **先落产物再校验**：即便校验失败，用户也应该能在历史里看到这次到底产出了什么。
        校验失败随之把阶段判为失败，产物本身保留为失败证据。
        """
        node = task.graph_snapshot.graph.node(stage.node_id)
        outputs: list[str] = []
        for succ in task.graph_snapshot.effective_successors(stage.node_id):
            edge = task.graph_snapshot.graph.edge(stage.node_id, succ)
            if edge and edge.output_contract:
                outputs.extend(edge.output_contract.outputs)
        contract_fields = sorted(set(outputs))

        text = rt.text(self.config.default_max_output_chars)
        if rt.truncated:
            await self.store.events.append(
                scope=EventScope.ATTEMPT,
                type=EventType.HANDOFF_FAILED,
                scope_id=attempt.attempt_id,
                task_id=task.task_id,
                stage_id=stage.stage_id,
                payload={
                    "kind": "output_truncated",
                    "kept_chars": len(text),
                    "total_chars": rt.output_chars,
                    "note": "输出超过保留上限，已截断；完整内容未落盘",
                },
            )

        summary_text: str | None = None
        covered: list[str] = []
        summary_ok = True
        if self.summarizer is not None and text.strip():
            try:
                result = await self.summarizer.summarize(
                    text, contract_fields=contract_fields
                )
                summary_text = getattr(result, "summary", None)
                covered = list(getattr(result, "covered_fields", []) or [])
                summary_ok = bool(getattr(result, "ok", True))
                if not summary_ok:
                    await self.store.events.append(
                        scope=EventScope.ATTEMPT,
                        type=EventType.HANDOFF_FAILED,
                        scope_id=attempt.attempt_id,
                        task_id=task.task_id,
                        stage_id=stage.stage_id,
                        payload={
                            "kind": "summary_incomplete",
                            "missing_fields": list(
                                getattr(result, "missing_fields", []) or []
                            ),
                            "reason": getattr(result, "reason", None),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                summary_ok = False
                await self.store.events.append(
                    scope=EventScope.ATTEMPT,
                    type=EventType.HANDOFF_FAILED,
                    scope_id=attempt.attempt_id,
                    task_id=task.task_id,
                    stage_id=stage.stage_id,
                    payload={
                        "kind": "summary_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                    },
                )

        artifacts = []
        if text or summary_text:
            from ..domain.artifact import ArtifactKind, ArtifactProducer

            art = await self.store.artifacts.put(
                text,
                kind=ArtifactKind.TEXT,
                producer=ArtifactProducer(
                    task_id=task.task_id,
                    stage_id=stage.stage_id,
                    attempt_seq=attempt.attempt_seq,
                    node_id=stage.node_id,
                    attempt_id=attempt.attempt_id,
                ),
                sensitivity="internal",
                media_type="text/plain",
                summary=summary_text,
                summary_ok=summary_ok,
                covered_fields=covered,
            )
            artifacts.append(art)

        contracts = [
            (task.graph_snapshot.graph.edge(p, stage.node_id) or _no_contract()).output_contract
            for p in task.graph_snapshot.effective_predecessors(stage.node_id)
        ]
        outgoing = [
            (task.graph_snapshot.graph.edge(stage.node_id, s) or _no_contract()).output_contract
            for s in task.graph_snapshot.effective_successors(stage.node_id)
        ]
        ok, missing, why = contract_satisfied(outgoing, artifacts)
        return artifacts, ok, missing, why

    async def _succeed_stage(
        self, task: Task, stage: TaskStage, attempt: Attempt, artifacts: Sequence[Any]
    ) -> bool:
        rt = self._runtimes.pop(attempt.attempt_id, None)

        await self.store.tasks.complete_attempt(
            attempt.attempt_id,
            outcome=AttemptOutcome(error_class=ErrorClass.SUCCESS),
            usage=rt.usage if rt else None,
        )
        if rt is not None and rt.compact_events:
            await self.store.tasks.update_attempt(
                attempt.attempt_id,
                compact_events=[e.model_dump(mode="json") for e in rt.compact_events],
            )

        # 成功路径同样必须释放资源。失败与取消路径都走了台账清理，唯独成功路径
        # 漏掉的话，每个跑完的阶段都会在台账里留下一个永不关闭的句柄——
        # 「完成」是最常见的路径，所以这个漏洞积累得最快（RES-01）。
        # 会话按 §8.2 归档：阶段结束后不再作为运行会话使用，历史阶段只读。
        await self.ledger.close_for_attempt(attempt.attempt_id)
        if attempt.session_ref:
            with contextlib.suppress(Exception):
                await self.harness.dispose(attempt.session_ref)

        ok = await self.sm.set_stage_state(
            stage,
            StageState.SUCCEEDED,
            reason="会话正常结束且产出物通过契约校验",
            actor="system",
            blocked_reason=None,
            status_reason=None,
        )
        if not ok:
            return False

        await on_stage_succeeded(
            store=self.store,
            sm=self.sm,
            task=task,
            stage=stage,
            attempt=attempt,
            artifacts=artifacts,
        )
        await self._evaluate_task(task)
        await self._notify_state(task.task_id, stage.stage_id)
        return True

    async def on_attempt_failed(
        self,
        *,
        attempt_id: str,
        detail: str,
        error_kind: str | None = None,
        error_class: str | None = None,
    ) -> bool:
        """尝试失败：按 D-05 决定重试、切换候选还是终止。"""
        rt = self._runtimes.get(attempt_id)
        stage_id = rt.stage_id if rt else None
        if stage_id is None:
            attempt = await self.store.tasks.get_attempt(attempt_id)
            if attempt is None:
                return False
            stage_id = attempt.stage_id

        stage = await self.store.tasks.get_stage(stage_id)
        if stage is None:
            return False
        task = await self.store.tasks.get_task(stage.task_id)
        if task is None:
            return False
        attempt = await self._current_attempt(stage)
        if attempt is None or attempt.attempt_id != attempt_id:
            return False

        retryable = error_class != ErrorClass.FATAL_ERROR and _retryable(
            task, stage, error_kind
        )
        await self._fail_stage(
            task,
            stage,
            detail,
            retryable=retryable,
            error_kind=error_kind,
            attempt=attempt,
        )
        return True

    async def _fail_stage(
        self,
        task: Task,
        stage: TaskStage,
        detail: str,
        *,
        retryable: bool,
        error_kind: str | None = None,
        attempt: Attempt | None = None,
    ) -> None:
        """失败处理的分岔口：重试 / 切换候选 / 终止。"""
        attempt = attempt or await self._current_attempt(stage)

        # 无论走哪条路，先回收这次尝试的资源。台账遍历是必须的：
        # 失败路径最容易漏资源，而漏掉的进程会在用户机器上留到下次开机。
        if attempt is not None:
            await self.store.tasks.complete_attempt(
                attempt.attempt_id,
                outcome=AttemptOutcome(
                    error_class=(
                        ErrorClass.RETRYABLE_ERROR if retryable else ErrorClass.FATAL_ERROR
                    ),
                    detail=detail,
                    error_kind=error_kind,
                ),
            )
            await self.ledger.close_for_attempt(attempt.attempt_id)
            if attempt.session_ref:
                with contextlib.suppress(Exception):
                    await self.harness.dispose(attempt.session_ref)
            await self.store.approvals.invalidate_for_attempt(
                attempt.attempt_id, "执行尝试已失败"
            )
            self._runtimes.pop(attempt.attempt_id, None)

        node = task.graph_snapshot.graph.node(stage.node_id)
        profile = (
            node.profiles[stage.profile_cursor]
            if node and stage.profile_cursor < len(node.profiles)
            else None
        )

        if retryable and profile is not None:
            used = stage.attempt_count
            if used < profile.retry.max_attempts:
                delay = _backoff_seconds(profile.retry, used)
                await self.sm.set_stage_state(
                    stage,
                    StageState.RETRYING,
                    reason=f"第 {used} 次尝试失败，{delay:.1f}s 后重试：{detail}",
                    actor="system",
                    blocked_reason=None,
                    status_reason=f"退避中（{delay:.1f}s）",
                )
                await self.store.events.append(
                    scope=EventScope.STAGE,
                    type=EventType.STAGE_RETRY,
                    actor=EventActor.SYSTEM,
                    scope_id=stage.stage_id,
                    task_id=task.task_id,
                    stage_id=stage.stage_id,
                    payload={
                        "attempt_seq": stage.current_attempt_seq,
                        "delay_s": delay,
                        "reason": detail,
                        "model": profile.model_name,
                    },
                )
                self.schedule_retry(stage.stage_id, delay)
                return

        # 候选切换：严格单向推进，已失败的候选在同一次阶段执行中不再回访（D-05）
        if node is not None and stage.profile_cursor + 1 < len(node.profiles):
            next_index = stage.profile_cursor + 1
            next_profile = node.profiles[next_index]
            await self.sm.set_stage_state(
                stage,
                StageState.RETRYING,
                reason=f"切换到候选 {next_profile.display()}",
                actor="system",
                profile_cursor=next_index,
                status_reason=f"已切换到候选：{next_profile.display()}",
            )
            await self.store.events.append(
                scope=EventScope.STAGE,
                type=EventType.STAGE_CANDIDATE_SWITCHED,
                actor=EventActor.SYSTEM,
                scope_id=stage.stage_id,
                task_id=task.task_id,
                stage_id=stage.stage_id,
                payload={
                    "from_index": stage.profile_cursor,
                    "to_index": next_index,
                    "from_model": profile.model_name if profile else None,
                    "to_model": next_profile.model_name,
                    "reason": detail,
                },
            )
            # 切换候选不需要退避等待（换了一个执行者，不是在等同一个它恢复），
            # 但仍要排进回队队列，否则阶段会永远停在 RETRYING。
            self.schedule_retry(stage.stage_id, 0.0)
            return

        # 全部候选耗尽 → 阶段 FAILED，展示各次原因与可选后续动作（CFG-03）
        await self.sm.set_stage_state(
            stage,
            StageState.FAILED,
            reason=detail,
            actor="system",
            blocked_reason=detail,
        )
        blocked = await block_downstream(
            store=self.store,
            sm=self.sm,
            task=task,
            origin_node_id=stage.node_id,
            reason=f"必需上游「{stage.node_name or stage.node_id[:8]}」失败：{detail}",
        )
        await self.store.events.append(
            scope=EventScope.STAGE,
            type=EventType.STAGE_STATE_CHANGED,
            actor=EventActor.SYSTEM,
            scope_id=stage.stage_id,
            task_id=task.task_id,
            stage_id=stage.stage_id,
            payload={
                "to": "failed",
                "reason": detail,
                "candidates_exhausted": True,
                "blocked_downstream": blocked,
            },
        )
        await self._evaluate_task(task, reason=detail)
        await self._notify_state(task.task_id, stage.stage_id, attention=True)

    async def _abort_attempt(
        self, task: Task, stage: TaskStage, attempt: Attempt, detail: str
    ) -> None:
        """会话已建立但派发未完成：必须走取消链回收，不能只是放弃。"""
        await cancel_chain(
            store=self.store,
            sm=self.sm,
            harness=self.harness,
            ledger=self.ledger,
            stage=stage,
            keep_checkpoint=False,
            reason="派发未完成，回收会话",
            grace_seconds=self.config.session_grace_seconds,
        )
        await self._fail_stage(task, stage, detail, retryable=True, error_kind="dispatch")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _current_attempt(self, stage: TaskStage) -> Attempt | None:
        attempts = await self.store.tasks.list_attempts(stage.stage_id, descending=True)
        return attempts[0] if attempts else None

    async def _evaluate_task(self, task: Task, reason: str | None = None) -> None:
        fresh = await self.store.tasks.get_task(task.task_id)
        if fresh is None:
            return
        target = await evaluate_task_state(store=self.store, sm=self.sm, task=fresh)
        if target is None or target == fresh.observed_state:
            return

        from ..domain.task import task_can_transition

        if not task_can_transition(fresh.observed_state, target):
            # 聚合状态是**推导**出来的，推导结果不可达说明状态机或推导规则有出入。
            # 记事件而不是抛异常：调度循环不能因为一次推导分歧而停摆；
            # 但也不能静默——这条记录就是排查入口。
            await self.store.events.append(
                scope=EventScope.TASK,
                type=EventType.TASK_STATE_CHANGED,
                actor=EventActor.SYSTEM,
                scope_id=fresh.task_id,
                task_id=fresh.task_id,
                payload={
                    "skipped": True,
                    "from": fresh.observed_state.value,
                    "derived": target.value,
                    "reason": "推导出的聚合状态在当前状态下不可达，已跳过本次收敛",
                },
            )
            return

        if target == TaskState.FAILED:
            summary = await summarize_failure(store=self.store, task=fresh, reason=reason)
            await self.sm.set_task_state(
                fresh, TaskState.FAILED, reason=reason, failure_summary=summary
            )
        elif target == TaskState.SUCCEEDED:
            await self.sm.set_task_state(fresh, TaskState.SUCCEEDED, reason="全部必需出口已完成")
            await self.store.events.append(
                scope=EventScope.TASK,
                type=EventType.TASK_COMPLETED,
                actor=EventActor.SYSTEM,
                scope_id=fresh.task_id,
                task_id=fresh.task_id,
                payload={"outcome": "succeeded"},
            )
        else:
            await self.sm.set_task_state(fresh, target, reason=reason)

    async def _notify_state(
        self, task_id: str, stage_id: str | None = None, *, attention: bool = False
    ) -> None:
        if self.notifier is None:
            return
        with contextlib.suppress(Exception):
            await self.notifier.state_changed(task_id=task_id, stage_id=stage_id)
            if attention:
                await self.notifier.attention_required(
                    kind="task_failed", task_id=task_id, payload={"stage_id": stage_id}
                )

    async def _log_error(self, message: str) -> None:
        with contextlib.suppress(Exception):
            await self.store.events.append(
                scope=EventScope.SYSTEM,
                type=EventType.SYSTEM_START,
                actor=EventActor.SYSTEM,
                payload={"level": "error", "message": message},
            )


# ---------------------------------------------------------------------------


class _NoContract:
    output_contract = None


def _no_contract() -> _NoContract:
    return _NoContract()


def _usage_from_payload(payload: dict[str, Any]) -> Usage:
    """把适配器上报的用量收敛成 ``Usage``。

    别名归一化放在**消费端**而不是生产端：同一条事件可能经由 router 的翻译路径
    到达，也可能被组合根以原始形态直接转发过来。把归一化放在这里，两条路径
    都能拿到完整数据；放在生产端则只要有一条路径绕开它，数据就会静默变成「未知」。
    （OBS-04：可取得则记录。丢掉厂商已经告诉我们的用量，等于把「可取得」
    硬说成「不可得」。）

    **不做** 0 兜底：字段缺失一律保持 None。未知与零是两回事。
    """

    def pick(*names: str) -> Any:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
        return None

    cost = pick("cost_estimate", "total_cost_usd", "cost_usd")
    basis = pick("cost_basis")
    if cost is not None and basis is None:
        basis = "provider_reported"

    return Usage(
        input_tokens=pick("input_tokens", "inputTokens"),
        output_tokens=pick("output_tokens", "outputTokens"),
        cache_read_tokens=pick(
            "cache_read_tokens", "cache_read_input_tokens", "cacheReadInputTokens"
        ),
        cache_write_tokens=pick(
            "cache_write_tokens", "cache_creation_input_tokens", "cacheCreationInputTokens"
        ),
        cost_estimate=cost,
        cost_basis=basis,
        notes=pick("notes"),
    )


def _fallback_input(task: Task, node: Any) -> str:
    """没有上下文组装器时的输入。

    仍然遵守 CFG-06：入口节点用提交输入，非入口节点至少拿到任务说明。
    """
    from json import dumps

    parts = [f"# 任务\n{task.input_payload.get('task', task.input_payload)}"]
    if node.role:
        parts.append(f"# 你的角色\n{node.role}")
    if task.input_payload:
        parts.append(f"# 提交输入（结构化）\n{dumps(task.input_payload, ensure_ascii=False, indent=2)}")
    return "\n\n".join(parts)


def _classify(exc: Exception) -> str:
    from ...adapters.sdk.protocol import AdapterError, JsonRpcError

    if isinstance(exc, (AdapterError, JsonRpcError)):
        return exc.error_class()
    return ErrorClass.RETRYABLE_ERROR


def _error_kind(exc: Exception) -> str | None:
    from ...adapters.sdk.protocol import AdapterError, JsonRpcError

    if isinstance(exc, (AdapterError, JsonRpcError)):
        return exc.error_kind()
    return None


def _retryable(task: Task, stage: TaskStage, error_kind: str | None) -> bool:
    """按候选上配置的 retryable_errors 判定（D-05）。"""
    node = task.graph_snapshot.graph.node(stage.node_id)
    if node is None or stage.profile_cursor >= len(node.profiles):
        return False
    policy = node.profiles[stage.profile_cursor].retry
    if error_kind is None:
        return True
    return error_kind in policy.retryable_errors


def _backoff_seconds(policy: Any, attempts_used: int) -> float:
    """退避 = min(base × 2^n, cap)，带 ±jitter 抖动，避免同刻重试叠加（D-05）。"""
    import random

    base_ms = policy.backoff_base_ms
    cap_ms = policy.backoff_cap_ms
    raw = min(base_ms * (2 ** max(0, attempts_used - 1)), cap_ms)
    jitter = raw * policy.jitter_ratio
    return max(0.0, (raw + random.uniform(-jitter, jitter)) / 1000.0)
