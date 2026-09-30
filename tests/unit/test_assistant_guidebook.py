"""使用指引（guidebook）的加载纪律（AI-01，计划 §4）。

- 仓库自带的 guidebook.md 必须能加载（占位骨架也算——缺失与空是两回事）；
- 文件缺失不报错，记 ``missing=True`` 由上层如实降级；
- 超预算截断并记 ``truncated``，「喂了多少 / 共有多少」可核对。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workerbee.assistant.guidebook import load_guidebook

pytestmark = pytest.mark.unit


async def test_bundled_guidebook_loads() -> None:
    guidebook = load_guidebook()
    assert not guidebook.missing
    assert "Workerbee" in guidebook.text
    assert not guidebook.truncated
    assert guidebook.total_chars > 0


async def test_missing_file_is_degraded_not_an_error(tmp_path: Path) -> None:
    guidebook = load_guidebook(tmp_path / "不存在.md")
    assert guidebook.missing
    assert guidebook.text == ""


async def test_empty_file_counts_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "guidebook.md"
    path.write_text("  \n", encoding="utf-8")
    assert load_guidebook(path).missing


async def test_budget_truncation_is_visible(tmp_path: Path) -> None:
    path = tmp_path / "guidebook.md"
    path.write_text("甲" * 5000, encoding="utf-8")
    guidebook = load_guidebook(path, budget=1000)
    assert guidebook.truncated
    assert guidebook.total_chars == 5000
    assert "截断" in guidebook.text
    assert len(guidebook.text) < 1100
