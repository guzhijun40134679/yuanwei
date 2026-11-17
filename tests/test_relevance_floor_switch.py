"""小修 31 的旧参照开关：配置 relevance_floor（默认 True）。False＝恢复 31 之前的取材（不筛候选、不带来源说明、没标定的向量模型也照用向量），只给研究回放和旧参照用。"""
import math

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT
from yuanwei import Config, Memory, relevance as RL, vectors as VX
from yuanwei import relevance_diagnostics as RDG
from yuanwei.errors import ConfigError

TEXTS = ["经费安排", "晚饭面条", "散步公园"]
SCORES = [0.95, 0.1, 0.2]
QUERY = "资金计划"                                    # 和任何一条原话没有共同词项：只靠向量
LABELS = ("关键词命中", "仅意思相近", "附带邻近材料", "附带同回材料")


def build(tmp_path, registry, monkeypatch, name, *, neighbors=1, scores=SCORES, **cfg):
    monkeypatch.setattr(RL, "CALIBRATIONS", registry)
    mem = Memory.open(str(tmp_path / name), "w", config=Config(retrieval="hybrid", neighbors=neighbors, **cfg), vector_identity=IDENT, provider=Scripted("skip"), clock=Clock())
    ids = [mem.record("h", "S", str(i), "user", t, time="2027-02-01")["msg_id"] for i, t in enumerate(TEXTS)]
    mem.end_session("h", "S")
    mem.run()
    snap = mem._reader.snapshot()
    score_map = dict(zip(TEXTS, scores))
    vecs = {a.text: [score_map[a.text], math.sqrt(1 - score_map[a.text] ** 2), 0, 0] for a in snap.anchors}
    vecs[QUERY] = [1, 0, 0, 0]
    vecs["资金预算"] = [1, 0, 0, 0]                      # 多路查询的补充关键词
    VX.register(mem.store, IDENT, vecs)
    return mem, ids


CALIBRATED = lambda: {RL.model_key(IDENT): {"threshold": 0.8, "vector_fps": [VX.identity_fp(IDENT)], "evidence": "synthetic-switch"}}  # noqa: E731


def test_default_is_floor_on():
    assert Config().relevance_floor is True


def test_default_applies_floor_calibrated(tmp_path, monkeypatch):
    mem, ids = build(tmp_path, CALIBRATED(), monkeypatch, "on.sqlite")
    sel = mem._reader.select(QUERY)
    assert sel["hits"] == ids[:1]                                          # 只有 0.95 过 0.8 的门；0.1、0.2 被挡
    assert [x["msg_id"] for x in sel["selected"]] == ids[:2]               # 命中那条加一条邻近
    assert "仅意思相近" in sel["material"] and "附带邻近材料" in sel["material"]


def test_default_applies_floor_uncalibrated_means_keywords_only(tmp_path, monkeypatch):
    mem, ids = build(tmp_path, {}, monkeypatch, "on-unc.sqlite")
    assert mem.recall(QUERY)["selected_total"] == 0                        # 没标定：只留关键词命中，这一问没有关键词
    assert mem._reader.retrieval_log[-1]["reason"] == "semantic_uncalibrated"


@pytest.mark.parametrize("registry", [CALIBRATED, dict], ids=["calibrated", "uncalibrated"])
def test_switch_off_restores_pre31_selection(tmp_path, monkeypatch, registry):
    mem, ids = build(tmp_path, registry(), monkeypatch, "off.sqlite", relevance_floor=False)
    sel = mem._reader.select(QUERY)
    # 31 之前：向量前 50（这里池里只有 3 个锚点）全部是候选，按向量名次排；没有关键词命中也照样交
    assert sel["hits"] == ids[:1] + [ids[2], ids[1]]
    assert [x["msg_id"] for x in sel["selected"]] == ids
    assert not any(x.get("retrieval_reason") for x in sel["selected"])
    assert not any(lb in sel["material"] for lb in LABELS)                 # 引用行回到〔角色｜编号〕，没有来源说明
    assert sel["material"].splitlines()[-1] == "〔用户｜%s〕%s" % (ids[2], TEXTS[2])
    assert all(r[3] is not None for r in sel["table"])                     # 候选全表里有向量名次：向量真的用上了（没标定也一样）


def test_switch_off_recall_pages_and_cursors_work(tmp_path, monkeypatch):
    mem, ids = build(tmp_path, {}, monkeypatch, "off-page.sqlite", relevance_floor=False, neighbors=0)
    r = mem.recall(QUERY)
    assert [x["msg_id"] for x in r["delivered"]] == ids and not any(lb in r["text"] for lb in LABELS)
    first = mem.recall(QUERY, limit_chars=145)                                # 页放不下全部：要有续读游标
    assert first["has_more"] and first["cursor"]
    nxt = mem.recall(cursor=first["cursor"], limit_chars=145)                  # 关着下限时，没有来源绑定的游标照常能续（默认档会要求重新查询）
    assert nxt["delivered"] and not any(lb in nxt["text"] for lb in LABELS)


@pytest.mark.parametrize("floor", [True, False])
def test_cursor_without_relevance_binding_refused_by_default_only(tmp_path, monkeypatch, floor):
    from yuanwei.errors import BadCursor
    mem, _ids = build(tmp_path, CALIBRATED(), monkeypatch, "cursor.sqlite", relevance_floor=floor, neighbors=0, scores=[0.95, 0.9, 0.2])
    first = mem.recall(QUERY, limit_chars=140)
    assert first["has_more"] and first["cursor"]
    cid = first["cursor"].split(":")[0]
    data = mem.store.get_cursor(cid)["data"]
    data.pop("relevance_checked", None)
    for chunk in data["chunks"]:
        chunk.pop("retrieval_reason", None)
    mem.store.update_cursor(cid, data)                                       # 做成「31 之前建立的旧游标」
    if floor:
        with pytest.raises(BadCursor, match="重新调用 recall"):
            mem._reader.recall(None, cursor=first["cursor"])
    else:
        assert mem._reader.recall(None, cursor=first["cursor"], limit_chars=140)["delivered"]


@pytest.mark.parametrize("floor", [True, False])
def test_multi_route_and_note_follow_the_switch(tmp_path, monkeypatch, floor):
    mem, ids = build(tmp_path, CALIBRATED(), monkeypatch, "paths.sqlite", relevance_floor=floor)
    multi = mem._reader.select_multi(QUERY, keywords=["资金预算"])
    note = mem.note(QUERY)
    if floor:
        assert multi["hits"] == ids[:1] and "仅意思相近" in multi["material"]
        assert "仅意思相近" in note["note"]
    else:
        assert multi["hits"] == ids[:1] + [ids[2], ids[1]] and not any(lb in multi["material"] for lb in LABELS)
        assert note["note"] and not any(lb in note["note"] for lb in LABELS)


def test_switch_off_status_is_off_and_not_a_calibration_gap(tmp_path, monkeypatch):
    mem, _ids = build(tmp_path, {}, monkeypatch, "off-status.sqlite", relevance_floor=False)
    assert mem.status()["retrieval"]["relevance"]["status"] == "off"
    assert RDG.from_config(Config(retrieval="hybrid", relevance_floor=False))["status"] == "off"
    assert RDG.from_config(Config(retrieval="hybrid", relevance_floor=True), identity=IDENT)["status"] == "uncalibrated"


def test_relevance_floor_must_be_a_bool():
    Config(relevance_floor=False).validate()
    for bad in (0, 1, "false", None):
        with pytest.raises(ConfigError):
            Config(relevance_floor=bad).validate()
    assert Config.from_dict({"relevance_floor": False}).relevance_floor is False
