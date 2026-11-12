"""V4 纯关键词模式：全局和回内都确实是关键词；明确词命中、零命中、长消息尾部的词命中、工作区隔离都对；混合模式缺向量报错；不能用假向量凑过。"""
import os
import subprocess
import sys

import pytest

from helpers import Clock, Scripted, make_mem, say
from yuanwei import Config, Memory, rank as RK, retrieve as RT
from yuanwei.errors import MissingVector, NotFound, VectorIdentityMismatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def organize(mem, session, texts, host="h", date="2027-02-01"):
    for i, t in enumerate(texts, 1):
        mem.record(host, session, "%s-%d" % (session, i), "user" if i % 2 else "assistant", t, time=date)
    mem.end_session(host, session)
    mem.run()


def test_keyword_mode_never_imports_numpy_and_uses_bm25_only(tmp_path):
    """在干净进程里走一遍（记、整理、查、回内再搜），NumPy 不能被导入；候选全表的向量名次全是空。"""
    script = f"""
import sys
sys.path.insert(0, {os.path.join(ROOT, 'src')!r}); sys.path.insert(0, {os.path.join(ROOT, 'tests')!r})
from helpers import *
mem = make_mem(__import__('pathlib').Path({str(tmp_path)!r}), provider=Scripted('thread'))
say(mem, 'S1', 8, prefix='菠萝')
mem.end_session('h', 'S1'); mem.run()
out = mem.recall('菠萝的编号3')
assert out['delivered'], out
snap = mem._reader.snapshot()
table, hits = snap.index().candidates('菠萝的编号3')
assert table and all(r[3] is None for r in table), '向量名次应当全是空'
assert not snap.index().hybrid and not snap.index().searcher().hybrid
assert 'numpy' not in sys.modules, 'numpy 被导入了'
print('OK')
"""
    r = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_explicit_word_hits_and_zero_hit(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    organize(mem, "S1", ["今天去了海边捡贝壳", "海边风很大，贝壳很漂亮", "晚上吃了烤鱼", "烤鱼很好吃", "明天要开会讨论预算", "预算已经批下来了"])
    out = mem.recall("贝壳")
    ids = [d["msg_id"] for d in out["delivered"]]
    contents = [mem.store.message_row("w", i)["content"] for i in ids]
    assert any("贝壳" in c for c in contents) and not any("预算" in c for c in contents[:1])      # 明确词命中；材料按时间排，不按名次排
    assert out["config"]["retrieval"] == "keyword"
    zero = mem.recall("zzqq 无关词汇 xyzxyz")
    assert zero["delivered"] == [] and zero["stop_reason"] == "no_hits" and "没有找到相关记录" in zero["text"]


def test_tail_word_of_a_long_message_is_found(tmp_path):
    """长消息尾部的词也能命中（索引覆盖全文，不是只索引开头）。"""
    body = "".join("这是第%d段铺垫的话，讲的是日常琐事。" % i for i in range(1, 51))
    tail = "最后提一句：保险柜密码改成了蓝鲸座。"
    assert len(body) + len(tail) < 1500
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    organize(mem, "S1", ["你好", body + tail, "收到", "还有别的吗"])
    out = mem.recall("保险柜密码蓝鲸座")
    assert any("蓝鲸座" in mem.store.message_row("w", d["msg_id"])["content"] for d in out["delivered"])
    snap = mem._reader.snapshot()
    assert any("蓝鲸座" in a.text for a in snap.anchors)


def test_workspaces_are_isolated(tmp_path):
    ma = make_mem(tmp_path, ws="甲", provider=Scripted("skip"), name="shared.sqlite")
    mb = make_mem(tmp_path, ws="乙", provider=Scripted("skip"), name="shared.sqlite")
    organize(ma, "SA", ["甲工作区的秘密：口令是紫罗兰", "收到", "再说一次口令紫罗兰", "好的"])
    organize(mb, "S1", ["乙工作区只聊天气", "天气很好", "明天下雨吗", "可能会下"])
    assert ma.recall("紫罗兰")["delivered"]
    assert mb.recall("紫罗兰")["delivered"] == []
    a_id = ma.recall("紫罗兰")["delivered"][0]["msg_id"]
    with pytest.raises(NotFound):
        mb.get_messages([a_id])
    with pytest.raises(NotFound):
        mb.attachments(a_id)
    # 同一个消息编号在两个工作区里各指各的原话（编号只在工作区内有意义）
    ma.record("h", "SX", "same", "user", "甲里的同名消息")
    mb.record("h", "SX", "same", "user", "乙里的同名消息")
    ma.end_session("h", "SX"); mb.end_session("h", "SX"); ma.run(); mb.run()
    mid = ma.store.message_by_host_key("甲", "h", "SX", "", "same")["msg_id"]
    assert ma.get_messages([mid])["messages"][0]["text"] == "甲里的同名消息"
    assert mb.get_messages([mid])["messages"][0]["text"] == "乙里的同名消息"


def test_hybrid_without_vectors_errors_instead_of_falling_back(tmp_path):
    ident = {"provider": "t", "model": "m", "model_file_sha256": "x" * 64, "runtime_version": "r", "context_length": 2048, "dimension": 4, "normalization": "l2",
             "prefix": "none", "template": "none", "text_processing": "verbatim"}
    mem = Memory.open(str(tmp_path / "h.sqlite"), "w", config=Config(retrieval="hybrid", index_version=2), provider=Scripted("skip"), clock=Clock(), vector_identity=ident)
    say(mem, "S1", 4, prefix="荔枝")
    mem.end_session("h", "S1")
    mem.run()
    with pytest.raises(MissingVector):
        mem.recall("荔枝")                                           # 库里一条向量都没有：报错，不退成关键词
    # 配了身份、给了部分向量也一样：只要缺一条就报
    from yuanwei import vectors as VX
    snap = mem._reader.snapshot()
    VX.register(mem.store, ident, {snap.anchors[0].text: [1.0, 0.0, 0.0, 0.0]})
    with pytest.raises(MissingVector):
        mem.recall("荔枝")
    st = mem.status()
    assert st["retrieval"]["mode"] == "hybrid" and st["retrieval"]["vectors"] == 1


def test_hybrid_without_identity_errors(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), retrieval="hybrid")
    say(mem, "S1", 4)
    mem.end_session("h", "S1")
    mem.run()
    with pytest.raises(VectorIdentityMismatch):
        mem.recall("编号")


def test_hybrid_works_with_real_cached_vectors_and_differs_from_keyword(tmp_path):
    """混合模式有向量时确实走向量：给一条与查询没有任何共同词的消息配上贴近查询的向量，它会进候选（关键词模式里永远不会）。"""
    np = pytest.importorskip("numpy")
    from yuanwei import vectors as VX
    ident = {"provider": "t", "model": "m", "model_file_sha256": "x" * 64, "runtime_version": "r", "context_length": 2048, "dimension": 4, "normalization": "l2",
             "prefix": "none", "template": "none", "text_processing": "verbatim"}
    texts = ["今天去了海边捡贝壳", "大海边上的沙滩很漂亮", "晚上吃了烤鱼", "明天要开会讨论预算"]
    mem = Memory.open(str(tmp_path / "hy.sqlite"), "w", config=Config(retrieval="hybrid", index_version=2), provider=Scripted("skip"), clock=Clock(), vector_identity=ident)
    organize(mem, "S1", texts)
    vecs = {texts[0]: [1, 0, 0, 0], texts[1]: [0.9, 0.1, 0, 0], texts[2]: [0, 1, 0, 0], texts[3]: [0, 0, 1, 0], "海滩捡贝壳": [1, 0.05, 0, 0]}
    VX.register(mem.store, ident, vecs)
    r = mem.recall("海滩捡贝壳")
    ids = [d["msg_id"] for d in r["delivered"]]
    contents = [mem.store.message_row("w", i)["content"] for i in ids]
    assert texts[1] in contents                                    # 与查询没有共同词，只靠向量进来
    kw_only = RT.Index(mem._reader.snapshot().anchors).candidates("海滩捡贝壳")[1]
    assert mem.store.message_row("w", [i for i in ids if mem.store.message_row("w", i)["content"] == texts[1]][0])["msg_id"] not in kw_only


def test_no_fake_embedder_in_package():
    """不能用假向量凑过：包里没有任何「造向量」的东西（哈希假向量、随机向量）。"""
    src = os.path.join(ROOT, "src", "yuanwei")
    bad = []
    for fn in os.listdir(src):
        if fn.endswith(".py"):
            t = open(os.path.join(src, fn), encoding="utf-8").read()
            for w in ("FakeEmbedder", "fake_embed", "random.", "hashlib.sha256(tok", "np.random"):
                if w in t:
                    bad.append((fn, w))
    assert not bad, bad


def test_tokenize_and_bm25_basics():
    assert RK.tokenize("海边贝壳 SQLite") == ["海边", "边贝", "贝壳", "sqlite"]

    class D:
        def __init__(self, n, doc):
            self.num, self.doc = n, doc
    pool = [D(1, "今天吃了烤鱼"), D(2, "烤鱼很好吃"), D(3, "明天开会")]
    r = RK.bm25_rank(pool, "烤鱼")
    assert [n for n, _ in r] == [2, 1] or [n for n, _ in r] == [1, 2]
    assert 3 not in [n for n, _ in r]
