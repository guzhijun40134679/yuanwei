"""跨区 recall：来源本地保存原话，聚合游标只引用本地游标；实际页定好后才登记交付。"""
from contextlib import ExitStack
from contextvars import ContextVar
import uuid

from . import hosts as H, ranked_pages as RP, vectors as VX, cuts as CU
from .access import Access
from .errors import BadCursor, HistoryDeleted
from .tools import ToolSurface, ToolResult


DELIVERY_TICKETS = ContextVar("yuanwei_cross_delivery_tickets", default=None)


class SourceAccess(Access):
    """借当前窗口的轮次许可，日志及删除版本始终属于材料来源。"""
    def __init__(self, memory, owner):
        super().__init__(memory)
        self.owner = owner

    def current_turn(self, host, session_id, branch=""):
        return self.owner.current_turn(host, session_id, branch)

    def _evaluate(self, ident):
        reason, current = self.owner._evaluate(ident)
        if reason is not None:
            return reason, current
        try:
            if self.mem._deletion_blocked:
                return "permission_unknown", current
            rows = self.store.read_settings(self.ns)
            for row in rows:
                if not row["allowed"] and row["scope"] == "workspace":
                    return "workspace_setting", current
                if not row["allowed"] and row["scope"] == "session" and row["key"] in (ident.session_id, self.session_key(ident.host, ident.session_id)):
                    return "session_setting", current
        except Exception:
            return "permission_unknown", current
        return None, current


def _render(adapter, sources, data, items, page, cursor):
    sections, local, delivery = [], {}, {}
    for source_index, source in enumerate(sources):
        chunks = [source["data"]["chunks"][i] for s, i in items if s == source_index]
        if not chunks:
            continue
        material = RP.material(source["reader"], chunks, False) + "\n——yuanwei｜截点 %s" % CU.label(source["data"]["cut"])
        local[source_index] = material
        delivery[source_index] = RP.spans(source["reader"], chunks)
        sections.append("〔工作区：%s〕\n%s" % (data["sources"][source_index]["label"], material))
    head = "〔读取范围：%s〕" % "、".join(data["read_labels"])
    if page == 0:
        head += "\n" + RP.NOTICE
    more = "还有下一页：再调用 recall，只给 cursor=%s" % cursor if cursor else "已是最后一页"
    text = head + "\n" + "\n".join(sections) + "\n\n——yuanwei｜本页 %d 条消息｜%s\n%s" % (sum(len(s) for s in delivery.values()), more, RP.BOUNDARY)
    if page == 0 and data.get("first_suffix"):
        text += "\n" + data["first_suffix"]
    return text, local, delivery


def _check_sources(sources, data, number):
    remaining = [item for page in data["pages"][number:] for item in page]
    for index, source in enumerate(sources):
        reader, local = source["reader"], source["data"]
        RP.validate(reader, local, path=source["surface"].profile.paths["mcp_return"])
        reader._check_relevance_cursor(local)
        reader.snapshot(local["cut"])
        reader._check_cursor_exclusion({"chunks": [local["chunks"][i] for s, i in remaining if s == index]}, 0)
        if source["surface"].acc.store.del_ver(source["surface"].acc.ns) != source["ticket"].del_ver:
            raise HistoryDeleted("历史已删除，请重新查询")
        denied = source["surface"].acc.check_permission(source["ticket"].ident)
        if denied:
            return source["surface"]._refused(denied)
    return None


def _failed(tickets, reason):
    for access, ticket in tickets:
        access.event(ticket, "injection_failed", {"reason": reason})


def execute(adapter, args, keys):
    """调用方已冻结读取范围并通过窗口permit；这里持来源锁到最终页登记结束。"""
    delivery_tickets = DELIVERY_TICKETS.get()
    owner = adapter._surfaces[adapter.ws["key"]]
    identity = owner.bind()
    sources = []
    with ExitStack() as stack, VX.query_scope():
        originals = []
        for key in keys:
            original = adapter._surfaces.get(key) or adapter._surface_for(key)
            if original is not None:
                originals.append((key, original))
        for _key, original in sorted(originals, key=lambda pair: pair[0]):
            stack.enter_context(original._lock)
        for key, original in originals:
            surface = ToolSurface(original.mem, SourceAccess(original.mem, owner.acc), original.profile, original.ids)
            ticket = surface.begin_ranked_read(identity)
            if isinstance(ticket, ToolResult):
                return ticket
            sources.append({"key": key, "surface": surface, "ticket": ticket, "reader": surface._reader(ticket)})
        if not sources:
            return ToolResult("error", "没有可读取的工作区，请重新查", "bad_cursor")
        try:
            path = owner.profile.paths["mcp_return"]
            token = args.get("cursor")
            if token is not None:
                if not isinstance(token, str) or not token.startswith("M") or any(args.get(k) is not None for k in ("query", "keywords", "budget")):
                    raise BadCursor("聚合分页游标已失效，请重新查")
                cid, number, offset = owner.mem._reader._split_token(token[1:])
                cursor = owner.mem.store.get_cursor(cid)
                if cursor is None or cursor["ns"] != owner.mem.ns or cursor["kind"] != "recall_multi" or offset != 0:
                    raise BadCursor("聚合分页游标不存在，请重新查")
                data = cursor["data"]
                RP.validate(owner.mem._reader, data, path=path)
                if data["read_scope"] != list(keys) or [s["key"] for s in sources] != [s["key"] for s in data["sources"]]:
                    raise BadCursor("读取范围已改变，请重新查")
                if number < 0 or number >= len(data["pages"]):
                    raise BadCursor("页码无效，请重新查")
                for source, ref in zip(sources, data["sources"]):
                    local = source["reader"].store.get_cursor(ref["cursor_id"])
                    if local is None or local["ns"] != source["reader"].ns or local["kind"] != "recall_source":
                        raise BadCursor("来源游标不存在，请重新查")
                    source["data"], source["cursor_id"] = local["data"], ref["cursor_id"]
            else:
                cid, number = uuid.uuid4().hex[:16], 0
                refs, suffix = [], ""
                for source in sources:
                    reader, selection, query, queries, suffix = source["surface"].prepare_ranked_selection(source["ticket"], args)
                    if not reader.cfg.recall_ranked_pages:
                        raise BadCursor("来源工作区的分页配置不一致，请重新查")
                    local = RP.selection_data(reader, selection, path=path, query=query, queries=queries)
                    local_id = uuid.uuid4().hex[:16]
                    reader.store.put_cursor(local_id, reader.ns, "recall_source", reader.clock() if reader.clock else None, local)
                    source.update(reader=reader, data=local, cursor_id=local_id)
                    refs.append({"key": source["key"], "cursor_id": local_id, "label": adapter._label(source["key"])})
                data = RP.bind(owner.mem._reader, {"sources": refs, "read_scope": list(keys), "read_labels": [ref["label"] for ref in refs], "first_suffix": suffix, "delivered": {}}, path=path)
                groups = []
                for rank in range(max((len(s["data"]["groups"]) for s in sources), default=0)):
                    for index, source in enumerate(sources):
                        if rank < len(source["data"]["groups"]):
                            groups.append([[index, i] for i in source["data"]["groups"][rank]])
                if not groups:
                    # 没有任何材料；不创建聚合续页，也不产生无关工作区提示。
                    text = "（没有找到相关记录）"
                    data["pages"] = []
                    denied = _check_sources(sources, data, 0)
                    if denied:
                        return denied
                    if suffix:
                        text += "\n" + suffix
                    if len(text) > data["page_limit"] or not H.fits(path, text)[0]:
                        from .paging import ReturnTooLarge
                        raise ReturnTooLarge("空结果说明也超过显示上限")
                    first = next((s for s in sources if s["key"] == adapter.ws["key"]), sources[0])
                    result = first["surface"]._finish(first["ticket"], text, [], {"cursor": None, "selected_total": 0, "delivered": []})
                    if result.status != "ok":
                        return result
                    tickets = [(first["surface"].acc, first["ticket"])]
                    denied = _check_sources(sources, data, 0)
                    if denied:
                        _failed(tickets, denied.reason)
                        return denied
                    if delivery_tickets is not None:
                        delivery_tickets.extend(tickets)
                    return result
                data["pages"] = RP.make_plan(groups, lambda items, page, more: _render(adapter, sources, data, items, page, "M%s:%d:0" % (cid, page + 1) if more else None)[0],
                                              lambda text: len(text) <= data["page_limit"] and H.fits(path, text)[0])
                owner.mem.store.put_cursor(cid, owner.mem.ns, "recall_multi", owner.mem.clock(), data)
            denied = _check_sources(sources, data, number)
            if denied:
                return denied
            next_token = "M%s:%d:0" % (cid, number + 1) if number + 1 < len(data["pages"]) else None
            text, local_texts, delivered = _render(adapter, sources, data, data["pages"][number], number, next_token)
            if len(text) > data["page_limit"] or not H.fits(path, text)[0]:
                raise RP.GroupTooLarge("固定页计划已不能满足上限，请重新查或按消息编号读取")
            tickets, deliveries = [], []
            for index, local_text in local_texts.items():
                source = sources[index]
                result = source["surface"]._finish(source["ticket"], local_text, delivered[index], {"cursor": next_token})
                if result.status != "ok":
                    _failed(tickets, result.reason)
                    return result
                tickets.append((source["surface"].acc, source["ticket"]))
                deliveries.append(result.meta["delivery"])
            denied = _check_sources(sources, data, number)
            if denied:
                _failed(tickets, denied.reason)
                return denied
            # owner只保留固定计数/交付状态，不保留外区正文、正文哈希、消息编号或来源ticket引用。
            # 这样关闭回执知道这一轮查/交过，不给外区删除留下跨ns内容残留。
            primary = next((s for s in sources if s["key"] == adapter.ws["key"]), None)
            if primary is not None:
                primary["surface"].acc.event(primary["ticket"], "returned", {"delivery": "confirmed_by_rule" if all(x == "confirmed_by_rule" for x in deliveries) else "unknown", "cross_workspace": True, "source_count": len(local_texts)})
                pair = (primary["surface"].acc, primary["ticket"])
                if pair not in tickets:
                    tickets.append(pair)
            if delivery_tickets is not None:
                delivery_tickets.extend(tickets)
            # 聚合处只记录页号与本地游标引用；实际跨度/正文指纹已在各来源ns登记。
            data["delivered"][str(number)] = True
            owner.mem.store.update_cursor(cid, data)
            return ToolResult("ok", text, meta={"cursor": next_token, "selected_total": sum(len(s["data"]["chunks"]) for s in sources)})
        except Exception as error:
            _failed(locals().get("tickets", []), getattr(error, "code", type(error).__name__))
            return sources[0]["surface"]._map_error(sources[0]["ticket"], error)
