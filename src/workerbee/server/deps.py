"""路由依赖：把已经组装好的 Engine／Services 交给路由，并守住访问边界（UI-02）。

依赖只做**取用**与**放行判定**，不含业务逻辑——业务逻辑要么在内核里，
要么在 ``services.py`` 里。路由函数因此可以只写「解析参数、调用服务、返回模型」。

鉴权与 loopback 限制走中间件而不是依赖：新增一个路由不写依赖也不会漏掉边界，
「忘记加装饰器」这种错误在这里不可能发生。WebSocket 的 scope 不被 HTTP 中间件
覆盖，故 ``ws.py`` 自行调用 :meth:`TokenAuth.authorize_scope`。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request, WebSocket

from .auth import TokenAuth
from .services import Services

__all__ = [
    "get_services",
    "get_engine",
    "get_auth",
    "allow_remote",
    "ws_state",
    "ServicesDep",
]


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_engine(request: Request) -> Any:
    return request.app.state.engine


def get_auth(request: Request) -> TokenAuth:
    return request.app.state.auth


def allow_remote(request: Request) -> bool:
    return bool(request.app.state.allow_remote)


def ws_state(websocket: WebSocket) -> tuple[Any, Services, TokenAuth]:
    """WebSocket 侧一次取齐（Engine、服务容器、鉴权器）。"""
    state = websocket.app.state
    return state.engine, state.services, state.auth


#: 路由签名里统一用这个：``async def handler(services: ServicesDep, ...)``。
ServicesDep = Annotated[Services, Depends(get_services)]
