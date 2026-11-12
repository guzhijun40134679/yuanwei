"""便条（自动档）：免查门接进 Memory.note、相关度门的分数和真值表、装料（停／跳过）、提示行集合 U、游标、轮次标记、状态里的当天费用。全部自造数据。"""
import math

import pytest

from helpers import Clock, Scripted, make_mem
from yuanwei import Config, Memory, note as NT
from yuanwei.errors import BadCursor, MissingVector
from yuanwei.memory import refusal_text


def organize(mem, session, texts, host="h", date="2027-02-01", dates=None, roles=None):
    for i, t in enumerate(texts, 1):
        d = dates[i - 1] if dates else date
        role = roles[i - 1] if roles else ("user" if i % 2 else "assistant")
        mem.record(host, session, "%s-%d" % (session, i), role, t, time=d)
    mem.end_session(host, session)
    mem.run()


def mk(tmp_path, texts=None, **cfg):
    mem = make_mem(tmp_path, provider=Scripted("skip"), **cfg)
    organize(mem, "S1", texts or ["今天去了海边捡贝壳", "海边风很大，贝壳很漂亮", "晚上吃了烤鱼", "烤鱼很好吃", "明天要开会讨论预算", "预算已经批下来了"])
    return mem


# ------------------------------------------------------------------ 分数和门的真值表（纯函数）
def test_coverage_is_idf_weighted_share_of_query_terms_and_hand_computed(tmp_path):
    texts = ["苹果香蕉很好吃", "苹果很好", "葡萄很甜"]
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    organize(mem, "S1", texts)
    snap = mem._reader.snapshot()
    ids = {snap.by_id[m]["content"]: m for m in snap.by_id}
    N = len(snap.anchors)
    c = NT.coverage_scores(snap, "苹果香蕉火星", [ids[texts[0]], ids[texts[1]], ids[texts[2]]])
    # 查询词项是 苹果、果香、香蕉、蕉火、火星（相邻两字），用 tokenize 的结果算分母
    from yuanwei import rank as RK
    qterms = list(dict.fromkeys(RK.tokenize("苹果香蕉火星")))
    dfs = {t: sum(1 for a in snap.anchors if t in set(RK.tokenize(a.text))) for t in qterms}
    idf = {t: math.log(1 + (N - dfs[t] + 0.5) / (dfs[t] + 0.5)) for t in qterms}
    den = sum(idf.values())
    for txt in texts:
        have = set(RK.tokenize(txt))
        want = sum(idf[t] for t in qterms if t in have) / den
        assert c[ids[txt]] == pytest.approx(want, abs=1e-12)
    assert 0 < c[ids[texts[0]]] < 1 and c[ids[texts[0]]] > c[ids[texts[1]]] > c[ids[texts[2]]] == 0
    assert NT.coverage_scores(snap, "，。！", [ids[texts[0]]]) == {ids[texts[0]]: 0.0}          # 查询没有词项记 0


def test_same_term_twice_in_query_counts_once(tmp_path):
    mem = mk(tmp_path)
    snap = mem._reader.snapshot()
    m = next(iter(snap.by_id))
    a = NT.coverage_scores(snap, "贝壳", [m])
    b = NT.coverage_scores(snap, "贝壳贝壳", [m])
    assert NT.coverage_scores(snap, "贝壳 贝壳", [m]) == a                           # 同一个词项写两遍，只算一次
    assert b[m] != a[m] or b[m] == 0                                            # 「贝壳贝壳」多出 壳贝，是另一个查询


@pytest.mark.parametrize("hybrid,tk,tv,c,v,want", [
    (False, None, None, 0.0, None, True),          # 纯关键词，关键词门关：不用分数门
    (False, 0.5, None, 0.49, None, False),
    (False, 0.5, None, 0.5, None, True),
    (False, 0.5, 0.1, 0.4, 0.99, False),           # 纯关键词没有向量判据，tau_vec 不起作用
    (True, None, None, 0.0, None, True),           # 混合：两门全关
    (True, None, 0.6, 1.0, None, False),           # 混合：关键词关、向量开——只看向量；不在向量前 50（v=None）不满足
    (True, None, 0.6, 1.0, 0.59, False),
    (True, None, 0.6, 0.0, 0.6, True),
    (True, 0.5, None, 0.4, 0.99, False),           # 混合：关键词开、向量关——只看关键词
    (True, 0.5, None, 0.5, None, True),
    (True, 0.5, 0.6, 0.4, 0.59, False),            # 两门都开：任一满足
    (True, 0.5, 0.6, 0.5, None, True),
    (True, 0.5, 0.6, 0.1, 0.7, True),
])
def test_gate_truth_table(hybrid, tk, tv, c, v, want):
    kw_on, vec_on = NT.gate_open(hybrid, tk, tv)
    assert NT.candidate_passes(c, v, kw_on, vec_on, tk, tv) is want


def test_gate_open_flags():
    assert NT.gate_open(False, 0.3, 0.6) == (True, False)
    assert NT.gate_open(True, None, 0.6) == (False, True)
    assert NT.gate_open(True, None, None) == (False, False)


# ------------------------------------------------------------------ 更晚、U 的顺序（纯函数）
def R(mid, t=None, long=False, seq=1):
    return {"msg_id": mid, "msg_time": t, "date": t[:10] if t else None, "is_long": long, "seq": seq}


def test_later_state_three_ways():
    shown = [R("a", "2027-03-01"), R("b", "2027-03-05")]
    assert NT.later_state([R("c", "2027-03-06")], shown) == "有"
    assert NT.later_state([R("c", "2027-03-05"), R("d", "2027-02-01")], shown) == "没有"            # 同一天没有具体时分，不编先后
    assert NT.later_state([R("c", None)], shown) == "无法判断"
    assert NT.later_state([R("c", "2027-03-06"), R("d", None)], shown) == "有"
    assert NT.later_state([R("c", "2027-03-06")], []) == "无法判断"                                # 没展示正文
    assert NT.later_state([R("c", "2027-03-06")], [R("a", None), R("b", "2027-03-01")]) == "无法判断"   # 已展示里有缺日期的：「最晚」没法确定
    assert NT.later_state([R("c", "2027-03-05T10:00:00Z")], [R("b", "2027-03-05T09:00:00Z")]) == "有"
    assert NT.later_state([R("c", "2027-03-05T09:00:00Z")], [R("b", "2027-03-05T09:00:00Z")]) == "没有"
    assert NT.later_state([R("c", "2027-03-05T10:00:00Z")], [R("b", "2027-03-05")]) == "没有"      # 一边只有日期：同一天不算更晚


def test_unshown_order_later_then_long_then_rest_with_rank_and_id_ties():
    rows = {"a": R("a", "2027-03-01"), "late1": R("late1", "2027-03-09"), "late2": R("late2", "2027-03-08"), "long": R("long", "2027-01-01", long=True),
            "longlate": R("longlate", "2027-03-10", long=True), "old1": R("old1", "2027-01-05"), "old2": R("old2", "2027-01-06"), "nodate": R("nodate", None)}
    hits = ["old2", "long", "late2", "nodate", "old1", "late1", "longlate", "a"]
    got = NT.order_unshown(["old2", "long", "late2", "nodate", "old1", "late1", "longlate"], hits, rows, [rows["a"]])
    assert got == ["late2", "late1", "longlate", "long", "old2", "nodate", "old1"]                  # 更晚(按名次)，超长，其余(按名次)；一条只出现一次
    assert len(got) == len(set(got))
    same = NT.order_unshown(["x2", "x1"], ["x1", "x2"], {"x1": R("x1", "2027-01-01"), "x2": R("x2", "2027-01-01")}, [])
    assert same == ["x1", "x2"]


# ------------------------------------------------------------------ 装料：停还是跳过
def test_fitter_stop_versus_skip_on_an_oversized_candidate():
    from yuanwei import encoding as EN
    mp = EN.load_mapping()
    rows = {"s1": {"msg_id": "s1", "session_id": "S", "date": "2027-01-01", "role": "user", "content": "短的第一条", "seq": 1, "is_long": 0},
            "big": {"msg_id": "big", "session_id": "S", "date": "2027-01-01", "role": "user", "content": "很长" * 1500, "seq": 2, "is_long": 0},
            "s3": {"msg_id": "s3", "session_id": "S", "date": "2027-01-01", "role": "user", "content": "短的第三条", "seq": 3, "is_long": 0}}
    seq = [("s1", None, True), ("big", None, True), ("s3", None, True)]
    stop = NT.NoteFitter([(2000, 2000)], "stop", mp, False)(seq, rows, mp, 2000)
    skip_f = NT.NoteFitter([(2000, 2000)], "skip", mp, False)
    skip = skip_f(seq, rows, mp, 2000)
    assert stop["选中"] == ["s1"] and stop["停在"] == "big"
    assert skip["选中"] == ["s1", "s3"] and skip_f.skipped == ["big"]


def test_fitter_both_limits_apply_and_conflict_chars_are_not_oversize():
    from yuanwei import encoding as EN
    mp = EN.load_mapping()
    rep = mp.rep_chars[0]
    rows = {"a": {"msg_id": "a", "session_id": "S", "date": None, "role": "user", "content": "字" * 100, "seq": 1, "is_long": 0},
            "c": {"msg_id": "c", "session_id": "S", "date": None, "role": "user", "content": "带替代字符" + rep, "seq": 2, "is_long": 0},
            "b": {"msg_id": "b", "session_id": "S", "date": None, "role": "user", "content": "字" * 100, "seq": 3, "is_long": 0}}
    seq = [("a", None, True), ("c", None, True), ("b", None, True)]
    f = NT.NoteFitter([(10 ** 6, 10 ** 6)], "stop", mp, False)
    r = f(seq, rows, mp, 0)
    assert r["选中"] == ["a", "b"] and f.conflicts == ["c"] and r["停在"] is None
    meter = NT.NoteMeter(rows, mp, False)
    t, ch = meter.totals(["a"])
    only_chars = NT.NoteFitter([(10 ** 6, ch)], "stop", mp, False)(seq, rows, mp, 0)           # 字符上限卡住，估算词元很宽
    only_toks = NT.NoteFitter([(t, 10 ** 6)], "stop", mp, False)(seq, rows, mp, 0)             # 估算词元上限卡住，字符很宽
    assert only_chars["选中"] == ["a"] and only_toks["选中"] == ["a"]


def test_meter_equals_full_render():
    from yuanwei import encoding as EN
    mp = EN.load_mapping()
    rows = {m: {"msg_id": m, "session_id": s, "date": d, "role": r, "content": c, "seq": i, "is_long": 0}
            for i, (m, s, d, r, c) in enumerate([("m1", "S1", "2027-01-01", "user", "你好【吗】"), ("m2", "S1", "2027-01-01", "assistant", "好\n第二行"), ("m3", "S2", None, "user", "x" * 30)], 1)}
    for rule in (False, True):
        meter = NT.NoteMeter(rows, mp, rule)
        for ids in (["m1"], ["m3", "m1"], ["m1", "m2", "m3"]):
            text = NT.render(ids, rows, mp, rule, None)
            t, c = meter.totals(ids)
            assert (t, c) == (NT.text_tokens(text), len(text)), (rule, ids)


# ------------------------------------------------------------------ Memory.note
def test_note_basic_structure_and_numbers(tmp_path):
    mem = mk(tmp_path)
    r = mem.note("贝壳")
    assert r["status"] == "ok" and r["note"].startswith(NT.NOTE_OPENING)
    assert r["chars"] == len(r["note"]) <= mem.cfg.note_chars and r["est_tokens"] == NT.text_tokens(r["note"])
    lines = r["note"].split("\n")
    s_idx = next(i for i, l in enumerate(lines) if l.startswith("⟪材料开始 "))
    e_idx = next(i for i, l in enumerate(lines) if l.startswith("⟪材料结束 "))
    assert s_idx < e_idx and lines[s_idx].split()[1].rstrip("⟫") == lines[e_idx].split()[1].rstrip("⟫")
    body = lines[s_idx + 1:e_idx]
    assert body[0].startswith("会话 S1｜2027-02-01") and all(l.startswith("〔") or l.startswith("会话 ") for l in body)
    for mid in r["delivered"]:
        assert any("｜%s〕" % mid in l for l in body)
    assert r["unshown_count"] == 0 and r["hint"] is None and r["cursor"] is None and r["later"] is None
    assert r["gates"]["kw_on"] is False and r["gates"]["vec_on"] is False
    seqs = [mem.store.message_row("w", m)["seq"] for m in r["delivered"]]
    assert seqs == sorted(seqs)


def test_note_rule_in_note_adds_the_standing_rule(tmp_path):
    mem = mk(tmp_path, rule_in_note=True)
    assert NT.RULE_LINE in mem.note("贝壳")["note"]
    (tmp_path / "x").mkdir()
    mem2 = mk(tmp_path / "x", rule_in_note=False)
    assert NT.RULE_LINE not in mem2.note("贝壳")["note"]


def test_free_gate_skips_greetings_and_commands_without_searching(tmp_path, monkeypatch):
    mem = mk(tmp_path)
    def boom(*a, **k):
        raise AssertionError("免查的句子不应该去检索")
    monkeypatch.setattr(mem._reader, "snapshot", boom)
    r = mem.note("好的")
    assert r["status"] == "skipped_greeting" and r["note"] == "" and r["gates"]["free"]["rule"] == "closed_set"
    assert mem.note("/clear", host_command=True)["status"] == "skipped_command"
    monkeypatch.undo()
    assert mem.note("/clear")["status"] != "skipped_command"                   # 没标明是宿主命令：照常查
    assert mem.note("好的，查一下贝壳")["status"] == "ok"


def test_free_gate_switch_only_controls_greetings_and_commands_not_read_permission(tmp_path):
    mem = mk(tmp_path, note_free_gate=False)
    assert mem.note("好的")["status"] in ("no_candidates", "ok")                   # 不免查：照常检索
    assert mem.note("你好", history_allowed=False)["status"] == "refused"          # 宿主显式禁用标记一直遵守
    assert mem.note("别用我的记忆")["status"] == "refused"                          # 文字识别的禁读也一直执行：免查门关着不影响读取权限


def test_refusal_returns_the_standing_note_and_blocks_every_read_entry_for_that_turn(tmp_path):
    mem = mk(tmp_path)
    r = mem.note("不用解释，别用我的记忆。", turn_id="t1")
    assert r["status"] == "refused" and r["note"] == refusal_text("clause_ban") and r["gates"]["permission"] == {"allowed": False, "reason": "clause_ban"}
    mid = next(iter(mem._reader.snapshot().by_id))
    assert mem.recall("贝壳", turn_id="t1")["status"] == "refused"
    assert mem.get_messages([mid], turn_id="t1")["status"] == "refused"
    assert mem.recall("贝壳", turn_id="t2")["delivered"]                              # 别的轮次不受影响
    assert mem.get_messages([mid], turn_id="t2")["messages"]
    assert mem.get_messages([mid])["status"] == "refused"                              # 省略轮次编号：按最近声明的这一轮算，绕不过
    assert mem.attachments(mid, turn_id="t1")["status"] == "refused"
    mem.end_turn("t1")
    assert mem.get_messages([mid])["messages"]                                        # 这一轮结束以后，省略轮次的调用才恢复（没有持续设置时）
    mem.close()
    mem2 = make_mem(tmp_path, provider=Scripted("skip"))
    assert mem2.recall("贝壳", turn_id="t1")["status"] == "refused"                   # 标记存在库里，换个进程也认
    assert mem2.status()["retrieval"]["mode"] == "keyword"


def test_host_marker_marks_the_turn_and_a_later_allowed_call_cannot_unmark(tmp_path):
    mem = mk(tmp_path)
    assert mem.note("你好", history_allowed=False, turn_id="t9")["status"] == "refused"
    assert mem.note("贝壳", turn_id="t9")["status"] == "refused"                       # 同一轮：已经拒绝过，后面的调用也拒绝
    mem.mark_turn_refused("t10")
    assert mem.recall("贝壳", turn_id="t10")["status"] == "refused"


def test_relevance_gate_blocks_whole_note_when_max_coverage_is_low(tmp_path):
    mem = mk(tmp_path, note_tau_kw=0.9)
    r = mem.note("贝壳和预算以及火星基地")
    assert r["status"] == "below_threshold" and r["note"] == "" and r["gates"]["max_c"] < 0.9
    (tmp_path / "b").mkdir()
    r2 = mk(tmp_path / "b", note_tau_kw=0.3).note("贝壳")
    assert r2["status"] == "ok" and r2["gates"]["max_c"] == pytest.approx(1.0)


def test_no_candidates_is_reported_not_faked(tmp_path):
    mem = mk(tmp_path)
    r = mem.note("zzqq完全没有的词")
    assert r["status"] == "no_candidates" and r["note"] == "" and r["candidates"] == 0


def test_gate_only_decides_whether_the_note_appears_not_what_is_selected(tmp_path):
    a = mk(tmp_path, note_tau_kw=0.3).note("贝壳")
    (tmp_path / "c").mkdir()
    b = mk(tmp_path / "c", note_tau_kw=None).note("贝壳")
    assert a["delivered"] == b["delivered"] and a["note"] == b["note"]


# ------------------------------------------------------------------ 提示行、集合 U、游标
def many(tmp_path, n=26, **cfg):
    texts = ["鲸鱼的第%d条记录，写了一些关于海洋的内容" % i for i in range(1, n + 1)]
    mem = make_mem(tmp_path, provider=Scripted("skip"), **cfg)
    dates = ["2027-02-%02d" % (1 + i % 20) for i in range(n)]
    organize(mem, "S1", texts, dates=dates, roles=["user"] * n)
    return mem, texts


def page_all(mem, cursor):
    out = []
    while cursor:
        r = mem.get_messages(cursor=cursor)
        out += [m["msg_id"] for m in r["messages"]]
        cursor = r["cursor"]
    return out


def test_hint_set_is_consistent_for_count_listed_ids_and_cursor(tmp_path):
    mem, texts = many(tmp_path, 26, note_chars=900, note_tokens=470)
    r = mem.note("鲸鱼")
    assert r["status"] == "ok" and r["chars"] <= 900 and r["est_tokens"] <= 470
    snap = mem._reader.snapshot()
    expect = [m for m in snap.by_id if m not in r["delivered"]]
    assert r["unshown_count"] == len(r["unshown"]) == len(expect) > 10                    # 每条消息都命中，没展示的都在 U 里
    assert sorted(r["unshown"]) == sorted(expect)
    assert len(r["listed"]) == 10 and r["listed"] == r["unshown"][:10]
    assert r["hint"] in r["note"] and r["note"].endswith(r["hint"])
    for mid in r["listed"]:
        assert mid in r["hint"]
    assert r["cursor"] in r["hint"] and "（先列 10 条" in r["hint"] and ("还有 %d 条未展示" % r["unshown_count"]) in r["hint"]
    assert page_all(mem, r["cursor"]) == r["unshown"]                                       # 游标覆盖整个 U，顺序固定
    got = mem.get_messages(r["listed"])["messages"]
    assert [m["msg_id"] for m in got] == sorted(r["listed"], key=lambda m: snap.by_id[m]["seq"])
    for m in got:
        assert m["text"] == mem._reader.mp.encode(snap.by_id[m["msg_id"]]["content"])


def test_hint_text_and_chars_are_counted_in_the_limit_by_trimming_the_last_added(tmp_path):
    mem, texts = many(tmp_path, 12, note_chars=5000, note_tokens=3000)
    full = mem.note("鲸鱼")
    assert full["unshown_count"] == 0 and full["hint"] is None
    saw_trim = False
    for cap in range(len(full["note"]) - 5, 520, -37):
        mem.cfg.note_chars = cap
        r = mem.note("鲸鱼")
        assert r["chars"] <= cap, cap
        assert r["unshown_count"] == len(full["delivered"]) - len(r["delivered"]), cap
        assert set(r["delivered"]) <= set(full["delivered"]) and set(r["unshown"]) | set(r["delivered"]) == set(full["delivered"])
        assert page_all(mem, r["cursor"]) == r["unshown"]
        saw_trim = saw_trim or r["trimmed"] > 0
    assert saw_trim                                                                          # 提示行占的地方真的逼得拿掉过最后装入的一条


def tight(tmp_path, texts, share=0.8, **cfg):
    """先用很宽的上限看全文有多长，再把上限收到它的 share：保证有一部分放不下。返回已按这个上限配好的 Memory。"""
    mem = make_mem(tmp_path, provider=Scripted("skip"), note_chars=20000, note_tokens=10000, **cfg)
    return mem


def test_later_state_is_stated_in_the_hint(tmp_path):
    texts = ["鲸鱼的记录%d" % i for i in range(1, 17)]
    mem = make_mem(tmp_path, provider=Scripted("skip"), note_chars=20000, note_tokens=10000)
    organize(mem, "S1", texts, dates=["2027-02-%02d" % i for i in range(1, 17)], roles=["user"] * 16)
    full = mem.note("鲸鱼")
    mem.cfg.note_chars, mem.cfg.note_tokens = int(len(full["note"]) * 0.8), 10000
    r = mem.note("鲸鱼")
    assert r["unshown_count"] > 0 and r["later"] in ("有", "没有") and ("其中%s消息日期晚于" % r["later"]) in r["hint"]
    snap = mem._reader.snapshot()
    shown_latest = max(snap.by_id[m]["date"] for m in r["delivered"])
    truly_later = any(snap.by_id[m]["date"] > shown_latest for m in r["unshown"])
    assert (r["later"] == "有") == truly_later
    if truly_later:
        assert snap.by_id[r["unshown"][0]]["date"] > shown_latest                            # 明确更晚的排在最前


def test_undated_messages_make_later_unknown(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), note_chars=20000, note_tokens=10000)
    for i in range(1, 17):
        mem.record("h", "S1", "S1-%d" % i, "user", "鲸鱼的记录%d" % i)                        # 没有时间
    mem.end_session("h", "S1")
    mem.run()
    full = mem.note("鲸鱼")
    mem.cfg.note_chars = int(len(full["note"]) * 0.8)
    r = mem.note("鲸鱼")
    assert r["unshown_count"] > 0 and r["later"] == "无法判断" and "无法判断是否有消息日期晚于" in r["hint"]


def test_oversize_message_stop_vs_skip_through_memory(tmp_path):
    texts = ["鲸鱼短一", "鲸鱼" + "很长的内容" * 150, "鲸鱼短三"]
    got = {}
    for mode in ("stop", "skip"):
        sub = tmp_path / mode
        sub.mkdir()
        mem = make_mem(sub, provider=Scripted("skip"), note_chars=700, note_tokens=380, note_oversize=mode, long_limit=10 ** 6)
        organize(mem, "S1", texts, roles=["user"] * 3)
        r = mem.note("鲸鱼")
        snap = mem._reader.snapshot()
        by_text = {snap.by_id[m]["content"]: m for m in snap.by_id}
        assert r["chars"] <= 700 and by_text[texts[1]] not in r["delivered"]                        # 大的那条两种都放不下
        assert by_text[texts[1]] in r["unshown"]                                                   # 放不下的那条在 U 里，不是丢了
        got[mode] = ({snap.by_id[m]["content"] for m in r["delivered"]}, r["oversize"])
    assert got["stop"][0] <= got["skip"][0]                                                        # 跳过接着装，只会多不会少
    assert got["skip"][0] == {texts[0], texts[2]} and got["skip"][1]["skipped"]
    assert got["stop"][1]["stop_at"] is not None or got["stop"][0] == {texts[0], texts[2]}


def test_long_unorganized_messages_are_listed_before_other_unshown(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), note_chars=20000, note_tokens=10000, long_limit=60)
    texts = ["鲸鱼的短记录%d" % i for i in range(1, 9)] + ["鲸鱼" + "说了很多很多的内容" * 20]
    organize(mem, "S1", texts, roles=["user"] * 9, dates=["2027-02-01"] * 9)
    full = mem.note("鲸鱼")
    mem.cfg.note_chars = int(len(full["note"]) * 0.75)
    r = mem.note("鲸鱼")
    snap = mem._reader.snapshot()
    longs = [m for m in r["unshown"] if snap.by_id[m]["is_long"]]
    assert longs
    assert r["unshown"].index(longs[0]) == 0                                                  # 同一天没有更晚，超长条排在最前


def test_cursor_belongs_to_the_workspace_that_made_it(tmp_path):
    mem, _ = many(tmp_path, 26, note_chars=900, note_tokens=470)
    r = mem.note("鲸鱼")
    other = make_mem(tmp_path, ws="other", provider=Scripted("skip"))
    with pytest.raises(BadCursor):
        other.get_messages(cursor=r["cursor"])


def test_note_runs_the_background_check_like_other_read_entries(tmp_path):
    mem = mk(tmp_path)
    called = []
    mem.check_and_spawn = lambda: called.append(1) or {"action": "none"}
    mem.note("贝壳")
    assert called
    called.clear()
    mem.note("好的")
    assert not called                                                                          # 免查的句子不触发


def test_note_in_hybrid_mode_uses_vectors_and_reports_missing_ones(tmp_path):
    np = pytest.importorskip("numpy")
    from yuanwei import vectors as VX
    ident = {"provider": "t", "model": "m", "model_file_sha256": "x" * 64, "runtime_version": "r", "context_length": 2048, "dimension": 4, "normalization": "l2",
             "prefix": "none", "template": "none", "text_processing": "verbatim"}
    texts = ["今天去了海边捡贝壳", "大海边上的沙滩很漂亮", "晚上吃了烤鱼", "明天要开会讨论预算"]
    mem = Memory.open(str(tmp_path / "hy.sqlite"), "w", config=Config(retrieval="hybrid", index_version=2, note_tau_vec=0.9), provider=Scripted("skip"), clock=Clock(), vector_identity=ident)
    organize(mem, "S1", texts)
    with pytest.raises(MissingVector):
        mem.note("海滩捡贝壳")                                                                  # 没有向量：报错，不退成关键词
    vecs = {texts[0]: [1, 0, 0, 0], texts[1]: [0.9, 0.1, 0, 0], texts[2]: [0, 1, 0, 0], texts[3]: [0, 0, 1, 0], "海滩捡贝壳": [1, 0.05, 0, 0]}
    VX.register(mem.store, ident, vecs)
    r = mem.note("海滩捡贝壳")
    assert r["status"] == "ok" and r["gates"]["vec_on"] and r["gates"]["kw_on"] is False and r["gates"]["max_v"] > 0.9
    snap = mem._reader.snapshot()
    got = [snap.by_id[m]["content"] for m in r["delivered"]]
    assert texts[1] in got                                                                     # 与查询没有共同词，靠向量进来
    mem.cfg.note_tau_vec = 0.999999
    assert mem.note("海滩捡贝壳")["status"] == "below_threshold"
    mem.cfg.note_tau_vec = None
    mem.cfg.note_tau_kw = 0.5
    r2 = mem.note("海滩捡贝壳")                                                                 # 向量门关：候选池照旧是混合的，只用关键词门判
    assert r2["gates"]["vec_on"] is False and r2["gates"]["hybrid"] is True


# ------------------------------------------------------------------ 状态、配置
def test_status_reports_cost_booked_today(tmp_path):
    clock = Clock("2027-03-01T08:00:00Z")
    mem = make_mem(tmp_path, provider=Scripted("thread"), clock=clock)
    for i in range(1, 5):
        mem.record("h", "S1", "S1-%d" % i, "user" if i % 2 else "assistant", "第%d句话" % i, time="2027-03-01")
    mem.end_session("h", "S1")
    mem.run()
    st = mem.status()
    assert st["cost_today"]["day"] == "2027-03-01" and st["cost_today"]["cny"] > 0 and st["cost_today"]["calls"] == st["cost"]["calls"]
    assert st["cost_today"]["cap_cny"] is None and st["cost_today"]["cap_set"] is False
    clock.advance(days=1)
    assert mem.status()["cost_today"]["cny"] == 0 and mem.status()["cost"]["cny"] > 0         # 今天还没花，总数还在
    mem.cfg.daily_cap_cny = 5.0
    st2 = mem.status()["cost_today"]
    assert st2["cap_cny"] == 5.0 and st2["cap_set"] is True


def test_status_does_not_spawn_or_change_anything(tmp_path):
    mem = mk(tmp_path)
    mem.check_and_spawn = lambda: (_ for _ in ()).throw(AssertionError("status 不能拉起整理"))
    mem.status()


@pytest.mark.parametrize("bad", [{"note_oversize": "x"}, {"note_tau_kw": 1.5}, {"note_tau_vec": -0.1}, {"note_chars": 0}, {"note_tokens": True}, {"note_tau_kw": "0.3"}])
def test_note_config_validation(bad):
    from yuanwei.errors import ConfigError
    with pytest.raises(ConfigError):
        Config(**bad).validate()


def test_note_defaults_are_off_for_gates_and_pending():
    c = Config()
    assert c.note_tau_kw is None and c.note_tau_vec is None and c.note_gates_status == "预览候选：关" and c.note_chars == 8000 and c.note_oversize == "stop"


def test_unshown_set_counts_only_candidates_that_pass_the_gate(tmp_path):
    """提示行的数、编号、游标是同一个集合：过了门槛、而且还有没展示的原话。没过门槛的候选不在里面（门槛开着时）。"""
    mem, texts = many(tmp_path, 26, note_chars=900, note_tokens=470)
    mem.cfg.note_tau_kw = 0.6
    an = NT.analyze(mem._reader, "鲸鱼的第3条记录", mem.cfg)
    c = an["c"]
    assert any(v < 0.6 for v in c.values()) and any(v >= 0.6 for v in c.values())              # 这个查询下有的候选过门槛、有的不过
    r = mem.note("鲸鱼的第3条记录")
    assert r["status"] == "ok"
    passing = {m for m, v in c.items() if v + NT.EPS >= 0.6}
    assert set(r["unshown"]) == passing - set(r["delivered"]) and r["unshown_count"] == len(r["unshown"])
    assert not (set(r["unshown"]) & {m for m, v in c.items() if v + NT.EPS < 0.6})
    if r["unshown"]:
        assert page_all(mem, r["cursor"]) == r["unshown"]


def test_gate_off_counts_every_unshown_candidate(tmp_path):
    mem, texts = many(tmp_path, 26, note_chars=900, note_tokens=470)
    an = NT.analyze(mem._reader, "鲸鱼的第3条记录", mem.cfg)
    r = mem.note("鲸鱼的第3条记录")
    assert set(r["unshown"]) == set(an["hits"]) - set(r["delivered"]) and "本次命中的候选还有" in r["hint"]            # 门槛关：不写「过门槛」
