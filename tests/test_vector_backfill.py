"""后台补算向量：新原话补上向量；服务断了退避、恢复后接着补；不重复算；不算已删除的（屏障）；只算过滤后的文字；编码身份变了全部重算；子进程带 PYTHONPATH、输出进日志。"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from helpers import Clock, Scripted, say
from helpers2 import IDENT, TEXTS
from test_deletion_core import delete, svc_of
from yuanwei import Config, Memory
from yuanwei import embedwork as EW
from yuanwei import vectors as VX

EMB = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "identity": IDENT, "backoff_seconds": [10, 30, 120, 600, 1800]}


class Svc:
    """假向量服务（进程内）。down＝连不上；reject＝输入里含这些词就被确定性拒绝（4xx）；on_call＝每次收到请求时调一下（测交错、测写事务）。"""

    def __init__(self):
        self.calls, self.down, self.reject, self.on_call, self.drift = [], False, [], None, False

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        if self.on_call:
            self.on_call(texts)
        if self.down:
            raise VX.ServiceDown("向量服务出错：URLError")
        if any(w in t for t in texts for w in self.reject):
            raise VX.Rejected("向量服务出错：HTTP 400")
        return [self.vec(t) for t in texts]

    def vec(self, t):
        if self.drift:
            return [0.2, 0.1, float(len(t) % 3) + 1.0, 5.0]
        return [1.0, float(len(t) % 7) + 0.5, 0.25, 0.125]

    @property
    def texts(self):
        """算过的文字（不含每轮开头的两句探针）。"""
        return [t for c in self.calls for t in c if t not in EW.PROBE_SENTENCES]


def mkmem(tmp_path, svc=None, ws="w", name="b.sqlite", spawn="inline", clock=None, ident=IDENT, src_path=None, emb=None, **cfg):
    svc = svc or Svc()
    e = dict(emb or EMB, identity=ident)
    cfg_ = Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=e, **cfg)
    mem = Memory.open(str(tmp_path / name), ws, config=cfg_, provider=Scripted("skip"), clock=clock or Clock(), embed_client=svc, spawn=spawn, src_path=src_path)
    return mem, svc


def vec_texts(mem, ident=IDENT):
    fp = VX.identity_fp(ident)
    rows = mem.store.conn.execute("SELECT text FROM anchors").fetchall()
    have = mem.store.get_vectors(fp, {VX.text_key(r[0]) for r in rows})
    return {r[0] for r in rows if VX.text_key(r[0]) in have}


def all_texts(mem):
    return {r[0] for r in mem.store.conn.execute("SELECT text FROM anchors")}


def test_new_original_gets_a_vector_in_the_background(tmp_path):
    mem, svc = mkmem(tmp_path)
    r = mem.record("h", "S1", "m1", "user", "今天去了海边捡贝壳，很开心。")
    assert r["raw_saved"] and r.get("vec_spawn") == "ran"
    assert vec_texts(mem) == all_texts(mem) and all_texts(mem)
    fp = VX.identity_fp(IDENT)
    assert mem.store.identity(fp) is not None and mem.store.meta_get("vec_done_sig")


def test_record_does_not_wait_for_the_vector_service_even_when_it_hangs(tmp_path, monkeypatch):
    launched = []

    class P:
        pass
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append((cmd, kw)) or P())
    mem, svc = mkmem(tmp_path, spawn="subprocess", src_path="/some/src")
    r = mem.record("h", "S1", "m1", "user", "今天去了海边捡贝壳，很开心。")
    assert r["raw_saved"] and r.get("vec_spawn") == "spawned" and svc.calls == []                   # 记原话这一步一次都没碰向量服务
    cmd, kw = [x for x in launched if "vectors" in x[0]][0]
    assert cmd[1:4] == ["-m", "yuanwei", "vectors"] and "backfill" in cmd and "--db" in cmd
    assert kw["env"]["PYTHONPATH"].startswith("/some/src") and kw["stderr"] is not subprocess.DEVNULL


def test_spawn_failure_is_reported_in_receipt_and_never_breaks_record(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no python")
    monkeypatch.setattr(subprocess, "Popen", boom)
    mem, svc = mkmem(tmp_path, spawn="subprocess")
    r = mem.record("h", "S1", "m1", "user", "今天去了海边捡贝壳，很开心。")
    assert r["raw_saved"] and r.get("vec_spawn_error") == "OSError"
    assert mem.store.list_messages("w")


def test_worker_holds_no_write_transaction_while_waiting_for_the_service(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "m1", "user", "今天去了海边捡贝壳，很开心。")
    seen = []

    def during(texts):
        c2 = sqlite3.connect(mem.db_path, timeout=0.3)
        try:
            c2.execute("BEGIN IMMEDIATE")                         # 拿得到写锁，说明补算没占着写事务
            c2.execute("ROLLBACK")
            seen.append("ok")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            c2.close()
    svc.on_call = during
    out = mem.run_vectors()
    assert out["status"] == "done" and seen and set(seen) == {"ok"}


def test_service_down_backs_off_and_resumes_after_recovery_without_recomputing(tmp_path):
    clock = Clock()
    mem, svc = mkmem(tmp_path, spawn="off", clock=clock)
    fill_some(mem)
    svc.down = True
    r = mem.run_vectors()
    assert r["status"] == "failed" and EW.backoff_state(mem.store)["failures"] == 1 and EW.backoff_state(mem.store)["next_try_at"] == "2027-01-01T08:00:10Z"
    n = len(svc.calls)
    assert mem.run_vectors()["status"] == "backoff" and len(svc.calls) == n                          # 退避没到：不发请求
    clock.advance(seconds=11)
    svc.down = False
    out = mem.run_vectors()
    assert out["status"] == "done" and vec_texts(mem) == all_texts(mem) and EW.backoff_state(mem.store)["failures"] == 0
    done = len(svc.texts)
    clock.advance(seconds=1)
    assert mem.run_vectors()["status"] == "idle" and len(svc.texts) == done                          # 再跑一轮：已经有的不重算


def fill_some(mem, n=6):
    for i, t in enumerate(TEXTS[:n], 1):
        mem.record("h", "S1", "m%d" % i, "user" if i % 2 else "assistant", t, time="2027-02-01")


def test_backoff_schedule_10_30_120_600_1800_and_success_resets(tmp_path):
    clock = Clock()
    mem, svc = mkmem(tmp_path, spawn="off", clock=clock)
    fill_some(mem)
    svc.down = True
    waits = []
    for _ in range(6):
        mem.run_vectors()
        b = EW.backoff_state(mem.store)
        waits.append(b["wait_seconds"])
        clock.advance(seconds=b["wait_seconds"] + 1)
    assert waits == [10, 30, 120, 600, 1800, 1800]
    svc.down = False
    mem.run_vectors()
    assert EW.backoff_state(mem.store)["failures"] == 0
    svc.down = True
    clock.advance(hours=1)
    # 重启：新进程（新的 Memory 对象）里退避状态还在
    fill_more = Memory.open(mem.db_path, "w", config=mem.cfg, provider=Scripted("skip"), clock=clock, embed_client=svc, spawn="off")
    fill_more.record("h", "S1", "x1", "user", "新来的一句话，用来触发欠账。")
    fill_more.run_vectors()
    assert EW.backoff_state(fill_more.store)["failures"] == 1
    mem2 = Memory.open(mem.db_path, "w", config=mem.cfg, provider=Scripted("skip"), clock=clock, embed_client=svc, spawn="off")
    n = len(svc.calls)
    assert mem2.run_vectors()["status"] == "backoff" and len(svc.calls) == n


def test_no_duplicate_computation_for_texts_that_already_have_vectors_or_repeat_across_workspaces(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 4)
    mem.run_vectors()
    first = list(svc.texts)
    mem_b, _s = mkmem(tmp_path, svc=svc, ws="w2", spawn="off")                    # 同一个库的另一个工作区，同样的话
    for i, t in enumerate(TEXTS[:4], 1):
        mem_b.record("h", "S9", "z%d" % i, "user" if i % 2 else "assistant", t, time="2027-02-01")
    n = len(svc.texts)
    assert mem_b.run_vectors()["status"] == "idle" and len(svc.texts) == n                           # 同一句话在两个工作区里，向量只算一次
    assert len(first) == len(set(first))


def test_deleted_text_is_never_computed_after_deletion(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
    mem.record("h", "S2", "b1", "user", "绿色保留乙：这一句留着。")
    mem.end_session("h", "S1")
    mem.end_session("h", "S2")
    delete(mem, "S1")
    mem.run_vectors()
    assert not [t for t in svc.texts if "蓝色标记甲" in t] and any("绿色保留乙" in t for t in svc.texts)


def test_deletion_between_read_phase_and_write_phase_discards_the_whole_batch(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
    mem.record("h", "S2", "b1", "user", "绿色保留乙：这一句留着。")
    mem.end_session("h", "S1")
    mem.end_session("h", "S2")
    done = {"n": 0}

    def during(texts):
        if not done["n"] and texts[0] not in EW.PROBE_SENTENCES:
            done["n"] = 1
            delete(mem, "S1")                                   # 读阶段之后、写阶段之前：这一段对话被删
    svc.on_call = during
    out = mem.run_vectors()
    assert out["discarded_by_delete"] >= 1 and EW._jget(mem.store, "vec_stats", {})["discarded_by_delete"] >= 1
    fp = VX.identity_fp(IDENT)
    assert VX.text_key("蓝色标记甲：这一句要被删掉。") not in mem.store.get_vectors(fp, {VX.text_key("蓝色标记甲：这一句要被删掉。")})
    assert not [r for r in mem.store.conn.execute("SELECT text FROM anchors") if "蓝色标记甲" in r[0]]
    assert "绿色保留乙：这一句留着。" in vec_texts(mem)


def test_interleaving_worker_commit_before_deletion_then_gc_removes_the_vector(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
    mem.record("h", "S2", "b1", "user", "绿色保留乙：这一句留着。")
    mem.end_session("h", "S1")
    mem.end_session("h", "S2")
    mem.run_vectors()
    fp = VX.identity_fp(IDENT)
    key = VX.text_key("蓝色标记甲：这一句要被删掉。")
    assert key in mem.store.get_vectors(fp, {key})
    delete(mem, "S1")
    assert key not in mem.store.get_vectors(fp, {key}) and VX.text_key("绿色保留乙：这一句留着。") in mem.store.get_vectors(fp, {VX.text_key("绿色保留乙：这一句留着。")})


def test_worker_does_not_start_a_batch_while_a_deletion_is_incomplete(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
    mem.record("h", "S2", "b1", "user", "绿色保留乙：这一句留着。")
    mem.end_session("h", "S1")

    def crash(point):
        if point == "after_commit":
            raise SystemExit("进程没了")
    with pytest.raises(SystemExit):
        delete(mem, "S1", svc={"fault": crash})
    n = len(svc.calls)
    out = mem.run_vectors()
    assert out["status"] == "deletion_in_progress" and len(svc.calls) == n


def test_only_redacted_text_is_sent_and_unredacted_legacy_anchor_is_skipped_and_counted(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    secret = "sk-" + "0123456789abcdef" * 2
    mem.record("h", "S1", "a1", "user", "正常的一句话，没有密钥。")
    # 旧库：新规则之前入库的、没有过滤过的锚点（直接塞进库里）
    with mem.store.tx() as c:
        c.execute("INSERT INTO anchors (ns,anchor_id,msg_id,ord,seq,text,start,end,index_version) VALUES ('w','legacy#1','legacy',999999999,999,?,0,10,2)", ("我的密钥是 %s 别告诉别人" % secret,))
    out = mem.run_vectors()
    assert not [t for t in svc.texts if secret in t] and secret not in json.dumps(svc.calls)
    assert EW.status(mem.store, mem.db_path, mem.cfg)["texts"]["skipped_secret"] == 1 and out["status"] == "done"


def test_unembeddable_text_is_isolated_by_bisecting_and_listed_not_retried_forever(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    svc.reject = ["超长的坏句"]
    fill_some(mem, 5)
    mem.record("h", "S1", "bad", "user", "这是一句超长的坏句。")
    out = mem.run_vectors()
    assert out["status"] == "done" and out["rejected"] == 1 and vec_texts(mem) == all_texts(mem) - {"这是一句超长的坏句。"}
    n = len(svc.calls)
    mem.record("h", "S1", "ok2", "user", "又一句正常的话，用来触发欠账。")
    mem.run_vectors()
    assert not [t for c in svc.calls[n:] for t in c if "超长的坏句" in t]                     # 不再重试
    sc = EW.status(mem.store, mem.db_path, mem.cfg)["texts"]
    assert sc["unembeddable"] == 1 and sc["pending"] == 0


def test_skip_list_is_pruned_by_deletion_for_texts_no_longer_in_any_anchor(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    svc.reject = ["坏句"]
    mem.record("h", "S1", "bad", "user", "这是一句坏句。")
    mem.record("h", "S2", "ok", "user", "这是一句好话。")
    mem.end_session("h", "S1")
    mem.run_vectors()
    fp = VX.identity_fp(IDENT)
    assert EW.skip_list(mem.store, fp) == {VX.text_key("这是一句坏句。")}
    delete(mem, "S1")
    assert EW.skip_list(mem.store, fp) == set()


def test_same_key_different_float_values_keep_the_first_and_do_not_raise(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fp = VX.identity_fp(IDENT)
    mem.store.put_identity(fp, IDENT)
    k = VX.text_key("同一句话")
    mem.store.put_vectors(fp, {k: [1.0, 2.0, 3.0, 4.0]})
    mem.store.put_vectors(fp, {k: [1.0, 2.0, 3.0, 4.0000001]}, keep_first=True)
    assert mem.store.get_vectors(fp, {k})[k] == [1.0, 2.0, 3.0, 4.0]
    from yuanwei.storeerr import StoreError
    with pytest.raises(StoreError):
        mem.store.put_vectors(fp, {k: [9.0, 9.0, 9.0, 9.0]})


def test_lock_prevents_two_workers_and_second_exits_immediately(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 3)
    with EW._lock(EW.lock_path(mem.db_path)) as got:
        assert got and EW.lock_held(mem.db_path) is True
        out = mem.run_vectors()
        assert out["status"] == "busy" and svc.calls == []
        assert mem.kick_vectors()["reason"] == "已有进程在补算"
    assert mem.run_vectors()["status"] == "done"


def test_cheap_signature_skips_spawn_when_nothing_changed(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    assert mem.kick_vectors()["reason"] == "spawn 关着" or mem.kick_vectors()["reason"] in ("没有欠账", "spawn 关着")
    fill_some(mem, 3)
    mem.run_vectors()
    assert mem.kick_vectors()["reason"] == "没有欠账"
    fp = VX.identity_fp(IDENT)
    before = EW.signature(mem.store, fp)
    mem.record("h", "S1", "new1", "user", "新增一句话让签名变。")
    assert EW.signature(mem.store, fp) != before and mem.kick_vectors()["reason"] != "没有欠账"
    mem.run_vectors()
    assert mem.kick_vectors()["reason"] == "没有欠账"
    delete(mem, "S1")
    assert mem.kick_vectors()["reason"] != "没有欠账"


def test_status_reports_pending_last_error_next_try_and_stale_identities(tmp_path):
    clock = Clock()
    mem, svc = mkmem(tmp_path, spawn="off", clock=clock)
    fill_some(mem, 4)
    svc.down = True
    mem.run_vectors()
    v = mem.status()["vectors"]
    assert v["texts"]["pending"] > 0 and v["backfill"]["failures"] == 1 and v["backfill"]["next_try_at"] and "URLError" in v["backfill"]["last_error"] and v["service"]["state"] == "down"
    svc.down = False
    clock.advance(seconds=11)
    mem.run_vectors()
    v = mem.status()["vectors"]
    assert v["texts"]["pending"] == 0 and v["texts"]["with_vector"] == v["texts"]["distinct"] and v["size"]["current_bytes"] > 0 and v["service"]["state"] == "ok"


def test_backfill_newest_first(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off", emb=dict(EMB, backfill_batch=2))
    fill_some(mem, 6)
    mem.run_vectors()
    order = [t for t in svc.texts if t not in EW.PROBE_SENTENCES]
    last_msgs = [r[0] for r in mem.store.conn.execute("SELECT text FROM anchors ORDER BY rowid DESC LIMIT 2")]
    assert set(order[:2]) == set(last_msgs)


# ---------------------------------------------------------------- 编码身份
def ident2(**kw):
    return dict(IDENT, **kw)


def test_changing_model_quantization_or_runtime_changes_fp_and_old_vectors_are_not_used(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 4)
    mem.run_vectors()
    old_fp = VX.identity_fp(IDENT)
    for change in ({"model": "m2"}, {"model_file_sha256": "y" * 64}, {"runtime_version": "r2"}):
        assert VX.identity_fp(ident2(**change)) != old_fp
    new = ident2(runtime_version="r2")
    mem2, _s = mkmem(tmp_path, svc=svc, spawn="off", ident=new)
    assert vec_texts(mem2, new) == set() and vec_texts(mem2, IDENT) == all_texts(mem2)       # 旧身份的还在表里，但新身份下一条都没有


def test_identity_change_recomputes_everything_under_the_new_fp_and_status_explains(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 4)
    mem.run_vectors()
    new = ident2(runtime_version="r2")
    mem2, _s = mkmem(tmp_path, svc=svc, spawn="off", ident=new)
    n = len(svc.texts)
    mem2.run_vectors()
    assert vec_texts(mem2, new) == all_texts(mem2) and len(svc.texts) > n
    v = mem2.status()["vectors"]
    assert v["stale_identities"] and v["stale_identities"][0]["count"] > 0 and v["texts"]["pending"] == 0


def test_during_recompute_reads_are_partial_hybrid_never_mixing_old_vectors(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 6)
    mem.run_vectors()
    new = ident2(runtime_version="r2")
    mem2, _s = mkmem(tmp_path, svc=svc, spawn="off", ident=new, emb=dict(EMB, backfill_batch=2))
    mem2.recall("烤鱼")
    assert mem2._reader.retrieval_log[-1]["with_vector"] == 0 and mem2._reader.retrieval_log[-1]["used"] == "keyword_fallback"
    # 补一小批：只补到几条，读取是部分向量
    fp2 = VX.identity_fp(new)
    mem2.store.put_identity(fp2, new)
    some = sorted(all_texts(mem2))[:2]
    mem2.store.put_vectors(fp2, {VX.text_key(t): svc.vec(t) for t in some})
    mem2.recall("烤鱼")
    i = mem2._reader.retrieval_log[-1]
    assert i["used"] == "hybrid_partial" and i["with_vector"] == len(some)


def test_service_drift_detected_by_worker_probe_pauses_backfill_and_degrades_queries(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 4)
    mem.run_vectors()
    mem.record("h", "S1", "late", "user", "漂移之后又来的一句话。")
    svc.drift = True                                                       # 同名换了模型：同维度、向量完全不一样
    n_vec = len(vec_texts(mem))
    out = mem.run_vectors()
    assert out["status"] == "drift" and len(vec_texts(mem)) == n_vec
    svc_state = json.loads(mem.store.meta_get("vec_service"))
    assert svc_state["state"] == "drift"
    mem.recall("烤鱼")
    assert mem._reader.retrieval_log[-1]["reason"] == "identity_drift" and mem.status()["vectors"]["service"]["state"] == "drift"
    svc.drift = False                                                      # 服务恢复：下一轮探针通过，自己恢复
    assert mem.run_vectors()["status"] in ("done", "idle") and json.loads(mem.store.meta_get("vec_service"))["state"] == "ok"


def test_prune_old_identity_vectors_goes_through_deletion_module_and_static_guard_allows_only_it(tmp_path):
    from yuanwei.deletion import DeletionService
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 4)
    mem.run_vectors()
    new = ident2(runtime_version="r2")
    mem2, _s = mkmem(tmp_path, svc=svc, spawn="off", ident=new)
    mem2.run_vectors()
    old_fp, new_fp = VX.identity_fp(IDENT), VX.identity_fp(new)
    n_old = mem2.store.vector_count(old_fp)
    assert n_old > 0
    removed = DeletionService(mem2.db_path, store=mem2.store).prune_vectors(new_fp)
    assert removed == n_old and mem2.store.vector_count(old_fp) == 0 and mem2.store.vector_count(new_fp) > 0 and mem2.store.identity(old_fp) is None
    import test_delete_static as S
    assert S.scan(S.SRC) == []


def test_vectors_prune_command_needs_yes(tmp_path, capsys):
    from yuanwei import cli
    from helpers_wizard import FAKE_KEY, FakeHTTP, FakeSystem, make_home
    mem, svc = mkmem(tmp_path, spawn="off")
    fill_some(mem, 3)
    mem.run_vectors()
    new = ident2(runtime_version="r2")
    toml = tmp_path / "c.toml"
    toml.write_text('retrieval = "hybrid"\nvector_fallback = "partial"\n[embedding]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "m"\nidentity = %s\n' % __import__("yuanwei.wizard", fromlist=["x"])._tv(new))
    assert cli._vectors_main(["prune", "--db", mem.db_path, "--config", str(toml)], sys_=FakeSystem()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["stale_identities"][0]["count"] > 0
    assert mem.store.vector_count(VX.identity_fp(IDENT)) > 0
    assert cli._vectors_main(["prune", "--yes", "--db", mem.db_path, "--config", str(toml)], sys_=FakeSystem()) == 0
    assert json.loads(capsys.readouterr().out)["removed"] > 0 and mem.store.vector_count(VX.identity_fp(IDENT)) == 0


# ---------------------------------------------------------------- 后台子进程：PYTHONPATH、输出不丢
def test_spawn_passes_pythonpath_from_src_path_and_child_can_import_yuanwei(tmp_path):
    """真起一个子进程（不联网）：装包的 Python 里没有 yuanwei，靠 PYTHONPATH 找到；它的 stderr 写进 <库>.spawn.log，status 里看得到。"""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    mem, svc = mkmem(tmp_path, spawn="subprocess", src_path=os.path.abspath(src))
    mem.record("h", "S1", "m1", "user", "今天去了海边。", time="2027-01-01")
    mem.end_session("h", "S1")
    p = mem._spawn(["--db", mem.db_path, "--workspace", "w", "run"], "run")                   # run 需要密钥：没设 → 报错写进日志（不会发任何请求）
    p.wait(timeout=60)
    log = open(mem._spawn_log_path(), encoding="utf-8").read()
    assert "No module named" not in log and ("DEEPSEEK_API_KEY" in log or "error" in log.lower() or p.returncode in (0, 2, 3, 4))
    # 子进程的 sys.path 里只有 PYTHONPATH 给的目录（-I 会忽略 PYTHONPATH，这里用 -E 以外的普通模式、清掉当前目录）：能 import 到 yuanwei，而且就是 src_path 那份
    env = mem._spawn_env()
    r = subprocess.run([sys.executable, "-c", "import yuanwei, os; print(os.path.dirname(os.path.dirname(yuanwei.__file__)))"], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0 and os.path.samefile(r.stdout.strip(), os.path.abspath(src))


def test_spawn_log_tail_shows_child_errors_and_is_redacted_and_trimmed(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    secret = "sk-" + "0123456789abcdef" * 2
    with open(mem._spawn_log_path(), "w", encoding="utf-8") as f:
        f.write("--- t run\nTraceback...\nValueError: 出错了 %s\n%s\n" % (secret, "x" * 1000))
    tail = mem.status()["spawn_log_tail"]
    assert tail and secret not in "\n".join(tail) and all(len(l) <= 300 for l in tail) and any("出错了" in l for l in tail)


def test_child_process_command_for_organizing_also_gets_pythonpath_and_log(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append((cmd, kw)))
    mem = Memory.open(str(tmp_path / "o.sqlite"), "w", config=Config(), provider=Scripted("skip"), clock=Clock(), spawn="subprocess", src_path="/pkg/src")
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    r = mem.check_and_spawn()
    assert r["action"] == "spawned"
    cmd, kw = launched[0]
    assert cmd[-1] == "run" and kw["env"]["PYTHONPATH"].startswith("/pkg/src") and kw["stderr"] is not subprocess.DEVNULL
    assert os.path.exists(mem._spawn_log_path())


def test_status_scan_time_on_10000_anchors_is_fast_enough(tmp_path):
    """主会话裁定 17：status 扫锚点这次不缓存，量一下 1 万条要多久（实测约 0.2 秒；这里只守「没有数量级的退步」，上限放宽到 10 秒）。"""
    import time
    mem, svc = mkmem(tmp_path, spawn="off")
    with mem.store.tx() as c:
        for i in range(10000):
            c.execute("INSERT INTO anchors (ns,anchor_id,msg_id,ord,seq,text,start,end,index_version) VALUES ('w',?,?,?,?,?,0,10,2)", ("a#%d" % i, "m%d" % i, i, i, "这是第%d句比较长的测试用的话，内容各不相同。" % i))
    t = time.time()
    v = mem.status()["vectors"]
    assert v["texts"]["distinct"] == 10000 and v["texts"]["pending"] == 10000 and time.time() - t < 10


def test_deletion_clears_the_spawn_log(tmp_path):
    mem, svc = mkmem(tmp_path, spawn="off")
    mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
    mem.end_session("h", "S1")
    with open(mem._spawn_log_path(), "w", encoding="utf-8") as f:
        f.write("--- t run\nValueError: 蓝色标记甲 出现在这里\n")
    assert delete(mem, "S1").status == "complete"
    assert os.path.getsize(mem._spawn_log_path()) == 0
