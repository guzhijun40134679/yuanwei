"""命令：conversations list／delete／status／resume（删除与密钥过滤·交回点乙）。预览只读：不整理、不调模型、不改库。"""
import json
import os

import pytest

from helpers import Clock, Scripted, make_mem, say
from test_deletion_core import MARK, KEEP, raw_bytes, world
from yuanwei.cli import main as cli_main
from yuanwei.deletion import legacy_conv_id
from yuanwei.window import WindowService


def run(capsys, *argv):
    code = cli_main(list(argv))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_list_shows_id_host_dates_count_and_head_of_first_user_message(tmp_path, capsys):
    mem, _c = world(tmp_path)
    code, out, _e = run(capsys, "conversations", "list", "--db", mem.db_path)
    rows = out["conversations"]
    assert code == 0 and len(rows) == 2
    r = [x for x in rows if MARK in (x["head"] or "")][0]
    assert r["host"] == "h" and r["messages"] == 4 and r["first_at"] == "2027-01-01" and len(r["head"]) <= 15 and r["kind"] == "legacy" and "终端" in r["head_note"]


def test_preview_does_not_write_spawn_or_call_a_model_and_delete_needs_yes(tmp_path, capsys):
    prov = Scripted("skip")
    mem, _c = world(tmp_path, provider=prov)
    n = len(prov.calls)
    before = open(mem.db_path, "rb").read()
    wal_before = open(mem.db_path + "-wal", "rb").read() if os.path.exists(mem.db_path + "-wal") else b""
    cid = legacy_conv_id("h", "S1", "")
    code, out, _e = run(capsys, "conversations", "delete", cid, "--db", mem.db_path)
    assert code == 0 and out["applied"] is False and out["plan"]["counts"]["messages"] == 4 and "impact" in out["plan"]
    assert open(mem.db_path, "rb").read() == before and len(prov.calls) == n
    assert (open(mem.db_path + "-wal", "rb").read() if os.path.exists(mem.db_path + "-wal") else b"") == wal_before
    assert len(mem.store.list_messages("w")) == 8
    code, out, _e = run(capsys, "conversations", "delete", cid, "--db", mem.db_path, "--yes")
    assert code == 0 and out["status"] == "complete" and out["applied"] is True and len(mem.store.list_messages("w")) == 4
    assert MARK.encode() not in raw_bytes(mem)


def test_not_found_exit_code_and_status_and_resume(tmp_path, capsys):
    mem, _c = world(tmp_path)
    code, out, err = run(capsys, "conversations", "delete", "t-000000000000", "--db", mem.db_path)
    assert code == 3
    code, out, _e = run(capsys, "conversations", "delete", "t-000000000000", "--db", mem.db_path, "--yes")
    assert code == 3
    from yuanwei.deletion import DeletionService
    DeletionService(mem.db_path, config_dir=mem.config_dir, clock=mem.clock, disk_free=lambda: 0).delete_conversation(legacy_conv_id("h", "S1", ""))
    code, out, _e = run(capsys, "conversations", "status", "--db", mem.db_path)
    d = out["deletions"][0]
    assert d["state"] == "files_cleaned" and d["code"] == "E_NO_SPACE" and MARK not in json.dumps(d, ensure_ascii=False)
    code, out, _e = run(capsys, "conversations", "resume", "--db", mem.db_path)
    assert code == 0 and out["resumed"][0]["status"] == "complete"


def test_cli_and_chat_use_the_same_window_ids_and_unbound_legacy_listed_exactly(tmp_path, capsys):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    ws = WindowService(mem.store)
    w = ws.bind_new("h", "B1")
    say(mem, "B1", 2, prefix="绑过的", date="2027-01-01")
    say(mem, "L1", 2, prefix="没绑过的", date="2027-01-02")
    code, out, _e = run(capsys, "conversations", "list", "--db", mem.db_path)
    kinds = {r["conv_id"]: r["kind"] for r in out["conversations"]}
    assert kinds[w.window_id] == "window" and kinds[legacy_conv_id("h", "L1", "")] == "legacy" and len(kinds) == 2
    code, out, _e = run(capsys, "conversations", "delete", legacy_conv_id("h", "L1", ""), "--db", mem.db_path, "--yes")
    assert out["counts"]["messages"] == 2 and [m["session_id"] for m in mem.store.list_messages("w")] == ["B1", "B1"]    # 只删精确的三元组
