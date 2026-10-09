"""workspace 文件系统边界与操作（v0.03 §5.4）。

安全模型一句话：**所有路径都必须先过 confinement，再谈读写。**

- ``resolve_in_workspace`` 是唯一入口：``expanduser().resolve()``（穿透 symlink）
  之后必须仍落在 workspace 根目录内，否则拒绝——``../``、绝对路径、symlink
  逃逸在这里一次拦住。
- 敏感文件名（``.env``、``*.pem``、``id_rsa*`` 等）拒读拒写：对话内容会发给
  模型服务商，这些文件的泄漏半径与凭据同级（红线 2 的延伸）。
- 读默认截断 100KB 并如实标注；二进制文件拒读（不是文本就不该进对话上下文）。
- 写按 ``expected_mtime`` 做乐观并发：文件在服务端被看过一眼之后又被改动时，
  显式冲突（409 语义），不静默覆盖别人的改动。

本模块不做审批、不写事件日志——那是调用方（REST 服务层与 chat 工具循环）的
职责，两边共用同一份边界实现，行为不可能分叉。
"""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "FSError",
    "FSForbidden",
    "FSNotFound",
    "FSConflict",
    "FSBinaryFile",
    "FSNotEmpty",
    "SENSITIVE_NAME_PATTERNS",
    "DEFAULT_READ_MAX_BYTES",
    "resolve_in_workspace",
    "is_sensitive_name",
    "list_dir",
    "read_file",
    "write_file",
    "make_dir",
    "move_entry",
    "delete_entry",
]

#: 敏感文件名模式（对 basename 做 fnmatch，大小写不敏感）。
#: 与「凭据不出安全层」同源：这些文件的内容一旦进入对话上下文就会出站。
SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "*.pfx",
    "*.keystore",
)

#: 读的默认截断（字节）。截断是如实标注的（truncated=True），不是静默省略。
DEFAULT_READ_MAX_BYTES = 100 * 1024


class FSError(RuntimeError):
    """FS 用例失败的基类。消息是大白话中文，可直接给最终用户看。"""

    def __init__(self, detail: str, *, hint: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class FSForbidden(FSError):
    """越出 workspace 边界，或触碰敏感文件名。"""


class FSNotFound(FSError):
    """目标不存在。"""


class FSConflict(FSError):
    """乐观并发冲突：文件的 mtime 与调用方持有的不一致。"""

    def __init__(self, detail: str, *, current_mtime: str | None = None) -> None:
        super().__init__(detail, hint="先重新读取该文件，带上最新的 mtime 再试")
        self.current_mtime = current_mtime


class FSBinaryFile(FSError):
    """二进制（或非 UTF-8 文本）文件拒读。"""


class FSNotEmpty(FSError):
    """删除的目录非空。"""


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SENSITIVE_NAME_PATTERNS)


def resolve_in_workspace(root_dir: str | Path, path: str) -> Path:
    """confinement 必经入口：把用户给的路径解析成 workspace 内的绝对路径。

    相对路径按 workspace 根解析；``resolve()`` 会穿透 symlink，因此
    「workspace 里放一个指向 /etc 的软链」也逃不出去。
    """
    root = Path(root_dir).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    real = candidate.resolve()
    if not real.is_relative_to(root):
        raise FSForbidden(
            "路径越出了当前工作区的边界",
            hint="只能访问工作区目录内的文件；请使用工作区内的相对路径",
        )
    return real


def _check_sensitive(real: Path, root: Path, action: str) -> None:
    if is_sensitive_name(real.name):
        raise FSForbidden(
            f"「{real.name}」属于敏感文件名，不允许{action}",
            hint="这类文件可能含有密钥或凭据，对话内容会发往模型服务，因此拒绝对它操作",
        )


def _rel(real: Path, root: Path) -> str:
    rel = real.relative_to(root)
    return "" if str(rel) == "." else rel.as_posix()


def list_dir(root_dir: str | Path, path: str) -> dict[str, Any]:
    """列一层目录（懒加载：不递归）。目录在前、按名排序。"""
    root = Path(root_dir).expanduser().resolve()
    real = resolve_in_workspace(root, path)
    if not real.exists():
        raise FSNotFound(f"目录不存在：{path or '.'}")
    if not real.is_dir():
        raise FSError(f"「{path}」不是目录", hint="列目录需要一个目录路径")

    entries: list[dict[str, Any]] = []
    with os.scandir(real) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # 扫描瞬间被删掉的条目：跳过，不让整次列表失败
            if entry.is_symlink():
                kind = "link"
            elif entry.is_dir(follow_symlinks=False):
                kind = "dir"
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            else:
                kind = "other"
            entries.append(
                {
                    "name": entry.name,
                    "path": _rel(real / entry.name, root),
                    "type": kind,
                    "size": st.st_size if kind == "file" else None,
                    "mtime": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "hidden": entry.name.startswith("."),
                    "sensitive": is_sensitive_name(entry.name),
                }
            )
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": _rel(real, root), "entries": entries}


def read_file(
    root_dir: str | Path, path: str, *, max_bytes: int = DEFAULT_READ_MAX_BYTES
) -> dict[str, Any]:
    """读文本文件。超出 ``max_bytes`` 截断并如实标注；二进制拒读。"""
    root = Path(root_dir).expanduser().resolve()
    real = resolve_in_workspace(root, path)
    _check_sensitive(real, root, "读取")
    if not real.exists():
        raise FSNotFound(f"文件不存在：{path}")
    if not real.is_file():
        raise FSError(f"「{path}」不是文件", hint="读内容需要一个文件路径")

    size = real.stat().st_size
    with real.open("rb") as fh:
        raw = fh.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    if b"\x00" in raw:
        raise FSBinaryFile(
            f"「{path}」是二进制文件，不能作为文本读取",
            hint="对话上下文只接受文本文件",
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FSBinaryFile(
            f"「{path}」不是 UTF-8 文本，不能作为文本读取",
            hint="对话上下文只接受 UTF-8 文本文件",
        ) from exc
    return {
        "path": _rel(real, root),
        "content": content,
        "truncated": truncated,
        "size": size,
        "mtime": _mtime_iso(real),
    }


def write_file(
    root_dir: str | Path,
    path: str,
    content: str,
    *,
    expected_mtime: str | None = None,
) -> dict[str, Any]:
    """写文件。目标已存在时必须带 ``expected_mtime`` 且与当前一致（乐观并发）。

    父目录不存在是显式错误（调用方有 mkdir 可用），不悄悄补建一串目录。
    """
    root = Path(root_dir).expanduser().resolve()
    real = resolve_in_workspace(root, path)
    _check_sensitive(real, root, "写入")
    if real.exists():
        if real.is_dir():
            raise FSError(f"「{path}」是目录，不能按文件写入")
        current = _mtime_iso(real)
        if expected_mtime is None:
            raise FSConflict(
                f"「{path}」已存在：覆盖写入需要先读出它的最新版本",
                current_mtime=current,
            )
        if expected_mtime != current:
            raise FSConflict(
                f"「{path}」在你读取之后又被改动过，本次写入未执行",
                current_mtime=current,
            )
    elif not real.parent.exists():
        raise FSNotFound(f"父目录不存在：{real.parent.relative_to(root).as_posix()}")
    real.write_text(content, encoding="utf-8")
    return {
        "path": _rel(real, root),
        "size": real.stat().st_size,
        "mtime": _mtime_iso(real),
    }


def make_dir(root_dir: str | Path, path: str) -> dict[str, Any]:
    """建目录（幂等：已存在时如实返回 existed=True，不报错）。"""
    root = Path(root_dir).expanduser().resolve()
    real = resolve_in_workspace(root, path)
    _check_sensitive(real, root, "创建")
    existed = real.is_dir()
    if real.exists() and not real.is_dir():
        raise FSError(f"「{path}」已存在且不是目录")
    real.mkdir(parents=True, exist_ok=True)
    return {"path": _rel(real, root), "existed": existed}


def move_entry(root_dir: str | Path, src: str, dst: str) -> dict[str, Any]:
    """移动/改名。源与目标都必须落在 workspace 内；目标已存在则拒绝（不覆盖）。"""
    root = Path(root_dir).expanduser().resolve()
    real_src = resolve_in_workspace(root, src)
    real_dst = resolve_in_workspace(root, dst)
    _check_sensitive(real_src, root, "移动")
    _check_sensitive(real_dst, root, "覆盖为")
    if not real_src.exists():
        raise FSNotFound(f"源路径不存在：{src}")
    if real_dst.exists():
        raise FSConflict(f"目标已存在：{dst}", current_mtime=_mtime_iso(real_dst))
    if not real_dst.parent.exists():
        raise FSNotFound(f"目标父目录不存在：{real_dst.parent.relative_to(root).as_posix()}")
    real_src.rename(real_dst)
    return {
        "src": _rel(real_src, root),
        "dst": _rel(real_dst, root),
        "mtime": _mtime_iso(real_dst),
    }


def delete_entry(root_dir: str | Path, path: str) -> dict[str, Any]:
    """删除文件或**空**目录。非空目录拒绝——递归删除太容易误伤，不做。"""
    root = Path(root_dir).expanduser().resolve()
    real = resolve_in_workspace(root, path)
    _check_sensitive(real, root, "删除")
    if real == root:
        raise FSForbidden("不能删除工作区根目录本身")
    if not real.exists() and not real.is_symlink():
        raise FSNotFound(f"路径不存在：{path}")
    if real.is_dir() and not real.is_symlink():
        try:
            real.rmdir()
        except OSError as exc:
            raise FSNotEmpty(
                f"目录「{path}」不为空，未删除",
                hint="先删除其中的条目，再删目录；不提供递归删除",
            ) from exc
        return {"path": _rel(real, root), "kind": "dir"}
    real.unlink()
    return {"path": _rel(real, root), "kind": "file"}
