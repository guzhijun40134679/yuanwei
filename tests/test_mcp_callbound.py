"""交回点乙·第 1 步：Claude Code、Codex 的调用编号绑定，走真正的 stdio MCP 子进程（官方 SDK 客户端，断网沙箱）。
宿主放的 _meta 经协议送到服务；工具参数不是 _meta。MCP 服务要带 --call-binding 才开放读取工具（默认不开放）。"""
import os
import site
import sys

import pytest

pytest.importorskip("mcp")
from mcp import StdioServerParameters  # noqa: E402

from helpers2 import TEXTS, organized  # noqa: E402
from test_mcp_stdio import SRC, text_of, with_server  # noqa: E402
from yuanwei.access import Access  # noqa: E402


def cb_params(tmp_path, host, flag=True):
    env = {"PYTHONPATH": os.pathsep.join([os.path.abspath(SRC), site.getusersitepackages()]), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    args = ["-P", "-m", "yuanwei.mcp", "--db", str(tmp_path / "m.sqlite"), "--workspace", "w", "--host", host] + (["--call-binding"] if flag else [])
    return StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(tmp_path))


def setup(tmp_path, host):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path)
    acc = Access(mem)
    r = acc.register_input(host, "S", "T1", "普通的一句")
    acc.resolve_inputs(host, "S", "T1", [{"input_key": r["input_key"], "verdict": "human", "label": "human"}])
    return mem, acc


def test_without_the_flag_claude_code_and_codex_keep_only_status(tmp_path):
    for host in ("claude-code", "codex"):
        mem, acc = setup(tmp_path / host, host)

        async def go(sess, init):
            assert [t.name for t in (await sess.list_tools()).tools] == ["status"]
            r = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_x", "callId": "exec-1"})
            assert "identity_unproven" in text_of(r) and TEXTS[1] not in text_of(r)
        with_server(cb_params(tmp_path / host, host, flag=False), go)


def test_claude_code_meta_tool_use_id_over_the_wire(tmp_path):
    mem, acc = setup(tmp_path, "claude-code")
    acc.register_call("claude-code", "toolu_ok", "S", "T1")

    async def go(sess, init):
        assert sorted(t.name for t in (await sess.list_tools()).tools) == ["attachments", "get_messages", "recall", "status"]
        ok = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_ok"})
        assert not ok.isError and TEXTS[1] in text_of(ok)
        for meta in (None, {}, {"claudecode/toolUseId": "toolu_unknown"}, {"progressToken": "p"}):
            r = await sess.call_tool("recall", {"query": "烤鱼"}, meta=meta)
            assert "已拒绝" in text_of(r) and TEXTS[1] not in text_of(r)
        spoof = await sess.call_tool("recall", {"query": "烤鱼", "claudecode/toolUseId": "toolu_ok", "tool_use_id": "toolu_ok"})
        assert "call_" in text_of(spoof) and TEXTS[1] not in text_of(spoof)
        for name, a in (("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"}), ("status", {})):
            assert "call_unregistered" in text_of(await sess.call_tool(name, a, meta={"claudecode/toolUseId": "toolu_unknown"}))
    with_server(cb_params(tmp_path, "claude-code"), go)


def test_codex_nested_turn_metadata_over_the_wire_and_a_mismatch_is_refused(tmp_path):
    mem, acc = setup(tmp_path, "codex")
    acc.register_call("codex", "exec-ok", "S", "T1")
    tm = {"turn_id": "T1", "session_id": "S", "thread_id": "S", "turn_trigger": "composer", "thread_source": "user"}

    async def go(sess, init):
        ok = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"callId": "exec-ok", "sessionId": "S", "threadId": "S", "x-codex-turn-metadata": tm})
        assert not ok.isError and TEXTS[1] in text_of(ok)
        bad = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"callId": "exec-ok", "x-codex-turn-metadata": dict(tm, turn_id="T9")})
        assert "call_mismatch" in text_of(bad) and TEXTS[1] not in text_of(bad)
        miss = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"callId": "exec-ok"})
        assert "call_meta_incomplete" in text_of(miss)
    with_server(cb_params(tmp_path, "codex"), go)


def test_a_banned_turn_still_blocks_a_registered_call_over_the_wire(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    r = acc.register_input("claude-code", "S", "T1", "#yuanwei-nomem\n查烤鱼")
    acc.resolve_inputs("claude-code", "S", "T1", [{"input_key": r["input_key"], "verdict": "human"}])
    acc.register_call("claude-code", "toolu_b", "S", "T1")

    async def go(sess, init):
        res = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_b"})
        assert "control_nomem" in text_of(res) and TEXTS[1] not in text_of(res)
    with_server(cb_params(tmp_path, "claude-code"), go)
