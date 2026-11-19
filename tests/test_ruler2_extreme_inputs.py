"""尺子 2：合成极端输入（全英文、全数字、全标点、代码、表情、超长一行）。核：材料外壳、分页说明、游标、编码后的正文都算进去了；每一页都满足宿主的原有限制。
全部自造数据，不联网。"""
import random

import pytest

from helpers import Scripted, make_mem
from yuanwei import hosts as H
from yuanwei import encoding as EN
from yuanwei import note as NT
from yuanwei import pack as PK
from yuanwei import paging as PG
from yuanwei import util as U

RNG = random.Random("尺子2-极端")
CODE = "def f(x):\n    return {k: v for k, v in x.items() if k.startswith('a')}\n\n" * 40
EXTREME = {
    "全英文": ("The quick brown fox jumps over the lazy dog. " * 120).strip(),
    "全数字": " ".join("".join(RNG.choice("0123456789") for _ in range(12)) for _ in range(300)),
    "全标点": "".join(RNG.choice("!?.,;:-()[]{}<>/\\|@#$%^&*_+=~") for _ in range(4000)),
    "代码": CODE,
    "表情和别的文字": "😀🔥✨ Привет мир ναι مرحبا こんにちは สวัสดี " * 80,
    "超长一行": "x" * 60000,
}
HOSTS = [(h, p) for h in ("claude-code", "codex", "hermes", "pi") for p in ("mcp_return",)]


def _mem(tmp_path, texts):
    mem = make_mem(tmp_path, provider=Scripted("skip"), spawn="off")
    ids = []
    for i, (name, t) in enumerate(texts.items(), 1):
        ids.append(mem.record("h", "S1", "e%d" % i, "user" if i % 2 else "assistant", t, time="2027-01-01")["msg_id"])
    return mem, ids


@pytest.mark.parametrize("host,pname", HOSTS)
def test_every_get_messages_page_fits_the_host_limits_for_extreme_inputs(tmp_path, host, pname):
    mem, ids = _mem(tmp_path, EXTREME)
    path = H.profile(host).paths[pname]
    rd = mem._reader
    for mid in ids:
        out = PG.get_messages_fit(rd, [mid], 0, 0, None, path)
        got, pages = [], 0
        while True:
            ok, detail = H.fits(path, out["text"])
            assert ok, (host, mid, detail)                                          # 最终文字（含说明、游标、数据边界）满足这家宿主这条路径的全部限制
            assert out["messages"] and out["text"].count("——yuanwei｜截点") == 1  # 37新增未整理说明，分页说明仍只有一份
            assert out["text"].count("——yuanwei｜未整理：") == 1
            if out["cursor"]:
                assert out["cursor"] in out["text"]                                  # 游标也在最终文字里
            got.append(out["messages"][0]["range"])
            pages += 1
            assert pages < 5000
            if not out["cursor"]:
                break
            out = PG.get_messages_fit(rd, None, 0, 0, out["cursor"], path)
        # 各页的范围首尾相接，拼起来盖住整条消息（编码后的正文）
        content = rd.store.message_row("w", mid)["content"]
        assert got[0][0] == 0 and got[-1][1] == len(rd.mp.encode(content))
        for a, b in zip(got, got[1:]):
            assert a[1] == b[0]


def test_get_messages_page_tokens_count_the_encoded_text_and_head_for_both_rulers(tmp_path):
    mem, ids = _mem(tmp_path, EXTREME)
    rd = mem._reader
    for ruler in (1, 2):
        rd.cfg.ruler_version = ruler
        for mid in ids:
            limit = 60
            res = rd.get_messages([mid], page_tokens=limit)
            used = 0
            for m in res["messages"]:
                head = "〔%s｜%s〕" % (PK.ROLE_CN[m["role"]], m["msg_id"])
                used += U.est_tokens_line(head + m["text"], ruler)
            assert used <= limit or (len(res["messages"]) == 1 and res["messages"][0]["truncated"])      # 超额只允许「一条消息里至少交一个字符」这一种
            assert res["cursor"].startswith(res["cursor"].split(":")[0]) if res["cursor"] else True
        assert rd.cfg.ruler_version == ruler
    rd.cfg.ruler_version = 2


def test_material_and_note_tokens_include_the_shell_for_both_rulers(tmp_path):
    texts = {k: v[:1500] for k, v in EXTREME.items()}
    mem, ids = _mem(tmp_path, texts)
    rows = [mem._reader.snapshot().by_id[m] for m in ids]
    for ruler in (1, 2):
        b = PK.build_material(rows, mem._reader.mp)
        assert b["material"]
        wrapped = EN.wrap_material(b["material"])
        want = sum(U.est_tokens_line(l, ruler) for l in wrapped.split("\n"))              # 开头说明、会话行、引用行、首尾边界行都在里面
        assert PK.model_tokens(b["material"], ruler) == want
        meter = PK.Meter({r["msg_id"]: r for r in rows}, mem._reader.mp, ruler=ruler)
        assert meter.total([r["msg_id"] for r in rows]) == want
    mem2 = make_mem(tmp_path, name="n.sqlite", provider=Scripted("skip"), spawn="off", note_tokens=700)
    for i, (k, v) in enumerate(texts.items(), 1):
        mem2.record("h", "S1", "n%d" % i, "user" if i % 2 else "assistant", "keyword " + v[:300], time="2027-01-01")
    r = mem2.note("keyword")
    assert r["status"] == "ok"
    own = sum(U.est_tokens_line(l, 2) for l in r["note"].split("\n"))                        # 自己重数：便条全文（开头说明、边界、提示行、编号）逐行算
    assert r["est_tokens"] == NT.text_tokens(r["note"], 2) == own <= mem2.cfg.note_tokens    # 都算进了额度
    assert r["chars"] == len(r["note"]) <= mem2.cfg.note_chars


def test_recall_pages_fit_hosts_for_extreme_inputs(tmp_path):
    texts = {k: "keyword " + v[:3000] for k, v in EXTREME.items()}
    mem, ids = _mem(tmp_path, texts)
    mem.cfg.recall_ranked_pages = False  # This case verifies the legacy arbitrary text splitting contract.
    for host, pname in HOSTS:
        path = H.profile(host).paths[pname]
        page = PG.recall_first(mem._reader, "keyword", None, 24000, path)
        n = 0
        while True:
            ok, detail = H.fits(path, page["text"])
            assert ok, (host, detail)
            n += 1
            assert n < 3000
            if not page["cursor"]:
                break
            page = PG.recall_next(mem._reader, page["cursor"], path)


def test_get_messages_page_budget_is_honoured_by_the_configured_ruler_for_many_short_messages(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), spawn="off")
    ids = [mem.record("h", "S1", "q%d" % i, "user" if i % 2 else "assistant", ("Short english message number %d with a few words in it. " % i) * 3, time="2027-01-01")["msg_id"]
           for i in range(1, 13)]
    rd = mem._reader
    pages = {}
    for ruler in (1, 2):
        rd.cfg.ruler_version = ruler
        res = rd.get_messages(ids=ids, page_tokens=60)
        used = sum(U.est_tokens_line("〔%s｜%s〕%s" % (PK.ROLE_CN[m["role"]], m["msg_id"], m["text"]), ruler) for m in res["messages"])
        assert used <= 60 or len(res["messages"]) == 1                             # 按配置的那把尺子，一页不超额
        pages[ruler] = len(res["messages"])
    assert pages[2] > pages[1]                                                      # 尺子 2 下英文每条更轻，同一个额度一页装得多


@pytest.mark.parametrize("host,pname", HOSTS)
def test_ranked_extreme_groups_error_or_every_whole_page_fits(tmp_path, host, pname):
    from yuanwei.ranked_pages import GroupTooLarge
    mem, ids = _mem(tmp_path, {k: "keyword " + v[:3000] for k, v in EXTREME.items()})
    path = H.profile(host).paths[pname]
    try:
        page = PG.recall_first(mem._reader, "keyword", None, 24000, path)
    except GroupTooLarge as error:
        assert "get_messages" in str(error)
        assert mem.store.conn.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
        return
    delivered = []
    while True:
        assert H.fits(path, page["text"])[0]
        delivered += page["delivered"]
        if not page["cursor"]:
            break
        page = PG.recall_next(mem._reader, page["cursor"], path)
    assert {d["msg_id"] for d in delivered}
    for d in delivered:
        assert d["start"] == 0
        assert d["end"] == len(mem._reader.mp.encode(mem.store.message_row(mem.ns, d["msg_id"])["content"]))
