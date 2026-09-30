"""组合根（架构设计 v0.02 §12 边界规则）。

**只有这个模块可以同时 import 全部各层**。core 只依赖接口，data 与 security 互不感知，
adapters 只能依赖自己的 SDK——这些约束在此处一次性接线，而不是让各层互相渗透。

本模块对外暴露的是**用例级 API**（发射任务、暂停、删除、调序……），
不是仓储 API。这样上层（HTTP 网关、TUI）不必知道内核内部有哪些部件，
也不必按正确顺序调用它们——顺序是内核的事。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, NamedTuple, Sequence

from pydantic import Field

from .executables import resolve as resolve_executable
from .core.domain.base import DomainModel
from .core.domain.registry import HarnessRegistration
from .core.resources.ledger import ResourceLedger
from .core.resources.reaper import Reaper, ReaperConfig
from .core.runtime import lifecycle as lc
from .core.runtime.notifier import BroadcastNotifier
from .core.runtime.reconcile import reconcile_on_startup, resume_from_stage
from .core.runtime.scheduler import Scheduler, SchedulerConfig
from .core.runtime.state import StateMachine
from .data.store import Store
from .security.approval_gateway import ApprovalGateway

__all__ = ["EngineConfig", "Engine"]


class _BuiltinHarness(NamedTuple):
    """首次启动时自动登记的候选。"""

    harness_id: str
    name: str
    adapter_id: str
    binary: str
    env_template: dict[str, str]


#: 内置适配器 → 默认登记信息。只在注册表为空时用（见
#: ``_auto_register_builtin_harnesses``）。
#:
#: claude 必须带上 INPUT_FORMAT=stream-json，否则会走默认的 text 输入模式，
#: 审批转发与运行中交互（attach）**静默不可用**——注册能成功、探测也能成功，
#: 只有等你真去点审批时才发现功能是灰的。这种「配好了但没有」最难查，所以
#: 自动登记时直接给对。
_BUILTIN_HARNESSES: tuple[_BuiltinHarness, ...] = (
    _BuiltinHarness(
        harness_id="claude",
        name="Claude Code",
        adapter_id="claude_code",
        binary="claude",
        env_template={"WORKERBEE_CLAUDE_CODE_INPUT_FORMAT": "stream-json"},
    ),
    _BuiltinHarness(
        harness_id="kimi",
        name="Kimi Code",
        adapter_id="kimi_code",
        binary="kimi",
        env_template={},
    ),
)


class EngineConfig(DomainModel):
    """启动参数。全部有合理默认值，`workerbee-core` 开箱即跑。"""

    data_dir: Path = Path(".workerbee")
    workspace_dir: Path | None = None
    """托管目录。资源台账只在这个范围内删除文件（RES-01 的安全底线）。"""

    passphrase: str | None = None
    """Secret Store 口令。None 表示本次不加载凭据库——
    此时引用凭据的节点会在派发时给出明确错误，而不是静默匿名运行。"""

    adapter_commands: dict[str, list[str]] = Field(default_factory=dict)
    """adapter_id → 启动命令。留空用内置映射。"""

    use_supervisor: bool = False
    """是否把 harness 子进程交给独立的 supervisor 进程托管。

    开启后 core 重启不会打断在跑的会话——这是产品的核心承诺（§3）。
    默认关闭是为了让单进程开发与测试简单；生产部署应开启。
    """

    supervisor_socket: Path | None = None
    """supervisor 的 Unix socket。留空则取 ``<data-dir>/supervisor.sock``。"""

    def resolved_supervisor_socket(self) -> Path:
        return Path(self.supervisor_socket) if self.supervisor_socket else (
            self.data_dir / "supervisor.sock"
        )

    poll_interval: float = 1.0
    reaper_interval: float = 300.0
    approval_timeout: float = 900.0
    artifact_gc_enabled: bool = False

    node_cwd: Path | None = None
    """节点执行的工作目录。None 时用 process 当前目录。"""

    use_context_assembler: bool = True
    use_summarizer: bool = True
    llm_backend: str | None = None
    """None 表示自动：优先 harness CLI（零额外配置），失败则退到无 LLM 模式。

    这里不做静默降级——降级结果会写进 `Engine.startup_notes`，UI 如实展示。
    """

    llm_base_url: str | None = None
    llm_api_key_locator: str | None = None
    llm_model: str | None = None

    def resolved_workspace(self) -> Path:
        return Path(self.workspace_dir) if self.workspace_dir else self.data_dir / "workspace"

    def db_path(self) -> Path:
        return self.data_dir / "workerbee.db"

    def artifact_root(self) -> Path:
        return self.data_dir / "artifacts"


class Engine:
    """内核的组装与生命周期。"""

    def __init__(self, config: EngineConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self.sm = StateMachine(store)
        self.notifier = BroadcastNotifier()
        self.startup_notes: list[str] = []

        self.ledger = ResourceLedger(
            store,
            managed_roots=[config.resolved_workspace()],
            default_grace_ms=3000,
        )
        self.approvals = ApprovalGateway(
            store=store,
            notifier=self.notifier,
            timeout_seconds=config.approval_timeout,
        )
        # 回注通道必须在装配期就接上。不接的话决定会被照常记录、照常显示为
        # 「已批准」，然后静静地送不出去——agent 在那头一直等，用户以为处理完了。
        # 这正是「静默失败」最危险的一种：两端各自都觉得自己是对的。
        self.approvals.set_deliver(self._deliver_approval)
        self.scheduler: Scheduler | None = None
        self.reaper: Reaper | None = None
        self.harness: Any | None = None

        self._secret_store: Any = None
        self._redactor: Any = None
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        config: EngineConfig | None = None,
        *,
        harness: Any | None = None,
        store: Store | None = None,
    ) -> "Engine":
        """装配内核。

        ``store`` 允许调用方传入一个已打开的 Store（测试、或嵌到别的进程里时用）。
        不传则按 ``config.db_path()`` 自己开一个。
        """
        cfg = config or EngineConfig()
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        cfg.resolved_workspace().mkdir(parents=True, exist_ok=True)

        if store is None:
            store = await Store.open(str(cfg.db_path()))
            store.artifacts.root = cfg.artifact_root()

        engine = cls(cfg, store)
        engine.harness = harness if harness is not None else await engine._build_harness()
        # 台账只负责「何时调、失败了怎么办」，真正的关闭动作在适配层（RES-01）。
        # 这条装配漏掉不会有任何报错：每一次会话清理都失败，资源停在
        # teardown_failed，Reaper 每轮重试、每轮失败，错误永久挂在「需处理」里。
        # 实测症状是「已完成的阶段一直报会话关不掉」，而阶段本身是成功的——
        # 报错和真正出问题的地方不在一处，所以它必须和 harness 同时就位。
        engine.ledger.harness_teardown = engine._teardown_harness_session
        engine._wire_security()
        await engine._build_pipeline()
        return engine

    async def _build_harness(self) -> Any:
        if self.config.use_supervisor:
            return await self._build_supervisor_client()

        try:
            from .adapters.host.router import HarnessRouter
        except ImportError as exc:  # pragma: no cover - 适配层缺失时的显式降级
            self.startup_notes.append(
                f"未找到 HarnessRouter（{exc}）；本轮不接任何 harness，"
                f"任务会在派发时给出明确错误"
            )
            return _UnavailableHarness(str(exc))

        return await HarnessRouter.create(
            self.store,
            adapter_commands=self.config.adapter_commands or None,
            secret_store=self._secret_store,
            on_event=self._on_adapter_event,
            on_permission=self.on_permission_request,
            on_session_ended=self._on_session_ended,
            on_exit=self._on_adapter_exit,
            log=self._log_adapter,
        )

    async def _build_supervisor_client(self) -> Any:
        """接上独立的 session 托管进程。

        连不上时**如实报出并继续以不可用状态运行**，而不是悄悄退回本地拉起子进程——
        静默降级会让用户以为「core 重启不会丢会话」，而实际上会。
        """
        from .adapters.host.remote import SupervisorClient

        socket = self.config.resolved_supervisor_socket()
        client = SupervisorClient(
            socket,
            on_event=self._on_adapter_event,
            on_session_died=self._on_session_died,
            on_log=self._log_adapter,
            registration_provider=self._harness_registration,
            credential_resolver=self._resolve_session_credential,
        )
        try:
            await client.ensure_connected(attempts=3, delay=0.5)
        except Exception as exc:  # noqa: BLE001
            self.startup_notes.append(
                f"supervisor 不可达（{socket}）：{type(exc).__name__}。"
                f"本轮不托管会话，core 重启会打断在跑的任务。"
                f"请先启动 workerbee-supervisor。"
            )
            return _UnavailableHarness(f"supervisor 不可达：{socket}")

        self.startup_notes.append(f"session 由 supervisor 托管（{socket}）")
        return client

    async def _harness_registration(self, harness_id: str) -> Any:
        return await self.store.registry.get_harness(harness_id)

    async def _resolve_session_credential(
        self, harness_id: str, credential_ref: str | None
    ) -> Any:
        """supervisor 形态下的凭据解析点（SupervisorClient 的 credential_resolver）。

        解析只能发生在 core：凭据注册表与凭据库口令都在这里，supervisor 刻意
        两者都没有。节点候选的 ``credential_ref`` 优先于 harness 的
        ``auth_binding``；两者皆无返回 None（本机登录态）。解析出的材料随
        session.create 参数经本机 socket 发给 supervisor，不落日志、不进事件。
        """
        from .adapters.host.router import resolve_credential_material

        ref_id = credential_ref
        if not ref_id:
            reg = await self.store.registry.get_harness(harness_id)
            ref_id = reg.auth_binding if reg else None
        if not ref_id:
            return None
        return await resolve_credential_material(
            self.store, self._secret_store, harness_id=harness_id, ref_id=ref_id
        )

    async def store_secret(
        self, locator: str, values: dict[str, str], *, label: str | None = None
    ) -> None:
        """写入凭据本体，并**立即**把值登记进脱敏器。

        脱敏器的已知值清单是在解锁时一次性登记的（unlock_secrets）；解锁之后
        新增的密值若不在此处补登记，会以明文形态通过事件日志与历史的脱敏检查——
        一个「看着在脱敏」的静默失效点。
        """
        if self._secret_store is None:
            raise RuntimeError("凭据库未解锁，无法写入")
        await self._secret_store.put(locator, values, label=label)
        if self._redactor is not None:
            self._redactor.bind_many(values.values())

    async def _deliver_approval(
        self, approval: Any, status: Any, modified_action: str | None
    ) -> bool:
        """把审批决定回注到原会话（HUM-04）。

        找不到会话就如实返回 False，由网关标成 ``undeliverable`` 并保持可见——
        「决定已产生但没送到」是必须让用户看到的事实，不能悄悄吞掉。
        """
        from .core.domain.approval import ApprovalStatus

        attempt = await self.store.tasks.get_attempt(approval.bound_to.attempt_id)
        session_ref = attempt.session_ref if attempt is not None else None
        if not session_ref:
            self.startup_notes.append(
                f"审批 {approval.approval_id[:8]} 无法回注：找不到对应会话"
            )
            return False

        respond = getattr(self.harness, "respond_permission", None)
        if respond is None:
            return False

        # 只有 approved / denied / expired 三种决定有明确语义；其中 expired 按
        # deny_pause 的策略等价于「拒绝该动作」，agent 应当收到一个否定答复而不是
        # 继续等下去。
        approved = status == ApprovalStatus.APPROVED
        delivered = bool(
            await respond(
                session_ref,
                approval_id=approval.approval_id,
                approved=approved,
                modified_action=modified_action,
            )
        )

        # 答复送到之后必须把阶段推回运行态。
        #
        # 不推的话它会一直停在「等待审批」——而 agent 那头其实已经拿到答复、
        # 干完活、甚至已经退出了。界面显示「等你批准」，实际早就批完跑完了；
        # 更糟的是完成判据只在 RUNNING/DISPATCHING 下生效，于是这条阶段
        # 永远不会被判定成功或失败。批准与拒绝都走这里：拒绝之后 agent
        # 同样会继续（它会看到工具被拒并给出结论），那也是一次正常的执行。
        if delivered:
            stage = await self.store.tasks.get_stage(approval.bound_to.stage_id)
            if stage is not None and stage.observed_state.value == "awaiting_approval":
                await self.sm.set_stage_state(
                    stage,
                    _stage_running(),
                    reason="已收到用户决定，继续执行",
                    actor="user",
                    status_reason=None,
                )
                await self.notifier.state_changed(
                    task_id=approval.bound_to.task_id, stage_id=stage.stage_id
                )
        return delivered

    async def _on_session_ended(
        self, session_ref: str, ok: bool, detail: str | None
    ) -> None:
        """会话终结（正常结束或进程死亡）→ 交给调度器走完成／失败判定。

        **这条接线漏了的后果特别隐蔽**：harness 崩溃时没有任何 ``session_ended``
        事件（进程直接没了，SDK 的清理路径根本没跑）。此时如果把崩溃信号丢在地上，
        阶段会永远停在 running，界面显示一切正常，用户等到天荒地老。
        「永远 running」是架构设计点名要消灭的失败模式（HAR-03）。
        """
        if self.scheduler is None:
            return
        await self.scheduler.on_session_ended(
            session_ref=session_ref, ok=ok, detail=detail
        )
        await self.notifier.state_changed(task_id="", stage_id=None)

    async def _on_session_died(self, session_ref: str, reason: str) -> None:
        """会话非正常终止 → 走 REC-02 的恢复路径，而不是直接判失败。"""
        # 先让调度器知道会话没了，否则阶段会卡在 running（同上）。
        if self.scheduler is not None:
            with contextlib.suppress(Exception):
                await self.scheduler.on_session_ended(
                    session_ref=session_ref, ok=False, detail=reason
                )

        await self.store.db.execute(
            "UPDATE session_handle SET state='lost', updated_at=datetime('now') "
            "WHERE session_ref=?",
            (session_ref,),
        )
        await self.store.events.append(
            scope=_scope("session"),
            type=_event("SESSION_LOST"),
            actor=_actor("adapter"),
            scope_id=session_ref,
            payload={"reason": reason, "note": "会话非正常终止，等待对账"},
        )
        await self.notifier.attention_required(
            kind="session_died",
            task_id=None,
            payload={"session_ref": session_ref, "reason": reason},
        )

    async def _teardown_harness_session(
        self, spec: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """L2 台账到 L3 适配层的会话关闭注入口（RES-01、LIFE-06）。

        台账把 ``{**locator, **spec}`` 交给这里，所以 ``session_ref`` 来自登记时的
        locator。返回 ``(ok, err)``；``ok=False`` 时资源保持 teardown_failed 并
        继续可见，而不是被当成已释放。
        """
        session_ref = spec.get("session_ref")
        if not session_ref:
            return False, "台账里没有 session_ref，无法定位要关闭的会话"
        if self.harness is None:
            return False, "适配层未装配，无法关闭会话"

        dispose = getattr(self.harness, "dispose", None)
        if dispose is None:
            return False, "当前适配层没有 dispose，无法关闭会话"
        try:
            await dispose(session_ref)
        except Exception as exc:  # noqa: BLE001 - 清理边界，异常要变成可读结果
            return False, f"关闭会话 {session_ref} 失败：{type(exc).__name__}: {exc}"

        # dispose 内部把适配器异常吞成日志，所以「没抛异常」不等于「已关闭」。
        # 复核一次存活，免得把仍然活着的会话记成已释放——那会让台账开始说谎。
        # 留几次重试是因为关闭分级执行（SIGTERM → 宽限 → SIGKILL），
        # 刚发完信号就查会读到「还没死」。
        alive = getattr(self.harness, "session_alive", None)
        if alive is None:
            return True, None
        for _ in range(3):
            try:
                if not await alive(session_ref):
                    return True, None
            except Exception as exc:  # noqa: BLE001
                return False, f"复核会话 {session_ref} 存活状态失败：{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
        return False, f"会话 {session_ref} 在 dispose 之后仍然存活"

    async def attach_session(self, session_ref: str) -> dict[str, Any]:
        """接入一个正在运行的会话：取出可看的输出与可做的操作。

        三个判据分开报，因为它们对应三种不同的处置：

        - 会话不在运行 → 没有可接入的对象（已结束，或不是内核本地持有的会话）
        - harness 不支持运行中注入 → **能看不能发**。这是正常状态，不是错误；
          Kimi 的 ``-p`` 模式就是这种，输入在建会话时就给定，运行中无法再注入。
        - 会话已失联 → 既不能看也不能发

        把「完全不可用」和「只能看」混成同一个 false，界面就只能对用户说
        「不支持」——而用户真正需要知道的是「能看，但发不进去」。
        """
        rt = self.scheduler.runtime_for_session(session_ref) if self.scheduler else None
        if rt is None:
            return {
                "attachable": False,
                "readable": False,
                "writable": False,
                "interruptible": False,
                "reason": "这个会话不在运行中。已结束的尝试请到任务详情页看事件时间线与产物。",
                "output": "",
                "total_chars": 0,
                "truncated": False,
                "harness_id": "",
            }

        alive = False
        with contextlib.suppress(Exception):
            alive = bool(await self.harness.session_alive(session_ref))

        caps: Any = None
        with contextlib.suppress(Exception):
            caps = await self.harness.capabilities(rt.harness_id)
        # 能力取不到时按「不支持」处理：宁可少给一个按钮，也不能让用户输完才失败。
        interact = bool(getattr(caps, "interact", False))
        can_interrupt = bool(getattr(caps, "interrupt", False))

        text, total, truncated = self.scheduler.session_output(session_ref)

        reason: str | None = None
        if not alive:
            reason = "会话已失联，无法接入。"
        elif not interact:
            reason = (
                f"{rt.harness_id} 不支持运行中交互，只能查看输出。"
                "这类 harness 的输入在创建会话时已经给定。"
            )

        return {
            "attachable": alive and interact,
            "readable": alive,
            "writable": alive and interact,
            "interruptible": alive and can_interrupt,
            "reason": reason,
            "output": text,
            "total_chars": total,
            "truncated": truncated,
            "harness_id": rt.harness_id,
        }

    async def send_to_session(self, session_ref: str, text: str) -> dict[str, Any]:
        """向运行中的会话注入一条消息。

        只投递**首轮之后**的消息。首轮输入若已在建会话时交付（``claude -p``
        这类一次性形态），再投一遍会让同一条指令执行两次——对会改文件的 agent
        那是数据损坏，不是小毛病。``accepted_initial_input`` 那条纪律管的就是
        这件事，这里沿用它。
        """
        info = await self.attach_session(session_ref)
        if not info["writable"]:
            return {"delivered": False, "reason": info["reason"] or "这个会话不接受输入。"}
        try:
            ok = await self.harness.send_input(session_ref, text, kind="user")
        except Exception as exc:  # noqa: BLE001 - 注入失败要变成可读结果
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "delivered": bool(ok),
            "reason": None if ok else "适配器拒绝了这条输入。",
        }

    def _wire_security(self) -> None:
        """把脱敏器接进事件日志。凭据在任何情况下都不进历史（AUTH-02）。"""
        try:
            from .security.secret_store import SecretRedactor
        except ImportError:  # pragma: no cover
            return

        redactor = SecretRedactor()
        self._redactor = redactor
        self.store.events.set_redactor(redactor)
        self._redact_artifacts(redactor)

    def _redact_artifacts(self, redactor: Any) -> None:
        """产物内容与摘要也要过脱敏。

        摘要是 LLM 生成的，而上游正文里可能有凭据；产物正文本身也会被写进
        下游的上下文。两处都不能漏（AUTH-02）。
        """

        original_put = self.store.artifacts.put

        async def guarded_put(content, **kwargs):
            summary = kwargs.get("summary")
            if isinstance(summary, str) and summary:
                kwargs["summary"] = redactor(summary)
            if isinstance(content, str) and content:
                content = redactor(content)
            return await original_put(content, **kwargs)

        self.store.artifacts.put = guarded_put  # type: ignore[assignment]

    async def _build_pipeline(self) -> None:
        """装配上下文组装器与摘要器。"""
        llm = None
        summarizer = None
        assembler = None

        if self.config.use_summarizer or self.config.use_context_assembler:
            llm = await self._build_llm()

        if self.config.use_summarizer:
            from .data.summarizer import NullSummarizer, Summarizer

            summarizer = (
                Summarizer(llm, timeout=60.0) if llm is not None else NullSummarizer()
            )
            if llm is None:
                self.startup_notes.append(
                    "摘要器不可用，已退化为截断式摘要。交接会标记为摘要不完整。"
                )

        if self.config.use_context_assembler:
            from .data.context_assembler import ContextAssembler

            assembler = ContextAssembler()

        self.scheduler = Scheduler(
            store=self.store,
            sm=self.sm,
            harness=self.harness,
            ledger=self.ledger,
            context_builder=assembler,
            summarizer=summarizer,
            notifier=self.notifier,
            config=SchedulerConfig(poll_interval=self.config.poll_interval),
            node_cwd=str(self.config.node_cwd) if self.config.node_cwd else None,
        )
        self.reaper = Reaper(
            store=self.store,
            ledger=self.ledger,
            notifier=self.notifier,
            config=ReaperConfig(
                interval_seconds=self.config.reaper_interval,
                artifact_gc_enabled=self.config.artifact_gc_enabled,
            ),
        )

    async def _build_llm(self) -> Any | None:
        from .data.llm import LLMRouter

        backends: list[Any] = []
        # 默认后端走本机已登录的 harness CLI：零额外配置。
        # 挑一个**实际存在**的，而不是假定名为 claude——
        # 在没有 claude 的机器上，假定会导致「摘要器不可用」这种本可避免的降级。
        # 用与适配器同一套查找，**不只查 PATH**：守护进程的 PATH 里通常没有
        # nvm / ~/.kimi-code/bin 这类位置，只查 PATH 会在这台明明装了两者的机器上
        # 报「既没有 claude 也没有 kimi」，与紧随其后的自动登记提示自相矛盾。
        chosen = next(
            (c for c in ("claude", "kimi") if resolve_executable(None, c) is not None),
            None,
        )
        if chosen is None:
            self.startup_notes.append(
                "本机既没有 claude 也没有 kimi，无法使用 CLI 后端做摘要／AI 建图"
            )
        else:
            try:
                from .data.llm import HarnessCLIBackend

                backends.append(
                    HarnessCLIBackend(harness=chosen, model=self.config.llm_model)
                )
            except Exception as exc:  # noqa: BLE001
                self.startup_notes.append(f"本机 harness CLI 后端不可用：{exc}")

        if self.config.llm_base_url and self.config.llm_api_key_locator:
            key = await self._load_secret_value(self.config.llm_api_key_locator)
            if key:
                try:
                    from .data.llm import OpenAICompatBackend

                    backends.append(
                        OpenAICompatBackend(
                            base_url=self.config.llm_base_url,
                            api_key=key,
                            model=self.config.llm_model or "gpt-4o-mini",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self.startup_notes.append(f"API 后端配置无效：{exc}")
            else:
                self.startup_notes.append(
                    "配置了 API 后端但凭据库未解锁，本次只用 harness CLI 后端"
                )

        if not backends:
            return None
        return LLMRouter(backends, on_fallback=self._on_llm_fallback)

    @property
    def secret_store(self) -> Any:
        return self._secret_store

    async def unlock_secrets(self, passphrase: str) -> int:
        """解锁凭据库并把全部密值登记进脱敏器。返回登记条数。

        两条容易写错、写错了还不会报错的地方，这里都显式处理：

        1. ``bind_store`` 是**协程**。漏掉 await 不会抛异常，只会让脱敏器永远
           登记不到任何密值——表面一切正常，直到某天一个不含可识别前缀的密钥
           原样出现在事件历史里。所以这里 await，并且核对登记条数。
        2. 库不存在时抛的是 ``StoreNotFoundError``（``SecretStoreError`` 的子类），
           不是 ``FileNotFoundError``。捕错异常会让首次启动直接失败。

        口令错误必须原样上抛：那不是「降级」，是用户必须知道的事。
        """
        from .security.secret_store import SecretStore, StoreNotFoundError

        vault = str(self.config.data_dir / "secrets.vault")
        try:
            store = await SecretStore.open(passphrase, vault)
        except StoreNotFoundError:
            store = await SecretStore.create(passphrase, vault)

        self._secret_store = store

        n = 0
        if self._redactor is not None:
            # bind_store 是协程。漏掉 await 不会抛异常，只会让脱敏器永远登记不到
            # 任何密值——表面正常，直到某天一个不含可识别前缀的密钥原样进了历史。
            n = await self._redactor.bind_store(store)

        if self.harness is not None and hasattr(self.harness, "set_secret_store"):
            self.harness.set_secret_store(store)

        locator_count: int | None = None
        try:
            locator_count = len(await store.list_locators())
        except Exception:  # noqa: BLE001 - 统计失败不该阻断解锁
            locator_count = None

        if n == 0 and locator_count:
            # 库里有凭据却一个都没登记——这正是那个静默失效的特征，必须报出来。
            self.startup_notes.append(
                f"凭据库已解锁（{locator_count} 条），但脱敏器没有登记到任何密值，"
                f"事件历史中的凭据可能不会被遮蔽"
            )

        await self.store.events.append(
            scope=_scope("system"),
            type=_event("SECRET_BOUND"),
            actor=_actor("user"),
            payload={
                "locator_count": locator_count,
                "bound_values": n,
                "note": "凭据库已解锁；密值已登记进脱敏器",
            },
        )
        return n

    async def lock_secrets(self) -> None:
        """锁定凭据库并清空脱敏器里登记的密值。"""
        if self._secret_store is not None:
            with contextlib.suppress(Exception):
                await self._secret_store.lock()
        if self._redactor is not None:
            with contextlib.suppress(Exception):
                self._redactor.clear()
        self._secret_store = None

    async def _load_secret_value(self, locator: str) -> str | None:
        """取一个凭据值。库未解锁时返回 None，由调用方如实降级而不是匿名运行。"""
        store = self._secret_store
        if store is None:
            return None
        try:
            data = await store.get(locator)
        except Exception:  # noqa: BLE001
            return None
        if not data:
            return None
        for value in data.values():
            if value:
                return value
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, *, reconcile: bool = True) -> None:
        if self._running:
            return
        self._running = True

        if self.harness is not None and hasattr(self.harness, "start"):
            with contextlib.suppress(Exception):
                await self.harness.start()

        await self.store.events.append(
            scope=_scope("system"),
            type=_event("SYSTEM_START"),
            actor=_actor("system"),
            payload={"data_dir": str(self.config.data_dir)},
        )

        if reconcile:
            report = await reconcile_on_startup(
                store=self.store,
                sm=self.sm,
                harness=self.harness,
                ledger=self.ledger,
                scheduler=self.scheduler,
                notifier=self.notifier,
            )
            if report.lost:
                self.startup_notes.append(
                    f"对账发现 {len(report.lost)} 个阶段状态不明，需人工核对"
                )
            if report.reattached:
                self.startup_notes.append(
                    f"对账恢复了 {len(report.reattached)} 个仍在执行的会话监控"
                )

        with contextlib.suppress(Exception):
            await self._auto_register_builtin_harnesses()

        with contextlib.suppress(Exception):
            await self._ensure_capability_snapshots()

        self._spawn(self.scheduler.run_forever(), "scheduler")
        self._spawn(self.reaper.run_forever(), "reaper")
        self._spawn(self._approval_expiry_loop(), "approval-expiry")
        self._spawn(self._drain_loop(), "drain-ops")

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"workerbee:{name}")
        self._tasks.append(task)

    async def stop(self, *, close_store: bool = True) -> None:
        """停止全部后台循环。

        ``close_store=False`` 供「store 由调用方持有」的场景（测试夹具、嵌入宿主）
        使用——那种情况下关库是调用方的事，这里再关一次会让它的清理报错。
        """
        self._running = False
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.reaper is not None:
            self.reaper.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        if self.harness is not None and hasattr(self.harness, "stop"):
            with contextlib.suppress(Exception):
                await self.harness.stop()
        with contextlib.suppress(Exception):
            await self.store.events.append(
                scope=_scope("system"),
                type=_event("SYSTEM_STOP"),
                actor=_actor("system"),
                payload={},
            )
        if close_store:
            await self.store.close()

    async def _auto_register_builtin_harnesses(self) -> list[str]:
        """首次启动（注册表为空）时，把本机找得到的内置 harness 登记上。

        **只在注册表为空时动手。** 只要里面已经有任何东西就完全不动——用户可能
        是有意只登记一部分，凭空补记录会打乱他的配置，而且删起来还得先弄清是
        自己建的还是自动建的。

        登记时存**解析后的绝对路径**，而不是留空靠每次再解析：路径要能看见、
        能改。用户以后想知道「它用的是哪个 claude」，不该需要去猜搜索顺序。
        代价是工具搬家后要重新登记，但那时界面上明摆着一个错的路径，比一个
        空字段好排查。
        """
        if await self.store.registry.list_harnesses():
            return []

        registered: list[str] = []
        for entry in _BUILTIN_HARNESSES:
            path = resolve_executable(None, entry.binary)
            if path is None:
                continue
            await self.store.registry.upsert_harness(
                HarnessRegistration(
                    harness_id=entry.harness_id,
                    name=entry.name,
                    adapter_id=entry.adapter_id,
                    exec_path=path,
                    env_template=dict(entry.env_template),
                    enabled=True,
                )
            )
            registered.append(f"{entry.name}（{path}）")

        if registered:
            self.startup_notes.append(
                "首次启动，已自动登记本机检测到的 harness："
                + "、".join(registered)
                + "。可在注册表页查看或修改。"
            )
        return registered

    async def _ensure_capability_snapshots(self, *, force: bool = False) -> dict[str, str]:
        """给尚未探测过的 harness 补一次能力探测。

        为什么必须有这一步：HUM-03 的权限门禁读的是库里的 ``capabilities_snapshot``。
        如果用户登记了 harness 却从未点过「探测」，快照为空——校验管线只会给一条
        INFO 就放过，于是「没有权限钩子 + 没说权限模式」这个组合可以一路发射，
        最终在运行时卡在一个无人应答的提问上。**能绕过的门禁等于没有门禁。**

        探测失败如实记录，不伪造能力（HAR-02）。返回 ``{harness_id: 结论}``。
        """
        results: dict[str, str] = {}
        harnesses = await self.store.registry.list_harnesses()
        for reg in harnesses:
            if not reg.enabled:
                continue
            if reg.capabilities_snapshot is not None and not force:
                snap = reg.capabilities_snapshot or {}
                # 快照存在不等于快照可信。三种情况都要重新探测：
                #   1) 上次探测失败；
                #   2) **权限模式声明为空**——校验管线会把它读成「该 harness 不支持
                #      任何不询问的模式」，从而拒绝一次本应合法的发射。一个「取不到」
                #      的能力被当成「没有」，正是本项目明令禁止的失败模式；
                #   3) 显式要求强制刷新。
                # 第 2 条的代价是：真的什么都不声明的 harness 每次发射会多一次
                # 廉价 RPC。用它换「绝不让陈旧空快照挡掉合法提交」，划算。
                # 只有「探测成功**且**声明完整」才跳过；其余一律重探。
                if reg.last_probe_ok is True and snap.get("permission_modes"):
                    continue
                results[reg.harness_id] = "reprobed"
            try:
                caps = await self.harness.capabilities(reg.harness_id)
                import dataclasses

                payload = (
                    dataclasses.asdict(caps) if dataclasses.is_dataclass(caps) else dict(caps)
                )
                await self.store.registry.record_probe(
                    reg.harness_id, ok=True, capabilities=payload, error=None
                )
                if not payload.get("permission_modes"):
                    # 适配层没能给出完整声明（例如它自己也拿不到注册信息，
                    # 只好回一份保守默认值）。把它记成**未验证**而不是成功，
                    # 下一次发射会再试一次。
                    await self.store.registry.record_probe(
                        reg.harness_id,
                        ok=None,  # 未知，不是失败——两者混同会阻断合法发射
                        capabilities=payload,
                        error="适配层未给出权限模式声明，本次结论视为未验证（HAR-02）",
                    )
                    results[reg.harness_id] = "unverified"
                    continue
                results[reg.harness_id] = "probed"
            except Exception as exc:  # noqa: BLE001 - 探测失败如实记录，不伪造
                await self.store.registry.record_probe(
                    reg.harness_id,
                    ok=False,
                    capabilities=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
                results[reg.harness_id] = f"failed: {type(exc).__name__}"
                self.startup_notes.append(
                    f"harness「{reg.name}」能力探测失败：{type(exc).__name__}。"
                    f"未探测的能力无法参与兼容性判定，提交任务时会被提示。"
                )
        return results

    async def _approval_expiry_loop(self) -> None:
        while self._running:
            await asyncio.sleep(max(1.0, min(30.0, self.config.approval_timeout / 10)))
            with contextlib.suppress(Exception):
                await self._expire_approvals_once()

    async def _expire_approvals_once(self) -> list[dict[str, Any]]:
        expired = await self.approvals.expire_due()
        for item in expired:
            # 超时的审批把阶段置为显式等待态，释放 stream 资源（§9.2、D-03）
            with contextlib.suppress(Exception):
                stage = await self.store.tasks.get_stage(item["stage_id"])
                if stage is not None:
                    await self.sm.set_stage_state(
                        stage,
                        _stage_blocked(),
                        reason="审批超时（deny_pause），等待用户处置",
                        actor="system",
                        blocked_reason="审批等待超时，动作已被拒绝",
                    )
            await self.notifier.attention_required(
                kind="approval_expired", task_id=item["task_id"], payload=item
            )
        return expired

    async def _drain_loop(self) -> None:
        """周期推进「排水」中的节点启停操作（D-01）。"""
        while self._running:
            await asyncio.sleep(2.0)
            with contextlib.suppress(Exception):
                done = await lc.complete_drain_ops(store=self.store, sm=self.sm)
                for item in done:
                    await self.notifier.attention_required(
                        kind="node_disabled",
                        task_id=None,
                        payload=item,
                    )

    # ------------------------------------------------------------------
    # 适配层回调
    # ------------------------------------------------------------------

    async def _on_adapter_event(self, event: Any) -> None:
        if self.scheduler is None:
            return
        kind = str(getattr(event, "kind", ""))
        session_ref = getattr(event, "session_ref", None)
        if not session_ref:
            return

        if kind == "session_ended":
            ok = bool(getattr(event, "data", {}).get("ok", True))
            detail = getattr(event, "data", {}).get("detail")
            await self.scheduler.on_session_ended(
                session_ref=session_ref, ok=ok, detail=detail
            )
            await self.notifier.state_changed(task_id="", stage_id=None)
            return

        data = dict(getattr(event, "data", {}) or {})
        if kind == "output" and getattr(event, "text", None) is not None:
            data["text"] = event.text
        await self.scheduler.on_event(session_ref=session_ref, kind=kind, payload=data)
        # 进度类事件不必逐条推送（OBS-05），只在有状态含义时提醒
        if kind in ("state_change", "compact", "error"):
            await self.notifier.state_changed(task_id="", stage_id=None)

    async def on_permission_request(self, request: Any) -> None:
        """把 harness 的权限事件转译为 Approval（HUM-03）。

        **归属优先用适配器回带的 ``attempt_id``，而不是用 ``session_ref`` 去反查在途表。**
        理由是一处真实存在的时序：权限请求可能在 ``session.create`` 尚未返回、
        调度器还没来得及把这次尝试登记进在途表时就到达（harness 在建会话的过程中
        就发问，是完全正常的行为）。此时按 session 反查会查不到，请求会被当成
        「无法归属」丢弃——用户永远看不到那条该由他决定的审批，而 agent 在那头干等。
        ``attempt_id`` 是适配器从建会话请求里带出来的，不依赖任何登记顺序。
        """
        attempt_id = getattr(request, "attempt_id", None)
        stage_id = task_id = None

        if attempt_id:
            attempt = await self.store.tasks.get_attempt(attempt_id)
            if attempt is not None:
                stage_id, task_id = attempt.stage_id, attempt.task_id

        if stage_id is None and self.scheduler is not None:
            rt = self.scheduler.runtime_for_session(request.session_ref)
            if rt is not None:
                attempt_id, stage_id, task_id = rt.attempt_id, rt.stage_id, rt.task_id

        if stage_id is None or task_id is None:
            # 既没有 attempt_id、也查不到在途会话：可能是已结束阶段的迟到请求，
            # 也可能是适配器没带上归属信息。**不能凭空授权**——如实登记并让用户知道。
            await self.notifier.attention_required(
                kind="orphan_permission",
                task_id=None,
                payload={
                    "session_ref": request.session_ref,
                    "attempt_id": attempt_id,
                    "action": request.action,
                    "target": request.target,
                    "note": (
                        "该权限请求无法归属到任何阶段，未自动处理。"
                        "若这属于一个仍在运行的任务，请人工介入——agent 可能在等待答复。"
                    ),
                },
            )
            await self.store.events.append(
                scope=_scope("approval"),
                type=_event("APPROVAL_REQUESTED"),
                actor=_actor("adapter"),
                payload={
                    "orphan": True,
                    "session_ref": request.session_ref,
                    "attempt_id": attempt_id,
                    "action": request.action,
                    "note": "无法归属，未生成审批项",
                },
            )
            return

        stage = await self.store.tasks.get_stage(stage_id)
        task = await self.store.tasks.get_task(task_id)
        if stage is None or task is None:
            return

        approval = await self.approvals.request(
            approval_id=request.approval_id,
            task_id=task.task_id,
            stage_id=stage.stage_id,
            attempt_id=attempt_id,
            revision_seq=task.revision_seq,
            node_id=stage.node_id,
            action=request.action,
            target=request.target,
            risk=request.risk,
            tool_name=request.tool_name,
            workflow_name=task.workflow_name,
            raw=getattr(request, "raw", None),
        )

        # 等待审批期间受保护操作不得执行，且阶段显示为「等待审批」——
        # 它不占执行槽（D-03），同节点的后续任务可以继续跑。
        if stage.observed_state.value == "running":
            await self.sm.set_stage_state(
                stage,
                _stage_awaiting(),
                reason=f"等待用户审批：{approval.action[:80]}",
                actor="adapter",
                status_reason="等待审批（不占用执行节点）",
            )
        await self.notifier.state_changed(task_id=task.task_id, stage_id=stage.stage_id)

    async def _on_adapter_exit(self, harness_id: str, code: int | None, stderr: str) -> None:
        """适配器进程退出。

        事件**按受影响的尝试逐条写**，而不是只写一条无归属的。崩溃会打断这些
        任务，它们的时间线上必须看得到——只写在全局视野里的话，用户在任务详情页
        只会看到一个停在 running 的阶段，不知道发生过什么。适配器空闲时退出
        （没有受影响的尝试）仍写一条无归属的，那是系统级事实。
        """
        payload = {
            "harness_id": harness_id,
            "exit_code": code,
            "stderr_tail": stderr[-2000:] if stderr else None,
            "note": "适配器进程退出；相关会话状态未知，等待对账（REC-02）",
        }
        affected = self.scheduler.runtimes_for_harness(harness_id) if self.scheduler else []

        if affected:
            for rt in affected:
                await self.store.events.append(
                    scope=_scope("session"),
                    type=_event("SESSION_LOST"),
                    actor=_actor("adapter"),
                    task_id=rt.task_id,
                    stage_id=rt.stage_id,
                    payload={**payload, "attempt_id": rt.attempt_id},
                )
        else:
            await self.store.events.append(
                scope=_scope("session"),
                type=_event("SESSION_LOST"),
                actor=_actor("adapter"),
                payload=payload,
            )

        # 「需处理」只报一条：一次崩溃是一个系统级事实，受影响的任务数放在
        # payload 里。按任务各报一条会让一次崩溃刷出 N 个同样的条目，把清单淹掉。
        await self.notifier.attention_required(
            kind="adapter_exited",
            task_id=None,
            payload={
                "harness_id": harness_id,
                "exit_code": code,
                "affected_tasks": [rt.task_id for rt in affected],
            },
        )

    def _log_adapter(self, message: str) -> None:
        # 适配器日志可能夹带 harness 原始输出；过一遍脱敏再落地
        redactor = getattr(self, "_redactor", None)
        if redactor is not None and message:
            with contextlib.suppress(Exception):
                message = redactor(message)
        print(f"[adapter] {message}")

    async def _on_llm_fallback(self, *args: Any, **kwargs: Any) -> None:
        """LLM 后端降级必须可见（不静默）。"""
        await self.notifier.attention_required(
            kind="llm_fallback",
            task_id=None,
            payload={"detail": str(args) or str(kwargs)},
        )

    # ------------------------------------------------------------------
    # 用例 API：发射与控制
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        workflow_id: str,
        input_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 50,
        actor: str = "user",
    ) -> dict[str, Any]:
        from .core.runtime.launch import launch_task

        # 发射前补齐能力快照：否则「登记了但没探测」的 harness 会绕过权限门禁。
        with contextlib.suppress(Exception):
            await self._ensure_capability_snapshots()

        try:
            result = await launch_task(
                store=self.store,
                workflow_id=workflow_id,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
                priority=priority,
                actor=actor,
            )
        except Exception as exc:
            from .core.runtime.launch import LaunchRejected

            if isinstance(exc, LaunchRejected):
                return {
                    "accepted": False,
                    "report": exc.report.model_dump(mode="json"),
                }
            raise

        await self.notifier.state_changed(task_id=result.task.task_id)
        return {
            "accepted": True,
            "created": result.created,
            "task_id": result.task.task_id,
        }

    async def pause(self, task_id: str, *, from_node_id: str | None = None, reason: str | None = None):
        return await lc.pause_task(
            store=self.store, sm=self.sm, harness=self.harness, ledger=self.ledger,
            task_id=task_id, origin_node_id=from_node_id, reason=reason,
        )

    async def resume(self, task_id: str, *, restart_failed: bool = False):
        return await lc.resume_task(
            store=self.store, sm=self.sm, harness=self.harness, ledger=self.ledger,
            task_id=task_id, restart_failed=restart_failed,
        )

    async def delete_task(self, task_id: str, *, from_node_id: str | None = None, reason: str | None = None):
        return await lc.delete_task(
            store=self.store, sm=self.sm, harness=self.harness, ledger=self.ledger,
            task_id=task_id, origin_node_id=from_node_id, reason=reason,
        )

    async def delete_workflow(self, workflow_id: str):
        return await lc.delete_workflow(
            store=self.store, sm=self.sm, harness=self.harness, ledger=self.ledger,
            workflow_id=workflow_id,
        )

    async def set_node_enabled(
        self, workflow_id: str, node_id: str, enable: bool, *, mode: str = "drain", reason: str | None = None
    ):
        return await lc.set_node_enabled(
            store=self.store, sm=self.sm, harness=self.harness, ledger=self.ledger,
            workflow_id=workflow_id, node_id=node_id, enable=enable, mode=mode, reason=reason,
        )

    async def preview_toggle(self, workflow_id: str, node_id: str, enable: bool) -> dict[str, Any]:
        """ACT-02 的路径可见：操作前展示受影响的节点、依赖与任务范围。"""
        from .core.graph.validate import validate_toggle

        rev = await self.store.workflows.get_current_revision(workflow_id)
        if rev is None:
            raise ValueError("Workflow 没有可用的修订版本")
        registry = await self.store.registry.snapshot()
        delta, report = validate_toggle(rev.graph, node_id, enable, registry)

        affected_tasks: list[str] = []
        for task in await self.store.tasks.list_live_tasks():
            if task.workflow_id != workflow_id:
                continue
            stages = await self.store.tasks.list_stages(task.task_id)
            if any(s.node_id == node_id for s in stages):
                affected_tasks.append(task.task_id)

        return {
            "delta": delta.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "affected_tasks": affected_tasks,
        }

    async def reorder(self, node_id: str, stage_ids: Sequence[str]):
        return await lc.reorder_stages(
            store=self.store, node_id=node_id, ordered_stage_ids=stage_ids
        )

    async def resume_from_stage(self, task_id: str, node_id: str) -> dict[str, Any]:
        return await resume_from_stage(
            store=self.store, sm=self.sm, task_id=task_id, node_id=node_id
        )

    # ------------------------------------------------------------------
    # 用例 API：查询
    # ------------------------------------------------------------------

    async def task_detail(self, task_id: str) -> dict[str, Any]:
        task = await self.store.tasks.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        stages = await self.store.tasks.list_stages(task_id)
        attempts = await self.store.tasks.list_attempts_for_task(task_id)
        artifacts = await self.store.artifacts.list_by_task(task_id)
        approvals = await self.store.approvals.list_for_task(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "stages": [s.model_dump(mode="json") for s in stages],
            "attempts": [a.model_dump(mode="json") for a in attempts],
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "kind": a.kind.value,
                    "summary": a.summary,
                    "summary_ok": a.summary_ok,
                    "covered_fields": a.covered_fields,
                    "size_bytes": a.size_bytes,
                    "sensitivity": a.sensitivity,
                    "producer": a.producer.model_dump(mode="json") if a.producer else None,
                }
                for a in artifacts
            ],
            "approvals": [a.model_dump(mode="json") for a in approvals],
        }

    async def node_queue(self, node_id: str) -> dict[str, Any]:
        """节点队列投影（RUN-04）：权威队列在 Task/TaskStage 表，这里是只读视图。"""
        pending = await self.store.tasks.list_stages_by_node(
            node_id, states=[_stage_waiting(), _stage_ready()]
        )
        running = await self.store.tasks.list_stages_by_node(
            node_id, states=[_stage_dispatching(), _stage_running(), _stage_awaiting()]
        )
        history = await self.store.tasks.list_all_stages_by_node(node_id, limit=100)
        return {
            "node_id": node_id,
            "pending": [s.model_dump(mode="json") for s in pending],
            "running": [s.model_dump(mode="json") for s in running],
            "history": [s.model_dump(mode="json") for s in history],
        }

    async def attention_items(self) -> dict[str, Any]:
        """「需处理」入口（OBS-05）：审批、失败、清理未完成、状态不明。"""
        approvals = await self.approvals.open_items()
        failed = await self.store.tasks.list_tasks(
            states=[_task_failed(), _task_blocked()], limit=100
        )
        lost = await self.store.tasks.list_stages_in_states([_stage_lost()])
        unresolved = await self.ledger.teardown_failed()
        return {
            "approvals": [a.model_dump(mode="json") for a in approvals],
            "failed_tasks": [t.model_dump(mode="json") for t in failed],
            "lost_stages": [s.model_dump(mode="json") for s in lost],
            "unresolved_resources": unresolved,
            "startup_notes": self.startup_notes,
        }

    async def storage_report(self) -> dict[str, Any]:
        if self.reaper is None:
            return await self.store.db.storage_report()
        return await self.reaper.storage_report()

    async def approve(
        self, approval_id: str, *, approve: bool, by: str = "user", modified_action: str | None = None
    ):
        return await self.approvals.decide(
            approval_id, approve=approve, by=by, modified_action=modified_action
        )


# ---------------------------------------------------------------------------
# 状态枚举的惰性引用（避免在模块顶部引入过多领域导入）
# ---------------------------------------------------------------------------


def _stage(state: str):
    from .core.domain.task import StageState

    return StageState(state)


def _task(state: str):
    from .core.domain.task import TaskState

    return TaskState(state)


def _stage_blocked():
    return _stage("blocked")


def _stage_ready():
    return _stage("ready")


def _stage_running():
    return _stage("running")


def _stage_dispatching():
    return _stage("dispatching")


def _stage_awaiting():
    return _stage("awaiting_approval")


def _stage_waiting():
    return _stage("waiting_deps")


def _stage_lost():
    return _stage("lost")


def _task_failed():
    return _task("failed")


def _task_blocked():
    return _task("blocked")


def _scope(name: str):
    from .data.event_log import EventScope

    return {
        "system": EventScope.SYSTEM,
        "session": EventScope.SESSION,
        "workflow": EventScope.WORKFLOW,
    }[name]


def _event(name: str):
    from .data.event_log import EventType

    return getattr(EventType, name)


def _actor(name: str):
    from .data.event_log import EventActor

    return EventActor(name)


class _UnavailableHarness:
    """适配层不可用时的替身。

    它**明确失败**而不是静默空转：派发一个任务会得到一条可读的原因，
    而不是永远停在 dispatching。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def create_session(self, **kwargs: Any) -> Any:
        from .adapters.sdk.protocol import AdapterError, ErrorCode

        raise AdapterError(
            ErrorCode.HARNESS_UNAVAILABLE, f"适配层不可用：{self.reason}"
        )

    async def capabilities(self, harness_id: str) -> Any:
        from .core.runtime.ports import SessionCaps

        return SessionCaps()

    async def session_alive(self, session_ref: str) -> bool:
        return False

    async def dispose(self, session_ref: str) -> None:
        return None

    async def terminate(self, session_ref: str, *, signal: str = "TERM") -> bool:
        return True

    async def abort_stream(self, session_ref: str) -> None:
        return None

    async def checkpoint(self, session_ref: str) -> str | None:
        return None

    async def pause(self, session_ref: str) -> bool:
        return False

    async def compact(self, session_ref: str, threshold: int | None) -> dict[str, Any]:
        return {"ok": False, "reason": "适配层不可用"}

    async def send_input(self, session_ref: str, text: str, *, kind: str = "user") -> bool:
        return False

    async def interrupt(self, session_ref: str) -> bool:
        return False

    async def resume_session(self, **kwargs: Any) -> Any:
        from .adapters.sdk.protocol import AdapterError, ErrorCode

        raise AdapterError(ErrorCode.NOT_SUPPORTED, "适配层不可用")
