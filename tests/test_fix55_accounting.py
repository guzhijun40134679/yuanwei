"""清单第 55 条（0.1.0a9）D：对账——开始几轮、记下几轮、没记几轮、宿主自己开的轮。统计放 sqlstore，不动表结构，全部按 turn_id 去重；助手的 status 工具（只在会写的 Hermes 接头上）和 yuanwei status hermes 都给。"""
import json

import pytest

import hermes_midturn_contract as C
from test_fix55_midturn import OLD, Host, call, counts, mk, skipped, stored_rows, turn
from test_hermes_adapter import msgs
from yuanwei import sqlstore as SQ


def acct(a, host="hermes", session="hs1", current=True):
    return a.mem.store.turn_accounting(host, session, "", current_turn=a._cur.get(session) if current else None)


def status_turns(a):
    out = call(a, "status")
    assert out["status"] == "ok", out
    return json.loads(out["text"].split("\n", 1)[1])["turns"]


def plain_turn(a, n, text="随便聊聊", reply="好的"):
    turn(a, n, text)
    a.sync_turn(text, reply, session_id="hs1", messages=msgs(text))


def test_plain_turns_are_started_and_recorded_and_the_current_one_is_set_aside(tmp_path):
    mem, a = mk(tmp_path)
    for n in (1, 2, 3):
        plain_turn(a, n)
    turn(a, 4, "第四轮开始了")
    r = acct(a)
    assert (r["started"], r["recorded"], r["unrecorded"], r["host_started"]) == (4, 3, 0, 0)
    assert r["current"] == 1 and r["started"] == r["recorded"] + r["unrecorded"] + r["host_started"] + r["excluded"] + r["current"]


def test_a_turn_whose_sync_never_matched_counts_as_not_recorded(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "甲")
    a.sync_turn("别的话", "好", session_id="hs1", messages=msgs("别的话"))                      # no_pending_turn：这一轮开始了，没记下
    assert skipped(a) == {"no_pending_turn": 1}
    turn(a, 2, "乙")
    r = acct(a)
    assert (r["started"], r["recorded"], r["unrecorded"]) == (2, 0, 1)


def test_a_host_started_turn_is_counted_apart_and_leaves_one_fixed_reason_row(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "后台任务完成的通知")
    m = msgs("后台任务完成的通知")
    m[-3] = dict(m[-3], display_kind="process_complete")
    a.sync_turn("后台任务完成的通知", "知道了", session_id="hs1", messages=m)
    assert skipped(a) == {"machinery_row": 1}
    turn(a, 2, "下一轮")
    r = acct(a)
    assert (r["started"], r["recorded"], r["unrecorded"], r["host_started"]) == (2, 0, 0, 1)
    rows = [dict(x) for x in mem.store.conn.execute("SELECT kind, detail, call_id FROM read_log WHERE kind='not_recorded'")]
    assert rows == [{"kind": "not_recorded", "detail": '{"reason": "host_started_turn"}', "call_id": None}]


def test_a_real_turn_with_a_machinery_row_in_the_middle_still_counts_as_not_recorded(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool()
    h.m.append({"role": "user", "content": "后台完成", "display_kind": "process_complete"})
    h.final().sync(a)
    assert skipped(a) == {"machinery_row": 1}
    turn(a, 2, "下一轮")
    r = acct(a)
    assert (r["unrecorded"], r["host_started"]) == (1, 0)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='not_recorded'").fetchone()[0] == 0


def test_a_turn_that_refused_reading_is_still_recorded_and_is_not_dropped_from_either_side(tmp_path):
    mem, a = mk(tmp_path)
    plain_turn(a, 1, "#yuanwei-nomem 别用记忆")
    plain_turn(a, 2, "随便聊聊")
    turn(a, 3, "再来")
    assert mem.store.conn.execute("SELECT COUNT(*) FROM turns WHERE refused=1").fetchone()[0] == 1
    r = acct(a)
    assert (r["started"], r["recorded"], r["unrecorded"]) == (3, 2, 0)


def test_after_deleting_many_turns_and_chatting_on_the_numbers_still_add_up_and_nothing_is_reported_lost(tmp_path):
    mem, a = mk(tmp_path)
    for n in (1, 2, 3):
        plain_turn(a, n)
    turn(a, 4, "删掉这段")
    call(a, "window_delete")
    a.sync_turn("删掉这段", "请确认", session_id="hs1", messages=msgs("删掉这段"))
    turn(a, 5, "确认删除")
    a.sync_turn("确认删除", "删了", session_id="hs1", messages=msgs("确认删除"))
    assert stored_rows(mem) == [] and skipped(a).get("delete_confirm_turn") == 1
    plain_turn(a, 6, "删完以后接着聊")
    plain_turn(a, 7, "再聊一句")
    turn(a, 8, "又一轮")
    r = acct(a)
    assert r["unrecorded"] == 0 and r["recorded"] == 2 and r["host_started"] == 0
    assert r["started"] == r["recorded"] + r["unrecorded"] + r["host_started"] + r["excluded"] + r["current"] and r["excluded"] >= 4


def test_a_conversation_that_was_deleted_does_not_make_older_turns_look_lost_even_without_the_confirm_turn_row(tmp_path):
    """被删除覆盖的轮：同宿主、会话、分支，开始时间不晚于删除登记的时间。"""
    mem, a = mk(tmp_path)
    for n in (1, 2):
        plain_turn(a, n)
    mem.store.conn.execute("DELETE FROM turn_messages")                                    # 删除清掉了这些轮的消息绑定
    mem.store.conn.execute("DELETE FROM messages")
    mem.store.conn.execute("DELETE FROM no_record_turns")
    assert acct(a, current=False)["unrecorded"] == 2                                        # 没有删除登记：真的丢了
    mem.store.conn.execute("INSERT INTO deletions (del_id, window_id, conv_kind, conv_sha, source, state, floor_capture, registered_at, counts, db_id) VALUES ('d1', NULL, 'window', 'x', 'chat', 'complete', 0, '2999-01-01T00:00:00Z', '{}', 'x')")
    mem.store.conn.execute("INSERT INTO del_scope (del_id, host, session_id, branch) VALUES ('d1', 'hermes', 'hs1', '')")
    assert acct(a, current=False)["unrecorded"] == 0 and acct(a, current=False)["excluded"] == 2


def test_the_deletion_cover_is_per_conversation_and_per_time(tmp_path):
    mem, a = mk(tmp_path)
    plain_turn(a, 1)
    mem.store.conn.execute("DELETE FROM turn_messages")
    mem.store.conn.execute("INSERT INTO deletions (del_id, window_id, conv_kind, conv_sha, source, state, floor_capture, registered_at, counts, db_id) VALUES ('d1', NULL, 'window', 'x', 'chat', 'complete', 0, '2000-01-01T00:00:00Z', '{}', 'x')")
    mem.store.conn.execute("INSERT INTO del_scope (del_id, host, session_id, branch) VALUES ('d1', 'hermes', 'hs1', '')")
    assert acct(a, current=False)["unrecorded"] == 1                                         # 这一轮开始在删除以后：不被覆盖
    mem.store.conn.execute("UPDATE del_scope SET session_id='other'")
    mem.store.conn.execute("UPDATE deletions SET registered_at='2999-01-01T00:00:00Z'")
    assert acct(a, current=False)["unrecorded"] == 1                                         # 删的是别的会话：不覆盖


def test_the_whole_database_numbers_cover_every_session(tmp_path):
    mem, a = mk(tmp_path)
    plain_turn(a, 1)
    turn(a, 2, "甲")
    a.sync_turn("别的话", "好", session_id="hs1", messages=msgs("别的话"))
    r = SQ.turn_accounting(mem.store.conn)
    assert (r["started"], r["recorded"], r["unrecorded"]) == (2, 1, 1)


def test_the_status_tool_on_hermes_gives_the_session_numbers_the_sentence_and_up_to_three_recent_reasons(tmp_path):
    mem, a = mk(tmp_path)
    plain_turn(a, 1)
    for why in ("prefetch_error", "owner_unknown", "not_owner", "bot_author", "no_messages", "record_error", "machinery_row", "feature_off_write"):
        from yuanwei import skipreasons as SK
        SK.record(a._home, why, now="2027-01-01T00:00:0%dZ" % (len(why) % 9))
    turn(a, 2, "现在问状态")
    t = status_turns(a)
    assert t["session"] == {"started": 1, "recorded": 1, "unrecorded": 0, "auto": 0}
    assert "按了停止" in t["note"] and "上一轮还在存" in t["note"] and "库忙排队待补" in t["note"] and "算在没记里" in t["note"]
    assert [x["reason"] for x in t["recent_not_recorded"]] == ["record_error", "machinery_row", "feature_off_write"]          # 最近 3 次，去掉四种不算丢的
    assert all(set(x) == {"reason", "t"} for x in t["recent_not_recorded"])


def test_status_numbers_are_given_even_when_zero(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "第一轮")
    t = status_turns(a)
    assert t["session"] == {"started": 0, "recorded": 0, "unrecorded": 0, "auto": 0} and t["recent_not_recorded"] == []         # 当前这一轮不算开始


def test_the_status_tool_on_other_hosts_is_byte_identical(tmp_path):
    """MCP 等别的宿主：不是会写的 Hermes 接头，输出里没有 turns 这一项。"""
    from helpers2 import organized
    from yuanwei import hosts as H
    from yuanwei.access import Access
    from yuanwei.idsource import InProcessIdentity
    from yuanwei.tools import ToolSurface
    d = tmp_path / "x"
    d.mkdir()
    mem = organized(d, host="hermes")
    ids = InProcessIdentity()
    acc = Access(mem)
    sf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    tok = acc.turn_token("hermes", "s1", "1:abc")
    acc.begin_turn("hermes", "s1", tok, text="问状态", origin="user")
    ids.set("hermes", "s1", "", tok)
    res = sf.call("status", {})
    assert res.status == "ok"
    out = json.loads(res.text.split("\n", 1)[1])
    assert "turns" not in out and sorted(out) == sorted(["host", "host_version", "identity_chain", "mode", "tools", "limits", "backlog", "pending_writes", "pending_review", "flush_error_count",
                                                          "failed_steps", "abandoned_steps", "skipped_steps", "done_steps", "long_unorganized", "paused", "paused_reason", "retrieval",
                                                          "write", "cost", "cost_today", "cut", "operational"])


def _world_with_config(w):
    cfg = {"hermes_home": str(w.home), "db": w.mem("life").db_path, "read_scope": "life_all", "owners": {"telegram": ["me"]}, "mode": "tools_only"}
    (w.home / "yuanwei").mkdir(exist_ok=True)
    (w.home / "yuanwei" / "config.json").write_text(json.dumps(cfg))
    return cfg


def test_status_hermes_gives_the_whole_database_numbers_and_only_numbers(tmp_path):
    from test_hermes_workspace import World, turn as wturn
    from test_hermes_workspace import msgs as wmsgs
    from yuanwei import hermes_setup as HS
    w = World(tmp_path, scope="life_all", owners={"telegram": ["me"]})
    a = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    wturn(a, 1, "我自己说的", author_id="me")
    a.sync_turn("我自己说的", "答", session_id="s1", messages=wmsgs("我自己说的"))
    wturn(a, 2, "这一轮没配上", author_id="me")
    a.sync_turn("别的话", "答", session_id="s1", messages=wmsgs("别的话"))
    _world_with_config(w)
    st = HS.install_status(str(w.home))
    t = st["turn_accounting"]
    assert (t["started"], t["recorded"], t["unrecorded"], t["host_started"], t["left_out"]) == (2, 1, 1, 0, 0)
    assert t["started"] == t["recorded"] + t["unrecorded"] + t["host_started"]
    assert "按了停止" in t["note"] and "我自己说的" not in json.dumps(st, ensure_ascii=False)


def test_status_hermes_reports_an_unreadable_accounting_instead_of_saying_zero(tmp_path):
    import sqlite3

    from yuanwei import hermes_setup as HS
    h = tmp_path / "home"
    (h / "yuanwei").mkdir(parents=True)
    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE messages (ns TEXT)")
    c.commit()
    c.close()
    (h / "yuanwei" / "config.json").write_text(json.dumps({"hermes_home": str(h), "db": str(db), "mode": "tools_only"}))
    st = HS.install_status(str(h))
    assert st["turn_accounting"]["problem"] == "unreadable" and "started" not in st["turn_accounting"]
    assert any("对账读不了" in n for n in st["notes"])


# ================================================================== 补一 11、12、13
def test_status_started_equals_recorded_plus_unrecorded_plus_auto_in_the_plain_case(tmp_path):
    mem, a = mk(tmp_path)
    for n in (1, 2, 3):
        plain_turn(a, n)
    turn(a, 4, "问状态")
    s = status_turns(a)["session"]
    assert s == {"started": 3, "recorded": 3, "unrecorded": 0, "auto": 0}
    assert s["started"] == s["recorded"] + s["unrecorded"] + s["auto"]


def test_status_started_equals_the_sum_after_deleting_many_turns_and_chatting_on(tmp_path):
    mem, a = mk(tmp_path)
    for n in (1, 2, 3):
        plain_turn(a, n)
    turn(a, 4, "删掉这段")
    call(a, "window_delete")
    a.sync_turn("删掉这段", "请确认", session_id="hs1", messages=msgs("删掉这段"))
    turn(a, 5, "确认删除")
    a.sync_turn("确认删除", "删了", session_id="hs1", messages=msgs("确认删除"))
    plain_turn(a, 6, "删完以后接着聊")
    plain_turn(a, 7, "再聊一句")
    turn(a, 8, "问状态")
    s = status_turns(a)["session"]
    assert s == {"started": 2, "recorded": 2, "unrecorded": 0, "auto": 0}                      # 删掉的 5 轮和当前这一轮都不在「开始」里，数字对得上
    assert s["started"] == s["recorded"] + s["unrecorded"] + s["auto"]


def test_status_started_still_adds_up_with_a_lost_turn_and_a_host_started_turn(tmp_path):
    mem, a = mk(tmp_path)
    plain_turn(a, 1)
    turn(a, 2, "甲")
    a.sync_turn("别的话", "好", session_id="hs1", messages=msgs("别的话"))
    turn(a, 3, "后台任务完成的通知")
    m = msgs("后台任务完成的通知")
    m[-3] = dict(m[-3], display_kind="process_complete")
    a.sync_turn("后台任务完成的通知", "知道了", session_id="hs1", messages=m)
    turn(a, 4, "问状态")
    s = status_turns(a)["session"]
    assert s == {"started": 3, "recorded": 1, "unrecorded": 1, "auto": 1} and s["started"] == s["recorded"] + s["unrecorded"] + s["auto"]


def test_status_hermes_counts_only_the_hermes_host_and_says_what_it_cannot_tell(tmp_path):
    from test_hermes_workspace import World, turn as wturn
    from test_hermes_workspace import msgs as wmsgs
    from yuanwei import hermes_setup as HS
    w = World(tmp_path, scope="life_all", owners={"telegram": ["me"]})
    a = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    wturn(a, 1, "我自己说的", author_id="me")
    a.sync_turn("我自己说的", "答", session_id="s1", messages=wmsgs("我自己说的"))
    a.mem.store.conn.execute("INSERT INTO turns (ns, host, session_id, branch, turn_id, gen, state, origin, created_at) VALUES (?, 'codex', 'c1', '', 'cx-1', 1, 'open', 'user', '2027-01-01T00:00:00Z')", (a.mem.ns,))
    _world_with_config(w)
    t = HS.install_status(str(w.home))["turn_accounting"]
    assert (t["started"], t["recorded"], t["unrecorded"], t["host_started"]) == (1, 1, 0, 0)       # 同一个库里别的宿主的轮不算
    assert "升级到 0.1.0a9 以前宿主自己开的轮分不出来，也算在没记里" in t["note"] and "只数 hermes" in t["note"]
    assert "升级到 0.1.0a9 以前宿主自己开的轮分不出来，也算在没记里" in status_note(w)


def status_note(w):
    from yuanwei.tools import ToolSurface
    return ToolSurface.TURNS_NOTE


def test_a_host_started_turn_with_midturn_words_is_not_written_as_auto_so_it_counts_as_not_recorded(tmp_path):
    """后台任务把助手叫醒，用户在它回答时又发了话：这一轮丢了用户的话，不能报成「不算丢」。"""
    mem, a = mk(tmp_path)
    turn(a, 1, "后台任务完成的通知")
    h = Host("后台任务完成的通知").tool().steer("停，改成七点").final("好")
    h.m[len(OLD)] = dict(h.m[len(OLD)], display_kind="process_complete")
    a.sync_turn("后台任务完成的通知", "好", session_id="hs1", messages=h.m)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='not_recorded'").fetchone()[0] == 0
    turn(a, 2, "问状态")
    s = status_turns(a)["session"]
    assert (s["unrecorded"], s["auto"]) == (1, 0)
