"""密钥规则表的测试（删除与密钥过滤·交回点甲）。

分两部分：
* 表本身的自检（现在就跑）：结构、正例是不是真的在规则覆盖范围里、反例是不是真的没被命中、重叠合并、幂等、GitHub 校验和故意算错、源码里没有整串的假密钥。用 tests/secret_reference.py 那个最小参考模型。
* 对着产品实现的金标测试（要等乙写出 yuanwei.redact；写出来之前整段跳过）：每条正例、每条反例、幂等、确定性。
"""
import collections
import os
import re

import pytest

import secret_reference as REF
import secret_rules_table as T


def _all_positive_rows():
    rows = [(p["id"], p) for r in T.RULES for p in r["positives"]]
    rows += [(p["id"], p) for p in T.OVERLAP]
    return rows


def _all_negative_rows():
    rows = [(n["id"], n) for r in T.RULES for n in r["negatives"]]
    rows += [(n["id"], n) for n in T.GLOBAL_NEGATIVES]
    return rows


POS = _all_positive_rows()
NEG = _all_negative_rows()


# ---------------------------------------------------------------- 一、表本身的结构
def test_rule_ids_unique_and_fields_present():
    ids = [r["id"] for r in T.RULES]
    assert len(ids) == len(set(ids))
    for r in T.RULES:
        for k in ("id", "kind", "priority", "res", "boundary", "replace", "reason", "positives", "negatives"):
            assert k in r, (r.get("id"), k)
        assert isinstance(r["priority"], int) and r["res"] and all(isinstance(x, str) for x in r["res"]), r["id"]
        assert len(r["reason"]) >= 6 and len(r["boundary"]) >= 10 and len(r["replace"]) >= 3, r["id"]
        assert len(r["positives"]) >= T.MIN_POSITIVES_PER_RULE and len(r["negatives"]) >= T.MIN_NEGATIVES_PER_RULE, r["id"]
        for rx in r["res"]:
            re.compile(rx)


def test_row_ids_unique_across_the_whole_table():
    ids = [i for i, _ in POS] + [i for i, _ in NEG] + [p["id"] for p in T.ACCEPTED_OVER_MASK] + [n["id"] for n in T.KNOWN_MISSES]
    assert len(ids) == len(set(ids)), [i for i, c in collections.Counter(ids).items() if c > 1]


def test_table_is_not_tiny():
    assert len(POS) >= 90 and len(NEG) >= 60 and len(T.RULES) >= 20


def test_required_kinds_are_all_covered():
    kinds = {r["kind"] for r in T.RULES}
    for k in T.REQUIRED_KINDS:
        assert k in kinds, k


def test_github_has_all_six_prefixes_in_positives():
    texts = [p["text"] for r in T.RULES if r["id"] == "T07" for p in r["positives"]]
    for pre in T.GITHUB_PREFIXES:
        assert any(pre in t for t in texts), pre


def test_every_positive_template_slots_match_its_slots():
    for pid, p in POS:
        n = len(re.findall(r"<<\d+>>", p["template"]))
        assert n == len(p["slots"]) == p["count"], pid
        for v, k in p["slots"]:
            assert isinstance(v, str) and v and k in {r["kind"] for r in T.RULES}, (pid, k)
            assert v in p["text"], pid
        assert "<<" not in p["text"] and "<<" not in p["expected"], pid
        assert T.PLACEHOLDER % p["slots"][0][1] in p["expected"], pid


def test_expected_keeps_only_the_type_no_fragment_of_the_secret():
    """占位符只留类型：expected 里不能出现任何一段被换掉的值（前几位、后四位都不留）。"""
    for pid, p in POS:
        for v, _k in p["slots"]:
            if len(v) < 8:
                continue
            assert v[:6] not in p["expected"] and v[-4:] not in p["expected"].replace(T.PLACEHOLDER % _k, ""), pid


def test_positives_do_not_use_published_documentation_example_keys():
    for pid, p in POS:
        for ex in T.DOC_EXAMPLE_KEYS:
            assert ex not in p["text"], pid


def test_github_positives_have_a_deliberately_wrong_checksum_but_are_still_in_rule_coverage():
    rx = re.compile(T.RULES[[r["id"] for r in T.RULES].index("T07")]["res"][0])
    n = 0
    for r in T.RULES:
        if r["id"] != "T07":
            continue
        for p in r["positives"]:
            v = p["slots"][0][0]
            if v.startswith("github_pat_"):
                continue
            assert not T.gh_checksum_ok(v), p["id"]                    # 校验和是错的：不是任何真值
            assert rx.search(p["text"]) and rx.search(p["text"]).group("s") == v, p["id"]    # 但规则照样覆盖它（没有造到范围外面）
            n += 1
    assert n == 5
    good = T.GH("ghp_", "x")
    assert not T.gh_checksum_ok(good)
    import zlib
    body = good[4:-6]
    real_shape = "ghp_" + body + T._b62(zlib.crc32(body.encode())).rjust(6, "0")
    assert T.gh_checksum_ok(real_shape)                                # 校验函数本身是对的（对得上才说明上一条的「错」有意义）


def test_table_source_has_no_whole_fake_secret_literal():
    """源码里不能有一整串像密钥的字面量（现算的才行）：连续 24 位以上、同时有大写、小写、数字的字母数字串（含 _ - + / =）一个都不许有。"""
    p = os.path.join(os.path.dirname(__file__), "secret_rules_table.py")
    src = open(p, encoding="utf-8").read()
    bad = [m.group(0)[:12] for m in re.finditer(r"[A-Za-z0-9_+/=-]{24,}", src)
           if re.search(r"[A-Z]", m.group(0)) and re.search(r"[a-z]", m.group(0)) and re.search(r"[0-9]", m.group(0))]
    assert bad == [], bad


def test_pem_markers_are_built_not_written_whole():
    p = os.path.join(os.path.dirname(__file__), "secret_rules_table.py")
    src = open(p, encoding="utf-8").read()
    assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", src.replace('"-----BEGIN " +', ""))


# ---------------------------------------------------------------- 二、表自洽：参考模型把整张表跑一遍
@pytest.mark.parametrize("pid,row", POS, ids=[i for i, _ in POS])
def test_reference_model_positive_rows(pid, row):
    out, n, kinds = REF.redact(row["text"])
    assert out == row["expected"], (pid, out)
    assert n == row["count"], (pid, n, kinds)


@pytest.mark.parametrize("nid,row", NEG, ids=[i for i, _ in NEG])
def test_reference_model_negative_rows_are_untouched(nid, row):
    out, n, _k = REF.redact(row["text"])
    assert out == row["text"] and n == 0, (nid, out)


@pytest.mark.parametrize("pid,row", POS, ids=[i for i, _ in POS])
def test_reference_model_is_idempotent_on_positives(pid, row):
    once, _n, _k = REF.redact(row["text"])
    twice, n2, _k2 = REF.redact(once)
    assert twice == once and n2 == 0, pid


def test_reference_model_idempotent_texts_are_untouched():
    for t in T.IDEMPOTENT_TEXTS:
        out, n, _k = REF.redact(t)
        assert out == t and n == 0, t


def test_reference_model_pins_the_accepted_over_masks():
    for p in T.ACCEPTED_OVER_MASK:
        out, n, _k = REF.redact(p["text"])
        assert out == p["expected"] and n == p["count"], p["id"]


def test_reference_model_pins_the_known_misses_as_not_masked():
    for n in T.KNOWN_MISSES:
        out, k, _kinds = REF.redact(n["text"])
        assert out == n["text"] and k == 0, n["id"]


def test_overlapping_hits_merge_into_one_interval_with_the_highest_priority_kind():
    text = "OPENAI_API_KEY=" + T.F("ov1", 8) + "." + "sk-proj-" + T.F("ov2", 30, T.B64URL)
    out, n, kinds = REF.redact(text)
    assert n == 1 and kinds == ["OpenAI"], (out, kinds)             # 键值规则的区间包着令牌规则的区间：并集是一个区间，类型取优先级高的
    assert out == "OPENAI_API_KEY=" + T.PLACEHOLDER % "OpenAI"
    # 相邻但不重叠的不合并
    a = "密码是" + "ab12" + " 和 密码是" + "cd34"
    assert REF.redact(a)[1] == 2


def test_same_secret_twice_counts_twice_but_two_rules_on_one_interval_count_once():
    s = "sk-ant-" + T.F("tw", 32, T.B64URL)
    assert REF.redact("%s 和 %s" % (s, s))[1] == 2
    assert REF.redact("ANTHROPIC_API_KEY=%s" % s)[1] == 1


def test_placeholder_is_ascii_free_of_the_original_and_stable_text():
    assert T.PLACEHOLDER % "OpenAI" == "〔密钥已挡掉：OpenAI〕"
    assert T.RULES_VERSION == 1


# ---------------------------------------------------------------- 三、产品实现上的金标测试（乙写出 yuanwei.redact 以后自动开跑）
@pytest.fixture(scope="module")
def prod():
    return pytest.importorskip("yuanwei.redact", reason="交回点乙才写 yuanwei.redact；这一组是金标，等实现")


@pytest.mark.parametrize("pid,row", POS, ids=[i for i, _ in POS])
def test_prod_positive_rows(prod, pid, row):
    r = prod.redact(row["text"])
    assert r.text == row["expected"] and r.count == row["count"], pid


@pytest.mark.parametrize("nid,row", NEG, ids=[i for i, _ in NEG])
def test_prod_negative_rows(prod, nid, row):
    r = prod.redact(row["text"])
    assert r.text == row["text"] and r.count == 0, nid


@pytest.mark.parametrize("pid,row", POS, ids=[i for i, _ in POS])
def test_prod_is_idempotent_and_deterministic(prod, pid, row):
    a = prod.redact(row["text"])
    b = prod.redact(row["text"])
    c = prod.redact(a.text)
    assert a.text == b.text and a.count == b.count, pid
    assert c.text == a.text and c.count == 0, pid


def test_prod_pinned_decisions(prod):
    for p in T.ACCEPTED_OVER_MASK:
        assert prod.redact(p["text"]).text == p["expected"], p["id"]
    for n in T.KNOWN_MISSES:
        assert prod.redact(n["text"]).text == n["text"], n["id"]
    for t in T.IDEMPOTENT_TEXTS:
        assert prod.redact(t).text == t


def test_prod_rules_version_matches_the_table(prod):
    assert prod.RULES_VERSION == T.RULES_VERSION
    assert prod.PLACEHOLDER.replace("{kind}", "%s").replace("%s", "%s") == T.PLACEHOLDER or prod.PLACEHOLDER == T.PLACEHOLDER


def test_prod_does_not_import_tests():
    pytest.importorskip("yuanwei.redact")
    import inspect
    import yuanwei.redact as R
    src = inspect.getsource(R)
    assert "secret_rules_table" not in src and "secret_reference" not in src
