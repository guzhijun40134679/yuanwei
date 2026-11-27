"""清点第 56 条第一批·修补单三（最后一轮小修）：压缩换号时提问卡和中途的「关」写失败的标记跟过去（1）、三种「下一轮再说」也能收回（2）、
关成了以后四句「没能确认」都拿掉（3）、连着两次开轮失败不重复（4）、两个放宽再收一点（5）、host_started 要求没有改口配对（6）。
假宿主、假库故障（monkeypatch），临时目录，不联网，不碰真实的 ~/.hermes。"""
import pytest

import fix56_fakehost as FH
import hermes_midturn_contract as MC
from test_fix53_clarify import clar_messages, mk as card_mk, one
from test_fix55_midturn import Host, _chat_adapter, env, skipped, stored_rows, turn
from test_fix56_fix2 import Q, _marked_after_a_failed_close, after
from test_fix56_rowproof import OPENING, host_rows, lost_opening_rows
from test_fix56_rowproof import got as rp_got
from test_fix56_rowproof import run as rp_run
from test_fix56_turnstart import call
from test_fix56_turnstart import mk as plain_mk
from test_fix56_turnstart_fix import SHOWN, fail_turn
from yuanwei import hermes_turn as HT
from yuanwei import window as WN

KEY = ("hermes", "hs1", "")
NEW = ("hermes", "hs-new", "")


def _boom(*_a, **_k):
    raise RuntimeError("写不进去")


def _card_turn(a, sid):
    res = one([{"question": "接下来？"}], {"q0": "以后别用记忆了"})
    a.sync_turn("开头", "好的", session_id=sid, messages=clar_messages("开头", [res], "好的"))


def _midturn_turn(a, sid):
    h = Host("开头").redirect("以后别用记忆了").final()
    a.sync_turn(h.user, h.reply, session_id=sid, messages=h.m)


SOURCES = [("card", _card_turn, "CLOSE_FAILED_LATE"), ("midturn", _midturn_turn, "CLOSE_FAILED_MIDTURN")]


# ================================================================== 1. 换了会话号以后写失败，标记要跟到现在这个号
@pytest.mark.parametrize("src, sync, name", SOURCES)
def test_1_a_close_that_fails_after_a_compression_switch_marks_the_new_session_too(tmp_path, monkeypatch, src, sync, name):
    mem, a = card_mk(tmp_path)
    turn(a, 1, "开头")
    a.on_session_switch("hs-new", parent_session_id="hs1", reason="compression")                    # 一轮中间压缩换了号：ref 还是旧号
    monkeypatch.setattr(a.win, "close", _boom)
    sync(a, "hs-new")
    assert KEY in a.win._suspect and NEW in a.win._suspect                                          # 旧代码：只标旧号
    monkeypatch.undo()
    turn(a, 2, "继续聊")                                                                            # 新号上的下一条普通话
    out, text = after(a)
    assert out.count(getattr(a, name)) == 1 and text.count(getattr(a, name)) == 1                  # 说一次
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"                            # 读取也停着
    out, text = after(a, 3, "又一句")
    assert getattr(a, name) not in out and getattr(a, name) not in text


@pytest.mark.parametrize("how", ["reset", "resume"])
@pytest.mark.parametrize("src, sync, name", SOURCES)
def test_1_a_close_that_fails_after_a_switch_to_an_unrelated_chat_marks_nothing_there(tmp_path, monkeypatch, src, sync, name, how):
    mem, a = card_mk(tmp_path)
    turn(a, 1, "开头")
    if how == "reset":
        a.on_session_switch("hs-new", parent_session_id="hs1", reset=True)                          # /new：新窗口
    else:
        a.win.bind_new("hermes", "hs-new")                                                          # 另一个已有的对话，切过去
        a.on_session_switch("hs-new", parent_session_id="hs1")
    monkeypatch.setattr(a.win, "close", _boom)
    sync(a, "hs-new")
    assert KEY in a.win._suspect and NEW not in a.win._suspect                                      # 原来的窗口照旧标；那边不标
    monkeypatch.undo()
    turn(a, 2, "那边的一句")
    out, text = after(a)
    assert getattr(a, name) not in out and getattr(a, name) not in text
    assert call(a, "recall", Q)["status"] == "ok"


def test_1_a_lookup_that_cannot_tell_the_window_marks_the_new_session_too(tmp_path, monkeypatch):
    mem, a = card_mk(tmp_path)
    turn(a, 1, "开头")
    a.on_session_switch("hs-new", parent_session_id="hs1", reset=True)
    monkeypatch.setattr(a.win, "close", _boom)
    real = a.win.resolve

    def resolve(host, sid, branch="", **kw):
        if kw.get("_raw") and sid == "hs-new":
            raise RuntimeError("读不出来")
        return real(host, sid, branch, **kw)
    monkeypatch.setattr(a.win, "resolve", resolve)
    _card_turn(a, "hs-new")
    assert NEW in a.win._suspect                                                                    # 读不出是不是同一个窗口：宁可多停一下读取


def test_1_a_session_that_did_not_change_still_marks_only_the_key(tmp_path, monkeypatch):
    mem, a = card_mk(tmp_path)
    turn(a, 1, "开头")
    monkeypatch.setattr(a.win, "close", _boom)
    _card_turn(a, "hs1")
    assert a.win._suspect == {KEY}


# ================================================================== 2. 三种「下一轮再说」的文字也能收回
def _say_into_notices_and_display(tmp_path, monkeypatch, heard=False):
    """失败的一轮（关）→ 下一条普通话开轮：「下一轮再说」那句进了 _notices 和 _display；这一轮被停下，宿主没调显示钩子。
    heard 假：宿主也没调预取（助手没听到，句子还在 _notices 里）；heard 真：预取调过了（助手听到了，只剩 _display 里那份）。"""
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    turn(a, 3, "普通话")
    assert a.CLOSE_FAILED_START in a._notices and "〔原委〕" + a.CLOSE_FAILED_START in a._display
    if heard:
        assert a.CLOSE_FAILED_START in a.prefetch("接着聊", session_id="hs1") and a.CLOSE_FAILED_START not in a._notices
    return mem, a


@pytest.mark.parametrize("heard", [False, True])
def test_2_a_stopped_plain_turn_then_a_new_chat_does_not_carry_the_sentence_over(tmp_path, monkeypatch, heard):
    mem, a = _say_into_notices_and_display(tmp_path, monkeypatch, heard)
    a.on_session_switch("hs-new", parent_session_id="hs1", reset=True)                              # /new
    out, text = after(a, 4, "新对话的第一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text                    # 旧代码：_notices 里的那句直接进了新对话的预取
    assert WN.CLOSE_FAILED_RECEIPT not in out and WN.CLOSE_FAILED_RECEIPT not in text
    assert call(a, "recall", Q)["status"] == "ok"


@pytest.mark.parametrize("heard", [False, True])
def test_2_a_stopped_plain_turn_then_the_open_command_does_not_carry_the_sentence_over(tmp_path, monkeypatch, heard):
    mem, a = _say_into_notices_and_display(tmp_path, monkeypatch, heard)
    out, text = after(a, 4, "#yuanwei-mem")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text
    assert call(a, "recall", Q)["status"] == "ok"


def test_2_a_turn_where_the_host_skipped_the_prefetch_is_said_again_once_by_the_next_plain_turn(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    turn(a, 3, "普通话")
    a.display_hook(response_text="回复")                                                           # 回复显示了，但宿主没调预取：助手还没听到
    assert a.CLOSE_FAILED_START in a._notices
    out, text = after(a, 4, "又一句普通话")
    assert out.count(a.CLOSE_FAILED_START) == 1 and a.CLOSE_FAILED_START not in text                # 助手这时才听到；用户上一轮已经看到，不再显示
    assert not [x for x in a._late if x[0] == "close_failed"]
    out, text = after(a, 5, "再一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text


def test_2_heard_by_the_assistant_but_not_shown_is_shown_once_by_the_next_reply_and_not_heard_again(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    turn(a, 3, "普通话")
    out = a.prefetch("接着聊", session_id="hs1")                                                    # 助手听到了；回复被停下，没显示出来
    assert out.count(a.CLOSE_FAILED_START) == 1 and "〔原委〕" + a.CLOSE_FAILED_START in a._display
    out, text = after(a, 4, "又一句普通话")
    assert a.CLOSE_FAILED_START not in out and text.count(a.CLOSE_FAILED_START) == 1               # 用户还没看到：补显示一次；助手听到过了：不再说
    out, text = after(a, 5, "再一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text


def test_2_several_short_replies_in_a_row_show_it_once_and_the_assistant_hears_it_once(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    a.display_hook(response_text="回复")
    shown = []
    for n, words in enumerate(("ok", "继续", "go ahead"), start=3):                                 # 宿主对这种短句跳过 prefetch，显示钩子照调
        turn(a, n, words)
        shown.append(a.CLOSE_FAILED_START in (a.display_hook(response_text="回复") or ""))
    assert shown == [True, False, False]
    out, text = after(a, 6, "说一句完整的话")
    assert out.count(a.CLOSE_FAILED_START) == 1 and a.CLOSE_FAILED_START not in text                # 助手这时才听到一次；用户早看到了，不再显示


def test_2_the_same_turn_receipt_left_in_the_note_is_not_handed_over_after_an_explicit_open(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    monkeypatch.setattr(a.win, "_change", _boom)
    turn(a, 2, "#yuanwei-nomem")                                                                    # 口令写失败：回执进了 _notices；这一轮 prefetch 没拿走
    monkeypatch.undo()
    assert WN.CLOSE_FAILED_RECEIPT in a._notices and KEY in a.win._suspect
    out, text = after(a, 3, "#yuanwei-mem")
    assert WN.CLOSE_FAILED_RECEIPT not in out and a.CLOSE_FAILED_START not in out and call(a, "recall", Q)["status"] == "ok"


def test_2_the_same_turn_receipt_left_in_the_note_is_said_in_past_tense_while_still_unconfirmed(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    monkeypatch.setattr(a.win, "_change", _boom)
    turn(a, 2, "#yuanwei-nomem")
    monkeypatch.undo()
    out, text = after(a, 3, "普通话")
    assert out.count(a.CLOSE_FAILED_START) == 1 and WN.CLOSE_FAILED_RECEIPT not in a._notices       # 便条里换成过去时的那句（读取被拒的回执本身带着当轮那句，不算）
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


@pytest.mark.parametrize("name", ["CLOSE_FAILED_LATE", "CLOSE_FAILED_MIDTURN"])
def test_2_the_other_two_sentences_are_taken_back_the_same_way(tmp_path, name):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    t = getattr(a, name)
    a._late.append(("close_failed", t))
    a.win._suspect.add(KEY)
    turn(a, 2, "普通话")
    assert t in a._notices and "〔原委〕" + t in a._display
    turn(a, 3, "#yuanwei-mem")                                                                      # 明确开了
    assert t not in a._notices and "〔原委〕" + t not in a._display and ("close_failed", t) not in a._late
    out, text = after(a)
    assert t not in out and t not in text


# ================================================================== 3. 关成了以后四句都拿掉
def test_3_a_successful_close_by_the_tool_takes_all_four_sentences_back(tmp_path, monkeypatch):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)
    texts = a._close_failed_texts()
    assert len(set(texts)) == 4
    for t in texts:
        a._display.append("〔原委〕" + t)
        a._notices.append(t)
    a._late.append(("close_failed", a.CLOSE_FAILED_START))
    a._late.append(("open", "别的提示"))
    r = call(a, "window_off")
    assert r["state"] == "off" and r["reason"] == "closed_by_tool"
    for t in texts:
        assert t not in a._notices and "〔原委〕" + t not in a._display, t                         # 旧代码：回复里一边说关好了一边说没能确认
    assert a._late == [("open", "别的提示")] and WN.NOTICE_CLOSED in a._notices
    out, text = after(a)
    assert not any(t in out or t in text for t in texts)


def test_3_a_close_that_fails_again_leaves_the_sentences_alone(tmp_path, monkeypatch):
    mem, a = _marked_after_a_failed_close(tmp_path, monkeypatch)
    a._display.append(SHOWN)
    a._late.append(("close_failed", a.CLOSE_FAILED_START))
    monkeypatch.setattr(a.win, "close", _boom)
    assert call(a, "window_off")["reason"] == "window_close_unconfirmed"
    assert SHOWN in a._display and ("close_failed", a.CLOSE_FAILED_START) in a._late


# ================================================================== 4. 连着两次开轮失败
def test_4_two_failed_turns_in_a_row_show_the_receipt_once_and_say_it_once_next_turn(tmp_path, monkeypatch):
    mem, a = plain_mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    fail_turn(a, monkeypatch, 3, "#yuanwei-nomem")
    assert a._display.count(SHOWN) == 1 and a._late.count(("close_failed", a.CLOSE_FAILED_START)) == 1     # 旧代码：各两条
    assert a.display_hook(response_text="回复") == "回复\n\n" + SHOWN
    out, text = after(a, 4, "再问一句")
    assert out.count(a.CLOSE_FAILED_START) == 1 and text.count(a.CLOSE_FAILED_START) == 1
    out, text = after(a, 5, "又一句")
    assert a.CLOSE_FAILED_START not in out and a.CLOSE_FAILED_START not in text


# ================================================================== 5. 两个放宽再收一点
def _chat_run(tmp_path, monkeypatch, raw, messages, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem, a = _chat_adapter(tmp_path, **kw)
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, raw, author_id="u1")
    a.sync_turn(raw, "事有原委，话有出处", session_id="hs1", messages=messages)
    return mem, a


def _envelope_cases():
    two = env(MC.merge_redirects(env("甲"), env("乙")))
    return [("merged", two, {}), ("whatsapp", env(OPENING, platform="whatsapp"), {"platform": "whatsapp", "chat_type": "dm"}),
            ("bot", env(OPENING, from_bot=True), {}), ("inner_host_head", env("[Goal set] 写完这个"), {})]


@pytest.mark.parametrize("name, raw, kw", _envelope_cases(), ids=[c[0] for c in _envelope_cases()])
def test_5_envelopes_that_cannot_be_read_are_not_recorded_on_either_relaxed_path(tmp_path, monkeypatch, name, raw, kw):
    assert HT.host_own_head(raw) is False                                                           # 光看开头认不出，要靠适配器那一道
    mem, a = _chat_run(tmp_path / "todo", monkeypatch, raw, host_rows(opening=raw), **kw)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}, name                      # 待办快照行那一支：旧代码照记
    mem, a = _chat_run(tmp_path / "lost", monkeypatch, raw, lost_opening_rows(), **kw)
    assert stored_rows(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}, name


def test_5_a_one_to_one_weixin_envelope_that_can_be_read_is_recorded_without_the_note(tmp_path, monkeypatch):
    raw = env(OPENING)
    mem, a = _chat_run(tmp_path / "todo", monkeypatch, raw, host_rows(opening=raw))
    assert stored_rows(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")] and skipped(a) == {}
    mem, a = _chat_run(tmp_path / "lost", monkeypatch, raw, lost_opening_rows())
    assert stored_rows(mem) == [("user", OPENING), ("assistant", "事有原委，话有出处")] and skipped(a) == {}


def test_5_the_host_opening_function_on_its_own(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    assert a._host_opening("[Goal set] x") and a._host_opening("Set up the 'x' automation") and a._host_opening("You have been invited to join a meeting: 周会")
    assert not a._host_opening(OPENING) and not a._host_opening("")
    assert not a._host_opening(env(OPENING))                                                        # 解得开、去掉以后是用户的话
    assert a._host_opening(env("[Goal set] x")) and a._host_opening(env(OPENING, from_bot=True))
    assert a._host_opening(env(MC.merge_redirects(env("甲"), env("乙"))))
    assert a._host_opening(env("   "))                                                              # 去掉以后是空的：认不得
    assert a._host_opening(5) is True                                                               # 不是字符串：当成宿主的


INVITE = "You have been invited to join a meeting: 周会\n\nMeeting Number: 123\nTopic: 周会\nInviter: 小王\nHost: 小李\n\nYou may use lark-cli and the relevant Lark/Feishu meeting skills to join the meeting."


def test_5_a_feishu_meeting_invite_is_a_host_head_and_is_not_recorded_on_either_path(tmp_path, monkeypatch):
    assert HT.host_own_head(INVITE) is True and HT.host_own_head("  " + INVITE) is True
    assert HT.host_own_head("You have been invited to a party") is False                          # 只认整句固定开头
    mem, a = rp_run(tmp_path / "todo", monkeypatch, host_rows(opening=INVITE), opening=INVITE)
    assert rp_got(mem) == [] and skipped(a) == {"machinery_row": 1}
    mem, a = rp_run(tmp_path / "lost", monkeypatch, lost_opening_rows(), opening=INVITE)
    assert rp_got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


@pytest.mark.parametrize("words", ["reaction:added:THUMBSUP", "reaction:removed:👍"])
def test_5_an_emoji_reaction_is_a_host_head_and_is_not_recorded_on_either_path(tmp_path, monkeypatch, words):
    assert HT.host_own_head(words) is True and HT.host_own_head("reaction time was slow") is False
    mem, a = rp_run(tmp_path / "todo", monkeypatch, host_rows(opening=words), opening=words)
    assert rp_got(mem) == [] and skipped(a) == {"machinery_row": 1}
    mem, a = rp_run(tmp_path / "lost", monkeypatch, lost_opening_rows(), opening=words)
    assert rp_got(mem) == [] and skipped(a) == {"current_user_row_not_found": 1}


# ================================================================== 6. host_started 也要没有改口配对（origin 是 None 的那一条路）
def test_6_a_machine_row_paired_through_a_redirect_with_unknown_origin_is_not_counted_as_host_started(tmp_path, monkeypatch):
    from test_fix55_accounting import acct
    from test_fix56_review3 import _not_recorded_rows
    from test_hermes_adapter import mk as amk
    from yuanwei import hermes_midturn as HM
    text = "后台任务完成的通知"
    mem, a = amk(tmp_path)
    h = Host(text).final("好")
    h.m[2]["display_kind"] = "process_complete"                                                    # 开头行本身是机器行
    FH.install(monkeypatch, None)                                                                  # 没有当前事件：origin 是 None
    turn(a, 1, text)
    a.sync_turn(text + HM.REDIRECT_SEP + "改成七点（清洗改过的）", "好", session_id="hs1", messages=h.m)
    turn(a, 2, "问状态")
    r = acct(a)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}
    assert _not_recorded_rows(mem) == 0 and (r["unrecorded"], r["host_started"]) == (1, 0)         # 旧代码：记成「宿主自己开的」


def test_6_the_same_machine_row_without_a_redirect_pairing_is_still_host_started(tmp_path, monkeypatch):
    from test_fix56_review3 import _not_recorded_rows
    from test_hermes_adapter import mk as amk
    text = "后台任务完成的通知"
    mem, a = amk(tmp_path)
    h = Host(text).final("好")
    h.m[2]["display_kind"] = "process_complete"
    FH.install(monkeypatch, None)
    turn(a, 1, text)
    a.sync_turn(text, "好", session_id="hs1", messages=h.m)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1} and _not_recorded_rows(mem) == 1
