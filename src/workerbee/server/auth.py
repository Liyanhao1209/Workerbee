"""访问边界：访问令牌与 loopback 限制（架构设计 v0.02 §9.4、UI-02）。

**默认绑 loopback + 访问令牌。** 开放局域网访问需要显式配置并显示警告——
这两件事都在这里与 ``main.py`` 落地，而不是靠使用者记得加参数。

令牌的呈现方式：

- HTTP：``X-Workerbee-Token`` 头，或 ``?token=`` 查询参数（便于浏览器直接打开链接）；
- WebSocket：**只能用查询参数**——浏览器的 WebSocket API 不能自定义请求头。

不匹配时返回 401，响应体只说明「缺失或无效」，**不回显令牌**、不写进日志。
比较用 ``hmac.compare_digest``：令牌是机密，比较时间不应泄露出前缀长度。
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from typing import Any

__all__ = [
    "TOKEN_HEADER",
    "TOKEN_QUERY",
    "PUBLIC_PATHS",
    "TokenAuth",
    "is_loopback_client",
]

TOKEN_HEADER = "X-Workerbee-Token"
TOKEN_QUERY = "token"

#: 免鉴权路径。只有 health：它不含任何数据，只说明服务在。
#: 非 ``/api/`` 前缀的路径（前端静态产物）同样免鉴权——它们是公开的代码，
#: 不含任何任务数据；真正的数据全部在 ``/api`` 之下。
PUBLIC_PATHS: frozenset[str] = frozenset({"/api/health"})


class TokenAuth:
    """访问令牌校验。"""

    def __init__(self, token: str) -> None:
        # 空令牌等于关掉鉴权——这在默认绑 loopback 时才勉强可接受，
        # 因此这里直接生成一个随机令牌，让「忘记配令牌」不会变成「没有鉴权」。
        self._token = token or secrets.token_urlsafe(32)
        self.generated = not token

    @property
    def token(self) -> str:
        """当前令牌。仅供 ``main()`` 打印到启动横幅——不要放进任何响应体。"""
        return self._token

    # ---- 取值 ----

    @staticmethod
    def from_scope(scope: dict[str, Any]) -> str | None:
        """从 ASGI scope 里取出客户端出示的令牌（头优先，其次查询参数）。"""
        for raw_name, raw_value in scope.get("headers") or []:
            if raw_name.decode("latin-1").lower() == TOKEN_HEADER.lower():
                return raw_value.decode("latin-1")

        query = scope.get("query_string") or b""
        for pair in query.decode("latin-1").split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            if key == TOKEN_QUERY:
                from urllib.parse import unquote_plus

                return unquote_plus(value)
        return None

    # ---- 判定 ----

    def matches(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(presented, self._token)

    def allows_path(self, path: str) -> bool:
        """该路径是否免鉴权。"""
        if path in PUBLIC_PATHS:
            return True
        return not path.startswith("/api/")

    def authorize_scope(self, scope: dict[str, Any]) -> bool:
        path = scope.get("path", "")
        if self.allows_path(path):
            return True
        return self.matches(self.from_scope(scope))


def is_loopback_client(scope: dict[str, Any]) -> bool:
    """请求是否来自本机（UI-02 的默认边界）。

    无法判定来源时返回 False——**未知按「远端」处理**：宁可多要一次显式配置，
    也不要把一个说不清来源的连接当成自己人。
    """
    client = scope.get("client")
    if not client:
        return False
    host = client[0]
    if not host:
        return False
    if host in ("testclient", "localhost"):
        # 测试传输（in-process ASGI）与本地解析都不是网络来源，按本机处理
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
