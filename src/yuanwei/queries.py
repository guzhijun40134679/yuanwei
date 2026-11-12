"""recall 的可选关键词：规范化、校验、加权 RRF 合并。

首版做法（方案第 5 节）：额外最多 3 项，每项最多 128 个码点，合计最多 384，原问题加额外最多 4 个查询文本；超了报错，不悄悄截断；
校验、去空、精确去重在检索和向量调用之前做，保留代码符号，不做模糊去重。每一路各自检索（照现行全局检索做、按消息去重），再合并名次：
加权 RRF，原问题总权重 1，全部补充路合计权重 1、按有效路数均分，常数 60，缺席记零；同分先看原问题的名次，再看补充路里最好的名次，
最后消息序号大的在前（和现行「同分 order 大的在前」同方向，是自定的稳定次序）。不另加「每路只取前多少条」。同义改写可能重复支持同一条消息，不是多份独立证据。
"""
from fractions import Fraction

from .errors import BadQuery

MAX_EXTRA = 3
MAX_ITEM_CP = 128
MAX_TOTAL_CP = 384
RRF_K = 60


def normalize(query, keywords):
    """返回 {"queries": [原问题, *额外], "extras": [...], "mode": "single"|"multi"}。去空、去首尾空白、精确去重（和原问题相同的也去掉）以后再数。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("问题不能是空的")
    if keywords is None:
        extras = []
    else:
        if not isinstance(keywords, (list, tuple)):
            raise BadQuery("关键词必须是文字列表，实际是 %s" % type(keywords).__name__)
        extras = []
        for k in keywords:
            if not isinstance(k, str):
                raise BadQuery("关键词列表里的每一项必须是文字，遇到 %s" % type(k).__name__)
            t = k.strip()
            if t and t != query.strip() and t not in extras:
                extras.append(t)
    if len(extras) > MAX_EXTRA:
        raise BadQuery("额外的关键词最多 %d 项（去掉空项和重复项以后），现在有 %d 项" % (MAX_EXTRA, len(extras)))
    for t in extras:
        if len(t) > MAX_ITEM_CP:
            raise BadQuery("每个关键词最多 %d 个码点，有一项是 %d" % (MAX_ITEM_CP, len(t)))
    if sum(len(t) for t in extras) > MAX_TOTAL_CP:
        raise BadQuery("额外的关键词合计最多 %d 个码点" % MAX_TOTAL_CP)
    return {"queries": [query] + extras, "extras": extras, "mode": "multi" if extras else "single"}


def clip_extras(query, keywords):
    """额外关键词去空、去重（和原问题相同的也去掉）以后还超过 MAX_EXTRA 项：只取前 MAX_EXTRA 项照常查，返回 (要用的关键词, 丢掉的几项)。
    不超过时原样返回（同一个对象，请求逐字节不变）；类型不对等别的不合格输入也原样交给 normalize 去报错。"""
    if not isinstance(query, str) or not isinstance(keywords, (list, tuple)) or any(not isinstance(k, str) for k in keywords):
        return keywords, []
    cleaned = []
    for k in keywords:
        t = k.strip()
        if t and t != query.strip() and t not in cleaned:
            cleaned.append(t)
    if len(cleaned) <= MAX_EXTRA:
        return keywords, []
    return cleaned[:MAX_EXTRA], cleaned[MAX_EXTRA:]


def clip_note(dropped):
    show = [d if len(d) <= 24 else d[:24] + "…" for d in dropped]
    return "〔yuanwei｜说明〕额外的关键词超过 %d 项，只用了前 %d 项照常查；没有用的是：%s。" % (MAX_EXTRA, MAX_EXTRA, "、".join(show))


def weights(n_extra):
    """原问题 1；补充路合计 1、均分。"""
    return [Fraction(1)] + ([Fraction(1, n_extra)] * n_extra if n_extra else [])


def merge_routes(orders, ws, seqs):
    """orders[i]＝第 i 路按消息去重后的命中序（第 0 路是原问题）；ws[i]＝权重；seqs＝{消息编号: 消息序号}（只用来定稳定次序）。
    返回 (合并后的消息序, {消息编号: {路序号: 名次（从 1 起）}})。"""
    prov = {}
    for i, order in enumerate(orders):
        for rank, m in enumerate(order, 1):
            prov.setdefault(m, {})[i] = rank
    score = {m: sum(ws[i] * Fraction(1, RRF_K + r) for i, r in routes.items()) for m, routes in prov.items()}
    inf = float("inf")

    def key(m):
        routes = prov[m]
        extra = [r for i, r in routes.items() if i > 0]
        return (-score[m], routes.get(0, inf), min(extra) if extra else inf, -seqs[m])
    return sorted(prov, key=key), prov
