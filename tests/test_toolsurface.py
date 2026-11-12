"""四个工具的核心（不经过 MCP 进程）：身份绑定和读取权限、可选关键词、返回大小按宿主单位核、翻页固定同一份结果、出错不冒充空结果。第二段·交回点甲（V7 的核心部分）。"""
import dataclasses
import json

import pytest

from helpers2 import TEXTS, Counter, FakeEmbedClient, fill, hybrid, organized
from yuanwei import hosts as H
from yuanwei.access import Access, ToolIdentity
from yuanwei.idsource import InProcessIdentity
from yuanwei.tools import ToolSurface


def setup(tmp_path, host="pi", texts=None, turn_text=None, profile=None, bind=True, **cfg):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path, texts=texts, **cfg)
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set(host, "S", "", "T1")
    if bind:
        acc.begin_turn(host, "S", "T1", text=turn_text)
    surf = ToolSurface(mem, acc, profile or H.profile(host), ids)
    return mem, acc, surf, ids


def small_profile(host="pi", path="mcp_return", **limits):
    """自定的小上限（可信配置），用来测边界；单位照给。"""
    base = H.profile(host)
    lim = tuple(H.Limit(u, t, "test") for u, t in limits.items())
    return dataclasses.replace(base, paths=dict(base.paths, **{path: dataclasses.replace(base.paths[path], limits=lim, overhead={})}))


# ------------------------------------------------------------------ 哪些工具开放
def test_tools_opened_follow_identity_chain_status(tmp_path):
    for host, want in (("pi", ["attachments", "get_messages", "recall", "status"]), ("hermes", ["attachments", "get_messages", "recall", "status"]),
                       ("claude-code", ["status"]), ("codex", ["status"])):
        mem, acc, surf, ids = setup(tmp_path / host, host=host)
        assert sorted(surf.tool_names()) == want


def test_unsupported_host_calls_never_reach_the_reader_and_status_has_no_counts(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, host="claude-code")
    cnt = Counter(mem)
    try:
        for name, args in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"})):
            r = surf.call(name, args)
            assert r.status == "refused" and r.reason == "identity_unproven" and "暂不支持" in r.text
        s = surf.call("status", {})
        assert s.status == "refused" and s.reason == "identity_unproven" and "pending" not in s.text and "backlog" not in s.text
        assert "claude-code" in s.text and "2.1.300" in s.text
        assert cnt.calls == []
    finally:
        cnt.close()


def test_unknown_tool_is_an_error(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    r = surf.call("remember", {"text": "x"})
    assert r.status == "error" and r.reason == "unknown_tool"
    for n in ("ask", "answer_request", "export", "remember"):
        assert n not in surf.tool_names()


# ------------------------------------------------------------------ 身份和权限
def test_unbound_identity_refused_before_any_search(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    ids.clear()
    cnt = Counter(mem)
    try:
        for name, args in (("recall", {"query": "烤鱼"}), ("get_messages", {"ids": ["x"]}), ("attachments", {"message_id": "x"}), ("status", {})):
            r = surf.call(name, args)
            assert r.status == "refused" and r.reason == "identity_unbound" and r.text.startswith("〔yuanwei｜已拒绝") and "无法确认权限，已暂停新增读取" in r.text
        assert cnt.calls == []
    finally:
        cnt.close()


def test_unregistered_turn_refused_and_registered_turn_reads(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, bind=False)
    assert surf.call("recall", {"query": "烤鱼"}).reason == "turn_unregistered"
    acc.begin_turn("pi", "S", "T1")
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"


def test_model_supplied_identity_and_permission_args_are_ignored(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, turn_text="#yuanwei-nomem\n查烤鱼")
    cnt = Counter(mem)
    try:
        r = surf.call("recall", {"query": "烤鱼", "history_allowed": True, "session_id": "OTHER", "turn_id": "T-other", "workspace": "elsewhere", "turn": "1", "host": "claude-code"})
        assert r.status == "refused" and r.reason == "control_nomem" and cnt.calls == []
    finally:
        cnt.close()
    acc.begin_turn("pi", "S", "T2")
    ids.set("pi", "S", "", "T2")
    r = surf.call("recall", {"query": "烤鱼", "history_allowed": False, "session_id": "OTHER", "workspace": "elsewhere"})
    assert r.status == "ok" and TEXTS[1] in r.text                      # 模型填 history_allowed=False 也不能改变可信状态（这一轮是允许的）
    start = [x for x in acc.log_rows("pi", "S", "T2") if x["kind"] == "start"][0]["detail"]["summary"]
    assert start["ignored_args"] == ["history_allowed", "session_id", "workspace"]


def test_banned_turn_blocks_all_four_tools_including_old_cursor(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    first = surf.call("get_messages", {"ids": [r["msg_id"] for r in mem.store.list_messages(mem.ns)][:2]})
    assert first.status == "ok"
    mem.cfg.recall_page_chars = 60
    cur = surf.call("recall", {"query": "预算"}).meta.get("cursor")
    acc.begin_turn("pi", "S", "T2", text="#yuanwei-nomem")
    ids.set("pi", "S", "", "T2")
    cnt = Counter(mem)
    try:
        calls = [("recall", {"query": "预算"}), ("recall", {"cursor": cur or "x:0:0"}), ("get_messages", {"cursor": "x:0:0"}), ("get_messages", {"ids": ["a"]}),
                 ("attachments", {"message_id": "a"}), ("status", {})]
        for name, args in calls:
            r = surf.call(name, args)
            assert r.status == "refused" and r.reason == "control_nomem", (name, r.text)
            assert "本轮未新增读取长期记忆" in r.text
        assert cnt.calls == []                                               # 没有搜索、没有拉起整理、没有游标查找
        assert "pending" not in surf.call("status", {}).text
    finally:
        cnt.close()


def test_status_gives_counts_and_config_but_no_original_text_or_thread_names(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    cnt = Counter(mem)
    try:
        r = surf.call("status", {})
        assert r.status == "ok"
        d = json.loads(r.text.split("\n", 1)[1]) if r.text.startswith("〔") else json.loads(r.text)
        for k in ("backlog", "pending_writes", "pending_review", "done_steps", "retrieval", "write", "host", "identity_chain", "mode", "limits"):
            assert k in d, k
        for t in TEXTS:
            assert t not in r.text and t[:4] not in r.text
        assert "check_and_spawn" not in cnt.calls                            # status 不拉起整理
    finally:
        cnt.close()


# ------------------------------------------------------------------ recall：参照路径、关键词、出错
def test_recall_without_keywords_prefix_equals_first_stage_text(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    ref = mem._reader.recall("吃了烤鱼")["text"]
    r = surf.call("recall", {"query": "吃了烤鱼"})
    assert r.status == "ok" and r.text.startswith(ref) and "截点" in r.text and "已是最后一页" in r.text
    for kw in (None, [], ["", " "], ["吃了烤鱼"]):
        r2 = surf.call("recall", {"query": "吃了烤鱼", "keywords": kw} if kw is not None else {"query": "吃了烤鱼", "keywords": None})
        assert r2.text.startswith(ref)


def test_recall_with_keywords_finds_what_the_question_misses(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    assert "没有找到相关记录" in surf.call("recall", {"query": "完全无关的话题"}).text
    r = surf.call("recall", {"query": "完全无关的话题", "keywords": ["贝壳", "日出"]})
    assert TEXTS[0] in r.text and TEXTS[3] in r.text
    assert r.meta["queries"] == 3


def test_errors_are_recognizable_and_bounded_not_empty_results(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    big = "x" * 100000
    cases = [("recall", {"query": "q", "keywords": "烤鱼"}, "bad_query"), ("recall", {"query": "q", "keywords": [big]}, "bad_query"),
             ("recall", {"query": "q", "keywords": [3]}, "bad_query"), ("recall", {}, "bad_arguments"), ("recall", {"query": 7}, "bad_arguments"),
             ("recall", {"query": big, "keywords": big}, "bad_query"),
             ("recall", {"cursor": "nope"}, "bad_cursor"), ("recall", {"cursor": "a:1:1", "query": "q"}, "bad_arguments"), ("get_messages", {"ids": ["no-such-id"]}, "not_found"),
             ("get_messages", {}, "bad_arguments"), ("get_messages", {"ids": big}, "bad_arguments"), ("get_messages", {"ids": [big]}, "not_found"),
             ("get_messages", {"cursor": "zz"}, "bad_cursor"), ("attachments", {"message_id": big}, "not_found"), ("attachments", {}, "bad_arguments"),
             ("recall", {"query": "q", "budget": "many"}, "bad_arguments"), ("get_messages", {"ids": ["a"], "before": -1}, "bad_arguments")]
    for name, args, reason in cases:
        r = surf.call(name, args)
        assert r.status == "error" and r.reason == reason and r.is_error, (name, args if len(str(args)) < 80 else "…", r.text[:100])
        assert 0 < len(r.text) <= 700 and big[:200] not in r.text


def test_missing_vector_is_an_error_not_zero_hits(tmp_path):
    mem, client = hybrid(tmp_path)
    mem._reader.embed_client = None
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    acc.begin_turn("pi", "S", "T")
    surf = ToolSurface(mem, acc, H.profile("pi"), ids)
    r = surf.call("recall", {"query": "吃了烤鱼"})
    assert r.status == "error" and r.reason == "missing_vector" and "没有找到相关记录" not in r.text


def test_unorganized_message_is_readable_and_says_no_result_in_this_cut(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    mem.record("h", "S9", "n1", "user", "这条还没整理", time="2027-02-02")
    mid = mem.store.list_messages(mem.ns, "S9")[0]["msg_id"]
    r = surf.call("get_messages", {"ids": [mid]})
    assert r.status == "ok" and "这条还没整理" in r.text
    r = surf.call("attachments", {"message_id": mid})
    assert r.status == "ok" and "本截点尚无整理结果" in r.text


def test_workspace_is_bound_by_configuration_and_ids_cannot_cross(tmp_path):
    memA = organized(tmp_path, ws="A", name="a.sqlite", texts=["甲里的话"], session="只在甲里")
    memB = organized(tmp_path, ws="B", name="b.sqlite", texts=["乙里的话"])
    mid_a = memA.store.list_messages("A")[0]["msg_id"]
    accB = Access(memB)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    accB.begin_turn("pi", "S", "T")
    surfB = ToolSurface(memB, accB, H.profile("pi"), ids)
    r = surfB.call("get_messages", {"ids": [mid_a], "workspace": "A"})
    assert r.status == "error" and r.reason == "not_found" and "甲里的话" not in r.text


def test_get_messages_and_attachments_work_and_render_bounded_text(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    mids = [r["msg_id"] for r in mem.store.list_messages(mem.ns)]
    r = surf.call("get_messages", {"ids": [mids[1]], "before": 1, "after": 1})
    assert r.status == "ok" and TEXTS[0] in r.text and TEXTS[1] in r.text and TEXTS[2] in r.text and mids[1] in r.text
    a = surf.call("attachments", {"message_id": mids[0]})
    assert a.status == "ok" and mids[0] in a.text


# ------------------------------------------------------------------ 返回大小：按宿主单位核完整的最终返回
def independent_units(text):
    """测试自己的计数（不借用 hosts.measure）。"""
    b = text.encode("utf-8")
    return {"cp": len(text), "utf16": len(text.encode("utf-16-le")) // 2, "bytes": len(b), "codex_tok": (len(b) + 3) // 4}


LONG_ZH = "这是一条很长的中文原话，" * 120
EMOJI = "😀好的😀谢谢😀" * 80
NEWLINES = "ab\n" * 300


@pytest.mark.parametrize("name,texts", [("中文", [LONG_ZH, "短的一句"]), ("表情", [EMOJI, "短的一句"]), ("很长的单条", ["长" * 4000, "另一条短的"]),
                                         ("编码后变长", [NEWLINES, NEWLINES]), ("混合", [LONG_ZH, EMOJI, NEWLINES, "短"])])
@pytest.mark.parametrize("unit,limit", [("bytes", 3000), ("utf16", 900), ("cp", 700), ("codex_tok", 500)])
def test_recall_pages_fit_unit_and_cover_everything_without_gaps(tmp_path, name, texts, unit, limit):
    mem, acc, surf, ids = setup(tmp_path, texts=texts, profile=small_profile(**{unit: limit}))
    cnt = Counter(mem)
    try:
        r = surf.call("recall", {"query": "原话 好的 短的 另一条 ab 长"})
        pages, spans = [], []
        while True:
            assert r.status == "ok", r.text
            assert independent_units(r.text)[unit] <= limit, (unit, independent_units(r.text), len(pages))
            pages.append(r.text)
            spans += r.meta["delivered"]
            cur = r.meta.get("cursor")
            if not cur:
                break
            r = surf.call("recall", {"cursor": cur})
            assert len(pages) < 400
        assert cnt.calls.count("select_multi") <= 1 and cnt.calls.count("select") <= 1       # 完整取材只走一次；翻页不重排、不重搜
        assert cnt.calls.count("candidates") <= 1 and cnt.calls.count("_prepare_query_vector") <= 1
    finally:
        cnt.close()
    by = {}
    for s in spans:
        by.setdefault(s["msg_id"], []).append((s["start"], s["end"]))
    assert by, "没有交出任何原话"
    for mid, rs in by.items():
        row = mem.store.message_row(mem.ns, mid)
        enc = mem._reader.mp.encode(row["content"])
        assert rs[0][0] == 0 and rs[-1][1] == len(enc)
        for (a1, b1), (a2, b2) in zip(rs, rs[1:]):
            assert b1 == a2 and a1 < b1                                              # 连续，不重复，不漏


def test_exactly_at_the_limit_fits_and_one_less_splits(tmp_path):
    texts = ["中文原话。" * 30]
    mem, acc, surf, ids = setup(tmp_path, texts=texts)
    whole = surf.call("recall", {"query": "中文原话"})
    n = independent_units(whole.text)["utf16"]
    assert whole.meta.get("cursor") is None
    mem2, acc2, surf2, ids2 = setup(tmp_path / "eq", texts=texts, profile=small_profile(utf16=n))
    assert surf2.call("recall", {"query": "中文原话"}).meta.get("cursor") is None             # 刚好等于上限：一页
    mem3, acc3, surf3, ids3 = setup(tmp_path / "lt", texts=texts, profile=small_profile(utf16=n - 1))
    r3 = surf3.call("recall", {"query": "中文原话"})
    assert r3.meta.get("cursor") and independent_units(r3.text)["utf16"] <= n - 1             # 差一：分页，且每页仍在限内


def test_same_page_refetch_is_identical_and_later_commits_do_not_change_pages(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, texts=["中文原话。" * 120, "另一条原话" * 50], profile=small_profile(utf16=800))
    r1 = surf.call("recall", {"query": "原话"})
    cur = r1.meta["cursor"]
    p2a = surf.call("recall", {"cursor": cur})
    fill(mem, "S2", ["后台新提交的一条原话", "再来一条原话"])                                  # 翻页期间后台有新提交
    p2b = surf.call("recall", {"cursor": cur})
    assert p2a.text == p2b.text and p2a.meta["delivered"] == p2b.meta["delivered"]
    assert "后台新提交" not in p2b.text


def test_only_the_delivered_page_is_registered_in_the_log(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, texts=["中文原话。" * 200], profile=small_profile(utf16=600))
    r1 = surf.call("recall", {"query": "原话"})
    rows = [x for x in acc.log_rows("pi", "S", "T1") if x["kind"] == "assembled"]
    assert len(rows) == 1 and rows[0]["detail"]["spans"] == r1.meta["delivered"]
    assert r1.meta["cursor"]


def test_get_messages_pages_fit_and_cover_with_cursor(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, texts=["长" * 2500, EMOJI, NEWLINES], profile=small_profile(bytes=2500))
    mids = [r["msg_id"] for r in mem.store.list_messages(mem.ns)]
    r = surf.call("get_messages", {"ids": mids})
    got, n = {}, 0
    while True:
        assert r.status == "ok", r.text
        assert independent_units(r.text)["bytes"] <= 2500
        for m in r.meta["messages"]:
            got.setdefault(m["msg_id"], []).append(tuple(m["range"]))
        cur = r.meta.get("cursor")
        if not cur:
            break
        r = surf.call("get_messages", {"cursor": cur})
        n += 1
        assert n < 400
    for mid in mids:
        enc = mem._reader.mp.encode(mem.store.message_row(mem.ns, mid)["content"])
        rs = got[mid]
        assert rs[0][0] == 0 and rs[-1][1] == len(enc) and all(b1 == a2 for (_a1, b1), (a2, _b2) in zip(rs, rs[1:]))


def test_structured_content_is_counted_in_the_final_return():
    from yuanwei.mcp import server as S
    text = "正文" * 100
    plain = S.final_return_text({"content": [{"type": "text", "text": text}], "structuredContent": None})
    dup = S.final_return_text({"content": [{"type": "text", "text": text}], "structuredContent": {"result": text}})
    assert plain == text and len(dup) > 2 * len(text)
    path = small_profile(utf16=len(text) + 10).paths["mcp_return"]
    assert H.fits(path, plain)[0] and not H.fits(path, dup)[0]


def test_unavoidable_oversize_is_an_error_not_delivered_and_logged(tmp_path):
    mem, acc, surf, ids = setup(tmp_path, profile=small_profile(cp=20))
    r = surf.call("recall", {"query": "吃了烤鱼"})
    assert r.status == "error" and r.reason == "return_too_large" and len(r.text) < 700
    kinds = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "returned" not in kinds and "oversize_blocked" in kinds


def test_return_time_recheck_blocks_a_deny_that_happens_during_the_read(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    orig = mem._reader.select_multi

    def deny_then_select(*a, **k):
        out = orig(*a, **k)
        acc.deny_turn("pi", "S", "T1")
        return out
    mem._reader.select_multi = deny_then_select
    r = surf.call("recall", {"query": "吃了烤鱼"})
    assert r.status == "refused" and r.reason == "control_changed" and TEXTS[1] not in r.text
    assert "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认" in r.text
    kinds = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "search_entered" in kinds and "blocked_at_return" in kinds and "returned" not in kinds        # 已经发生的查询照实记


def test_result_dict_shapes_never_include_structured_content(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    r = surf.call("recall", {"query": "烤鱼"})
    assert not hasattr(r, "structured") or r.structured is None


def test_hybrid_vector_service_is_called_only_on_the_first_page(tmp_path):
    mem, client = hybrid(tmp_path, texts=["烤鱼" + "，中文原话" * 60 for _ in range(6)])
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set("pi", "S", "", "T")
    acc.begin_turn("pi", "S", "T")
    surf = ToolSurface(mem, acc, small_profile(utf16=700), ids)
    r = surf.call("recall", {"query": "烤鱼的事", "keywords": ["中文原话"]})
    assert r.status == "ok" and r.meta["cursor"]
    n = len(client.calls)
    assert n == 1 and r.meta["queries"] == 2
    pages = 1
    while r.meta.get("cursor"):
        r = surf.call("recall", {"cursor": r.meta["cursor"]})
        assert r.status == "ok"
        pages += 1
        assert pages < 100
    assert pages > 2 and len(client.calls) == n                           # 翻页不发新的向量请求
    vec = [x for x in acc.log_rows("pi", "S", "T") if x["kind"] == "vector_called"]
    assert len(vec) == 1 and vec[0]["detail"]["calls"] == 1


def test_standing_switches_block_tools_and_receipt_follows_the_log(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    assert surf.call("recall", {"query": "烤鱼"}).status == "ok"
    acc.set_session_permission("pi", "S", False)
    r = surf.call("recall", {"query": "烤鱼"})
    assert r.reason == "session_setting" and "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认" in r.text        # 这一轮已经读过，交付没法确认
    acc.clear_session_permission("pi", "S")
    mem.set_read_permission(False)
    assert surf.call("get_messages", {"ids": ["x"]}).reason == "workspace_setting"
    acc.begin_turn("pi", "S", "T2")
    ids.set("pi", "S", "", "T2")
    assert surf.call("recall", {"query": "烤鱼"}).reason == "workspace_setting" and "本轮未新增读取长期记忆" in surf.call("status", {}).text


def test_failure_after_assembly_is_logged_as_delivery_failed_and_receipt_says_not_delivered(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    orig = acc.finish_read

    def boom(*a, **k):
        raise RuntimeError("down")
    acc.finish_read = boom
    r = surf.call("recall", {"query": "烤鱼"})
    acc.finish_read = orig
    assert r.status == "error" and r.reason == "internal_error"
    kinds = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "assembled" in kinds and "delivery_failed" in kinds and "returned" not in kinds
    acc.deny_turn("pi", "S", "T1")
    assert "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认" in surf.call("recall", {"query": "x"}).text


@pytest.mark.parametrize("host", ["claude-code", "codex", "hermes", "pi"])
def test_every_real_host_profile_mcp_path_pages_fit_all_its_limits(tmp_path, host):
    """每家的真实阈值和单位（不是自定的小上限）：长中文、表情、会膨胀的换行，每页的最终文字都在这家这条路径的全部限制内（用测试自己的计数）。
    读取工具没开放的宿主（身份链没证实）这里把 identity 临时改成 in_process，只为核大小这一层，不说它们的工具已开放。"""
    prof = dataclasses.replace(H.profile(host), identity="in_process")
    texts = ["这是一条很长的中文原话，" * 3000, "😀好的😀谢谢😀" * 3000, "ab\n" * 9000]
    mem, acc, surf, ids = setup(tmp_path, host=host, texts=texts, profile=prof)
    path = prof.paths["mcp_return"]
    r = surf.call("recall", {"query": "原话 好的 ab"})
    pages = 0
    while True:
        assert r.status == "ok", r.text[:200]
        u = independent_units(r.text)
        for l in path.limits:
            have = {"claude_tok_proxy": u["bytes"]}.get(l.unit, u.get(l.unit))
            assert have + path.overhead.get(l.unit, 0) <= l.threshold, (host, l.unit, have)
        pages += 1
        if not r.meta.get("cursor"):
            break
        r = surf.call("recall", {"cursor": r.meta["cursor"]})
        assert pages < 400
    assert pages >= (1 if host == "hermes" else 2)                      # Hermes 的 50,000 字符一页就够装下参照档的 24,000 估算词元；其余三家都要分页


@pytest.mark.parametrize("host", ["claude-code", "codex", "hermes", "pi"])
def test_every_real_host_hook_path_note_fits(tmp_path, host):
    from yuanwei.modes import ModeConfig, ModeRunner
    prof = H.profile(host).configured(codex_hook_limit=8000) if host == "codex" else H.profile(host)
    mem = organized(tmp_path, texts=["烤鱼" + "😀" * 900 + "\n" * 50 for _ in range(14)])
    runner = ModeRunner(mem, Access(mem), prof, ModeConfig(mode="note"))
    out = runner.on_user_prompt("S", "T1", "烤鱼")
    assert out["kind"] == "note"
    u = independent_units(out["inject"])
    for l in prof.paths["hook_context"].limits:
        assert u[l.unit] + prof.paths["hook_context"].overhead.get(l.unit, 0) <= l.threshold, (host, l.unit, u[l.unit])
    assert len(out["inject"]) <= 8000


def test_allowed_reads_may_start_background_organizing_but_status_and_refused_calls_never_do(tmp_path):
    mem, acc, surf, ids = setup(tmp_path)
    cnt = Counter(mem)
    try:
        surf.call("status", {})
        assert "check_and_spawn" not in cnt.calls
        surf.call("recall", {"query": "烤鱼"})
        assert cnt.calls.count("check_and_spawn") == 1                      # 允许的读取入口顺带检查要不要整理（第一段的约定），status 不拉起
        acc.begin_turn("pi", "S", "T2", text="#yuanwei-nomem")
        ids.set("pi", "S", "", "T2")
        cnt.reset()
        surf.call("recall", {"query": "烤鱼"})
        assert cnt.calls == []
    finally:
        cnt.close()
