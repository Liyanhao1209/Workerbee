"""chat 的命令执行边界（v0.03 §5.4、§2 D-G，Phase 3b）。

安全模型一句话：**命令走 shell 是功能所需，威力如实明示，审批兜底一切。**

- ``command`` 经 shell 执行（管道、重定向、通配都是用户要的表达能力）——
  它能做到的事情与「用户在终端里敲这条命令」完全一样，包括读写 workspace
  之外的文件。本模块**不假装能 confined 一条 shell 命令**：能 confined 的只有
  起始工作目录（``cwd`` 过 :func:`chat.fs.resolve_in_workspace`）。
  真正的闸门在调用方：每次执行前过审批（D-G 默认逐次审批）。
- ``DANGEROUS_COMMAND_PATTERNS`` 命中的命令**永远逐次审批**，会话级授权
  也不能豁免——授权消解的是高频疲劳，不是给破坏性命令开后门。
- 超时默认 60s（上限由调用方封顶），超时即杀进程并如实标注；stdout/stderr
  合并后有界截断并如实标注；退出码原样返回（非零不是异常，是事实）。

本模块不做审批、不写事件日志——与 ``fs.py`` 同一分工。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from . import fs

__all__ = [
    "DANGEROUS_COMMAND_PATTERNS",
    "DEFAULT_RUN_TIMEOUT_S",
    "RUN_OUTPUT_MAX_BYTES",
    "dangerous_reason",
    "run_command",
]

#: 执行的默认超时（秒）。上限由工具执行器封顶（见 chat/tools.py）。
DEFAULT_RUN_TIMEOUT_S = 60.0

#: 合并输出（stdout+stderr）的字节上限。与读文件的 100KB 纪律同源：
#: 截断保留**末尾**（报错与结论通常在最后），并如实标注被掐掉的头部。
RUN_OUTPUT_MAX_BYTES = 100 * 1024

#: 危险命令模式：(正则, 威胁说明)。命中者始终逐次审批，会话级授权不豁免。
#: 清单集中在这里维护——每条注释写清它防的是什么，新增条目照此办理。
#: 注意这是「提示审批闸门收紧」的启发式清单，不是沙箱：shell 命令的威力
#: 边界不在这里，在审批。
DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), note)
    for pattern, note in (
        # rm：不可逆删除。workspace confinement 管不住 shell 里的 rm 碰别处。
        (r"\brm\b", "rm 删除文件，不可逆"),
        # sudo：提权到 root，之后的破坏不再受运行用户身份限制。
        (r"\bsudo\b", "sudo 提权执行"),
        # dd：按块裸写磁盘/文件，写错 of= 就是毁盘。
        (r"\bdd\b", "dd 裸写磁盘或文件"),
        # mkfs 系：格式化文件系统。
        (r"\bmkfs\b|\bmkfs\.\w+", "mkfs 格式化磁盘"),
        # fork 炸弹：`:(){ :|:& };:` 及其变体，瞬间耗尽进程表。
        (r":\s*\(\s*\)\s*\{", "fork 炸弹（耗尽进程表）"),
        # chmod -R 作用于 /：递归改权限到文件系统根，系统级破坏。
        (r"\bchmod\b[^;&|\n]*\s-R[\w]*\s+[^;&|\n]*\s/(\s|$|;|&)", "chmod -R 作用于 /，系统级破坏"),
        # chown -R 作用于 /：同理。
        (r"\bchown\b[^;&|\n]*\s-R[\w]*\s+[^;&|\n]*\s/(\s|$|;|&)", "chown -R 作用于 /，系统级破坏"),
        # 重定向到绝对路径：> /etc/xxx 这类写入越出 workspace（cwd 边界管不到）。
        (r">>?\s*['\"]?/", "重定向写入绝对路径（workspace 外）"),
        # 重定向到 ../：从 cwd 向上逃逸出 workspace。
        (r">>?\s*['\"]?\.\./", "重定向写入 ../（workspace 外）"),
    )
)


#: ``> /dev/null`` 是丢弃输出的常规写法，不是越界写——匹配前先抹掉它，
#: 否则「重定向到绝对路径」模式会把大量良性命令打成永远逐次审批。
_DEV_NULL_RE = re.compile(r"\d*>>?\s*['\"]?/dev/null")


def dangerous_reason(command: str) -> str | None:
    """命令命中危险模式时返回威胁说明（给审批卡片看），否则 None。"""
    sanitized = _DEV_NULL_RE.sub("", command)
    for pattern, note in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(sanitized):
            return note
    return None


def _truncate_output(raw: bytes) -> tuple[str, bool]:
    """有界截断：保留末尾 RUN_OUTPUT_MAX_BYTES，按 UTF-8 容错解码。"""
    truncated = len(raw) > RUN_OUTPUT_MAX_BYTES
    if truncated:
        raw = raw[-RUN_OUTPUT_MAX_BYTES:]
    return raw.decode("utf-8", errors="replace"), truncated


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """杀掉整组进程；组已不在时退回只杀 shell 本身。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def run_command(
    root_dir: str | Path,
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = DEFAULT_RUN_TIMEOUT_S,
) -> dict[str, Any]:
    """在 workspace 内执行一条 shell 命令。

    ``cwd`` 缺省为 workspace 根；给出时过与读写同款的 confinement
    （resolve 后必须仍在根内）。返回退出码、合并输出、截断与超时标注——
    全部如实，失败（超时、非零退出）不抛异常，只有 confinement 违例抛出
    ``fs.FSError``。
    """
    root = Path(root_dir).expanduser().resolve()
    if cwd is None or not cwd.strip():
        real_cwd = root
        rel_cwd = ""
    else:
        real_cwd = fs.resolve_in_workspace(root, cwd)
        if not real_cwd.is_dir():
            raise fs.FSNotFound(f"工作目录不存在：{cwd}")
        rel_cwd = real_cwd.relative_to(root).as_posix()

    started = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(real_cwd),
        stdout=asyncio.subprocess.PIPE,
        # stderr 并入 stdout：交错顺序就是用户看到的顺序，分开收会乱。
        stderr=asyncio.subprocess.STDOUT,
        # 独立进程组：超时杀的是整组（只杀 shell 本身的话，它 fork 出的
        # 子进程仍握着管道，communicate 会一直等下去）。
        start_new_session=True,
    )
    timed_out = False
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc)
        raw, _ = await proc.communicate()

    output, truncated = _truncate_output(raw or b"")
    return {
        "command": command,
        "cwd": rel_cwd,
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 3),
        "output": output,
        "output_truncated": truncated,
        "output_bytes": len(raw or b""),
    }
