"""尺子 2、英文切句（索引版本 3、split-2）、禁读分句的英文句号：2026-11-17 修尺子与英文切句。全部自造数据，不联网。"""
import random

import pytest

from helpers import Scripted, make_mem
from yuanwei import anchors as AN
from yuanwei import gate as GT
from yuanwei import sentence as SN
from yuanwei import split as SP
from yuanwei import util as U
from yuanwei.config import Config
from yuanwei.errors import ConfigError

HAN_ALPHABET = list("今天去了海边捡贝壳晚上吃烤鱼我你他的是不在有这那个人说话好吧") + list("，。！？；\n ")


# ================================================================ 尺子 2
def test_ruler2_weights_and_rounding():
    assert U.est_tokens("你好世界", 2) == 3                                  # 4 × 0.52 = 2.08 → 3（向上取整，和尺子 1 一样）
    assert U.est_tokens("你好世界") == U.est_tokens("你好世界", 1) == 3
    assert U.est_tokens("a" * 100, 2) == 18                                 # 100 × 0.177 = 17.7 → 18
    assert U.est_tokens("a" * 100, 1) == 52                                 # 尺子 1 原样：100 × 0.52
    assert U.est_tokens("", 2) == 0
    # 乙：拉丁字母、数字、英文标点、空白；别的字符（表情、别的文字）照甲算
    assert U.ruler2_weight("abc 123,.") == 9 * 177
    assert U.ruler2_weight("好") == 520 and U.ruler2_weight("あ") == 520 and U.ruler2_weight("，") == 520 and U.ruler2_weight("😀") == 520
    assert U.ruler2_weight("é") == 177 and U.ruler2_weight("Привет") == 6 * 520


def test_ruler1_is_kept_exactly():
    for n in range(0, 300):
        for ch in ("好", "a", "😀"):
            assert U.est_tokens(ch * n, 1) == (52 * n + 99) // 100
    assert U.est_tokens_line("好好", 1) == U.est_tokens("好好\n", 1) == 2
    with pytest.raises(ValueError):
        U.est_tokens("x", 3)


def test_han_character_weight_is_unchanged_between_rulers():
    """汉字、假名、全角符号的单字权重没变：纯这些字符的行，两把尺子只差在行末换行那一个字符上。"""
    for n in range(1, 200):
        s = "汉" * n
        assert U.ruler2_weight(s) == 520 * n                                  # 单字 520‰ 不变
        assert U.est_tokens(s, 2) == U.est_tokens(s, 1) == (52 * n + 99) // 100
    for ch in "あ、，。！？（）":
        assert U.ruler2_weight(ch) == 520


def test_est_lines_is_sum_of_lines_for_both_rulers():
    lines = ["第一行", "second line", "混合 mixed 行"]
    for r in (1, 2):
        assert U.est_lines(lines, r) == sum(U.est_tokens_line(x, r) for x in lines)


def test_config_ruler_version_default_and_validation():
    c = Config()
    assert c.ruler_version == 2 and c.split_version == "split-2" and c.index_version == 3
    with pytest.raises(ConfigError):
        Config(ruler_version=3).validate()
    with pytest.raises(ConfigError):
        Config(split_version="split-9").validate()
    Config(ruler_version=1, split_version="split-1", index_version=1).validate()
    for v in (1, 2, 3):
        Config(index_version=v).validate()
    with pytest.raises(ConfigError):
        Config(index_version=4).validate()


def test_snapshot_omits_versions_only_when_both_are_legacy():
    new = Config()
    assert new.write_snapshot()["split_version"] == "split-2" and new.write_snapshot()["ruler_version"] == 2
    old = Config(split_version="split-1", ruler_version=1)
    assert "split_version" not in old.write_snapshot() and "ruler_version" not in old.write_snapshot()          # 旧配置的快照、指纹和升级前逐字一样
    assert old.config_version() == old.config_version(legacy=True)
    assert new.config_version() != new.config_version(legacy=True)
    assert Config.snapshot_versions(old.write_snapshot()) == ("split-1", 1)
    assert Config.snapshot_versions(new.write_snapshot()) == ("split-2", 2)


# ================================================================ 英文句末：短例表（施工单 2.3 节）
def spans2(text):
    return [text[a:b] for a, b in SP.split_message(text, "split-2")]


def anchors3(text):
    return [t for t, _a, _b in AN.anchor_spans_v3(text)]


CASES = [
    ("Meet at 3 p.m. We can talk later.", ["Meet at 3 p.m.", "We can talk later."]),
    ("I live in the U.S. My office is nearby.", ["I live in the U.S.", "My office is nearby."]),
    ("Dr. Smith arrived.", ["Dr. Smith arrived."]),
    ("J. Smith arrived.", ["J. Smith arrived."]),
    ("version 0.1.0a2 is out", ["version 0.1.0a2 is out"]),
    ("it costs 3.5 dollars", ["it costs 3.5 dollars"]),
    ("1. Install it\n2) Run it", ["1. Install it", "2) Run it"]),
    ('He said "stop." Then left.', ['He said "stop."', "Then left."]),
    ("Wait... What?", ["Wait...", "What?"]),
    ("Wait… What?", ["Wait…", "What?"]),
    ("i went home. then i slept.", ["i went home. then i slept."]),                         # 已知限制：小写起头的下一句不断
    ("- item one. Item two.", ["- item one.", "Item two."]),                               # 项目列表：行首标记不断，行里的英文句子照常断（11-17 改定）
    ("1. Step one. Step two.", ["1. Step one.", "Step two."]),
    ("- Bought milk. Then went home.", ["- Bought milk.", "Then went home."]),
    ("* Done. Next.", ["* Done.", "Next."]),
    ("1. 安装", ["1. 安装"]),
    ("2) Run it", ["2) Run it"]),
    ("10. Install it. Run it.\n2. Next one", ["10. Install it.", "Run it.", "2. Next one"]),
    ("`a. B` and more. Next", ["`a. B` and more.", "Next"]),                              # 行内代码里的点不断
    ("see foo.py and example.com/a.b now", ["see foo.py and example.com/a.b now"]),
    ("好的。Then we go. 明天见", ["好的。", "Then we go.", "明天见"]),
    # 派工后主会话补的一条：英文句末后面不带空白、直接接汉字，也断
    ("Done.好的", ["Done.", "好的"]),
    ("OK!明天见", ["OK!", "明天见"]),
    ("U.S.的政策", ["U.S.的政策"]),
    ("Dr.李", ["Dr.李"]),
    ("foo.py文件", ["foo.py文件"]),
    ("0.1.0a2发布", ["0.1.0a2发布"]),
    ("1.安装", ["1.安装"]),
    ("J.李来了", ["J.李来了"]),
    ("Wait…好的", ["Wait…", "好的"]),
    # Codex 审查 S1：行内代码按等长的反引号串配对
    ("``foo. Bar`` after", ["``foo. Bar`` after"]),
    ("``a ` bar. Cat`` and more. Next", ["``a ` bar. Cat`` and more.", "Next"]),
    ("`a. B` and ``c. D`` then. Next", ["`a. B` and ``c. D`` then.", "Next"]),
    # Codex 审查 S3：中文句界后面紧接英文缩写，缩写保护照样起作用
    ("好的。Dr. Smith arrived.", ["好的。", "Dr. Smith arrived."]),
    ("好的。J. Smith arrived.", ["好的。", "J. Smith arrived."]),
    ("好的。Meet at 3 p.m. We go.", ["好的。", "Meet at 3 p.m.", "We go."]),
]


@pytest.mark.parametrize("text,want", CASES)
def test_short_example_table_split2_and_anchors_v3(text, want):
    assert spans2(text) == want
    assert anchors3(text) == want


def test_fenced_code_block_is_not_cut_at_periods_but_newlines_still_cut():
    text = "before. After\n```\nx = 1. Y. Z\n```\nlast. One"
    got = [t for t, _a, _b in AN.anchor_spans_v3(text)]
    assert "x = 1. Y. Z" in got                                                          # 围栏里的行不在句点处断
    assert got[0] == "before." and got[1] == "After"                                     # 围栏外照常断，换行照旧切
    assert got[-2:] == ["last.", "One"]


def test_old_cuts_are_untouched_by_the_new_rule():
    """旧切法（索引版本 1、2；split-1）整套原样：不认英文句号。"""
    t = "Meet at 3 p.m. We can talk later. Done.好的"
    assert [t[a:b] for a, b in SP.split_message(t)] == [t]
    assert [x["text"] for x in AN.anchors_for("m", 1, t, 2)] == [t]
    assert [x["text"] for x in AN.anchors_for("m", 1, t, 1)] == [t]
    assert [x["text"] for x in AN.anchors_for("m", 1, t, 3)] == ["Meet at 3 p.m.", "We can talk later.", "Done.", "好的"]


def test_old_versions_keep_the_comma_cutback_by_60_codepoints():
    """索引版本 2 的「太长」是 60 个码点；版本 3 按尺子 2 的权重判，英文句子要长得多才从逗号处切。"""
    s = ", ".join(["word%d" % i for i in range(12)])                                       # 约 70 个字符的英文句子
    assert len(s) > 60
    v2 = [x["text"] for x in AN.anchors_for("m", 1, s, 2)]
    v3 = [x["text"] for x in AN.anchors_for("m", 1, s, 3)]
    assert len(v2) > 1 and len(v3) == 1 and v3[0] == s
    han = "，".join(["今天去了海边捡贝壳"] * 8)                                              # 纯中文：两版一样
    assert [x["text"] for x in AN.anchors_for("m", 1, han, 2)] == [x["text"] for x in AN.anchors_for("m", 1, han, 3)]


def test_anchor_index_versions_ids_and_ords():
    a2 = AN.anchors_for("m1", 5, "第一句。第二句。", 2)
    a3 = AN.anchors_for("m1", 5, "第一句。第二句。", 3)
    assert [(x["anchor_id"], x["ord"]) for x in a2] == [(x["anchor_id"], x["ord"]) for x in a3]            # 版本 3 编号同版本 2
    assert all(x["index_version"] == 3 for x in a3) and all(x["index_version"] == 2 for x in a2)
    with pytest.raises(AN.AnchorError):
        AN.anchors_for("m1", 5, "x", 4)


# ================================================================ 性质：范围有序不重叠、拼得回原文、碎片并进前一句、中文不变
def _rand_text(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


MIXED_ALPHABET = list("abcXYZ019 .!?…,\"'()`-*\n。！？；，好的中文") + ["...", "Dr.", "U.S.", "p.m. ", "e.g. ", "```\n"]


def test_properties_on_random_text_mixed():
    rng = random.Random("尺子2-性质")
    for _ in range(600):
        text = "".join(rng.choice(MIXED_ALPHABET) for _ in range(rng.randint(0, 120)))
        for version in ("split-1", "split-2"):
            spans = SP.split_message(text, version)
            assert SP.reconstructs(text, spans)                                       # 有序、不重叠、句子之外全是空白：句子加间隔拼得回原文
            assert all(a < b for a, b in spans)
            for i in range(1, len(spans)):
                assert spans[i - 1][1] <= spans[i][0]
            # 标点碎片并进前一句：除第一句外，没有只由标点符号构成的句子
            for a, b in spans[1:]:
                assert not SP._punct_only(text[a:b])
        for t, a, b in AN.anchor_spans_v3(text):
            assert text[a:b] == t and len(t) >= 2
        # 一条消息的锚点区间有序且不重叠
        sp = AN.anchor_spans_v3(text)
        for i in range(1, len(sp)):
            assert sp[i - 1][2] <= sp[i][1]
        # sentence.pieces 是整段的划分：换行之外的字符每个恰好在一个片段里
        covered = []
        for a, b in SN.pieces(text):
            covered.extend(range(a, b))
        assert covered == [i for i, ch in enumerate(text) if ch != "\n"]


def test_han_text_without_ascii_is_cut_exactly_as_before():
    """由汉字和全角符号构成、又没碰到新断句规则的正文，切法不变（施工单第零节的保证）。"""
    rng = random.Random("尺子2-中文不变")
    for _ in range(600):
        text = _rand_text(rng, HAN_ALPHABET, rng.randint(0, 200))
        assert SP.split_message(text, "split-1") == SP.split_message(text, "split-2")
        assert AN.anchor_spans(text) == AN.anchor_spans_v3(text)


def test_split2_sentences_of_session_use_the_version():
    msgs = [{"msg_id": "m1", "role": "user", "content": "Hello there. How are you?", "msg_time": "2027-01-01", "session_id": "S", "seq": 1}]
    old = SP.sentences_of_session(msgs)
    new = SP.sentences_of_session(msgs, "split-2")
    assert [s["text"] for s in old] == ["Hello there. How are you?"]
    assert [s["text"] for s in new] == ["Hello there.", "How are you?"] and [s["sid"] for s in new] == ["s1", "s2"]
    with pytest.raises(ValueError):
        SP.split_message("x", "split-3")


def test_long_sentence_judged_by_ruler2_weight_not_codepoints():
    eng = "ab " * 50 + "end"                                    # 153 个字符，没有逗号：不会从逗号切，一句
    assert len(SP.split_message(eng, "split-2")) == 1
    boundary = ("a" * 175 + "，" + "b" * 30)                    # 权重：175×177 + 520 + 30×177 = 36,805 > 31,200：太长，从逗号处切成两段
    assert len(SP.split_message(boundary, "split-2")) == 2
    shortish = "a" * 170                                       # 170 × 177 = 30,090 ≤ 31,200：不算长
    assert len(SP.split_message(shortish + "，" + "b", "split-2")) == 1
    han60 = "汉" * 59 + "，" + "字"                              # 纯中文 61 个字，权重 31,720 > 31,200
    assert len(SP.split_message(han60, "split-2")) == 2 and len(SP.split_message(han60, "split-1")) == 2


# ================================================================ 禁读分句里的英文句号（施工单第三节）
def test_ban_english_period_split_examples_from_codex():
    assert GT.read_ban("How does it work. Don't read old chats") is True                    # 施工单例子：句号后面是空白，要切开
    assert GT.read_ban("Don't read old chats") is True                                      # 名词表缺口：old chats 本来也认不出
    assert GT.read_ban("Don't read previous chats") is True
    assert GT.read_ban("How does it work? don't read previous chats") is True               # 现有问号切分不能被改坏
    assert GT.read_ban("怎么用！别用记忆") is True                                           # 无空白、全角标点的现有行为


def test_ban_period_after_abbreviation_also_splits():
    assert GT.read_ban("Why meet at 3 p.m. Don't read old chats.") is True                  # S1：缩写也照断，后面的明确禁令要判出
    assert GT.read_ban("Why meet at 3 p.m. tomorrow") is False                              # 反例：没有独立禁令
    assert GT.read_ban("Why meet at 3 p.m. Don't read old chats") is True


def test_ban_case_variants_and_no_space_after_punctuation():
    for t in ("DON'T READ OLD CHATS", "Do not Read Old Conversations", "never use previous conversations", "DONT read earlier chats.", "Ignore past conversations"):
        assert GT.read_ban(t) is True, t
    assert [c for c, _ in GT.ban_clauses("Done.Don't read old chats")] == ["done.don't read old chats"]      # 点后面是字母：不切（句号后面既不是空白也不是汉字）
    assert GT.read_ban("Done.Don't read old chats") is True                                             # 整句照样被现有的英文规则认出
    assert GT.read_ban("好的.不要参考之前的对话") is True                                      # 句号后面直接是汉字：切开（派工后补的一条）
    assert GT.read_ban("Done.别用记忆") is True
    assert GT.ban_clauses("OK!Don't read old chats")[-1] == ("don't read old chats", True)
    assert [c for c, _ in GT.ban_clauses("Done.好的")] == ["done", "好的"]


def test_ban_decimals_filenames_do_not_split():
    cl = [c for c, _ in GT.ban_clauses("version 0.1.0a2 and foo.py are fine")]
    assert cl == ["version 0.1.0a2 and foo.py are fine"]
    assert [c for c, _ in GT.ban_clauses("it costs 3.5 dollars")] == ["it costs 3.5 dollars"]


def test_ban_quoted_instruction_is_not_a_ban_and_real_ban_is():
    assert GT.read_ban('The sample says "Don\'t read old chats." What does that mean?') is False        # 引号里的禁令样例，不是用户的禁令
    assert GT.read_ban('I wrote "never use previous conversations". Fine. Don\'t read old chats.') is True  # 引号外有真正的禁令
    assert GT.text_permission("Hello. Don't read old chats.")["allowed"] is False


def test_ban_clauses_entry_for_window_control():
    cl = GT.ban_clauses("Hi. First do this. Don't read old chats. Then continue.")
    assert [b for _c, b in cl] == [False, False, True, False]
    assert GT.read_ban("Hi. First do this. Don't read old chats. Then continue.") is True


def test_ban_chinese_and_existing_behaviour_unchanged():
    for t in ("这一轮别用以前的对话", "不要参考我们之前聊的", "怎么用记忆功能？", "why do you ignore memory"):
        assert GT.read_ban(t) == GT.read_ban(t.lower())
    assert GT.read_ban("怎么用记忆功能？") is False
    assert GT.read_ban("这一轮别用以前的对话") is True


# ---------------------------------------------------------------- 禁读：窗口控制和读取入口也要测（read_ban、ban_clauses 之外）
def _window(tmp_path):
    from yuanwei import sqlstore as SQ
    from yuanwei import window as W

    class Clk:
        t = 1_800_000_000

        def __call__(self):
            import time
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.t))
    st = SQ.Store(str(tmp_path / "w.db"))
    return W.WindowService(st, clock=Clk()), W


@pytest.mark.parametrize("text,closes", [
    ("How does it work. Don't read old chats", True),                       # 句号后面是空白：切开，后半句是禁令
    ("Why meet at 3 p.m. Don't read old chats.", True),                     # 缩写句号也切
    ("Done.不要参考之前的对话", True),                                        # 句号后面直接是汉字
    ("Why meet at 3 p.m. tomorrow", False),
    ("The sample reads \"Don't read old chats.\" Is that clear?", False),   # 引号里的禁令样例
    ("version 0.1.0a2 is out, foo.py too", False),
])
def test_window_control_entry_uses_the_new_clause_split(tmp_path, text, closes):
    svc, W = _window(tmp_path)
    n = [0]

    def ref():
        n[0] += 1
        return W.InputRef("hermes", "S1", "t%d" % n[0], "k%d" % n[0], "fp%d" % n[0], "", "ns1")
    svc.bind_explicit("hermes", "S1", "", state="on", source="command", op_id="op-test")
    r = svc.process_input(ref(), text, origin_verified=True)
    assert (svc.view("hermes", "S1").state == "off") is closes, (text, r)


def test_memory_note_entry_refuses_on_english_period_split_and_passes_the_counterexample(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), spawn="off")
    mem.record("h", "S1", "m1", "user", "The beach trip with shells", time="2027-01-01")
    r = mem.note("Why meet at 3 p.m. Don't read old chats.", turn_id="T1", session_id="S9")
    assert r["status"] == "refused"
    r2 = mem.note("beach shells", turn_id="T2", session_id="S9")
    assert r2["status"] != "refused"



# ---------------------------------------------------------------- Codex 审查 S2：围栏记住开启它的字符和长度
def test_fence_is_closed_only_by_a_matching_fence():
    t1 = "~~~text\n```\nHello. World\n~~~\nout. Side"                     # 波浪围栏里夹三个反引号：反引号不关闭它
    got1 = [x for x, _a, _b in AN.anchor_spans_v3(t1)]
    assert "Hello. World" in got1 and got1[-2:] == ["out.", "Side"]
    t2 = "````\n```\nHello. World\n```\n````\nout. Side"                  # 四个反引号围住三个反引号：里面的三个不关闭它
    got2 = [x for x, _a, _b in AN.anchor_spans_v3(t2)]
    assert "Hello. World" in got2 and got2[-2:] == ["out.", "Side"]
    t3 = "```py\nHello. World\n```\nout. Side"                              # 普通围栏照旧
    got3 = [x for x, _a, _b in AN.anchor_spans_v3(t3)]
    assert "Hello. World" in got3 and got3[-2:] == ["out.", "Side"]
    t4 = "```foo. Bar``` x. Y"                                               # 说明文字里有反引号：是行内代码，不是围栏
    assert [x for x, _a, _b in AN.anchor_spans_v3(t4)] == ["```foo. Bar``` x. Y"]


# ---------------------------------------------------------------- Codex 审查 M1：成对的弯单引号里的禁读样例不触发
CURLY = "The sample says \u2018Do not read old chats.\u2019 What does that mean?"


def test_curly_single_quoted_ban_sample_does_not_trigger_read_ban_or_clauses():
    assert GT.read_ban(CURLY) is False
    assert not any(b for _c, b in GT.ban_clauses(CURLY))
    assert GT.read_ban("It says \u2018hi\u2019. Don\u2019t read old chats.") is True              # 引号外真正的禁令（弯撇号的 Don\u2019t 照样认得）
    assert any(b for _c, b in GT.ban_clauses("It says \u2018hi\u2019. Don\u2019t read old chats."))
    assert GT.read_ban("\u2018Don\u2019t read old chats\u2019 is a sample. Fine.") is False     # 引号里的 Don\u2019t 不把引号配断
    assert GT.read_ban("Don\u2019t read old chats") is True


def test_curly_single_quoted_ban_sample_at_the_window_entry(tmp_path):
    svc, W = _window(tmp_path)
    n = [0]

    def ref():
        n[0] += 1
        return W.InputRef("hermes", "S1", "t%d" % n[0], "k%d" % n[0], "fp%d" % n[0], "", "ns1")
    svc.bind_explicit("hermes", "S1", "", state="on", source="command", op_id="op-test")
    svc.process_input(ref(), CURLY, origin_verified=True)
    assert svc.view("hermes", "S1").state == "on"                                  # 样例不关窗口
    svc.process_input(ref(), "It says \u2018hi\u2019. Don\u2019t read old chats.", origin_verified=True)
    assert svc.view("hermes", "S1").state == "off"                                 # 引号外真正的禁令关窗口
