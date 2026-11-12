"""整理结果的存储校验（变更集整份校验、只追加、按提交序号还原）。自造数据。"""
import copy

import pytest

from helpers import Scripted, make_mem, say
from yuanwei import lstate as LS
from yuanwei.storeerr import KeyConflict, ReceiptExists, StaleBase


@pytest.fixture
def organized(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    say(mem, "S1", 4)
    mem.end_session("h", "S1")
    mem.run()
    return mem


def _cs_for_new_thread(mem, state, key_thread="th-%s-0099"):
    from yuanwei import contract as WC
    ns = "w"
    seq = state["seq"] + 1
    tid = key_thread % WC.ns_tag(ns)
    rows = {t: [] for t in LS.TABLES}
    rows["threads"].append({"key": tid, "thread_id": tid, "created_by": "x", "created_seq": seq})
    rows["thread_names"].append({"key": tid + "#n1", "thread_id": tid, "version": 1, "name": "n", "by": "x", "created_seq": seq})
    rows["thread_descs"].append({"key": tid + "#d1", "thread_id": tid, "version": 1, "text": "d", "by": "x", "created_seq": seq})
    rows["receipts"].append({"key": "op:zz", "kind": "op", "op": "abandon_step", "op_id": "zz", "step_id": "s", "k": 99, "commit_seq": seq, "created_seq": seq})
    return {"ns": ns, "expected_prev_seq": state["seq"], "new_seq": seq, "inserts": rows, "updates": []}


def test_unreferenced_new_thread_is_rejected_whole(organized):
    st = organized.store.read_state("w")
    cs = _cs_for_new_thread(organized, st)
    with pytest.raises(LS.LightInvalid):                       # 新主线没有被任何句子引用：整份不收（receipt-only 的操作也不许带别的表）
        LS.validate_changeset(cs, st, organized.store.get_message)


def test_duplicate_key_and_receipt_and_stale_base(organized):
    st = organized.store.read_state("w")
    get = organized.store.get_message
    dup = copy.deepcopy(st["tables"]["receipts"][0])
    seq = st["seq"] + 1
    dup.update(commit_seq=seq, created_seq=seq)
    cs = {"ns": "w", "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": {"receipts": [dup]}, "updates": []}
    with pytest.raises(ReceiptExists):
        LS.validate_changeset(cs, st, get)
    old = {"ns": "w", "expected_prev_seq": st["seq"] - 1, "new_seq": st["seq"], "inserts": {"receipts": [dup]}, "updates": []}
    with pytest.raises(StaleBase):
        LS.validate_changeset(old, st, get)
    th = copy.deepcopy(st["tables"]["threads"][0])
    th["created_seq"] = seq
    r2 = {"key": "op:ok", "kind": "step", "step_id": "q", "k": 5, "commit_seq": seq, "created_seq": seq}
    r2["key"] = "step:q"
    cs2 = {"ns": "w", "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": {"threads": [th], "receipts": [r2]}, "updates": []}
    with pytest.raises(KeyConflict):                           # 同一个主线键再加一次：不许覆盖
        LS.validate_changeset(cs2, st, get)


def test_updates_are_not_allowed_and_as_of_hides_later_rows(organized):
    st = organized.store.read_state("w")
    seq = st["seq"] + 1
    cs = {"ns": "w", "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": {}, "updates": [{"table": "threads", "key": "x", "set": {}}]}
    with pytest.raises(LS.LightInvalid):
        LS.validate_changeset(cs, st, organized.store.get_message)
    assert LS.as_of(st, 0)["tables"]["sentences"] == [] and len(LS.as_of(st, st["seq"])["tables"]["sentences"]) == len(st["tables"]["sentences"])
