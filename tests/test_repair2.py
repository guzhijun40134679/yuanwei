"""真机试装修补（二）（2026-11-11）：①插件在 initialize 之前也要交完整的工具清单（Hermes 的 add_provider 在 initialize 之前读它）；②整理的费用账要记上、status 要看得到；③向导计划的措辞和更新文件先备份。"""
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import out_skip, sids_of
from helpers_wizard import FAKE_KEY, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import Config, Memory
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei.hermes_adapter import ALL_TOOLS, CONTROL_TOOLS, static_tool_schemas

SRC = str(Path(__file__).resolve().parent.parent / "src")


# ================================================================== 一　按 Hermes 真实的顺序：add_provider → initialize_for_agent → has_tool → handle_tool_call
class MiniMemoryManager:
    """照 Hermes agent/memory_manager.py 的逻辑一模一样地仿（本机 Hermes 的 MemoryManager 要一串 Hermes 内部依赖才导得进来，测试里不导）：
    add_provider 在 initialize **之前**调 provider.get_tool_schemas()，把工具名写进 _tool_to_provider，以后不再刷新（:283、:322-333）；has_tool 只查这张表（:589-590）；
    handle_tool_call 按这张表路由（:592-614）；initialize_for_agent → initialize_all → provider.initialize（:911-935）。"""

    def __init__(self):
        self._providers, self._tool_to_provider = [], {}

    def add_provider(self, provider):
        schemas = list(provider.get_tool_schemas())
        self._providers.append(provider)
        for sc in schemas:
            self._tool_to_provider.setdefault(sc["name"], provider)

    def initialize_for_agent(self, **kwargs):
        sid = kwargs.pop("session_id")
        for p in self._providers:
            p.initialize(session_id=sid, **kwargs)

    def has_tool(self, name):
        return name in self._tool_to_provider

    def handle_tool_call(self, name, args, **kw):
        p = self._tool_to_provider.get(name)
        if p is None:
            return json.dumps({"error": "No memory provider handles tool '%s'" % name})        # 对应 Hermes 的 Unknown tool
        return p.handle_tool_call(name, args, **kw)


def _home_with_config(tmp_path, monkeypatch, src_path=SRC):
    home = tmp_path / "hh"
    (home / "yuanwei").mkdir(parents=True)
    (home / "yuanwei" / "config.json").write_text(json.dumps({"hermes_home": str(home), "db": str(tmp_path / "m.sqlite"), "src_path": src_path, "spawn": "off", "mode": "tools_only"}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _load_plugin_rich(tmp_path):
    """和 test_deletion_chat._load_plugin 一样，但假基类带上 handle_tool_call 等真实基类有的方法（不然适配器按功能探测会把「读取工具」判成关着）。"""
    import abc
    import importlib.util
    import types
    base = types.ModuleType("agent.memory_provider")

    class MemoryProvider(abc.ABC):
        @property
        @abc.abstractmethod
        def name(self): ...
        @abc.abstractmethod
        def is_available(self): ...
        @abc.abstractmethod
        def initialize(self, session_id, **kw): ...
        @abc.abstractmethod
        def get_tool_schemas(self): ...
        def handle_tool_call(self, tool_name, args, **kwargs): ...
        def prefetch(self, query, *, session_id=""): ...
        def on_turn_start(self, turn_number, message, **kwargs): ...
        def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs): ...
        def on_session_end(self, messages): ...
        def shutdown(self): ...
        def system_prompt_block(self): ...
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None): ...
    base.MemoryProvider = MemoryProvider
    saved = {k: sys.modules.get(k) for k in ("agent", "agent.memory_provider")}
    sys.modules["agent"], sys.modules["agent.memory_provider"] = types.ModuleType("agent"), base
    try:
        f = tmp_path / "plugin_init.py"
        f.write_text(HS.plugin_init_text(), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("plug_rich", str(f))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _provider(tmp_path):
    return _load_plugin_rich(tmp_path).YuanweiMemoryProvider()


def test_in_hermes_order_the_tools_are_routable_after_initialize_and_calls_reach_the_adapter(tmp_path, monkeypatch):
    home = _home_with_config(tmp_path, monkeypatch)
    p = _provider(tmp_path)
    mgr = MiniMemoryManager()
    mgr.add_provider(p)                                                                              # initialize 之前
    assert all(mgr.has_tool(n) for n in list(ALL_TOOLS) + list(CONTROL_TOOLS)) and mgr.has_tool("recall")
    mgr.initialize_for_agent(session_id="s1", platform="cli", hermes_home=str(home), agent_context="primary", cwd=None)
    assert mgr.has_tool("recall")
    out = json.loads(mgr.handle_tool_call("recall", {"query": "咖喱"}))
    assert out.get("reason") != "not_initialized" and "error" not in out and "status" in out         # 走到了适配器


def test_tool_names_are_the_same_before_and_after_initialize(tmp_path, monkeypatch):
    home = _home_with_config(tmp_path, monkeypatch)
    p = _provider(tmp_path)
    before = p.get_tool_schemas()
    p.initialize("s1", platform="cli", hermes_home=str(home), agent_context="primary")
    after = p.get_tool_schemas()
    assert [s["name"] for s in before] == [s["name"] for s in after] and before == after == static_tool_schemas()
    assert {s["name"] for s in before} == set(ALL_TOOLS) | set(CONTROL_TOOLS) and "recall" in {s["name"] for s in before}


def test_handle_tool_call_before_initialize_still_says_not_initialized(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch)
    p = _provider(tmp_path)
    r = json.loads(p.handle_tool_call("recall", {"query": "x"}))
    assert r["status"] == "error" and r["reason"] == "not_initialized"


def test_when_the_static_definitions_cannot_be_imported_the_list_is_empty_and_the_reason_is_kept(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch, src_path="/nonexistent/src")
    monkeypatch.setitem(sys.modules, "yuanwei.hermes_adapter", None)                                  # 导入失败
    p = _provider(tmp_path)
    assert p.get_tool_schemas() == []
    st = p.status()
    assert "yuanwei.hermes_adapter" in st["tool_schema_error"] and st["active"] is False
    monkeypatch.delitem(sys.modules, "yuanwei.hermes_adapter")
    assert [s["name"] for s in p.get_tool_schemas()] == [s["name"] for s in static_tool_schemas()] and p.status()["tool_schema_error"] is None   # 以后能导了就恢复


def test_the_adapter_returns_nothing_only_when_inactive_or_tools_off_so_names_may_differ_only_then(tmp_path, monkeypatch):
    home = _home_with_config(tmp_path, monkeypatch)
    p = _provider(tmp_path)
    p.initialize("s1", platform="cli", hermes_home=str(home), agent_context="subagent")               # 子代理：适配器不活跃
    assert p.get_tool_schemas() == [] and p._a.active is False                                         # 登记时 Hermes 已经按完整清单记过名字；调用时适配器回「没有启用」
    r = json.loads(p.handle_tool_call("recall", {"query": "x"}))
    assert r["reason"] == "inactive"


_HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_SRC") or Path.home() / ".hermes" / "hermes-agent")          # 在导入时取（测试里的 HOME 会被换成临时目录）


@pytest.mark.skipif(not (_HERMES_ROOT / "agent" / "memory_manager.py").exists(), reason="本机没有 Hermes 源码")
def test_the_real_hermes_memory_manager_still_works_the_way_the_emulation_assumes():
    root = _HERMES_ROOT
    text = (root / "agent" / "memory_manager.py").read_text(encoding="utf-8")
    a = text.index("def add_provider")
    body = text[a:text.index("def ", a + 20)]
    assert body.index("schemas = list(provider.get_tool_schemas())") < body.index("self._providers.append(provider)")        # 先读清单（initialize 之前），再登记
    assert "self._tool_to_provider[tool_name] = provider" in body
    assert "return tool_name in self._tool_to_provider" in text[text.index("def has_tool"):text.index("def has_tool") + 200]


# ================================================================== 二　费用账
class FakeUpstream:
    """本机回环上的假 DeepSeek：回应里带 DeepSeek 那样的 usage。"""

    def __init__(self, usage=None):
        import http.server
        outer = self
        self.calls = 0
        self.usage = usage if usage is not None else {"prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300, "prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 1500,
                                                      "prompt_tokens_details": {"cached_tokens": 500}, "completion_tokens_details": {"reasoning_tokens": 100}}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls += 1
                out = {"model": "deepseek-v4-flash", "system_fingerprint": "fp-1", "choices": [{"message": {"content": out_skip(sids_of(b["messages"][-1]["content"]))}, "finish_reason": "stop"}]}
                if outer.usage:
                    out["usage"] = outer.usage
                d = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _install_light(tmp_path, up):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC], sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    tp = os.path.join(h, "yuanwei", "yuanwei.toml")
    import re
    text = re.sub(r'base_url = ".*?"', 'base_url = "%s"' % up.url, open(tp).read())
    open(tp, "w").write(text)
    return h, json.load(open(os.path.join(h, "yuanwei", "config.json")))


def _drive_hermes_path(h, cfg, wait=40):
    """Hermes 这条路：几轮对话 → on_session_end（end_session）→ 下一个会话的第一轮（record 才会拉起子进程 run）。"""
    from yuanwei.hermes_adapter import HermesAdapter
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY                                                         # 子进程继承（Hermes 会把 .env 读进环境）
    a = HermesAdapter(cfg)
    a.initialize("S1", platform="cli", agent_context="primary", hermes_home=h, cwd="/x")
    for n, (u, r) in enumerate([("今天天气不错", "是的"), ("明天去爬山", "好主意"), ("带点水", "记得")], 1):
        a.on_turn_start(n, u, author_id=None, author_name=None, author_is_bot=False)
        a.sync_turn(u, r, session_id="S1", messages=[{"role": "user", "content": u}, {"role": "assistant", "content": r}])
    a.drain(5)
    a.on_session_end([])
    a.drain(5)
    a.on_session_switch("S2", parent_session_id="S1", reset=True, reason="new_session")
    a.on_turn_start(1, "新的一句话", author_id=None, author_name=None, author_is_bot=False)
    a.sync_turn("新的一句话", "嗯", session_id="S2", messages=[{"role": "user", "content": "新的一句话"}, {"role": "assistant", "content": "嗯"}])
    a.drain(5)
    for _ in range(wait):
        time.sleep(1)
        c = sqlite3.connect(cfg["db"])
        steps = [tuple(r) for r in c.execute("SELECT step_id, status FROM steps")]
        c.close()
        if steps and all(s[1] in ("done", "skipped", "failed") for s in steps):
            break
    a.shutdown()
    return steps


def test_hermes_path_end_session_then_background_run_records_the_cost_and_status_shows_today_and_total(tmp_path, capsys):
    up = FakeUpstream()
    try:
        h, cfg = _install_light(tmp_path, up)
        steps = _drive_hermes_path(h, cfg)
    finally:
        up.close()
        os.environ.pop("DEEPSEEK_API_KEY", None)
    assert steps and all(s[1] == "done" for s in steps) and up.calls >= 1
    c = sqlite3.connect(cfg["db"])
    rows = c.execute("SELECT currency, cny, unknown, model, usage FROM cost_ledger").fetchall()
    assert len(rows) == up.calls and all(r[0] == "CNY" and r[1] > 0 and r[2] == 0 for r in rows)
    assert json.loads(rows[0][4])["completion_tokens"] == 300
    c.close()
    st = HS.install_status(h)["cost"]
    assert st["day_utc"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert st["today"]["by_currency"]["CNY"]["calls"] == up.calls and st["total"]["by_currency"]["CNY"]["amount"] > 0 and st["total"]["by_currency"]["CNY"]["amount"] == st["today"]["by_currency"]["CNY"]["amount"]
    assert st["unrecorded_calls"] == 0 and st["pending_rows"] == 0
    assert cli.main(["status", "hermes", "--hermes-home", h]) == 0
    assert "unrecorded_calls" in capsys.readouterr().out


def test_a_response_without_usage_is_still_booked_as_an_unknown_worst_case_row(tmp_path):
    up = FakeUpstream(usage={})
    try:
        h, cfg = _install_light(tmp_path, up)
        _drive_hermes_path(h, cfg)
    finally:
        up.close()
        os.environ.pop("DEEPSEEK_API_KEY", None)
    st = HS.install_status(h)["cost"]
    assert st["total"]["calls"] >= 1 and st["total"]["unknown_calls"] == st["total"]["calls"] and st["unrecorded_calls"] == 0


def test_status_cost_with_nothing_recorded_says_so_and_counts_calls_that_were_not_booked(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC], sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    mem = Memory.open(json.load(open(os.path.join(h, "yuanwei", "config.json")))["db"], "life", config=Config(index_version=2), spawn="off")
    mem.close()
    st = HS.install_status(h)["cost"]
    assert st["total"] == {"calls": 0, "unknown_calls": 0, "by_currency": {}} and "还没有记录" in st["note"] and st["unrecorded_calls"] == 0
    db = json.load(open(os.path.join(h, "yuanwei", "config.json")))["db"]
    c = sqlite3.connect(db)                                                                          # 造「收到过回应、账里却没有」：一行判断记录有 raw_output，费用账没有这一步
    c.execute("INSERT INTO judgments VALUES ('life','step-0001','q1',1,?)", (json.dumps({"raw_output": "{}", "status": "validated"}),))
    c.commit()
    c.close()
    assert HS.install_status(h)["cost"]["unrecorded_calls"] == 1                                       # 这类缺口以后一眼能看到


def test_a_cost_write_that_fails_is_kept_beside_the_db_and_booked_later_never_lost(tmp_path, monkeypatch):
    from helpers import Clock, Scripted, say
    from yuanwei import sqlstore as SQ
    import sqlite3 as _sq
    p = {"base_url": "http://127.0.0.1:1/v1", "api_key_env": None, "timeout": 5, "params": {"model": "deepseek-flash"}}
    base = Scripted("skip", usage=False)

    def call(request):
        return {"text": base(request), "model": "m", "fingerprint": "f", "usage": {"prompt_cache_miss_tokens": 1000, "prompt_cache_hit_tokens": 0, "completion_tokens": 1000}}
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(provider=p, index_version=2), provider=call, clock=Clock(), spawn="off")
    say(mem, "S1", 2)
    mem.end_session("h", "S1")
    real = SQ.Store.add_cost
    state = {"n": 0}

    def flaky(self, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real(self, *a, **k)
    monkeypatch.setattr(SQ.Store, "add_cost", flaky)
    r = mem.run()
    assert r["status"] == "done" and r["done"] == 1                                                    # 整理本身不因为记账失败而作废
    assert mem.cost_pending_count() == 1 and mem.store.cost_rows("w") == []                              # 账暂存在库旁边的文件里，没丢
    mem.run()                                                                                          # 下一次运行一开始补进库
    assert mem.cost_pending_count() == 0 and len(mem.store.cost_rows("w")) == 1 and not os.path.exists(mem._cost_pending_path())


def test_pending_cost_rows_are_flushed_at_the_start_of_the_next_run_and_shown_in_status(tmp_path, monkeypatch):
    from helpers import Clock, Scripted
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(index_version=2), provider=Scripted("skip"), clock=Clock(), spawn="off")
    row = dict(ns="w", step_id="step-0001", ts="2027-01-01T08:00:00Z", day="2027-01-01", model="m", fingerprint=None, usage={"completion_tokens": 5}, cny=0.25, unknown=False, note=None, currency="CNY")
    with open(mem._cost_pending_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n" + "not json\n")
    assert mem.cost_pending_count() == 2
    from yuanwei.sqlstore import Store
    st = HS.cost_status(Store.open_readonly(mem.db_path), mem.db_path)
    assert st["pending_rows"] == 2
    mem.run()
    assert [r["cny"] for r in mem.store.cost_rows("w")] == [0.25] and not os.path.exists(mem._cost_pending_path())


# ================================================================== 三　向导计划的措辞；更新文件先备份
def _tpl(monkeypatch, extra):
    old = HS.plugin_init_text()
    monkeypatch.setattr(HS, "plugin_init_text", lambda: old + extra)


def test_plan_separates_new_files_from_updated_ones_and_rerun_updates_an_unmodified_plugin_file(tmp_path, monkeypatch, capsys):
    h = make_home(tmp_path)
    args = ["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC]
    sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
    assert cli._setup_main(args, sys_=sysx) == 0
    first = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert " 新放的文件  " in first and "plugins/yuanwei/__init__.py" in first and "更新的文件" not in first          # 第一次：全是新放
    assert cli._setup_main(args + ["--yes"], sys_=sysx) == 0
    capsys.readouterr()
    assert cli._setup_main(args, sys_=sysx) == 0                                                           # 第二遍：模板没变，没有任何文件要放
    again = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "新放的文件  （没有）" in again and "更新的文件" not in again
    os.chmod(os.path.join(h, "plugins", "yuanwei", "__init__.py"), 0o600)                                    # 权限不是默认的，才看得出备份有没有照原样
    old_text = open(os.path.join(h, "plugins", "yuanwei", "__init__.py")).read()
    old_mode = stat.S_IMODE(os.stat(os.path.join(h, "plugins", "yuanwei", "__init__.py")).st_mode)
    _tpl(monkeypatch, "\n# 新模板\n")                                                                       # 插件模板变了，已装的文件还是旧模板（用户没改过）
    assert cli._setup_main(args, sys_=sysx) == 0
    third = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "更新的文件" in third and "plugins/yuanwei/__init__.py" in third and "先各备份一份" in third and "新放的文件  （没有）" in third
    assert cli._setup_main(args + ["--yes"], sys_=sysx) == 0
    capsys.readouterr()
    cur = open(os.path.join(h, "plugins", "yuanwei", "__init__.py")).read()
    assert cur.endswith("# 新模板\n")
    backs = [n for n in os.listdir(os.path.join(h, "plugins", "yuanwei")) if n.startswith("__init__.py.yuanwei-backup-")]
    assert len(backs) == 1 and open(os.path.join(h, "plugins", "yuanwei", backs[0])).read() == old_text
    assert stat.S_IMODE(os.stat(os.path.join(h, "plugins", "yuanwei", backs[0])).st_mode) == old_mode        # 备份保留原权限


def test_a_plugin_file_the_user_changed_is_still_refused_not_overwritten(tmp_path, monkeypatch, capsys):
    h = make_home(tmp_path)
    args = ["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC]
    sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
    assert cli._setup_main(args + ["--yes"], sys_=sysx) == 0
    capsys.readouterr()
    p = os.path.join(h, "plugins", "yuanwei", "__init__.py")
    open(p, "a").write("\n# 用户自己改的\n")
    _tpl(monkeypatch, "\n# 新模板\n")
    assert cli._setup_main(args + ["--yes"], sys_=sysx) == 2
    assert "plugin_file_modified" in capsys.readouterr().err and open(p).read().endswith("# 用户自己改的\n")


def test_new_install_makes_no_backup_of_plugin_files(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h, "--config-method", "yaml", "--src-path", SRC], sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    assert not [n for n in os.listdir(os.path.join(h, "plugins", "yuanwei")) if "backup" in n]
