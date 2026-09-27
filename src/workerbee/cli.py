"""`workerbee` 命令行客户端（架构设计 v0.02 §12 tui/ 的可选终端入口，UI-03）。

它**复用同一套 API 语义**：所有命令都打到 core 的 HTTP 接口，不另造一套任务事实。
这样「终端看到的状态」与「网页看到的状态」必然一致——因为它们本来就是同一份。

命令行存在的意义是运维修理：网页挂了、或要在脚本里跑，还能诊断和操作。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from . import __version__

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

    typer.echo("依赖")
    for cmd, why in (
        ("claude", "Claude Code harness 适配器"),
        ("kimi", "Kimi Code harness 适配器"),
        ("git", "节点在仓库里工作时的常见前置"),
    ):
        path = shutil.which(cmd)
        if path:
            typer.echo(f"  [ok] {cmd:8s} {path}")
        else:
            typer.echo(f"  [--] {cmd:8s} 未找到（{why} 将不可用）")

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
    as_json: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """列出已登记的 harness 及其能力。"""
    _print(_client(api, token).get("/api/registry/harnesses"), as_json=as_json)


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
