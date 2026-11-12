"""免查门（只管寒暄、应答、宿主命令）和文字识别禁读（读取权限的一部分，始终执行）。纯函数测试，不碰库。
所有例子都是自己造的；PR-05A 金标里的 8 句拒用只放在最后的回归测试里，它们不能当「识别准了」的证明。"""
import pytest

from yuanwei import gate as GT


def d(text, **kw):
    return GT.free_check(text, **kw)["decision"]


def banned(text):
    return not GT.text_permission(text)["allowed"]


# ------------------------------------------------------------------ 免查门
@pytest.mark.parametrize("text", ["你好", "您好", "早上好", "晚安", "在吗", "嗨", "谢谢", "多谢", "感谢", "辛苦了", "哈哈", "呵呵", "嘻嘻", "hi", "Hello", "HEY", "yo", "sup",
                                   "good morning", "Good Night", "thanks", "thank you", "thx", "ty", "lol", "haha", "hehe", "好的", "好", "嗯", "嗯嗯", "行", "可以", "收到", "ok", "okay"])
def test_closed_set_sentences_are_skipped(text):
    assert d(text) == "skip_greeting"


@pytest.mark.parametrize("text", ["OK!", "谢谢！！", "  好的。 ", "ＯＫ！", "Thanks...", "你好～", "嗯…", "hi."])
def test_trailing_tone_punctuation_only_is_stripped(text):
    assert d(text) == "skip_greeting"


@pytest.mark.parametrize("text", ["可以？", "ok()", "`ok`", "ok?", "好的，", "好的,按上次的来", "你好，上次的端口是多少？", "可以，按上次要求改", "谢谢，帮我查一下上次的配置", "(ok)", "嗯？", "hi there",
                                   "早上好啊", "哈哈哈", "ok ok"])
def test_anything_not_exactly_in_the_closed_set_is_checked(text):
    assert d(text) == "check"


def test_pairs_greeting_versus_request():
    assert d("好的") == "skip_greeting" and d("确认一下上次的安排") == "check"
    assert d("翻译本句：今天天气不错") == "check" and d("翻译上次那段") == "check"        # 翻译、改写不整类免


def test_host_command_must_be_marked_explicitly():
    assert d("/clear", host_command=True) == "skip_command"
    assert d("/clear") == "check"                                  # 斜杠开头但没标明：照常查
    assert d("/review 看看上次的决定") == "check"
    assert d("/clear", host_command=False) == "check"


def test_free_check_does_not_decide_read_bans():
    """免查门不管禁读：禁读的句子在这里只是「查」（权限在更前面的一道检查里拦）。"""
    assert d("别用我的记忆") == "check" and d("不用解释，别用我的记忆。") == "check"
    assert GT.free_check("好的")["rule"] == "closed_set" and GT.free_check("先查一下")["rule"] is None


def test_non_string_input_is_an_error_not_a_skip():
    for bad in (None, "   "):
        with pytest.raises(ValueError):
            GT.free_check(bad)
        with pytest.raises(ValueError):
            GT.text_permission(bad)


# ------------------------------------------------------------------ 文字识别禁读：要认的（自造）
@pytest.mark.parametrize("text", [
    "这轮不要用记忆功能", "这一轮别用以前的对话", "不要参考我们之前聊的", "别管之前说的", "不带上下文记录回答", "忽略聊天记录，重新来", "禁止调用历史对话", "请勿读取聊天记录",
    "这件事先不用查记忆了", "别翻我的聊天历史", "不用解释，别用我的记忆。", "先把这个改了，不要看聊天记录。", "别用我的记忆，查一下上次的决定", "别提以前的对话",
    "please don't consult my chat history", "do not refer to previous conversations", "ignore all prior conversations", "disregard past conversations and start over",
    "answer without my memory", "don't bring up our past chats", "Never use my memories for this task", "ok, no memory please", "ignore memory", "don't use memory",
    "reply without any context from before"])
def test_read_bans_are_recognised_by_meaning_not_by_single_words(text):
    assert banned(text)


# ------------------------------------------------------------------ 要放过的（自造）
@pytest.mark.parametrize("text", [
    "别修改历史记录，查一下上次的决定。", "我之前说过别用这个方案", "no, use the memory I have", "别删除我的记忆", "不要写入记忆，只回答", "please don't edit my chat history", "不要把这个写入记忆",
    "别用我之前说的方案", "git 历史记录里查一下这次改动", "别看git历史记录", "rewrite the git history", "don't use memory-mapped files", "there is no memory leak in this code", "I have no memory of that meeting",
    "ignore the memory leak warning", "forget it", "fresh start please", "this is a fresh install", "ignore this warning", "the browser history is long", "别用历史版本的接口",
    "记忆化搜索别用递归", "我不记得之前聊的了", "别忘了之前聊的", "don't forget my history", "Do not check my history please",
    "「别用我的记忆」是什么意思", "“不要看聊天记录”这句话怎么翻译", "为什么你不用我的记忆", "怎么不用我的记忆", "how do I stop using memory", "查一下上次的端口"])
def test_other_phrasings_are_not_bans(text):
    assert not banned(text)


def test_feature_words_do_not_turn_a_ban_into_discussion():
    """撤掉「带功能／开关／设置就当讨论」：这句是在下指令，不是在问功能。"""
    assert banned("这轮不要用记忆功能") and banned("this turn, don't use the memory feature") and banned("请勿使用记忆设置里的历史对话")


def test_ban_in_a_later_clause_is_not_hidden_by_an_earlier_negation():
    assert banned("不用解释，别用我的记忆。") and banned("别修改历史记录，别用我的记忆。")
    assert not banned("别修改历史记录，查一下上次的决定。")


def test_persistent_wording_is_flagged_but_does_not_change_the_rule():
    assert GT.text_permission("从现在起别用我的记忆") == {"allowed": False, "rule": "clause_ban", "persistent": True}
    assert GT.text_permission("以后都不要参考之前的对话") ["persistent"] is True
    assert GT.text_permission("from now on don't use my memory")["persistent"] is True
    assert GT.text_permission("这轮别用记忆")["persistent"] is False
    assert GT.text_permission("以后记得叫我老师")["allowed"] is True


def test_allowed_result_shape():
    assert GT.text_permission("查一下上次的决定") == {"allowed": True, "rule": None, "persistent": False}


# ------------------------------------------------------------------ 回归（不是证明）：PR-05A 金标里的 8 句拒用。以前认出 4 句，补充二改规则以后应当都认出；
# 这 8 句我已经见过，规则也是看过它们之后改的，所以只能当回归，不能说明规则识别准了。新的独立句子由主会话另量。
@pytest.mark.parametrize("text", ["这次不要用我的记忆", "别参考之前的对话，重新回答", "忽略我的历史记录", "不用查我的记忆，直接说", "don't use my memory for this", "ignore our previous conversations",
                                   "answer without any context from before", "no memory please, fresh answer"])
def test_regression_pr05a_refusals(text):
    assert banned(text)


# ------------------------------------------------------------------ 补充三：写入要求不是读取禁令（按分句、按指令判；不用「句里有记／存／写就整句允许读」的反向规则）
@pytest.mark.parametrize("text,ban", [
    ("这条不用记进记忆，查一下上次的决定。", False),                     # 主会话的三条开发例子（表里写定的）
    ("这条别存，也别用以前的聊天记录。", True),
    ("别把这次写进记忆；不过以前的记录可以查。", False),
    ("这条不用记进记忆里哈，临时问下 docker 怎么看容器日志", False),    # n36 的形状（自造等价句，不含原句里别的东西）
    ("不用记到记忆里", False), ("不要保存到记忆", False), ("这条别存", False), ("don't save this to memory", False), ("不用存进记忆里，帮我查个东西", False),
    ("不用记忆", True), ("不用记忆里的东西", True), ("不用查我的记忆，直接说", True), ("忽略聊天记录，重新来", True),
    ("别存这条，也别用记忆回答", True),                                  # 同一分句里写入要求和禁读并存：禁读照拦
    ("不用记进记忆也不用查记忆", True),                                  # 两个「不用」在同一分句：第二个是真禁读，第一个的写入动作不能盖住它
    ("存一下这条记忆，别用以前的对话", True),                            # 句里有「存」，但另有真正的禁读：不能整句放行
])
def test_write_requests_are_not_read_bans_but_a_real_ban_in_the_same_clause_still_is(text, ban):
    assert banned(text) is ban


def test_write_request_wording_does_not_change_what_gets_saved():
    """只修读取判断：文字里的写入要求目前不改变保存行为（没有按消息禁写）；这里只验权限检查不把它当禁读。"""
    assert GT.text_permission("这条不用记进记忆，查一下上次的决定。") == {"allowed": True, "rule": None, "persistent": False}
