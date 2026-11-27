"""Hermes 这一小层：问宿主「这一轮是谁开的」「用户打的是哪几个字」（清单第 56 条第一批，0.1.0a10）。

宿主的事实都是在隔离的真 Hermes v0.23.2 里探出来的（`gateway.session_context._SESSION_TURN_EVENT` 在 on_turn_start 里拿得到，桌面和微信都是）。
这里每一个宿主模块都在函数里才导入（不在模块顶层）；导入失败、没有事件、字段不对、任何异常，一律当「不知道」，返回 None，调用方走今天的老路。
结果只在内存里用：不存文字，只返回三个值之一，或（技能命令）用户打的那几个字，由调用方立刻拿去算指纹。
"""

# 宿主自己的固定开头里不带方括号的（带方括号的一律由 host_own_head 按「方括号开头」处理：[/plan — plan mode]、[/learn] 、[/init] 、[Continuing toward your standing goal…、[IMPORTANT:、
# [Goal set] 、终端的 [/loop wakeup #、[Heartbeat — recurring instruction 这些）。飞书会议邀请（plugins/platforms/feishu/feishu_meeting_invite.py 的 build_meeting_invite_prompt）是机器拼的整句，开头固定；表情回应（飞书、Slack、Photon 适配器拼的 reaction:added:／reaction:removed:）也是。这些话今天在「行找不到」「待办快照」的路上就不记，保持不记
HOST_OWN_HEADS = ("Set up the '", "You have been invited to join a meeting: ", "reaction:added:", "reaction:removed:")


def host_own_head(text):
    """这段文字是不是宿主自己写的开头：去掉前面的空白后以 `[` 开头，或以 HOST_OWN_HEADS 里任何一个开头。
    两个放宽（待办快照行不算机器行、开头行找不到时记开头句，清单第 56 条第 5 节 a、b）都不许放进宿主自己写的开头。
    用户自己用 `[` 开头的话在这两种少见情形里也照 0.1.0a9 不记（宁可不记，不记错人）。"""
    if not isinstance(text, str):
        return True                                                                      # 不是字符串：当成宿主的，不放宽
    t = text.lstrip()
    return t.startswith("[") or t.startswith(HOST_OWN_HEADS)


# 技能命令（agent/skill_commands.py）：只认「单个技能」和「技能包」两种开头；自动加载那种（`[IMPORTANT: The "…" skill is auto-loaded`）不认
SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"                                          # 单个技能那一行的结尾（宿主 _SINGLE_SKILL_MARKER；skills_loaded_by_scaffold 用它认头一行）
SINGLE_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "     # 宿主取字取的是最后一处
BUNDLE_MARKER = " skill bundle,"
BUNDLE_USER_INSTRUCTION = "\nUser instruction: "                                                                       # 宿主取字取的是第一处
BUNDLE_FIRST_SKILL_BLOCK = "\n\n[Loaded as part of the "
BUNDLE_INSTRUCTION_LINE = "\nBundle instruction: "
SKILL_DIR_HEAD = "[Skill directory: "                                                                              # _build_skill_message 在每个技能正文后面写的目录说明（两行）
SKILL_DIR_NOTE_START = "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
HOST_APPENDED_CONTEXT = ("--- Attached Context ---", "--- Context Warnings ---")                                    # 宿主把 @文件 的内容接在用户的字后面


def turn_origin(text):
    """这一轮是谁开的：`"user"`｜`"host"`｜None。
    取不到宿主模块、没有当前事件、`event.text != text`、任何异常 → None；事件带 `inherit_channel_pin` 或 `_heartbeat_session_id`（目标接着干、心跳、/goal resume）→ None；`display_kind_for_event(event)` 不是 None 或 `event.internal` 为真 → "host"；否则 "user"。"""
    try:
        from gateway import session_context as sc
        from gateway.response_filters import display_kind_for_event
        event = sc._SESSION_TURN_EVENT.get()
        if event is None or not isinstance(text, str):
            return None
        event_text = getattr(event, "text", None)
        if not isinstance(event_text, str) or event_text != text:
            return None
        if getattr(event, "inherit_channel_pin", False) or getattr(event, "_heartbeat_session_id", None):
            return None                                                                  # 宿主自己拼的目标接着干、心跳到点、/goal resume（gateway/platforms/event.py）：不是用户开的，也不当「宿主证明」，今天怎么记的照旧
        if display_kind_for_event(event) is not None or getattr(event, "internal", False):
            return "host"
        return "user"
    except Exception:                                                                    # noqa: BLE001  取不到就是不知道，走今天的老路
        return None


def typed_words(text):
    """技能命令整段文字里，用户自己打的那几个字；不是、认不准、取不到都是 None。
    1. 只认单个技能和技能包两种开头；文字里有宿主接的 @文件内容 的分隔标记 → None；
    2. 单个技能还是技能包只看头一行（照宿主 skills_loaded_by_scaffold 的写法，agent/skill_commands.py）：头一行里有「 skill bundle,」→ 技能包；
       否则头一行必须以单个技能的标记（「The full skill content is loaded below.]」）结尾才算单个技能，不然 None；
       头一行是单个技能、全文里别处又有「 skill bundle,」（技能正文里引用的）→ None：宿主取字会按全文走错到技能包那条路；
    3. 调宿主自己的 extract_user_instruction_from_skill_message（函数里才导入）；结果空、等于原文、抛错 → None；
    4. 再核一道：宿主取字用「最后一处标记」（单个技能）或「第一处标记」（技能包），技能正文自己引用了标记时，宿主取到的会是正文的字。
       两条路取到的字里有技能目录说明（技能正文后面一定有；用户真打的字里不会有）→ None；
       技能包：标记要落在头里（第一块技能正文之前），头里有「Bundle instruction:」（来自技能包文件的话）也不认。"""
    try:
        if not isinstance(text, str) or not text.startswith(SKILL_INVOCATION_PREFIX):
            return None
        if any(m in text for m in HOST_APPENDED_CONTEXT):
            return None
        header = text.split("\n", 1)[0]
        bundle = BUNDLE_MARKER in header
        if not bundle and (not header.endswith(SINGLE_SKILL_MARKER) or BUNDLE_MARKER in text):
            return None
        from agent.skill_commands import extract_user_instruction_from_skill_message
        words = extract_user_instruction_from_skill_message(text)
        if not isinstance(words, str) or not words.strip() or words == text:
            return None
        if bundle:
            first_block = text.find(BUNDLE_FIRST_SKILL_BLOCK)
            at = text.find(BUNDLE_USER_INSTRUCTION)
            if at < 0 or (first_block >= 0 and at > first_block):
                return None
            head = text if first_block < 0 else text[:first_block]
            if BUNDLE_INSTRUCTION_LINE in head:
                return None
        else:
            at = text.rfind(SINGLE_SKILL_INSTRUCTION)
            if at < 0:
                return None
            tail = text[at + len(SINGLE_SKILL_INSTRUCTION):]
            if SKILL_DIR_HEAD in tail or SKILL_DIR_NOTE_START in tail:
                return None
        if SKILL_DIR_HEAD in words or SKILL_DIR_NOTE_START in words:
            return None
        return words
    except Exception:                                                                    # noqa: BLE001
        return None
