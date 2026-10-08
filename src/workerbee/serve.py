"""``workerbee serve`` / ``workerbee stop`` 的编排层（v0.03 Phase 1）。

薄编排：探测既有实例（锁文件 + 探活）、按需拉起 supervisor、然后把控制权交给
现有的 ``workerbee-core`` 入口。这里不实现任何新能力——supervisor 的职责不变，
core 的启动链路不变，本模块只负责「按正确顺序调用现有入口，并把失败如实报出来」。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .paths import default_data_dir, legacy_data_dir_notice, read_token_file, resolve_token

__all__ = [
    "LOCK_NAME",
    "SUPERVISOR_SOCK_NAME",
    "WORKSPACE_CURRENT_KEY",
    "lock_path",
    "read_lock",
    "write_lock",
    "remove_lock",
    "pid_alive",
    "health_ok",
    "port_accepting",
    "probe_instance",
    "supervisor_socket_path",
    "socket_live",
    "spawn_supervisor",
    "wait_for_supervisor",
    "match_workspace",
    "unique_workspace_name",
    "resolve_serve_workspace",
    "run_serve",
    "run_stop",
]

LOCK_NAME = "core.lock"
SUPERVISOR_SOCK_NAME = "supervisor.sock"

#: 等 supervisor socket 就绪的上限。supervisor 启动只做本地初始化，正常在一秒内；
#: 超时说明它没起来，必须显式失败而不是让 core 退化成「连不上 supervisor」继续跑。
SUPERVISOR_READY_TIMEOUT = 15.0

#: 停 core / 等 supervisor 退出的上限。
STOP_TIMEOUT = 20.0

#: meta_kv 里记录「最近一次 serve 匹配/注册的工作区」的键。前端首次打开、
#: 本地尚无记忆时以它为默认选择（见 WorkspaceListResponse.current_workspace_id）。
WORKSPACE_CURRENT_KEY = "serve.current_workspace"


# ---------------------------------------------------------------------------
# 工作区匹配与注册（v0.03 §3、D-B）
# ---------------------------------------------------------------------------


def match_workspace(rows: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    """cwd → 工作区：根目录的最长前缀匹配。

    ``rows`` 的 root_dir 与 ``path`` 都必须是已规范化的绝对路径（resolve 后）。
    根为文件系统根（``/``）时匹配一切——它本来就包含所有路径。
    """
    best: dict[str, Any] | None = None
    for row in rows:
        root = row["root_dir"]
        if root == os.sep:
            matched = path.startswith(root)
        else:
            matched = path == root or path.startswith(root + os.sep)
        if matched and (best is None or len(root) > len(best["root_dir"])):
            best = row
    return best


def unique_workspace_name(existing_names: set[str], base: str) -> str:
    """目录名撞车时追加序号：``proj`` → ``proj 2`` → ``proj 3``…"""
    if base not in existing_names:
        return base
    n = 2
    while f"{base} {n}" in existing_names:
        n += 1
    return f"{base} {n}"


async def resolve_serve_workspace(data_dir: Path, cwd: Path) -> dict[str, Any]:
    """serve 启动时的工作区归位：把 cwd 匹配到工作区，匹配不到就注册一个。

    返回 ``{"workspace": <行>, "created": <bool>}``，并把结果写进 meta_kv
    （``WORKSPACE_CURRENT_KEY``）供 API 层读——前端据此做首次定位。

    匹配不到时的自动注册是**显式**的：banner 会如实说出「已注册新工作区」，
    不会静默把任务落到进程目录（红线：cwd 解析失败显式失败，但这里不是失败——
    是显式注册）。
    """
    from .data.store import Store

    store = await Store.open(str(data_dir / "workerbee.db"))
    try:
        await store.workspaces.ensure_default(
            root_dir=str((data_dir / "workspace").resolve())
        )
        resolved = str(Path(cwd).expanduser().resolve())
        rows = await store.workspaces.list(include_archived=True)
        hit = match_workspace(rows, resolved)
        created = False
        if hit is None:
            base = Path(resolved).name or "workspace"
            name = unique_workspace_name({r["name"] for r in rows}, base)
            hit = await store.workspaces.create(name=name, root_dir=resolved)
            created = True
        await store.db.execute(
            "INSERT INTO meta_kv(k, v, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (WORKSPACE_CURRENT_KEY, hit["workspace_id"], _now_iso()),
        )
        return {"workspace": hit, "created": created}
    finally:
        await store.close()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 实例锁
# ---------------------------------------------------------------------------


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / LOCK_NAME


def read_lock(data_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(lock_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_lock(data_dir: Path, *, pid: int, port: int) -> Path:
    path = lock_path(data_dir)
    path.write_text(
        json.dumps({"pid": pid, "port": port, "started_at": _now_iso()}, indent=2),
        encoding="utf-8",
    )
    return path


def remove_lock(data_dir: Path, *, pid: int) -> None:
    """只清自己写的那把锁。锁指向别的 pid 时不动——那可能是一个还活着的实例。"""
    lock = read_lock(data_dir)
    if lock is not None and int(lock.get("pid") or 0) == pid:
        lock_path(data_dir).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不归我们发信号
    except OSError:
        return False
    # 僵尸进程：kill(pid, 0) 仍判活，但它已经死了（子进程退出后尚未被父进程回收
    # 就是这个形态）。平台相关代码隔离在这一处，非 /proc 平台按存活处理。
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        if stat_text.rpartition(")")[2].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def health_ok(host: str, port: int, *, timeout: float = 1.5) -> bool:
    """GET /api/health。免鉴权的公开探针，正好用来区分「我们的 core」与「别的进程」。"""
    try:
        return httpx.get(f"http://{host}:{port}/api/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 - 探活失败一律按「不健康」处理
        return False


def port_accepting(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_instance(data_dir: Path, host: str) -> tuple[str, dict[str, Any] | None]:
    """实例探测：锁文件 + 进程存活 + 健康探针三者对齐才算「已在运行」。

    - ``none``：没有锁文件；
    - ``stale``：锁指向的进程已不存在（上次没正常退出）；
    - ``running``：进程活着且健康探针通过——直接复用；
    - ``conflict``：进程活着但健康探针不过。可能是 pid 复用，也可能是 core 还没起完；
      两者都不能擅自动它，如实报出让用户处置。
    """
    lock = read_lock(data_dir)
    if lock is None:
        return "none", None
    pid = int(lock.get("pid") or 0)
    port = int(lock.get("port") or 0)
    if not pid_alive(pid):
        return "stale", lock
    if port and health_ok(host, port):
        return "running", lock
    return "conflict", lock


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------


def supervisor_socket_path(data_dir: Path) -> Path:
    return Path(data_dir) / SUPERVISOR_SOCK_NAME


def socket_live(sock: Path, *, timeout: float = 1.0) -> bool:
    """socket 文件存在不等于有人在听（supervisor 自己的存活探测同款逻辑）。"""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
        return True
    except OSError:
        return False


def _supervisor_rpc(sock: Path, method: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """一次性 JSON-RPC 调用。失败返回 None，由调用方决定怎么报。"""
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
            s.sendall((request + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode())
    except (OSError, json.JSONDecodeError):
        return None


def spawn_supervisor(data_dir: Path, adapter_commands: str | None) -> subprocess.Popen:
    """拉起 supervisor 子进程。

    ``start_new_session=True``：它要活到 core 退出之后（这是它存在的理由），
    不能跟着我们收终端的 Ctrl-C。
    """
    argv = [
        sys.executable, "-m", "workerbee.supervisor.main",
        "--data-dir", str(data_dir),
    ]
    if adapter_commands:
        argv += ["--adapter-commands", adapter_commands]
    return subprocess.Popen(argv, start_new_session=True)


def wait_for_supervisor(sock: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_live(sock):
            return True
        time.sleep(0.1)
    return False


def shutdown_supervisor(sock: Path, *, timeout: float = STOP_TIMEOUT) -> bool:
    """经 RPC 让 supervisor 退出，并等到 socket 不再接受连接。"""
    if _supervisor_rpc(sock, "shutdown") is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not socket_live(sock):
            return True
        time.sleep(0.1)
    return not socket_live(sock)


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _open_when_up(url: str, host: str, port: int, *, timeout: float = 20.0) -> None:
    """等 core 真正开始接受请求后再开浏览器；开不了要如实说，不是静默吞掉。"""
    import webbrowser

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(host, port):
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                print(f"[startup] 自动打开浏览器失败（{exc}），请手动访问上面的地址")
            return
        time.sleep(0.25)


def run_serve(
    *,
    data_dir: Path | None,
    host: str,
    port: int,
    token: str | None,
    passphrase: str | None,
    in_process: bool,
    open_browser: bool,
    stop_supervisor_on_exit: bool,
    no_reconcile: bool,
    adapter_commands: str | None,
    supervisor_timeout: float = SUPERVISOR_READY_TIMEOUT,
    cwd: Path | None = None,
) -> int:
    explicit_data_dir = data_dir is not None
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    if not explicit_data_dir:
        notice = legacy_data_dir_notice()
        if notice:
            print(f"[升级提示] {notice}", file=sys.stderr)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 0. 工作区归位（v0.03 §3）：cwd 匹配到工作区，匹配不到就显式注册一个。
    #    失败只降级为警告，不阻断启动——core 起来后仍会确保默认工作区存在。
    try:
        info = asyncio.run(resolve_serve_workspace(data_dir, cwd or Path.cwd()))
        ws = info["workspace"]
        note = "（已注册新工作区）" if info["created"] else ""
        if ws["archived"]:
            note += "（已归档：其下流程冻结，不可发射新任务）"
        print(f"[workspace] 当前工作区：{ws['name']}（{ws['root_dir']}）{note}")
    except Exception as exc:  # noqa: BLE001 - 工作区归位失败不阻断启动，但必须可见
        print(
            f"[workspace] 工作区匹配/注册失败（{type(exc).__name__}: {exc}），"
            f"本次启动不记录当前工作区；core 仍会使用默认工作区",
            file=sys.stderr,
        )

    # 1. 实例探测
    status, lock = probe_instance(data_dir, host)
    if status == "running":
        assert lock is not None
        existing_port = int(lock["port"])
        file_token = read_token_file(data_dir)
        url = f"http://{host}:{existing_port}/"
        url += f"?token={file_token}" if file_token else ""
        print(f"Workerbee 已在运行（pid {lock['pid']}，端口 {existing_port}），不重复启动。")
        print(f"  打开：{url}")
        if not file_token:
            print("  （未找到令牌文件；若该实例不是由 serve 启动的，请用其启动时打印的令牌）")
        if int(port) != existing_port:
            print(f"  （注意：本次指定的 --port {port} 与运行中实例不一致，已被忽略）")
        if open_browser:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  自动打开浏览器失败（{exc}），请手动访问上面的地址")
        return 0
    if status == "conflict":
        assert lock is not None
        print(
            f"错误：实例锁 {lock_path(data_dir)} 指向的进程（pid {lock['pid']}）仍然存活，"
            f"但健康检查未通过。\n"
            f"  它可能仍在启动中（稍后重试），也可能是一个与 Workerbee 无关的进程"
            f"复用了该 pid。\n"
            f"  确认它不是 Workerbee 后可删除锁文件，或用 workerbee stop 处置。",
            file=sys.stderr,
        )
        return 2
    if status == "stale":
        assert lock is not None
        print(f"[startup] 清理陈旧的实例锁（pid {lock.get('pid')} 已不存在）")
        lock_path(data_dir).unlink(missing_ok=True)

    # 2. 端口探测：被占用时显式报错，不让用户面对一个裸的 OSError
    if port_accepting(host, port):
        if health_ok(host, port):
            detail = "占用者看起来是另一个 Workerbee 实例（健康检查通过），但没有对应的实例锁"
        else:
            detail = "占用者不是 Workerbee"
        print(
            f"错误：{host}:{port} 已被占用，{detail}。\n"
            f"  请用 --port 指定其他端口，或先停止占用端口的进程。",
            file=sys.stderr,
        )
        return 2

    # 3. 令牌
    token_value, token_source = resolve_token(token, data_dir)

    # 4. supervisor（默认拉起；--in-process 显式降级）
    sock = supervisor_socket_path(data_dir)
    spawned: subprocess.Popen | None = None
    if in_process:
        print(
            "[session] --in-process：会话由 core 自身托管——core 重启会打断在跑的任务。"
            "生产使用请去掉该选项（默认由 supervisor 托管）。"
        )
    elif socket_live(sock):
        print(f"[session] 复用已在运行的 supervisor（{sock}）")
    else:
        spawned = spawn_supervisor(data_dir, adapter_commands)
        if not wait_for_supervisor(sock, supervisor_timeout):
            spawned.terminate()
            print(
                f"错误：supervisor 未在 {supervisor_timeout:.0f} 秒内就绪（{sock}）。\n"
                f"  请单独运行 workerbee-supervisor --data-dir {data_dir} 查看其输出。",
                file=sys.stderr,
            )
            return 2
        print(f"[session] supervisor 已就绪（pid {spawned.pid}）")

    # 5. 锁 + 同进程起 core
    write_lock(data_dir, pid=os.getpid(), port=port)
    url = f"http://{host}:{port}/?token={token_value}"
    print(f"[startup] 数据目录：{data_dir}")
    print(f"[startup] 令牌来源：{token_source}")
    print(f"[startup] 打开界面：{url}")
    if open_browser:
        threading.Thread(
            target=_open_when_up, args=(url, host, port), daemon=True
        ).start()

    argv = [
        "--data-dir", str(data_dir),
        "--host", host,
        "--port", str(port),
        "--token", token_value,
    ]
    if passphrase:
        argv += ["--passphrase", passphrase]
    if not in_process:
        argv += ["--use-supervisor", "--supervisor-socket", str(sock)]
    if no_reconcile:
        argv.append("--no-reconcile")

    from .server.main import main as core_main

    rc = 1
    try:
        rc = core_main(argv)
    except OSError as exc:
        # 与第 2 步之间的竞态：探测到绑定之间端口被别人抢走。如实翻译，不裸抛。
        print(
            f"错误：内核启动失败（{exc}）。若是地址被占用，请用 --port 换端口。",
            file=sys.stderr,
        )
        rc = 2
    finally:
        remove_lock(data_dir, pid=os.getpid())
        if not in_process:
            if stop_supervisor_on_exit:
                if shutdown_supervisor(sock):
                    print("[shutdown] supervisor 已随 core 一并停止")
                else:
                    print(
                        f"[shutdown] supervisor 未能停止，请人工检查 {sock}",
                        file=sys.stderr,
                    )
                    rc = rc or 2
            else:
                print("[shutdown] core 已退出；supervisor 仍在运行（workerbee stop 可一并停止）")
    return rc


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def run_stop(*, data_dir: Path | None, timeout: float = STOP_TIMEOUT) -> int:
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    ok = True

    # core：经实例锁发 SIGTERM（core 的现有信号链路负责优雅收束）
    lock = read_lock(data_dir)
    if lock is None:
        print("core：未在运行（没有实例锁）")
    else:
        pid = int(lock.get("pid") or 0)
        if pid_alive(pid):
            print(f"core：正在停止（pid {pid}）…")
            os.kill(pid, signal.SIGTERM)
            if _wait_pid_exit(pid, timeout):
                print("core：已停止")
                remove_lock(data_dir, pid=pid)  # serve 退出时也会清，这里幂等
            else:
                ok = False
                print(
                    f"core：{timeout:.0f} 秒内未退出。请人工检查 pid {pid} 的状态。",
                    file=sys.stderr,
                )
        else:
            print(f"core：实例锁指向的 pid {pid} 已不存在，清理陈旧锁")
            lock_path(data_dir).unlink(missing_ok=True)

    # supervisor：经 socket 的 shutdown 命令；socket 残留但无人监听时清理
    sock = supervisor_socket_path(data_dir)
    if socket_live(sock):
        print("supervisor：正在停止…")
        if shutdown_supervisor(sock, timeout=timeout):
            print("supervisor：已停止")
        else:
            ok = False
            print(
                f"supervisor：未在预期时间内停止，请人工检查 {sock}",
                file=sys.stderr,
            )
    elif sock.exists():
        print("supervisor：socket 文件残留但没有进程在监听，已清理")
        sock.unlink(missing_ok=True)
    else:
        print("supervisor：未在运行")

    return 0 if ok else 2
