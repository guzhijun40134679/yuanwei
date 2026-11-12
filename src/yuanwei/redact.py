"""正文密钥过滤（删除与密钥过滤）。规则表的金标在测试目录里；这里是产品实现，不依赖测试目录。

所有规则的命中先收齐，重叠的合并成并集再替换（一个区间只算一处，类型取优先级最高的）；占位符只留类型；过滤幂等；改任何规则必须升 RULES_VERSION。
"""
import re
from typing import NamedTuple, Tuple

RULES_VERSION = 1
PLACEHOLDER = "〔密钥已挡掉：%s〕"

# (规则编号, 类型, 优先级, [正则…])：命名组 s 是被替换的范围，没有命名组就整段匹配都换掉
_RULES = [    ('T01', 'Anthropic', 10, [
        '(?<![A-Za-z0-9_-])(?P<s>sk-ant-[A-Za-z0-9_-]{20,})',
    ]),    ('T02', 'OpenAI', 11, [
        '(?<![A-Za-z0-9_-])(?P<s>sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,})',
    ]),    ('T03', 'DeepSeek', 12, [
        '(?<![A-Za-z0-9_-])(?P<s>sk-[0-9a-f]{32})(?![A-Za-z0-9_-])',
    ]),    ('T04', 'OpenAI', 13, [
        '(?<![A-Za-z0-9_-])(?P<s>sk-[A-Za-z0-9]{40,})(?![A-Za-z0-9_])',
    ]),    ('T05', 'API密钥', 14, [
        '(?<![A-Za-z0-9_-])(?P<s>sk-[A-Za-z0-9_-]{32,})',
    ]),    ('T06', 'Google', 15, [
        '(?<![A-Za-z0-9_-])(?P<s>AIza[0-9A-Za-z_-]{35})(?![A-Za-z0-9_-])',
    ]),    ('T07', 'GitHub', 16, [
        '(?<![A-Za-z0-9_-])(?P<s>(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}))',
    ]),    ('T08', 'GitLab', 17, [
        '(?<![A-Za-z0-9_-])(?P<s>glpat-[A-Za-z0-9_-]{20,})',
    ]),    ('T09', 'Slack', 18, [
        '(?<![A-Za-z0-9_-])(?P<s>(?:xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,}))',
    ]),    ('T10', 'AWS', 19, [
        '(?<![A-Za-z0-9_-])(?P<s>(?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Za-z0-9])',
    ]),    ('T11', 'AWS', 55, [
        '(?i)(?<![A-Za-z0-9])(?:aws[_-]?)?(?:secret[_-]?access[_-]?key|session[_-]?token)(?![A-Za-z0-9_])[\\"\']?[ \\t]*(?::=|=>|=|:|：)[ \\t]*[\\"\']?(?!〔密钥已挡掉：)(?P<s>[A-Za-z0-9/+=]{16,})',
    ]),    ('T12', 'Stripe', 20, [
        '(?<![A-Za-z0-9_-])(?P<s>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})',
    ]),    ('T13', 'HuggingFace', 21, [
        '(?<![A-Za-z0-9_-])(?P<s>hf_[A-Za-z0-9]{30,})',
    ]),    ('T14', 'Telegram', 22, [
        '(?<![0-9A-Za-z])(?P<s>[0-9]{8,10}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])',
    ]),    ('T15', 'PyPI', 23, [
        '(?<![A-Za-z0-9_-])(?P<s>pypi-[A-Za-z0-9_-]{50,})',
    ]),    ('T16', 'npm', 24, [
        '(?<![A-Za-z0-9_-])(?P<s>npm_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])',
    ]),    ('S01', '私钥', 30, [
        '(?s)(?P<s>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----)',
    ]),    ('S02', '私钥', 31, [
        '(?s)(?P<s>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:(?!-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----).)*\\Z)',
    ]),    ('S03', 'JWT', 32, [
        '(?<![A-Za-z0-9_-])(?P<s>eyJ[A-Za-z0-9_-]{8,}\\.eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]*)',
    ]),    ('C01', '连接串密码', 40, [
        '(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\\s:/@]*:(?!〔密钥已挡掉：)(?P<s>[^\\s/]+)@(?=[^\\s/@]+)',
    ]),    ('H01', '请求头凭据', 50, [
        '(?i)(?:proxy-)?authorization[\\"\']?[ \\t]*[:=][ \\t]*[\\"\']?(?:bearer|basic|token)[ \\t]+(?P<s>[A-Za-z0-9._~+/=-]{8,})',
    ]),    ('H02', '请求头密钥', 51, [
        '(?i)(?<![A-Za-z0-9_-])(?:x-api-key|x-auth-token|x-access-token|x-goog-api-key|api-key|ocp-apim-subscription-key)[\\"\']?[ \\t]*:[ \\t]*[\\"\']?(?!〔密钥已挡掉：)(?P<s>[^\\s\\"\',;]{8,})',
    ]),    ('U01', '网址参数', 52, [
        '(?i)(?<=[?&;])(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|password|passwd|pwd|auth|sig|signature|x-amz-signature|x-amz-security-token)=(?!〔密钥已挡掉：)(?!<[^>\\n]*>|\\$|\\{\\{)(?P<s>[^&\\s#\\"\'<>)]+)',
        '(?<=[?&;])key=(?!〔密钥已挡掉：)(?!<[^>\\n]*>|\\$|\\{\\{)(?P<s>[^&\\s#\\"\'<>)]{12,})',
    ]),    ('K01', '键值', 60, [
        '(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,20}[_.-]){0,4}(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key|secret|client[_-]?secret|access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key|token|passwd|password|pwd|passphrase)(?![A-Za-z0-9_])[\\"\']?\\]?[ \\t]*(?::=|=>|=|:|：)[ \\t]*\\"(?!〔密钥已挡掉：)(?!(?:none|null|nil|undefined|true|false|\\*+|x{3,}|-+|\\?+)(?:[\\s,;\\"\'`)\\]}]|$))(?!<[^>\\n]*>|\\$|\\{\\{|%[sd(])(?P<s>[^\\"\\n]+)\\"',
        '(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,20}[_.-]){0,4}(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key|secret|client[_-]?secret|access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key|token|passwd|password|pwd|passphrase)(?![A-Za-z0-9_])[\\"\']?\\]?[ \\t]*(?::=|=>|=|:|：)[ \\t]*\'(?!〔密钥已挡掉：)(?!(?:none|null|nil|undefined|true|false|\\*+|x{3,}|-+|\\?+)(?:[\\s,;\\"\'`)\\]}]|$))(?!<[^>\\n]*>|\\$|\\{\\{|%[sd(])(?P<s>[^\'\\n]+)\'',
        '(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,20}[_.-]){0,4}(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key|secret|client[_-]?secret|access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key|token|passwd|password|pwd|passphrase)(?![A-Za-z0-9_])[\\"\']?\\]?[ \\t]*(?::=|=>|=|:|：)[ \\t]*(?!〔密钥已挡掉：)(?!(?:none|null|nil|undefined|true|false|\\*+|x{3,}|-+|\\?+)(?:[\\s,;\\"\'`)\\]}]|$))(?!<[^>\\n]*>|\\$|\\{\\{|%[sd(])(?P<s>[\\x21\\x23-\\x25\\x28-\\x2b\\x2d-\\x3a\\x3c-\\x5f\\x61-\\x7e]+)',
    ]),    ('K02', '键值', 61, [
        '(?i)(?:API\\s?密钥|API\\s?Key|访问令牌|密码|口令|密钥|秘钥|令牌)[ \\t]*(?:(?:是|为)[ \\t]*[:：]?|[:：=])[ \\t]*[\\"“「‘\'](?!〔密钥已挡掉：)(?!(?:none|null|nil|undefined|true|false|\\*+|x{3,}|-+|\\?+)(?:[\\s,;\\"\'`)\\]}]|$))(?!<[^>\\n]*>|\\$|\\{\\{|%[sd(])(?P<s>[^\\"”」’\'\\n]+)[\\"”」’\']',
        '(?i)(?:API\\s?密钥|API\\s?Key|访问令牌|密码|口令|密钥|秘钥|令牌)[ \\t]*(?:(?:是|为)[ \\t]*[:：]?|[:：=])[ \\t]*(?!〔密钥已挡掉：)(?!(?:none|null|nil|undefined|true|false|\\*+|x{3,}|-+|\\?+)(?:[\\s,;\\"\'`)\\]}]|$))(?!<[^>\\n]*>|\\$|\\{\\{|%[sd(])(?P<s>[\\x21\\x23-\\x25\\x28-\\x2b\\x2d-\\x3a\\x3c-\\x5f\\x61-\\x7e]+)',
    ]),]
_COMPILED = [(rid, kind, pri, re.compile(rx)) for rid, kind, pri, rxs in _RULES for rx in rxs]


class Redaction(NamedTuple):
    text: str
    count: int
    kinds: Tuple[str, ...]
    version: int


def _hits(text):
    out = []
    for rid, kind, pri, rx in _COMPILED:
        named = "s" in rx.groupindex
        for m in rx.finditer(text):
            a, b = m.span("s") if named and m.group("s") is not None else m.span()
            if b > a:
                out.append((a, b, pri, kind, rid))
    return out


def _merge(hs):
    groups = []
    for h in sorted(hs, key=lambda h: (h[0], h[1])):
        if groups and h[0] < groups[-1][1]:
            g = groups[-1]
            g[1] = max(g[1], h[1])
            g[2].append(h)
        else:
            groups.append([h[0], h[1], [h]])
    out = []
    for a, b, members in groups:
        best = sorted(members, key=lambda h: (h[2], h[0], h[4]))[0]
        out.append((a, b, best[3]))
    return out


def redact(text):
    """过滤一段文字。没有命中就原样返回同一个对象。"""
    if not isinstance(text, str):
        raise TypeError("要过滤的必须是文字")
    spans = _merge(_hits(text))
    if not spans:
        return Redaction(text, 0, (), RULES_VERSION)
    out = text
    for a, b, kind in reversed(spans):
        out = out[:a] + PLACEHOLDER % kind + out[b:]
    return Redaction(out, len(spans), tuple(k for _a, _b, k in spans), RULES_VERSION)


def redact_query(text):
    """查询路径用：同一个过滤器，只要文字。"""
    return redact(text).text


def count_by_kind(kinds):
    out = {}
    for k in kinds:
        out[k] = out.get(k, 0) + 1
    return out
