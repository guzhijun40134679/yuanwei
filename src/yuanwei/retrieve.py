"""检索：截点内的锚点 → 关键词排名［＋可选向量排名］→ 融合名次的候选全表 → 按消息去重的命中序。

关键词模式只用标准库（不导入 NumPy）：候选＝BM25 全部正分锚点。
混合模式：关键词全部正分候选 ＋ 向量池内前 50 个锚点，RRF（k＝60）合并；缺向量直接报 MissingVector，不悄悄退成关键词。
BM25 的统计池和向量候选池是同一个池，而且只含这次截点内可见的锚点。
"""
import types

from . import rank as RK
from .errors import MissingVector, VectorIdentityMismatch
from .util import est_tokens_line  # noqa: F401
from . import vectors as VX


class Anchor:
    """锚点。.doc＝要分词的文字，.num＝order（同分时 num 大的在前）。"""
    __slots__ = ("anchor_id", "order", "text", "msg_id", "seq", "start", "end", "role", "date")

    def __init__(self, anchor_id, order, text, msg_id, seq, start, end, role=None, date=None):
        self.anchor_id, self.order, self.text, self.msg_id, self.seq, self.start, self.end = anchor_id, order, text, msg_id, seq, start, end
        self.role, self.date = role, date

    @property
    def doc(self):
        return self.text

    @property
    def num(self):
        return self.order


def anchors_from_store_rows(rows):
    return [Anchor(r["anchor_id"], r["ord"], r["text"], r["msg_id"], r["seq"], r["start"], r["end"]) for r in rows]


class Index:
    """截点内的索引。embedder＝None 是关键词模式；否则是混合模式（embedder.embed(texts) 返回二维数组，缺向量时抛 MissingVector）。
    embedder.partial 为真（vector_fallback＝partial）：池里有向量的锚点走「关键词＋向量」，没有的只靠关键词；查询向量算不出、没有 NumPy，这一次只用关键词；
    这些都不报错，而是记在 info() 里（读取登记用）。向量齐全时和原来逐字节相同。"""

    def __init__(self, anchors, embedder=None, degraded=None):
        self.pool = sorted(anchors, key=lambda a: a.order)
        self.by_order = {a.order: a for a in self.pool}
        if len(self.by_order) != len(self.pool):
            raise ValueError("池内 order 有重复")
        self.embedder = embedder
        self.partial = bool(getattr(embedder, "partial", False))
        self.degraded = degraded                       # 整次降成关键词的原因（identity_drift 等）：传进来的
        self._qfail = None                             # 这个索引上查询向量算不出的原因
        self._vec_pool = self.pool
        self._vs = None
        if embedder is not None and self.pool and self.degraded is None:
            if self.partial:
                have = embedder.have([a.doc for a in self.pool])
                self._vec_pool = [a for a, h in zip(self.pool, have) if h]
            if self._vec_pool:
                try:
                    self._vs = RK.VectorSource(embedder, types.SimpleNamespace(rows=self._vec_pool))
                except ImportError:
                    if not self.partial:
                        raise
                    self.degraded, self._vec_pool = "numpy_missing", self.pool

    @property
    def hybrid(self):
        return self.embedder is not None

    def rank(self, query):
        """{"kw","vec","merged"}：关键词全部正分候选；向量池内前 50（关键词模式为空）；融合（RRF，同分 order 大的在前）。池为空都是空表。"""
        if not self.pool:
            return {"kw": [], "vec": [], "merged": []}
        kw = RK.bm25_rank(self.pool, query)
        vec = self._vec_rank(query)
        return {"kw": kw, "vec": vec, "merged": RK.rrf_merge(kw, vec)}

    def _vec_rank(self, query):
        if self._vs is None:
            return []
        if not self.partial:
            return self._vs.rank(self.pool, query)
        try:
            return self._vs.rank(self._vec_pool, query)
        except MissingVector:
            self._qfail = "query_vector_unavailable"
            return []

    def info(self, qreason=None):
        """这一次读取用了什么检索：{"used": keyword｜hybrid｜hybrid_partial｜keyword_fallback, "reason", "pool", "with_vector", "missing"}。登记用，不进材料正文。"""
        n = len(self.pool)
        if self.embedder is None and self.degraded is None:
            return {"used": "keyword", "reason": None, "pool": n, "with_vector": 0, "missing": 0}
        reason = self.degraded or qreason or self._qfail
        w = len(self._vec_pool) if (self._vs is not None) else 0
        if reason:
            return {"used": "keyword_fallback", "reason": reason, "pool": n, "with_vector": w, "missing": n - w}
        if n and w == 0:
            return {"used": "keyword_fallback", "reason": "no_vectors_yet", "pool": n, "with_vector": 0, "missing": n}
        if w < n:
            return {"used": "hybrid_partial", "reason": "vectors_missing", "pool": n, "with_vector": w, "missing": n - w}
        return {"used": "hybrid", "reason": None, "pool": n, "with_vector": w, "missing": 0}

    def candidates(self, query):
        """候选全表 [[名次, 锚点编号, 关键词名次, 向量名次]]（按融合名次）和按消息去重的命中序。"""
        r = self.rank(query)
        kw_rank = {n: i for i, (n, _) in enumerate(r["kw"], 1)}
        vec_rank = {n: i for i, (n, _) in enumerate(r["vec"], 1)}
        table, order, seen = [], [], set()
        for pos, (o, _s) in enumerate(r["merged"], 1):
            a = self.by_order[o]
            table.append([pos, a.anchor_id, kw_rank.get(o), vec_rank.get(o)])
            if a.msg_id not in seen:
                seen.add(a.msg_id)
                order.append(a.msg_id)
        return table, order

    def vector_sims(self, query):
        """便条的向量判据用：{消息编号: (该消息所有锚点里与查询向量余弦的最大值, 有没有锚点在向量前 50 里)}。关键词模式返回空字典。缺向量照样报 MissingVector。"""
        if self._vs is None:
            return {}
        vs = self._vs
        try:
            q = vs.query_vec(query)
            top = {o for o, _ in vs.rank(self._vec_pool, query)}
        except MissingVector:
            if not self.partial:
                raise
            self._qfail = "query_vector_unavailable"
            return {}
        out = {}
        for a in self._vec_pool:
            sim = float(vs.doc_vecs[vs.num_index[a.num]] @ q)
            cur = out.get(a.msg_id)
            out[a.msg_id] = (sim if cur is None else max(cur[0], sim), (cur[1] if cur else False) or a.num in top)
        return out

    def searcher(self):
        """回内再搜用的检索器：关键词模式只有 BM25；混合模式用索引已经取好的文档向量和查询向量（partial：没有向量的锚点、算不出的查询向量都跳过，只靠关键词）。"""
        from .pack import ReSearcher
        if self._vs is None:
            return ReSearcher(self.pool)
        vs = self._vs
        if not self.partial:
            return ReSearcher(self.pool, lambda a: vs.doc_vecs[vs.num_index[a.num]], vs.query_vec)

        def qv(text):
            try:
                return vs.query_vec(text)
            except MissingVector:
                self._qfail = "query_vector_unavailable"
                return None
        return ReSearcher(self.pool, lambda a: vs.doc_vecs[vs.num_index[a.num]] if a.num in vs.num_index else None, qv, partial=True)


class CacheEmbedder:
    """从库里的向量缓存取向量（按「实际送进模型的文字」和编码身份认）。缺任何一条就报 MissingVector。"""

    def __init__(self, store, fp):
        self.store, self.fp = store, fp
        if store.identity(fp) is None:
            raise VectorIdentityMismatch("库里没有编码身份 %s… 的向量缓存" % fp[:10])
        self.missing_texts = []
        self.partial = False

    def have(self, texts):
        """每条文字在缓存里有没有向量（partial 用）。"""
        got = self.store.get_vectors(self.fp, {VX.text_key(t) for t in texts})
        return [VX.text_key(t) in got for t in texts]

    def embed(self, texts):
        import numpy as np
        keys = [VX.text_key(t) for t in texts]
        got = self.store.get_vectors(self.fp, set(keys))
        miss = [t for t, k in zip(texts, keys) if k not in got]
        if miss:
            raise MissingVector("缺向量：%d 条文字在缓存里没有（不退回关键词、不退回假向量、不联网），例如 %r" % (len(set(miss)), miss[0][:40]))
        return np.asarray([got[k] for k in keys], dtype=float)


class PartialCacheEmbedder(CacheEmbedder):
    """vector_fallback＝partial：Index 先用 have() 挑出有向量的锚点，只给它们取向量；查询向量没有时 embed 照样报 MissingVector，由 Index 接住、降成关键词。"""

    def __init__(self, store, fp):
        super().__init__(store, fp)
        self.partial = True
