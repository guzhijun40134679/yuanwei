"""编码适配：不可信正文（原话）里可能被当成结构的字符，一对一换成替代字符（固定映射表 18 对）；材料首尾包上边界行（取自研究的 闭环_编码）。
程序自己生成的结构（编号、日期、块头、提示行）不编码。一对一替换，码点位置不动。"""
import hashlib
import json
import re  # noqa: F401

from .errors import ConfigError

EXPECTED_PAIRS = 18
PINNED_SHA256 = "aafe98c8dd80ffe6bcdbfd4093a06b3c8c2c954af5c3823bf8e0587e1c6df01b"


LINE_CLASS_COUNT = 10            # 映射表里前 10 个是换行类控制字符


class Mapping:
    def __init__(self, pairs, sha):
        self.pairs = [tuple(p) for p in pairs]
        self.sha = sha
        self.orig_to_rep = {o: r for o, r, _ in self.pairs}
        self.rep_to_orig = {r: o for o, r, _ in self.pairs}
        self.orig_chars = tuple(o for o, _, _ in self.pairs)
        self.rep_chars = tuple(r for _, r, _ in self.pairs)
        self.line_chars = self.orig_chars[:LINE_CLASS_COUNT]
        self._enc = str.maketrans(self.orig_to_rep)
        self._dec = str.maketrans(self.rep_to_orig)

    def encode(self, s):
        return s.translate(self._enc)

    def decode(self, s):
        return s.translate(self._dec)

    def has_orig(self, s):
        return any(c in s for c in self.orig_chars)

    def has_rep(self, s):
        return any(c in s for c in self.rep_chars)


def boundary(material):
    sha16 = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return "⟪材料开始 %s⟫" % sha16, "⟪材料结束 %s⟫" % sha16, sha16


def wrap_material(material):
    s, e, _ = boundary(material)
    return s + "\n" + material + "\n" + e


def encode_leaf(v, mp):
    if isinstance(v, str):
        return mp.encode(v)
    if isinstance(v, list):
        return [encode_leaf(x, mp) for x in v]
    if isinstance(v, dict):
        return {k: encode_leaf(x, mp) for k, x in v.items()}
    return v


def load_mapping(raw=None):
    """读映射表资源并核：指纹等于登记的、恰好 18 对、单码点换单码点、原字符和替代字符各自不重复、两个集合不相交。"""
    if raw is None:
        from .resources import read_bytes
        raw = read_bytes("encoding-map.json")
    got = hashlib.sha256(raw).hexdigest()
    if got != PINNED_SHA256:
        raise ConfigError("编码映射表指纹对不上（要 %s…，实际 %s…）" % (PINNED_SHA256[:12], got[:12]))
    pairs = json.loads(raw.decode("utf-8"))["映射"]
    if not isinstance(pairs, list) or len(pairs) != EXPECTED_PAIRS:
        raise ConfigError("映射表应当恰好 %d 对" % EXPECTED_PAIRS)
    for pr in pairs:
        if not (isinstance(pr, list) and len(pr) >= 2 and isinstance(pr[0], str) and isinstance(pr[1], str) and len(pr[0]) == 1 and len(pr[1]) == 1):
            raise ConfigError("映射表里有不是「单码点换单码点」的项：%r" % (pr,))
    origs, reps = [pr[0] for pr in pairs], [pr[1] for pr in pairs]
    if len(set(origs)) != len(origs) or len(set(reps)) != len(reps) or set(origs) & set(reps):
        raise ConfigError("映射表不是一对一，或原字符集合和替代字符集合相交")
    return Mapping([(pr[0], pr[1], pr[2] if len(pr) > 2 else "") for pr in pairs], got)
