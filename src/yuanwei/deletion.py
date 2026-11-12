"""删一段对话（删除与密钥过滤）。**这是唯一允许删原话、整理结果的模块**（tests/test_delete_static.py 守着）：普通写入、整理只追加；删除是受授权的「历史撤销」，是「只增不删」约定的例外。

流程（设计文档第 3 节）：
  阶段 1（一个 SQLite 事务）：撤销＋删行＋占位＋游标作废＋向量孤儿清理＋删除版本／整理序号／编号高水位＋写屏障＋消费「待确认」＋登记任务。出错整笔回滚，什么都没变。
  阶段 2 文件：重写待补、领取、待核、临时文件，去掉范围内的行（无法归属的行清掉并计数）。
  阶段 3 VACUUM；阶段 4 wal_checkpoint(TRUNCATE)（busy 就不算完成，WAL 要是 0 字节）；阶段 5 核实（受管文件里没有范围内的行；有金丝雀就在库、WAL、文件里扫一遍）→ complete。
「删除完成」只有 complete；其余都是「清理中」。每个阶段幂等，重启接着做。锁的顺序 L1 删除锁 → L3 补写者锁 → L2 追加锁 → L4 SQLite 写锁；阶段 1 只用 L4。
这里不调模型、不读密钥、不联网。
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import contract as WC
from . import vectors as VX
from .errors import NotFound, YuanweiError
from .reader import purge_all
from .sqlstore import Store, now_iso

TOMB = {
    "occ_title": "〔标题：来源对话已删除〕",
    "occ_summary": "这条摘要的来源被删了",
    "thread_name": "〔主线名称：来源对话已删除〕",
    "thread_desc": "〔主线说明：来源对话已删除〕",
    "reason": "〔理由已清除：来源对话已删除〕",
}
JUDGMENT_VOID = {"status": "void_by_deletion"}
STATES = ("registered", "files_cleaned", "compacted", "checkpointed", "complete")

DELETE_DONE = "已删掉这段对话在原委里记下的 %d 条原话和相关整理%s。这一轮助手已经看到的内容，原委删不掉；Hermes 自己的会话记录也不归原委管。"
DELETE_CLEANING = "已从原委里撤销这段对话的 %d 条原话和相关整理，正在清理残留（状态码 %s），完成前请不要当成已经删干净。"
DELETE_FAILED = "没能完成删除（状态码 %s），原委里的记录没有被改动。"
FORK_SENTENCE = "从这个窗口分叉出去的对话里，分叉之前那段属于这个窗口，也会一起删掉。"
DELETE_PROMPT = ("要删掉这段对话在原委里记下的全部内容吗？删了找不回来。涉及 %d 个工作区、约 %d 条原话（这个窗口压缩前后的记录都算，截到你这次回复确认为止）；"
                 "分叉出去的另一个窗口里属于它自己的记录不动，" + FORK_SENTENCE + "确认后你再说的话照常记。"
                 "Hermes 自己的会话记录、你另外导出的文件、已经发给模型的内容，原委删不掉。回复「确认删除」即可。")
PHASE1_BUDGET_SECONDS = 5.0        # 第 1 阶段（同步，确认那一轮里跑）的预期上限：超过就在结果里报 slow_phase1，不影响正确性

_NUM_RE = re.compile(r"-([0-9]+)$")


class ConvInfo(NamedTuple):
    conv_id: str
    kind: str
    host: str
    first_at: Optional[str]
    last_at: Optional[str]
    messages: int
    by_ns: Dict[str, int]
    sessions: int
    head: Optional[str]


class DeletePlan(NamedTuple):
    conv_id: str
    scope: Tuple[Tuple[str, str, str], ...]
    counts: Dict[str, int]
    impact: Dict[str, int]
    pending_lines: int
    warnings: Tuple[str, ...]


class DeleteResult(NamedTuple):
    del_id: Optional[str]
    status: str                      # complete｜cleaning｜failed｜refused｜noop
    code: Optional[str]
    counts: Dict[str, int]
    floor_capture: Optional[int]
    verified: Optional[str]
    text: str


class DeleteRefused(YuanweiError):
    code = "delete_refused"


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _num(ident):
    m = _NUM_RE.search(ident or "")
    return int(m.group(1)) if m else 0


def pending_path(config_dir, ns):
    return os.path.join(config_dir, "pending-%s.jsonl" % WC.sha256_hex(ns)[:10])


def legacy_conv_id(host, session_id, branch):
    return "t-" + _sha("\x1f".join((host, session_id, branch or "")))[:12]


@contextlib.contextmanager
def _flock(path, blocking, timeout=None):
    """进程间文件锁；yield True＝拿到。blocking 且给了 timeout：轮询到超时。非 POSIX：不加锁。"""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    got = False
    try:
        end = time.time() + (timeout or 0)
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if (blocking and timeout is None) else fcntl.LOCK_NB))
                got = True
                break
            except OSError:
                if not blocking or timeout is None or time.time() >= end:
                    break
                time.sleep(0.05)
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


class DeletionService:
    def __init__(self, db_path, *, config_dir=None, clock=None, fault=None, disk_free=None, canary=True, store=None, sleep=None, busy_s=3.0):
        self.db_path = os.path.abspath(db_path)
        self.config_dir = config_dir or (self.db_path + ".d")
        self.clock = clock or now_iso
        self.fault = fault
        self.disk_free = disk_free
        self.canary = canary
        self._store = store
        self._own_store = False
        self._canaries = {}
        self._sleep = sleep or time.sleep
        self.busy_s = busy_s

    # ---------------------------------------------------------------- 连接
    @property
    def store(self):
        if self._store is None:
            self._store = Store(self.db_path)
            self._own_store = True
        return self._store

    def close(self):
        if self._own_store and self._store is not None:
            self._store.close()
            self._store, self._own_store = None, False

    def _fire(self, point):
        if self.fault is not None:
            self.fault(point)

    def _ro(self):
        c = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=3.0)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------------- 对话：列出、解析
    @staticmethod
    def _tables(c):
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def list_conversations(self) -> List[ConvInfo]:
        c = self._ro()
        try:
            tbls = self._tables(c)
            out, bound = [], set()
            if "window_bindings" in tbls:
                for w in c.execute("SELECT window_id, host FROM windows ORDER BY created_at, window_id").fetchall():
                    trip = [(r["host"], r["session_id"], r["branch"]) for r in c.execute("SELECT host, session_id, branch FROM window_bindings WHERE window_id=?", (w["window_id"],))]
                    bound.update(trip)
                    info = self._info(c, w["window_id"], "window", w["host"], trip)
                    if info.messages:
                        out.append(info)
            seen = {}
            for r in c.execute("SELECT DISTINCT host, session_id, branch FROM messages"):
                t = (r["host"], r["session_id"], r["branch"])
                if t not in bound:
                    seen[t] = None
            for t in sorted(seen):
                out.append(self._info(c, legacy_conv_id(*t), "legacy", t[0], [t]))
            return out
        finally:
            c.close()

    @staticmethod
    def _info(c, conv_id, kind, host, trip):
        by_ns, first, last, n = {}, None, None, 0
        head_row = None
        for h, s, b in trip:
            for r in c.execute("SELECT ns, COUNT(*) n, MIN(COALESCE(msg_time, received_at)) a, MAX(COALESCE(msg_time, received_at)) z FROM messages WHERE host=? AND session_id=? AND branch=? GROUP BY ns", (h, s, b)):
                by_ns[r["ns"]] = by_ns.get(r["ns"], 0) + r["n"]
                n += r["n"]
                first = r["a"] if first is None or (r["a"] and r["a"] < first) else first
                last = r["z"] if last is None or (r["z"] and r["z"] > last) else last
            r2 = c.execute("SELECT content, received_at, seq FROM messages WHERE host=? AND session_id=? AND branch=? AND role='user' ORDER BY received_at, seq LIMIT 1", (h, s, b)).fetchone()
            if r2 is not None and (head_row is None or (r2["received_at"], r2["seq"]) < (head_row["received_at"], head_row["seq"])):
                head_row = r2
        head = head_row["content"].strip().replace("\n", " ")[:15] if head_row is not None else None
        return ConvInfo(conv_id, kind, host, first, last, n, by_ns, len(trip), head)

    def _resolve(self, c, conv_id):
        """返回 (kind, window_id, [(host, session, branch)…])。没有就抛 NotFound。"""
        tbls = self._tables(c)
        if conv_id.startswith("w-") and "window_bindings" in tbls:
            trip = [(r["host"], r["session_id"], r["branch"]) for r in c.execute("SELECT host, session_id, branch FROM window_bindings WHERE window_id=? ORDER BY host, session_id, branch", (conv_id,))]
            if not trip:
                raise NotFound("没有这个对话：%s" % conv_id)
            return "window", conv_id, trip
        if conv_id.startswith("t-"):
            bound = set()
            if "window_bindings" in tbls:
                bound = {(r[0], r[1], r[2]) for r in c.execute("SELECT host, session_id, branch FROM window_bindings")}
            for r in c.execute("SELECT DISTINCT host, session_id, branch FROM messages"):
                t = (r["host"], r["session_id"], r["branch"])
                if t not in bound and legacy_conv_id(*t) == conv_id:
                    return "legacy", None, [t]
        raise NotFound("没有这个对话：%s" % conv_id)

    def counts_for_window(self, window_id):
        c = self._ro()
        try:
            _k, _w, trip = self._resolve(c, window_id)
            ns, n = set(), 0
            for h, s, b in trip:
                for r in c.execute("SELECT ns, COUNT(*) n FROM messages WHERE host=? AND session_id=? AND branch=? GROUP BY ns", (h, s, b)):
                    ns.add(r["ns"])
                    n += r["n"]
            return {"workspaces": len(ns), "sessions": len(trip), "messages": n}
        except NotFound:
            return {"workspaces": 0, "sessions": 0, "messages": 0}
        finally:
            c.close()

    # ---------------------------------------------------------------- 计划（纯读）：删什么、清什么
    def preview(self, conv_id) -> DeletePlan:
        c = self._ro()
        try:
            kind, wid, trip = self._resolve(c, conv_id)
            plan = self._plan(c, trip, None, None)
            pend = self._count_pending_lines(c, trip, plan["affected_ns"])
            warn = []
            if plan["counts"]["messages"] == 0:
                warn.append("这个对话里没有原话可删")
            return DeletePlan(conv_id, tuple(trip), plan["counts"], plan["impact"], pend, tuple(warn))
        finally:
            c.close()

    def _plan(self, c, trip, floor, del_id):
        """读出要清的东西。纯读，不写。返回 dict：每个工作区的删除/占位清单（只有编号）、计数、影响。"""
        tripset = set(trip)
        has_cap = "capture_seq" in {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        msgs = []
        for h, s, b in trip:
            q = "SELECT ns, msg_id, host, session_id, branch, host_msg_id, content FROM messages WHERE host=? AND session_id=? AND branch=?"
            args = [h, s, b]
            if floor is not None and has_cap:
                q += " AND (capture_seq IS NULL OR capture_seq <= ?)"
                args.append(floor)
            msgs += [dict(r) for r in c.execute(q, args)]
        by_ns = {}
        for m in msgs:
            by_ns.setdefault(m["ns"], []).append(m)
        counts = {"messages": len(msgs), "workspaces": len(by_ns), "steps": 0, "anchors": 0, "judgments_deleted": 0, "org_rows_deleted": 0, "cursors_voided": 0,
                  "vectors_removed": 0, "turn_messages": 0}
        impact = {"steps_deleted": 0, "steps_tainted": 0, "occasions_tombstoned": 0, "threads_named_cleared": 0, "attachments_reason_cleared": 0, "judgments_scrubbed": 0,
                  "judgments_deleted_for_rerun": 0}
        per_ns = {}
        for ns, ms in by_ns.items():
            d = self._plan_ns(c, ns, tripset, {m["msg_id"] for m in ms}, counts, impact)
            per_ns[ns] = d
        return {"msgs": msgs, "by_ns": by_ns, "per_ns": per_ns, "counts": counts, "impact": impact, "affected_ns": sorted(by_ns)}

    def _plan_ns(self, c, ns, tripset, dmsg, counts, impact):
        steps = [dict(r) for r in c.execute("SELECT step_id, k, host, session_id, branch, status FROM steps WHERE ns=?", (ns,))]
        dsteps = {s["step_id"] for s in steps if (s["host"], s["session_id"], s["branch"]) in tripset}
        keptsteps = {s["step_id"]: s for s in steps if s["step_id"] not in dsteps}
        rows = []
        for r in c.execute("SELECT id, tbl, data FROM org_rows WHERE ns=? ORDER BY id", (ns,)):
            rows.append((r["id"], r["tbl"], json.loads(r["data"])))
        by_tbl = {}
        for rid, t, d in rows:
            by_tbl.setdefault(t, []).append((rid, d))
        g = lambda t: by_tbl.get(t, [])
        sent_del = {d["sentence_id"] for _i, d in g("sentences") if d["msg_id"] in dmsg}
        att_keep = [(i, d) for i, d in g("attachments") if d["sentence_id"] not in sent_del]
        gaps_keep = []
        gap_del = []
        for i, d in g("gaps"):
            if d["created_by"] in dsteps or all(x in sent_del for x in d["sentence_ids"]):
                gap_del.append(i)
            else:
                gaps_keep.append((i, d))
        refs = set()
        for _i, a in att_keep:
            if a["target_id"]:
                refs.add(a["target_id"])
            refs.update(a["candidates"])
        for _i, gp in gaps_keep:
            if gp["known_thread"]:
                refs.add(gp["known_thread"])
            refs.update(gp["found"])
        occ_th = {d["occasion_id"]: d["thread_id"] for _i, d in g("occasions")}
        kept_occ = {o for o in occ_th if o in refs}
        kept_th = {d["thread_id"] for _i, d in g("threads") if d["thread_id"] in refs} | {occ_th[o] for o in kept_occ}
        all_th = {d["thread_id"] for _i, d in g("threads")}
        del_occ = set(occ_th) - kept_occ
        del_th = all_th - kept_th
        # ---- 污染闭包
        att_del_targets = set()
        for _i, a in g("attachments"):
            if a["sentence_id"] in sent_del and a["target_id"]:
                att_del_targets.add(a["target_id"])
        authored = {}                                  # 对象编号 -> 写过它的名称/说明/标题/摘要的步集合
        for t, key, fld in (("thread_names", "thread_id", "by"), ("thread_descs", "thread_id", "by"), ("occ_titles", "occasion_id", "by"), ("occ_summaries", "occasion_id", "by")):
            for _i, d in g(t):
                authored.setdefault(d[key], set()).add(d[fld])
        kept_objs = kept_occ | kept_th
        seeds = {o for o in kept_objs if o in att_del_targets}
        saw = {}
        all_objs = set(occ_th) | all_th
        gone = del_occ | del_th                       # 被删掉的对象：它们的卡片里有被删的原话和摘要，看过它们的步一样受污染
        for sid in keptsteps:
            saw[sid] = self._saw(c, ns, sid, all_objs)
        bad = set(dsteps)
        tainted = set(seeds)
        changed = True
        while changed:
            changed = False
            for o in kept_objs:
                if o not in tainted and authored.get(o, set()) & bad:
                    tainted.add(o)
                    changed = True
            for sid in keptsteps:
                if sid not in bad and saw[sid] & (tainted | gone):
                    bad.add(sid)
                    changed = True
        tainted_steps = bad - dsteps
        # ---- 清单
        d = {"ns": ns, "dsteps": sorted(dsteps), "dmsg": sorted(dmsg), "tainted_steps": sorted(tainted_steps), "rows_delete": [], "rows_update": [], "step_ks": sorted(s["k"] for s in steps if s["step_id"] in dsteps)}
        dsent_rows = set()

        def delete(i):
            d["rows_delete"].append(i)

        for i, x in g("sentences"):
            if x["sentence_id"] in sent_del:
                delete(i)
        for i, a in g("attachments"):
            if a["sentence_id"] in sent_del:
                delete(i)
            elif a["created_by"] in tainted_steps and a["reason"] and a["reason"] != TOMB["reason"]:
                a2 = dict(a, reason=TOMB["reason"])
                d["rows_update"].append((i, a2))
                impact["attachments_reason_cleared"] += 1
        for i in gap_del:
            delete(i)
        for i, gp in gaps_keep:
            gp2 = dict(gp)
            gp2["sentence_ids"] = [x for x in gp["sentence_ids"] if x not in sent_del]
            gp2["found"] = [x for x in gp["found"] if x not in del_occ and x not in del_th]
            if gp["created_by"] in tainted_steps:
                gp2["words"] = []
            if gp2 != gp:
                d["rows_update"].append((i, gp2))
        n_occ_tomb, n_th_clear = set(), set()
        for t, key in (("threads", "thread_id"), ("thread_names", "thread_id"), ("thread_descs", "thread_id"), ("occasions", "occasion_id"), ("occ_titles", "occasion_id"), ("occ_summaries", "occasion_id")):
            for i, x in g(t):
                oid = x[key]
                if (t.startswith("thread") and oid in del_th) or (t.startswith("occ") and oid in del_occ):
                    delete(i)
                    continue
                if t == "occ_titles" and oid in tainted and x["title"] != TOMB["occ_title"]:
                    d["rows_update"].append((i, dict(x, title=TOMB["occ_title"])))
                    n_occ_tomb.add(oid)
                elif t == "occ_summaries" and oid in tainted:
                    x2 = dict(x, text=TOMB["occ_summary"], reason=TOMB["reason"] if x["reason"] else "", request_fp=None)
                    x2["evidence_sentences"] = [e for e in x["evidence_sentences"] if e not in sent_del]
                    if x2 != x:
                        d["rows_update"].append((i, x2))
                        n_occ_tomb.add(oid)
                elif t == "occ_summaries":
                    ev = [e for e in x["evidence_sentences"] if e not in sent_del]
                    if ev != x["evidence_sentences"]:
                        d["rows_update"].append((i, dict(x, evidence_sentences=ev)))
                elif t in ("thread_names", "thread_descs") and x["by"] in bad:
                    f, tomb = ("name", TOMB["thread_name"]) if t == "thread_names" else ("text", TOMB["thread_desc"])
                    if x[f] != tomb:
                        d["rows_update"].append((i, dict(x, **{f: tomb})))
                        n_th_clear.add(oid)
        for i, rc in g("receipts"):
            if rc.get("step_id") in dsteps:
                delete(i)
                continue
            r2 = dict(rc)
            if "touched_threads" in rc:
                r2["touched_threads"] = [x for x in rc["touched_threads"] if x not in del_th]
            if rc.get("step_id") in tainted_steps:
                if rc.get("reason"):
                    r2["reason"] = TOMB["reason"]
                for f in ("input_fp", "op_fp"):
                    if rc.get(f):
                        r2[f] = ""
            if r2 != rc:
                d["rows_update"].append((i, r2))
        impact["occasions_tombstoned"] += len(n_occ_tomb)
        impact["threads_named_cleared"] += len(n_th_clear)
        impact["steps_tainted"] += len(tainted_steps)
        impact["steps_deleted"] += len(dsteps)
        counts["steps"] += len(dsteps)
        counts["org_rows_deleted"] += len(d["rows_delete"])
        counts["anchors"] += c.execute("SELECT COUNT(*) FROM anchors WHERE ns=? AND msg_id IN (%s)" % ",".join("?" * len(dmsg)), [ns] + sorted(dmsg)).fetchone()[0] if dmsg else 0
        # ---- 判断记录
        d["j_delete_steps"] = sorted(dsteps)
        d["j_scrub"], d["j_delete_rerun"] = [], []
        for sid in sorted(tainted_steps):
            if keptsteps[sid]["status"] in ("done", "abandoned", "skipped"):
                d["j_scrub"].append(sid)
            else:
                d["j_delete_rerun"].append(sid)
        for sid in dsteps:
            counts["judgments_deleted"] += c.execute("SELECT COUNT(*) FROM judgments WHERE ns=? AND step_id=?", (ns, sid)).fetchone()[0]
        for sid in d["j_scrub"]:
            impact["judgments_scrubbed"] += c.execute("SELECT COUNT(*) FROM judgments WHERE ns=? AND step_id=?", (ns, sid)).fetchone()[0]
        for sid in d["j_delete_rerun"]:
            impact["judgments_deleted_for_rerun"] += c.execute("SELECT COUNT(*) FROM judgments WHERE ns=? AND step_id=?", (ns, sid)).fetchone()[0]
        d["hwm"] = {"thread": max([_num(x["thread_id"]) for _i, x in g("threads")] or [0]), "occasion": max([_num(x["occasion_id"]) for _i, x in g("occasions")] or [0]),
                    "gap": max([_num(x["gap_id"]) for _i, x in g("gaps")] or [0]), "step": max([s["k"] for s in steps] or [0])}
        counts["turn_messages"] += c.execute("SELECT COUNT(*) FROM turn_messages WHERE ns=? AND msg_id IN (%s)" % ",".join("?" * len(dmsg)), [ns] + sorted(dmsg)).fetchone()[0] if dmsg else 0
        counts["cursors_voided"] += c.execute("SELECT COUNT(*) FROM cursors WHERE ns=?", (ns,)).fetchone()[0]
        return d

    @staticmethod
    def _saw(c, ns, sid, kept_objs):
        """这一步的判断记录（含失败归档）里请求展示过的对象编号。认不出形状就按「看过全部」算（宁可多清）。"""
        out = set()
        for r in c.execute("SELECT data FROM judgments WHERE ns=? AND step_id=?", (ns, sid)):
            try:
                rec = json.loads(r["data"])
                req = rec.get("request")
                if rec.get("status") == "void_by_deletion":
                    continue
                if not isinstance(req, dict) or "shown_threads" not in req:
                    return set(kept_objs)
                out.update(req.get("shown_threads") or [])
                out.update((req.get("shown_occasions") or {}).keys())
                out.update(req.get("found_threads") or [])
                out.update(req.get("found_occasions") or [])
                out.update((req.get("found_occasion_map") or {}).keys())
            except Exception:                                                    # noqa: BLE001
                return set(kept_objs)
        return out

    def _count_pending_lines(self, c, trip, namespaces):
        n = 0
        tripset = set(trip)
        for ns in namespaces:
            base = os.path.basename(pending_path(self.config_dir, ns))
            try:
                names = [x for x in os.listdir(self.config_dir) if x.startswith(base) and not x.endswith(".lock")]
            except FileNotFoundError:
                continue
            for name in names:
                try:
                    with open(os.path.join(self.config_dir, name), "rb") as f:
                        for line in f.read().split(b"\n"):
                            if line.strip() and self._classify(line, name.endswith(".review"), tripset, None) != "keep":
                                n += 1
                except OSError:
                    continue
        return n

    # ---------------------------------------------------------------- 待补文件里一行的归属
    @staticmethod
    def _inner(line, review):
        """待核记录：取出原行（line 或 line_b64 解码）；其余直接是待补行。返回 dict 或 None（认不出）。"""
        try:
            obj = json.loads(line.decode("utf-8"))
            if not isinstance(obj, dict):
                return None
            if review:
                if "line" in obj and isinstance(obj["line"], str):
                    obj = json.loads(obj["line"])
                elif "line_b64" in obj:
                    import base64
                    obj = json.loads(base64.b64decode(obj["line_b64"]).decode("utf-8"))
                else:
                    return None
                if not isinstance(obj, dict):
                    return None
            return obj
        except Exception:                                                        # noqa: BLE001
            return None

    def _classify(self, line, review, tripset, floor, reg_at=None):
        """keep｜drop（范围内）｜unattributable（认不出：也清掉并计数）。floor 是 None 就是预览：范围内不看捕获序号。"""
        a = self._inner(line, review)
        if a is None or not all(k in a for k in ("host", "session_id")):
            return "unattributable"
        t = (a["host"], a["session_id"], a.get("branch") or "")
        if t not in tripset:
            return "keep"
        cap = a.get("capture")
        if floor is None:
            return "drop"
        if cap is None:                                                          # 没有捕获序号：按收到的时间和删除登记的时间比（比登记晚的才保留）
            return "keep" if (reg_at is not None and a.get("received_at") and a["received_at"] > reg_at) else "drop"
        return "drop" if cap <= floor else "keep"

    # ---------------------------------------------------------------- 删除：阶段 1
    def delete_conversation(self, conv_id, *, source="command", operator="cli", pending_id=None, input_win_seq=None, confirm_turn=None, capture=None, finish=True) -> DeleteResult:
        st = self.store
        t0 = time.time()
        self._fire("before_tx")
        c = st.conn
        try:
            kind, wid, trip = self._resolve(c, conv_id)
        except NotFound:
            return DeleteResult(None, "refused", "E_NOT_FOUND", {}, None, None, DELETE_FAILED % "E_NOT_FOUND")
        del_id = "d-" + uuid.uuid4().hex[:12]
        try:
            with st.tx() as cx:
                if pending_id is not None:
                    p = cx.execute("SELECT * FROM delete_pending WHERE pending_id=?", (pending_id,)).fetchone()
                    if p is None or p["state"] != "waiting" or p["db_id"] != st.db_id() or (wid is not None and p["window_id"] != wid):
                        raise DeleteRefused("待确认已经不在了")
                floor = Store.take_capture_in(cx)
                plan = self._plan(cx, trip, floor, del_id)
                if self.canary:
                    self._canaries[del_id] = self._pick_canaries(plan["msgs"])
                counts = self._apply(cx, plan, trip, floor, del_id, kind, wid, conv_id, source, pending_id, input_win_seq, confirm_turn, capture)
                self._fire("in_tx")
        except DeleteRefused as e:
            return DeleteResult(None, "refused", "E_PENDING_GONE", {}, None, None, DELETE_FAILED % "E_PENDING_GONE")
        except Exception as e:                                                   # noqa: BLE001  整笔回滚：什么都没变
            self._canaries.pop(del_id, None)
            return DeleteResult(None, "failed", "E_TX_" + type(e).__name__, {}, None, None, DELETE_FAILED % ("E_TX_" + type(e).__name__))
        if del_id in self._canaries:                                             # 同一句话在别的对话里合法留着的，不能当金丝雀（不然会误报残留）
            keep = []
            for cn in self._canaries[del_id]:
                t = cn.decode("utf-8")
                if not st.conn.execute("SELECT 1 FROM messages WHERE instr(content, ?) > 0 LIMIT 1", (t,)).fetchone():
                    keep.append(cn)
            self._canaries[del_id] = keep
        self._fire("after_commit")
        purge_all(self.db_path)
        if time.time() - t0 > PHASE1_BUDGET_SECONDS:
            counts["slow_phase1"] = 1
        if finish:
            return self.finish(del_id)
        return self._result(del_id)

    @staticmethod
    def _pick_canaries(msgs):
        out, seen = [], set()
        for m in msgs:
            text = m["content"]
            for i in range(0, max(1, len(text) - 12), 12):
                snip = text[i:i + 12]
                if len(snip.strip()) >= 8 and snip not in seen:
                    seen.add(snip)
                    out.append(snip.encode("utf-8"))
                    break
            if len(out) >= 64:
                break
        return out

    def _apply(self, cx, plan, trip, floor, del_id, kind, wid, conv_id, source, pending_id, input_win_seq, confirm_turn, capture):
        now = self.clock()
        counts = dict(plan["counts"])
        impact = plan["impact"]
        db_id = cx.execute("SELECT v FROM store_meta WHERE k='db_id'").fetchone()[0]
        # 屏障
        for h, s, b in trip:
            cx.execute("INSERT OR IGNORE INTO del_scope (del_id, host, session_id, branch) VALUES (?,?,?,?)", (del_id, h, s, b))
        for m in plan["msgs"]:
            cx.execute("INSERT OR IGNORE INTO del_keys (key_sha, del_id) VALUES (?,?)", (Store.key_sha(m["host"], m["session_id"], m["branch"], m["host_msg_id"]), del_id))
        confirm_tok = None
        if confirm_turn is not None:
            confirm_tok = confirm_turn[1]
            cx.execute("INSERT OR IGNORE INTO no_record_turns (turn_id, host, window_id, reason, del_id, capture_seq) VALUES (?,?,?,?,?,?)",
                       (confirm_tok, confirm_turn[0], wid, "delete_confirm", del_id, capture))
        cx.execute("INSERT INTO deletions (del_id, window_id, conv_kind, conv_sha, source, state, code, floor_capture, requested_at, registered_at, counts, plan, pending_id, input_win_seq, db_id) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (del_id, wid, kind, _sha(conv_id), source, "registered", None, floor, None, now, "{}", _dumps({"affected_ns": plan["affected_ns"]}), pending_id, input_win_seq, db_id))
        if pending_id is not None:
            cx.execute("UPDATE delete_pending SET state='confirmed', resolved_at=?, resolved_win_seq=?, del_id=? WHERE pending_id=? AND state='waiting'", (now, input_win_seq, del_id, pending_id))
        if wid is not None:
            cx.execute("UPDATE window_pending SET state='void', void_reason='deleted', resolved_at=? WHERE window_id=? AND state='waiting'", (now, wid))
            cx.execute("UPDATE delete_pending SET state='void', void_reason='superseded', resolved_at=? WHERE window_id=? AND state='waiting'", (now, wid))
            cx.execute("UPDATE window_permits SET params_fp='' WHERE window_id=? AND state NOT IN ('issued','claimed')", (wid,))
        # 每个受影响工作区
        all_ids = [m["msg_id"] for m in plan["msgs"]]
        for ns, d in plan["per_ns"].items():
            dmsg = d["dmsg"]
            for chunk in _chunks(dmsg, 400):
                ph = ",".join("?" * len(chunk))
                cx.execute("DELETE FROM anchors WHERE ns=? AND msg_id IN (%s)" % ph, [ns] + chunk)
                cx.execute("DELETE FROM turn_messages WHERE ns=? AND msg_id IN (%s)" % ph, [ns] + chunk)
                cx.execute("DELETE FROM messages WHERE ns=? AND msg_id IN (%s)" % ph, [ns] + chunk)
            for i in d["rows_delete"]:
                cx.execute("DELETE FROM org_rows WHERE id=?", (i,))
            for i, data in d["rows_update"]:
                cx.execute("UPDATE org_rows SET data=? WHERE id=?", (_dumps(data), i))
            for sid in d["j_delete_steps"] + d["j_delete_rerun"]:
                cx.execute("DELETE FROM judgments WHERE ns=? AND step_id=?", (ns, sid))
            for sid in d["j_scrub"]:
                for r in cx.execute("SELECT question, attempt, data FROM judgments WHERE ns=? AND step_id=?", (ns, sid)).fetchall():
                    old = json.loads(r["data"])
                    keep = {k: old.get(k) for k in ("step_id", "attempt", "question", "base_seq", "k", "session_id") if k in old}
                    cx.execute("UPDATE judgments SET data=? WHERE ns=? AND step_id=? AND question=? AND attempt=?", (_dumps(dict(keep, **JUDGMENT_VOID)), ns, sid, r["question"], r["attempt"]))
                cx.execute("UPDATE steps SET last_error=NULL WHERE ns=? AND step_id=?", (ns, sid))
            for sid in d["dsteps"]:
                cx.execute("DELETE FROM bindings WHERE ns=? AND step_id=?", (ns, sid))
                cx.execute("DELETE FROM steps WHERE ns=? AND step_id=?", (ns, sid))
            for k in d["step_ks"]:
                cx.execute("INSERT OR IGNORE INTO deleted_steps (ns, k, del_id) VALUES (?,?,?)", (ns, k, del_id))
            cx.execute("UPDATE cursors SET data=? WHERE ns=?", (_dumps({"void": "history_deleted"}), ns))
            self._scrub_logs(cx, ns, set(dmsg), trip, del_id, confirm_tok)
            h = d["hwm"]
            cx.execute("UPDATE workspaces SET del_ver=del_ver+1, org_seq=org_seq+1, step_hwm=MAX(step_hwm, ?), thread_hwm=MAX(thread_hwm, ?), occ_hwm=MAX(occ_hwm, ?), gap_hwm=MAX(gap_hwm, ?) WHERE ns=?",
                       (h["step"], h["thread"], h["occasion"], h["gap"], ns))
        counts["vectors_removed"] = self._gc_vectors(cx)
        # 窗口输入、轮次令牌里的哈希（被删窗口）：换成不可逆编号（确认那一轮自己的行保留键，只清 fp）
        for h_, s_, b_ in trip:
            for r in cx.execute("SELECT ns, turn_id, seq, input_key FROM inputs WHERE host=? AND session_id=? AND branch=?", (h_, s_, b_)).fetchall():
                if r["turn_id"] == confirm_tok:
                    cx.execute("UPDATE inputs SET fp='' WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND seq=?", (r["ns"], h_, s_, b_, r["turn_id"], r["seq"]))
                else:
                    cx.execute("UPDATE inputs SET fp='', input_key=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND seq=?",
                               ("x" + _sha(del_id + r["input_key"])[:14], r["ns"], h_, s_, b_, r["turn_id"], r["seq"]))
            for r in cx.execute("SELECT window_id, win_seq, turn_id, input_key FROM window_inputs WHERE host=? AND session_id=? AND branch=?", (h_, s_, b_)).fetchall():
                if r["turn_id"] == confirm_tok:
                    cx.execute("UPDATE window_inputs SET fp='' WHERE window_id=? AND win_seq=?", (r["window_id"], r["win_seq"]))
                else:
                    cx.execute("UPDATE window_inputs SET fp='', input_key=? WHERE window_id=? AND win_seq=?", ("x" + _sha(del_id + r["input_key"])[:14], r["window_id"], r["win_seq"]))
            for r in cx.execute("SELECT ns, turn_id, event_key FROM turn_tokens WHERE host=? AND session_id=? AND branch=?", (h_, s_, b_)).fetchall():
                if r["turn_id"] != confirm_tok:
                    cx.execute("UPDATE turn_tokens SET event_key=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND event_key=?",
                               ("x" + _sha(del_id + r["event_key"])[:14], r["ns"], h_, s_, b_, r["event_key"]))
        out_counts = dict(counts, **{"impact_" + k: v for k, v in impact.items()})
        cx.execute("UPDATE deletions SET counts=? WHERE del_id=?", (_dumps(out_counts), del_id))
        return out_counts

    @staticmethod
    def _scrub_logs(cx, ns, dmsg, trip, del_id, confirm_tok):
        for r in cx.execute("SELECT id, detail FROM read_log WHERE ns=?", (ns,)).fetchall():
            try:
                d = json.loads(r["detail"])
            except Exception:                                                    # noqa: BLE001
                cx.execute("UPDATE read_log SET detail='{}' WHERE id=?", (r["id"],))
                continue
            if not isinstance(d, dict):
                continue
            spans = d.get("spans")
            if isinstance(spans, list) and any(isinstance(s, dict) and s.get("msg_id") in dmsg for s in spans):
                d["spans"], d["fingerprint"] = [], None
                cx.execute("UPDATE read_log SET detail=? WHERE id=?", (_dumps(d), r["id"]))

    @staticmethod
    def _gc_vectors(cx):
        """全库已经没有任何锚点还用到的文字哈希，向量整行删（含查询的向量，那是缓存，下次要用会重新算）。"""
        have = {VX.text_key(r[0]) for r in cx.execute("SELECT text FROM anchors")}
        n = 0
        for r in cx.execute("SELECT fp, text_sha FROM vectors").fetchall():
            if r["text_sha"] not in have:
                cx.execute("DELETE FROM vectors WHERE fp=? AND text_sha=?", (r["fp"], r["text_sha"]))
                n += 1
        for k, v in cx.execute("SELECT k, v FROM store_meta WHERE k LIKE 'vec_skip:%'").fetchall():        # 补算的「算不了」名单（只有文字的哈希）：对应文字已经没有锚点用了，一起清掉
            try:
                lst = json.loads(v)
            except ValueError:
                lst = []
            keep = [x for x in lst if x in have]
            if len(keep) != len(lst):
                cx.execute("UPDATE store_meta SET v=? WHERE k=?", (json.dumps(keep), k))
        return n

    def prune_vectors(self, keep_fp):
        """清掉「不是 keep_fp 这个编码身份」的全部向量（旧身份留下的，已经不用了）、对应的身份记录、探针和「算不了」名单。返回被删的向量条数。"""
        with self.store.tx() as cx:
            n = cx.execute("SELECT COUNT(*) FROM vectors WHERE fp != ?", (keep_fp,)).fetchone()[0]
            cx.execute("DELETE FROM vectors WHERE fp != ?", (keep_fp,))
            cx.execute("DELETE FROM vector_identities WHERE fp != ?", (keep_fp,))
            for k in [r[0] for r in cx.execute("SELECT k FROM store_meta WHERE k LIKE 'vec_skip:%' OR k LIKE 'vec_probe:%'").fetchall()]:
                if k.split(":", 1)[1] != keep_fp[:10]:
                    cx.execute("DELETE FROM store_meta WHERE k=?", (k,))
        return n

    # ---------------------------------------------------------------- 阶段 2–5
    def _row(self, del_id):
        r = self.store.conn.execute("SELECT * FROM deletions WHERE del_id=?", (del_id,)).fetchone()
        return dict(r) if r else None

    def _set(self, del_id, **cols):
        keys = list(cols)
        with self.store.tx() as c:
            c.execute("UPDATE deletions SET %s WHERE del_id=?" % ",".join("%s=?" % k for k in keys), [cols[k] for k in keys] + [del_id])

    def _result(self, del_id):
        r = self._row(del_id)
        if r is None:
            return DeleteResult(None, "refused", "E_NOT_FOUND", {}, None, None, DELETE_FAILED % "E_NOT_FOUND")
        counts = json.loads(r["counts"] or "{}")
        n = counts.get("messages", 0)
        extra = counts.get("impact_occasions_tombstoned", 0) + counts.get("impact_threads_named_cleared", 0) + counts.get("impact_steps_tainted", 0)
        if r["state"] == "complete":
            verified = counts.get("verified")
            return DeleteResult(del_id, "complete", None, counts, r["floor_capture"], verified, DELETE_DONE % (n, ("（另有 %d 处别的整理文字因为可能受它影响，已清成占位）" % extra) if extra else ""))
        code = r["code"] or "E_PENDING"
        return DeleteResult(del_id, "cleaning", code, counts, r["floor_capture"], None, DELETE_CLEANING % (n, code))

    def finish(self, del_id) -> DeleteResult:
        """阶段 2–5，幂等。拿不到删除锁：状态码 E_LOCK_BUSY，不改状态。"""
        os.makedirs(self.config_dir, exist_ok=True)
        with _flock(os.path.join(self.config_dir, "delete.lock"), True, timeout=10.0) as got:
            if not got:
                return self._result_with_code(del_id, "E_LOCK_BUSY")
            return self._finish_locked(del_id)

    def _result_with_code(self, del_id, code):
        r = self._result(del_id)
        return r._replace(code=code, text=DELETE_CLEANING % (r.counts.get("messages", 0), code)) if r.status == "cleaning" else r

    def _finish_locked(self, del_id):
        r = self._row(del_id)
        if r is None or r["state"] == "complete":
            return self._result(del_id)
        for phase in (self._phase_files, self._phase_vacuum, self._phase_checkpoint, self._phase_verify):
            r = self._row(del_id)
            if r["state"] == "complete":
                break
            code = phase(r)
            if code:
                self._set(del_id, code=code)
                return self._result(del_id)
        return self._result(del_id)

    def _scope_of(self, r):
        c = self.store.conn
        trip = {(x["host"], x["session_id"], x["branch"]) for x in c.execute("SELECT host, session_id, branch FROM del_scope WHERE del_id=?", (r["del_id"],))}
        ns = json.loads(r["plan"] or "{}").get("affected_ns", []) if r["plan"] else []
        return trip, ns

    def _phase_files(self, r):
        if STATES.index(r["state"]) >= 1:
            return None
        trip, nss = self._scope_of(r)
        dropped = {"scope": 0, "unattributable": 0}
        for ns in sorted(nss):                                                   # 按工作区名排序拿锁
            base = pending_path(self.config_dir, ns)
            with self._flock_retry(base + ".flush.lock") as got3:                # L3 补写者锁（不阻塞，试 5 次）
                if not got3:
                    return "E_FLUSHER_BUSY"
                with _flock(base + ".lock", True) as got2:                       # L2 追加锁
                    try:
                        for name in self._managed_files(ns):
                            self._rewrite(os.path.join(self.config_dir, name), name.endswith(".review"), trip, r["floor_capture"], dropped, r["registered_at"])
                    except OSError:
                        return "E_FILE_IO"
        try:                                                                      # 后台子进程的输出日志（只有异常类别和固定文案，也不留）
            lp = self.db_path + ".spawn.log"
            if os.path.exists(lp):
                open(lp, "w").close()
        except OSError:
            return "E_FILE_IO"
        counts = json.loads(r["counts"] or "{}")
        counts["dropped_pending_lines"] = counts.get("dropped_pending_lines", 0) + dropped["scope"]
        counts["dropped_unattributable"] = counts.get("dropped_unattributable", 0) + dropped["unattributable"]
        self._set(r["del_id"], state="files_cleaned", code=None, counts=_dumps(counts))
        self._fire("after_files")
        return None

    @contextlib.contextmanager
    def _flock_retry(self, path, tries=5):
        for i in range(tries):
            with _flock(path, False) as g:
                if g:
                    yield True
                    return
            self._sleep(0.2)
        yield False

    def _managed_files(self, ns):
        base = os.path.basename(pending_path(self.config_dir, ns))
        try:
            return sorted(x for x in os.listdir(self.config_dir) if x.startswith(base) and not x.endswith(".lock"))
        except FileNotFoundError:
            return []

    def _rewrite(self, path, review, trip, floor, dropped, reg_at=None):
        try:
            with open(path, "rb") as f:
                lines = [l for l in f.read().split(b"\n") if l.strip()]
        except FileNotFoundError:
            return
        keep = []
        for l in lines:
            v = self._classify(l, review, trip, floor, reg_at)
            if v == "keep":
                keep.append(l)
            else:
                dropped["scope" if v == "drop" else "unattributable"] += 1
        if len(keep) == len(lines):
            return
        if not keep:
            os.remove(path)
            self._fsync_dir(os.path.dirname(path))
            return
        tmp = path + ".dtmp"
        with open(tmp, "wb") as f:
            for l in keep:
                f.write(l + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self._fsync_dir(os.path.dirname(path))

    @staticmethod
    def _fsync_dir(d):
        try:
            fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _wal_size(self):
        try:
            return os.path.getsize(self.db_path + "-wal")
        except OSError:
            return 0

    def _vac_conn(self):
        c = sqlite3.connect(self.db_path, timeout=self.busy_s, isolation_level=None)
        c.execute("PRAGMA secure_delete=ON")
        return c

    def _phase_vacuum(self, r):
        if STATES.index(r["state"]) >= 2:
            return None
        need = 2 * (os.path.getsize(self.db_path) + self._wal_size()) + 64 * 1024 * 1024
        free = self.disk_free() if self.disk_free is not None else shutil.disk_usage(os.path.dirname(self.db_path) or ".").free
        if free < need:
            return "E_NO_SPACE"
        last = None
        for i in range(5):
            c = self._vac_conn()
            try:
                c.execute("VACUUM")
                last = None
                break
            except sqlite3.OperationalError as e:
                last = "E_VACUUM_BUSY" if ("locked" in str(e).lower() or "busy" in str(e).lower()) else "E_VACUUM"
                if last == "E_VACUUM":
                    break
                self._sleep(0.2 * (2 ** i))
            finally:
                c.close()
        if last:
            return last
        self._set(r["del_id"], state="compacted", code=None)
        return None

    def _phase_checkpoint(self, r):
        if STATES.index(r["state"]) >= 3:
            return None
        busy = 1
        for i in range(5):
            c = self._vac_conn()
            try:
                row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                busy = row[0]
            except sqlite3.OperationalError:
                busy = 1
            finally:
                c.close()
            if busy == 0 and self._wal_size() == 0:
                break
            busy = 1
            self._sleep(0.2 * (2 ** i))
        if busy:
            return "E_CHECKPOINT_BUSY"
        self._set(r["del_id"], state="checkpointed", code=None)
        return None

    def _phase_verify(self, r):
        trip, nss = self._scope_of(r)
        left = 0
        for ns in nss:
            for name in self._managed_files(ns):
                try:
                    with open(os.path.join(self.config_dir, name), "rb") as f:
                        for l in f.read().split(b"\n"):
                            if l.strip() and self._classify(l, name.endswith(".review"), trip, r["floor_capture"], r["registered_at"]) != "keep":
                                left += 1
                except OSError:
                    return "E_FILE_IO"
        verified = "structure"
        canaries = self._canaries.get(r["del_id"])
        if canaries:
            hits = 0
            paths = [self.db_path, self.db_path + "-wal"] + [os.path.join(self.config_dir, n) for ns in nss for n in self._managed_files(ns)]
            for p in paths:
                try:
                    with open(p, "rb") as f:
                        blob = f.read()
                except OSError:
                    continue
                hits += sum(1 for cn in canaries if cn in blob)
            if hits:
                counts = json.loads(r["counts"] or "{}")
                counts["canary_hits"] = hits
                self._set(r["del_id"], counts=_dumps(counts))
                return "E_VERIFY"
            verified = "structure+canary"
        if left:
            return "E_VERIFY"
        counts = json.loads(r["counts"] or "{}")
        counts["verified"] = verified
        self._set(r["del_id"], state="complete", code=None, completed_at=self.clock(), plan=None, counts=_dumps(counts))
        self._canaries.pop(r["del_id"], None)
        return None

    # ---------------------------------------------------------------- 恢复、状态
    def incomplete(self, ns=None) -> List[str]:
        try:
            rows = self.store.conn.execute("SELECT del_id, plan FROM deletions WHERE state != 'complete' ORDER BY registered_at, del_id").fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            if ns is None or ns in (json.loads(r["plan"]).get("affected_ns", []) if r["plan"] else []):
                out.append(r["del_id"])
        return out

    def resume(self) -> List[DeleteResult]:
        return [self.finish(d) for d in self.incomplete()]

    def resume_for(self, ns) -> Optional[DeleteResult]:
        """打开 Memory 时用：试拿删除锁（不阻塞），拿到就做完这个工作区里没完成的；拿不到说明别的进程在做，返回 None。"""
        os.makedirs(self.config_dir, exist_ok=True)
        last = None
        with _flock(os.path.join(self.config_dir, "delete.lock"), False) as got:
            if not got:
                return None
            for d in self.incomplete(ns):
                last = self._finish_locked(d)
        return last

    def status(self, del_id=None) -> List[dict]:
        sql = "SELECT del_id, window_id, conv_kind, source, state, code, floor_capture, registered_at, completed_at, counts FROM deletions"
        args = ()
        if del_id:
            sql += " WHERE del_id=?"
            args = (del_id,)
        try:
            return [dict(r, counts=json.loads(r["counts"] or "{}")) for r in self.store.conn.execute(sql + " ORDER BY registered_at, del_id", args)]
        except sqlite3.OperationalError:
            return []


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield list(seq[i:i + n])
