"""清单第 56 条第一批·第 6 节：聊天平台一对一对话里，开头三行来源说明（宿主加的，含账号编号）不再存进库。
指纹、令牌、配对还用原文；多份并在一起（bad）、共用会话、本机平台照今天；是机器人发的不记；说明后面第一行是 #yuanwei-nomem 时也要关（只认关）。"""
import hermes_midturn_contract as C
from test_fix55_midturn import Host, _chat_adapter, counts, env, stored_rows, turn
from test_hermes_adapter import mk
from yuanwei import hermes_midturn as HM


def sync_raw(a, raw, reply="好的", author="u1", n=1, messages=None):
    turn(a, n, raw, author_id=author)
    m = messages or [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}, {"role": "user", "content": raw}, {"role": "assistant", "content": reply}]
    a.sync_turn(raw, reply, session_id="hs1", messages=m)


def rows(mem):
    return stored_rows(mem)


def test_three_line_envelope_is_not_stored_and_only_the_words_and_the_reply_are(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    sync_raw(a, env("帮我订个餐厅"))
    assert rows(mem) == [("user", "帮我订个餐厅"), ("assistant", "好的")]
    assert a.skipped == {}


def test_ids_pairing_and_the_token_still_use_the_original_text(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    raw = env("帮我订个餐厅")
    turn(a, 1, raw, author_id="u1")
    token = a._cur["hs1"]
    a.sync_turn(raw, "好的", session_id="hs1", messages=[{"role": "user", "content": raw}, {"role": "assistant", "content": "好的"}])
    ids = [r[0] for r in mem.store.conn.execute("SELECT host_msg_id FROM messages WHERE host='hermes' AND session_id='hs1' ORDER BY seq")]
    assert ids[0] == token + "-u" and ids[1].startswith(token + "-a-")


def test_several_envelopes_joined_together_are_stored_as_today(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    raw = env(C.merge_redirects(env("甲"), env("乙")))                                            # 去掉第一份以后里面还有第二份：bad
    status, _rest, _bot = HM._strip_envelope(raw)
    assert status == "bad"
    sync_raw(a, raw)
    assert rows(mem)[0] == ("user", raw)


def test_a_bad_json_or_leftover_note_is_stored_as_today(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    raw = env("坏的").replace('{"platform"', '{"platform" BROKEN')
    sync_raw(a, raw)
    assert rows(mem)[0] == ("user", raw)


def test_a_message_from_a_bot_is_not_recorded(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    sync_raw(a, env("机器人说的", from_bot=True))
    assert rows(mem) == [] and a.skipped == {"bot_author": 1}


def test_nothing_left_after_stripping_is_stored_as_today(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    raw = env("   ")
    sync_raw(a, raw)
    assert rows(mem)[0] == ("user", raw)


def test_a_shared_chat_does_not_strip(tmp_path):
    mem, a = _chat_adapter(tmp_path, platform="matrix", chat_type="room", chat_id="r1")
    raw = env("帮我订个餐厅")
    sync_raw(a, raw)
    assert rows(mem)[0] == ("user", raw)


def test_a_local_platform_never_strips_what_the_user_typed(tmp_path):
    mem, a = mk(tmp_path)
    raw = env("我自己打的")
    sync_raw(a, raw)
    assert rows(mem)[0] == ("user", raw)


def test_a_plain_chat_turn_without_an_envelope_is_unchanged(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    sync_raw(a, "帮我订个餐厅")
    assert rows(mem) == [("user", "帮我订个餐厅"), ("assistant", "好的")]


def test_a_redirect_that_was_never_applied_and_became_the_opening_is_stripped_and_pairs(tmp_path):
    """/retry 一条改口：开头就是来源说明，再接上宿主的「User correction」——开头那句去掉说明再存（今天存的是带账号编号的整段）。"""
    mem, a = _chat_adapter(tmp_path)
    h = Host(env("帮我订个餐厅")).redirect("要靠窗的位子").final()
    turn(a, 1, h.opening, author_id="u1")
    a.sync_turn(h.user, h.reply, session_id="hs1", messages=h.m)
    users = [c for r, c in rows(mem) if r == "user"]
    assert users and users[0].startswith("帮我订个餐厅") and "Gateway message origin" not in users[0] and '"user_id"' not in users[0]


# ------------------------------------------------------------------ 口令：说明后面第一行是 #yuanwei-nomem 也要关；只认关
def state(a):
    return a.win.view("hermes", "hs1").state


def test_the_close_command_after_the_envelope_closes_the_window(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    turn(a, 1, env("#yuanwei-nomem\n帮我订个餐厅"), author_id="u1")
    assert state(a) == "off"


def test_without_the_envelope_rule_the_same_text_does_not_close(tmp_path):
    """对照：说明开头、口令不在第一行——今天就是不关（整句按第一行认口令）。共用会话里这条规则不适用，仍不关。"""
    mem, a = _chat_adapter(tmp_path, platform="matrix", chat_type="room", chat_id="r1")
    turn(a, 1, env("#yuanwei-nomem\n帮我订个餐厅"), author_id="u1")
    assert state(a) == "on"


def test_open_and_confirm_after_the_envelope_do_not_count(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    turn(a, 1, "#yuanwei-nomem", author_id="u1")
    assert state(a) == "off"
    for n, word in enumerate(("#yuanwei-mem", "确认开启"), start=2):
        turn(a, n, env(word), author_id="u1")
        assert state(a) == "off", word


def test_a_bot_envelope_does_not_close_through_this_rule(tmp_path):
    mem, a = _chat_adapter(tmp_path)
    turn(a, 1, env("#yuanwei-nomem", from_bot=True), author_id="u1")
    assert state(a) == "on"
