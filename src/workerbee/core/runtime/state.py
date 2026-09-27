"""状态机应用层（架构设计 v0.02 §6.2、OBS-01）。

**所有**状态迁移都必须经过这里。这样「合法迁移表」与「事件留痕」是不可绕过的，
而不是靠调用方自觉。

两种失败必须区分对待：

- **非法迁移**（当前状态到目标状态不在迁移表里）→ 抛 ``IllegalTransition``。
  这是代码缺陷，吞掉它只会把 bug 推迟到更难查的地方。
- **CAS 失败**（守卫不满足，例如控制操作刚刚 bump 了 epoch）→ 返回 ``False``。
  这是竞态下的正常结果，调用方据此读取「实际生效结果」而非静默覆盖（AC-03/AC-11）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..domain.task import (
    DesiredState,
    OriginOfControl,
    StageState,
    Task,
    TaskStage,
    TaskState,
    stage_can_transition,
    task_can_transition,
)
from ...data.event_log import EventScope, EventType
from .launch import to_actor

__all__ = ["IllegalTransition", "StateMachine"]


class IllegalTransition(RuntimeError):
    """当前状态不允许迁移到目标状态。属于代码缺陷，不做静默处理。"""

    def __init__(self, entity: str, entity_id: str, src: str, dst: str) -> None:
        super().__init__(f"{entity} {entity_id[:8]} 不允许从 {src} 迁移到 {dst}")
        self.entity = entity
        self.entity_id = entity_id
        self.src = src
        self.dst = dst


class StateMachine:
    """把状态迁移、原子性守卫与事件留痕绑在一起。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.events = store.events

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    async def set_task_state(
        self,
        task: Task | str,
        to_state: TaskState,
        *,
        reason: str | None = None,
        actor: str = "system",
        from_states: Iterable[TaskState] | None = None,
        expected_epoch: int | None = None,
        **fields: Any,
    ) -> bool:
        """迁移任务状态。返回 False 表示守卫未通过（并发方已改过）。

        **始终以库中的当前状态为准**，而不是调用方传入的对象。调用方手里的
        对象可能已经过期（例如刚写完事件、刚被并发操作改过），用它的状态做
        合法性判断会得到错误的结论——把合法迁移误判成非法，或反之。
        """
        task_id = task if isinstance(task, str) else task.task_id
        current = await self.store.tasks.get_task(task_id)
        if current is None:
            raise KeyError(f"任务不存在: {task_id}")

        if current.observed_state == to_state:
            return True  # 幂等：重复通知不产生第二次迁移事件

        if not task_can_transition(current.observed_state, to_state):
            raise IllegalTransition("task", task_id, current.observed_state.value, to_state.value)

        ok = await self.store.tasks.update_task(
            task_id,
            to_state=to_state,
            from_states=list(from_states) if from_states else [current.observed_state],
            expected_epoch=expected_epoch,
            **fields,
        )
        if not ok:
            return False

        await self.events.append(
            scope=EventScope.TASK,
            type=EventType.TASK_STATE_CHANGED,
            actor=to_actor(actor),
            scope_id=task_id,
            task_id=task_id,
            payload={
                "from": current.observed_state.value,
                "to": to_state.value,
                "reason": reason,
            },
        )
        return True

    async def set_task_desired(
        self,
        task: Task | str,
        desired: DesiredState,
        *,
        origin: OriginOfControl | None = None,
        reason: str | None = None,
        actor: str = "user",
    ) -> bool:
        """写控制意图并 bump ``control_epoch``。

        控制操作**只写 desired**，由执行器收敛后写 observed（§1.2 原则 3）。
        epoch 单调递增，使「在途的旧操作」在 CAS 时自然失败（AC-11）。
        """
        task_id = task if isinstance(task, str) else task.task_id
        fields: dict[str, Any] = {"desired_state": desired}
        if origin is not None:
            fields["last_origin"] = origin

        ok = await self.store.tasks.update_task(
            task_id, bump_epoch=True, **fields
        )
        if ok:
            await self.events.append(
                scope=EventScope.TASK,
                type=EventType.TASK_CONTROL,
                actor=to_actor(actor),
                scope_id=task_id,
                task_id=task_id,
                payload={
                    "desired_state": desired.value,
                    "op": origin.op if origin else None,
                    "from_node_id": origin.from_node_id if origin else None,
                    "reason": reason,
                },
            )
        return ok

    # ------------------------------------------------------------------
    # 节点阶段
    # ------------------------------------------------------------------

    async def set_stage_state(
        self,
        stage: TaskStage | str,
        to_state: StageState,
        *,
        reason: str | None = None,
        actor: str = "system",
        from_states: Iterable[StageState] | None = None,
        expected_epoch: int | None = None,
        bump_epoch: bool = False,
        **fields: Any,
    ) -> bool:
        stage_id = stage if isinstance(stage, str) else stage.stage_id
        # 同上：以库中状态为准，不用调用方对象里可能过期的状态做判断。
        current = await self.store.tasks.get_stage(stage_id)
        if current is None:
            raise KeyError(f"阶段不存在: {stage_id}")

        if current.observed_state == to_state:
            # 幂等，但要允许附带字段更新（例如补写 blocked_reason）
            if not fields:
                return True

        elif not stage_can_transition(current.observed_state, to_state):
            raise IllegalTransition(
                "stage", stage_id, current.observed_state.value, to_state.value
            )

        guards = list(from_states) if from_states else [current.observed_state]

        ok = await self.store.tasks.update_stage(
            stage_id,
            to_state=to_state,
            from_states=guards,
            expected_epoch=expected_epoch,
            bump_epoch=bump_epoch,
            **fields,
        )
        if not ok:
            return False

        if current.observed_state != to_state:
            await self.events.append(
                scope=EventScope.STAGE,
                type=EventType.STAGE_STATE_CHANGED,
                actor=to_actor(actor),
                scope_id=stage_id,
                task_id=current.task_id,
                stage_id=stage_id,
                payload={
                    "from": current.observed_state.value,
                    "to": to_state.value,
                    "node_id": current.node_id,
                    "reason": reason,
                },
            )
        return True

    # ------------------------------------------------------------------
    # 批量
    # ------------------------------------------------------------------

    async def cancel_stages_of_task(
        self, task_id: str, *, except_states: Sequence[StageState] = ()
    ) -> int:
        """把任务下未终结的阶段置为 CANCELLED。

        「删除任务」与「立即撤回停用」共用这条路径；正在跑的阶段必须**先停进程**
        再落到 CANCELLED，所以调用方负责先走 cancel_chain，再调这里。
        """
        stages = await self.store.tasks.list_stages(task_id)
        n = 0
        for st in stages:
            if st.observed_state in except_states:
                continue
            if st.observed_state in (
                StageState.SUCCEEDED,
                StageState.FAILED,
                StageState.SKIPPED,
                StageState.CANCELLED,
            ):
                continue
            if await self.set_stage_state(
                st, StageState.CANCELLED, reason="task cancelled", actor="user"
            ):
                n += 1
        return n

    async def mark_stages_skipped(self, stage_ids: Sequence[str], *, reason: str) -> int:
        """D-01(b) 立即撤回：排队阶段标 SKIPPED（可被重新启用拉回 READY）。"""
        n = 0
        for sid in stage_ids:
            st = await self.store.tasks.get_stage(sid)
            if st is None:
                continue
            if st.observed_state not in (StageState.WAITING_DEPS, StageState.READY):
                continue
            if await self.set_stage_state(
                st, StageState.SKIPPED, reason=reason, actor="user"
            ):
                n += 1
        return n

    async def revive_skipped(self, stage_ids: Sequence[str], *, reason: str) -> int:
        """重新启用被立即撤回跳过的阶段（ACT-04：停用不等于删除这些任务）。"""
        n = 0
        for sid in stage_ids:
            st = await self.store.tasks.get_stage(sid)
            if st is None or st.observed_state != StageState.SKIPPED:
                continue
            target = (
                StageState.READY
                if await self._deps_satisfied(st)
                else StageState.WAITING_DEPS
            )
            if await self.set_stage_state(st, target, reason=reason, actor="user"):
                n += 1
        return n

    async def _deps_satisfied(self, stage: TaskStage) -> bool:
        """轻量判定：不重新派生有效图，只查该任务已钉扎的依赖是否都已成功。"""
        task = await self.store.tasks.get_task(stage.task_id)
        if task is None:
            return False
        required = task.graph_snapshot.effective_predecessors(stage.node_id)
        if not required:
            return True
        stages = {s.node_id: s for s in await self.store.tasks.list_stages(stage.task_id)}
        return all(
            stages.get(n) is not None and stages[n].observed_state == StageState.SUCCEEDED
            for n in required
        )
