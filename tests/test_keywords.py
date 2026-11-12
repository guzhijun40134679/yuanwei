"""recall 的可选关键词：规范化和校验在检索之前；分别检索、加权 RRF 合并一次、取材只走一次；不给关键词时逐字走参照路径。第二段·交回点甲。"""
from fractions import Fraction

import pytest

from helpers2 import TEXTS, Counter, FakeEmbedClient, hybrid, organized
from yuanwei import queries as Q
from yuanwei.errors import BadQuery, MissingVector


# ------------------------------------------------------------------ 规范化
@pytest.mark.parametrize("kw", [None, [], [""], ["  "], ["", "  ", "\t"], ["问题本身"], [" 问题本身 "], ("问题本身",)])
def test_no_effective_extras_means_single_path(kw):
    n = Q.normalize("问题本身", kw)
    assert n["extras"] == [] and n["mode"] == "single" and n["queries"] == ["问题本身"]


def test_clean_strip_dedup_and_keep_order():
    n = Q.normalize("原问题", [" 烤鱼 ", "贝壳", "烤鱼", "", "贝壳 "])
    assert n["extras"] == ["烤鱼", "贝壳"] and n["mode"] == "multi" and n["queries"] == ["原问题", "烤鱼", "贝壳"]


def test_dedup_is_exact_and_keeps_code_symbols():
    n = Q.normalize("q", ["C++", "c++", "C#", "C++"])
    assert n["extras"] == ["C++", "c++", "C#"]                      # 精确去重：大小写不同就是不同；不做模糊合并
    with pytest.raises(BadQuery):
        Q.normalize("q", ["C++", "c++", "C#", "c"])                  # 去重后 4 项


@pytest.mark.parametrize("q,kw", [
    ("问题", "烤鱼"),                                  # 不是列表
    ("问题", ["a", 3]),
    ("问题", [None]),
    ("问题", ["a", "b", "c", "d"]),                    # 去重后 4 项 > 3
    ("问题", ["x" * 129]),
    ("问题", [{"a": 1}]),
    ("问题", "a" * 3),
])
def test_bad_keywords_raise_before_anything(q, kw):
    with pytest.raises(BadQuery) as e:
        Q.normalize(q, kw)
    assert e.value.code == "bad_query"


def test_limits_are_exact_at_the_boundary():
    Q.normalize("q", ["字" * 128, "词" * 128, "句" * 128])           # 128×3＝384 正好
    with pytest.raises(BadQuery):
        Q.normalize("q", ["字" * 129])
    n = Q.normalize("q", ["字" * 128, "字" * 128, "字" * 128, "字" * 128])   # 精确去重后只剩 1 项
    assert len(n["extras"]) == 1
    with pytest.raises(BadQuery):
        Q.normalize("q", ["1", "2", "3", "4"])
    assert len(Q.normalize("q", ["😀" * 128])["extras"]) == 1           # 码点数，不是字节数、UTF-16 数


def test_empty_original_query_is_value_error():
    with pytest.raises(ValueError):
        Q.normalize("   ", ["a"])
    with pytest.raises(ValueError):
        Q.normalize(None, None)


# ------------------------------------------------------------------ 加权 RRF
def test_weighted_rrf_exact_arithmetic_and_provenance():
    orig = ["a", "b", "c"]
    ex1 = ["c", "d"]
    merged, prov = Q.merge_routes([orig, ex1], [Fraction(1), Fraction(1)], {"a": 1, "b": 2, "c": 3, "d": 4})
    assert merged == ["c", "a", "b", "d"]                           # c: 1/63+1/61 最高；b、d 都是 1/62，同分先看原问题名次
    assert prov["c"] == {0: 3, 1: 1} and prov["d"] == {1: 2} and prov["a"] == {0: 1}


def test_weights_original_one_extras_split_one():
    w = Q.weights(2)
    assert w == [Fraction(1), Fraction(1, 2), Fraction(1, 2)] and sum(w[1:]) == 1
    assert Q.weights(0) == [Fraction(1)] and Q.weights(3)[1] == Fraction(1, 3)


def test_tie_break_original_rank_then_best_extra_then_newer_message():
    # x、y 都只在补充路出现且分数相同：名次相同时按消息序号大者在前
    merged, _ = Q.merge_routes([[], ["x"], ["y"]], [Fraction(1), Fraction(1, 2), Fraction(1, 2)], {"x": 5, "y": 9})
    assert merged == ["y", "x"]
    merged, _ = Q.merge_routes([["a"], ["b"], ["a"]], [Fraction(1), Fraction(1, 2), Fraction(1, 2)], {"a": 1, "b": 2})
    assert merged == ["a", "b"]


def test_absent_route_counts_zero_and_no_per_route_truncation():
    big = ["m%d" % i for i in range(300)]
    merged, prov = Q.merge_routes([big, ["m299"]], [Fraction(1), Fraction(1)], {m: i for i, m in enumerate(big)})
    assert len(merged) == 300 and merged[0] == "m299" and prov["m299"] == {0: 300, 1: 1}


# ------------------------------------------------------------------ 选料：不给关键词和原路径逐字相同
def test_no_extras_is_the_reference_path_exactly(tmp_path):
    mem = organized(tmp_path)
    rd = mem._reader
    base = rd.select("吃了烤鱼")
    for kw in (None, [], [""], ["吃了烤鱼"]):
        got = rd.select_multi("吃了烤鱼", kw)
        assert got["material"] == base["material"] and got["hits"] == base["hits"] and got["table"] == base["table"]
        assert [r["msg_id"] for r in got["selected"]] == [r["msg_id"] for r in base["selected"]]
        assert got["routes"] is None and got["stats"]["queries"] == 1


def test_extras_bring_in_messages_the_original_misses_and_record_routes(tmp_path):
    mem = organized(tmp_path)
    rd = mem._reader
    q = "完全无关的话题"                                          # 和任何原话都没有共同的相邻两字
    assert rd.select(q)["selected"] == []
    got = rd.select_multi(q, ["贝壳", "日出"])
    texts = [r["content"] for r in got["selected"]]
    assert TEXTS[0] in texts and TEXTS[3] in texts
    mid0 = [r["msg_id"] for r in got["selected"] if r["content"] == TEXTS[0]][0]
    assert got["routes"][mid0] == {1: 1} and got["stats"]["queries"] == 3
    assert got["stats"]["weights"] == ["1", "1/2", "1/2"]


def test_pack_receives_original_query_for_same_occasion_supplement(tmp_path, monkeypatch):
    mem = organized(tmp_path)
    from yuanwei import pack as PK
    seen = []
    orig = PK.pack_reference

    def spy(hits, sidx, rows_by_id, mp, tmap, m2o, visible, searcher, query, **kw):
        seen.append(query)
        return orig(hits, sidx, rows_by_id, mp, tmap, m2o, visible, searcher, query, **kw)
    monkeypatch.setattr(PK, "pack_reference", spy)
    mem._reader.select_multi("原问题没有命中", ["贝壳", "烤鱼"])
    assert seen == ["原问题没有命中"]                                # 取材只走一次，同回补充只用原问题


def test_validation_runs_before_any_search(tmp_path):
    mem = organized(tmp_path)
    cnt = Counter(mem)
    try:
        for kw in ("烤鱼", ["a", "b", "c", "d"], ["字" * 129], [3]):
            with pytest.raises(BadQuery):
                mem._reader.select_multi("吃了烤鱼", kw)
        assert [c for c in cnt.calls if c != "select_multi"] == []              # 入口被调用了，但没有进到快照、候选、向量
    finally:
        cnt.close()


def test_hybrid_counts_queries_and_vector_calls_and_missing_vector_for_an_extra_is_an_error(tmp_path):
    mem, client = hybrid(tmp_path)
    got = mem._reader.select_multi("吃了烤鱼", ["贝壳"])
    assert got["stats"]["queries"] == 2 and got["stats"]["vector_calls"] == len(client.calls) == 1
    assert sorted(sum(client.calls, [])) == ["吃了烤鱼", "贝壳"]
    mem2, _ = hybrid(tmp_path, name="hy2.sqlite", client=FakeEmbedClient())
    mem2._reader.embed_client = None                                 # 没有向量客户端也没有缓存的查询向量：补充路失败就整体报错，不能少跑一路还算成功
    with pytest.raises(MissingVector):
        mem2._reader.select_multi("吃了烤鱼", ["贝壳"])
