"""写入变轻·模型输出的转换和校验（中文字段 → 内部结构；拒收理由）。

这一层是「模型输出」的关口：下面任何一条不过，都算模型输出不合格（格式不合格，可以在次数内带着理由重问）；过了才交给规划。
照现有转换（layer_convert）的做法：未知键、重复键、类型不对都拒收，所有输入（任何类型、任何形状）只返回理由、不崩。
JSON 的读法、键检查、小件直接复用 layer_convert 的函数（只读复用）。

输入格式见 提示词-轻写-草稿.txt：{"新建": {"主线", "回"}, "去处": [...], "缺口": [...], "改摘要": [...]}。
  每个要判断的句子必须在「去处」或「缺口」里恰好出现一次；编号只能用本步展示过的，或本次新建的临时代号（主线 T 加数字，回 N 加数字）；
  新建的临时代号必须被引用；新回至少要有一句「确定」挂在它下面；日常、不记不和别的去处同时出现；
  「可能」、回待定、主线待定必须写理由；改摘要只许改本步展示过的已有回，同一回一步只改一次，摘要和理由不能空，
  依据句只核是不是本步的句子编号（可以为空），不做逐字核对。
  一条去处可以写好几句；这些句子可以跨消息、跨说话人（提示词自己的例子就这样写）：默认按句拆开、记一条备注；strict_grouping=True 时拒收。
  重判（phase=rejudge）：只处理 judge_sids 里的句子；可以用补找回来的编号；输出里没有「改摘要」这一栏；仍写缺口算通过（系统按存疑记）。
data = {"new_threads": [{ref,name,desc}], "new_occasions": [{ref,thread,title,summary}], "placements": {sid: [dest...]},
        "gaps": [{sentences,layer,known_thread,words}], "summary_edits": [{occasion,text,reason,evidence}]}
  dest = {"kind": occasion|thread_summary|thread_pending|thread_unknown|daily|skip, "target", "confidence", "candidates", "reason"}。
"""
import re

from . import convhelp as LCV

FENCE_NOTE = LCV.FENCE_NOTE
SELF_ERROR = "转换自己出错："
Result = LCV.Result
_short, _ok_str, _str_list, _parse, _keys, _has_surrogate = LCV._short, LCV._ok_str, LCV._str_list, LCV._parse, LCV._keys, LCV._has_surrogate
RE_T, RE_N = re.compile(r"T[0-9]+$"), re.compile(r"N[0-9]+$")

TOP = ("新建", "去处", "缺口", "改摘要")
TOP_REQ = ("新建", "去处", "缺口")
TOP_REJUDGE = ("新建", "去处", "缺口")
NEW = ("主线", "回")
NEW_T = ("代号", "名称", "说明")
NEW_N = ("代号", "主线", "标题", "摘要")
PLACE = ("句", "挂", "日常", "不记")
GAP = ("句", "层", "已知主线", "词")
EDIT = ("回", "摘要", "理由", "依据句")


class Ctx:
    """转换和校验用的上下文。
    sentences：这一步全部句子 [{sid,msg_id,role,...}]；judge_sids：要判断的句子编号（初判不给＝全部；重判只给缺口句子）；
    shown_threads：展示过的主线编号；shown_occasions：{展示过的回: 它的主线}；summaries：{展示过的回: 当前摘要}；
    allow_summary_edit：改摘要开关；phase：initial／rejudge；found_threads、found_occasion_map：补找回来的（只在重判里可以用）；strict_grouping：见上。"""

    def __init__(self, sentences, shown_threads, shown_occasions, summaries=None, allow_summary_edit=True, phase="initial", judge_sids=None,
                 found_threads=(), found_occasion_map=None, strict_grouping=False):
        self.sentences = list(sentences)
        self.by_sid = {s["sid"]: s for s in self.sentences}
        self.judge = list(judge_sids) if judge_sids is not None else [s["sid"] for s in self.sentences]
        self.shown_threads = set(shown_threads) | set(found_threads)
        self.shown_occasions = dict(shown_occasions)
        self.shown_occasions.update(dict(found_occasion_map or {}))
        self.shown_for_edit = dict(shown_occasions) if phase == "initial" else {}
        self.summaries = dict(summaries or {})
        self.allow_summary_edit = bool(allow_summary_edit) and phase == "initial"
        self.phase = phase
        self.strict_grouping = strict_grouping


def _obj(v, allowed, required, where, problems):
    if not isinstance(v, dict):
        problems.append("%s必须是对象" % where)
        return False
    _keys(v, allowed, required, where, problems)
    return all(k in v for k in required)


def convert(raw, ctx):
    problems, notes = [], []
    try:
        obj = _parse(raw, problems, notes)
        if obj is None:
            return Result(False, None, problems, notes)
        data = _convert(obj, ctx, problems, notes)
    except RecursionError:
        return Result(False, None, ["输出嵌套得太深，读不了"], notes)
    except Exception as e:                                                         # noqa: BLE001  保险：转换自己出错也只返回理由，不崩；测试会查有没有走到这里
        return Result(False, None, [SELF_ERROR + "%s：%s" % (type(e).__name__, _short(e, 80))], notes)
    if not problems and _has_surrogate(obj):
        problems.append("输出里有不能编成 UTF-8 的字符（孤立代理字符）")
    if problems:
        return Result(False, None, problems, notes)
    return Result(True, data, [], notes)


def _convert(obj, ctx, problems, notes):
    if not isinstance(obj, dict):
        problems.append("输出必须是一个 JSON 对象")
        return None
    allowed = TOP if ctx.allow_summary_edit else TOP_REJUDGE
    _keys(obj, allowed, TOP_REQ, "输出", problems)
    if "新建" not in obj or "去处" not in obj or "缺口" not in obj:
        return None
    new_threads, new_occ, trefs, nrefs = _new(obj["新建"], ctx, problems)
    thread_ok = ctx.shown_threads | set(trefs)
    occ_parent = dict(ctx.shown_occasions)
    for n in new_occ:
        occ_parent[n["ref"]] = n["thread"]
    placements, covered, used = _places(obj["去处"], ctx, thread_ok, occ_parent, problems, notes)
    gaps = _gaps(obj["缺口"], ctx, thread_ok, covered, problems, notes)
    for g in gaps:
        for sid in g["sentences"]:
            covered.setdefault(sid, 0)
            covered[sid] += 1
    for sid in ctx.judge:
        if covered.get(sid, 0) == 0:
            problems.append("句子 %s 既不在「去处」也不在「缺口」里（每句必须出现一次）" % sid)
        elif covered[sid] > 1:
            problems.append("句子 %s 出现了 %d 次（只能在「去处」或「缺口」里出现一次）" % (sid, covered[sid]))
    certain_occ = {d["target"] for ds in placements.values() for d in ds if d["kind"] == "occasion" and d["confidence"] == "确定"}
    ref_threads = {d["target"] for ds in placements.values() for d in ds if d["kind"] in ("thread_summary", "thread_pending")}
    ref_threads |= {occ_parent[o] for o in certain_occ if o in occ_parent}
    for n in new_occ:
        if n["ref"] not in certain_occ:
            problems.append("新建的回 %s 没有任何一句「确定」挂在它下面（只是「可能」属于新的事，不要新开）" % n["ref"])
    for t in new_threads:
        if t["ref"] not in ref_threads:
            problems.append("新建的主线 %s 没有被任何句子引用（总述、回待定，或确定挂在它的回上）" % t["ref"])
    edits = _edits(obj.get("改摘要", []), ctx, problems)
    if "改摘要" not in obj and ctx.allow_summary_edit:
        notes.append("缺「改摘要」栏，按空处理")
    if problems:
        return None
    return {"new_threads": new_threads, "new_occasions": new_occ, "placements": placements, "gaps": gaps, "summary_edits": edits}


# ---------------------------------------------------------------- 新建
def _new(v, ctx, problems):
    if not _obj(v, NEW, NEW, "「新建」", problems):
        return [], [], [], []
    threads, occ, trefs, nrefs = [], [], [], []
    if not isinstance(v["主线"], list):
        problems.append("「新建」里的「主线」必须是列表")
    else:
        for i, t in enumerate(v["主线"], 1):
            w = "新建主线第 %d 项" % i
            if not _obj(t, NEW_T, NEW_T, w, problems):
                continue
            ok = True
            for k in NEW_T:
                if not _ok_str(t[k]):
                    problems.append("%s的「%s」必须是非空文字" % (w, k))
                    ok = False
            if not ok:
                continue
            if not RE_T.match(t["代号"]):
                problems.append("%s的代号 %s 不对（新主线的临时代号写 T 加数字）" % (w, _short(t["代号"])))
            elif t["代号"] in trefs:
                problems.append("新建主线的临时代号 %s 重复" % t["代号"])
            else:
                trefs.append(t["代号"])
                threads.append({"ref": t["代号"], "name": t["名称"], "desc": t["说明"]})
    thread_ok = ctx.shown_threads | set(trefs)
    if not isinstance(v["回"], list):
        problems.append("「新建」里的「回」必须是列表")
    else:
        for i, n in enumerate(v["回"], 1):
            w = "新建回第 %d 项" % i
            if not _obj(n, NEW_N, NEW_N, w, problems):
                continue
            ok = True
            for k in NEW_N:
                if not _ok_str(n[k]):
                    problems.append("%s的「%s」必须是非空文字" % (w, k))
                    ok = False
            if not ok:
                continue
            if not RE_N.match(n["代号"]):
                problems.append("%s的代号 %s 不对（新回的临时代号写 N 加数字）" % (w, _short(n["代号"])))
                continue
            if n["代号"] in nrefs:
                problems.append("新建回的临时代号 %s 重复" % n["代号"])
                continue
            if n["主线"] not in thread_ok:
                problems.append("新建回 %s 的主线 %s 不是展示过的主线，也不是本次新建的主线" % (n["代号"], _short(n["主线"], 40)))
                continue
            nrefs.append(n["代号"])
            occ.append({"ref": n["代号"], "thread": n["主线"], "title": n["标题"], "summary": n["摘要"]})
    return threads, occ, trefs, nrefs


# ---------------------------------------------------------------- 去处
def _sid_list(v, ctx, where, problems, allowed):
    if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
        problems.append("%s的「句」必须是非空的句子编号列表" % where)
        return None
    out, seen = [], set()
    for sid in v:
        if sid not in ctx.by_sid:
            problems.append("%s里的句子编号 %s 不存在" % (where, _short(sid)))
        elif sid not in allowed:
            problems.append("%s里的句子 %s 不是这一次要处理的句子" % (where, sid))
        elif sid in seen:
            problems.append("%s里句子 %s 写了两次" % (where, sid))
        else:
            seen.add(sid)
            out.append(sid)
    return out if len(out) == len(v) else None


def _group_note(ctx, sids, where, problems, notes):
    msgs = {ctx.by_sid[s]["msg_id"] for s in sids}
    roles = {ctx.by_sid[s]["role"] for s in sids}
    if len(msgs) > 1 or len(roles) > 1:
        if ctx.strict_grouping:
            problems.append("%s把跨消息或跨说话人的句子写在了一起（%s）" % (where, "、".join(sids)))
        else:
            n = "%s 跨消息或跨说话人，已按句拆开" % where
            if n not in notes:
                notes.append(n)


def _places(v, ctx, thread_ok, occ_parent, problems, notes):
    placements, covered, used = {}, {}, set()
    if not isinstance(v, list):
        problems.append("「去处」必须是列表")
        return placements, covered, used
    allowed = set(ctx.judge)
    for i, e in enumerate(v, 1):
        w = "去处第 %d 项" % i
        if not isinstance(e, dict):
            problems.append("%s必须是对象" % w)
            continue
        _keys(e, PLACE, ("句",), w, problems)
        kinds = [k for k in ("挂", "日常", "不记") if k in e]
        if len(kinds) != 1:
            problems.append("%s必须恰好写一种去处（挂／日常／不记），实际写了 %d 种" % (w, len(kinds)))
            continue
        if "句" not in e:
            continue
        sids = _sid_list(e["句"], ctx, w, problems, allowed)
        kind = kinds[0]
        dests = None
        if kind in ("日常", "不记"):
            if e[kind] is not True:
                problems.append("%s的「%s」必须是 true" % (w, kind))
            else:
                dests = [{"kind": "daily" if kind == "日常" else "skip", "target": None, "confidence": None, "candidates": [], "reason": ""}]
        else:
            dests = _dests(e["挂"], ctx, thread_ok, occ_parent, w, problems)
        if sids is None:
            continue
        for sid in sids:
            covered[sid] = covered.get(sid, 0) + 1
        _group_note(ctx, sids, w, problems, notes)
        if dests is None:
            continue
        for sid in sids:
            cur = placements.setdefault(sid, [])
            for d in dests:
                if any((x["kind"], x["target"]) == (d["kind"], d["target"]) for x in cur):
                    problems.append("句子 %s 重复挂同一个对象（%s %s）" % (sid, d["kind"], d["target"]))
                else:
                    cur.append(dict(d))
    for sid, ds in placements.items():
        if any(d["kind"] in ("daily", "skip") for d in ds) and len(ds) > 1:
            problems.append("句子 %s 有日常或不记，就不能再有别的去处" % sid)
    return placements, covered, used


def _dests(挂, ctx, thread_ok, occ_parent, w, problems):
    if not (isinstance(挂, list) and 挂):
        problems.append("%s的「挂」必须是非空列表" % w)
        return None
    out = []
    for j, d in enumerate(挂, 1):
        ww = "%s第 %d 条挂接" % (w, j)
        if not isinstance(d, dict):
            problems.append("%s必须是对象" % ww)
            continue
        if "回" in d:
            r = _occ_dest(d, occ_parent, ww, problems)
        elif d.get("层") == "主线待定" and "主线" not in d:
            r = _unknown_dest(d, thread_ok, ww, problems)
        elif "主线" in d:
            r = _thread_dest(d, thread_ok, occ_parent, ww, problems)
        else:
            problems.append("%s不认识：要写「回」（挂回）、「主线」＋「层」（总述／回待定），或「层」写主线待定" % ww)
            continue
        if r is not None:
            out.append(r)
    return out if len(out) == len(挂) else None


def _reason(d, ww, problems, required):
    if "理由" in d and not isinstance(d["理由"], str):
        problems.append("%s的「理由」必须是文字" % ww)
        return None
    r = d.get("理由", "")
    if required and not r.strip():
        problems.append("%s必须写理由" % ww)
        return None
    return r


def _occ_dest(d, occ_parent, ww, problems):
    _keys(d, ("回", "把握", "理由"), ("回", "把握"), ww, problems)
    if not all(k in d for k in ("回", "把握")):
        return None
    if not isinstance(d["回"], str) or d["回"] not in occ_parent:
        problems.append("%s的回 %s 不是本步展示过的回，也不是本次新建的回" % (ww, _short(d["回"], 40)))
        return None
    if d["把握"] not in ("确定", "可能"):
        problems.append("%s的「把握」必须是确定或可能" % ww)
        return None
    r = _reason(d, ww, problems, d["把握"] == "可能")
    if r is None:
        return None
    if set(d) - {"回", "把握", "理由"}:
        return None
    return {"kind": "occasion", "target": d["回"], "confidence": d["把握"], "candidates": [], "reason": r}


def _thread_dest(d, thread_ok, occ_parent, ww, problems):
    layer = d.get("层")
    if layer == "总述":
        _keys(d, ("主线", "层", "理由"), ("主线", "层"), ww, problems)
    elif layer == "回待定":
        _keys(d, ("主线", "层", "候选回", "理由"), ("主线", "层", "候选回", "理由"), ww, problems)
    else:
        problems.append("%s的「层」必须是总述或回待定" % ww)
        return None
    if not all(k in d for k in (("主线", "层", "候选回", "理由") if layer == "回待定" else ("主线", "层"))):
        return None
    if not isinstance(d["主线"], str) or d["主线"] not in thread_ok:
        problems.append("%s的主线 %s 不是本步展示过的主线，也不是本次新建的主线" % (ww, _short(d["主线"], 40)))
        return None
    cands = []
    if layer == "回待定":
        c = d.get("候选回")
        if not _str_list(c):
            problems.append("%s的「候选回」必须是编号列表（可以为空）" % ww)
            return None
        if len(set(c)) != len(c):
            problems.append("%s的候选回有重复" % ww)
            return None
        for oc in c:
            if oc not in occ_parent:
                problems.append("%s的候选回 %s 不是展示过的回，也不是本次新建的回" % (ww, _short(oc, 40)))
                return None
            if occ_parent[oc] != d["主线"]:
                problems.append("%s的候选回 %s 不是主线 %s 下的回" % (ww, oc, d["主线"]))
                return None
        cands = list(c)
    r = _reason(d, ww, problems, layer == "回待定")
    if r is None:
        return None
    if set(d) - ({"主线", "层", "候选回", "理由"} if layer == "回待定" else {"主线", "层", "理由"}):
        return None
    return {"kind": "thread_summary" if layer == "总述" else "thread_pending", "target": d["主线"], "confidence": None, "candidates": cands, "reason": r}


def _unknown_dest(d, thread_ok, ww, problems):
    _keys(d, ("层", "候选主线", "理由"), ("层", "候选主线", "理由"), ww, problems)
    if not all(k in d for k in ("候选主线", "理由")):
        return None
    c = d["候选主线"]
    if not _str_list(c):
        problems.append("%s的「候选主线」必须是编号列表（可以为空）" % ww)
        return None
    if len(set(c)) != len(c):
        problems.append("%s的候选主线有重复" % ww)
        return None
    for t in c:
        if t not in thread_ok:
            problems.append("%s的候选主线 %s 不是展示过的主线，也不是本次新建的主线" % (ww, _short(t, 40)))
            return None
    r = _reason(d, ww, problems, True)
    if r is None or set(d) - {"层", "候选主线", "理由"}:
        return None
    return {"kind": "thread_unknown", "target": None, "confidence": None, "candidates": list(c), "reason": r}


# ---------------------------------------------------------------- 缺口
def _gaps(v, ctx, thread_ok, covered, problems, notes):
    out = []
    if not isinstance(v, list):
        problems.append("「缺口」必须是列表")
        return out
    allowed = set(ctx.judge)
    for i, g in enumerate(v, 1):
        w = "缺口第 %d 项" % i
        if not _obj(g, GAP, GAP, w, problems):
            continue
        sids = _sid_list(g["句"], ctx, w, problems, allowed)
        ok = sids is not None
        if g["层"] not in ("主线", "回"):
            problems.append("%s的「层」必须是主线或回" % w)
            ok = False
        known = g["已知主线"]
        if not (known is None or isinstance(known, str)):
            problems.append("%s的「已知主线」必须是编号或 null" % w)
            ok = False
        elif known is not None and known not in thread_ok:
            problems.append("%s的已知主线 %s 不是本步展示过的主线" % (w, _short(known, 40)))
            ok = False
        elif known is None and g["层"] == "回":
            problems.append("%s的层是「回」，必须给「已知主线」" % w)
            ok = False
        if not (_str_list(g["词"]) and g["词"]):
            problems.append("%s的「词」必须是非空的词语列表" % w)
            ok = False
        if not ok:
            continue
        _group_note(ctx, sids, w, problems, notes)
        out.append({"sentences": sids, "layer": g["层"], "known_thread": known, "words": list(g["词"])})
    return out


# ---------------------------------------------------------------- 改摘要
def _edits(v, ctx, problems):
    out = []
    if not isinstance(v, list):
        problems.append("「改摘要」必须是列表")
        return out
    seen = set()
    for i, e in enumerate(v, 1):
        w = "改摘要第 %d 项" % i
        if not _obj(e, EDIT, EDIT, w, problems):
            continue
        ok = True
        if not isinstance(e["回"], str) or e["回"] not in ctx.shown_for_edit:
            problems.append("%s的回 %s 不是本步展示过的已有回（临时代号、没展示的回不能改摘要）" % (w, _short(e["回"], 40)))
            ok = False
        elif e["回"] in seen:
            problems.append("%s：同一回一步只能改一次（%s）" % (w, e["回"]))
            ok = False
        for k in ("摘要", "理由"):
            if not (isinstance(e[k], str) and e[k].strip()):
                problems.append("%s的「%s」必须是非空文字" % (w, k))
                ok = False
        if not _str_list(e["依据句"]):
            problems.append("%s的「依据句」必须是句子编号列表（可以为空）" % w)
            ok = False
        else:
            for sid in e["依据句"]:
                if sid not in ctx.by_sid:
                    problems.append("%s的依据句 %s 不是本步的句子编号" % (w, _short(sid)))
                    ok = False
        if ok and ctx.summaries.get(e["回"], "").strip() == e["摘要"].strip():
            problems.append("%s：改后的摘要和现在的相同" % w)
            ok = False
        if ok:
            seen.add(e["回"])
            out.append({"occasion": e["回"], "text": e["摘要"], "reason": e["理由"], "evidence": list(e["依据句"])})
    return out
