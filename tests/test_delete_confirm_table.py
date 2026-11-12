"""「确认删除」判表的测试。表结构、和禁读判表的关系现在就测；产品里的判法（yuanwei.window.is_delete_confirm，乙写）等写出来再跑。"""
import pytest

import delete_confirm_table as T
import window_open_table as W

REF_TRAIL = "。．.！!"


def ref_is_delete_confirm(text):
    """表自检用的参考判法（只在 tests/ 里；产品的 is_delete_confirm 由乙写，不能 import 这个）。"""
    return text.strip().rstrip(REF_TRAIL) == "确认删除"


def test_table_structure():
    ids = [r["id"] for r in T.ROWS]
    assert len(ids) == len(set(ids)) and len(T.ROWS) >= 50
    assert sum(1 for r in T.ROWS if r["confirm"]) >= 10 and sum(1 for r in T.ROWS if not r["confirm"]) >= 35
    for r in T.ROWS:
        assert isinstance(r["text"], str) and isinstance(r["confirm"], bool) and len(r["why"]) >= 1, r["id"]


@pytest.mark.parametrize("r", T.ROWS, ids=[r["id"] for r in T.ROWS])
def test_reference_rule_agrees_with_every_row(r):
    assert ref_is_delete_confirm(r["text"]) is r["confirm"], r["id"]


def test_required_negative_kinds_are_present():
    texts = [r["text"] for r in T.ROWS if not r["confirm"]]
    assert any("确认删除吧" == t for t in texts)
    assert any(t.endswith("？") for t in texts)
    assert any(t.startswith("「") for t in texts) and any(t.startswith("```") for t in texts)
    assert any("\n" in t and t.startswith("确认删除\n") for t in texts)
    assert any(t in ("确认", "好", "是") for t in texts)
    assert any("确认开启" in t for t in texts)


def test_no_sentence_of_the_open_table_confirms_a_deletion():
    """禁读（开启）判表里的 98 句，一句也不能当「确认删除」。"""
    for r in W.ROWS:
        assert ref_is_delete_confirm(r["text"]) is False, r["id"]


def test_open_confirm_word_is_not_a_delete_confirm_and_vice_versa():
    from yuanwei.window import is_confirm_line
    assert is_confirm_line("确认开启") and not ref_is_delete_confirm("确认开启")
    assert not is_confirm_line("确认删除") and ref_is_delete_confirm("确认删除")
    assert is_confirm_line("确认开启\n后面还有话") is True          # 「确认开启」的判法不变（只看第一行）
    assert ref_is_delete_confirm("确认删除\n后面还有话") is False    # 「确认删除」要整条消息


@pytest.fixture(scope="module")
def prod():
    mod = pytest.importorskip("yuanwei.window")
    if not hasattr(mod, "is_delete_confirm"):
        pytest.skip("交回点乙才写 window.is_delete_confirm")
    return mod


@pytest.mark.parametrize("r", T.ROWS, ids=[r["id"] for r in T.ROWS])
def test_prod_rule_agrees_with_every_row(prod, r):
    assert prod.is_delete_confirm(r["text"]) is r["confirm"], r["id"]


def test_prod_rule_rejects_every_sentence_of_the_open_table(prod):
    for r in W.ROWS:
        assert prod.is_delete_confirm(r["text"]) is False, r["id"]
