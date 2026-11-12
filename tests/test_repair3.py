"""真机试装修补（三）（2026-11-11）：①recall 额外关键词超过 3 项只取前 3 项；②secrets／conversations 不手填库；③卸载计划说人话；④计划的「更新的文件」和实际一致。"""
import json
import os
import sqlite3
import sys

import pytest

from helpers_wizard import FAKE_KEY, FakeIO, FakeSystem, make_home, tree_bytes
from test_hermes_workspace import World, call, turn
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei import queries as Q
from yuanwei import tools as TL

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


# ================================================================== 1　recall 额外关键词超过 3 项
def test_clip_extras_pure_function():
    q = "清迈 旅行"
    same = ["a", "b", "c"]
    assert Q.clip_extras(q, same) == (same, []) and Q.clip_extras(q, same)[0] is same                  # 3 项以内：同一个对象，原样
    messy = ["a", " a ", "", "b", "清迈 旅行", "c"]
    assert Q.clip_extras(q, messy)[0] is messy and Q.clip_extras(q, messy)[1] == []                      # 去空去重（含和原问题相同的）以后 3 项：原样
    kw, dropped = Q.clip_extras(q, ["a", "b", "c", "d", " e ", "a", ""])
    assert kw == ["a", "b", "c"] and dropped == ["d", "e"]
    assert Q.clip_extras(q, None) == (None, []) and Q.clip_extras(q, "abc") == ("abc", []) and Q.clip_extras(q, ["a", 5, "b", "c", "d"])[1] == []      # 不合格的原样交给 normalize 报错
    assert "没有用的是：d、e" in Q.clip_note(["d", "e"]) and "…" in Q.clip_note(["x" * 40])


def _surface(tmp_path):
    w = World(tmp_path, scope="isolated", seed=True)
    a = w.adapter("/elsewhere")
    turn(a, 1, "普通")
    return a


def _spy(monkeypatch):
    seen = []
    real = TL.PG.recall_first

    def spy(rd, q, kw, budget, path, **k):
        seen.append(kw)
        return real(rd, q, kw, budget, path, **k)
    monkeypatch.setattr(TL.PG, "recall_first", spy)
    return seen


def test_up_to_three_extras_reach_the_search_unchanged_and_get_no_note(tmp_path, monkeypatch):
    a = _surface(tmp_path)
    seen = _spy(monkeypatch)
    kws = ["咖喱", " 咖喱 ", "", "土豆"]
    r = call(a, "recall", {"query": "周末做饭", "keywords": kws})
    assert r["status"] == "ok" and seen[0] is not None and seen[0] == kws and "额外的关键词超过" not in r["text"]       # 请求用的就是调用方给的那一份（150 次复验走这条路：逐字节不变）


def test_more_than_three_extras_uses_the_first_three_and_says_which_were_dropped(tmp_path, monkeypatch):
    a = _surface(tmp_path)
    seen = _spy(monkeypatch)
    r = call(a, "recall", {"query": "周末做饭", "keywords": ["咖喱", "土豆", "预算", "蓝绿", "演示稿", "咖喱", ""]})
    assert r["status"] == "ok" and seen[0] == ["咖喱", "土豆", "预算"]
    assert "〔yuanwei｜说明〕额外的关键词超过 3 项，只用了前 3 项照常查；没有用的是：蓝绿、演示稿。" in r["text"]
    r2 = call(a, "recall", {"query": "周末做饭", "keywords": ["咖喱", "土豆", "预算"]})
    assert r2["status"] == "ok" and "额外的关键词超过" not in r2["text"]


def test_other_bad_keyword_input_still_errors(tmp_path):
    a = _surface(tmp_path)
    for bad in ("咖喱", [1, 2], ["a", 5], ["x" * 129], ["x" * 129, "b", "c", "d"]):                         # 非列表、非文字、有一项过长（哪怕前面还有被丢掉的）
        r = call(a, "recall", {"query": "周末做饭", "keywords": bad})
        assert r["status"] == "error" and r.get("reason") == "bad_query", bad
    r = call(a, "recall", {"query": "周末做饭", "keywords": ["a" * 128, "b" * 128, "c" * 128, "d"]})        # 前 3 项合计 384，刚好合规；第 4 项丢掉
    assert r["status"] == "ok" and "没有用的是：d" in r["text"]
    r = call(a, "recall", {"query": "", "keywords": ["a"]})
    assert r["status"] == "error"


# ================================================================== 2　secrets／conversations 不手填库
def _installed_home(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC], sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    db = json.load(open(os.path.join(h, "yuanwei", "config.json")))["db"]
    from yuanwei import Config, Memory
    m = Memory.open(db, "life", config=Config(index_version=2), spawn="off")
    m.record("hermes", "S1", "m1", "user", "一句话", time="2027-01-01")
    m.close()
    return h, db


@pytest.mark.parametrize("argv", [["secrets", "scan"], ["secrets", "scan", "hermes"], ["conversations", "list"], ["conversations", "list", "hermes"], ["conversations", "status"], ["conversations", "status", "hermes"]])
def test_without_db_the_hermes_adapters_db_is_used_and_the_output_says_which(tmp_path, capsys, argv):
    h, db = _installed_home(tmp_path)
    capsys.readouterr()
    assert cli.main(argv + ["--hermes-home", h]) == 0
    out = json.loads(capsys.readouterr().out)
    assert os.path.realpath(out["db"]) == os.path.realpath(db) and "Hermes 接头的库" in out["db_source"] and "config.json" in out["db_source"]


def test_explicit_db_wins_and_is_labelled_and_missing_everything_says_how_to_write_it(tmp_path, capsys):
    h, db = _installed_home(tmp_path)
    capsys.readouterr()
    assert cli.main(["secrets", "scan", "--db", db, "--hermes-home", str(tmp_path / "nowhere")]) == 0
    assert json.loads(capsys.readouterr().out)["db_source"] == "--db"
    assert cli.main(["conversations", "list", "--hermes-home", str(tmp_path / "nowhere")]) == 2             # 没装接头、也没给 --db：照旧报错，并说怎么写
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "no_db" and "--db" in err["message"] and "conversations list hermes" in err["message"]
    assert cli.main(["secrets", "scan", "hermes", "--hermes-home", str(tmp_path / "nowhere")]) == 2
    capsys.readouterr()


def test_conversations_delete_preview_through_the_hermes_db(tmp_path, capsys):
    h, db = _installed_home(tmp_path)
    capsys.readouterr()
    assert cli.main(["conversations", "list", "--hermes-home", h]) == 0
    conv = json.loads(capsys.readouterr().out)["conversations"][0]
    cid = [v for k, v in conv.items() if k in ("window_id", "conversation_id", "id")] or [list(conv.values())[0]]
    assert cli.main(["conversations", "delete", "hermes", str(cid[0]), "--hermes-home", h]) in (0, 3)       # 预览（不加 --yes）：能走到库；写法 `delete hermes <编号>` 也行
    out = capsys.readouterr()
    assert "db_source" in out.out or "E_NOT_FOUND" in out.err or "not_found" in out.err.lower()


def test_help_text_and_docs_say_the_db_is_optional():
    assert "不给 --db 就用 Hermes 接头的库" in cli.USER_HELP and "secrets scan [hermes]" in cli.USER_HELP
    docs = open(os.path.join(os.path.dirname(__file__), "..", "docs", "装到Hermes上.md"), encoding="utf-8").read()
    assert "secrets scan hermes" in docs and "conversations list hermes" in docs


# ================================================================== 3　卸载只列计划：说人话
def test_uninstall_plan_is_plain_text_and_changes_nothing(tmp_path, capsys):
    h, db = _installed_home(tmp_path)
    capsys.readouterr()
    before = tree_bytes(h)
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--config-method", "yaml"]) == 0
    out = capsys.readouterr().out
    assert tree_bytes(h) == before
    assert "卸载原委" in out and "会把插件文件 plugins/yuanwei/__init__.py 换成一个什么都不做的空壳" in out and "memory.provider" in out
    assert "不删：你的记忆库" in out and "密钥文件" in out and "备份" in out
    assert out.rstrip().endswith("这是计划，什么都没改；加 --yes 才执行。")
    assert '"changed"' not in out and '"applied"' not in out and not out.lstrip().startswith("{")


def test_uninstall_plan_json_calls_it_would_change_and_applying_still_says_changed(tmp_path, capsys):
    h, db = _installed_home(tmp_path)
    capsys.readouterr()
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--config-method", "yaml", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["would_change"] is True and "changed" not in d and d["applied"] is False
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--config-method", "yaml", "--yes"]) == 0
    d2 = json.loads(capsys.readouterr().out)
    assert d2["applied"] is True and d2["changed"] is True and "would_change" not in d2
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--config-method", "yaml"]) == 0           # 再列一次：插件已经是空壳、设置已撤回
    again = capsys.readouterr().out
    assert "已经是空壳" in again and again.rstrip().endswith("这是计划，什么都没改；加 --yes 才执行。")


def test_uninstall_plan_when_nothing_is_installed_says_so(tmp_path, capsys):
    h = make_home(tmp_path)
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--config-method", "yaml"]) == 0
    out = capsys.readouterr().out
    assert "没有找到原委的安装记录" in out and out.rstrip().endswith("什么都没改；加 --yes 才执行。")


# ================================================================== 4　计划里的「更新的文件」要和实际一致
def _vec_args(h, *more):
    return ["setup", "--host", "hermes", "--preset", "recommended", "--vectors", "ollama", "--hermes-home", h, "--hermes-python", "/fake/bin/py", "--config-method", "yaml", "--src-path", SRC] + list(more)


def test_plan_lists_a_file_as_updated_only_when_its_content_would_really_change(tmp_path, capsys):
    from helpers_wizard import FakeHTTP
    from test_vector_setup import mk_sys, ollama_cmds
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http), py=lambda a: (0, "2.0", ""))
    assert cli._setup_main(_vec_args(h, "--yes"), sys_=s) == 0
    capsys.readouterr()
    toml_before = open(os.path.join(h, "yuanwei", "yuanwei.toml"), "rb").read()
    assert b"identity" in toml_before
    assert cli._setup_main(_vec_args(h), sys_=s) == 0                                                         # 再来一遍、选择一样：内容没变
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "更新的文件" not in lines and "新放的文件  （没有）" in lines and "不变的文件" in lines and "yuanwei.toml" in lines.split("不变的文件")[1]
    assert cli._setup_main(_vec_args(h, "--yes"), sys_=s) == 0                                                # 真的再装一遍：没动、没备份
    capsys.readouterr()
    assert open(os.path.join(h, "yuanwei", "yuanwei.toml"), "rb").read() == toml_before
    assert not [n for n in os.listdir(os.path.join(h, "yuanwei")) if "backup" in n]
    args2 = _vec_args(h)
    i = args2.index("--vectors")
    args2[i + 1] = "none"                                                                                     # 改成不开向量：yuanwei.toml 真的会变
    assert cli._setup_main(args2, sys_=s) == 0
    lines2 = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "更新的文件" in lines2 and "yuanwei.toml" in lines2.split("更新的文件")[1].split("不变的文件")[0]


# ================================================================== 5　一条进了候选的回答为什么没交出去（只查不改交付规则）：自造小库，一段 10 条（最后一条是十几句的长回答、前一条是短问题）＋一段 2 条没整理
QUERY5 = "清迈 旅行 行程 准备"
_TOPICS = ["住处", "天气", "证件", "交通", "古城", "夜市", "酒店", "预算"]
_Q9 = "我最近还想学一下吉他。你有什么建议吗？"
_A10 = ("先选一把适合初学者的吉他。民谣吉他和古典吉他各有特点。民谣吉他弦硬一些。古典吉他用尼龙弦。尼龙弦对手指更友好。每天练习二十分钟就够了。先学几个基本和弦。比如 C、G、Am、F。"
        "和弦转换要慢慢练。节拍器可以帮你稳住节奏。再跟着简单的歌弹唱。遇到瓶颈就换一首新歌。不要一开始就追求速度。坚持三个月会有明显进步。")


def _build5(tmp_path, q_rank, ans_rank, answer_unattached=False, answer_without_vectors=False, **cfg):
    """第一段 S1：8 条清迈旅行（每条 7 句，关键词命中）＋ 第 9 条短问题（吉他）＋ 第 10 条十四句的长回答，全挂在同一步同一条主线；第二段 S2：2 条没整理。
    向量名次由测试直接定：问题那句排 q_rank、长回答最好的一句排 ans_rank（向量只取前 50）；这两条的其余句子都排在 50 名之外；关键词一句都不命中它们。"""
    import math
    from helpers import Clock, Scripted
    from helpers2 import IDENT
    from yuanwei import Config, Memory
    from yuanwei import vectors as VX

    class Cl:
        def embed_batch(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
    tmp_path.mkdir(parents=True, exist_ok=True)
    prov = Scripted("thread")
    if answer_unattached:                                                                             # 整理时把长回答的 14 句标成「不记」：它们没有挂到任何一回上
        import json as _j
        from helpers import sids_of

        def organize(request, n):
            sids = sids_of(request["text"])
            body, tail = sids[:-14], sids[-14:]
            return _j.dumps({"新建": {"主线": [{"代号": "T1", "名称": "清迈旅行", "说明": "说明"}], "回": [{"代号": "N1", "主线": "T1", "标题": "这一回", "摘要": "摘要"}]},
                             "去处": [{"句": body, "挂": [{"回": "N1", "把握": "确定"}]}, {"句": tail, "不记": True}], "缺口": [], "改摘要": []}, ensure_ascii=False)
        prov = Scripted(organize)
    mem = Memory.open(str(tmp_path / "m5.sqlite"), "w", config=Config(retrieval="hybrid", index_version=2, **cfg), provider=prov, clock=Clock(), vector_identity=IDENT, embed_client=Cl())
    msgs = [("user" if i % 2 == 0 else "assistant", "".join("关于清迈旅行的%s第%d点：行程准备要提前安排好，并且留出机动时间%d。" % (_TOPICS[i], k, k) for k in range(1, 8))) for i in range(8)]
    msgs += [("user", _Q9), ("assistant", _A10)]
    for i, (r, t) in enumerate(msgs, 1):
        mem.record("h", "S1", "S1-%d" % i, r, t, time="2027-02-01")
    mem.end_session("h", "S1")
    mem.run()
    for i, (r, t) in enumerate([("user", "晚上去吃了烤鱼。味道很好。"), ("assistant", "听起来不错。下次可以试试别的。")], 1):
        mem.record("h", "S2", "S2-%d" % i, r, t, time="2027-02-02")
    anchors = list(mem._reader.snapshot().anchors)
    byc = {}
    for a in anchors:
        byc.setdefault(a.msg_id, []).append(a)
    rows = {m["msg_id"]: m for m in mem.store.list_messages(mem.ns)}
    seq = {m["seq"]: m["msg_id"] for m in rows.values()}
    q_best, ans_best = byc[seq[9]][0], byc[seq[10]][0]
    low = [a for m in (seq[9], seq[10]) for a in byc[m] if a not in (q_best, ans_best)]
    rest = [a for a in anchors if a.msg_id not in (seq[9], seq[10])]
    ranked = rest + low
    for pos, a in sorted([(q_rank, q_best), (ans_rank, ans_best)], key=lambda x: x[0]):
        ranked.insert(pos - 1, a)
    sims = {}
    for rk, a in enumerate(ranked, 1):
        s = 1 - rk * 0.01
        sims[a.text] = [s, math.sqrt(max(0.0, 1 - s * s)), 0.0, 0.0]
    if answer_without_vectors:                                                                        # 长回答的向量还没算好（后台补算没轮到它）：混合检索里它没有向量名次
        for a in byc[seq[10]]:
            sims.pop(a.text, None)
    VX.register(mem.store, IDENT, sims)
    return mem, seq, rows


def _delivered5(mem, seq):
    from yuanwei import multi as MU
    sel = MU.select_multi(mem._reader, QUERY5)
    got = {m["seq"] for m in sel["selected"]}
    return sel, [s for s in range(1, 13) if s in got]


def test_5_the_delivery_rule_is_message_level_hits_plus_one_neighbour_each_plus_same_occasion_pool(tmp_path):
    """交付只有三个来源：①命中消息（关键词有分的句子所在的消息，加上向量前 50 个句子所在的消息，按消息去重）；②每个命中的前一条和后一条消息（同一次会话里，cfg.neighbors＝1）；③命中所在那一回里还没进来的消息（每回最多 10 条，回内再搜后排序）。
    不在这三个来源里的消息，额度再富余也不交。额度够用时（这个小库只有约 1300 估算词元，额度 16000／24000）不会拿掉任何候选。"""
    # A：问题（第 18 名）和长回答（第 20 名）的最好一句都在向量前 50 里：两条都是命中，一起交
    mem, seq, rows = _build5(tmp_path / "a", 18, 20)
    sel, got = _delivered5(mem, seq)
    assert 9 in got and 10 in got and sel["pack"]["最终"]["停在"] is None and sel["pack"]["最终"]["tokens"] < 2000
    assert 11 not in got and 12 not in got                                                           # 第二段 2 条（没整理、也不相关）既不是命中也不挨着命中，不交
    # B：长回答排到 50 名之外、问题还在前 50：长回答不是命中，但它是问题的后一条，作为邻居照样交
    mem, seq, rows = _build5(tmp_path / "b", 18, 70)
    sel, got = _delivered5(mem, seq)
    assert 9 in got and 10 in got
    assert sel["pack"]["最终"]["候选序"].index(seq[10]) > sel["pack"]["最终"]["候选序"].index(seq[9])     # 排在问题后面
    # C：问题和长回答都排到 50 名之外：问题只因为是第 8 条（命中）的后一条才进来（邻居只算一条），长回答是第 8 条的后二条——它不是命中、也不是邻居，
    #    是「同回池」把它补进来的（它和命中挂在同一回里）
    mem, seq, rows = _build5(tmp_path / "c", 60, 70)
    sel, got = _delivered5(mem, seq)
    pk = sel["pack"]
    assert 9 in got and 10 in got and seq[10] not in pk["第一遍"]["选中"] and pk["池大小"]["同回池"] == 1 and pk["最终"]["分段"][1] == ["同回池", 1]
    # D：同样的排名，但长回答的 14 句整理时被标成「不记」（没有挂到任何一回）：邻居够不着、同回池也补不到 → 拿掉。进了候选之前就没了，和额度无关
    mem, seq, rows = _build5(tmp_path / "e", 60, 70, answer_unattached=True)
    sel, got = _delivered5(mem, seq)
    pk = sel["pack"]
    assert 9 in got and 10 not in got and pk["池大小"]["同回池"] == 0 and pk["最终"]["停在"] is None and pk["最终"]["tokens"] < 2000
    # E：长回答不挂任何一回，但它的最好一句在向量前 50 里（第 20 名）：它是命中，照样交（命中是第一来源，不看有没有主线、有没有回）
    mem, seq, rows = _build5(tmp_path / "f", 18, 20, answer_unattached=True)
    sel, got = _delivered5(mem, seq)
    assert 10 in got


def test_5_budget_and_pool_never_drop_a_candidate_in_a_small_library_but_a_tight_budget_stops_at_the_first_that_does_not_fit(tmp_path):
    mem, seq, rows = _build5(tmp_path / "d", 18, 20)
    from yuanwei import multi as MU
    big = MU.select_multi(mem._reader, QUERY5)["pack"]
    assert big["池大小"]["同回池"] == 0 and big["第一遍"]["选中"] == big["最终"]["选中"]                    # 同一回的消息已经全在第一遍里，池没有可补的
    small = MU.select_multi(mem._reader, QUERY5, total_budget=400)["pack"]["最终"]
    assert small["停在"] is not None and len(small["选中"]) < len(big["最终"]["选中"])                    # 额度紧时：按候选次序一条条装，第一条装不下就停（后面的不再看）


def test_5_when_the_answers_vectors_are_not_computed_yet_it_is_not_a_hit_and_the_rest_of_the_chain_decides(tmp_path):
    """真机上向量是后台补算的（status 的 texts.pending）。长回答的向量还没算时它不可能是向量命中；关键词又没命中，就只剩邻居和同回池两条路。
    问题那句在前 50 里（第 18 名）→ 长回答是问题的后一条，作为邻居交；若再没挂回、问题也不在前 50（只是别人的邻居），就拿掉。"""
    mem, seq, rows = _build5(tmp_path / "g", 18, 20, answer_without_vectors=True, vector_fallback="partial")
    sel, got = _delivered5(mem, seq)
    assert seq[10] not in sel["hits"] and 10 in got                                                      # 不是命中，但是问题（命中）的后一条
    mem, seq, rows = _build5(tmp_path / "h", 60, 70, answer_unattached=True, answer_without_vectors=True, vector_fallback="partial")
    sel, got = _delivered5(mem, seq)
    assert seq[10] not in sel["hits"] and 9 in got and 10 not in got
