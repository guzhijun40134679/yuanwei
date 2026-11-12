"""写入变轻·粗筛和补找。算法照 layer_shortlist.rank_threads／supplement，改成读轻写存储（照抄差异.md 列了每处不同）。
只读状态；纯函数；不联网、不调模型。分数只是排序依据，不叫概率。"""
from . import lstate as LS
from . import util as U
from . import view as LV


def thread_text_parts(state):
    """{主线: {"head": [...], "all": [...]}}。head 是粗筛打分用的（名称、说明、下面各回的标题）；all 是补找用的（再加各回摘要、挂在这条主线上的句子原话）。
    没有长期约定（这一轮不单独建约定）。"""
    nm, ds, tt, sm = LV.names(state), LV.descs(state), LV.titles(state), LV.summaries(state)
    occs = LV.thread_occasions(state)
    tsent = LV.thread_sentences(state)
    out = {}
    for t in state["tables"]["threads"]:
        tid = t["thread_id"]
        head = []
        if tid in nm:
            head.append(nm[tid])
        if tid in ds:
            head.append(ds[tid])
        allp = list(head)
        for oc in occs.get(tid, []):
            if oc in tt:
                head.append(tt[oc])
                allp.append(tt[oc])
            if oc in sm:
                allp.append(sm[oc])
        allp += [s["text"] for s in tsent.get(tid, [])]
        out[tid] = {"head": head, "all": allp}
    return out


def recent_threads(state, n):
    """最近 n 个已提交的会话步（收据 kind=step，按 k 从大到小）碰过的主线。"""
    steps = sorted((r for r in state["tables"]["receipts"] if r.get("kind") == "step"), key=lambda r: -r["k"])[:max(0, n)]
    out = set()
    for r in steps:
        out |= set(r.get("touched_threads") or [])
    return out


def rank_threads(state, messages, config):
    """给每条主线算相关性分数（三项加权），高的在前，同分按主线编号。返回 [{"thread_id","score","parts":{"overlap","recent","open"}}]。
    open 项：没有状态，「有没有结束」一律当作没结束，所以对每条主线都是 1.0。"""
    parts = thread_text_parts(state)
    grams = {tid: set() for tid in parts}
    for tid, p in parts.items():
        for t in p["head"]:
            grams[tid] |= U.bigrams(t)
    df = {}
    for gs in grams.values():
        for g in gs:
            df[g] = df.get(g, 0) + 1
    q = set()
    for m in messages:
        q |= U.bigrams(m["content"])
    recent = recent_threads(state, config.recent_sessions)
    out = []
    for tid in parts:
        overlap = round(sum(1.0 / df[g] for g in sorted(grams[tid] & q)), 6)
        rec = 1.0 if tid in recent else 0.0
        opn = 1.0
        score = round(config.w_overlap * overlap + config.w_recent * rec + config.w_open * opn, 6)
        out.append({"thread_id": tid, "score": score, "parts": {"overlap": overlap, "recent": rec, "open": opn}})
    out.sort(key=lambda r: (-r["score"], r["thread_id"]))
    return out


def supplement(state, base_seq, gaps, shown_threads, shown_occasions, config):
    """补找：每项缺口各找各的；只读 base_seq 时点的状态。
    层＝主线：在没展示的主线里找；层＝回：只在已知主线下没展示的回里找（已知主线不存在则找不到）。
    词的匹配照现有 supplement：词（小写）出现在文字里，命中词数多的在前，同数按最近碰过（最后一次被改动的提交序号大的在前）、再按编号；每项最多 supplement_max 条。
    gaps：[{"sentences","layer","known_thread","words"}]。返回与 gaps 逐项对应的 [{"sentences","layer","known_thread","words","found","hits"}]。找回的只是候选。"""
    st = LS.as_of(state, base_seq)
    shown_t, shown_o = set(shown_threads), set(shown_occasions)
    parts = thread_text_parts(st)
    ttexts = {tid: "\n".join(p["all"]).lower() for tid, p in parts.items()}
    last = LV.last_changed_seq(st)
    tt, sm = LV.titles(st), LV.summaries(st)
    osent = LV.occasion_sentences(st)
    otexts = {}
    for o in st["tables"]["occasions"]:
        oc = o["occasion_id"]
        otexts[oc] = ("\n".join([tt.get(oc, ""), sm.get(oc, "")] + [s["text"] for s in osent.get(oc, [])])).lower()
    occ_last = {}
    for r in st["tables"]["occasions"]:
        occ_last[r["occasion_id"]] = r["created_seq"]
    for a in st["tables"]["attachments"]:
        if a["kind"] == "occasion" and a["created_seq"] > occ_last.get(a["target_id"], 0):
            occ_last[a["target_id"]] = a["created_seq"]
    occs = LV.thread_occasions(st)
    out = []
    for g in gaps:
        hits, order = {}, []
        if g["layer"] == "主线":
            for tid, t in ttexts.items():
                if tid in shown_t:
                    continue
                n = sum(1 for w in g["words"] if w.lower() in t)
                if n > 0:
                    hits[tid] = n
            order = sorted(hits, key=lambda x: (-hits[x], -last.get(x, 0), x))[:config.supplement_max]
        else:
            for oc in occs.get(g["known_thread"], []):
                if oc in shown_o:
                    continue
                n = sum(1 for w in g["words"] if w.lower() in otexts[oc])
                if n > 0:
                    hits[oc] = n
            order = sorted(hits, key=lambda x: (-hits[x], -occ_last.get(x, 0), x))[:config.supplement_max]
        out.append({"sentences": list(g["sentences"]), "layer": g["layer"], "known_thread": g["known_thread"], "words": list(g["words"]),
                    "found": order, "hits": {x: hits[x] for x in order}})
    return out
