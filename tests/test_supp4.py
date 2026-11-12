"""补充施工单四：整段复核的四处必改（R1 并发封步、R2 待补原话两个漏口、R3 record 拉起整理、R4 封步配置快照）和顺带三项。
每一处都用确定性安排的交错（不靠多跑碰运气）；先写成在原来的代码上会失败的测试。全部自造数据。"""
import json
import os
import sqlite3

import pytest

from helpers import Clock, Scripted, make_mem
from yuanwei import Config, Memory
from yuanwei import memory as MEM
from yuanwei.errors import ConfigError


def open_mem(tmp_path, ws="w", clock=None, provider=None, name="m.sqlite", spawn="off", **cfg):
    cfg.setdefault("index_version", 2)
    return Memory.open(str(tmp_path / name), ws, config=Config(**cfg), provider=provider, clock=clock or Clock(), spawn=spawn)


def args(i, text=None, session="S1"):
    return dict(host="h", session_id=session, message_id="m%d" % i, role="user" if i % 2 else "assistant", text=text or "第%d句话" % i, time="2027-01-01", branch="",
                time_precision="day", stable_id=None)


# ================================================================ R1 并发封步
def test_r1_two_connections_cannot_seal_the_same_messages_twice(tmp_path):
    prov = Scripted("skip")
    a = open_mem(tmp_path, provider=prov)
    b = open_mem(tmp_path, provider=prov)
    a.record("h", "S1", "1", "user", "你好呀")
    a.record("h", "S1", "2", "assistant", "你好")
    a._event("h", "S1", "", "end")                                     # 会话结束事件，但还没有人封步
    real = a.store.seal_step
    state = {"n": 0}

    def interleaved(*x, **k):
        if state["n"] == 0:
            state["n"] = 1
            b.maintain()                                              # 甲已经取得待封集合，乙抢先封好了
        return real(*x, **k)
    a.store.seal_step = interleaved
    a.maintain()
    steps = a.store.list_steps("w")
    assert len(steps) == 1, [s["step_id"] for s in steps]
    owners = {a.store.message_row("w", m)["step_id"] for m in a.store.get_step("w", steps[0]["step_id"])["msg_ids"]}
    assert owners == {steps[0]["step_id"]}
    r = a.run()
    assert r["done"] == 1 and len(prov.calls) == 1                     # 整理一次
    assert len(a.store.visible_messages("w", a.store.cut("w")["vclock"])) == 2   # 原话可见（旧格式截点的读法：步结束后才有 visible_at）
    a.record("h", "S2", "1", "user", "下一个会话")
    a.end_session("h", "S2")
    assert a.run()["done"] == 1                                        # 后面的步接着做


def test_r1_store_level_claim_is_conditional_and_rolls_back(tmp_path):
    mem = open_mem(tmp_path)
    mem.record("h", "S1", "1", "user", "甲")
    mem.record("h", "S1", "2", "assistant", "乙")
    ids = [m["msg_id"] for m in mem.store.unsealed_messages("w")]
    mem.store.seal_step("w", "h", "S1", "", ids, [], lambda k: "step-%d" % k, "2027-01-01T00:00:00Z", "t")
    from yuanwei.storeerr import StoreError
    with pytest.raises(StoreError):
        mem.store.seal_step("w", "h", "S1", "", ids, [], lambda k: "step-%d" % k, "2027-01-01T00:00:01Z", "t")
    assert len(mem.store.list_steps("w")) == 1 and {mem.store.message_row("w", m)["step_id"] for m in ids} == {"step-1"}


# ================================================================ R2 待补原话
def test_r2_flush_keeps_items_appended_while_it_runs(tmp_path):
    m1 = open_mem(tmp_path, name="m.sqlite")
    m2 = open_mem(tmp_path, name="m.sqlite")
    m1._write_pending(args(1))
    orig = m1._insert
    state = {"n": 0}

    def insert_then_other_instance_appends(a):
        r = orig(a)
        if state["n"] == 0:
            state["n"] = 1
            m2._write_pending(args(2))                                   # 补写中途，另一个实例追加了一条
        return r
    m1._insert = insert_then_other_instance_appends
    assert m1.flush_pending() == 1
    assert m1.pending_count() == 1                                      # 后来追加的留下
    m1._insert = orig
    assert m1.flush_pending() == 1 and m1.pending_count() == 0
    assert {m["host_msg_id"] for m in m1.store.unsealed_messages("w")} == {"m1", "m2"}


def test_r2_interrupted_flush_loses_nothing_and_resend_does_not_duplicate(tmp_path):
    m1 = open_mem(tmp_path)
    m1._write_pending(args(1))
    m1._write_pending(args(2))
    orig = m1._insert
    state = {"n": 0}

    def die_on_second(a):
        state["n"] += 1
        if state["n"] == 2:
            raise KeyboardInterrupt("补写中途失败")
        return orig(a)
    m1._insert = die_on_second
    with pytest.raises(KeyboardInterrupt):
        m1.flush_pending()
    m1.close()
    m2 = open_mem(tmp_path)                                              # 重开：构造时补写
    keys = sorted(m["host_msg_id"] for m in m2.store.unsealed_messages("w"))
    assert keys == ["m1", "m2"] and m2.pending_count() == 0              # 不丢
    assert m2.record("h", "S1", "m1", "user", "第1句话", time="2027-01-01")["duplicate"] is True      # 重送不重
    assert len(m2.store.unsealed_messages("w")) == 2


def test_r2_pre_insert_maintenance_failure_cannot_bypass_the_pending_file(tmp_path, monkeypatch):
    clock = Clock()
    mem = open_mem(tmp_path, clock=clock)
    mem.record("h", "OLD", "1", "user", "旧会话")
    mem.record("h", "OLD", "2", "assistant", "回复")
    clock.advance(minutes=45)                                            # 旧会话到了空闲封步时间
    monkeypatch.setattr(MEM, "MAX_RETRY_BUSY", 1)
    monkeypatch.setattr(MEM.time, "sleep", lambda s: None)
    mem.store.conn.execute("PRAGMA busy_timeout=20")
    blocker = sqlite3.connect(str(tmp_path / "m.sqlite"), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")                                   # 另一个连接真实持有写锁
    try:
        r = mem.record("h", "NEW", "1", "user", "新消息")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert r["raw_saved"] is False and r["pending"] is True               # 没有抛错，原话进了待补
    assert mem.pending_count() == 1
    arrived = clock()
    clock.advance(hours=2)                                               # 补写发生在两小时以后
    assert mem.flush_pending() == 1
    assert mem.store.conn.execute("SELECT received_at FROM messages WHERE session_id='NEW'").fetchone()[0] == arrived      # 记的是 record 当时收到的时间，不是补写的时刻
    assert [r[0] for r in mem.store.conn.execute("SELECT content FROM messages WHERE session_id='NEW'")] == ["新消息"]


def test_r2_any_maintenance_failure_before_or_after_insert_keeps_the_raw_message_and_receipt(tmp_path):
    mem = open_mem(tmp_path)
    real = mem.maintain
    calls = {"n": 0}

    def flaky(now=None):
        calls["n"] += 1
        raise RuntimeError("维护出错 %d" % calls["n"])
    mem.maintain = flaky
    r = mem.record("h", "S1", "1", "user", "仍然要存下来")
    assert r["raw_saved"] is True and r["pending"] is False and r["seq"] == 1 and "维护出错" in (r.get("maintenance_error") or "")
    mem.maintain = real
    assert mem.store.message_row("w", r["msg_id"])["content"] == "仍然要存下来"


def test_r2_pending_message_keeps_its_original_arrival_time(tmp_path):
    clock = Clock("2027-01-01T08:00:00Z")
    mem = open_mem(tmp_path, clock=clock)
    mem._write_pending(dict(args(1), received_at="2027-01-01T07:00:00Z"))
    clock.advance(hours=3)
    assert mem.flush_pending() == 1
    assert mem.store.unsealed_messages("w")[0]["received_at"] == "2027-01-01T07:00:00Z"


# ================================================================ R3 record 拉起整理
def test_r3_record_starts_organizing_through_the_public_entry(tmp_path):
    prov = Scripted("skip")
    mem = open_mem(tmp_path, provider=prov, spawn="inline", max_step_messages=2)
    mem.record("h", "S1", "1", "user", "问一句")
    assert prov.calls == []
    r = mem.record("h", "S1", "2", "assistant", "答一句")
    assert r["raw_saved"] is True and r["status"] in ("已保存", "已封步", "已整理")
    assert len(prov.calls) >= 1 and mem.store.list_steps("w")[0]["status"] == "done"


def test_r3_spawn_off_and_paused_and_status_do_not_spawn(tmp_path):
    prov = Scripted("skip")
    mem = open_mem(tmp_path, provider=prov, spawn="off", max_step_messages=2)
    mem.record("h", "S1", "1", "user", "问")
    mem.record("h", "S1", "2", "assistant", "答")
    assert prov.calls == []
    mem2 = open_mem(tmp_path, provider=prov, spawn="inline", name="p.sqlite", max_step_messages=2)
    mem2._pause("consecutive_abandons")
    mem2.record("h", "S1", "1", "user", "问")
    mem2.record("h", "S1", "2", "assistant", "答")
    assert prov.calls == []                                              # 暂停：照旧不拉起
    mem3 = open_mem(tmp_path, provider=prov, spawn="inline", name="q.sqlite", max_step_messages=2)
    mem3.check_and_spawn = lambda: (_ for _ in ()).throw(AssertionError("status 不能拉起"))
    mem3.status()


def test_r3_spawn_failure_cannot_overturn_the_saved_receipt(tmp_path):
    mem = open_mem(tmp_path, provider=Scripted("skip"), spawn="inline")
    mem.check_and_spawn = lambda: (_ for _ in ()).throw(RuntimeError("拉起失败"))
    r = mem.record("h", "S1", "1", "user", "原话")
    assert r["raw_saved"] is True and "拉起失败" in (r.get("spawn_error") or "")


# ================================================================ R4 封步配置快照
def requests_for(tmp_path, name, **cfg):
    prov = Scripted("skip")
    mem = open_mem(tmp_path, provider=prov, name=name, **cfg)
    mem.record("h", "S1", "1", "user", "改摘要要不要")
    mem.record("h", "S1", "2", "assistant", "好")
    mem.end_session("h", "S1")
    mem.run()
    return prov.calls[0]["text"], mem


def test_r4_config_changed_after_sealing_still_runs_with_the_sealed_config(tmp_path):
    base_text, base = requests_for(tmp_path, "base.sqlite", allow_summary_edit=True)
    prov = Scripted("skip")
    m1 = open_mem(tmp_path, provider=prov, name="x.sqlite", allow_summary_edit=True)
    m1.record("h", "S1", "1", "user", "改摘要要不要")
    m1.record("h", "S1", "2", "assistant", "好")
    m1.end_session("h", "S1")                                            # 封步（登记的是 allow_summary_edit=True）
    sealed_version = m1.store.get_step("w", "step-0001")["config_version"]
    m1.close()
    m2 = open_mem(tmp_path, provider=prov, name="x.sqlite", allow_summary_edit=False)      # 重开，配置换了
    assert m2.cfg.config_version() != sealed_version
    m2.run()
    sent = prov.calls[0]["text"]
    assert sent == base_text                                             # 按封步时的配置发：和一直没换配置的基线逐字节相同
    assert m2.store.get_step("w", "step-0001")["config_version"] == sealed_version          # 登记的标签没有被「修齐」
    assert m2.store.get_step("w", "step-0001")["status"] == "done"


def test_r4_config_changed_after_a_failure_keeps_the_same_binding(tmp_path):
    state = {"n": 0}
    texts = []

    def mode(request, n):
        texts.append(request["text"])
        state["n"] += 1
        if state["n"] <= 2:
            from yuanwei.ask import RetryableNetworkError
            raise RetryableNetworkError("断网")
        from helpers import out_skip, sids_of
        return out_skip(sids_of(request["text"]))
    clock = Clock()
    m1 = open_mem(tmp_path, provider=Scripted(mode), clock=clock, allow_summary_edit=True)
    m1.record("h", "S1", "1", "user", "问")
    m1.record("h", "S1", "2", "assistant", "答")
    m1.end_session("h", "S1")
    assert m1.run()["status"] == "stopped"                               # 失败：发了但没成功
    m1.close()
    clock.advance(minutes=5)
    m2 = open_mem(tmp_path, provider=Scripted(mode), clock=clock, allow_summary_edit=False)
    assert m2.run()["done"] == 1
    assert all(("改摘要" in t) == ("改摘要" in texts[0]) for t in texts)      # 失败前后各次发送用的是同一版提示


def test_r4_legacy_step_without_a_snapshot_is_rejected_before_sending_when_config_differs(tmp_path):
    prov = Scripted("skip")
    m1 = open_mem(tmp_path, provider=prov, allow_summary_edit=True)
    m1.record("h", "S1", "1", "user", "问")
    m1.record("h", "S1", "2", "assistant", "答")
    m1.end_session("h", "S1")
    m1.store.conn.execute("UPDATE steps SET config_snapshot=NULL")        # 旧库里封的步：没有快照，还原不了
    m1.close()
    m2 = open_mem(tmp_path, provider=prov, allow_summary_edit=False)
    r = m2.run()
    assert r["status"] == "stopped" and "配置" in r["reason"] and prov.calls == []
    st = m2.store.get_step("w", "step-0001")
    assert st["status"] == "sealed" and st["failures"] == 0
    m3 = open_mem(tmp_path, provider=prov, allow_summary_edit=True)       # 配置和封步时一致：照常
    assert m3.run()["done"] == 1


def test_r4_snapshot_is_the_full_write_config_and_fingerprint_matches(tmp_path):
    mem = open_mem(tmp_path, allow_summary_edit=False, strict_grouping=True)
    mem.record("h", "S1", "1", "user", "问")
    mem.end_session("h", "S1")
    st = mem.store.get_step("w", "step-0001")
    snap = json.loads(st["config_snapshot"])
    assert set(snap) == {"light", "allow_summary_edit", "strict_grouping", "long_limit"} and snap["allow_summary_edit"] is False and snap["strict_grouping"] is True
    assert snap["light"] == mem.cfg.light.as_dict()
    assert Config.snapshot_version(snap) == st["config_version"] == mem.cfg.config_version()


def test_r4_snapshot_that_does_not_match_its_fingerprint_is_rejected(tmp_path):
    prov = Scripted("skip")
    m1 = open_mem(tmp_path, provider=prov)
    m1.record("h", "S1", "1", "user", "问")
    m1.end_session("h", "S1")
    snap = json.loads(m1.store.get_step("w", "step-0001")["config_snapshot"])
    snap["allow_summary_edit"] = not snap["allow_summary_edit"]
    m1.store.conn.execute("UPDATE steps SET config_snapshot=?", (json.dumps(snap),))
    r = m1.run()
    assert r["status"] == "stopped" and prov.calls == []


# ================================================================ 顺带：中性回执、README
@pytest.mark.parametrize("reason,needle", [("clause_ban", "禁读"), ("turn_marked", "禁读"), ("host_marker", "禁读"), ("workspace_setting", "持续设置"), ("session_setting", "持续设置"),
                                           ("permission_unknown", "无法确认权限，已暂停新增读取")])
def test_refusal_text_depends_on_the_reason_and_never_claims_what_it_does_not_know(reason, needle):
    t = MEM.refusal_text(reason)
    assert needle in t
    assert "这次没有查" not in t and "用户明确说了" not in t or reason == "clause_ban"
    if reason == "permission_unknown":
        assert "禁读" not in t and "用户" not in t


def test_refusals_carry_status_and_reason_and_the_reason_specific_text(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("h", "S1", "1", "user", "贝壳")
    mem.end_session("h", "S1")
    mem.run()
    mem.set_read_permission(False)
    r = mem.recall("贝壳")
    assert r["status"] == "refused" and r["reason"] == "workspace_setting" and r["text"] == MEM.refusal_text("workspace_setting")
    mem.store.read_settings = lambda ns: (_ for _ in ()).throw(RuntimeError("读不了"))
    r2 = mem.recall("贝壳")
    assert r2["reason"] == "permission_unknown" and r2["text"] == MEM.refusal_text("permission_unknown")


def test_readme_matches_the_current_state():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    t = open(os.path.join(root, "README.zh-CN.md"), encoding="utf-8").read()                 # 11-12 起中文说明在 README.zh-CN.md，README.md 是英文
    en = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    assert "Every memory has its origin; every quote has its source." in en and "open source" not in en.lower() and "open-source" not in en.lower()
    assert "(https://github.com/guzhijun40134679/yuanwei/blob/main/README.zh-CN.md)" in en and "(https://github.com/guzhijun40134679/yuanwei/blob/main/README.md)" in t and "Chinese only" in en   # 完整网址：PyPI 页面上相对链接打不开
    assert "回答前先查" not in t and "没有便条" not in t
    assert "交回点" not in t and "还没做" not in t and "开源" not in t          # 11-11 文档改准：README 给用户看，不留开发过程的说法，不称开源
    assert "事有原委，话有出处" in t
    dev = open(os.path.join(root, "docs", "开发说明.md"), encoding="utf-8").read()  # 开发细节从 README 挪到这里
    assert "回答前先查" not in dev and "没有便条" not in dev
    assert "只给工具" in dev and "便条" in dev and "第二段" in dev and "接头" in dev
    d = open(os.path.join(root, "docs", "产品规则表.md"), encoding="utf-8").read()
    assert "不能当多会话的安全绑定" in d and "不能直接暴露给模型" in d
