"""第 56 条第一批·审后修补第三节：不许新记宿主的话，不许藏掉丢了的话。
假宿主模块，不 import 真宿主，不联网。"""
import json

import pytest

import fix56_fakehost as FH
import hermes_midturn_contract as MC
from test_fix55_midturn import Host, _chat_adapter, counts, skipped, stored_rows, turn
from test_fix56_rowproof import OLD, OPENING, SUMMARY, TODO, host_rows, lost_opening_rows
from test_fix56_rowproof import got as rp_got
from test_fix56_rowproof import run as rp_run
from test_hermes_adapter import mk
from yuanwei import hermes_turn as HT
from yuanwei.hermes_adapter import HermesAdapter

REPLY = "事有原委，话有出处"


# ================================================================== 三.1 固定开头
def test_the_goal_head_has_no_closing_bracket_so_both_goal_forms_match():
    for head in ("[Continuing toward your standing goal]\nGoal: 写完", "[Continuing toward your standing goal — a quality gate failed]\nGoal: 写完"):
        assert HT.host_own_head(head), head                                                 # 带方括号的开头一律由 host_own_head 按「方括号开头」认（修补二乙1）
    assert "[Continuing toward your standing goal]" not in HT.HOST_OWN_HEADS


def test_both_goal_forms_are_not_recorded_on_the_lost_opening_path(tmp_path, monkeypatch):
    for i, head in enumerate(("[Continuing toward your standing goal]\nGoal: 写完", "[Continuing toward your standing goal — a quality gate failed]\nGoal: 写完")):
        mem, a = rp_run(tmp_path / ("g%d" % i), monkeypatch, lost_opening_rows(), opening=head)
        assert rp_got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}, head


# ================================================================== 三.2 turn_origin：目标接着干、心跳、/goal resume 给 None
def test_a_pinned_or_heartbeat_event_gives_none_not_user_or_host(monkeypatch):
    for attr, val in (("inherit_channel_pin", True), ("_heartbeat_session_id", "heartbeat-session")):
        ev = FH.user_event("续跑")
        setattr(ev, attr, val)
        FH.install(monkeypatch, ev)
        assert HT.turn_origin("续跑") is None, attr
        ev = FH.host_event("续跑")
        setattr(ev, attr, val)
        FH.install(monkeypatch, ev)
        assert HT.turn_origin("续跑") is None, attr


def test_falsy_values_of_those_two_fields_change_nothing(monkeypatch):
    ev = FH.user_event("你好")
    ev.inherit_channel_pin, ev._heartbeat_session_id = False, None
    FH.install(monkeypatch, ev)
    assert HT.turn_origin("你好") == "user"


def test_a_goal_continuation_turn_with_a_lost_opening_row_is_not_recorded_as_today(tmp_path, monkeypatch):
    """/goal resume 的事件 internal 为假、没有 display_kind：不能被当成「宿主证明是用户开的」。"""
    mem, a = mk(tmp_path)
    ev = FH.user_event("继续做那件事")
    ev.inherit_channel_pin = True
    FH.install(monkeypatch, ev)
    turn(a, 1, "继续做那件事")
    assert [e.get("origin") for entries in a._pending.values() for e in entries] == [None]
    a.sync_turn("继续做那件事", REPLY, session_id="hs1", messages=lost_opening_rows())
    assert stored_rows(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


def test_the_same_turn_from_an_ordinary_user_event_is_still_recorded_opening_only(tmp_path, monkeypatch):
    mem, a = rp_run(tmp_path, monkeypatch, lost_opening_rows(), opening="继续做那件事")
    assert rp_got(mem) == [("user", "继续做那件事"), ("assistant", REPLY)]


# ================================================================== 三.3 待办快照行：开头句是宿主自己的固定开头时不放过
HEADS = ("[/plan — plan mode]\n计划…", "[/learn] The user wants…", "[/init] The user wants you to…", "Set up the 'x' automation for me",
         "[Continuing toward your standing goal]\nGoal: 写完", "[Continuing toward your standing goal — a quality gate failed]\nGoal: 写完", "[IMPORTANT: MCP servers have been reloaded.]\n\n你好")


def test_each_fixed_head_with_the_todo_row_order_is_not_recorded_as_today(tmp_path, monkeypatch):
    for i, head in enumerate(HEADS):
        mem, a = rp_run(tmp_path / ("h%d" % i), monkeypatch, host_rows(opening=head), opening=head)
        assert rp_got(mem) == [] and skipped(a) == {"machinery_row": 1}, head


def test_an_ordinary_opening_with_the_todo_row_order_is_still_recorded(tmp_path, monkeypatch):
    mem, a = rp_run(tmp_path, monkeypatch, host_rows())
    assert rp_got(mem) == [("user", OPENING), ("assistant", REPLY)]


def test_leading_white_space_before_a_fixed_head_does_not_hide_it(tmp_path, monkeypatch):
    head = "  [Continuing toward your standing goal]\nGoal: 写完"
    mem, a = rp_run(tmp_path, monkeypatch, host_rows(opening=head), opening=head)
    assert rp_got(mem) == [] and skipped(a) == {"machinery_row": 1}


# ================================================================== 三.4 丢了开头行、不是一对一而且有改口／插话行
STEER = MC.steer_row(MC.steer_join(["再加一条要求"]))


def _shared(tmp_path, monkeypatch, rows, chat_type="thread", opening=OPENING):
    if chat_type == "dm":
        mem, a = _chat_adapter(tmp_path)
    else:
        from helpers2 import organized
        d = tmp_path / "s"
        d.mkdir()
        mem = organized(d, host="hermes")
        a = HermesAdapter({"spawn": "off", "owners": {"discord": ["u1"]}}, memory=mem, executor="inline")
        a.initialize("hs1", platform="discord", agent_context="primary", hermes_home=str(d / "home"), user_id="u1", chat_type="thread", chat_id="x")
    FH.install(monkeypatch, FH.user_event(opening))
    turn(a, 1, opening, author_id="u1")
    a.sync_turn(opening, REPLY, session_id="hs1", messages=rows)
    return mem, a


def test_a_shared_chat_with_a_midturn_row_and_a_lost_opening_row_is_not_recorded_as_today(tmp_path, monkeypatch):
    mem, a = _shared(tmp_path, monkeypatch, lost_opening_rows(after=[STEER]))
    assert stored_rows(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


def test_a_private_chat_with_the_same_rows_is_recorded_opening_only(tmp_path, monkeypatch):
    mem, a = _shared(tmp_path, monkeypatch, lost_opening_rows(after=[STEER]), chat_type="dm")
    assert stored_rows(mem) == [("user", OPENING), ("assistant", REPLY)] and skipped(a) == {}
    assert counts(a)["totals"] == {"opening_only:row_not_found": 1}


def test_a_shared_chat_without_any_midturn_row_still_takes_the_lost_opening_path(tmp_path, monkeypatch):
    mem, a = _shared(tmp_path, monkeypatch, lost_opening_rows())
    assert stored_rows(mem) == [("user", OPENING), ("assistant", REPLY)]


# ================================================================== 三.5 宿主开的轮：开头行之后有改口、插话时不写「宿主自己开的」
def _host_turn(tmp_path, monkeypatch, rows, text="后台任务完成的通知"):
    from test_fix55_accounting import acct
    mem, a = mk(tmp_path)
    FH.install(monkeypatch, FH.host_event(text))
    turn(a, 1, text)
    a.sync_turn(text, "好", session_id="hs1", messages=rows)
    turn(a, 2, "问状态")
    return mem, a, acct(a)


def _not_recorded_rows(mem):
    return mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='not_recorded'").fetchone()[0]


def test_a_host_origin_turn_with_a_steer_row_after_the_opening_row_is_not_counted_as_host_started(tmp_path, monkeypatch):
    text = "后台任务完成的通知"
    h = Host(text).tool().steer("停，改成七点").final("好")
    mem, a, r = _host_turn(tmp_path, monkeypatch, h.m)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}
    assert _not_recorded_rows(mem) == 0
    assert (r["unrecorded"], r["host_started"]) == (1, 0)


def test_a_host_origin_turn_with_a_redirect_row_is_not_counted_as_host_started_either(tmp_path, monkeypatch):
    text = "后台任务完成的通知"
    h = Host(text).tool().redirect("改成七点").final("好")
    mem, a, r = _host_turn(tmp_path, monkeypatch, h.m)
    assert stored_rows(mem) == [] and _not_recorded_rows(mem) == 0
    assert (r["unrecorded"], r["host_started"]) == (1, 0)


def test_a_host_origin_turn_without_midturn_rows_is_still_host_started(tmp_path, monkeypatch):
    text = "后台任务完成的通知"
    h = Host(text).tool().final("好")
    mem, a, r = _host_turn(tmp_path, monkeypatch, h.m)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}
    assert _not_recorded_rows(mem) == 1 and (r["unrecorded"], r["host_started"]) == (0, 1)


def test_a_host_origin_turn_whose_opening_row_is_not_found_is_host_started_only_when_no_midturn_row_exists(tmp_path, monkeypatch):
    mem, a, r = _host_turn(tmp_path / "a", monkeypatch, lost_opening_rows())
    assert _not_recorded_rows(mem) == 1 and r["host_started"] == 1
    mem, a, r = _host_turn(tmp_path / "b", monkeypatch, lost_opening_rows(after=[STEER]))
    assert stored_rows(mem) == [] and _not_recorded_rows(mem) == 0 and (r["unrecorded"], r["host_started"]) == (1, 0)
