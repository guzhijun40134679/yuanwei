"""V9：已入库的原话不等整理就能查到（新截点格式：原话上界 raw_seq、整理上界 org_seq）；旧格式截点照旧；整理提交后即对新截点可见；
状态说法按截点；本轮排除（可信接头给出的当前轮消息集合）。全部自造数据、假提供方、临时库。"""
import pytest

from helpers import Clock, Scripted, make_mem, say
from helpers2 import hybrid
from yuanwei import contract as WC
from yuanwei import hosts as H
from yuanwei.access import Access
from yuanwei.ask import RetryableNetworkError
from yuanwei.idsource import InProcessIdentity
from yuanwei.modes import ModeConfig, ModeRunner
from yuanwei.storeerr import StoreError
from yuanwei.tools import ToolSurface


def legacy_cut(mem):
    """旧格式截点（只有 vclock、org_seq、index_version）：用来核旧格式照旧。"""
    return dict(mem.store.cut(mem.ns), index_version=mem.index_version)


def delivered_ids(out):
    return [d["msg_id"] for d in out["delivered"]]


# ------------------------------------------------------------------ 1 未封步就能读；整理失败、暂停也能读
def test_unsealed_original_is_readable_by_keyword_and_by_id(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ids = say(mem, "S1", 4, prefix="葡萄")
    assert mem.message_status(ids[0]) == "已保存"
    out = mem.recall("葡萄")
    assert ids[0] in delivered_ids(out)
    assert "还没整理" not in out["text"] and "未整理" not in out["text"]          # 交给回答模型的材料正文里不加「还没整理」的字样，只在工具元数据里写明
    assert mem.get_messages([ids[0]])["messages"][0]["msg_id"] == ids[0]
    a = mem.attachments(ids[0])
    assert a["organization"]["state"] == "no_receipt_in_cut" and "本截点尚无整理结果" in a["note"]


def _network_down(request):
    raise RetryableNetworkError("断网")


@pytest.mark.parametrize("how", ["abandoned", "failed", "paused"])
def test_sealed_but_failed_abandoned_or_paused_steps_are_still_readable(tmp_path, how):
    if how == "abandoned":
        mem = make_mem(tmp_path, provider=Scripted("garbage"))
    elif how == "failed":
        mem = make_mem(tmp_path, provider=_network_down)
    else:
        mem = make_mem(tmp_path, provider=Scripted("skip"), daily_cap_cny=0.0000001)
    ids = say(mem, "S1", 4, prefix="榴莲")
    mem.end_session("h", "S1")
    r = mem.run()
    st = mem.store.get_step("w", "step-0001")["status"]
    assert st == {"abandoned": "abandoned", "failed": "failed", "paused": "sealed"}[how], (how, st, r)
    assert ids[0] in delivered_ids(mem.recall("榴莲"))
    assert mem.get_messages([ids[1]])["messages"][0]["msg_id"] == ids[1]


# ------------------------------------------------------------------ 2 旧截点（新格式或旧格式）后来入库、后来整理都稳定
def test_a_cut_stays_stable_after_later_ingest_and_later_organization(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    ids1 = say(mem, "S1", 4, prefix="苹果")
    cut1 = mem.cut()
    assert cut1["cut_format"] == 2 and cut1["raw_seq"] == 4 and cut1["org_seq"] == 0 and cut1["ns"] == "w" and "vclock" not in cut1
    a0 = mem.attachments(ids1[0], cut=cut1)
    assert a0["organization"]["state"] == "no_receipt_in_cut"
    mem.end_session("h", "S1")
    mem.run()
    ids2 = say(mem, "S2", 2, prefix="仙人掌")
    mem.end_session("h", "S2")
    mem.run()
    assert mem.attachments(ids1[0], cut=cut1) == a0                          # 整理做完了，旧截点上还是「尚无整理结果」，不混进后来的状态
    assert not any(m in delivered_ids(mem.recall("仙人掌", cut=cut1)) for m in ids2)
    cut2 = mem.cut()
    a2 = mem.attachments(ids1[0], cut=cut2)
    assert a2["organization"]["state"] == "processed" and a2["sentences"]
    assert any(m in delivered_ids(mem.recall("仙人掌", cut=cut2)) for m in ids2)


# ------------------------------------------------------------------ 3 整理提交以后、end_step 之前读取；进程中途退出再恢复
def test_organization_is_visible_between_commit_and_end_step_and_recovers(tmp_path):
    clock = Clock()
    prov = Scripted("thread")
    mem = make_mem(tmp_path, provider=prov, clock=clock)
    ids = say(mem, "S1", 4, prefix="香蕉")
    mem.end_session("h", "S1")
    real_end = mem.store.end_step

    def boom(*a, **k):
        raise RuntimeError("进程在整理提交和 end_step 之间退出")
    mem.store.end_step = boom
    with pytest.raises(RuntimeError):
        mem.run()
    mem.store.end_step = real_end
    assert mem.store.get_step("w", "step-0001")["status"] == "sealed"        # 还没 end_step
    cut = mem.cut()
    assert cut["org_seq"] >= 1
    a = mem.attachments(ids[0], cut=cut)
    assert a["sentences"] and a["organization"]["state"] == "processed"       # 整理已提交，新截点就看得到，不等 end_step
    assert ids[0] in delivered_ids(mem.recall("香蕉"))
    mem.close()
    prov2 = Scripted("thread")
    mem2 = make_mem(tmp_path, provider=prov2, clock=clock)
    assert mem2.run()["done"] == 1 and prov2.calls == []                      # 按收据认出来，不重发
    assert mem2.store.get_step("w", "step-0001")["status"] == "done"
    assert mem2.attachments(ids[0], cut=mem2.cut())["organization"]["state"] == "processed"


# ------------------------------------------------------------------ 4 旧格式截点的含义不变（不连续可见原话的反例）
def test_legacy_cut_keeps_visible_at_semantics_and_is_not_converted_to_a_max_seq(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    a_ids = say(mem, "SA", 2, prefix="甲线")                                     # seq 1、2：没封步
    b_ids = say(mem, "SB", 2, prefix="乙线")                                     # seq 3、4
    mem.end_session("h", "SB")
    mem.run()                                                                   # SB 的步先结束，拿到 vclock=1
    lc = legacy_cut(mem)
    assert lc["vclock"] == 1 and "cut_format" not in lc
    old = delivered_ids(mem.recall("今天聊聊编号", cut=lc))
    assert old and set(old) <= set(b_ids) and not set(old) & set(a_ids)          # 旧格式只看得到 visible_at 在截点内的（不连续）
    assert set(a_ids) & set(delivered_ids(mem.recall("今天聊聊编号")))           # 新格式：已入库的都看得到
    snap = mem._reader.snapshot(lc)
    assert {r["msg_id"] for r in snap.rows} == set(b_ids)


def test_new_format_material_equals_legacy_when_everything_ended_and_nothing_excluded(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    say(mem, "S1", 6, prefix="橘子")
    mem.end_session("h", "S1")
    mem.run()
    say(mem, "S2", 6, prefix="橙子")
    mem.end_session("h", "S2")
    mem.run()
    lc = legacy_cut(mem)
    rd = mem._reader
    for q in ("今天聊聊编号3的事情", "橘子第2句话", "橙子的事情编号5"):
        a, b = rd.select(q, cut=lc), rd.select(q)
        assert b["cut"]["cut_format"] == 2 and "cut_format" not in a["cut"]
        assert a["material"] == b["material"] and [r["msg_id"] for r in a["selected"]] == [r["msg_id"] for r in b["selected"]]
        assert a["hits"] == b["hits"] and a["table"] == b["table"]
        assert a["pack"]["最终"] == b["pack"]["最终"]
    ra = mem.answer_request("橘子第2句话", "2027-03-01", as_of=lc)
    rb = mem.answer_request("橘子第2句话", "2027-03-01")
    assert ra["request"]["bytes"] == rb["request"]["bytes"]


# ------------------------------------------------------------------ 5 调用方自己传截点：不存在、超前、组合不一致的一律拒绝
def test_caller_supplied_new_format_cut_is_validated(tmp_path):
    from yuanwei.errors import BadCut
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    say(mem, "S1", 4, prefix="西瓜")
    mem.end_session("h", "S1")
    mem.run()
    good = mem.cut()
    assert good["org_seq"] >= 1 and good["raw_seq"] == 4
    bad = [dict(good, ns="别的工作区"), dict(good, raw_seq=good["raw_seq"] + 5), dict(good, org_seq=good["org_seq"] + 1),
           dict(good, raw_seq=0),                                                  # 整理记录引用的原话超出了原话上界：两个上界对不上
           dict(good, cut_format=3), {"cut_format": 2}, dict(good, raw_seq=-1), dict(good, raw_seq=True), dict(good, raw_seq="4"), dict(good, index_version=99)]
    for c in bad:
        with pytest.raises(BadCut):
            mem.recall("西瓜", cut=c)
        with pytest.raises(BadCut):
            mem.get_messages(["x"], cut=c)
        with pytest.raises(BadCut):
            mem.attachments("x", cut=c)
    assert mem.recall("西瓜", cut=good)["delivered"]


# ------------------------------------------------------------------ 6 save_message 不许往已经发布过的截点里补洞
def test_save_message_cannot_fill_a_hole_below_the_published_range(tmp_path):
    mem = make_mem(tmp_path)
    store = mem.store

    def msg(seq, mid):
        return WC.make_message("w", "S", seq, mid, "user", "第%d条" % seq, None, "2027-01-01T00:00:00Z")
    assert store.save_message(msg(5, "x5")) == "new"
    before = mem.cut()
    with pytest.raises(StoreError):
        store.save_message(msg(3, "x3"))                                            # 5 已经入库，3 是洞：不许补
    assert store.save_message(msg(5, "x5")) == "duplicate"                          # 同一条重送还是重送
    assert store.save_message(msg(6, "x6")) == "new"
    assert mem.cut()["raw_seq"] == 6 and before["raw_seq"] == 5
    r = mem.record("h", "S", "later", "user", "补写的话，用实际入库时的新序号")
    assert r["seq"] == 7
    assert {m["msg_id"] for m in mem._reader.snapshot(before).rows} == {"x5"}       # 旧截点里没有被塞进东西


# ------------------------------------------------------------------ 7 状态说法按截点；skipped 不再映成「已放弃」
def test_attachments_says_skipped_abandoned_and_long_apart(tmp_path):
    from helpers2 import fill
    # 整理放弃
    m1 = make_mem(tmp_path, ws="ab", name="ab.sqlite", provider=Scripted("garbage"))
    ids = say(m1, "S1", 4)
    m1.end_session("h", "S1")
    assert m1.run()["abandoned"] == 1
    a = m1.attachments(ids[0])
    assert a["organization"]["state"] == "abandoned" and "整理放弃" in a["note"] and "超长" not in a["note"]
    assert m1.message_status(ids[0]) == "已放弃"
    # 整步超长：跳过
    m2 = make_mem(tmp_path, ws="sk", name="sk.sqlite", provider=Scripted("skip"))
    big = m2.record("h", "S1", "b1", "user", "超长。" * 800)["msg_id"]
    m2.end_session("h", "S1")
    assert m2.run()["skipped"] == 1
    b = m2.attachments(big)
    assert b["organization"]["state"] == "skipped_long" and "超长跳过" in b["note"] and "放弃" not in b["note"]
    assert m2.message_status(big) == "已跳过"
    # 混合步里的超长条：不整理，也不是放弃
    m3 = make_mem(tmp_path, ws="mx", name="mx.sqlite", provider=Scripted("thread"))
    m3.record("h", "S1", "a1", "user", "先说一句普通的话。")
    big3 = m3.record("h", "S1", "a2", "assistant", "很长的回复。" * 400)["msg_id"]
    m3.end_session("h", "S1")
    m3.run()
    c = m3.attachments(big3)
    assert c["organization"]["state"] == "long_not_organized" and "超长" in c["note"] and "放弃" not in c["note"]


def test_org_state_processed_without_attachments_is_said_so():
    from yuanwei.reader import message_org_state
    state = {"tables": {"receipts": [{"key": "step:s1", "kind": "step", "step_id": "s1", "k": 1, "commit_seq": 1}], "sentences": [], "attachments": []}}
    row = {"msg_id": "m1", "step_id": "s1", "is_long": 0}
    st = message_org_state(state, row)
    assert st["state"] == "processed_no_attach" and "已处理，本条无挂接" in st["text"]
    st2 = message_org_state({"tables": {"receipts": [], "sentences": [], "attachments": []}}, row)
    assert st2["state"] == "no_receipt_in_cut" and "本截点尚无整理结果" in st2["text"]
    assert message_org_state(state, dict(row, step_id=None))["state"] == "no_receipt_in_cut"
    ab = {"tables": {"receipts": [{"key": "op:o1", "kind": "op", "op": "abandon_step", "step_id": "s1", "k": 1, "commit_seq": 1}], "sentences": [], "attachments": []}}
    sk = {"tables": {"receipts": [{"key": "op:o2", "kind": "op", "op": "skip_step", "step_id": "s1", "k": 1, "commit_seq": 1}], "sentences": [], "attachments": []}}
    assert message_org_state(ab, row)["state"] == "abandoned" and message_org_state(sk, row)["state"] == "skipped_long"


def test_status_marks_queue_and_pause_fields_as_current(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    say(mem, "S1", 2)
    st = mem.status()
    assert st["operational"]["scope"] == "current" and "当前" in st["operational"]["note"]
    assert st["cut"]["cut_format"] == 2
    assert "oldest_not_visible_received_at" not in st["backlog"] and st["backlog"]["oldest_unfinished_received_at"]


# ------------------------------------------------------------------ 8 导出按截点
def test_export_uses_the_cut_and_not_vclock(tmp_path):
    import json
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    ids1 = say(mem, "S1", 2, prefix="桃子")
    mem.end_session("h", "S1")
    mem.run()
    cut1 = mem.cut()
    ids2 = say(mem, "S2", 2, prefix="李子")                                       # 没封步
    p = str(tmp_path / "e.jsonl")
    mem.export(p)
    rows = [json.loads(l) for l in open(p, encoding="utf-8")]
    assert rows[0]["cut"]["cut_format"] == 2
    assert {r["msg_id"] for r in rows if r["type"] == "message"} == set(ids1) | set(ids2)
    assert [r["step_id"] for r in rows if r["type"] == "step"] == ["step-0001"]
    p1 = str(tmp_path / "e1.jsonl")
    mem.end_session("h", "S2")
    mem.run()
    mem.export(p1, as_of=cut1)
    rows1 = [json.loads(l) for l in open(p1, encoding="utf-8")]
    assert {r["msg_id"] for r in rows1 if r["type"] == "message"} == set(ids1) and [r["step_id"] for r in rows1 if r["type"] == "step"] == ["step-0001"]
    assert rows1[0]["cut"] == cut1


# ------------------------------------------------------------------ 9 混合模式缺一条向量就整次报缺，不退回关键词
def test_hybrid_missing_vector_for_a_new_original_is_reported_not_degraded(tmp_path):
    from yuanwei.errors import MissingVector
    mem, _client = hybrid(tmp_path)
    assert mem.recall("预算")["delivered"]
    mem.record("h", "S2", "n1", "user", "刚刚说的新话：预算又变了。")
    with pytest.raises(MissingVector):
        mem.recall("预算")


# ------------------------------------------------------------------ 10 禁读不被新路径绕过
def test_denied_reads_still_blocked_for_unorganized_originals(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ids = say(mem, "S1", 4, prefix="芒果")
    assert mem.recall("芒果", history_allowed=False)["status"] == "refused"
    mem.mark_turn_refused("TX", "S1")
    r = mem.recall("芒果", turn_id="TX", session_id="S1")
    assert r["status"] == "refused" and r["delivered"] == []
    assert mem.get_messages([ids[0]], turn_id="TX", session_id="S1")["status"] == "refused"
    mem.set_read_permission(False)
    assert mem.get_messages([ids[0]])["status"] == "refused" and mem.attachments(ids[0])["status"] == "refused"


# ------------------------------------------------------------------ 11 本轮排除
def _history_and_current(tmp_path, **cfg):
    """会话 C：先有一条历史原话（提到榴莲），再有一条本轮绑定的用户消息（和历史原话紧挨着、同一回，但没有共同的词）。"""
    mem = make_mem(tmp_path, provider=Scripted("thread"), **cfg)
    old = mem.record("h", "C", "ca", "user", "历史里提过一次榴莲的安排。")["msg_id"]
    cur = mem.record("h", "C", "cb", "user", "随便再问问别的话题吧。", turn_id="T1")["msg_id"]
    mem.end_session("h", "C")
    mem.run()
    return mem, old, cur


def test_current_turn_is_not_pulled_back_by_neighbours_or_the_same_occasion(tmp_path):
    mem, old, cur = _history_and_current(tmp_path)
    q = "历史里提过一次榴莲的安排"
    plain = delivered_ids(mem.recall(q))
    assert old in plain and cur in plain, "前提：不排除时，本轮这条会被前后句或同回补料带回来"
    ex = mem.recall(q, host="h", session_id="C", turn_id="T1")
    assert old in delivered_ids(ex) and cur not in delivered_ids(ex)
    assert cur not in mem.recall(q, host="h", session_id="C", turn_id="T1", cut=mem.cut())["text"]
    # 没有轮次上下文的接口（不给 host／session／turn）：不做本轮排除
    assert cur in delivered_ids(mem.recall(q, turn_id="T1"))
    assert cur in delivered_ids(mem.recall(q, host="h", session_id="C"))


def test_same_session_earlier_text_stays_readable_even_when_the_text_is_identical(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    first = mem.record("h", "S", "a", "user", "重复的一句话：香蕉六根。")["msg_id"]
    now = mem.record("h", "S", "b", "user", "重复的一句话：香蕉六根。", turn_id="T9")["msg_id"]
    out = delivered_ids(mem.recall("重复的一句话香蕉六根", host="h", session_id="S", turn_id="T9"))
    assert first in out and now not in out
    assert mem.get_messages([first], host="h", session_id="S", turn_id="T9")["messages"][0]["msg_id"] == first
    from yuanwei.errors import ExcludedThisTurn
    with pytest.raises(ExcludedThisTurn):
        mem.get_messages([now], host="h", session_id="S", turn_id="T9")
    with pytest.raises(ExcludedThisTurn):
        mem.attachments(now, host="h", session_id="S", turn_id="T9")
    # 邻句也不把它带回来
    nb = mem.get_messages([first], before=0, after=3, host="h", session_id="S", turn_id="T9")
    assert [m["msg_id"] for m in nb["messages"]] == [first]


def test_exclusion_is_bound_to_workspace_host_branch_session_and_turn(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    a = mem.record("h", "S", "a", "user", "晴天里的香蕉。", turn_id="T")["msg_id"]
    b = mem.record("h", "S", "b", "user", "雨天里的香蕉。", branch="br", turn_id="T")["msg_id"]
    assert mem.excluded_ids("h", "S", "", "T") == frozenset({a})
    assert mem.excluded_ids("h", "S", "br", "T") == frozenset({b})
    for other in (("h2", "S", "", "T"), ("h", "S2", "", "T"), ("h", "S", "", "T2"), ("h", "S", "zz", "T")):
        assert mem.excluded_ids(*other) == frozenset()
    mem2 = make_mem(tmp_path, ws="w2", name="m.sqlite", provider=Scripted("skip"))       # 同一个库文件里的另一个工作区
    assert mem2.excluded_ids("h", "S", "", "T") == frozenset()
    assert a in delivered_ids(mem.recall("晴天里的香蕉", host="h2", session_id="S", turn_id="T"))


def test_pending_file_original_is_bound_when_it_finally_lands(tmp_path):
    """待补文件里的原话：绑定跟着原话走，补进库的时候一起落下。"""
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("h", "S", "a", "user", "旧的一句香蕉话。")
    mem._append_pending({"host": "h", "session_id": "S", "message_id": "p1", "role": "user", "text": "排队的一句香蕉话。", "time": None, "branch": "", "time_precision": None,
                         "stable_id": None, "turn_id": "T7"})
    assert mem.flush_pending() == 1
    mid = mem.store.message_by_host_key("w", "h", "S", "", "p1")["msg_id"]
    assert mem.excluded_ids("h", "S", "", "T7") == frozenset({mid})


def test_snapshot_cache_and_cursors_are_bound_to_the_exclusion(tmp_path):
    from yuanwei.errors import CursorExcluded
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ids = say(mem, "S", 6, prefix="柿子")
    rd = mem._reader
    s_plain = rd.snapshot()
    victim = ids[4]
    scoped = rd.scoped(frozenset({victim}))
    s_ex = scoped.snapshot()
    assert victim in s_plain.by_id and victim not in s_ex.by_id and victim not in s_ex.visible
    assert victim not in {a.msg_id for a in s_ex.anchors} and victim in {a.msg_id for a in s_plain.anchors}
    assert rd.snapshot() is s_plain and scoped.snapshot() is s_ex                       # 缓存各管各的
    # get_messages 的续读游标：剩下的页里有被排除的 → 要求重新查询
    res = rd.get_messages(ids=ids, page_tokens=40)
    assert res["cursor"]
    with pytest.raises(CursorExcluded):
        scoped.get_messages(cursor=res["cursor"])
    assert rd.get_messages(cursor=res["cursor"])["messages"]                              # 没有排除就照常续读
    first_page_ids = {m["msg_id"] for m in res["messages"]}
    ok = rd.scoped(frozenset(first_page_ids)).get_messages(cursor=res["cursor"])           # 排除的是已经交过的页：剩下的不受影响
    assert ok["messages"]
    # recall 的续读游标
    page = rd.recall("今天聊聊编号", limit_chars=90)
    assert page["has_more"]
    cid = page["cursor"].split(":")[0]
    chunks = rd.store.get_cursor(cid)["data"]["chunks"]
    with pytest.raises(CursorExcluded):
        rd.scoped(frozenset({chunks[-1]["msg_id"]})).recall(None, cursor=page["cursor"])
    assert rd.recall(None, cursor=page["cursor"])["text"]


# ------------------------------------------------------------------ 12 工具、便条、目录：同一条排除规则
def _tool_setup(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    old = mem.record("pi", "S", "o1", "user", "榴莲的独有词：之前聊过。")["msg_id"]
    cur = mem.record("pi", "S", "o2", "user", "榴莲的独有词：这是本轮刚说的。", turn_id="T1")["msg_id"]
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T1")
    acc.begin_turn("pi", "S", "T1")
    return mem, acc, ToolSurface(mem, acc, H.profile("pi"), ids), old, cur


def test_tools_apply_the_current_turn_exclusion_and_mark_the_cut_version(tmp_path):
    mem, acc, surf, old, cur = _tool_setup(tmp_path)
    r = surf.call("recall", {"query": "榴莲的独有词"})
    assert r.status == "ok" and old in [d["msg_id"] for d in r.meta["delivered"]] and cur not in [d["msg_id"] for d in r.meta["delivered"]]
    assert "之前聊过" in r.text and "这是本轮刚说的" not in r.text and "截点 r2·o0" in r.text
    g = surf.call("get_messages", {"ids": [cur]})
    assert g.status == "error" and g.reason == "excluded_this_turn"
    g2 = surf.call("get_messages", {"ids": [old], "after": 3})
    assert g2.status == "ok" and [m["msg_id"] for m in g2.meta["messages"]] == [old]
    a = surf.call("attachments", {"message_id": cur})
    assert a.status == "error" and a.reason == "excluded_this_turn"
    a2 = surf.call("attachments", {"message_id": old})
    assert a2.status == "ok" and "本截点尚无整理结果" in a2.text and "截点 r2·o0" in a2.text
    s = surf.call("status", {})
    assert s.status == "ok" and s.meta["status"]["operational"]["scope"] == "current" and "当前" in s.text


def test_tool_cursor_from_another_turn_cannot_bypass_the_new_exclusion(tmp_path):
    mem, acc, surf, old, cur = _tool_setup(tmp_path)
    # 上一轮（没有绑定任何消息）留下的游标：里面有现在这一轮的消息
    rd = mem._reader
    res = rd.get_messages(ids=[old, cur], page_tokens=14)
    assert res["cursor"], "前提：第一页放不下两条"
    r = surf.call("get_messages", {"cursor": res["cursor"]})
    assert r.status == "error" and r.reason == "cursor_excluded"


def test_note_applies_the_current_turn_exclusion(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("pi", "S", "o1", "user", "榴莲的独有词：之前聊过。")
    mem.record("pi", "S", "o2", "user", "榴莲的独有词：这是本轮刚说的。", turn_id="T1")
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="note"))
    out = r.on_user_prompt("S", "T1", "榴莲的独有词")
    assert out["kind"] == "note" and "之前聊过" in out["inject"] and "这是本轮刚说的" not in out["inject"]
    r2 = ModeRunner(mem, Access(mem), H.profile("pi"), ModeConfig(mode="note"))
    out2 = r2.on_user_prompt("S", "T2", "榴莲的独有词")                                   # 下一轮：上一轮的话是历史了
    assert "这是本轮刚说的" in out2["inject"]


def test_directory_version_and_label_follow_the_cut_format(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    say(mem, "S1", 4, prefix="枇杷")
    mem.end_session("h", "S1")
    mem.run()
    d = mem._reader.directory()
    assert d["version"] == "dir-o%d" % d["cut"]["org_seq"] and "截点 r%d·o%d" % (d["cut"]["raw_seq"], d["cut"]["org_seq"]) in d["text"]
    v1 = mem._reader.directory(cut=legacy_cut(mem))
    assert v1["version"].startswith("dir-v") and "截点 v" in v1["text"]
    say(mem, "S2", 2, prefix="杨梅")                                                     # 只多了没整理的原话：目录版本不变（不用重复注入同样内容的目录）
    assert mem._reader.directory()["version"] == d["version"]


def test_reader_scope_cache_key_does_not_leak_between_exclusions(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ids = say(mem, "S", 4, prefix="杏子")
    rd = mem._reader
    a = rd.scoped(frozenset({ids[0]})).select("杏子第1句话")
    b = rd.select("杏子第1句话")
    assert ids[0] not in [r["msg_id"] for r in a["selected"]] and ids[0] in [r["msg_id"] for r in b["selected"]]


def test_workspaces_read_with_their_own_cuts(tmp_path):
    a = make_mem(tmp_path, ws="wa", name="x.sqlite", provider=Scripted("skip"))
    b = make_mem(tmp_path, ws="wb", name="x.sqlite", provider=Scripted("skip"))
    say(a, "S", 5)
    say(b, "S", 2)
    ca, cb = a.cut(), b.cut()
    assert (ca["ns"], ca["raw_seq"]) == ("wa", 5) and (cb["ns"], cb["raw_seq"]) == ("wb", 2)
    from yuanwei.errors import BadCut
    with pytest.raises(BadCut):
        b.recall("今天", cut=ca)                                                         # 别的工作区的截点不能拿来用


# ------------------------------------------------------------------ 13 收据、绑定入口、迁移、分页沿用截点
def test_receipt_says_whether_the_original_is_readable_now(tmp_path, monkeypatch):
    import sqlite3
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    r = mem.record("h", "S", "a", "user", "阿尔法已入库。")
    assert r["raw_saved"] is True and r["readable"] is True
    mem.store.conn.execute("PRAGMA busy_timeout=30")
    blocker = sqlite3.connect(str(tmp_path / "m.sqlite"), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    q = mem.record("h", "S", "r", "user", "贝塔忙时记录。")
    blocker.execute("ROLLBACK")
    blocker.close()
    assert q["pending"] is True and q["raw_saved"] is False and q["readable"] is False and "没有入库" in q["readable_note"]
    assert mem.recall("贝塔忙时记录")["delivered"] == []                    # 待补里的话现在查不到：回执没有冒称能查
    assert mem.flush_pending() == 1
    assert mem.recall("贝塔忙时记录")["delivered"]                          # 补进库以后才读得到


def test_record_rejects_a_bad_turn_id(tmp_path):
    mem = make_mem(tmp_path)
    for bad in ("", 5, ["T"]):
        with pytest.raises(ValueError):
            mem.record("h", "S", "a", "user", "话", turn_id=bad)


def test_resending_the_same_message_adds_the_turn_binding_once(tmp_path):
    mem = make_mem(tmp_path)
    a = mem.record("h", "S", "a", "user", "同一句话。")["msg_id"]
    assert mem.excluded_ids("h", "S", "", "T") == frozenset()
    r = mem.record("h", "S", "a", "user", "同一句话。", turn_id="T")
    assert r["duplicate"] is True and mem.excluded_ids("h", "S", "", "T") == frozenset({a})
    mem.record("h", "S", "a", "user", "同一句话。", turn_id="T")
    assert mem.store.conn.execute("SELECT COUNT(*) FROM turn_messages").fetchone()[0] == 1


def test_a_v8_db_upgrades_to_v9_and_keeps_its_data(tmp_path, monkeypatch):
    import yuanwei.sqlstore as SQ
    p = str(tmp_path / "v8.sqlite")
    from helpers import make_old_db
    import sqlite3
    make_old_db(p, 8, content="八版库里的一句话", msg_time=None)
    c = sqlite3.connect(p)
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='turn_messages'").fetchone()
    c.close()
    mem2 = make_mem(tmp_path, name="v8.sqlite")
    assert mem2.store.schema_version() == 12 and mem2.store.conn.execute("SELECT name FROM sqlite_master WHERE name='turn_messages'").fetchone()
    assert [r["content"] for r in mem2.store.list_messages("w")] == ["八版库里的一句话"]
    assert mem2.recall("八版库里的一句话")["delivered"]                              # 旧库打开能用，原话不等整理就能读


def test_get_messages_page_retries_keep_the_first_cut(tmp_path):
    from helpers import make_mem as mk2
    from yuanwei import paging as PG
    from test_toolsurface import small_profile
    mem = mk2(tmp_path, provider=Scripted("skip"))
    mid = mem.record("h", "S", "a", "user", "一条比较长的话，要被截成几段才放得下这家宿主的单次上限；这一句是为了让它再长一些，好超出一点点。")["msg_id"]
    rd = mem._reader
    cuts = []
    orig = rd.get_messages

    def spy(ids, before, after, cursor, cut, encode, page_tokens=None):
        cuts.append(cut)
        out = orig(ids, before, after, cursor, cut, encode, page_tokens=page_tokens)
        if len(cuts) == 1:                                                              # 第一次取完，库里又进来一条新原话：后面重试不能换成更新的截点
            mem.record("h", "S", "b", "user", "取到一半时新来的话。")
        return out
    rd.get_messages = spy
    path = small_profile("pi", cp=130).paths["mcp_return"]
    try:
        with pytest.raises(PG.ReturnTooLarge):                                           # 这个上限怎么缩都装不下：只看重试用的截点
            PG.get_messages_fit(rd, [mid], 0, 0, None, path)
    finally:
        rd.get_messages = orig
    assert len(cuts) >= 3, "前提：第一次装不下，一路重试"
    assert cuts[0] is None and all(c is not None and c["raw_seq"] == 1 for c in cuts[1:]), cuts


def test_hermes_adapter_binds_the_turn_of_the_messages_it_records(tmp_path):
    from test_hermes_adapter import mk, msgs, turn
    mem, a = mk(tmp_path)
    turn(a, 1, "吃了什么烤鱼")
    token = a._cur["hs1"]
    a.sync_turn("吃了什么烤鱼", "吃了烤鱼", session_id="hs1", messages=msgs("吃了什么烤鱼"))
    mine = {r["msg_id"] for r in mem.store.list_messages(mem.ns, "hs1")}
    assert len(mine) == 2 and mem.excluded_ids("hermes", "hs1", "", token) == frozenset(mine)
    assert mem.excluded_ids("hermes", "hs1", "", "别的轮") == frozenset()


# ------------------------------------------------------------------ 14 目录、多个工作区
def test_directory_does_not_show_the_current_turn_as_the_latest_related_message(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    old = mem.record("h", "C", "ca", "user", "早一点的话，说的是橘子园的事。", time="2027-01-05")["msg_id"]
    cur = mem.record("h", "C", "cb", "user", "这一轮刚说的，橘子园又提了一次。", time="2027-01-09", turn_id="T1")["msg_id"]
    mem.end_session("h", "C")
    mem.run()
    plain = mem._reader.directory()["text"]
    assert "2027-01-09" in plain and cur in plain
    ex = mem._reader.scoped(mem.excluded_ids("h", "C", "", "T1")).directory()["text"]
    assert "2027-01-09" not in ex and "2027-01-05" in ex and old in ex and cur not in ex


def test_each_workspace_in_a_multi_workspace_read_uses_its_own_binding_and_cut(tmp_path):
    life = make_mem(tmp_path, ws="life", name="x.sqlite", provider=Scripted("skip"))
    pa = make_mem(tmp_path, ws="proj:a", name="x.sqlite", provider=Scripted("skip"))
    life.record("hermes", "S", "o1", "user", "榴莲的独有词：生活里之前聊过。")
    life.record("hermes", "S", "o2", "user", "榴莲的独有词：生活里本轮刚说的。", turn_id="T1")
    pa.record("hermes", "S", "p1", "user", "榴莲的独有词：甲项目里的一句，会话和轮次编号碰巧一样。")           # 别的工作区没有绑定：不能因为编号碰巧相同被排除
    pa.record("hermes", "S", "p2", "user", "榴莲的独有词：甲项目里本轮刚说的。", turn_id="T1")
    assert life.cut()["raw_seq"] == 2 and pa.cut()["raw_seq"] == 2 and life.cut()["ns"] == "life" and pa.cut()["ns"] == "proj:a"
    r = ModeRunner(life, Access(life), H.profile("hermes"), ModeConfig(mode="note"))
    r.acc.begin_turn("hermes", "S", "T1", text="榴莲的独有词", origin="user")
    out = r.pre_answer("S", "T1", "榴莲的独有词", extras=[("proj:a", "甲项目", pa, 1)], primary=("life", "生活", 0))
    t = out["inject"]
    assert "生活里之前聊过" in t and "生活里本轮刚说的" not in t
    assert "甲项目里的一句" in t and "甲项目里本轮刚说的" not in t
