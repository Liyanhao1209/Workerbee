"""``workerbee serve`` / ``workerbee stop`` 端到端测试（v0.03 Phase 1 验收草案）。

对应验收：
- 重复执行识别已有实例，直接打印/打开带令牌的 URL，退出 0；
- Ctrl-C（SIGINT）core 干净收束且 supervisor 存活；
- ``workerbee stop`` 后无残留进程；
- 端口被非本实例占用时显式报错建议 --port；
- 查询类命令自动读令牌文件，不需要手动传 token。

全部用真实子进程跑真实的 core/supervisor——编排层的问题几乎都出在进程边界上，
用假件测 serve 等于没测。
"""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from workerbee.serve import lock_path, read_lock, socket_live, supervisor_socket_path

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _isolated_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("WORKERBEE_") and k != "VIRTUAL_ENV"
    }
    env["HOME"] = str(home)
    return env


def _wait_health(port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return False


def _spawn_serve(home: Path, cwd: Path, port: int, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "workerbee.cli", "serve",
            "--port", str(port), "--no-open", *extra,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(cwd),
        env=_isolated_env(home),
    )


def _run_cli(home: Path, cwd: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "workerbee.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_isolated_env(home),
        timeout=timeout,
    )


@pytest.fixture
def sandbox(tmp_path: Path):
    """隔离的 HOME（数据目录默认落在 ~/.workerbee）与 cwd（无旧版 ./.workerbee）。"""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return home, cwd


def test_serve_in_process_full_lifecycle(sandbox):
    home, cwd = sandbox
    data_dir = home / ".workerbee"
    port = _free_port()
    proc = _spawn_serve(home, cwd, port, "--in-process")
    try:
        assert _wait_health(port), f"core 未就绪：\n{_dump(proc)}"

        # 实例锁指向本进程
        lock = read_lock(data_dir)
        assert lock is not None and lock["pid"] == proc.pid and lock["port"] == port

        # 令牌文件落盘且 0600；用它访问数据端点
        token_file = data_dir / "token"
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        token = token_file.read_text().strip()
        assert token
        r = httpx.get(
            f"http://127.0.0.1:{port}/api/system/status",
            headers={"X-Workerbee-Token": token},
            timeout=5.0,
        )
        assert r.status_code == 200

        # 前端产物经源码布局回退被服务（开发态第三级）
        assert httpx.get(f"http://127.0.0.1:{port}/", timeout=5.0).status_code == 200

        # 重复执行：识别已有实例，打印带令牌的 URL 后退出 0
        again = _run_cli(home, cwd, "serve", "--port", str(port), "--no-open")
        assert again.returncode == 0, again.stdout
        assert "已在运行" in again.stdout
        assert f"?token={token}" in again.stdout

        # 查询类命令不传 token：自动读令牌文件
        status = _run_cli(home, cwd, "status", "--api", f"http://127.0.0.1:{port}")
        assert status.returncode == 0, status.stderr
        assert "version" in status.stdout, status.stdout

        # stop：core 优雅收束，锁清理，端口释放
        stopped = _run_cli(home, cwd, "stop")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        proc.wait(timeout=30)
        assert not lock_path(data_dir).exists()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _port_accepting(port):
            time.sleep(0.2)
        assert not _port_accepting(port)
    finally:
        proc.kill()
        proc.wait()


def test_serve_supervisor_survives_core_and_stop_cleans_all(sandbox):
    """默认模式：supervisor 被拉起；SIGINT 后 core 收束、supervisor 存活；
    workerbee stop 后两者都不留。"""
    home, cwd = sandbox
    data_dir = home / ".workerbee"
    sock = supervisor_socket_path(data_dir)
    port = _free_port()
    proc = _spawn_serve(home, cwd, port)
    try:
        assert _wait_health(port), f"core 未就绪：\n{_dump(proc)}"
        assert socket_live(sock), "supervisor socket 应已就绪"

        proc.send_signal(signal.SIGINT)  # 等价于 Ctrl-C
        proc.wait(timeout=30)
        assert not lock_path(data_dir).exists(), "core 退出应清理自己的锁"
        assert socket_live(sock), "core 退出后 supervisor 必须存活（下次启动复用）"

        stopped = _run_cli(home, cwd, "stop")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "supervisor：已停止" in stopped.stdout
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and socket_live(sock):
            time.sleep(0.2)
        assert not socket_live(sock), "stop 之后不得有残留 supervisor"
    finally:
        proc.kill()
        proc.wait()
        # 防线：用例失败也不许把 supervisor 孤儿留在机器上
        if socket_live(sock):
            from workerbee.serve import shutdown_supervisor

            shutdown_supervisor(sock, timeout=10.0)


def test_serve_port_conflict_is_explicit(sandbox):
    home, cwd = sandbox
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]

        result = _run_cli(
            home, cwd, "serve", "--port", str(port), "--no-open", "--in-process",
            timeout=60.0,
        )

    assert result.returncode == 2
    assert "--port" in result.stderr
    assert "已被占用" in result.stderr


def _port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _dump(proc: subprocess.Popen) -> str:
    proc.kill()
    out, _ = proc.communicate(timeout=10)
    return out or ""
