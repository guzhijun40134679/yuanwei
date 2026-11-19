"""37：真实 Hermes 接头、默认取材参数，自造数据/临时库/假向量，断网。"""
import re

import pytest

from helpers import Clock, Scripted, make_mem
from helpers2 import IDENT, FakeEmbedClient
from test_hermes_workspace import World, call, turn
from yuanwei import Config, Memory, vectors as VX, relevance as RL


@pytest.fixture
def positive_floor(monkeypatch):
    # 假向量也要用真正为正的门槛：不能用 -1 把“不相关”判断关闭。
    monkeypatch.setitem(RL.CALIBRATIONS, RL.model_key(IDENT),
                        {"threshold": .9, "vector_fps": [VX.identity_fp(IDENT)], "evidence": "fix37-synthetic"})


class PendingWorld(World):
    def __init__(self, path, vector="keyword", **cfg):
        self.vector = vector
        super().__init__(path, scope="life_all", seed=False, mem_cfg=cfg)

    def mem(self, key):
        if key not in self.mems:
            hybrid = self.vector != "keyword"
            self.mems[key] = Memory.open(str(self.tmp / "shared.sqlite"), key,
                config=Config(retrieval="hybrid" if hybrid else "keyword", vector_fallback="partial", **self.mem_cfg),
                provider=Scripted("skip"), clock=Clock(), spawn="off",
                vector_identity=IDENT if hybrid else None, embed_client=FakeEmbedClient() if hybrid else None)
        return self.mems[key]


def saved(a, session, text="紫檀盒放在蓝色抽屉里。", n=1):
    turn(a, n, text)
    a.sync_turn(text, "收到，蓝色抽屉。", session_id=session,
                messages=[{"role": "user", "content": text}, {"role": "assistant", "content": "收到，蓝色抽屉。"}])
    return a.mem.store.list_messages(a.mem.ns)


def configure_vectors(w):
    for mem in w.mems.values():
        if w.vector == "ready":
            VX.register(mem.store, IDENT, {a.text: [1., 0., 0., 0.] for a in mem._reader.snapshot().anchors})
        if w.vector == "down":
            class Down:
                def embed_batch(self, texts):
                    raise VX.ServiceDown("synthetic service down")
            mem._reader.embed_client = Down()


@pytest.mark.parametrize("vector", ["keyword", "ready", "missing", "down"])
@pytest.mark.parametrize("cross", [False, True])
def test_A_saved_B_immediate_default_recall_note_and_by_id(tmp_path, positive_floor, vector, cross):
    w = PendingWorld(tmp_path, vector)
    a = w.adapter("/work/a" if cross else "/elsewhere", session="A")
    rows = saved(a, "A")
    b = w.adapter("/elsewhere", session="B")
    configure_vectors(w)
    turn(b, 1, "紫檀盒")
    out = call(b, "recall", {"query": "紫檀盒", "keywords": ["蓝色抽屉"]})
    assert out["status"] == "ok", out
    assert all(r["msg_id"] in out["text"] for r in rows), out
    assert a.mem.cfg.recall_budget == 8000 and a.mem.cfg.relevance_floor and a.mem.cfg.recall_ranked_pages
    assert all(a.mem.message_status(r["msg_id"]) == "已保存" for r in rows)
    # 旧代码的检索本身已通过；红点是缺少施工单要求的工具说明。原话正文不可插字。
    assert "未整理：" in out["text"]
    selection = a.mem._reader.select("紫檀盒")
    assert "未整理" not in selection["material"]
    for r in rows:
        assert a.mem.store.message_row(a.mem.ns, r["msg_id"])["content"] == r["content"]
    w.cfg["mode"] = "note"
    c = w.adapter("/elsewhere", session="C")
    configure_vectors(w)
    turn(c, 1, "紫檀盒")
    note = c.prefetch("紫檀盒", session_id="C")
    assert rows[0]["msg_id"] in note and "未整理：" in note
    direct = call(b, "get_messages", {"ids": [rows[0]["msg_id"]]})
    assert direct["status"] == "ok", direct


@pytest.mark.parametrize("vector", ["ready", "missing", "down"])
def test_pending_vectors_join_semantic_route_only_when_available(tmp_path, positive_floor, vector):
    w = PendingWorld(tmp_path, vector)
    a = w.adapter("/elsewhere", session="A")
    mid = saved(a, "A")[0]["msg_id"]
    b = w.adapter("/elsewhere", session="B")
    configure_vectors(w)
    class Exact:
        def embed_batch(self, texts):
            return [[1., 0., 0., 0.] for _ in texts]
    if vector != "down":
        a.mem._reader.embed_client = Exact()
    turn(b, 1, "unmatchedterm")
    out = call(b, "recall", {"query": "unmatchedterm"})
    assert out["status"] == "ok", out
    assert (mid in out["text"]) == (vector == "ready")
    if vector == "ready":
        assert "仅意思相近" in out["text"] and "未整理：" in out["text"]


def test_same_keyword_before_and_after_organization_finds_the_same_originals(tmp_path):
    w = PendingWorld(tmp_path)
    a = w.adapter("/elsewhere", session="A")
    rows = saved(a, "A")
    b = w.adapter("/elsewhere", session="B")
    turn(b, 1, "紫檀盒")
    before = call(b, "recall", {"query": "紫檀盒"})
    a.mem.end_session("hermes", "A"); a.mem.run()
    after = call(b, "recall", {"query": "紫檀盒"})
    assert all(r["msg_id"] in before["text"] and r["msg_id"] in after["text"] for r in rows)
    assert "未整理：" in before["text"] and "未整理：" not in after["text"]


def test_same_session_earlier_pending_is_delivered_and_marked_but_current_turn_is_not(tmp_path):
    """11-18 回到 11-10 规则：只排当前轮；同会话更早的原话（整理过的、没整理的）都能查到，没整理的在说明区标出。"""
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    old = mem.record("h", "S", "old", "user", "紫檀盒旧话")["msg_id"]
    mem.end_session("h", "S"); mem.run()
    pending = mem.record("h", "S", "pending", "user", "紫檀盒新话")["msg_id"]
    current = mem.record("h", "S", "cur", "user", "紫檀盒本轮话", turn_id="T")["msg_id"]
    others = [mem.record(h, s, "other", "user", "紫檀盒别处", branch=br)["msg_id"]
              for h, s, br in [("h2", "S", ""), ("h", "S2", ""), ("h", "S", "fork")]]
    kw = dict(host="h", session_id="S", branch="", turn_id="T")
    result = mem.recall("紫檀盒", **kw)
    got = {d["msg_id"] for d in result["delivered"]}
    assert {old, pending, *others} <= got and current not in got
    footer = result["text"][result["text"].index("未整理："):]
    assert pending in footer and old not in footer and current not in result["text"]
    note = mem.note("紫檀盒", **kw)
    assert pending in note["note"] and old in note["note"] and current not in note["note"]
    assert "未整理：" in note["note"]
    assert mem.get_messages([pending], **kw)["messages"][0]["msg_id"] == pending
    assert {pending, current} <= {d["msg_id"] for d in mem.recall("紫檀盒")["delivered"]}


def test_same_session_earlier_pending_is_delivered_by_real_adapter_recall_and_note(tmp_path):
    w = PendingWorld(tmp_path)
    w.cfg["mode"] = "note"
    a = w.adapter("/elsewhere", session="S")
    mid = saved(a, "S")[0]["msg_id"]
    turn(a, 2, "紫檀盒")
    out = call(a, "recall", {"query": "紫檀盒"})
    assert out["status"] == "ok" and mid in out["text"] and "未整理：" in out["text"]
    note = a.prefetch("紫檀盒", session_id="S")
    assert mid in note and "未整理：" in note
    assert mid in call(a, "get_messages", {"ids": [mid]})["text"]
