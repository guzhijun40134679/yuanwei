"""切句的共用部分（2026-11-17 起）：在「。！？；」和换行之外，再认英文的句末。检索锚点（索引版本 3）和整理句子（split-2）都用它；
禁读分句（gate.py）用自己的规则，不用这里（禁读先转小写，不能套「下一句大写才断」）。

只做一件事：给一条消息，返回「切成的片段」（起止码点，左闭右开，不去空白，相邻片段首尾相接、换行不在任何片段里）。每个使用者再按自己的规矩去空白、丢短句、处理太长的句子。
旧切法（锚点索引版本 1、2，整理 split-1）原样留在 anchors.py、split.py 里，不经过这里。

在哪断（英文）：句末符号（. ! ? … 和连着的几个，可以连着几个）＋收尾的引号或括号（算这一句的），后面
  * 是空白，空白之后是大写字母、数字、开头的引号或括号、汉字，或者消息到此为止 —— 断；
  * 直接是汉字（不带空白）—— 断（派工后主会话补的一条，照中英双语的用法：`Done.好的`、`OK!明天见`）。
拿不准就少切，原文一个字不丢；小写字母起头的下一句不断（已知限制）；不做语法分析。
例外（只对单个句号）：
  * 称谓类缩写（Dr. Mr. Mrs. Ms. Prof. Sr. Jr. St. Mt. Gen. Col. Capt. Lt. Sgt. Rev. Hon. Fig. 以及 e.g. i.e. cf. vs. viz.）和单个字母加点（J.）：后面不断；
  * 带点的缩写（U.S. p.m. a.m.）：后面有空白和大写时断（`in the U.S. My office`），后面直接是汉字时不断（`U.S.的政策`）；
  * 只由数字构成的片段加点（`1.安装`）后面直接是汉字时不断；
  * 点后面是字母或数字（foo.py、0.1.0a2、example.com/a.b、3.5）本来就不是句末；
  * 项目列表行行首的标记（- * + • 或「数字加 . 或 )」）本身不当句末，行里的英文句子照常断（11-17 主会话改定）；围栏代码块里的行、行内代码（反引号包着的）里的点：不当句末，换行照旧切。
纯函数，不联网、不读密钥、不调模型。
"""
import re

TERMINATORS = ".!?…"
CLOSERS = "\"'”’)]}»」』）】》"
OPENERS = "\"'“‘([{<«「『（【《"
CN_TERMS = "。！？；"
_HAN = "㐀-鿿"
_HAN_RE = re.compile("[" + _HAN + "]")
_LIST_LINE = re.compile(r"^[ \t]*(?:[-*+•][ \t]|\d+[.)][ \t])")
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_TICKS = re.compile(r"`+")
TITLES = frozenset(["dr", "mr", "mrs", "ms", "mx", "prof", "sr", "jr", "st", "mt", "gen", "col", "capt", "lt", "sgt", "rev", "hon", "fig", "e.g", "i.e", "cf", "vs", "viz"])
_DOTTED = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]$")


def _is_han(ch):
    return bool(_HAN_RE.match(ch))


def _token_before(line, i):
    """行内下标 i（单个句号的位置）前面紧挨着的那一串非空白字符（去掉开头的引号括号）。"""
    t = i
    while t > 0 and not line[t - 1].isspace() and ord(line[t - 1]) <= 0x24F:       # 到最近的句界为止：空白，或汉字、中文标点（好的。Dr. 里的「Dr」不带「好的。」）
        t -= 1
    while t < i and line[t] in OPENERS:
        t += 1
    return line[t:i]


def _no_break_abbrev(tok, han_follows):
    """单个句号前的片段是不是不该在这里断的缩写。han_follows＝句号后面不带空白、直接是汉字。"""
    low = tok.lower()
    if low in TITLES:
        return True
    if len(tok) == 1 and tok.isalpha():
        return True
    if han_follows and (_DOTTED.match(tok) or tok.isdigit()):
        return True
    return False


def english_cuts(line, masked=None, start=0):
    """一行里的英文句末位置（片段在这个下标之前结束）。masked：被行内代码盖住的下标集合；start：这个下标之前（项目列表的行首标记）不当句末。"""
    out = []
    n = len(line)
    i = 0
    while i < n:
        if line[i] not in TERMINATORS:
            i += 1
            continue
        j = i
        while j < n and line[j] in TERMINATORS:
            j += 1
        k = j
        while k < n and line[k] in CLOSERS:
            k += 1
        run = line[i:j]
        if i < start or (masked and i in masked):
            i = j
            continue
        cut = False
        han_follows = False
        if k >= n:
            cut = False                                          # 行尾：本来就是片段结尾，不用另切
        elif _is_han(line[k]):
            cut, han_follows = True, True
        elif line[k].isspace():
            m = k
            while m < n and line[m].isspace():
                m += 1
            if m < n:
                c = line[m]
                cut = c.isupper() or c.isdigit() or c in OPENERS or _is_han(c)
        if cut and run == ".":
            tok = _token_before(line, i)
            if _no_break_abbrev(tok, han_follows):
                cut = False
        if cut:
            out.append(k)
        i = j
    return out


def code_mask(line):
    """行内代码盖住的下标集合：开头的反引号串和下一个一样长的反引号串配对（`a` 、``a ` b``）；配不上的不算代码。"""
    runs = [(m.start(), m.end()) for m in _TICKS.finditer(line)]
    out, k = set(), 0
    while k < len(runs):
        n = runs[k][1] - runs[k][0]
        close = next((q for q in range(k + 1, len(runs)) if runs[q][1] - runs[q][0] == n), None)
        if close is None:
            k += 1
            continue
        out.update(range(runs[k][0], runs[close][1]))
        k = close + 1
    return out


def pieces(content):
    """一条消息切成的片段 [(起, 止)]：按换行、「。！？；」和英文句末切开（换行不在任何片段里，其余字符都在）。"""
    out = []
    pos = 0
    fence = None                              # 开着的围栏：(字符, 长度)；只有同字符、不短于开启长度、后面只有空白的行才关闭它
    for line in content.split("\n"):
        base = pos
        pos += len(line) + 1
        if not line:
            continue
        fence_line = False
        if fence is None:
            m = _FENCE_OPEN.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):                 # 反引号围栏的说明文字里不能有反引号；有就是行内代码
                fence, fence_line = (m.group(1)[0], len(m.group(1))), True
        else:
            fence_line = True
            m = re.match(r"^[ \t]*(" + re.escape(fence[0]) + r"{%d,})[ \t]*$" % fence[1], line)
            if m:
                fence = None
        in_fence = fence is not None
        cuts = set()
        for idx, ch in enumerate(line):
            if ch in CN_TERMS:
                cuts.add(idx + 1)
        if not (in_fence or fence_line):
            lm = _LIST_LINE.match(line)
            masked = code_mask(line) if "`" in line else None
            cuts.update(english_cuts(line, masked, lm.end() if lm else 0))
        a = 0
        for c in sorted(cuts):
            if c < len(line):
                out.append((base + a, base + c))
                a = c
        out.append((base + a, base + len(line)))
    return out
