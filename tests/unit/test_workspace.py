"""工作区匹配与命名的纯逻辑测试（v0.03 §3、D-B）。

serve 启动时把 cwd 归位到工作区：根目录最长前缀匹配、撞名追加序号。
这里是纯函数，不碰数据库；持久化与自动注册在集成测试里覆盖。
"""

from __future__ import annotations

import pytest

from workerbee.serve import match_workspace, unique_workspace_name

pytestmark = pytest.mark.unit


def _ws(root: str, name: str = "ws", archived: bool = False) -> dict:
    return {
        "workspace_id": f"id-{root}",
        "name": name,
        "root_dir": root,
        "archived": archived,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


class TestMatchWorkspace:
    def test_精确命中根目录(self) -> None:
        rows = [_ws("/data/a"), _ws("/data/b")]
        assert match_workspace(rows, "/data/a") is rows[0]

    def test_子目录命中父工作区(self) -> None:
        rows = [_ws("/data/proj")]
        assert match_workspace(rows, "/data/proj/src/deep") is rows[0]

    def test_最长前缀优先(self) -> None:
        """嵌套工作区：/data/proj/sub 必须落到 sub，而不是 proj。"""
        outer = _ws("/data/proj")
        inner = _ws("/data/proj/sub")
        assert match_workspace([outer, inner], "/data/proj/sub/x") is inner

    def test_名字前缀不等于路径前缀(self) -> None:
        """``/data/proj2`` 不是 ``/data/proj`` 的子目录——边界必须是路径分隔符。"""
        rows = [_ws("/data/proj")]
        assert match_workspace(rows, "/data/proj2") is None

    def test_无匹配返回空(self) -> None:
        assert match_workspace([_ws("/data/a")], "/elsewhere") is None

    def test_根工作区匹配一切(self) -> None:
        rows = [_ws("/")]
        assert match_workspace(rows, "/anywhere/at/all") is rows[0]

    def test_已归档的也能匹配(self) -> None:
        """匹配是归位，不是放行——归档拦截在发射路径，不在这里。"""
        rows = [_ws("/data/old", archived=True)]
        assert match_workspace(rows, "/data/old") is rows[0]


class TestUniqueWorkspaceName:
    def test_不撞名原样返回(self) -> None:
        assert unique_workspace_name({"a", "b"}, "proj") == "proj"

    def test_撞名追加序号(self) -> None:
        assert unique_workspace_name({"proj"}, "proj") == "proj 2"
        assert unique_workspace_name({"proj", "proj 2"}, "proj") == "proj 3"

    def test_序号不占位就不跳(self) -> None:
        assert unique_workspace_name({"proj", "proj 3"}, "proj") == "proj 2"
