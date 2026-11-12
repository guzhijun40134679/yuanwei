"""后台补算向量：给库里还没有向量的锚点文字按批算向量（只连用户配置的本机服务）。

* 对象是全库（所有工作区）里还没有向量的**不同文字**：向量表的键是（文字 sha256，身份指纹），跨工作区共用。
* 一个进程持 <库>.vec.lock（flock，非阻塞）；抢不到就退出。最近入库的先算。
* 一个批次：读阶段（不开写事务）取文字、记「屏障签名」；服务调用（没有任何数据库事务开着）；写阶段一个很短的 BEGIN IMMEDIATE，先重核屏障签名，
  变了（这期间发生过删除）整批作废、一条都不写；没变才 INSERT（同键已有就保留先到的）。所以被删的话不会被算回来，也不会占着写事务等服务。
* 只算过滤以后的文字：发出去之前再过一遍 redact（旧库里没过滤过的、含密钥的锚点不发、不算、计数）。
* 退避：失败 1–5 次后分别等 10、30、120、600、1800 秒，之后一直 1800；成功清零；状态存在库里（store_meta），重启还在。
* 编码身份：只算配置里那个身份指纹下的向量；每次启动先用探针句和核验时存的探针向量比一下，对不上（同名换了模型）就暂停、不写，并标 drift。
* 这里不调模型（大模型）、不读密钥、不联网（只连配置里的向量服务）。
"""
import contextlib
import json
import math
import os
import time

from . import catalog as CT
from . import redact as RD
from . import vectors as VX
from .errors import ProviderError

DEFAULT_BACKOFF = (10, 30, 120, 600, 1800)
SKIP_CAP = 10000
PROBE_SENTENCES = ("今天去海边捡了很多贝壳。", "The weather is nice today.")
PROBE_COS = 0.999
PAGE = 500


def lock_path(db_path):
    return db_path + ".vec.lock"


@contextlib.contextmanager
def _lock(path):
    try:
        import fcntl
    except ImportError:                                       # 非 POSIX：退回独占创建锁文件
        fd = None
        try:
            fd = os.open(path + ".x", os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            yield False
            return
        try:
            yield True
        finally:
            os.close(fd)
            os.remove(path + ".x")
        return
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def lock_held(db_path):
    if not os.path.exists(lock_path(db_path)):              # 只读地问（status）：没有锁文件就是没人在补，不顺手建一个
        return False
    with _lock(lock_path(db_path)) as got:
        return not got


# ---------------------------------------------------------------- 签名、屏障
def signature(store, fp):
    """库的签名：锚点条数、最大 rowid、各工作区删除版本之和、身份指纹。和上次补完时记的一样，就没有欠账。"""
    c = store.conn
    n, mx = c.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM anchors").fetchone()
    dv = c.execute("SELECT COALESCE(SUM(del_ver),0) FROM workspaces").fetchone()[0]
    return "%d:%d:%d:%s" % (n, mx, dv, fp)


def barrier_sig(c):
    """屏障签名：所有工作区的删除版本，加上没做完的删除个数。读阶段取一次，写阶段（同一个事务里）再核。"""
    dv = tuple((r[0], r[1]) for r in c.execute("SELECT ns, del_ver FROM workspaces ORDER BY ns"))
    inc = c.execute("SELECT COUNT(*) FROM deletions WHERE state != 'complete'").fetchone()[0]
    return (dv, inc)


# ---------------------------------------------------------------- 退避状态
def _jget(store, k, default):
    try:
        v = store.meta_get(k)
        return json.loads(v) if v else default
    except (ValueError, TypeError):
        return default


def backoff_state(store):
    return _jget(store, "vec_backoff", {"failures": 0, "next_try_at": None, "last_error": None})


def in_backoff(store, now_ts):
    b = backoff_state(store)
    return bool(b.get("next_try_at")) and now_ts < b["next_try_at"], b


def _iso_plus(now_iso, seconds):
    import calendar
    t = calendar.timegm(time.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ")) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def note_failure(store, now_iso, err, schedule=DEFAULT_BACKOFF):
    b = backoff_state(store)
    n = int(b.get("failures") or 0) + 1
    wait = schedule[min(n, len(schedule)) - 1]
    store.meta_set("vec_backoff", json.dumps({"failures": n, "next_try_at": _iso_plus(now_iso, wait), "last_error": err, "wait_seconds": wait}))
    return n, wait


def clear_failure(store):
    if backoff_state(store).get("failures"):
        store.meta_set("vec_backoff", json.dumps({"failures": 0, "next_try_at": None, "last_error": None}))


def _skip_key(fp):
    return "vec_skip:" + fp[:10]


def skip_list(store, fp):
    return set(_jget(store, _skip_key(fp), []))


def add_skip(store, fp, shas):
    cur = _jget(store, _skip_key(fp), [])
    s = list(dict.fromkeys(cur + list(shas)))[-SKIP_CAP:]
    store.meta_set(_skip_key(fp), json.dumps(s))


# ---------------------------------------------------------------- 找欠账
def scan(store, fp, limit=None, skip=None):
    """扫锚点（最近入库的先）。返回 {"distinct","with_vector","pending"（最多 limit 条 (sha, 文字)，其余只数）,"pending_count","secret","unembeddable"}。"""
    skip = skip if skip is not None else skip_list(store, fp)
    c = store.conn
    seen, distinct, have_n, pend, pend_n, secret, unemb = set(), 0, 0, [], 0, 0, 0
    last = None
    while True:
        rows = c.execute("SELECT rowid, text FROM anchors" + (" WHERE rowid < ?" if last is not None else "") + " ORDER BY rowid DESC LIMIT %d" % PAGE,
                         (last,) if last is not None else ()).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        page = {}
        for _rid, text in rows:
            sha = VX.text_key(text)
            if sha not in seen:
                seen.add(sha)
                page[sha] = text
        have = set(store.get_vectors(fp, set(page)).keys()) if page else set()
        for sha, text in page.items():
            distinct += 1
            if sha in have:
                have_n += 1
            elif sha in skip:
                unemb += 1
            elif RD.redact(text).count:
                secret += 1
            else:
                pend_n += 1
                if limit is None or len(pend) < limit:
                    pend.append((sha, text))
    return {"distinct": distinct, "with_vector": have_n, "pending": pend, "pending_count": pend_n, "secret": secret, "unembeddable": unemb}


def next_batch(store, fp, n, skip):
    """最近入库的先：只取前 n 条欠账，不扫全库（凑够就停）。"""
    c = store.conn
    out, seen = [], set()
    last = None
    while len(out) < n:
        rows = c.execute("SELECT rowid, text FROM anchors" + (" WHERE rowid < ?" if last is not None else "") + " ORDER BY rowid DESC LIMIT %d" % PAGE,
                         (last,) if last is not None else ()).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        page = {}
        for _rid, text in rows:
            sha = VX.text_key(text)
            if sha not in seen:
                seen.add(sha)
                page[sha] = text
        have = set(store.get_vectors(fp, set(page)).keys()) if page else set()
        for sha, text in page.items():
            if sha in have or sha in skip or RD.redact(text).count:
                continue
            out.append((sha, text))
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------- 探针（发现「同名换了模型」）
def _cos(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if not na or not nb or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def probe_key(fp):
    return "vec_probe:" + fp[:10]


def store_probe(store, fp, vectors):
    store.meta_set(probe_key(fp), json.dumps([list(v) for v in vectors]))


def check_probe(store, fp, client, dimension):
    """返回 ("ok"｜"drift", 说明)。服务连不上会抛 ServiceDown（调用方退避）。没有存过探针就现存一份（第一次）。"""
    vecs = client.embed_batch(list(PROBE_SENTENCES))
    for v in vecs:
        VX.check_vector(v, dimension)
    old = _jget(store, probe_key(fp), None)
    if not old:
        store_probe(store, fp, vecs)
        return "ok", "probe_stored"
    if len(old) != len(vecs) or any(_cos(a, b) < PROBE_COS for a, b in zip(old, vecs)):
        return "drift", "probe_mismatch"
    return "ok", None


# ---------------------------------------------------------------- 主循环
class _Discard(Exception):
    pass


def _compute(client, texts, dimension):
    """算一批。整批被确定性拒绝（Rejected）就二分到单条；单条被拒的记进 rejected。返回 ({sha: vec}, [rejected sha])。网络类错误往外抛。"""
    got, rejected = {}, []

    def go(items):
        try:
            vecs = client.embed_batch([t for _s, t in items])
            for v in vecs:
                VX.check_vector(v, dimension)
        except VX.Rejected:
            if len(items) == 1:
                rejected.append(items[0][0])
                return
            mid = len(items) // 2
            go(items[:mid])
            go(items[mid:])
            return
        for (sha, _t), v in zip(items, vecs):
            got[sha] = v
    go(texts)
    return got, rejected


def run_backfill(store, db_path, emb, clock, client, sleep=time.sleep, max_seconds=600, fault=None, mono=time.monotonic):
    """补算一轮。emb＝配置里的 embedding 字典（要有 identity）；client＝有 embed_batch 的客户端。返回一个只有计数和原因类别的字典。"""
    out = {"status": "idle", "computed": 0, "discarded_by_delete": 0, "rejected": 0, "reason": None}
    ident = emb.get("identity")
    fp = VX.identity_fp(ident)
    dim = ident["dimension"]
    batch = int(emb.get("backfill_batch", VX.EMBED_BATCH))
    schedule = tuple(emb.get("backoff_seconds") or DEFAULT_BACKOFF)
    with _lock(lock_path(db_path)) as got:
        if not got:
            out.update(status="busy", reason="已有进程在补算")
            return out
        now = clock()
        waiting, b = in_backoff(store, now)
        if waiting:
            out.update(status="backoff", reason="到 %s 才重试" % b["next_try_at"])
            return out
        if barrier_sig(store.conn)[1]:                                  # 有没做完的删除：不碰服务，不开始任何批次（不去挡 VACUUM／checkpoint）
            out.update(status="deletion_in_progress", reason="有没做完的删除：不开始新批次")
            return out
        store.put_identity(fp, ident)
        t0 = mono()
        try:
            state, why = check_probe(store, fp, client, dim)
        except ProviderError as e:
            n, wait = note_failure(store, clock(), type(e).__name__ if not str(e) else str(e)[:80], schedule)
            _service(store, fp, "down", clock(), str(e)[:80])
            out.update(status="failed", reason=str(e)[:80], failures=n)
            return out
        if state == "drift":
            _service(store, fp, "drift", clock(), why)
            out.update(status="drift", reason="identity_drift")
            return out
        _service(store, fp, "ok", clock(), None)
        while True:
            if mono() - t0 > max_seconds:
                out.update(status="time_up")
                break
            sig0 = barrier_sig(store.conn)
            if sig0[1]:
                out.update(status="deletion_in_progress", reason="有没做完的删除：不开始新批次")
                break
            skip = skip_list(store, fp)
            texts = next_batch(store, fp, batch, skip)
            if not texts:
                store.meta_set("vec_done_sig", signature(store, fp))
                clear_failure(store)
                out.update(status="done" if out["computed"] else "idle")
                break
            if fault:
                fault("after_read")
            try:
                vecs, rejected = _compute(client, texts, dim)
            except ProviderError as e:
                n, wait = note_failure(store, clock(), str(e)[:80], schedule)
                _service(store, fp, "down", clock(), str(e)[:80])
                out.update(status="failed", reason=str(e)[:80], failures=n)
                break
            if fault:
                fault("after_embed")
            try:
                with store.tx() as c:
                    if barrier_sig(c) != sig0:
                        raise _Discard()
                    store.put_vectors_in(c, fp, vecs, keep_first=True)
            except _Discard:
                out["discarded_by_delete"] += 1
                _bump(store, "discarded_by_delete", 1)
                continue
            if rejected:
                add_skip(store, fp, rejected)
                out["rejected"] += len(rejected)
            out["computed"] += len(vecs)
            _bump(store, "computed", len(vecs))
            clear_failure(store)
            sleep(0.05)
        store.meta_set("vec_last_run", json.dumps({"at": clock(), "status": out["status"], "reason": out["reason"]}))
        return out


def _service(store, fp, state, now_iso, err):
    d = {"state": state, "fp": fp, "at": now_iso, "error": err}
    if state == "ok":
        d["last_ok_at"] = now_iso
    store.meta_set("vec_service", json.dumps(d))


def _bump(store, key, n):
    st = _jget(store, "vec_stats", {})
    st[key] = int(st.get(key, 0)) + n
    store.meta_set("vec_stats", json.dumps(st))


# ---------------------------------------------------------------- 状态
def status(store, db_path, cfg, scan_info=None):
    """向量段（给 status 用）：只读，不改任何东西。"""
    emb = cfg.embedding or {}
    ident = emb.get("identity")
    out = {"enabled": False}
    if cfg.retrieval != "hybrid" or not ident:
        return out
    fp = VX.identity_fp(ident)
    sc = scan_info or scan(store, fp, limit=0)
    svc = _jget(store, "vec_service", {})
    b = backoff_state(store)
    olds = []
    for r in store.conn.execute("SELECT fp, COUNT(*), COALESCE(SUM(LENGTH(vec)),0) FROM vectors GROUP BY fp"):
        if r[0] != fp:
            olds.append({"fp10": r[0][:10], "count": r[1], "bytes": r[2]})
    cur_bytes = store.conn.execute("SELECT COALESCE(SUM(LENGTH(vec)),0) FROM vectors WHERE fp=?", (fp,)).fetchone()[0]
    stats = _jget(store, "vec_stats", {})
    last = _jget(store, "vec_last_run", None)
    eng = None
    if emb.get("managed") and emb.get("engine_dir"):                         # 自带引擎：只读地看（不启动、不续命、不修复）
        from . import enginepkg as EP
        from . import enginerun as ER
        eng = ER.engine_status(EP.Paths(emb["engine_dir"]))
    return {"engine": eng, "enabled": True, "runtime": emb.get("runtime"), "model": emb.get("model"), "identity_fp10": fp[:10], "weak_identity": bool(emb.get("weak_identity")),
            "tested_comparison": CT.compare_to_tested(ident, bool(emb.get("weak_identity"))),
            "fallback": cfg.vector_fallback,
            "service": {"state": svc.get("state") if svc.get("fp") == fp else "unknown", "last_ok_at": svc.get("last_ok_at"), "last_error": svc.get("error")},
            "texts": {"distinct": sc["distinct"], "with_vector": sc["with_vector"], "pending": sc["pending_count"], "skipped_secret": sc["secret"], "unembeddable": sc["unembeddable"]},
            "backfill": {"running": lock_held(db_path), "last_run": last, "failures": b.get("failures", 0), "next_try_at": b.get("next_try_at"), "last_error": b.get("last_error"),
                         "computed_total": stats.get("computed", 0), "discarded_by_delete": stats.get("discarded_by_delete", 0)},
            "size": {"current_bytes": cur_bytes, "stale_bytes": sum(o["bytes"] for o in olds)},
            "stale_identities": olds}
