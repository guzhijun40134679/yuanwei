"""清点第 56 条第一批·审后修补二（修补单二）：开关和回执（甲 1-7）、说话人和计数（乙 1-3）。
假宿主、假库故障（monkeypatch），临时目录，不联网，不碰真实的 ~/.hermes。乙 4 在 test_fix56_review4.py 里。"""
import json

import pytest

import fix56_fakehost as FH
import hermes_skill_contract as SC
from helpers2 import organized
from test_fix55_midturn import Host, skipped, stored_rows
from test_fix56_branchwin import closed_library, shown, start
from test_fix56_rowproof import host_rows, lost_opening_rows, OPENING
from test_fix56_rowproof import got as rp_got
from test_fix56_rowproof import run as rp_run
from test_fix56_skill import BODY, desktop, go, got, rows_for
from test_fix56_turnstart import broken_token, call, mk, turn
from test_fix56_turnstart_fix import SHOWN, fail_turn
from test_fix56_turntoken import sync
from yuanwei import hermes_midturn as HM
from yuanwei import hermes_turn as HT
from yuanwei import window as WN
from yuanwei.hermes_adapter import HermesAdapter

Q = {"query": "烤鱼"}


def after(a, n=None, text=None):
    """（可选地先开一轮，）把这一轮的预取便条和显示给用户的文字都取走，返回 (便条, 显示)。"""
    if text is not None:
        turn(a, n, text)
    out = a.prefetch("接着聊", session_id=a.session)
    return out, a.display_hook(response_text="回复") or ""


# ================================================================== 甲1 「没确认已关」的下一轮提示看标记
def test_a1_a_failed_close_is_not_repeated_after_a_new_chat(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    a.on_session_switch("hs-new", parent_session_id="hs1", reset=True)                    # /new：换到一个全新的对话
    out, text = after(a, 3, "新对话的第一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text            # 旧代码：新窗口开着、没有「关」，照说
    assert WN.CLOSE_FAILED_RECEIPT not in text and WN.CLOSE_FAILED_RECEIPT not in out
    assert call(a, "recall", Q)["status"] == "ok"


def test_a1_a_failed_close_is_not_repeated_after_switching_to_another_open_chat(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    a.win.bind_new("hermes", "hs-other")                                                    # 另一个开着的对话
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    a.on_session_switch("hs-other", parent_session_id="hs1")                                # 恢复／切到它
    out, text = after(a, 3, "那边的一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text
    assert call(a, "recall", Q)["status"] == "ok"


def test_a1_the_same_chat_says_it_once_with_the_next_plain_sentence(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    out, text = after(a, 3, "再问一句")
    assert out.count(a.CLOSE_FAILED_START) == 1 and text.count(a.CLOSE_FAILED_START) == 1
    out, text = after(a, 4, "又一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text            # 只说一次
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"                    # 读取仍停着


@pytest.mark.parametrize("word", ["#yuanwei-nomem", "#yuanwei-mem", "别用记忆"])
def test_a1_nothing_is_said_when_the_close_landed_or_an_explicit_open_landed(tmp_path, monkeypatch, word):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    out, text = after(a, 3, word)
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text
    assert ("hermes", "hs1", "") not in a.win._suspect


@pytest.mark.parametrize("name", ["CLOSE_FAILED_START", "CLOSE_FAILED_LATE", "CLOSE_FAILED_MIDTURN"])
def test_a1_all_three_sources_follow_the_mark(tmp_path, name):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    t = getattr(a, name)
    a._late.append(("close_failed", t))                                                      # 标记不在（关上了、明确开了、已经换到别的对话）：丢，不说
    out, text = after(a, 2, "又一句")
    assert t not in out and t not in text
    a._late.append(("close_failed", t))
    a.win._suspect.add(("hermes", "hs1", ""))                                                # 标记还在：说一次
    out, text = after(a, 3, "再一句")
    assert out.count(t) == 1 and text.count(t) == 1
    out, text = after(a, 4, "还有一句")
    assert t not in out and t not in text


# ================================================================== 甲2 没显示出去的旧回执要收回
@pytest.mark.parametrize("word", ["#yuanwei-nomem", "#yuanwei-mem"])
def test_a2_the_old_close_failed_receipt_is_taken_back_when_the_next_input_closes_or_opens(tmp_path, monkeypatch, word):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")                                            # 这一轮被停下，宿主没调显示钩子
    assert "〔原委〕" + WN.CLOSE_FAILED_RECEIPT in a._display
    out, text = after(a, 3, word)
    assert WN.CLOSE_FAILED_RECEIPT not in text and WN.CLOSE_FAILED_RECEIPT not in out       # 旧代码：回复后面还挂着旧回执


def test_a2_the_old_regeneration_receipt_is_taken_back_when_a_new_message_really_opens(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    turn(a, 2, "#yuanwei-mem\n查一下")
    call(a, "window_off")
    sync(a, "#yuanwei-mem\n查一下", "好")
    a._display.clear()
    turn(a, 2, "#yuanwei-mem\n查一下")                                                       # 重新生成：被拒，回执排下了
    assert ["〔原委〕" + WN.REISSUED_RECEIPT] == a._display and WN.REISSUED_RECEIPT in a._notices
    out, text = after(a, 3, "#yuanwei-mem")                                                  # 那一轮被停下；新发一条真开了
    assert call(a, "recall", Q)["status"] == "ok"
    assert WN.REISSUED_RECEIPT not in text and WN.REISSUED_RECEIPT not in out


def test_a2_the_old_voided_receipt_is_taken_back_too(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "别用记忆")
    sync(a, "别用记忆", "好")
    turn(a, 2, "确认开启")
    assert call(a, "window_on")["reason"] == "awaiting_confirmation"
    sync(a, "确认开启", "好")
    a._display.clear()
    turn(a, 2, "确认开启")
    assert ["〔原委〕" + WN.REISSUED_VOIDED_RECEIPT] == a._display
    out, text = after(a, 3, "#yuanwei-mem")
    assert WN.REISSUED_VOIDED_RECEIPT not in text and WN.REISSUED_VOIDED_RECEIPT not in out


def test_a2_other_texts_in_the_display_list_are_left_alone(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    a._display.append("〔原委〕" + WN.NOTICE_CLOSED)
    a._display.append("〔原委〕别的固定文字")
    turn(a, 2, "又一句")
    assert "〔原委〕" + WN.NOTICE_CLOSED in a._display and "〔原委〕别的固定文字" in a._display


# ================================================================== 甲3 window_off 遇到「没确认已关」要真去关
def _marked_after_a_failed_close(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    turn(a, 3, "再说一句")                                                                    # 下一轮开成功了，但「关」没落地：读取仍停着
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"
    return mem, a


def test_a3_window_off_in_a_later_turn_really_closes_and_clears_the_mark(tmp_path, monkeypatch):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)
    assert mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "on"
    r = call(a, "window_off")
    assert r["state"] == "off" and r["reason"] == "closed_by_tool"                          # 旧代码：回「待确认」、库里还是 on、标记不动
    assert mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "off"
    assert a.win._suspect == set() and WN.NOTICE_CLOSED in a._notices
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and r["reason"] != "window_close_unconfirmed"
    assert call(a, "window_state")["state"] == "off"


def test_a3_a_failed_write_of_that_close_does_not_say_off(tmp_path, monkeypatch):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("写不进去")
    monkeypatch.setattr(a.win, "close", boom)
    r = call(a, "window_off")
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT and r["reason"] == "window_close_unconfirmed"
    assert ("hermes", "hs1", "") in a.win._suspect
    assert mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "on"


def test_a3_a_close_that_did_not_turn_the_window_off_keeps_the_mark(tmp_path, monkeypatch):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)
    monkeypatch.setattr(a.win, "close", lambda *x, **k: WN.CloseResult("stale_ignored", "on", 1, False, "op"))
    r = call(a, "window_off")
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT
    assert ("hermes", "hs1", "") in a.win._suspect


@pytest.mark.parametrize("name", ["window_on", "window_delete", "window_state"])
def test_a3_the_other_control_tools_keep_state_and_reason_but_speak_the_close_failed_receipt(tmp_path, monkeypatch, name):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)
    r = call(a, name)
    assert r["state"] == "off" and r["reason"] == "window_close_unconfirmed" and r["message"] == WN.CLOSE_FAILED_RECEIPT
    assert ("hermes", "hs1", "") in a.win._suspect and mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "on"


def test_a3_other_refusal_reasons_still_speak_the_pending_hint(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    a.win.resolve = lambda *x, **k: WN.WindowRef(None, None, None, True, "window_identity_pending")   # 窗口身份待确认：不是「没确认已关」
    r = call(a, "window_off")
    assert r["reason"] == "window_identity_pending" and r["message"] == WN.PENDING_HINT


# ================================================================== 甲4 库打不开时的「关」也要记住
def remote_failing(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "m").mkdir(exist_ok=True)
    state = {"fail": True}

    def factory(key):
        if state["fail"]:
            raise OSError("库打不开")
        return organized(tmp_path / "m", host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"weixin": ["u1"]}}, memory_factory=factory, executor="inline")
    a.initialize("hs1", platform="weixin", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="c1")
    return a, state


def test_a4_a_close_in_the_first_sentence_with_an_unopenable_library_is_remembered(tmp_path):
    a, state = remote_failing(tmp_path)
    with pytest.raises(OSError):
        turn(a, 1, "#yuanwei-nomem", author_id="u1")
    assert a.win is None and ("hermes", "hs1", "") in a._suspect_wait
    assert a.display_hook(response_text="回复") == "回复\n\n" + SHOWN                          # 这一轮回复后面有回执
    state["fail"] = False
    turn(a, 2, "第二句普通话", author_id="u1")                                                # 库打开了：标记并进窗口服务
    assert a._suspect_wait == set() and ("hermes", "hs1", "") in a.win._suspect
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and r["reason"] == "window_close_unconfirmed"            # 旧代码：标记没记住，这里读得到
    out, text = after(a)
    assert out.count(a.CLOSE_FAILED_START) == 1 and text.count(a.CLOSE_FAILED_START) == 1
    turn(a, 3, "第三句", author_id="u1")
    out, text = after(a)
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text
    turn(a, 4, "#yuanwei-mem", author_id="u1")
    assert a.win._suspect == set() and call(a, "recall", Q)["status"] == "ok"


def test_a4_window_off_in_a_failed_turn_with_no_library_is_remembered_too(tmp_path):
    a, state = remote_failing(tmp_path)
    with pytest.raises(OSError):
        turn(a, 1, "你好", author_id="u1")
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT
    assert ("hermes", "hs1", "") in a._suspect_wait
    state["fail"] = False
    turn(a, 2, "再来", author_id="u1")
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


@pytest.mark.parametrize("reason", ["compression", "branch"])
def test_a4_the_waiting_mark_follows_a_compression_or_a_branch(tmp_path, reason):
    a, state = remote_failing(tmp_path)
    with pytest.raises(OSError):
        turn(a, 1, "#yuanwei-nomem", author_id="u1")
    a.on_session_switch("hs-new", parent_session_id="hs1", reason=reason)
    assert ("hermes", "hs-new", "") in a._suspect_wait
    state["fail"] = False
    turn(a, 2, "继续", author_id="u1")
    assert a._suspect_wait == set() and call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


def test_a4_a_failed_turn_without_a_close_leaves_nothing_waiting(tmp_path):
    a, state = remote_failing(tmp_path)
    with pytest.raises(OSError):
        turn(a, 1, "普通一句", author_id="u1")
    assert a._suspect_wait == set() and a._display == [] and a._late == []


# ================================================================== 甲5 第二种重发回执
def test_a5_the_two_reissue_receipts_are_different_texts():
    assert WN.REISSUED_RECEIPT != WN.REISSUED_VOIDED_RECEIPT
    assert "确认" in WN.REISSUED_VOIDED_RECEIPT and "作废" in WN.REISSUED_VOIDED_RECEIPT


def test_a5_window_layer_picks_the_receipt_by_what_was_voided_or_dropped(tmp_path):
    from test_window import feed, mk as wmk, ref, request as wrequest
    svc, st, clk = wmk(tmp_path)
    svc.bind_new("hermes", "S1")
    feed(svc, "#yuanwei-nomem")
    r = svc.process_input(ref(), "#yuanwei-mem", origin_verified=True, closes_only=True)           # 原文有 open 步、窗口仍关着
    assert r.receipt == WN.REISSUED_RECEIPT
    wrequest(svc)
    r = svc.process_input(ref(), "确认开启", origin_verified=True, closes_only=True)             # 原文是确认开启行：作废了等确认的申请
    assert r.receipt == WN.REISSUED_VOIDED_RECEIPT
    svc2, st2, clk2 = wmk(tmp_path, name="w2.db")
    svc2.bind_new("hermes", "S1")
    feed(svc2, "#yuanwei-nomem")
    wrequest(svc2)
    r = svc2.process_input(ref(), "请你想想办法", origin_verified=True, closes_only=True)
    assert r.receipt == WN.NOT_CONFIRMED_RECEIPT                                                  # 不是确认行：还是今天那句


# ================================================================== 甲6 失败轮里 window_off 的快捷那一支只在正在跑的就是失败的那一轮时走
def test_a6_someone_elses_turn_calling_window_off_does_not_mark_the_owners_window(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    a.initialize("sB", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="1")
    turn(a, 1, "主人的一句", author_id="u1")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem", author_id="u1")
    a.win._suspect.clear()                                                                       # 只看别人那一轮自己会不会加标记
    turn(a, 3, "别人的一句", author_id="u2")
    assert a._turn_failed == {"sB": True} and a._cur["sB"].startswith("ignored-")
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["reason"] == "turn_unproven" and a.win._suspect == set()                            # 旧代码：走快捷那一支，给主人的会话加了标记


def test_a6_the_failed_turn_itself_still_takes_the_shortcut(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "请把记忆关掉")
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT and ("hermes", "hs1", "") in a.win._suspect


# ================================================================== 甲7 第一条输入自己就把窗口打开了
def test_a7_the_history_closed_notice_is_taken_back_when_the_first_input_opens_the_window(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    assert WN.NOTICE_HISTORY_CLOSED in b._notices and "〔原委〕" + WN.NOTICE_HISTORY_CLOSED in b._display
    turn(b, 1, "#yuanwei-mem")
    assert b.win.view("hermes", "hs2").state == "on"
    assert WN.NOTICE_HISTORY_CLOSED not in b._notices and "〔原委〕" + WN.NOTICE_HISTORY_CLOSED not in b._display    # 旧代码：「这里先关着」照说，和已经打开打架


def test_a7_the_notice_stays_when_the_first_input_does_not_open(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "普通的一句")
    assert WN.NOTICE_HISTORY_CLOSED in shown(b)


# ================================================================== 乙1 两个放宽都不许放进宿主自己写的开头
@pytest.mark.parametrize("head", ["[Goal set] 写完这个", "[/loop wakeup #3] 继续", "[Heartbeat — recurring instruction: 检查一下", "[/plan — plan mode]\n计划…", "[/learn] The user wants…",
                                  "[/init] The user wants you to…", "Set up the 'x' automation for me", "[Continuing toward your standing goal]\n…",
                                  "[Continuing toward your standing goal — a quality gate failed]\n…", "[IMPORTANT: MCP servers have been reloaded.]\n\n你好", "  [前面有空白的方括号]"])
def test_b1_host_heads_are_not_recorded_on_either_relaxed_path(tmp_path, monkeypatch, head):
    assert HT.host_own_head(head) is True
    mem, a = rp_run(tmp_path / "todo", monkeypatch, host_rows(opening=head), opening=head)             # 待办快照行那一支
    assert rp_got(mem) == [] and skipped(a) == {"machinery_row": 1}, head
    mem, a = rp_run(tmp_path / "lost", monkeypatch, lost_opening_rows(), opening=head)                 # 开头行找不到那一支
    assert rp_got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}, head


def test_b1_an_ordinary_user_sentence_is_still_recorded_on_both_paths(tmp_path, monkeypatch):
    assert HT.host_own_head(OPENING) is False and HT.host_own_head("") is False
    mem, a = rp_run(tmp_path / "todo", monkeypatch, host_rows())
    assert [r for r, _c in rp_got(mem)] == ["user", "assistant"] and skipped(a) == {}
    mem, a = rp_run(tmp_path / "lost", monkeypatch, lost_opening_rows())
    assert [r for r, _c in rp_got(mem)] == ["user", "assistant"] and skipped(a) == {}


# ================================================================== 乙2 技能命令那一轮：开头判断看用户打的字
def _skill_rows(raw, todo=True):
    rows = list(rows_for(raw))[:-1]
    if todo:
        rows += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "工具输出", "tool_call_id": "c1"},
                 {"role": "user", "content": "[Your active task list was preserved across context compression]\n- [>] 写口号 (in_progress)", "_todo_snapshot_synthetic": True}]
    return rows + [{"role": "assistant", "content": "一句口号"}]


def test_b2_a_skill_command_with_a_todo_snapshot_row_records_the_typed_words_and_the_reply(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, "帮我写一句口号")
    go(a, monkeypatch, raw, "帮我写一句口号", messages=_skill_rows(raw))
    assert got(mem) == [("user", "帮我写一句口号"), ("assistant", "一句口号")]                      # 旧代码：整段脚手架以 [IMPORTANT: 开头，快照行被当成机器行，整轮不记
    assert skipped(a) == {}


def test_b2_typed_words_that_start_with_a_bracket_are_not_recorded_as_today(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    words = "[转发] 帮我写一句口号"
    raw = SC.single("demo", BODY, words)
    go(a, monkeypatch, raw, words, messages=_skill_rows(raw))
    assert got(mem) == [] and skipped(a) == {"machinery_row": 1}


def test_b2_a_skill_command_without_a_todo_row_is_recorded_as_before(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, "帮我写一句口号")
    go(a, monkeypatch, raw, "帮我写一句口号", messages=_skill_rows(raw, todo=False))
    assert got(mem) == [("user", "帮我写一句口号"), ("assistant", "一句口号")]


# ================================================================== 乙3 host_started 备注再要求没有改口配对
def _host_turn_with_redirect_pairing(tmp_path, monkeypatch):
    from test_fix55_accounting import acct
    from test_fix56_review3 import _not_recorded_rows
    from test_hermes_adapter import mk as amk
    text = "后台任务完成的通知"
    mem, a = amk(tmp_path)
    h = Host(text).final("好")
    FH.install(monkeypatch, FH.host_event(text))
    turn(a, 1, text)
    a.sync_turn(text + HM.REDIRECT_SEP + "改成七点（清洗改过的）", "好", session_id="hs1", messages=h.m)       # 用户中途说了话：改口接在后面，整串配不上，退到 T2
    turn(a, 2, "问状态")
    return mem, a, acct(a), _not_recorded_rows(mem)


def test_b3_a_host_turn_paired_through_a_redirect_is_not_counted_as_host_started(tmp_path, monkeypatch):
    mem, a, r, n_rows = _host_turn_with_redirect_pairing(tmp_path, monkeypatch)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}
    assert n_rows == 0 and (r["unrecorded"], r["host_started"]) == (1, 0)                          # 旧代码：记成「宿主自己开的」，用户中途说的话被藏掉
