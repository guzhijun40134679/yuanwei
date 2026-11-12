"""判表的测试（禁读按窗口）。

主会话裁定二（11-10）以后，开不再靠规则认用户的话，判表的用途是：
* 关侧：每一句上现行 gate.read_ban 的实测钉住；缺口集合（漏认、多认）钉住。乙已按裁定一第 6 条把 7 句漏认补上，多认的 2 句（R04、M04）不收回（宁可多拒）。
* 开侧：关着的窗口里，这些句子一句也打不开——不管有没有待确认（tests/test_window.py 里的总测试用这张表的句子）。
* 结构：编号不重、类别齐、设计第 5 节的五句原句还在。
全部是自造文字。
"""
import pytest

from yuanwei import gate
import window_open_table as T

REQUIRED_CATS = ("positive", "negation", "quote", "third_party", "hypothetical", "future", "howto", "code_attachment", "retract", "mixed", "outside_rules")
DESIGN_SENTENCES = {
    "P01": "和我记忆里的核对一下",
    "N01": "不要和我记忆里的核对，只看眼前这份",
    "Q01": "不要把「可以使用记忆」当成我同意打开",
    "O01": "他建议我让你查以前的记录，我还没决定",
    "F01": "先讨论以后能不能开，现在不要查",
}


def test_table_is_not_tiny():
    assert len(T.ROWS) >= 90


def test_ids_unique_and_texts_unique():
    ids = [r["id"] for r in T.ROWS]
    texts = [r["text"] for r in T.ROWS]
    assert len(set(ids)) == len(ids)
    assert len(set(texts)) == len(texts)


def test_every_row_has_text_reason_and_valid_fields():
    for r in T.ROWS:
        assert isinstance(r["text"], str) and r["text"].strip(), r["id"]
        assert isinstance(r["reason"], str) and len(r["reason"]) >= 4, r["id"]
        assert r["verdict"] in T.VERDICTS, r["id"]
        assert r["cat"] in T.CATEGORIES, r["id"]
        assert r["source"] in ("design", "own"), r["id"]
        assert isinstance(r["ban_want"], bool), r["id"]


def test_required_categories_are_all_covered_with_at_least_four_rows():
    by = {}
    for r in T.ROWS:
        by[r["cat"]] = by.get(r["cat"], 0) + 1
    for c in REQUIRED_CATS:
        assert by.get(c, 0) >= 4, (c, by.get(c, 0))


def test_design_section5_sentences_are_in_the_table_verbatim():
    by_id = {r["id"]: r for r in T.ROWS}
    for rid, text in DESIGN_SENTENCES.items():
        assert by_id[rid]["text"] == text, rid
        assert by_id[rid]["source"] == "design", rid
    assert sorted(r["id"] for r in T.ROWS if r["source"] == "design") == sorted(DESIGN_SENTENCES)


def test_no_table_sentence_is_a_confirm_line():
    """表里没有哪一句本身就是「确认开启」：否则「一句也打不开」的总测试就不成立了。"""
    from yuanwei import window as W
    assert [r["id"] for r in T.ROWS if W.is_confirm_line(r["text"])] == []


# ---------------------------------------------------------------- 关的一侧：现行 gate.read_ban 的实测钉住
@pytest.mark.parametrize("row", T.ROWS, ids=[r["id"] for r in T.ROWS])
def test_ban_now_is_pinned_to_current_gate_read_ban(row):
    assert gate.read_ban(row["text"]) == (row["id"] in T.BAN_NOW_TRUE)


def test_close_side_gaps_are_closed_and_the_two_extras_are_kept():
    """甲时 7 处漏认（N01 N06 N07 N08 F01 F04 R05）：现在全部认出来（主会话裁定一第 6 条）；多认的 2 处（M04 R04）不收回，宁可多拒。"""
    missed = sorted(r["id"] for r in T.ROWS if r["ban_want"] and r["id"] not in T.BAN_NOW_TRUE)
    extra = sorted(r["id"] for r in T.ROWS if not r["ban_want"] and r["id"] in T.BAN_NOW_TRUE)
    assert missed == []
    assert extra == ["M04", "R04"]
    for rid in ("N01", "N06", "N07", "N08", "F01", "F04", "R05"):
        assert rid in T.BAN_NOW_TRUE


def test_confirm_tables_are_well_formed():
    assert len(T.CONFIRM_OK) >= 8 and len(T.NOT_CONFIRM) >= 20
    assert not set(T.CONFIRM_OK) & set(T.NOT_CONFIRM)
