"""补充施工单五之三：新记录不因计数读到 0 而插队；半个汉字不挡新原话；旧随机名批次重写后顺序不反。先写成在 fbafd1b 上会失败的测试；守护测试单列在文件末尾。全部自造数据。"""
import base64
import json
import os
import sqlite3
import threading

import pytest

from helpers import Clock, Scripted
from yuanwei import Config, Memory
from yuanwei import memory as MEM


def open_mem(tmp_path, name="m.sqlite", clock=None, **cfg):
    cfg.setdefault("index_version", 2)
    return Memory.open(str(tmp_path / name), "w", config=Config(**cfg), provider=Scripted("skip"), clock=clock or Clock(), spawn="off")


def args(i, session="S1", t=None, text=None):
    return dict(host="h", session_id=session, message_id="m%d" % i, role="user" if i % 2 else "assistant", text=text or "第%d句话" % i, time=t or "2027-01-01", branch="",
                time_precision="day", stable_id=None)


def seq_order(mem):
    return [r[0] for r in mem.store.conn.execute("SELECT host_msg_id FROM messages WHERE ns='w' ORDER BY seq")]


def review_lines(mem):
    p = mem._pending_path() + ".review"
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def put_batch(mem, name_tail, items, mtime=None):
    p = "%s.claim-%s" % (mem._pending_path(), name_tail)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for a in items:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def cut_char_line():
    """一条追加被截在汉字中间：JSON 开头完整，「中」字（三个字节）只写了前两个字节，没有换行。"""
    return ('{"host": "h", "session_id": "S1", "message_id": "m2", "role": "assistant", "text": "半个汉字：中').encode("utf-8")[:-1]


# ================================================================ 一 新记录不能因为计数读到 0 而插队
def test_d1_record_cannot_jump_ahead_when_another_flusher_claims_between_listing_and_counting(tmp_path):
    """Codex 的反例：m1 在待补里。甲 record(m2) 列出领取文件（空）；乙这时领取了 m1（改名）；甲接着数，漏掉改名后的 m1，把 m2 直接入库。
    修好以后：甲的「有没有更早的待补」和乙的领取在同一套排他协议里，乙的领取要等甲做完判断；最后 m1 在前、m2 在后。"""
    a = open_mem(tmp_path)
    b = open_mem(tmp_path)
    a._write_pending(args(1))
    real = a._batches
    state = {"n": 0, "thread": None}

    def hooked():
        got = real()
        state["n"] += 1
        if state["n"] == 1:                                              # 甲第一次列领取文件之后：乙（另一个线程）去领取；协议对的话它会被挡住，等甲放手
            t = threading.Thread(target=b._claim_current, daemon=True)
            t.start()
            t.join(0.5)
            state["thread"] = t
        return got
    a._batches = hooked
    a.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")
    a._batches = real
    if state["thread"] is not None:
        state["thread"].join(5)
    a.flush_pending()
    b.flush_pending()
    assert seq_order(a) == ["m1", "m2"]                                  # 一问一答没有被拆开


# ================================================================ 二 半个汉字不能挡住新原话
def test_d2_half_a_character_does_not_block_record_count_status_or_open(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    bad = cut_char_line()
    with open(m._pending_path(), "ab") as f:
        f.write(bad)
    assert m.pending_count() == 2                                        # 按原始字节数行，不靠正文能不能解码
    assert m.status()["pending_writes"] == 2
    r = m.record("h", "S1", "m3", "user", "第3句话", time="2027-01-01")
    assert r["raw_saved"] is True or r["pending"] is True                # 入库，或者明确进了待补
    assert "m1" in seq_order(m)
    rv = review_lines(m)
    assert len(rv) == 1 and base64.b64decode(rv[0]["line_b64"]) == bad  # 坏行的原始字节原样保存、能还原
    assert "UnicodeDecodeError" in rv[0]["reason"] and rv[0]["at"]
    m.close()
    m2 = open_mem(tmp_path)                                              # 重新打开也不报错
    assert m2.status()["pending_review"] == 1
    assert "m3" in seq_order(m2) or m2.pending_count() == 1


def test_d3_complete_items_before_and_after_a_bad_utf8_line_are_flushed_in_order(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    bad = "{\"x\": \"中".encode("utf-8")[:-2] + b"\n"                 # 在文件中间、带换行的坏行
    with open(m._pending_path(), "ab") as f:
        f.write(bad)
    m._write_pending(args(2))
    assert m.flush_pending() == 2
    assert seq_order(m) == ["m1", "m2"] and m.pending_count() == 0
    rv = review_lines(m)
    assert len(rv) == 1 and base64.b64decode(rv[0]["line_b64"]) == bad.rstrip(b"\n")


def test_d4_an_append_after_a_cut_line_starts_on_a_new_line_and_is_not_swallowed(tmp_path):
    """追加被截断的那一行没有换行：下一条追加不能接在它后面变成一行（那样完整的一条也会跟着进待核）。"""
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    with open(m._pending_path(), "ab") as f:
        f.write(cut_char_line())
    m._write_pending(args(3))                                            # 追加接在没有换行的截断行后面
    assert m.pending_count() == 3                                        # m1、截断行、m3 是三行，不是两行
    assert m.flush_pending() == 2                                        # m1、m3 都补进去；只有截断那一行进待核
    assert seq_order(m) == ["m1", "m3"] and len(review_lines(m)) == 1


def test_d5_a_stop_rewrite_keeps_raw_bytes_of_the_remaining_lines(tmp_path):
    """补写停在「忙」时，后面的行按原始字节写回，不能因为里面有坏字节而丢掉或抛错。"""
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    bad = cut_char_line() + b"\n"
    with open(m._pending_path(), "ab") as f:
        f.write(bad)
    m._write_pending(args(2))
    orig = m._insert

    def busy_m1(a):
        if a["message_id"] == "m1":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = busy_m1
    assert m.flush_pending() == 0 and m.pending_count() == 3
    claimed = m._batches()[0]
    with open(claimed, "rb") as f:
        assert bad in f.read()                                           # 坏行原样还在批次里
    m._insert = orig
    assert m.flush_pending() == 2 and seq_order(m) == ["m1", "m2"]


def test_d7_if_the_pending_check_itself_fails_the_new_message_is_queued_not_lost(tmp_path):
    """record 的保护要把「检查有没有待补」也包进去：检查出错（读不了待补文件），看不清就排队，新原话不丢，回执列出这个错。"""
    m = open_mem(tmp_path)

    def broken():
        raise RuntimeError("读不了待补文件")
    m.pending_count = broken
    r = m.record("h", "S1", "m1", "user", "第1句话", time="2027-01-01")
    assert r["raw_saved"] is False and r["pending"] is True
    assert any("RuntimeError" in e for e in r["flush_errors"])
    del m.pending_count
    assert m.pending_count() == 1 and m.flush_pending() == 1 and seq_order(m) == ["m1"]


# ================================================================ 三 旧随机名批次重写后顺序不反
def test_d6_legacy_batch_stopped_midway_keeps_its_place_before_a_newer_legacy_batch(tmp_path):
    m = open_mem(tmp_path)
    put_batch(m, "aaaaaaaa", [args(1), args(2)], mtime=1_000_000_000)
    put_batch(m, "bbbbbbbb", [args(3)], mtime=1_100_000_000)
    orig = m._insert
    n = {"busy": True}

    def busy_m2(a):
        if n["busy"] and a["message_id"] == "m2":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = busy_m2
    assert m.flush_pending() == 1 and seq_order(m) == ["m1"]            # 第一份补到一半遇到「忙」，写回时不能刷新它的修改时间
    n["busy"] = False
    assert m.flush_pending() == 2
    assert seq_order(m) == ["m1", "m2", "m3"]


# ================================================================ 守护（不算复现：fbafd1b 上就通过）
def test_guard_with_older_pending_and_a_busy_database_the_new_record_queues_without_deadlock(tmp_path):
    """协议里同一进程不能把同一把锁拿两次：有积压又遇到数据库忙时，新记录要能排进待补（不能自己等自己）。放在线程里，卡住就算失败。"""
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    orig = m._insert

    def busy(a):
        raise sqlite3.OperationalError("database is locked")
    m._insert = busy
    box = {}

    def run():
        box["r"] = m.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "record 自己等自己的锁，卡住了"
    assert box["r"]["pending"] is True
    m._insert = orig
    assert m.flush_pending() == 2 and seq_order(m) == ["m1", "m2"]


def test_guard_concurrent_records_without_backlog_are_all_saved_directly(tmp_path):
    """没有积压时几个线程同时记录（各自一个连接）：全部直接入库，没有一条因为排他协议被排进待补。"""
    path = str(tmp_path / "m.sqlite")
    mems = [Memory.open(path, "w", config=Config(index_version=2), provider=Scripted("skip"), clock=Clock(), spawn="off") for _ in range(6)]
    res = {}

    def run(i):
        res[i] = mems[i].record("h", "S%d" % i, "m%d" % i, "user", "并发的第%d句话" % i, time="2027-01-01")
    ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert all(not t.is_alive() for t in ts) and len(res) == 6
    assert all(r["raw_saved"] is True and r["pending"] is False for r in res.values())
    assert mems[0].pending_count() == 0 and len(seq_order(mems[0])) == 6


def test_guard_pending_count_is_still_a_plain_count_for_status(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    m._write_pending(args(2))
    assert m.pending_count() == 2 and m.status()["pending_writes"] == 2
