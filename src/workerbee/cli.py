"""`workerbee` 命令行客户端（架构设计 v0.02 §12 tui/ 的可选终端入口，UI-03）。

它**复用同一套 API 语义**：所有命令都打到 core 的 HTTP 接口，不另造一套任务事实。
这样「终端看到的状态」与「网页看到的状态」必然一致——因为它们本来就是同一份。

命令行存在的意义是运维修理：网页挂了、或要在脚本里跑，还能诊断和操作。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from . import __version__
from .adapters.sdk.executables import resolve as resolve_executable

app = typer.Typer(
    name="workerbee",
    help="Workerbee —— 多 agent harness 协作 workflow 框架",
    no_args_is_help=True,
    add_completion=False,
)
registry_app = typer.Typer(help="共享配置注册表")
task_app = typer.Typer(help="任务操作")
workflow_app = typer.Typer(help="Workflow 操作")
app.add_typer(registry_app, name="registry")
app.add_typer(task_app, name="task")
app.add_typer(workflow_app, name="workflow")


DEFAULT_API = os.environ.get("WORKERBEE_API", "http://127.0.0.1:8765")
DEFAULT_PORT = 8765


def _read_registrations(db: Path) -> list[dict[str, Any]] | None:
    """只读地看一眼注册表。读不到返回 None。

    doctor 是离线命令，不该为了看这么一眼就去装配整个内核（那会起连接、跑迁移）。
    以只读模式打开，也不会干扰正在运行的内核。
    """
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT harness_id, exec_path, last_probe_ok, last_probe_error "
                    "FROM harness_registration ORDER BY harness_id"
                )
            ]
    except sqlite3.Error:
        return None


def _client(base_url: str, token: str | None):
    from .client import ApiClient

    return ApiClient(base_url, token=token or os.environ.get("WORKERBEE_TOKEN"))


def _print(value: Any, *, as_json: bool = False) -> None:
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    elif isinstance(value, dict):
        for k, v in value.items():
            typer.echo(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    else:
        typer.echo(str(value))


# ===========================================================================
# 诊断
# ===========================================================================


@app.command()
def version() -> None:
    """显示版本。"""
    typer.echo(f"workerbee {__version__}")


@app.command()
def doctor(
    data_dir: Path = typer.Option(Path(".workerbee"), "--data-dir"),
    api: str = typer.Option(DEFAULT_API, "--api"),
) -> None:
    """检查本机环境是否具备跑通 Workerbee 的条件。

    每一项都给出**可执行的结论**，而不是「未检测到」这种没用的输出。
    """
    ok = True
    typer.echo(f"workerbee {__version__}")
    typer.echo("")

    typer.echo("依赖（以下是你当前 shell 看到的）")
    found: dict[str, str] = {}
    for cmd, why in (
        ("claude", "Claude Code harness 适配器"),
        ("kimi", "Kimi Code harness 适配器"),
        ("git", "节点在仓库里工作时的常见前置"),
    ):
        path = shutil.which(cmd)
        if path:
            found[cmd] = path
            typer.echo(f"  [ok] {cmd:8s} {path}")
        else:
            typer.echo(f"  [--] {cmd:8s} 未找到（{why} 将不可用）")

    # 上面那节查的是**你当前 shell**。真正拉起 harness 的是守护进程，而服务形态
    # 拿到的是 systemd 的默认 PATH，与你 shell 的不是同一个。这里用适配层同一套
    # 解析跑一遍，报出它实际会用的路径——「你的 shell 找得到」不代表「它也找得到」，
    # 这两件事的输出长得几乎一样，是这类故障最难反推的地方。
    resolved = {cmd: resolve_executable(None, cmd) for cmd in ("claude", "kimi", "git")}
    typer.echo("")
    typer.echo("  守护进程解析（内核以服务运行时用的是这套）")
    for cmd, path in resolved.items():
        typer.echo(f"    [{'ok' if path else '--'}] {cmd:8s} {path or '找不到'}")

    gap = [c for c, p in resolved.items() if p is None and c in found]
    if gap:
        typer.echo("")
        typer.echo("  [!!] 以下可执行文件你的 shell 找得到，但守护进程找不到：")
        for cmd in gap:
            typer.echo(f"       {cmd:8s} {found[cmd]}")
        typer.echo("       把内核作为服务运行时，探测与派发都会失败。二选一：")
        typer.echo("         1. 在注册表的「可执行路径」里填上面的绝对路径（推荐，按 harness 精确指定）")
        typer.echo("         2. 在 service unit 里设 Environment=PATH=... 把它并进去")

    typer.echo("")
    typer.echo("数据目录")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        typer.echo(f"  [ok] {data_dir.resolve()} 可写")
    except OSError as exc:
        ok = False
        typer.echo(f"  [!!] {data_dir.resolve()} 不可写：{exc}")

    db = data_dir / "workerbee.db"
    if db.exists():
        size = db.stat().st_size
        typer.echo(f"  [ok] 数据库 {db}（{size / 1024:.0f} KB）")
    else:
        typer.echo(f"  [--] 数据库尚未创建：{db}（首次启动 core 时生成）")

    vault = data_dir / "secrets.vault"
    typer.echo(
        f"  [{'ok' if vault.exists() else '--'}] 凭据库 {vault}"
        f"{'' if vault.exists() else '（尚未创建）'}"
    )

    typer.echo("")
    typer.echo("已登记的 harness")
    rows = _read_registrations(db)
    if rows is None:
        typer.echo("  [--] 读不到注册表（数据库尚未创建）")
    elif not rows:
        typer.echo("  [--] 尚未登记任何 harness")
    else:
        for row in rows:
            exec_path = row["exec_path"] or "（未指定，由适配器自动解析）"
            if row["last_probe_ok"] is None:
                probe = "尚未探测"
            elif row["last_probe_ok"]:
                probe = "成功"
            else:
                detail = (row["last_probe_error"] or "").strip().replace("\n", " ")
                probe = f"失败：{detail[:70]}"
            typer.echo(f"  {row['harness_id']:12s} 可执行路径 = {exec_path}")
            typer.echo(f"  {'':12s} 最近探测   = {probe}")

    typer.echo("")
    typer.echo("内核")
    try:
        import httpx

        r = httpx.get(f"{api}/api/health", timeout=3.0)
        if r.status_code == 200:
            typer.echo(f"  [ok] {api} 可达：{r.json()}")
        else:
            typer.echo(f"  [!!] {api} 返回 {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"  [--] {api} 不可达：{type(exc).__name__}")
        typer.echo("       启动内核：workerbee-core --data-dir " + str(data_dir))

    typer.echo("")
    typer.echo("结论：" + ("环境就绪" if ok else "存在阻断项，见上面的 [!!]"))
    raise typer.Exit(code=0 if ok else 1)


# ===========================================================================
# 服务
# ===========================================================================


@app.command()
def core(
    data_dir: Path = typer.Option(Path(".workerbee"), "--data-dir"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(DEFAULT_PORT, "--port"),
    token: Optional[str] = typer.Option(None, "--token", envvar="WORKERBEE_TOKEN"),
    allow_remote: bool = typer.Option(False, "--allow-remote", help="允许非 loopback 访问（有风险）"),
    passphrase: Optional[str] = typer.Option(
        None, "--passphrase", envvar="WORKERBEE_PASSPHRASE", help="凭据库口令"
    ),
    use_supervisor: bool = typer.Option(
        False, "--use-supervisor", help="把 harness 子进程交给 supervisor 托管（生产建议开启）"
    ),
    no_reconcile: bool = typer.Option(False, "--no-reconcile"),
) -> None:
    """启动内核（API + 调度）。

    直接转发给 `workerbee-core`；这里只做参数转写，避免两套参数解析漂移。
    """
    argv = [
        "workerbee-core",
        "--data-dir", str(data_dir),
        "--host", host,
        "--port", str(port),
    ]
    if token:
        argv += ["--token", token]
    if passphrase:
        argv += ["--passphrase", passphrase]
    if allow_remote:
        argv.append("--allow-remote")
    if no_reconcile:
        argv.append("--no-reconcile")

    from .server.main import main as core_main

    raise typer.Exit(code=core_main(argv))


@app.command()
def supervisor(
    data_dir: Path = typer.Option(Path(".workerbee"), "--data-dir"),
) -> None:
    """启动 Session 托管进程（独立于内核，持有 harness 子进程）。"""
    from .supervisor.main import main as supervisor_main

    sys.argv = ["workerbee-supervisor", "--data-dir", str(data_dir)]
    supervisor_main()


# ===========================================================================
# 查询与操作
# ===========================================================================


@app.command()
def status(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """内核状态。"""
    _print(_client(api, token).get("/api/system/status"), as_json=as_json)


@app.command()
def attention(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """「需处理」清单：审批、失败任务、状态不明、清理未完成。"""
    _print(_client(api, token).get("/api/attention"), as_json=as_json)


@workflow_app.command("list")
def workflow_list(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出 Workflow。"""
    _print(_client(api, token).get("/api/workflows"), as_json=as_json)


@workflow_app.command("show")
def workflow_show(
    workflow_id: str,
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """查看某个 Workflow。"""
    _print(_client(api, token).get(f"/api/workflows/{workflow_id}"), as_json=as_json)


@workflow_app.command("validate")
def workflow_validate(
    workflow_id: str,
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
) -> None:
    """校验当前修订。错误的定位信息会一并打印。"""
    client = _client(api, token)
    report = client.post(f"/api/workflows/{workflow_id}/validate", json={"mode": "publish"})
    for d in report.get("diagnostics", []):
        loc = d.get("node_name") or d.get("node_id") or (d.get("edge") or ["", ""])[0] or "全局"
        typer.echo(f"[{d.get('severity', '?')}] {loc}: {d.get('message')}")
        if d.get("hint"):
            typer.echo(f"        → {d['hint']}")
    typer.echo(f"结论：{'通过' if report.get('ok') else '未通过'}")


@app.command("submit")
def submit(
    workflow_id: str,
    input_text: str = typer.Argument(..., help="任务输入（纯文本，或 JSON 字符串）"),
    priority: int = typer.Option(50, "--priority"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """向 Workflow 提交一个任务。"""
    try:
        payload = json.loads(input_text)
        if not isinstance(payload, dict):
            payload = {"task": payload}
    except json.JSONDecodeError:
        payload = {"task": input_text}

    result = _client(api, token).post(
        f"/api/workflows/{workflow_id}/tasks",
        json={"input_payload": payload, "priority": priority, "idempotency_key": idempotency_key},
    )
    if not result.get("accepted", True):
        typer.echo("提交被拒绝：", err=True)
        for d in (result.get("report") or {}).get("diagnostics", []):
            typer.echo(f"  [{d.get('severity')}] {d.get('message')}", err=True)
            if d.get("hint"):
                typer.echo(f"      → {d['hint']}", err=True)
        raise typer.Exit(code=2)
    _print(result, as_json=as_json)


@task_app.command("list")
def task_list(
    workflow_id: Optional[str] = typer.Option(None, "--workflow"),
    state: Optional[str] = typer.Option(None, "--state"),
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出任务。"""
    params = {k: v for k, v in (("workflow_id", workflow_id), ("state", state)) if v}
    _print(_client(api, token).get("/api/tasks", params=params), as_json=as_json)


@task_app.command("show")
def task_show(
    task_id: str,
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """查看任务详情（阶段、尝试、产物、审批）。"""
    _print(_client(api, token).get(f"/api/tasks/{task_id}"), as_json=as_json)


@task_app.command("pause")
def task_pause(task_id: str, from_node: Optional[str] = typer.Option(None, "--from-node"),
               api: str = typer.Option(DEFAULT_API, "--api"),
               token: Optional[str] = typer.Option(None, "--token")) -> None:
    """暂停整次任务。"""
    _print(_client(api, token).post(f"/api/tasks/{task_id}/pause",
                                    json={"from_node_id": from_node}), as_json=True)


@task_app.command("resume")
def task_resume(task_id: str, restart_failed: bool = typer.Option(False, "--restart-failed"),
                api: str = typer.Option(DEFAULT_API, "--api"),
                token: Optional[str] = typer.Option(None, "--token")) -> None:
    """恢复被暂停的任务。"""
    _print(_client(api, token).post(f"/api/tasks/{task_id}/resume",
                                    json={"restart_failed": restart_failed}), as_json=True)


@task_app.command("delete")
def task_delete(task_id: str, from_node: Optional[str] = typer.Option(None, "--from-node"),
                api: str = typer.Option(DEFAULT_API, "--api"),
                token: Optional[str] = typer.Option(None, "--token")) -> None:
    """主动删除任务（不可续跑）。"""
    _print(_client(api, token).delete(f"/api/tasks/{task_id}",
                                      json={"from_node_id": from_node}), as_json=True)


@registry_app.command("harnesses")
def registry_harnesses(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    probe: Optional[str] = typer.Option(
        None, "--probe", help="探测指定 harness 的能力（HAR-02：实测优先）"
    ),
    as_json: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """列出已登记的 harness 及其能力。

    路径与网关的实际挂载一致（扁平前缀 ``/api/harnesses``）。
    """
    client = _client(api, token)
    if probe:
        _print(client.post(f"/api/harnesses/{probe}/probe"), as_json=True)
        return
    _print(client.get("/api/harnesses"), as_json=as_json)


@app.command()
def approvals(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出待处理审批。"""
    _print(_client(api, token).get("/api/approvals"), as_json=as_json)


@app.command()
def storage(
    api: str = typer.Option(DEFAULT_API, "--api"),
    token: Optional[str] = typer.Option(None, "--token"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """存储占用（RES-03：提供查看入口，不强制自动清理）。"""
    _print(_client(api, token).get("/api/storage"), as_json=as_json)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
