"""校验管线（架构设计 v0.02 §4.4 WF-05、§4.3 ACT-03）。

四种建图入口（手动、AI、Graph Capture、模板）的产物统一以草稿身份进入**同一条**
校验管线。本模块是纯逻辑：不碰数据库、不碰文件系统、不发网络请求，
外部世界通过 ``RegistryView`` 以只读方式提供。

三个校验档位：
- ``DRAFT``：只做结构性检查。草稿可保存不完整内容，但不伪装成可执行流程（WF-01）。
- ``PUBLISH``：全部静态检查。错误阻断发布，并把问题定位到节点／连线／配置。
- ``LAUNCH``：发布级 + 运行期就绪检查（有效入口、输入衔接、能力匹配）。

ACT-03 的判定规则（本实现给出的明确口径，供复核）：
    R-1 节点声明了 required_inputs，就等于选择了「可机器校验的契约机制」。
        某个必需输入在有效图上没有**已声明契约**的上游可提供它 → ERROR。
        用户可在 UI 上显式确认「以降级方式继续」，该确认落为 ContractWaiver，
        之后降级为 INFO 且被如实标注。不允许无提示地把 A 的输出当作 B 的结果。
    R-2 无论是否声明契约，只要有生效的**绕过边**（via 非空）进入某节点，
        该节点的输入来源已改变。声明了契约 ⇒ 由 R-1 给出 ERROR；
        未声明契约 ⇒ 给出 INFO 并如实标注「文本交接，无法机器校验」。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable, Protocol, Sequence

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.node import NodeDefinition
from ..domain.registry import CredentialRef, HarnessRegistration, SkillDoc, ToolSpec
from ..domain.workflow import GraphSpec
from .derive import EffectiveGraph, GraphDelta, derive, preview_toggle

__all__ = [
    "Severity",
    "ValidationMode",
    "Diagnostic",
    "ValidationReport",
    "RegistryView",
    "InMemoryRegistry",
    "validate",
    "validate_toggle",
    "topological_order",
]


class Severity(StrEnum):
    ERROR = "error"
    """阻断发布／发射。"""

    WARNING = "warning"
    """允许继续，但用户必须能看到。"""

    INFO = "info"
    """能力边界与如实标注，例如「文本交接，未做机器校验」。"""


class ValidationMode(StrEnum):
    DRAFT = "draft"
    PUBLISH = "publish"
    LAUNCH = "launch"


class Diagnostic(DomainModel):
    """一条可定位的校验结论。

    「错误应定位到节点、连线或配置」（WF-05）由 ``node_id`` / ``edge`` 承载。
    """

    code: str
    """稳定编号，供前端做交互与测试做断言。"""

    severity: Severity
    message: str

    node_id: str | None = None
    node_name: str | None = None
    edge: tuple[str, str] | None = None
    profile_id: str | None = None
    slot: str | None = None
    """配置落点，如 ``profiles[1].credential_ref``。"""

    hint: str | None = None
    """可执行的修复建议。"""

    requirement: str | None = None
    """关联的功能编号（WF-05 / ACT-03 / CFG-05 …），供需求映射核对。"""

    fix_action: str | None = None
    """前端可直接提供的修复动作标识，如 ``re_enable_node`` / ``waive_contract``。"""

    def location(self) -> str:
        bits: list[str] = []
        if self.node_name:
            bits.append(f"节点「{self.node_name}」")
        elif self.node_id:
            bits.append(f"节点 {self.node_id[:8]}")
        if self.edge:
            bits.append(f"连线 {self.edge[0][:8]}→{self.edge[1][:8]}")
        if self.slot:
            bits.append(self.slot)
        return " ".join(bits) or "（全局）"


class ValidationReport(DomainModel):
    mode: ValidationMode
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.ERROR]

    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.WARNING]

    def infos(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.INFO]

    def ok(self) -> bool:
        return not self.errors()

    def has_code(self, code: str) -> bool:
        return any(d.code == code for d in self.diagnostics)

    def extend(self, other: "ValidationReport") -> None:
        self.diagnostics.extend(other.diagnostics)

    def summary(self) -> str:
        if self.ok():
            tail = f"，{len(self.warnings())} 条警告" if self.warnings() else ""
            return f"{self.mode.value} 校验通过{tail}"
        return f"{self.mode.value} 校验失败：{len(self.errors())} 条错误"


class RegistryView(Protocol):
    """校验管线所需的只读注册表视图。实现方可以是内存表，也可以是数据库。"""

    def harness(self, harness_id: str) -> HarnessRegistration | None: ...

    def credential(self, credential_id: str) -> CredentialRef | None: ...

    def skill(self, skill_id: str) -> SkillDoc | None: ...

    def tool(self, tool_id: str) -> ToolSpec | None: ...


class InMemoryRegistry:
    """用于单测与离线校验的注册表视图。"""

    def __init__(
        self,
        harnesses: Iterable[HarnessRegistration] = (),
        credentials: Iterable[CredentialRef] = (),
        skills: Iterable[SkillDoc] = (),
        tools: Iterable[ToolSpec] = (),
    ) -> None:
        self._h = {h.harness_id: h for h in harnesses}
        self._c = {c.credential_id: c for c in credentials}
        self._s = {s.skill_id: s for s in skills}
        self._t = {t.tool_id: t for t in tools}

    def harness(self, harness_id: str) -> HarnessRegistration | None:
        return self._h.get(harness_id)

    def credential(self, credential_id: str) -> CredentialRef | None:
        return self._c.get(credential_id)

    def skill(self, skill_id: str) -> SkillDoc | None:
        return self._s.get(skill_id)

    def tool(self, tool_id: str) -> ToolSpec | None:
        return self._t.get(tool_id)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def validate(
    graph: GraphSpec,
    registry: RegistryView | None = None,
    *,
    mode: ValidationMode = ValidationMode.PUBLISH,
    enabled: set[str] | None = None,
) -> ValidationReport:
    """对定义图执行校验。

    :param enabled: 覆盖启用集（用于预览某次启停后的可执行性），None 取图自身状态。
    """
    registry = registry or InMemoryRegistry()
    report = ValidationReport(mode=mode)

    _check_structure(graph, report, mode)
    if report.errors():
        # 结构已坏（有环／悬空）时，后续以有效图为前提的检查没有意义。
        return report

    eff = derive(graph, enabled)
    _check_entry_exit(graph, eff, report, mode)
    _check_nodes(graph, eff, registry, report, mode)
    _check_contracts(graph, eff, report, mode)
    _check_bypass_annotations(graph, eff, report)
    return report


def validate_toggle(
    graph: GraphSpec,
    node_id: str,
    enable: bool,
    registry: RegistryView | None = None,
) -> tuple[GraphDelta, ValidationReport]:
    """ACT-02：启停操作前的「路径可见」预检。

    返回（拓扑差异，按拟议状态校验的报告）。**不修改任何状态**。
    """
    delta = preview_toggle(graph, node_id, enable)
    target = set(graph.enabled_node_ids())
    target.add(node_id) if enable else target.discard(node_id)
    report = validate(graph, registry, mode=ValidationMode.LAUNCH, enabled=target)
    return delta, report


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------


def topological_order(graph: GraphSpec) -> list[str] | None:
    """Kahn 拓扑序。含环时返回 None（调用方据此报 cycle）。"""
    indeg: dict[str, int] = {n.node_id: 0 for n in graph.nodes}
    adj: dict[str, list[str]] = {n.node_id: [] for n in graph.nodes}
    for e in graph.edges:
        if e.from_node not in indeg or e.to_node not in indeg:
            return None
        adj[e.from_node].append(e.to_node)
        indeg[e.to_node] += 1

    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(order) != len(graph.nodes):
        return None
    return order


def _check_structure(
    graph: GraphSpec, report: ValidationReport, mode: ValidationMode
) -> None:
    """结构检查。草稿档只降级不下达 ERROR。

    WF-01 明确「允许保留尚不完整的草稿」——用户在手动编辑途中画出环、或删掉
    一个节点导致连线悬空，属于**过程状态**，此时阻断保存会直接丢掉用户的编辑。
    因此草稿档把这些记为 WARNING 并如实展示，阻断只发生在发布与发射两档。
    """
    sev = Severity.WARNING if mode == ValidationMode.DRAFT else Severity.ERROR
    ids = {n.node_id for n in graph.nodes}
    name_of = {n.node_id: n.name for n in graph.nodes}

    for e in graph.edges:
        if e.from_node == e.to_node:
            report.diagnostics.append(
                Diagnostic(
                    code="self_loop",
                    severity=sev,
                    message=f"节点「{name_of.get(e.from_node, '?')}」存在自环",
                    node_id=e.from_node,
                    node_name=name_of.get(e.from_node),
                    edge=(e.from_node, e.to_node),
                    hint="删除这条指向自身的连线",
                    requirement="WF-05",
                )
            )
        if e.from_node not in ids or e.to_node not in ids:
            report.diagnostics.append(
                Diagnostic(
                    code="dangling_edge",
                    severity=sev,
                    message="连线引用了不存在的节点",
                    edge=(e.from_node, e.to_node),
                    hint="删除悬空连线或恢复被引用的节点",
                    requirement="WF-05",
                )
            )

    if report.has_code("self_loop") or report.has_code("dangling_edge"):
        return

    order = topological_order(graph)
    if order is None:
        cyc = _find_cycle(graph)
        names = " → ".join(name_of.get(n, n[:8]) for n in cyc)
        report.diagnostics.append(
            Diagnostic(
                code="cycle",
                severity=sev,
                message=f"定义图存在环：{names}",
                node_id=cyc[0] if cyc else None,
                node_name=name_of.get(cyc[0]) if cyc else None,
                hint="第一版采用无环依赖；请断开环中的一条连线",
                requirement="WF-05",
            )
        )


def _find_cycle(graph: GraphSpec) -> list[str]:
    """返回一条环路上的节点序列（含首尾重复的首节点），供 UI 高亮。"""
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.from_node, []).append(e.to_node)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n.node_id: WHITE for n in graph.nodes}
    parent: dict[str, str | None] = {}

    for root in [n.node_id for n in graph.nodes]:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        color[root] = GRAY
        parent[root] = None
        while stack:
            node, idx = stack[-1]
            children = adj.get(node, [])
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if color.get(child) == GRAY:
                    # 回溯出环
                    path = [child]
                    cur = node
                    while cur is not None and cur != child:
                        path.append(cur)
                        cur = parent.get(cur)
                    path.append(child)
                    path.reverse()
                    return path
                if color.get(child, WHITE) == WHITE:
                    color[child] = GRAY
                    parent[child] = node
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                stack.pop()
    return []


# --------------------------------------------------------------------------
# 入口 / 出口
# --------------------------------------------------------------------------


def _check_entry_exit(
    graph: GraphSpec,
    eff: EffectiveGraph,
    report: ValidationReport,
    mode: ValidationMode,
) -> None:
    if mode == ValidationMode.DRAFT:
        return

    sev = Severity.ERROR
    if eff.is_empty():
        report.diagnostics.append(
            Diagnostic(
                code="no_enabled_node",
                severity=sev,
                message="没有任何已启用的节点，无法执行",
                hint="至少启用一个节点；若确实想停用全部节点，请保留为草稿而非发布",
                requirement="ACT-02",
            )
        )
        return

    if not eff.entry_nodes():
        report.diagnostics.append(
            Diagnostic(
                code="no_entry",
                severity=sev,
                message="有效图没有入口（每个已启用节点都有有效上游），无法确定任务起点",
                hint="启用一个无有效上游的节点作为入口",
                requirement="WF-05",
            )
        )

    if not eff.exit_nodes():
        report.diagnostics.append(
            Diagnostic(
                code="no_exit",
                severity=sev,
                message="有效图没有出口，任务永远不会完成",
                hint="断开指向末节点的连线，或启用其下游节点",
                requirement="WF-05",
            )
        )


# --------------------------------------------------------------------------
# 节点可运行性
# --------------------------------------------------------------------------


def _check_nodes(
    graph: GraphSpec,
    eff: EffectiveGraph,
    registry: RegistryView,
    report: ValidationReport,
    mode: ValidationMode,
) -> None:
    structural_only = mode == ValidationMode.DRAFT

    for node_id in eff.node_ids:
        node = graph.require_node(node_id)
        _check_one_node(node, registry, report, structural_only, mode)

    # 停用节点保留配置与原始关系（ACT-01），因此不检查其候选完整性；
    # 但把「它被停用」如实告知，避免用户误以为它还在跑。
    for node in graph.nodes:
        if not node.enabled:
            report.diagnostics.append(
                Diagnostic(
                    code="node_disabled",
                    severity=Severity.INFO,
                    message=f"节点「{node.name}」已停用，不参与有效图",
                    node_id=node.node_id,
                    node_name=node.name,
                    hint="重新启用可精确恢复其原始依赖（ACT-01）",
                    requirement="ACT-01",
                )
            )


def _check_one_node(
    node: NodeDefinition,
    registry: RegistryView,
    report: ValidationReport,
    structural_only: bool,
    mode: ValidationMode,
) -> None:
    if not node.profiles:
        _add(
            report,
            structural_only,
            code="node_no_profile",
            message=f"节点「{node.name}」没有执行候选，无法运行",
            node=node,
            hint="为该节点至少添加一组候选（模型 + harness + 凭据）",
            requirement="CFG-01",
        )
        return

    seen_profile_ids: set[str] = set()
    for idx, profile in enumerate(node.profiles):
        slot_base = f"profiles[{idx}]"
        if profile.profile_id in seen_profile_ids:
            _add(
                report,
                structural_only,
                code="duplicate_profile_id",
                severity=Severity.ERROR,
                message=f"节点「{node.name}」存在重复的候选标识，会导致重排／删除时错配",
                node=node,
                slot=f"{slot_base}.profile_id",
                hint="重新生成该候选（CFG-01 要求候选有稳定对齐键）",
                requirement="CFG-01",
            )
        seen_profile_ids.add(profile.profile_id)

        if not profile.model_name:
            report.diagnostics.append(
                Diagnostic(
                    code="model_is_harness_default",
                    severity=Severity.INFO,
                    message=(
                        f"节点「{node.name}」的第 {idx + 1} 组候选未指定模型，"
                        f"将使用 harness「{profile.harness_ref or '?'}」的默认模型"
                    ),
                    node_id=node.node_id,
                    node_name=node.name,
                    slot=f"{slot_base}.model_name",
                    hint="实际使用的模型会在会话建立后如实记入执行尝试，可在任务详情里核对",
                    requirement="CFG-07",
                )
            )

        if not profile.harness_ref:
            _add(
                report,
                structural_only,
                code="profile_no_harness",
                message=f"节点「{node.name}」的第 {idx + 1} 组候选未指定 harness",
                node=node,
                slot=f"{slot_base}.harness_ref",
                hint="选择一台已登记的本机 harness",
                requirement="CFG-01",
            )
        else:
            _check_harness(profile, node, idx, registry, report, structural_only, mode)

        _check_credential(profile, node, idx, registry, report, structural_only)

    for ref in node.skill_refs:
        skill = registry.skill(ref.ref_id)
        if skill is None:
            _add(
                report,
                structural_only,
                code="skill_not_registered",
                message=f"节点「{node.name}」引用了不存在的 Skill：{ref.ref_id}",
                node=node,
                hint="从工具库中移除该引用，或先创建该 Skill",
                requirement="EXT-01",
            )
        elif not skill.enabled:
            _add(
                report,
                structural_only,
                code="skill_disabled",
                message=f"节点「{node.name}」引用了已停用的 Skill「{skill.name}」",
                node=node,
                severity=Severity.WARNING,
                hint="改用其他 Skill，或重新启用它",
                requirement="EXT-03",
            )

    for ref in node.tool_refs:
        tool = registry.tool(ref.ref_id)
        if tool is None:
            _add(
                report,
                structural_only,
                code="tool_not_registered",
                message=f"节点「{node.name}」引用了不存在的工具：{ref.ref_id}",
                node=node,
                hint="从工具库中移除该引用，或先登记该 MCP 工具",
                requirement="EXT-02",
            )
        elif not tool.enabled:
            _add(
                report,
                structural_only,
                code="tool_disabled",
                message=f"节点「{node.name}」引用了已停用的工具「{tool.name}」",
                node=node,
                severity=Severity.WARNING,
                hint="重新启用该工具，或改用其他工具",
                requirement="EXT-03",
            )


def _check_harness(
    profile,
    node: NodeDefinition,
    idx: int,
    registry: RegistryView,
    report: ValidationReport,
    structural_only: bool,
    mode: ValidationMode,
) -> None:
    slot = f"profiles[{idx}].harness_ref"
    reg = registry.harness(profile.harness_ref or "")
    if reg is None:
        _add(
            report,
            structural_only,
            code="harness_not_registered",
            message=f"节点「{node.name}」的第 {idx + 1} 组候选引用了未登记的 harness："
            f"{profile.harness_ref}",
            node=node,
            slot=slot,
            hint="先在本机接入该 harness（HAR-01），或改选已登记的 harness",
            requirement="HAR-01",
        )
        return

    if not reg.enabled:
        _add(
            report,
            structural_only,
            code="harness_disabled",
            message=f"节点「{node.name}」引用了已停用的 harness「{reg.name}」",
            node=node,
            slot=slot,
            hint="重新启用该 harness，或改选其他 harness",
            requirement="HAR-01",
        )

    caps = reg.capabilities_snapshot
    if caps is None:
        report.diagnostics.append(
            Diagnostic(
                code="capability_unprobed",
                severity=Severity.INFO,
                message=f"harness「{reg.name}」的能力尚未实测，无法预检兼容性",
                node_id=node.node_id,
                node_name=node.name,
                slot=slot,
                hint="执行一次能力探测（HAR-02）以获得准确的兼容性结论",
                requirement="HAR-02",
            )
        )
        return

    # CFG-04/05：要求自动整理，但 harness 不支持 —— 不能宣称已执行整理。
    if profile.compact_threshold is not None and caps.get("compact") is False:
        _add(
            report,
            structural_only,
            code="capability_missing",
            message=f"节点「{node.name}」设置了 compact 阈值，但 harness「{reg.name}」"
            f"不支持上下文整理",
            node=node,
            slot=f"profiles[{idx}].compact_threshold",
            hint="清除该阈值（节点将声明「不支持自动整理」），或改用支持整理的 harness",
            requirement="CFG-05",
        )

    # HUM-03：不支持权限钩子的适配器不得声称支持**非自动权限模式**。
    #
    # 这里刻意不是「一律拒绝」。清单的原话是「不得声称支持该权限模式」——
    # 也就是说：没有钩子的 harness 仍然可用，但用户必须**显式**选一个不会询问的
    # 权限模式。框架绝不替用户把它默认成「自动放行」：权限相关的事不做隐式默认。
    if caps.get("permission_hook") is False:
        _check_permission_mode(profile, node, idx, slot, reg, caps, report, structural_only)

    # CFG-02：不可用的 effort 取值必须提示，不能接受后静默忽略。
    #
    # 注意这两段**不属于**权限模式检查：它们对每个候选都要跑，不能被上面
    # 权限分支的提前 return 带走（那会让 CFG-02 与 HAR-01 悄悄失效）。
    efforts = caps.get("reasoning_efforts")
    if profile.reasoning_effort and isinstance(efforts, list) and efforts:
        if profile.reasoning_effort not in efforts:
            _add(
                report,
                structural_only,
                code="reasoning_effort_unsupported",
                message=f"harness「{reg.name}」不支持 reasoning_effort="
                f"{profile.reasoning_effort}（可选：{'、'.join(map(str, efforts))}）",
                node=node,
                slot=f"profiles[{idx}].reasoning_effort",
                hint="改选受支持的取值，或留空",
                requirement="CFG-02",
            )

    if mode == ValidationMode.LAUNCH and reg.last_probe_ok is False:
        _add(
            report,
            structural_only,
            code="harness_probe_failed",
            message=f"harness「{reg.name}」最近一次探测失败：{reg.last_probe_error or '原因未知'}",
            node=node,
            slot=slot,
            hint="先修复该 harness 的接入配置，或改选其他候选",
            requirement="HAR-01",
        )


def _check_permission_mode(
    profile,
    node: NodeDefinition,
    idx: int,
    slot: str,
    reg,
    caps: dict,
    report: ValidationReport,
    structural_only: bool,
) -> None:
    mode = profile.permission_mode
    non_interactive = list(caps.get("non_interactive_modes") or [])
    supported = list(caps.get("permission_modes") or [])

    if mode is None:
        _add(
            report,
            structural_only,
            code="permission_mode_unset",
            message=(
                f"harness「{reg.name}」没有权限钩子，框架无法代你拦截审批；"
                f"未指定权限模式时无法确定它会不会中途停下来等人"
            ),
            node=node,
            slot=slot,
            hint=(
                f"为该候选显式指定一个不会询问的模式（可选："
                f"{'、'.join(non_interactive) if non_interactive else '该 harness 未声明任何不询问的模式'}）；"
                f"框架不会替你默认放行"
            ),
            requirement="HUM-03",
        )
        return

    if supported and mode not in supported:
        _add(
            report,
            structural_only,
            code="permission_mode_unsupported",
            message=(
                f"harness「{reg.name}」不支持权限模式 {mode}"
                f"（可选：{'、'.join(supported)}）"
            ),
            node=node,
            slot=slot,
            hint="改选受支持的模式",
            requirement="HUM-03",
        )
        return

    if mode not in non_interactive:
        _add(
            report,
            structural_only,
            code="permission_mode_needs_hook",
            message=(
                f"权限模式 {mode} 会向用户请求授权，但 harness「{reg.name}」"
                f"没有权限钩子，框架无法接收并转达该请求"
            ),
            node=node,
            slot=slot,
            hint=(
                f"改用不会询问的模式（{'、'.join(non_interactive) if non_interactive else '该 harness 未声明'}），"
                f"或改用支持权限钩子的 harness"
            ),
            requirement="HUM-03",
        )
        return

    # 显式选了自动模式：允许执行，但「不支持审批」这件事必须对用户可见（HAR-02）。
    report.diagnostics.append(
        Diagnostic(
            code="approval_unavailable",
            severity=Severity.WARNING,
            message=(
                f"harness「{reg.name}」没有权限钩子，本节点上的人工审批不可用；"
                f"所有权限决定将由 harness 自身按模式 {mode} 处理"
            ),
            node_id=node.node_id,
            node_name=node.name,
            slot=slot,
            hint="这是如实声明的能力边界，不是错误；如需人工审批请改用支持权限钩子的 harness",
            requirement="HUM-03",
        )
    )

    # CFG-02 / HAR-01 的检查在 _check_harness 里，对每个候选无条件执行；
    # 本函数只负责 HUM-03 的权限模式分档。


def _check_credential(
    profile,
    node: NodeDefinition,
    idx: int,
    registry: RegistryView,
    report: ValidationReport,
    structural_only: bool,
) -> None:
    slot = f"profiles[{idx}].credential_ref"

    if not profile.credential_ref:
        # 允许：harness 使用本机已有登录态时框架不持有密钥（AUTH-01）。
        # 但必须让用户知道这一组候选没有框架托管的凭据。
        reg = registry.harness(profile.harness_ref or "")
        if reg is not None and reg.auth_binding:
            _add(
                report,
                structural_only,
                code="credential_missing",
                message=f"节点「{node.name}」的第 {idx + 1} 组候选未绑定凭据，"
                f"但 harness「{reg.name}」登记了凭据绑定",
                node=node,
                slot=slot,
                hint="为该候选选择一份凭据",
                requirement="AUTH-01",
            )
        return

    cred = registry.credential(profile.credential_ref)
    if cred is None:
        _add(
            report,
            structural_only,
            code="credential_not_registered",
            message=f"节点「{node.name}」引用了不存在的凭据：{profile.credential_ref}",
            node=node,
            slot=slot,
            hint="先在认证管理中创建该凭据，或改选其他凭据",
            requirement="AUTH-01",
        )
        return

    if cred.revoked:
        _add(
            report,
            structural_only,
            code="credential_revoked",
            message=f"节点「{node.name}」引用了已撤销的凭据「{cred.label}」",
            node=node,
            slot=slot,
            hint="重新绑定一份有效凭据；已撤销的访问权限不能被钉扎快照绕过",
            requirement="CFG-07",
        )


def _add(
    report: ValidationReport,
    downgrade_to_warning: bool,
    *,
    code: str,
    message: str,
    node: NodeDefinition,
    severity: Severity = Severity.ERROR,
    hint: str | None = None,
    slot: str | None = None,
    requirement: str | None = None,
) -> None:
    """草稿档位下不下达 ERROR——草稿允许不完整，但不允许被误认为可执行。"""
    if downgrade_to_warning and severity == Severity.ERROR:
        severity = Severity.WARNING
    report.diagnostics.append(
        Diagnostic(
            code=code,
            severity=severity,
            message=message,
            node_id=node.node_id,
            node_name=node.name,
            slot=slot,
            hint=hint,
            requirement=requirement,
        )
    )


# --------------------------------------------------------------------------
# ACT-03 输入衔接
# --------------------------------------------------------------------------


def _check_contracts(
    graph: GraphSpec,
    eff: EffectiveGraph,
    report: ValidationReport,
    mode: ValidationMode,
) -> None:
    if mode == ValidationMode.DRAFT:
        return

    for node_id in eff.node_ids:
        node = graph.require_node(node_id)
        if not node.required_inputs:
            continue

        provided: set[str] = set()
        for pred in eff.predecessors(node_id):
            edge = graph.edge(pred, node_id)
            if edge is not None and edge.output_contract is not None:
                provided |= set(edge.output_contract.outputs)

        for required in node.required_inputs:
            if required in provided:
                continue
            waiver = graph.waiver_for(node_id, required)
            if waiver is not None:
                report.diagnostics.append(
                    Diagnostic(
                        code="contract_waived",
                        severity=Severity.INFO,
                        message=f"节点「{node.name}」的必需输入「{required}」无契约上游，"
                        f"用户已确认以降级方式继续",
                        node_id=node_id,
                        node_name=node.name,
                        hint=waiver.reason or "该确认已记录在修订中",
                        requirement="ACT-03",
                    )
                )
                continue
            report.diagnostics.append(
                Diagnostic(
                    code="contract_unsatisfied",
                    severity=Severity.ERROR,
                    message=f"节点「{node.name}」需要输入「{required}」，但其有效上游的"
                    f"输出契约都不提供该字段",
                    node_id=node_id,
                    node_name=node.name,
                    hint="重新启用提供该产物的上游节点 / 修改本节点的输入要求 / "
                    "显式确认「以上游原始输出降级继续」",
                    requirement="ACT-03",
                    fix_action="waive_contract",
                )
            )


def _check_bypass_annotations(
    graph: GraphSpec, eff: EffectiveGraph, report: ValidationReport
) -> None:
    """如实标注被绕过的边（§4.3 第 3 条：未声明契约时这是能力边界）。"""
    name_of = {n.node_id: n.name for n in graph.nodes}
    for edge in eff.edges:
        if edge.is_direct():
            continue
        node = graph.node(edge.to_node)
        if node is None:
            continue
        bypassed = "、".join(f"「{name_of.get(n, n[:8])}」" for n in edge.via)
        upstream = name_of.get(edge.from_node, edge.from_node[:8])
        # 原路径上是否本来就声明了契约：决定这条 INFO 是「已被机器校验」还是
        # 「回退为文本交接、框架不做机器校验」。
        machine_checked = bool(node.required_inputs) or _path_has_contract(
            graph, edge.from_node, edge.via, edge.to_node
        )
        report.diagnostics.append(
            Diagnostic(
                code="bypass_changed_input" if machine_checked else "bypass_unverifiable",
                severity=Severity.INFO,
                message=(
                    f"节点「{node.name}」的输入来源已改变：原路径上的{bypassed}被停用，"
                    f"现直接取用「{upstream}」的输出"
                ),
                node_id=edge.to_node,
                node_name=node.name,
                edge=(edge.from_node, edge.to_node),
                hint=(
                    "该路径声明了输入要求或输出契约，缺失字段会单独报错"
                    if machine_checked
                    else "未声明输出契约的边回退为文本交接，框架不做机器校验；"
                    "如需自动校验，请为相关连线声明输出契约"
                ),
                requirement="ACT-03",
            )
        )


def _path_has_contract(
    graph: GraphSpec, from_node: str, via: Sequence[str], to_node: str
) -> bool:
    chain = [from_node, *via, to_node]
    for a, b in zip(chain, chain[1:]):
        e = graph.edge(a, b)
        if e is not None and e.output_contract is not None:
            return True
    return False
