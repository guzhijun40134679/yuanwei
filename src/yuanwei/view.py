"""轻写状态的只读视图：主线、回的当前文字，句子归属，提到的日期。纯函数；不联网、不读密钥、不调模型。"""

from . import lstate as LS


def latest(rows, key):
    out = {}
    for r in rows:
        if r[key] not in out or r["version"] > out[r[key]]["version"]:
            out[r[key]] = r
    return out


def names(st):
    return {t: r["name"] for t, r in latest(st["tables"]["thread_names"], "thread_id").items()}


def descs(st):
    return {t: r["text"] for t, r in latest(st["tables"]["thread_descs"], "thread_id").items()}


def titles(st):
    return {o: r["title"] for o, r in latest(st["tables"]["occ_titles"], "occasion_id").items()}


def summaries(st):
    return {o: r["text"] for o, r in latest(st["tables"]["occ_summaries"], "occasion_id").items()}


def sentence_rows(st):
    return {s["sentence_id"]: s for s in st["tables"]["sentences"]}


def _chron(rows):
    return sorted(rows, key=lambda s: (s["created_seq"], s["ord"], s["sentence_id"]))


def occasion_sentences(st):
    """{回: [句子行（按先后）]}：只算确定挂在这一回的。"""
    sent = sentence_rows(st)
    out = {}
    for a in st["tables"]["attachments"]:
        if a["kind"] == "occasion" and a["confidence"] == "确定":
            out.setdefault(a["target_id"], []).append(sent[a["sentence_id"]])
    return {o: _chron(rows) for o, rows in out.items()}


def thread_sentences(st):
    """{主线: [句子行（按先后）]}：确定挂在它的回上的、总述、回待定。"""
    sent = sentence_rows(st)
    occ_th = LS.occasion_thread(st)
    out = {}
    for a in st["tables"]["attachments"]:
        t = None
        if a["kind"] == "occasion" and a["confidence"] == "确定":
            t = occ_th.get(a["target_id"])
        elif a["kind"] in ("thread_summary", "thread_pending"):
            t = a["target_id"]
        if t is not None:
            out.setdefault(t, {})[a["sentence_id"]] = sent[a["sentence_id"]]
    return {t: _chron(list(d.values())) for t, d in out.items()}


def thread_occasions(st):
    out = {}
    for o in st["tables"]["occasions"]:
        out.setdefault(o["thread_id"], []).append(o["occasion_id"])
    return out


def last_changed_seq(st):
    """{主线: 最后一次改动它（或它的回、归属）的提交序号}。"""
    occ_th = LS.occasion_thread(st)
    out = {}

    def bump(t, seq):
        if t is not None and seq > out.get(t, 0):
            out[t] = seq
    for r in st["tables"]["threads"]:
        bump(r["thread_id"], r["created_seq"])
    for r in st["tables"]["occasions"]:
        bump(r["thread_id"], r["created_seq"])
    for tb in ("occ_titles", "occ_summaries"):
        for r in st["tables"][tb]:
            bump(occ_th.get(r["occasion_id"]), r["created_seq"])
    for a in st["tables"]["attachments"]:
        t = occ_th.get(a["target_id"]) if a["kind"] == "occasion" else (a["target_id"] if a["kind"] in ("thread_summary", "thread_pending") else None)
        bump(t, a["created_seq"])
    return out


def confirmed_thread_map(st):
    """{消息: frozenset(主线)}：只认显式确认的父线——确定挂回、总述、回待定。候选里的编号、可能的挂接、主线待定都不算。"""
    sent = sentence_rows(st)
    occ_th = LS.occasion_thread(st)
    out = {}
    for a in st["tables"]["attachments"]:
        t = None
        if a["kind"] == "occasion" and a["confidence"] == "确定":
            t = occ_th.get(a["target_id"])
        elif a["kind"] in ("thread_summary", "thread_pending"):
            t = a["target_id"]
        if t is not None:
            out.setdefault(sent[a["sentence_id"]]["msg_id"], set()).add(t)
    return {m: frozenset(s) for m, s in out.items()}
