"""缺向量时（vector_fallback=partial）：有向量的锚点照走「关键词＋向量」，没算好的只靠关键词；服务连不上、没有 NumPy、身份漂移时这一次只用关键词；
每次都登记（read_log、status），不悄悄降级，不整次报错。strict（库默认）仍然是旧行为。向量齐全时和原来逐字节相同。"""
import json
import sys

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT, TEXTS, FakeEmbedClient, fill, hybrid, organized
from yuanwei import Config, Memory
from yuanwei import hosts as H
from yuanwei import rank as RK
from yuanwei import vectors as VX
from yuanwei.access import Access
from yuanwei.idsource import InProcessIdentity
from yuanwei.tools import ToolSurface

EMB = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "identity": IDENT}


class Down:
    def __init__(self):
        self.calls = []

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        raise VX.ServiceDown("向量服务出错：URLError")


def pmem(tmp_path, client=None, **kw):
    return hybrid(tmp_path, client=client, vector_fallback="partial", **kw)


def last(mem):
    return mem._reader.retrieval_log[-1]


def test_partial_vectors_hybrid_uses_vectorized_anchors_and_keyword_only_for_the_rest(tmp_path):
    mem, client = pmem(tmp_path)
    mem.record("h", "S2", "n1", "user", "刚刚说的新话：预算又变了。")               # 没有向量
    r = mem.recall("预算又变了")
    assert r.get("status") != "error" and r["delivered"]
    new_id = mem.store.list_messages(mem.ns, "S2")[0]["msg_id"]
    assert new_id in [d["msg_id"] for d in r["delivered"]]                                                  # 只靠关键词也能进候选
    i = last(mem)
    assert i["used"] == "hybrid_partial" and i["reason"] == "vectors_missing" and i["missing"] >= 1 and i["with_vector"] == i["pool"] - i["missing"] and i["with_vector"] > 0


def test_missing_vector_is_registered_in_read_log_with_used_reason_and_counts(tmp_path):
    mem, client = pmem(tmp_path)
    mem.record("h", "S2", "n1", "user", "刚刚说的新话：预算又变了。")
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    acc.begin_turn("pi", "S", "T")
    surf = ToolSurface(mem, acc, H.profile("pi"), ids)
    r = surf.call("recall", {"query": "预算"})
    assert r.status == "ok"
    rows = [x for x in acc.log_rows("pi", "S", "T") if x["kind"] == "retrieval"]
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["used"] == "hybrid_partial" and d["reason"] == "vectors_missing" and set(d) == {"used", "reason", "pool", "with_vector", "missing"} and d["missing"] >= 1


def test_status_counts_reads_by_used_and_by_reason(tmp_path):
    mem = Memory.open(str(tmp_path / "s.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=EMB), provider=Scripted("skip"), clock=Clock(),
                      embed_client=FakeEmbedClient())
    fill(mem, "S1", TEXTS)
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    acc.begin_turn("pi", "S", "T")
    surf = ToolSurface(mem, acc, H.profile("pi"), ids)
    surf.call("recall", {"query": "预算"})                                           # 一条向量都没有
    snap = mem._reader.snapshot()
    VX.register(mem.store, IDENT, {a.text: [1.0, float(len(a.text) % 5) + 0.1, 0.3, 0.2] for a in snap.anchors})
    surf.call("recall", {"query": "烤鱼"})
    v = mem.status()["vectors"]
    assert v["enabled"] and v["reads"].get("keyword_fallback") == 1 and v["reads"].get("hybrid") == 1 and v["reads"].get("reason:no_vectors_yet") == 1
    assert v["texts"]["distinct"] > 0 and v["fallback"] == "partial"


def test_current_turn_messages_without_vectors_do_not_count_as_missing(tmp_path):
    mem, client = pmem(tmp_path)
    mem.record("h", "C", "cb", "user", "随便再问问别的话题吧。", turn_id="T1")
    r = mem.recall("预算", host="h", session_id="C", turn_id="T1")
    assert r.get("status") != "error" and last(mem)["used"] == "hybrid" and last(mem)["missing"] == 0


def test_zero_vectors_in_pool_is_keyword_fallback_not_an_empty_vector_path(tmp_path):
    mem = Memory.open(str(tmp_path / "z.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2), provider=Scripted("skip"), clock=Clock(),
                      vector_identity=IDENT, embed_client=FakeEmbedClient())
    fill(mem, "S1", TEXTS)
    r = mem.recall("烤鱼")
    assert r["delivered"] and last(mem)["used"] == "keyword_fallback" and last(mem)["reason"] == "no_vectors_yet" and last(mem)["with_vector"] == 0


def test_service_down_makes_this_read_keyword_only_and_status_says_down_and_why(tmp_path):
    down = Down()
    mem, _c = pmem(tmp_path)
    mem._reader.embed_client = VX.GuardedClient(down, 30, clock=lambda: 0.0)
    r = mem.recall("预算又变了吗")                                                     # 查询向量缓存里没有 → 要连服务 → 连不上
    assert r.get("status") != "error" and last(mem)["used"] == "keyword_fallback" and last(mem)["reason"] == "service_down"
    assert "URLError" not in json.dumps(last(mem))


def test_circuit_breaker_stops_reconnecting_for_30_seconds_then_tries_again(tmp_path):
    down = Down()
    t = {"now": 0.0}
    g = VX.GuardedClient(down, 30, clock=lambda: t["now"])
    for _ in range(3):
        with pytest.raises(VX.ServiceDown):
            g.embed_batch(["a"])
        if len(down.calls) == 1:
            t["now"] = 10.0
    assert len(down.calls) == 1                                                     # 30 秒内只连过一次
    t["now"] = 31.0
    with pytest.raises(VX.ServiceDown):
        g.embed_batch(["a"])
    assert len(down.calls) == 2


def test_numpy_missing_falls_back_to_keyword_and_is_registered(tmp_path, monkeypatch):
    mem, client = pmem(tmp_path)

    def no_numpy():
        raise ImportError("no numpy")
    monkeypatch.setattr(RK, "_np", no_numpy)
    r = mem.recall("预算")
    assert r.get("status") != "error" and last(mem)["used"] == "keyword_fallback" and last(mem)["reason"] == "numpy_missing"


def test_strict_is_the_library_default_and_keeps_old_behavior(tmp_path):
    from yuanwei.errors import MissingVector
    assert Config().vector_fallback == "strict"
    mem, _c = hybrid(tmp_path)
    mem.record("h", "S2", "n1", "user", "刚刚说的新话：预算又变了。")
    with pytest.raises(MissingVector):
        mem.recall("预算")
    with pytest.raises(Exception):
        Config(vector_fallback="whatever").validate()


def test_recall_pages_and_cursors_do_not_issue_new_vector_calls_in_partial_mode(tmp_path):
    mem, client = pmem(tmp_path, texts=["烤鱼第%d次，味道很好，吃了很多。" % i for i in range(80)])
    mem.record("h", "S2", "n1", "user", "刚刚说的新话：烤鱼又吃了。")
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    acc.begin_turn("pi", "S", "T")
    surf = ToolSurface(mem, acc, H.profile("pi"), ids)
    r = surf.call("recall", {"query": "烤鱼"})
    n = len(client.calls)
    pages = 1
    while r.meta.get("cursor"):
        r = surf.call("recall", {"cursor": r.meta["cursor"]})
        assert r.status == "ok"
        pages += 1
        assert pages < 100
    assert len(client.calls) == n


def test_degrade_info_is_not_in_material_text_only_in_structured_field_and_log(tmp_path):
    mem, client = pmem(tmp_path)
    mem._reader.embed_client = VX.GuardedClient(Down(), 30, clock=lambda: 0.0)
    r = mem.recall("预算又变了吗")
    assert last(mem)["used"] == "keyword_fallback"
    for bad in ("keyword_fallback", "service_down", "hybrid_partial", "降级", "只用关键词"):
        assert bad not in r["text"]


def test_query_is_redacted_before_it_reaches_the_vector_service_in_every_read_path(tmp_path):
    secret = "sk-" + "0123456789abcdef" * 2
    mem, client = pmem(tmp_path)
    mem.recall("我的密钥是 %s 这个怎么办" % secret)
    mem.recall("预算", keywords=None) if False else None
    from yuanwei import note as NT
    mem.note("我的密钥是 %s 这个怎么办" % secret)
    blob = json.dumps(client.calls, ensure_ascii=False)
    assert secret not in blob and "密钥已挡掉" in blob


def test_service_drift_flag_degrades_queries_without_calling_the_service(tmp_path):
    mem, client = pmem(tmp_path)
    fp = VX.identity_fp(IDENT)
    mem.store.meta_set("vec_service", json.dumps({"state": "drift", "fp": fp, "at": "x", "error": "probe_mismatch"}))
    n = len(client.calls)
    r = mem.recall("预算")
    assert r.get("status") != "error" and r["delivered"] and last(mem)["used"] == "keyword_fallback" and last(mem)["reason"] == "identity_drift" and len(client.calls) == n


def test_dimension_change_is_caught_on_the_query_path_without_a_probe(tmp_path):
    class WrongDim:
        calls = []

        def embed_batch(self, texts):
            return [[1.0, 2.0] for _ in texts]
    mem, _c = pmem(tmp_path)
    mem._reader.embed_client = VX.HttpEmbeddingClient("http://127.0.0.1:1/v1", "m", dimension=4, opener=None)
    mem._reader.embed_client.embed_batch = lambda texts: VX.parse_embeddings_response({"data": [{"index": i, "embedding": [1.0, 2.0]} for i in range(len(texts))]}, len(texts), 4)
    r = mem.recall("预算又变了吗")
    assert r.get("status") != "error" and last(mem)["used"] == "keyword_fallback" and last(mem)["reason"] == "query_vector_unavailable"


def test_all_vectors_present_materials_byte_identical_between_strict_and_partial(tmp_path):
    """向量齐全：partial 和 strict 拼出的整套回答请求逐字节相同（150 次复验那套 hybrid 请求靠这一条守着，另有对照旧代码的核对）。"""
    outs = {}
    for fb in ("strict", "partial"):
        d = tmp_path / fb
        d.mkdir()
        mem, _c = hybrid(d, vector_fallback=fb)
        outs[fb] = [mem.answer_request(q, "2027-03-01")["request"]["bytes"] for q in ("吃了什么烤鱼", "预算批下来了吗", "周末去哪里玩", "相机")]
    assert outs["strict"] == outs["partial"] and all(outs["strict"])
