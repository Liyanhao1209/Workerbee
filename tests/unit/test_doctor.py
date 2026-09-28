"""`workerbee doctor` 的环境报告。

重点在**它必须说破的那个陷阱**：doctor 查的是当前 shell 的 PATH，而真正拉起
harness 的是守护进程，后者不继承登录 shell 的 PATH。claude 常装在 ~/.nvm/...、
kimi 常装在 ~/.kimi-code/bin，都不在 systemd 的默认 PATH 里。

真出过这事：`workerbee doctor` 在终端里报「kimi /home/.../.kimi-code/bin/kimi」，
用户以为一切就绪，装成服务后探测照样失败——因为 supervisor 看不到那个目录。
两边查的根本不是同一个环境，光看输出无从反推。所以 doctor 要主动提示。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workerbee.cli import _in_default_service_path, app

pytestmark = pytest.mark.unit


def _run(*args: str) -> str:
    result = CliRunner().invoke(app, ["doctor", *args])
    # doctor 在环境不满足时会以非零码退出，那是设计如此；这里只关心输出。
    return result.output


# ===========================================================================
# 判定本身
# ===========================================================================


def test_paths_inside_the_service_path_are_recognized():
    assert _in_default_service_path("/usr/bin/git")
    assert _in_default_service_path("/usr/local/bin/claude")
    assert _in_default_service_path("/usr/sbin/whatever")


def test_paths_outside_the_service_path_are_recognized():
    """用户自己装的 harness 基本都落在这些位置。"""
    assert not _in_default_service_path("/home/u/.kimi-code/bin/kimi")
    assert not _in_default_service_path("/home/u/.nvm/versions/node/v22.23.2/bin/claude")
    assert not _in_default_service_path("/home/u/.local/bin/claude")
    # 前缀相同但目录不同，不能误判成「在里面」
    assert not _in_default_service_path("/usr/local/bin/../home/u/claude")


# ===========================================================================
# 输出
# ===========================================================================


def test_warns_when_a_harness_is_outside_the_service_path(tmp_path, monkeypatch):
    """hook 出来的路径在默认 PATH 之外时，必须给出提示与两种处理方式。"""
    monkeypatch.setattr(
        "workerbee.cli.shutil.which",
        lambda cmd: f"/home/u/.kimi-code/bin/{cmd}" if cmd == "kimi" else None,
    )

    out = _run("--data-dir", str(tmp_path))

    assert "守护进程默认找不到" in out
    assert "/home/u/.kimi-code/bin/kimi" in out
    assert "可执行路径" in out and "Environment=PATH=" in out, "要给出两种可行的处理方式"


def test_no_warning_when_everything_is_inside_the_service_path(tmp_path, monkeypatch):
    """都在默认 PATH 里时不该平白吓唬人。"""
    monkeypatch.setattr(
        "workerbee.cli.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}",
    )

    out = _run("--data-dir", str(tmp_path))

    assert "守护进程默认找不到" not in out


def test_missing_harness_is_reported_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("workerbee.cli.shutil.which", lambda _cmd: None)

    out = _run("--data-dir", str(tmp_path))

    assert "未找到" in out
    assert "守护进程默认找不到" not in out, "根本没找到就不是 PATH 差异问题"


# ===========================================================================
# 注册表一览
# ===========================================================================


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / "workerbee.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE harness_registration ("
            " harness_id TEXT, exec_path TEXT, last_probe_ok INTEGER, last_probe_error TEXT)"
        )
        conn.executemany(
            "INSERT INTO harness_registration VALUES (?,?,?,?)", rows
        )
    return db


def test_registrations_are_listed_with_probe_result(tmp_path, monkeypatch):
    """把「注册了什么、探测成没成」摆出来——这是排障时最先要看的两列。"""
    monkeypatch.setattr("workerbee.cli.shutil.which", lambda _cmd: None)
    _make_db(
        tmp_path,
        [
            ("kimi", None, 0, "PATH 中找不到 kimi。请在 HarnessRegistration.exec_path 里指定绝对路径"),
            ("claude", "/opt/bin/claude", 1, None),
        ],
    )

    out = _run("--data-dir", str(tmp_path))

    assert "已登记的 harness" in out
    assert "跟随 PATH" in out, "空 exec_path 要说明它走 PATH 查找"
    assert "/opt/bin/claude" in out
    assert "失败：" in out and "PATH 中找不到 kimi" in out


def test_registry_section_degrades_when_there_is_no_database(tmp_path, monkeypatch):
    """数据库还没建时如实说读不到，而不是当成「一个都没登记」。"""
    monkeypatch.setattr("workerbee.cli.shutil.which", lambda _cmd: None)

    out = _run("--data-dir", str(tmp_path / "nonexistent"))

    assert "读不到注册表" in out


def test_empty_registry_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("workerbee.cli.shutil.which", lambda _cmd: None)
    _make_db(tmp_path, [])

    out = _run("--data-dir", str(tmp_path))

    assert "尚未登记任何 harness" in out
