"""第二段的测试小件（不改第一段的 helpers）：整理好的自造库、假向量客户端、读取层调用计数器。"""
from helpers import Clock, Scripted, make_mem
from yuanwei import Config, Memory
from yuanwei import vectors as VX

IDENT = {"provider": "t", "model": "m", "model_file_sha256": "x" * 64, "runtime_version": "r", "context_length": 2048, "dimension": 4, "normalization": "l2",
         "prefix": "none", "template": "none", "text_processing": "verbatim"}

TEXTS = ["今天去了海边捡贝壳", "晚上吃了烤鱼，味道很好", "下周要开会讨论预算", "周末爬山看日出", "预算批下来了", "新买的相机很好用"]


def organized(tmp_path, texts=None, ws="w", name="m.sqlite", session="S1", dates=None, host="h", **cfg):
    """自造的、已经整理好（原话可见）的库。"""
    mem = make_mem(tmp_path, ws=ws, name=name, provider=Scripted("skip"), **cfg)
    fill(mem, session, texts or TEXTS, dates=dates, host=host)
    return mem


def fill(mem, session, texts, dates=None, host="h", roles=None, date="2027-02-01"):
    for i, t in enumerate(texts, 1):
        d = dates[i - 1] if dates else date
        role = roles[i - 1] if roles else ("user" if i % 2 else "assistant")
        mem.record(host, session, "%s-%d" % (session, i), role, t, time=d)
    mem.end_session(host, session)
    mem.run()


class FakeEmbedClient:
    """假向量客户端：只用来数调用（向量值是按文字算的确定值，不当真实向量）。"""

    def __init__(self):
        self.calls = []

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [[1.0, float(len(t) % 7) + 0.5, 0.25, 0.125] for t in texts]


def hybrid(tmp_path, texts=None, client=None, name="hy.sqlite", **cfg):
    client = client or FakeEmbedClient()
    mem = Memory.open(str(tmp_path / name), "w", config=Config(retrieval="hybrid", index_version=2, **cfg), provider=Scripted("skip"), clock=Clock(), vector_identity=IDENT, embed_client=client)
    fill(mem, "S1", texts or TEXTS)
    snap = mem._reader.snapshot()
    VX.register(mem.store, IDENT, {a.text: [1.0, float(len(a.text) % 5) + 0.1, 0.3, 0.2] for a in snap.anchors})
    return mem, client


class Counter:
    """把读取层的检索入口换成计数器（被调用就记一次）：「拒绝发生在搜索之前」靠它看。"""

    def __init__(self, mem):
        self.calls = []
        rd = mem._reader
        for name in ("select", "select_multi", "snapshot", "recall", "get_messages", "attachments", "_prepare_query_vector", "directory"):
            if hasattr(rd, name):
                setattr(rd, name, self._wrap(name, getattr(rd, name)))
        from yuanwei import retrieve as RT
        orig = RT.Index.candidates
        counter = self

        def cand(index, q):
            counter.calls.append("candidates")
            return orig(index, q)
        RT.Index.candidates = cand
        self._restore = lambda: setattr(RT.Index, "candidates", orig)
        mem.check_and_spawn = self._wrap("check_and_spawn", mem.check_and_spawn)

    def _wrap(self, name, fn):
        def inner(*a, **k):
            self.calls.append(name)
            return fn(*a, **k)
        return inner

    def reset(self):
        self.calls.clear()

    def close(self):
        self._restore()
