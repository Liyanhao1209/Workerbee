"""仓库卫生：磁盘上的源码必须都在 git 里。

这条测试的存在理由是一次真实事故：``.gitignore`` 里的 ``data/`` 没有前导
斜杠，而 gitignore 的规则不带斜杠时匹配任意层级，于是 ``src/workerbee/data/``
（整个数据层，16 个文件）从未被提交。本地一切正常——文件在磁盘上，import 得到，
测试全绿；而任何人 clone 下来都在 import 阶段就崩。

**没有任何一个测「代码对不对」的用例能发现这类问题。** 它测的是「提交全不全」，
是另一个维度。所以单独放在这里。

不在 git 仓库里跑（源码包、导出的 tarball）时跳过，而不是失败：那种环境下
「有没有被跟踪」没有意义。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

#: 需要检查的源码根目录。docs/ 与 assets/ 不放进来：前者是文档，后者是图片，
#: 漏提交它们只会让 README 缺图，不会让服务起不来。
SOURCE_ROOTS = ("src", "tests", "scripts", "web/src")

#: 本来就不该进版本控制的东西。
IGNORED_PARTS = {"__pycache__", "node_modules", ".pytest_cache", ".hypothesis"}


def _tracked_files() -> set[str] | None:
    """返回 git 跟踪的文件集合。不在仓库里时返回 None。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return set(out.split())


def _on_disk() -> list[str]:
    found: list[str] = []
    for root in SOURCE_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if IGNORED_PARTS & set(path.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            found.append(str(path.relative_to(REPO)))
    return sorted(found)


def test_every_source_file_is_tracked_by_git():
    """磁盘上存在但没被 git 跟踪的源码文件 = 别人 clone 下来会缺东西。"""
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("不在 git 仓库里（源码包或导出目录），本检查不适用")

    untracked = [f for f in _on_disk() if f not in tracked]
    if untracked:
        listing = "\n".join(f"  {f}" for f in untracked)
        pytest.fail(
            f"以下 {len(untracked)} 个文件在磁盘上存在，但未被 git 跟踪。\n"
            f"别人 clone 下来会缺少它们。多半是 .gitignore 某条规则误伤——\n"
            f"gitignore 的规则不带前导斜杠时匹配任意层级，`data/` 会连\n"
            f"`src/workerbee/data/` 一起吃掉。\n\n{listing}\n\n"
            f"排查：git check-ignore -v <上面的路径>"
        )


def test_gitignore_rules_are_anchored_or_intentional():
    """``.gitignore`` 里不带斜杠的目录规则会匹配任意层级，容易误伤源码。

    这里只查「会吃掉 src/ 下目录」的那一类：任何一条规则如果匹配到了
    ``src/`` 里的文件，就是误伤（我们没有任何一条规则是以这个为目的的）。
    """
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("不在 git 仓库里，本检查不适用")

    # --no-index：连已跟踪的文件也一并判定，才能看出规则本身有多宽
    try:
        out = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", *sorted(_on_disk())],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git check-ignore 不可用")

    offenders = [
        line
        for line in out.splitlines()
        if line.split(":", 2)[-1].startswith("src/")
    ]
    assert not offenders, (
        "以下 .gitignore 规则误伤了 src/ 下的文件：\n  "
        + "\n  ".join(offenders)
        + "\n\n目录规则若只想匹配仓库根，需要写成 /name/。"
    )
