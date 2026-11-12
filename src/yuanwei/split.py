"""切句：一条消息 → 若干句 [起, 止)（码点，左闭右开）。照 检索入口/装置/入口_语料.py 的 anchor_spans 的切法
（先按「。！？；」和换行切，超过 60 字再从逗号处切），只在三处不同（照抄差异.md）：
  1. 不丢短句：不到 2 个字的也成句（每句都要有去处）；
  2. 只由标点、符号构成的碎片（例如「好。。」切出的第二个「。」）并进前一句，不单独成句；整条消息只有这种碎片时才单独成句；
  3. 空白（含不换行空格等 JS trim 认的那一组）只在句子两头被去掉，不算内容。
保证：句子互不重叠、按位置排序；句子之外的字符全是空白；所以句子加上间隔的空白能拼回原话。
纯函数，不联网、不读密钥、不调模型。"""
import re
import unicodedata


LONG_LIMIT = 60
# 与 JS String.prototype.trim 一致的空白集
JS_WS = " \t\n\r\v\f                 　﻿"
_SENT_RE = re.compile(r"[^。！？；\n]*[。！？；]|[^\n]+")


def _trim_span(content, a, b):
    while a < b and content[a] in JS_WS:
        a += 1
    while b > a and content[b - 1] in JS_WS:
        b -= 1
    return a, b


def _punct_only(text):
    return all(unicodedata.category(ch)[0] in "PSZC" for ch in text)


def split_message(content):
    """返回 [(start, end)]。"""
    raw = []
    for m in _SENT_RE.finditer(content):
        a, b = _trim_span(content, m.start(), m.end())
        if a >= b:
            continue
        if b - a <= LONG_LIMIT:
            raw.append((a, b))
            continue
        pos = a
        for p in re.findall(r"[^，,]*[，,]|[^，,]+", content[a:b]):
            pa, pb = _trim_span(content, pos, pos + len(p))
            pos += len(p)
            if pa < pb:
                raw.append((pa, pb))
    out = []
    for a, b in raw:
        if out and _punct_only(content[a:b]):
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def reconstructs(content, spans):
    """核：句子互不重叠、按位置排序、都在正文里，句子之外的字符全是空白。"""
    pos = 0
    for a, b in spans:
        if not (0 <= pos <= a < b <= len(content)):
            return False
        if content[pos:a].strip(JS_WS) != "":
            return False
        pos = b
    return content[pos:].strip(JS_WS) == ""


def sentences_of_session(messages):
    """一次会话的全部句子，编号 s1、s2……（按消息顺序、消息内按位置）。messages：[{msg_id, role, content, msg_time, session_id, seq}]。
    返回 [{"sid","msg_id","role","date","start","end","text","session_id"}]。"""
    out = []
    n = 0
    for m in sorted(messages, key=lambda m: m["seq"]):
        spans = split_message(m["content"])
        if not reconstructs(m["content"], spans):
            raise ValueError("切句后拼不回原话：%s" % m["msg_id"])
        mt = m.get("msg_time")
        date = mt[:10] if isinstance(mt, str) and re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", mt) else None
        for a, b in spans:
            n += 1
            out.append({"sid": "s%d" % n, "msg_id": m["msg_id"], "role": m["role"], "date": date, "start": a, "end": b,
                        "text": m["content"][a:b], "session_id": m.get("session_id")})
    return out
