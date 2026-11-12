"""规则表的自检用参考模型（只在 tests/ 里用，**不是产品实现**，产品的 yuanwei/redact.py 由交回点乙来写，不能 import 这个文件）。

用途：交回点甲没有实现，但规则表本身得是「自洽」的——每条正例确实在它那条规则的覆盖范围里、反例确实没被任何一条规则命中、重叠命中合并以后的区间数和类型对得上。
所以这里用表里的参考正则（res）加「先合并区间、再替换」的最小模型，把整张表跑一遍。表里哪条写错了，这里就红，不用等乙。
"""
import re

import secret_rules_table as T


def _compiled():
    out = []
    for r in T.RULES:
        for rx in r["res"]:
            out.append((r, re.compile(rx)))
    return out


_RX = _compiled()


def hits(text):
    """所有规则的命中：[(起, 止, 优先级, 类型, 规则编号)]，区间是被替换的那一段（命名组 s，没有就整段）。"""
    out = []
    for r, rx in _RX:
        for m in rx.finditer(text):
            if "s" in rx.groupindex and m.group("s") is not None:
                a, b = m.span("s")
            else:
                a, b = m.span()
            if b > a:
                out.append((a, b, r["priority"], r["kind"], r["id"]))
    return out


def merge(hs):
    """重叠的命中合并成并集（只算一个区间）；类型取组里优先级最高的（数小的），同级取靠前的。相邻但不重叠的不合并。"""
    hs = sorted(hs, key=lambda h: (h[0], h[1]))
    groups = []
    for h in hs:
        if groups and h[0] < groups[-1]["end"]:
            g = groups[-1]
            g["end"] = max(g["end"], h[1])
            g["members"].append(h)
        else:
            groups.append({"start": h[0], "end": h[1], "members": [h]})
    out = []
    for g in groups:
        best = sorted(g["members"], key=lambda h: (h[2], h[0], h[4]))[0]
        out.append((g["start"], g["end"], best[3], best[4]))
    return out


def redact(text):
    """返回 (过滤后的文字, 区间数, [类型…])。"""
    spans = merge(hits(text))
    out = text
    for a, b, kind, _rid in reversed(spans):
        out = out[:a] + T.PLACEHOLDER % kind + out[b:]
    return out, len(spans), [k for _a, _b, k, _r in spans]
