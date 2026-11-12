"""补充施工单一·第 2 节：失败步不放弃，退避重试（1 分钟、5 分钟、30 分钟、之后每次 2 小时），同一步连续失败 5 次 → 持久暂停「整理连续失败」。"""
import sqlite3

import pytest

from helpers import Clock, Scripted, make_mem, say, out_skip, sids_of
from yuanwei import memory as M
from yuanwei.ask import RetryableNetworkError


def net_down(request, n):
    raise RetryableNetworkError("断网")


def setup(tmp_path, mode=net_down, **kw):
    clock = Clock()
    prov = Scripted(mode)
    mem = make_mem(tmp_path, provider=prov, clock=clock, spawn="inline", **kw)
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    return mem, prov, clock


def test_backoff_schedule_values():
    assert [M.Memory.backoff_seconds(n) for n in range(0, 8)] == [0, 60, 300, 1800, 7200, 7200, 7200, 7200]


def test_failed_step_waits_and_ordinary_queries_do_not_resend(tmp_path):
    mem, prov, clock = setup(tmp_path)
    r = mem.run()
    assert r["status"] == "stopped" and len(prov.calls) == 2           # 初判两次发送都断网，算这一步失败一次
    st = mem.store.get_step("w", "step-0001")
    assert st["status"] == "failed" and st["failures"] == 1 and st["last_failed_at"]
    n = len(prov.calls)
    for _ in range(3):                                                 # 退避期间普通查询（会触发检查的入口）不发请求
        mem.recall("第1句")
    assert len(prov.calls) == n
    r2 = mem.run()
    assert r2["status"] == "backoff" and r2["stopped_at"] == "step-0001" and len(prov.calls) == n and r2["retry_at"]
    clock.advance(seconds=59)
    mem.recall("第1句")
    assert len(prov.calls) == n                                       # 差 1 秒到 1 分钟：仍不发
    clock.advance(seconds=1)
    mem.recall("第1句")                                               # 到时间：重试
    assert len(prov.calls) == n + 2
    assert mem.store.get_step("w", "step-0001")["failures"] == 2


def test_full_schedule_then_pause_after_fifth_failure(tmp_path):
    mem, prov, clock = setup(tmp_path)
    mem.run()
    waits = [60, 300, 1800, 7200]
    for i, w in enumerate(waits, 1):
        before = len(prov.calls)
        clock.advance(seconds=w - 1)
        mem.recall("x")
        assert len(prov.calls) == before                                 # 第 i 次失败后要等 w 秒：差一秒不发
        clock.advance(seconds=1)
        mem.recall("x")
        assert len(prov.calls) == before + 2                              # 到点重试一次（每次重试两次发送）
        assert mem.store.get_step("w", "step-0001")["failures"] == i + 1
    p = mem.pause_state()                                                 # 第 5 次失败：持久暂停
    assert p["reason"] == "step_failures"
    assert p["detail"] == {"label": "整理连续失败", "step_id": "step-0001", "failures": 5, "last_error": mem.store.get_step("w", "step-0001")["last_error"]}
    assert "断网" in p["detail"]["last_error"]
    assert mem.status()["paused"]["detail"]["failures"] == 5
    n = len(prov.calls)
    clock.advance(days=3)
    for _ in range(3):                                                    # 暂停不被普通查询抹掉，也不再发请求
        mem.recall("x")
        mem.check_and_spawn()
    assert mem.pause_state()["reason"] == "step_failures" and len(prov.calls) == n
    assert mem.run()["status"] == "paused"
    # 恢复命令：解除并清零这一步的失败计数和退避
    mem.resume()
    st = mem.store.get_step("w", "step-0001")
    assert mem.pause_state() is None and st["failures"] == 0 and st["last_failed_at"] is None and st["status"] == "sealed"
    mem.recall("x")                                                       # 清零以后马上就能重试（没有退避）
    assert len(prov.calls) == n + 2 and mem.store.get_step("w", "step-0001")["failures"] == 1
    assert mem.store.get_step("w", "step-0001")["status"] != "abandoned"


def test_failures_are_not_abandons(tmp_path):
    mem, prov, clock = setup(tmp_path)
    mem.run()
    for w in (60, 300, 1800, 7200):
        clock.advance(seconds=w)
        mem.run()
    assert mem.store.workspace("w")["consec_abandons"] == 0
    assert mem.status()["abandoned_steps"] == 0 and mem.store.get_step("w", "step-0001")["status"] == "failed"


def test_success_clears_failure_count(tmp_path):
    state = {"fail": 2}

    def flaky(request, n):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise RuntimeError("临时出错")                                 # 模型出错（不是网络类）也算失败
        return out_skip(sids_of(request["text"]))
    mem, prov, clock = setup(tmp_path, mode=flaky)
    mem.run()
    assert mem.store.get_step("w", "step-0001")["failures"] == 1
    clock.advance(seconds=60)
    mem.run()
    assert mem.store.get_step("w", "step-0001")["failures"] == 2
    clock.advance(seconds=300)
    assert mem.run()["done"] == 1
    st = mem.store.get_step("w", "step-0001")
    assert st["status"] == "done" and st["failures"] == 0 and st["last_error"] is None and st["last_failed_at"] is None


def test_later_steps_do_not_jump_ahead_during_backoff(tmp_path):
    mem, prov, clock = setup(tmp_path)
    say(mem, "S2", 2)
    mem.end_session("h", "S2")
    mem.run()
    n = len(prov.calls)
    assert {c["step_id"] for c in prov.calls} == {"step-0001"}
    mem.recall("x")
    assert len(prov.calls) == n and mem.store.get_step("w", "step-0002")["status"] == "sealed"


def test_format_failures_still_abandon_and_pause_after_three(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("garbage"), spawn="off")           # 记原话的时候不拉起（record 现在会拉起），封好四步以后手动 run
    for s in ("S1", "S2", "S3", "S4"):
        say(mem, s, 2)
        mem.end_session("h", s)
    r = mem.run()
    assert r["abandoned"] == 3 and mem.pause_state()["reason"] == "consecutive_abandons"


def test_migration_from_v1_adds_retry_column_turn_marks_and_read_settings(tmp_path):
    from yuanwei.sqlstore import MIGRATIONS, SCHEMA_VERSION, Store
    path = str(tmp_path / "old.sqlite")
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    c.execute("INSERT INTO schema_meta VALUES (1)")
    for stmt in MIGRATIONS[1]:
        c.execute(stmt)
    c.close()
    s = Store(path)
    assert s.schema_version() == SCHEMA_VERSION == 12
    cols = [r["name"] for r in s.conn.execute("PRAGMA table_info(steps)")]
    assert "last_failed_at" in cols and "config_snapshot" in cols
    for t in ("turn_marks", "read_settings", "current_turn"):                                           # v3、v4 的表也一并迁移上来
        assert s.conn.execute("SELECT name FROM sqlite_master WHERE name=?", (t,)).fetchone(), t


def test_network_failed_sends_do_not_use_up_the_retry_budget_but_rejected_ones_do(tmp_path):
    def mode(request, n):
        if n == 1:
            return "这不是 JSON"                                           # 第一次：真发出去、被拒收（算数）
        if n == 2:
            raise RetryableNetworkError("断网")                           # 第二次：断网（重试时不占次数）
        return out_skip(sids_of(request["text"]))
    mem, prov, clock = setup(tmp_path, mode=mode)
    assert mem.run()["status"] == "stopped"
    js = mem.store.list_judgments("w", "step-0001", "q1")
    assert [j["status"] for j in js] == ["rejected"]                       # 断网那条挪去归档了，被拒的还在
    assert len(mem.store.list_judgments("w", "step-0001", "q1~f1")) == 1
    clock.advance(seconds=60)
    r = mem.run()                                                          # 这一次只剩 1 次发送可用，够了
    assert r["done"] == 1 and len(prov.calls) == 3
    assert [j["status"] for j in mem.store.list_judgments("w", "step-0001", "q1")] == ["rejected", "validated"]
