"""V6 存储：同键异文拒绝；保存后中断、提交后中断都能接着做；两处同时追加不丢原话；没挂接和放弃步的原话照样能检索；待整理的边界；
历史检索的统计池和候选池不借未来消息；切 A／B0 不改已封步；同一工作区按封步顺序一步一步做，重启后顺序不变；
连续放弃后的暂停和软阈值暂停，不会被普通查询抹掉；status 不拉起整理。"""
import threading

import pytest

from helpers import Clock, Scripted, make_mem, say, sids_of, out_skip
from yuanwei import Config, Memory
from yuanwei import retrieve as RT
from yuanwei.sqlstore import ResponseLost
from yuanwei.storeerr import MessageConflict


def test_same_key_different_text_is_rejected_and_same_text_returns_original(tmp_path):
    mem = make_mem(tmp_path)
    r1 = mem.record("h", "S1", "m1", "user", "第一句", time="2027-01-01")
    r2 = mem.record("h", "S1", "m1", "user", "第一句", time="2027-01-01")
    assert r2["duplicate"] is True and (r2["msg_id"], r2["seq"], r2["fingerprint"]) == (r1["msg_id"], r1["seq"], r1["fingerprint"])
    with pytest.raises(MessageConflict):
        mem.record("h", "S1", "m1", "user", "第一句改了", time="2027-01-01")
    with pytest.raises(MessageConflict):
        mem.record("h", "S1", "m1", "assistant", "第一句", time="2027-01-01")
    assert mem.store.count_messages("w") == 1          # 重送没有重复入库


def test_stable_id_collision_is_a_conflict(tmp_path):
    mem = make_mem(tmp_path)
    mem.record("h", "S1", "m1", "user", "甲", stable_id="X-1")
    with pytest.raises(MessageConflict):
        mem.record("h", "S1", "m2", "user", "乙", stable_id="X-1")


def test_crash_after_save_resumes(tmp_path):
    """保存后中断：原话已存、已封步，进程没了；重开后从队列接着做。"""
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted("thread"), clock=clock)
    say(mem, "S1", 4)
    mem.end_session("h", "S1")
    mem.close()
    prov = Scripted("thread")
    mem2 = make_mem(tmp_path, provider=prov, clock=clock)
    assert [s["status"] for s in mem2.store.list_steps("w")] == ["sealed"]
    assert mem2.run()["done"] == 1
    assert len(prov.calls) == 1


def test_crash_after_commit_resumes_without_resending(tmp_path):
    """提交后中断：整理结果已提交，但响应丢失（或进程在标记步结束之前没了）；重开后按收据认出来，不重发请求，也不重复提交。"""
    clock = Clock()
    prov = Scripted("thread")
    mem = make_mem(tmp_path, provider=prov, clock=clock)
    say(mem, "S1", 4)
    mem.end_session("h", "S1")

    def fault(point):
        if point == "after_commit":
            mem.store.fault = None
            raise ResponseLost("提交已生效，响应丢失")
    mem.store.fault = fault
    r = mem.run()
    assert r["done"] == 1                                 # 响应丢失：按收据认出已提交（recovered），不当失败
    assert len(prov.calls) == 1
    # 进程在 commit 之后、end_step 之前没了：模拟——直接把步改回 sealed，再重开
    mem.store.update_step("w", "step-0001", status="sealed")
    mem.close()
    prov2 = Scripted("thread")
    mem2 = make_mem(tmp_path, provider=prov2, clock=clock)
    assert mem2.run()["done"] == 1
    assert prov2.calls == []                              # replayed：不重发
    st = mem2.store.read_state("w")
    assert len([x for x in st["tables"]["receipts"] if x["kind"] == "step"]) == 1
    assert mem2.store.get_step("w", "step-0001")["status"] == "done"


def test_two_writers_do_not_lose_raw_messages(tmp_path):
    """两处同时追加不丢原话：两个连接、两个线程各记 40 条。"""
    path = str(tmp_path / "shared.sqlite")
    a = Memory.open(path, "w", config=Config(index_version=2), clock=Clock())
    b = Memory.open(path, "w", config=Config(index_version=2), clock=Clock("2027-01-01T09:00:00Z"))
    errs = []

    def work(mem, tag):
        try:
            for i in range(40):
                mem.record("h", "S-" + tag, "m%d" % i, "user" if i % 2 == 0 else "assistant", "%s 线程的第 %d 句" % (tag, i))
        except Exception as e:                                       # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(a, "甲")), threading.Thread(target=work, args=(b, "乙"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    rows = a.store.list_messages("w")
    assert len(rows) == 80
    assert sorted(r["seq"] for r in rows) == list(range(1, 81))     # 序号唯一、连续


def test_unattached_and_abandoned_messages_still_searchable(tmp_path):
    prov = Scripted("skip")                                   # 全部「不记」：没有任何挂接
    mem = make_mem(tmp_path, provider=prov)
    say(mem, "S1", 4, prefix="柠檬")
    mem.end_session("h", "S1")
    mem.run()
    out = mem.recall("柠檬")
    assert out["delivered"], "没挂接的原话照样能检索"
    # 放弃的步
    bad = Scripted("garbage")
    mem2 = make_mem(tmp_path, ws="w2", provider=bad, name="m.sqlite")
    say(mem2, "S9", 4, prefix="榴莲")
    mem2.end_session("h", "S9")
    assert mem2.run()["abandoned"] == 1
    assert mem2.store.get_step("w2", "step-0001")["status"] == "abandoned"
    out2 = mem2.recall("榴莲")
    assert out2["delivered"], "放弃步的原话照样能检索（当作没挂接的原话）"
    assert len(bad.calls) == 2                                  # 初判最多两次（每步最多三发里留一次给重判）；用完记放弃


def test_not_yet_organized_boundary(tmp_path):
    """已入库的原话不等整理就能读（V9）；状态只说处理到了哪个阶段；旧格式截点还是按 visible_at 读（所属的步结束前不可见）。"""
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov)
    ids = say(mem, "S1", 4, prefix="葡萄")
    assert mem.message_status(ids[0]) == "已保存"
    out = mem.recall("葡萄")
    assert out["delivered"] and out["backlog"]["unsealed_messages"] == 4
    assert mem.get_messages([ids[0]])["messages"][0]["msg_id"] == ids[0]
    from yuanwei.errors import NotVisible
    legacy = dict(mem.store.cut("w"), index_version=mem.index_version)
    assert mem.recall("葡萄", cut=legacy)["delivered"] == []                  # 旧格式截点：步没结束，不可见
    with pytest.raises(NotVisible):
        mem.get_messages([ids[0]], cut=legacy)
    mem.end_session("h", "S1")
    assert mem.message_status(ids[0]) == "已封步"
    assert mem.recall("葡萄")["delivered"]                                    # 封了步、还没整理：照样能读
    st = mem.status()
    assert st["backlog"]["pending_steps"] == 1 and st["backlog"]["oldest_unfinished_received_at"]
    mem.run()
    assert mem.message_status(ids[0]) == "已整理"
    assert mem.recall("葡萄")["delivered"]
    assert mem.get_messages([ids[0]])["messages"][0]["msg_id"] == ids[0]


def test_history_cut_does_not_borrow_future_messages(tmp_path):
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov)
    say(mem, "S1", 6, prefix="苹果")
    mem.end_session("h", "S1")
    mem.run()
    cut1 = mem.cut()
    # 后面的会话里有一条带稀有词的消息
    s2_ids = []
    for i in range(1, 5):
        s2_ids.append(mem.record("h", "S2", "x%d" % i, "user" if i % 2 else "assistant", "第二次会话 %d：仙人掌开花了" % i if i == 1 else "第二次会话第%d句闲话" % i)["msg_id"])
    mem.end_session("h", "S2")
    mem.run()
    assert any(d["msg_id"] in s2_ids for d in mem.recall("仙人掌开花")["delivered"])
    old = mem.recall("仙人掌开花", cut=cut1)
    assert not any(d["msg_id"] in s2_ids for d in old["delivered"])          # 旧截点上看不到后来的原话
    assert old["cut"]["raw_seq"] == cut1["raw_seq"]
    snap_old = mem._reader.snapshot(cut1)
    assert all(a.seq <= max(r["seq"] for r in snap_old.rows) for a in snap_old.anchors)
    assert not any("仙人掌" in a.text for a in snap_old.anchors)
    # 统计池：旧截点上的关键词排名，和一个只含第一次会话的新工作区完全一样
    mem_b = make_mem(tmp_path, ws="only1", name="other.sqlite", provider=Scripted("skip"))
    say(mem_b, "S1", 6, prefix="苹果")
    mem_b.end_session("h", "S1")
    mem_b.run()
    q = "苹果的事情编号3"
    ra = RT.Index(snap_old.anchors).rank(q)["kw"]
    rb = RT.Index(mem_b._reader.snapshot().anchors).rank(q)["kw"]
    assert ra and [(round(s, 12)) for _, s in ra] == [(round(s, 12)) for _, s in rb]


def test_switch_prompt_does_not_change_sealed_steps(tmp_path):
    clock = Clock()
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov, clock=clock, write_prompt="A")
    say(mem, "S1", 4)
    mem.end_session("h", "S1")                                 # 封步时是 A
    mem.close()
    mem2 = make_mem(tmp_path, provider=prov, clock=clock, write_prompt="B0")
    say(mem2, "S2", 4)
    mem2.end_session("h", "S2")                                # 这一步是 B0
    s = mem2.store.list_steps("w")
    assert [x["prompt_version"] for x in s] == ["A", "B0"]
    assert s[0]["prompt_sha256"].startswith("dfad13f7") and s[1]["prompt_sha256"].startswith("d9fe1072")
    mem2.run()
    from yuanwei import resources as RS
    assert prov.calls[0]["text"].startswith(RS.write_prompt("A")[0].rstrip("\n")[:200])
    assert prov.calls[1]["text"].startswith(RS.write_prompt("B0")[0].rstrip("\n")[:200])
    assert [x["prompt_version"] for x in mem2.store.list_steps("w")] == ["A", "B0"]      # 跑完也没被改


def test_steps_are_done_in_seal_order_and_survive_restart(tmp_path):
    clock = Clock()
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov, clock=clock)
    for s in ("S1", "S2", "S3"):
        say(mem, s, 2)
        mem.end_session("h", s)
    assert [x["k"] for x in mem.store.list_steps("w")] == [1, 2, 3]
    assert mem.run(max_steps=1)["done"] == 1
    mem.close()
    mem2 = make_mem(tmp_path, provider=prov, clock=clock)
    assert mem2.run()["done"] == 2
    assert [c["step_id"] for c in prov.calls] == ["step-0001", "step-0002", "step-0003"]


def test_failed_step_blocks_the_next_one(tmp_path):
    """前一步没结束（出错停下），后一步不会抢在它前面做。"""
    def mode(request, n):
        raise RuntimeError("模拟装置出错")
    prov = Scripted(mode)
    mem = make_mem(tmp_path, provider=prov)
    for s in ("S1", "S2"):
        say(mem, s, 2)
        mem.end_session("h", s)
    r = mem.run()
    assert r["status"] == "stopped" and r["stopped_at"] == "step-0001"
    assert [c["step_id"] for c in prov.calls] == ["step-0001"]
    assert mem.store.get_step("w", "step-0002")["status"] == "sealed"


def test_pause_after_three_abandons_survives_ordinary_queries(tmp_path):
    prov = Scripted("garbage")
    mem = make_mem(tmp_path, provider=prov, spawn="off")                       # 记原话的时候不拉起（record 现在会拉起）
    for s in ("S1", "S2", "S3", "S4"):
        say(mem, s, 2)
        mem.end_session("h", s)
    mem.spawn_mode = "inline"
    r = mem.run()
    assert r["status"] == "paused" and r["reason"] == "consecutive_abandons" and r["abandoned"] == 3
    calls = len(prov.calls)
    for _ in range(3):                                        # 普通查询（允许拉起整理的入口）都抹不掉暂停
        mem.recall("第1句")
        mem.check_and_spawn()
    assert mem.pause_state()["reason"] == "consecutive_abandons"
    assert len(prov.calls) == calls and mem.store.get_step("w", "step-0004")["status"] == "sealed"
    assert mem.status()["paused"]["reason"] == "consecutive_abandons"
    mem.resume()                                               # 只有明确的恢复命令能解除
    assert mem.pause_state() is None


def test_daily_budget_pause_is_not_erased_by_queries(tmp_path):
    clock = Clock()
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov, clock=clock, daily_cap_cny=0.5, spawn="inline")
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    r = mem.run()
    assert r["status"] == "paused" and r["reason"] == "daily_budget" and prov.calls == []      # 开始新的一步之前，已记账加最坏估计超线：不开始
    for _ in range(3):
        mem.recall("第1句")
    assert mem.pause_state()["reason"] == "daily_budget" and prov.calls == []
    clock.advance(days=1)                                      # 第二天软阈值自己解除
    assert mem.pause_state() is None


def test_unknown_cost_is_charged_at_worst_case(tmp_path):
    """超时但不知道是否计费的调用，按最坏估计记，不记零。"""
    from yuanwei.ask import RetryableNetworkError

    def mode(request, n):
        if n == 1:
            raise RetryableNetworkError("超时")
        return out_skip(sids_of(request["text"]))
    prov = Scripted(mode)
    mem = make_mem(tmp_path, provider=prov)
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.run()
    rows = mem.store.cost_rows("w")
    assert rows[0]["unknown"] == 1 and rows[0]["cny"] > 0.5
    assert rows[1]["unknown"] == 0


def test_status_does_not_spawn_but_record_and_recall_do(tmp_path):
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov, spawn="inline")
    say(mem, "S1", 2)
    mem.end_session("h", "S1")                                 # end_session 本身不是拉起整理的入口
    assert prov.calls == []
    mem.status()
    mem.export(str(tmp_path / "x.jsonl"))
    assert prov.calls == []                                    # status／导出不拉起
    mem.recall("第1句")                                         # recall 拉起
    assert len(prov.calls) == 1


def test_schema_version_and_migration_framework(tmp_path):
    from yuanwei.sqlstore import SCHEMA_VERSION, Store
    s = Store(str(tmp_path / "v.sqlite"))
    assert s.schema_version() == SCHEMA_VERSION == 12
    s.conn.execute("UPDATE schema_meta SET version=99")
    s.close()
    from yuanwei.storeerr import StoreError
    with pytest.raises(StoreError):
        Store(str(tmp_path / "v.sqlite"))


def test_workspace_store_kind_and_index_version_are_fixed(tmp_path):
    mem = make_mem(tmp_path, index_version=1)
    mem.close()
    mem2 = make_mem(tmp_path, index_version=2)                 # 已有工作区：沿用它自己的索引版本
    assert mem2.index_version == 1


def test_write_transaction_is_not_held_while_calling_the_model(tmp_path):
    """调模型时不占写事务：模型调用期间，别的连接能立刻拿到写锁。"""
    import sqlite3
    path = str(tmp_path / "m.sqlite")
    seen = []

    def mode(request, n):
        c = sqlite3.connect(path, timeout=0.05, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
            seen.append(True)
        except sqlite3.OperationalError:
            seen.append(False)
        finally:
            c.close()
        return out_skip(sids_of(request["text"]))
    mem = make_mem(tmp_path, provider=Scripted(mode))
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.run()
    assert seen == [True]


def test_check_and_spawn_conditions(tmp_path, monkeypatch):
    """后台拉起：有积压、没暂停、没人在整理才拉起；用独立子进程跑 yuanwei run。"""
    import subprocess
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    mem = make_mem(tmp_path, provider=Scripted("skip"), spawn="subprocess")
    assert mem.check_and_spawn()["action"] == "none" and launched == []                   # 没积压
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    r = mem.check_and_spawn()
    assert r["action"] == "spawned" and launched[0][-1] == "run" and "-m" in launched[0] and "--workspace" in launched[0]
    with mem._run_lock() as got:                                                           # 有人在整理（持锁）
        assert got and mem.check_and_spawn()["reason"] == "已有进程在整理"
        assert len(launched) == 1
    mem._pause("consecutive_abandons")                                                     # 暂停
    assert mem.check_and_spawn()["reason"].startswith("paused") and len(launched) == 1


def test_spawn_checks_budget_before_launching(tmp_path, monkeypatch):
    import subprocess
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted("skip"), clock=clock, daily_cap_cny=1.0, spawn="subprocess")
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.store.add_cost("w", None, clock(), clock()[:10], "m", "fp", None, 1.5, False)     # 今天已记账 ¥1.5，超过 ¥1 的线
    r = mem.check_and_spawn()
    assert r["reason"] == "paused:daily_budget" and launched == [] and mem.pause_state()["reason"] == "daily_budget"
