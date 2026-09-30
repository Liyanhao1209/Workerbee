"""运行时内核依赖的外部接口（架构设计 v0.02 §12 边界规则）。

``core`` 不 import ``adapters``/``server`` 的实现，只依赖这里声明的协议。
这样内核可以脱离真实 harness 与 HTTP 单独测试——测试替身只要满足协议即可。

端口设计刻意保持窄：内核需要的不是「一个会启动 agent 的东西」，而是六件具体的事
（建会话、发输入、读状态、停会话、报能力、报存活）。窄接口让测试替身的实现成本
低到「写一个就够了」，从而真的会被写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from ..domain.task import Attempt, TaskStage

__all__ = [
    "SessionHandle",
    "SessionCaps",
    "InputKind",
    "HarnessPort",
    "LedgerPort",
    "NotifierPort",
    "PermissionInbox",
]


class InputKind:
    USER = "user"
    BTW = "btw"
    INTERRUPT = "interrupt"


@dataclass
class SessionCaps:
    """内核关心的能力子集。刻意只保留调度决策真正用到的字段。"""

    create_session: bool = True
    resume_session: bool = False
    interact: bool = False
    interrupt: bool = False
    stop: bool = True
    compact: bool = False
    permission_hook: bool = False
    background_tasks: bool = False
    pause_in_place: bool = False
    checkpoint_resume: bool = False
    token_usage: bool = False
    reasoning_efforts: list[str] = field(default_factory=list)
    permission_modes: list[str] = field(default_factory=list)
    """该 harness 支持的权限模式取值。"""

    non_interactive_modes: list[str] = field(default_factory=list)
    """其中不会向用户请求授权的模式。

    与 ``permission_hook=False`` 配对使用：没有钩子时框架拦不住审批，
    因此只有用户显式选定的模式落在这里，执行才被允许（HUM-03）。
    """

    def pause_support(self) -> str:
        """D-07 的四档之一：in_place / checkpoint / restart / none。"""
        if self.pause_in_place:
            return "in_place"
        if self.checkpoint_resume:
            return "checkpoint"
        if self.stop:
            return "restart"
        return "none"

    @classmethod
    def from_mapping(cls, caps: dict[str, Any] | None) -> "SessionCaps":
        if not caps:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in caps.items() if k in known})


@dataclass
class SessionHandle:
    """内核视角的会话句柄。"""

    session_ref: str
    harness_id: str
    state: str = "alive"
    persist_locator: str | None = None
    pid: int | None = None
    model_name: str | None = None
    used_resume: bool = False
    """本次是复用原 session 还是新开 session——两条路径都必须在历史中留痕（§8.2）。"""

    accepted_initial_input: bool = False
    """首轮输入是否已在建会话时交付。

    为 True 时内核**不得**再用 ``send_input`` 投递同一个输入，否则同一条指令会被
    执行两次——对会改文件的 agent 来说那是数据损坏，不是小毛病。
    """

    detail: str | None = None

    def is_alive(self) -> bool:
        return self.state == "alive"


class HarnessPort(Protocol):
    """L3 向内暴露的能力。"""

    async def create_session(
        self,
        *,
        harness_id: str,
        attempt: Attempt,
        stage: TaskStage,
        model_name: str,
        reasoning_effort: str | None,
        system_prompt: str | None,
        initial_input: str | None = None,
        permission_mode: str | None = None,
        credential_ref: str | None = None,
        cwd: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SessionHandle: ...
    """建会话。

    ``initial_input`` 是本次阶段要交给 agent 的首轮输入（即 ContextPackage 的
    用户侧内容）。**必须支持在建会话时给出**：相当一部分 harness 的无头模式是
    「一次性执行一条 prompt 然后退出」（如 ``claude -p``），它们没有「先建空会话
    再注入」这种形态。若适配器确实支持运行中注入（``interact=True``），
    内核在首轮之后还会继续用 ``send_input`` 投递 BTW 等消息。

    ``credential_ref`` 是节点候选上指定的凭据引用（ExecutionProfile.credential_ref），
    优先级高于 harness 登记时的 ``auth_binding``。为 None 时回退到 ``auth_binding``。
    """

    async def resume_session(
        self, *, harness_id: str, persist_locator: str, attempt: Attempt, stage: TaskStage
    ) -> SessionHandle: ...

    async def send_input(
        self, session_ref: str, text: str, *, kind: str = InputKind.USER
    ) -> bool: ...

    async def interrupt(self, session_ref: str) -> bool: ...

    async def terminate(self, session_ref: str, *, signal: str = "TERM") -> bool: ...

    async def abort_stream(self, session_ref: str) -> None:
        """先停远端流以终止继续计费（§10.4）。

        能否真正停止计费取决于厂商实现；UI 需如实标注「已请求取消，计费以厂商为准」。
        """
        ...

    async def pause(self, session_ref: str) -> bool:
        """原位暂停。不支持时返回 False，由调用方走协作停止。"""
        ...

    async def checkpoint(self, session_ref: str) -> str | None:
        """取断点。取不到返回 None——此时恢复只能从头重跑，必须如实告知（LIFE-02）。"""
        ...

    async def compact(self, session_ref: str, threshold: int | None) -> dict[str, Any]: ...

    async def session_alive(self, session_ref: str) -> bool: ...

    async def capabilities(self, harness_id: str) -> SessionCaps: ...

    async def dispose(self, session_ref: str) -> None: ...


class LedgerPort(Protocol):
    """资源台账。先登记后使用，清理只信台账（RES-01/02）。"""

    async def register(
        self,
        *,
        kind: str,
        locator: dict[str, Any],
        owner: dict[str, str | None],
        teardown: dict[str, Any] | None = None,
    ) -> str: ...

    async def close_for_attempt(self, attempt_id: str) -> dict[str, int]:
        """关闭并回收某次尝试名下的全部资源。

        返回三态计数：``closed`` / ``teardown_failed`` / ``orphaned``。
        「已接受删除」「执行已停止」「资源清理完成」是三个独立判据（LIFE-06），
        所以这里必须返回分类计数，而不是一个布尔值。
        """
        ...

    async def close_for_task(self, task_id: str) -> dict[str, int]: ...

    async def reconcile_orphans(self) -> dict[str, Any]: ...


class NotifierPort(Protocol):
    """向客户端推送状态与「需处理」事项（OBS-05）。"""

    async def state_changed(self, *, task_id: str, stage_id: str | None = None) -> None: ...

    async def attention_required(
        self, *, kind: str, task_id: str | None, payload: dict[str, Any]
    ) -> None: ...


@dataclass
class AssembledContext:
    """一次 Attempt 启动前组装好的注入内容（§7.3）。

    组装记录会写入事件日志，使「交接了什么及其来源」可检查（DATA-05）。
    """

    system_prompt: str
    user_input: str
    partitions: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    """发生过降级的记录。降级必须可见，不允许静默压缩。"""

    token_estimate: int = 0
    log_summary: dict[str, Any] = field(default_factory=dict)
    """可直接写进事件日志的摘要信息（不含凭据、不含上游正文）。"""


class ContextBuilderPort(Protocol):
    """L4 上下文组装器向内暴露的能力。"""

    async def build(
        self,
        *,
        task: Any,
        stage: TaskStage,
        attempt: Attempt,
        node: Any,
        contracts: Sequence[Any],
        artifacts: Sequence[Any],
    ) -> AssembledContext: ...


class SummarizerPort(Protocol):
    """摘要生成与质量门禁（D-06、DATA-03）。"""

    async def summarize(
        self, content: str, *, contract_fields: Sequence[str], max_chars: int = 24_000
    ) -> Any:
        """返回带 ``summary`` / ``covered_fields`` / ``missing_fields`` / ``ok`` 的结果。

        覆盖不足时 ``ok=False`` —— 那是交接失败，必须显式报出，
        不允许以静默省略换取「成功」。
        """
        ...


class PermissionInbox(Protocol):
    """审批网关向内暴露的能力（HUM-03/04）。

    内核只做两件事：登记一个待决请求，以及查询某个尝试是否仍有未决请求。
    审批的完整生命周期（超时、失效、回注）属于 L5，内核不重复实现。
    """

    async def pending_for_attempt(self, attempt_id: str) -> list[Any]: ...

    async def invalidate_for_attempt(self, attempt_id: str, why: str) -> int: ...

    async def invalidate_for_task(self, task_id: str, why: str) -> int: ...


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
