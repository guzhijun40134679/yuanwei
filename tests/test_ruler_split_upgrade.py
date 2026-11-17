"""升级测试（施工单 4.2 节，R4）：用旧配置（索引版本 2、split-1、尺子 1）封好的步和建好的工作区，换成新默认配置（索引版本 3、split-2、尺子 2）打开以后的行为。
每种情形都核请求文字、任务指纹、调用次数、已存的范围，不只核「返回成功」。全部自造数据，不联网。"""
import json

import pytest

from helpers import Clock, Scripted, make_mem, out_skip, out_thread, sids_of
from yuanwei import Config, Memory
from yuanwei import step as STEP
from yuanwei.cards import LightConfig
from yuanwei.errors import BadCursor

LEGACY = dict(index_version=2, split_version="split-1", ruler_version=1)
TEXT_U = "Hello there. How are you today? I am fine."          # 旧切法一句；新切法三句
TEXT_A = "Great. See you soon, my friend."


def open_legacy(tmp_path, provider=None, name="m.sqlite", clock=None, **cfg):
    return Memory.open(str(tmp_path / name), "w", config=Config(**dict(LEGACY, **cfg)), provider=provider, clock=clock or Clock(), spawn="off")


def open_new(tmp_path, provider=None, name="m.sqlite", clock=None, **cfg):
    return Memory.open(str(tmp_path / name), "w", config=Config(**cfg), provider=provider, clock=clock or Clock(), spawn="off")


def one_session(mem, session="S1", prefix=""):
    mem.record("h", session, prefix + "u1", "user", TEXT_U, time="2027-01-01")
    mem.record("h", session, prefix + "a1", "assistant", TEXT_A, time="2027-01-01")
    mem.end_session("h", session)


def snapshot_of(mem, step_id="step-0001"):
    st = mem.store.get_step("w", step_id)
    return st, json.loads(st["config_snapshot"])


def facts(mem, step_id="step-0001"):
    """请求文字、任务指纹、调用次数、已存的句子范围、收据里的输入指纹。"""
    q = {k: [(j["task_fp"], j["request_fp"], j["request"]["text"], j["status"]) for j in mem.store.list_judgments("w", step_id, k)] for k in ("q1", "q2")}
    st = mem.store.read_state("w")
    sents = sorted((s["msg_id"], s["start"], s["end"], s["text"]) for s in st["tables"]["sentences"])
    rc = [r for r in st["tables"]["receipts"] if r["key"] == "step:" + step_id]
    return {"judgments": q, "sentences": sents, "receipt_input_fp": rc[0]["input_fp"] if rc else None, "n_receipts": len(rc)}


def baseline(tmp_path, name, mode="skip"):
    """基线：从头到尾都用旧配置跑同一份对话。"""
    prov = Scripted(mode)
    mem = open_legacy(tmp_path, provider=prov, name=name)
    one_session(mem)
    assert mem.run()["done"] == 1
    return mem, prov


# ---------------------------------------------------------------- 1 旧步还没发
def test_old_sealed_step_not_yet_sent_keeps_old_split_ruler_and_fingerprints(tmp_path):
    base, bprov = baseline(tmp_path, "base.sqlite")
    base_facts = facts(base)
    m1 = open_legacy(tmp_path, provider=Scripted("skip"), name="up.sqlite")
    one_session(m1)                                                           # 旧配置封好，还没发
    st, snap = snapshot_of(m1)
    assert "split_version" not in snap and "ruler_version" not in snap       # 旧步的快照里没有这两个字段
    old_version = st["config_version"]
    m1.close()
    prov = Scripted("skip")
    m2 = open_new(tmp_path, provider=prov, name="up.sqlite")                  # 升级：新默认配置打开
    assert m2.cfg.split_version == "split-2" and m2.cfg.ruler_version == 2 and m2.cfg.index_version == 3
    assert m2.run()["done"] == 1
    got = facts(m2)
    assert got == base_facts                                                  # 请求文字、任务指纹、调用次数、已存范围、收据，和一直用旧配置的基线逐项相同
    assert len(prov.calls) == 1 and prov.calls[0]["text"] == bprov.calls[0]["text"]
    assert m2.store.get_step("w", "step-0001")["config_version"] == old_version        # 不改旧指纹
    assert len(got["sentences"]) == 2 and got["sentences"][0][3] == TEXT_U            # 旧切法：一条消息一句
    # 对照：新封的步（新配置）才用新切法
    m2.record("h", "S2", "u1", "user", TEXT_U, time="2027-01-02")
    m2.record("h", "S2", "a1", "assistant", TEXT_A, time="2027-01-02")
    m2.end_session("h", "S2")
    st2, snap2 = snapshot_of(m2, "step-0002")
    assert snap2["split_version"] == "split-2" and snap2["ruler_version"] == 2
    assert m2.run()["done"] == 1
    sents2 = [s["text"] for s in m2.store.read_state("w")["tables"]["sentences"] if s["session_id"] == "S2"]
    assert sents2 == ["Hello there.", "How are you today?", "I am fine.", "Great.", "See you soon, my friend."]


# ---------------------------------------------------------------- 2 第一问已收下、第二问待续
class Crash(BaseException):
    pass


def _thread_steps(mem):
    for i, name in ((1, "甲"), (2, "乙")):
        mem.record("h", "T%d" % i, "u1", "user", "Thread %s sentence one. Thread %s sentence two." % (name, name), time="2027-01-0%d" % i)
        mem.record("h", "T%d" % i, "a1", "assistant", "ok", time="2027-01-0%d" % i)
        mem.end_session("h", "T%d" % i)


def _gap_provider(crash_q2_once):
    state = {"q2_calls": 0}

    def mode(request, n):
        text = request["text"]
        sids = sids_of(text)
        if request["question"] == "q1" and text.count("Hello there") and "【重判】" not in text:
            return json.dumps({"新建": {"主线": [], "回": []}, "去处": [], "缺口": [{"句": sids, "层": "主线", "已知主线": None, "词": ["测试主线"]}], "改摘要": []}, ensure_ascii=False)
        if request["question"] == "q2":
            state["q2_calls"] += 1
            if crash_q2_once and state["q2_calls"] == 1:
                raise Crash()
            return json.dumps({"新建": {"主线": [], "回": []}, "去处": [{"句": sids_of(text.split("要重判的句子：")[1]), "不记": True}], "缺口": []}, ensure_ascii=False)
        return out_thread(sids) if "Thread" in text.split("这次对话：")[-1] else out_skip(sids)
    return mode


def test_q1_accepted_q2_pending_is_resumed_with_old_split_and_old_counts(tmp_path):
    light = LightConfig(max_threads=1)                                         # 只展示一条候选主线：另一条留给补找，才有第二问
    # 基线：旧配置从头跑完，崩溃一次
    pb = Scripted(_gap_provider(True))
    mb = open_legacy(tmp_path, provider=pb, name="b.sqlite", light=light)
    _thread_steps(mb)
    mb.run()
    mb.record("h", "S3", "u1", "user", TEXT_U, time="2027-01-03")
    mb.record("h", "S3", "a1", "assistant", TEXT_A, time="2027-01-03")
    mb.end_session("h", "S3")
    with pytest.raises(Crash):
        mb.run()
    mb.close()
    mb = open_legacy(tmp_path, provider=pb, name="b.sqlite", light=light)
    assert mb.run()["done"] == 1
    # 升级场景：同样的旧配置做到「第一问已收下、第二问发出时崩溃」，再用新默认配置接着做
    pu = Scripted(_gap_provider(True))
    mu = open_legacy(tmp_path, provider=pu, name="u.sqlite", light=light)
    _thread_steps(mu)
    mu.run()
    mu.record("h", "S3", "u1", "user", TEXT_U, time="2027-01-03")
    mu.record("h", "S3", "a1", "assistant", TEXT_A, time="2027-01-03")
    mu.end_session("h", "S3")
    with pytest.raises(Crash):
        mu.run()
    sid = "step-0003"
    assert len(mu.store.list_judgments("w", sid, "q1")) == 1 and len(mu.store.list_judgments("w", sid, "q2")) == 1       # Q1 已收下；Q2 发出过一次（崩溃那一次）
    q1_task_fp = mu.store.list_judgments("w", sid, "q1")[0]["task_fp"]
    mu.close()
    calls_before = len(pu.calls)
    mu2 = open_new(tmp_path, provider=pu, name="u.sqlite", light=light)
    assert mu2.run()["done"] == 1
    new_calls = pu.calls[calls_before:]
    assert [c["question"] for c in new_calls] == ["q2"]                         # 第一问没有重发（任务指纹没变，合格输出照旧复用）
    assert mu2.store.list_judgments("w", sid, "q1")[0]["task_fp"] == q1_task_fp
    assert len(mu2.store.list_judgments("w", sid, "q1")) == 1 and len(mu2.store.list_judgments("w", sid, "q2")) == 2       # 次数接着数，不重置
    assert facts(mu2, sid) == facts(mb, sid)                                    # 请求文字、任务指纹、已存的范围、收据，和一直用旧配置的基线相同
    sents = [s["text"] for s in mu2.store.read_state("w")["tables"]["sentences"] if s["session_id"] == "S3"]
    assert sents == [TEXT_U, TEXT_A]                                            # 还是旧切法


# ---------------------------------------------------------------- 3 已提交的旧步再执行一次
def test_committed_old_step_executed_again_is_replayed_without_a_send(tmp_path):
    m1, prov1 = baseline(tmp_path, "c.sqlite", "thread")
    before = facts(m1)
    m1.store.conn.execute("UPDATE steps SET status='sealed' WHERE step_id='step-0001'")         # 提交成功但没来得及记「做完」：重开以后再执行一次
    m1.close()
    prov = Scripted("thread")
    m2 = open_new(tmp_path, provider=prov, name="c.sqlite")
    assert m2.run()["done"] == 1
    assert prov.calls == []                                                     # 已提交：重放，不发请求
    after = facts(m2)
    assert after == before and after["n_receipts"] == 1                         # 已存的范围、指纹、收据一个字不变
    assert after["judgments"]["q1"][0][2] == prov1.calls[0]["text"]             # 请求文字还是旧切法的那份
    assert len(after["judgments"]["q1"]) == 1                                   # 调用次数没有增加


# ---------------------------------------------------------------- 4 旧工作区来了新消息
def test_old_workspace_keeps_its_index_version_for_new_messages(tmp_path):
    m1 = open_legacy(tmp_path, provider=Scripted("skip"), name="w.sqlite")
    m1.record("h", "S1", "u1", "user", "Meet at 3 p.m. We can talk.", time="2027-01-01")
    mid0 = next(iter(m1._reader.snapshot().by_id))
    a0 = m1.store.anchors_of("w", mid0)
    assert len(a0) == 1 and a0[0]["index_version"] == 2
    m1.close()
    m2 = open_new(tmp_path, provider=Scripted("skip"), name="w.sqlite")
    assert m2.cfg.index_version == 3 and m2.index_version == 2                  # 已有工作区沿用存着的版本
    mid1 = m2.record("h", "S1", "u2", "user", "Meet at 3 p.m. We can talk.", time="2027-01-01")["msg_id"]
    a1 = m2.store.anchors_of("w", mid1)
    assert [(a["text"], a["index_version"]) for a in a1] == [("Meet at 3 p.m. We can talk.", 2)]      # 升级以后新来的消息也照旧版本切（整句一个锚点）
    assert m2.store.anchors_of("w", mid0) == a0                                 # 旧锚点原样
    assert all(a["ord"] >= 1000000 for a in a1)                                 # 版本 2 的编号倍率
    assert m2.recall("Meet")["delivered"]                                       # 读取照常


# ---------------------------------------------------------------- 5 新工作区用版本 3
def test_new_workspace_uses_index_version_3_and_new_split_for_new_steps(tmp_path):
    prov = Scripted("skip")
    m = open_new(tmp_path, provider=prov, name="n.sqlite")
    assert m.index_version == 3
    mid = m.record("h", "S1", "u1", "user", "Meet at 3 p.m. We can talk.", time="2027-01-01")["msg_id"]
    a = m.store.anchors_of("w", mid)
    assert [(x["text"], x["index_version"]) for x in a] == [("Meet at 3 p.m.", 3), ("We can talk.", 3)]
    m.record("h", "S1", "a1", "assistant", "Sure. Good.", time="2027-01-01")
    m.end_session("h", "S1")
    st, snap = snapshot_of(m)
    assert snap["split_version"] == "split-2" and snap["ruler_version"] == 2
    assert m.run()["done"] == 1
    assert "s3（" in prov.calls[0]["text"] or "s2（" in prov.calls[0]["text"]
    texts = [s["text"] for s in m.store.read_state("w")["tables"]["sentences"]]
    assert texts == ["Meet at 3 p.m.", "We can talk.", "Sure.", "Good."]
    # 导出文件头照实写版本
    out = tmp_path / "x.jsonl"
    m.export(str(out))
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["index_version"] == 3


# ---------------------------------------------------------------- 6 旧游标续读被拒
def _two_page_cursor(mem, ruler):
    rd = mem._reader
    rd.cfg.ruler_version = ruler
    ids = [mem.record("h", "S1", "p%d" % i, "user", "page message %d with some words. " % i * 8, time="2027-01-01")["msg_id"] for i in range(6)]
    res = rd.get_messages(ids=ids, page_tokens=60)
    assert res["cursor"]
    return res["cursor"], ids


def test_old_cursor_is_rejected_after_ruler_upgrade_and_current_cursor_is_stable(tmp_path):
    m1 = open_legacy(tmp_path, provider=Scripted("skip"), name="k.sqlite")
    cur, ids = _two_page_cursor(m1, 1)
    again = m1._reader.get_messages(cursor=cur)
    assert again == m1._reader.get_messages(cursor=cur)                          # 同一尺子下，同一个游标重取相同
    m1.close()
    m2 = open_new(tmp_path, provider=Scripted("skip"), name="k.sqlite")
    with pytest.raises(BadCursor) as e:
        m2._reader.get_messages(cursor=cur)
    assert "重新查" in str(e.value)                                              # 明确拒绝，不静默换页界
    # 没有版本的旧游标（升级前存的）也拒
    old = json.loads(json.dumps(m2.store.get_cursor(cur.split(":")[0])["data"]))
    old.pop("ruler")
    m2.store.put_cursor("d" * 16, "w", "get_messages", None, old)
    with pytest.raises(BadCursor):
        m2._reader.get_messages(cursor="d" * 16 + ":1:0")
    # 重新查（新尺子）的游标是好的，且同页重取内容相同
    res = m2._reader.get_messages(ids=ids, page_tokens=60)
    assert res["cursor"]
    assert m2._reader.get_messages(cursor=res["cursor"]) == m2._reader.get_messages(cursor=res["cursor"])
    # 便条里的游标（origin=note）也带尺子版本
    cid = res["cursor"].split(":")[0]
    assert m2.store.get_cursor(cid)["data"]["ruler"] == 2
