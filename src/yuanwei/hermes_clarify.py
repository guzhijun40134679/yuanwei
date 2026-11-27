"""Hermes 这一小层：认出这一轮里「问用户」工具的结果，严格解析，按 tool_call_id 配对，转成 asked.py 的中性结构（清单第 53 条，0.1.0a8）。

不 import Hermes，只读 sync_turn 拿到的 messages（只读，不改）。

* **白名单**（ASK_USER_TOOLS）：只认明确是「问用户」的工具。Hermes 目前只有 clarify。白名单以外的工具结果一律不读、不存
  （会让用户输密码、做确认的那些——sudo、密钥录入、保险箱——本来就不出 clarify 结果行，也不在白名单里）。以后加宿主、加工具就在这里加，先核它的结果格式、有没有可能带密码。
* **只认现行格式**（Hermes 5eea87882a 起，v0.21.6 到 v0.23.2 `ae2a81ccc0`；tools/clarify_tool.py 的 _result）：
  ``{"responses": [{question, choices_offered, status, user_response}], "outcome"[, "notice"]}``，JSON 后面只许跟 "\\n\\n" 开头的宿主附注（插件便条、子目录提示；附注不存）。
  格式不对就整条拒收、记备注，不猜、不抢救，这一轮其余照 0.1.0a7 存。备注分三种（0.1.0a8 复核后分开，好让「格式变了」这个信号干净）：
  - clarify_dropped:host_rewritten：Hermes 自己固定的改写——裁剪留下的「[clarify] user responded: …」、去重占位、「[Result unavailable…」、溢出到文件的 <persisted-output>、
    同一轮里一字不差的重复结果换成的「[hermes note: this result is byte-identical …」（agent/tool_guardrails.py）；
  - clarify_dropped:bad_json：以「{」开头但 JSON 解不开——已知的原因是 Hermes 外发前的密钥清洗（agent/redact.py 的 _ENV_ASSIGN_RE 用「非空白一串」，
    把 OPENAI_API_KEY=… 后面的右引号和标点一起吃掉），这时整张卡的回答都收不到；
  - clarify_dropped:unknown_shape：别的一切（0.20.0 的平铺格式、935c 那一版、多了少了键、未知的 outcome/status、列表正文……）＝格式可能变了，先核再放宽。
  没解析成功的正文一律不读里面的字（也不认「关」口令）。{"error": …}（子代理里、回调出错、策略拒绝）没有用户的话，静默略过。
* **只存答了的**：status 是 answered 的才转；skipped、unanswered、outcome、notice、报错、附注都不存。
  多选里 Hermes 去掉「(Recommended)」后变空的项只丢那一项，其余照收。
* **配对**：这一轮（当前用户行之后）助手行 tool_calls 里的 id、call_id（照 Hermes _normalize_tool_call_id，「编号|别的」取前半）才算数；
  没有 id 的永远不进配对集合；结果行没有 tool_call_id、编号对不上、同一个编号第二次出现、name 和 tool_name 不一致：clarify_dropped:unpaired。
  不核助手调用里的函数名（tool_search 包一层时调用名是 tool_call，结果行仍是 clarify）。
* **认不清是哪一轮**：当前用户行（按文字往回找到的）之后，如果还有一条像是「一轮开头」的用户行，说明找到的可能是更早一轮的同字行，
  这一轮的提问卡结果一律不收，记 clarify_dropped:turn_unclear。像一轮开头的（turn_start）：
  - 以 Hermes 技能脚手架开头的（TURN_START_PREFIXES：「[IMPORTANT: The user has invoked the 」「[IMPORTANT: The "」，agent/skill_commands.py）；
  - 去掉两头空白后以这一轮用户那句话结尾的（宿主把标记行并进了当前行，例如终端界面换模型的「[System: …]\n\n继续」）；
  - 其余不以「[」开头的，但 Hermes 自己固定追加的催促行（HOST_NUDGE_TEXTS：到达工具调用上限、压缩后续接，agent/context_compressor.py）除外。
  这两张表由 doctor 的 clarify_turn_markers 核对。
* **只在一对一里收**：这一层不管；适配器按平台的白名单判（见 hermes_adapter.PRIVATE_CHATS；共用的会话里别人也能答主人的卡，结果里没有答题人）。
"""
import json

from . import asked as AS

ASK_USER_TOOLS = frozenset({"clarify"})           # 白名单：Hermes 里明确是「问用户」的工具
OUTCOMES = frozenset({"submitted", "cancelled", "timed_out", "undelivered"})
STATUSES = frozenset({"answered", "skipped", "unanswered"})
RESULT_KEYS = frozenset({"responses", "outcome"})
RESULT_OPTIONAL = frozenset({"notice"})
RESP_KEYS = frozenset({"question", "choices_offered", "status", "user_response"})

# Hermes 固定的改写（不是格式变了）：裁剪、去重、结果不可用、溢出到文件
HOST_REWRITE_PREFIXES = ("[clarify] user responded:", "[Duplicate tool output", "[Result unavailable", "<persisted-output>",
                         "[hermes note: this result is byte-identical")     # 最后一个：同一轮里结果一字不差地重复时换成的引用（agent/tool_guardrails.py 的 _build_result_reference_stub）
# 认不清是哪一轮：Hermes 技能脚手架的开头（agent/skill_commands.py 的 _SKILL_INVOCATION_PREFIX、_AUTO_LOAD_PREFIX）——当一轮开头看
TURN_START_PREFIXES = ("[IMPORTANT: The user has invoked the ", '[IMPORTANT: The "')
# Hermes 自己追加的固定催促行（agent/context_compressor.py 的 COMPRESSION_CONTINUATION_USER_CONTENT、_LEGACY_…、MAX_ITERATIONS_SUMMARY_REQUEST）——当脚手架看
HOST_NUDGE_TEXTS = frozenset({
    "Continue from the compressed conversation context above. This marker exists because no human user turn was available.",
    "Continue from the compressed conversation context above. This marker exists because the compacted transcript contained no preserved user turn.",
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools.",
})

NOTE_UNPAIRED = "clarify_dropped:unpaired"
NOTE_UNKNOWN = "clarify_dropped:unknown_shape"
NOTE_BAD_JSON = "clarify_dropped:bad_json"
NOTE_HOST_REWRITTEN = "clarify_dropped:host_rewritten"
NOTE_SHARED_CHAT = "clarify_dropped:shared_chat"
NOTE_TURN_UNCLEAR = "clarify_dropped:turn_unclear"
NOTE_INTERNAL = "clarify_dropped:internal_error"


def norm(v):
    """Hermes 的 _normalize_tool_call_id：「编号|别的」取前半、去两头空白；不是非空文字（或取完是空）返回 None。"""
    if not isinstance(v, str) or not v:
        return None
    v = v.split("|", 1)[0].strip()
    return v or None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def parse(content):
    """一条 clarify 结果的正文 → ("ok", responses) ｜ ("error", None) ｜ ("host_rewritten", None) ｜ ("bad_json", None) ｜ ("unknown_shape", None)。
    只认现行格式；Hermes 固定的改写、解不开的 JSON、别的形状分开报（见模块说明），都不抢救。"""
    if isinstance(content, str) and content.startswith(HOST_REWRITE_PREFIXES):
        return "host_rewritten", None
    if not isinstance(content, str) or not content.startswith("{"):
        return "unknown_shape", None
    try:
        obj, end = json.JSONDecoder().raw_decode(content, 0)
    except ValueError:
        return "bad_json", None
    rest = content[end:]
    if rest and not rest.startswith("\n\n"):
        return "unknown_shape", None
    if not isinstance(obj, dict):
        return "unknown_shape", None
    if set(obj) == {"error"}:
        return "error", None
    keys = set(obj)
    if not (RESULT_KEYS <= keys <= RESULT_KEYS | RESULT_OPTIONAL):
        return "unknown_shape", None
    if not isinstance(obj["outcome"], str) or obj["outcome"] not in OUTCOMES or not isinstance(obj["responses"], list) or not obj["responses"]:
        return "unknown_shape", None
    if "notice" in obj and not isinstance(obj["notice"], str):
        return "unknown_shape", None
    for r in obj["responses"]:
        if not isinstance(r, dict) or set(r) != RESP_KEYS:
            return "unknown_shape", None
        q, ch, st, ur = r["question"], r["choices_offered"], r["status"], r["user_response"]
        if not isinstance(q, str) or not q:
            return "unknown_shape", None
        if ch is not None and (not isinstance(ch, list) or not all(isinstance(c, str) for c in ch)):
            return "unknown_shape", None
        if not isinstance(st, str) or st not in STATUSES:
            return "unknown_shape", None
        if st == "answered":
            if isinstance(ur, str):
                if not ur:
                    return "unknown_shape", None
            elif isinstance(ur, list):
                if not ur or not all(isinstance(x, str) for x in ur):
                    return "unknown_shape", None                          # 列表里每一项都要是文字；去掉标签后变空的项允许（下面只丢那一项）
            else:
                return "unknown_shape", None
        elif ur is not None:
            return "unknown_shape", None
    return "ok", obj["responses"]


def to_items(responses):
    """解析过的 responses → asked.py 的中性结构（只取答了的；和某个选项一字不差的算点选）。"""
    items = []
    for r in responses:
        if r["status"] != "answered":
            continue
        choices = list(r["choices_offered"] or [])
        vals = r["user_response"] if isinstance(r["user_response"], list) else [r["user_response"]]
        answers = [{"text": v, "picked": v in choices} for v in vals if v]
        if answers:
            items.append({"question": r["question"], "choices": choices or None, "answers": answers})
    return items


def _norm_text(t):
    return t.replace("\r\n", "\n").strip()


def turn_start(text, turn_text=None):
    """当前用户行之后的这一条用户行，像不像「一轮开头」（像，就说明按文字找到的当前行可能是更早一轮的同字行）。规则见模块说明。"""
    if text.startswith(TURN_START_PREFIXES):
        return True
    if _norm_text(text) in HOST_NUDGE_TEXTS:
        return False
    want = _norm_text(turn_text) if isinstance(turn_text, str) else ""
    if want and _norm_text(text).endswith(want):
        return True
    return not text.startswith("[")


def turn_unclear(messages, idx, turn_text=None):
    """messages[idx] 是按文字找到的这一轮用户行；它后面还有一条像「一轮开头」的用户行（turn_start），说明找到的可能是更早一轮的同字行：这一轮里提问卡结果、
    中途的话（清单第 55 条）都不能收。提问卡和中途的话共用这一个判断。只读 messages。"""
    return any(isinstance(m, dict) and m.get("role") == "user" and turn_start(_text(m.get("content")), turn_text) for m in messages[idx + 1:])


def collect(messages, idx, turn_text=None, mid=None):
    """messages[idx] 是这一轮的用户行；turn_text 是交给记忆的这一轮用户那句话（用来认「并进来的当前行」，不给就不认这一条）。
    返回 (items, notes)：items 是这一轮所有提问卡里答了的题、和中途又说的话（按行的先后；提问卡按结果行的先后、再按题目先后），notes 是备注（一处一条）。
    mid：{下标: {"items": […], "notes": […]}}，hermes_midturn 解析好的中途的话，下标是占位行在 messages 里的位置（只有 idx 之后的会收）。认不清是哪一轮时一条都不收，只记一条 midturn_dropped:turn_unclear。
    更早的轮次（idx 之前）一律不读。只读 messages，不改。"""
    calls, used = set(), set()
    items, notes = [], []
    unclear = turn_unclear(messages, idx, turn_text)
    mid_seen = False
    for j in range(idx + 1, len(messages)):
        m = messages[j]
        if not isinstance(m, dict):
            continue
        if mid and j in mid:
            mid_seen = True
            if not unclear:
                items.extend(mid[j]["items"])
                notes.extend(mid[j]["notes"])
            continue
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                if isinstance(tc, dict):
                    for k in ("id", "call_id"):
                        v = norm(tc.get(k))
                        if v is not None:                                  # 没有 id 的永远不进配对集合
                            calls.add(v)
            continue
        if role != "tool":
            continue
        names = {m.get("name"), m.get("tool_name")}
        if not names & ASK_USER_TOOLS:
            continue                                                       # 白名单以外的工具结果：不读、不存
        if unclear:
            notes.append(NOTE_TURN_UNCLEAR)
            continue
        cid = norm(m.get("tool_call_id"))
        named = names - {None}
        if len(named) != 1 or not named <= ASK_USER_TOOLS or cid is None or cid not in calls or cid in used:
            notes.append(NOTE_UNPAIRED)
            continue
        used.add(cid)
        kind, responses = parse(m.get("content"))
        if kind == "error":
            continue
        if kind != "ok":
            notes.append({"bad_json": NOTE_BAD_JSON, "host_rewritten": NOTE_HOST_REWRITTEN}.get(kind, NOTE_UNKNOWN))
            continue
        items.extend(to_items(responses))
    if mid_seen and unclear:
        notes.append(AS.NOTE_MID_UNCLEAR)
    return items, notes


def fold_turn(messages, idx, opening, limit=AS.LONG_LIMIT, turn_text=None, mid=None):
    """这一轮的提问卡回答和中途又说的话 → asked.Folded。实时写入和以后的「补回」都用它：同一段历史、同一轮，得到的行逐字相同。纯函数。
    opening：要接上去的那句（带图的是改写过的那份）；turn_text：交给记忆的原句（认「并进来的当前行」用），不给就用 opening；mid：见 collect（没有中途的话就不给，行为和 0.1.0a8 逐字节相同）。"""
    items, notes = collect(messages, idx, opening if turn_text is None else turn_text, mid)
    return AS.render(items, opening=opening, limit=limit, notes=notes)
