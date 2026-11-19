"""37 独立安全补验：真实工具/便条路径，自造未整理原话、临时库和假向量。"""
import re

import pytest

from helpers2 import IDENT
from test_fix37_pending import PendingWorld, configure_vectors, saved
from test_hermes_workspace import call, turn
from test_ranked_pages import token
from yuanwei import vectors as VX
from yuanwei.deletion import DeletionService


def pending_pages(tmp_path, vector="keyword", cross=True):
    w = PendingWorld(tmp_path, vector, neighbors=0, recall_page_chars=700)
    a = w.adapter("/work/a" if cross else "/elsewhere", session="A")
    for n in range(1, 5):
        saved(a, "A", "紫檀盒第%d条。" % n + "这是一段用于分页的合成记录。" * 10, n=n)
    b = w.adapter("/elsewhere", session="B")
    configure_vectors(w)
    turn(b, 1, "紫檀盒")
    first = call(b, "recall", {"query": "紫檀盒"})
    assert first["status"] == "ok", first
    cur = token(first["text"])
    assert cur, first
    return w, a, b, cur


@pytest.mark.parametrize("cross", [False, True])
def test_pending_permission_revoke_blocks_all_entrypoints_before_vector_query(tmp_path, calibrated_test_vectors, cross):
    w, a, b, cursor = pending_pages(tmp_path, "ready", cross)
    mid = a.mem.store.list_messages(a.mem.ns)[0]["msg_id"]
    a.mem.set_read_permission(False)
    clients = [m._reader.embed_client for m in w.mems.values()]
    before = [len(c.calls) for c in clients]
    for name, args in [("recall", {"query": "紫檀盒"}), ("recall", {"cursor": cursor}),
                       ("get_messages", {"ids": [mid]})]:
        out = call(b, name, args)
        assert out["status"] == "refused", out
        assert mid not in out["text"]
    w.cfg["mode"] = "note"
    c = w.adapter("/elsewhere", session="C")
    turn(c, 1, "紫檀盒")
    assert mid not in c.prefetch("紫檀盒", session_id="C")
    assert [len(c.calls) for c in clients] == before


@pytest.mark.parametrize("cross", [False, True])
def test_delete_pending_conversation_removes_anchors_vectors_and_invalidates_page(tmp_path, calibrated_test_vectors, cross):
    w, a, b, cursor = pending_pages(tmp_path, "ready", cross)
    rows = a.mem.store.list_messages(a.mem.ns)
    mids = {r["msg_id"] for r in rows}
    keys = {VX.text_key(x.text) for x in a.mem._reader.snapshot().anchors}
    assert a.mem.store.get_vectors(a.mem.vector_fp, keys)
    svc = DeletionService(a.mem.db_path, config_dir=a.mem.config_dir, clock=a.mem.clock)
    try:
        assert svc.delete_conversation(a._win_ref.window_id).status == "complete"
    finally:
        svc.close()
    assert not a.mem.store.list_messages(a.mem.ns)
    assert not a.mem._reader.snapshot().anchors
    assert not a.mem.store.get_vectors(a.mem.vector_fp, keys)
    old = call(b, "recall", {"cursor": cursor})
    assert old["status"] == "error" and old["reason"] == "history_deleted", old
    fresh = call(b, "recall", {"query": "紫檀盒"})
    assert fresh["status"] == "ok" and not any(m in fresh["text"] for m in mids)
    direct = call(b, "get_messages", {"ids": [rows[0]["msg_id"]]})
    assert direct["status"] != "ok", direct
    w.cfg["mode"] = "note"
    c = w.adapter("/elsewhere", session="C")
    turn(c, 1, "紫檀盒")
    assert not any(m in c.prefetch("紫檀盒", session_id="C") for m in mids)


def test_confirmation_no_record_turn_cannot_create_pending_original_or_anchor(tmp_path, calibrated_test_vectors):
    w = PendingWorld(tmp_path, "ready")
    a = w.adapter("/elsewhere", session="A")
    saved(a, "A")
    configure_vectors(w)
    turn(a, 2, "请删除这段对话")
    assert call(a, "window_delete")["state"] == "waiting"
    turn(a, 3, "确认删除")
    tok = a._cur["A"]
    a.sync_turn("确认删除", "紫檀盒已删", session_id="A", messages=[{"role": "user", "content": "确认删除"}])
    assert not a.mem.store.list_messages(a.mem.ns)
    assert not a.mem._reader.snapshot().anchors
    late = a.mem.record("hermes", "A", "late-retry", "assistant", "紫檀盒迟到答复", turn_id=tok)
    assert late["blocked"] == "no_record_turn"
    assert a.mem.store.vector_count(a.mem.vector_fp) == 0
    b = w.adapter("/elsewhere", session="B")
    turn(b, 1, "紫檀盒")
    assert "紫檀盒迟到答复" not in call(b, "recall", {"query": "紫檀盒"})["text"]


def test_cross_workspace_earlier_pending_of_same_session_id_is_found_and_marked(tmp_path):
    """11-18：不再按宿主/会话/分支排未整理原话；另一个工作区里同会话号的未整理原话照常交出，并标未整理。"""
    w = PendingWorld(tmp_path)
    project = w.adapter("/work/a", session="S")
    earlier = saved(project, "S")[0]["msg_id"]
    foreign = project.mem
    keep = {foreign.record(host, sid, "other-" + host + br, "user", "紫檀盒边界", branch=br)["msg_id"]
            for host, sid, br in [("hermes", "OTHER", ""), ("otherhost", "S", ""), ("hermes", "S", "fork")]}
    life = w.adapter("/elsewhere", session="S")
    turn(life, 2, "紫檀盒")
    result = call(life, "recall", {"query": "紫檀盒"})
    assert result["status"] == "ok", result
    assert earlier in result["text"] and all(m in result["text"] for m in keep)
    assert "未整理：" in result["text"]


def test_same_conversation_pending_history_stays_readable_through_a_cursor(tmp_path):
    """11-18 回到只排当前轮：游标里还没交的是同会话更早的未整理原话时照常续读并标未整理；当前轮的游标排除另见 test_v9_raw_cut。"""
    w, a, b, cursor = pending_pages(tmp_path, cross=False)
    turn(a, 8, "紫檀盒")
    result = call(a, "recall", {"cursor": cursor})
    assert result["status"] == "ok", result
    assert "紫檀盒第" in result["text"] and "未整理：" in result["text"]


def test_organized_and_pending_share_one_selection_budget_and_ranked_pages(tmp_path):
    w = PendingWorld(tmp_path, neighbors=0, recall_page_chars=1600)
    a = w.adapter("/elsewhere", session="OLD")
    mem = a.mem
    # 使用完整 8,000 默认额度；页上限只缩小展示，不能给未整理材料额外额度。
    old = []
    for i in range(10):
        old.append(mem.record("hermes", "OLD", str(i), "user", "紫檀盒旧%d。" % i + "合成旧材料。" * 180)["msg_id"])
    mem.end_session("hermes", "OLD"); mem.run()
    new = []
    for i in range(10):
        new.append(mem.record("hermes", "NEW", str(i), "user", "紫檀盒新%d。" % i + "合成新材料。" * 180)["msg_id"])
    selected = mem._reader.select("紫檀盒")
    wanted = set(selected["pack"]["最终"]["选中"])
    assert selected["budget_total"] == 8000
    assert selected["pack"]["最终"]["tokens"] <= 8000
    assert wanted & set(old) and wanted & set(new) and len(wanted) < len(old) + len(new)
    b = w.adapter("/elsewhere", session="B")
    turn(b, 1, "紫檀盒")
    got, pages = [], []
    out = call(b, "recall", {"query": "紫檀盒"})
    while True:
        assert out["status"] == "ok", out
        pages.append(out["text"])
        got.extend(re.findall(r"〔(?:用户|助手)｜[^〕]*?(m-[a-f0-9]+)〕", out["text"]))
        cursor = token(out["text"])
        if not cursor:
            break
        out = call(b, "recall", {"cursor": cursor})
    assert len(pages) > 1 and set(got) == wanted and len(got) == len(set(got))
    assert all("未整理：" in page for page in pages if any(m in page for m in new))
