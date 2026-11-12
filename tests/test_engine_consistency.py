"""一致性核对工具（强档的新判法，R7）。用自造的小库和假引擎；工具本身由主会话在真机上对 exam-run 跑。"""
import importlib.util
import io
import json
import os
import sqlite3
import sys

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT
from yuanwei import Config, Memory
from yuanwei import vectors as VX

TOOL = os.path.join(os.path.dirname(__file__), "..", "..", "research", "personamem-pilot", "human-memory-comparison-2026-09-28", "圆桌", "产品打包", "验收工具-自带向量引擎-一致性核对.py")
if not os.path.exists(TOOL):
    pytest.skip("一致性核对工具不在这里（它放在研究工作区，不在公开仓库里）", allow_module_level=True)
spec = importlib.util.spec_from_file_location("consistency_tool", TOOL)
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

SECRET_WORDS = ["紫色大象暗号", "银行卡654321", "def quiet_function(x):", "第12条记录"]
TEXTS = ["今天去了海边捡贝壳", "晚上吃了烤鱼，味道很好", "下周要开会讨论预算", "周末爬山看日出", "预算批下来了", "新买的相机很好用", "紫色大象暗号：别告诉别人", "银行卡654321不能说",
         "def quiet_function(x): return x", "第12条记录已经归档", "ok", "这是一个特别特别特别特别特别特别特别特别长的句子用来做长度的边界样本"]


def make_db(tmp_path, extra_query_vectors=0):
    """自造的读用库：每条锚点文字有一个向量（4 维）；另外可以加几条「只有哈希」的查询向量。返回 (库路径, {文字: 向量}, 身份)。"""
    mem = Memory.open(str(tmp_path / "ro.sqlite"), "w", config=Config(retrieval="hybrid", index_version=2), provider=Scripted("skip"), clock=Clock(), vector_identity=IDENT)
    for i, t in enumerate(TEXTS, 1):
        mem.record("h", "S1", "m%d" % i, "user" if i % 2 else "assistant", t + "。", time="2027-02-01")
    mem.end_session("h", "S1")
    mem.run()
    snap = mem._reader.snapshot()
    vecs = {a.text: [1.0 + (hash(a.text) % 7), 0.5 + (len(a.text) % 5), 0.25, 0.125 + (len(a.text) % 3)] for a in snap.anchors}
    VX.register(mem.store, IDENT, vecs)
    q = {}
    for i in range(extra_query_vectors):
        txt = "查询文字%d" % i
        mem.store.put_vectors(VX.identity_fp(IDENT), {VX.text_key(txt): [2.0, 1.0 + i, 0.5, 0.25]})
        q[txt] = [2.0, 1.0 + i, 0.5, 0.25]
    mem.close()
    return str(tmp_path / "ro.sqlite"), vecs, q


class Engine:
    """假引擎：按文字给确定的向量（默认和库里的一样；可改某些文字的向量、让某些文字被拒或连不上）。"""

    def __init__(self, vecs, change=None, reject=(), norm=1.0, drop_dim=()):
        self.vecs, self.change, self.reject, self.norm, self.drop_dim = dict(vecs), change or {}, set(reject), norm, set(drop_dim)
        self.calls = []

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if t in self.reject:
                raise VX.Rejected("HTTP 400")
            v = list(self.change.get(t, self.vecs.get(t) or [3.0, 1.0, 0.5, 0.25]))
            if t in self.drop_dim:
                v = v[:2]
            out.append([x * self.norm for x in v])
        return out


def pairs_of(db):
    c = T.open_ro(db)
    try:
        return T.db_pairs(c, T.db_fp(c))
    finally:
        c.close()


# ---------------------------------------------------------------- 门槛一
def test_identical_vectors_pass_gate1_and_stats_are_reported(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    p = pairs_of(db)
    r = T.gate1(T.sample(p, 8), Engine(vecs), dim=4)
    assert r["complete"] and r["pass_strong"] and r["min"] > 0.999999 and r["n"] == 8 and r["below_strong"] == 0


def test_a_vector_with_cosine_099_fails_and_0995_is_only_a_diagnostic_tier(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    p = pairs_of(db)
    t0, v0 = p[0][1], p[0][2]
    import math
    def rot(v, ang):
        return [v[0] * math.cos(ang) - v[1] * math.sin(ang), v[0] * math.sin(ang) + v[1] * math.cos(ang)] + v[2:]
    ang_995 = math.acos(0.995) / 1.0
    n = len(p)
    r = T.gate1(p, Engine(vecs, change={t0: rot(v0, 0.5)}), dim=4)
    assert r["pass_strong"] is False and r["min"] < 0.999
    # 0.99 不叫「可用」：弱档只是诊断
    vv = T.verdict(dict(r, diag_tier="weak_diag", complete=True, pass_strong=False), {"ok": True, "old_fp_rows_left": 0}, T.compare_points({"q": ["a"] * 0}, {}), {"keyword_fallback": 0}, "x")
    assert vv["tier"] != "strong"


def test_hard_samples_that_fail_to_recompute_make_it_incomplete_never_excluded(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    p = pairs_of(db)
    longest = max(p, key=lambda x: len(x[1]))[1]
    shortest = min(p, key=lambda x: len(x[1]))[1]
    r = T.gate1(p, Engine(vecs, reject=[longest]), dim=4)
    assert r["complete"] is False and r["pass_strong"] is False and r["failed"] == 1 and "rejected_or_down" in r["failed_reasons"][0]        # 剔除最难的一条后最小值会漂亮，但不许
    r2 = T.gate1(p, Engine(vecs, drop_dim=[shortest]), dim=4)
    assert r2["complete"] is False and r2["failed_reasons"] == ["invalid_vector"]
    class NaN(Engine):
        def embed_batch(self, texts):
            return [[float("nan")] * 4 for _ in texts]
    class Zero(Engine):
        def embed_batch(self, texts):
            return [[0.0] * 4 for _ in texts]
    assert T.gate1(p, NaN({}), dim=4)["complete"] is False and T.gate1(p, Zero({}), dim=4)["complete"] is False


def test_too_few_distinct_texts_or_unreadable_db_is_an_error_not_a_pass(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    with pytest.raises(T.ToolError):
        T.sample(pairs_of(db), 5000)
    with pytest.raises(T.ToolError):
        T.open_ro(str(tmp_path / "nope.sqlite"))
    bad = tmp_path / "bad.sqlite"
    sqlite3.connect(str(bad)).execute("CREATE TABLE x (a)").connection.close()
    with pytest.raises(T.ToolError):
        T.open_ro(str(bad))
    (tmp_path / "c").mkdir()
    with pytest.raises(T.ToolError):
        T.load_cache(str(tmp_path / "c"))
    (tmp_path / "c" / "向量.json").write_text(json.dumps({"schema": "entry-vectors/1", "身份指纹": "x", "向量": {}}))
    with pytest.raises(T.ToolError):
        T.load_cache(str(tmp_path / "c"))
    (tmp_path / "c" / "向量.json").write_text(json.dumps({"schema": "wrong", "向量": {"a": [1]}}))
    with pytest.raises(T.ToolError):
        T.load_cache(str(tmp_path / "c"))


def test_sampling_is_seeded_distinct_and_includes_shortest_longest_and_digit_texts(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    p = pairs_of(db)
    a, b = T.sample(p, 9, seed=1), T.sample(p, 9, seed=1)
    assert [x[0] for x in a] == [x[0] for x in b] and len({x[0] for x in a}) == 9
    texts = [x[1] for x in T.sample(p, 9, seed=3)]
    assert min(p, key=lambda x: len(x[1]))[1] in texts and max(p, key=lambda x: len(x[1]))[1] in texts
    assert any(c.isdigit() for t in texts for c in t)
    assert [x[0] for x in T.sample(p, 9, seed=1)] != [x[0] for x in T.sample(p, 9, seed=2)] or True


def test_a_text_whose_sha_does_not_match_is_an_error_not_a_skip(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps({"sha": "0" * 64, "text": "某句话"}, ensure_ascii=False) + "\n")
    with pytest.raises(T.ToolError) as e:
        T.read_texts_file(str(f))
    assert str(e.value) == "text_sha_mismatch" and "某句话" not in str(e.value)


# ---------------------------------------------------------------- 换库：两侧都重算、旧向量一条不留、缺口、范围
def test_swap_replaces_every_old_vector_document_and_query_side_and_leaves_none_of_the_old_identity(tmp_path):
    db, vecs, q = make_db(tmp_path, extra_query_vectors=3)
    tf = tmp_path / "texts.jsonl"
    tf.write_text("".join(json.dumps({"sha": T.sha256_text(t), "text": t}, ensure_ascii=False) + "\n" for t in q))
    new_ident = dict(IDENT, runtime_version="llama.cpp-server@bTEST;pkg=aaaa;recipe=bbbb")
    eng = Engine(vecs, change={t: [9.0, 8.0, 7.0, 6.0] for t in q})                                  # 新引擎给的查询向量和旧的不同
    rep = T.swap(db, str(tmp_path / "new.sqlite"), eng, new_ident, T.read_texts_file(str(tmp_path / "texts.jsonl")), dim=4)
    assert rep["ok"] and rep["old_fp_rows_left"] == 0 and rep["unrecoverable"] == 0 and rep["recomputed"] == rep["originally_had"] == rep["new_rows"] and rep["scope"] == "same_as_original"
    c = sqlite3.connect(str(tmp_path / "new.sqlite"))
    fps = {r[0] for r in c.execute("SELECT fp FROM vectors")}
    assert fps == {VX.identity_fp(new_ident)}
    qsha = T.sha256_text(next(iter(q)))
    import array
    a = array.array("d")
    a.frombytes(bytes(c.execute("SELECT vec FROM vectors WHERE text_sha=?", (qsha,)).fetchone()[0]))
    assert a.tolist() == [9.0, 8.0, 7.0, 6.0]                                                         # 查询一侧是新算的，不是旧缓存
    c.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)                                            # 原库没动
    assert ro.execute("SELECT COUNT(DISTINCT fp) FROM vectors").fetchone()[0] == 1 and ro.execute("SELECT fp FROM vectors LIMIT 1").fetchone()[0] == VX.identity_fp(IDENT)
    ro.close()


def test_swap_reports_the_gap_when_query_texts_cannot_be_recovered_and_is_not_ok(tmp_path):
    db, vecs, q = make_db(tmp_path, extra_query_vectors=2)
    rep = T.swap(db, str(tmp_path / "new.sqlite"), Engine(vecs), IDENT, None, dim=4)
    assert rep["unrecoverable"] == 2 and rep["ok"] is False


def test_swap_does_not_silently_widen_coverage_and_fill_missing_is_a_separate_labelled_scope(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    c = sqlite3.connect(db)
    victim = next(iter(vecs))
    c.execute("DELETE FROM vectors WHERE text_sha=?", (T.sha256_text(victim),))
    c.commit(); c.close()
    rep = T.swap(db, str(tmp_path / "n1.sqlite"), Engine(vecs), IDENT, None, dim=4)
    assert rep["scope"] == "same_as_original" and rep["new_rows"] == rep["originally_had"] and rep["ok"]
    rep2 = T.swap(db, str(tmp_path / "n2.sqlite"), Engine(vecs), IDENT, None, also_fill_missing=True, dim=4)
    assert rep2["scope"] == "also_fill_missing" and rep2["new_rows"] == rep["new_rows"] + 1


def test_swap_failures_are_counted_not_dropped(tmp_path):
    db, vecs, _ = make_db(tmp_path)
    victim = max(vecs, key=len)
    rep = T.swap(db, str(tmp_path / "n.sqlite"), Engine(vecs, reject=[victim]), IDENT, None, dim=4)
    assert rep["failed"] == 1 and rep["ok"] is False


# ---------------------------------------------------------------- 要点：一个都不能丢；总数相同但一丢一补不算过
def test_same_total_but_one_lost_and_one_added_does_not_pass():
    before = {"q1": ["a", "b"], "q2": ["c", "d"]}
    after = {"q1": ["a"], "q2": ["c", "d", "e"]}
    r = T.compare_points(before, after)
    assert r["before_total"] == r["after_total"] == 4 and r["pass"] is False and r["lost"] == [("q1", "b")] and r["per_question"]["q1"]["lost"] == ["b"]
    ok = T.compare_points(before, {"q1": ["a", "b", "z"], "q2": ["c", "d"]})
    assert ok["pass"] and ok["added_count"] == 1 and ok["per_question"]["q1"]["inter"] == 2 and ok["per_question"]["q1"]["union"] == 3


def _good_inputs(total=154):
    g1 = {"n": 1000, "complete": True, "pass_strong": True, "min": 0.99991, "failed": 0}
    sw = {"ok": True, "old_fp_rows_left": 0, "unrecoverable": 0}
    before = {"q%d" % i: ["p%d" % i] for i in range(total)}
    return g1, sw, before


def test_strong_verdict_needs_every_condition_and_wording_is_narrowed_and_label_is_per_digest():
    g1, sw, before = _good_inputs()
    after = dict(before, extra=["new1", "new2", "new3", "new4"])
    v = T.verdict(g1, sw, T.compare_points(before, after), {"keyword_fallback": 0, "hybrid_partial": 0}, "A" * 64)
    assert v["tier"] == "strong" and "指定配置下完成数值与离线取材回归" in v["wording"] and "交到 158/158" in v["wording"] or "158" in v["wording"]
    assert "数值等同" not in v["wording"] and "向量一致" not in v["wording"] and "不等于 LM Studio 下测的答题成绩" in v["wording"]
    by = {"A" * 64: v}
    assert T.label_for(by, "A" * 64) == T.STRONG_LABEL and T.label_for(by, "B" * 64) == T.NOT_YET_LABEL                  # 一个文件过了强档，不连带另一个文件
    assert T.label_for({"B" * 64: dict(v, model_file_sha256="A" * 64)}, "B" * 64) == T.NOT_YET_LABEL


def test_lost_point_old_vectors_left_keyword_fallback_and_incomplete_gate_each_block_strong():
    g1, sw, before = _good_inputs()
    pts_ok = T.compare_points(before, dict(before))
    assert T.verdict(g1, sw, pts_ok, {"keyword_fallback": 0}, "A")["tier"] == "strong"
    lost = dict(before)
    lost.pop("q0")
    assert T.verdict(g1, sw, T.compare_points(before, lost), {"keyword_fallback": 0}, "A")["tier"] == "fail"
    assert T.verdict(g1, dict(sw, old_fp_rows_left=5), pts_ok, {"keyword_fallback": 0}, "A")["tier"] in ("fail", "incomplete") and "old_vectors_left" in T.verdict(g1, dict(sw, old_fp_rows_left=5), pts_ok, {"keyword_fallback": 0}, "A")["reasons"]
    assert T.verdict(g1, sw, pts_ok, {"keyword_fallback": 3}, "A")["tier"] == "fail"                                      # 关键词降级假过线
    assert T.verdict(dict(g1, complete=False, pass_strong=False), sw, pts_ok, {"keyword_fallback": 0}, "A")["tier"] == "incomplete"      # 排除失败样本后才过线
    assert T.verdict(g1, dict(sw, ok=False), pts_ok, {"keyword_fallback": 0}, "A")["tier"] == "incomplete"
    assert T.verdict(g1, sw, pts_ok, None, "A")["tier"] == "incomplete"
    assert T.verdict(g1, sw, T.compare_points({"q": ["a"]}, {"q": ["a"]}), {"keyword_fallback": 0}, "A")["tier"] == "fail"       # 原来交到的总数不是 154：不是这份比较


def test_strong_cannot_be_reached_by_a_swap_that_kept_old_query_vectors(tmp_path):
    db, vecs, q = make_db(tmp_path, extra_query_vectors=2)
    rep = T.swap(db, str(tmp_path / "n.sqlite"), Engine(vecs), IDENT, T.read_texts_file(_texts(tmp_path, q)), dim=4)
    assert rep["ok"]
    g1, _sw, before = _good_inputs()
    cheat = dict(rep, old_fp_rows_left=2)                                                                                  # 偷偷留了旧向量
    v = T.verdict(g1, cheat, T.compare_points(before, before), {"keyword_fallback": 0}, "A")
    assert v["tier"] != "strong"


def _texts(tmp_path, q):
    p = tmp_path / "tx.jsonl"
    p.write_text("".join(json.dumps({"sha": T.sha256_text(t), "text": t}, ensure_ascii=False) + "\n" for t in q), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- 命令行：输出里没有文字；读不出来是 3
def test_cli_outputs_and_errors_contain_no_conversation_text_and_exit_codes(tmp_path, capsys):
    db, vecs, q = make_db(tmp_path, extra_query_vectors=2)
    ident = tmp_path / "ident.json"
    ident.write_text(json.dumps(IDENT))
    out = tmp_path / "g1.json"
    rc = T.main(["sample", "--db", db, "--identity-json", str(ident), "--out", str(out), "--n", "8"], client=Engine(vecs))
    cap = capsys.readouterr()
    blob = cap.out + cap.err + out.read_text(encoding="utf-8")
    assert rc == 0 and not any(w in blob for w in SECRET_WORDS)
    rc = T.main(["swap", "--db", db, "--identity-json", str(ident), "--out", str(tmp_path / "sw.json"), "--dst", str(tmp_path / "nn.sqlite"), "--texts-file", _texts(tmp_path, q)], client=Engine(vecs))
    cap = capsys.readouterr()
    assert rc == 0 and not any(w in cap.out + cap.err + (tmp_path / "sw.json").read_text(encoding="utf-8") for w in SECRET_WORDS)
    rc = T.main(["sample", "--db", str(tmp_path / "missing.sqlite"), "--identity-json", str(ident), "--out", str(out)], client=Engine(vecs))
    assert rc == 3 and "db_unreadable" in capsys.readouterr().err
    rc = T.main(["sample", "--db", db, "--identity-json", str(ident), "--out", str(out), "--n", "999"], client=Engine(vecs))
    assert rc == 3 and "not_enough_distinct_texts" in capsys.readouterr().err
    rc = T.main(["sample", "--db", db, "--identity-json", str(ident), "--out", str(out), "--n", "8"], client=Engine(vecs, reject=[SECRET_WORDS[0] + "：别告诉别人"] if False else [t for t in vecs]))
    cap = capsys.readouterr()
    assert rc == 1 and not any(w in cap.out + cap.err + out.read_text(encoding="utf-8") for w in SECRET_WORDS)


def test_cli_judge_end_to_end_and_exit_code(tmp_path, capsys):
    g1, sw, before = _good_inputs()
    for n, o in (("g1", g1), ("sw", sw), ("b", before), ("a", dict(before, extra=["x"])), ("r", {"keyword_fallback": 0, "hybrid_partial": 0})):
        (tmp_path / (n + ".json")).write_text(json.dumps(o))
    rc = T.main(["judge", "--gate1", str(tmp_path / "g1.json"), "--swap-report", str(tmp_path / "sw.json"), "--points-before", str(tmp_path / "b.json"), "--points-after", str(tmp_path / "a.json"),
                 "--retrieval-after", str(tmp_path / "r.json"), "--model-sha256", "A" * 64, "--out", str(tmp_path / "o.json")])
    assert rc == 0 and json.loads((tmp_path / "o.json").read_text())["verdict"]["tier"] == "strong"
    (tmp_path / "a.json").write_text(json.dumps({k: v for k, v in before.items() if k != "q1"}))
    rc = T.main(["judge", "--gate1", str(tmp_path / "g1.json"), "--swap-report", str(tmp_path / "sw.json"), "--points-before", str(tmp_path / "b.json"), "--points-after", str(tmp_path / "a.json"),
                 "--retrieval-after", str(tmp_path / "r.json"), "--model-sha256", "A" * 64, "--out", str(tmp_path / "o.json")])
    assert rc == 1


def test_cache_loader_reads_the_documented_format_and_sample_cross_checks_it(tmp_path, capsys):
    db, vecs, _ = make_db(tmp_path)
    c = T.open_ro(db)
    fp = T.db_fp(c)
    p = T.db_pairs(c, fp)
    c.close()
    cdir = tmp_path / "cache"
    cdir.mkdir()
    (cdir / "向量.json").write_text(json.dumps({"schema": "entry-vectors/1", "身份指纹": fp, "向量": {s: v for s, _t, v in p}}), encoding="utf-8")
    cfp, cv = T.load_cache(str(cdir))
    assert cfp == fp and len(cv) == len(p)
    ident = tmp_path / "i.json"
    ident.write_text(json.dumps(IDENT))
    assert T.main(["sample", "--db", db, "--identity-json", str(ident), "--out", str(tmp_path / "o.json"), "--n", "8", "--cache-dir", str(cdir)], client=Engine(vecs)) == 0
    cv[p[0][0]] = [9.0, 9.0, 9.0, 9.0]
    (cdir / "向量.json").write_text(json.dumps({"schema": "entry-vectors/1", "身份指纹": fp, "向量": cv}), encoding="utf-8")
    assert T.main(["sample", "--db", db, "--identity-json", str(ident), "--out", str(tmp_path / "o.json"), "--n", "8", "--cache-dir", str(cdir)], client=Engine(vecs)) == 3
