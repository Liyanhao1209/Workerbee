"""`workerbee doctor` 的环境报告。

重点在它必须分清两件事：**你当前 shell 看到的**，和**守护进程实际会用的**。
真正拉起 harness 的是后者，而服务形态拿到的是 systemd 的默认 PATH，与 shell
的不是同一个。真出过这事：doctor 在终端里报「kimi 在 ~/.kimi-code/bin/kimi」，
用户以为一切就绪，装成服务后探测照样失败——两边输出长得几乎一样，无从反推。

所以 doctor 两套都报，并且只在**两者不一致**时才告警。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workerbee.cli import app

pytestmark = pytest.mark.unit


def _run(*args: str) -> str:
    result = CliRunner().invoke(app, ["doctor", *args])
    # doctor 在环境不满足时会以非零码退出，那是设计如此；这里只关心输出。
    return result.output


def _fake_which(monkeypatch, mapping: dict[str, str | None]) -> None:
    monkeypatch.setattr("workerbee.cli.shutil.which", lambda cmd: mapping.get(cmd))


def _fake_resolve(monkeypatch, mapping: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        "workerbee.cli.resolve_executable", lambda _path, cmd: mapping.get(cmd)
    )


# ===========================================================================
# 两套视角都要报
# ===========================================================================


def test_reports_both_shell_view_and_daemon_view(tmp_path, monkeypatch):
    """两个视角必须都出现——只报一个正是当初误导人的原因。"""
    _fake_which(monkeypatch, {"claude": "/home/u/.local/bin/claude", "kimi": None})
    _fake_resolve(monkeypatch, {"claude": "/home/u/.local/bin/claude", "kimi": "/home/u/.kimi-code/bin/kimi"})

    out = _run("--data-dir", str(tmp_path))

    assert "你当前 shell 看到的" in out
    assert "守护进程解析" in out
    assert "/home/u/.kimi-code/bin/kimi" in out


def test_no_warning_when_the_daemon_can_resolve_everything(tmp_path, monkeypatch):
    """守护进程找得到就不该告警——哪怕你的 shell 找不到。"""
    _fake_which(monkeypatch, {"claude": None, "kimi": None})
    _fake_resolve(monkeypatch, {"claude": "/opt/x/claude", "kimi": "/opt/x/kimi"})

    out = _run("--data-dir", str(tmp_path))

    assert "守护进程找不到" not in out


def test_warns_when_only_the_shell_can_find_a_harness(tmp_path, monkeypatch):
    """真正的缺口：shell 找得到、守护进程找不到。这时才需要提示两种处理方式。"""
    _fake_which(monkeypatch, {"claude": "/home/u/odd-place/claude", "kimi": None})
    _fake_resolve(monkeypatch, {"claude": None, "kimi": None})

    out = _run("--data-dir", str(tmp_path))

    assert "守护进程找不到" in out
    assert "/home/u/odd-place/claude" in out
    assert "可执行路径" in out and "Environment=PATH=" in out, "要给出两种可行的处理方式"


def test_missing_everywhere_is_reported_as_unavailable(tmp_path, monkeypatch):
    _fake_which(monkeypatch, {"claude": None, "kimi": None})
    _fake_resolve(monkeypatch, {"claude": None, "kimi": None})

    out = _run("--data-dir", str(tmp_path))

    assert "未找到" in out
    assert "守护进程找不到" not in out, "两边都没有就不是环境差异问题"


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
    assert "自动解析" in out, "空 exec_path 要说明它由适配器解析，而不是留白"
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
