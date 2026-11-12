"""正文密钥过滤接进 record、查询路径、待补、status、读取标记、scan（删除与密钥过滤·交回点乙）。假密钥全部现算（secret_rules_table.F），没有真值。"""
import glob
import json
import os
import sqlite3

import pytest

import secret_rules_table as T
from helpers import Clock, Scripted, make_mem, make_old_db, say
from yuanwei import redact as RD
from yuanwei import secretscan as SS
from yuanwei.cli import main as cli_main

SK = "sk-ant-" + T.F("int1", 40, T.B64URL)
SK2 = "sk-proj-" + T.F("int2", 40, T.B64URL)


def raw_bytes(mem):
    out = b""
    for p in [mem.db_path, mem.db_path + "-wal"] + glob.glob(os.path.join(mem.config_dir, "*")):
        if os.path.isfile(p):
            out += open(p, "rb").read()
    return out


def test_record_filters_user_and_assistant_before_fingerprint_slicing_and_pending(tmp_path):
    mem = make_mem(tmp_path)
    r1 = mem.record("h", "S", "m1", "user", "我的 key 是 %s，别外传" % SK, time="2027-01-01")
    r2 = mem.record("h", "S", "m2", "assistant", "好的，你的 key %s 我不会外传" % SK, time="2027-01-01")
    assert r1["redacted"] == 1 and r2["redacted"] == 1
    rows = mem.store.list_messages("w")
    assert all(SK not in r["content"] and "〔密钥已挡掉：Anthropic〕" in r["content"] for r in rows)
    assert not any(SK in a["text"] for a in mem.store.conn.execute("SELECT text FROM anchors").fetchall())
    assert SK.encode() not in raw_bytes(mem)


def test_fake_secret_not_in_raw_bytes_of_db_wal_pending_review_or_upstream_request(tmp_path):
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov)
    mem.record("h", "S", "m1", "user", "密钥 %s 在这" % SK, time="2027-01-01")
    mem.record("h", "S", "m2", "assistant", "收到", time="2027-01-01")
    sq = sqlite3.OperationalError("database is locked")
    orig = mem._insert
    mem._insert = lambda a: (_ for _ in ()).throw(sq)
    r = mem.record("h", "S", "m3", "user", "排队的 %s" % SK2, time="2027-01-01")
    assert r["pending"] is True
    mem._set_aside(json.dumps({"host": "h", "session_id": "S", "text": "占位"}).encode(), ValueError("x"))
    mem._insert = orig
    mem.end_session("h", "S")
    mem.run()
    assert prov.calls and all(SK not in c["text"] and SK2 not in c["text"] for c in prov.calls)
    blob = raw_bytes(mem)
    assert SK.encode() not in blob and SK2.encode() not in blob


def test_resend_same_message_same_fingerprint_no_conflict_and_not_counted_twice(tmp_path):
    mem = make_mem(tmp_path)
    a = mem.record("h", "S", "m1", "user", "key %s" % SK, time="2027-01-01")
    b = mem.record("h", "S", "m1", "user", "key %s" % SK, time="2027-01-01")
    assert b["duplicate"] is True and a["fingerprint"] == b["fingerprint"]
    assert mem.status()["redaction"]["stored_messages"] == 1 and mem.status()["redaction"]["stored"] == {"Anthropic": 1}


def test_resend_after_rules_upgrade_is_duplicate_not_conflict(tmp_path, monkeypatch):
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "我的备忘 abcdef 先放着", time="2027-01-01")           # 旧规则下没有命中
    monkeypatch.setattr(RD, "redact", lambda t: RD.Redaction(t.replace("abcdef", "〔密钥已挡掉：键值〕"), 1 if "abcdef" in t else 0, ("键值",) if "abcdef" in t else (), 2))
    r = mem.record("h", "S", "m1", "user", "我的备忘 abcdef 先放着", time="2027-01-01")           # 新规则命中：重送按新规则过滤已存文字判等
    assert r["duplicate"] is True


def test_conflicting_resend_is_still_a_conflict(tmp_path):
    from yuanwei.storeerr import MessageConflict
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "一句话", time="2027-01-01")
    with pytest.raises(MessageConflict):
        mem.record("h", "S", "m1", "user", "另一句话", time="2027-01-01")


def test_status_counts_by_kind_and_pending_separate(tmp_path):
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "a %s b %s" % (SK, SK2), time="2027-01-01")
    mem._insert = lambda a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    mem.record("h", "S", "m2", "user", "排队 %s" % SK, time="2027-01-01")
    st = mem.status()["redaction"]
    assert st["stored"] == {"Anthropic": 1, "OpenAI": 1} and st["stored_messages"] == 1 and st["pending_blocks"] == 1 and st["rules_version"] == RD.RULES_VERSION


def test_redacted_message_is_marked_in_read_output_and_clean_output_unchanged(tmp_path):
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "我的密钥 %s 先放着" % SK, time="2027-01-01")
    mem.record("h", "S", "m2", "user", "普通的一句关于烤鱼的话", time="2027-01-01")
    t = mem.recall("密钥 先放着")["text"]
    assert "｜挡掉 1 处〕" in t and SK not in t
    (tmp_path / "c").mkdir()
    clean = make_mem(tmp_path / "c")
    clean.record("h", "S", "m2", "user", "普通的一句关于烤鱼的话", time="2027-01-01")
    assert "挡掉" not in clean.recall("烤鱼")["text"]
    g = mem.get_messages([mem.store.list_messages("w")[0]["msg_id"]])
    assert g["messages"][0].get("redacted") == 1


def test_filter_error_fails_closed_nothing_stored(tmp_path, monkeypatch):
    mem = make_mem(tmp_path)
    monkeypatch.setattr(RD, "redact", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        mem.record("h", "S", "m1", "user", "一句话 %s" % SK, time="2027-01-01")
    assert mem.store.list_messages("w") == [] and mem.pending_count() == 0


class VecClient:
    def __init__(self):
        self.texts = []

    def embed_batch(self, texts):
        self.texts += list(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_query_with_fake_secret_filtered_before_vector_request_cursor_and_note(tmp_path):
    from helpers2 import hybrid
    mem, client = hybrid(tmp_path)
    try:
        mem.recall("我之前说的 %s 是什么" % SK)
    except Exception:                                                              # noqa: BLE001  缺向量等错不重要：关心的是发出去的文字
        pass
    sent = " ".join(t for batch in client.calls for t in batch)
    assert client.calls and SK not in sent and "〔密钥已挡掉：Anthropic〕" in sent                                      # 向量服务收到的是遮掉的查询
    for (data,) in mem.store.conn.execute("SELECT data FROM cursors").fetchall():
        assert SK not in data
    (tmp_path / "b").mkdir()
    mem2 = make_mem(tmp_path / "b")
    mem2.record("h", "S", "m1", "user", "今天吃了烤鱼", time="2027-01-01")
    r = mem2.note("我之前说的 %s 烤鱼" % SK)
    assert SK not in json.dumps(r, ensure_ascii=False, default=str)
    for (data,) in mem2.store.conn.execute("SELECT data FROM cursors").fetchall():
        assert SK not in data


def test_recall_keywords_filtered_like_query_and_clean_query_unchanged(tmp_path):
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "今天吃了烤鱼", time="2027-01-01")
    from yuanwei import paging as PG
    from yuanwei import hosts as H
    PG.recall_first(mem._reader, "烤鱼", ["我的 %s" % SK], None, H.profile("hermes").paths["mcp_return"])
    assert mem.store.conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] >= 1
    for (data,) in mem.store.conn.execute("SELECT data FROM cursors").fetchall():
        assert SK not in data
    assert RD.redact_query("烤鱼") == "烤鱼"


def test_legacy_unfiltered_pending_line_is_filtered_before_flush_inserts_it(tmp_path):
    mem = make_mem(tmp_path)
    line = {"host": "h", "session_id": "S", "message_id": "m9", "role": "user", "text": "旧待补行里的 %s" % SK, "time": "2027-01-01", "branch": "", "time_precision": "day",
            "stable_id": None, "turn_id": None, "received_at": mem.clock()}                                        # 没有 rv、capture：v11 之前写的
    mem._write_pending(line)
    r = mem.flush_pending_detail()
    assert r["inserted"] == 1
    row = mem.store.list_messages("w")[0]
    assert SK not in row["content"] and "〔密钥已挡掉：Anthropic〕" in row["content"]
    assert mem.status()["redaction"]["stored"] == {"Anthropic": 1}


def test_secrets_scan_reports_counts_only_no_content_and_does_not_modify(tmp_path, capsys):
    p = str(tmp_path / "old.sqlite")
    make_old_db(p, 10, content="旧库里的 %s 一句话" % SK)
    before = open(p, "rb").read()
    code = cli_main(["secrets", "scan", "--db", p])
    out = capsys.readouterr().out
    assert code == 0 and SK not in out and json.loads(out)["workspaces"][0]["messages_with_hits"] == 1 and json.loads(out)["workspaces"][0]["hits_by_kind"] == {"Anthropic": 1}
    assert open(p, "rb").read() == before                                                                            # 库文件字节不变


def test_secrets_scan_reports_unreadable_regions_not_zero(tmp_path, capsys):
    assert cli_main(["secrets", "scan", "--db", str(tmp_path / "nope.sqlite")]) == 5
    rep = json.loads(capsys.readouterr().out)
    assert rep["complete"] is False and rep["errors"] and rep["scope"]["db"] == "unreadable"
    mem = make_mem(tmp_path)
    mem.record("h", "S", "m1", "user", "x", time="2027-01-01")
    open(mem._review_path(), "wb").write(b'{"reason":"x","at":"t","line_b64":"@@@notb64"}\n')
    capsys.readouterr()
    code = cli_main(["secrets", "scan", "--db", mem.db_path])
    rep = json.loads(capsys.readouterr().out)
    assert code == 5 and any(e["code"] == "E_BAD_LINE" for e in rep["errors"])


def test_unredacted_clean_text_keeps_record_receipt_and_output_shape(tmp_path):
    mem = make_mem(tmp_path)
    r = mem.record("h", "S", "m1", "user", "普通的话", time="2027-01-01")
    assert r["redacted"] == 0 and "挡掉" not in mem.recall("普通的话")["text"]


@pytest.mark.parametrize("old", list(range(1, 11)))
def test_v11_opens_any_older_db_v1_to_v10(tmp_path, old):
    p = str(tmp_path / ("v%d.sqlite" % old))
    make_old_db(p, old, content="旧库里的一句话 %d" % old)
    mem = make_mem(tmp_path, name="v%d.sqlite" % old)
    assert mem.store.schema_version() == 12
    assert [r["content"] for r in mem.store.list_messages("w")] == ["旧库里的一句话 %d" % old]
    assert mem.store.conn.execute("SELECT step_hwm FROM workspaces").fetchone()[0] == 0
    assert mem.store.conn.execute("SELECT n FROM captures").fetchone()[0] == 0 and mem.store.db_id()
    assert mem.recall("旧库里的一句话")["delivered"]
    mem.record("h", "S2", "x1", "user", "新话", time="2027-01-02")
    assert mem.store.conn.execute("SELECT capture_seq FROM messages WHERE host_msg_id='x1'").fetchone()[0] == 1


def test_legacy_claim_batch_lines_are_never_inserted_raw(tmp_path):
    mem = make_mem(tmp_path)
    line = {"host": "h", "session_id": "S", "message_id": "m7", "role": "user", "text": "领取批次里的 %s" % SK, "time": "2027-01-01", "branch": "", "time_precision": "day", "stable_id": None,
            "turn_id": None, "received_at": mem.clock()}
    open(os.path.join(mem.config_dir, os.path.basename(mem._pending_path()) + ".claim-000000000001"), "wb").write((json.dumps(line, ensure_ascii=False) + "\n").encode())
    assert mem.flush_pending_detail()["inserted"] == 1
    assert SK not in mem.store.list_messages("w")[0]["content"] and SK.encode() not in raw_bytes(mem)
