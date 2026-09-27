"""出站脱敏与敏感级传播（架构设计 v0.02 §9.4、§5.4、§7）。

两个职责：

1. **敏感级传播**（:class:`SensitivityPropagator`）：产物沿血缘取**最高**级
   （public < internal < sensitive），跨 harness／厂商边界传递**前**判定是否
   需要脱敏（§9.4）。
2. **出站扫描**（:class:`OutboundScanner`）：把文本里认得出的凭据形态、私钥块、
   ``.env`` 赋值特征找出来并遮罩。

关于本模块能力的**诚实声明**（请连同代码一起读，不要只读函数名）：

- 这是**尽力而为的缓解措施（best-effort mitigation），不是保证**。
- 它**防不住 prompt injection**。注入是「恶意指令混进模型上下文」的问题，与
  「文本里有没有能被正则认出的密钥」是两件不同的事。本模块不做、也做不到指令
  与数据的隔离——那要靠 ContextPackage 分区（§7.3）与审批闸门（§9.2）。
- 正则只认得出**有固定形态**的凭据。自造格式的随机串、被 base64／URL 编码过的
  密钥、被拆行或同形字替换的凭据、无前缀的高熵 token，都会**漏过**。
- 因此它**不得**被当作唯一防线。真正的边界是「凭据本体只存于 Secret Store，
  其余位置只出现 credential_ref」（§9.1）；本模块只是最后一层兜底。
- 命中判定只看形态，**不看上下文**，因此会误报（把普通的长字符串当成密钥）。
  出站场景下误报的代价是可接受的。

本模块不 import ``data``／``adapters`` 的任何实现（§12 边界规则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Iterator

from ..core.domain.artifact import SENSITIVITY_RANK, max_sensitivity
from ..core.domain.edge import Sensitivity

__all__ = [
    "DEFAULT_MASK",
    "MIN_LITERAL_LENGTH",
    "FindingKind",
    "Severity",
    "Finding",
    "PatternRule",
    "PATTERN_RULES",
    "OutboundScanner",
    "scan",
    "scan_and_redact",
    "Boundary",
    "RedactionDecision",
    "SensitivityPropagator",
]


#: 遮罩文本。默认不保留任何原文片段——命中项的**值**在任何输出中都不出现。
DEFAULT_MASK = "***"

#: 低于该长度的「已知密钥值」不做子串替换，只在整串相等时替换。
#: 否则一个 1–2 字符的短口令会把正常文本打得千疮百孔；代价是短口令更依赖
#: 调用方在写入前就不要把值拼进自由文本。
MIN_LITERAL_LENGTH = 4


class FindingKind(StrEnum):
    """命中项的大类（任务口径：凭据形态 / 私钥块 / .env 内容特征）。"""

    CREDENTIAL = "credential"
    """有厂商前缀的凭据形态（sk- / xoxb- / ghp_ / AKIA / JWT / Bearer …）。"""

    PRIVATE_KEY = "private_key"
    """PEM 私钥块（含 OPENSSH / RSA / EC / DSA / PKCS8）。"""

    ENV_ASSIGNMENT = "env_assignment"
    """``NAME=value`` 且 NAME 含 KEY／TOKEN／SECRET／PASSWORD 一类词的赋值行。"""

    ASSIGNMENT = "assignment"
    """键名敏感但与 .env 无关的行内赋值（JSON／YAML／命令行 --token=…）。"""

    LITERAL = "literal"
    """调用方显式登记的已知密钥值。"""


class Severity(StrEnum):
    """仅用于排序与展示优先级，**不是**风险分类器（清单 §3.8 注记）。"""

    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class PatternRule:
    """一条扫描规则。

    ``replacement`` 为 None 时用扫描器的 mask；否则按 ``match.expand`` 展开，
    可保留非敏感的键名（例如 ``OPENAI_API_KEY=***``）。
    替换模板里**不得**引用命中值本身的分组。
    """

    name: str
    kind: FindingKind
    severity: Severity
    pattern: re.Pattern[str]
    replacement: str | None = None
    note: str = ""


def _rule(
    name: str,
    kind: FindingKind,
    severity: Severity,
    pattern: str,
    *,
    flags: int = 0,
    replacement: str | None = None,
    note: str = "",
) -> PatternRule:
    return PatternRule(
        name=name,
        kind=kind,
        severity=severity,
        pattern=re.compile(pattern, flags),
        replacement=replacement,
        note=note,
    )


#: 规则顺序只影响同一起点的重叠取舍（见 ``OutboundScanner._merged_spans``）。
PATTERN_RULES: tuple[PatternRule, ...] = (
    # ---- 私钥块 ----
    _rule(
        "private_key_header",
        FindingKind.PRIVATE_KEY,
        Severity.CRITICAL,
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----",
        note="只有头没有尾（被截断或只贴了一行）时兜底；完整的块由 "
        "_iter_private_key_spans 成对识别，整块会盖住这条命中。",
    ),
    # ---- 厂商前缀凭据 ----
    _rule(
        "sk_prefixed_key",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}",
        note="OpenAI（sk-、sk-proj-）与 Anthropic（sk-ant-）风格。",
    ),
    _rule(
        "slack_token",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}",
        note="xoxb / xoxp / xoxa / xoxr / xoxs。",
    ),
    _rule(
        "github_token",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})",
    ),
    _rule(
        "aws_access_key_id",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])",
        note="只覆盖 key id；secret access key 无固定形态，靠已知值替换兜底。",
    ),
    _rule(
        "google_api_key",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}",
    ),
    _rule(
        "jwt",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}",
        note="三段式 JWT：header.payload.signature。",
    ),
    _rule(
        "authorization_header",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?i)\b(?P<scheme>bearer|basic|token)\s+(?P<cred>[A-Za-z0-9\-._~+/=]{12,})",
        replacement=r"\g<scheme> ***",
        note="保留 scheme 便于定位，值整体遮罩。",
    ),
    # ---- .env 特征 ----
    _rule(
        "env_assignment",
        FindingKind.ENV_ASSIGNMENT,
        Severity.HIGH,
        r"(?im)^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*"
        r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|CREDS|AUTH)[A-Za-z0-9_]*)"
        r"[ \t]*=[ \t]*(?P<val>\"[^\"\n]*\"|'[^'\n]*'|[^\s#\n]{3,})[ \t]*$",
        replacement=r"\g<prefix>\g<key>=***",
        note=".env 的内容特征：整行、键名含敏感词、值非空。保留键名与 export 前缀。",
    ),
    # ---- 行内赋值 ----
    _rule(
        "inline_assignment",
        FindingKind.ASSIGNMENT,
        Severity.MEDIUM,
        r"(?i)(?P<key>[\"']?(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret"
        r"|access[_-]?token|auth[_-]?token|refresh[_-]?token|password|passwd|token)[\"']?"
        r"\s*[:=]\s*)(?P<val>\"[^\"\n]{6,}\"|'[^'\n]{6,}'|[^\s,;'\"\)\]}]{6,})",
        replacement=r"\g<key>***",
        note="JSON/YAML/命令行里的键值对。误报率最高的一条，故只给 MEDIUM。",
    ),
    _rule(
        "url_credentials",
        FindingKind.CREDENTIAL,
        Severity.HIGH,
        r"(?i)\b(?P<scheme>https?://)(?P<user>[^/\s:@]{1,64}):(?P<pw>[^/\s:@]{1,128})@",
        replacement=r"\g<scheme>***:***@",
        note="连接串里的 user:password@。",
    ),
)


# ---------------------------------------------------------------------------
# 命中项
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """一次命中。

    **刻意不携带任何命中值**：只有规则名、位置与长度。调用方自己手里本来就有
    原文（是他交给扫描器的），位置足够定位；把值带进 Finding 只会让凭据跟着
    报告流进日志与 UI（§9.1）。
    """

    rule: str
    kind: FindingKind
    severity: Severity
    start: int
    end: int
    line: int = 1

    @property
    def length(self) -> int:
        return self.end - self.start

    def describe(self) -> str:
        """可安全进日志／UI 的一行描述。不含任何原文。"""
        return (
            f"[{self.severity}] {self.rule}（{self.kind}）"
            f" 第 {self.line} 行 偏移 {self.start}–{self.end}，长度 {self.length}"
        )


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    rule: str
    kind: FindingKind
    severity: Severity
    replacement: str


_BEGIN_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_END_PRIVATE_KEY = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")


def _iter_private_key_spans(text: str) -> Iterator[tuple[int, int]]:
    """成对识别私钥块，返回 ``(start, end)`` 跨度。

    为什么不用一条 ``BEGIN.*?END`` 正则：在「很多 BEGIN 却没有 END」的输入上它会
    退化成二次复杂度（每个 BEGIN 都要把余下文本重扫一遍）。出站扫描面对的恰恰是
    可能被人塞东西的文本，这里不能留放大面。依据「BEGIN_i 之后找不到 END，则更靠
    后的 BEGIN 也必然找不到」一次线性推进；找不到配对的头交给 ``private_key_header``
    规则单行兜底。
    """
    cursor = 0
    while True:
        begin = _BEGIN_PRIVATE_KEY.search(text, cursor)
        if begin is None:
            return
        end = _END_PRIVATE_KEY.search(text, begin.end())
        if end is None:
            return
        yield (begin.start(), end.end())
        cursor = end.end()


def _safe_expand(match: re.Match[str], template: str | None, mask: str) -> str:
    """展开替换模板；模板有问题时**退回整段遮罩**（fail closed，绝不回吐原文）。"""
    if template is None:
        return mask
    try:
        return match.expand(template)
    except (re.error, IndexError):
        return mask


# ---------------------------------------------------------------------------
# 出站扫描
# ---------------------------------------------------------------------------


class OutboundScanner:
    """文本出站扫描与遮罩。

    用法::

        scanner = OutboundScanner()
        clean, findings = scanner.scan_and_redact(text)
        for f in findings:
            log(f.describe())     # 只输出规则与位置，不输出值

    能力边界见模块 docstring：这是尽力而为的缓解，不是保证，防不住 prompt injection。
    """

    def __init__(
        self,
        *,
        rules: Iterable[PatternRule] | None = None,
        mask: str = DEFAULT_MASK,
        known_values: Iterable[str] = (),
        min_literal_length: int = MIN_LITERAL_LENGTH,
    ) -> None:
        self.rules: tuple[PatternRule, ...] = (
            tuple(rules) if rules is not None else PATTERN_RULES
        )
        self.mask = mask
        self.min_literal_length = min_literal_length
        # 长的先找，避免短值把长值切碎。
        self._literals: tuple[str, ...] = tuple(
            sorted({v for v in known_values if isinstance(v, str) and v}, key=len, reverse=True)
        )

    # ---- 已知值 ----

    @property
    def known_value_count(self) -> int:
        return len(self._literals)

    def __repr__(self) -> str:  # 绝不显示已知值
        return (
            f"OutboundScanner(rules={len(self.rules)}, "
            f"known_values=<{len(self._literals)} 个值已隐藏>，mask={self.mask!r})"
        )

    # ---- 扫描 ----

    def scan(self, text: str) -> list[Finding]:
        """返回命中列表（按位置升序）。不修改文本。"""
        if not isinstance(text, str) or not text:
            return []
        return self._to_findings(text, self._merged_spans(text))

    def scan_and_redact(self, text: str) -> tuple[str, list[Finding]]:
        """返回 (遮罩后的文本, 命中列表)。两者由同一次扫描得出，必然一致。"""
        if not isinstance(text, str) or not text:
            return text, []
        spans = self._merged_spans(text)
        if not spans:
            return text, []

        out: list[str] = []
        cursor = 0
        for span in spans:
            out.append(text[cursor : span.start])
            out.append(span.replacement)
            cursor = span.end
        out.append(text[cursor:])
        return "".join(out), self._to_findings(text, spans)

    def redact(self, text: str) -> str:
        """只要遮罩后的文本。"""
        return self.scan_and_redact(text)[0]

    # ---- 内部 ----

    def _raw_spans(self, text: str) -> Iterator[_Span]:
        # 成对的私钥块不随 rules 关闭：它是「凭据任何情况下不出系统」的兜底，
        # 门槛比可裁剪的规则更高。
        for start, end in _iter_private_key_spans(text):
            yield _Span(
                start=start,
                end=end,
                rule="private_key_block",
                kind=FindingKind.PRIVATE_KEY,
                severity=Severity.CRITICAL,
                replacement=self.mask,
            )
        for rule in self.rules:
            for m in rule.pattern.finditer(text):
                if m.end() == m.start():
                    continue
                yield _Span(
                    start=m.start(),
                    end=m.end(),
                    rule=rule.name,
                    kind=rule.kind,
                    severity=rule.severity,
                    replacement=_safe_expand(m, rule.replacement, self.mask),
                )
        for value in self._literals:
            if len(value) < self.min_literal_length:
                # 短值只在整串相等时替换，避免把正常文本打碎。
                if text == value:
                    yield _Span(0, len(text), "known_literal", FindingKind.LITERAL,
                                Severity.CRITICAL, self.mask)
                continue
            at = text.find(value)
            while at != -1:
                yield _Span(at, at + len(value), "known_literal", FindingKind.LITERAL,
                            Severity.CRITICAL, self.mask)
                at = text.find(value, at + len(value))

    @staticmethod
    def _to_findings(text: str, spans: list[_Span]) -> list[Finding]:
        """跨度 → 命中项。行号增量推进：span 已按起点升序，整趟只扫一遍文本。

        逐条重新 ``text.count("\\n", 0, start)`` 会让总代价变成 O(文本 × 命中数)，
        在「一堆命中」的输入上又是一个放大面。
        """
        findings: list[Finding] = []
        scanned = 0
        newlines = 0
        for span in spans:
            newlines += text.count("\n", scanned, span.start)
            scanned = span.start
            findings.append(
                Finding(
                    rule=span.rule,
                    kind=span.kind,
                    severity=span.severity,
                    start=span.start,
                    end=span.end,
                    line=newlines + 1,
                )
            )
        return findings

    def _merged_spans(self, text: str) -> list[_Span]:
        """重叠合并：从最左开始，同起点取最长，其后被覆盖的区间整体丢弃。

        这样「私钥块」不会被「私钥头」切成两半，`KEY=sk-…` 也只会被遮罩一次。
        """
        spans = sorted(self._raw_spans(text), key=lambda s: (s.start, -s.end))
        merged: list[_Span] = []
        cursor = 0
        for span in spans:
            if span.start < cursor:
                continue
            merged.append(span)
            cursor = span.end
        return merged


_DEFAULT_SCANNER = OutboundScanner()


def scan(text: str) -> list[Finding]:
    """用默认规则集扫描文本（等价于 ``OutboundScanner().scan``）。"""
    return _DEFAULT_SCANNER.scan(text)


def scan_and_redact(text: str) -> tuple[str, list[Finding]]:
    """用默认规则集扫描并遮罩：``(clean_text, findings)``。"""
    return _DEFAULT_SCANNER.scan_and_redact(text)


# ---------------------------------------------------------------------------
# 敏感级传播与边界判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Boundary:
    """一次传递的端点标识。

    ``vendor`` 是**厂商**（适配器／供应商）维度，``harness_id`` 是**进程实例**维度。
    两者任一不同即视为跨越了边界：换 harness 就可能换进程、换日志落盘位置；
    换厂商则意味着数据出了原供应商的管辖范围（§9.4）。
    """

    harness_id: str
    vendor: str | None = None

    def identity(self) -> tuple[str, str]:
        return (self.harness_id, self.vendor or self.harness_id)

    @classmethod
    def from_registration(cls, registration: Any) -> Boundary:
        """从 ``HarnessRegistration`` 取标识；厂商维度用 ``adapter_id``。"""
        return cls(
            harness_id=str(getattr(registration, "harness_id", "")),
            vendor=str(getattr(registration, "adapter_id", "") or "") or None,
        )


@dataclass(frozen=True, slots=True)
class RedactionDecision:
    """跨边界传递前的判定结果。``required`` 为真表示必须先过出站脱敏。"""

    required: bool
    sensitivity: Sensitivity
    crosses_boundary: bool
    reason: str

    def __bool__(self) -> bool:  # 便于 `if decision:`
        return self.required


class SensitivityPropagator:
    """敏感级沿血缘取最高级，并判定跨边界时是否必须脱敏（§5.4、§9.4）。

    默认策略（可用 ``strict=True`` 收紧）：

    ============  ====================  ====================
    敏感级        跨厂商边界            仅跨 harness（同厂商）
    ============  ====================  ====================
    public        不脱敏                不脱敏
    internal      **必须脱敏**          不脱敏
    sensitive     **必须脱敏**          **必须脱敏**
    ============  ====================  ====================

    ``strict=True`` 时 internal 在仅跨 harness 时也要求脱敏。
    同一端点（harness 与厂商都相同）之间传递永不要求脱敏——那不是边界。

    这是**默认策略**而非硬约束：调用方可以只取 :meth:`max_of` 自行决定。
    """

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict

    # ---- 传播 ----

    @staticmethod
    def rank(sensitivity: str) -> int:
        """敏感级序。未知取值报错而不是静默当作 internal（报出而非隐藏）。"""
        if sensitivity not in SENSITIVITY_RANK:
            raise ValueError(
                f"未知的敏感级 {sensitivity!r}；只接受 {sorted(SENSITIVITY_RANK)}"
            )
        return SENSITIVITY_RANK[sensitivity]

    def max_of(self, values: Iterable[str | None]) -> Sensitivity:
        """一组敏感级取最高级。空集按 ``public`` 处理。"""
        known = [v for v in values if v is not None]
        for v in known:
            self.rank(v)
        return max_sensitivity(known) if known else "public"

    def effective(self, own: Sensitivity, lineage: Iterable[str | None] = ()) -> Sensitivity:
        """本产物与其血缘的合成敏感级：取最高级，只升不降。"""
        return self.max_of([own, *lineage])

    def inherit(self, own: Sensitivity, parents: Iterable[str | None]) -> Sensitivity:
        """``effective`` 的别名，语义是「从父产物继承」。"""
        return self.effective(own, parents)

    # ---- 边界 ----

    @staticmethod
    def crosses_boundary(source: Boundary, target: Boundary) -> bool:
        """是否跨越 harness 或厂商边界。同一端点之间不算跨边界。"""
        s_harness, s_vendor = source.identity()
        t_harness, t_vendor = target.identity()
        return s_harness != t_harness or s_vendor != t_vendor

    def requires_redaction(
        self, sensitivity: Sensitivity, source: Boundary, target: Boundary
    ) -> RedactionDecision:
        """该敏感级的产物能否不经脱敏地从 source 传到 target。"""
        self.rank(sensitivity)
        crosses = self.crosses_boundary(source, target)
        if not crosses:
            return RedactionDecision(
                required=False,
                sensitivity=sensitivity,
                crosses_boundary=False,
                reason="同一 harness/厂商边界内，不触发脱敏",
            )
        same_vendor = source.identity()[1] == target.identity()[1]
        if sensitivity == "public":
            return RedactionDecision(
                required=False,
                sensitivity=sensitivity,
                crosses_boundary=True,
                reason="public 产物跨边界不要求脱敏",
            )
        if sensitivity == "internal" and same_vendor and not self.strict:
            return RedactionDecision(
                required=False,
                sensitivity=sensitivity,
                crosses_boundary=True,
                reason="internal 且同厂商，仅跨 harness：默认策略不要求脱敏",
            )
        return RedactionDecision(
            required=True,
            sensitivity=sensitivity,
            crosses_boundary=True,
            reason=(
                f"{sensitivity} 产物跨越 "
                f"{'厂商' if not same_vendor else 'harness'} 边界，必须先过出站脱敏（§9.4）"
            ),
        )

    def decide(
        self,
        *,
        own: Sensitivity,
        lineage: Iterable[str | None] = (),
        source: Boundary,
        target: Boundary,
    ) -> RedactionDecision:
        """一次传递的完整判定：先沿血缘取最高级，再判是否要脱敏。"""
        return self.requires_redaction(self.effective(own, lineage), source, target)
