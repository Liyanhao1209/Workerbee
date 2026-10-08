"""`workerbee-supervisor` 进程入口。

由 OS 级守护拉起（systemd user service / launchd / Windows Service，见 D-13）。
它自己崩溃时由守护重启，重启后靠 `supervisor.db` 的会话台账 + 适配器存活探测对账——
**不假设上一次的自己在内存里留下了什么**。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path

from .server import Supervisor


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workerbee-supervisor",
        description="Workerbee Session 托管进程：持有 harness 子进程，跨 core 重启存活",
    )
    p.add_argument(
        "--data-dir", default=None, help="数据目录（与 core 共用，默认 ~/.workerbee）"
    )
    p.add_argument("--socket", default=None, help="Unix socket 路径，默认 <data-dir>/supervisor.sock")
    p.add_argument(
        "--adapter-commands",
        default=None,
        help='adapter_id → 命令的 JSON，例如 \'{"mock":["python","-m","workerbee.adapters.mock.main"]}\'',
    )
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return p


async def run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    commands = json.loads(args.adapter_commands) if args.adapter_commands else {}

    supervisor = Supervisor(
        data_dir=data_dir,
        socket_path=Path(args.socket) if args.socket else None,
        adapter_commands=commands,
    )
    await supervisor.start()

    stop_event = asyncio.Event()

    def _signal(signum: int) -> None:
        print(f"[supervisor] 收到信号 {signum}，正在收束…", file=sys.stderr)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal, sig)

    # 两条退出路径：信号（systemd/Ctrl-C），或 socket 上的 shutdown 命令
    # （workerbee stop 走这条）。后者完成的是 Supervisor.stop() 本身，
    # 进程要真正退出还得靠这里等到它。
    rpc_stopped = asyncio.create_task(supervisor.wait_stopped())
    signal_stop = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {rpc_stopped, signal_stop}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    if rpc_stopped in done and not signal_stop.done():
        print("[supervisor] 收到 shutdown 命令，正在收束…", file=sys.stderr)
    await supervisor.stop()
    return 0


def main() -> None:
    args = build_parser().parse_args()
    if args.data_dir is None:
        from ..paths import default_data_dir, legacy_data_dir_notice

        notice = legacy_data_dir_notice()
        if notice:
            print(f"[升级提示] {notice}", file=sys.stderr)
        args.data_dir = str(default_data_dir())
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
