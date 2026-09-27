"""API 网关（L6 后端）：HTTP + WebSocket 的访问层。

对外只有两件事：``create_app(engine, token=..., allow_remote=...)`` 组装应用，
``main()`` 是 ``workerbee-core`` 的进程入口。其余模块都是实现细节。

边界（UI-02）：默认只监听 loopback；除 ``/api/health`` 外全部端点需要访问令牌。
凭据只以引用（``CredentialRef.secret_locator``）出入，密钥本体不出 L5。
"""

from __future__ import annotations

from .app import create_app
from .auth import TokenAuth
from .main import main

__all__ = ["create_app", "main", "TokenAuth"]
