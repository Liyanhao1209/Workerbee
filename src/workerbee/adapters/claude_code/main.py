"""Claude Code 适配器进程入口。

    python -m workerbee.adapters.claude_code.main

可选环境变量：
- ``WORKERBEE_CLAUDE_CODE_INPUT_FORMAT``：``text``（默认）或 ``stream-json``。
  后者开启流式输入通道，适配器据此把 ``interact`` / ``interrupt`` 声明为支持
  （manifest 必须与当前配置一致，不能声明一个做不到的能力）。
"""

from __future__ import annotations

import sys

from ..sdk.base import run_adapter
from ..sdk.protocol import AdapterError
from .adapter import ClaudeCodeAdapter


def main() -> int:
    try:
        adapter = ClaudeCodeAdapter()
    except AdapterError as exc:
        print(f"[claude-code] 启动配置非法：{exc.message}", file=sys.stderr, flush=True)
        return 2
    run_adapter(adapter)
    return 0


if __name__ == "__main__":
    sys.exit(main())
