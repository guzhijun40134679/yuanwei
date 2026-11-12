"""补充施工单五之二：待补里补不进去的项不能卡住记录。先写成在 eb47563 上会失败的测试；守护测试（旧代码上就通过）单列在文件末尾。全部自造数据。"""
import json
import os
import sqlite3

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


def raw_append(mem, line):
    with open(mem._pending_path(), "a", encoding="utf-8") as f:
        f.write(line + "\n")


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


# ================================================================ 1 补不进去又不是「忙」的项：挪到待核，后面的接着补
def test_c1_conflicting_duplicate_is_set_aside_and_later_items_still_flush(tmp_path):
    """主会话探针：数据库忙时同一条消息记了两次、内容不同。补写碰到冲突：这一项挪到待核，后面的照常补；新原话不能丢。"""
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    m._write_pending(args(1, text="第1句话（改了的另一份）"))
    m._write_pending(args(2))
    r = m.record("h", "S1", "m3", "user", "第3句话", time="2027-01-01")
    assert r["raw_saved"] is True and r["pending"] is False                        # 新原话入库了，不是丢了
    assert seq_order(m) == ["m1", "m2", "m3"]                                      # 冲突那一项之后的 m2 没被卡住，顺序对
    assert m.pending_count() == 0
    rv = review_lines(m)
    assert len(rv) == 1 and json.loads(rv[0]["line"])["text"] == "第1句话（改了的另一份）"
    assert "MessageConflict" in rv[0]["reason"] and rv[0]["at"]                    # 原行、原因、挪出的时间都在
    assert m.status()["pending_review"] == 1


def test_c2_half_written_line_is_set_aside_with_its_original_text(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    bad = '{"host": "h", "session_id": "S1", "message_id": "m2", "role": "assis'
    raw_append(m, bad)
    m._write_pending(args(3))
    r = m.record("h", "S1", "m4", "assistant", "第4句话", time="2027-01-01")
    assert r["raw_saved"] is True
    assert seq_order(m) == ["m1", "m3", "m4"] and m.pending_count() == 0
    rv = review_lines(m)
    assert len(rv) == 1 and rv[0]["line"] == bad and "JSONDecodeError" in rv[0]["reason"]


def test_c3_non_dict_and_missing_fields_are_set_aside_not_fatal(tmp_path):
    m = open_mem(tmp_path)
    raw_append(m, "[1, 2, 3]")
    raw_append(m, json.dumps({"host": "h"}))
    m._write_pending(args(1))
    assert m.flush_pending() == 1
    assert seq_order(m) == ["m1"] and len(review_lines(m)) == 2 and m.pending_count() == 0


def test_c4_after_a_bad_item_a_busy_item_still_stops_in_order_and_new_record_queues_behind(tmp_path):
    """坏项挪走以后，后面遇到「忙」仍照补充五的顺序规则停在那一条；新原话排在它们后面进待补，不丢、不抛。"""
    m = open_mem(tmp_path)
    raw_append(m, "not json at all")
    m._write_pending(args(1))
    m._write_pending(args(2))
    orig = m._insert

    def busy_m1(a):
        if a["message_id"] == "m1":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = busy_m1
    r = m.record("h", "S1", "m3", "user", "第3句话", time="2027-01-01")
    assert r["raw_saved"] is False and r["pending"] is True
    assert m.pending_count() == 3 and len(review_lines(m)) == 1 and seq_order(m) == []
    m._insert = orig
    assert m.flush_pending() == 3 and seq_order(m) == ["m1", "m2", "m3"]


def test_c5_set_aside_items_are_not_retried_again(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    m._write_pending(args(1, text="另一份"))
    assert m.flush_pending() == 1
    assert len(review_lines(m)) == 1
    assert m.flush_pending() == 0 and m.flush_pending() == 0
    assert len(review_lines(m)) == 1 and m.pending_count() == 0                    # 待核里的不再自动重试，也不重复记


# ================================================================ 2 record 入口：补写出任何错，新原话都要落盘
def test_c6_with_older_pending_and_flush_raising_the_new_message_queues_behind(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))

    def boom():
        raise RuntimeError("补写意外出错")
    m.flush_pending_detail = boom
    m.flush_pending = boom
    r = m.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")
    assert r["raw_saved"] is False and r["pending"] is True                        # 排在 m1 后面进待补，不抢先、不丢
    assert any("RuntimeError" in e for e in r["flush_errors"])
    del m.flush_pending_detail, m.flush_pending
    assert m.flush_pending() == 2 and seq_order(m) == ["m1", "m2"]


def test_c7_receipt_lists_the_errors_found_while_flushing(tmp_path):
    m = open_mem(tmp_path)
    raw_append(m, "{坏行")
    r = m.record("h", "S1", "m1", "user", "第1句话", time="2027-01-01")
    assert r["raw_saved"] is True
    assert len(r["flush_errors"]) == 1 and "JSONDecodeError" in r["flush_errors"][0]


# ================================================================ 3 Memory.open 不能被补写出错挡住
def test_c8_open_is_not_blocked_by_a_bad_pending_line(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    m._write_pending(args(1, text="另一份"))
    raw_append(m, "半行")
    m.close()
    m2 = open_mem(tmp_path)                                                         # 旧版本这里直接抛 MessageConflict／JSONDecodeError
    assert seq_order(m2) == ["m1"] and m2.pending_count() == 0
    assert m2.status()["pending_review"] == 2


def test_c9_open_survives_an_unexpected_flush_error_and_status_shows_it(tmp_path, monkeypatch):
    def boom(self):
        raise RuntimeError("打开时补写意外出错")
    monkeypatch.setattr(MEM.Memory, "flush_pending_detail", boom)
    m = open_mem(tmp_path)
    st = m.status()
    assert any("RuntimeError" in e for e in st["flush_errors"])


# ================================================================ 5 0c6a58b 留下的随机名领取文件
def test_c10_legacy_random_named_claim_files_are_counted(tmp_path):
    m = open_mem(tmp_path)
    put_batch(m, "ab12cd34", [args(1)])
    put_batch(m, "12345678", [args(2)])                                             # 随机名恰好全是数字，也不能当成编号批次
    assert m.pending_count() == 2


def test_c11_legacy_claim_files_flush_before_numbered_batches_in_modification_time_order(tmp_path):
    m = open_mem(tmp_path)
    put_batch(m, "ffffffff", [args(2)], mtime=2_000_000_000)                       # 名字排在后面、修改得更晚
    put_batch(m, "00000000", [args(1)], mtime=1_900_000_000)                       # 名字排在前面、修改得更早（全数字，最容易被误认成编号）
    put_batch(m, "%012d" % 1, [args(3)], mtime=1_000_000_000)                      # 编号批次修改得再早，也排在所有随机名批次后面
    raw_append(m, json.dumps(args(4)))
    assert m.pending_count() == 4
    assert m.flush_pending() == 4
    assert seq_order(m) == ["m1", "m2", "m3", "m4"] and m.pending_count() == 0


def test_c12_legacy_batch_that_cannot_be_inserted_stops_in_place_and_numbered_ones_wait(tmp_path):
    m = open_mem(tmp_path)
    p = put_batch(m, "ab12cd34", [args(1), args(2)], mtime=1_900_000_000)
    put_batch(m, "%012d" % 1, [args(3)])
    orig = m._insert

    def busy_m1(a):
        if a["message_id"] == "m1":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = busy_m1
    assert m.flush_pending() == 0 and seq_order(m) == [] and m.pending_count() == 3
    assert os.path.exists(p)
    m._insert = orig
    assert m.flush_pending() == 3 and seq_order(m) == ["m1", "m2", "m3"]


def test_c13_new_claims_after_legacy_files_use_numbers_above_every_numbered_batch(tmp_path):
    m = open_mem(tmp_path)
    put_batch(m, "ab12cd34", [args(1)])
    orig = m._insert

    def busy(a):
        raise sqlite3.OperationalError("database is locked")
    m._insert = busy
    m._write_pending(args(2))
    assert m.flush_pending() == 0
    names = sorted(n for n in os.listdir(os.path.dirname(m._pending_path())) if ".claim-" in n)
    assert os.path.exists("%s.claim-%012d" % (m._pending_path(), 1)) and len(names) == 2      # 随机名文件不占用编号：新批次从 1 起，也不能因为认得随机名而在算最大编号时出错
    m._insert = orig
    assert m.flush_pending() == 2 and seq_order(m) == ["m1", "m2"]


# ================================================================ 守护（不算复现：eb47563 上就通过；靠放回缺陷验证它守得住）
def test_guard_gap_in_batch_numbers_never_lets_a_new_claim_overwrite_an_unfinished_batch(tmp_path):
    """批次 1 补完删掉，批次 2 补到一半停下（编号有空档）；下一次领取必须用 3，不能用批次个数（=1）算出 2 而覆盖批次 2。"""
    m = open_mem(tmp_path)
    p2 = put_batch(m, "%012d" % 2, [args(1), args(2)])
    orig = m._insert

    def busy(a):
        raise sqlite3.OperationalError("database is locked")
    m._insert = busy
    m._write_pending(args(3))
    assert m.flush_pending() == 0
    assert os.path.exists(p2) and m.pending_count() == 3                           # 批次 2 的两条没有被覆盖
    with open(p2, encoding="utf-8") as f:
        assert [json.loads(l)["message_id"] for l in f if l.strip()] == ["m1", "m2"]
    assert os.path.exists("%s.claim-%012d" % (m._pending_path(), 3))
    m._insert = orig
    assert m.flush_pending() == 3 and seq_order(m) == ["m1", "m2", "m3"]


def test_guard_busy_still_stops_in_place_and_is_not_reported_as_an_error(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    orig = m._insert

    def busy(a):
        raise sqlite3.OperationalError("database is locked")
    m._insert = busy
    r = m.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")
    assert r["pending"] is True and "flush_errors" not in r and review_lines(m) == []
    m._insert = orig
    assert m.flush_pending() == 2
