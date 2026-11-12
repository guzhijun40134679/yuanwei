"""关的一侧补漏（禁读按窗口·交回点乙，主会话裁定一第 6 条）：用户最在意的是「我说了别用，它还在用」，宁可多拒、不可漏拒。
甲时 gate.read_ban 对 7 句按意思该关的没认出来；这里把它们认出来，同时钉住「不能因此把正反问、写入要求、别的东西误认成禁读」。全部自造文字。"""
import pytest

from yuanwei import gate as GT


@pytest.mark.parametrize("text", [
    "不要和我记忆里的核对，只看眼前这份",          # N01：否定词后面接「和＋记忆」
    "我不希望你再用我的记忆了",                    # N06
    "不想让你看以前的对话。",                      # N07
    "不能使用记忆，现在先按我说的做。",            # N08
    "先讨论以后能不能开，现在不要查",              # F01：没有宾语的短句
    "这个先不开，回头再说。",                      # F04
    "可以使用记忆，不过先别用。",                  # R05
])
def test_the_seven_gaps_are_now_recognised_as_bans(text):
    assert GT.read_ban(text)


@pytest.mark.parametrize("text", [
    "不愿意让你翻我的聊天记录", "无需参考记忆", "不必使用记忆", "你不应该调用以前的对话", "别和以前的对话核对", "不要跟我之前的聊天记录对照",
    "现在别用", "这次不要查", "先别用了", "暂时不要使用",
])
def test_neighbouring_phrasings_are_bans_too(text):
    assert GT.read_ban(text)


@pytest.mark.parametrize("text", [
    "能不能让你使用记忆？", "可不可以使用记忆", "你想不想用我的记忆", "愿不愿意查我的聊天记录",   # 正反问不是否定（新加的否定词）。「要不要使用记忆」早就被旧的「不要」认成禁读，不在这一单改，宁可多拒
    "这个库别用", "这个文件不要查了", "别看git历史记录", "不要写入记忆，只回答", "不能写进记忆吗",               # 没有记忆宾语、或是写入要求
    "我不记得之前聊的了", "他不想用这个方案", "我想用我的记忆", "现在可以使用我的记忆", "和我记忆里的核对一下",    # 不是禁读
    "下一轮不用了", "不过先看看这个",
])
def test_not_overreaching(text):
    assert not GT.read_ban(text)


def test_the_two_known_over_recognitions_are_kept():
    """R04（过去叙述）、M04（用不用记忆都行）按意思不是禁读，现行规则认成禁读：主会话裁定不收回，宁可多拒。"""
    assert GT.read_ban("刚才说别用记忆，现在改主意了，可以使用记忆。")
    assert GT.read_ban("用不用记忆都行。")


def test_ban_clauses_keep_order_and_agree_with_read_ban():
    got = GT.ban_clauses("别用记忆。还是算了，和我记忆里的核对一下。")
    assert [b for _c, b in got] == [True, False, False]
    assert GT.read_ban("别用记忆。还是算了，和我记忆里的核对一下。") == any(b for _c, b in got)
    assert GT.ban_clauses("“不要用记忆”这句话怎么翻译") and not any(b for _c, b in GT.ban_clauses("“不要用记忆”这句话怎么翻译"))
