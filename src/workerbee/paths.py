"""进程入口共用的路径与令牌约定（v0.03 Phase 1 单命令启动）。

cli、server、supervisor 三个入口的默认值都从这里取，避免三处漂移。
数据目录自 v0.03 起默认 ``~/.workerbee``（此前是 cwd 下的 ``./.workerbee``——
相对路径意味着「在哪个目录启动，数据就在哪」，换目录启动会凭空另起一套库）。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

__all__ = [
    "ENV_TOKEN",
    "default_data_dir",
    "token_path",
    "read_token_file",
    "write_token_file",
    "resolve_token",
    "legacy_data_dir_notice",
]

ENV_TOKEN = "WORKERBEE_TOKEN"

#: v0.03 之前的数据目录名（相对于启动时的 cwd）。
LEGACY_DIR_NAME = ".workerbee"


def default_data_dir() -> Path:
    return Path.home() / ".workerbee"


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / "token"


def read_token_file(data_dir: Path) -> str | None:
    """读取令牌文件。不存在、读不到、或内容为空都返回 None（按「没有」处理下一级）。"""
    try:
        text = token_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_token_file(data_dir: Path, token: str) -> Path:
    """写入令牌文件，权限收紧为 0600——它与凭据同级，不能被同机其他用户读到。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = token_path(data_dir)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    # 文件可能先于本次写入存在且权限宽松；O_CREAT 不会改既有文件的权限，单独收紧
    os.chmod(path, 0o600)
    return path


def resolve_token(explicit: str | None, data_dir: Path) -> tuple[str, str]:
    """令牌解析优先级：显式参数 > 环境变量 > 令牌文件 > 新生成并写入令牌文件。

    返回 ``(令牌, 来源描述)``。来源要如实打印——令牌从哪来影响「重启后还是不是
    同一个」的判断，不该让用户猜。
    """
    if explicit:
        return explicit, "--token"
    env = os.environ.get(ENV_TOKEN)
    if env:
        return env, f"环境变量 {ENV_TOKEN}"
    existing = read_token_file(data_dir)
    if existing:
        return existing, f"令牌文件 {token_path(data_dir)}"
    token = secrets.token_urlsafe(32)
    write_token_file(data_dir, token)
    return token, f"新生成（已写入 {token_path(data_dir)}）"


def legacy_data_dir_notice() -> str | None:
    """升级兼容提示：cwd 下存在旧版 ``./.workerbee`` 且新默认目录还不存在时返回提示文本。

    只在**使用默认目录**时由调用方触发（显式 ``--data-dir`` 是用户已经表态）。
    不自动搬移——旧目录里可能有仍在被引用的库与凭据；也不静默另起新库当作
    无事发生。用户必须知道两个目录的存在并自己选择。
    """
    legacy = Path.cwd() / LEGACY_DIR_NAME
    default = default_data_dir()
    if legacy.is_dir() and not default.exists():
        return (
            f"检测到旧版数据目录 {legacy}，而默认数据目录现在是 {default}。\n"
            "  Workerbee 不会自动搬移数据。三选一：\n"
            f"    1. 迁移：mv {legacy} {default}\n"
            f"    2. 继续用旧目录：显式加 --data-dir {legacy}\n"
            "    3. 不处理：本次将以全新的空数据目录启动"
        )
    return None
