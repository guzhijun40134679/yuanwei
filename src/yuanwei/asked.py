"""用户回答助手提问（提问卡、选项卡）：和宿主无关的共用一层（清单第 53 条，0.1.0a8）。

助手在一轮中间用宿主的「问用户」工具（Hermes 的 clarify 等）出几道题，用户点选或自己打字；答案作为工具结果回给助手，不是用户消息。
这里把这些回答当作用户原话，按固定模板生成几行，由适配器接在这一轮用户那条话后面（同一条、同一轮、同一个编号，不新增角色）。

* **输入**是中性的结构（各宿主那一小层负责认出、解析、转成它）：
  ``[{"question": str, "choices": [str, …] 或 None, "answers": [{"text": str, "picked": bool}, …]}, …]``，
  外加宿主层带来的备注（固定名字，见下）。picked＝用户点的是助手给的某个选项（文字和选项一字不差）；否则是用户自己打的字。
* **输出**（Folded）：要追加的行、备注（固定名字，可重复：一处一条）、存下的回答条数、用来认「关」口令的打字回答原文（只在内存里用，不落盘）、
  所有回答原文（只给适配器认「确认开启／确认删除」以便提示用户「这不算数」，不落盘）。
* **纯函数**：不读宿主、不读库、不看时间。同一份输入永远得到同样的行，所以以后从历史对话补回漏掉的回答，用它得到的行和实时写入时逐字相同。
  这个模块不 import 原委的别的模块（切句、排名、卡片、删除都要用这里的标签，不能绕成循环导入）。

模板（每一物理行都带标签；题目、选项、回答里的换行后面都补上同一个标签）：
    （助手问）题目
    （助手给的选项）1）甲／2）乙        ← 只要助手给了选项就写（用户 11-21 定：点选、打字都写）
    （用户选）甲                        ← 和某个选项一字不差
    （用户答）自己打的字
提问卡里的回答和用户直接发的话一视同仁（用户 11-21 定）：原样存，现有的密钥过滤照常挡掉认得出的密钥格式，别的字（包括打进来的普通密码）照打的存。
护栏：题目、选项、回答里有孤立代理字符的，这一题不存（clarify_dropped:bad_text）；题目、选项里出现这几个标签字样的，这一题不存
（clarify_dropped:tag_in_text）。拼上以后超过长消息线：先去掉所有选项行（clarify_options_trimmed）；还超就存去掉了选项的那份、另记 clarify_long。
标签字符不在那 18 对替代字符里、不会被密钥过滤挡掉（测试核过）；但一行里的「。！？；」、英文句末、过长时的逗号照样是切句点，
所以按句子显示的地方（整理请求、主线卡片、attachments）要用 shown() 补上这一行的标签（只改显示，不改存下的字、不改锚点）。
"""
import re
from typing import NamedTuple, Tuple

LONG_LIMIT = 1500               # 和 config.LONG_LIMIT 相同（测试核过）；这里不 import config，免得 config → cards → asked → config 绕成循环

TAG_Q = "（助手问）"
TAG_OPTS = "（助手给的选项）"
TAG_PICK = "（用户选）"
TAG_TYPED = "（用户答）"
TAG_SAID = "（用户中途又说）"                   # 清单第 55 条（0.1.0a9）：助手没答完时用户又发的话，来源核实过（改口；聊天平台上来源说明认得出的插话）
TAG_SAID_UNVERIFIED = "（中途插入·来源未核实）"  # 同上（间隔号 U+00B7，不用逗号：切句和检索切锚点都在逗号处切，标签里不能有切点），来源没核实（本机的插话：桌面端页面脚本忙时也走这条，分不出是不是用户亲手打的）
MID_TAGS = (TAG_SAID, TAG_SAID_UNVERIFIED)
OPT_NUM = "%d）"
OPT_SEP = "／"
TAGS = (TAG_Q, TAG_OPTS, TAG_PICK, TAG_TYPED) + MID_TAGS

# 备注（固定名字；以 clarify_ 开头的都表示「这一轮照常记了，备注说的是提问卡那一部分怎么了」，不是「这一轮没记」）
NOTE_BAD_TEXT = "clarify_dropped:bad_text"
NOTE_TAG_IN_TEXT = "clarify_dropped:tag_in_text"
NOTE_OPTIONS_TRIMMED = "clarify_options_trimmed"
NOTE_LONG = "clarify_long"
# 中途的话的备注（清单第 55 条）：这一轮照常记了，备注说的是中途那部分怎么了；进 clarify-counts.json，不进 skip-reasons.json
NOTE_MID_BAD_TEXT = "midturn_dropped:bad_text"
NOTE_MID_UNMATCHED = "midturn_dropped:unmatched"            # 开头和助手回复照记，改口的字没法和行对上，一个字不存
NOTE_MID_UNCLEAR = "midturn_dropped:turn_unclear"
NOTE_MID_ENVELOPE = "midturn_dropped:unknown_envelope"
NOTE_MID_FROM_BOT = "midturn_dropped:from_bot"
NOTE_MID_INTERNAL = "midturn_dropped:internal_error"
NOTE_STEER_SHAPE = "steer_dropped:unknown_shape"
NOTE_STEER_HOST = "steer_dropped:host_note"
NOTE_OPENING_ONLY = "opening_only:row_not_found"                  # 开头行被宿主压缩改写、没找到：宿主证明是用户开的，只记了开头句和回复（清单第 56 条第 5 节）
NOTE_MID_KEPT = "midturn_kept"                              # 存下一条记一次

# 编码映射表（encoding.py）前 10 个换行类字符的替代字符：交给模型时一律是这些字，和真的换行分不出来；来源没核实的话里出现它们，后面也补上同一个标签，不然能用它们造出一行假的「（用户中途又说）」
_BREAK_REPLACEMENTS = "␊␍␋␌␜␝␞␤↵⁋"
_BREAK_UNVERIFIED = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029" + _BREAK_REPLACEMENTS + "]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
# 物理换行：和编码映射表里前 10 个「换行类」字符一致（\r\n 算一个）
_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


class Folded(NamedTuple):
    lines: Tuple[str, ...]          # 要追加到用户那条话后面的行（适配器用 "\n" 接上；一项里如果有换行，换行后面已经补了标签）
    notes: Tuple[str, ...]          # 备注（固定名字；一处一条，可重复）
    answers: int                    # 存下的回答条数（用户选＋用户答）
    texts: Tuple[str, ...]          # 用户**打的**回答原文，只给适配器在内存里认「关」口令；点选的不在里面（那是助手写的字）
    all_texts: Tuple[str, ...] = ()  # 所有回答原文（打的＋点的），只给适配器认「确认开启／确认删除」以便提示「提问卡里的不算数」，不改任何状态
    said: int = 0                   # 存下的「中途又说」条数（改口、插话，不管核没核实）
    said_texts: Tuple[str, ...] = ()  # 它们的原文，只给适配器在内存里认「关」「确认开启」「确认删除」「#yuanwei-mem」，不落盘


def _tagged(tag, text, brk=_BREAK):
    """标签＋文字；文字里的每个物理换行后面都再补一个同样的标签（原来的换行字符照留）。brk：什么算换行（来源没核实的话把换行的替代字符也算上）。"""
    return brk.sub(lambda m: m.group(0) + tag, tag + text)


def _check_items(items):
    if not isinstance(items, (list, tuple)):
        raise ValueError("items 必须是列表")
    for it in items:
        if isinstance(it, dict) and "said" in it:                                # 中途又说的一条（清单第 55 条）：{"said": 文字, "verified": 真假}
            if not isinstance(it["said"], str) or type(it.get("verified")) is not bool or set(it) != {"said", "verified"}:
                raise ValueError("中途的话要是 {said: 文字, verified: 真假}")
            continue
        if not isinstance(it, dict) or not isinstance(it.get("question"), str):
            raise ValueError("每一题要有文字的 question")
        ch = it.get("choices")
        if ch is not None and (not isinstance(ch, (list, tuple)) or not all(isinstance(c, str) for c in ch)):
            raise ValueError("choices 要么是 None，要么是文字列表")
        ans = it.get("answers")
        if not isinstance(ans, (list, tuple)) or not all(isinstance(a, dict) and isinstance(a.get("text"), str) and isinstance(a.get("picked"), bool) for a in ans):
            raise ValueError("answers 要是 [{text: 文字, picked: 真假}]")


def render(items, opening="", limit=LONG_LIMIT, notes=()):
    """items（中性结构，见模块说明）→ Folded。opening 是用户那条话原来的样子（只用来量长度，不改它）；limit 是长消息线（超过不送去整理）。
    notes：宿主那一层带来的备注（固定名字），原样排在前面。没有能存的回答：lines 为空（这一轮存下的东西和没有提问卡时逐字相同）。
    长度按密钥过滤之前的字数量（库里判长短用过滤之后的；只在回答里有被挡掉的密钥、又正好卡在线上时两者才不一样，已知局限）。"""
    _check_items(items)
    out_notes = list(notes)
    blocks = []                     # 每题一块：[(是否选项行, 行)]
    answers, texts, all_texts = 0, [], []
    said, said_texts = 0, []
    for it in items:
        if "said" in it:                                                   # 中途又说的一条：按先后和提问卡的题排在一起，每个物理行都带标签
            if not it["said"].strip():
                continue
            if _SURROGATE.search(it["said"]):
                out_notes.append(NOTE_MID_BAD_TEXT)                       # 孤立代理字符：这一条不存（存进库会让整轮报错丢掉）；别的照存
                continue
            blocks.append([(False, _tagged(TAG_SAID, it["said"]) if it["verified"] else _tagged(TAG_SAID_UNVERIFIED, it["said"], _BREAK_UNVERIFIED))])
            said += 1
            said_texts.append(it["said"])
            continue
        q, choices = it["question"], list(it.get("choices") or [])
        given = [a for a in it["answers"] if a["text"].strip()]
        if not given:
            continue                                                       # 没答的、跳过的：不存，也不记备注
        texts.extend(a["text"] for a in given if not a["picked"])
        all_texts.extend(a["text"] for a in given)
        if any(_SURROGATE.search(s) for s in [q] + choices + [a["text"] for a in given]):
            out_notes.append(NOTE_BAD_TEXT)                               # 孤立代理字符：这一题不存（存进库会让整轮报错丢掉）
            continue
        if any(t in s for t in TAGS for s in [q] + choices):
            out_notes.append(NOTE_TAG_IN_TEXT)                            # 助手的字里有我们的标签：会冒充用户的回答，这一题不存
            continue
        block = [(False, _tagged(TAG_Q, q))]
        if choices:
            block.append((True, _tagged(TAG_OPTS, OPT_SEP.join(OPT_NUM % (i + 1) + c for i, c in enumerate(choices)))))
        for a in given:
            if a["picked"]:
                block.append((False, _tagged(TAG_PICK, a["text"])))
                answers += 1
            else:
                block.append((False, _tagged(TAG_TYPED, a["text"])))
                answers += 1
        blocks.append(block)
    full = [line for b in blocks for _o, line in b]

    def size(lines):
        return len(opening) + (1 + len("\n".join(lines)) if lines else 0)
    lines = full
    if full and size(full) > limit:                                        # 拼上回答超了长消息线（开头那句本来就超的也一样先缩选项，用户 11-21 定）
        trimmed = [line for b in blocks for o, line in b if not o]
        if len(trimmed) < len(full):
            lines = trimmed                                                # 先去掉所有选项行
            out_notes.append(NOTE_OPTIONS_TRIMMED)
        if size(lines) > limit:
            out_notes.append(NOTE_LONG)                                    # 还超：存去掉选项的那份（回答一个字不少），记一笔好数次数
    return Folded(tuple(lines), tuple(out_notes), answers, tuple(texts), tuple(all_texts), said, tuple(said_texts))


def fold(opening, lines):
    """把行接到用户那条话后面（适配器照这个接）。没有行就原样返回。"""
    return opening + ("\n" + "\n".join(lines) if lines else "")


def strip_tags(text):
    """把标签字样换成空格（只给关键词分词用：「助手」「用户」「选项」这些标签里的字不该让每一条提问卡都算命中）。没有标签的字原样返回。"""
    if "（" not in text:
        return text
    for t in TAGS:
        if t in text:
            text = text.replace(t, " ")
    return text


_OPENERS = ("\n" + TAG_Q, "\n" + TAG_SAID, "\n" + TAG_SAID_UNVERIFIED)


def opening_of(content):
    """一条（可能折进了提问卡回答、中途又说的话的）用户话里，用户自己打的那一段：切在第一处「换行＋（助手问）／（用户中途又说）／（中途插入·来源未核实）」之前。没有就原样返回。"""
    cut = [i for i in (content.find(o) for o in _OPENERS) if i >= 0]
    return content if not cut else content[:min(cut)]


def line_tag(content, start):
    """按句子显示时用：content 里从 start 开始的这一句，如果自己不以标签开头、而它所在的物理行以我们的标签开头，返回那个标签；否则 None。
    只认折进了提问卡回答或中途又说的话的话（里面有「换行＋（助手问）／（用户中途又说）／（中途插入·来源未核实）」）；别的话一律 None（显示逐字节不变）。
    中途的标签（0.1.0a9）：所在的物理行以它开头时，这一句正好在行首就是 None，否则一律返回它——不管这一句自己是不是以别的标签开头（那是用户中途说的字，不是我们加的标签）。"""
    if not isinstance(content, str) or not any(o in content for o in _OPENERS) or not isinstance(start, int) or start < 0:
        return None
    ls = 0
    for m in _BREAK.finditer(content, 0, start):
        ls = m.end()
    for t in MID_TAGS:
        if content.startswith(t, ls):
            return None if start == ls else t
    if content.startswith(TAGS[:4], start):                                                      # 只看四个旧标签：中途标签字样出现在别的行中间，是用户自己打的字，显示和 0.1.0a8 一样
        return None
    for t in TAGS:
        if content.startswith(t, ls):
            return t
    return None


def shown(content, start, text):
    """按句子显示的文字：要补标签就补上（见 line_tag），不然原样。"""
    t = line_tag(content, start)
    return t + text if t else text
