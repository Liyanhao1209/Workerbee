"""Kimi Code 适配器进程入口。

    python -m workerbee.adapters.kimi_code.main

该适配器没有可选配置：``kimi -p`` 的模式是固定的（见 adapter.py 的模块文档）。
"""

from __future__ import annotations

import sys

from ..sdk.base import run_adapter
from .adapter import KimiCodeAdapter


def main() -> int:
    run_adapter(KimiCodeAdapter())
    return 0


if __name__ == "__main__":
    sys.exit(main())
