"""HTTP 网关的组装（架构设计 v0.02 §9.4 UI-02、§9.2 OBS-02）。

三层结构，各管一件事：

1. **边界**（``auth.py`` + 本模块的中间件）：令牌与 loopback。放在中间件而不是
   逐个路由的依赖里——新增路由忘写依赖不会漏掉边界，这类错误在这里不可能发生。
2. **服务**（``services.py``）：API 独有的用例编排，路由的唯一去处。
3. **路由**（``routes/``）：参数解析 + 调服务 + 返回已声明的响应模型。

``response_model`` 是**契约的唯一来源**：前端只按 OpenAPI 写，形状对不上在服务端
就暴露（响应模型宽容，请求模型严格）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import __version__
from . import schemas as S
from .auth import TokenAuth, is_loopback_client
from .routes import ROUTERS
from .services import ServiceError, Services
from .ws import router as ws_router

__all__ = ["create_app", "static_dir", "explanation_page"]

#: 前端构建产物的默认位置：仓库根下的 ``web/dist``。
#: 网关只做托管，不参与构建——产物不存在时返回解释页而不是 404（UI-04）。
_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


def static_dir() -> Path:
    return _DIST


def create_app(engine: Any, *, token: str, allow_remote: bool = False) -> FastAPI:
    """组装网关。

    :param engine: 已组装的内核。**网关只调它的用例 API**，不直接碰任何内部部件。
    :param token: 访问令牌。传空字符串表示自动生成一个（见 :class:`TokenAuth`）。
    :param allow_remote: 是否允许非本机来源访问。默认否——开放局域网必须显式表态，
        且 ``main()`` 会为此打印醒目警告（UI-02）。
    """
    auth = TokenAuth(token)
    services = Services(engine)

    app = FastAPI(
        title="Workerbee API",
        version=__version__,
        description=(
            "本地多智能体工作流编排框架的网关。默认只监听 loopback，"
            "全部数据端点需要 X-Workerbee-Token 头或 ?token= 查询参数。"
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.state.services = services
    app.state.auth = auth
    app.state.allow_remote = allow_remote

    _install_handlers(app)
    _install_boundary(app, auth=auth, allow_remote=allow_remote)

    for router in ROUTERS:
        app.include_router(router)
    app.include_router(ws_router)

    _install_frontend(app)
    return app


# ---------------------------------------------------------------------------
# 异常 → 响应
# ---------------------------------------------------------------------------


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        """服务层的失败语义在这里一次性翻译成 HTTP。

        响应体只描述「哪里不对、怎么改」，**不回显令牌**，也不回显请求里的凭据值。
        """
        payload = exc.to_response().model_dump(mode="json")
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        # 内核用 ValueError 表达「这个操作对当前状态不成立」，语义上等于 400
        return JSONResponse(
            status_code=400,
            content=S.ErrorResponse(
                detail=str(exc) or "请求与当前状态不符", hint=None
            ).model_dump(mode="json"),
        )

    @app.exception_handler(KeyError)
    async def _key_error(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=S.ErrorResponse(
                detail=f"对象不存在: {exc}", hint=None
            ).model_dump(mode="json"),
        )


# ---------------------------------------------------------------------------
# 访问边界（UI-02）
# ---------------------------------------------------------------------------


def _install_boundary(app: FastAPI, *, auth: TokenAuth, allow_remote: bool) -> None:
    @app.middleware("http")
    async def _boundary(request: Request, call_next: Any) -> Any:
        if not auth.allows_path(request.url.path):
            if not is_loopback_client(request.scope) and not allow_remote:
                # 未知来源按远端处理：宁可多要一次显式配置，也不把说不清的连接当自己人
                return JSONResponse(
                    status_code=403,
                    content=S.ErrorResponse(
                        detail="网关默认只接受本机访问",
                        hint="若确实需要局域网访问，请以 --allow-remote 启动并自行承担风险",
                    ).model_dump(mode="json"),
                )
            if not auth.matches(TokenAuth.from_scope(request.scope)):
                return JSONResponse(
                    status_code=401,
                    content=S.ErrorResponse(
                        detail="令牌缺失或无效",
                        hint="请在 X-Workerbee-Token 头或 ?token= 查询参数中携带访问令牌",
                    ).model_dump(mode="json"),
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# 前端托管（UI-04）
# ---------------------------------------------------------------------------


def explanation_page(*, dist: Path) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Workerbee</title>
<style>
  body {{ font: 15px/1.7 system-ui, sans-serif; margin: 4rem auto; max-width: 44rem;
         color: #1c1e21; padding: 0 1.5rem; }}
  code {{ background: #f2f3f5; padding: .15em .4em; border-radius: 4px; }}
  pre {{ background: #f2f3f5; padding: 1rem; border-radius: 8px; overflow-x: auto; }}
</style></head>
<body>
<h1>Workerbee 内核正在运行</h1>
<p>这里本该是前端界面，但构建产物不存在：<code>{dist}</code></p>
<p>网关不参与前端构建。要看到界面，请在仓库根目录执行：</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>构建完成后刷新本页即可。</p>
<p>接口契约（OpenAPI）：<a href="/api/docs">/api/docs</a>；
健康探针：<a href="/api/health">/api/health</a>。
除 <code>/api/health</code> 外的全部数据端点都需要访问令牌
（<code>X-Workerbee-Token</code> 头或 <code>?token=</code> 查询参数）。</p>
</body></html>"""
    )


def _install_frontend(app: FastAPI) -> None:
    dist = static_dir()

    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        # 挂在最后：/api/* 先匹配，其余交给前端（html=True 支持前端路由直接刷新）
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
        return

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _frontend_missing() -> HTMLResponse:
        return explanation_page(dist=dist)
