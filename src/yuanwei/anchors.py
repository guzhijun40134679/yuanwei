"""锚点切句：一条消息 → 若干锚点（文字、码点起止 [起,止)）。检索用；和整理用的切句（split.py）是两套范围，分别保存。
切法（取自研究）：先按「。！？；」和换行切，超过 60 字再从逗号处切，去首尾空白后短于 2 字的丢掉。
索引版本：1＝参照回放用的研究版（锚点 order＝消息序号×100＋句序，一条消息最多 98 句，超过就拒收）；
         2＝产品版（order＝消息序号×1,000,000＋句序，单条不限 98 段；排序关系和版本 1 一致，所以不超过 98 句的消息两版的检索结果相同）。
"""
import re

from .errors import YuanweiError


class AnchorError(YuanweiError):
    code = "anchor"


MAX_SENTENCES = 98                   # 版本 1：句序 1…98


LONG_LIMIT = 60                      # 超过 60 字再从逗号处切


MIN_LEN = 2                          # 短于 2 字的丢掉


_JS_WS = " \t\n\r\v\f                 　﻿"


_SENT_RE = re.compile(r"[^。！？；\n]*[。！？；]|[^\n]+")


def js_trim(s):
    return s.strip(_JS_WS)
V2_FACTOR = 1000000
V1_FACTOR = 100
V2_MAX = V2_FACTOR - 1


def anchor_spans(content):
    """一条消息的锚点文字和它在正文里的码点起止 [起, 止)。照 V0.1 anchor_texts 的切法（句子 → 超过 60 字再从逗号处切），
    只多算位置；算完核一遍：文字列表等于 anchor_texts、每段正文切片等于文字。"""
    out = []
    for m in _SENT_RE.finditer(content):
        g = m.group(0)
        s = js_trim(g)
        if len(s) < MIN_LEN:
            continue
        start = m.start() + (len(g) - len(g.lstrip(_JS_WS)))
        if len(s) <= LONG_LIMIT:
            out.append((s, start, start + len(s)))
            continue
        pos = 0
        for p in re.findall(r"[^，,]*[，,]|[^，,]+", s):
            ps = js_trim(p)
            lead = len(p) - len(p.lstrip(_JS_WS))
            if len(ps) >= MIN_LEN:
                out.append((ps, start + pos + lead, start + pos + lead + len(ps)))
            pos += len(p)
    for t, a, b in out:
        if content[a:b] != t:
            raise AnchorError("锚点 %r 的码点区间 [%d,%d) 对不上正文" % (t, a, b))
    return out


def anchors_for(msg_id, seq, content, index_version):
    """一条消息的锚点行（供存储）。返回 [{"anchor_id","ord","text","start","end","index_version"}]。"""
    spans = anchor_spans(content)
    if index_version == 1:
        if len(spans) > MAX_SENTENCES:
            raise AnchorError("%s：切出 %d 句，超过 %d 句（索引版本 1 不支持；新工作区用索引版本 2）" % (msg_id, len(spans), MAX_SENTENCES))
        factor = V1_FACTOR
    elif index_version == 2:
        if len(spans) > V2_MAX:
            raise AnchorError("%s：切出 %d 句，超过索引版本 2 的上限 %d" % (msg_id, len(spans), V2_MAX))
        factor = V2_FACTOR
    else:
        raise AnchorError("不认识的索引版本 %r" % (index_version,))
    return [{"anchor_id": "%s#%d" % (msg_id, i), "ord": seq * factor + i, "text": t, "start": s, "end": e, "index_version": index_version} for i, (t, s, e) in enumerate(spans, 1)]
