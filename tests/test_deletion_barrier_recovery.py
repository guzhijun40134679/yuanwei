"""删除的屏障、交错点、锁顺序、完成与清理中、重启恢复（删除与密钥过滤·交回点乙）。"""
import glob
import json
import os
import sqlite3
import threading

import pytest

from helpers import Clock, Scripted, make_mem, out_thread, say, sids_of
from test_deletion_core import KEEP, MARK, delete, raw_bytes, svc_of, world
from yuanwei import redact as RD
from yuanwei import storeerr as WS
from yuanwei.deletion import DeletionService, legacy_conv_id
from yuanwei.errors import DeletionIncomplete, HistoryDeleted
from yuanwei.reader import _READERS, purge_all

CID = legacy_conv_id("h", "S1", "")


# ---------------------------------------------------------------- 三个交错点
def test_interleave_pending_batch_read_into_memory_then_delete_then_flush_inserts_nothing(tmp_path):
    mem, _c = world(tmp_path)
    for i in range(3):
        mem._write_pending({"host": "h", "session_id": "S1", "message_id": "q%d" % i, "role": "user", "text": MARK + "待补%d" % i, "time": "2027-01-01", "branch": "", "time_precision": "day", "stable_id": None,
                            "turn_id": None, "received_at": mem.clock(), "capture": mem.store.take_capture(), "rv": RD.RULES_VERSION, "redacted": 0, "kinds": []})
    orig, done = mem._insert, {"d": False}

    def interleaved(a):                                          # 补写者已经把这批读进内存、正要逐行入库：这时删除完成
        if not done["d"]:
            done["d"] = True
            assert svc_of(mem).delete_conversation(CID, finish=False).status in ("cleaning", "complete")
        return orig(a)
    mem._insert = interleaved
    r = mem.flush_pending_detail()
    assert r["inserted"] == 0 and r["dropped_by_deletion"] == 3
    assert [m for m in mem.store.list_messages("w") if m["session_id"] == "S1"] == []


def test_interleave_model_returned_before_judgment_saved_then_delete_then_release_restores_nothing(tmp_path):
    clock = Clock()
    state = {"n": 0, "deleted": False}
    box = {}

    def prov(req, n):
        state["n"] += 1
        if "绿色保留乙" in req["text"] and not state["deleted"]:             # S2 的整理：模型调用进行中，S1 被删
            state["deleted"] = True
            svc_of(box["mem"]).delete_conversation(CID, finish=False)
        return out_thread(sids_of(req["text"]), name="主线%d" % state["n"])
    mem = make_mem(tmp_path, provider=Scripted(prov), clock=clock)
    box["mem"] = mem
    say(mem, "S1", 4, prefix=MARK, date="2027-01-01")
    mem.end_session("h", "S1")
    clock.advance(hours=2)
    say(mem, "S2", 4, prefix=KEEP, date="2027-01-02")
    mem.end_session("h", "S2")
    r = mem.run()
    assert state["deleted"] and r["status"] == "interrupted" and r["reason"] == "deletion"
    s2 = [s for s in mem.store.list_steps("w") if s["session_id"] == "S2"][0]
    assert s2["status"] == "sealed" and mem.store.read_state("w")["tables"]["receipts"] == []        # 没有整理提交，不恢复
    assert all(MARK not in json.dumps(j) for j in mem.store.list_judgments("w", s2["step_id"]))
    assert mem.run()["status"] == "done"                                                                  # 之后按删除后的状态重新整理
    svc_of(mem).resume()
    assert MARK.encode() not in raw_bytes(mem)


def test_interleave_read_assembled_not_returned_then_delete_then_release_history_deleted(tmp_path):
    mem, _c = world(tmp_path)
    rd = mem._reader
    orig = rd.recall

    def slow(*a, **kw):
        out = orig(*a, **kw)                                                    # 已经组装好
        svc_of(mem).delete_conversation(CID, finish=False)                      # 返回前：删除完成
        return out
    rd.recall = slow
    with pytest.raises(HistoryDeleted):
        mem.recall("今天聊聊编号")


def test_finish_read_ticket_blocks_result_assembled_before_delete(tmp_path):
    from test_hermes_adapter import mk, turn
    mem, a = mk(tmp_path)
    turn(a, 1, "你好")
    ident = a.ids.current()
    tk = a.acc.start_read(ident, "recall")
    mem.record("hermes", "hs1", "x1", "user", "一句话", time="2027-01-01")
    svc = DeletionService(mem.db_path, config_dir=mem.config_dir, clock=mem.clock)
    svc.delete_conversation(legacy_conv_id("hermes", "S1", ""), finish=False)
    out = a.acc.finish_read(tk, "文字", "mcp_return")
    assert out["status"] == "error" and out["reason"] == "history_deleted"


# ---------------------------------------------------------------- 缓存、并发
def test_every_reader_instance_in_the_process_drops_cached_snapshots(tmp_path):
    mem, _c = world(tmp_path)
    mem_b = make_mem(tmp_path, provider=Scripted("skip"))
    mem.recall("今天聊聊"); mem_b.recall("今天聊聊")
    assert mem._reader._snaps and mem_b._reader._snaps
    delete(mem)
    assert not mem._reader._snaps and not mem_b._reader._snaps
    assert MARK not in mem_b.recall("今天聊聊编号")["text"]


def test_concurrent_record_flush_run_and_delete_do_not_deadlock(tmp_path):
    mem, _c = world(tmp_path)
    errs = []

    def rec():
        try:
            for i in range(15):
                mem.record("h", "S2", "c%d" % i, "user", KEEP + "并发%d" % i, time="2027-01-02")
        except Exception as e:                                                  # noqa: BLE001
            errs.append(repr(e))

    def flush():
        try:
            for _ in range(10):
                mem.flush_pending_detail()
        except Exception as e:                                                  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=rec), threading.Thread(target=flush)]
    for t in ts:
        t.start()
    r = svc_of(mem).delete_conversation(CID)
    for t in ts:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in ts) and r.status == "complete" and not errs
    assert MARK.encode() not in raw_bytes(mem)


# ---------------------------------------------------------------- 完成与清理中、恢复
def test_phase1_fault_in_transaction_leaves_nothing_changed(tmp_path):
    mem, _c = world(tmp_path)
    n = len(mem.store.list_messages("w"))

    def boom(point):
        if point == "in_tx":
            raise RuntimeError("事务里出错")
    r = svc_of(mem, fault=boom).delete_conversation(CID)
    assert r.status == "failed" and "没有被改动" in r.text and "已删掉" not in r.text
    assert len(mem.store.list_messages("w")) == n and mem.store.conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0
    assert mem.store.del_ver("w") == 0 and mem.store.conn.execute("SELECT COUNT(*) FROM del_keys").fetchone()[0] == 0
    assert svc_of(mem).delete_conversation(CID).status == "complete"


def test_crash_after_db_commit_resume_finishes_without_resurrecting(tmp_path):
    mem, _c = world(tmp_path)
    line = {"host": "h", "session_id": "S1", "message_id": "q1", "role": "user", "text": MARK + "待补", "time": "2027-01-01", "branch": "", "received_at": mem.clock(), "capture": 1}
    mem._write_pending(line)

    def crash(point):
        if point == "after_commit":
            raise SystemExit("进程没了")
    with pytest.raises(SystemExit):
        svc_of(mem, fault=crash).delete_conversation(CID)
    s = svc_of(mem)
    assert s.status()[0]["state"] == "registered" and MARK.encode() in open(mem._pending_path(), "rb").read()      # 文件还没清
    assert [m for m in mem.store.list_messages("w") if m["session_id"] == "S1"] == []                              # 数据库里已经没了，复活不了
    mem2 = make_mem(tmp_path, provider=Scripted("skip"))                                                            # 重启：打开时先做完没做完的
    assert mem2.status()["deletion"]["incomplete"] == [] and s.status()[0]["state"] == "complete"
    assert MARK.encode() not in raw_bytes(mem2) and mem2.pending_count() == 0


def test_crash_between_files_and_vacuum_resume_finishes(tmp_path):
    mem, _c = world(tmp_path)

    def crash(point):
        if point == "after_files":
            raise SystemExit("中断")
    with pytest.raises(SystemExit):
        svc_of(mem, fault=crash).delete_conversation(CID)
    assert svc_of(mem).status()[0]["state"] == "files_cleaned"
    r = svc_of(mem).resume()
    assert r[0].status == "complete" and MARK.encode() not in raw_bytes(mem)


def test_checkpoint_busy_reports_cleaning_never_complete(tmp_path):
    mem, _c = world(tmp_path)
    reader = sqlite3.connect(mem.db_path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM messages").fetchall()                         # 另一个连接开着读事务：挡住 TRUNCATE
    r = svc_of(mem, busy_s=0.05, sleep=lambda s: None).delete_conversation(CID)
    assert r.status == "cleaning" and r.code == "E_CHECKPOINT_BUSY" and "已删掉" not in r.text and "清理残留" in r.text
    reader.execute("COMMIT")
    reader.close()
    assert svc_of(mem).resume()[0].status == "complete"
    assert MARK.encode() not in raw_bytes(mem)


def test_not_enough_disk_space_reports_cleaning_with_code_and_resume_completes(tmp_path):
    mem, _c = world(tmp_path)
    r = svc_of(mem, disk_free=lambda: 0).delete_conversation(CID)
    assert r.status == "cleaning" and r.code == "E_NO_SPACE" and mem.store.conn.execute("SELECT state FROM deletions").fetchone()[0] == "files_cleaned"
    assert [m for m in mem.store.list_messages("w") if m["session_id"] == "S1"] == []                              # 已撤销的不复活
    assert svc_of(mem).resume()[0].status == "complete"


def test_vacuum_failure_reports_cleaning_with_code(tmp_path, monkeypatch):
    mem, _c = world(tmp_path)

    class Bad:
        def execute(self, sql, *a):
            if sql.strip().upper().startswith("VACUUM"):
                raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass
    s = svc_of(mem)
    monkeypatch.setattr(s, "_vac_conn", lambda: Bad())
    r = s.delete_conversation(CID)
    assert r.status == "cleaning" and r.code == "E_VACUUM"


def test_complete_requires_all_conditions_and_receipt_never_says_clean_before(tmp_path):
    mem, _c = world(tmp_path)
    s = svc_of(mem, disk_free=lambda: 0)
    r = s.delete_conversation(CID)
    s.disk_free = None
    assert "已删掉" not in r.text and "完成前请不要当成已经删干净" in r.text
    done = s.resume()[0]
    assert done.status == "complete" and done.verified == "structure+canary" and "已删掉这段对话" in done.text
    assert mem.store.conn.execute("SELECT plan FROM deletions").fetchone()[0] is None                           # 完成后计划清掉
    assert MARK.encode() not in raw_bytes(mem)                                                                 # 完成后写状态行会让 WAL 重新有几个字节，但里面没有被删的内容


def test_memory_open_resumes_and_unrecoverable_workspace_refuses_reads_and_queues_writes(tmp_path, monkeypatch):
    mem, _c = world(tmp_path)
    svc_of(mem, disk_free=lambda: 0).delete_conversation(CID)
    monkeypatch.setattr(DeletionService, "resume_for", lambda self, ns: None)                                   # 恢复不了（比如别的进程占着）
    mem2 = make_mem(tmp_path, provider=Scripted("skip"))
    assert mem2._deletion_blocked and mem2.status()["deletion"]["blocked"] is True
    r = mem2.recall("今天聊聊")
    assert r["status"] == "refused" and r["reason"] == "deletion_incomplete"
    w = mem2.record("h", "S2", "new1", "user", "恢复之前新说的话", time="2027-01-05")
    assert w["pending"] is True and w["raw_saved"] is False
    monkeypatch.undo()
    mem3 = make_mem(tmp_path, provider=Scripted("skip"))
    assert mem3._deletion_blocked is None and mem3.status()["deletion"]["incomplete"] == [] and mem3.recall("恢复之前新说的话")["delivered"]


def test_recovery_log_and_codes_contain_no_old_text(tmp_path):
    mem, _c = world(tmp_path)
    svc_of(mem, disk_free=lambda: 0).delete_conversation(CID)
    row = dict(mem.store.conn.execute("SELECT * FROM deletions").fetchone())
    assert MARK not in json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False)


def test_canary_self_check_finds_planted_residue_and_blocks_complete(tmp_path):
    mem, _c = world(tmp_path)
    leak = os.path.join(mem.config_dir, os.path.basename(mem._pending_path()) + ".claim-000000000042")

    def plant(point):
        if point == "after_files":                                              # 文件清理之后又冒出一份残留（比如别的进程写的）
            open(leak, "wb").write((MARK + "第1句话：今天聊聊编号1的事情。").encode())
    r = svc_of(mem, fault=plant).delete_conversation(CID)
    assert r.status == "cleaning" and r.code == "E_VERIFY"


def test_unattributable_pending_and_review_lines_are_dropped_and_counted(tmp_path):
    mem, _c = world(tmp_path)
    open(mem._pending_path(), "wb").write(b'{"host": "h", "sess\n' + json.dumps({"host": "h", "session_id": "S2", "message_id": "k", "role": "user", "text": KEEP, "capture": 10**9}, ensure_ascii=False).encode() + b"\n")
    open(mem._review_path(), "wb").write(b'not json at all\n')
    r = svc_of(mem).delete_conversation(CID)
    assert r.status == "complete" and r.counts["dropped_unattributable"] == 2
    assert KEEP.encode() in open(mem._pending_path(), "rb").read()


def test_delete_refuses_unknown_conversation_and_changes_nothing(tmp_path):
    mem, _c = world(tmp_path)
    r = svc_of(mem).delete_conversation("t-000000000000")
    assert r.status == "refused" and r.code == "E_NOT_FOUND" and mem.store.conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0
