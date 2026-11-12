"""检索的排名件：分词、BM25（统计只在传进来的池上算）、向量源、RRF 合并（取自研究的 甲离线、乙离线）。
基础包只用标准库；向量（NumPy）只在用到时才导入。"""
import math
import re
import unicodedata
from collections import Counter
from fractions import Fraction

BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
VEC_TOPN = 50


def _np():
    import numpy as np
    return np


def tokenize(text):
    """施工单 §3.4：NFKC＋小写；汉字段取相邻两字（单字段取这一字）；[0-9a-z]+ 取整段；其余分隔。"""
    t = unicodedata.normalize("NFKC", text).lower()
    out = []
    for m in re.finditer(r"[一-鿿]+|[0-9a-z]+", t):
        seg = m.group(0)
        if "一" <= seg[0] <= "鿿":
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg)
    return out


def bm25_rank(pool, q_text):
    """返回 [(行号, 分数)]：分数高在前，同分行号大的在前；得分为 0 的行不进候选。
    文档数、平均长度、IDF 只在 pool 上算。"""
    N = len(pool)
    if N == 0:
        return []
    doc_tf = []
    dls = []
    df = Counter()
    for r in pool:
        toks = tokenize(r.doc)
        tf = Counter(toks)
        doc_tf.append(tf)
        dls.append(len(toks))
        for t in tf:
            df[t] += 1
    avgdl = sum(dls) / N
    if avgdl == 0:
        return []
    qterms = []
    seen = set()
    for t in tokenize(q_text):
        if t not in seen:
            seen.add(t)
            qterms.append(t)
    idf = {t: math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) for t in qterms if df[t] > 0}
    scored = []
    for r, tf, dl in zip(pool, doc_tf, dls):
        s = 0.0
        for t in qterms:
            f = tf.get(t, 0)
            if f == 0:
                continue
            s += idf[t] * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        if s > 0:
            scored.append((r.num, s))
    scored.sort(key=lambda x: (-x[1], -x[0]))
    return scored


def l2_normalize(m):
    np = _np()
    m = np.asarray(m, dtype=np.float64)
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n = np.where(n == 0, 1.0, n)
    return m / n


class VectorSource:
    """给一串的全部行预先算向量（每行一次），查询向量按文本缓存。排名只在池内做。"""

    def __init__(self, embedder, strand):
        self.embedder = embedder
        self.num_index = {r.num: i for i, r in enumerate(strand.rows)}
        self.doc_vecs = l2_normalize(embedder.embed([r.doc for r in strand.rows]))
        self._q = {}

    def query_vec(self, text):
        if text not in self._q:
            self._q[text] = l2_normalize(self.embedder.embed([text]))[0]
        return self._q[text]

    def rank(self, pool, q_text):
        """返回池内前 50：[(行号, 相似度)]，相似度高在前，同分行号大的在前。"""
        if not pool:
            return []
        q = self.query_vec(q_text)
        idx = [self.num_index[r.num] for r in pool]
        sims = self.doc_vecs[idx] @ q
        nums = _np().array([r.num for r in pool])
        order = _np().lexsort((-nums, -sims))[:VEC_TOPN]
        return [(int(nums[i]), float(sims[i])) for i in order]


def rrf_merge(kw_list, vec_list):
    """合并名单 RRF：分数＝Σ 1/(60＋名次)，KW 用全部、VEC 用前 50，名次从 1 起；用 Fraction 精确求和；
    分数高在前，同分时行号大的在前。只在一路出现的行只算一项。"""
    score = {}
    for rank, (n, _) in enumerate(kw_list, 1):
        score[n] = score.get(n, Fraction(0)) + Fraction(1, RRF_K + rank)
    for rank, (n, _) in enumerate(vec_list, 1):
        score[n] = score.get(n, Fraction(0)) + Fraction(1, RRF_K + rank)
    return sorted(score.items(), key=lambda x: (-x[1], -x[0]))
