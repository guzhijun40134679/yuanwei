"""清单第 56 条第一批·第 3 节：轮次号不倒退。同一个事件重送、进程重启、崩溃重来还是同一个号；那一轮已经结束了再来同样的话（重新生成、/retry、压缩换号）是新的一轮，旧的一个字不变。
假宿主，临时目录，不联网。"""
import time

import pytest

from helpers2 import organized
from test_deletion_chat import n_deletions, request, setup_chat, talk
from test_hermes_adapter import call, mk, msgs, turn
from yuanwei.access import Access
from yuanwei.hermes_adapter import HermesAdapter


def rows(mem):
    return [tuple(r) for r in mem.store.conn.execute("SELECT msg_id, host_msg_id, role, content, seq, session_id FROM messages WHERE host='hermes' AND session_id!='S1' ORDER BY seq")]


def sync(a, text, reply, session="hs1"):
    a.sync_turn(text, reply, session_id=session, messages=msgs(text))


# ================================================================== Access.event_turn
def test_event_turn_same_key_same_token_until_the_turn_ends_then_a_new_one_each_time(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1 = acc.event_turn("hermes", "S", "1:abc")[0]
    assert acc.event_turn("hermes", "S", "1:abc")[0] == t1                                    # 重送
    assert Access(mem).event_turn("hermes", "S", "1:abc")[0] == t1                            # 进程重启
    assert acc.turn_token("hermes", "S", "1:abc") == t1                                    # 老接口看到的是同一个号
    acc.begin_turn("hermes", "S", t1, text="话")
    assert acc.event_turn("hermes", "S", "1:abc")[0] == t1                                    # 开了轮、还没记、没有更晚的轮：还是它
    mem.record("hermes", "S", t1 + "-u", "user", "话", time="2027-02-01", turn_id=t1)      # 记下了：结束
    t2 = acc.event_turn("hermes", "S", "1:abc")[0]
    assert t2 != t1 and acc.event_turn("hermes", "S", "1:abc")[0] == t2
    keys = [r[0] for r in mem.store.conn.execute("SELECT event_key FROM turn_tokens WHERE session_id='S' ORDER BY created_at, rowid")]
    assert keys == ["1:abc", "1:abc\x1f2"]
    mem.record("hermes", "S", t2 + "-u", "user", "话", time="2027-02-01", turn_id=t2)
    t3 = acc.event_turn("hermes", "S", "1:abc")[0]
    assert t3 not in (t1, t2)
    assert [r[0] for r in mem.store.conn.execute("SELECT event_key FROM turn_tokens WHERE session_id='S' ORDER BY rowid")][-1] == "1:abc\x1f3"
    assert acc.event_turn("hermes", "S", "other")[0] not in (t1, t2, t3)
    assert acc.event_turn("hermes", "S2", "1:abc")[0] not in (t1, t2, t3)


def test_event_turn_a_later_turn_ends_the_earlier_one_even_without_recorded_messages(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1 = acc.event_turn("hermes", "S", "1:abc")[0]
    acc.begin_turn("hermes", "S", t1, text="话")
    t2 = acc.event_turn("hermes", "S", "2:def")[0]
    acc.begin_turn("hermes", "S", t2, text="下一句")
    n = acc.event_turn("hermes", "S", "1:abc")[0]
    assert n != t1                                                                        # 有更晚的轮：结束了（这一轮自己没记下东西也算）
    other = acc.event_turn("hermes", "OTHER", "1:abc")[0]
    acc.begin_turn("hermes", "OTHER", other, text="话")
    assert acc.event_turn("hermes", "OTHER", "1:abc")[0] == other                            # 别的会话里的轮不算


def test_event_turn_messages_bound_in_any_session_end_the_turn_compression_renumbering(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1 = acc.event_turn("hermes", "S-new", "3:abc")[0]
    mem.store.conn.execute("INSERT INTO turn_messages (ns,host,session_id,branch,turn_id,msg_id) VALUES (?,?,?,?,?,?)", (mem.ns, "hermes", "S-old", "", t1, "m-x"))
    assert acc.event_turn("hermes", "S-new", "3:abc")[0] != t1


def test_event_turn_delete_confirm_turn_never_counts_as_ended(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    t1 = acc.event_turn("hermes", "S", "4:abc")[0]
    mem.store.conn.execute("INSERT INTO no_record_turns (turn_id, host, window_id, reason, del_id, capture_seq) VALUES (?,?,?,?,?,?)", (t1, "hermes", None, "delete_confirm", "d-1", 1))
    mem.store.conn.execute("INSERT INTO turn_messages (ns,host,session_id,branch,turn_id,msg_id) VALUES (?,?,?,?,?,?)", (mem.ns, "hermes", "S", "", t1, "m-x"))
    acc.begin_turn("hermes", "S", t1, text="x")
    t2 = acc.event_turn("hermes", "S", "5:zzz")[0]
    acc.begin_turn("hermes", "S", t2, text="y")
    assert acc.event_turn("hermes", "S", "4:abc")[0] == t1


def test_event_turn_rejects_the_separator_and_bad_arguments(tmp_path):
    acc = Access(organized(tmp_path))
    for bad in ("a\x1fb", "\x1f2", ""):
        with pytest.raises(ValueError):
            acc.event_turn("hermes", "S", bad)[0]
    with pytest.raises(ValueError):
        acc.event_turn("", "S", "k")[0]


# ================================================================== 接头里
def test_same_hook_twice_restart_and_crash_before_the_reply_keep_the_same_token(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "问个问题")
    t1 = a._cur["hs1"]
    turn(a, 1, "问个问题")                                                                # 同一个钩子连调两次
    assert a._cur["hs1"] == t1 and len(a._pending["hs1"]) == 1
    b = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")  # 进程重启：回复前崩了，重启后同一个事件再来
    b.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(b, 1, "问个问题")
    assert b._cur["hs1"] == t1
    sync(b, "问个问题", "答案")
    assert [r[2] for r in rows(mem)] == ["user", "assistant"] and mem.store.conn.execute("SELECT COUNT(DISTINCT turn_id) FROM turn_messages").fetchone()[0] == 1


def test_regenerate_keeps_both_pairs_and_the_old_one_does_not_change_a_byte(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "给我讲个笑话")
    sync(a, "给我讲个笑话", "第一版笑话")
    first = rows(mem)
    assert len(first) == 2
    turn(a, 1, "给我讲个笑话")                                                            # 重新生成：宿主又发了一次同样的话、同一个轮次号
    sync(a, "给我讲个笑话", "第二版笑话")
    now = rows(mem)
    assert now[:2] == first                                                               # 旧的一对一个字不变（编号、文字、顺序号）
    assert [(r[2], r[3]) for r in now] == [("user", "给我讲个笑话"), ("assistant", "第一版笑话"), ("user", "给我讲个笑话"), ("assistant", "第二版笑话")]
    assert mem.store.conn.execute("SELECT COUNT(DISTINCT turn_id) FROM turn_messages").fetchone()[0] == 2
    assert a.recorded_turns == 2 and a.skipped == {}


def test_retry_a_second_later_is_a_new_pair_too(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 2, "再来一次")
    sync(a, "再来一次", "答一")
    time.sleep(1.1)                                                                       # /retry 隔 1 秒以上
    turn(a, 2, "再来一次")
    sync(a, "再来一次", "答二")
    assert [(r[2], r[3]) for r in rows(mem)] == [("user", "再来一次"), ("assistant", "答一"), ("user", "再来一次"), ("assistant", "答二")]


def test_seven_continues_after_compression_are_seven_pairs(tmp_path):
    mem, a = mk(tmp_path)
    cur = "hs1"
    for i in range(7):
        turn(a, 3, "继续")                                                                # 压缩以后轮次号和话都一样
        a.sync_turn("继续", "回复%d" % i, session_id=cur, messages=msgs("继续"))
        new = "hs-c%d" % i
        a.on_session_switch(new, parent_session_id=cur, reason="compression")
        cur = new
    got = [(r[2], r[3]) for r in rows(mem)]
    assert got == [x for i in range(7) for x in (("user", "继续"), ("assistant", "回复%d" % i))]
    assert a.recorded_turns == 7


def test_seven_continues_with_the_same_session_and_turn_number_are_seven_pairs(tmp_path):
    mem, a = mk(tmp_path)
    for i in range(7):
        turn(a, 3, "继续")                                                                # 会话号没换、轮次号也一样（旧代码里这 7 句只剩一对）
        sync(a, "继续", "回复%d" % i)
    assert [(r[2], r[3]) for r in rows(mem)] == [x for i in range(7) for x in (("user", "继续"), ("assistant", "回复%d" % i))]
    assert a.recorded_turns == 7


def test_delete_confirm_twice_is_not_recorded_and_not_deleted_again(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    assert n_deletions(mem) == 1
    a.sync_turn("确认删除", "已删除", session_id="hs1", messages=msgs("确认删除"))
    talk(a, 5, "删完以后又说了一句")
    turn(a, 4, "确认删除")                                                              # 隔一轮再来同样的「确认删除」、同样的轮次号
    a.sync_turn("确认删除", "好的", session_id="hs1", messages=msgs("确认删除"))
    assert n_deletions(mem) == 1
    assert "确认删除" not in [r[3] for r in rows(mem)]
    assert "删完以后又说了一句" in [r[3] for r in rows(mem)]


def test_event_key_renamed_by_a_deletion_works_as_before(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    assert n_deletions(mem) == 1
    a.sync_turn("确认删除", "已删除", session_id="hs1", messages=msgs("确认删除"))
    talk(a, 1, "今天聊聊我养的猫")                                                       # 删除把旧键换成假名；同样的话同样的号：新的一轮，照常记
    assert [(r[2], r[3]) for r in rows(mem) if r[5] == "hs1"] == [("user", "今天聊聊我养的猫"), ("assistant", "好的")]


# ================================================================== 竞争兜底
def _two_pending_with_one_token(a, text1, text2):
    turn(a, 1, text1)
    ent = a._pending["hs1"][0]
    from yuanwei.access import Access as A
    a._pending["hs1"].append(dict(ent, fp=A.text_fp(text2)))                              # 两个同步抢同一个轮次号
    return ent["token"]


@pytest.mark.parametrize("first", [1, 2])
def test_two_syncs_racing_on_one_token_both_pairs_are_recorded_in_either_order(tmp_path, first):
    mem, a = mk(tmp_path)
    tok = _two_pending_with_one_token(a, "问一", "问二")
    # 让第二个同步的用户句和第一个撞在同一个消息编号上、内容不同：把用户句换成不同的文字（模拟提问卡回答等让存下的内容不一样）
    order = [("问一", "答一"), ("问二", "答二")]
    if first == 2:
        order.reverse()
    # 先造一条同编号、不同内容的原话，等于另一个同步已经落库
    mem.record("hermes", "hs1", "%s-u" % tok, "user", "另一个同步先落的那一句", time="2027-02-01", turn_id=tok)
    for text, reply in order:
        sync(a, text, reply)
    got = [(r[2], r[3]) for r in rows(mem)]
    assert ("user", "另一个同步先落的那一句") in got
    for text, reply in order:
        assert ("user", text) in got and ("assistant", reply) in got                       # 两对都在，一对都没丢
    note = mem.store.conn.execute("SELECT kind, detail FROM read_log WHERE kind='recorded_under_new_token'").fetchall()
    assert len(note) == 2 and all("message_conflict" in r[1] for r in note)               # 每次改用新号都备注一笔
    assert a.recorded_turns == 2 and a.skipped == {}
    ids = {r[0] for r in mem.store.conn.execute("SELECT DISTINCT turn_id FROM turn_messages")}
    assert tok in ids and len(ids) == 3
    assert mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens WHERE session_id='hs1'").fetchone()[0] == 1                    # 本地新号不写库里的 turn_tokens


def test_only_message_conflict_takes_the_new_token_path_other_errors_stay_as_before(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "问个问题")
    real = mem.record

    def boom(*x, **k):
        raise RuntimeError("别的错")
    monkeypatch.setattr(mem, "record", boom)
    with pytest.raises(RuntimeError):
        sync(a, "问个问题", "答")
    assert a.skipped == {"record_error": 1}
    monkeypatch.setattr(mem, "record", real)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='recorded_under_new_token'").fetchone()[0] == 0


def test_blocked_by_a_deletion_barrier_is_still_just_skipped(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    a.sync_turn("确认删除", "已删除", session_id="hs1", messages=msgs("确认删除"))
    assert a.skipped.get("delete_confirm_turn") == 1
    assert mem.store.conn.execute("SELECT COUNT(*) FROM read_log WHERE kind='recorded_under_new_token'").fetchone()[0] == 0
