"""chat 的文件系统工具：LLMToolSpec 清单与执行器（v0.03 §5.3、§5.4）。

纪律：
- 全部工具共用一个 workspace 根（会话所属工作区），路径参数一律过
  :func:`chat.fs.resolve_in_workspace`——工具入口与 REST 端点同一 confinement。
- 写类工具（write/mkdir/move/delete）执行前先过审批回调：审批通过才动手，
  拒绝/超时都如实变成工具结果回给模型（模型据此向用户解释，而不是假装成功）。
- 工具的失败**不抛出**：失败文本作为该次调用的结果回注给模型，让它自己修正
  路径或向用户说明；只有实现 bug 才应该炸穿 tool loop。
- 结果文本有界：list/read 的产物可能很大，统一截断到 ``RESULT_MAX_CHARS``。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..core.domain.base import DomainModel
from ..data.llm import LLMToolCall, LLMToolSpec
from . import fs, run as run_mod

__all__ = [
    "CHAT_TOOL_SPECS",
    "WRITE_TOOLS",
    "RUN_TOOLS",
    "APPROVAL_TOOLS",
    "ApprovalGate",
    "ToolOutcome",
    "execute_tool",
    "RESULT_MAX_CHARS",
    "RUN_TIMEOUT_CAP_S",
]

#: 单个工具结果回注给模型的字符上限（超出截断并标注）。
RESULT_MAX_CHARS = 60_000

#: 需要用户审批才能执行的写类工具。
WRITE_TOOLS: frozenset[str] = frozenset({"fs_write", "fs_mkdir", "fs_move", "fs_delete"})

#: 需要用户审批才能执行的执行类工具（与写类分开授权，D-G）。
RUN_TOOLS: frozenset[str] = frozenset({"fs_run"})

#: 全部需审批工具。
APPROVAL_TOOLS: frozenset[str] = WRITE_TOOLS | RUN_TOOLS

#: fs_run 的 timeout_seconds 参数上限（秒）。模型可以给更短的，不能要更长的。
RUN_TIMEOUT_CAP_S = 300.0

_PATH_SCHEMA: dict[str, Any] = {"type": "string", "description": "工作区内的相对路径"}

CHAT_TOOL_SPECS: list[LLMToolSpec] = [
    LLMToolSpec(
        name="fs_list",
        description="列出工作区内某个目录的一层内容（名称、类型、大小、修改时间）。",
        parameters={
            "type": "object",
            "properties": {"path": {**_PATH_SCHEMA, "description": "目录路径，空串为工作区根"}},
            "required": ["path"],
        },
    ),
    LLMToolSpec(
        name="fs_read",
        description="读取工作区内一个文本文件的内容（默认最多 100KB，超出会截断）。",
        parameters={
            "type": "object",
            "properties": {"path": _PATH_SCHEMA},
            "required": ["path"],
        },
    ),
    LLMToolSpec(
        name="fs_write",
        description=(
            "写入（新建或覆盖）工作区内一个文本文件。覆盖已存在的文件时必须带上"
            "读取它时得到的 mtime（expected_mtime）。需要用户批准后才会执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH_SCHEMA,
                "content": {"type": "string", "description": "要写入的完整内容"},
                "expected_mtime": {
                    "type": ["string", "null"],
                    "description": "覆盖已存在文件时必填：读取该文件时返回的 mtime",
                },
            },
            "required": ["path", "content"],
        },
    ),
    LLMToolSpec(
        name="fs_mkdir",
        description="在工作区内创建目录（可多层）。需要用户批准后才会执行。",
        parameters={
            "type": "object",
            "properties": {"path": _PATH_SCHEMA},
            "required": ["path"],
        },
    ),
    LLMToolSpec(
        name="fs_move",
        description="移动或改名工作区内的文件/目录。需要用户批准后才会执行。",
        parameters={
            "type": "object",
            "properties": {
                "src": {**_PATH_SCHEMA, "description": "源路径"},
                "dst": {**_PATH_SCHEMA, "description": "目标路径（不得已存在）"},
            },
            "required": ["src", "dst"],
        },
    ),
    LLMToolSpec(
        name="fs_delete",
        description="删除工作区内的文件或空目录（不提供递归删除）。需要用户批准后才会执行。",
        parameters={
            "type": "object",
            "properties": {"path": _PATH_SCHEMA},
            "required": ["path"],
        },
    ),
    LLMToolSpec(
        name="fs_run",
        description=(
            "在工作区内执行一条 shell 命令（经 shell 解析，支持管道与重定向）。"
            "威力与用户在终端里亲手敲这条命令完全相同——包括访问工作区之外的文件，"
            "cwd 限制的只是起始目录，不是沙箱。因此每次执行都需要用户逐次批准；"
            "危险命令（rm/sudo/dd/mkfs/向工作区外重定向等）永远需要批准。"
            "默认超时 60 秒（可用 timeout_seconds 调短或调长，上限 300 秒），"
            "stdout 与 stderr 合并返回，超长会截断并标注。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "cwd": {
                    "type": ["string", "null"],
                    "description": "工作目录（工作区内相对路径），缺省为工作区根",
                },
                "timeout_seconds": {
                    "type": ["number", "null"],
                    "description": "超时秒数，缺省 60，上限 300",
                },
            },
            "required": ["command"],
        },
    ),
]

#: 审批回调：``(tool_name, 动作摘要, 目标路径)`` → 是否获准执行。
#: 由 chat 服务装配（过 ApprovalGateway + 轮询决定）；None 等价于全部拒绝——
#: 写操作没有审批通道时绝不能执行（deny by default，与审批网关同一语义）。
ApprovalGate = Callable[[str, str, str], Awaitable[bool]]


class ToolOutcome(DomainModel):
    """一次工具调用的结果。``text`` 回注给模型；``effect`` 描述真实发生的
    文件系统写（供调用方写事件日志），未执行或只读时为 None。"""

    text: str
    effect: dict[str, Any] | None = None
    """形如 {"type": "fs.write", "path": ..., "detail": ..., "extra": {...}}，
    只在写/执行操作真实发生后给出。"""

    refused: bool = False
    """True 表示写操作被审批拦下（用户拒绝/超时/无审批通道）。"""


def _truncate(text: str) -> str:
    if len(text) <= RESULT_MAX_CHARS:
        return text
    return text[:RESULT_MAX_CHARS] + f"\n\n（结果过长，已截断到 {RESULT_MAX_CHARS} 字符）"


def _arg_str(call: LLMToolCall, key: str, *, default: str | None = None) -> str:
    value = call.arguments.get(key, default)
    if not isinstance(value, str):
        raise fs.FSError(f"工具 {call.name} 的参数 {key} 缺失或不是字符串")
    return value


def _describe(call: LLMToolCall) -> tuple[str, str]:
    """给审批卡片用的「动作摘要 + 目标」。路径原文呈现，用户要看的就是它。"""
    args = call.arguments
    if call.name == "fs_write":
        return "写入文件", str(args.get("path", ""))
    if call.name == "fs_mkdir":
        return "创建目录", str(args.get("path", ""))
    if call.name == "fs_move":
        return "移动/改名", f"{args.get('src', '')} → {args.get('dst', '')}"
    if call.name == "fs_delete":
        return "删除", str(args.get("path", ""))
    if call.name == "fs_run":
        return "执行命令", str(args.get("command", ""))
    return call.name, str(args.get("path", ""))


async def execute_tool(
    call: LLMToolCall,
    *,
    workspace_root: str,
    approval_gate: ApprovalGate | None = None,
    run_timeout_cap: float = RUN_TIMEOUT_CAP_S,
) -> ToolOutcome:
    """执行一次工具调用，返回回注给模型的结果（失败也是结果，不抛出）。

    写/执行类工具先过 ``approval_gate``；未获准（拒绝、超时、无审批通道）时返回
    ``refused=True`` 的说明文本，让模型如实转告用户。
    """
    try:
        if call.name in APPROVAL_TOOLS:
            action, target = _describe(call)
            approved = (
                await approval_gate(call.name, action, target)
                if approval_gate is not None
                else False
            )
            if not approved:
                return ToolOutcome(
                    text=f"操作「{action} {target}」未获用户批准，没有执行。",
                    refused=True,
                )

        if call.name == "fs_list":
            result = fs.list_dir(workspace_root, _arg_str(call, "path", default=""))
            if not result["entries"]:
                return ToolOutcome(text="（空目录）")
            lines = [
                f"{'[目录]' if e['type'] == 'dir' else '[文件]' if e['type'] == 'file' else '[其他]'}"
                f" {e['name']}"
                + (f"（{e['size']} 字节）" if e["size"] is not None else "")
                + ("（敏感文件，不可读写）" if e["sensitive"] else "")
                for e in result["entries"]
            ]
            return ToolOutcome(text=_truncate("\n".join(lines)))

        if call.name == "fs_read":
            result = fs.read_file(workspace_root, _arg_str(call, "path"))
            note = ""
            if result["truncated"]:
                note = f"（文件共 {result['size']} 字节，已截断到前 100KB）\n"
            return ToolOutcome(
                text=_truncate(f"{note}mtime: {result['mtime']}\n\n{result['content']}")
            )

        if call.name == "fs_write":
            expected = call.arguments.get("expected_mtime")
            result = fs.write_file(
                workspace_root,
                _arg_str(call, "path"),
                _arg_str(call, "content"),
                expected_mtime=expected if isinstance(expected, str) else None,
            )
            return ToolOutcome(
                text=f"已写入 {result['path']}（{result['size']} 字节，mtime: {result['mtime']}）",
                effect={
                    "type": "fs.write",
                    "path": result["path"],
                    "detail": f"{result['size']} 字节",
                },
            )

        if call.name == "fs_mkdir":
            result = fs.make_dir(workspace_root, _arg_str(call, "path"))
            if result["existed"]:
                return ToolOutcome(text=f"目录 {result['path']} 已存在（未改动）")
            return ToolOutcome(
                text=f"已创建目录 {result['path']}",
                effect={"type": "fs.mkdir", "path": result["path"], "detail": None},
            )

        if call.name == "fs_move":
            result = fs.move_entry(
                workspace_root, _arg_str(call, "src"), _arg_str(call, "dst")
            )
            return ToolOutcome(
                text=f"已移动 {result['src']} → {result['dst']}",
                effect={
                    "type": "fs.move",
                    "path": result["dst"],
                    "detail": f"{result['src']} → {result['dst']}",
                },
            )

        if call.name == "fs_delete":
            result = fs.delete_entry(workspace_root, _arg_str(call, "path"))
            kind = "目录" if result["kind"] == "dir" else "文件"
            return ToolOutcome(
                text=f"已删除{kind} {result['path']}",
                effect={
                    "type": "fs.delete",
                    "path": result["path"],
                    "detail": result["kind"],
                },
            )

        if call.name == "fs_run":
            timeout_raw = call.arguments.get("timeout_seconds")
            timeout = run_mod.DEFAULT_RUN_TIMEOUT_S
            if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool):
                timeout = min(max(float(timeout_raw), 1.0), run_timeout_cap)
            cwd_raw = call.arguments.get("cwd")
            result = await run_mod.run_command(
                workspace_root,
                _arg_str(call, "command"),
                cwd=cwd_raw if isinstance(cwd_raw, str) and cwd_raw.strip() else None,
                timeout=timeout,
            )
            header = f"工作目录：{result['cwd'] or '（工作区根）'}\n"
            if result["timed_out"]:
                header += f"执行超过 {timeout:g} 秒，进程已被终止（超时）。\n"
            else:
                header += f"退出码：{result['exit_code']}（耗时 {result['duration_s']}s）\n"
            if result["output_truncated"]:
                header += (
                    f"输出共 {result['output_bytes']} 字节，"
                    f"只保留最后 {run_mod.RUN_OUTPUT_MAX_BYTES // 1024}KB。\n"
                )
            body = result["output"] or "（无输出）"
            return ToolOutcome(
                text=_truncate(f"{header}\n{body}"),
                effect={
                    "type": "fs.run",
                    "path": result["cwd"],
                    "detail": (
                        "超时终止" if result["timed_out"]
                        else f"退出码 {result['exit_code']}"
                    ),
                    "extra": {
                        "command": result["command"][:500],
                        "exit_code": result["exit_code"],
                        "timed_out": result["timed_out"],
                        "duration_s": result["duration_s"],
                        "output_truncated": result["output_truncated"],
                    },
                },
            )

        return ToolOutcome(text=f"不认识工具「{call.name}」，本次调用未执行。")
    except fs.FSError as exc:
        detail = exc.detail
        if isinstance(exc, fs.FSConflict) and exc.current_mtime:
            detail += f"（当前 mtime: {exc.current_mtime}）"
        return ToolOutcome(text=f"操作失败：{detail}")
