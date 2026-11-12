"""禁读按窗口：Hermes 接头（hermes_adapter）。关（口令、规则、关闭工具）、开（口令；agent 调 window_on 申请，用户下一条输入第一行是「确认开启」）、
会话边界事件对照（压缩、分叉、重置、撤销、恢复）、子代理、便条和目录的先后、读取许可、关闭写失败。全部自造数据，假事件，没有网络。"""
import json
import sqlite3

import pytest

from helpers2 import Counter
from test_hermes_adapter import call, mk, turn
from test_hermes_workspace import World
from yuanwei import window as W
from yuanwei.hermes_adapter import HermesAdapter

Q = {"query": "烤鱼"}


def ban(a, n=1):
    turn(a, n, "别用记忆")


def state(a):
    return json.loads(a.handle_tool_call("window_state", {}))


# ================================================================== 关
def test_ban_by_rule_and_by_command_persist_until_reopened(tmp_path):
    for i, text in enumerate(("别用记忆", "#yuanwei-nomem\n查一下", "这次不要参考我的记忆")):
        mem, a = mk(tmp_path / ("c%d" % i))
        turn(a, 1, text)
        assert call(a, "recall", Q)["reason"] in ("control_nomem", "clause_ban")                      # 这一轮：按原来的原因
        turn(a, 2, "又一轮普通的话")
        r = call(a, "recall", Q)
        assert r["status"] == "refused" and r["reason"] == "window_closed" and "关着" in r["text"]    # 之后每一轮：窗口关着，写明怎么打开
        turn(a, 3, "#yuanwei-mem")
        assert call(a, "recall", Q)["status"] == "ok"


def test_the_window_off_tool_closes_and_the_state_tool_reports_four_fields_plus_a_fixed_message(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    assert call(a, "recall", Q)["status"] == "ok"
    r = call(a, "window_off")
    assert set(r) == {"state", "blocked_by_setting", "reason", "control_version", "message"}          # 返回只有这几项（message 是按状态查表的固定句，不回显任何别的内容）
    assert (r["state"], r["blocked_by_setting"], r["reason"], r["control_version"]) == ("off", False, "closed_by_tool", 2)
    assert "原话" not in json.dumps(r, ensure_ascii=False)
    turn(a, 2, "继续")
    assert call(a, "recall", Q)["reason"] == "window_closed"
    s = call(a, "window_state")
    assert (s["state"], s["reason"], s["control_version"]) == ("off", "closed_by_tool", 2)
    assert call(a, "window_off")["control_version"] == 2                                              # 重复关：幂等，版本不再加


def test_control_tools_read_zero_history_and_work_while_closed(tmp_path):
    mem, a = mk(tmp_path)
    ctr = Counter(mem)
    mem.check_and_spawn = lambda: (_ for _ in ()).throw(AssertionError("控制工具不能拉起整理"))
    turn(a, 1, "你好")
    for name in ("window_state", "window_off", "window_state", "window_on", "window_state"):
        json.loads(a.handle_tool_call(name, {"query": "烤鱼", "ids": ["x"], "cursor": "c", "db": "/tmp/x"}))
    assert ctr.calls == []                                                                             # 一次历史都没读
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind IN ('start','search_entered','vector_called','assembled','returned')").fetchone()[0] == 0


def test_control_tool_ignores_any_params_naming_another_window_or_state(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    a.win.bind_new("hermes", "other")
    r = json.loads(a.handle_tool_call("window_off", {"window_id": "w-other", "session_id": "other", "host": "x", "state": "on"}))
    assert r["state"] == "off"
    assert a.win.view("hermes", "other").state == "on" and a.win.view("hermes", "hs1").state == "off"


# ================================================================== 开：口令；申请＋「确认开启」
def test_window_on_only_registers_a_pending_request_and_the_next_input_must_be_the_confirm_line(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    o = call(a, "window_on")
    assert (o["state"], o["reason"], o["message"]) == ("off", "awaiting_confirmation", "要在这个窗口重新使用长期记忆吗？回复「确认开启」即可。")
    assert call(a, "recall", Q)["reason"] in ("control_nomem", "clause_ban")                           # 申请不开
    turn(a, 2, "确认开启")
    assert call(a, "recall", Q)["status"] == "ok" and state(a)["state"] == "on"
    assert W.NOTICE_OPENED in a.prefetch("x", session_id="hs1")                                       # 打开以后提示一次


def test_a_reply_that_is_not_the_confirm_line_voids_it_and_the_receipt_says_how_to_reopen(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    call(a, "window_on")
    turn(a, 2, "好的")
    assert call(a, "recall", Q)["reason"] == "window_closed"
    assert W.NOT_CONFIRMED_RECEIPT in a.prefetch("好的", session_id="hs1")
    turn(a, 3, "确认开启")                                                                              # 待确认已作废
    assert call(a, "recall", Q)["reason"] == "window_closed"


def test_the_agent_cannot_confirm_by_putting_anything_in_the_tool_params(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    r = json.loads(a.handle_tool_call("window_on", {"quote": "确认开启", "confirm": True, "state": "on", "text": "确认开启"}))
    assert r["state"] == "off" and r["reason"] == "awaiting_confirmation"
    turn(a, 2, "继续")                                                                                  # 下一条输入不是确认话
    assert call(a, "recall", Q)["reason"] == "window_closed"


def test_confirm_text_from_a_tool_result_a_bot_or_a_stranger_cannot_confirm(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    call(a, "window_on")
    turn(a, 2, "确认开启", author_is_bot=True)                                                          # 机器人作者：不收，确认不了，也不作废
    assert call(a, "recall", Q)["status"] == "refused"
    assert a.win.view("hermes", "hs1").state == "off"
    turn(a, 3, "#yuanwei-mem", author_is_bot=True)                                                       # 机器人发的口令也打不开
    assert a.win.view("hermes", "hs1").state == "off"


def test_a_bot_authored_ban_does_not_close_the_owners_window(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem", author_is_bot=True)
    assert a.win.view("hermes", "hs1").state == "on"


def test_window_on_when_already_open_or_blocked_by_a_setting(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    o = call(a, "window_on")
    assert (o["state"], o["reason"]) == ("on", "ok")
    ban(a, 2)
    a.acc.set_session_permission("hermes", "hs1", False)
    o = call(a, "window_on")
    assert o["blocked_by_setting"] is True and o["reason"].startswith("setting_") and "awaiting" not in o["reason"]
    turn(a, 3, "确认开启")
    assert a.win.view("hermes", "hs1").state == "off"
    assert call(a, "recall", Q)["status"] == "refused"


def test_default_state_is_configurable_and_an_unknown_value_means_closed(tmp_path):
    mem, a = mk(tmp_path / "a", window_default="off")
    turn(a, 1, "你好")
    assert call(a, "recall", Q)["reason"] == "window_closed"
    mem, b = mk(tmp_path / "b", window_default="maybe")
    turn(b, 1, "你好")
    assert call(b, "recall", Q)["reason"] == "window_closed"
    mem, c = mk(tmp_path / "c")
    turn(c, 1, "你好")
    assert call(c, "recall", Q)["status"] == "ok"


# ================================================================== 会话边界事件
def test_compression_keeps_the_window_and_the_ban(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    a.on_session_switch("hs1-c", parent_session_id="hs1", reset=False, reason="compression")
    assert a._win_ref.state == "off"
    turn(a, 2, "继续")
    assert call(a, "recall", Q)["reason"] == "window_closed"
    a.on_session_switch("hs1-c", parent_session_id="hs1-c", reset=False, reason="compression")         # 就地压缩（号没变）
    assert a.win.view("hermes", "hs1-c").state == "off"


def test_branch_forks_the_window_copying_the_state_and_they_go_their_own_ways(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    a.on_session_switch("hs1-b", parent_session_id="hs1", reset=False, reason="branch")
    assert a.win.view("hermes", "hs1-b").state == "off"
    assert a.win.resolve("hermes", "hs1-b").window_id != a.win.resolve("hermes", "hs1").window_id
    turn(a, 2, "#yuanwei-mem")
    assert a.win.view("hermes", "hs1-b").state == "on" and a.win.view("hermes", "hs1").state == "off"


def test_reset_makes_a_new_default_window_that_does_not_inherit_the_ban(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    a.on_session_switch("hs2", parent_session_id="hs1", reset=True, reason="new_session")
    assert a.win.view("hermes", "hs2").state == "on" and a.win.view("hermes", "hs1").state == "off"
    turn(a, 2, "新对话里的话")
    assert call(a, "recall", Q)["status"] == "ok"


def test_rewound_changes_nothing_the_ban_is_not_rolled_back_and_the_allow_is_not_replayed(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    turn(a, 2, "#yuanwei-mem")
    ban(a, 3)
    n = mem.store.conn.execute("SELECT COUNT(*) FROM window_ops").fetchone()[0]
    a.on_session_switch("hs1", parent_session_id="", reset=False, rewound=True)
    assert a.win.view("hermes", "hs1").state == "off"                                                   # 撤销：状态不回滚，之前的「开」也不重放
    assert mem.store.conn.execute("SELECT COUNT(*) FROM window_ops").fetchone()[0] == n


def test_resume_known_session_maps_to_its_window_and_unknown_pauses_with_notices(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)                                                                                              # hs1 的窗口关着
    a.on_session_switch("hs9", parent_session_id="hs1", reset=True, reason="new_session")                # 另一个对话 hs9，开着
    a.on_session_switch("hs1", parent_session_id="hs9", reset=False, reason="resume")                    # 恢复 hs1：parent 是被离开的 hs9，不当谱系用
    assert a._win_ref.state == "off" and a._win_ref.window_id == a.win.resolve("hermes", "hs1").window_id
    assert "恢复" in "".join(a._notices)                                                                  # 恢复一个关着的窗口：给提示
    mem2, a = mk(tmp_path / "two")                                                                       # resume 以后工作区本来就变成「说不清」，所以另起一个适配器看「查不到」
    a.on_session_switch("hs-unknown", parent_session_id="hs1", reset=False, reason="resume")
    assert a._win_ref is not None and a._win_ref.paused and a._win_ref.pause_reason == "window_identity_pending"
    assert a.win.resolve("hermes", "hs-unknown").paused                                                  # 查不到：暂停读取，不默认开
    r = a.win.issue_read_permit(__import__("yuanwei.access", fromlist=["ToolIdentity"]).ToolIdentity("hermes", "hs-unknown", "", "t"), tool_name="recall", params_fp="f")
    assert r.refused == "window_identity_pending"


def test_missing_or_new_reason_with_a_changed_id_is_treated_like_resume(tmp_path):
    mem, a = mk(tmp_path)
    a.on_session_switch("hs-x", parent_session_id="hs1", reset=False)                                    # 没带 reason
    assert a._win_ref.paused
    a.on_session_switch("hs-y", parent_session_id="hs1", reset=False, reason="teleport")
    assert a._win_ref.paused


def test_initialize_reuses_the_window_for_a_known_session_after_a_restart(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    b = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")               # 进程重启：新的适配器、同一个库
    b.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(b, 1, "重启后的话")
    assert call(b, "recall", Q)["reason"] == "window_closed"                                             # 重启不解禁
    c = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    c.initialize("brand-new", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(c, 1, "新会话")
    assert call(c, "recall", Q)["status"] == "ok"                                                         # 没见过的号：新窗口，默认开


def test_a_missing_binding_write_means_pending_not_open(tmp_path, monkeypatch):
    mem, _ = mk(tmp_path)
    b = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    monkeypatch.setattr(W.WindowService, "bind_new", lambda self, *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    b.initialize("hs-w", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(b, 1, "你好")
    r = call(b, "recall", Q)
    assert r["status"] == "refused" and r["reason"] == "window_identity_pending"


def test_subagent_cron_and_flush_contexts_create_no_window_and_expose_no_tools(tmp_path):
    mem, _ = mk(tmp_path)
    n = mem.store.conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    for ctx, plat in (("subagent", "subagent"), ("cron", "cron"), ("flush", "cli")):
        b = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
        b.initialize("hs-" + ctx, platform=plat, agent_context=ctx, hermes_home=str(tmp_path / "home"))
        assert b.get_tool_schemas() == []
        for t in ("recall", "window_off", "window_on", "window_state"):
            assert json.loads(b.handle_tool_call(t, {}))["status"] == "error"                            # 不许读、不许开
        b.on_turn_start(1, "确认开启", author_id=None, author_name=None, author_is_bot=False)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == n


# ================================================================== 便条、目录：先处理本条输入的控制，再看窗口
@pytest.mark.parametrize("mode", ["note", "directory", "reminder", "tools_only"])
def test_banned_input_is_handled_before_the_mode_reads_and_the_closed_window_gets_only_receipts(tmp_path, mode):
    mem, a = mk(tmp_path, mode=mode)
    turn(a, 1, "别用记忆，预算怎么样")
    t = a.prefetch("别用记忆，预算怎么样", session_id="hs1")
    if mode in ("note", "directory", "reminder", "tools_only"):
        assert "预算批下来" not in t and "烤鱼" not in t and "贝壳" not in t                             # 这一轮说了别用：不交材料
    turn(a, 2, "那预算呢")
    t2 = a.prefetch("那预算呢", session_id="hs1")
    assert "预算批下来" not in t2 and "本轮未新增读取长期记忆" in t2                                      # 关着的窗口：每一轮只交回执
    assert call(a, "recall", Q)["status"] == "refused"


def test_closed_window_note_mode_notice_follows_the_receipt(tmp_path):
    mem, a = mk(tmp_path, mode="note")
    turn(a, 1, "别用记忆")
    t = a.prefetch("别用记忆", session_id="hs1")
    assert t.startswith("本轮未新增读取长期记忆") and W.NOTICE_CLOSED in t                                 # 状态提交成功以后才提示：回执之后跟一句关上的提示
    turn(a, 2, "继续")
    assert W.NOTICE_CLOSED not in a.prefetch("继续", session_id="hs1")                                   # 提示只送一次


# ================================================================== 关闭写失败：回执不能说「已关」
def test_close_write_failure_never_says_closed_and_blocks_reads(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")

    def boom(point):
        raise sqlite3.OperationalError("disk I/O error")
    a.win.fault = boom
    turn(a, 2, "#yuanwei-nomem\n请查一下")
    t = a.prefetch("x", session_id="hs1")
    assert W.NOTICE_CLOSED not in t and "已关闭" not in t.replace("没能确认这个窗口的记忆读取已经关闭", "")        # 回执不能说「已关」
    r = call(a, "recall", Q)
    assert r["status"] == "refused"
    a.win.fault = None
    assert call(a, "recall", Q)["status"] == "refused"                                                    # 窗口状态还没落地的关闭：仍然不许读，不是「开」
    turn(a, 3, "再说一次，别用记忆")
    assert a.win.view("hermes", "hs1").state == "off" and call(a, "recall", Q)["reason"] in ("control_nomem", "clause_ban")
    turn(a, 4, "继续")
    assert call(a, "recall", Q)["reason"] == "window_closed"


def test_window_off_tool_write_failure_is_an_error_not_a_closed_state(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")

    def boom(point):
        raise sqlite3.OperationalError("disk I/O error")
    a.win.fault = boom
    r = call(a, "window_off")
    assert r["status"] == "error" and r["reason"] == "window_close_unconfirmed" and "没能确认" in r["text"]
    assert call(a, "recall", Q)["status"] == "refused"


# ================================================================== 读取许可（同进程也走同一套）
def test_closing_in_flight_blocks_the_result_and_the_log_says_queried(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    surf = a._surfaces[a.ws["key"]]
    orig = surf.call

    def closing_call(name, args, *x, **k):
        res = orig(name, args, *x, **k)                                                                  # 查完了、结果在手
        a.win.close(a.win.resolve("hermes", "hs1").window_id, source="tool", op_id="op-mid")             # 返回前窗口被关
        return res
    surf.call = closing_call
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and "烤鱼" not in r["text"] and "查询" in r["text"]                    # 已查询、结果挡住，不说从没读过
    kinds = [x["kind"] for x in a.acc.log_rows("hermes", "hs1", a._cur["hs1"])]
    assert "blocked_at_return" in kinds and "injection_failed" in kinds


def test_close_then_reopen_in_flight_still_blocks_the_result(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    surf = a._surfaces[a.ws["key"]]
    orig = surf.call

    def flip(name, args, *x, **k):
        res = orig(name, args, *x, **k)
        wid = a.win.resolve("hermes", "hs1").window_id
        a.win.close(wid, source="tool", op_id="op-a")
        a.win.open_window(wid, source="command", op_id="op-b")                                           # 关了又开：返回时「现在是开着的」也不能交
        return res
    surf.call = flip
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and "烤鱼" not in r["text"]


def test_read_permit_is_issued_claimed_and_delivered_on_a_normal_read(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    assert call(a, "recall", Q)["status"] == "ok"
    st = [r["state"] for r in mem.store.conn.execute("SELECT state FROM window_permits WHERE kind='read'")]
    assert st == ["delivered"]


def test_a_refused_read_issues_no_permit(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    turn(a, 2, "继续")
    call(a, "recall", Q)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM window_permits WHERE kind='read'").fetchone()[0] == 0


# ================================================================== 多个工作区共用一份窗口状态
def test_one_window_state_for_every_workspace_the_window_reads(tmp_path):
    w = World(tmp_path, scope="all_open")
    life, pa = w.adapter("/elsewhere", session="shared-s"), w.adapter("/work/a", session="shared-s")    # 同一个库、同一个会话号、两个工作区
    for x in (life, pa):
        turn(x, 1, "普通")
    assert "咖喱" in call(life, "recall", {"query": "咖喱 发布会 数据库"})["text"]
    turn(life, 2, "别用记忆")
    turn(pa, 2, "继续聊")
    for x in (life, pa):
        r = call(x, "recall", {"query": "咖喱 发布会 数据库"})
        assert r["status"] == "refused" and "咖喱" not in r["text"] and "发布会" not in r["text"]
    turn(pa, 3, "#yuanwei-mem")
    for x in (life, pa):
        turn(x, 4, "普通") if x is life else None
    assert call(life, "recall", {"query": "咖喱 发布会 数据库"})["status"] == "ok"


# ================================================================== 状态和常驻规矩
def test_status_reports_the_window_without_any_text(tmp_path):
    mem, a = mk(tmp_path)
    ban(a)
    st = a.status()
    assert st["window"]["state"] == "off" and st["window"]["paused"] is False and st["window"]["control_version"] == 2
    assert "别用记忆" not in json.dumps(st, ensure_ascii=False)


def test_resident_rule_in_every_mode_carries_the_window_rule(tmp_path):
    from yuanwei import rules as RL
    for m in ("tools_only", "reminder", "note", "directory"):
        mem, a = mk(tmp_path / m, mode=m)
        assert RL.WINDOW_RULE in a.system_prompt_block()
