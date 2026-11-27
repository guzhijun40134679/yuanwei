"""清单第 55 条（0.1.0a9）：助手没答完时用户又发一句，整轮不再丢；这些话折进开头那一条，每个物理行带标签。

行的形状用 Hermes v0.23.2 自己的源码片段造（逐字冻结在 hermes_midturn_contract.py，exec 跑，不 import 宿主）：改口行是宿主的 _apply_active_turn_redirect 加的，
拼进开头那句的串是 begin_iteration 那几行拼的，插话行是 steer_user_row 造的，聊天平台的来源说明是 _steer_text_with_origin 的最后几行加的。全部自造数据，没有网络，不碰 ~/.hermes。
"""
import json
import os
import subprocess

import pytest

import hermes_midturn_contract as C
from hermes_clarify_contract import make_result
from test_hermes_adapter import call, mk, turn
from yuanwei import asked as AS
from yuanwei import hermes_midturn as HM
from yuanwei.hermes_adapter import HermesAdapter

REAL = os.path.expanduser("~/.hermes/hermes-agent")                              # 导入时取（测试隔离会把 HOME 改掉）；只读源码，不 import
SAID, UNV = AS.TAG_SAID, AS.TAG_SAID_UNVERIFIED
OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]


class Host:
    """一轮的宿主那一侧：messages 和交给记忆的那句话（original_user_message），事件按发生的先后一件件加。"""

    def __init__(self, opening, pre=None):
        self.m = list(OLD if pre is None else pre) + [{"role": "user", "content": opening}]
        self.opening, self.user, self.n = opening, opening, 0

    def tool(self, result="工具输出：这不该进记忆"):
        self.n += 1
        cid = "call_%d" % self.n
        self.m += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
                   {"role": "tool", "content": result, "tool_call_id": cid, "name": "terminal"}]
        return self

    def redirect(self, text, streamed=""):
        """宿主 begin_iteration 的真实几行：加（占位或半截）助手行和改口行，原句后面接「User correction during the turn: …」。"""
        if streamed:
            self.m.append({"role": "assistant", "content": streamed})                      # 宿主在尾巴是用户行时会先把半截当助手行加上；这里让半截先进来，源码片段看到的尾巴就是助手行
        self.user = C.begin_iteration(self.m, self.user, text)
        return self

    def steer(self, *texts):
        self.m.append(C.steer_row(C.steer_join(list(texts))))
        return self

    def final(self, text="答完了"):
        self.m.append({"role": "assistant", "content": text})
        self.reply = text
        return self

    def sync(self, a, n=1, session="hs1", **kw):
        turn(a, n, self.opening)
        a.sync_turn(self.user, self.reply, session_id=session, messages=self.m, **kw)


def stored_rows(mem, session="hs1"):
    return [(r["role"], r["content"]) for r in mem.store.list_messages(mem.ns) if r["session_id"] == session]


def user_rows(mem):
    return [c for r, c in stored_rows(mem) if r == "user"]


def counts(a):
    from yuanwei import askcounts as AC
    return AC.read(a._home)


def skipped(a):
    return a.status()["skipped"]


# ================================================================== 冻结的源码片段本身
def test_frozen_fingerprints_are_unchanged_and_match_the_real_host_source_when_it_is_there():
    assert C.check_fingerprints() == []
    real = REAL
    if not os.path.isdir(real) or subprocess.run(["git", "-C", real, "cat-file", "-e", "ae2a81ccc0"], capture_output=True).returncode != 0:
        pytest.skip("本机没有 Hermes 源码（或没有这个提交）")
    for name, (path, text) in C.SOURCES.items():
        src = subprocess.run(["git", "-C", real, "show", "ae2a81ccc0:" + path], capture_output=True, text=True).stdout
        for block in ([b for b in text.split("\n\n") if b.strip()] if name in C.COMPOSITE else [text]):       # 拼起来的片段（中间隔着宿主的注释）每一块都要原样在源码里
            assert block.rstrip("\n") in src, name


def test_our_constants_are_the_ones_the_frozen_source_uses():
    op, cl, kind = C.steer_open_close()
    assert (HM.STEER_OPEN, HM.STEER_CLOSE, HM.STEER_DISPLAY_KIND) == (op, cl, kind)
    assert C.begin_iteration([], "原句", "改口") == "原句" + HM.REDIRECT_SEP + "改口"
    assert C.merge_redirects("甲", "乙") == "甲" + HM.REDIRECT_MERGE + "乙"
    row = C.apply_redirect([{"role": "user", "content": "开头"}], "改口")[-1]
    assert HM.classify(row) == "redirect" and row["api_content"].startswith(HM.REDIRECT_API_PREFIX) and "display_kind" not in row
    assert HM.classify(C.steer_row("加一句")) == "steer"
    assert C.envelope("字", {"platform": "weixin"}).startswith(HM.ENVELOPE_HEAD + "\n") and ("\n" + HM.ENVELOPE_FOOT + "\n\n字") in C.envelope("字", {"platform": "weixin"})
    assert C.kanban_note(["甲"]).startswith(HM.KANBAN_NOTE_PREFIXES[0]) and C.kanban_note(["甲", "乙"]).startswith(HM.KANBAN_NOTE_PREFIXES[1])


# ================================================================== A：还原和配对
def test_one_redirect_pairs_to_the_opening_and_is_stored_with_the_verified_tag(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().redirect("要靠窗的位子").final("订好了")
    assert h.user == "帮我订个餐厅\n\nUser correction during the turn: 要靠窗的位子"
    h.sync(a)
    assert stored_rows(mem) == [("user", "帮我订个餐厅\n%s要靠窗的位子" % SAID), ("assistant", "订好了")]
    assert a.recorded_turns == 1 and skipped(a) == {}
    assert counts(a)["totals"] == {"midturn_kept": 1}


def test_two_redirects_in_one_turn_pair_with_k_equal_two(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("要靠窗的位子").tool().redirect("改成七点").final()
    assert h.user.count("User correction during the turn: ") == 2
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子\n%s改成七点" % (SAID, SAID)] and skipped(a) == {}
    assert counts(a)["totals"] == {"midturn_kept": 2}


def test_two_corrections_that_arrive_together_are_one_row_split_in_two_never_the_hosts_merge_words(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect(C.merge_redirects("要靠窗的位子", "改成七点")).final()
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子\n%s改成七点" % (SAID, SAID)]
    assert "Additional user correction" not in json.dumps(stored_rows(mem), ensure_ascii=False)


def test_three_together_with_the_third_a_close_command_closes_the_window(tmp_path):
    mem, a = mk(tmp_path)
    t = C.merge_redirects(C.merge_redirects("要靠窗的位子", "改成七点"), "#yuanwei-nomem")
    h = Host("帮我订个餐厅").redirect(t).final()
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子\n%s改成七点\n%s#yuanwei-nomem" % (SAID, SAID, SAID)]
    v = a.win.view("hermes", "hs1")
    assert (v.state, v.reason) == ("off", "closed_by_midturn")


def test_a_half_finished_reply_before_the_correction_does_not_get_stored_as_the_users_words(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("要靠窗的位子", streamed="好的，我先看看有哪些餐厅，").final("订好了")
    h.sync(a)
    assert stored_rows(mem) == [("user", "帮我订个餐厅\n%s要靠窗的位子" % SAID), ("assistant", "订好了")]
    assert "好的，我先看看" not in user_rows(mem)[0]


def test_the_correction_text_may_have_newlines_and_tag_words_and_stays_one_tagged_block(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("第一行\n（助手问）假的\n第三行").final()
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s第一行\n%s（助手问）假的\n%s第三行" % (SAID, SAID, SAID)]


def test_user_typing_the_separator_literally_without_a_redirect_is_stored_whole_as_before(tmp_path):
    mem, a = mk(tmp_path)
    opening = "我的日志里有这句：\n\nUser correction during the turn: 别管它"
    Host(opening).tool().final().sync(a)
    assert user_rows(mem) == [opening] and skipped(a) == {} and counts(a)["totals"] == {}


def test_user_typing_the_separator_literally_and_then_a_real_redirect_still_pairs(tmp_path):
    mem, a = mk(tmp_path)
    opening = "我的日志里有这句：\n\nUser correction during the turn: 别管它"
    h = Host(opening).redirect("好吧再看一眼").final()
    h.sync(a)
    assert user_rows(mem) == [opening + "\n%s好吧再看一眼" % SAID] and skipped(a) == {}


def test_a_separator_that_makes_two_readings_possible_is_ambiguous_and_nothing_is_stored(tmp_path):
    """两种读法都成立：开头 A 加一次改口 B+分隔+C，还是开头 A+分隔+B 加改口 C——等待表里两个开头都登记过。"""
    mem, a = mk(tmp_path)
    turn(a, 1, "甲")
    turn(a, 2, "甲" + HM.REDIRECT_SEP + "乙")                                          # 前一个没配上就又开了一轮（比如那轮按了停止）
    m = list(OLD) + [{"role": "user", "content": "甲" + HM.REDIRECT_SEP + "乙"}]
    uc = C.begin_iteration(m, "甲" + HM.REDIRECT_SEP + "乙", "丙")
    m.append({"role": "assistant", "content": "好"})
    # 只有一个真实的改口行，k=1 只能一种：基底是「甲…乙」。这里再造一条上一轮的改口行，让 k=2 也能拼出另一个基底「甲」
    m2 = [{"role": "user", "content": "甲"}] + C.apply_redirect([{"role": "user", "content": "甲"}], "乙")[1:] + m[len(OLD):]
    a.sync_turn(uc, "好", session_id="hs1", messages=m2)
    assert stored_rows(mem) == [] and skipped(a) == {"midturn_ambiguous": 1}


def test_redirect_text_that_the_hosts_key_scrub_changed_differently_goes_to_t2_and_only_the_opening_and_reply_are_stored(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("要靠窗的位子").final("订好了")
    h.user = "帮我订个餐厅" + HM.REDIRECT_SEP + "要靠窗的位子（清洗改过的）"             # 清洗在整串上做、在行上做的结果对不上
    h.sync(a)
    assert stored_rows(mem) == [("user", "帮我订个餐厅"), ("assistant", "订好了")]
    assert skipped(a) == {} and counts(a)["totals"] == {AS.NOTE_MID_UNMATCHED: 1}
    assert "靠窗" not in json.dumps(stored_rows(mem), ensure_ascii=False)               # 拼出来的字一个都不存


def test_t2_with_no_matching_opening_row_is_a_named_skip_not_no_pending_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "帮我订个餐厅")
    a.sync_turn("别的话" + HM.REDIRECT_SEP + "改口", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "别的话"}, {"role": "assistant", "content": "好"}])
    assert stored_rows(mem) == [] and skipped(a) == {"midturn_unmatched": 1}


def test_a_turn_with_no_redirect_and_no_separator_still_says_no_pending_turn(tmp_path):
    mem, a = mk(tmp_path)
    a.sync_turn("没登记过的话", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "没登记过的话"}])
    assert skipped(a) == {"no_pending_turn": 1}


def test_a_plain_host_row_without_the_mark_after_the_redirect_does_not_stop_t1_from_pairing(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().redirect("要靠窗的位子").tool()
    h.m.append({"role": "user", "content": "Continue from the compressed conversation context above. This marker exists because no human user turn was available."})
    h.final("订好了")
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子" % SAID] and skipped(a) == {}


def test_another_machinery_row_in_the_turn_still_drops_the_whole_turn(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().redirect("要靠窗的位子").tool()
    h.m.append({"role": "user", "content": "后台完成", "display_kind": "process_complete"})
    h.final().sync(a)
    assert stored_rows(mem) == [] and skipped(a) == {"machinery_row": 1}


def test_same_text_old_registration_that_agrees_takes_the_latest_one(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "帮我订个餐厅")                                                         # 上一轮按了停止：登记还留着
    h = Host("帮我订个餐厅").redirect("要靠窗的位子").final()
    h.sync(a, n=2)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子" % SAID]
    assert len(a._pending["hs1"]) == 1                                                  # 只取走了最新的那一条


def test_same_text_old_registration_that_disagrees_is_ambiguous_and_nothing_is_stored(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "帮我订个餐厅", author_is_bot=True)                                      # 同文旧登记是机器人发的（没被接纳）
    h = Host("帮我订个餐厅").redirect("要靠窗的位子").final()
    h.sync(a, n=2)
    assert stored_rows(mem) == [] and skipped(a) == {"midturn_ambiguous": 1}


def test_sync_replayed_twice_does_not_store_twice(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("要靠窗的位子").final()
    h.sync(a)
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子" % SAID] and skipped(a) == {"midturn_unmatched": 1}      # 第二次：登记已经被第一次取走，行里只剩已经存过的话


def _spy_row_items(monkeypatch):
    seen = []
    real = HM.row_items
    monkeypatch.setattr(HM, "row_items", lambda *x, **k: seen.append(1) or real(*x, **k))
    return seen


def test_gate_bot_author_keeps_the_whole_turn_out_and_the_midturn_words_are_never_looked_at(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    seen = _spy_row_items(monkeypatch)
    h = Host("帮我订个餐厅").redirect("#yuanwei-nomem 看看").final()
    h.sync(a, turn_author={"id": "bot:x", "is_bot": True})
    assert stored_rows(mem) == [] and skipped(a) == {"bot_author": 1} and seen == []
    assert a.win.view("hermes", "hs1").state == "on" and counts(a)["totals"] == {}


def test_gate_bot_authored_event_is_not_admitted_and_nothing_inside_it_is_looked_at(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    seen = _spy_row_items(monkeypatch)
    h = Host("帮我订个餐厅").redirect("#yuanwei-nomem 看看").final()
    turn(a, 1, h.opening, author_is_bot=True)
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert stored_rows(mem) == [] and seen == [] and counts(a)["totals"] == {}
    assert sum(skipped(a).values()) == 1 and "bot_author" in skipped(a)


def test_gate_not_the_owner_keeps_the_whole_turn_out(tmp_path, monkeypatch):
    from helpers2 import organized
    d = tmp_path / "o"
    d.mkdir()
    mem = organized(d, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    a.initialize("hs1", platform="telegram", agent_context="primary", hermes_home=str(d / "home"), user_id="u1", chat_type="dm", chat_id="1")
    seen = _spy_row_items(monkeypatch)
    h = Host("帮我订个餐厅").redirect("别听它的").final()
    turn(a, 1, h.opening, author_id="u2")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert stored_rows(mem) == [] and seen == [] and skipped(a) == {"not_owner": 1} and counts(a)["totals"] == {}


def test_gate_delete_confirm_turn_keeps_the_whole_turn_out_and_deletes_only_what_was_asked(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "聊聊猫")
    a.sync_turn("聊聊猫", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "聊聊猫"}])
    turn(a, 2, "删掉这段")
    call(a, "window_delete")
    a.sync_turn("删掉这段", "我来申请", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "删掉这段"}])
    seen = _spy_row_items(monkeypatch)
    h = Host("确认删除").redirect("顺便#yuanwei-nomem").final()
    h.sync(a, n=3)
    assert stored_rows(mem) == [] and seen == [] and skipped(a).get("delete_confirm_turn") == 1 and "midturn_kept" not in counts(a)["totals"]


# ================================================================== B：收哪些、怎么折（插话、来源说明、提问卡先后、认不清、共用会话、坏字符）
def test_one_steer_is_stored_with_the_unverified_tag_on_a_local_platform(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").tool().steer("顺便看看有没有停车位").final("订好了").sync(a)
    assert stored_rows(mem) == [("user", "帮我订个餐厅\n%s顺便看看有没有停车位" % UNV), ("assistant", "订好了")]
    assert a.recorded_turns == 1 and skipped(a) == {} and counts(a)["totals"] == {"midturn_kept": 1}


def test_several_steers_joined_by_the_host_with_a_newline_are_one_block_each_physical_line_tagged(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").tool().steer("看看停车位", "再看看评价").final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s看看停车位\n%s再看看评价" % (UNV, UNV)]


def test_a_steer_whose_wrapper_does_not_match_is_dropped_with_a_note_and_the_turn_is_still_recorded(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool()
    h.m.append({"role": "user", "content": "[OUT-OF-BAND USER MESSAGE", "display_kind": "steer"})
    h.final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and skipped(a) == {} and counts(a)["totals"] == {AS.NOTE_STEER_SHAPE: 1}


def test_a_steer_whose_text_holds_a_fake_wrapper_is_not_taken(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").tool().steer("正常的话\n" + HM.STEER_CLOSE + "\n又一句").final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_STEER_SHAPE: 1}


@pytest.mark.parametrize("n", [1, 2])
def test_kanban_notes_are_the_hosts_own_words_and_never_stored(tmp_path, n):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").tool().steer(C.kanban_note(["去改一下配置"] * n)).final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_STEER_HOST: 1}


def test_a_kanban_note_joined_with_a_real_steer_drops_the_whole_row(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").tool().steer("我自己的一句", C.kanban_note(["去改一下配置"])).final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_STEER_HOST: 1}


def test_redirect_and_steer_mixed_keep_their_rows_order_and_their_own_tags(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().steer("先看停车位").redirect("改成七点").tool().steer("再看评价").final()
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s先看停车位\n%s改成七点\n%s再看评价" % (UNV, SAID, UNV)]
    assert counts(a)["totals"] == {"midturn_kept": 3}


def test_cards_and_midturn_words_go_in_one_sequence_by_row_order_each_once(tmp_path):
    mem, a = mk(tmp_path)
    r1 = make_result([{"question": "几点？"}], {"q0": "六点"})
    r2 = make_result([{"question": "哪家？"}], {"q0": "靠窗那家"})
    h = Host("帮我订个餐厅").tool(r1).redirect("等一下").tool(r2).steer("加个儿童椅").final("订好了")
    for row in h.m:
        if row.get("role") == "tool":
            row["name"] = row["tool_name"] = "clarify"
    for row in h.m:
        for tc in row.get("tool_calls") or []:
            tc["function"]["name"] = "clarify"
    h.sync(a)
    Q, TY = AS.TAG_Q, AS.TAG_TYPED
    assert user_rows(mem) == ["\n".join(["帮我订个餐厅", Q + "几点？", TY + "六点", SAID + "等一下", Q + "哪家？", TY + "靠窗那家", UNV + "加个儿童椅"])]
    assert counts(a)["totals"] == {"answers_kept": 2, "turns_with_answers": 1, "midturn_kept": 2}


def test_the_message_rows_passed_in_are_never_mutated(tmp_path):
    import copy
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().redirect("改口").tool().steer("插话").final()
    before = copy.deepcopy(h.m)
    h.sync(a)
    assert h.m == before and any(r.get("api_content") for r in h.m)


def test_unclear_which_turn_stores_the_opening_and_reply_only_and_never_looks_at_the_words(tmp_path, monkeypatch):
    """开头行（按文字找到的）后面还有一条像一轮开头的普通用户行：找到的可能是更早一轮的同字行，中途的话一条不收。"""
    mem, a = mk(tmp_path)
    seen = _spy_row_items(monkeypatch)
    h = Host("帮我订个餐厅").tool().steer("#yuanwei-nomem")
    h.m.append({"role": "user", "content": "一句新的开头"})
    h.final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and skipped(a) == {} and counts(a)["totals"] == {AS.NOTE_MID_UNCLEAR: 1}
    assert a.win.view("hermes", "hs1").state == "on"                                       # 口令一个字不认


def test_the_turn_text_given_to_the_unclear_check_is_the_restored_opening_not_the_joined_string(tmp_path):
    """并进来的当前行（「[System: …]\\n\\n开头」）要按还原后的原句认；传拼好的串就认不出，会把更早一轮的话收进来。"""
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").tool().redirect("改成七点").tool()
    h.m.append({"role": "user", "content": "[System: 换了模型]\n\n帮我订个餐厅"})
    h.final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_MID_UNCLEAR: 1}


@pytest.mark.parametrize("platform,chat_type,chat_id", [("discord", "thread", "x"), ("slack", "dm", "G123"), ("matrix", "dm", "r")])
def test_shared_or_unknown_chats_do_not_store_the_turn_and_never_close_the_window(tmp_path, platform, chat_type, chat_id, monkeypatch):
    from helpers2 import organized
    d = tmp_path / "s"
    d.mkdir()
    mem = organized(d, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {platform: ["u1"]}}, memory=mem, executor="inline")
    a.initialize("hs1", platform=platform, agent_context="primary", hermes_home=str(d / "home"), user_id="u1", chat_type=chat_type, chat_id=chat_id)
    seen = _spy_row_items(monkeypatch)
    h = Host("帮我订个餐厅").redirect("#yuanwei-nomem").final()
    turn(a, 1, h.opening, author_id="u1")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert stored_rows(mem) == [] and skipped(a) == {"midturn_shared_chat": 1} and seen == []
    assert a.win.view("hermes", "hs1").state == "on"


def test_a_lone_surrogate_in_one_correction_drops_only_that_one_and_the_turn_is_recorded(tmp_path):
    mem, a = mk(tmp_path)
    h = Host("帮我订个餐厅").redirect("坏字\ud800字").tool().redirect("改成七点").final()
    h.sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s改成七点" % SAID] and skipped(a) == {}
    assert counts(a)["totals"] == {AS.NOTE_MID_BAD_TEXT: 1, "midturn_kept": 1}


def test_overlong_midturn_text_is_kept_whole_and_noted_long(tmp_path):
    mem, a = mk(tmp_path)
    big = "很长的一句。" * 400
    Host("帮我订个餐厅").redirect(big).final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n%s%s" % (SAID, big)] and counts(a)["totals"].get("clarify_long") == 1


# ---------------- 聊天平台：来源说明
def _chat_adapter(tmp_path, platform="weixin", chat_type="dm", chat_id="c1"):
    from helpers2 import organized
    d = tmp_path / "chat"
    d.mkdir()
    mem = organized(d, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {platform: ["u1"]}}, memory=mem, executor="inline")
    a.initialize("hs1", platform=platform, agent_context="primary", hermes_home=str(d / "home"), user_id="u1", chat_type=chat_type, chat_id=chat_id)
    return mem, a


def env(text, **extra):
    origin = {"platform": "weixin", "chat_id": "c1", "chat_type": "dm", "user_id": "u1", "message_id": "m9"}
    origin.update(extra)
    return C.envelope(text, origin)


def _sync_chat(a, h, n=1):
    turn(a, n, h.opening, author_id="u1")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)


def test_chat_redirect_envelope_is_removed_and_the_words_are_verified(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(env("要靠窗的位子")).final()
    assert "Gateway message origin" in h.user                                          # 宿主真的把来源说明接进了拼好的串
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅\n%s要靠窗的位子" % SAID] and skipped(a) == {}
    assert "Gateway" not in json.dumps(stored_rows(mem), ensure_ascii=False) and "m9" not in json.dumps(stored_rows(mem), ensure_ascii=False)


def test_chat_steer_with_an_envelope_is_verified_and_without_one_is_not(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").tool().steer(env("看看停车位")).tool().steer("没有来源说明的一句").final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅\n%s看看停车位\n%s没有来源说明的一句" % (SAID, UNV)]


def test_chat_steers_joined_into_one_row_are_split_at_each_envelope_and_each_is_verified(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").tool().steer(env("看看停车位\n第二行"), env("再看看评价")).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅\n%s看看停车位\n%s第二行\n%s再看看评价" % (SAID, SAID, SAID)]


def test_chat_two_redirects_together_each_segment_loses_its_own_envelope(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(C.merge_redirects(env("甲"), env("乙"))).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅\n%s甲\n%s乙" % (SAID, SAID)]


def test_chat_third_together_segment_is_a_close_command_after_its_envelope_is_removed(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(C.merge_redirects(C.merge_redirects(env("甲"), env("乙")), env("#yuanwei-nomem"))).final()
    _sync_chat(a, h)
    assert a.win.view("hermes", "hs1").state == "off"


def test_chat_envelope_with_broken_json_drops_the_whole_row(tmp_path):                      # 补一第 6 条：一行要么整行收、要么整行不收
    mem, a = _chat_adapter(tmp_path)
    bad = env("坏的").replace('{"platform"', '{"platform" BROKEN')
    h = Host("帮我订个餐厅").redirect(C.merge_redirects(bad, env("好的"))).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_MID_ENVELOPE: 1}


def test_chat_envelope_that_is_not_a_json_object_is_dropped(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(env("字").replace('{"platform": "weixin", "chat_id": "c1", "chat_type": "dm", "user_id": "u1", "message_id": "m9"}', '["x"]')).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_MID_ENVELOPE: 1}


def test_chat_leftover_envelope_words_after_stripping_drop_the_piece(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(env("字\n" + HM.ENVELOPE_FOOT)).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_MID_ENVELOPE: 1}


def test_chat_message_from_a_bot_drops_the_whole_row(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    h = Host("帮我订个餐厅").redirect(C.merge_redirects(env("机器人说的", from_bot=True), env("我说的"))).final()
    _sync_chat(a, h)
    assert user_rows(mem) == ["帮我订个餐厅"] and counts(a)["totals"] == {AS.NOTE_MID_FROM_BOT: 1}                # 补一第 6 条：任何一段是机器人发的，整行不存


def test_a_local_platform_never_strips_a_literal_envelope_the_user_typed(tmp_path):
    """桌面用户字面打了来源说明那几行：照原样存（本机没有这层说明，一个字不动）。"""
    mem, a = mk(tmp_path)
    typed = env("我自己打的")
    Host("帮我订个餐厅").redirect(typed).final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n" + "\n".join(SAID + line for line in typed.split("\n"))]


def test_a_local_steer_with_a_literal_envelope_is_unverified_and_untouched(tmp_path):
    mem, a = mk(tmp_path)
    typed = env("我自己打的")
    Host("帮我订个餐厅").tool().steer(typed).final().sync(a)
    assert user_rows(mem) == ["帮我订个餐厅\n" + "\n".join(UNV + line for line in typed.split("\n"))]


# ---------------- 带图
def _image_text_row(tmp_path, text, count=1):
    """附件路由：Hermes 真的 build_native_content_parts（逐字冻结的源码，exec 跑；图片转换换成假的）。"""
    import pathlib
    import typing

    from hermes_image_contract import SOURCES
    ns = {"Path": pathlib.Path, "Optional": typing.Optional, "Any": typing.Any, "_file_to_data_url": lambda _p: "data:image/png;base64,c3ludGhldGlj"}
    exec(compile("from __future__ import annotations\n" + SOURCES["agent/image_routing.py"], "image_routing.py", "exec"), ns)
    paths = []
    for i in range(count):
        p = tmp_path / ("synthetic-image%d.png" % i)
        p.write_bytes(b"synthetic")
        paths.append(str(p))
    parts, skipped_ = ns["build_native_content_parts"](text, paths)
    assert not skipped_
    return parts


def _api_row(text, count=1):
    """API 会话：开始和同步都是「字＋提示」（文字块和这串一模一样）。"""
    hints = "\n".join("[Image attached at: /synthetic/native-inputs/api_%032d.png]" % i for i in range(count))
    return [{"type": "text", "text": "%s\n\n%s" % (text, hints)}] + [{"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}} for _ in range(count)]


def _flat(row):
    return "\n".join(p["text"] for p in row if p.get("type") == "text")


def _no_hint(mem):
    return "[Image attached" not in json.dumps(stored_rows(mem), ensure_ascii=False) and "synthetic" not in json.dumps(stored_rows(mem), ensure_ascii=False)


@pytest.mark.parametrize("route", ["attachment", "api"])
@pytest.mark.parametrize("events", [["redirect"], ["redirect", "redirect"], ["steer"], ["steer", "steer"], ["redirect", "steer"]], ids=lambda e: "+".join(e))
def test_picture_turns_with_redirects_and_steers_store_the_caption_and_the_words_and_never_a_hint_line(tmp_path, route, events):
    mem, a = mk(tmp_path / "m")
    typed = "看看这张图"
    row = _image_text_row(tmp_path, typed, 1) if route == "attachment" else _api_row(typed, 1)
    h = Host(typed)
    h.m[-1] = {"role": "user", "content": row}
    h.opening = typed if route == "attachment" else _flat(row)
    h.user = h.opening                                                                       # 附件路由：交给记忆的是用户打的字（宿主拼改口时是字符串）；API 路由：原句是列表，宿主不拼，下面把它改回去
    h.tool()
    want = []
    for n, ev in enumerate(events):
        if ev == "redirect":
            before = h.user
            h.redirect("改口%d" % n)
            if route == "api":
                h.user = before                                                              # 原句是列表时宿主不接改口（begin_iteration 的 isinstance(…, str) 为假）
            want.append(SAID + "改口%d" % n)
        else:
            h.steer("插话%d" % n)
            want.append(UNV + "插话%d" % n)
        h.tool()
    h.final("看到了")
    h.sync(a)
    assert stored_rows(mem) == [("user", "\n".join([typed, "（带 1 张图）"] + want)), ("assistant", "看到了")], stored_rows(mem)
    assert _no_hint(mem) and skipped(a) == {}


def test_picture_turn_with_a_redirect_and_a_steer_together_on_the_attachment_route(tmp_path):
    mem, a = mk(tmp_path / "m")
    typed = "看看这张图"
    row = _image_text_row(tmp_path, typed, 2)
    h = Host(typed)
    h.m[-1] = {"role": "user", "content": row}
    h.tool().steer("加一句").redirect("改口").final("看到了")
    h.sync(a)
    assert user_rows(mem) == ["%s\n（带 2 张图）\n%s加一句\n%s改口" % (typed, UNV, SAID)] and _no_hint(mem)


# ================================================================== C：口令
@pytest.mark.parametrize("how", ["redirect", "steer", "steer_unverified_chat_without_envelope"])
def test_a_close_command_in_midturn_words_closes_the_window_with_source_midturn_and_every_word_is_still_stored(tmp_path, how):
    mem, a = (_chat_adapter(tmp_path) if how.endswith("chat_without_envelope") else mk(tmp_path))
    h = Host("帮我订个餐厅")
    h = h.redirect("先说一句").redirect("#yuanwei-nomem") if how == "redirect" else h.tool().steer("先说一句").steer("#yuanwei-nomem")
    h.tool().steer("关了以后又说了一句") if how == "redirect" else None
    h.final()
    if how.endswith("chat_without_envelope"):
        _sync_chat(a, h)
    else:
        h.sync(a)
    v = a.win.view("hermes", "hs1")
    assert (v.state, v.reason) == ("off", "closed_by_midturn")
    assert "先说一句" in user_rows(mem)[0] and "#yuanwei-nomem" in user_rows(mem)[0]
    assert counts(a)["totals"]["midturn_control:closed"] == 1


def test_plain_words_that_ban_reading_also_close_it_and_a_later_recall_is_refused(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").redirect("以后别用记忆了").final().sync(a)
    assert a.win.view("hermes", "hs1").state == "off"
    turn(a, 2, "下一轮")
    assert call(a, "recall", {"query": "餐厅"})["reason"] == "window_closed"


def test_a_close_command_that_is_not_the_first_line_of_its_row_but_the_first_of_a_later_segment_still_closes(tmp_path):
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").redirect(C.merge_redirects("看看评价", "#yuanwei-nomem\n然后订")).final().sync(a)
    assert a.win.view("hermes", "hs1").state == "off"


def test_open_and_confirm_words_in_midturn_never_open_never_confirm_and_queue_a_wording_for_the_next_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem")
    a.sync_turn("#yuanwei-nomem", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "#yuanwei-nomem"}])
    turn(a, 2, "请重新用记忆")
    assert "确认开启" in call(a, "window_on")["message"]
    a._notices.clear()
    h = Host("请重新用记忆").redirect("确认开启").final("请确认")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert a.win.view("hermes", "hs1").state == "off"
    turn(a, 3, "嗯")
    assert a.MIDTURN_NOT_COUNTED["open"] in a._notices and a.MIDTURN_NOT_COUNTED["open"].startswith("上一轮你在助手没答完时发的「确认开启」不算数")
    assert "提问卡" not in a.MIDTURN_NOT_COUNTED["open"] and counts(a)["totals"]["midturn_control:confirm_not_counted"] == 1


def test_confirm_delete_in_midturn_words_never_deletes(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "聊聊猫")
    a.sync_turn("聊聊猫", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "聊聊猫"}])
    turn(a, 2, "删掉这段对话的记忆")
    assert call(a, "window_delete")["state"] == "waiting"
    h = Host("删掉这段对话的记忆").tool().steer("确认删除").final("请确认")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0
    assert user_rows(mem)[-1].endswith(UNV + "确认删除") and len(stored_rows(mem)) == 4
    turn(a, 3, "好的谢谢")
    assert a.MIDTURN_NOT_COUNTED["delete"] in a._notices and "提问卡" not in a.MIDTURN_NOT_COUNTED["delete"]


def test_the_open_command_in_midturn_words_when_the_window_is_closed_never_opens_and_says_so_next_turn(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "#yuanwei-nomem")
    a.sync_turn("#yuanwei-nomem", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "#yuanwei-nomem"}])
    h = Host("继续聊").redirect("#yuanwei-mem").final()
    h.sync(a, n=2)
    assert a.win.view("hermes", "hs1").state == "off" and user_rows(mem)[-1].endswith(SAID + "#yuanwei-mem")
    turn(a, 3, "嗯")
    assert a.MIDTURN_NOT_COUNTED["mem"] in a._notices


def test_the_open_command_in_midturn_words_when_the_window_is_open_says_nothing(tmp_path):
    mem, a = mk(tmp_path)
    Host("继续聊").redirect("#yuanwei-mem").final().sync(a)
    turn(a, 2, "嗯")
    assert a.win.view("hermes", "hs1").state == "on" and not any("不算数" in x for x in a._notices)


def test_open_then_close_in_one_turn_follows_the_close(tmp_path):
    mem, a = mk(tmp_path)
    Host("继续聊").redirect(C.merge_redirects("#yuanwei-mem", "#yuanwei-nomem")).final().sync(a)
    assert a.win.view("hermes", "hs1").state == "off"


def test_a_late_close_from_midturn_words_does_not_override_a_later_valid_open(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "开头")
    turn(a, 2, "#yuanwei-nomem")
    turn(a, 3, "#yuanwei-mem")
    h = Host("开头").redirect("别用记忆").final()
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    assert a.win.view("hermes", "hs1").state == "on"
    ops = [tuple(r) for r in mem.store.conn.execute("SELECT source, outcome FROM window_ops WHERE source='midturn'")]
    assert ops == [("midturn", "stale_ignored")]


def test_a_failed_close_from_midturn_words_is_told_next_turn_in_its_own_wording(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    monkeypatch.setattr(a.win, "close", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("写不进去")))
    Host("开头").redirect("以后别用记忆了").final().sync(a)
    assert counts(a)["totals"]["midturn_control:close_failed"] == 1
    monkeypatch.undo()
    turn(a, 2, "继续聊")
    assert a.CLOSE_FAILED_MIDTURN in a._notices and a.CLOSE_FAILED_MIDTURN.startswith("上一轮你在助手没答完时发的「关」")


def test_window_close_accepts_the_new_source_and_still_refuses_unknown_ones(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "开头")
    w = a.win.resolve("hermes", "hs1", "")
    assert a.win.close(w.window_id, source="midturn", op_id="op-x").outcome == "applied"
    with pytest.raises(ValueError):
        a.win.close(w.window_id, source="somewhere_else", op_id="op-y")


# ================================================================== E：备注进 clarify-counts.json，不进 skip-reasons.json
def test_midturn_notes_and_kept_counts_go_to_clarify_counts_and_never_to_skip_reasons(tmp_path):
    from yuanwei import skipreasons as SK
    mem, a = mk(tmp_path)
    Host("帮我订个餐厅").redirect("甲").tool().steer("乙").tool().steer(C.kanban_note(["便条"])).final().sync(a)
    c = counts(a)
    assert c["totals"] == {"midturn_kept": 2, AS.NOTE_STEER_HOST: 1} and c["problem"] is None
    assert sorted(c["recent"][-1]["notes"]) == sorted(["midturn_kept", "midturn_kept", AS.NOTE_STEER_HOST]) and "midturn_dropped" in c["meaning"] and "midturn_kept" in c["meaning"]
    assert SK.read(a._home)["totals"] == {} and a.status()["skipped"] == {}                 # 这一轮照常记了：不是跳过
    assert a.status()["clarify"]["notes"] == {"midturn_kept": 2, AS.NOTE_STEER_HOST: 1}      # 实例里的计数也有


def test_skipped_midturn_turns_are_skip_reasons_not_clarify_notes(tmp_path):
    from yuanwei import skipreasons as SK
    mem, a = mk(tmp_path)
    turn(a, 1, "帮我订个餐厅")
    a.sync_turn("别的话" + HM.REDIRECT_SEP + "改口", "好", session_id="hs1", messages=list(OLD) + [{"role": "user", "content": "别的话"}])
    assert SK.read(a._home)["totals"] == {"midturn_unmatched": 1} and counts(a)["totals"] == {}


def test_every_note_name_is_a_fixed_name_the_counts_file_accepts(tmp_path):
    import re
    from yuanwei import askcounts as AC
    names = [getattr(AS, n) for n in dir(AS) if n.startswith(("NOTE_MID", "NOTE_STEER"))] + ["midturn_control:closed", "midturn_control:close_failed", "midturn_control:confirm_not_counted"]
    assert len(names) >= 11 and all(re.fullmatch(AC._NAME, n) for n in names)
    assert AC.record(str(tmp_path), 0, names) is True and set(AC.read(str(tmp_path))["totals"]) == set(names)         # 一个都没被记成 other


# ================================================================== F：显示（标签、按句子显示的三处、别处）
LONG_SAID = "我想先看看环境怎么样，再看停车方不方便，最后确认一下价格是不是合理，没问题就订下来，如果有包间的话也一起问一下，顺便问问能不能提前预留位置，还有他们几点开始营业。"


def _said_row(opening="帮我订个餐厅", items=None):
    items = items or [{"said": "要靠窗的位子", "verified": True}, {"said": "顺便看看停车位", "verified": False}]
    f = AS.render(items, opening=opening)
    return AS.fold(opening, f.lines), f


def test_render_gives_one_tagged_line_per_physical_line_and_a_pure_result():
    items = [{"said": "甲\n乙", "verified": True}, {"said": "丙", "verified": False}, {"said": "  ", "verified": True}]
    f = AS.render(items, opening="开")
    assert f.lines == (SAID + "甲\n" + SAID + "乙", UNV + "丙") and f.said == 2 and f.said_texts == ("甲\n乙", "丙") and f.answers == 0
    assert AS.render(items, opening="开") == f and AS.render([], opening="开") == AS.Folded((), (), 0, (), ())
    for bad in ([{"said": 1, "verified": True}], [{"said": "x"}], [{"said": "x", "verified": 1}], [{"said": "x", "verified": True, "question": "q"}]):
        with pytest.raises(ValueError):
            AS.render(bad)


def test_cards_and_said_items_keep_their_order_in_render():
    items = [{"question": "Q？", "choices": None, "answers": [{"text": "答", "picked": False}]}, {"said": "中途", "verified": True}, {"question": "R？", "choices": ["a"], "answers": [{"text": "a", "picked": True}]}]
    f = AS.render(items, opening="开")
    assert f.lines == (AS.TAG_Q + "Q？", AS.TAG_TYPED + "答", SAID + "中途", AS.TAG_Q + "R？", AS.TAG_OPTS + "1）a", AS.TAG_PICK + "a") and (f.answers, f.said) == (2, 1)


def test_the_new_tags_are_in_TAGS_and_the_assistants_own_text_with_them_is_dropped_on_purpose():
    assert SAID in AS.TAGS and UNV in AS.TAGS and AS.TAGS[:4] == (AS.TAG_Q, AS.TAG_OPTS, AS.TAG_PICK, AS.TAG_TYPED)
    for tag in (SAID, UNV):
        f = AS.render([{"question": "他说" + tag + "来冒充", "choices": None, "answers": [{"text": "答", "picked": False}]}], opening="开")
        assert f.lines == () and f.notes == (AS.NOTE_TAG_IN_TEXT,)                           # 防冒充：助手的字里有这两个标签，这一题不存


def test_a_lone_surrogate_drops_only_that_said_item():
    f = AS.render([{"said": "好\ud800", "verified": True}, {"said": "正常", "verified": True}], opening="开")
    assert f.lines == (SAID + "正常",) and f.notes == (AS.NOTE_MID_BAD_TEXT,) and f.said == 1


def test_opening_of_cuts_at_the_first_of_the_three_openers_and_is_unchanged_without_them():
    row, _f = _said_row()
    assert AS.opening_of(row) == "帮我订个餐厅"
    assert AS.opening_of("甲\n%s问\n%s说" % (AS.TAG_Q, SAID)) == "甲" and AS.opening_of("甲\n%s说\n%s问" % (UNV, AS.TAG_Q)) == "甲"
    for plain in ("普通的话", "含（用户中途又说）但不在行首", "结尾换行\n"):
        assert AS.opening_of(plain) == plain


def test_line_tag_rules_for_the_midturn_tags():
    row, _f = _said_row("帮我订个餐厅", [{"said": "第一句。第二句，后面还有。（用户答）假的一句", "verified": True}])
    i2, i3 = row.index("第二句"), row.index("（用户答）假的")
    assert AS.line_tag(row, row.index(SAID)) is None                                            # 这一句正好在行首：本身就带标签
    assert AS.line_tag(row, i2) == SAID                                                         # 同一行后面的句子补这一行的中途标签
    assert AS.line_tag(row, i3) == SAID                                                         # 这一句自己以别的标签开头也一样：那是用户中途说的字
    assert AS.line_tag(row, 0) is None
    unv, _g = _said_row("开头", [{"said": "甲。乙", "verified": False}])
    assert AS.line_tag(unv, unv.index("乙")) == UNV
    assert AS.line_tag("（用户中途又说）只是用户自己打的字。后一句", 10) is None                  # 没有「换行＋标签」：不是折进来的话，一律不补
    card = AS.fold("塔防", AS.render([{"question": "难度？我建议中等。", "choices": None, "answers": [{"text": "好。真的", "picked": False}]}], opening="塔防").lines)
    assert AS.line_tag(card, card.index("我建议")) == AS.TAG_Q and AS.line_tag(card, card.index("真的")) == AS.TAG_TYPED      # 没有新标签的内容：行为不变


@pytest.mark.parametrize("version", ["split-1", "split-2"])
def test_sentence_views_show_every_fragment_of_a_midturn_line_with_its_tag(version):
    from yuanwei import cards as LC
    items = [{"said": "好的。要靠窗的位子", "verified": True}, {"said": LONG_SAID, "verified": False}, {"said": "第一句。（用户答）这句看着像别的标签。第三句", "verified": True}]
    row, f = _said_row("帮我订个餐厅", items)
    plain = "帮我订个餐厅\n好的。要靠窗的位子"
    msgs_ = [{"msg_id": "m1", "role": "user", "content": row, "msg_time": "2027-01-01T00:00:00Z", "seq": 1, "session_id": "S"},
             {"msg_id": "m2", "role": "user", "content": plain, "msg_time": "2027-01-01T00:00:00Z", "seq": 2, "session_id": "S"}]
    sents = LC.session_sentences(msgs_, "w", version)
    mine = [s for s in sents if s["msg_id"] == "m1"]
    assert len(mine) > 7                                                                          # 两行各切成几句，长句按逗号切开
    for s in mine[1:]:                                                                            # 第一句是用户自己打的开头
        shown = LC.sentence_line(s).split("：", 1)[1]
        assert shown.startswith((SAID, UNV)), shown
        assert s["text"] == row[s["start"]:s["end"]]                                               # 存下的句子字不变
    assert any(LC.sentence_line(s).split("：", 1)[1] == UNV + "再看停车方不方便，" for s in mine)                # 长句按逗号切开，每段都带完整的标签
    assert not any("来源未核实）来源未核实）" in LC.sentence_line(s) for s in mine)                         # 标签里没有逗号，不会被切开
    assert any(LC.sentence_line(s).split("：", 1)[1] == SAID + "（用户答）这句看着像别的标签。" for s in mine)       # 这一句自己以「（用户答）」开头：照样补上中途的标签（那是用户中途说的字）
    assert all("display" not in s for s in sents if s["msg_id"] == "m2")


def test_hinted_row_match_ignores_the_placeholders_and_still_finds_the_picture_row(tmp_path):
    """占位行不是用户行，_hinted_row_match 只看最近的用户行：改口、插话夹在带图那一行后面也不会让它认不出（也不会借到更早的图）。"""
    from yuanwei.hermes_adapter import _hinted_row_match, _image_sync_match
    row = _image_text_row(tmp_path, "看这张图", 1)
    base = list(OLD) + [{"role": "user", "content": row}]
    marked, marks = HM.mark_rows(base + [C.steer_row("插话")])
    assert marks and _hinted_row_match(marked, "看这张图")[1:3] == (1, len(OLD))
    assert _hinted_row_match(base + [C.steer_row("插话")], "看这张图") is None                       # 不换占位：最近的用户行是插话行，不是列表，认不出
    assert _image_sync_match(marked, "看这张图") is not None


def test_mark_rows_gives_the_same_list_object_when_there_is_nothing_to_mark():
    rows = list(OLD) + [{"role": "user", "content": "普通的话"}, {"role": "user", "content": "后台", "display_kind": "process_complete"}]
    out, marks = HM.mark_rows(rows)
    assert out is rows and marks == {}


def test_core_layer_stays_host_independent():
    import yuanwei.asked as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "hermes" not in src.split('"""', 2)[2].lower()
    import yuanwei.hermes_midturn as hm
    code = open(hm.__file__, encoding="utf-8").read()
    assert "import hermes" not in code and "from hermes" not in code and "import agent" not in code and "import gateway" not in code


def test_tokenizing_drops_the_new_tag_words_like_the_old_ones():
    from yuanwei import util as U
    assert U.bigrams(SAID + "靠窗") == U.bigrams("靠窗") and U.bigrams(UNV + "停车") == U.bigrams("停车")


def test_organizer_request_cards_and_attachments_show_the_midturn_tag(tmp_path):
    """真的走一遍：整理请求（这次对话）、主线卡片引的原话、attachments 工具；三处都不出现「不带标签的半句」。"""
    from helpers import Scripted, make_mem
    from test_a7_organize_switch import named
    from yuanwei import cards as LC
    prov = Scripted(named)
    mem = make_mem(tmp_path, provider=prov, spawn="inline", max_step_messages=2)
    a = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    h = Host("塔防").redirect("先做中等难度，新手更容易上手。再做困难难度，老玩家会喜欢。").final("好")
    h.sync(a, n=1)
    Host("第二件事").final().sync(a, n=2)
    Host("第三件事").final().sync(a, n=3)
    mem.end_session("hermes", "hs1")
    mem.run()
    texts = [c["text"] for c in prov.calls]
    assert texts and any(SAID + "再做困难难度，老玩家会喜欢。" in t for t in texts)                         # 整理请求
    assert not any("）：再做困难难度" in t or "「再做困难难度" in t for t in texts)
    st = mem.store.read_state(mem.ns)
    tid = st["tables"]["threads"][0]["thread_id"]
    v = LC._View(st, lambda mid: (mem.store.get_message(mem.ns, mid) or {}).get("content"))
    lines = LC.thread_card_lines(v, tid, LC.LightConfig(first_quotes=50, recent_quotes=0, excerpt_chars=200))
    quotes = [l.split("「", 1)[1] for l in lines if "原话（" in l and "，用户，" in l]
    assert any(q.startswith(SAID + "再做困难难度") for q in quotes) and all(q.startswith(SAID) for q in quotes[1:] if "难度" in q)    # 主线卡片
    turn(a, 4, "看看挂接")
    mid = next(r["msg_id"] for r in mem.store.list_messages(mem.ns) if r["content"].startswith("塔防\n"))
    out = call(a, "attachments", {"message_id": mid})
    assert out["status"] == "ok" and "用户：" + SAID + "再做困难难度" in out["text"] and "用户：再做困难难度" not in out["text"]         # attachments


def test_a_card_answer_after_a_midturn_line_keeps_its_own_tag_and_the_midturn_line_keeps_its_own(tmp_path):
    from yuanwei import cards as LC
    row = AS.fold("塔防", AS.render([{"said": "好。就这样", "verified": True}, {"question": "难度？我建议中等。", "choices": None, "answers": [{"text": "好。真的", "picked": False}]}], opening="塔防").lines)
    sents = LC.session_sentences([{"msg_id": "m1", "role": "user", "content": row, "msg_time": "2027-01-01T00:00:00Z", "seq": 1, "session_id": "S"}], "w", "split-2")
    shown = [LC.sentence_line(s).split("：", 1)[1] for s in sents][1:]
    assert shown[0].startswith(SAID) and shown[1] == SAID + "就这样" and any(x.startswith(AS.TAG_Q) for x in shown) and shown[-1] == AS.TAG_TYPED + "真的"


# ================================================================== 更早的轮次里的改口、插话行不会被这一轮再存一遍
def test_midturn_rows_of_earlier_turns_in_messages_are_never_stored_into_this_turn(tmp_path):
    mem, a = mk(tmp_path)
    old = Host("上一轮的话").tool().redirect("上一轮的改口").tool().steer("上一轮的插话").final("上一轮的回答")
    new = Host("这一轮的话", pre=old.m).tool().final("这一轮的回答")
    new.sync(a, n=2)
    assert stored_rows(mem) == [("user", "这一轮的话"), ("assistant", "这一轮的回答")] and skipped(a) == {} and counts(a)["totals"] == {}


def test_an_earlier_turns_redirect_with_the_same_words_is_not_taken_as_this_turns_second_redirect(tmp_path):
    """这一轮一次改口，上一轮的改口行文字和它一样：k＝1 成立，k＝2 要求串里有两段，不成立——不会把上一轮那条当成这一轮的。"""
    mem, a = mk(tmp_path)
    old = Host("上一轮的话").redirect("要靠窗").final("好")
    new = Host("这一轮的话", pre=old.m).redirect("要靠窗").final("订好了")
    new.sync(a, n=2)
    assert user_rows(mem) == ["这一轮的话\n%s要靠窗" % SAID] and counts(a)["totals"] == {"midturn_kept": 1}


def test_a_model_switch_line_merged_into_the_opening_row_does_not_pull_in_an_older_turns_steer(tmp_path):
    """换模型这类情形：宿主把「[System: …]」并进了当前开头行，按文字往回找会落到更早一轮的同字行，那一轮后面的插话不能算这一轮的。"""
    mem, a = mk(tmp_path)
    old = Host("帮我订个餐厅").tool().steer("旧轮的插话").final("订好了")
    m = list(old.m) + [{"role": "user", "content": "[System: 换了模型]\n\n帮我订个餐厅"}]
    h = Host("帮我订个餐厅", pre=old.m)
    h.m = m
    h.tool().final("又订好了")
    h.sync(a, n=2)
    assert [c for c in user_rows(mem)] == ["帮我订个餐厅"] and "旧轮的插话" not in json.dumps(stored_rows(mem), ensure_ascii=False)
    assert counts(a)["totals"] == {AS.NOTE_MID_UNCLEAR: 1}


def test_the_opening_row_found_by_text_is_an_older_turns_so_the_redirect_rows_of_the_older_turn_are_not_taken_as_this_turns(tmp_path):
    """T1 配上以后还要核：开头行（按文字找到的）之后的改口行，必须恰好就是配上的那最后 k 条；找到的是更早一轮的同字行时，早一轮的改口行也会落在它后面，不能算这一轮的。"""
    mem, a = mk(tmp_path)
    old = Host("帮我订个餐厅").redirect("旧轮的改口").final("订好了")
    h = Host("帮我订个餐厅", pre=old.m)
    h.m[-1] = {"role": "user", "content": "[备注：用户在桌面端发的]\n帮我订个餐厅（已读）"}                    # 这一轮的开头行被宿主改过字，按文字找不到它，只能落到更早一轮的同字行
    h.redirect("这一轮的改口").final("又订好了")
    h.sync(a, n=2)
    assert user_rows(mem) == ["帮我订个餐厅"] and "旧轮的改口" not in json.dumps(stored_rows(mem), ensure_ascii=False)
    assert counts(a)["totals"] == {AS.NOTE_MID_UNMATCHED: 1}


def test_the_conversation_list_preview_and_deletion_use_only_what_the_user_typed_first(tmp_path):
    from yuanwei import deletion as DL
    mem, a = mk(tmp_path)
    Host("订餐厅的开头那句").redirect("中途又说的话").final("好").sync(a)
    row = user_rows(mem)[0]
    assert AS.opening_of(row) == "订餐厅的开头那句" and "中途又说的话" in row
    assert "中途又说" not in DL.AS.opening_of(row)
