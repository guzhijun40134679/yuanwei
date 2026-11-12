"""回答：回答提示 ＝ R1 回答提示 ＋ 闭环加句；请求 ＝ 系统提示 ＋ 一条用户消息（边界行包住的编码材料、当前日期、当前问题）；严格读 JSON（取自研究的 闭环_提示）。
提问日和时区显式给：当前日期写成「YYYY-MM-DD 周X（时区）」，不取机器的今天。"""
import datetime as dt
import json
import re

from . import encoding as EN
from . import resources as RS
from .provider import PARAMETERS, body_bytes

ANSWER_KEYS = ("answer", "needs_source", "missing_information")
WEEKDAYS = "一二三四五六日"
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TZ_LABEL = {"Asia/Shanghai": "北京时间", "Asia/Bangkok": "泰国时间"}
ANSWER_CODES = {
    "A1": "回复不是一个合格的 JSON 对象（解析不了、不是对象，或有重复的键）",
    "A2": "回复的字段不是「answer」「needs_source」「missing_information」这三个",
    "A3": "字段的类型不对，或「answer」是空的",
}


def answer_date_text(date_iso, tz_label="北京时间"):
    if not (isinstance(date_iso, str) and _DATE_RE.fullmatch(date_iso)):
        raise ValueError("日期不是 YYYY-MM-DD：%r" % (date_iso,))
    try:
        d = dt.date(int(date_iso[0:4]), int(date_iso[5:7]), int(date_iso[8:10]))
    except ValueError as e:
        raise ValueError("日期不存在：%r" % (date_iso,)) from e
    return "%s 周%s（%s）" % (date_iso, WEEKDAYS[d.weekday()], tz_label)


def user_text(material_enc, date_iso, question, tz_label="北京时间"):
    return "%s\n\n当前日期：%s\n\n当前问题：\n%s" % (EN.wrap_material(material_enc), answer_date_text(date_iso, tz_label), question)


def build_request(material_enc, date_iso, question, tz_label="北京时间"):
    """返回 {"system","user","bytes","material_sha16"}。"""
    system = RS.answer_system()
    user = user_text(material_enc, date_iso, question, tz_label)
    body = {**PARAMETERS, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    return {"system": system, "user": user, "bytes": body_bytes(body), "material_sha16": EN.boundary(material_enc)[2]}


class _DupKey(ValueError):
    pass


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise _DupKey("重复的键 %r" % (k,))
        d[k] = v
    return d


def loads_strict(raw):
    """严格的 JSON 对象解析：不去代码框、拒绝重复的键、必须是对象。失败返回 (None, 说明)。"""
    if not isinstance(raw, str):
        return None, "回复不是文字"
    try:
        obj = json.loads(raw, object_pairs_hook=_pairs)
    except _DupKey as e:
        return None, str(e)
    except (ValueError, RecursionError):
        return None, "不是合法的 JSON"
    if not isinstance(obj, dict):
        return None, "不是一个 JSON 对象"
    return obj, None


def parse_answer(raw):
    """恰好 answer（非空文字）、needs_source（布尔）、missing_information（文字列表）。"""
    def fail(code, why):
        return {"ok": False, "code": code, "problems": [why], "answer": None}
    obj, why = loads_strict(raw)
    if obj is None:
        return fail("A1", why)
    if set(obj) != set(ANSWER_KEYS):
        return fail("A2", "字段是 %s" % sorted(obj))
    if not (isinstance(obj["answer"], str) and obj["answer"].strip() and isinstance(obj["needs_source"], bool) and isinstance(obj["missing_information"], list)
            and all(isinstance(x, str) for x in obj["missing_information"])):
        return fail("A3", "字段类型不符或回答是空的")
    return {"ok": True, "code": None, "problems": [], "answer": obj}
