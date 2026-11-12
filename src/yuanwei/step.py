"""写入变轻·一步的流程（run_step）和显式操作（apply_operation：放弃一步）。

一步 = 一次已结束的会话 k。流程：
  1 原话先存（独立于整理）；步绑定、次序检查（同现行：k 必须是已结束的最大步序号＋1）；
  2 读 base_seq 时的状态；程序切句、编号 s1、s2……；粗筛（算法照 rank_threads，12 条主线、每条 8 回）；拼初判请求（提示词＋候选卡片＋这次的句子）；
  3 初判（question=q1）→ 转换和校验；不合格带着理由重问；
  4 有缺口就补找（一次会话的缺口合并成一次，只读 base_seq 时的状态，找没展示的主线、已知主线下没展示的回）：
      没有新候选：不发模型，缺口句子按已知范围记待定（有已知主线记 thread_pending，没有记 thread_unknown）；
      有新候选：把有缺口的句子拿去重判一次（question=q2），补到的只是未确认候选；重判后仍有缺口也记待定，不进第三轮；
  5 规划（合成一份完整的变更集）、整份校验、一次提交。规划或存储拒收是装置问题（apparatus_error），不重问、不提交。
次数：一步最多 3 次实际发送 = 一次初判、最多一次重判、两阶段合用一次技术重问（格式不合格或网络失败都占这一次）。初判最多 2 次；重判最多 3 − 初判实际次数 次。
网络失败＝judge 抛 RetryableNetworkError；格式不合格＝转换拒收；别的异常算 model_failed（装置问题，不重问）。
改摘要：开关 allow_summary_edit。提议存在判断记录里，只有提交成功才进状态（占 occ_summaries 的新版本，旧版本留着）；放弃的步、重问、回放都不生效。
重问、复用沿用现行的 layer_step._ask（任务指纹相同的合格输出直接复用，重开不重发、不重置次数）。
不联网、不读密钥、不调模型（判断由调用方注入）。"""
from . import ask as ASK
from . import cards as LC
from . import contract as WC
from . import convert as CV
from . import lstate as LS
from . import shortlist as SL
from . import storeerr as WS
from . import util as U
from .ask import RetryableNetworkError  # noqa: F401

SPLIT_VERSION = "split-1"
MAX_SENDS = 3
GAP_NO_CANDIDATE = "补找没找到，按已知范围记待定"
GAP_AFTER_REJUDGE = "重判后仍找不到，按已知范围记待定"


def _fp(obj):
    return WC.sha256_hex(WC.canonical(obj))


class _Cap:
    def __init__(self, n):
        self.max_sends = n


def default_shortlister(state, messages, config):
    return [r["thread_id"] for r in SL.rank_threads(state, messages, config)]


def run_step(store, ns, step_id, session_id, k, messages, judge, clock, config=None, allow_summary_edit=True, prompt_text=None, shortlister=None, strict_grouping=False):
    """返回 dict：status ∈ committed / replayed / abandoned / q1_failed / q2_failed / model_failed / apparatus_error / commit_failed。"""
    if not (isinstance(k, int) and not isinstance(k, bool) and k >= 1):
        raise WC.ContractError("步序号 k 必须是正整数，实际 %r" % (k,))
    config = config or LC.LightConfig()
    shortlister = shortlister or default_shortlister
    if prompt_text is None:
        raise WC.ContractError("必须给提示词原文（prompt_text）")
    for m in messages:
        if m.get("session_id", session_id) != session_id:
            raise WC.ContractError("消息 %r 不属于会话 %r" % (m.get("msg_id"), session_id))
        old = store.get_message(ns, m["msg_id"])
        msg = WC.make_message(ns, session_id, m["seq"], m["msg_id"], m["role"], m["content"], m.get("msg_time"), old["received_at"] if old else clock())
        store.save_message(msg)
    stored = [store.get_message(ns, m["msg_id"]) for m in sorted(messages, key=lambda m: m["seq"])]
    msg_fp_digest = WC.sha256_hex(WC.canonical([m["fingerprint"] for m in stored]))
    key = "step:" + step_id
    msg_ids = [m["msg_id"] for m in stored]
    binding = store.get_step_binding(ns, step_id)
    if binding is not None and (binding["session_id"], binding["k"], binding["msg_ids"], binding["msg_fp_digest"]) != (session_id, k, msg_ids, msg_fp_digest):
        raise WS.StepConflict("步 %r 已绑定会话 %r、k=%r、原话 %s；同一步不能换会话、步序号或原话集合" % (step_id, binding["session_id"], binding["k"], binding["msg_ids"]))
    state = store.read_state(ns)
    base = {"step_id": step_id, "session_id": session_id, "receipt": None, "requests": {"q1": None, "q2": None}, "reasons": [], "error": None, "q1_sends": None,
            "q2_sends": None, "reused_q1": False, "reused_q2": False, "recovered_uncertain_commit": False, "supp": [], "proposals": [], "sentences": None}
    rc = WS.find_receipt(state, key)
    if rc is not None:
        if rc["msg_fp_digest"] != msg_fp_digest or rc["session_id"] != session_id or rc["k"] != k:
            raise WS.StepConflict("步 %r 已按另一组原话或另一个步序号提交过；同一步不能换输入" % step_id)
        return dict(base, status="replayed", receipt=rc)
    ab = WS.abandon_receipt(state, step_id)
    if ab is not None:
        return dict(base, status="abandoned", receipt=ab)
    expected_k = WS.expected_k(state)
    if k != expected_k:
        raise WS.StepOrderError("步序号 %r 不对：已结束的最大步序号是 %d，下一步必须是 %d（前面的会话要先提交，或用 abandon_step 明确放弃）" % (k, expected_k - 1, expected_k))
    if binding is None:
        store.bind_step(ns, step_id, {"step_id": step_id, "session_id": session_id, "k": k, "msg_ids": msg_ids, "msg_fp_digest": msg_fp_digest})

    base_seq = state["seq"]
    st = LS.as_of(state, base_seq)
    sentences = LC.session_sentences(stored, ns)
    base["sentences"] = sentences
    prompt_on = LC.render_prompt(prompt_text, allow_summary_edit)
    pfp = WC.sha256_hex(prompt_on)

    # ---- 初判
    ranked = shortlister(st, stored, config)
    q1req = LC.build_request(st, ns, ranked, stored, sentences, config, store.get_message, prompt_text, allow_summary_edit)
    q1_task_fp = _fp({"q": "l1", "prompt": pfp, "split": SPLIT_VERSION, "base_seq": base_seq, "msgs": msg_fp_digest, "cards": q1req["cards_text"], "sw": bool(allow_summary_edit),
                      "strict": bool(strict_grouping)})
    base["requests"]["q1"] = q1req
    c1 = CV.Ctx(sentences, q1req["shown_threads"], q1req["shown_occasions"], q1req["summaries"], allow_summary_edit=allow_summary_edit, strict_grouping=strict_grouping)
    a1 = ASK.ask(store, ns, step_id, "q1", q1req, q1_task_fp, lambda raw: CV.convert(raw, c1), judge, _Cap(MAX_SENDS - 1), base_seq, k, session_id)
    n1 = len(store.list_judgments(ns, step_id, "q1"))
    base["q1_sends"], base["reused_q1"] = n1, a1["reused"]
    if a1["status"] == "model_failed":
        return dict(base, status="model_failed", error=a1["error"])
    if a1["status"] == "failed":
        return dict(base, status="q1_failed", reasons=a1["reasons"], error=a1["error"])
    q1 = a1["result"].data
    base["proposals"] = list(q1["summary_edits"])
    accepted_q1 = store.list_judgments(ns, step_id, "q1")[a1["no"] - 1]

    # ---- 补找（一次会话的缺口合并成一次）和重判
    supp, q2, q2req, q2_task_fp = [], None, None, None
    base["q2_sends"] = 0
    if q1["gaps"]:
        supp = SL.supplement(st, base_seq, q1["gaps"], q1req["shown_threads"], q1req["shown_occasions"], config)
        base["supp"] = supp
        if any(item["found"] for item in supp):
            gap_sids = []
            for g in q1["gaps"]:
                for sid in g["sentences"]:
                    if sid not in gap_sids:
                        gap_sids.append(sid)
            by_sid = {s["sid"]: s for s in sentences}
            gap_sentences = [by_sid[sid] for sid in sorted(gap_sids, key=lambda x: int(x[1:]))]
            q2req = LC.build_rejudge_request(st, ns, q1req, prompt_text, sentences, gap_sentences, supp, config)
            base["requests"]["q2"] = q2req
            q2_task_fp = _fp({"q": "l2", "prompt": WC.sha256_hex(LC.render_prompt(prompt_text, False)), "split": SPLIT_VERSION, "base_seq": base_seq, "q1": _fp(q1), "supp": supp,
                              "text": WC.sha256_hex(q2req["text"])})
            c2 = CV.Ctx(sentences, q1req["shown_threads"], q1req["shown_occasions"], allow_summary_edit=False, phase="rejudge", judge_sids=q2req["judge_sids"],
                        found_threads=q2req["found_threads"], found_occasion_map=q2req["found_occasion_map"], strict_grouping=strict_grouping)
            a2 = ASK.ask(store, ns, step_id, "q2", q2req, q2_task_fp, lambda raw: CV.convert(raw, c2), judge, _Cap(MAX_SENDS - n1), base_seq, k, session_id)
            base["q2_sends"] = len(store.list_judgments(ns, step_id, "q2"))
            base["reused_q2"] = a2["reused"]
            if a2["status"] == "model_failed":
                return dict(base, status="model_failed", error=a2["error"])
            if a2["status"] == "failed":
                return dict(base, status="q2_failed", reasons=a2["reasons"], error=a2["error"])
            q2 = a2["result"].data

    # ---- 规划、提交：到这里模型输出已经合格，再出问题就是装置问题
    input_fp = _fp({"q1": q1_task_fp, "q2": q2_task_fp, "msgs": msg_fp_digest})
    info = {"q1_sends": base["q1_sends"], "q2_sends": base["q2_sends"], "input_fp": input_fp, "msg_fp_digest": msg_fp_digest, "q1_request_fp": accepted_q1.get("request_fp"),
            "rejudged": q2 is not None}
    try:
        cs, receipt = plan_changeset(st, ns, step_id, session_id, k, base_seq, sentences, q1, q2, supp, info)
    except Exception as e:                                                       # noqa: BLE001
        if q2 is not None:
            store.update_judgment(ns, step_id, "q2", base["q2_sends"], {"status": "apparatus_error", "error": "规划出错：%r" % (e,)})
        else:
            store.update_judgment(ns, step_id, "q1", a1["no"], {"status": "apparatus_error", "error": "规划出错：%r" % (e,)})
        return dict(base, status="apparatus_error", error="规划出错：%r" % (e,))
    out = _commit(store, ns, key, cs, input_fp, base)
    out["gap_rows"] = cs["inserts"]["gaps"]
    return out


def _commit(store, ns, key, cs, input_fp, base):
    status, rcpt, err = U.commit_with_recovery(store, ns, cs, key, lambda r: r.get("input_fp") == input_fp)
    if isinstance(err, WS.StaleBase):
        raise err
    if status == "commit_failed":
        if isinstance(err, (LS.LightInvalid, WS.KeyConflict)):
            return dict(base, status="apparatus_error", error="存储拒收了规划出的变更集：%s" % (err,))
        return dict(base, status="commit_failed", error=repr(err))
    return dict(base, status="committed", receipt=rcpt, recovered_uncertain_commit=(status == "recovered"))


# ---------------------------------------------------------------- 规划
def plan_changeset(st, ns, step_id, session_id, k, base_seq, sentences, q1, q2, supp, info):
    """把初判（和重判）的结果合成一份完整的变更集和收据。st 必须是 base_seq 时点的状态。"""
    seq = st["seq"] + 1
    ins = {t: [] for t in LS.TABLES}

    def add(table, **rec):
        rec["created_seq"] = seq
        if table != "receipts":
            rec["key"] = LS.key_for(table, rec)
        ins[table].append(rec)
        return rec

    n_th, n_oc, n_gp = (LS.next_number(st, kd) for kd in ("thread", "occasion", "gap"))
    tmap = {1: {}, 2: {}}
    nmap = {1: {}, 2: {}}
    occ_thread = LS.occasion_thread(st)
    created_threads = []
    for phase, data in ((1, q1), (2, q2)):
        if data is None:
            continue
        for t in data["new_threads"]:
            tid = LS.thread_id_for(ns, n_th)
            n_th += 1
            tmap[phase][t["ref"]] = tid
            add("threads", thread_id=tid, created_by=step_id)
            add("thread_names", thread_id=tid, version=1, name=t["name"], by=step_id)
            add("thread_descs", thread_id=tid, version=1, text=t["desc"], by=step_id)
            created_threads.append(tid)
        for n in data["new_occasions"]:
            oid = LS.occasion_id_for(ns, n_oc)
            n_oc += 1
            nmap[phase][n["ref"]] = oid
            parent = tmap[phase].get(n["thread"], n["thread"])
            occ_thread[oid] = parent
            add("occasions", occasion_id=oid, thread_id=parent, created_by=step_id)
            add("occ_titles", occasion_id=oid, version=1, title=n["title"], by=step_id)
            add("occ_summaries", occasion_id=oid, version=1, text=n["summary"], by=step_id, reason="", evidence_sentences=[], request_fp=None)

    def real_t(phase, x):
        return tmap[phase].get(x, x)

    def real_o(phase, x):
        return nmap[phase].get(x, x)

    by_sid = {s["sid"]: s for s in sentences}
    final = {}                                   # sid -> [(phase, dest)]
    for sid, ds in q1["placements"].items():
        final[sid] = [(1, d) for d in ds]
    if q2 is not None:
        for sid, ds in q2["placements"].items():
            final[sid] = [(2, d) for d in ds]
    # ---- 缺口：每个初判缺口按结局分行
    gap_rows, unresolved = [], {}
    for g in q1["gaps"]:
        found = []
        for item in supp:
            if item["sentences"] == g["sentences"] and item["layer"] == g["layer"] and item["words"] == g["words"]:
                found = list(item["found"])
                break
        known = real_t(1, g["known_thread"]) if g["known_thread"] is not None else None
        done = [sid for sid in g["sentences"] if sid in final]
        left = [sid for sid in g["sentences"] if sid not in final]
        groups = []
        if done:
            groups.append((done, "resolved"))
        if left:
            groups.append((left, "unresolved_after_rejudge" if q2 is not None else "unresolved_no_candidate"))
        for sids, outcome in groups:
            add("gaps", gap_id=LS.gap_id_for(ns, n_gp), sentence_ids=[by_sid[x]["fragment_id"] for x in sids], layer=g["layer"], known_thread=known, words=list(g["words"]),
                found=found, outcome=outcome, created_by=step_id)
            n_gp += 1
            if outcome != "resolved":
                for sid in sids:
                    unresolved[sid] = (known, outcome, g["words"])
    # ---- 句子和挂接
    for i, s in enumerate(sentences, 1):
        add("sentences", sentence_id=s["fragment_id"], msg_id=s["msg_id"], session_id=session_id, start=s["start"], end=s["end"], text=s["text"], role=s["role"], date=s["date"],
            ord=i, created_by=step_id)
    touched = set()
    for s in sentences:
        sid, fid = s["sid"], s["fragment_id"]
        if sid in unresolved:
            known, outcome, words = unresolved[sid]
            note = (GAP_NO_CANDIDATE if outcome == "unresolved_no_candidate" else GAP_AFTER_REJUDGE) + "（词：%s）" % "、".join(words)
            if known is not None:
                add("attachments", sentence_id=fid, kind="thread_pending", target_id=known, confidence=None, candidates=[], reason=note, created_by=step_id)
                touched.add(known)
            else:
                add("attachments", sentence_id=fid, kind="thread_unknown", target_id=None, confidence=None, candidates=[], reason=note, created_by=step_id)
            continue
        for phase, d in final[sid]:
            kind, tgt = d["kind"], d["target"]
            cands = list(d["candidates"])
            if kind == "occasion":
                tgt = real_o(phase, tgt)
                touched.add(occ_thread[tgt])
            elif kind in ("thread_summary", "thread_pending"):
                tgt = real_t(phase, tgt)
                touched.add(tgt)
                cands = [real_o(phase, c) for c in cands]
            elif kind == "thread_unknown":
                cands = [real_t(phase, c) for c in cands]
            add("attachments", sentence_id=fid, kind=kind, target_id=tgt, confidence=d["confidence"], candidates=cands, reason=d["reason"], created_by=step_id)
    # ---- 改摘要（提议在初判里，提交成功才生效）
    cur = {}
    for r in st["tables"]["occ_summaries"]:
        cur[r["occasion_id"]] = max(cur.get(r["occasion_id"], 0), r["version"])
    for e in q1["summary_edits"]:
        add("occ_summaries", occasion_id=e["occasion"], version=cur[e["occasion"]] + 1, text=e["text"], by=step_id, reason=e["reason"],
            evidence_sentences=[by_sid[x]["fragment_id"] for x in e["evidence"]], request_fp=info["q1_request_fp"])
    receipt = {"key": "step:" + step_id, "kind": "step", "step_id": step_id, "session_id": session_id, "k": k, "base_seq": base_seq, "commit_seq": seq,
               "msg_fp_digest": info["msg_fp_digest"], "input_fp": info["input_fp"], "touched_threads": sorted(touched), "q1_sends": info["q1_sends"], "q2_sends": info["q2_sends"],
               "sentences": len(sentences), "attachments": len(ins["attachments"]), "gaps": len(ins["gaps"]), "rejudged": bool(info["rejudged"]),
               "new_threads": len(created_threads), "new_occasions": len(ins["occasions"]),
               "summary_edits_proposed": len(q1["summary_edits"]), "summary_edits_effective": len(q1["summary_edits"]), "created_seq": seq}
    ins["receipts"].append(receipt)
    cs = {"ns": ns, "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": ins, "updates": []}
    return cs, receipt


# ---------------------------------------------------------------- 显式操作
def apply_operation(store, ns, op, clock):
    """放弃一步（abandon_step）：对一个还没有收据的步记一张「已放弃」收据，只写这张收据；原话留在原话层，这一步不整理；
    k 必须是下一个步序号；对已提交或已放弃的步拒绝；同一操作重放幂等，同 ID 不同内容报 OpConflict。"""
    st = store.read_state(ns)
    if isinstance(op, dict) and isinstance(op.get("op_id"), str):
        rc = WS.find_receipt(st, "op:" + op["op_id"])
        if rc is not None:
            if rc["op_fp"] != _fp(op):
                raise WS.OpConflict("操作 ID %r 已用于另一个内容不同的操作" % op["op_id"])
            return {"status": "replayed", "receipt": rc, "reasons": []}
    if not isinstance(op, dict) or op.get("op") != "abandon_step":
        return {"status": "rejected", "receipt": None, "reasons": [{"code": "bad_op", "detail": "只支持 abandon_step"}]}
    need = ("op_id", "operator", "step_id", "session_id", "k", "reason", "msg_fp_digest")
    miss = [x for x in need if x not in op]
    if miss:
        return {"status": "rejected", "receipt": None, "reasons": [{"code": "missing_key", "detail": "缺少 %s" % miss}]}
    binding = store.get_step_binding(ns, op["step_id"])
    if binding is None:
        raise WS.StepConflict("要放弃的步 %r 没有步绑定（没有开始过）" % op["step_id"])
    reasons = []
    if binding["session_id"] != op["session_id"] or binding["k"] != op["k"] or binding["msg_fp_digest"] != op["msg_fp_digest"]:
        reasons.append({"code": "binding_mismatch", "detail": "操作的会话、k 或原话指纹和步绑定对不上"})
    if WS.find_receipt(st, "step:" + op["step_id"]) is not None or WS.abandon_receipt(st, op["step_id"]) is not None:
        reasons.append({"code": "step_ended", "detail": "这一步已经提交或已经放弃，不能再放弃"})
    expected_k = WS.expected_k(st)
    if op["k"] != expected_k:
        reasons.append({"code": "bad_k", "detail": "k 必须是下一个步序号 %d" % expected_k})
    if not isinstance(op["reason"], str) or not op["reason"].strip():
        reasons.append({"code": "bad_reason", "detail": "放弃必须写原因"})
    if reasons:
        return {"status": "rejected", "receipt": None, "reasons": reasons}
    seq = st["seq"] + 1
    receipt = {"key": "op:" + op["op_id"], "kind": "op", "op": "abandon_step", "op_id": op["op_id"], "op_fp": _fp(op), "step_id": op["step_id"], "session_id": op["session_id"],
               "k": op["k"], "reason": op["reason"], "operator": op["operator"], "base_seq": st["seq"], "commit_seq": seq, "created_seq": seq,
               "msg_fp_digest": op["msg_fp_digest"]}
    cs = {"ns": ns, "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": {"receipts": [receipt]}, "updates": []}
    status, rcpt, err = U.commit_with_recovery(store, ns, cs, "op:" + op["op_id"], lambda r: r["op_fp"] == _fp(op))
    if status == "commit_failed":
        return {"status": "commit_failed", "receipt": None, "reasons": [], "error": repr(err)}
    return {"status": "committed", "receipt": rcpt, "reasons": [], "recovered_uncertain_commit": status == "recovered"}
