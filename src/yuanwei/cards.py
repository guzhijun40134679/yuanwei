"""写入变轻·候选卡片和请求文字。

一问同时看两层：候选主线（名称、一句说明、首次和最近提到的日期、最初几句和最近几句原话），每条主线下的候选「回」
（标题、一句摘要、首次和最近提到的日期、最初和最近的原话）。没有栏位、没有追加。
上限：最多 12 条主线、每条 8 回（LightConfig），卡片总长度 16,000 估算词元；超了按写死的顺序让位：先删排名靠后主线的回卡
（主线从后往前，每条里从最后一张删起），回卡全删光以后再删主线卡（从后往前）；删掉的都记在 dropped 里，补找还能找回。
只读状态；不联网、不调模型。"""
import re

from . import contract as WC
from . import lstate as LS  # noqa: F401
from . import split as SP
from . import util as U
from . import view as LV

CUT = "（节选，未完）"


class LightConfig:
    """可调的默认值。粗筛算法和 12×8 的上限沿用现行；卡片总长度上限 16,000 估算词元。"""

    def __init__(self, max_threads=12, max_occasions=8, supplement_max=3, cards_tokens=16000, first_quotes=2, recent_quotes=3, excerpt_chars=60,
                 max_sends=3, recent_sessions=3, w_overlap=1.0, w_recent=0.5, w_open=0.3):
        self.max_threads, self.max_occasions, self.supplement_max = max_threads, max_occasions, supplement_max
        self.cards_tokens, self.first_quotes, self.recent_quotes, self.excerpt_chars = cards_tokens, first_quotes, recent_quotes, excerpt_chars
        self.max_sends, self.recent_sessions = max_sends, recent_sessions
        self.w_overlap, self.w_recent, self.w_open = w_overlap, w_recent, w_open

    def as_dict(self):
        return dict(self.__dict__)


def _tokens(lines):
    return U.est_lines(lines) if lines else 0


def role_zh(role):
    return "用户" if role == "user" else "助手"


# ---------------------------------------------------------------- 提示词（改摘要开关）
def render_prompt(text, allow_summary_edit):
    """开关打开：原文；关掉：去掉【改摘要】一节、输出格式里的「改摘要」栏、例子里的「改摘要」栏（提示词里一个「改摘要」字样都不留）。"""
    if allow_summary_edit:
        return text
    out = re.sub(r"【改摘要】.*?(?=【输出】)", "", text, flags=re.S)
    lines = out.split("\n")
    keep = []
    for ln in lines:
        if '"改摘要"' in ln:
            if keep and keep[-1].rstrip().endswith(","):
                keep[-1] = keep[-1].rstrip()[:-1]
            continue
        keep.append(ln)
    return "\n".join(keep)


# ---------------------------------------------------------------- 句子
def session_sentences(messages, ns):
    """这一次会话切好的句子（编号 s1、s2……），每句多一个 fragment_id（存储里的句子编号）。"""
    out = SP.sentences_of_session(messages)
    for s in out:
        s["fragment_id"] = WC.fragment_id(ns, s["msg_id"], s["start"], s["end"])
    return out


def sentence_line(s):
    return "%s（%s，%s，%s）：%s" % (s["sid"], role_zh(s["role"]), s["msg_id"], s["date"] or "日期未知", s["text"])


def _quote(s, chars):
    t, cut = s["text"], ""
    if len(t) > chars:
        t, cut = t[:chars], CUT
    return "原话（%s，%s，%s）：「%s」%s" % (s["msg_id"], role_zh(s["role"]), s["date"] or "日期未知", t, cut)


def _dates(rows):
    ds = sorted(s["date"] for s in rows if s["date"])
    return (ds[0], ds[-1]) if ds else (None, None)


def _quotes(rows, config):
    first = rows[:config.first_quotes]
    rest = [r for r in rows if r not in first]
    recent = rest[-config.recent_quotes:] if config.recent_quotes > 0 else []
    return first, recent


# ---------------------------------------------------------------- 卡片
class _View:
    def __init__(self, st):
        self.st = st
        self.nm, self.ds, self.tt, self.sm = LV.names(st), LV.descs(st), LV.titles(st), LV.summaries(st)
        self.occs = LV.thread_occasions(st)
        self.tsent, self.osent = LV.thread_sentences(st), LV.occasion_sentences(st)
        self.last = LV.last_changed_seq(st)
        self.occ_last = {}
        for o in st["tables"]["occasions"]:
            self.occ_last[o["occasion_id"]] = o["created_seq"]
        for a in st["tables"]["attachments"]:
            if a["kind"] == "occasion" and a["created_seq"] > self.occ_last.get(a["target_id"], 0):
                self.occ_last[a["target_id"]] = a["created_seq"]
        for tb in ("occ_titles", "occ_summaries"):
            for r in st["tables"][tb]:
                if r["created_seq"] > self.occ_last.get(r["occasion_id"], 0):
                    self.occ_last[r["occasion_id"]] = r["created_seq"]

    def occasion_grams(self, oc):
        parts = [self.tt.get(oc, ""), self.sm.get(oc, "")] + [s["text"] for s in self.osent.get(oc, [])]
        gs = set()
        for t in parts:
            gs |= U.bigrams(t)
        return gs


def thread_card_lines(v, tid, config, label=""):
    rows = v.tsent.get(tid, [])
    d0, d1 = _dates(rows)
    out = ["%s【主线 %s】%s" % (label, tid, v.nm.get(tid, tid)), "  说明：%s" % v.ds.get(tid, "（没有说明）"),
           "  下面有 %d 回；首次提到：%s；最近提到：%s" % (len(v.occs.get(tid, [])), d0 or "无", d1 or "无")]
    first, recent = _quotes(rows, config)
    for lab, group in (("最初", first), ("最近", recent)):
        for s in group:
            out.append("  %s的话：%s" % (lab, _quote(s, config.excerpt_chars)))
    return out


def occasion_card_lines(v, oc, config, label=""):
    rows = v.osent.get(oc, [])
    d0, d1 = _dates(rows)
    out = ["  %s【回 %s】%s（首次提到：%s；最近提到：%s）" % (label, oc, v.tt.get(oc, oc), d0 or "无", d1 or "无"), "    摘要：%s" % v.sm.get(oc, "（没有摘要）")]
    first, recent = _quotes(rows, config)
    for lab, group in (("最初", first), ("最近", recent)):
        for s in group:
            out.append("    %s：%s" % (lab, _quote(s, config.excerpt_chars)))
    return out


class _Block:
    def __init__(self, v, tid, texts, config, label=""):
        self.v, self.tid, self.config, self.label = v, tid, config, label
        qg = set()
        for t in texts:
            qg |= U.bigrams(t)
        self.ranked = sorted(v.occs.get(tid, []), key=lambda oc: (-len(qg & v.occasion_grams(oc)), -v.occ_last.get(oc, 0), oc))
        self.shown = self.ranked[:config.max_occasions]
        self.dropped = [{"kind": "occasion", "occasion_id": oc, "thread_id": tid, "reason": "max_occasions"} for oc in self.ranked[config.max_occasions:]]
        self.head = thread_card_lines(v, tid, config, label)
        self.cards = {oc: occasion_card_lines(v, oc, config) for oc in self.shown}

    def hidden(self):
        return len(self.ranked) - len(self.shown)

    def lines(self):
        out = list(self.head)
        for oc in self.shown:
            out += self.cards[oc]
        if self.hidden():
            out.append("  另有 %d 回没列出" % self.hidden())
        return out


def candidate_blocks(st, ranked_ids, texts, config):
    """候选主线块（最多 config.max_threads 条）和因条数上限被去掉的主线。返回 (blocks, dropped)。"""
    v = _View(st)
    cands = [t for t in ranked_ids if t in v.nm or t in {r["thread_id"] for r in st["tables"]["threads"]}]
    keep = cands[:config.max_threads]
    dropped = [{"kind": "thread", "thread_id": t, "reason": "max_threads"} for t in cands[config.max_threads:]]
    return [_Block(v, t, texts, config) for t in keep], dropped, v


def _fit(blocks, dropped, limit):
    """让位：先删排名靠后主线的回卡，再删主线卡。"""
    def total():
        return sum(_tokens(b.lines()) for b in blocks)
    n = total()
    while n > limit:
        victim = next((b for b in reversed(blocks) if b.shown), None)
        if victim is None:
            break
        oc = victim.shown.pop()
        victim.cards.pop(oc)
        dropped.append({"kind": "occasion", "occasion_id": oc, "thread_id": victim.tid, "reason": "token_budget"})
        n = total()
    while n > limit and blocks:
        b = blocks.pop()
        dropped.append({"kind": "thread", "thread_id": b.tid, "reason": "token_budget"})
        n = total()
    return n


def build_request(st, ns, ranked_ids, messages, sentences, config, get_message, prompt_text, allow_summary_edit=True):
    """初判的请求。st 必须是 base_seq 时点的状态。返回请求 dict（还没有指纹、没有重问反馈）。
    prompt_text：提示词原文（本函数按开关去掉改摘要一节）。"""
    prompt = render_prompt(prompt_text, allow_summary_edit)
    today = next((s["date"] for s in sentences if s["date"]), None)
    texts = [s["text"] for s in sentences]
    blocks, dropped, v = candidate_blocks(st, ranked_ids, texts, config)
    dropped = list(dropped) + [d for b in blocks for d in b.dropped]
    tokens = _fit(blocks, dropped, config.cards_tokens)
    lines = [l for b in blocks for l in b.lines()] or ["（档案里还没有主线）"]
    cards_text = "\n".join(lines)
    text = "%s\n\n本次会话日期：%s\n候选主线：\n%s\n\n这次对话：\n%s" % (prompt.rstrip("\n"), today or "未知", cards_text, "\n".join(sentence_line(s) for s in sentences))
    return {"question": "q1", "phase": "initial", "text": text, "cards_text": cards_text, "shown_threads": [b.tid for b in blocks],
            "shown_occasions": {oc: b.tid for b in blocks for oc in b.shown}, "dropped": dropped, "not_listed": {b.tid: b.hidden() for b in blocks if b.hidden()},
            "cards_tokens": tokens if blocks else 0, "total_tokens": _tokens(text.split("\n")), "session_date": today, "allow_summary_edit": bool(allow_summary_edit),
            "summaries": {oc: v.sm.get(oc, "") for b in blocks for oc in b.shown}}


REJUDGE_NOTE = ("【重判】上一次你把下面这些句子写进了「缺口」，系统在这次会话开始前的档案里又找了一次，找回的候选写在下面，都还没有确认。\n"
                "只处理「要重判的句子」，其余句子已经有去处，不用再写。每个要重判的句子必须出现在「去处」或「缺口」里，恰好一次。\n"
                "编号只能用「候选主线」「补找回来的候选」里给你看过的，或本次新建的临时代号。\n"
                "候选不对就仍然写进「缺口」，系统会按已知范围记为存疑，不会再找第三次。")


def build_rejudge_request(st, ns, initial_req, prompt_text, sentences, gap_sentences, supp, config):
    """重判的请求：初判时展示的候选卡片、补找回来的候选（标「还没确认」）、整场对话（供理解）、要重判的句子。st 是 base_seq 时点的状态。
    返回请求 dict；found_threads／found_occasions 是补找回来的编号（重判可以用）。"""
    prompt = render_prompt(prompt_text, False)
    v = _View(st)
    found_t, found_o = [], []
    for item in supp:
        for f in item["found"]:
            if item["layer"] == "主线" and f not in found_t:
                found_t.append(f)
            if item["layer"] == "回" and f not in found_o:
                found_o.append(f)
    sup_lines, occ_map = [], {}
    for t in found_t:
        sup_lines += thread_card_lines(v, t, config, "【补找回来，还没确认】")
        for oc in v.occs.get(t, [])[:config.max_occasions]:
            sup_lines += occasion_card_lines(v, oc, config)
            occ_map[oc] = t
    for oc in found_o:
        th = next((o["thread_id"] for o in st["tables"]["occasions"] if o["occasion_id"] == oc), "")
        if oc not in occ_map:
            sup_lines += occasion_card_lines(v, oc, config, "【补找回来，还没确认；属于主线 %s】" % th)
        occ_map[oc] = th
    gap_by_sid = {}
    for item in supp:
        for sid in item["sentences"]:
            gap_by_sid.setdefault(sid, item)
    gap_lines = []
    by_id = {s["sid"]: s for s in sentences}
    for s in gap_sentences:
        item = gap_by_sid.get(s["sid"])
        words = "、".join(item["words"]) if item else ""
        gap_lines.append("%s　（补找用词：%s）" % (sentence_line(by_id[s["sid"]]), words))
    today = initial_req["session_date"]
    text = "%s\n\n%s\n\n本次会话日期：%s\n候选主线：\n%s\n\n补找回来的候选（还没确认）：\n%s\n\n这次对话（供理解）：\n%s\n\n要重判的句子：\n%s" % (
        prompt.rstrip("\n"), REJUDGE_NOTE, today or "未知", initial_req["cards_text"], "\n".join(sup_lines) or "（没有）",
        "\n".join(sentence_line(s) for s in sentences), "\n".join(gap_lines))
    return {"question": "q2", "phase": "rejudge", "text": text, "found_threads": found_t, "found_occasions": found_o, "found_occasion_map": occ_map,
            "judge_sids": [s["sid"] for s in gap_sentences], "total_tokens": _tokens(text.split("\n")), "session_date": today}
