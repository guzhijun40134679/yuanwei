"""删除留痕清单的完整性测试（R1，现在就跑）：库里每张表、每一列、org_rows 的每个字段、判断记录里真实出现的每个键、游标的键、待补行的键、待核行的键，
都必须在 tests/delete_trace_inventory.py 里登记过类别和处理办法。以后有人加表、加列、加字段而没登记，这里就红。
"""
import json
import sqlite3

import pytest

import delete_trace_inventory as D
from helpers import Clock, Scripted, make_mem, say
from yuanwei import lstate as LS
from yuanwei import sqlstore as SS


def _schema():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    for v in range(1, SS.SCHEMA_VERSION + 1):
        for s in SS.MIGRATIONS[v]:
            c.execute(s)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        out[t] = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    return out


def test_every_table_and_column_in_the_real_schema_is_in_the_inventory():
    for t, cols in _schema().items():
        assert t in D.TABLES, "表 %s 没有登记到留痕清单" % t
        for c in cols:
            assert c in D.TABLES[t]["columns"], "列 %s.%s 没有登记到留痕清单" % (t, c)


def test_inventory_columns_that_are_not_in_the_schema_are_marked_as_planned_v11_and_only_while_the_schema_is_older():
    real = _schema()
    for t, spec in D.TABLES.items():
        if t not in real:
            assert spec["since"] == 11 and SS.SCHEMA_VERSION < 11, "清单里有表 %s，库里没有，也没标成 v11 计划" % t
            continue
        for c, info in spec["columns"].items():
            if c not in real[t]:
                assert info["since"] == 11 and SS.SCHEMA_VERSION < 11, "清单里有列 %s.%s，库里没有，也没标成 v11 计划" % (t, c)
    if SS.SCHEMA_VERSION >= 11:                                          # 乙加了 v11 以后：计划里的表和列必须真的都在
        for t, spec in D.TABLES.items():
            assert t in real, t
            for c in spec["columns"]:
                assert c in real[t], (t, c)


def test_every_entry_has_a_valid_class_and_action_and_the_class_matches_the_action():
    def check(where, info):
        assert info["cls"] in D.CLASSES, where
        assert info["action"] in D.ACTIONS, where
        if info["cls"] in (D.A, D.B):
            assert info["action"] != "keep", "%s：原文副本、派生文字不能留着不动" % where
        if info["cls"] == D.H and info["action"] == "keep":
            assert info["note"], "%s：内容哈希留着要写明理由" % where
        if info["action"] in ("scrub", "placeholder", "pseudonymize", "file_rewrite", "void", "gc_orphans"):
            assert info["cls"] in D.CLASSES
    for t, spec in D.TABLES.items():
        assert spec["action"] in D.ACTIONS, t
        assert spec["columns"], t
        for c, info in spec["columns"].items():
            check("%s.%s" % (t, c), info)
    for name, group in (("ORG", D.ORG_FIELDS), ):
        for t, fields in group.items():
            for f, info in fields.items():
                check("%s.%s.%s" % (name, t, f), info)
    for name, group in (("JUDGMENT", D.JUDGMENT_KEYS), ("REQUEST", D.REQUEST_KEYS), ("CURSOR", D.CURSOR_KEYS), ("PENDING", D.PENDING_LINE_KEYS), ("REVIEW", D.REVIEW_RECORD_KEYS)):
        for f, info in group.items():
            check("%s.%s" % (name, f), info)


def test_every_text_bearing_column_is_classified_as_original_or_derived_and_never_kept():
    """表里凡是装文字的大列（content、text、data、detail、reason、last_error…）都不能是 C 类留着不动。"""
    must = {("messages", "content"): D.A, ("anchors", "text"): D.A, ("judgments", "data"): D.A, ("cursors", "data"): D.A}
    for (t, c), cls in must.items():
        assert D.TABLES[t]["columns"][c]["cls"] == cls, (t, c)
    assert D.TABLES["steps"]["columns"]["last_error"]["cls"] == D.B and D.TABLES["steps"]["columns"]["last_error"]["action"] != "keep"
    assert D.TABLES["org_rows"]["columns"]["data"]["cls"] == D.B


def test_org_rows_inventory_covers_every_field_of_every_table_in_lstate():
    assert set(D.ORG_FIELDS) == set(LS.TABLES)
    for t in LS.TABLES:
        need = set(LS.RECORD_FIELDS[t]) | {"key", "created_seq"}
        have = set(D.ORG_FIELDS[t])
        assert need <= have, (t, sorted(need - have))
    for t, fields in D.ORG_FIELDS.items():                              # 文字字段必须是 B 或 A，不能是 C
        for f in ("name", "text", "title", "reason"):
            if f in fields and t in ("thread_names", "thread_descs", "occ_titles", "occ_summaries", "attachments", "sentences", "receipts"):
                assert fields[f]["cls"] in (D.A, D.B), (t, f)


def _scenario(tmp_path):
    """真跑一遍：整理成功一步、整理放弃一步（拿到判断记录里的各种键、收据里的各种键）、查一次留下游标。"""
    clock = Clock()
    modes = {"n": 0}

    def prov(request, n):
        modes["n"] += 1
        from helpers import out_thread, sids_of
        if request.get("step_id") and request["step_id"].endswith("0002"):
            return "这不是 JSON"
        return out_thread(sids_of(request["text"]))

    mem = make_mem(tmp_path, provider=Scripted(prov), clock=clock)
    say(mem, "S1", 4, date="2027-01-01")
    mem.end_session("h", "S1")
    clock.advance(hours=2)
    say(mem, "S2", 4, date="2027-01-02")
    mem.end_session("h", "S2")
    mem.run()
    mem.recall("今天聊聊")
    return mem


def test_every_key_in_real_judgments_receipts_and_cursors_is_in_the_inventory(tmp_path):
    mem = _scenario(tmp_path)
    st = mem.store
    seen_j, seen_r = set(), set()
    n_j = 0
    for s in st.list_steps("w"):
        for rec in st.list_judgments("w", s["step_id"]):
            n_j += 1
            seen_j |= set(rec)
            seen_r |= set(rec.get("request") or {})
    assert n_j >= 2
    assert seen_j <= set(D.JUDGMENT_KEYS), sorted(seen_j - set(D.JUDGMENT_KEYS))
    assert seen_r <= set(D.REQUEST_KEYS), sorted(seen_r - set(D.REQUEST_KEYS))
    rk = set()
    for r in st.read_state("w")["tables"]["receipts"]:
        rk |= set(r)
    assert rk <= set(D.ORG_FIELDS["receipts"]), sorted(rk - set(D.ORG_FIELDS["receipts"]))
    for t in LS.TABLES:
        for r in st.read_state("w")["tables"][t]:
            assert set(r) <= set(D.ORG_FIELDS[t]), (t, sorted(set(r) - set(D.ORG_FIELDS[t])))
    ck = set()
    for (data,) in st.conn.execute("SELECT data FROM cursors").fetchall():
        ck |= set(json.loads(data))
    assert ck and ck <= set(D.CURSOR_KEYS), sorted(ck - set(D.CURSOR_KEYS))
    for (data,) in st.conn.execute("SELECT data FROM cursors").fetchall():
        d = json.loads(data)
        assert "chunks" in d and "query" in d                              # 游标里确实有原话和查询原文：清单里写 A 类不是白写


def test_every_key_of_real_pending_and_review_lines_is_in_the_inventory(tmp_path):
    import sqlite3 as sq
    mem = make_mem(tmp_path)

    def busy(a):
        raise sq.OperationalError("database is locked")
    mem._insert = busy
    r = mem.record("h", "S1", "m1", "user", "排队的一句话", time="2027-01-01", turn_id="t-1")
    assert r["pending"] is True
    keys = set()
    with open(mem._pending_path(), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                keys |= set(json.loads(line))
    assert keys and keys <= set(D.PENDING_LINE_KEYS), sorted(keys - set(D.PENDING_LINE_KEYS))
    mem._set_aside("读不出的一行".encode("utf-8"), ValueError("x"))
    mem._set_aside(b"\xff\xfe\xfd", ValueError("y"))
    rk = set()
    with open(mem._review_path(), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rk |= set(json.loads(line))
    assert rk == {"reason", "at", "line", "line_b64"} and rk <= set(D.REVIEW_RECORD_KEYS)


def test_files_memory_and_external_boundary_are_listed():
    assert len(D.FILES) >= 10 and len(D.MEMORY) >= 5 and len(D.EXTERNAL_BOUNDARY) >= 6
    for k, (cls, how) in list(D.FILES.items()) + list(D.MEMORY.items()):
        assert cls and how and len(how) >= 4, k
    names = " ".join(D.FILES)
    for needed in (".claim-", ".review", ".tmp", "-wal", "pending-"):
        assert needed in names, needed
    assert any("Hermes" in x for x in D.EXTERNAL_BOUNDARY) and any("备份" in x for x in D.EXTERNAL_BOUNDARY) and any("模型服务" in x for x in D.EXTERNAL_BOUNDARY)


def test_tombstones_carry_no_content():
    for k, v in D.TOMBSTONES.items():
        assert v, k
    assert D.TOMBSTONES["occ_summary"] == "这条摘要的来源被删了"
