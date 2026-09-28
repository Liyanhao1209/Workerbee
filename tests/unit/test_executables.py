"""harness 可执行文件的定位。

这批测试守的是一个真实故障：把内核装成服务后，守护进程拿到的是 systemd 的
默认 PATH，而 claude / kimi 这类用户自装的工具基本都在它之外。于是
``workerbee doctor`` 在终端里说「找得到」，探测却失败，而两边的输出都不足以
让人看出这是环境差异。修法是解析时不只查 PATH，还查一组约定俗成的安装位置。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workerbee.adapters.sdk import executables
from workerbee.adapters.sdk.cli import find_executable
from workerbee.adapters.sdk.protocol import AdapterError, ErrorCode

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_dirs(tmp_path, monkeypatch):
    """把搜索目录换成受控的临时目录，测试不去碰真实的家目录。"""
    bin_a = tmp_path / "a" / "bin"
    bin_b = tmp_path / "b" / "bin"
    for d in (bin_a, bin_b):
        d.mkdir(parents=True)

    def _make(directory: Path, name: str) -> Path:
        exe = directory / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return exe

    monkeypatch.setattr(executables, "SEARCH_DIRS", (str(bin_a), str(bin_b)))
    monkeypatch.setattr(executables, "SEARCH_GLOBS", ())
    return _make, bin_a, bin_b


# ===========================================================================
# 解析顺序
# ===========================================================================


def test_path_wins_over_search_dirs(monkeypatch, fake_dirs):
    """PATH 命中说明用户显式配置过，应当优先——不要去猜他装在哪。"""
    _make, bin_a, _ = fake_dirs
    _make(bin_a, "claude")
    monkeypatch.setattr(
        "workerbee.adapters.sdk.executables.shutil.which",
        lambda _n: "/usr/bin/claude",
    )

    assert executables.resolve(None, "claude") == "/usr/bin/claude"


def test_falls_back_to_a_search_dir_when_path_misses(monkeypatch, fake_dirs):
    """守护进程的 PATH 里没有，但装在了约定俗成的位置——这种情况必须能找到。"""
    _make, bin_a, _ = fake_dirs
    expected = _make(bin_a, "kimi")
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    assert executables.resolve(None, "kimi") == str(expected)


def test_later_search_dir_is_used_when_earlier_ones_lack_it(monkeypatch, fake_dirs):
    _make, _, bin_b = fake_dirs
    expected = _make(bin_b, "claude")
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    assert executables.resolve(None, "claude") == str(expected)


def test_explicit_exec_path_is_returned_verbatim(tmp_path, monkeypatch):
    exe = tmp_path / "my-claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: "/usr/bin/other")

    assert executables.resolve(str(exe), "claude") == str(exe)


def test_missing_absolute_path_resolves_to_none(tmp_path, monkeypatch):
    """显式指定的路径不存在时**不**回落到搜索——那会悄悄用了别的 harness。"""
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    assert executables.resolve(str(tmp_path / "nope"), "claude") is None


def test_non_executable_file_is_not_accepted(monkeypatch, fake_dirs):
    """存在但不可执行的文件不算数——否则会在 spawn 时炸成另一个错误。"""
    _make, bin_a, _ = fake_dirs
    plain = bin_a / "claude"
    plain.write_text("not executable")
    plain.chmod(0o644)
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    assert executables.resolve(None, "claude") is None


def test_nothing_anywhere_resolves_to_none(monkeypatch, fake_dirs):
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    assert executables.resolve(None, "nonexistent-harness") is None


# ===========================================================================
# 版本目录排序
# ===========================================================================


def test_version_dirs_are_ordered_numerically_not_lexically(tmp_path, monkeypatch):
    """装了多个 node 时优先用新的。

    字符串排序在这里是错的：``v9`` 会排在 ``v22`` 后面，于是选中旧版本。
    """
    root = tmp_path / "versions" / "node"
    for version in ("v9.11.2", "v22.23.2", "v20.11.1"):
        d = root / version / "bin"
        d.mkdir(parents=True)
        exe = d / "claude"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

    monkeypatch.setattr(executables, "SEARCH_DIRS", ())
    monkeypatch.setattr(executables, "SEARCH_GLOBS", (str(root / "*" / "bin"),))
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    resolved = executables.resolve(None, "claude")

    assert resolved is not None
    assert "v22.23.2" in resolved, f"应选版本最高的，实际选了 {resolved}"


def test_search_dirs_skips_nonexistent_entries(monkeypatch, tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    monkeypatch.setattr(
        executables, "SEARCH_DIRS", (str(present), str(tmp_path / "absent"))
    )
    monkeypatch.setattr(executables, "SEARCH_GLOBS", ())

    assert executables.search_dirs() == [present]


# ===========================================================================
# 报错信息
# ===========================================================================


def test_find_executable_raises_with_the_searched_locations(monkeypatch, fake_dirs):
    """找不到时的报错要能让人知道「都找过哪儿了」，否则无从下手。"""
    _make, bin_a, _ = fake_dirs
    monkeypatch.setattr("workerbee.adapters.sdk.executables.shutil.which", lambda _n: None)

    with pytest.raises(AdapterError) as excinfo:
        find_executable(None, "claude")

    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "exec_path" in excinfo.value.message
    searched = excinfo.value.data["searched"]
    assert str(bin_a) in searched, "应报出实际搜过的目录"


def test_find_executable_reports_a_missing_explicit_path(tmp_path):
    with pytest.raises(AdapterError) as excinfo:
        find_executable(str(tmp_path / "gone"), "claude")

    assert excinfo.value.code == ErrorCode.HARNESS_UNAVAILABLE
    assert "不存在" in excinfo.value.message
