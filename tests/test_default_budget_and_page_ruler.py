"""默认额度降到 8,000；recall 的页和便条改成按尺子量（0.1.0a4）。全部自造数据，断网。

覆盖：默认值、中文分页和旧的 30,000 字基本一致、英文一页装满新上限、宿主硬限制仍然生效（四家各一例）、旧游标被拒、
便条在英文上用到词元额度、旧参照配置照旧按字数；第二轮：不带 budget 每次总额 8,000（recall_budget）、页 14,000 词元。"""
import dataclasses

import pytest

pytestmark = pytest.mark.usefixtures("calibrated_test_vectors")

from helpers import Clock, Scripted
from helpers2 import organized
from yuanwei import Config, Memory, hosts as H, util as U
from yuanwei.access import Access
from yuanwei.errors import BadCursor, ConfigError
from yuanwei.idsource import InProcessIdentity
from yuanwei.ranked_pages import GroupTooLarge, MessageTooLarge
from yuanwei.tools import ToolSurface


def han(i, n, mix=True):
    """n 个互不重样的汉字（每条消息用不同的起点），开头带共同的关键词。"""
    body = "".join(chr(0x4E00 + (i * 131 + j * 7) % 900) + (" " if mix and j % 7 == 6 else "") for j in range(n))      # 每 7 个汉字一个空格：真实中文里也有数字、标点、英文词，按尺子 2 更轻
    return "预算" + body


def eng(i, n):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    out = "needle %d" % i
    j = 0
    while len(out) < n:
        out += " " + words[(i * 3 + j) % 10] + str((i * 17 + j) % 97)
        j += 1
    return out[:n]


def seeded(tmp_path, texts, **cfg):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(neighbors=0, **cfg), provider=Scripted("skip"), clock=Clock())
    ids = [mem.record("h", "S", str(i), "user", t, time="2027-01-%02d" % (i % 28 + 1))["msg_id"] for i, t in enumerate(texts)]
    mem.end_session("h", "S")
    mem.run()
    return mem, ids


def pages(mem, query, budget=None):
    out = []
    page = mem.recall(query, budget=budget)
    while True:
        out.append(page)
        if not page["cursor"]:
            return out
        page = mem.recall(cursor=page["cursor"])


def tok(text, ruler=2):
    return U.est_lines(text.split("\n"), ruler)


def surface(mem, host):
    prof = dataclasses.replace(H.profile(host), identity="in_process")       # 只为核大小这一层，不说这家的工具已开放
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set(host, "S", "", "T1")
    acc.begin_turn(host, "S", "T1")
    return ToolSurface(mem, acc, prof, ids), prof


def units(text):
    b = text.encode("utf-8")
    return {"cp": len(text), "utf16": len(text.encode("utf-16-le")) // 2, "bytes": len(b), "claude_tok_proxy": len(b), "codex_tok": (len(b) + 3) // 4}


# ------------------------------------------------------------------ 默认值
def test_defaults_recall_budget_8000_page_tokens_14000_note_tokens_main():
    c = Config()
    assert (c.first_budget, c.total_budget, c.recall_budget) == (16000, 24000, 8000)
    assert (c.recall_page_tokens, c.recall_page_chars) == (14000, None)
    assert (c.note_tokens, c.note_chars) == (4200, 24000)
    assert c.note_chars >= 4200 * 1000 // U.W_ALNUM - 1                        # 字数兜底不会先于词元额度顶住（尺子 2 最轻的字符）


@pytest.mark.parametrize("bad", [{"recall_page_tokens": 0}, {"recall_page_tokens": True}, {"recall_page_tokens": "16000"}, {"recall_page_chars": 0}, {"recall_page_chars": -5}, {"recall_budget": 0}, {"recall_budget": "8000"}, {"recall_budget": True}])
def test_page_config_validation(bad):
    with pytest.raises(ConfigError):
        Config(**bad).validate()
    assert Config(recall_page_tokens=None, recall_page_chars=None, recall_budget=None).validate()


def test_read_budgets_rules():
    c = Config()
    assert c.read_budgets() == (8000, 5333)                                    # 不带 budget：每次总共 8,000，第一遍＝总额×2÷3
    assert c.read_budgets(24000) == (24000, 16000)                             # 助手要更多：照旧能要到 24,000，第一遍仍是 16,000
    assert c.read_budgets(12000) == (12000, 8000)
    assert c.read_budgets(50000) == (24000, 16000)                             # total_budget 仍是上限
    assert c.read_budgets(3000) == (3000, 2000)
    old = Config(recall_budget=None)                                           # 旧算法：不带就是 24,000，第一遍＝min(16,000，总额)
    assert old.read_budgets() == (24000, 16000) and old.read_budgets(12000) == (12000, 12000) and old.read_budgets(3000) == (3000, 3000)
    assert Config(first_budget=4000).read_budgets(24000) == (24000, 4000)


def test_default_recall_selects_about_8000_total_and_budget_arg_still_reaches_24000(tmp_path):
    texts = [han(i, 700) for i in range(120)]
    mem, _ = seeded(tmp_path, texts)
    got = mem._reader.select("预算")
    assert 7000 <= got["pack"]["最终"]["tokens"] <= 8000 and got["pack"]["第一遍"]["tokens"] <= 5333
    big = mem._reader.select("预算", total_budget=24000)
    assert 22000 <= big["pack"]["最终"]["tokens"] <= 24000 and 14000 <= big["pack"]["第一遍"]["tokens"] <= 16000
    assert mem._reader.select_multi("预算", ["预算"])["pack"]["最终"]["tokens"] <= 8000      # 多路查询同一个规则
    old, _ = seeded(tmp_path / "o", texts, recall_budget=None)
    o = old._reader.select("预算")
    assert 22000 <= o["pack"]["最终"]["tokens"] <= 24000 and 14000 <= o["pack"]["第一遍"]["tokens"] <= 16000   # 旧算法照旧


def test_recall_budget_is_in_the_cursor_binding(tmp_path):
    mem, _ = seeded(tmp_path, [eng(i, 1500) for i in range(60)], recall_page_tokens=3000)
    cur = mem.recall("needle", budget=24000)["cursor"]
    assert cur
    mem.cfg.recall_budget = 6000
    with pytest.raises(BadCursor, match="重新查"):
        mem.recall(cursor=cur)


# ------------------------------------------------------------------ 中文：和旧的 30,000 字基本一致
def test_chinese_pages_match_the_old_char_pages_closely(tmp_path):
    texts = [han(i, 480) for i in range(110)]
    new, _ = seeded(tmp_path / "n", texts)
    old, _ = seeded(tmp_path / "o", texts, recall_page_tokens=None, recall_page_chars=30000)
    pn, po = pages(new, "预算", 24000), pages(old, "预算", 24000)
    assert len(pn) == len(po) >= 2
    for a, b in zip(pn[:-1], po[:-1]):                                          # 末页只是剩下的，不比
        assert abs(len(a["text"]) - len(b["text"])) <= 0.08 * len(b["text"])        # 页的长度差在 8% 以内（页里的拉丁字母、数字、标点按尺子 2 更轻，多装不到一条）
        assert abs(len(a["delivered"]) - len(b["delivered"])) <= 0.08 * len(b["delivered"])        # 每页条数差在 8% 以内（这份合成数据一条约 550 字，差不到 4 条）
    first_new = [d["msg_id"] for d in pn[0]["delivered"]]
    first_old = [d["msg_id"] for d in po[0]["delivered"]]
    assert len(set(first_new) & set(first_old)) >= 0.9 * len(first_old)
    assert {d["msg_id"] for p in pn for d in p["delivered"]} == {d["msg_id"] for p in po for d in p["delivered"]}     # 选料集合不变，只是怎么分页


# ------------------------------------------------------------------ 英文：一页装满新上限
def test_english_page_fills_the_token_limit_not_the_old_30000_chars(tmp_path):
    texts = [eng(i, 1500) for i in range(150)]
    mem, _ = seeded(tmp_path, texts, recall_page_tokens=4000)
    ps = pages(mem, "needle", 24000)
    assert len(ps) >= 3
    biggest_group = max(tok(p["text"]) for p in ps[:-1])
    first = ps[0]["text"]
    assert len(first) > 15000 and tok(first) <= 4000                            # 一页的词元不超过主上限
    assert all(tok(p["text"]) <= 4000 for p in ps)
    for p in ps[:-1]:
        assert tok(p["text"]) >= 4000 - 2 * 300                                 # 装到差不到两条的地方（一条约 280 词元）
    assert biggest_group <= 4000
    default, _ = seeded(tmp_path / "d", texts)                                  # 默认页 14,000 词元：8,000 额度的材料一页装下
    d = pages(default, "needle", 8000)
    assert len(d) == 1 and len(d[0]["text"]) > 30000                            # 英文：一页的字符数远多于旧的 30,000
    old, _ = seeded(tmp_path / "x", texts, recall_page_tokens=None)
    o = pages(old, "needle", 8000)
    assert all(len(p["text"]) <= 30000 for p in o) and len(o) > 1               # 旧参照：只按 30,000 字，英文页数更多


# ------------------------------------------------------------------ 宿主硬限制照旧独立核（四家各一例）
@pytest.mark.parametrize("host", ["claude-code", "codex", "hermes", "pi"])
def test_host_hard_limits_still_apply_on_ranked_token_pages(tmp_path, host):
    texts = [eng(i, 1500) for i in range(110)]
    mem, _ = seeded(tmp_path / host, texts)
    sf, prof = surface(mem, host)
    path = prof.paths["mcp_return"]
    r = sf.call("recall", {"query": "needle", "budget": 24000})
    n, widest = 0, 0
    while True:
        assert r.status == "ok", (host, r.text[:200])
        u = units(r.text)
        for l in path.limits:
            assert u[l.unit] + path.overhead.get(l.unit, 0) <= l.threshold, (host, l.unit, u[l.unit])
        assert tok(r.text) <= mem.cfg.recall_page_tokens
        widest = max(widest, len(r.text))
        n += 1
        if not r.meta.get("cursor"):
            break
        r = sf.call("recall", {"cursor": r.meta["cursor"]})
        assert n < 400
    if host == "hermes":                                                        # 50,000 字符的硬限制：比旧的 30,000 字宽，装到接近硬限制
        assert 40000 < widest <= 50000 - 500
    else:
        assert n >= 2


def test_group_too_large_still_explicit_under_token_limit(tmp_path):
    mem, _ = seeded(tmp_path, [eng(i, 1500) for i in range(5)], recall_page_tokens=100)
    with pytest.raises(GroupTooLarge, match="get_messages"):
        mem.recall("needle")


# ------------------------------------------------------------------ 游标：旧版本、改了配置都拒绝
def test_old_cursor_from_char_paging_is_rejected_and_config_change_is_too(tmp_path):
    mem, _ = seeded(tmp_path, [eng(i, 1500) for i in range(60)], recall_page_tokens=3000)
    first = mem.recall("needle", budget=24000)
    cur = first["cursor"]
    assert cur and mem.recall(cursor=cur)["text"] == mem.recall(cursor=cur)["text"]
    cid = cur.split(":")[0]
    data = mem.store.get_cursor(cid)["data"]
    assert data["paging_version"] == 3 and data["page_limit"] == {"tokens": 3000, "chars": None, "ruler": 2}  # 37：游标冻结未整理状态
    for version in (1, 2):  # 1按字符分页；2还没有37要求的未整理状态
        old = dict(data, paging_version=version)
        mem.store.update_cursor(cid, old)
        with pytest.raises(BadCursor, match="重新查"):
            mem.recall(cursor=cur)
    mem.store.update_cursor(cid, data)
    assert mem.recall(cursor=cur)["text"]
    mem.cfg.recall_page_tokens = 4000                                           # 配置变了：页界会变，拒绝
    with pytest.raises(BadCursor, match="重新查"):
        mem.recall(cursor=cur)


# ------------------------------------------------------------------ 旧参照：照旧按字数
def test_old_reference_config_pages_by_chars_and_chronological_path_ignores_tokens(tmp_path):
    texts = [eng(i, 1500) for i in range(60)]
    mem, _ = seeded(tmp_path / "a", texts, recall_page_tokens=None, recall_page_chars=9000)
    ps = pages(mem, "needle", 24000)
    assert len(ps) > 3 and all(len(p["text"]) <= 9000 for p in ps)
    data = mem.store.get_cursor(mem.recall("needle")["cursor"].split(":")[0])["data"]
    assert data["page_limit"] == {"tokens": None, "chars": 9000, "ruler": 2}
    chron, _ = seeded(tmp_path / "b", texts, recall_ranked_pages=False)         # 按时间分页的旧路径：不管 recall_page_tokens，没设字符上限就是 30,000
    cs = pages(chron, "needle", 24000)
    assert all(len(p["text"]) <= 30000 for p in cs) and len(cs) > 1
    d = chron.store.get_cursor(chron.recall("needle")["cursor"].split(":")[0])["data"]
    assert d["page_limit"] == {"tokens": None, "chars": 30000, "ruler": 2}


def test_explicit_char_cap_still_applies_on_top_of_token_limit(tmp_path):
    mem, _ = seeded(tmp_path, [eng(i, 1500) for i in range(60)], recall_page_chars=7000)
    assert all(len(p["text"]) <= 7000 for p in pages(mem, "needle", 24000))


# ------------------------------------------------------------------ 便条
def test_english_note_uses_the_token_quota_and_old_reference_stays_on_chars(tmp_path):
    texts = [eng(i, 300) for i in range(120)]
    (tmp_path / "n").mkdir()
    (tmp_path / "o").mkdir()
    new = organized(tmp_path / "n", texts=texts)
    r = new.note("needle")
    assert r["status"] == "ok" and r["est_tokens"] <= 4200 and r["est_tokens"] >= 4200 - 300
    assert r["chars"] > 16000 and r["chars"] == len(r["note"])                  # 旧的 8,000 字先顶住时只有约 1,500 词元
    old = organized(tmp_path / "o", texts=texts, note_chars=8000)
    ro = old.note("needle")
    assert ro["chars"] <= 8000 and ro["est_tokens"] < 2200


def test_chinese_note_is_still_bound_by_tokens_at_about_the_old_size(tmp_path):
    texts = [han(i, 160, False) for i in range(80)]
    r = organized(tmp_path, texts=texts).note("预算")
    assert r["est_tokens"] <= 4200 and 7000 <= r["chars"] <= 9000               # 中文：4,200 词元约 8,000 字，和原来基本一样


def test_hermes_english_note_is_bound_by_the_host_hook_limit_not_the_old_8000(tmp_path):
    from yuanwei.modes import ModeConfig, ModeRunner
    texts = [eng(i, 300) for i in range(120)]
    mem = organized(tmp_path, texts=texts)
    prof = H.profile("hermes")
    out = ModeRunner(mem, Access(mem), prof, ModeConfig(mode="note")).on_user_prompt("S", "T1", "needle")
    assert out["kind"] == "note"
    path = prof.paths["hook_context"]
    assert H.fits(path, out["inject"])[0] and len(out["inject"]) <= H.char_cap(path)
    assert len(out["inject"]) > 8000                                            # Hermes 钩子 10,000 字符的硬限制，不是旧的 8,000 字


def test_host_char_cap_helper():
    assert H.char_cap(H.profile("hermes").paths["hook_context"]) == 10000 - 300
    assert H.char_cap(H.profile("claude-code").paths["mcp_return"]) == 50000 - 500
    assert H.char_cap(H.profile("codex").paths["mcp_return"]) is None           # 只有词元限制：靠 fits 核


# ------------------------------------------------------------------ get_messages 的分页本来就按配置里的尺子量（核过，没改）
def test_get_messages_pages_are_measured_on_the_configured_ruler(tmp_path):
    texts = [eng(i, 1500) for i in range(60)]
    mem, ids = seeded(tmp_path, texts)
    r = mem.get_messages(ids=ids[:50])
    assert r["cursor"]
    used2 = sum(U.est_tokens_line(m["text"], 2) for m in r["messages"])
    used1 = sum(U.est_tokens_line(m["text"], 1) for m in r["messages"])
    assert used2 <= mem.cfg.get_messages_page_tokens and used1 > mem.cfg.get_messages_page_tokens      # 按尺子 2 装满，换成尺子 1 就量超了
    assert U.est_tokens_line(r["messages"][0]["text"], 2) * (len(r["messages"]) + 1) > mem.cfg.get_messages_page_tokens * 0.9


# ------------------------------------------------------------------ 单条超长消息：不能被说成「没找到」
def long_msg(n):
    return han(7, n, False)                                                       # 约 0.52×n 估算词元


def test_one_long_relevant_message_is_reached_without_budget_by_relaxing_to_total(tmp_path):
    mem, ids = seeded(tmp_path, [long_msg(20000), "别的话题 晚饭吃面条"])                   # 约 10,400 词元：超过默认的 8,000，没超过 24,000
    r = mem.recall("预算")
    assert r["delivered"] and r["delivered"][0]["msg_id"] == ids[0] and "没有找到" not in r["text"]
    sel = mem._reader.select("预算")
    assert sel["selected"] and sel["budget_total"] == 24000                        # 自动放宽到 total_budget
    sf, _ = surface(mem, "hermes")
    got = sf.call("recall", {"query": "预算"})
    assert got.status == "ok" and "没有找到" not in got.text and len(got.meta["delivered"]) == 1
    multi = sf.call("recall", {"query": "预算", "keywords": ["预算"]})              # 多路查询的那条取材路径同一个规则
    assert multi.status == "ok" and len(multi.meta["delivered"]) == 1


def test_message_over_total_budget_says_found_but_too_long_not_not_found(tmp_path):
    mem, ids = seeded(tmp_path, [long_msg(60000)])                                  # 约 31,200 词元：超过 24,000
    with pytest.raises(MessageTooLarge, match="单条太长") as e:
        mem.recall("预算")
    assert ids[0] in str(e.value) and "get_messages" in str(e.value) and "没有找到" not in str(e.value)
    assert "已经到了额度上限" in str(e.value)
    sf, _ = surface(mem, "hermes")
    for args in ({"query": "预算"}, {"query": "预算", "keywords": ["预算"]}):
        res = sf.call("recall", args)
        assert res.status == "error" and res.reason == "group_too_large" and "单条太长" in res.text and "get_messages" in res.text and "没有找到" not in res.text


def test_explicit_small_budget_also_says_too_long_and_suggests_a_bigger_budget(tmp_path):
    mem, ids = seeded(tmp_path, [long_msg(20000)])
    with pytest.raises(MessageTooLarge, match="请带更大的 budget"):
        mem.recall("预算", budget=8000)
    assert mem.recall("预算", budget=24000)["delivered"]                              # 带大一点就拿得到
    assert "没有找到" in mem.recall("完全无关的话题xyz")["text"]                           # 真的没候选：照旧说没找到


def test_old_reference_budget_is_not_relaxed_and_not_reworded(tmp_path):
    mem, ids = seeded(tmp_path, [long_msg(20000)], recall_budget=None)             # 旧算法：不带就是 24,000，本来就装得下
    assert mem.recall("预算")["delivered"]
    big, _ = seeded(tmp_path / "b", [long_msg(60000)], recall_budget=None)         # 超过 24,000：旧行为是回「没有找到」（旧参照不改）
    r = big.recall("预算")
    assert not r["delivered"] and "没有找到" in r["text"]
    old_small, _ = seeded(tmp_path / "c", [long_msg(20000)], recall_budget=None)
    assert not old_small.recall("预算", budget=8000)["delivered"]                    # 旧算法也不会放宽


def test_legacy_chronological_path_also_reports_too_long_when_new_budget_is_on(tmp_path):
    mem, ids = seeded(tmp_path, [long_msg(60000)], recall_ranked_pages=False)
    with pytest.raises(MessageTooLarge, match="单条太长"):
        mem.recall("预算")


def test_cross_workspace_all_empty_but_one_too_long_says_too_long(tmp_path):
    from test_hermes_workspace import World, turn, call
    long_text = long_msg(60000)
    texts = {"life": [long_text], "proj:p-a": ["别的 内容 无关"], "proj:p-b": ["再别的 内容"]}
    world = World(tmp_path, scope="all_open", texts=texts, mem_cfg={"neighbors": 0})
    a = world.adapter("/elsewhere")
    turn(a, 1, "一个新问题")
    res = call(a, "recall", {"query": "预算"})
    assert res["status"] == "error" and res["reason"] == "group_too_large" and "单条太长" in res["text"] and "没有找到" not in res["text"]
    none = call(a, "recall", {"query": "qqqzzzxxx"})
    assert none["status"] == "ok" and "没有找到" in none["text"]
