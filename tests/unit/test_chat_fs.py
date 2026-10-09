"""chat 文件系统边界的单元测试（v0.03 §5.4）。

安全模型是这层的全部价值：confinement、敏感名拒读写、截断如实标注、
乐观并发冲突。每个用例都直接对应一条「绝不能破」的纪律。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from workerbee.chat import fs

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    r.mkdir()
    return r


# ===========================================================================
# confinement：所有路径必须先过边界
# ===========================================================================


class TestConfinement:
    def test_相对路径正常工作(self, root: Path) -> None:
        (root / "a.txt").write_text("hello", encoding="utf-8")
        result = fs.read_file(root, "a.txt")
        assert result["content"] == "hello"
        assert result["path"] == "a.txt"

    def test_dotdot逃逸被拒(self, root: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            fs.read_file(root, "../outside.txt")

    def test_绝对路径越界被拒(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(fs.FSForbidden):
            fs.read_file(root, str(outside))

    def test_symlink逃逸被拒(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (root / "link.txt").symlink_to(outside)
        with pytest.raises(fs.FSForbidden):
            fs.read_file(root, "link.txt")

    def test_工作区内的symlink可用(self, root: Path) -> None:
        (root / "real.txt").write_text("real", encoding="utf-8")
        (root / "alias.txt").symlink_to(root / "real.txt")
        assert fs.read_file(root, "alias.txt")["content"] == "real"

    def test_写入也越界被拒(self, root: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            fs.write_file(root, "../evil.txt", "x")
        with pytest.raises(fs.FSForbidden):
            fs.make_dir(root, "/tmp/evil-dir")
        with pytest.raises(fs.FSForbidden):
            fs.move_entry(root, "a", "../b")
        with pytest.raises(fs.FSForbidden):
            fs.delete_entry(root, "../victim")


# ===========================================================================
# 敏感文件名：拒读拒写，但列表里如实可见
# ===========================================================================


class TestSensitiveNames:
    @pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "id_rsa", "id_ed25519.pub", "cert.p12"])
    def test_敏感文件拒读(self, root: Path, name: str) -> None:
        (root / name).write_text("secret", encoding="utf-8")
        with pytest.raises(fs.FSForbidden):
            fs.read_file(root, name)

    def test_敏感文件拒写(self, root: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            fs.write_file(root, ".env", "KEY=x")

    def test_敏感文件拒删拒移(self, root: Path) -> None:
        (root / ".env").write_text("KEY=x", encoding="utf-8")
        with pytest.raises(fs.FSForbidden):
            fs.delete_entry(root, ".env")
        with pytest.raises(fs.FSForbidden):
            fs.move_entry(root, ".env", "env.bak")

    def test_列表里敏感文件如实标注(self, root: Path) -> None:
        (root / ".env").write_text("KEY=x", encoding="utf-8")
        (root / "normal.txt").write_text("hi", encoding="utf-8")
        entries = {e["name"]: e for e in fs.list_dir(root, "")["entries"]}
        assert entries[".env"]["sensitive"] is True
        assert entries[".env"]["hidden"] is True
        assert entries["normal.txt"]["sensitive"] is False


# ===========================================================================
# 读：截断标注、二进制拒读
# ===========================================================================


class TestRead:
    def test_大文件截断并标注(self, root: Path) -> None:
        (root / "big.txt").write_text("x" * 2048, encoding="utf-8")
        result = fs.read_file(root, "big.txt", max_bytes=1024)
        assert result["truncated"] is True
        assert len(result["content"]) == 1024
        assert result["size"] == 2048

    def test_小文件不截断(self, root: Path) -> None:
        (root / "s.txt").write_text("abc", encoding="utf-8")
        result = fs.read_file(root, "s.txt")
        assert result["truncated"] is False
        assert result["mtime"]

    def test_二进制拒读(self, root: Path) -> None:
        (root / "bin.dat").write_bytes(b"\x00\x01\x02")
        with pytest.raises(fs.FSBinaryFile):
            fs.read_file(root, "bin.dat")

    def test_非utf8拒读(self, root: Path) -> None:
        (root / "gbk.txt").write_bytes("中文".encode("gbk"))
        with pytest.raises(fs.FSBinaryFile):
            fs.read_file(root, "gbk.txt")

    def test_不存在报NotFound(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            fs.read_file(root, "ghost.txt")


# ===========================================================================
# 写：乐观并发（expected_mtime CAS）
# ===========================================================================


class TestWrite:
    def test_新文件直接写(self, root: Path) -> None:
        result = fs.write_file(root, "new.txt", "内容")
        assert result["size"] == len("内容".encode("utf-8"))
        assert (root / "new.txt").read_text(encoding="utf-8") == "内容"

    def test_覆盖必须带expected_mtime(self, root: Path) -> None:
        (root / "f.txt").write_text("old", encoding="utf-8")
        with pytest.raises(fs.FSConflict) as exc_info:
            fs.write_file(root, "f.txt", "new")
        assert exc_info.value.current_mtime is not None

    def test_mtime不符返回冲突(self, root: Path) -> None:
        (root / "f.txt").write_text("old", encoding="utf-8")
        with pytest.raises(fs.FSConflict):
            fs.write_file(root, "f.txt", "new", expected_mtime="2000-01-01T00:00:00+00:00")

    def test_mtime相符则写入(self, root: Path) -> None:
        (root / "f.txt").write_text("old", encoding="utf-8")
        mtime = fs.read_file(root, "f.txt")["mtime"]
        result = fs.write_file(root, "f.txt", "new", expected_mtime=mtime)
        assert (root / "f.txt").read_text(encoding="utf-8") == "new"
        assert result["mtime"]

    def test_父目录不存在显式失败(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            fs.write_file(root, "no/such/dir/f.txt", "x")

    def test_目录不能按文件写(self, root: Path) -> None:
        (root / "d").mkdir()
        with pytest.raises(fs.FSError):
            fs.write_file(root, "d", "x")


# ===========================================================================
# mkdir / move / delete
# ===========================================================================


class TestDirOps:
    def test_mkdir幂等(self, root: Path) -> None:
        first = fs.make_dir(root, "a/b/c")
        assert first["existed"] is False
        second = fs.make_dir(root, "a/b/c")
        assert second["existed"] is True
        assert (root / "a/b/c").is_dir()

    def test_mkdir与现有文件冲突(self, root: Path) -> None:
        (root / "f").write_text("x", encoding="utf-8")
        with pytest.raises(fs.FSError):
            fs.make_dir(root, "f")

    def test_move正常(self, root: Path) -> None:
        (root / "a.txt").write_text("x", encoding="utf-8")
        result = fs.move_entry(root, "a.txt", "b.txt")
        assert result["dst"] == "b.txt"
        assert not (root / "a.txt").exists()
        assert (root / "b.txt").read_text(encoding="utf-8") == "x"

    def test_move目标已存在拒绝(self, root: Path) -> None:
        (root / "a.txt").write_text("x", encoding="utf-8")
        (root / "b.txt").write_text("y", encoding="utf-8")
        with pytest.raises(fs.FSConflict):
            fs.move_entry(root, "a.txt", "b.txt")
        assert (root / "b.txt").read_text(encoding="utf-8") == "y"

    def test_move源不存在(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            fs.move_entry(root, "ghost", "b")

    def test_delete文件(self, root: Path) -> None:
        (root / "f.txt").write_text("x", encoding="utf-8")
        result = fs.delete_entry(root, "f.txt")
        assert result["kind"] == "file"
        assert not (root / "f.txt").exists()

    def test_delete空目录(self, root: Path) -> None:
        (root / "d").mkdir()
        assert fs.delete_entry(root, "d")["kind"] == "dir"

    def test_delete非空目录拒绝(self, root: Path) -> None:
        (root / "d").mkdir()
        (root / "d" / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(fs.FSNotEmpty):
            fs.delete_entry(root, "d")

    def test_delete根目录拒绝(self, root: Path) -> None:
        with pytest.raises(fs.FSForbidden):
            fs.delete_entry(root, "")

    def test_delete不存在(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            fs.delete_entry(root, "ghost")


class TestList:
    def test_目录在前按名排序(self, root: Path) -> None:
        (root / "z.txt").write_text("x", encoding="utf-8")
        (root / "a_dir").mkdir()
        (root / "b.txt").write_text("x", encoding="utf-8")
        entries = fs.list_dir(root, "")["entries"]
        assert [e["name"] for e in entries] == ["a_dir", "b.txt", "z.txt"]
        assert entries[0]["type"] == "dir"
        assert entries[1]["size"] == 1

    def test_子目录路径(self, root: Path) -> None:
        (root / "sub").mkdir()
        (root / "sub" / "f.txt").write_text("x", encoding="utf-8")
        result = fs.list_dir(root, "sub")
        assert result["path"] == "sub"
        assert result["entries"][0]["path"] == "sub/f.txt"

    def test_列文件报错(self, root: Path) -> None:
        (root / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(fs.FSError):
            fs.list_dir(root, "f.txt")

    def test_列不存在目录(self, root: Path) -> None:
        with pytest.raises(fs.FSNotFound):
            fs.list_dir(root, "ghost")

    def test_symlink条目如实标注(self, root: Path) -> None:
        (root / "real.txt").write_text("x", encoding="utf-8")
        os.symlink(root / "real.txt", root / "link.txt")
        entries = {e["name"]: e for e in fs.list_dir(root, "")["entries"]}
        assert entries["link.txt"]["type"] == "link"
