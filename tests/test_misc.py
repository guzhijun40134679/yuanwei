"""其余部件：真实提供方（假网络）、费用账记模型名和指纹、续读游标、待补文件、导出、回答请求、向量客户端的回应校验、命令行。"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error

import pytest

from helpers import Clock, Scripted, make_mem, say
from yuanwei import Config, Memory, answer as AW, provider as PV, vectors as VX
from yuanwei.ask import RetryableNetworkError
from yuanwei.errors import BadCursor, ConfigError, ProviderError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def chat_json(text="ok", finish="stop"):
    return json.dumps({"model": "deepseek-flash", "system_fingerprint": "fp123", "choices": [{"finish_reason": finish, "message": {"content": text}}],
                       "usage": {"prompt_cache_miss_tokens": 10, "prompt_cache_hit_tokens": 0, "completion_tokens": 5}}).encode("utf-8")


def test_write_request_body_is_one_user_message_compact_json():
    b = PV.write_request_bytes("你好")
    assert b == '{"model":"deepseek-flash","temperature":0,"reasoning_effort":"high","max_tokens":65536,"messages":[{"role":"user","content":"你好"}]}'.encode("utf-8")


def test_real_provider_paths_with_fake_network(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k-123")
    seen = {}

    def opener(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = req.data
        return FakeResp(chat_json("好"))
    p = PV.OpenAICompatProvider("https://example.invalid/v1", "TEST_KEY", opener=opener)
    r = p({"text": "x"})
    assert r["text"] == "好" and r["model"] == "deepseek-flash" and r["fingerprint"] == "fp123" and r["usage"]["completion_tokens"] == 5
    assert seen["auth"] == "Bearer k-123" and seen["body"] == PV.write_request_bytes("x")

    def e500(req, timeout):
        raise urllib.error.HTTPError("u", 502, "bad", {}, io.BytesIO(b""))

    def e401(req, timeout):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))

    def eurl(req, timeout):
        raise urllib.error.URLError("down")

    def trunc(req, timeout):
        return FakeResp(chat_json("半截", finish="length"))
    for op, exc in ((e500, RetryableNetworkError), (eurl, RetryableNetworkError), (e401, ProviderError), (trunc, RetryableNetworkError)):
        with pytest.raises(exc):
            PV.OpenAICompatProvider("https://example.invalid/v1", "TEST_KEY", opener=op)({"text": "x"})
    monkeypatch.delenv("TEST_KEY")
    with pytest.raises(ConfigError):
        PV.OpenAICompatProvider("https://example.invalid/v1", "TEST_KEY", opener=opener)({"text": "x"})      # 密钥环境变量没设：报错，不读任何文件


def test_cost_ledger_records_model_and_fingerprint_and_does_not_pin(tmp_path):
    seq = iter(["fp-A", "fp-B"])

    def mode(request, n):
        from helpers import out_skip, sids_of
        return out_skip(sids_of(request["text"]))

    class P(Scripted):
        def __call__(self, request):
            r = super().__call__(request)
            r["fingerprint"] = next(seq)                 # 中途服务指纹变了：只记，不停
            return r
    mem = make_mem(tmp_path, provider=P(mode))
    for s in ("S1", "S2"):
        say(mem, s, 2)
        mem.end_session("h", s)
    assert mem.run()["done"] == 2
    rows = mem.store.cost_rows("w")
    assert [r["fingerprint"] for r in rows] == ["fp-A", "fp-B"] and {r["model"] for r in rows} == {"fake-model"} and all(r["cny"] > 0 and not r["unknown"] for r in rows)


def test_recall_pagination_is_complete_stable_and_logs_only_delivered(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), recall_page_chars=400)
    for i in range(1, 31):
        mem.record("h", "S1", "p%d" % i, "user" if i % 2 else "assistant", "榴莲酥第%d条：这是一句带编号的话，专门用来分页。" % i, time="2027-03-01")
    mem.end_session("h", "S1")
    mem.run()
    first = mem.recall("榴莲酥")
    assert first["has_more"] and first["cursor"]
    pages, delivered, cur = [first], list(first["delivered"]), first["cursor"]
    while cur:
        # 翻页期间后台又有新提交：后面几页仍是同一份结果
        if len(pages) == 1:
            mem.record("h", "S2", "late1", "user", "榴莲酥新来的一条")
            mem.record("h", "S2", "late2", "assistant", "榴莲酥新来的回复")
            mem.end_session("h", "S2")
            mem.run()
        p = mem.recall(cursor=cur)
        assert p == mem.recall(cursor=cur)                    # 同一个游标重取，同一页
        pages.append(p)
        delivered += p["delivered"]
        cur = p["cursor"]
    ids = [d["msg_id"] for d in delivered]
    total = first["selected_total"]
    assert len(ids) >= total                                  # 每条都交出（长条被切开时同一条会出现多段）
    assert len(set(ids)) == total
    ranges = {}
    for d in delivered:
        ranges.setdefault(d["msg_id"], []).append((d["start"], d["end"]))
    for mid, rs in ranges.items():
        assert rs[0][0] == 0 and all(rs[i][1] == rs[i + 1][0] for i in range(len(rs) - 1))      # 同一条切开的各段首尾相接
    assert all("新来的" not in mem.store.message_row("w", m)["content"] for m in ids)          # 翻页期间的新提交没混进来
    cursor_row = mem.store.get_cursor(first["cursor"].split(":")[0])
    assert sum(len(v) for v in cursor_row["data"]["delivered"].values()) == len(delivered)    # 只记本页实际交出的，重取不重复登记
    with pytest.raises(BadCursor):
        mem.recall(cursor="nope:0:0")
    with pytest.raises(BadCursor):
        mem.recall(cursor="%s:999:0" % first["cursor"].split(":")[0])


def test_busy_database_goes_to_pending_file_and_is_flushed_on_next_open(tmp_path, monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.store.conn.execute("PRAGMA busy_timeout=30")
    blocker = sqlite3.connect(str(tmp_path / "m.sqlite"), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    r = mem.record("h", "S1", "w1", "user", "忙的时候记的一句")
    assert r["pending"] is True and r["raw_saved"] is False
    assert mem.pending_count() == 1 and mem.status()["pending_writes"] == 1
    blocker.execute("ROLLBACK")
    blocker.close()
    mem.close()
    mem2 = make_mem(tmp_path, provider=Scripted("skip"))               # 下次启动补进库
    assert mem2.pending_count() == 0 and mem2.store.count_messages("w") == 1
    assert mem2.record("h", "S1", "w1", "user", "忙的时候记的一句")["duplicate"] is True      # 重送不重复


def test_export_jsonl_has_format_version_and_records(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    say(mem, "S1", 4)
    mem.end_session("h", "S1")
    mem.run()
    p = str(tmp_path / "out.jsonl")
    info = mem.export(p)
    lines = [json.loads(l) for l in open(p, encoding="utf-8")]
    assert lines[0]["type"] == "header" and lines[0]["format"] == "yuanwei-export/1" and lines[0]["workspace"] == "w"
    kinds = {l["type"] for l in lines}
    assert {"header", "message", "org", "step"} <= kinds and info["records"] == len(lines)
    assert sum(1 for l in lines if l["type"] == "message") == 4


def test_answer_request_and_strict_parse(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    say(mem, "S1", 4, prefix="杧果", date="2027-06-01")
    mem.end_session("h", "S1")
    mem.run()
    ar = mem.answer_request("杧果的事情", "2027-06-02", tz_label="北京时间")
    body = json.loads(ar["request"]["bytes"])
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "当前日期：2027-06-02 周三（北京时间）" in body["messages"][1]["content"] and body["messages"][1]["content"].startswith("⟪材料开始 ")
    import hashlib
    assert hashlib.sha256(body["messages"][0]["content"].encode()).hexdigest().startswith("cdb99a73")
    ok = AW.parse_answer('{"answer":"好","needs_source":false,"missing_information":[]}')
    assert ok["ok"]
    for raw, code in (('```json\n{}\n```', "A1"), ('{"answer":"a","answer":"b"}', "A1"), ('{"answer":"a"}', "A2"), ('{"answer":"","needs_source":false,"missing_information":[]}', "A3")):
        r = AW.parse_answer(raw)
        assert not r["ok"] and r["code"] == code
    mem2 = make_mem(tmp_path, name="m2.sqlite", provider=Scripted(lambda req, n: "不是 JSON", usage=False))
    mem2.record("h", "S1", "x", "user", "杧果")
    mem2.end_session("h", "S1")
    mem2.provider = Scripted(lambda req, n: "不是 JSON", usage=False)
    r = mem2.ask("杧果", "2027-06-02")
    assert r["ok"] is False and r["code"] == "A1" and r["raw"] == "不是 JSON"       # 格式不合格照实返回，不悄悄重试
    with pytest.raises(ValueError):
        AW.answer_date_text("2027-13-40")


def test_embeddings_response_validation():
    good = {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
    assert VX.parse_embeddings_response(good, 2, 2) == [[1.0, 0.0], [0.0, 1.0]]
    bads = [{"data": [{"index": 0, "embedding": [1, 0]}]},                                       # 条数不对
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},     # index 重复
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 0]}]},     # 零向量
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [float("nan"), 1]}]},
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1, 0, 0]}]}]   # 维度不对
    for b in bads:
        with pytest.raises(ProviderError):
            VX.parse_embeddings_response(b, 2, 2)


def test_vector_identity_must_be_complete():
    ident = {"provider": "t", "model": "m", "model_file_sha256": "x", "runtime_version": "r", "context_length": 1, "dimension": 2, "normalization": "l2", "prefix": "none",
             "template": "none", "text_processing": "verbatim"}
    VX.identity_fp(ident)
    for k in ident:
        bad = dict(ident, **{k: VX.UNKNOWN})
        with pytest.raises(Exception):
            VX.identity_fp(bad)
    with pytest.raises(Exception):
        VX.identity_fp({k: v for k, v in ident.items() if k != "prefix"})


def test_config_validation_and_toml(tmp_path):
    with pytest.raises(ConfigError):
        Config(write_prompt="Z").validate()
    with pytest.raises(ConfigError):
        Config(retrieval="semantic").validate()
    p = tmp_path / "c.toml"
    p.write_text('write_prompt = "A"\nretrieval = "keyword"\nidle_minutes = 10\n[light]\nmax_threads = 5\n', encoding="utf-8")
    c = Config.load(str(p))
    assert c.write_prompt == "A" and c.idle_minutes == 10 and c.light.max_threads == 5
    assert Config().write_prompt == "B0"                                 # 默认 B0


def test_cli_end_to_end_without_provider_calls(tmp_path):
    db = str(tmp_path / "c.sqlite")
    base = [sys.executable, "-B", "-m", "yuanwei", "--db", db, "--workspace", "proj"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))

    def run(*a, ok=True):
        r = subprocess.run(base + list(a), capture_output=True, text=True, env=env, timeout=60)
        if ok:
            assert r.returncode == 0, r.stderr
        return r
    out = json.loads(run("record", "--host", "cli", "--session", "S1", "--message-id", "m1", "--role", "user", "--text", "命令行记的一句").stdout)
    assert out["raw_saved"] and out["seq"] == 1
    assert json.loads(run("end-session", "--host", "cli", "--session", "S1").stdout)["sealed"] == 1
    st = json.loads(run("status").stdout)
    assert st["backlog"]["pending_steps"] == 1 and st["workspace"] == "proj"
    bad = run("get-messages", "--id", "nope", ok=False)
    assert bad.returncode == 3 and "not_found" in bad.stderr
    abbrev = run("stat", ok=False)
    assert abbrev.returncode != 0                                         # 命令不许缩写
