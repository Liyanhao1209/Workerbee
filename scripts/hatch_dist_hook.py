"""把 web/dist 打进 wheel 的构建钩子。

- standard（wheel）构建：web/dist 必须已构建好，否则显式失败——
  不允许悄悄产出一个没有前端的 wheel。
- editable（pip install -e）构建：web/dist 不存在也放行。editable 安装
  主要用于 CI 与源码开发，运行时 server/app.py 的 static_dir 会回退到
  源码布局的 web/dist（开发态）或解释页；在这里强制要求前端产物会让
  任何不构建前端的源码安装直接失败。
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class DistHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        dist = Path(self.root) / "web" / "dist"
        if dist.is_dir():
            build_data["force_include"][str(dist)] = "workerbee/web/dist"
        elif version != "editable":
            raise RuntimeError(
                "web/dist 不存在：先 `cd web && npm ci && npm run build` 再打包"
            )
