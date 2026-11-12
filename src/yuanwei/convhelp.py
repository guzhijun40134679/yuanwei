"""模型输出转换用的小件（取自研究的 layer_convert：JSON 读法、键检查、结果对象）。"""
import json
import re
from dataclasses import dataclass, field


FENCE_NOTE = "输出外面包了一层代码框，已去掉"


@dataclass
class Result:
    ok: bool
    data: object = None
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _short(v, n=24):
    s = v if isinstance(v, str) else repr(v)
    s = s.replace("\n", "↵").replace("\r", "↵")
    return s if len(s) <= n else s[:n] + "…"


def _ok_str(v):
    return isinstance(v, str) and v != ""


def _has_surrogate(obj):
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in x):
                return True
        elif isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _strip_fence(raw):
    s = raw.strip()
    m = re.fullmatch(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n?```", s, flags=re.S)
    if m:
        return m.group(1).strip(), True
    return s, False


def _parse(raw, problems, notes):
    if not isinstance(raw, str):
        problems.append("输出不是文字")
        return None
    text, fenced = _strip_fence(raw)
    if fenced:
        notes.append(FENCE_NOTE)
    if not text:
        problems.append("输出是空的")
        return None
    if not text.startswith("{"):
        problems.append("输出不是一个 JSON 对象（要以「{」开头，前面不能有别的文字，也不能是列表或别的东西）")
        return None
    dups = []

    def hook(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen and k not in dups:
                dups.append(k)
            seen.add(k)
        return dict(pairs)

    def bad_const(name):
        raise ValueError("JSON 里不允许 %s" % name)

    try:
        obj, end = json.JSONDecoder(object_pairs_hook=hook, parse_constant=bad_const).raw_decode(text)
    except RecursionError:
        problems.append("JSON 嵌套得太深，读不了")
        return None
    except ValueError as e:
        problems.append("不是合法的 JSON：%s" % _short(e, 80))
        return None
    for k in dups:
        problems.append("JSON 里有重复的键「%s」" % _short(k))
    if text[end:].strip():
        problems.append("JSON 对象后面还有别的文字")
        return None
    return obj


def _keys(d, allowed, required, where, problems):
    """未知键拒；缺必填拒。返回是否没缺必填。"""
    extra = [k for k in d if k not in allowed]
    if extra:
        problems.append("%s里有不认识的键：%s（只认 %s）" % (where, "、".join("「%s」" % _short(k, 12) for k in extra), "、".join("「%s」" % k for k in allowed)))
    miss = [k for k in required if k not in d]
    if miss:
        problems.append("%s缺%s" % (where, "、".join("「%s」" % k for k in miss)))
    return not miss


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) and x != "" for x in v)
