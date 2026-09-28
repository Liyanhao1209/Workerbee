"""定位 harness 可执行文件。

**为什么不只查 PATH。** 把内核装成服务时，守护进程拿到的是 systemd 给的默认
PATH（``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin``），而用户
自装的工具基本都落在这个范围之外——claude 常随 nvm 落在
``~/.nvm/versions/node/*/bin``，kimi 在 ``~/.kimi-code/bin``。

于是出现一种极难反推的故障：``workerbee doctor`` 在终端里报「kimi 找得到，
在 /home/…/.kimi-code/bin/kimi」，用户据此以为一切就绪，装成服务后探测却失败。
两边查的根本不是同一个环境，而两边的输出都不足以让人看出这一点。

**为什么可以搜这些目录。** 这不是在猜，也不是在扫盘——这些工具的官方安装脚本
就是写在这些位置的。搜索范围是一个静态的、可审计的短列表，而不是全盘查找。

顺序上 PATH 优先：PATH 命中说明用户显式配置过，应当尊重。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path

#: 用户自装工具常见的落点。按「越可能是用户本意」的顺序排列——但这只在
#: PATH 未命中时才有意义，PATH 命中一律优先。
SEARCH_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "~/bin",
    "~/.kimi-code/bin",
    "~/.claude/local",
    "~/.claude/bin",
    "~/.bun/bin",
    "~/.cargo/bin",
    "~/.deno/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

#: 需要按版本展开的（nvm 一类）。装多个版本时优先用版本号最高的那个。
SEARCH_GLOBS: tuple[str, ...] = (
    "~/.nvm/versions/node/*/bin",
    "~/.nvm/current/bin",
    "~/.volta/bin",
    "~/.local/share/pnpm",
)


def _version_key(path: Path) -> tuple[int, ...]:
    """从 ``…/versions/node/v22.23.2/bin`` 里取出 ``(22, 23, 2)`` 用于排序。

    不能直接按字符串排：那样 ``v9`` 会排在 ``v22`` 后面。
    """
    for part in reversed(path.parts):
        if re.fullmatch(r"v\d+(\.\d+)*", part):
            return tuple(int(x) for x in re.findall(r"\d+", part))
    return ()


def search_dirs() -> list[Path]:
    """要搜索的目录，已展开 ``~`` 且只保留真实存在的。"""
    found: list[Path] = []
    for spec in SEARCH_DIRS:
        path = Path(spec).expanduser()
        if path.is_dir():
            found.append(path)

    versioned: list[Path] = []
    for spec in SEARCH_GLOBS:
        for match in glob.glob(os.path.expanduser(spec)):
            candidate = Path(match)
            if candidate.is_dir():
                versioned.append(candidate)
    versioned.sort(key=_version_key, reverse=True)
    found.extend(versioned)
    return found


def resolve(exec_path: str | None, default: str) -> str | None:
    """解析可执行文件，找不到返回 ``None``（不抛异常）。

    给需要「先问一句有没有」的调用方用（doctor、启动自检）。
    """
    candidate = exec_path or default
    if os.path.isabs(candidate) or os.sep in candidate:
        return candidate if os.path.exists(candidate) else None

    found = shutil.which(candidate)
    if found is not None:
        return found

    for directory in search_dirs():
        path = directory / candidate
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None
