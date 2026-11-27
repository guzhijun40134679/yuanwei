"""清单第 56 条第一批·第 1 节：同一个库连接被几个线程共用时，事务不能互相踩。
复现（第 6 行）：Hermes 的后台线程（sync_turn、end_session）和主线程的开轮（turn_token）同时写，旧代码里一个线程的 BEGIN 撞上另一个线程还没提交的事务。
全部在临时目录里、假数据，不联网，不碰 ~/.hermes。"""
import inspect
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from helpers2 import organized
from yuanwei import sqlstore as SS
from yuanwei.access import Access
from yuanwei.hermes_adapter import HermesAdapter

ROUNDS = 1000


def _race(fns, timeout=120):
    """每个函数在自己的线程里跑；返回各线程抛出的异常（类名＋文字）。"""
    errs, ts = [], []

    def run(fn):
        try:
            fn()
        except BaseException as e:                                                       # noqa: BLE001
            errs.append("%s: %s" % (type(e).__name__, e))
    for fn in fns:
        ts.append(threading.Thread(target=run, args=(fn,)))
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout)
    assert not any(t.is_alive() for t in ts), "线程没在 %d 秒内结束" % timeout
    return errs


def test_turn_token_and_record_in_two_threads_1000_rounds_zero_failures(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)

    def tokens():
        for i in range(ROUNDS):
            acc.turn_token("hermes", "hs1", "%d:abc" % i)

    def records():
        for i in range(ROUNDS):
            mem.record("hermes", "hs1", "m-%d" % i, "user" if i % 2 else "assistant", "第%d句话，编号%d" % (i, i), time="2027-02-01")
    errs = _race([tokens, records])
    assert errs == []
    n = mem.store.conn.execute("SELECT COUNT(*) FROM turn_tokens").fetchone()[0]
    assert n == ROUNDS
    assert mem.store.conn.execute("SELECT COUNT(*) FROM messages WHERE host='hermes'").fetchone()[0] == ROUNDS
    assert not mem.store.conn.in_transaction


def test_reader_in_the_middle_of_another_threads_transaction_sees_only_committed_rows(tmp_path):
    mem = organized(tmp_path)
    st = mem.store
    inside, go, seen, mark = threading.Event(), threading.Event(), [], {}

    def writer():
        with st.tx() as c:
            c.execute("INSERT INTO store_meta VALUES ('probe', 'x')")
            inside.set()
            go.wait(10)
            time.sleep(0.3)                                                              # 让读线程有机会在事务中途读
            mark["end_of_body"] = time.time()

    def reader():
        inside.wait(10)
        go.set()
        r = st.conn.execute("SELECT v FROM store_meta WHERE k='probe'").fetchone()       # 要等写线程提交才轮到；旧代码里它立刻就读到了没提交的行
        mark["read_at"] = time.time()
        seen.append(None if r is None else r[0])
    errs = _race([writer, reader])
    assert errs == [] and seen == ["x"]
    assert mark["read_at"] >= mark["end_of_body"]


def test_reader_never_sees_a_rolled_back_row(tmp_path):
    mem = organized(tmp_path)
    st = mem.store
    inside, go, seen = threading.Event(), threading.Event(), []

    def writer():
        try:
            with st.tx() as c:
                c.execute("INSERT INTO store_meta VALUES ('probe', 'x')")
                inside.set()
                go.wait(10)
                time.sleep(0.2)
                raise RuntimeError("回滚")
        except RuntimeError:
            pass

    def reader():
        inside.wait(10)
        go.set()
        r = st.conn.execute("SELECT v FROM store_meta WHERE k='probe'").fetchone()
        seen.append(r)
    assert _race([writer, reader]) == [] and seen == [None]


def test_worker_end_session_and_sync_turn_at_the_same_time(tmp_path):
    """/bg 那种：后台线程里 end_session，同时 Hermes 自己的线程在 sync_turn、主线程在开轮。"""
    mem = organized(tmp_path)
    a = HermesAdapter({"mode": "tools_only", "spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    old = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]
    N = 60

    def ends():
        for i in range(N):
            mem.end_session("hermes", "hs1")

    def syncs():
        for i in range(N):
            text = "后台线程里的第%d句" % i
            a.on_turn_start(i + 1, text, author_id=None, author_name=None, author_is_bot=False)
            a.sync_turn(text, "回复%d" % i, session_id="hs1", messages=old + [{"role": "user", "content": text}, {"role": "assistant", "content": "回复%d" % i}])

    def tokens():
        acc = Access(mem)
        for i in range(N * 3):
            acc.turn_token("hermes", "hs9", "x%d" % i)
    errs = _race([ends, syncs, tokens])
    assert errs == []
    assert a.recorded_turns == N and a.skipped == {}
    assert mem.store.conn.execute("SELECT COUNT(*) FROM messages WHERE host='hermes' AND role='user'").fetchone()[0] == N


def test_another_process_holding_the_write_lock_for_two_seconds_the_turn_waits_and_is_not_lost(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ready = tmp_path / "ready"
    code = ("import sqlite3,sys,time;c=sqlite3.connect(sys.argv[1],isolation_level=None);c.execute('BEGIN IMMEDIATE');"
            "open(sys.argv[2],'w').write('1');time.sleep(2);c.execute('ROLLBACK')")
    p = subprocess.Popen([sys.executable, "-B", "-c", code, mem.db_path, str(ready)])
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists()
        t0 = time.time()
        tok = acc.turn_token("hermes", "hs1", "1:abc")                                   # 别的进程握着写锁：等它放（默认等 3 秒）
        waited = time.time() - t0
    finally:
        p.wait(10)
    assert waited >= 1.0
    assert acc.turn_token("hermes", "hs1", "1:abc") == tok
    r = mem.record("hermes", "hs1", "after", "user", "锁放了以后写进去", time="2027-02-01")
    assert not r.get("pending")


def test_generator_and_nested_reads_leave_no_open_statement(tmp_path):
    mem = organized(tmp_path)
    st = mem.store
    it = iter(st.conn.execute("SELECT * FROM messages"))                                 # 生成器式的读：用到一半放着
    next(it)
    with st.read_tx() as c:
        with st.read_tx() as c2:                                                         # 嵌套读：同一个连接、同一个事务
            assert c is c2
            assert c2.execute("SELECT COUNT(*) FROM messages").fetchone()[0] > 0
        assert st.conn.in_transaction
    assert not st.conn.in_transaction
    with st.tx() as c:
        c.execute("INSERT INTO store_meta VALUES ('p2', 'y')")                           # 放着的读没有挡住后面的写（旧代码里半截的语句会让提交、WAL 检查点出问题）
    for _ in range(3):
        st.conn.execute("SELECT * FROM messages").fetchone()                             # 只取一行就丢：也不能留着开着的语句
    assert st.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0         # 0＝没被挡住（有开着的语句会返回 1）


def test_close_waits_for_the_running_transaction(tmp_path):
    mem = organized(tmp_path)
    st = mem.store
    inside, done = threading.Event(), []

    def writer():
        with st.tx() as c:
            c.execute("INSERT INTO store_meta VALUES ('p3', 'z')")
            inside.set()
            time.sleep(0.3)
        done.append("committed")

    def closer():
        inside.wait(10)
        st.close()
        done.append("closed")
    assert _race([writer, closer]) == [] and done == ["committed", "closed"]


# ---------------------------------------------------------------- 静态：_gate 永远在最里面
VIOLATIONS = []


class _Boom:
    """握着 tx() 时换在别的锁位置上的东西：谁去拿它，就记一笔（调用处可能把异常吞掉，所以不能只靠抛错）并抛错。"""

    def _hit(self):
        VIOLATIONS.append("握着 tx() 去拿了别的锁（锁次序：_gate 永远在最里面）")
        raise AssertionError(VIOLATIONS[-1])

    def __enter__(self):
        self._hit()

    def __exit__(self, *a):
        return False

    def acquire(self, *a, **k):
        self._hit()

    def release(self):
        return None


def test_lock_order_gate_is_innermost_no_other_lock_is_taken_while_holding_tx(tmp_path, monkeypatch):
    from yuanwei import tools as TL
    from yuanwei import modes as MD
    from yuanwei import window as WN
    mem = organized(tmp_path)
    a = HermesAdapter({"mode": "tools_only", "spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    del VIOLATIONS[:]
    a.on_turn_start(1, "先开一轮", author_id=None, author_name=None, author_is_bot=False)
    surface = next(iter(a._surfaces.values()))
    runner = a.runner
    locks = [(a, "_lock"), (a.win, "_lock"), (surface, "_lock"), (runner, "_lock")]
    real = {}
    for obj, name in locks:
        if hasattr(obj, name):
            real[(id(obj), name)] = getattr(obj, name)
    assert len(real) >= 2, "这一版里这几把锁至少要有两把在，不然这个测试是空的"
    assert hasattr(a, "_lock") and hasattr(a.win, "_lock")
    holding = {"n": 0}
    orig_tx = mem.store.tx

    import contextlib

    @contextlib.contextmanager
    def tx_marked():
        with orig_tx() as c:
            for obj, name in locks:
                if hasattr(obj, name):
                    setattr(obj, name, _Boom())
            try:
                yield c
            finally:
                for obj, name in locks:
                    if hasattr(obj, name):
                        setattr(obj, name, real[(id(obj), name)])
    monkeypatch.setattr(mem.store, "tx", tx_marked)
    # 主流程里会开事务的几个入口：开轮、记一轮、窗口控制、发读许可
    old = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]
    a.on_turn_start(2, "第二轮的话", author_id=None, author_name=None, author_is_bot=False)
    a.sync_turn("第二轮的话", "回复", session_id="hs1", messages=old + [{"role": "user", "content": "第二轮的话"}, {"role": "assistant", "content": "回复"}])
    a.on_turn_start(3, "#yuanwei-nomem", author_id=None, author_name=None, author_is_bot=False)
    a.on_turn_start(4, "#yuanwei-mem", author_id=None, author_name=None, author_is_bot=False)
    a.handle_tool_call("window_state", {})
    a.handle_tool_call("recall", {"query": "编号"})
    assert VIOLATIONS == []
    assert a.recorded_turns == 1


def test_gate_is_one_reentrant_lock_per_store_and_wraps_every_sql_call():
    src = inspect.getsource(SS._Conn)
    assert "RLock" in inspect.getsource(SS) and "def execute" in src
    for forbidden in ("cursor", "executemany", "executescript"):
        assert "def %s" % forbidden in src                                               # 用不到的入口：显式拒绝，不让它悄悄绕过锁
