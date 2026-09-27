#!/usr/bin/env python
"""GUI 冒烟检查：用无头浏览器真的打开每个页面，收集控制台错误并截图。

这是「界面能不能用」的机器判据，不替代人工验收——它只能回答「有没有白屏、
有没有 JS 报错、关键元素在不在」，回答不了「这个交互顺不顺手」。

用法：
    .venv/bin/python -m playwright install chromium   # 一次性
    .venv/bin/python scripts/gui_smoke.py --token <TOKEN> --base http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import ConsoleMessage, Page, async_playwright

ROUTES = [
    ("workflows", "/#/workflows", ["流程"]),
    ("tasks", "/#/tasks", ["任务"]),
    ("sessions", "/#/sessions", ["会话"]),
    ("registry", "/#/registry", ["注册"]),
    ("templates", "/#/templates", ["模板"]),
    ("storage", "/#/storage", ["存储"]),
    ("execution", "/#/execution", ["执行"]),
]


async def _login(page: Page, token: str) -> None:
    """首次访问会出现令牌输入框；把它填上。"""
    try:
        await page.wait_for_selector("input[type=password], input#token, input[name=token]",
                                     timeout=3000)
    except Exception:  # noqa: BLE001 - 没有令牌门就直接进
        return
    field = page.locator("input[type=password], input#token, input[name=token]").first
    await field.fill(token)
    for label in ("进入", "连接", "保存", "确定", "登录"):
        btn = page.get_by_role("button", name=label)
        if await btn.count() > 0:
            await btn.first.click()
            break
    await page.wait_for_timeout(800)


async def _check(route_name: str, path: str, needles: list[str], base: str,
                 token: str, out_dir: Path) -> dict:
    errors: list[str] = []
    page_errors: list[str] = []
    result: dict = {"route": route_name, "path": path, "ok": False, "errors": [],
                    "screenshot": None, "body_chars": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        await ctx.add_init_script(
            f"window.localStorage.setItem('workerbee.token', {json.dumps(token)});"
        )
        page = await ctx.new_page()

        def _on_console(msg: ConsoleMessage) -> None:
            if msg.type in ("error",):
                errors.append(f"{msg.type}: {msg.text[:300]}")

        page.on("console", _on_console)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))

        try:
            await page.goto(f"{base}{path}", wait_until="networkidle", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"导航失败：{type(exc).__name__}: {exc}")
            await browser.close()
            return result

        await _login(page, token)
        await page.wait_for_timeout(1200)

        body = await page.inner_text("body")
        result["body_chars"] = len(body.strip())
        lowered = body.lower()
        missing = [n for n in needles if n.lower() not in lowered]

        shot = out_dir / f"{route_name}.png"
        await page.screenshot(path=str(shot), full_page=True)
        result["screenshot"] = str(shot)

        result["errors"] = errors + page_errors
        if missing:
            result["errors"].append(f"页面未出现预期内容：{missing}")
        if result["body_chars"] < 40:
            result["errors"].append(f"页面几乎是空白的（正文 {result['body_chars']} 字）")

        result["ok"] = not result["errors"]
        await browser.close()
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", default=".workerbee/gui-smoke")
    ap.add_argument("--task", default=None, help="额外检查某个任务详情页")
    ap.add_argument("--workflow", default=None, help="额外检查某个流程编辑器（画布）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    routes = list(ROUTES)
    if args.workflow:
        routes.append(("editor", f"/#/workflows/{args.workflow}", ["节点", "校验"]))
    if args.task:
        routes.append(("task-detail", f"/#/tasks/{args.task}", ["阶段"]))
        routes.append(("task-graph", f"/#/execution/{args.task}", ["执行"]))

    results = []
    for name, path, needles in routes:
        results.append(await _check(name, path, needles, args.base, args.token, out_dir))

    print(f"{'页面':<12} {'结果':<6} 说明")
    print("-" * 78)
    bad = 0
    for r in results:
        status = "OK" if r["ok"] else "FAIL"
        if not r["ok"]:
            bad += 1
        detail = "；".join(r["errors"])[:180] if r["errors"] else f"{r['body_chars']} 字"
        print(f"{r['route']:<12} {status:<6} {detail}")

    print()
    print(f"截图目录：{out_dir.resolve()}")
    print(f"结论：{'全部页面可用' if bad == 0 else f'{bad} 个页面有问题'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
