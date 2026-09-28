"""L4 内部的凭据兜底脱敏（架构设计 v0.02 §5.5、§9.1、AUTH-02）。

**本模块不替代 L5。** 权威的脱敏器属于 ``security/redaction.py``（出站与落库的统一
过滤）。这里只做本层的最后一道机器防线：L4 的框架自身 LLM 通路（摘要、AI 建图）与
上下文组装，必须在任何字符串离开本层之前确认其中不含凭据——异常消息、事件日志
payload、prompt 文本、ContextPackage 分区一视同仁。

口径与能力边界（如实声明）：
- **精确匹配**：已知的凭据值（``Secret``）整体替换为 ``<redacted:secret>``。
- **形态匹配**：常见密钥形态（``sk-`` 前缀、``Bearer`` 头、``AKIA``、JWT、
  ``api_key=`` / ``token:`` 键值对）替换为 ``<redacted:shape>``。
- 形态匹配**必然**有误报与漏报面，它是缓解而不是保证。真正的保证来自
  「凭据只以 ``CredentialRef.secret_locator`` 形式流动，本体不出 L5」。
- 有意**不**加入「长随机串一律脱敏」规则：那会把内容寻址的 sha256 digest（64 位
  hex）一起打掉，而 digest 是产物引用与历史回看的关键字段。宁可漏掉无前缀的随机串，
  也不破坏可追溯性——这条取舍必须显式记录，而不是藏在正则里。
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = ["REDACTED", "Secret", "redact_text", "scrub", "looks_secret_like"]

REDACTED = "<redacted:shape>"
_REDACTED_SECRET = "<redacted:secret>"


class Secret:
    """凭据持有者。只在出站请求（header／url）处 ``reveal()``。

    ``repr``/``str`` 一律脱敏，使得「把凭据对象顺手打进日志、异常或 prompt」
    在默认路径上不会泄露。这不是密码学保护，是让错误更容易被发现的工程约束。
    """

    __slots__ = ("_value", "_label")

    def __init__(self, value: str, *, label: str | None = None) -> None:
        self._value = value
        self._label = label

    def reveal(self) -> str:
        """唯一能拿到明文的入口。调用点应集中在传输层构造处。"""
        return self._value

    def present(self) -> bool:
        return bool(self._value)

    def label(self) -> str:
        """非敏感的定位信息（如 secret_locator），可用于日志。"""
        return self._label or "<unnamed>"

    def __repr__(self) -> str:
        return f"Secret(label={self._label!r}, value=<redacted>)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


#: 形态规则。顺序有意义：先具体（厂商前缀），后通用（键值对）。
_SHAPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # sk- / sk-ant- / rk- 等厂商前缀
    (re.compile(r"\b(?:sk|rk|pk|ghp|gho|xoxb|xoxp)[-_][A-Za-z0-9_\-]{8,}"), REDACTED),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), f"Bearer {REDACTED}"),
    # AWS access key id
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"), REDACTED),
    # JWT（三段式）
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), REDACTED),
    # 键值对形态：api_key=... / "token": "..." / password: ...
    # 键名与分隔符之间允许一个引号（JSON/YAML 写成 "api_key": "..."）。
    # 值里排除 <> ，使输出可重复脱敏而不再命中（幂等）。
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|"
            r"password|passwd|authorization|x-api-key)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',}<>]{8,})"
        ),
        rf"\1={REDACTED}",
    ),
)


def _known(values: Iterable[Secret | str] | None) -> list[str]:
    out: list[str] = []
    for v in values or ():
        text = v.reveal() if isinstance(v, Secret) else str(v)
        if text:
            out.append(text)
    return out


def scrub(
    text: str | None, secrets: Iterable[Secret | str] | None = None
) -> tuple[str, int]:
    """脱敏并返回 ``(文本, 命中次数)``。命中次数用于「不静默」地记录发生过脱敏。"""
    if not text:
        return ("" if text is None else text), 0

    hits = 0
    out = text
    # 精确值优先：已知凭据可能不含任何可识别前缀。
    for value in sorted(_known(secrets), key=len, reverse=True):
        if value and value in out:
            hits += out.count(value)
            out = out.replace(value, _REDACTED_SECRET)
    for pattern, repl in _SHAPE_RULES:
        out, n = pattern.subn(repl, out)
        hits += n
    return out, hits


def redact_text(text: str | None, secrets: Iterable[Secret | str] | None = None) -> str:
    """``scrub`` 的便捷形式：只要文本。"""
    return scrub(text, secrets)[0]


def looks_secret_like(text: str | None) -> bool:
    """是否检测到疑似凭据。用于在注入前给出可见提示，不作访问控制依据。"""
    if not text:
        return False
    return any(p.search(text) for p, _ in _SHAPE_RULES)
