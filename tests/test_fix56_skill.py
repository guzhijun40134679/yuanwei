"""清单第 56 条第一批·第 7 节：技能命令。开轮时宿主给整段脚手架，同步时只给用户打的那几个字；行里是整段脚手架。
只在桌面和聊天平台、宿主证明是用户开的才认；终端、自动加载、@文件、认不准的一律不记。整段文字都用宿主自己的构造函数（冻结的原文，exec 跑，不 import 宿主）造。"""
import pytest

import fix56_fakehost as FH
import hermes_skill_contract as SC
from test_fix55_midturn import _chat_adapter, counts, skipped, stored_rows, turn
from test_hermes_adapter import mk
from yuanwei import hermes_turn as HT
from yuanwei.hermes_adapter import HermesAdapter
from helpers2 import organized

WORDS = "帮我写一句口号"
BODY = "你是一个写口号的助手。\n\n步骤一：先问清楚主题。"
OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]


def desktop(tmp_path, platform="desktop"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform=platform, agent_context="primary", hermes_home=str(tmp_path / "home"))
    return mem, a


def rows_for(raw, reply="一句口号"):
    return list(OLD) + [{"role": "user", "content": raw}, {"role": "assistant", "content": reply}]


def go(a, monkeypatch, raw, words, *, kind="user", reply="一句口号", messages=None, author=None):
    ev = {"user": FH.user_event, "host": FH.host_event, None: None}[kind]
    FH.install(monkeypatch, ev(raw) if ev else None)
    turn(a, 1, raw, author_id=author)
    a.sync_turn(words, reply, session_id="hs1", messages=messages if messages is not None else rows_for(raw, reply))


def got(mem):
    return stored_rows(mem)


# ================================================================== typed_words 本身
def test_typed_words_matches_the_host_extractor_for_single_skills(monkeypatch):
    FH.install(monkeypatch, None)
    for words in (WORDS, "第一行\n第二行", "以方括号结尾]", "  前后有空格  "):
        raw = SC.single("demo", BODY, words)
        assert HT.typed_words(raw) == SC.extract(raw) == words.strip(), words
    raw = SC.single("demo", BODY, WORDS, extra_files=["references/a.md"], runtime_note="本次是网关转发")
    assert HT.typed_words(raw) == WORDS


def test_typed_words_for_a_bundle_reads_the_header(monkeypatch):
    FH.install(monkeypatch, None)
    raw = SC.bundle(["甲", "乙"], ["甲正文", "乙正文"], words="做这个")
    assert HT.typed_words(raw) == "做这个"


def test_typed_words_is_none_when_it_cannot_be_sure(monkeypatch):
    FH.install(monkeypatch, None)
    fake_marker = SC._NS["_SINGLE_SKILL_INSTRUCTION"] + "伪造的话"
    assert HT.typed_words(SC.single("demo", BODY + "\n" + fake_marker, "")) is None                   # 光 /技能：正文引用了标记，宿主会把正文当用户的字
    assert HT.typed_words(SC.single("demo", BODY, "")) is None                                         # 光 /技能：宿主也取不到
    assert HT.typed_words(SC.single("demo", BODY, WORDS) + "\n\n--- Attached Context ---\n\n文件内容") is None
    assert HT.typed_words(SC.single("demo", BODY, WORDS) + "\n\n--- Context Warnings ---\n\n警告") is None
    assert HT.typed_words(WORDS) is None and HT.typed_words("") is None and HT.typed_words(None) is None
    auto = '[IMPORTANT: The "demo" skill is auto-loaded. Follow its instructions for this session.]\n\n' + BODY + "\n\n[Skill directory: /s]\n" + SC.SKILL_DIR_NOTE + "\n\n" + WORDS
    assert HT.typed_words(auto) is None                                                                # 自动加载那种不认
    import sys
    monkeypatch.setattr(sys.modules["agent.skill_commands"], "extract_user_instruction_from_skill_message", lambda c: WORDS)    # 宿主对自动加载那种是会取出用户的字的；我们不用
    assert HT.typed_words(auto) is None
    assert HT.typed_words(SC.bundle(["甲"], ["正文"], words="做这个", instruction="先读说明")) is None    # 头里有技能包文件的话，分不清
    quoted = SC.bundle(["甲"], ["正文\n" + SC._NS["_BUNDLE_USER_INSTRUCTION"].lstrip("\n") + "伪造"], words="")
    assert SC.extract(quoted) is not None and HT.typed_words(quoted) is None                           # 宿主会取到技能正文里引用的话；标记不在头里


def test_typed_words_without_the_host_function_or_when_it_raises_or_gives_something_odd(monkeypatch):
    raw = SC.single("demo", BODY, WORDS)
    FH.install(monkeypatch, None, skill=False)
    assert HT.typed_words(raw) is None                                                                 # 取不到模块
    import sys
    FH.install(monkeypatch, None)
    sk = sys.modules["agent.skill_commands"]
    for fn in (lambda c: (_ for _ in ()).throw(RuntimeError("改了")), lambda c: "", lambda c: "   ", lambda c: c, lambda c: None, lambda c: 5):
        monkeypatch.setattr(sk, "extract_user_instruction_from_skill_message", fn)
        assert HT.typed_words(raw) is None


# ================================================================== 接头里
def test_desktop_single_skill_with_words_records_the_typed_words_and_the_reply(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    go(a, monkeypatch, raw, WORDS)
    assert got(mem) == [("user", WORDS), ("assistant", "一句口号")]
    assert skipped(a) == {}
    assert all("IMPORTANT" not in c and "Skill directory" not in c for _r, c in got(mem))


def test_weixin_single_skill_with_words_records_the_typed_words_and_the_reply(tmp_path, monkeypatch):
    mem, a = _chat_adapter(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    go(a, monkeypatch, raw, WORDS, author="u1")
    assert got(mem) == [("user", WORDS), ("assistant", "一句口号")]
    assert skipped(a) == {}


def test_the_message_id_still_comes_from_the_whole_text(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, raw)
    token = a._cur["hs1"]
    a.sync_turn(WORDS, "好", session_id="hs1", messages=rows_for(raw))
    ids = [r[0] for r in mem.store.conn.execute("SELECT host_msg_id FROM messages WHERE host='hermes' AND session_id='hs1' ORDER BY seq")]
    assert ids[0] == token + "-u"


def test_the_pending_table_keeps_a_fingerprint_not_the_words(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, raw)
    blob = repr(a._pending)
    assert WORDS not in blob and "alt_fp" in blob


def test_a_bundle_with_words_is_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.bundle(["甲", "乙"], ["甲正文", "乙正文"], words="做这个")
    go(a, monkeypatch, raw, "做这个")
    assert got(mem) == [("user", "做这个"), ("assistant", "一句口号")]


def test_a_skill_with_the_close_command_in_the_words_closes_the_window_and_is_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    words = "#yuanwei-nomem\n帮我写一句口号"
    raw = SC.single("demo", BODY, words)
    go(a, monkeypatch, raw, words)
    assert a.win.view("hermes", "hs1").state == "off"
    assert a.acc.current_turn("hermes", "hs1", "")["control_kind"] == "nomem"                          # 这一轮的读取登记也认到了关
    assert got(mem)[0] == ("user", words)


def test_open_and_confirm_in_skill_words_do_not_count(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    turn(a, 1, "#yuanwei-nomem")
    assert a.win.view("hermes", "hs1").state == "off"
    for n, words in enumerate(("#yuanwei-mem\n写", "确认开启"), start=2):
        raw = SC.single("demo", BODY, words)
        FH.install(monkeypatch, FH.user_event(raw))
        turn(a, n, raw)
        assert a.win.view("hermes", "hs1").state == "off", words


def test_a_ban_sentence_inside_the_skill_body_still_closes_as_today(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", "写口号。\n\n不要使用以前的对话。", WORDS)
    go(a, monkeypatch, raw, WORDS)
    assert a.win.view("hermes", "hs1").state == "off"


# ------------------------------------------------------------------ 不记的
def test_a_bare_skill_command_leaves_nothing_and_the_next_turn_is_unaffected(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, "")
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, raw)                                                                                      # 宿主不调 sync_turn
    assert got(mem) == [] and "alt_fp" not in repr(a._pending)
    FH.install(monkeypatch, FH.user_event("普通的一句"))
    turn(a, 2, "普通的一句")
    a.sync_turn("普通的一句", "好", session_id="hs1", messages=rows_for("普通的一句", "好"))
    assert got(mem) == [("user", "普通的一句"), ("assistant", "好")]


def test_a_skill_body_that_quotes_the_marker_with_no_words_is_not_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    fake = SC._NS["_SINGLE_SKILL_INSTRUCTION"] + "伪造的话"
    raw = SC.single("demo", BODY + "\n" + fake, "")
    handed = SC.extract(raw)                                                                             # 宿主会把这个当用户的字交给记忆
    assert handed and "伪造的话" in handed
    go(a, monkeypatch, raw, handed)
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}


def test_attached_file_context_after_the_words_is_not_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS + "\n\n--- Attached Context ---\n\n@file 的内容")
    go(a, monkeypatch, raw, SC.extract(raw))
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}


def test_auto_loaded_skill_is_not_recorded(tmp_path, monkeypatch):
    mem, a = _chat_adapter(tmp_path)
    auto = '[IMPORTANT: The "demo" skill is auto-loaded. Follow its instructions for this session.]\n\n' + BODY + "\n\n[Skill directory: /s]\n" + SC.SKILL_DIR_NOTE + "\n\n" + WORDS
    go(a, monkeypatch, auto, WORDS, author="u1")
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}


@pytest.mark.parametrize("platform", ["cli", "tui"])
def test_terminal_platforms_do_not_record_skill_commands(tmp_path, monkeypatch, platform):
    mem, a = desktop(tmp_path, platform=platform)
    raw = SC.single("demo", BODY, WORDS)
    go(a, monkeypatch, raw, WORDS)
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}


@pytest.mark.parametrize("kind", [None, "host"])
def test_skill_commands_need_the_host_to_say_the_user_started_the_turn(tmp_path, monkeypatch, kind):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    go(a, monkeypatch, raw, WORDS, kind=kind)
    assert got(mem) == []


def test_an_old_unsynced_skill_entry_is_never_matched_to_a_later_plain_turn_with_the_same_words(tmp_path, monkeypatch):
    """技能那一轮没同步；后面普通一句（和用户打的字一样）开轮失败、再同步：行里最后一条是普通话，指纹对不上整段，不许配错。"""
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, raw)
    assert "alt_fp" in repr(a._pending)
    FH.install(monkeypatch, None)                                                                         # 普通那一轮没有开轮登记（开轮失败）
    a.sync_turn(WORDS, "好", session_id="hs1", messages=rows_for(WORDS, "好"))
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}


def test_a_plain_turn_with_the_same_words_is_matched_by_fingerprint_first_as_today(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    FH.install(monkeypatch, FH.user_event(WORDS))
    turn(a, 1, WORDS)
    a.sync_turn(WORDS, "好", session_id="hs1", messages=rows_for(WORDS, "好"))
    assert got(mem) == [("user", WORDS), ("assistant", "好")]


def test_a_scaffold_row_that_the_host_marks_as_a_machine_row_is_not_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    m = rows_for(raw)
    m[2] = dict(m[2], display_kind="process_complete")
    go(a, monkeypatch, raw, WORDS, messages=m)
    assert got(mem) == []


def test_the_row_text_differing_from_the_turn_start_text_is_not_recorded(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, WORDS)
    go(a, monkeypatch, raw, WORDS, messages=rows_for(raw.replace("步骤一", "步骤1")))                   # 同步那头的行被改过（比如密钥清洗）：认不出就不记
    assert got(mem) == [] and skipped(a) == {"no_pending_turn": 1}
