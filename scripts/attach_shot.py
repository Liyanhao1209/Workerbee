"""截一张「接入会话」控制台打开后的界面。

gui_smoke.py 只做逐页检查（有没有白屏、有没有 JS 报错），它不点按钮，所以看不到
浮层类界面。这个脚本补的正是那一段：提交一个任务 → 等出活会话 → 点「接入」→ 截图。

    .venv/bin/python scripts/attach_shot.py --token <TOKEN> --workflow <WF_ID>

会消耗一次真实 harness 调用。截图落在 <data-dir>/gui-smoke/attach-console.png。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(".workerbee/gui-smoke/attach-console.png")


def _api(base: str, token: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Workerbee-Token": token, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


async def _wait_for_live_session(base: str, token: str, task_id: str) -> str | None:
    """等这个任务出现一个活着的会话。"""
    for _ in range(40):
        try:
            data = _api(base, token, "/api/sessions")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(2)
            continue
        for s in data.get("sessions", []):
            if s.get("owner_task_id") == task_id and s.get("state") == "alive":
                return str(s["session_ref"])
        await asyncio.sleep(2)
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--text", default="用一句话说明什么是幂等性，不要使用任何工具。")
    args = ap.parse_args()

    result = _api(
        args.base,
        args.token,
        f"/api/workflows/{args.workflow}/tasks",
        {"input_payload": {"task": args.text}, "priority": 50},
    )
    task_id = result.get("task_id")
    if not task_id:
        print(f"提交被拒绝：{result}", file=sys.stderr)
        return 1
    print(f"已提交任务 {task_id}，等会话出现……")

    session_ref = await _wait_for_live_session(args.base, args.token, task_id)
    if session_ref is None:
        print("没等到活会话（任务可能太快跑完了）", file=sys.stderr)
        return 1
    print(f"会话 {session_ref} 已就绪，打开界面……")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 980})
        page = await ctx.new_page()
        await page.add_init_script(
            f"window.localStorage.setItem('workerbee.token', {json.dumps(args.token)});"
        )
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto(f"{args.base}/#/sessions", wait_until="networkidle", timeout=20000)
        # 只点这个会话那一行的「接入」，免得点到别的行
        row = page.locator("tr", has_text=session_ref[:12]).first
        await row.get_by_role("button", name="接入").click()
        await page.wait_for_selector("text=接入会话", timeout=10000)
        # 让轮询跑一轮，把输出填进来
        await asyncio.sleep(4)
        await page.screenshot(path=str(OUT), full_page=False)

        print(f"截图：{OUT}")
        if errors:
            print("控制台错误：")
            for e in errors:
                print("  -", e)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
