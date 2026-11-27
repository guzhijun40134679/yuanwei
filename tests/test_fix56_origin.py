"""清单第 56 条第一批·第 4 节：谁开的这一轮（hermes_turn.turn_origin）。假宿主模块，不 import 真宿主，不联网。"""
import ast
import os
import sys

import pytest

import fix56_fakehost as FH
from test_fix55_accounting import acct
from test_hermes_adapter import mk, msgs, stored, turn
from yuanwei import hermes_turn as HT


# ================================================================== turn_origin 本身
def test_user_event_with_the_same_text_is_user(monkeypatch):
    FH.install(monkeypatch, FH.user_event("你好"))
    assert HT.turn_origin("你好") == "user"


def test_internal_event_is_host(monkeypatch):
    FH.install(monkeypatch, FH.host_event("后台任务完成"))
    assert HT.turn_origin("后台任务完成") == "host"


def test_display_kind_not_none_is_host_even_when_internal_is_false(monkeypatch):
    FH.install(monkeypatch, FH.Event("定时唤醒", internal=False, display_kind="heartbeat"))
    assert HT.turn_origin("定时唤醒") == "host"


def test_internal_flag_alone_is_enough_for_host(monkeypatch):
    FH.install(monkeypatch, FH.host_event("后台任务完成"), kind_fn=lambda event: None)               # 宿主的 display_kind 说不出，但事件自己标了 internal
    assert HT.turn_origin("后台任务完成") == "host"


def test_no_gateway_module_gives_none(monkeypatch):
    FH.install(monkeypatch, FH.user_event("你好"), gateway=False)
    assert HT.turn_origin("你好") is None


def test_missing_name_in_the_gateway_module_gives_none(monkeypatch):
    FH.install(monkeypatch, FH.user_event("你好"))
    del sys.modules["gateway.response_filters"].display_kind_for_event
    assert HT.turn_origin("你好") is None


def test_no_current_event_gives_none(monkeypatch):
    FH.install(monkeypatch, None)
    assert HT.turn_origin("你好") is None


def test_event_text_different_from_the_hook_text_gives_none_even_for_an_internal_event(monkeypatch):
    FH.install(monkeypatch, FH.host_event("别的话"))
    assert HT.turn_origin("你好") is None
    FH.install(monkeypatch, FH.user_event("你好 "))                                      # 差一个空格也是不同
    assert HT.turn_origin("你好") is None


def test_display_kind_for_event_raising_gives_none(monkeypatch):
    def boom(event):
        raise RuntimeError("宿主改了")
    FH.install(monkeypatch, FH.user_event("你好"), kind_fn=boom)
    assert HT.turn_origin("你好") is None


def test_event_without_text_or_with_wrong_types_gives_none(monkeypatch):
    class NoText:
        internal = False
    FH.install(monkeypatch, NoText())
    assert HT.turn_origin("你好") is None
    FH.install(monkeypatch, FH.Event(123))
    assert HT.turn_origin("你好") is None
    FH.install(monkeypatch, FH.user_event("你好"))
    assert HT.turn_origin(None) is None and HT.turn_origin(5) is None


def test_the_module_imports_no_host_module_at_the_top_level():
    src = open(os.path.join(os.path.dirname(HT.__file__), "hermes_turn.py"), encoding="utf-8").read()
    top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert top == []


# ================================================================== 接头里：开轮存下来、同步时用
def _origin_of(a):
    return [e.get("origin") for entries in a._pending.values() for e in entries]


def test_turn_start_keeps_only_the_three_values_and_never_the_text(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    var = FH.install(monkeypatch, FH.user_event("甲话"))
    turn(a, 1, "甲话")
    var.set(FH.host_event("乙话"))
    turn(a, 2, "乙话")
    var.set(FH.user_event("别的"))
    turn(a, 3, "丙话")
    var.set(None)
    turn(a, 4, "丁话")
    assert _origin_of(a) == ["user", "host", None, None]
    blob = repr(a._pending)
    assert all(w not in blob for w in ("甲话", "乙话", "丙话", "丁话"))


def test_a_turn_the_host_says_it_started_is_not_recorded_even_when_no_row_has_a_mark(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    FH.install(monkeypatch, FH.host_event("后台任务完成的通知"))
    turn(a, 1, "后台任务完成的通知")
    a.sync_turn("后台任务完成的通知", "知道了", session_id="hs1", messages=msgs("后台任务完成的通知"))   # 行上没有任何标记
    assert stored(mem) == []
    assert a.skipped == {"machinery_row": 1}
    turn(a, 2, "下一轮")
    r = acct(a)
    assert (r["started"], r["recorded"], r["unrecorded"], r["host_started"]) == (2, 0, 0, 1)
    rows = [dict(x) for x in mem.store.conn.execute("SELECT kind, detail FROM read_log WHERE kind='not_recorded'")]
    assert rows == [{"kind": "not_recorded", "detail": '{"reason": "host_started_turn"}'}]


def test_a_user_turn_and_an_unknown_origin_turn_are_recorded_as_today(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    var = FH.install(monkeypatch, FH.user_event("甲话"))
    turn(a, 1, "甲话")
    a.sync_turn("甲话", "好", session_id="hs1", messages=msgs("甲话"))
    var.set(None)
    turn(a, 2, "乙话")
    a.sync_turn("乙话", "好", session_id="hs1", messages=msgs("乙话"))
    assert [(r, c) for _s, r, c in stored(mem)] == [("user", "甲话"), ("assistant", "好"), ("user", "乙话"), ("assistant", "好")]
    assert a.skipped == {}


def test_a_host_origin_turn_with_a_marked_row_is_still_one_machinery_row_and_one_host_started(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    FH.install(monkeypatch, FH.host_event("后台完成"))
    turn(a, 1, "后台完成")
    m = msgs("后台完成")
    m[-3] = dict(m[-3], display_kind="process_complete")
    a.sync_turn("后台完成", "好", session_id="hs1", messages=m)
    assert a.skipped == {"machinery_row": 1}
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='not_recorded'").fetchone()[0] == 1
