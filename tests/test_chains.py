"""每家一条完整的身份链，用假事件证明（施工单 §2.1）。假事件只用这家宿主资料里**真有**的字段名（session_id、prompt_id、turn_id、agent_id、source、author_is_bot…），
不加 is_human、is_final 这类真实宿主没有的字段。「暂不支持读取」的宿主要证明：拿不到可信链就拒绝，不拿最近的会话顶上。"""
import pytest

from helpers2 import TEXTS, Counter, organized
from yuanwei import hosts as H
from yuanwei import idchain as IC
from yuanwei.access import Access, ToolIdentity
from yuanwei.idsource import ConnectionIdentity, InProcessIdentity, NoIdentity
from yuanwei.mcp import server as S
from yuanwei.tools import ToolSurface


def register(acc, idev):
    """接头的职责：把宿主事件翻成登记调用。只用 IdEvent 里的字段。"""
    return acc.begin_turn(idev.host, idev.session_id, idev.turn_id, idev.branch, text=idev.text, origin=idev.origin) if idev.ignore is None else {"registered": False, "ignored": idev.ignore}


# ------------------------------------------------------------------ Hermes：同进程插件，读取工具由插件自带
def test_hermes_in_process_chain_with_fake_provider_events(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ids = InProcessIdentity()
    surf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    # initialize(session_id, agent_context="primary", platform="cli") —— 宿主在插件实例里设会话
    init = {"session_id": "hs1", "agent_context": "primary", "platform": "cli", "hermes_home": "/fake/home"}
    # 第 1 个用户回合：on_turn_start(turn_number=1, message=…, author_is_bot=False)
    ev = IC.hermes_turn(init, 1, "吃了什么烤鱼", {"id": "u1", "name": "u", "is_bot": False}, token="tok-1")
    assert ev.origin == "user" and ev.ignore is None and (ev.host, ev.session_id, ev.turn_id) == ("hermes", "hs1", "tok-1")
    register(acc, ev)
    ids.set("hermes", init["session_id"], "", ev.turn_id)                      # 插件在自己的 on_turn_start 里把会话和这一轮的轮次标记一起设好
    r = surf.call("recall", {"query": "吃了什么烤鱼"})                       # handle_tool_call 不带会话和轮次：身份来自插件实例里的状态（调用发起时冻结）
    assert r.status == "ok" and TEXTS[1] in r.text
    # 第 2 个回合带口令：新轮次覆盖上一轮的允许，工具被拒
    register(acc, IC.hermes_turn(init, 2, "#yuanwei-nomem\n再查一次", {"id": "u1", "name": "u", "is_bot": False}, token="tok-2"))
    assert surf.call("recall", {"query": "烤鱼"}).reason == "stale_turn"          # 插件还没把身份换成 tok-2：旧身份不借新一轮的许可，也不借它的拒绝以外的东西
    ids.set("hermes", init["session_id"], "", "tok-2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "control_nomem"
    # 压缩：on_session_switch(child, parent_session_id=hs1, reset=False, reason="compression")：同一轮，继承拒绝
    ids.set("hermes", "hs1-child", "", "tok-2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "turn_unregistered"      # 切换后、继承登记之前：拒绝（不拿父会话的轮次顶上）
    acc.inherit_turn("hermes", "hs1", "hs1-child", "tok-2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "control_nomem"
    # /branch：reset=False, reason=branch：分叉不继承
    ids.set("hermes", "hs1-branch", "", "tok-b1")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "turn_unregistered"
    register(acc, IC.hermes_turn({"session_id": "hs1-branch", "agent_context": "primary", "platform": "cli"}, 1, "继续", {"is_bot": False}, token="tok-b1"))
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"
    # /new（reset=True）：新会话，旧会话的允许和拒绝都不带过来
    ids.set("hermes", "hs2", "", "tok-n1")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "turn_unregistered"


def test_hermes_origin_excludes_cron_subagent_bot_and_missing_context():
    base = {"session_id": "s", "platform": "cli"}
    assert IC.hermes_turn(dict(base, agent_context="primary"), 1, "x", {"is_bot": False}, "t").origin == "user"
    for ctx in ("subagent", "cron", "flush"):
        e = IC.hermes_turn(dict(base, agent_context=ctx), 1, "x", {"is_bot": False}, "t")
        assert e.origin == "unknown" and e.ignore == "agent_context_" + ctx
    assert IC.hermes_turn(dict(base, agent_context="primary"), 1, "x", {"is_bot": True}, "t").ignore == "bot_author"
    assert IC.hermes_turn({"session_id": "s", "platform": "cron"}, 1, "x", {"is_bot": False}, "t").ignore == "platform_cron"
    assert IC.hermes_turn({"platform": "cli"}, 1, "x", {}, "t").ignore == "identity_incomplete"
    assert IC.hermes_turn(dict(base), 1, "x", None, "").ignore == "identity_incomplete"                # 没有轮次标记


def test_hermes_failed_registration_never_reuses_the_previous_allow(tmp_path):
    """on_turn_start 在宿主里被 suppress(Exception) 包着：插件自己登记失败，宿主不会知道。登记分两步，先落「登记中」，失败就停在拒绝。"""
    mem = organized(tmp_path)
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("hermes", "hs1", "", "t1")
    surf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    init = {"session_id": "hs1", "agent_context": "primary", "platform": "cli"}
    register(acc, IC.hermes_turn(init, 1, "第一问", {"is_bot": False}, "t1"))
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"
    acc.fault = lambda point: (_ for _ in ()).throw(RuntimeError("db busy")) if point == "between_open_and_complete" else None
    with pytest.raises(RuntimeError):
        register(acc, IC.hermes_turn(init, 2, "第二问", {"is_bot": False}, "t2"))
    acc.fault = None
    ids.set("hermes", "hs1", "", "t2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "registration_incomplete"


# ------------------------------------------------------------------ Pi：扩展给每个会话单起 MCP 进程并带连接句柄
def test_pi_launch_bound_chain_with_fake_extension_events(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    # session_start(reason="startup")：扩展（可信启动端）登记连接，拿句柄，把它放进自己启动的 MCP 进程的环境里。
    # 会话级句柄只证明会话、不证明轮次：读取被拒（turn_unproven，待核）；要读，要用逐轮的能力（句柄绑定这一轮的轮次标记）。
    h_session = acc.register_connection("pi", "ps1")
    ids_s = ConnectionIdentity(acc, h_session, "proc-0")
    acc.begin_turn("pi", "ps1", "pi-t1")
    assert ToolSurface(mem, acc, H.profile("pi"), ids_s).call("recall", {"query": "烤鱼"}).reason == "turn_unproven"
    acc.close_connection(h_session)
    # input 事件：{text, source="interactive"}；input 不带 id，轮次标记由扩展自己生成
    ev = IC.pi_input({"type": "input", "text": "吃了什么烤鱼", "source": "interactive"}, "ps1", token="pi-t9")
    assert ev.origin == "user" and ev.ignore is None
    handle = acc.register_connection("pi", "ps1", turn_id="pi-t9")
    ids = ConnectionIdentity(acc, handle, "proc-1")
    assert ids.error is None
    surf = ToolSurface(mem, acc, H.profile("pi"), ids)
    assert surf.call("recall", {"query": "烤鱼"}).reason == "stale_turn"                    # 这一轮的输入事件还没登记：当前轮还是 pi-t1，这个句柄的轮次对不上
    register(acc, ev)
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"
    for src, why in (("rpc", "origin_unknown"), ("extension", "extension_input")):
        e = IC.pi_input({"type": "input", "text": "#yuanwei-nomem", "source": src}, "ps1", token="x-" + src)
        assert e.ignore == why
        register(acc, e)
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"                            # rpc、扩展发的输入不开新一轮，也不当真人的控制
    # 排队输入（streamingBehavior）：不在 input 事件上登记，等这次运行真的开始——否则会把正在回答的这一轮的拒绝冲掉
    q = IC.pi_input({"type": "input", "text": "排队的一句", "source": "interactive", "streamingBehavior": "followUp"}, "ps1", token="pi-t2")
    assert q.ignore == "queued_wait_for_run_start"
    # session_shutdown：连接关闭，之后这个进程不能再读
    acc.close_connection(handle)
    assert surf.call("recall", {"query": "烤鱼"}).reason == "connection_closed"
    # 换一个进程拿同一个句柄：认领不了，一直没有身份
    ids2 = ConnectionIdentity(acc, handle, "proc-2")
    assert ids2.error == "connection_closed" and ids2.current() is None
    h2 = acc.register_connection("pi", "ps2", turn_id="x")
    assert ConnectionIdentity(acc, h2, "proc-3").error is None
    dup = ConnectionIdentity(acc, h2, "proc-4")
    assert dup.error == "connection_claimed" and dup.current() is None


def test_pi_static_env_without_a_registered_connection_has_no_identity(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ids = ConnectionIdentity(acc, "", "proc-1")                                    # 静态配置里没有按连接生成的句柄
    assert ids.error == "connection_unknown" and ids.current() is None
    ids = ConnectionIdentity(acc, "a" * 32, "proc-1")
    assert ids.error == "connection_unknown"


# ------------------------------------------------------------------ Claude Code、Codex：静态 stdio MCP 拿不到会话身份
def test_claude_code_event_fields_origin_unknown_and_static_mcp_has_no_chain(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ev = IC.claude_code_prompt({"hook_event_name": "UserPromptSubmit", "session_id": "cs1", "prompt_id": "p1", "cwd": "/fake/proj", "prompt": "吃了什么烤鱼"})
    assert (ev.host, ev.session_id, ev.turn_id, ev.origin) == ("claude-code", "cs1", "p1", "unknown")
    assert register(acc, ev)["registered"] is False and acc.current_turn("claude-code", "cs1") is None       # 来源不明：不开新一轮
    sub = IC.claude_code_prompt({"hook_event_name": "UserPromptSubmit", "session_id": "cs1", "prompt_id": "p2", "agent_id": "a1", "agent_type": "x", "prompt": "子代理"})
    assert sub.ignore == "subagent"
    assert IC.claude_code_prompt({"session_id": "cs1", "prompt": "x"}).ignore == "identity_incomplete"          # 第一次用户输入之前没有 prompt_id
    ids = NoIdentity()
    surf = ToolSurface(mem, acc, H.profile("claude-code"), ids)
    assert surf.tool_names() == ["status"]
    cnt = Counter(mem)
    try:
        for n, a in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"})):
            assert surf.call(n, a).reason == "identity_unproven"
        # 就算有人在同一个库里替这个会话登记了一条「允许」的轮次，工具也不会拿「同目录唯一的活动记录」顶上
        acc.begin_turn("claude-code", "cs1", "p1")
        assert surf.call("recall", {"query": "烤鱼"}).reason == "identity_unproven"
        assert cnt.calls == []
    finally:
        cnt.close()


def test_codex_event_fields_main_thread_only_and_static_mcp_has_no_chain(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ev = IC.codex_prompt({"hook_event_name": "UserPromptSubmit", "session_id": "xs1", "turn_id": "t1", "cwd": "/fake", "model": "m", "permission_mode": "default",
                          "transcript_path": None, "prompt": "吃了什么烤鱼"})
    assert (ev.host, ev.session_id, ev.turn_id, ev.origin, ev.ignore) == ("codex", "xs1", "t1", "user", None)
    assert IC.codex_prompt({"session_id": "xs1", "turn_id": "t2", "agent_id": "a", "agent_type": "explorer", "prompt": "x"}).ignore == "subagent"
    assert IC.codex_prompt({"session_id": "xs1", "prompt": "x"}).ignore == "identity_incomplete"
    stop = IC.codex_stop({"hook_event_name": "Stop", "session_id": "xs1", "turn_id": "t1", "last_assistant_message": None, "stop_hook_active": False})
    assert stop["reply"] is None and stop["continued"] is False
    assert IC.codex_stop({"session_id": "xs1", "turn_id": "t1", "last_assistant_message": "答复", "stop_hook_active": True})["continued"] is True
    surf = ToolSurface(mem, acc, H.profile("codex"), NoIdentity())
    assert surf.tool_names() == ["status"] and surf.call("recall", {"query": "烤鱼"}).reason == "identity_unproven"
    # 钩子路径本身（回答前先查）不经 MCP：事件自带会话和轮次，登记后由 modes 判断；这里只证明登记得上
    register(acc, ev)
    assert acc.current_turn("codex", "xs1")["turn_id"] == "t1"


def test_no_fake_host_only_fields_in_the_fake_events():
    """假事件不能靠真实宿主没有的字段过测。"""
    forbidden = {"is_human", "is_final", "is_user", "from_user", "final", "human"}
    import inspect
    src = inspect.getsource(IC)
    for w in forbidden:
        assert ("'%s'" % w) not in src and ('"%s"' % w) not in src


def test_inherit_turn_only_from_the_current_turn_of_the_parent_session(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("h", "P", "T1", text="#yuanwei-nomem")
    acc.begin_turn("h", "P", "T2")
    assert acc.inherit_turn("h", "P", "C", "T1") is None                                # 旧轮次不能带到新会话
    assert acc.inherit_turn("h", "P", "C", "T9") is None
    row = acc.inherit_turn("h", "P", "C", "T2")
    assert row["turn_id"] == "T2" and row["refused"] is False and acc.current_turn("h", "C")["turn_id"] == "T2"
    acc.begin_turn("h", "P2", "U1", text="#yuanwei-nomem")
    acc.inherit_turn("h", "P2", "C2", "U1")
    assert acc.current_turn("h", "C2")["refused"] is True and acc.current_turn("h", "C2")["refuse_reason"] == "control_nomem"
    acc.inherit_turn("h", "P2", "C2", "U1")                                             # 重复继承：不新开、不改
    assert acc.current_turn("h", "C2")["gen"] == 1
