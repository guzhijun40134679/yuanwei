"""取材：命中 → 主线优先装第一遍 → 补同一回 → 按时间呈现整条原话（参照档 16,000／24,000）。

逐函数取自研究（交_朴素、交_主题、同回_核心、少_核心；对应关系见 docs/复制关系表.md），改成不依赖研究装置、不读金标：
  * 输入是「截点内的消息行」{msg_id, session_id, seq, role, date, content}、命中序（按消息去重，沿融合名次）、
    消息→主线、消息→回两张表（由存储的挂接算出，见 recall.attach_maps）；
  * 装入：按候选次序一条条加，整条放不下就停（参照档的规则）；额度是估算词元（每行 ceil(0.52×(码点数＋1))），套首尾边界行以后数；
  * 材料文字：开头一句说明、【原文】、会话行（会话或日期变了插一行）、〔角色｜消息编号〕＋编码后的原话。
"""
from fractions import Fraction

from . import encoding as EN
from . import rank as RK
from .util import est_tokens_line

OPENING_PARTIAL = "以下是按检索挑出的一部分原话，不是全部记录。"
LINKS = ((1, +1), (2, -1), (3, +2), (4, -2))           # （链序, 偏移）：后一条、前一条、后二、前二
ROLE_CN = {"user": "用户", "assistant": "助手"}
SEG_THEME, SEG_POOL, SEG_OTHER, SEG_FIRST, SEG_LEFT = "主线命中及邻句", "同回池", "其他", "G1已选", "其余候选"
NONE_DATE = "日期未知"


class PackError(Exception):
    pass


def est(text):
    return sum(est_tokens_line(x) for x in text.split("\n"))


def model_tokens(material):
    """交给模型的那份：套首尾边界行以后的估算词元。"""
    return est(EN.wrap_material(material))


# ================================================================ 会话里的前后位置
class SessionIndex:
    """同一次会话里的前后位置。邻句只在同一次会话里：越过会话两头给 None。"""

    def __init__(self, rows):
        self.by_session, self.pos, self.session_of = {}, {}, {}
        for r in sorted(rows, key=lambda r: r["seq"]):
            self.by_session.setdefault(r["session_id"], []).append(r["msg_id"])
            self.session_of[r["msg_id"]] = r["session_id"]
        for sid, ids in self.by_session.items():
            for i, m in enumerate(ids):
                self.pos[m] = (sid, i)

    def neighbor(self, mid, offset):
        sid, i = self.pos[mid]
        j = i + offset
        lst = self.by_session[sid]
        return lst[j] if 0 <= j < len(lst) else None

    def assert_same_session(self, a, b):
        if self.session_of[a] != self.session_of[b]:
            raise PackError("邻句 %s 和命中 %s 不在同一次会话里：拒绝" % (b, a))


def hits_from_table(table, anchor_msg, depth):
    """候选全表（[名次, 锚点编号, 关键词名次, 向量名次]，已按融合名次排好）前 depth 个锚点 → 按消息去重、保留名次的命中列表。"""
    out, seen = [], set()
    for row in table[:depth]:
        m = anchor_msg[row[1]]
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ================================================================ 主题（主线）
def themes_of(tmap, mid):
    return tmap.get(mid, frozenset())


def choose_t1(hits, tmap):
    """T1＝命中排第 1 的那条消息所属的主线（多条主线就都算）；第一名没有主线就是空的，不往后找。"""
    return themes_of(tmap, hits[0]) if hits else frozenset()


def order_candidates(hits, sidx, nbr, sel, tmap):
    """装入次序（排后）：选中主线里的命中（连它的邻句）先按名次装，其他命中（连邻句）在后。返回 [(消息, 它跟着的命中, 是不是主题内)]，按首次出现去重。"""
    sel = frozenset(sel)

    def in_theme(m):
        return bool(themes_of(tmap, m) & sel)
    hit_theme = [h for h in hits if in_theme(h)]
    hit_other = [h for h in hits if not in_theme(h)]
    seen, main = set(), []

    def add(m, parent, flag):
        if m not in seen:
            seen.add(m)
            main.append((m, parent, flag))

    def group(group_hits, flag):
        for h in group_hits:
            add(h, h, flag)
            for _o, off in LINKS:
                if abs(off) > nbr:
                    continue
                n = sidx.neighbor(h, off)
                if n is None:
                    continue
                sidx.assert_same_session(h, n)
                add(n, h, flag)
    group(hit_theme, True)
    group(hit_other, False)
    return main


# ================================================================ 材料文字和估算
class Meter:
    """逐条加消息时的估算词元，不用每次重拼整份：材料的行是 开头说明、【原文】、（会话变了就加）会话行、引用行，估算词元按行相加；再加首尾两条边界行。
    最后一定要和「整份拼出来再数」的结果相等，不等就是装置错误。"""

    def __init__(self, rows_by_id, mp, opening=OPENING_PARTIAL):
        self.rows_by_id, self.mp = rows_by_id, mp
        s_, e_ = EN.boundary("x")[:2]
        self.base = est_tokens_line(opening) + est_tokens_line("【原文】") + est_tokens_line(s_) + est_tokens_line(e_)
        self._q, self._s = {}, {}

    def q(self, mid):
        if mid not in self._q:
            r = self.rows_by_id[mid]
            self._q[mid] = est_tokens_line("〔%s｜%s〕%s" % (ROLE_CN[r["role"]], mid, self.mp.encode(r["content"])))
        return self._q[mid]

    def s(self, r):
        key = (r["session_id"], r.get("date") or NONE_DATE)
        if key not in self._s:
            self._s[key] = est_tokens_line("会话 %s｜%s" % key)
        return self._s[key]

    def total(self, mids):
        rows = sorted((self.rows_by_id[m] for m in mids), key=lambda r: r["seq"])
        n, cur = self.base, None
        for r in rows:
            key = (r["session_id"], r.get("date") or NONE_DATE)
            if key != cur:
                n += self.s(r)
                cur = key
            n += self.q(r["msg_id"])
        return n


def conflict_chars(rows, mp):
    return sorted({c for r in rows for c in r["content"] if c in mp.rep_to_orig})


def build_material(rows_sel, mp, opening=OPENING_PARTIAL):
    """选中的消息（任意次序）→ 材料（编码后，不含边界行）。"""
    rows = sorted(rows_sel, key=lambda r: r["seq"])
    bad = conflict_chars(rows, mp)
    if bad:
        return {"material": None, "conflict": bad, "rows": rows}
    lines, cur = [opening, "【原文】"], None
    for r in rows:
        key = (r["session_id"], r.get("date") or NONE_DATE)
        if key != cur:
            lines.append("会话 %s｜%s" % key)
            cur = key
        lines.append("〔%s｜%s〕%s" % (ROLE_CN[r["role"]], r["msg_id"], mp.encode(r["content"])))
    return {"material": "\n".join(lines), "conflict": [], "rows": rows}


def read_back(material, enc_content, opening=OPENING_PARTIAL):
    """成品材料 → {"order","text","sessions"}。逐行语法：开头说明、【原文】、会话行、引用行。每条引用行的原话必须等于编码后的原话。"""
    lines = material.split("\n")
    if len(lines) < 2 or lines[0] != opening or lines[1] != "【原文】":
        raise PackError("开头两行不是固定的（开头说明、【原文】）")
    order, text, sessions, cur_session = [], {}, [], False
    for n, line in enumerate(lines[2:], 3):
        if line.startswith("会话 ") and "｜" in line and not line.startswith("〔"):
            sid, _, date = line[len("会话 "):].partition("｜")
            sessions.append((sid, date))
            cur_session = True
            continue
        if line.startswith("〔"):
            head, sep, body = line.partition("〕")
            if not sep or head.count("〔") != 1 or "｜" not in head:
                raise PackError("第 %d 行：引用行格式不对" % n)
            role, _, mid = head[1:].partition("｜")
            if role not in ("用户", "助手"):
                raise PackError("第 %d 行：角色不认识" % n)
            c = enc_content(mid)
            if c is None:
                raise PackError("第 %d 行：读不到原话 %s" % (n, mid))
            if body != c:
                raise PackError("第 %d 行：消息 %s 的引用和原话不一样" % (n, mid))
            if not cur_session:
                raise PackError("第 %d 行：引用行前面没有会话行" % n)
            order.append(mid)
            text[mid] = [[0, len(c)]]
            continue
        raise PackError("第 %d 行不是已知的行种类" % n)
    return {"order": order, "text": text, "sessions": sessions}


def select_ordered(cands, rows_by_id, mp, budget):
    """按给定次序一条条装，装不下就停。返回 {状态, 选中, 材料, tokens, 停在, 候选数}。额度按套首尾边界行以后的估算词元。"""
    for m, _p, _f in cands:
        bad = sorted({c for c in rows_by_id[m]["content"] if c in mp.rep_to_orig})
        if bad:
            return {"状态": "conflict", "选中": [], "材料": None, "tokens": None, "停在": m, "候选数": len(cands), "冲突字符": bad}
    meter = Meter(rows_by_id, mp)
    chosen, stop_at, flags = [], None, {}
    for m, _p, f in cands:
        if m in flags:
            continue
        if meter.total(chosen + [m]) <= budget:
            chosen.append(m)
            flags[m] = f
        else:
            stop_at = m
            break
    base = {"状态": "ok", "停在": stop_at, "候选数": len(cands)}
    if not chosen:
        return dict(base, 选中=[], 材料=None, tokens=0)
    b = build_material([rows_by_id[x] for x in chosen], mp)
    n = model_tokens(b["material"])
    if n != meter.total(chosen) or n > budget:
        raise PackError("逐条累计的估算词元 %d 和整份拼出来数的 %d 不一样，或超过额度 %d" % (meter.total(chosen), n, budget))
    return dict(base, 选中=chosen, 材料=b["material"], tokens=n)


# ================================================================ 池
def invert(m2o):
    o2m = {}
    for m, os_ in m2o.items():
        for o in os_:
            o2m.setdefault(o, set()).add(m)
    return o2m


def dedup_segments(parts):
    """parts：[(分段名, [(消息, 父命中, 标记)…])…] → (按第一次出现去重的序列, [[分段名, 条数]…])。"""
    seen, seq, segs = set(), [], []
    for name, items in parts:
        n = 0
        for c in items:
            if c[0] not in seen:
                seen.add(c[0])
                seq.append(c)
                n += 1
        segs.append([name, n])
    return seq, segs


def occasion_groups(hits_theme, m2o, o2m, visible):
    """选中主线里的命中各自所在的「一回」：按命中名次、再按一回的编号排，同一回只出现一次，只含截点以内（visible）的消息，一回里的消息按编号排。"""
    groups, pos = [], {}
    for h in hits_theme:
        for o in sorted(m2o.get(h, ())):
            if o not in pos:
                pos[o] = len(groups)
                groups.append({"occ": o, "hits": [], "msgs": sorted(m for m in o2m.get(o, ()) if m in visible)})
            groups[pos[o]]["hits"].append(h)
    return groups


class ReSearcher:
    """在一回的消息里再检索一次。anchors：锚点（.num .doc .msg_id .seq）；一条消息的关键词分＝它的锚点里最高的 BM25（0 分的不进这一路，统计只在这一回的锚点上算）；
    混合模式再加向量分（它的锚点里最高的余弦，这一回的全部消息都排），两路按 RRF（k＝60）合并，同分 seq 大的在前；关键词模式只有第一路。
    没有锚点的消息排在最后，保持传进来的次序。混合模式缺向量就报错，不退回关键词。"""

    def __init__(self, anchors, doc_vec=None, query_vec=None, partial=False):
        self.partial = partial                       # 混合模式缺向量时：跳过没有向量的锚点、算不出的查询向量，不报错（只在 vector_fallback＝partial 时为真）
        self.by_msg = {}
        for a in anchors:
            self.by_msg.setdefault(a.msg_id, []).append(a)
        self.doc_vec, self.query_vec = doc_vec, query_vec

    @property
    def hybrid(self):
        return self.doc_vec is not None

    def detail(self, msgs, query):
        msgs = list(msgs)
        anchors = [a for m in msgs for a in self.by_msg.get(m, ())]
        if not anchors:
            return {"kw": [], "vec": [], "rrf": []}
        seq = {a.msg_id: a.seq for a in anchors}
        by_order = {a.num: a.msg_id for a in anchors}
        kw = {}
        for order, s in RK.bm25_rank(anchors, query):
            m = by_order[order]
            if m not in kw or s > kw[m]:
                kw[m] = s
        kw_list = sorted(kw.items(), key=lambda x: (-x[1], -seq[x[0]]))
        vec_list = []
        if self.hybrid:
            import numpy as np
            qv = self.query_vec(query)
            sims = {}
            for a in anchors:
                if qv is None and self.partial:
                    break
                dv = self.doc_vec(a)
                if dv is None and self.partial:
                    continue
                if dv is None:
                    raise PackError("锚点 %s 没有向量：不退回关键词" % (getattr(a, "anchor_id", a.num),))
                s = float(np.dot(dv, qv))
                if a.msg_id not in sims or s > sims[a.msg_id]:
                    sims[a.msg_id] = s
            vec_list = sorted(sims.items(), key=lambda x: (-x[1], -seq[x[0]]))
        score = {}
        for r, (m, _s) in enumerate(kw_list, 1):
            score[m] = score.get(m, Fraction(0)) + Fraction(1, RK.RRF_K + r)
        for r, (m, _s) in enumerate(vec_list, 1):
            score[m] = score.get(m, Fraction(0)) + Fraction(1, RK.RRF_K + r)
        rrf = sorted(score.items(), key=lambda x: (-x[1], -seq[x[0]]))
        return {"kw": kw_list, "vec": vec_list, "rrf": rrf}

    def rank(self, msgs, query):
        msgs = list(msgs)
        d = self.detail(msgs, query)
        ranked = [m for m, _ in d["rrf"]]
        got = set(ranked)
        return ranked + [m for m in msgs if m not in got]


def pool_sequence(groups, searcher, query, placed, cap):
    """同回池里的装入次序：[(消息, 带它的那条命中, True)]。每一回先在回内再搜排好、去掉前面已经排进去的（placed）、再按每回上限截；
    按命中名次轮流取（第 1 名命中那一回的第 1 条、第 2 名那一回的第 1 条……），同一条消息只取一次。"""
    lists, cache = [], {}
    for g in groups:
        k = tuple(g["msgs"])
        if k not in cache:
            cache[k] = searcher.rank(g["msgs"], query)
        lst = [m for m in cache[k] if m not in placed]
        if cap is not None:
            lst = lst[:cap]
        lists.append((g["hits"][0], lst))
    seen, out = set(), []
    for i in range(max((len(l) for _, l in lists), default=0)):
        for h, lst in lists:
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                out.append((lst[i], h, True))
    return out


def pack_reference(hits, sidx, rows_by_id, mp, tmap, m2o, visible, searcher, query, first_budget=16000, total_budget=24000, nbr=1, cap=10, selector=None):
    """参照档（Q1-10／后补24000）：先照 T1 主线优先装满 first_budget，再补同回池（回内再搜、每回最多 cap 条、按命中名次轮流取），最后接其余候选，到 total_budget 为止，装不下就停。
    返回 {"T1","池大小","第一遍":{…},"最终":{…}}，每个是 select_ordered 的结果加 候选序／分段。
    selector：可选，签名同 select_ordered(seq, rows_by_id, mp, budget)；便条用它换成自己的装法（字符数、跳过接着装）。不给就是参照档原来的装法。"""
    sel = choose_t1(hits, tmap)
    o2m = invert(m2o)
    cands1 = order_candidates(hits, sidx, nbr, sel, tmap)
    th_part = [c for c in cands1 if c[2]]
    other = [c for c in cands1 if not c[2]]
    hits_theme = [h for h in hits if themes_of(tmap, h) & sel]
    groups = occasion_groups(hits_theme, m2o, o2m, visible)

    def run(name, budget, parts):
        seq, segs = dedup_segments(parts)
        r = (selector or select_ordered)(seq, rows_by_id, mp, budget)
        if r["状态"] != "ok":
            raise PackError("原话里有编码用的替代字符，装不了：%s" % (r.get("冲突字符"),))
        return dict(r, 装法=name, 额度=budget, 候选序=[c[0] for c in seq], 分段=segs)
    first_pack = run("第一遍", first_budget, [(SEG_THEME, th_part), (SEG_OTHER, other)])
    first = [(m, None, True) for m in first_pack["选中"]]
    placed_first = {c[0] for c in first}
    pool_after = pool_sequence(groups, searcher, query, placed_first, cap)
    final = run("后补", total_budget, [(SEG_FIRST, first), (SEG_POOL, pool_after), (SEG_LEFT, cands1)])
    return {"T1": sel, "池大小": {"同回池": len(pool_after)}, "第一遍": first_pack, "最终": final}
