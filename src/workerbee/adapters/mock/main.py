"""mock 适配器进程入口。

    python -m workerbee.adapters.mock.main

剧本从环境变量读（``WORKERBEE_MOCK_SCRIPT`` / ``WORKERBEE_MOCK_SCRIPT_FILE``），
这样测试只要 ``AdapterProcess.start(argv, env={...})`` 就能换一出戏，
不必为每个用例准备一个可执行文件。

注意：本模块的 stdout 是协议通道，任何 ``print`` 都会污染它——
``AdapterBase.run()`` 已经把 ``sys.stdout`` 重定向到 stderr 来兜住这件事。
"""

from __future__ import annotations

import sys

from ..sdk.base import run_adapter
from ..sdk.protocol import AdapterError
from .adapter import MockAdapter


def main() -> int:
    try:
        adapter = MockAdapter.from_env()
    except AdapterError as exc:
        # 剧本本身写错属于测试配置错误：直接报错退出，别让内核等到超时
        # 才发现「适配器起来了但什么都不会做」。
        print(f"[mock] 剧本装载失败：{exc.message}", file=sys.stderr, flush=True)
        return 2
    run_adapter(adapter)
    return 0


if __name__ == "__main__":
    sys.exit(main())
