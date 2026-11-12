"""补充施工单一（第二段·交回点甲）：调用绑定在发起它的那一轮、连接关闭以后在途结果不交出、两步登记不绕开来源检查，加三条小改。
全部自造数据，没有网络。每一项都先在旧代码上失败过（见 测试先行记录.txt）。"""
import threading

import pytest

from helpers import make_mem
from helpers2 import TEXTS, Counter, FakeEmbedClient, hybrid, organized
from yuanwei import hosts as H
from yuanwei import note as NT
from yuanwei.access import Access, ConnectionRefused, ToolIdentity
from yuanwei.idsource import ConnectionIdentity, InProcessIdentity
from yuanwei.mcp import server as S
from yuanwei.modes import ModeConfig, ModeRunner
from yuanwei.tools import ToolSurface

W1 = "本轮未新增读取长期记忆"
W3 = "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认"
W4 = "无法确认权限，已暂停新增读取"


class FlipSource:
    """测试用的身份源：current() 返回「此刻」的身份，测试可以在两次调用之间改它（模拟同进程宿主里轮次换了）。"""

    def __init__(self, ident):
        self.ident = ident

    def current(self):
        return self.ident


def surf_for(tmp_path, ident, host="pi", turns=("T1",)):
    mem = organized(tmp_path)
    acc = Access(mem)
    for t in turns:
        acc.begin_turn(host, "S", t)
    src = FlipSource(ident)
    return mem, acc, ToolSurface(mem, acc, H.profile(host), src), src


# ================================================================== 接头-R1：调用绑定在发起它的那一轮
def test_identity_without_a_turn_marker_is_refused_not_given_the_latest_turn(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1", text="#yuanwei-nomem")
    acc.begin_turn("pi", "S", "T2", text="普通")
    cnt = Counter(mem)
    try:
        r = acc.start_read(ToolIdentity("pi", "S"), "recall")
        assert isinstance(r, dict) and r["reason"] == "turn_unproven" and r["receipt"] == W4
        assert cnt.calls == []
    finally:
        cnt.close()


def test_t1_denied_t2_allowed_t1_late_call_is_refused_and_not_logged_under_t2(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1", text="#yuanwei-nomem")
    late = ToolIdentity("pi", "S", "", "T1")                       # T1 发起的调用，身份在发起时就绑定了 T1
    acc.begin_turn("pi", "S", "T2", text="普通")
    r = acc.start_read(late, "recall")
    assert isinstance(r, dict) and r["reason"] == "stale_turn"
    assert [x for x in acc.log_rows("pi", "S", "T2") if x["kind"] == "start"] == []


def test_session_level_connection_cannot_prove_the_turn_so_a_late_t1_call_is_not_let_through(tmp_path):
    """Codex 探针：T1 禁读，身份只是会话级；T2 允许以后，T1 的迟到调用不能被放行、不能记到 T2。"""
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1", text="#yuanwei-nomem")
    handle = acc.register_connection("pi", "S")                     # 会话级句柄：只证明会话，不证明轮次
    src = ConnectionIdentity(acc, handle, "proc-1")
    late = src.current()
    acc.begin_turn("pi", "S", "T2", text="普通")
    r = acc.start_read(late, "recall")
    assert isinstance(r, dict) and r["reason"] == "turn_unproven"
    assert [x for x in acc.log_rows("pi", "S", "T2") if x["kind"] == "start"] == []


def test_per_turn_connection_capability_binds_the_turn_and_goes_stale(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1")
    h1 = acc.register_connection("pi", "S", turn_id="T1")
    ids = ConnectionIdentity(acc, h1, "proc-1")
    assert ids.current().turn_id == "T1"
    tk = acc.start_read(ids.current(), "recall")
    assert not isinstance(tk, dict)
    acc.begin_turn("pi", "S", "T2")
    r = acc.start_read(ids.current(), "recall")
    assert isinstance(r, dict) and r["reason"] == "stale_turn"


def test_call_waiting_in_the_queue_keeps_the_turn_it_was_bound_to(tmp_path):
    """同一条连接队列里排着的旧调用，到 T2 才轮到执行：被拒，而且不碰读取层。"""
    mem, acc, surf, src = surf_for(tmp_path, ToolIdentity("pi", "S", "", "T1"))
    cnt = Counter(mem)
    out = {}
    try:
        surf._lock.acquire()                                       # 前面有一个调用还占着队列
        t = threading.Thread(target=lambda: out.setdefault("r", surf.call("recall", {"query": "烤鱼"})))
        t.start()
        for _ in range(100):                                       # 等它真的排上队（身份必须在这之前就取好）
            threading.Event().wait(0.01)
            if t.is_alive():
                break
        acc.begin_turn("pi", "S", "T2")
        src.ident = ToolIdentity("pi", "S", "", "T2")               # 宿主这一刻的身份已经是 T2
        surf._lock.release()
        t.join(10)
        assert out["r"].status == "refused" and out["r"].reason == "stale_turn"
        assert "recall" not in cnt.calls and "select" not in cnt.calls and "candidates" not in cnt.calls
    finally:
        cnt.close()


def test_in_process_identity_carries_the_turn_it_was_set_with(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ids = InProcessIdentity()
    surf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    acc.begin_turn("hermes", "S", "T1")
    ids.set("hermes", "S", "", "T1")
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"
    acc.begin_turn("hermes", "S", "T2", text="#yuanwei-nomem")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "stale_turn"                  # 插件还没把身份换成 T2：不拿最新一轮顶上
    ids.set("hermes", "S", "", "T2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "control_nomem"
    ids.set("hermes", "S", "")                                                              # 没有轮次标记
    assert surf.call("recall", {"query": "烤鱼"}).reason == "turn_unproven"


# ================================================================== 接头-R2：连接关闭以后在途结果不交出
def conn_surface(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1")
    handle = acc.register_connection("pi", "S", turn_id="T1")
    ids = ConnectionIdentity(acc, handle, "proc-1")
    return mem, acc, ToolSurface(mem, acc, H.profile("pi"), ids), handle


@pytest.mark.parametrize("point", ["after_search", "after_assembled", "before_return"])
def test_connection_closed_in_flight_discards_the_result_but_keeps_the_log(tmp_path, point):
    mem, acc, surf, handle = conn_surface(tmp_path)
    orig = {"mark": acc.mark_search, "asm": acc.assembled, "fin": acc.finish_read}

    def closing(fn):
        def inner(*a, **k):
            r = fn(*a, **k)
            acc.close_connection(handle)
            return r
        return inner
    if point == "after_search":
        acc.mark_search = closing(orig["mark"])
    elif point == "after_assembled":
        acc.assembled = closing(orig["asm"])
    else:
        def fin(*a, **k):
            acc.close_connection(handle)
            return orig["fin"](*a, **k)
        acc.finish_read = fin
    r = surf.call("recall", {"query": "吃了什么烤鱼"})
    assert r.status == "refused" and r.reason == "connection_closed"
    assert TEXTS[1] not in r.text and r.meta["receipt"] == W3                             # 已经查过、没交出：回执照实写
    kinds = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "search_entered" in kinds and "blocked_at_return" in kinds and "returned" not in kinds


def test_finish_read_rechecks_the_connection_not_only_the_turn(tmp_path):
    """Codex 探针：认领后开始读取，随后连接关闭，旧凭据的 finish_read 不能返回 ok。"""
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1")
    handle = acc.register_connection("pi", "S", turn_id="T1")
    src = ConnectionIdentity(acc, handle, "proc-1")
    tk = acc.start_read(src.current(), "recall")
    assert not isinstance(tk, dict)
    acc.close_connection(handle)
    again = acc.start_read(src.current(), "recall")                                         # 冻结身份还在，但连接已关：新的读取也被拒
    assert isinstance(again, dict) and again["reason"] == "connection_closed"
    out = acc.finish_read(tk, "synthetic result", "mcp_return", host="pi")
    assert out["status"] == "refused" and out["reason"] == "connection_closed"


def test_connection_claimant_is_a_per_start_random_token_not_the_pid(tmp_path):
    a, b = S.instance_claimant(), S.instance_claimant()
    assert a != b and a.startswith("inst-") and len(a) >= 20
    mem = organized(tmp_path)
    acc = Access(mem)
    h = acc.register_connection("pi", "S", turn_id="T1")
    ConnectionIdentity(acc, h, a)
    dup = ConnectionIdentity(acc, h, b)                                                     # 同 pid 的另一次启动认领不了
    assert dup.error == "connection_claimed" and dup.current() is None


# ================================================================== 接头-R3：两步登记不能绕开来源检查
@pytest.mark.parametrize("kw", [{"origin": "unknown"}, {"replay": True}])
def test_complete_turn_refuses_unknown_origin_and_replay_and_keeps_reads_refused(tmp_path, kw):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.open_turn("pi", "U", "T1")
    r = acc.complete_turn("pi", "U", "T1", text="#yuanwei-nomem", **kw)
    assert r["registered"] is False and r["ignored"] in ("origin_unknown", "replay")
    assert acc.current_turn("pi", "U")["state"] == "registering"
    got = acc.start_read(ToolIdentity("pi", "U", "", "T1"), "recall")
    assert isinstance(got, dict) and got["reason"] == "registration_incomplete"
    assert [x["kind"] for x in acc.log_rows("pi", "U", "T1")][-2:] == [("unknown_origin_ignored" if "origin" in kw else "replay_ignored"), "refused"]
    # 之后同一轮由可信的用户来源完成，照常成立
    ok = acc.complete_turn("pi", "U", "T1", text="#yuanwei-nomem")
    assert ok["registered"] and ok["turn"]["refused"] is True


def test_complete_turn_on_an_already_open_denied_turn_is_unchanged_by_an_unknown_call(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.begin_turn("pi", "S", "T1", text="#yuanwei-nomem")
    acc.complete_turn("pi", "S", "T1", text="普通", origin="unknown")
    assert acc.current_turn("pi", "S")["refused"] is True
    assert acc.start_read(ToolIdentity("pi", "S", "", "T1"), "recall")["reason"] == "control_nomem"


def test_normal_two_step_registration_resend_and_retry_after_failure_behave_as_before(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    acc.open_turn("pi", "S", "T1")
    r = acc.complete_turn("pi", "S", "T1", text="普通的一句")
    assert r["registered"] and r["created"] and r["turn"]["state"] == "open" and r["turn"]["refused"] is False
    again = acc.complete_turn("pi", "S", "T1", text="#yuanwei-nomem")                    # 重复事件：不改已经成立的状态
    assert again["created"] is False and again["turn"]["refused"] is False
    acc.fault = lambda p: (_ for _ in ()).throw(RuntimeError("busy")) if p == "between_open_and_complete" else None
    with pytest.raises(RuntimeError):
        acc.begin_turn("pi", "S", "T2", text="#yuanwei-nomem")
    acc.fault = None
    assert acc.start_read(ToolIdentity("pi", "S", "", "T2"), "recall")["reason"] == "registration_incomplete"
    done = acc.begin_turn("pi", "S", "T2", text="#yuanwei-nomem")                         # 补登记
    assert done["turn"]["refused"] is True


# ================================================================== 小改
def test_turn_token_is_reused_for_the_same_event_and_differs_across_events_and_restarts(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    a = acc.turn_token("pi", "S", "evt-1")
    assert acc.turn_token("pi", "S", "evt-1") == a
    assert acc.turn_token("pi", "S", "evt-2") != a
    assert acc.turn_token("pi", "S2", "evt-1") != a
    assert acc.turn_token("pi", "S", "evt-1", branch="b") != a
    assert Access(mem).turn_token("pi", "S", "evt-1") == a                                 # 重新打开（进程重启）也一样
    assert isinstance(a, str) and a
    with pytest.raises(ValueError):
        acc.turn_token("pi", "S", "")


def test_resent_event_with_a_reused_token_does_not_clear_a_ban_or_open_a_new_turn(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    tok = acc.turn_token("pi", "S", "evt-1")
    acc.begin_turn("pi", "S", tok, text="#yuanwei-nomem")
    acc.begin_turn("pi", "S", acc.turn_token("pi", "S", "evt-1"), text="#yuanwei-nomem")
    assert acc.current_turn("pi", "S")["gen"] == 1 and acc.current_turn("pi", "S")["refused"] is True


def test_auto_fetch_does_not_pile_up_threads_in_one_session_and_late_vector_calls_are_logged(tmp_path, monkeypatch):
    mem, client = hybrid(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="note", auto_timeout_s=0.2))
    release = threading.Event()
    orig = NT.analyze
    calls = {"n": 0}

    def slow(reader, query, cfg, cut=None):
        calls["n"] += 1
        release.wait(5)
        return orig(reader, query, cfg, cut)
    monkeypatch.setattr(NT, "analyze", slow)
    a = r.on_user_prompt("S", "T1", "吃了什么烤鱼")
    assert a["kind"] == "timeout"
    b = r.on_user_prompt("S", "T2", "预算批下来了吗")                                         # 同一会话里上一个线程还没结束：不再叠开
    assert b["kind"] == "busy" and b["inject"] is None and calls["n"] == 1
    assert "auto_fetch_skipped_busy" in [x["kind"] for x in acc.log_rows("pi", "S", "T2")]
    to = [x for x in acc.log_rows("pi", "S", "T1") if x["kind"] == "timeout"][0]
    assert to["detail"]["worker_still_running"] is True
    release.set()
    r.join_workers(5)
    k1 = acc.log_rows("pi", "S", "T1")
    assert "late_result_dropped" in [x["kind"] for x in k1]
    vec = [x for x in k1 if x["kind"] == "vector_called" and x["detail"].get("late")]
    assert client.calls and vec and vec[0]["detail"]["calls"] == len(client.calls)           # 超时不等于没花向量的钱：照样记账
    c = r.on_user_prompt("S", "T3", "新买的相机")                                             # 线程结束后又能正常取材
    assert c["kind"] != "busy"


def test_persistent_ban_receipt_is_sent_once_per_turn(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="tools_only"))
    mem.set_read_permission(False)
    a = r.on_user_prompt("S", "T1", "普通一句")
    assert a["kind"] == "receipt" and a["inject"].startswith(W1)
    b = r.compact_continue("S", "T1")                                                       # 同一轮里压缩续跑：不再刷第二句
    assert b["inject"] is None and b["kind"] == "receipt_already_sent"
    c = r.pre_answer("S", "T1", "普通一句")
    assert c["inject"] is None
    d = r.on_user_prompt("S", "T2", "又一轮")                                                # 新的一轮：照样送一次
    assert d["kind"] == "receipt" and d["inject"].startswith(W1)


def test_v6_db_upgrades_to_v7_and_keeps_its_connections(tmp_path, monkeypatch):
    import sqlite3
    import yuanwei.sqlstore as SQ
    p = str(tmp_path / "v6.sqlite")
    monkeypatch.setattr(SQ, "SCHEMA_VERSION", 6)
    mem = make_mem(tmp_path, name="v6.sqlite")
    import hashlib
    h = "ab" * 16
    with mem.store.tx() as c:                                                   # v6 的连接表没有 turn_id 一列：直接按 v6 的列写一行
        c.execute("INSERT INTO connections (handle_sha, ns, host, session_id, branch, created_at) VALUES (?,?,?,?,?,?)", (hashlib.sha256(h.encode()).hexdigest(), mem.ns, "pi", "S", "", "2027-01-01"))
    mem.close()
    assert SQ.Store(p).schema_version() == 6
    monkeypatch.undo()
    mem2 = make_mem(tmp_path, name="v6.sqlite")
    assert mem2.store.schema_version() == 12
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(connections)")}
    assert "turn_id" in cols and "turn_tokens" in {r[0] for r in sqlite3.connect(p).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ident = Access(mem2).claim_connection(h, "inst-x")                         # 旧连接仍在，没有轮次标记 → 会话级
    assert ident.session_id == "S" and ident.turn_id is None
    mem2.close()
