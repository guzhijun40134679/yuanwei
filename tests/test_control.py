"""固定口令 #yuanwei-nomem：只认当前可信用户提交的第一物理行。第二段·交回点甲，先写测试。全部自造文字。"""
import pytest

from yuanwei import control as C


@pytest.mark.parametrize("text", [
    "#yuanwei-nomem",
    "#yuanwei-nomem\n",
    "#yuanwei-nomem   ",
    "#yuanwei-nomem\t",
    "#yuanwei-nomem \t \t",
    "#yuanwei-nomem\r\n查一下上次的端口",
    "#yuanwei-nomem\n查一下上次的端口",
    "#YuanWei-NoMem\n正文",
    "#YUANWEI-NOMEM",
])
def test_first_line_exact_command_is_control(text):
    c = C.parse(text)
    assert c.kind == "nomem"
    assert c.span[0] == 0 and 14 <= c.span[1] <= len(text)
    assert text[c.span[0]:c.span[1]].rstrip(" \t").lower() == "#yuanwei-nomem"


def test_span_excludes_line_terminator_including_crlf():
    assert C.parse("#yuanwei-nomem\r\nabc").span == (0, 14)
    assert C.parse("#yuanwei-nomem\nabc").span == (0, 14)
    assert C.parse("#yuanwei-nomem  \nabc").span == (0, 16)


@pytest.mark.parametrize("text", [
    "请问 #yuanwei-nomem 是什么",                       # 句中
    "你好\n#yuanwei-nomem",                             # 第二行
    "\n#yuanwei-nomem",                                 # 第一物理行是空行
    " #yuanwei-nomem",                                  # 有缩进
    "\t#yuanwei-nomem",
    "> #yuanwei-nomem",                                 # 引用
    "`#yuanwei-nomem`",                                 # 反引号
    "```\n#yuanwei-nomem\n```",                         # 代码块
    "“#yuanwei-nomem”",
    "＃yuanwei-nomem",                                  # 全角 #：不做折叠
    "#ｙuanwei-nomem",                                  # 全角字母
    "#yuanwei－nomem",                                  # 全角连字符
    "yuanwei-nomem",                                    # 没有 #
    "#nomem",
    "/nomem",
    "普通的一句话",
    "",
])
def test_not_control_and_not_reserved(text):
    assert C.parse(text).kind == "none"


@pytest.mark.parametrize("text", [
    "#yuanwei-nomem 请别查",
    "#yuanwei-nomemx",
    "#yuanwei-nomem.",
    "#yuanwei-off",
    "#yuanwei-",
    "#yuanwei-nomem\rfoo",                              # 孤立的 CR 不是换行
    "#yuanwei-nomem\r",                                 # 没有 LF 的行尾 CR
    "#yuanwei-nomem ",                             # 不是空格或 Tab
    "#yuanwei-nomem　\n正文",
    "#Yuanwei-nomem now\n正文",
    "#yuanwei-no mem",
])
def test_reserved_prefix_with_wrong_format_is_malformed(text):
    c = C.parse(text)
    assert c.kind == "malformed"
    assert "#yuanwei-nomem" in c.hint


def test_hint_only_for_malformed():
    assert C.parse("你好").hint is None
    assert C.parse("#yuanwei-nomem").hint is None


def test_non_text_is_error():
    with pytest.raises(ValueError):
        C.parse(None)
    with pytest.raises(ValueError):
        C.parse(b"#yuanwei-nomem")


def test_task_view_is_derived_and_original_unchanged():
    raw = "#yuanwei-nomem\r\n查一下上次的端口"
    c = C.parse(raw)
    assert C.task_view(raw, c) == "查一下上次的端口"
    assert raw == "#yuanwei-nomem\r\n查一下上次的端口"
    assert C.task_view("#yuanwei-nomem", C.parse("#yuanwei-nomem")) == ""
    assert C.task_view("#yuanwei-nomem\n", C.parse("#yuanwei-nomem\n")) == ""
    assert C.task_view("普通", C.parse("普通")) == "普通"
    m = "#yuanwei-nomem 请别查\n正文"
    assert C.task_view(m, C.parse(m)) == m              # 格式不对不当控制行去掉


def test_standard_line_and_generator_round_trip():
    assert C.standard_line() == "#yuanwei-nomem"
    for body in ("", "查一下", "多行\n正文\n#yuanwei-nomem", "#yuanwei-nomem"):
        t = C.with_command(body)
        c = C.parse(t)
        assert c.kind == "nomem"
        assert C.task_view(t, c) == body


def test_ascii_only_case_folding():
    # 只折叠 ASCII 字母；K（开尔文符号）之类不会被 .lower() 变成 ASCII
    assert C.ascii_lower("ABC-xyzK") == "abc-xyzK"
    assert C.parse("#yuanwei-nomem").kind == "nomem"
