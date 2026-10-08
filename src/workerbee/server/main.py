"""``workerbee-core`` 进程入口（架构设计 v0.02 UI-02）。

**默认只绑 127.0.0.1。** 开放局域网访问必须显式给 ``--allow-remote``，并在启动横幅
里看到一条醒目的警告——这是「默认安全」与「风险知情」两件事，不是一道开关。
访问令牌没有配置文件也会自动生成并打印一次；令牌只在启动横幅出现，
不会进任何响应体（AUTH-02）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

from ..app import Engine, EngineConfig
from ..security.secret_store import SecretStore
from .app import create_app
from .auth import TokenAuth

__all__ = ["build_parser", "run", "main"]

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workerbee-core",
        description="Workerbee 内核 + HTTP 网关（默认仅本机可访问）",
    )
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p.add_argument(
        "--data-dir", default=None, help="数据目录（默认 ~/.workerbee）"
    )
    p.add_argument("--workspace-dir", default=None, help="托管目录（资源清理的边界）")
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许非本机来源访问。开放前请确认网络环境可信——网关没有传输层加密",
    )
    p.add_argument(
        "--token",
        default=None,
        help="访问令牌。省略则取环境变量 WORKERBEE_TOKEN，再省略则随机生成并打印一次",
    )
    p.add_argument(
        "--passphrase",
        default=None,
        help="凭据库口令。省略则不加载凭据；引用凭据的节点会在派发时明确报错，"
        "而不是静默匿名运行",
    )
    p.add_argument("--poll-interval", type=float, default=1.0, help="调度轮询间隔（秒）")
    p.add_argument(
        "--use-supervisor",
        action="store_true",
        help="把 harness 子进程交给独立的 workerbee-supervisor 进程托管（生产建议开启）："
        "core 重启不打断在跑的会话。默认关闭，便于单进程开发",
    )
    p.add_argument(
        "--supervisor-socket",
        default=None,
        help="supervisor 的 Unix socket 路径，默认 <data-dir>/supervisor.sock",
    )
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.add_argument(
        "--no-reconcile",
        action="store_true",
        help="启动时不与既有状态对账（仅在明确知道残留会话可忽略时使用）",
    )
    return p


async def _bind_secrets_for_redaction(engine: Engine) -> int:
    """把凭据库里的全部密值登记进脱敏器，返回登记条数。

    组合根在解锁时**应当**已经做过这件事；这里再登记一次是幂等的，目的是让
    「凭据不进事件历史与产物」（AUTH-02）这条保证不依赖于上游那一步有没有做到。
    登记的对象沿用内核自己的脱敏器实例，产物侧的守卫因此同样受益。
    """
    redactor = getattr(engine, "_redactor", None)
    store = engine.secret_store
    if redactor is None or store is None or not hasattr(redactor, "bind_store"):
        return 0
    return int(await redactor.bind_store(store))


def _request_shutdown(server: object) -> None:
    """请求 uvicorn 优雅退出（在途请求跑完，不再接受新的）。"""
    setattr(server, "should_exit", True)


def _banner(host: str, port: str, token: str, generated: bool, allow_remote: bool) -> str:
    lines = [
        "",
        "  Workerbee 内核已启动",
        f"    界面/接口   http://{host}:{port}/",
        f"    接口文档     http://{host}:{port}/api/docs",
        f"    令牌         {token}" if not generated else f"    令牌（本次自动生成） {token}",
        "    令牌请通过 X-Workerbee-Token 头或 ?token= 查询参数携带；"
        "它不会出现在任何响应体里。",
    ]
    if allow_remote or host not in _LOOPBACK_HOSTS:
        lines += [
            "",
            "  " + "!" * 66,
            "  !!  注意：允许非本机访问（--allow-remote）。",
            "  !!  网关没有传输层加密，令牌以明文经过网络；任何能访问该端口的人",
            "  !!  都可能读取任务内容、调用工具并审批受保护操作。",
            "  !!  请只在可信网络中使用，或置于反向代理 + TLS 之后。",
            "  " + "!" * 66,
        ]
    lines.append("")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = EngineConfig(
        data_dir=data_dir,
        workspace_dir=Path(args.workspace_dir) if args.workspace_dir else None,
        passphrase=args.passphrase,
        poll_interval=args.poll_interval,
        use_supervisor=args.use_supervisor,
        supervisor_socket=Path(args.supervisor_socket) if args.supervisor_socket else None,
    )

    engine = await Engine.create(config)
    if args.passphrase:
        # 首次启动还没有库文件：显式建一个空库，而不是让「文件不存在」以异常形态冒出来
        vault = data_dir / "secrets.vault"
        if not vault.exists():
            await SecretStore.create(args.passphrase, vault)
            print(f"[security] 已新建空的凭据库：{vault}")

        # 口令错等失败如实上抛——带着未解锁的凭据库继续跑会让节点在派发时才失败
        await engine.unlock_secrets(args.passphrase)
        registered = await _bind_secrets_for_redaction(engine)
        locators = await engine.secret_store.list_locators()
        print(
            f"[security] 凭据库已解锁：{len(locators)} 条引用，"
            f"{registered} 条密值已登记用于脱敏"
        )
    else:
        engine.startup_notes.append(
            "未提供 --passphrase：本次不加载凭据库，引用凭据的节点会在派发时明确报错"
        )

    await engine.start(reconcile=not args.no_reconcile)

    # 会话托管方式决定「core 重启会不会打断在跑的任务」，值得在启动时说清楚（§3）
    if args.use_supervisor:
        print("[session] 会话由 supervisor 托管：core 重启不打断在跑的会话")
    else:
        print(
            "[session] 会话由 core 自身托管：core 重启会打断在跑的任务；"
            "生产部署请加 --use-supervisor"
        )
    for note in engine.startup_notes:
        print(f"[startup] {note}")

    token = args.token or os.environ.get("WORKERBEE_TOKEN") or ""
    app = create_app(engine, token=token, allow_remote=args.allow_remote)
    auth: TokenAuth = app.state.auth
    print(_banner(args.host, str(args.port), auth.token, auth.generated, args.allow_remote))

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            access_log=False,  # 访问日志会记录 ?token= 查询参数，默认关掉
        )
    )

    # 收到信号先让 uvicorn 停收新请求，再收束内核——顺序反了会让在途请求扑空
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, server)

    await server.serve()
    await engine.stop()
    print("[shutdown] 内核已收束，资源台账已对账")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.data_dir is None:
        from ..paths import default_data_dir, legacy_data_dir_notice

        notice = legacy_data_dir_notice()
        if notice:
            print(f"[升级提示] {notice}", file=sys.stderr)
        args.data_dir = str(default_data_dir())

    if args.host not in _LOOPBACK_HOSTS and not args.allow_remote:
        # 不静默放宽：要么显式表态，要么改回 loopback
        print(
            f"拒绝启动：--host {args.host} 不是本机地址。\n"
            f"  如果确实要开放访问，请加上 --allow-remote（并确认网络可信）。",
            file=sys.stderr,
        )
        return 2
    if args.allow_remote and args.host in _LOOPBACK_HOSTS:
        print("[security] --allow-remote 已开启：本机之外也能访问该端口，请确认网络可信")

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
