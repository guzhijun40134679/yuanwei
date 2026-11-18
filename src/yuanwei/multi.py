"""多路检索选料：原问题加最多 3 个补充关键词，各路分别检索、加权 RRF 合并一次、取材只走一次。不给有效的补充关键词时，直接走 Reader.select（和第一段逐字相同）。"""
from . import pack as PK
from . import queries as Q
from . import relevance as RL
from . import vectors as VX
from .errors import MissingVector


class _CountingClient:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def embed_batch(self, texts):
        self.calls += 1
        return self.inner.embed_batch(texts)


@VX.query_scope()
def select_multi(reader, query, keywords=None, total_budget=None, first_budget=None, cut=None):
    """返回和 Reader.select 同形的字典，另加 routes（{消息编号: {路序号: 名次}}，单路时是 None）和 stats（查询数、向量调用数、权重）。"""
    norm = Q.normalize(query, keywords)                                  # 校验在任何检索、向量调用之前
    inner = reader.embed_client
    counter = _CountingClient(inner) if (reader.cfg.retrieval == "hybrid" and inner is not None) else None
    try:
        if counter is not None:
            reader.embed_client = counter
        if norm["mode"] == "single":
            r = dict(reader.select(query, total_budget=total_budget, first_budget=first_budget, cut=cut))
            r["routes"] = None
            r["stats"] = {"queries": 1, "vector_calls": counter.calls if counter else 0, "weights": ["1"]}
            if r.get("retrieval"):
                r["stats"]["retrieval"] = r["retrieval"]
            return r
        snap = reader.snapshot(cut)
        total, first = reader.cfg.read_budgets(total_budget, first_budget)
        qreason = None
        if reader.cfg.retrieval == "hybrid" and reader.relevance_policy()["status"] == "uncalibrated":
            qreason = "semantic_uncalibrated"
        elif counter is not None:
            if reader.vector_fp is None:
                from .errors import VectorIdentityMismatch
                raise VectorIdentityMismatch("混合模式需要配置向量的编码身份（vector_identity），现在没有")
            if not reader.partial():
                VX.ensure_query_vectors(reader.store, reader.store.identity(reader.vector_fp), norm["queries"], counter)      # 所有查询文字一次批量补齐缺的向量
            elif not reader.blocked_reason():
                try:
                    VX.ensure_query_vectors(reader.store, reader.store.identity(reader.vector_fp), norm["queries"], counter, keep_first=True)
                except VX.ServiceDown as e:
                    qreason = VX.down_reason(e)
                except VX.ProviderError:
                    qreason = "query_vector_unavailable"
        idx = snap.index()
        tables, orders, reasons = [], [], {}
        for text in norm["queries"]:
            t, hits = idx.candidates(text)                               # 缺向量照样报 MissingVector，不能少跑一路还算成功
            tables.append(t)
            orders.append(hits)
            for mid, reason in idx.last_provenance.items():
                reasons[mid] = RL.merge_reason(reasons.get(mid), reason)
        ws = Q.weights(len(norm["extras"]))
        seqs = {m: snap.by_id[m]["seq"] for o in orders for m in o}
        merged, prov = Q.merge_routes(orders, ws, seqs)
        stats = {"queries": len(norm["queries"]), "vector_calls": counter.calls if counter else 0, "weights": [str(w) for w in ws]}
        info = reader._note_retrieval(idx, qreason)
        if info:
            stats["retrieval"] = info
        if not merged:
            return {"cut": snap.cut, "snapshot": snap, "pack": None, "selected": [], "material": None, "hits": [], "table": None, "tables": tables, "routes": {}, "stats": stats}
        tmap, m2o = snap.maps()
        sidx = PK.SessionIndex(snap.rows)
        def pack(total, first):
            return PK.pack_reference(merged, sidx, snap.by_id, reader.mp, tmap, m2o, snap.visible, idx.searcher(), query, first_budget=first, total_budget=total,
                                     nbr=reader.cfg.neighbors, cap=reader.cfg.pool_cap, ruler=reader.cfg.ruler_version, provenance=reasons if idx.provenance_on else None)
        pk = pack(total, first)
        if not pk["最终"]["选中"] and reader.cfg.relaxable_budget(total_budget, total):       # 同 Reader.select：第一条整条装不下，不带 budget 时放宽到 total_budget 再选一次
            total, first = reader.cfg.read_budgets(reader.cfg.total_budget)
            pk = pack(total, first)
        fin = pk["最终"]
        sel = sorted((pk["rows_by_id"][m] for m in fin["选中"]), key=lambda r: r["seq"])
        return {"cut": snap.cut, "snapshot": snap, "pack": pk, "selected": sel, "material": fin["材料"], "hits": merged, "table": None, "tables": tables, "budget_total": total,
                "routes": {m: prov[m] for m in fin["选中"] if m in prov}, "stats": stats}
    finally:
        reader.embed_client = inner
