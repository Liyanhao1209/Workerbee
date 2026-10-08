"""serve/stop 编排层与 CLI 接线的单元测试。

重点覆盖「中间断线」风险点：CLI 声明的选项必须真的转发下去、令牌必须真的
从文件读到客户端、锁文件的三种状态必须分清——这些接线任何一处断掉，
两头的各自测试照样全绿。
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workerbee import serve as srv
from workerbee.cli import app
from workerbee.paths import (
    legacy_data_dir_notice,
    read_token_file,
    resolve_token,
    token_path,
    write_token_file,
)

pytestmark = pytest.mark.unit


def _dead_pid() -> int:
    """拿一个确定已退出且已回收的 pid。"""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ===========================================================================
# 令牌解析优先级
# ===========================================================================


def test_token_explicit_wins_over_env_and_file(tmp_path, monkeypatch):
    write_token_file(tmp_path, "file-token")
    monkeypatch.setenv("WORKERBEE_TOKEN", "env-token")

    token, source = resolve_token("--cli-token", tmp_path)

    assert token == "--cli-token"
    assert "--token" in source


def test_token_env_wins_over_file(tmp_path, monkeypatch):
    write_token_file(tmp_path, "file-token")
    monkeypatch.setenv("WORKERBEE_TOKEN", "env-token")

    token, source = resolve_token(None, tmp_path)

    assert token == "env-token"
    assert "WORKERBEE_TOKEN" in source


def test_token_file_wins_over_generate(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKERBEE_TOKEN", raising=False)
    write_token_file(tmp_path, "file-token")

    token, source = resolve_token(None, tmp_path)

    assert token == "file-token"
    assert "token" in source
    # 不得重新生成覆盖既有文件
    assert read_token_file(tmp_path) == "file-token"


def test_token_generated_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKERBEE_TOKEN", raising=False)

    token, source = resolve_token(None, tmp_path)

    assert token and "新生成" in source
    path = token_path(tmp_path)
    assert read_token_file(tmp_path) == token
    # 令牌与凭据同级：必须 0600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_write_tightens_permissions_of_existing_file(tmp_path):
    path = write_token_file(tmp_path, "first")
    os.chmod(path, 0o644)

    write_token_file(tmp_path, "second")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_token_file(tmp_path) == "second"


def test_read_token_file_tolerates_missing_and_empty(tmp_path):
    assert read_token_file(tmp_path) is None
    token_path(tmp_path).write_text("  \n")
    assert read_token_file(tmp_path) is None


# ===========================================================================
# 实例锁与探活
# ===========================================================================


def test_lock_round_trip(tmp_path):
    srv.write_lock(tmp_path, pid=1234, port=8765)

    lock = srv.read_lock(tmp_path)

    assert lock is not None
    assert lock["pid"] == 1234 and lock["port"] == 8765
    assert lock["started_at"]


def test_read_lock_tolerates_missing_and_corrupt(tmp_path):
    assert srv.read_lock(tmp_path) is None
    srv.lock_path(tmp_path).write_text("{not json")
    assert srv.read_lock(tmp_path) is None


def test_pid_alive_distinguishes_live_and_dead():
    assert srv.pid_alive(os.getpid()) is True
    assert srv.pid_alive(_dead_pid()) is False
    assert srv.pid_alive(0) is False
    assert srv.pid_alive(-1) is False


def test_probe_instance_without_lock_is_none(tmp_path):
    assert srv.probe_instance(tmp_path, "127.0.0.1") == ("none", None)


def test_probe_instance_stale_when_pid_dead(tmp_path):
    srv.write_lock(tmp_path, pid=_dead_pid(), port=8765)

    status, lock = srv.probe_instance(tmp_path, "127.0.0.1")

    assert status == "stale" and lock is not None


def test_probe_instance_running_requires_health(tmp_path, monkeypatch):
    """进程活着但健康探针不过，不算「已在运行」——pid 可能被无关进程复用。"""
    srv.write_lock(tmp_path, pid=os.getpid(), port=8765)

    monkeypatch.setattr(srv, "health_ok", lambda *a, **k: True)
    assert srv.probe_instance(tmp_path, "127.0.0.1")[0] == "running"

    monkeypatch.setattr(srv, "health_ok", lambda *a, **k: False)
    assert srv.probe_instance(tmp_path, "127.0.0.1")[0] == "conflict"


def test_remove_lock_only_removes_own(tmp_path):
    srv.write_lock(tmp_path, pid=os.getpid(), port=8765)
    srv.remove_lock(tmp_path, pid=os.getpid() + 1)
    assert srv.lock_path(tmp_path).exists(), "别人的锁不能动"
    srv.remove_lock(tmp_path, pid=os.getpid())
    assert not srv.lock_path(tmp_path).exists()


def test_port_accepting(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert srv.port_accepting("127.0.0.1", port) is True
    assert srv.port_accepting("127.0.0.1", port) is False


def test_wait_for_supervisor_times_out_explicitly(tmp_path):
    assert srv.wait_for_supervisor(tmp_path / "nope.sock", timeout=0.3) is False


# ===========================================================================
# run_serve 编排（外部效果全部用假件，专测中间接线）
# ===========================================================================


def _serve_kwargs(tmp_path: Path) -> dict:
    return {
        "data_dir": tmp_path,
        "host": "127.0.0.1",
        "port": 18765,
        "token": None,
        "passphrase": None,
        "in_process": False,
        "open_browser": False,
        "stop_supervisor_on_exit": False,
        "no_reconcile": False,
        "adapter_commands": None,
    }


def test_serve_reuses_running_instance(tmp_path, monkeypatch, capsys):
    """已有实例：打印带令牌的 URL 后直接退出，不重复启动。"""
    write_token_file(tmp_path, "reuse-token")
    monkeypatch.setattr(
        srv, "probe_instance",
        lambda *_a, **_k: ("running", {"pid": 4321, "port": 8765}),
    )

    rc = srv.run_serve(**_serve_kwargs(tmp_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert "已在运行" in out
    assert "http://127.0.0.1:8765/?token=reuse-token" in out


def test_serve_conflict_lock_is_explicit_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        srv, "probe_instance",
        lambda *_a, **_k: ("conflict", {"pid": 4321, "port": 8765}),
    )

    rc = srv.run_serve(**_serve_kwargs(tmp_path))

    err = capsys.readouterr().err
    assert rc == 2
    assert "4321" in err and "workerbee stop" in err


def test_serve_port_occupied_is_explicit_error(tmp_path, monkeypatch, capsys):
    """端口被非本实例占用：显式建议 --port，而不是裸 OSError。"""
    monkeypatch.setattr(srv, "port_accepting", lambda *_a, **_k: True)
    monkeypatch.setattr(srv, "health_ok", lambda *_a, **_k: False)

    rc = srv.run_serve(**_serve_kwargs(tmp_path))

    err = capsys.readouterr().err
    assert rc == 2
    assert "--port" in err and "已被占用" in err


def test_serve_supervisor_timeout_is_explicit_failure(tmp_path, monkeypatch, capsys):
    class _FakeProc:
        pid = 999
        terminated = False

        def terminate(self):
            self.terminated = True

    proc = _FakeProc()
    monkeypatch.setattr(srv, "socket_live", lambda *_a, **_k: False)
    monkeypatch.setattr(srv, "spawn_supervisor", lambda *_a, **_k: proc)
    monkeypatch.setattr(srv, "wait_for_supervisor", lambda *_a, **_k: False)
    monkeypatch.setattr(srv, "port_accepting", lambda *_a, **_k: False)

    rc = srv.run_serve(**_serve_kwargs(tmp_path))

    err = capsys.readouterr().err
    assert rc == 2
    assert proc.terminated, "起不来的 supervisor 不能留孤儿"
    assert "supervisor" in err and "就绪" in err


def test_serve_full_wiring_with_fakes(tmp_path, monkeypatch, capsys):
    """编排主链路：stale 锁清理 → 令牌文件 → supervisor 复用 → core argv → 锁随退出清理。

    core 入口用假件替换，但假件会**断言锁在运行期间存在**——这正是
    「锁写了没有、token 传了没有」这种中间接线最容易断的地方。
    """
    monkeypatch.delenv("WORKERBEE_TOKEN", raising=False)
    write_token_file(tmp_path, "wired-token")
    srv.write_lock(tmp_path, pid=_dead_pid(), port=18765)  # 陈旧锁，应被清理后继续

    monkeypatch.setattr(srv, "port_accepting", lambda *_a, **_k: False)
    monkeypatch.setattr(srv, "socket_live", lambda *_a, **_k: True)  # supervisor 已在跑

    seen: dict = {}

    def fake_core_main(argv):
        seen["argv"] = argv
        lock = srv.read_lock(tmp_path)
        assert lock is not None and lock["pid"] == os.getpid(), "core 运行期间实例锁必须在"
        return 0

    monkeypatch.setattr(importlib.import_module("workerbee.server.main"), "main", fake_core_main)

    rc = srv.run_serve(**_serve_kwargs(tmp_path))

    assert rc == 0
    argv = seen["argv"]
    assert "--use-supervisor" in argv
    assert "--supervisor-socket" in argv
    assert argv[argv.index("--token") + 1] == "wired-token"
    assert not srv.lock_path(tmp_path).exists(), "退出后锁必须清掉"
    out = capsys.readouterr().out
    assert "复用已在运行的 supervisor" in out
    assert "http://127.0.0.1:18765/?token=wired-token" in out


def test_serve_in_process_degrades_visibly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(srv, "port_accepting", lambda *_a, **_k: False)
    monkeypatch.setattr(importlib.import_module("workerbee.server.main"), "main", lambda argv: 0)
    spawned = []
    monkeypatch.setattr(srv, "spawn_supervisor", lambda *a, **k: spawned.append(1))

    kwargs = _serve_kwargs(tmp_path) | {"in_process": True, "token": "t"}
    rc = srv.run_serve(**kwargs)

    assert rc == 0
    assert not spawned, "--in-process 不得拉起 supervisor"
    assert "core 重启会打断在跑的任务" in capsys.readouterr().out


# ===========================================================================
# run_stop
# ===========================================================================


def test_stop_with_nothing_running(tmp_path, capsys):
    rc = srv.run_stop(data_dir=tmp_path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "core：未在运行" in out
    assert "supervisor：未在运行" in out


def test_stop_cleans_stale_lock(tmp_path, capsys):
    srv.write_lock(tmp_path, pid=_dead_pid(), port=8765)

    rc = srv.run_stop(data_dir=tmp_path)

    assert rc == 0
    assert not srv.lock_path(tmp_path).exists()
    assert "陈旧锁" in capsys.readouterr().out


def test_stop_terminates_live_core(tmp_path):
    # sleep 是**本测试的子进程**：被 SIGTERM 杀死后要先被 wait() 回收，
    # 否则它以僵尸形态存在，kill(pid,0) 仍判活。回收与轮询并发进行。
    import threading

    proc = subprocess.Popen(["sleep", "60"])
    srv.write_lock(tmp_path, pid=proc.pid, port=8765)
    results: list[int] = []
    t = threading.Thread(
        target=lambda: results.append(srv.run_stop(data_dir=tmp_path, timeout=10.0))
    )
    t.start()
    proc.wait()
    t.join()

    assert results == [0]
    assert not srv.lock_path(tmp_path).exists()


def test_stop_cleans_dead_supervisor_socket(tmp_path, capsys):
    sock = srv.supervisor_socket_path(tmp_path)
    sock.touch()

    rc = srv.run_stop(data_dir=tmp_path)

    assert rc == 0
    assert not sock.exists()
    assert "残留" in capsys.readouterr().out


# ===========================================================================
# 旧数据目录迁移提示
# ===========================================================================


def test_legacy_notice_when_old_dir_exists_and_default_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".workerbee").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    notice = legacy_data_dir_notice()

    assert notice is not None
    assert "mv" in notice and "--data-dir" in notice


def test_legacy_notice_silent_when_default_already_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".workerbee").mkdir()
    (tmp_path / "home" / ".workerbee").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert legacy_data_dir_notice() is None


def test_legacy_notice_silent_without_old_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert legacy_data_dir_notice() is None


# ===========================================================================
# static_dir 三级回退
# ===========================================================================


def test_static_dir_prefers_packaged_dist(tmp_path, monkeypatch):
    import workerbee.server.app as server_app

    fake_pkg = tmp_path / "workerbee"
    packaged = fake_pkg / "web" / "dist"
    packaged.mkdir(parents=True)
    (fake_pkg / "server").mkdir()
    monkeypatch.setattr(
        server_app, "__file__", str(fake_pkg / "server" / "app.py")
    )

    assert server_app.static_dir() == packaged


def test_static_dir_env_fallback(tmp_path, monkeypatch):
    import workerbee.server.app as server_app

    dist = tmp_path / "elsewhere" / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setenv("WORKERBEE_WEB_DIST", str(dist))
    # 包内不存在（开发仓库布局下 src/workerbee/web/dist 不存在）
    monkeypatch.setattr(
        server_app, "__file__", str(tmp_path / "workerbee" / "server" / "app.py")
    )

    assert server_app.static_dir() == dist


def test_static_dir_env_missing_dir_warns_and_falls_through(tmp_path, monkeypatch, capsys):
    import workerbee.server.app as server_app

    monkeypatch.setenv("WORKERBEE_WEB_DIST", str(tmp_path / "ghost"))
    # 伪造源码布局的深度：<root>/src/workerbee/server/app.py → 回退到 <root>/web/dist
    monkeypatch.setattr(
        server_app, "__file__", str(tmp_path / "src" / "workerbee" / "server" / "app.py")
    )

    result = server_app.static_dir()

    assert result == tmp_path / "web" / "dist"
    assert "WORKERBEE_WEB_DIST" in capsys.readouterr().err


# ===========================================================================
# CLI 接线：转发与令牌自动读取
# ===========================================================================


def _capture_core_main(monkeypatch) -> list[list[str]]:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        importlib.import_module("workerbee.server.main"),
        "main",
        lambda argv: captured.append(argv) or 0,
    )
    return captured


def test_cli_core_forwards_use_supervisor(monkeypatch, tmp_path):
    """回归：--use-supervisor 曾声明了却在构造 argv 时丢弃。"""
    captured = _capture_core_main(monkeypatch)

    result = CliRunner().invoke(
        app, ["core", "--data-dir", str(tmp_path), "--use-supervisor"]
    )

    assert result.exit_code == 0
    assert "--use-supervisor" in captured[0]


def test_cli_core_omits_use_supervisor_by_default(monkeypatch, tmp_path):
    captured = _capture_core_main(monkeypatch)

    CliRunner().invoke(app, ["core", "--data-dir", str(tmp_path)])

    assert "--use-supervisor" not in captured[0]


def test_cli_supervisor_forwards_socket_and_adapter_commands(monkeypatch, tmp_path):
    captured: list[list[str]] = []
    monkeypatch.setattr(
        importlib.import_module("workerbee.supervisor.main"),
        "main",
        lambda: captured.append(list(sys.argv)),
    )

    result = CliRunner().invoke(
        app,
        [
            "supervisor", "--data-dir", str(tmp_path),
            "--socket", "/tmp/custom.sock",
            "--adapter-commands", '{"mock":["python","-m","x"]}',
        ],
    )

    assert result.exit_code == 0
    argv = captured[0]
    assert "--socket" in argv and "/tmp/custom.sock" in argv
    assert "--adapter-commands" in argv and '{"mock":["python","-m","x"]}' in argv


def _capture_client(monkeypatch) -> list:
    import workerbee.client as client_mod

    captured = []

    class _FakeClient:
        def __init__(self, base_url, *, token=None, timeout=30.0):
            captured.append({"base_url": base_url, "token": token})

        def get(self, path, *, params=None):
            return {"ok": True}

    monkeypatch.setattr(client_mod, "ApiClient", _FakeClient)
    return captured


def test_query_commands_read_token_file_automatically(tmp_path, monkeypatch):
    """查询类命令按 --token > env > 令牌文件 取令牌，消灭手动传 token。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WORKERBEE_TOKEN", raising=False)
    write_token_file(tmp_path / ".workerbee", "auto-token")
    captured = _capture_client(monkeypatch)

    result = CliRunner().invoke(app, ["status", "--api", "http://127.0.0.1:1"])

    assert result.exit_code == 0
    assert captured[0]["token"] == "auto-token"


def test_query_commands_explicit_token_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKERBEE_TOKEN", "env-token")
    write_token_file(tmp_path / ".workerbee", "file-token")
    captured = _capture_client(monkeypatch)

    CliRunner().invoke(app, ["status", "--api", "http://127.0.0.1:1", "--token", "cli-token"])

    assert captured[0]["token"] == "cli-token"


def test_bare_workerbee_invokes_serve(monkeypatch):
    import workerbee.serve as serve_mod

    called: list[dict] = []
    monkeypatch.setattr(serve_mod, "run_serve", lambda **kwargs: called.append(kwargs) or 0)

    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert called, "裸 workerbee 必须等价于 workerbee serve"
    assert called[0]["in_process"] is False
