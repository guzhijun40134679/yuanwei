"""清点第 56 条第一批·审后修补一：重新生成／重发只认「关」。Access.event_turn 返回 (令牌, reissued)，窗口层 process_input(closes_only=True) 只留关、待确认用空文字判断，
接头把它接上；确认删除那一轮的键被删除改名以后不再造新号。假宿主，临时目录，不联网。"""
import pytest

from test_deletion_chat import n_deletions, request, setup_chat, talk
from test_fix56_turntoken import rows, sync
from test_hermes_adapter import call, mk, msgs, turn
from test_window import feed, ident, mk as wmk, ref, request as wrequest, state
from helpers2 import organized
from yuanwei import window as W
from yuanwei.access import Access

Q = {"query": "烤鱼"}


# ================================================================== Access.event_turn 的 reissued
def test_event_turn_reports_reissued_only_on_the_continuation_keys(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1, r1 = acc.event_turn("hermes", "S", "1:abc")
    assert r1 is False
    assert acc.event_turn("hermes", "S", "1:abc") == (t1, False)                              # 重送第一个令牌：还是 False
    mem.record("hermes", "S", t1 + "-u", "user", "话", time="2027-02-01", turn_id=t1)
    t2, r2 = acc.event_turn("hermes", "S", "1:abc")
    assert t2 != t1 and r2 is True                                                          # 续号键上的新令牌
    assert acc.event_turn("hermes", "S", "1:abc") == (t2, True)                               # 重送同一个续号令牌也是 True
    assert Access(mem).event_turn("hermes", "S", "1:abc") == (t2, True)                       # 进程重启
    assert acc.turn_token("hermes", "S", "1:abc") == t1                                     # turn_token 不动


def test_event_turn_deleted_keys_hole_the_delete_confirm_token_on_a_continuation_key_is_found(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1, _ = acc.event_turn("hermes", "S", "4:abc")
    mem.record("hermes", "S", t1 + "-u", "user", "话", time="2027-02-01", turn_id=t1)
    t2, r2 = acc.event_turn("hermes", "S", "4:abc")
    assert r2 is True
    mem.store.conn.execute("INSERT INTO no_record_turns (turn_id, host, window_id, reason, del_id, capture_seq) VALUES (?,?,?,?,?,?)", (t2, "hermes", None, "delete_confirm", "d-1", 1))
    mem.store.conn.execute("UPDATE turn_tokens SET event_key='x123' WHERE event_key='4:abc'")        # 删除把别的键改成假名，只留确认那一轮的键 K\x1f2，于是 K 空着
    before = mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens").fetchone()[0]
    assert acc.event_turn("hermes", "S", "4:abc") == (t2, True)                               # 旧代码：走查停在 n=1，又发一个新号
    assert mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens").fetchone()[0] == before
    assert acc.event_turn("hermes", "OTHER", "4:abc")[0] != t2                                # 别的会话不受影响


# ================================================================== 窗口层：closes_only
def test_window_closes_only_never_opens_the_command_open_is_dropped_and_said(tmp_path):
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    feed(svc, "#yuanwei-nomem")
    assert state(svc) == "off"
    r = svc.process_input(ref(), "#yuanwei-mem\n查一下", origin_verified=True, closes_only=True)
    assert state(svc) == "off" and r.steps == () and r.receipt == W.REISSUED_RECEIPT
    r = feed(svc, "#yuanwei-mem\n查一下")                                                    # 默认：一个字节都不变，照常打开
    assert state(svc) == "on" and r.steps == (("open", "command", "applied"),) and r.receipt is None


def test_window_closes_only_on_an_open_window_says_nothing_and_changes_nothing(tmp_path):
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    r = svc.process_input(ref(), "#yuanwei-mem", origin_verified=True, closes_only=True)
    assert state(svc) == "on" and r.steps == () and r.receipt is None


@pytest.mark.parametrize("text", ["#yuanwei-nomem", "#yuanwei-nomem\n查一下", "别用记忆", "这次不要参考我的记忆", "#yuanwei-mem\n别用记忆"])
def test_window_closes_only_still_closes_for_the_command_and_for_plain_ban_words(tmp_path, text):
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    r = svc.process_input(ref(), text, origin_verified=True, closes_only=True)
    assert state(svc) == "off" and any(k == "close" and o == "applied" for k, _s, o in r.steps) and not any(k == "open" for k, _s, _o in r.steps)


def test_window_closes_only_voids_a_pending_open_and_never_confirms_it(tmp_path):
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    feed(svc, "#yuanwei-nomem")
    assert wrequest(svc).status == "waiting"
    r = svc.process_input(ref(), "确认开启", origin_verified=True, closes_only=True)
    assert state(svc) == "off" and r.command != "confirm" and r.receipt == W.REISSUED_VOIDED_RECEIPT and r.reason == "pending_voided"
    r = feed(svc, "确认开启")                                                                # 待确认已作废：之后单独说确认也打不开
    assert state(svc) == "off"


def test_window_closes_only_a_plain_regeneration_voiding_a_pending_gets_the_old_receipt(tmp_path):
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    feed(svc, "#yuanwei-nomem")
    wrequest(svc)
    r = svc.process_input(ref(), "请你想想办法", origin_verified=True, closes_only=True)
    assert state(svc) == "off" and r.receipt == W.NOT_CONFIRMED_RECEIPT                    # 不是确认行：只说「别的情形」下今天说的那句，不说重新生成


def test_window_closes_only_in_an_unbound_session_binds_only_for_a_close(tmp_path):
    svc, st, clk = wmk(tmp_path)
    r = svc.process_input(ref("NEW1"), "#yuanwei-mem", origin_verified=True, closes_only=True)
    assert r.outcome == "paused_identity" and svc.resolve("hermes", "NEW1", "").paused
    r = svc.process_input(ref("NEW2"), "#yuanwei-nomem", origin_verified=True, closes_only=True)
    assert r.outcome == "closed" and state(svc, "NEW2") == "off"


# ================================================================== 接头：重新生成
def test_regenerated_delete_confirm_does_not_delete_and_a_fresh_request_then_deletes(tmp_path):
    mem, a = setup_chat(tmp_path)
    turn(a, 3, "确认删除")                                                                   # 这一轮里没有待确认：当普通话记下
    assert call(a, "window_delete")["state"] == "waiting"                                   # 助手在这一轮里登记了删除申请
    sync(a, "确认删除", "我来申请")
    assert n_deletions(mem) == 0
    a._display.clear()
    n_before = len(rows(mem))
    turn(a, 3, "确认删除")                                                                   # 重新生成：同一个轮次号、同一句话
    assert n_deletions(mem) == 0                                                            # 旧代码：这一遍把申请当作确认，整段对话被删
    assert W.REISSUED_VOIDED_RECEIPT in a._notices and ["〔原委〕" + W.REISSUED_VOIDED_RECEIPT] == a._display          # 原文是整条「确认删除」、作废了等确认的申请：用第二种回执
    assert a.display_hook(response_text="回复") == "回复\n\n〔原委〕" + W.REISSUED_VOIDED_RECEIPT
    sync(a, "确认删除", "第二版")
    assert len(rows(mem)) == n_before + 2
    # 申请已作废：新一轮单独说确认也不删
    talk(a, 4, "好了")
    turn(a, 5, "确认删除")
    assert n_deletions(mem) == 0
    sync(a, "确认删除", "好")
    # 用户下一轮（新的轮次号）重新申请再确认：删掉
    talk(a, 6, "请把这段对话在原委里的记忆删掉")
    assert call(a, "window_delete")["state"] == "waiting"
    turn(a, 7, "确认删除")
    assert n_deletions(mem) == 1


def test_regenerated_mem_command_does_not_reopen_a_window_closed_in_the_same_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    turn(a, 2, "#yuanwei-mem\n查一下")
    assert call(a, "recall", Q)["status"] == "ok"                                           # 开过
    assert call(a, "window_off")["state"] == "off"                                          # 同一轮里关了（插话关）
    sync(a, "#yuanwei-mem\n查一下", "好")
    a._display.clear()
    turn(a, 2, "#yuanwei-mem\n查一下")                                                       # 重新生成：同一键
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and r["reason"] == "window_closed"                      # 窗口仍关；本轮口令 mem 被窗口那一关挡住（begin_turn 不会打开）
    assert a.win.view("hermes", "hs1").state == "off"
    assert ["〔原委〕" + W.REISSUED_RECEIPT] == a._display


def test_regenerated_confirm_open_does_not_reopen_a_window_closed_in_the_same_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    turn(a, 2, "确认开启")                                                                   # 这一轮里没有待确认；助手在这一轮里申请开启
    assert call(a, "window_on")["reason"] == "awaiting_confirmation"
    sync(a, "确认开启", "好")
    a._display.clear()
    turn(a, 2, "确认开启")                                                                   # 重新生成：旧代码会把这个申请当作它的确认而打开
    assert a.win.view("hermes", "hs1").state == "off"
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and r["reason"] == "window_closed"
    assert ["〔原委〕" + W.REISSUED_VOIDED_RECEIPT] == a._display                            # 原文是「确认开启」行：用第二种回执
    turn(a, 3, "确认开启")                                                                   # 申请已作废：新一轮单独说也打不开
    assert a.win.view("hermes", "hs1").state == "off"


def test_regenerated_confirm_open_after_a_real_open_and_a_later_close_stays_closed(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    assert call(a, "window_on")["reason"] == "awaiting_confirmation"
    turn(a, 2, "确认开启")
    assert call(a, "recall", Q)["status"] == "ok"
    call(a, "window_off")
    sync(a, "确认开启", "好")
    turn(a, 2, "确认开启")
    assert a.win.view("hermes", "hs1").state == "off"
    assert call(a, "recall", Q)["reason"] == "window_closed"


@pytest.mark.parametrize("text", ["#yuanwei-nomem", "别用记忆", "#yuanwei-nomem\n帮我查一下"])
def test_regenerated_close_still_closes(tmp_path, text):
    mem, a = mk(tmp_path)
    turn(a, 2, text)
    sync(a, text, "好")
    turn(a, 3, "#yuanwei-mem")                                                              # 用户后来又打开了
    sync(a, "#yuanwei-mem", "好")
    assert call(a, "recall", Q)["status"] == "ok"
    turn(a, 2, text)                                                                        # 重新生成第 2 轮：关照样关
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and a.win.view("hermes", "hs1").state == "off"


def test_ordinary_regeneration_still_keeps_both_pairs_and_says_nothing(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "给我讲个笑话")
    sync(a, "给我讲个笑话", "第一版")
    turn(a, 1, "给我讲个笑话")
    sync(a, "给我讲个笑话", "第二版")
    assert [(r[2], r[3]) for r in rows(mem)] == [("user", "给我讲个笑话"), ("assistant", "第一版"), ("user", "给我讲个笑话"), ("assistant", "第二版")]
    assert a._display == [] and W.REISSUED_RECEIPT not in a._notices
    assert call(a, "recall", Q)["status"] == "ok"


def test_resent_reissued_token_does_not_process_the_window_input_twice(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    turn(a, 1, "别用记忆")                                                                  # 重新生成
    n = mem.store.conn.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0]
    turn(a, 1, "别用记忆")                                                                  # 同一个续号令牌的钩子再来一次（登记还在等）
    assert mem.store.conn.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == n
    assert a._cur["hs1"] == a._pending["hs1"][-1]["token"]


def test_delete_confirm_token_on_a_continuation_key_is_returned_again_not_recorded_not_deleted(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    from yuanwei.hermes_adapter import HOST
    fp = Access.text_fp("确认删除")
    key = "4:%s" % fp[:12]
    t2 = "t-confirmturn"
    mem.store.conn.execute("INSERT INTO turn_tokens (ns, host, session_id, branch, event_key, turn_id, created_at) VALUES (?,?,?,?,?,?,?)", (mem.ns, HOST, "hs1", "", key + "\x1f2", t2, "2027-02-01T00:00:00Z"))
    mem.store.conn.execute("INSERT INTO no_record_turns (turn_id, host, window_id, reason, del_id, capture_seq) VALUES (?,?,?,?,?,?)", (t2, HOST, None, "delete_confirm", "d-0", 1))
    n_tok = mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens").fetchone()[0]
    turn(a, 4, "确认删除")                                                                   # K 空着、K\x1f2 是确认那一轮：同一键再来还它，不另发新号
    assert a._cur["hs1"] == t2
    assert mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens").fetchone()[0] == n_tok
    assert n_deletions(mem) == 0                                                            # 这是重新生成（续号键）：只认关，不确认删除
    a.sync_turn("确认删除", "好", session_id="hs1", messages=msgs("确认删除"))
    assert a.skipped.get("blocked_no_record_turn") == 1 and "确认删除" not in [r[3] for r in rows(mem)]
