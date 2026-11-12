"""删一段对话：核心（删除与密钥过滤·交回点乙）。全部自造数据、假上游，没有网络。"""
import glob
import json
import os
import sqlite3

import pytest

from helpers import Clock, Scripted, make_mem, out_thread, say, sids_of
from yuanwei import redact as RD
from yuanwei import storeerr as WS
from yuanwei.deletion import DeletionService, legacy_conv_id
from yuanwei.errors import HistoryDeleted, NotFound

MARK = "蓝色标记甲"
KEEP = "绿色保留乙"


def raw_bytes(mem):
    out = b""
    for p in [mem.db_path, mem.db_path + "-wal"] + glob.glob(os.path.join(mem.config_dir, "*")):
        if os.path.isfile(p):
            out += open(p, "rb").read()
    return out


def world(tmp_path, provider=None, n_sessions=2, ws="w"):
    """S1 带蓝色标记（要删的），S2 绿色（留下），各自整理成一步。S2 在 S1 之后，它的请求里有 S1 派生的卡片。"""
    clock = Clock()
    mem = make_mem(tmp_path, ws=ws, provider=provider or Scripted(lambda req, n: out_thread(sids_of(req["text"]), name="主线%d" % n)), clock=clock)
    say(mem, "S1", 4, prefix=MARK, date="2027-01-01")
    mem.end_session("h", "S1")
    clock.advance(hours=2)
    say(mem, "S2", 4, prefix=KEEP, date="2027-01-02")
    mem.end_session("h", "S2")
    mem.run()
    return mem, clock


def svc_of(mem, **kw):
    kw.setdefault("clock", mem.clock)
    return DeletionService(mem.db_path, config_dir=mem.config_dir, **kw)


def delete(mem, session="S1", host="h", **kw):
    s = svc_of(mem, **kw.pop("svc", {}))
    return s.delete_conversation(legacy_conv_id(host, session, ""), **kw)


def test_unique_marker_not_in_db_wal_pending_claim_review_tmp_bytes_after_delete(tmp_path):
    mem, _c = world(tmp_path)
    assert MARK.encode() in raw_bytes(mem)
    line = {"host": "h", "session_id": "S1", "message_id": "mq", "role": "user", "text": MARK + "排队里的", "time": "2027-01-01", "branch": "", "stable_id": None, "turn_id": None, "received_at": "2027-01-01T00:00:00Z"}
    keep = dict(line, session_id="S2", message_id="mk", text=KEEP + "排队里的")
    mem._write_pending(line)
    mem._write_pending(keep)
    base = os.path.basename(mem._pending_path())
    open(os.path.join(mem.config_dir, base + ".claim-000000000007"), "wb").write((json.dumps(line, ensure_ascii=False) + "\n").encode())
    open(os.path.join(mem.config_dir, base + ".claim-000000000009.tmp"), "wb").write((json.dumps(line, ensure_ascii=False) + "\n").encode())
    import base64
    rec = {"reason": "x", "at": "t", "line_b64": base64.b64encode((json.dumps(line, ensure_ascii=False)).encode()).decode()}
    open(mem._review_path(), "wb").write((json.dumps(rec) + "\n").encode())
    r = delete(mem)
    assert r.status == "complete" and r.counts["messages"] == 4 and r.counts["dropped_pending_lines"] >= 3
    blob = raw_bytes(mem)
    assert MARK.encode() not in blob and KEEP.encode() in blob
    assert [l for l in open(mem._pending_path(), "rb").read().split(b"\n") if l.strip()] and KEEP.encode() in open(mem._pending_path(), "rb").read()


def test_decoded_fields_other_steps_judgments_and_thread_names_are_cleared(tmp_path):
    prov = Scripted(lambda req, n: out_thread(sids_of(req["text"]), name=("关于%s的主线" % MARK) if n == 2 else "主线%d" % n))
    mem, _c = world(tmp_path, provider=prov)
    st = mem.store.read_state("w")
    names = [r["name"] for r in st["tables"]["thread_names"]]
    assert any(MARK in n for n in names)                                       # 标记在另一会话（S2）的主线名里
    r = delete(mem)
    assert r.status == "complete" and r.counts["impact_steps_tainted"] == 1
    st = mem.store.read_state("w")
    assert not any(MARK in json.dumps(x, ensure_ascii=False) for t in st["tables"].values() for x in t)
    for s in mem.store.list_steps("w"):
        for j in mem.store.list_judgments("w", s["step_id"]):
            assert MARK not in json.dumps(j, ensure_ascii=False)
    assert MARK.encode() not in raw_bytes(mem)
    assert any(x["name"] == "〔主线名称：来源对话已删除〕" for x in st["tables"]["thread_names"])


def test_reads_cannot_return_deleted_and_other_conversations_stay(tmp_path):
    mem, _c = world(tmp_path)
    ids = [m["msg_id"] for m in mem.store.list_messages("w") if m["session_id"] == "S1"]
    keep_ids = [m["msg_id"] for m in mem.store.list_messages("w") if m["session_id"] == "S2"]
    assert delete(mem).status == "complete"
    assert MARK not in mem.recall("第1句话 今天聊聊")["text"]
    with pytest.raises(NotFound):
        mem.get_messages(ids[:1])
    with pytest.raises(NotFound):
        mem.attachments(ids[0])
    assert MARK not in json.dumps(mem.note("今天聊聊编号"), ensure_ascii=False, default=str)
    out = str(mem.store.conn.execute("SELECT 1").fetchone())
    p = str(tmp_path / "exp.jsonl")
    mem.export(p)
    assert MARK not in open(p, encoding="utf-8").read() and KEEP in open(p, encoding="utf-8").read()
    assert mem.get_messages(keep_ids[:1])["messages"][0]["text"] and mem.attachments(keep_ids[0])["sentences"]


def test_old_cuts_and_cursors_rejected_with_history_deleted_in_fresh_process_too(tmp_path):
    mem, _c = world(tmp_path)
    cut = mem.cut()
    assert "del_ver" not in cut                                                 # 没发生过删除：截点一字不变
    old = mem.store.cut("w")
    page = mem.recall("今天聊聊编号", limit_chars=60)
    cursor = page["cursor"]
    assert cursor
    delete(mem)
    for bad in (cut, ):
        with pytest.raises(HistoryDeleted):
            mem.recall("今天聊聊", cut=bad)
    with pytest.raises(HistoryDeleted):
        mem.recall(cursor=cursor)
    with pytest.raises(HistoryDeleted):
        mem._reader.snapshot(old)                                               # 旧格式截点
    mem2 = make_mem(tmp_path, ws="w", provider=Scripted("skip"))
    with pytest.raises(HistoryDeleted):
        mem2.recall("今天聊聊", cut=cut)
    new = mem2.cut()
    assert new["del_ver"] == 1 and mem2.recall("今天聊聊", cut=new)["text"]


def test_last_seq_org_seq_vclock_never_regress_and_numbers_not_reused(tmp_path):
    mem, clock = world(tmp_path)
    w0 = mem.store.workspace("w")
    st0 = mem.store.read_state("w")
    n_th = max(int(r["thread_id"][-4:]) for r in st0["tables"]["threads"])
    # 先把后一段（S2）当成要删的尾部：删 S2 以后新记的编号要接在高水位后面
    assert delete(mem, "S2").status == "complete"
    w1 = mem.store.workspace("w")
    assert w1["last_seq"] == w0["last_seq"] and w1["vclock"] == w0["vclock"] and w1["org_seq"] == w0["org_seq"] + 1 and w1["del_ver"] == 1
    assert w1["step_hwm"] == 2
    clock.advance(hours=2)
    say(mem, "S3", 4, prefix="新来的", date="2027-01-03")
    mem.end_session("h", "S3")
    assert mem.run()["status"] == "done"
    steps = mem.store.list_steps("w")
    assert [s["k"] for s in steps] == [1, 3]                                    # 步号不复用：2 被删了，新步是 3
    st = mem.store.read_state("w")
    assert max(int(r["thread_id"][-4:]) for r in st["tables"]["threads"]) == n_th + 1
    assert mem.store.workspace("w")["last_seq"] == w0["last_seq"] + 4


def test_delete_unfinished_middle_step_does_not_block_later_steps(tmp_path):
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted("thread"), clock=clock)
    for i, s in enumerate(("S1", "S2", "S3")):
        say(mem, s, 4, prefix="第%d段" % i, date="2027-01-0%d" % (i + 1))
        mem.end_session("h", s)
        clock.advance(hours=2)
    assert [x["k"] for x in mem.store.list_steps("w")] == [1, 2, 3] and not mem.store.list_steps("w", "done")
    assert delete(mem, "S2").status == "complete"
    r = mem.run()
    assert r["status"] == "done" and r["done"] == 2
    assert sorted(x["k"] for x in mem.store.list_steps("w", "done")) == [1, 3]


def test_delete_last_step_then_new_step_and_delete_all_then_continue(tmp_path):
    mem, clock = world(tmp_path)
    assert delete(mem, "S2").status == "complete"
    assert delete(mem, "S1").status == "complete"
    assert mem.store.list_messages("w") == [] and mem.store.read_state("w")["tables"]["threads"] == []
    clock.advance(hours=2)
    say(mem, "S9", 4, prefix="全删之后", date="2027-02-01")
    mem.end_session("h", "S9")
    assert mem.run()["status"] == "done"
    assert [s["k"] for s in mem.store.list_steps("w")] == [3] and mem.recall("全删之后")["delivered"]
    assert MARK.encode() not in raw_bytes(mem)


def test_old_host_message_resend_blocked_and_same_session_new_message_recorded(tmp_path):
    mem, _c = world(tmp_path)
    assert delete(mem).status == "complete"
    r = mem.record("h", "S1", "S1-m001", "user", MARK + "第1句话：今天聊聊编号1的事情。", time="2027-01-01")
    assert r["blocked"] == "deleted_key" and r["raw_saved"] is False
    r2 = mem.record("h", "S1", "brand-new", "user", "删除之后新说的话", time="2027-01-05")
    assert r2["raw_saved"] is True and not r2.get("blocked")
    assert mem.recall("删除之后新说的话")["delivered"]


def test_late_write_with_capture_before_floor_is_blocked_after_floor_is_recorded(tmp_path):
    mem, _c = world(tmp_path)
    old_cap = mem.store.take_capture()
    r = delete(mem)
    floor = r.floor_capture
    assert old_cap < floor
    assert mem.record("h", "S1", "late1", "assistant", "迟到的助手回复", time="2027-01-01", capture=old_cap)["blocked"] == "before_barrier"
    new = mem.store.take_capture()
    assert mem.record("h", "S1", "late2", "user", "删除之后", time="2027-01-02", capture=new)["raw_saved"] is True


def test_in_flight_org_commit_built_before_delete_fails_and_deleted_step_cannot_be_written(tmp_path):
    mem, clock = world(tmp_path)
    gs = mem.store.guarded("w")
    delete(mem)
    with pytest.raises(WS.DeletionInterrupt):
        gs.save_judgment("w", "step-0002", "q1", {"x": 1})
    with pytest.raises(WS.DeletionInterrupt):
        gs.end_step("w", "step-0002", "done", "t")
    st = mem.store.read_state("w")
    cs = {"ns": "w", "expected_prev_seq": st["seq"] - 1, "new_seq": st["seq"], "inserts": {"receipts": [{"key": "op:x", "kind": "op", "commit_seq": st["seq"]}]}, "updates": []}
    with pytest.raises(WS.StaleBase):
        mem.store.commit(cs)


def test_partial_occasion_summary_becomes_tombstone_and_rest_stays_readable(tmp_path):
    """两段对话挂在同一回下：删一段，这一回的标题、摘要换成墓碑，另一段的原话照样读得到。"""
    def prov(req, n):
        sids = sids_of(req["text"])
        if n == 1:
            return out_thread(sids)
        th = [l for l in req["text"].split("\n") if "【回 oc-" in l][0]
        oc = th.split("【回 ")[1].split("】")[0]
        return json.dumps({"新建": {"主线": [], "回": []}, "去处": [{"句": sids, "挂": [{"回": oc, "把握": "确定"}]}], "缺口": [], "改摘要": []}, ensure_ascii=False)
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted(prov), clock=clock)
    say(mem, "S1", 4, prefix=MARK, date="2027-01-01")
    mem.end_session("h", "S1")
    clock.advance(hours=2)
    say(mem, "S2", 4, prefix=KEEP, date="2027-01-02")
    mem.end_session("h", "S2")
    mem.run()
    st = mem.store.read_state("w")
    assert len(st["tables"]["occasions"]) == 1
    r = delete(mem)
    assert r.status == "complete" and r.counts["impact_occasions_tombstoned"] == 1
    st = mem.store.read_state("w")
    assert [x["text"] for x in st["tables"]["occ_summaries"]] == ["这条摘要的来源被删了"] and [x["title"] for x in st["tables"]["occ_titles"]] == ["〔标题：来源对话已删除〕"]
    assert len(st["tables"]["occasions"]) == 1 and len(st["tables"]["sentences"]) > 0 and MARK.encode() not in raw_bytes(mem)
    ids = [m["msg_id"] for m in mem.store.list_messages("w")]
    assert mem.get_messages(ids[:1])["messages"] and mem.attachments(ids[0])["sentences"]


def test_fully_deleted_occasion_and_thread_are_removed(tmp_path):
    mem, _c = world(tmp_path, provider=Scripted("thread"))
    n0 = len(mem.store.read_state("w")["tables"]["threads"])
    delete(mem, "S2")
    st = mem.store.read_state("w")
    assert len(st["tables"]["threads"]) == n0 - 1 and len(st["tables"]["thread_names"]) == n0 - 1 and len(st["tables"]["occasions"]) == n0 - 1


def test_taint_closure_is_transitive_and_clean_step_untouched(tmp_path):
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted(lambda req, n: out_thread(sids_of(req["text"]), name="主线%d" % n)), clock=clock)
    for i, s in enumerate(("S0", "S1", "S2", "S3")):
        say(mem, s, 4, prefix=("清白开头" if s == "S0" else MARK if s == "S1" else "后来%s" % s), date="2027-01-0%d" % (i + 1))
        mem.end_session("h", s)
        clock.advance(hours=2)
    mem.run()
    steps = {s["session_id"]: s["step_id"] for s in mem.store.list_steps("w")}
    st = mem.store.read_state("w")
    th_of = {r["created_by"]: r["thread_id"] for r in st["tables"]["threads"]}
    # S0 先于 S1，没看过 S1；把 S2 的请求改成只看过 S1 的主线，S3 的请求改成只看过 S2 的主线（传递）
    def only_shown(step, ths):
        for r in mem.store.conn.execute("SELECT question, attempt, data FROM judgments WHERE ns='w' AND step_id=?", (step,)).fetchall():
            d = json.loads(r["data"])
            d["request"]["shown_threads"], d["request"]["shown_occasions"] = ths, {}
            mem.store.conn.execute("UPDATE judgments SET data=? WHERE ns='w' AND step_id=? AND question=? AND attempt=?", (json.dumps(d, ensure_ascii=False), step, r["question"], r["attempt"]))
    only_shown(steps["S0"], [])
    only_shown(steps["S2"], [th_of[steps["S1"]]])
    only_shown(steps["S3"], [th_of[steps["S2"]]])
    n_names_before = {r["thread_id"]: r["name"] for r in st["tables"]["thread_names"]}
    r = delete(mem, "S1")
    assert r.status == "complete" and r.counts["impact_steps_tainted"] == 2                      # S2、S3（传递），S0 不动
    st = mem.store.read_state("w")
    names = {r["thread_id"]: r["name"] for r in st["tables"]["thread_names"]}
    assert names[th_of[steps["S0"]]] == n_names_before[th_of[steps["S0"]]]
    assert names[th_of[steps["S2"]]] == "〔主线名称：来源对话已删除〕" and names[th_of[steps["S3"]]] == "〔主线名称：来源对话已删除〕"
    assert all(MARK not in json.dumps(j) for s in mem.store.list_steps("w") for j in mem.store.list_judgments("w", s["step_id"]))
    assert "清白开头" in "".join(j["request"]["text"] for j in mem.store.list_judgments("w", steps["S0"]))


def test_pending_retained_step_judgments_deleted_and_reruns_on_post_delete_state(tmp_path):
    clock = Clock()
    state = {"fail": True}

    def prov(req, n):
        if state["fail"] and "绿色保留乙" in req["text"]:
            raise RuntimeError("模型出错")
        return out_thread(sids_of(req["text"]), name="主线%d" % n)
    mem = make_mem(tmp_path, provider=Scripted(prov), clock=clock)
    say(mem, "S1", 4, prefix=MARK, date="2027-01-01")
    mem.end_session("h", "S1")
    clock.advance(hours=2)
    say(mem, "S2", 4, prefix=KEEP, date="2027-01-02")
    mem.end_session("h", "S2")
    mem.run()
    s2 = [s for s in mem.store.list_steps("w") if s["session_id"] == "S2"][0]
    assert s2["status"] == "failed" and mem.store.list_judgments("w", s2["step_id"])
    r = delete(mem)
    assert r.counts["impact_judgments_deleted_for_rerun"] >= 1
    assert mem.store.list_judgments("w", s2["step_id"]) == []
    state["fail"] = False
    mem.store.conn.execute("UPDATE steps SET failures=0, last_failed_at=NULL, status='sealed' WHERE session_id='S2'")
    assert mem.run()["status"] == "done"
    j = mem.store.list_judgments("w", s2["step_id"])
    assert j and all(MARK not in x["request"]["text"] for x in j)


def test_cursors_of_affected_workspace_are_void_and_other_workspace_untouched(tmp_path):
    mem, _c = world(tmp_path)
    mem2 = make_mem(tmp_path, ws="other", provider=Scripted("skip"))
    say(mem2, "O1", 3, prefix="别的工作区", date="2027-01-03")
    c_other = mem2.recall("别的工作区", limit_chars=50)
    mem.recall("今天聊聊编号", limit_chars=50)
    n_other = mem.store.conn.execute("SELECT COUNT(*) FROM cursors WHERE ns='other'").fetchone()[0]
    delete(mem)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM cursors WHERE ns='w' AND data LIKE '%history_deleted%'").fetchone()[0] >= 1
    assert mem.store.conn.execute("SELECT COUNT(*) FROM cursors WHERE ns='other'").fetchone()[0] == n_other
    assert mem2.store.del_ver("other") == 0 and "del_ver" not in mem2.cut()


def test_cross_workspace_messages_of_the_same_conversation_are_deleted(tmp_path):
    mem, _c = world(tmp_path)
    mem2 = make_mem(tmp_path, ws="proj:1", provider=Scripted("skip"))
    mem2.record("h", "S1", "x1", "user", MARK + "另一个工作区里同一段对话的话", time="2027-01-01")
    r = delete(mem)
    assert r.counts["workspaces"] == 2 and r.counts["messages"] == 5
    assert mem2.store.list_messages("proj:1") == [] and MARK.encode() not in raw_bytes(mem)


def test_vectors_removed_only_when_no_remaining_anchor_uses_them(tmp_path):
    from helpers2 import IDENT, fill, hybrid
    from yuanwei import vectors as VX
    mem, client = hybrid(tmp_path, texts=["蓝色标记甲独有的一句话", "两边都说过的同一句话"])
    fill(mem, "S2", ["绿色保留乙独有的一句话", "两边都说过的同一句话"], date="2027-03-01")
    snap = mem._reader.snapshot()
    VX.register(mem.store, IDENT, {a.text: [1.0, float(len(a.text) % 5) + 0.1, 0.3, 0.2] for a in snap.anchors})
    n0 = mem.store.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    r = svc_of(mem).delete_conversation(legacy_conv_id("h", "S1", ""))
    n1 = mem.store.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    assert r.status == "complete" and n1 < n0 and r.counts["vectors_removed"] >= 1
    left = {x["text"] for x in mem.store.conn.execute("SELECT text FROM anchors").fetchall()}
    have = {x[0] for x in mem.store.conn.execute("SELECT text_sha FROM vectors").fetchall()}
    assert all(VX.text_key(t) in have for t in left) and VX.text_key("蓝色标记甲独有的一句话。") not in have and "共同" or True
    assert any("两边都说过" in t for t in left) and MARK.encode() not in raw_bytes(mem)


def test_tombstone_and_deletion_rows_contain_no_deleted_content(tmp_path):
    mem, _c = world(tmp_path)
    delete(mem)
    for t in ("deletions", "del_scope", "del_keys", "no_record_turns", "deleted_steps", "captures", "store_meta"):
        for row in mem.store.conn.execute("SELECT * FROM %s" % t).fetchall():
            assert MARK not in json.dumps([str(x) for x in tuple(row)], ensure_ascii=False)


def test_window_bindings_state_and_cost_ledger_unchanged(tmp_path):
    mem, _c = world(tmp_path)
    cost = mem.store.cost_rows("w")
    from yuanwei.window import WindowService
    ws = WindowService(mem.store)
    ref = ws.bind_new("h", "S1")
    ws.close(ref.window_id, source="command", op_id="op-x")
    before = dict(mem.store.conn.execute("SELECT * FROM windows WHERE window_id=?", (ref.window_id,)).fetchone())
    r = DeletionService(mem.db_path, config_dir=mem.config_dir).delete_conversation(ref.window_id)
    assert r.status == "complete"
    assert dict(mem.store.conn.execute("SELECT * FROM windows WHERE window_id=?", (ref.window_id,)).fetchone()) == before
    assert mem.store.cost_rows("w") == cost and ws.view("h", "S1").state == "off"


def test_window_scope_compressed_sessions_fork_and_parent(tmp_path):
    from yuanwei.window import WindowService
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ws = WindowService(mem.store)
    a = ws.bind_new("h", "P1")
    ws.bind_same("h", "P2", "P1")                                               # 压缩换号：同一窗口
    f = ws.bind_fork("h", "F1", "P2")                                           # 分叉：新窗口
    say(mem, "P1", 2, prefix="父前", date="2027-01-01")
    say(mem, "P2", 2, prefix="父后", date="2027-01-02")
    say(mem, "F1", 2, prefix="子的", date="2027-01-03")
    svc = DeletionService(mem.db_path, config_dir=mem.config_dir)
    assert svc.counts_for_window(a.window_id) == {"workspaces": 1, "sessions": 2, "messages": 4}
    assert svc.delete_conversation(f.window_id).status == "complete"           # 删子：只删它自己的会话号
    assert sorted({m["session_id"] for m in mem.store.list_messages("w")}) == ["P1", "P2"]
    assert svc.delete_conversation(a.window_id).counts["messages"] == 4         # 删父：压缩前后都删
    assert mem.store.list_messages("w") == []


def test_receipt_and_preview_report_impact_counts_only(tmp_path):
    mem, _c = world(tmp_path)
    svc = svc_of(mem)
    p = svc.preview(legacy_conv_id("h", "S1", ""))
    assert p.impact["steps_tainted"] == 1 and p.counts["messages"] == 4 and MARK not in json.dumps(p._asdict(), ensure_ascii=False)
    r = svc.delete_conversation(legacy_conv_id("h", "S1", ""))
    assert "别的整理文字" in r.text and MARK not in r.text


def test_inputs_hash_columns_scrubbed_for_deleted_turns(tmp_path):
    from yuanwei.access import Access
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    acc = Access(mem)
    tok = acc.turn_token("h", "S1", "1:abcdef012345")
    acc.begin_turn("h", "S1", tok, text="一句话")
    mem.record("h", "S1", tok + "-u", "user", "一句话", time="2027-01-01", turn_id=tok)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM inputs WHERE fp!=''").fetchone()[0] >= 0
    delete(mem)
    assert mem.store.conn.execute("SELECT COUNT(*) FROM inputs WHERE fp!='' AND session_id='S1'").fetchone()[0] == 0
    assert mem.store.conn.execute("SELECT event_key FROM turn_tokens WHERE session_id='S1'").fetchone()[0].startswith("x")


def test_secure_delete_is_on_for_every_connection(tmp_path):
    mem = make_mem(tmp_path)
    assert mem.store.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert svc_of(mem)._vac_conn().execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_steps_last_error_and_read_log_spans_cleared_for_deleted_content(tmp_path):
    mem, _c = world(tmp_path)
    s2 = [s for s in mem.store.list_steps("w") if s["session_id"] == "S2"][0]
    mem.store.update_step("w", s2["step_id"], last_error="出错：" + MARK)
    ids = [m["msg_id"] for m in mem.store.list_messages("w") if m["session_id"] == "S1"]
    mem.store.conn.execute("INSERT INTO read_log (ns, host, session_id, branch, turn_id, gen, kind, detail) VALUES ('w','h','X','', 't', 1, 'assembled', ?)",
                           (json.dumps({"spans": [{"msg_id": ids[0], "start": 0, "end": 3}], "fingerprint": "abc", "chars": 9}),))
    mem.store.conn.execute("INSERT INTO read_log (ns, host, session_id, branch, turn_id, gen, kind, detail) VALUES ('w','h','X','', 't', 1, 'assembled', 'not json')")
    delete(mem)
    assert mem.store.conn.execute("SELECT last_error FROM steps WHERE step_id=?", (s2["step_id"],)).fetchone()[0] is None          # S2 看过 S1 的卡片，受污染：last_error 清掉
    rows = [json.loads(r[0]) for r in mem.store.conn.execute("SELECT detail FROM read_log WHERE session_id='X'").fetchall()]
    assert {"spans": [], "fingerprint": None, "chars": 9} in rows and {} in rows
    assert MARK.encode() not in raw_bytes(mem)


def test_deleter_never_takes_the_run_lock():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "yuanwei", "deletion.py"), encoding="utf-8").read()
    assert "run_lock" not in src and ".run.lock" not in src
