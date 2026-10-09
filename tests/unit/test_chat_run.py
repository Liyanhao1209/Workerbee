"""chat 命令执行边界的单元测试（v0.03 §5.4、§2 D-G，Phase 3b）。

红队视角：cwd confinement、超时杀进程、输出截断标注、危险命令模式清单——
每条都直接对应「绝不能破」的纪律。审批与会话授权在 service 层测，不在本层。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from workerbee.chat import fs, run

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    r.mkdir()
    return r


# ===========================================================================
# cwd confinement：与读写同一条边界
# ===========================================================================


class TestCwdConfinement:
    async def test_默认工作目录是工作区根(self, root: Path) -> None:
        result = await run.run_command(root, "pwd")
        assert result["exit_code"] == 0
        assert result["cwd"] == ""
        assert Path(result["output"].strip()) == root.resolve()

    async def test_子目录作为cwd(self, root: Path) -> None:
        (root / "sub").mkdir()
        result = await run.run_command(root, "pwd", cwd="sub")
        assert result["exit_code"] == 0
        assert result["cwd"] == "sub"
        assert Path(result["output"].strip()) == (root / "sub").resolve()

    async def test_cwd_dotdot逃逸被拒(self, root: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            await run.run_command(root, "echo hi", cwd="../")

    async def test_cwd绝对路径越界被拒(self, root: Path, tmp_path: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            await run.run_command(root, "echo hi", cwd=str(tmp_path))

    async def test_cwd_symlink逃逸被拒(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with pytest.raises(fs.FSForbidden):
            await run.run_command(root, "echo hi", cwd="link")

    async def test_cwd不存在显式失败(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            await run.run_command(root, "echo hi", cwd="ghost")


# ===========================================================================
# 执行语义：退出码如实、超时即杀、输出有界
# ===========================================================================


class TestExecution:
    async def test_退出码与输出如实返回(self, root: Path) -> None:
        result = await run.run_command(root, "echo hello && echo err >&2")
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        # stdout 与 stderr 合并
        assert "hello" in result["output"]
        assert "err" in result["output"]

    async def test_非零退出不是异常(self, root: Path) -> None:
        result = await run.run_command(root, "exit 3")
        assert result["exit_code"] == 3

    async def test_超时杀进程并如实标注(self, root: Path) -> None:
        result = await run.run_command(root, "sleep 30", timeout=0.3)
        assert result["timed_out"] is True
        assert result["exit_code"] is None
        # 进程真的死了：fixture 目录里没有残留的 sleep 占着
        assert result["duration_s"] < 10

    async def test_输出截断保留末尾并标注(self, root: Path) -> None:
        command = (
            f"{sys.executable} -c \"print('H' * 1024);"
            f"print('T' * (200 * 1024))\""
        )
        result = await run.run_command(root, command)
        assert result["output_truncated"] is True
        assert result["output_bytes"] > run.RUN_OUTPUT_MAX_BYTES
        # 保留的是末尾（尾部标记在，头部被掐掉）
        assert "TTTT" in result["output"]
        assert "HHHH" not in result["output"]

    async def test_非utf8输出容错解码不炸(self, root: Path) -> None:
        result = await run.run_command(root, "printf '\\xff\\xfe ok\\n'")
        assert result["exit_code"] == 0
        assert "ok" in result["output"]


# ===========================================================================
# 危险命令模式：清单每条都钉住，良性命令不误伤
# ===========================================================================


class TestDangerousPatterns:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf build/",
            "rm out.txt",
            "sudo apt install x",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sda1",
            ":(){ :|:& };:",
            "chmod -R 777 /",
            "chmod -R 777 / ",
            "chown -R root /",
            "echo pwned > /etc/cron.d/x",
            "echo pwned >> /tmp/x",
            "cat data > ../outside.txt",
            "tar xzf a.tgz -C / && echo done > /etc/marker",
        ],
    )
    def test_危险命令被标注(self, command: str) -> None:
        assert run.dangerous_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "grep -rn pattern src/",
            "echo hello > out.txt",  # 重定向到工作区相对路径
            "make test > /dev/null 2>&1",
            "git status && git log --oneline -3",
            "cat a.txt | wc -l",
            "chmod 755 run.sh",  # 非递归、目标是相对路径
            sys.executable + " -m pytest -q",
        ],
    )
    def test_良性命令不误伤(self, command: str) -> None:
        assert run.dangerous_reason(command) is None

    def test_清单每条都有威胁说明(self) -> None:
        for pattern, note in run.DANGEROUS_COMMAND_PATTERNS:
            assert pattern.pattern
            assert note.strip()
