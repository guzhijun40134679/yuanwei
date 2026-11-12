"""写入变轻·隔离的轻写存储（只追加）。

组织状态 = {"ns", "seq", "tables": {表名: [记录...]}}。每条记录有 key 和 created_seq（哪一次提交创建的）。
变更集 = {"ns", "expected_prev_seq", "new_seq", "inserts": {表名: [记录...]}, "updates": []}；一步一份，整份校验，任何一项不过整份不收。
这个存储没有更新：改摘要是在 occ_summaries 里追加新版本，旧版本留着；as_of 只靠 created_seq 还原任意时刻。

表：threads、thread_names、thread_descs（主线：名称、说明的版本）；occasions、occ_titles、occ_summaries（回：父线、标题、摘要的版本）；
sentences（句子：消息编号、消息内起止码点、说话人、日期）；attachments（挂接）；gaps（缺口与补找记录）；receipts（收据）。
挂接种类（attachments.kind）：
  occasion       挂回，confidence 确定／可能，可能必须带理由；
  thread_summary 挂主线「总述」，不生成回；
  thread_pending 挂主线「回待定」，candidates 是这条主线下的回（可以为空），必须带理由；
  thread_unknown 「主线待定」，candidates 是主线（可以为空），必须带理由；
  daily／skip    日常、不记，不和别的挂接同时出现。
不依赖现有 layer_store 的表和校验（那里的表写死）；复用第一步的原话检查（write_contract）和错误类（write_store）。
文件载体 LightFileStore 沿用 tests/fakes_layer.FileStore（原话层、调用记录、步绑定、故障注入），只换组织层的校验和应用。
只用标准库；不联网、不读密钥、不调模型。"""
import copy
import re

from . import contract as WC
from .storeerr import (StoreError, MessageConflict, StaleBase, ChangeSetInvalid, KeyConflict,  # noqa: F401
                       ReceiptExists, TransactionFailed, StepConflict, StepOrderError, ended_steps, abandon_receipt, find_receipt)  # noqa: F401


class LightInvalid(ChangeSetInvalid):
    def __init__(self, code, msg):
        super().__init__("[%s] %s" % (code, msg))
        self.code = code


def _bad(code, msg):
    raise LightInvalid(code, msg)


TABLES = ("threads", "thread_names", "thread_descs", "occasions", "occ_titles", "occ_summaries", "sentences", "attachments", "gaps", "receipts")
KINDS = ("occasion", "thread_summary", "thread_pending", "thread_unknown", "daily", "skip")
CONFIDENCES = (None, "确定", "可能")
GAP_OUTCOMES = ("resolved", "unresolved_no_candidate", "unresolved_after_rejudge")
GAP_LAYERS = ("主线", "回")

RECORD_FIELDS = {
    "threads": ("thread_id", "created_by"),
    "thread_names": ("thread_id", "version", "name", "by"),
    "thread_descs": ("thread_id", "version", "text", "by"),
    "occasions": ("occasion_id", "thread_id", "created_by"),
    "occ_titles": ("occasion_id", "version", "title", "by"),
    "occ_summaries": ("occasion_id", "version", "text", "by", "reason", "evidence_sentences", "request_fp"),
    "sentences": ("sentence_id", "msg_id", "session_id", "start", "end", "text", "role", "date", "ord", "created_by"),
    "attachments": ("sentence_id", "kind", "target_id", "confidence", "candidates", "reason", "created_by"),
    "gaps": ("gap_id", "sentence_ids", "layer", "known_thread", "words", "found", "outcome", "created_by"),
    "receipts": ("kind", "commit_seq"),
}


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _s(v):
    return isinstance(v, str) and v != ""


def _ns(v):
    return v is None or _s(v)


def _str(v):
    return isinstance(v, str)


def _sl(v):
    return isinstance(v, list) and all(_s(x) for x in v)


def _ver(v):
    return _int(v) and v >= 1


TYPES = {
    "threads": {"thread_id": _s, "created_by": _s},
    "thread_names": {"thread_id": _s, "version": _ver, "name": _s, "by": _s},
    "thread_descs": {"thread_id": _s, "version": _ver, "text": _s, "by": _s},
    "occasions": {"occasion_id": _s, "thread_id": _s, "created_by": _s},
    "occ_titles": {"occasion_id": _s, "version": _ver, "title": _s, "by": _s},
    "occ_summaries": {"occasion_id": _s, "version": _ver, "text": _s, "by": _s, "reason": _str, "evidence_sentences": _sl, "request_fp": _ns},
    "sentences": {"sentence_id": _s, "msg_id": _s, "session_id": _s, "start": _int, "end": _int, "text": _str, "role": lambda v: v in ("user", "assistant"),
                  "date": _ns, "ord": _int, "created_by": _s},
    "attachments": {"sentence_id": _s, "kind": lambda v: v in KINDS, "target_id": _ns, "confidence": lambda v: v in CONFIDENCES, "candidates": _sl,
                    "reason": _str, "created_by": _s},
    "gaps": {"gap_id": _s, "sentence_ids": lambda v: _sl(v) and len(v) > 0, "layer": lambda v: v in GAP_LAYERS, "known_thread": _ns, "words": _sl, "found": _sl,
             "outcome": lambda v: v in GAP_OUTCOMES, "created_by": _s},
    "receipts": {"kind": lambda v: v in ("step", "op"), "commit_seq": _int},
}


# ---------------------------------------------------------------- 编号与键
def thread_id_for(ns, n):
    return "th-%s-%04d" % (WC.ns_tag(ns), n)


def occasion_id_for(ns, n):
    return "oc-%s-%04d" % (WC.ns_tag(ns), n)


def gap_id_for(ns, n):
    return "gp-%s-%04d" % (WC.ns_tag(ns), n)


_NUM_RE = re.compile(r"-([0-9]+)$")
ID_KINDS = {"thread": ("threads", "thread_id"), "occasion": ("occasions", "occasion_id"), "gap": ("gaps", "gap_id")}


def next_number(state, kind):
    """这一类编号现有的最大序号加一（不是记录条数加一）。kind ∈ thread / occasion / gap。"""
    table, fld = ID_KINDS[kind]
    nums = [(state.get("hwm") or {}).get(kind, 0)]                       # 删除留下的高水位：编号不复用
    for r in state["tables"][table]:
        m = _NUM_RE.search(r[fld])
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _id_ok(ns, prefix, v):
    return isinstance(v, str) and re.fullmatch(r"%s-%s-[0-9]{4,}" % (prefix, WC.ns_tag(ns)), v) is not None


def attachment_key(sentence_id, kind, target_id):
    return "at:%s:%s:%s" % (sentence_id, kind, target_id or "-")


def key_for(table, r):
    if table == "threads":
        return r["thread_id"]
    if table == "thread_names":
        return "%s#n%d" % (r["thread_id"], r["version"])
    if table == "thread_descs":
        return "%s#d%d" % (r["thread_id"], r["version"])
    if table == "occasions":
        return r["occasion_id"]
    if table == "occ_titles":
        return "%s#t%d" % (r["occasion_id"], r["version"])
    if table == "occ_summaries":
        return "%s#s%d" % (r["occasion_id"], r["version"])
    if table == "sentences":
        return r["sentence_id"]
    if table == "attachments":
        return attachment_key(r["sentence_id"], r["kind"], r["target_id"])
    if table == "gaps":
        return r["gap_id"]
    return r["key"]


# ---------------------------------------------------------------- 状态
def empty_state(ns):
    return {"ns": ns, "seq": 0, "tables": {t: [] for t in TABLES}}


def apply_changeset(state, cs, get_message):
    validate_changeset(cs, state, get_message)
    return _raw_apply(state, cs)


def _raw_apply(state, cs):
    new = copy.deepcopy(state)
    for t in TABLES:
        for r in cs["inserts"].get(t, []):
            new["tables"][t].append(copy.deepcopy(r))
    new["seq"] = cs["new_seq"]
    return new


def as_of(state, seq):
    """还原到提交序号 seq：之后创建的记录看不见。"""
    out = {"ns": state["ns"], "seq": min(seq, state["seq"]), "tables": {}}
    for t in TABLES:
        out["tables"][t] = [copy.deepcopy(r) for r in state["tables"][t] if r["created_seq"] <= seq]
    for extra in ("hwm", "deleted_ks"):                                  # 删除留下的编号高水位、已删步号：只有发生过删除才有
        if state.get(extra):
            out[extra] = copy.deepcopy(state[extra])
    return out


def _latest(rows, key):
    out = {}
    for r in rows:
        if r[key] not in out or r["version"] > out[r[key]]["version"]:
            out[r[key]] = r
    return out


def current_summary(state, occasion_id):
    rows = [r for r in state["tables"]["occ_summaries"] if r["occasion_id"] == occasion_id]
    return max(rows, key=lambda r: r["version"]) if rows else None


def current_title(state, occasion_id):
    rows = [r for r in state["tables"]["occ_titles"] if r["occasion_id"] == occasion_id]
    return max(rows, key=lambda r: r["version"]) if rows else None


def occasion_thread(state):
    return {o["occasion_id"]: o["thread_id"] for o in state["tables"]["occasions"]}


# ---------------------------------------------------------------- 变更集校验
def validate_changeset(cs, state, get_message):
    """任何一条不过整份不收（抛 LightInvalid / StaleBase / KeyConflict / ReceiptExists）。get_message(ns, msg_id) 是只读的原话查询。"""
    try:
        _validate(cs, state, get_message)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
        raise LightInvalid("malformed", "变更集结构不合法：%r" % (e,)) from e


def _validate(cs, state, get_message):
    if not isinstance(cs, dict):
        _bad("missing_key", "变更集必须是对象")
    for k in ("ns", "expected_prev_seq", "new_seq", "inserts", "updates"):
        if k not in cs:
            _bad("missing_key", "变更集缺少 %s" % k)
    ns = state["ns"]
    if cs["ns"] != ns:
        _bad("ns_mismatch", "变更集的命名空间 %r 与存储的 %r 不同" % (cs["ns"], ns))
    if not _int(cs["expected_prev_seq"]) or not _int(cs["new_seq"]):
        _bad("bad_seq", "序号必须是整数")
    if cs["expected_prev_seq"] != state["seq"]:
        raise StaleBase("变更集基于序号 %r，存储现在是 %r" % (cs["expected_prev_seq"], state["seq"]))
    new_seq = cs["new_seq"]
    if new_seq != cs["expected_prev_seq"] + 1:
        _bad("bad_seq", "new_seq 必须是 expected_prev_seq+1")
    ins = cs["inserts"]
    if not isinstance(ins, dict) or not isinstance(cs["updates"], list):
        _bad("missing_key", "inserts 必须是对象，updates 必须是列表")
    if cs["updates"]:
        _bad("bad_update", "轻写存储只追加，不允许更新")
    for t in ins:
        if t not in TABLES:
            _bad("unknown_table", "不认识的表 %r" % (t,))
        if not isinstance(ins[t], list):
            _bad("bad_record", "%s 必须是列表" % t)
    for t in TABLES:
        for r in ins.get(t, []):
            _check_record(ns, t, r, new_seq)
    _check_attachment_dups(ins.get("attachments", []))
    existing = {t: {r["key"] for r in state["tables"][t]} for t in TABLES}
    seen = {t: set() for t in TABLES}
    for t in TABLES:
        for r in ins.get(t, []):
            if t != "receipts" and r["key"] != key_for(t, r):
                _bad("bad_key", "%s 的键 %r 与字段算出的 %r 不同" % (t, r["key"], key_for(t, r)))
            if r["key"] in existing[t] or r["key"] in seen[t]:
                if t == "receipts":
                    raise ReceiptExists("收据 %r 已存在" % r["key"])
                raise KeyConflict("%s 表里键 %r 已存在，不能覆盖" % (t, r["key"]))
            seen[t].add(r["key"])
    rc = ins.get("receipts", [])
    if len(rc) != 1 or rc[0]["commit_seq"] != new_seq:
        _bad("receipt_count", "每份变更集必须恰好带一张收据，且 commit_seq == new_seq")
    _check_receipt(rc[0], ins)
    post = _raw_apply(state, cs)["tables"]
    c = _Ctx(state, cs, post)
    _check_sentences(c, get_message)
    _check_dangling(c)
    _check_new_objects(c)
    _check_versions(c)
    _check_attachments(c)
    _check_gaps(c)
    _check_summary_edits(c)


def _check_record(ns, t, r, new_seq):
    if not isinstance(r, dict):
        _bad("bad_record", "%s 的记录必须是对象" % t)
    want = set(RECORD_FIELDS[t]) | {"key", "created_seq"}
    have = set(r)
    if t == "receipts":
        if not want <= have:
            _bad("bad_record", "收据缺少基础字段 %s" % sorted(want - have))
    elif have != want:
        _bad("bad_record", "%s 的字段集合不对：多了 %s，少了 %s" % (t, sorted(have - want), sorted(want - have)))
    if not _s(r["key"]):
        _bad("bad_record", "%s 的 key 必须是非空字符串" % t)
    if not _int(r["created_seq"]) or r["created_seq"] != new_seq:
        _bad("bad_record", "%s %r 的 created_seq 必须是本次提交序号 %d" % (t, r["key"], new_seq))
    for f, pred in TYPES[t].items():
        if not pred(r[f]):
            _bad("bad_record", "%s %r 的 %s 类型或取值不合法：%r" % (t, r["key"], f, r[f]))
    if t == "threads" and not _id_ok(ns, "th", r["thread_id"]):
        _bad("bad_id", "主线编号 %r 不是 th-<命名空间标记>-NNNN" % (r["thread_id"],))
    if t == "occasions" and not _id_ok(ns, "oc", r["occasion_id"]):
        _bad("bad_id", "回的编号 %r 不是 oc-<命名空间标记>-NNNN" % (r["occasion_id"],))
    if t == "gaps" and not _id_ok(ns, "gp", r["gap_id"]):
        _bad("bad_id", "缺口编号 %r 不是 gp-<命名空间标记>-NNNN" % (r["gap_id"],))


def _check_attachment_dups(rows):
    seen = set()
    for a in rows:
        sig = (a["sentence_id"], a["kind"], a["target_id"])
        if sig in seen:
            _bad("dup_attachment", "同一句 %r 重复挂同一个对象（%s %r）" % (a["sentence_id"], a["kind"], a["target_id"]))
        seen.add(sig)


def _check_receipt(r, ins):
    if r["kind"] == "step":
        if not (_s(r.get("step_id")) and _int(r.get("k")) and r["k"] >= 1):
            _bad("bad_record", "步收据要有 step_id 和 k（正整数）")
        if r["key"] != "step:" + r["step_id"]:
            _bad("bad_key", "步收据的键必须是 step:<step_id>")
    else:
        if not (_s(r.get("op")) and _s(r.get("op_id")) and _s(r.get("step_id")) and _int(r.get("k"))):
            _bad("bad_record", "操作收据要有 op、op_id、step_id、k")
        if r["key"] != "op:" + r["op_id"]:
            _bad("bad_key", "操作收据的键必须是 op:<op_id>")
        if any(t != "receipts" and rows for t, rows in ins.items()):
            _bad("bad_record", "操作（放弃）的变更集只能带收据")


class _Ctx:
    def __init__(self, state, cs, post):
        self.state, self.cs, self.post = state, cs, post
        self.ns = state["ns"]
        self.ins = cs["inserts"]
        self.new_seq = cs["new_seq"]
        self.threads = {r["thread_id"] for r in post["threads"]}
        self.occ = {r["occasion_id"]: r["thread_id"] for r in post["occasions"]}
        self.pre_threads = {r["thread_id"] for r in state["tables"]["threads"]}
        self.pre_occ = {r["occasion_id"] for r in state["tables"]["occasions"]}
        self.new_threads = [r["thread_id"] for r in self.ins.get("threads", [])]
        self.new_occ = [r["occasion_id"] for r in self.ins.get("occasions", [])]
        self.step_sentences = {r["sentence_id"] for r in self.ins.get("sentences", [])}
        self.step_atts = self.ins.get("attachments", [])


def _check_sentences(c, get_message):
    for r in c.ins.get("sentences", []):
        if r["key"] != r["sentence_id"]:
            _bad("bad_key", "句子的键必须等于 sentence_id")
        m = get_message(c.ns, r["msg_id"])
        if m is None:
            _bad("sentence_message", "句子 %r 引用的消息 %r 没有保存的原话" % (r["key"], r["msg_id"]))
        if m["session_id"] != r["session_id"] or m["role"] != r["role"]:
            _bad("sentence_message", "句子 %r 的会话或说话人和原话对不上" % r["key"])
        mt = m.get("msg_time")
        if (mt[:10] if isinstance(mt, str) and mt else None) != r["date"]:
            _bad("sentence_message", "句子 %r 的日期和原话的消息日期对不上" % r["key"])
        try:
            WC.check_span(m["content"], r["start"], r["end"], r["text"])
        except WC.ContractError as e:
            _bad("sentence_span", "句子 %r：%s" % (r["key"], e))
        if r["sentence_id"] != WC.fragment_id(c.ns, r["msg_id"], r["start"], r["end"]):
            _bad("sentence_id", "句子 %r 的 sentence_id 与消息、区间对不上" % r["key"])


def _check_dangling(c):
    for t in ("thread_names", "thread_descs"):
        for r in c.ins.get(t, []):
            if r["thread_id"] not in c.threads:
                _bad("dangling_ref", "%s %r 指向不存在的主线 %r" % (t, r["key"], r["thread_id"]))
    for r in c.ins.get("occasions", []):
        if r["thread_id"] not in c.threads:
            _bad("dangling_ref", "回 %r 指向不存在的主线 %r" % (r["key"], r["thread_id"]))
    for t in ("occ_titles", "occ_summaries"):
        for r in c.ins.get(t, []):
            if r["occasion_id"] not in c.occ:
                _bad("dangling_ref", "%s %r 指向不存在的回 %r" % (t, r["key"], r["occasion_id"]))
    for a in c.step_atts:
        if a["sentence_id"] not in c.step_sentences:
            _bad("dangling_ref", "挂接 %r 的句子不是本步新增的句子" % a["key"])
        if a["kind"] == "occasion" and a["target_id"] not in c.occ:
            _bad("dangling_ref", "挂接 %r 指向不存在的回 %r" % (a["key"], a["target_id"]))
        if a["kind"] in ("thread_summary", "thread_pending") and a["target_id"] not in c.threads:
            _bad("dangling_ref", "挂接 %r 指向不存在的主线 %r" % (a["key"], a["target_id"]))


def _check_new_objects(c):
    ins = c.ins
    for t in c.new_threads:
        names = [r for r in ins.get("thread_names", []) if r["thread_id"] == t]
        descs = [r for r in ins.get("thread_descs", []) if r["thread_id"] == t]
        if len(names) != 1 or len(descs) != 1 or names[0]["version"] != 1 or descs[0]["version"] != 1:
            _bad("thread_incomplete", "新主线 %r 必须同时带名称第 1 版和说明第 1 版（各一条）" % t)
    for o in c.new_occ:
        titles = [r for r in ins.get("occ_titles", []) if r["occasion_id"] == o]
        sums = [r for r in ins.get("occ_summaries", []) if r["occasion_id"] == o]
        if len(titles) != 1 or len(sums) != 1 or titles[0]["version"] != 1 or sums[0]["version"] != 1:
            _bad("occasion_incomplete", "新回 %r 必须同时带标题第 1 版和摘要第 1 版（各一条）" % o)
    certain_occ = {a["target_id"] for a in c.step_atts if a["kind"] == "occasion" and a["confidence"] == "确定"}
    ref_threads = {a["target_id"] for a in c.step_atts if a["kind"] in ("thread_summary", "thread_pending")}
    ref_threads |= {c.occ[o] for o in certain_occ if o in c.occ}
    for o in c.new_occ:
        if o not in certain_occ:
            _bad("unreferenced_new", "新回 %r 没有任何一句确定挂在它下面" % o)
    for t in c.new_threads:
        if t not in ref_threads:
            _bad("unreferenced_new", "新主线 %r 没有被任何句子引用（总述、回待定，或确定挂在它的回上）" % t)


def _check_versions(c):
    for table, parent in (("thread_names", "thread_id"), ("thread_descs", "thread_id"), ("occ_titles", "occasion_id"), ("occ_summaries", "occasion_id")):
        groups = {}
        for r in c.ins.get(table, []):
            groups.setdefault(r[parent], []).append(r)
        for pid, rows in groups.items():
            if len(rows) > 1:
                _bad("bad_version", "%s：同一份里 %r 不能新增多个版本" % (table, pid))
            r = rows[0]
            pre = [x["version"] for x in c.state["tables"][table] if x[parent] == pid]
            latest = max(pre) if pre else 0
            if r["version"] != latest + 1:
                _bad("bad_version", "%s：%r 的新版本号必须是 %d，实际 %r" % (table, pid, latest + 1, r["version"]))
            if table in ("thread_names", "thread_descs", "occ_titles") and r["version"] != 1:
                _bad("bad_version", "%s 只允许第 1 版（这一轮不改名称、说明、标题）" % table)


def _check_attachments(c):
    by_sent = {}
    for a in c.step_atts:
        by_sent.setdefault(a["sentence_id"], []).append(a)
    for sid in c.step_sentences:
        if sid not in by_sent:
            _bad("sentence_unplaced", "句子 %r 没有任何去处" % sid)
    for a in c.step_atts:
        k, conf, tgt, cands, reason = a["kind"], a["confidence"], a["target_id"], a["candidates"], a["reason"]
        w = "挂接 %r" % a["key"]
        if k == "occasion":
            if tgt is None or conf not in ("确定", "可能") or cands:
                _bad("bad_attachment", "%s：挂回要有目标、把握（确定／可能），不带候选" % w)
            if conf == "可能" and not reason.strip():
                _bad("bad_attachment", "%s：把握是「可能」，必须写理由" % w)
        elif k == "thread_summary":
            if tgt is None or conf is not None or cands:
                _bad("bad_attachment", "%s：总述要有主线目标，不带把握和候选" % w)
        elif k == "thread_pending":
            if tgt is None or conf is not None:
                _bad("bad_attachment", "%s：回待定要有主线目标，不带把握" % w)
            if not reason.strip():
                _bad("bad_attachment", "%s：回待定必须写理由" % w)
            for cand in cands:
                if c.occ.get(cand) != tgt:
                    _bad("bad_candidate", "%s：候选 %r 不是主线 %r 下的回" % (w, cand, tgt))
        elif k == "thread_unknown":
            if tgt is not None or conf is not None:
                _bad("bad_attachment", "%s：主线待定不带目标和把握" % w)
            if not reason.strip():
                _bad("bad_attachment", "%s：主线待定必须写理由" % w)
            for cand in cands:
                if cand not in c.threads:
                    _bad("bad_candidate", "%s：候选主线 %r 不存在" % (w, cand))
        else:   # daily／skip
            if tgt is not None or conf is not None or cands:
                _bad("bad_attachment", "%s：日常、不记不带目标、把握和候选" % w)
        if len(set(cands)) != len(cands):
            _bad("bad_candidate", "%s：候选重复" % w)
    for sid, rows in by_sent.items():
        if any(a["kind"] in ("daily", "skip") for a in rows) and len(rows) > 1:
            _bad("exclusive", "句子 %r 有日常或不记，就不能再有别的去处" % sid)


def _check_gaps(c):
    atts = {}
    for a in c.step_atts:
        atts.setdefault(a["sentence_id"], []).append(a)
    seen = set()
    for g in c.ins.get("gaps", []):
        w = "缺口 %r" % g["key"]
        if g["known_thread"] is not None and g["known_thread"] not in c.threads:
            _bad("bad_gap", "%s 的已知主线 %r 不存在" % (w, g["known_thread"]))
        for f in g["found"]:
            if f not in c.threads and f not in c.occ:
                _bad("bad_gap", "%s 的找回对象 %r 不存在" % (w, f))
        for sid in g["sentence_ids"]:
            if sid not in c.step_sentences:
                _bad("bad_gap", "%s 的句子 %r 不是本步新增的句子" % (w, sid))
            if sid in seen:
                _bad("bad_gap", "句子 %r 出现在两个缺口里" % sid)
            seen.add(sid)
            if g["outcome"].startswith("unresolved"):
                kinds = [(a["kind"], a["target_id"]) for a in atts.get(sid, [])]
                want = ("thread_pending", g["known_thread"]) if g["known_thread"] is not None else ("thread_unknown", None)
                if want not in kinds:
                    _bad("bad_gap", "%s 没解决，句子 %r 必须记 %s%s" % (w, sid, want[0], "" if want[1] is None else "（目标 %s）" % want[1]))
                if len(kinds) != 1:
                    _bad("bad_gap", "%s 没解决，句子 %r 只能有这一条去处" % (w, sid))


def _check_summary_edits(c):
    new_occ = set(c.new_occ)
    for r in c.ins.get("occ_summaries", []):
        if r["occasion_id"] in new_occ:
            if r["version"] != 1 or r["reason"] != "" or r["evidence_sentences"]:
                _bad("bad_summary_edit", "新回 %r 的摘要第 1 版不带修改理由和依据句" % r["occasion_id"])
            continue
        if r["occasion_id"] not in c.pre_occ:
            _bad("dangling_ref", "摘要 %r 指向不存在的回" % r["key"])
        if r["version"] < 2:
            _bad("bad_version", "已有的回只能追加第 2 版以后的摘要")
        if not r["reason"].strip():
            _bad("bad_summary_edit", "改摘要必须写理由（%r）" % r["key"])
        for sid in r["evidence_sentences"]:
            if sid not in c.step_sentences:
                _bad("bad_summary_edit", "改摘要的依据句 %r 不是本步的句子" % sid)
