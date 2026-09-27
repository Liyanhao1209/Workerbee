"""权限钩子的可离线验证部分（HUM-03/04、AC-14）。

背景：claude 的权限钩子**不在 CLI 的 flag 列表里**，而在 host 控制协议里——
启动时发 ``initialize`` 注册 ``PermissionRequest`` 钩子，harness 需要授权时
反过来发 ``control_request`` 回调。官方 Agent SDK 就是这么做的
（它设 ``CLAUDE_CODE_ENTRYPOINT=sdk-ts`` 再发那条 initialize）。

这里钉住的是几个「错了不会报错、只是功能悄悄不生效」的点：
能力声明是否随通道翻转、动作描述怎么翻译、答复帧的形状、
以及审批的两个字段有没有真的落库。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from workerbee.adapters.claude_code.adapter import (
    ClaudeCodeAdapter,
    _build_manifest,
)
from workerbee.adapters.sdk.cli import _describe_tool_input, _target_of
from workerbee.adapters.sdk.contract import CreateSessionRequest, HarnessConfig

pytestmark = pytest.mark.unit


# ===========================================================================
# 能力声明
# ===========================================================================


def test_permission_hook_follows_the_input_channel():
    """有控制通道才有钩子。

    声明错了的两个方向都有代价：说 ``True`` 但实际没有，会让 harness 卡在一个
    无人应答的提问上；说 ``False`` 但实际有，会把用户最想要的「遇事问我」模式
    误判成不可用（HUM-03 的门禁会拦住它）。
    """
    with_channel = _build_manifest(True)
    without = _build_manifest(False)

    assert with_channel.capabilities.permission_hook is True
    assert with_channel.capabilities.interact is True
    assert without.capabilities.permission_hook is False
    assert without.capabilities.interact is False


def test_default_is_a_supported_permission_mode():
    """``default`` 不在 --help 的 choices 里，但确实被接受（实测）。

    漏掉它会让用户最想要的那个「遇事就问我」填不进去。
    """
    caps = _build_manifest(True).capabilities
    assert "default" in caps.permission_modes
    # 它会询问，因此**不能**出现在「不询问」那一组里。
    assert "default" not in caps.non_interactive_modes


# ===========================================================================
# 权限提问参数
# ===========================================================================


def _request(**extra: Any) -> CreateSessionRequest:
    return CreateSessionRequest(
        harness=HarnessConfig(harness_id="h"), model_name="m", extra=extra
    )


def _argv(adapter: ClaudeCodeAdapter, request: CreateSessionRequest) -> list[str]:
    return adapter.build_argv(
        request=request, prompt="跑", resume_locator=None, checkpoint=None
    )


def test_permission_prompts_target_follows_the_channel():
    """这一行曾经硬编码成 ``none``（「没人回答，一律拒绝」）。

    后果特别难反推：钩子接通了、审批收到了、用户也批了，**命令却没执行**——
    因为 CLI 早已自行拒绝。有控制通道时我们**就是**那个 host。
    """
    with_channel = ClaudeCodeAdapter(input_format="stream-json")
    without = ClaudeCodeAdapter(input_format="text")

    argv_yes = _argv(with_channel, _request())
    argv_no = _argv(without, _request())

    assert _flag_value(argv_yes, "--permission-prompts") == "host"
    assert _flag_value(argv_no, "--permission-prompts") == "none"


def test_explicit_permission_prompts_still_wins():
    """注册表里的显式配置优先，不被默认值覆盖。"""
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    argv = _argv(adapter, _request(options={"permission_prompts": "none"}))
    assert _flag_value(argv, "--permission-prompts") == "none"


def test_host_env_is_set_only_on_the_control_channel(monkeypatch):
    """这两个变量告诉 CLI「上面有一个 host」。

    少了它们，CLI 遇到需要授权的操作时**不是不问，而是直接拒绝**。

    注意：测试进程自己就跑在 claude 里，``os.environ`` 里本来可能带着这个变量，
    所以先把环境清干净再断言，否则测的是宿主环境而不是被测逻辑。
    """
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)

    adapter = ClaudeCodeAdapter(input_format="stream-json")
    env = adapter.build_env(_request(), {})
    assert env.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-ts"

    text_adapter = ClaudeCodeAdapter(input_format="text")
    text_env = text_adapter.build_env(_request(), {})
    assert "CLAUDE_CODE_ENTRYPOINT" not in text_env


def test_initialize_registers_the_permission_hook():
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    session = _FakeSession("abcd1234efgh")
    payload = adapter.initialize_request(session)

    assert payload is not None
    assert payload["subtype"] == "initialize"
    hooks = payload["hooks"]["PermissionRequest"]
    assert len(hooks) == 1
    assert hooks[0]["hookCallbackIds"], "必须给出回调 id，否则 harness 不知道回调谁"
    assert session.hook_callback_ids["PermissionRequest"] in hooks[0]["hookCallbackIds"]


def test_text_channel_has_no_initialize():
    """没有控制通道就别发握手——发不出去，也没人回。"""
    adapter = ClaudeCodeAdapter(input_format="text")
    assert adapter.initialize_request(_FakeSession("x")) is None


# ===========================================================================
# 载荷翻译
# ===========================================================================


def test_action_description_prefers_the_meaningful_field():
    assert _describe_tool_input("Bash", {"command": "rm -rf build/"}) == "Bash(rm -rf build/)"
    assert _describe_tool_input("Read", {"file_path": "/etc/hosts"}) == "Read(/etc/hosts)"
    assert _describe_tool_input("WebFetch", {"url": "https://x"}) == "WebFetch(https://x)"


def test_action_description_never_returns_empty():
    """没有已知字段时至少给出键名——空字符串在界面上等于没信息。"""
    assert _describe_tool_input("Odd", {}) == "Odd"
    described = _describe_tool_input("Odd", {"alpha": 1, "beta": 2})
    assert "alpha" in described and "beta" in described


def test_target_is_none_when_unknown():
    """取不到就返回 None，不编造。"""
    assert _target_of({"command": "ls"}) == "ls"
    assert _target_of({"unknown_key": "x"}) is None


def test_permission_request_carries_harness_context():
    """把 harness 给的东西尽量带上：它知道「为什么拦」，我们拼不出来。"""
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    session = _FakeSession("sess1234")
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi > /tmp/x"},
        "decision_reason": "路径在工作目录之外",
        "cwd": "/home/u/proj",
        "permission_mode": "default",
    }
    req = adapter.build_permission_request(session, "req-1", payload)

    assert req.tool_name == "Bash"
    assert req.action == "Bash(echo hi > /tmp/x)"
    assert req.target == "echo hi > /tmp/x"
    assert req.risk == "路径在工作目录之外"
    assert req.raw["request_id"] == "req-1"
    assert req.session_ref == session.session_ref


# ===========================================================================
# 答复帧形状
# ===========================================================================


async def test_respond_frame_is_wrapped_in_hook_specific_output():
    """答复必须包在 ``hookSpecificOutput`` 里并带上事件名。

    裸的 ``{"behavior": "allow"}`` 会被 harness 当成「没听懂」而退回自行拒绝。
    现象与「--permission-prompts 用了 none」几乎一样：
    审批收到了、也批了、命令却没执行。
    """
    adapter = _ready_adapter("sess-xyz")
    session = adapter._sessions["sess-xyz"]
    session.pending_hooks["ap-1"] = "req-1"

    result = await adapter.on_permission_respond(
        {"approval_id": "ap-1", "decision": "approve"}
    )
    assert result["ok"] is True

    frame = json.loads(session.written[-1])
    assert frame["type"] == "control_response"
    inner = frame["response"]["response"]
    assert inner["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert inner["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert frame["response"]["request_id"] == "req-1"
    assert "ap-1" not in session.pending_hooks, "答复过的钩子必须出队"


async def test_deny_frame_says_deny():
    adapter = _ready_adapter("sess-deny")
    session = adapter._sessions["sess-deny"]
    session.pending_hooks["ap-2"] = "req-2"

    await adapter.on_permission_respond({"approval_id": "ap-2", "decision": "deny"})
    frame = json.loads(session.written[-1])
    decision = frame["response"]["response"]["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert decision.get("message")


async def test_duplicate_respond_is_rejected_not_ignored():
    """AC-14：重复通知不重复授权。"""
    from workerbee.adapters.sdk.protocol import AdapterError

    adapter = _ready_adapter("sess-dup")
    session = adapter._sessions["sess-dup"]
    session.pending_hooks["ap-3"] = "req-3"

    await adapter.on_permission_respond({"approval_id": "ap-3", "decision": "approve"})

    with pytest.raises(AdapterError):
        await adapter.on_permission_respond(
            {"approval_id": "ap-3", "decision": "approve"}
        )


# ===========================================================================
# 持久化：审批的两个字段
# ===========================================================================


async def test_tool_name_and_fingerprint_survive_a_round_trip(store):
    """这两个字段曾经只活在内存里，回读就丢。

    ``action_fingerprint`` 尤其要紧：AC-14 的「命令内容变化即旧批准作废」
    靠它比较，丢了它这条规则永远判成「没变」。
    """
    from tests.conftest import make_approval

    approval = make_approval()
    approval.tool_name = "Bash"
    approval.bound_to.action_fingerprint = "abc123"
    await store.approvals.create(approval)

    got = await store.approvals.get(approval.approval_id)
    assert got is not None
    assert got.tool_name == "Bash"
    assert got.bound_to.action_fingerprint == "abc123"

    # 换了命令内容，指纹就变了 —— 旧批准据此作废
    changed = got.action_changed("different")
    assert changed is True


# ===========================================================================


def _flag_value(argv: list[str], flag: str) -> str | None:
    for i, item in enumerate(argv):
        if item == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _ready_adapter(session_ref: str) -> ClaudeCodeAdapter:
    """构造一个已登记会话、且事件上报被打断的适配器。

    单测里没有 ``run()``，SDK 的输出通道不存在，``emit_event`` 会炸；
    这里把它换掉——测的是答复帧的内容与时机，不是事件上报。
    """
    adapter = ClaudeCodeAdapter(input_format="stream-json")
    session = _FakeSession(session_ref)

    async def _noop_event(*args: Any, **kwargs: Any) -> None:
        session.events.append((args, kwargs))

    adapter.emit_event = _noop_event  # type: ignore[method-assign]
    adapter._sessions[session_ref] = session
    return adapter


class _FakeSession:
    """够用的会话替身：只承载钩子映射与写出的帧。"""

    def __init__(self, ref: str) -> None:
        self.session_ref = ref
        self.attempt_id = "at-1"
        self.pending_hooks: dict[str, str] = {}
        self.hook_callback_ids: dict[str, str] = {}
        self.written: list[str] = []
        self.events: list[Any] = []
        self.proc = _FakeProc(self.written)


class _FakeStdin:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink
        self._buf = ""

    def write(self, data: bytes) -> None:
        self._buf += data.decode()
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.sink.append(line)

    def is_closing(self) -> bool:
        return False

    async def drain(self) -> None:
        return None


class _FakeProc:
    def __init__(self, sink: list[str]) -> None:
        self.stdin = _FakeStdin(sink)
