"""补充施工单一·第 1 节：封步正好卡阈值（各差一的两边）；跨工作区的续读游标。"""
import pytest

from helpers import Clock, Scripted, make_mem, say
from yuanwei import sealing as SE
from yuanwei.errors import BadCursor


def turn(n_msgs, chars_each=1):
    msgs = [{"role": "user" if i == 0 else "assistant", "content": "x" * chars_each, "seq": i} for i in range(n_msgs)]
    return {"msgs": msgs, "complete": True, "aborted": False}


def shape(steps):
    return [[len(t["msgs"]) for t in s] for s in steps]


# ---------------------------------------------------------------- 纯函数：条数阈值 40
def test_count_threshold_exact_and_off_by_one():
    # 累计：19 轮×2＝38 条，再来 1 轮 2 条＝40 → 正好到阈值，在这一轮之后封
    steps, rest = SE.pack_turns([turn(2)] * 20, 40, 8000, final=False)
    assert shape(steps) == [[2] * 20] and rest == []
    # 差一：19 轮（38 条）不封；39 条（18 轮×2＋1 轮 3 条）也不封
    steps, rest = SE.pack_turns([turn(2)] * 19, 40, 8000, final=False)
    assert steps == [] and len(rest) == 19
    steps, rest = SE.pack_turns([turn(2)] * 18 + [turn(3)], 40, 8000, final=False)
    assert steps == [] and sum(len(t["msgs"]) for t in rest) == 39
    # 多一：41 条（20 轮×2＋1 轮 1 条）在第 20 轮时就到了 40，封；多出的 1 条留着
    steps, rest = SE.pack_turns([turn(2)] * 20 + [turn(1)], 40, 8000, final=False)
    assert shape(steps) == [[2] * 20] and shape([rest]) == [[1]]


def test_single_turn_exactly_at_count_threshold_is_its_own_step():
    # 前面攒着一个小轮次；这一轮自己正好 40 条：前面的先封，这一轮单独一步
    steps, rest = SE.pack_turns([turn(2), turn(40)], 40, 8000, final=False)
    assert shape(steps) == [[2], [40]] and rest == []
    # 差一：这一轮 39 条，不算自己到阈值；它和前面的小轮次攒在一起（2＋39＝41 ≥ 40，在这一轮之后一起封）
    steps, rest = SE.pack_turns([turn(2), turn(39)], 40, 8000, final=False)
    assert shape(steps) == [[2, 39]] and rest == []
    # 差一：39 条的轮次单独来，不到阈值，不封
    steps, rest = SE.pack_turns([turn(39)], 40, 8000, final=False)
    assert steps == [] and len(rest) == 1


# ---------------------------------------------------------------- 纯函数：字数阈值 8000
def test_chars_threshold_exact_and_off_by_one():
    big = lambda c: turn(2, c // 2)                       # 一轮两条，共 c 字
    steps, rest = SE.pack_turns([big(3000), big(3000), big(2000)], 40, 8000, final=False)
    assert shape(steps) == [[2, 2, 2]]                    # 3000＋3000＋2000＝8000：正好到，封
    steps, rest = SE.pack_turns([big(3000), big(3000), big(1998)], 40, 8000, final=False)
    assert steps == [] and len(rest) == 3                 # 7998：不封
    steps, rest = SE.pack_turns([big(3000), big(3000), big(2000), big(100)], 40, 8000, final=False)
    assert shape(steps) == [[2, 2, 2]] and len(rest) == 1
    # 单独一轮正好 8000：自己一步，前面攒着的先封；7998 的单独一轮不算
    steps, rest = SE.pack_turns([big(100), big(8000)], 40, 8000, final=False)
    assert shape(steps) == [[2], [2]]
    steps, rest = SE.pack_turns([big(100), big(7998)], 40, 8000, final=False)
    assert shape(steps) == [[2, 2]]                       # 100＋7998＝8098 ≥ 8000：一起封，不是各自一步
    # final（会话结束／空闲到期）：没到阈值的剩余也封
    steps, rest = SE.pack_turns([big(100)], 40, 8000, final=True)
    assert shape(steps) == [[2]] and rest == []


# ---------------------------------------------------------------- 经 Memory 走一遍
def test_memory_seals_exactly_at_40_messages_but_not_at_38(tmp_path):
    mem = make_mem(tmp_path, name="a.sqlite", provider=Scripted("skip"))
    say(mem, "S1", 38)                                     # 19 轮
    assert mem.store.list_steps("w") == []
    say(mem, "S1", 2, start=39)                            # 第 40 条到了（第 20 轮的回复）
    steps = mem.store.list_steps("w")
    assert len(steps) == 1 and len(steps[0]["msg_ids"]) == 40 and mem.store.count_messages("w", "step_id IS NULL") == 0


def test_memory_seals_exactly_at_8000_chars(tmp_path):
    def feed(last_len):
        mem = make_mem(tmp_path, name="c%d.sqlite" % last_len, provider=Scripted("skip"))
        for i, n in enumerate((1500, 1500, 1500, 1500, 1500, 500 + last_len - 500)):     # 前五条 7500 字
            mem.record("h", "S1", "c%d" % i, "user" if i % 2 == 0 else "assistant", "字" * n)
        return mem
    mem = feed(500)                                       # 7500＋500＝8000：正好，在轮次（第 3 轮的回复）之后封
    assert len(mem.store.list_steps("w")) == 1
    mem2 = feed(499)                                      # 7999：不封
    assert mem2.store.list_steps("w") == []


def test_memory_single_turn_exactly_at_char_threshold(tmp_path):
    mem = make_mem(tmp_path, name="d.sqlite", max_step_chars=100, provider=Scripted("skip"))
    mem.record("h", "S1", "a1", "user", "前" * 10)
    mem.record("h", "S1", "a2", "assistant", "前" * 10)
    mem.record("h", "S1", "b1", "user", "大" * 50)
    mem.record("h", "S1", "b2", "assistant", "大" * 50)       # 这一轮正好 100 字
    assert [len(s["msg_ids"]) for s in mem.store.list_steps("w")] == [2, 2]
    mem2 = make_mem(tmp_path, name="e.sqlite", max_step_chars=100, provider=Scripted("skip"))
    mem2.record("h", "S1", "a1", "user", "前" * 10)
    mem2.record("h", "S1", "a2", "assistant", "前" * 10)
    mem2.record("h", "S1", "b1", "user", "大" * 50)
    mem2.record("h", "S1", "b2", "assistant", "大" * 49)       # 99 字：不算自己到阈值；和前面的 20 字攒在一起＝119 ≥ 100，一起封
    assert [len(s["msg_ids"]) for s in mem2.store.list_steps("w")] == [4]


# ---------------------------------------------------------------- 跨工作区的游标
def _two_workspaces(tmp_path):
    ma = make_mem(tmp_path, ws="甲", name="s.sqlite", provider=Scripted("skip"), recall_page_chars=300, get_messages_page_tokens=60)
    mb = make_mem(tmp_path, ws="乙", name="s.sqlite", provider=Scripted("skip"), recall_page_chars=300, get_messages_page_tokens=60)
    for mem, tag in ((ma, "甲"), (mb, "乙")):
        for i in range(1, 21):
            mem.record("h", "S" + tag, "p%d" % i, "user" if i % 2 else "assistant", "%s库的榴莲酥第%d条：专门用来分页的一句比较长的话。" % (tag, i), time="2027-03-01")
        mem.end_session("h", "S" + tag)
        mem.run()
    return ma, mb


def test_recall_cursor_from_another_workspace_is_rejected_and_delivers_nothing(tmp_path):
    ma, mb = _two_workspaces(tmp_path)
    page = ma.recall("榴莲酥")
    assert page["cursor"] and page["delivered"]
    with pytest.raises(BadCursor):
        mb.recall(cursor=page["cursor"])
    # 乙里自己的游标照样能用，甲的游标在甲里也照样能用（拒绝不影响本工作区）
    assert mb.recall("榴莲酥")["cursor"]
    assert ma.recall(cursor=page["cursor"])["delivered"]


def test_get_messages_cursor_from_another_workspace_is_rejected(tmp_path):
    ma, mb = _two_workspaces(tmp_path)
    ids = [d["msg_id"] for d in ma.recall("榴莲酥")["delivered"]][:6]
    page = ma.get_messages(ids)
    assert page["truncated"] and page["cursor"]
    with pytest.raises(BadCursor):
        mb.get_messages(cursor=page["cursor"])


def test_cursor_kinds_are_not_interchangeable(tmp_path):
    ma, _ = _two_workspaces(tmp_path)
    rc = ma.recall("榴莲酥")["cursor"]
    ids = [d["msg_id"] for d in ma.recall("榴莲酥")["delivered"]][:6]
    gm = ma.get_messages(ids)["cursor"]
    with pytest.raises(BadCursor):
        ma.get_messages(cursor=rc)
    with pytest.raises(BadCursor):
        ma.recall(cursor=gm)
