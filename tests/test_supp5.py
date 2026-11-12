"""补充施工单五：待补原话的顺序（R2-a）和补写者归属（R2-b）。确定性地安排交错；先写成在 0c6a58b 上会失败的测试，守护测试单列在文件末尾。全部自造数据。"""
import os
import sqlite3
import subprocess
import sys

import pytest

from helpers import Clock, Scripted
from yuanwei import Config, Memory
from yuanwei import memory as MEM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def open_mem(tmp_path, name="m.sqlite", clock=None, **cfg):
    cfg.setdefault("index_version", 2)
    return Memory.open(str(tmp_path / name), "w", config=Config(**cfg), provider=Scripted("skip"), clock=clock or Clock(), spawn="off")


def args(i, session="S1", t=None):
    return dict(host="h", session_id=session, message_id="m%d" % i, role="user" if i % 2 else "assistant", text="第%d句话" % i, time=t or "2027-01-01", branch="",
                time_precision="day", stable_id=None)


def locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")


def seq_order(mem):
    return [r[0] for r in mem.store.conn.execute("SELECT host_msg_id FROM messages WHERE ns='w' ORDER BY seq")]


# ================================================================ R2-a 顺序
def test_a1_failed_earlier_item_stays_ahead_of_items_appended_later(tmp_path):
    """Codex 的反例：用户句 m1 补写时锁忙，这时另一个实例追加了助手回复 m2；补完以后 m1 必须在 m2 前面，一问一答配对对。"""
    m1 = open_mem(tmp_path)
    m2 = open_mem(tmp_path)
    m1._write_pending(args(1))
    orig = m1._insert

    def fail_m1_and_other_instance_appends(a):
        if a["message_id"] == "m1":
            m2._write_pending(args(2))
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m1._insert = fail_m1_and_other_instance_appends
    assert m1.flush_pending() == 0
    m1._insert = orig
    assert m1.flush_pending() == 2
    assert seq_order(m1) == ["m1", "m2"]
    m1._event("h", "S1", "", "end")
    steps = m1.maintain()
    assert len(steps) == 1 and [m1.store.message_row("w", x)["host_msg_id"] for x in steps[0]["msg_ids"]] == ["m1", "m2"]      # 一问一答在同一步里，没有被拆错


def test_a2_in_one_batch_a_failed_item_stops_the_batch_and_later_items_wait(tmp_path):
    m = open_mem(tmp_path)
    for i in (1, 2, 3):
        m._write_pending(args(i))
    orig = m._insert

    def fail_first(a):
        if a["message_id"] == "m1":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = fail_first
    assert m.flush_pending() == 0
    assert seq_order(m) == [] and m.pending_count() == 3                  # m2、m3 本来能补，但不能越过 m1
    m._insert = orig
    assert m.flush_pending() == 3 and seq_order(m) == ["m1", "m2", "m3"] and m.pending_count() == 0
    assert m.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")["duplicate"] is True           # 重送不重复


def test_a3_leftover_batches_are_consumed_in_claim_order_not_by_random_file_name(tmp_path, monkeypatch):
    """中断遗留的批次加上新批次：先领取的先补，和文件名的随机性无关（让后领取的批次文件名排在前面）。"""
    ids = iter(["ffffffff", "00000000", "88888888", "11111111"])

    class FakeUuid:
        @staticmethod
        def uuid4():
            class U:
                hex = next(ids) * 4
            return U()
    monkeypatch.setattr(MEM, "uuid", FakeUuid)
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    orig = m._insert

    def die(a):
        raise KeyboardInterrupt("补写中途失败")
    m._insert = die
    with pytest.raises(KeyboardInterrupt):
        m.flush_pending()                                                # 批次 1（m1）留在原地
    m._write_pending(args(2))
    with pytest.raises(KeyboardInterrupt):
        m.flush_pending()                                                # 批次 2（m2）领取了但也没补成
    m._write_pending(args(3))                                            # 当前文件里的新追加
    m._insert = orig
    assert m.flush_pending() == 3
    assert seq_order(m) == ["m1", "m2", "m3"] and m.pending_count() == 0


def test_a4_while_older_items_are_still_pending_new_records_queue_behind_them(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    orig = m._insert
    state = {"block_m1": True}

    def maybe_fail(a):
        if state["block_m1"] and a["message_id"] == "m1":
            raise sqlite3.OperationalError("database is locked")
        return orig(a)
    m._insert = maybe_fail
    r = m.record("h", "S1", "m2", "assistant", "第2句话", time="2027-01-01")
    assert r["raw_saved"] is False and r["pending"] is True               # 较早的 m1 还没补进去：m2 不能抢在它前面直接入库
    state["block_m1"] = False
    assert m.flush_pending() == 2 and seq_order(m) == ["m1", "m2"]


# ================================================================ R2-b 补写者归属
def test_b1_second_flusher_during_a_flush_gets_busy_and_cannot_touch_the_claimed_file(tmp_path, monkeypatch):
    a = open_mem(tmp_path)
    b = open_mem(tmp_path)
    for i in (1, 2):
        a._write_pending(args(i))
    state = {"fired": False, "b": None}
    real_open = open

    def hooked_open(path, *x, **k):
        if ".claim-" in str(path) and not state["fired"] and (not x or "r" in x[0]):
            state["fired"] = True
            state["b"] = b.flush_pending()                                 # 甲取得领取、还没打开文件，乙来补写
        return real_open(path, *x, **k)
    monkeypatch.setattr(MEM, "open", hooked_open, raising=False)
    assert a.flush_pending() == 2                                          # 甲不被中断
    assert state["fired"] and state["b"] == 0                              # 乙没有处理甲领取的文件
    assert seq_order(a) == ["m1", "m2"] and a.pending_count() == 0


def test_b2_open_is_not_blocked_by_an_active_flusher(tmp_path):
    a = open_mem(tmp_path)
    a._write_pending(args(1))
    with a._flusher_lock() as got:
        assert got is True
        b = open_mem(tmp_path)                                             # 正常打开：不报错、不阻塞
        assert b.pending_count() == 1 and b.flush_pending() == 0
        assert b.flush_pending_detail() == {"status": "busy", "inserted": 0, "left": 1}


def test_b3_a_dead_flusher_releases_its_lock_and_the_next_one_continues_its_batch(tmp_path):
    a = open_mem(tmp_path)
    a._write_pending(args(1))
    a._write_pending(args(2))
    code = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "from yuanwei import Memory\n"
        "m = Memory.__new__(Memory); m.ns = 'w'; m.config_dir = %r\n"           # 不用 open（open 会自己补写）：只要补写者锁和领取
        "cm = m._flusher_lock(); assert cm.__enter__() is True\n"
        "p = m._pending_path(); os.replace(p, p + '.claim-%%012d' %% 1)\n"       # 领取了批次，然后进程没了
        "os._exit(0)\n" % (os.path.join(ROOT, "src"), a.config_dir))
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert a.pending_count() == 2
    assert a.flush_pending() == 2 and seq_order(a) == ["m1", "m2"]         # 锁随进程消失，后一个补写者接上遗留批次


def test_b4_appending_is_allowed_while_a_flusher_holds_the_flusher_lock(tmp_path):
    a = open_mem(tmp_path)
    b = open_mem(tmp_path)
    a._write_pending(args(1))
    orig = a._insert

    def insert_then_append(x):
        r = orig(x)
        b._write_pending(args(2))                                          # 补写期间照样能保存新项（短锁，不被补写者锁挡住）
        return r
    a._insert = insert_then_append
    assert a.flush_pending() == 1 and a.pending_count() == 1
    a._insert = orig
    assert a.flush_pending() == 1 and seq_order(a) == ["m1", "m2"]


def test_b5_pending_count_survives_files_changing_underneath_it(tmp_path, monkeypatch):
    a = open_mem(tmp_path)
    a._write_pending(args(1))
    p = a._pending_path()
    os.replace(p, p + ".claim-%012d" % 1)
    a._write_pending(args(2))
    real_open = open

    def vanishing(path, *x, **k):
        if ".claim-" in str(path):
            raise FileNotFoundError(path)                                  # 正常领取／清理时文件被改名或删掉
        return real_open(path, *x, **k)
    monkeypatch.setattr(MEM, "open", vanishing, raising=False)
    assert a.pending_count() == 1                                          # 不报错；消失的文件不计


# ================================================================ 守护（不算复现：旧代码上就通过，守住不退步）
def test_guard_appended_items_survive_a_flush_and_resend_does_not_duplicate(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    assert m.flush_pending() == 1
    assert m.record("h", "S1", "m1", "user", "第1句话", time="2027-01-01")["duplicate"] is True
    assert seq_order(m) == ["m1"]


def test_guard_single_flusher_interrupted_midway_loses_nothing(tmp_path):
    m = open_mem(tmp_path)
    m._write_pending(args(1))
    m._write_pending(args(2))
    orig = m._insert
    n = {"c": 0}

    def die_on_second(a):
        n["c"] += 1
        if n["c"] == 2:
            raise KeyboardInterrupt("中断")
        return orig(a)
    m._insert = die_on_second
    with pytest.raises(KeyboardInterrupt):
        m.flush_pending()
    m._insert = orig
    assert m.flush_pending() >= 1
    assert seq_order(m) == ["m1", "m2"] and m.pending_count() == 0
