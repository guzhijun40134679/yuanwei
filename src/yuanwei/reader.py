"""读取：一次读取绑定一个截点，同一次读取里原话、挂接、检索池用同一份范围；一次读取全程沿用这次固定的范围，中途不重新取最新状态。

* 截点有两种格式（见 cuts.py）。新格式（默认）：原话按 seq ≤ raw_seq（已入库提交成功的原话，不等整理），整理按 created_seq ≤ org_seq（整理结果和收据一次提交就算，不等步结束）。
  旧格式 {vclock, org_seq}：原话按 visible_at ≤ vclock（所属的步结束时才有），照旧，不换算成新格式。
* 本轮排除：scoped(exclude) 给出一个读取视图，把可信接头绑定的「当前这一轮」消息整条从快照里拿掉（原话、锚点、统计池、向量候选池、前后句、同回补料、按编号取、挂接查询都看不到）。
  只排当前这一轮，同一会话里更早的原话（哪怕文字相同）照常可读。没有轮次上下文的读取（离线接口）不做本轮排除。续读游标里还没交的材料碰上要排除的消息，要求重新查询。
* 历史检索的统计池（BM25）和向量候选池只含截点内的、没被排除的原话，不先全量建索引再过滤。混合模式缺任何一条向量就整次报缺，不退回关键词。
* 出错不冒充空结果：缺向量、绑定不符、坏引用、坏游标、坏截点都返回可识别的错误。
"""
import json
import uuid
import weakref

from . import cuts as CU
from . import encoding as EN
from . import lstate as LS
from . import pack as PK
from . import retrieve as RT
from . import view as LV
from .redact import redact_query
from .errors import BadCursor, BadCut, CursorExcluded, ExcludedThisTurn, MissingVector, NotFound, NotVisible, VectorIdentityMismatch
from .util import est_tokens_line

OPENING = PK.OPENING_PARTIAL


def attach_maps(state):
    """挂接 → 取材要用的两张表（取自研究的 maps_light，口径照读取约定 §3）：
    消息→主线：确定挂回（回的父主线）、总述、回待定；消息→回：只有确定挂回的算；可能挂回、主线待定、日常、不记都不算。返回 (tmap, m2o)。"""
    sent = LV.sentence_rows(state)
    occ_th = LS.occasion_thread(state)
    tmap, m2o = {}, {}
    for a in state["tables"]["attachments"]:
        msg = sent[a["sentence_id"]]["msg_id"]
        k = a["kind"]
        if k == "occasion":
            if a["confidence"] == "确定":
                m2o.setdefault(msg, set()).add(a["target_id"])
                tmap.setdefault(msg, set()).add(occ_th[a["target_id"]])
        elif k in ("thread_summary", "thread_pending"):
            tmap.setdefault(msg, set()).add(a["target_id"])
    return {m: frozenset(v) for m, v in tmap.items()}, m2o


class Snapshot:
    """某个截点上的只读世界（可以去掉一批要排除的消息）。"""

    def __init__(self, reader, cut, exclude=frozenset()):
        self.reader, self.cut = reader, cut
        st = reader.store
        with st.read_tx():                                                  # 原话、锚点、整理状态在同一个读事务里取：有删除的时候不能一半新一半旧
            if CU.is_v2(cut):
                st.check_cut_v2(reader.ns, cut)
                raw_rows = st.raw_messages(reader.ns, cut["raw_seq"])
                anchor_rows = st.raw_anchors(reader.ns, cut["raw_seq"], reader.index_version)
            else:
                st.check_cut_v1(reader.ns, cut)
                raw_rows = st.visible_messages(reader.ns, cut["vclock"])
                anchor_rows = st.visible_anchors(reader.ns, cut["vclock"], reader.index_version)
            self.state = LS.as_of(st.read_state(reader.ns), cut["org_seq"])
        raw_ids = {r["msg_id"] for r in raw_rows}
        if CU.is_v2(cut):                                              # 两个上界必须一致：整理记录引用的原话都在原话上界之内
            gone = {s["msg_id"] for s in self.state["tables"]["sentences"]} - raw_ids
            if gone:
                raise BadCut("截点的两个上界对不上：整理上界 %d 里的整理记录引用了 %d 条不在原话上界 %s 之内的原话" % (cut["org_seq"], len(gone), cut.get("raw_seq")))
        self.exclude = frozenset(exclude or ())
        self.excluded = self.exclude & raw_ids                          # 真的在这个截点里、被拿掉的
        self.rows = []
        for r in raw_rows:
            if r["msg_id"] in self.excluded:
                continue
            date = r["msg_time"][:10] if isinstance(r["msg_time"], str) and len(r["msg_time"]) >= 10 else None
            self.rows.append({"msg_id": r["msg_id"], "session_id": r["session_id"], "seq": r["seq"], "role": r["role"], "date": date, "content": r["content"],
                              "msg_time": r["msg_time"], "fingerprint": r["fingerprint"], "is_long": r["is_long"], "redacted": r.get("redacted", 0)})
        self.by_id = {r["msg_id"]: r for r in self.rows}
        self.visible = set(self.by_id)
        self.anchors = RT.anchors_from_store_rows([a for a in anchor_rows if a["msg_id"] not in self.excluded])
        self._index = None
        self._maps = None

    def maps(self):
        if self._maps is None:
            self._maps = attach_maps(self.state)
        return self._maps

    def index(self):
        if self._index is None:
            self._index = self.reader.make_index(self)
        return self._index


_READERS = weakref.WeakSet()


def purge_all(db_path=None):
    """删除以后：同进程里所有 Reader（同一个库的）清掉缓存的快照。别的进程在下次读时发现删除版本变了会丢弃。"""
    n = 0
    for r in list(_READERS):
        if db_path is None or getattr(r.store, "path", None) == db_path:
            r._snaps.clear()
            n += 1
    return n


class Reader:
    def __init__(self, store, ns, cfg, index_version, vector_fp=None, embed_client=None, mapping=None, clock=None):
        self.store, self.ns, self.cfg, self.index_version = store, ns, cfg, index_version
        self.vector_fp, self.embed_client = vector_fp, embed_client
        self.mp = mapping or EN.load_mapping()
        self.clock = clock
        self._snaps = {}
        self.retrieval_log = []                                     # 混合模式每次选料用了什么检索（读取登记用）；scoped() 的视图共用同一个表
        _READERS.add(self)
        self.exclude = frozenset()                                  # 本轮排除：默认没有（没有轮次上下文的读取不做本轮排除）

    # ---------------------------------------------------------------- 截点和快照
    def current_cut(self):
        """新格式的当下截点：原话上界和整理上界从同一次查询里取。"""
        return self.store.cut_v2(self.ns, self.index_version)

    def scoped(self, exclude):
        """一个排除了 exclude（消息编号集合）的读取视图；和原来的 Reader 共用库连接和快照缓存（缓存键带排除集合，不串）。"""
        r = Reader(self.store, self.ns, self.cfg, self.index_version, self.vector_fp, self.embed_client, self.mp, self.clock)
        r.exclude = frozenset(exclude or ())
        r._snaps = self._snaps
        r.retrieval_log = self.retrieval_log
        return r

    def snapshot(self, cut=None, exclude=None):
        cut = cut or self.current_cut()
        CU.check_shape(cut)
        ex = self.exclude if exclude is None else frozenset(exclude)
        v2 = CU.is_v2(cut)
        key = (CU.FORMAT if v2 else 1, cut["raw_seq"] if v2 else cut["vclock"], cut["org_seq"], cut["index_version"] if v2 else self.index_version, self.ns, self.index_version,
               self.cfg.retrieval, self.vector_fp, self.store.vector_count(self.vector_fp) if self.vector_fp else 0, ex, self.store.del_ver(self.ns), self.blocked_reason())
        if key not in self._snaps:
            if len(self._snaps) > 4:
                self._snaps.clear()
            self._snaps[key] = Snapshot(self, cut, ex)
        return self._snaps[key]

    def _check_cursor_exclusion(self, d, pos):
        """续读游标里还没交的材料（从 pos 起的各条）碰上现在要排除的消息：旧游标不能绕过新的排除规则，要求重新查询，不悄悄重算剩下的页。"""
        if not self.exclude:
            return
        bad = [ch["msg_id"] for ch in d["chunks"][pos:] if ch["msg_id"] in self.exclude]
        if bad:
            raise CursorExcluded("续读游标里还没交的材料有 %d 条属于现在这一轮，不能继续用这个游标；请重新查询" % len(bad))

    def partial(self):
        return self.cfg.retrieval == "hybrid" and getattr(self.cfg, "vector_fallback", "strict") == "partial"

    def blocked_reason(self):
        """partial 模式下，整次读取都只用关键词的原因：向量服务的编码身份和配置里的对不上（identity_drift，补算的轻探发现的）。没有就是 None。"""
        if not self.partial() or not self.vector_fp:
            return None
        try:
            v = self.store.meta_get("vec_service")
            d = json.loads(v) if v else None
        except (ValueError, TypeError):
            return None
        if isinstance(d, dict) and d.get("state") == "drift" and d.get("fp") == self.vector_fp:
            return "identity_drift"
        return None

    def embedder(self):
        if self.cfg.retrieval != "hybrid":
            return None
        if not self.vector_fp:
            raise VectorIdentityMismatch("混合模式需要配置向量的编码身份（vector_identity），现在没有")
        if self.partial():
            if self.blocked_reason():
                return None
            return RT.PartialCacheEmbedder(self.store, self.vector_fp)
        return RT.CacheEmbedder(self.store, self.vector_fp)

    def make_index(self, snap):
        """快照的索引：partial 模式下被拦住（identity_drift）就整次降成关键词，并带上原因。"""
        return RT.Index(snap.anchors, self.embedder(), degraded=self.blocked_reason() if self.partial() else None)

    def _prepare_query_vector(self, query):
        """给查询算向量（缓存里没有才去连服务）。strict：出错照旧往外抛。partial：连不上、被拒、回应坏了都不报错，返回原因（service_down／query_vector_unavailable），这一次只用关键词。"""
        if self.cfg.retrieval == "hybrid" and self.embed_client is not None:
            from . import vectors as VX
            if not self.partial():
                VX.ensure_vectors(self.store, self.store.identity(self.vector_fp), [query], self.embed_client)
                return None
            if self.blocked_reason():
                return None
            try:
                VX.ensure_vectors(self.store, self.store.identity(self.vector_fp), [query], self.embed_client, keep_first=True)
            except VX.ServiceDown as e:
                return VX.down_reason(e)
            except VX.ProviderError:
                return "query_vector_unavailable"
        return None

    def _note_retrieval(self, idx, qreason):
        if self.cfg.retrieval != "hybrid":
            return None
        info = idx.info(qreason)
        self.retrieval_log.append(info)
        return info

    # ---------------------------------------------------------------- 参照档选料
    def select(self, query, total_budget=None, first_budget=None, cut=None):
        """参照档选料。返回 {"cut","snapshot","pack","selected"（按时间排的消息行）,"material"（编码后）}。没有可见原话或没有命中就是空选。"""
        query = redact_query(query)
        snap = self.snapshot(cut)
        total = min(total_budget or self.cfg.total_budget, self.cfg.total_budget)
        first = min(first_budget or self.cfg.first_budget, total)
        qreason = self._prepare_query_vector(query)
        idx = snap.index()
        table, hits = idx.candidates(query)
        info = self._note_retrieval(idx, qreason)
        if not hits:
            return dict({"cut": snap.cut, "snapshot": snap, "pack": None, "selected": [], "material": None, "hits": hits, "table": table}, **({"retrieval": info} if info else {}))
        tmap, m2o = snap.maps()
        sidx = PK.SessionIndex(snap.rows)
        pk = PK.pack_reference(hits, sidx, snap.by_id, self.mp, tmap, m2o, snap.visible, idx.searcher(), query, first_budget=first, total_budget=total,
                               nbr=self.cfg.neighbors, cap=self.cfg.pool_cap)
        fin = pk["最终"]
        sel = sorted((snap.by_id[m] for m in fin["选中"]), key=lambda r: r["seq"])
        return dict({"cut": snap.cut, "snapshot": snap, "pack": pk, "selected": sel, "material": fin["材料"], "hits": hits, "table": table}, **({"retrieval": info} if info else {}))

    def select_multi(self, query, keywords=None, total_budget=None, first_budget=None, cut=None):
        """参照档选料加可选关键词（第二段）。没有有效的补充关键词时就是 select 本身。见 multi.select_multi。"""
        from . import multi
        if isinstance(query, str):
            query = redact_query(query)
        if isinstance(keywords, (list, tuple)):
            keywords = [redact_query(k) if isinstance(k, str) else k for k in keywords]
        return multi.select_multi(self, query, keywords, total_budget, first_budget, cut)

    def directory(self, cut=None, limit=2000):
        """主线目录（第二段，「只给目录」模式用）。见 directory.build_directory。没有主线、或上限小到连开头和结尾都放不下时返回 None。"""
        from . import directory
        return directory.build_directory(self, cut, limit)

    # ---------------------------------------------------------------- 分页渲染
    def _render_page(self, chunks, pos, off, last_key, first_page, limit_chars):
        """从 (pos, off) 起装一页：按消息装，一条消息放不下整条就按码点切。返回 (text, delivered, new_pos, new_off, new_last_key)。"""
        lines, delivered = [], []
        used = 0
        if first_page:
            lines += [OPENING, "【原文】"]
            used += len(OPENING) + len("【原文】") + 2
        i, o = pos, off
        while i < len(chunks):
            ch = chunks[i]
            enc = self.mp.encode(ch["content"])
            key = (ch["session_id"], ch["date"] or PK.NONE_DATE)
            head = []
            if key != last_key:
                head.append("会话 %s｜%s" % key)
            prefix = "〔%s｜%s%s〕" % (PK.ROLE_CN[ch["role"]], ch["msg_id"], ("｜挡掉 %d 处" % ch["redacted"]) if ch.get("redacted") else "")
            room = limit_chars - used - sum(len(h) + 1 for h in head) - len(prefix) - 1
            rest = enc[o:]
            if room <= 0 and delivered:
                break
            take = rest if len(rest) <= room else rest[:max(1, room)]
            lines += head
            cont = o > 0
            lines.append(prefix + ("（续）" if cont else "") + take)
            used += sum(len(h) + 1 for h in head) + len(prefix) + len(take) + 1 + (3 if cont else 0)
            delivered.append({"msg_id": ch["msg_id"], "start": o, "end": o + len(take)})
            last_key = key
            if len(take) < len(rest):
                o += len(take)
                break
            i, o = i + 1, 0
            if used >= limit_chars:
                break
        return "\n".join(lines), delivered, i, o, last_key

    @staticmethod
    def _split_token(token):
        try:
            cid, pos, off = token.split(":")
            return cid, int(pos), int(off)
        except (ValueError, AttributeError):
            raise BadCursor("续读游标的格式不对：%r" % (token,))

    def recall(self, query, budget=None, cursor=None, cut=None, limit_chars=None):
        """查记忆（参照档）。第一次调用定下这份结果（工作区、截点、索引版本、查询和取材配置、选中的有序原话），存在库里不再改；
        游标＝「结果编号:下一页的起点」，用同一个游标重取得到同一页，不会换成最新截点。只记本页实际交出的。"""
        limit = limit_chars or self.cfg.recall_page_chars
        if cursor is not None:
            cid, pos, off = self._split_token(cursor)
            cur = self.store.get_cursor(cid)
            if cur is None or cur["ns"] != self.ns or cur["kind"] != "recall":
                raise BadCursor("续读游标不存在，或不属于这个工作区的 recall：%s" % cursor)
            d = cur["data"]
            if pos >= len(d["chunks"]) or off > len(d["chunks"][pos]["content"]):
                raise BadCursor("续读游标指到的位置不在这份结果里")
            self._check_cursor_exclusion(d, pos)
        else:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("问题不能是空的")
            query = redact_query(query)
            sel = self.select(query, total_budget=budget, cut=cut)
            snap = sel["snapshot"]
            chunks = [_chunk(r) for r in sel["selected"]]
            d = {"query": query, "cut": snap.cut, "chunks": chunks, "delivered": {}, "budget": budget, "exclude": sorted(snap.excluded),
                 "config": {"retrieval": self.cfg.retrieval, "first_budget": self.cfg.first_budget, "total_budget": self.cfg.total_budget, "pool_cap": self.cfg.pool_cap,
                            "neighbors": self.cfg.neighbors, "index_version": self.index_version}}
            cid = uuid.uuid4().hex[:16]
            pos, off = 0, 0
            if not chunks:
                self.store.put_cursor(cid, self.ns, "recall", self.clock() if self.clock else None, d)
                return {"cursor": None, "has_more": False, "text": "（没有找到相关记录）", "delivered": [], "cut": snap.cut, "selected_total": 0,
                        "stop_reason": "no_hits", "config": d["config"]}
            self.store.put_cursor(cid, self.ns, "recall", self.clock() if self.clock else None, d)
        first_page = (pos == 0 and off == 0)
        prev = d["chunks"][pos - 1 if off == 0 else pos] if (pos > 0 or off > 0) else None
        last_key = (prev["session_id"], prev["date"] or PK.NONE_DATE) if prev else None
        text, delivered, npos, noff, _lk = self._render_page(d["chunks"], pos, off, last_key, first_page, limit)
        d["delivered"]["%d:%d" % (pos, off)] = delivered            # 同一页重取覆盖同一个键，不重复登记
        self.store.update_cursor(cid, d)
        more = npos < len(d["chunks"])
        return {"cursor": "%s:%d:%d" % (cid, npos, noff) if more else None, "has_more": more, "text": text, "delivered": delivered, "cut": d["cut"],
                "selected_total": len(d["chunks"]), "config": d["config"]}

    # ---------------------------------------------------------------- get_messages
    def get_messages(self, ids=None, before=0, after=0, cursor=None, cut=None, encode=True, page_tokens=None):
        """按编号取原话，每页最多 get_messages_page_tokens 估算词元；放不下的条按码点切、给截断标志和游标。游标＝「结果编号:下一页的起点」，绑定消息身份、原话版本（指纹）、截点、码点范围。"""
        limit = page_tokens or self.cfg.get_messages_page_tokens          # page_tokens：第二段按宿主单位核最终返回时缩小一页用；不给就是配置里的值
        if cursor is not None:
            cid, pos, off = self._split_token(cursor)
            cur = self.store.get_cursor(cid)
            if cur is None or cur["ns"] != self.ns or cur["kind"] != "get_messages":
                raise BadCursor("续读游标不存在，或不属于这个工作区的 get_messages：%s" % cursor)
            d = cur["data"]
            if pos >= len(d["chunks"]) or off > len(d["chunks"][pos]["content"]):
                raise BadCursor("续读游标指到的位置不在这份结果里")
            self._check_cursor_exclusion(d, pos)
            for ch in d["chunks"]:                                   # 原话变了（指纹不同）就拒绝
                r = self.store.message_row(self.ns, ch["msg_id"])
                if r is None or r["fingerprint"] != ch["fingerprint"]:
                    raise BadCursor("游标绑定的原话 %s 的版本变了或不存在" % ch["msg_id"])
        else:
            if not ids:
                raise ValueError("要给消息编号")
            snap = self.snapshot(cut)
            chosen = []
            for mid in ids:
                row = self.store.message_row(self.ns, mid)
                if row is None:
                    raise NotFound("没有这条消息：%r" % (mid,))
                if mid in snap.excluded:
                    raise ExcludedThisTurn("消息 %r 属于当前这一轮，不作为历史交出" % (mid,))
                if mid not in snap.visible:
                    raise NotVisible("消息 %r 不在这次读取的截点里（%s），暂时不交" % (mid, "比截点的原话上界更晚入库" if CU.is_v2(snap.cut) else "所属的步在截点之后才结束"))
                chosen.append(mid)
            sidx = PK.SessionIndex(snap.rows)
            want = []
            for mid in chosen:
                sid, i = sidx.pos[mid]
                lst = sidx.by_session[sid]
                for j in range(max(0, i - before), min(len(lst), i + after + 1)):
                    if lst[j] not in want:
                        want.append(lst[j])
            want.sort(key=lambda m: snap.by_id[m]["seq"])
            chunks = [_chunk(snap.by_id[m]) for m in want]
            d = {"cut": snap.cut, "chunks": chunks, "exclude": sorted(snap.excluded)}
            cid = uuid.uuid4().hex[:16]
            pos, off = 0, 0
            self.store.put_cursor(cid, self.ns, "get_messages", self.clock() if self.clock else None, d)
        out, used, i, o = [], 0, pos, off
        while i < len(d["chunks"]):
            ch = d["chunks"][i]
            text = self.mp.encode(ch["content"]) if encode else ch["content"]
            rest = text[o:]
            head = "〔%s｜%s%s〕" % (PK.ROLE_CN[ch["role"]], ch["msg_id"], ("｜挡掉 %d 处" % ch["redacted"]) if ch.get("redacted") else "")
            cost = est_tokens_line(head + rest)
            if used + cost <= limit:
                out.append(_mark({"msg_id": ch["msg_id"], "role": ch["role"], "session_id": ch["session_id"], "date": ch["date"], "text": rest, "range": [o, len(text)], "truncated": False}, ch))
                used += cost
                i, o = i + 1, 0
                continue
            room = limit - used
            if room <= est_tokens_line(head):
                if out:
                    break
                room = est_tokens_line(head) + 1
            lo, hi = 1, len(rest)
            while lo < hi:                                         # 二分：最长的、估算词元不超过 room 的前缀（至少一个字符，免得死循环）
                mid = (lo + hi + 1) // 2
                if est_tokens_line(head + rest[:mid]) <= room:
                    lo = mid
                else:
                    hi = mid - 1
            n = lo
            out.append(_mark({"msg_id": ch["msg_id"], "role": ch["role"], "session_id": ch["session_id"], "date": ch["date"], "text": rest[:n], "range": [o, o + n], "truncated": True}, ch))
            o += n
            used += est_tokens_line(head + rest[:n])
            break
        more = i < len(d["chunks"])
        return {"messages": out, "truncated": more, "cursor": "%s:%d:%d" % (cid, i, o) if more else None, "cut": d["cut"], "encoded": bool(encode)}

    # ---------------------------------------------------------------- attachments
    def attachments(self, message_id, cut=None):
        snap = self.snapshot(cut)
        row = self.store.message_row(self.ns, message_id)
        if row is None:
            raise NotFound("没有这条消息：%r" % (message_id,))
        if message_id in snap.excluded:
            raise ExcludedThisTurn("消息 %r 属于当前这一轮，不作为历史交出" % (message_id,))
        if message_id not in snap.visible:
            raise NotVisible("消息 %r 不在这次读取的截点里（%s）" % (message_id, "比截点的原话上界更晚入库" if CU.is_v2(snap.cut) else "所属的步在截点之后才结束"))
        st = snap.state
        sents = [s for s in st["tables"]["sentences"] if s["msg_id"] == message_id]
        by_sent = {}
        for a in st["tables"]["attachments"]:
            by_sent.setdefault(a["sentence_id"], []).append(a)
        nm, tt = LV.names(st), LV.titles(st)
        occ_th = LS.occasion_thread(st)
        out = []
        for s in sorted(sents, key=lambda s: s["start"]):
            atts = []
            for a in by_sent.get(s["sentence_id"], []):
                d = {"kind": a["kind"], "target": a["target_id"], "confidence": a["confidence"], "candidates": list(a["candidates"]), "reason": a["reason"]}
                if a["kind"] == "occasion":
                    d["thread"] = occ_th.get(a["target_id"])
                    d["thread_name"] = nm.get(d["thread"])
                    d["occasion_title"] = tt.get(a["target_id"])
                elif a["kind"] in ("thread_summary", "thread_pending"):
                    d["thread_name"] = nm.get(a["target_id"])
                atts.append(d)
            out.append({"sentence_id": s["sentence_id"], "range": [s["start"], s["end"]], "text": s["text"], "role": s["role"], "attachments": atts})
        org = message_org_state(st, row)                      # 步的状态也按这个截点说，不读实时的 get_step
        step = {"step_id": org["step_id"], "state": org["state"]} if org["step_id"] else None       # 这一步在这个截点里已经有收据才算数；没有就不说它（封步本身不是截点里的事实）
        return {"message_id": message_id, "role": row["role"], "session_id": row["session_id"], "message_fingerprint": row["fingerprint"], "step": step,
                "cut": snap.cut, "sentences": out, "organization": org, "note": None if org["state"] == "processed" else org["text"]}


def _chunk(r):
    ch = {"msg_id": r["msg_id"], "session_id": r["session_id"], "date": r["date"], "role": r["role"], "content": r["content"], "fingerprint": r["fingerprint"]}
    if r.get("redacted"):                                                  # 挡掉过密钥的原话：读取输出里标「挡掉 N 处」；没挡过的字节不变
        ch["redacted"] = r["redacted"]
    return ch


def _mark(out, ch):
    if ch.get("redacted"):
        out["redacted"] = ch["redacted"]
    return out


ORG_TEXTS = {
    "no_receipt_in_cut": "本截点尚无整理结果（所属的步的收据还没进这个截点）",
    "processed": "已处理",
    "processed_no_attach": "已处理，本条无挂接",
    "abandoned": "所属的步在本截点已整理放弃，没有挂接",
    "skipped_long": "超长跳过：这一步的消息都是超长条，不送去整理，只存全文和检索",
    "long_not_organized": "超长条不整理，只存全文和检索",
}


def message_org_state(state, row):
    """一条消息在某个截点上的整理状态（只看这个截点里的收据和挂接，不读实时的步状态）。
    状态：no_receipt_in_cut（收据还没进截点）、processed（已处理，有挂接）、processed_no_attach（已处理，本条无挂接）、
    abandoned（整理放弃）、skipped_long（整步超长跳过）、long_not_organized（超长条不整理）。超长跳过和整理放弃分开说。"""
    tables = state["tables"]
    sid = row.get("step_id")
    rc = None
    if sid:
        for r in tables["receipts"]:
            if r.get("step_id") == sid and (r["kind"] == "step" or r.get("op") in ("abandon_step", "skip_step")):
                rc = r
                break
    if rc is not None and rc["kind"] == "op" and rc.get("op") == "skip_step":
        code = "skipped_long"
    elif row.get("is_long"):
        code = "long_not_organized"
    elif rc is None:
        code = "no_receipt_in_cut"
    elif rc["kind"] == "op":
        code = "abandoned"
    else:
        sids = {s["sentence_id"] for s in tables["sentences"] if s["msg_id"] == row["msg_id"]}
        code = "processed" if any(a["sentence_id"] in sids for a in tables["attachments"]) else "processed_no_attach"
    return {"state": code, "text": ORG_TEXTS[code], "step_id": rc["step_id"] if rc else None, "receipt_commit_seq": rc["commit_seq"] if rc else None}
