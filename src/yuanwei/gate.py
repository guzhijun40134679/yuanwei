"""读取权限的文字识别，和便条的免查门。两件事分开：

1. **文字识别禁读**（`read_ban`／`text_permission`）：用户这一句话里有没有明确的「这一轮别读／别用长期记忆或以前的对话」。这是读取权限的一部分，**始终执行**，和免查门开不开无关；
   规则是产品自己的（不是研究路由器验证过的）。按「对长期记忆或以前对话的读取、使用」来认，不按单个词：单独出现的 forget、fresh、ignore、历史、记录都不算，它们可能说的是程序、报错、版本历史、测试样例。
2. **免查门**（`free_check`）：只管寒暄、应答和宿主命令，三类不查，其余一律查。`note_free_gate` 只开关这一道，不影响第 1 项。

规则素材取自 hermes-pro/agent/turn_router.py 的寒暄词表和拒用思路，判法不同，见 docs/产品规则表.md（哪些是新改的也标在那里）。
"""
import re
import unicodedata

# ================================================================ 免查门
CLOSED_SET = frozenset([
    "你好", "您好", "早上好", "中午好", "晚上好", "晚安", "在吗", "嗨", "哈喽", "谢谢", "多谢", "感谢", "辛苦了", "哈哈", "呵呵", "嘻嘻",
    "hi", "hello", "hey", "yo", "sup", "good morning", "good afternoon", "good evening", "good night", "thanks", "thank you", "thx", "ty", "lol", "haha", "hehe",
    "好的", "好", "嗯", "嗯嗯", "行", "可以", "收到", "ok", "okay",
])
PRODUCT_NEW_SET = frozenset(["好的", "好", "嗯", "嗯嗯", "行", "可以", "收到", "ok", "okay"])          # 产品新加的应答（其余来自研究 _GREETING）
TRAILING_TONE = "。.!~…"                                                                            # NFKC 之后只剥句尾这几个；问号、括号、反引号等一律不剥


def normalize(text):
    return unicodedata.normalize("NFKC", text).lower().replace("’", "'").strip()


def closed_set_form(text):
    """闭集比较用的形式：NFKC、小写、去首尾空白，再只剥句尾的语气标点（剥完再去一次尾部空白）。"""
    return normalize(text).rstrip(TRAILING_TONE).rstrip()


def free_check(text, host_command=False):
    """返回 {"decision": "check"|"skip_greeting"|"skip_command", "rule": 规则名或 None}。只管寒暄、应答、宿主命令；**不管禁读**（禁读见 text_permission）。
    宿主命令必须由调用方显式标明 host_command=True，不从斜杠开头反推。空白或不是文字：报错（不当成寒暄）。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("便条的查询必须是非空文字")
    if host_command is True:
        return {"decision": "skip_command", "rule": "host_command"}
    if closed_set_form(text) in CLOSED_SET:
        return {"decision": "skip_greeting", "rule": "closed_set"}
    return {"decision": "check", "rule": None}


# ================================================================ 文字识别禁读（2026-11-08 补充施工单二改过：撤掉「带功能／开关／设置就当讨论」，改成按对长期记忆的读取、使用来认）
CLAUSE_SPLIT = re.compile(r"[，,。;；!！?？\n]")
QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("‘", "’"), ("《", "》"))
QUESTION_LEAD = ("为什么", "为何", "怎么", "如何", "why ", "how ")                  # 以这些开头的分句在问这个功能本身，不算本轮禁令
PERSIST_ZH = ("以后", "今后", "往后", "从现在起", "从此", "永远", "一直", "始终", "每次")
PERSIST_EN = ("from now on", "always", "forever", "never again", "in the future", "going forward", "from here on")

# ---- 中文
NEG_ZH = ("不要", "请勿", "不许", "不准", "禁止", "别")                                # 要接读取／使用动词
NEG_ZH_MORE = ("不希望", "不想", "不愿", "不能", "不可以", "不应该", "不应", "不必", "无需", "无须")     # 窗口禁读单补的否定词（用户 11-10：宁可多拒）；「能不能」这类正反问不算（见 _a_not_a）
PARTICLE_ZH = ("和", "跟", "与", "同")                                                          # 「不要和我记忆里的核对」：否定词后面直接接「和/跟/与/同＋记忆」
IMPLICIT_ZH = ("不用", "忽略", "别管", "不带", "无视", "不考虑")                         # 本身就带「不用」的意思，后面直接接记忆说法
READ_ZH = ("使用", "参考", "调用", "引用", "依赖", "访问", "用", "看", "查", "翻", "读", "调", "带", "搜", "提")
EDIT_ZH = ("修改", "删除", "写入", "记下", "记进", "记到", "记入", "记在", "记住", "保存", "存进", "存到", "存入", "写进", "写到", "录入", "改", "删", "写", "存")        # 修改、写入、保存一类：对象是记忆也不算禁读（补充三：写入要求不是读取禁令）
NOUN_ZH_RX = re.compile(
    r"(?:以前|之前|先前)的?(?:对话|聊天)|(?:以前|之前|先前)(?:聊|说|讲)过?的(?=$|内容|话|东西|对话|事|上下文)|聊天记录|聊天历史|历史记录|历史对话|上下文记录|记忆(?!化)")
NOT_MEMORY_PREFIX_ZH = ("git", "版本", "提交", "commit", "浏览", "浏览器", "命令", "终端", "构建", "build", "shell", "操作")      # 「git 历史记录」「浏览记录」这类不是对话记忆
GAP_NEG_VERB = 4
GAP_VERB_NOUN = 12
GAP_IMPLICIT_NOUN = 8

# ---- 英文
EN_EXPLICIT = re.compile(r"\b(?:don't|do not|dont|never)\s+(?:(?:please|just|ever|now|really|even|go and|go|ever)\s+)*(?:use|check|look at|read|refer to|consult|rely on|access|search|load|bring up|mention|reference|pull in)\b([^,.;!?]{0,40})")
EN_IMPLICIT = re.compile(r"\b(ignore|disregard|without|no)\b((?:\s+(?:all|any|the|my|our|your|anything|everything|of))*)\s+(?=\S)([^,.;!?]{0,40})")
_TECH = r"(?![- ]?(?:leak|pool|usage|limit|address|allocation|alloc|mapped|map|management|footprint|error|dump|bank|cache|layout|barrier|order|safe|safety|bound|bandwidth))"
EN_NOUN = re.compile(r"\b(?:(?:previous|past|prior|earlier)\s+(?:conversations?|chats?|sessions?|discussions?|messages?)|chat history|conversation history|context from before|(?:my|our|your|the|long-term|saved|stored)\s+memor(?:y|ies)" + _TECH + r")\b")
EN_NOUN_BARE = re.compile(r"\bmemor(?:y|ies)\b" + _TECH)


def _strip_quotes(t):
    for a, b in QUOTE_PAIRS:
        if a == b:
            t = re.sub(re.escape(a) + r"[^" + re.escape(a) + r"]*" + re.escape(a), " ", t)
        else:
            t = re.sub(re.escape(a) + r"[^" + re.escape(a + b) + r"]*" + re.escape(b), " ", t)
    return t


def _noun_near(s, limit):
    """s 开头 limit 个字内有没有「对长期记忆或以前对话」的说法（排除 git 历史记录这类前缀）。返回是否有。"""
    window = s[:limit + 8]
    for m in NOUN_ZH_RX.finditer(window):
        if m.start() > limit:
            break
        before = window[:m.start()]
        if any(before.endswith(p) or before[-len(p) - 1:-1] == p for p in NOT_MEMORY_PREFIX_ZH):
            continue
        return True
    return False


def _first_verb(rest):
    """rest 开头附近（间隔不超过 GAP_NEG_VERB 个字）最早出现的动词：返回 ("read"|"edit", 动词结束位置)，没有返回 None。同一位置先看长的。"""
    best = None
    for kind, words in (("edit", EDIT_ZH), ("read", READ_ZH)):
        for w in words:
            i = rest.find(w, 0, GAP_NEG_VERB + len(w))
            if i < 0:
                continue
            key = (i, -len(w))
            if best is None or key < best[0]:
                best = (key, kind, i + len(w))
    return None if best is None else (best[1], best[2])


def _a_not_a(c, p, neg):
    """「能不能」「可不可以」「想不想」这类正反问里的「不能」「不可以」「不想」不是否定：否定词前面紧挨着它自己后半段的开头（能|可|可以|想…）就跳过。只用于单补的否定词。"""
    tail = neg[1:]
    return any(tail[:k] and c[max(0, p - k):p] == tail[:k] for k in range(1, len(tail) + 1))


_PRE = r"(?:不过|但是|但|可是|还是|所以|那就|先|现在|这次|这轮|这一轮|本轮|暂时|目前|就|请|你|这个|这|那个|它|记忆(?:功能)?|我们|咱们)*"
BARE_READ_RX = re.compile(r"^" + _PRE + r"(?:不要|别|不用|请勿|不许|不准|不能|不可以)再?(?:使用|调用|参考|搜索|查|用|看|翻|读|搜)(?:了|吧|啊|哈|哦|呀)?$")
BARE_OPEN_RX = re.compile(r"^" + _PRE + r"(?:先|暂时|目前|现在)(?:不要|别|不)(?:要)?(?:开启|开|打开|启用)(?:了|吧|啊|哈|哦|呀)?$")


def _bare_ban(c):
    """没有宾语的短句：「现在不要查」「不过先别用」「这个先不开」。必须整句就是这几个字（前面只能有时间、语气这类词），所以「这个库别用」「别看git历史记录」不受影响。"""
    return bool(BARE_READ_RX.match(c) or BARE_OPEN_RX.match(c))


def _zh_clause_ban(c):
    for word in IMPLICIT_ZH:
        p = c.find(word)
        while p >= 0:
            rest = c[p + len(word):]
            v = _first_verb(rest)
            if v is not None and v[0] == "edit":                                         # 「不用记进记忆」「别存」：限制的是写入，不是读取；只对这一个否定词，同一分句里另有真正的禁读照样拦
                p = c.find(word, p + 1)
                continue
            if _noun_near(rest, GAP_IMPLICIT_NOUN):
                return True
            p = c.find(word, p + 1)
    for neg in NEG_ZH + NEG_ZH_MORE:
        start = 0
        while True:
            p = c.find(neg, start)
            if p < 0:
                break
            start = p + 1
            if neg in NEG_ZH_MORE and _a_not_a(c, p, neg):
                continue
            rest = c[p + len(neg):]
            v = _first_verb(rest)
            if v is None:
                if rest.startswith(PARTICLE_ZH) and _noun_near(rest[1:], GAP_VERB_NOUN):    # 「不要和我记忆里的核对」
                    return True
                continue
            if v[0] == "edit":                                                           # 修改类不算禁读
                continue
            if _noun_near(rest[v[1]:], GAP_VERB_NOUN):
                return True
    return _bare_ban(c)


def _en_clause_ban(c):
    m = EN_EXPLICIT.search(c)
    if m and (EN_NOUN.search(m.group(1)) or EN_NOUN_BARE.search(m.group(1))):
        return True
    for m in EN_IMPLICIT.finditer(c):
        neg, rest = m.group(1), m.group(3)
        head = c[:m.start()].split()
        if neg == "no" and len(head) > 1:                                               # 「no」只在分句开头算（避免 I have no memory of it、there is no memory leak 这类）
            continue
        window = rest[:40]
        if EN_NOUN.search(window):
            return True
        if len(c.split()) <= 6 and EN_NOUN_BARE.match(window.strip()):
            return True                                                                 # 「no memory please」「ignore memory」这类很短的祈使句
    return False


def clause_is_ban(c):
    """一个分句（已 normalize、去过引号）是不是禁读分句：以「为什么、怎么、why、how」开头的在问功能本身，不算。"""
    return (not c.startswith(QUESTION_LEAD)) and (_zh_clause_ban(c) or _en_clause_ban(c))


def ban_clauses(text):
    """按分句给出（分句文字，是不是禁读分句）的列表，顺序同原文；引号里的话先去掉。read_ban 就是「其中有一个为真」。给窗口层按先后顺序判最后的表态用。"""
    t = _strip_quotes(normalize(text))
    out = []
    for c in CLAUSE_SPLIT.split(t):
        c = c.strip()
        if not c:
            continue
        out.append((c, clause_is_ban(c)))
    return out


def read_ban(text):
    """按分句认当前这句话里有没有明确的「别读／别用长期记忆或以前的对话」。引号里的话不算；以「为什么、为何、怎么、why、how」开头的分句（在问这个功能本身）不算；
    修改类（改、修改、删、删除、写、写入、存、记下）不算禁读。单独的 forget、fresh、ignore、历史、记录不算。"""
    t = _strip_quotes(normalize(text))
    for c in CLAUSE_SPLIT.split(t):
        c = c.strip()
        if not c or c.startswith(QUESTION_LEAD):
            continue
        if _zh_clause_ban(c) or _en_clause_ban(c):
            return True
    return False


def is_persistent(text):
    """这句话里有没有「以后都、从现在起」这类持续的说法（只用来在说明里指向持续开关，不自动改任何设置）。"""
    t = normalize(text)
    return any(w in t for w in PERSIST_ZH) or any(w in t for w in PERSIST_EN)


def text_permission(text):
    """文字识别的读取权限：返回 {"allowed": bool, "rule": "clause_ban"|None, "persistent": bool}。始终执行，和免查门开不开无关。空白或不是文字：报错。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("查询必须是非空文字")
    if read_ban(text):
        return {"allowed": False, "rule": "clause_ban", "persistent": is_persistent(text)}
    return {"allowed": True, "rule": None, "persistent": False}
