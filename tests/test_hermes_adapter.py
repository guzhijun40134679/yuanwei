"""交回点乙·第 2 步：Hermes 记忆插件（核心适配器 hermes_adapter.HermesAdapter 和装到 Hermes 里的插件文件）。
接口照本机 Hermes v0.22.0 源码（~/.hermes/hermes-agent，只读）；0.20.0 的两份检出（工作区 hermes-upstream、hermes-pro）核兼容。
假事件的字段只用 Hermes 源码里真有的：initialize(session_id, platform, agent_context, hermes_home…)、on_turn_start(turn_number, message, author_id, author_name, author_is_bot)、
prefetch(query, session_id)、sync_turn(user_content, assistant_content, session_id, messages, turn_author)、on_session_switch(new, parent_session_id, reset, rewound, reason)、
messages 里的行用 role、content、display_kind、_todo_snapshot_synthetic 这些真有的键。全部自造数据，没有网络，不碰 ~/.hermes 的配置和会话。"""
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
import threading
import time
import types

import pytest

from helpers2 import TEXTS, organized
from yuanwei import rules as RL
from yuanwei.hermes_adapter import HermesAdapter

HOME = os.path.expanduser("~/.hermes/hermes-agent/agent/memory_provider.py")
WS = os.environ.get("YUANWEI_HERMES_CHECKOUTS") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))   # 放 hermes-upstream、hermes-pro 两份检出的目录；没有就跳过
BASES = {"v0.22.0": HOME, "0.20.0-upstream": WS + "/hermes-upstream/agent/memory_provider.py", "0.20.0-pro": WS + "/hermes-pro/agent/memory_provider.py"}


def mk(tmp_path, mode="tools_only", **extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    cfg = dict(mode=mode, spawn="off")
    cfg.update(extra)
    a = HermesAdapter(cfg, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    return mem, a


def turn(a, n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


def call(a, name, args=None):
    return json.loads(a.handle_tool_call(name, args or {}))


def stored(mem):
    return [(r["session_id"], r["role"], r["content"]) for r in mem.store.list_messages(mem.ns) if r["session_id"] == "hs1"]


def msgs(user_text, extra=()):
    return [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}] + list(extra) + [{"role": "user", "content": user_text}, {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "工具输出：这不该进记忆", "tool_call_id": "c1"}]


# ================================================================== 接口：对着真实 Hermes 的 MemoryProvider 核
def load_base(path):
    spec = importlib.util.spec_from_file_location("agent.memory_provider", path)
    mod = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.get(k) for k in ("agent", "agent.memory_provider")}
    sys.modules["agent"] = types.ModuleType("agent")
    sys.modules["agent.memory_provider"] = mod
    spec.loader.exec_module(mod)
    return mod, saved


def load_plugin(base_path, tmp_path):
    base, saved = load_base(base_path)
    try:
        from yuanwei import hermes_setup as HS
        text = HS.plugin_init_text()
        ns = types.ModuleType("yuanwei_hermes_plugin")
        ns.__file__ = str(tmp_path / "__init__.py")
        exec(compile(text, "yuanwei_hermes_plugin/__init__.py", "exec"), ns.__dict__)
        return base, ns
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@pytest.mark.parametrize("ver", sorted(BASES))
def test_plugin_file_satisfies_the_real_memory_provider_interface(tmp_path, ver):
    path = BASES[ver]
    if not os.path.exists(path):
        pytest.skip("这台机器上没有 %s 的源码" % ver)
    base, ns = load_plugin(path, tmp_path)
    prov = ns.YuanweiMemoryProvider()                                   # 抽象方法都实现了，才能实例化
    assert isinstance(prov, base.MemoryProvider) and prov.name == "yuanwei"
    for meth in ("initialize", "prefetch", "sync_turn", "on_turn_start", "on_session_switch", "on_session_end", "handle_tool_call", "get_tool_schemas", "shutdown", "system_prompt_block"):
        mine, theirs = inspect.signature(getattr(prov, meth)), inspect.signature(getattr(base.MemoryProvider, meth))
        t_params = [p for p in theirs.parameters.values() if p.name != "self"]
        m_params = {p.name: p for p in mine.parameters.values() if p.name != "self"}
        for p in t_params:
            assert p.name in m_params or any(q.kind is q.VAR_KEYWORD for q in m_params.values()), (ver, meth, p.name)
    assert hasattr(ns, "register")
    got = []
    ns.register(types.SimpleNamespace(register_memory_provider=got.append))
    assert len(got) == 1 and isinstance(got[0], base.MemoryProvider)


def test_plugin_file_runs_end_to_end_through_the_real_provider_base(tmp_path, monkeypatch):
    if not os.path.exists(HOME):
        pytest.skip("没有 Hermes 源码")
    base, ns = load_plugin(HOME, tmp_path)
    home = tmp_path / "home"
    (home / "yuanwei").mkdir(parents=True)
    (home / "yuanwei" / "config.json").write_text(json.dumps({"hermes_home": str(home), "db": str(tmp_path / "m.sqlite"), "workspace": "hermes-test", "mode": "tools_only", "spawn": "off", "src_path": os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))}))
    monkeypatch.setenv("HERMES_HOME", str(home))                       # 插件找设置文件的办法之一；不让它碰真实的 ~/.hermes
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    prov = ns.YuanweiMemoryProvider()
    assert prov.is_available() is True                                  # 配置文件在、源码路径在
    prov.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(home))
    prov.on_turn_start(1, "吃了什么烤鱼", author_id=None, author_name=None, author_is_bot=False)
    assert prov.prefetch("吃了什么烤鱼", session_id="hs1") == ""
    names = sorted(s["name"] for s in prov.get_tool_schemas())
    assert names == ["attachments", "get_messages", "recall", "status", "window_delete", "window_off", "window_on", "window_state"]
    out = json.loads(prov.handle_tool_call("status", {}))
    assert out["status"] == "ok"
    prov.sync_turn("吃了什么烤鱼", "吃了烤鱼", session_id="hs1", messages=msgs("吃了什么烤鱼"))
    prov.shutdown()
    c = sqlite3.connect(str(tmp_path / "m.sqlite"))
    assert [r[0] for r in c.execute("SELECT role FROM messages ORDER BY seq")] == ["user", "assistant"]


# ================================================================== 一轮从哪来：只认 Hermes 源码里有的字段
def test_cron_subagent_flush_contexts_are_inactive_nothing_recorded_no_tools(tmp_path):
    for ctx, plat in (("cron", "cron"), ("subagent", "subagent"), ("flush", "cli")):
        mem = organized(tmp_path / ctx, host="hermes") if (tmp_path / ctx).mkdir(parents=True) is None else None
        a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline")
        a.initialize("hs1", platform=plat, agent_context=ctx, hermes_home=str(tmp_path / "home"))
        turn(a, 1, "定时任务的提示")
        assert a.prefetch("定时任务的提示", session_id="hs1") == "" and a.get_tool_schemas() == []
        a.sync_turn("定时任务的提示", "回复", session_id="hs1", messages=msgs("定时任务的提示"))
        assert stored(mem) == [] and a.status()["active"] is False and a.status()["inactive_reason"] == "agent_context_" + ctx


def test_cron_platform_with_primary_context_is_still_inactive(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cron", agent_context="primary", hermes_home=str(tmp_path / "home"))
    assert a.status()["active"] is False and a.status()["inactive_reason"] == "platform_cron"


def test_bot_author_turn_is_not_admitted_no_read_permission_and_not_recorded(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "机器人发来的", author_id="bot:x", author_name="x", author_is_bot=True)
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] in ("turn_unregistered", "stale_turn", "turn_unproven")
    a.sync_turn("机器人发来的", "回复", session_id="hs1", messages=msgs("机器人发来的"), turn_author={"id": "bot:x", "name": "x", "is_bot": True})
    assert stored(mem) == [] and a.status()["skipped"].get("not_admitted", 0) + a.status()["skipped"].get("bot_author", 0) >= 1


def test_machinery_rows_in_messages_keep_the_turn_out_of_the_store(tmp_path):
    """宿主自己区分「真人提问」和「后台完成、委派完成唤醒」靠 display_kind 和 _*_synthetic 标记（源码：context_compressor._is_real_user_turn、conversation_compression._is_real_user_message）。
    on_turn_start 看不到这些，所以写入前在 sync_turn 的 messages 里核：当前这一轮的用户行有 display_kind 或合成标记，就不记。"""
    for row in ({"display_kind": "process_complete"}, {"display_kind": "async_delegation_complete"}, {"_todo_snapshot_synthetic": True}):
        d = tmp_path / str(sorted(row)[0]) / str(list(row.values())[0])
        mem, a = mk(d)
        turn(a, 1, "后台任务完成的通知")
        m = msgs("后台任务完成的通知")
        m[-3] = dict(m[-3], **row)
        a.sync_turn("后台任务完成的通知", "知道了", session_id="hs1", messages=m)
        assert stored(mem) == [] and a.status()["skipped"].get("machinery_row") == 1


def test_missing_messages_or_missing_user_row_cannot_confirm_so_nothing_is_recorded(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "第一问")
    a.sync_turn("第一问", "回答", session_id="hs1")
    assert stored(mem) == [] and a.status()["skipped"].get("no_messages") == 1
    turn(a, 2, "第二问")
    a.sync_turn("第二问", "回答", session_id="hs1", messages=[{"role": "user", "content": "完全不同的另一句"}])
    assert stored(mem) == [] and a.status()["skipped"].get("current_user_row_not_found") == 1


def test_steer_or_later_machinery_user_row_after_the_turn_input_also_keeps_it_out(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "一句话")
    m = msgs("一句话") + [{"role": "user", "content": "[OUT-OF-BAND USER MESSAGE", "display_kind": "steer"}]
    a.sync_turn("一句话", "回答", session_id="hs1", messages=m)
    assert stored(mem) == []


def test_old_host_without_author_fields_is_admitted_and_labelled(tmp_path):
    """0.20.0 的 memory_manager 不传 author_*，sync_turn 也没有 turn_author：字段整个不存在，是「老宿主没有这个功能」，不是「明确写着不是机器人」。"""
    mem, a = mk(tmp_path)
    a.on_turn_start(1, "吃了什么烤鱼")
    assert call(a, "status")["status"] == "ok"
    a.sync_turn("吃了什么烤鱼", "吃了烤鱼", session_id="hs1", messages=msgs("吃了什么烤鱼"))
    assert [x[1] for x in stored(mem)] == ["user", "assistant"] and a.status()["evidence"] == "legacy_no_author_field"


# ================================================================== 控制登记不能只放在预取里
def test_control_is_registered_in_on_turn_start_even_when_the_host_skips_prefetch(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem\n帮我想想上次的烤鱼")                       # Hermes 对简单提问会跳过预取：这里模拟它根本没调 prefetch
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] == "control_nomem"
    assert call(a, "status")["status"] == "refused"


def test_second_turn_replaces_the_first_and_model_args_cannot_lift_the_ban(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"
    turn(a, 2, "#yuanwei-nomem")
    r = call(a, "recall", {"query": "烤鱼", "history_allowed": True, "session_id": "hs1", "turn_id": "x"})
    assert r["reason"] == "control_nomem"


def test_prefetch_modes_tools_only_reminder_note_and_receipt_on_a_banned_turn(tmp_path):
    mem, a = mk(tmp_path / "t")
    turn(a, 1, "吃了什么烤鱼")
    assert a.prefetch("吃了什么烤鱼", session_id="hs1") == ""                         # 默认只给工具：回答前什么都不交
    turn(a, 2, "#yuanwei-nomem\n烤鱼")
    out = a.prefetch("#yuanwei-nomem\n烤鱼", session_id="hs1")
    assert out.startswith("本轮未新增读取长期记忆")
    assert a.prefetch("#yuanwei-nomem\n烤鱼", session_id="hs1") == ""                 # 同一轮第二次不再刷回执
    mem, b = mk(tmp_path / "r", mode="reminder")
    turn(b, 1, "普通")
    assert b.prefetch("普通", session_id="hs1") == RL.REMINDER
    mem, c = mk(tmp_path / "n", mode="note")
    turn(c, 1, "吃了什么烤鱼")
    note = c.prefetch("吃了什么烤鱼", session_id="hs1")
    assert TEXTS[1] in note and len(note) <= 8000


def test_system_prompt_block_is_the_resident_rule_and_can_be_turned_off(tmp_path):
    mem, a = mk(tmp_path / "a")
    assert a.system_prompt_block() == RL.RULES["tools_only"]
    mem, b = mk(tmp_path / "b", mode="directory")
    assert b.system_prompt_block() == RL.RULES["directory"]
    mem, c = mk(tmp_path / "c", resident_rule=False)
    assert c.system_prompt_block() == ""


# ================================================================== 工具
def test_tool_schemas_and_json_string_results_and_unknown_tool(tmp_path):
    mem, a = mk(tmp_path)
    sch = a.get_tool_schemas()
    assert sorted(s["name"] for s in sch) == ["attachments", "get_messages", "recall", "status", "window_delete", "window_off", "window_on", "window_state"]
    assert all(set(s) >= {"name", "description", "parameters"} and s["parameters"]["type"] == "object" for s in sch)
    turn(a, 1, "吃了什么烤鱼")
    raw = a.handle_tool_call("recall", {"query": "吃了什么烤鱼"})
    assert isinstance(raw, str)
    r = json.loads(raw)
    assert r["status"] == "ok" and TEXTS[1] in r["text"]
    assert json.loads(a.handle_tool_call("remember", {"text": "x"}))["status"] == "error"


def test_tool_call_in_a_later_turn_uses_that_turns_permission_not_the_old_one(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通")
    turn(a, 2, "#yuanwei-nomem")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "control_nomem"
    turn(a, 3, "又一轮普通的")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "window_closed"                  # 改了（禁读按窗口）：口令关的是整个窗口，下一轮仍然关着，不再只管这一轮
    turn(a, 4, "#yuanwei-mem")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"                              # 重新打开的口令


# ================================================================== 写入：只收传进来的两段话；对得上才记；重送不重复
def test_sync_turn_records_only_the_two_texts_not_the_message_list(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "我想吃烤鱼")
    a.sync_turn("我想吃烤鱼", "好的，去买鱼", session_id="hs1", messages=msgs("我想吃烤鱼"))
    rows = stored(mem)
    assert [(r[1], r[2]) for r in rows] == [("user", "我想吃烤鱼"), ("assistant", "好的，去买鱼")]
    assert "工具输出" not in json.dumps(rows, ensure_ascii=False) and "老的一轮" not in json.dumps(rows, ensure_ascii=False)


def test_redelivered_sync_is_idempotent_and_a_different_reply_is_a_second_message(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "我想吃烤鱼")
    a.sync_turn("我想吃烤鱼", "好的", session_id="hs1", messages=msgs("我想吃烤鱼"))
    a.sync_turn("我想吃烤鱼", "好的", session_id="hs1", messages=msgs("我想吃烤鱼"))             # 重送：没有新的待配对输入，不重复入库
    assert len(stored(mem)) == 2 and a.status()["skipped"].get("no_pending_turn") == 1


def test_sync_for_turn_n_arriving_after_turn_n_plus_1_started_is_paired_by_content(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "第一问")
    turn(a, 2, "第二问")                                       # 宿主的后台线程还没轮到第一轮的 sync
    a.sync_turn("第一问", "答一", session_id="hs1", messages=msgs("第一问"))
    a.sync_turn("第二问", "答二", session_id="hs1", messages=msgs("第二问"))
    assert [(r[1], r[2]) for r in stored(mem)] == [("user", "第一问"), ("assistant", "答一"), ("user", "第二问"), ("assistant", "答二")]


def test_interrupted_turn_never_synced_does_not_steal_the_next_turns_pairing(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "被打断的一问")                                  # 宿主对被打断的轮次不调 sync_turn
    turn(a, 2, "接着问")
    a.sync_turn("接着问", "答", session_id="hs1", messages=msgs("接着问"))
    assert [(r[1], r[2]) for r in stored(mem)] == [("user", "接着问"), ("assistant", "答")]
    assert [e["number"] for e in a._pending["hs1"]] == [1]                    # 配走的是第 2 轮自己的待配对项，被打断的第 1 轮还留在那里，没被错配
    ids = [r[0] for r in mem.store.conn.execute("SELECT host_msg_id FROM messages WHERE session_id='hs1' AND content IN ('接着问', '答')")]
    assert all(a.acc.turn_token("hermes", "hs1", "2:%s" % a.acc.text_fp("接着问")[:12]) in i for i in ids)


def test_write_is_independent_of_the_read_ban(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem\n别用记忆查，记一下我爱吃烤鱼")
    a.sync_turn("#yuanwei-nomem\n别用记忆查，记一下我爱吃烤鱼", "好", session_id="hs1", messages=msgs("#yuanwei-nomem\n别用记忆查，记一下我爱吃烤鱼"))
    assert len(stored(mem)) == 2                                # 口令只管读，不管存（第一段的规矩：用户关了保存才不存）


def test_empty_assistant_reply_records_only_the_user_side(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "问")
    a.sync_turn("问", "", session_id="hs1", messages=msgs("问"))
    assert [r[1] for r in stored(mem)] == ["user"]


def test_multimodal_user_row_is_matched_by_its_text_parts(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "看这张图\n它是什么")
    m = [{"role": "user", "content": [{"type": "text", "text": "看这张图"}, {"type": "image_url", "image_url": {"url": "x"}}, {"type": "text", "text": "它是什么"}]}]
    a.sync_turn("看这张图\n它是什么", "是一只猫", session_id="hs1", messages=m)
    assert [r[1] for r in stored(mem)] == ["user", "assistant"]


# ================================================================== 原话可靠落盘：数据库忙时先接收；结束事件不在宿主线程里做
def test_database_busy_goes_to_the_pending_file_then_flushes(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "数据库忙的时候来的话")
    blocker = sqlite3.connect(mem.db_path, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        mem.store.conn.execute("PRAGMA busy_timeout=50")
        a.sync_turn("数据库忙的时候来的话", "回复", session_id="hs1", messages=msgs("数据库忙的时候来的话"))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert mem.pending_count() == 2 or len(stored(mem)) == 2                          # 要么在待补文件里，要么已经入库，不丢
    mem.flush_pending()
    assert [r[1] for r in stored(mem)] == ["user", "assistant"]


def test_session_end_and_switch_are_handed_to_the_background_worker_not_done_in_the_host_thread(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="thread")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    gate, started = threading.Event(), threading.Event()
    orig = mem.end_session

    def slow(host, session, branch=""):
        started.set()
        gate.wait(5)
        return orig(host, session, branch)
    mem.end_session = slow
    t0 = time.time()
    a.on_session_end(msgs("x"))
    assert time.time() - t0 < 1.0 and not gate.is_set()                              # 宿主线程立刻回来
    assert started.wait(5)
    gate.set()
    assert a.drain(5) is True
    a.shutdown()


def test_shutdown_drains_queued_writes(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="thread")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(a, 1, "一问")
    a.sync_turn("一问", "一答", session_id="hs1", messages=msgs("一问"))
    a.shutdown()
    c = sqlite3.connect(mem.db_path)
    assert [r[0] for r in c.execute("SELECT role FROM messages WHERE session_id='hs1' ORDER BY seq")] == ["user", "assistant"]


# ================================================================== 会话切换：压缩继承，分叉和新会话不继承
def test_compression_switch_inherits_the_turn_ban_but_branch_resume_and_new_do_not(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem\n烤鱼")
    a.on_session_switch("hs1-c", parent_session_id="hs1", reset=False, reason="compression")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "control_nomem"          # 压缩：同一轮继续，拒绝跟着走
    a.on_session_switch("hs1-b", parent_session_id="hs1-c", reset=False, reason="branch")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "turn_unregistered"      # 分叉：不借上一个会话的轮次
    a.on_session_switch("hs2", parent_session_id="", reset=True)
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "turn_unregistered"
    turn(a, 1, "新会话的第一句")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_records_after_a_switch_land_under_the_new_session_id(tmp_path):
    mem, a = mk(tmp_path)
    a.on_session_switch("hs1-c", parent_session_id="hs1", reset=False, reason="compression")
    turn(a, 5, "压缩以后的一问")
    a.sync_turn("压缩以后的一问", "答", session_id="hs1-c", messages=msgs("压缩以后的一问"))
    assert {r["session_id"] for r in mem.store.list_messages(mem.ns) if r["content"] in ("压缩以后的一问", "答")} == {"hs1-c"}


def test_directory_mode_marks_boundaries_without_reading(tmp_path):
    mem, a = mk(tmp_path, mode="directory")
    a.on_session_switch("hs1-c", parent_session_id="hs1", reset=False, reason="compression")
    assert a.acc.dir_get("hermes", "hs1-c") is not None and a.acc.dir_get("hermes", "hs1-c")["state"] == "needed"


# ================================================================== 状态：数得出来
def test_status_counts_recorded_and_skipped_without_text(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "一问")
    a.sync_turn("一问", "一答", session_id="hs1", messages=msgs("一问"))
    a.sync_turn("没配对的", "答", session_id="hs1", messages=msgs("没配对的"))
    st = a.status()
    assert st["recorded_turns"] == 1 and st["skipped"] == {"no_pending_turn": 1} and "一问" not in json.dumps(st, ensure_ascii=False)


# ================================================================== V7 里和 Hermes 有关的绑定条目
def test_two_sessions_in_one_workspace_interleaved_a_banned_b_normal(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline")
    b = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")      # 第 2b 步起：网关平台要配主人才算（本机会话自动是主人）
    a.initialize("sA", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "h"))
    b.initialize("sB", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "h"), user_id="u1")
    turn(a, 1, "#yuanwei-nomem\nA 不许读")
    turn(b, 1, "B 普通")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "control_nomem"
    assert call(b, "recall", {"query": "烤鱼"})["status"] == "ok"
    turn(b, 2, "B 又一轮")
    assert call(a, "recall", {"query": "烤鱼", "session_id": "sB"})["reason"] == "control_nomem"        # 模型填别的会话号没用
    assert call(b, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_refusals_happen_before_any_search_including_old_cursors(tmp_path):
    from helpers2 import Counter
    mem, a = mk(tmp_path)
    mem.cfg.recall_page_chars = 60
    turn(a, 1, "普通")
    first = call(a, "recall", {"query": "预算 烤鱼 贝壳 相机 爬山 开会"})
    cur = None
    if "cursor=" in first["text"]:
        cur = first["text"].split("只给 cursor=", 1)[1].split("\n")[0].strip()
    turn(a, 2, "#yuanwei-nomem")
    cnt = Counter(mem)
    try:
        for args in ({"query": "烤鱼"}, {"cursor": cur or "x:0:0"}):
            assert call(a, "recall", args)["status"] == "refused"
        assert call(a, "get_messages", {"ids": ["x"]})["status"] == "refused" and call(a, "attachments", {"message_id": "x"})["status"] == "refused"
        assert cnt.calls == []
    finally:
        cnt.close()


def test_a_read_that_started_in_turn_1_is_dropped_when_turn_2_arrives_before_it_returns(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通")
    rd = mem._reader
    orig = rd.select_multi

    def sel(*args, **kw):
        out = orig(*args, **kw)
        turn(a, 2, "#yuanwei-nomem")                                  # 搜索做完、还没返回，下一轮到了
        return out
    rd.select_multi = sel
    r = call(a, "recall", {"query": "吃了什么烤鱼"})
    assert r["status"] == "refused" and TEXTS[1] not in r["text"]


def test_on_turn_start_failing_halfway_never_reuses_the_previous_turns_allow(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"
    a.acc.fault = lambda point: (_ for _ in ()).throw(RuntimeError("db busy")) if point == "between_open_and_complete" else None
    with pytest.raises(RuntimeError):
        turn(a, 2, "#yuanwei-nomem")                                  # 宿主把插件的 on_turn_start 异常吞掉；这里直接看后果
    a.acc.fault = None
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "refused"


def test_persistent_switch_blocks_reads_and_the_receipt_is_sent_once_per_turn(tmp_path):
    mem, a = mk(tmp_path)
    mem.set_read_permission(False)
    turn(a, 1, "普通")
    assert a.prefetch("普通", session_id="hs1").startswith("本轮未新增读取长期记忆")
    assert a.prefetch("普通", session_id="hs1") == ""
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "workspace_setting"
    mem.clear_read_permission()
    turn(a, 2, "普通二")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_command_on_the_second_line_is_not_a_control(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通的一句\n#yuanwei-nomem")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_process_dies_after_the_busy_queue_then_the_next_open_flushes_it(tmp_path):
    from yuanwei import Memory
    mem, a = mk(tmp_path)
    turn(a, 1, "死掉之前来的话")
    blocker = sqlite3.connect(mem.db_path, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        mem.store.conn.execute("PRAGMA busy_timeout=50")
        a.sync_turn("死掉之前来的话", "回复", session_id="hs1", messages=msgs("死掉之前来的话"))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    path = mem.db_path
    ws = mem.workspace if hasattr(mem, "workspace") else "w"
    del a
    mem2 = Memory.open(path, ws, config=mem.cfg, spawn="off")
    mem2.flush_pending()
    assert [r["role"] for r in mem2.store.list_messages(mem2.ns) if r["session_id"] == "hs1"] == ["user", "assistant"]


# ================================================================== 升级不重写：薄接头、按功能探测
def test_plugin_file_imports_only_the_public_memory_provider_from_hermes():
    import ast
    from yuanwei import hermes_setup as HS
    tree = ast.parse(HS.plugin_init_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module)
    hermes_side = {m for m in mods if m and m.split(".")[0] not in ("json", "os", "sys", "abc", "yuanwei")}
    assert hermes_side == {"agent.memory_provider"}                          # 接头只依赖 Hermes 公开的记忆插件基类，别的 Hermes 内部模块一个都不 import


def load_plugin_with_base(base_text, tmp_path):
    p = tmp_path / "base_mp.py"
    p.write_text(base_text)
    base, ns = load_plugin(str(p), tmp_path)
    return base, ns


def test_plugin_survives_a_base_class_with_a_new_required_method_and_says_so(tmp_path):
    from test_hermes_compat import BASE
    text = BASE.replace("    def system_prompt_block(self): return \"\"", "    @abc.abstractmethod\n    def brand_new_required(self): ...\n    def system_prompt_block(self): return \"\"")
    base, ns = load_plugin_with_base(text, tmp_path)
    prov = ns.YuanweiMemoryProvider()                                   # 不会因为宿主多了一个必须实现的方法就起不来
    assert isinstance(prov, base.MemoryProvider) and prov.brand_new_required() is None
    assert ns.COMPAT["extra_abstract"] == ["brand_new_required"]


def test_plugin_methods_accept_unknown_extra_keywords_from_a_newer_host(tmp_path):
    from test_hermes_compat import BASE
    base, ns = load_plugin_with_base(BASE, tmp_path)
    prov = ns.YuanweiMemoryProvider()
    prov.on_turn_start(1, "x", author_is_bot=False, brand_new_kw=1)       # 没初始化也不炸
    prov.prefetch("q", session_id="s", brand_new_kw=1)
    prov.sync_turn("u", "a", session_id="s", messages=[], turn_author=None, brand_new_kw=1)
    prov.on_session_switch("n", parent_session_id="p", reset=False, rewound=False, brand_new_kw=1)
    prov.on_session_end([])
    names = [x["name"] for x in prov.get_tool_schemas(brand_new_kw=1)]                  # 没初始化：交完整的固定清单（Hermes 在 initialize 之前登记工具名；修补二）
    assert "recall" in names and names == [x["name"] for x in prov.get_tool_schemas()]


def test_adapter_features_follow_what_the_host_base_really_offers(tmp_path):
    import abc

    class NoMessages(abc.ABC):
        def sync_turn(self, user_content, assistant_content, *, session_id=""): ...
        def handle_tool_call(self, tool_name, args, **kw): ...
        def get_tool_schemas(self): ...
        def prefetch(self, query, *, session_id=""): ...
        def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kw): ...
        def on_turn_start(self, turn_number, message, **kw): ...
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline", host_base=NoMessages)
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    st = a.status()["compat"]
    assert st["off"]["write"].startswith("host_sync_turn_without_messages") and "tools" not in st["off"]
    turn(a, 1, "一问")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"             # 读取照常
    a.sync_turn("一问", "一答", session_id="hs1", messages=msgs("一问"))
    assert stored(mem) == [] and a.status()["skipped"] == {"feature_off_write": 1}


def test_adapter_without_host_base_assumes_everything_on_and_reports_no_compat_problem(tmp_path):
    mem, a = mk(tmp_path)
    assert a.status()["compat"] == {"off": {}, "extra_abstract": []}


def test_any_underscore_synthetic_flag_counts_as_machinery_not_just_the_known_names(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "一句")
    m = msgs("一句")
    m[-3] = dict(m[-3], _some_future_nudge_synthetic=True)
    a.sync_turn("一句", "答", session_id="hs1", messages=m)
    assert stored(mem) == [] and a.status()["skipped"].get("machinery_row") == 1


def test_tool_call_naming_another_profile_is_refused(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通")
    home = str(tmp_path / "home")
    ok = json.loads(a.handle_tool_call("recall", {"query": "烤鱼"}, hermes_home=home, agent_identity="default"))
    assert ok["status"] == "ok"
    bad = json.loads(a.handle_tool_call("recall", {"query": "烤鱼"}, hermes_home=str(tmp_path / "other-profile")))
    assert bad["status"] == "refused" and bad["reason"] == "profile_mismatch" and TEXTS[1] not in bad["text"]
    assert json.loads(a.handle_tool_call("recall", {"query": "烤鱼"}))["status"] == "ok"


def test_each_load_gets_a_new_instance_and_no_module_level_profile_state(tmp_path):
    from test_hermes_compat import BASE
    base, ns = load_plugin_with_base(BASE, tmp_path)
    got = []
    ns.register(types.SimpleNamespace(register_memory_provider=got.append))
    ns.register(types.SimpleNamespace(register_memory_provider=got.append))
    assert got[0] is not got[1] and got[0]._a is None


def test_plugin_that_cannot_start_never_raises_into_hermes_and_says_why(tmp_path, monkeypatch):
    from test_hermes_compat import BASE
    base, ns = load_plugin_with_base(BASE, tmp_path)
    home = tmp_path / "home"
    (home / "yuanwei").mkdir(parents=True)
    (home / "yuanwei" / "config.json").write_text(json.dumps({"hermes_home": str(home), "db": "/nonexistent-dir/x/m.sqlite", "workspace": "w", "spawn": "off", "src_path": os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    prov = ns.YuanweiMemoryProvider()
    prov.initialize("s", platform="cli", agent_context="primary", hermes_home=str(home))          # 数据库打不开：Hermes 照常起，插件这一项关掉
    assert prov.get_tool_schemas() == [] and prov.prefetch("q", session_id="s") == ""
    prov.on_turn_start(1, "x")
    prov.sync_turn("u", "a", session_id="s", messages=[])
    assert prov.status()["active"] is False and prov.status()["init_error"]
    out = json.loads(prov.handle_tool_call("recall", {"query": "x"}))
    assert out["status"] == "error"
