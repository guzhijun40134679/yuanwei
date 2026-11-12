"""费用账分币种（v12 加 currency 列）：DeepSeek 人民币、gpt-6-luna 美元，不混加；没有价格表的模型只记用量、不估价、不暂停、不报假数。"""
import sqlite3

import pytest

from helpers import Clock, Scripted, make_mem, say
from yuanwei import Config, Memory
from yuanwei import catalog as CT
from yuanwei import sqlstore as SQ
from yuanwei.ask import RetryableNetworkError


def mk(tmp_path, model, call, clock=None, spawn="off", **cfg):
    """配置里的整理模型是 model；call 是假提供方（可调用的）。"""
    p = {"base_url": "http://127.0.0.1:1/v1", "api_key_env": None, "timeout": 5, "params": {"model": model}}
    return Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(provider=p, **cfg), provider=call, clock=clock or Clock(), spawn=spawn)


def usage_provider(usage, model="x"):
    base = Scripted("skip", usage=False)

    def call(request):
        raw = base(request)
        return {"text": raw, "model": model, "fingerprint": "fp", "usage": usage}
    call.calls = base.calls
    return call


def run_one(tmp_path, model, usage, **cfg):
    mem = mk(tmp_path, model, usage_provider(usage, model), **cfg)
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.run()
    return mem


def test_ledger_gets_currency_column_v12_and_old_rows_read_as_cny(tmp_path, monkeypatch):
    p = str(tmp_path / "old.db")
    monkeypatch.setattr(SQ, "SCHEMA_VERSION", 11)
    s = SQ.Store(p)
    assert "currency" not in [r[1] for r in s.conn.execute("PRAGMA table_info(cost_ledger)")]
    s.conn.execute("INSERT INTO cost_ledger (ns,step_id,ts,day,model,fingerprint,usage,cny,unknown,note) VALUES ('w',NULL,'2027-01-01T00:00:00Z','2027-01-01','m',NULL,NULL,1.25,0,NULL)")
    s.conn.close()
    monkeypatch.undo()
    s2 = SQ.Store(p)
    assert s2.schema_version() == 12
    assert tuple(s2.conn.execute("SELECT currency, cny FROM cost_ledger").fetchone()) == ("CNY", 1.25)
    assert s2.cost_sum("w")["cny"] == 1.25


def test_deepseek_cost_rows_are_cny_and_luna_rows_are_usd_and_never_summed_together(tmp_path):
    usage = {"prompt_tokens": 1000, "prompt_cache_miss_tokens": 1000, "prompt_cache_hit_tokens": 0, "completion_tokens": 1000}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    m1 = run_one(tmp_path / "a", "deepseek-flash", usage)
    d = m1.store.cost_sum("w")
    assert set(d["by_currency"]) == {"CNY"} and d["by_currency"]["CNY"]["amount"] > 0 and d["cny"] == d["by_currency"]["CNY"]["amount"]
    m3 = run_one(tmp_path / "b", "gpt-6-luna", {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 200}, "completion_tokens": 1000})
    d3 = m3.store.cost_sum("w")
    assert set(d3["by_currency"]) == {"USD"} and d3["cny"] == 0
    expect = (800 * 0.10 + 200 * 0.01 + 1000 * 0.50) / 1e6
    assert d3["by_currency"]["USD"]["amount"] == pytest.approx(expect * d3["by_currency"]["USD"]["calls"])
    # 同一个库里两种币种：分开列，cny 只加人民币
    m3.store.add_cost("w", None, m3.clock(), m3.clock()[:10], "deepseek-flash", None, None, 7.0, False)
    d4 = m3.store.cost_sum("w")
    assert d4["cny"] == 7.0 and set(d4["by_currency"]) == {"CNY", "USD"} and d4["by_currency"]["CNY"]["amount"] == 7.0


def test_cost_sum_legacy_keys_keep_meaning_cny_only(tmp_path):
    s = SQ.Store(str(tmp_path / "s.db"))
    s.add_cost("w", None, "2027-01-01T00:00:00Z", "2027-01-01", "m", None, None, 2.0, False)
    s.add_cost("w", None, "2027-01-01T00:00:00Z", "2027-01-01", "m", None, None, 9.0, False, currency="USD")
    d = s.cost_sum("w")
    assert d["cny"] == 2.0 and d["calls"] == 2 and d["unknown_calls"] == 0


def test_unpriced_model_records_real_token_counts_with_zero_amount_and_no_invented_number(tmp_path):
    mem = run_one(tmp_path, "some-new-model", {"prompt_tokens": 1234, "completion_tokens": 56})
    rows = mem.store.cost_rows("w")
    assert rows and all(r["currency"] == "NONE" and r["cny"] == 0.0 for r in rows)
    d = mem.store.cost_sum("w")
    n = d["by_currency"]["NONE"]
    assert n["tokens_in"] == 1234 * len(rows) and n["tokens_out"] == 56 * len(rows) and "amount" not in n and d["cny"] == 0
    st = mem.status()
    assert st["organizer"]["cost"]["price_table"] is False and "没有这个模型的价格表" in st["organizer"]["cost"]["note"]


def test_unpriced_model_network_failure_records_unknown_without_pricing(tmp_path):
    def boom(request):
        raise RetryableNetworkError("timeout")
    mem = mk(tmp_path, "some-new-model", boom)
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.run()
    rows = mem.store.cost_rows("w")
    assert rows and all(r["currency"] == "NONE" and r["unknown"] == 1 and r["cny"] == 0.0 and "不估价" in r["note"] for r in rows)


def test_unpriced_model_never_pauses_or_blocks_even_when_cap_is_set(tmp_path):
    prov = usage_provider({"prompt_tokens": 10, "completion_tokens": 5}, "some-new-model")
    mem = mk(tmp_path, "some-new-model", prov, spawn="inline", daily_cap_cny=0.000001)
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    r = mem.run()
    assert r["status"] != "paused" and prov.calls, r
    assert mem.pause_state() is None and mem.worst_send_cny(1000) is None


def test_status_says_budget_not_enforced_for_unpriced_model_and_wizard_warns(tmp_path, capsys):
    from helpers_wizard import FAKE_KEY, FakeSystem, make_home
    from yuanwei import cli
    mem = mk(tmp_path, "some-new-model", Scripted("skip"), daily_cap_cny=5.0)
    org = mem.status()["organizer"]
    assert org["cost"]["budget_enforced"] is False and "不起作用" in org["cost"]["note"]
    h = make_home(tmp_path, "hh")
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "custom", "--provider", "moonshot", "--provider-model", "m", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x"], sys_=FakeSystem()) == 0
    lines = "\n".join(__import__("json").loads(capsys.readouterr().out)["plan"]["lines"])
    assert "没有价格表" in lines and "预算上限" in lines and "不准" in lines


def test_daily_cap_cny_only_counts_cny_rows(tmp_path):
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted("skip"), clock=clock, daily_cap_cny=1.0, spawn="off")
    mem.store.add_cost("w", None, clock(), clock()[:10], "m", None, None, 100.0, False, currency="USD")
    mem.store.add_cost("w", None, clock(), clock()[:10], "m", None, None, 100.0, False, currency="NONE")
    assert mem.store.cost_sum("w", clock()[:10])["cny"] == 0
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    assert mem.check_and_spawn()["action"] != "none" or mem.pause_state() is None


def test_existing_deepseek_cost_numbers_unchanged_after_the_change(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), spawn="off")           # 老写法：配置里没有指定模型
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    mem.run()
    rows = mem.store.cost_rows("w")
    assert rows and all(r["currency"] == "CNY" and r["cny"] == pytest.approx((1000 * 2.0 + 1000 * 8.0) / 1e6) for r in rows)
    assert mem.worst_send_cny(1000) == pytest.approx((1000 * 2.0 + 2 * 65536 * 8.0) / 1e6)


def test_cost_ledger_currency_column_is_in_the_delete_trace_inventory():
    import delete_trace_inventory as D
    assert "currency" in D.TABLES["cost_ledger"]["columns"]
