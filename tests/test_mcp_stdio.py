"""V7（第二段·交回点甲）：官方 SDK 的客户端，在断网沙箱里启动当次测试用的 stdio MCP 子进程，测完就退出。
证明的是 MCP 服务这一层（工具、身份源、权限、返回大小、分页、出错），不是四家宿主已经接通；这里的「登记方」是测试自己，扮演可信的接头。"""
import asyncio
import json
import os
import site
import subprocess
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from helpers2 import TEXTS, organized  # noqa: E402
from yuanwei import hosts as H  # noqa: E402
from yuanwei.access import Access  # noqa: E402
from yuanwei.mcp import server as S  # noqa: E402
from yuanwei.rules import TOOLS_RULE  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def params(tmp_path, host, handle=None, mode="tools_only", db="m.sqlite", ws="w"):
    env = {"PYTHONPATH": os.pathsep.join([os.path.abspath(SRC), site.getusersitepackages()]), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    if handle is not None:
        env[S.ENV_CONNECTION] = handle
    return StdioServerParameters(command=sys.executable, args=["-P", "-m", "yuanwei.mcp", "--db", str(tmp_path / db), "--workspace", ws, "--host", host, "--mode", mode], env=env, cwd=str(tmp_path))


def with_server(p, fn, timeout=90):
    async def main():
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as sess:
                init = await sess.initialize()
                return await fn(sess, init)
    return asyncio.run(asyncio.wait_for(main(), timeout))


def text_of(res):
    return res.content[0].text


def raw(res):
    return res.model_dump(by_alias=True)


def pi_setup(tmp_path, texts=None, turn_text=None):
    mem = organized(tmp_path, texts=texts)
    acc = Access(mem)
    handle = acc.register_connection("pi", "ps1", turn_id="T1")                  # 逐轮能力：这个句柄只证明 T1（会话级句柄只证明会话，读取会被拒，见下面那条）
    acc.begin_turn("pi", "ps1", "T1", text=turn_text)
    return mem, acc, handle


# ------------------------------------------------------------------ 工具、说明、基本调用
def test_pi_server_lists_four_tools_with_rule_and_returns_content_only(tmp_path):
    mem, acc, handle = pi_setup(tmp_path)

    async def go(sess, init):
        assert init.instructions == TOOLS_RULE
        tools = (await sess.list_tools()).tools
        assert sorted(t.name for t in tools) == ["attachments", "get_messages", "recall", "status"]
        for t in tools:
            assert t.description and t.inputSchema["type"] == "object" and t.meta == {"anthropic/alwaysLoad": True}
        assert "remember" not in {t.name for t in tools}
        r = await sess.call_tool("recall", {"query": "吃了什么烤鱼"})
        assert not r.isError and TEXTS[1] in text_of(r) and r.structuredContent is None and len(r.content) == 1
        r2 = await sess.call_tool("recall", {"query": "完全无关的话题", "keywords": ["贝壳"]})
        assert TEXTS[0] in text_of(r2) and not r2.isError
        r3 = await sess.call_tool("recall", {"query": "完全无关的话题"})
        assert "没有找到相关记录" in text_of(r3)
        s = await sess.call_tool("status", {})
        assert not s.isError and "done_steps" in text_of(s) and TEXTS[1] not in text_of(s)
    with_server(params(tmp_path, "pi", handle), go)


def test_model_supplied_identity_args_are_ignored_over_the_wire_and_ban_holds(tmp_path):
    mem, acc, handle = pi_setup(tmp_path)

    async def go(sess, init):
        ok = await sess.call_tool("recall", {"query": "烤鱼", "history_allowed": False, "session_id": "other", "workspace": "x"})
        assert not ok.isError and TEXTS[1] in text_of(ok)
        acc.deny_turn("pi", "ps1", "T1")                                         # 这一轮被可信地标成不许读
        for args in ({"query": "烤鱼"}, {"query": "烤鱼", "history_allowed": True, "session_id": "ps1", "turn_id": "T1", "workspace": "w", "turn": "T1"}):
            r = await sess.call_tool("recall", args)
            assert "已拒绝｜原因：host_marker" in text_of(r) and "已停止后续读取；本轮此前已查询" in text_of(r) and TEXTS[1] not in text_of(r)
        for name, a in (("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"}), ("status", {})):
            assert "host_marker" in text_of(await sess.call_tool(name, a))
    with_server(params(tmp_path, "pi", handle), go)


def test_session_level_handle_is_refused_over_the_wire_because_the_turn_is_unproven(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    handle = acc.register_connection("pi", "ps1")                                  # 会话级句柄
    acc.begin_turn("pi", "ps1", "T1")

    async def go(sess, init):
        for name, a in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"}), ("status", {})):
            r = await sess.call_tool(name, a)
            assert "turn_unproven" in text_of(r) and "无法确认权限，已暂停新增读取" in text_of(r) and TEXTS[1] not in text_of(r)
    with_server(params(tmp_path, "pi", handle), go)


def test_unbound_and_double_claimed_and_closed_connections_refuse_everything(tmp_path):
    mem, acc, handle = pi_setup(tmp_path)

    async def unbound(sess, init):
        for name, a in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"}), ("status", {})):
            r = await sess.call_tool(name, a)
            assert "identity_unbound" in text_of(r) and "无法确认权限，已暂停新增读取" in text_of(r) and TEXTS[1] not in text_of(r)
    with_server(params(tmp_path, "pi", None), unbound)                       # 没有句柄（静态配置）
    with_server(params(tmp_path, "pi", "f" * 32), unbound)                    # 没登记过的句柄

    async def first(sess, init):
        assert TEXTS[1] in text_of(await sess.call_tool("recall", {"query": "烤鱼"}))
        # 第二个进程在第一个还活着的时候拿同一个句柄：认领不了
        with_server_inner = await asyncio.to_thread(lambda: with_server(params(tmp_path, "pi", handle), unbound))
        assert with_server_inner is None
        acc.close_connection(handle)
        assert "connection_closed" in text_of(await sess.call_tool("recall", {"query": "烤鱼"}))
    with_server(params(tmp_path, "pi", handle), first)


def test_claude_code_server_exposes_only_status_and_refuses_by_name_too(tmp_path):
    mem = organized(tmp_path)

    async def go(sess, init):
        assert "暂不支持读取" in init.instructions
        assert [t.name for t in (await sess.list_tools()).tools] == ["status"]
        for name, a in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("status", {})):
            r = await sess.call_tool(name, a)
            assert "identity_unproven" in text_of(r) and TEXTS[1] not in text_of(r) and "done_steps" not in text_of(r)
    with_server(params(tmp_path, "claude-code", None), go)
    with_server(params(tmp_path, "codex", None), go)


# ------------------------------------------------------------------ 返回大小（真实的 Pi 路径：20 KB，按 UTF-8 字节保守数）
def test_pi_real_profile_pages_fit_20kb_cover_everything_and_search_only_once(tmp_path):
    texts = ["这是一条很长的中文原话，" * 700, "😀好的😀谢谢😀" * 500, "ab\n" * 2000, "短的一句话"]
    mem, acc, handle = pi_setup(tmp_path, texts=texts)
    limit = H.profile("pi").paths["mcp_return"].limits[0].threshold

    async def go(sess, init):
        pages, spans, r = [], [], await sess.call_tool("recall", {"query": "原话 好的 ab 短的"})
        while True:
            assert not r.isError, text_of(r)
            full = S.final_return_text(raw(r))
            assert len(full.encode("utf-8")) <= limit and r.structuredContent is None
            pages.append(full)
            tail = full.split("续读：", 1)
            cur = None
            if "只给 cursor=" in full:
                cur = full.split("只给 cursor=", 1)[1].split("\n")[0].strip()
            if not cur:
                break
            r = await sess.call_tool("recall", {"cursor": cur})
            assert len(pages) < 300
            again = await sess.call_tool("recall", {"cursor": cur})                 # 同一页重取：内容相同
            assert text_of(again) == text_of(r)
        return pages
    pages = with_server(params(tmp_path, "pi", handle), go)
    assert len(pages) >= 3
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "m.sqlite"))
    assert c.execute("SELECT COUNT(*) FROM cursors WHERE kind='recall'").fetchone()[0] == 1       # 完整取材只做了一次；翻页都用同一份结果
    got = "".join(pages)
    for marker in ("很长的中文原话", "😀", "ab␊", "短的一句话"):
        assert marker in got


def test_wire_errors_are_marked_and_bounded(tmp_path):
    mem, acc, handle = pi_setup(tmp_path)
    big = "x" * 200000

    async def go(sess, init):
        for name, a in (("recall", {"query": "q", "keywords": big}), ("recall", {"query": "q", "keywords": [big]}), ("recall", {"cursor": "no"}),
                        ("get_messages", {"ids": [big]}), ("get_messages", {"ids": big}), ("attachments", {"message_id": big}), ("recall", {})):
            r = await sess.call_tool(name, a)
            assert r.isError and 0 < len(text_of(r)) < 700 and "〔yuanwei｜错误｜" in text_of(r), (name, text_of(r)[:80])
        r = await sess.call_tool("recall", {"query": "q", "keywords": ["a" * 129, "b", "c", "d"]})      # 超过 3 项只取前 3 项（修补三）；别的不合格（这里是有一项过长）仍然报 bad_query
        assert r.isError and "bad_query" in text_of(r)
        r = await sess.call_tool("no_such_tool", {})
        assert r.isError and "unknown_tool" in text_of(r) and len(text_of(r)) < 700
    with_server(params(tmp_path, "pi", handle), go)


# ------------------------------------------------------------------ 基础包不依赖 MCP
def test_base_package_and_server_module_import_without_the_sdk_but_building_a_server_needs_it():
    code = ("import sys; sys.modules['mcp']=None\n"
            "import yuanwei, yuanwei.tools, yuanwei.modes, yuanwei.access, yuanwei.idchain, yuanwei.paging, yuanwei.directory\n"
            "import yuanwei.mcp.server as S\n"
            "try:\n    S.build_server(None)\n    print('NO-ERROR')\nexcept ImportError:\n    print('IMPORT-ERROR')\n")
    out = subprocess.run([sys.executable, "-P", "-c", code], env={"PYTHONPATH": os.path.abspath(SRC), "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "IMPORT-ERROR"


def test_concurrent_calls_on_one_server_do_not_interleave_transactions(tmp_path):
    mem, acc, handle = pi_setup(tmp_path)

    async def go(sess, init):
        rs = await asyncio.gather(*[sess.call_tool("recall", {"query": "烤鱼 %d" % i, "keywords": ["贝壳"]}) for i in range(12)],
                                  *[sess.call_tool("status", {}) for _ in range(6)], *[sess.call_tool("get_messages", {"ids": ["no-such"]}) for _ in range(4)])
        assert all("事务" not in text_of(r) and "internal_error" not in text_of(r) for r in rs)
        assert sum(1 for r in rs if not r.isError) == 18
    with_server(params(tmp_path, "pi", handle), go)


def test_forged_request_meta_is_not_an_identity(tmp_path):
    """MCP 请求的 _meta（client_id、会话、轮次、claudecode/toolUseId 之类）不是绑定证明：伪造的元数据不能解除禁读，也不能换会话。"""
    mem, acc, handle = pi_setup(tmp_path, turn_text="#yuanwei-nomem")
    forged = {"client_id": "trusted-client", "session_id": "ps1", "turn_id": "T0", "claudecode/toolUseId": "toolu_x", "progressToken": "p"}

    async def go(sess, init):
        r = await sess.call_tool("recall", {"query": "烤鱼"}, meta=forged)
        assert "control_nomem" in text_of(r) and TEXTS[1] not in text_of(r)
        acc.begin_turn("pi", "ps1", "T2")
        late = await sess.call_tool("recall", {"query": "烤鱼"}, meta={"session_id": "somebody-else", "turn_id": "T2", "claudecode/toolUseId": "toolu_y"})
        assert "stale_turn" in text_of(late) and TEXTS[1] not in text_of(late)   # 句柄只证明 T1：伪造的 _meta 说「T2」也换不了这一轮
    with_server(params(tmp_path, "pi", handle), go)
