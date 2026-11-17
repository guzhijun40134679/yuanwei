"""小修 31：真实读取/装料/分页链、合成原话与手工向量；分数不代表真实模型效果。"""
import dataclasses
import json
import math

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT
from yuanwei import Config, Memory, relevance as RL, vectors as VX, note as NT, pack as PK
from yuanwei import hosts as H
from yuanwei.access import Access
from yuanwei.idsource import InProcessIdentity
from yuanwei.tools import ToolSurface


@pytest.fixture
def calibrated(monkeypatch):
    monkeypatch.setattr(RL, "CALIBRATIONS", {RL.model_key(IDENT): {"threshold": 0.8, "vector_fps": [VX.identity_fp(IDENT)], "evidence": "synthetic-floor31"}})


def memory(tmp_path, texts, scores, query, *, neighbors=0, thread=False, name="floor.sqlite", **cfg):
    mem = Memory.open(str(tmp_path/name), "w", config=Config(retrieval="hybrid", neighbors=neighbors, **cfg), vector_identity=IDENT,
                      provider=Scripted("thread" if thread else "skip"), clock=Clock())
    ids = []
    for i, text in enumerate(texts):
        ids.append(mem.record("h", "S", str(i), "user", text, time="2027-02-01")["msg_id"])
    mem.end_session("h", "S")
    mem.run()
    snap = mem._reader.snapshot()
    score_map = dict(zip(texts, scores))
    vecs = {a.text: [score_map[a.text], math.sqrt(max(0, 1-score_map[a.text]**2)), 0, 0] for a in snap.anchors}
    vecs[query] = [1, 0, 0, 0]
    VX.register(mem.store, IDENT, vecs)
    return mem, ids


@pytest.mark.parametrize("query,text", [("spektralab", "晚餐吃了面条"), ("spectral calibration", "Dinner was noodles")])
def test_low_vectors_no_keywords_return_no_material_or_pages(tmp_path, calibrated, query, text):
    mem, ids = memory(tmp_path, [text], [0.2], query)
    r = mem.recall(query)
    assert r["selected_total"] == 0 and r["delivered"] == [] and r["cursor"] is None and not r["has_more"]
    assert "没有找到相关记录" in r["text"] and text not in r["text"]
    n = mem.note(query)
    assert n["note"] == "" and n["cursor"] is None and not n["unshown"]


@pytest.mark.parametrize("query,text", [("资金计划", "经费安排"), ("financial plan", "budget arrangement")])
def test_semantic_only_passes_at_threshold_and_preserves_quote(tmp_path, calibrated, query, text):
    mem, ids = memory(tmp_path, [text], [0.8], query)
    r = mem.recall(query)
    assert [x["msg_id"] for x in r["delivered"]] == ids
    assert "仅意思相近｜余弦 0.8000" in r["text"] and r["text"].endswith(text)
    n = mem.note(query)
    assert ids == n["delivered"] and "仅意思相近｜余弦 0.8000" in n["note"]


def test_keyword_passes_even_with_low_cosine(tmp_path, calibrated):
    mem, ids = memory(tmp_path, ["预算已获批准"], [-0.9], "预算")
    r = mem.recall("预算")
    assert r["delivered"] and "关键词命中" in r["text"] and "仅意思相近" not in r["text"]


def test_mixed_filters_direct_hits_and_marks_only_existing_neighbors(tmp_path, calibrated):
    texts = ["经费安排", "晚饭面条", "散步公园", "小狗睡觉"]
    mem, ids = memory(tmp_path, texts, [0.95, 0.1, 0.2, 0.1], "资金计划", neighbors=1)
    r = mem._reader.select("资金计划")
    assert r["hits"] == ids[:1]
    assert [x["msg_id"] for x in r["selected"]] == ids[:2]
    assert r["selected"][0]["retrieval_reason"]["kind"] == "semantic"
    assert r["selected"][1]["retrieval_reason"] == {"kind": "neighbor", "parent": ids[0]}
    assert "附带邻近材料" in r["material"] and texts[2] not in r["material"]
    assert PK.read_back(r["material"], lambda mid: mem._reader.mp.encode(mem.store.message_row("w", mid)["content"]))["order"] == ids[:2]


def test_same_occasion_material_kept_and_not_claimed_as_a_hit(tmp_path, calibrated):
    texts = ["经费安排", "晚饭面条", "散步公园"]
    mem, ids = memory(tmp_path, texts, [0.95, 0.1, 0.2], "资金计划", thread=True)
    r = mem._reader.select("资金计划")
    assert r["hits"] == ids[:1] and len(r["selected"]) == 3
    assert all(x["retrieval_reason"]["kind"] == "occasion" for x in r["selected"][1:])
    assert "附带同回材料" in r["material"]


def test_unknown_model_uses_keywords_and_never_calls_query_client(tmp_path, monkeypatch):
    monkeypatch.setattr(RL, "CALIBRATIONS", {})
    mem, ids = memory(tmp_path, ["经费安排", "预算获准"], [1, 1], "资金计划")
    class Forbidden:
        def embed_batch(self, _texts):
            raise AssertionError("未标定时不能发向量请求")
    mem._reader.embed_client = Forbidden()
    assert mem.recall("资金计划")["selected_total"] == 0
    assert mem.recall("预算")["delivered"]
    info = mem._reader.retrieval_log[-1]
    assert info["used"] == "keyword_fallback" and info["reason"] == "semantic_uncalibrated"
    assert "换了说法的内容暂时找不到" in info["relevance"]["message"]
    assert "推荐" in info["relevance"]["recommendation"]


def test_same_model_key_reuses_floor_for_any_runtime_and_marks_unlisted_one(calibrated):
    original = RL.resolve(IDENT)
    assert original["status"] == "calibrated" and original["basis"] == "measured" and original["message"] == "语义阈值已标定"
    other = dict(IDENT, provider="other", model="alias", runtime_version="new")
    assert RL.model_key(other) == RL.model_key(IDENT) and VX.identity_fp(other) != VX.identity_fp(IDENT)
    r = RL.resolve(other)                                                  # 名单外的运行程序：同一个模型文件，沿用门槛，注明未实测
    assert r["status"] == "calibrated" and r["threshold"] == original["threshold"] and r["basis"] == "same_file_unlisted_runtime"
    assert "沿用同模型文件的语义阈值" in r["message"] and "当前运行程序未实测" in r["message"] and r["recommendation"] is None
    assert RL.resolve(dict(other, context_length=4096))["basis"] == "same_file_unlisted_runtime"      # 上下文长度不进模型层 key
    RL.CALIBRATIONS[RL.model_key(IDENT)]["vector_fps"].append(VX.identity_fp(other))                 # 进了名单就按名单上的基准
    assert RL.resolve(other)["basis"] == "measured"
    for field, value in [("model_file_sha256", "y"*64), ("prefix", "different"), ("dimension", 2048), ("normalization", "none"), ("template", "t"), ("text_processing", "lower")]:
        r = RL.resolve(dict(other, **{field: value}))                      # 模型层任何一项不同：照旧未标定
        assert r["status"] == "uncalibrated" and r["threshold"] is None


def test_explicitly_authorized_same_file_runtime_is_honestly_marked(calibrated):
    other = dict(IDENT, provider="bundled", runtime_version="unmeasured-runtime")
    fp = VX.identity_fp(other)
    assert RL.resolve(other)["basis"] == "same_file_unlisted_runtime"
    record = RL.CALIBRATIONS[RL.model_key(IDENT)]
    record["vector_fps"].append(fp)
    record["runtime_basis"] = {fp: "same_file_authorized_unmeasured"}
    result = RL.resolve(other)
    assert result["threshold"] == 0.8 and result["basis"] == "same_file_authorized_unmeasured"
    assert "沿用同模型文件" in result["message"] and "当前运行程序未实测" in result["message"]
    assert RL.resolve(dict(other, runtime_version="another-runtime"))["basis"] == "same_file_unlisted_runtime"


def test_real_registry_old_lm_studio_runtime_is_calibrated_not_keyword_only():
    """用户本机的库是早先的 LM Studio 旧运行程序建的：同一个模型文件、运行程序和上下文长度不在名单上。这里用登记记录里的 LM Studio 身份换成旧运行程序造等价身份（不读任何真实库）。"""
    lm = {"provider": "lmstudio", "model": "text-embedding-qwen3-embedding-0.6b", "dimension": 1024, "context_length": 32768, "normalization": "l2", "prefix": "none", "template": "none",
          "text_processing": "verbatim", "model_file_sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439", "runtime_version": "llama.cpp-mac-arm64-apple-metal-advsimd@2.76.0"}
    measured = RL.resolve(lm)
    assert measured["status"] == "calibrated" and measured["basis"] == "measured" and measured["model_key"].startswith("4023a4a7")
    old = dict(lm, runtime_version="llama.cpp-mac-arm64-apple-metal-advsimd@2.53.0", context_length=2048)
    assert VX.identity_fp(old) not in RL.CALIBRATIONS[measured["model_key"]]["vector_fps"]
    r = RL.resolve(old, VX.identity_fp(old))
    assert r["status"] == "calibrated" and r["threshold"] == measured["threshold"] == 0.4319852256245753
    assert r["basis"] == "same_file_unlisted_runtime" and "当前运行程序未实测" in r["message"] and r["model_key"] == measured["model_key"]
    assert RL.resolve(dict(old, model_file_sha256="0" * 64))["status"] == "uncalibrated"             # 换了模型文件就不沿用
    with pytest.raises(VX.VectorIdentityMismatch):                                                  # 身份和 vector_fp 对不上照旧报错
        RL.resolve(old, VX.identity_fp(lm))


def test_keyword_only_zero_hits(tmp_path):
    mem = Memory.open(str(tmp_path/"kw.sqlite"), "w", config=Config(), provider=Scripted("skip"), clock=Clock())
    mem.record("h", "S", "1", "user", "晚饭面条")
    r = mem.recall("spektralab")
    assert r["selected_total"] == 0 and r["cursor"] is None
    assert mem.note("spektralab")["note"] == ""


def test_multi_route_labels_use_keyword_if_any_route_matches(tmp_path, calibrated):
    mem, ids = memory(tmp_path, ["经费安排", "公园散步"], [0.95, 0.2], "资金计划")
    VX.register(mem.store, IDENT, {"经费": [-1,0,0,0]})
    result = mem._reader.select_multi("资金计划", ["经费"])
    assert result["hits"] == ids[:1] and result["selected"][0]["retrieval_reason"]["kind"] == "keyword"


def test_paginated_recall_keeps_reason_and_original_ranges(tmp_path, calibrated):
    text = "经费安排" * 30
    mem, ids = memory(tmp_path, [text], [0.95], "资金计划", recall_ranked_pages=False)
    r = mem.recall("资金计划", limit_chars=100)
    chunks = []
    while True:
        assert "仅意思相近｜余弦 0.9500" in r["text"]
        for d in r["delivered"]:
            chunks.append(text[d["start"]:d["end"]])
        if not r["cursor"]:
            break
        r = mem.recall(None, cursor=r["cursor"], limit_chars=100)
    assert "".join(chunks) == text


def test_note_unshown_cursor_keeps_source_and_budget(tmp_path, calibrated):
    texts = ["经费安排" + str(i) for i in range(20)]
    mem, ids = memory(tmp_path, texts, [0.95]*20, "资金计划", note_chars=900, note_tokens=900)
    n = mem.note("资金计划")
    assert n["chars"] == len(n["note"]) <= 900 and n["cursor"] and n["unshown"]
    out = mem.get_messages(cursor=n["cursor"])
    assert all(m["retrieval_reason"]["kind"] == "semantic" for m in out["messages"])


def test_cross_workspace_no_hits_has_no_more_material_or_workspace_hints(tmp_path):
    from test_hermes_workspace import World, turn, call
    w = World(tmp_path, scope="all_open")
    a = w.adapter("/elsewhere")
    turn(a, 1, "ordinary prompt")
    r = call(a, "recall", {"query": "spektralab"})
    assert r["status"] == "ok" and "没有找到相关记录" in r["text"]
    assert "下一页" not in r["text"] and "工作区的结果没放进来" not in r["text"] and "【原文】" not in r["text"]


def test_cross_workspace_empty_sections_are_not_material(tmp_path):
    from test_hermes_workspace import World, turn, call
    w = World(tmp_path, scope="all_open")
    a = w.adapter("/elsewhere")
    turn(a, 1, "ordinary prompt")
    r = call(a, "recall", {"query": "咖喱"})
    assert r["status"] == "ok" and "关键词命中" in r["text"]
    assert r["text"].count("〔工作区：") == 1
    assert "工作区的结果没放进来" not in r["text"]


def test_cross_workspace_note_no_hits_is_empty(tmp_path):
    from test_hermes_workspace import World, turn
    w = World(tmp_path, scope="all_open")
    w.cfg["mode"] = "note"
    a = w.adapter("/elsewhere")
    turn(a, 1, "spektralab")
    token = a._cur[a.session]
    extras = [(key, key, m, i) for i, (key, m) in enumerate(w.mems.items()) if key != a.ws["key"]]
    result = a.runner.pre_answer(a.session, token, "spektralab", extras=extras, primary=(a.ws["key"], "生活", 0))
    assert result["inject"] is None and result["kind"] == "note_none"


def _legacy_cursor(mem, token, *, keep_marker=False):
    cid = token.split(":")[0]
    data = mem.store.get_cursor(cid)["data"]
    if not keep_marker:
        data.pop("relevance_checked", None)
    for chunk in data["chunks"]:
        chunk.pop("retrieval_reason", None)
    mem.store.update_cursor(cid, data)


@pytest.mark.parametrize("tool_path", [False, True])
@pytest.mark.parametrize("keep_marker", [False, True])
def test_old_recall_cursor_cannot_bypass_relevance_gate(tmp_path, calibrated, tool_path, keep_marker):
    from yuanwei import paging
    from yuanwei.errors import BadCursor
    mem, ids = memory(tmp_path, ["经费安排"*50, "经费安排"*50], [0.95, 0.95], "资金计划", recall_page_chars=450)
    path = H.profile("pi").paths["mcp_return"]
    first = paging.recall_first(mem._reader, "资金计划", None, None, path) if tool_path else mem.recall("资金计划")
    token = first["cursor"]
    assert token and mem.store.get_cursor(token.split(":")[0])["data"]["relevance_checked"] is True
    _legacy_cursor(mem, token, keep_marker=keep_marker)
    with pytest.raises(BadCursor, match="重新调用 recall"):
        if tool_path:
            paging.recall_next(mem._reader, token, path)
        else:
            mem._reader.recall(None, cursor=token)


def test_old_note_cursor_is_refused_but_explicit_id_cursor_still_continues(tmp_path, calibrated):
    from yuanwei.errors import BadCursor
    texts = ["经费安排" + str(i) for i in range(20)]
    mem, ids = memory(tmp_path, texts, [0.95]*20, "资金计划", note_chars=900, note_tokens=900)
    note = mem.note("资金计划")
    assert note["cursor"]
    _legacy_cursor(mem, note["cursor"])
    with pytest.raises(BadCursor, match="重新调用 recall"):
        mem._reader.get_messages(cursor=note["cursor"])
    by_id = mem._reader.get_messages(ids=ids, page_tokens=25)
    assert by_id["cursor"]
    cid = by_id["cursor"].split(":")[0]
    data = mem.store.get_cursor(cid)["data"]
    assert "origin" not in data and "relevance_checked" not in data
    continued = mem._reader.get_messages(cursor=by_id["cursor"], page_tokens=25)
    assert continued["messages"]
