"""真机试装修补（一）甲（2026-11-11）：本机会话没给工作目录时怎么认。
Hermes 的 agent/agent_init.py `_memory_provider_init_kwargs`：只有 agent.session_cwd 有值才传 cwd；经典终端（cli）建 agent 时不给；桌面版聊天窗口平台是 desktop、
hermes --tui 和桌面版里的终端是 tui（tui_gateway/agent_factory.py _resolve_session_platform），这两个也是本机、主人是电脑的主人。
规矩：给了 cwd 的，项目判定不变；没给——cli 取 Hermes 进程的目录；tui、desktop、local 和其他平台记进「生活」；目录是 "/"、取不到、不是绝对路径：记进「生活」。"""
import ast
import json
import os
import sys
import types
from contextlib import suppress
from pathlib import Path

import pytest

from conftest import REAL_LOCAL_PROCESS_CWD
from test_hermes_workspace import World, call, msgs, rows, turn
from yuanwei import hermes_workspace as HW


# ---------------------------------------------------------------- 表：平台 × 有没有 cwd × 进程目录
class Boom(Exception):
    pass


def _proc(value):
    def f():
        return value
    return f


def _must_not_ask():
    raise Boom("这个平台不该去问进程目录")


# (平台, 给的 cwd, 进程目录, 期望的工作区键, 期望的依据)
TABLE = [
    ("cli", "/work/a/x", "/work/b", "proj:p-a", "cwd_given"),                                  # 给了 cwd：照旧，进程目录不看
    ("cli", "/elsewhere", "/work/b", "life", "cwd_given"),
    ("cli", None, "/work/a", "proj:p-a", "cwd_from_process"),                                  # 经典终端没给：取进程目录
    ("cli", None, "/work/b/sub", "proj:p-b", "cwd_from_process"),
    ("cli", None, "/elsewhere", "life", "cwd_from_process"),
    ("cli", None, "/", "life", "no_cwd_life"),                                                 # 根目录不是任何人的项目目录
    ("cli", None, None, "life", "no_cwd_life"),                                                # 取不到
    ("cli", None, "work/a", "life", "no_cwd_life"),                                            # 不是绝对路径
    ("cli", None, "", "life", "no_cwd_life"),
    ("tui", "/work/a", "/work/b", "proj:p-a", "cwd_given"),
    ("tui", None, "/work/a", "life", "no_cwd_life"),                                           # tui、desktop 没给：生活（进程目录不是用户的项目目录）
    ("desktop", "/work/b/x", "/work/a", "proj:p-b", "cwd_given"),
    ("desktop", None, "/work/a", "life", "no_cwd_life"),
    ("local", None, "/work/a", "life", "no_cwd_life"),                                         # 其他本机平台：生活
    ("telegram", None, "/work/a", "life", "no_cwd_life"),                                      # 网关平台没给：生活（本来就是）
    ("telegram", "/work/a", "/work/b", "proj:p-a", "cwd_given"),
]


@pytest.mark.parametrize("platform,cwd,proc,key,why", TABLE)
def test_workspace_table_platform_by_cwd(tmp_path, platform, cwd, proc, key, why):
    w = World(tmp_path, seed=False)
    r = HW.resolve(str(w.home), cwd, platform, process_cwd=_proc(proc))
    assert (r["key"], r["reason"]) == (key, why)


@pytest.mark.parametrize("platform", ["tui", "desktop", "local", "telegram", "acp", "api_server", "webhook"])
def test_only_cli_ever_asks_for_the_process_directory(tmp_path, platform):
    w = World(tmp_path, seed=False)
    r = HW.resolve(str(w.home), None, platform, process_cwd=_must_not_ask)                     # 问了就 Boom
    assert r["key"] == "life" and r["reason"] == "no_cwd_life"


def test_cwd_given_never_asks_for_the_process_directory(tmp_path):
    w = World(tmp_path, seed=False)
    assert HW.resolve(str(w.home), "/work/a", "cli", process_cwd=_must_not_ask)["key"] == "proj:p-a"


def test_a_given_but_bad_cwd_is_still_unresolved_not_life(tmp_path):
    w = World(tmp_path, seed=False)                                                            # 「给了的」不合格：说不清，不拿进程目录顶上，也不落进生活
    for bad, why in (("relative/x", "cwd_not_absolute"), ("   ", "cwd_invalid"), (5, "cwd_invalid")):
        r = HW.resolve(str(w.home), bad, "cli", process_cwd=_proc("/work/a"))
        assert r["key"] is None and r["reason"] == why


def test_archived_project_from_the_process_directory_is_still_unresolved(tmp_path):
    from test_hermes_workspace import make_home
    home = make_home(tmp_path, [("p-old", "旧", ["/work/a"], 1)])
    r = HW.resolve(str(home), None, "cli", process_cwd=_proc("/work/a/x"))
    assert r["key"] is None and r["reason"] == "project_archived"                              # 判定规则一个字不改：归档的管着，说不清


def test_no_projects_db_gives_life_with_the_basis(tmp_path):
    from test_hermes_workspace import make_home
    home = make_home(tmp_path)
    assert HW.resolve(str(home), None, "cli", process_cwd=_proc("/work/a"))["reason"] == "cwd_from_process"
    assert HW.resolve(str(home), None, "desktop", process_cwd=_must_not_ask)["reason"] == "no_cwd_life"
    assert HW.resolve(str(home), "/x", "cli")["reason"] == "cwd_given"


# ---------------------------------------------------------------- 进程目录怎么取
def test_process_cwd_prefers_hermes_own_resolver_and_falls_back_to_getcwd(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: "/proc/dir")
    assert REAL_LOCAL_PROCESS_CWD() == "/proc/dir"                                             # 不在 Hermes 里：进程目录
    fake = types.ModuleType("agent.runtime_cwd")
    fake.resolve_agent_cwd = lambda: Path("/configured/dir")
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.runtime_cwd", fake)
    assert REAL_LOCAL_PROCESS_CWD() == "/configured/dir"                                       # 在 Hermes 里：用它自己的说法
    fake.resolve_agent_cwd = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert REAL_LOCAL_PROCESS_CWD() == "/proc/dir"                                             # 它出错：退回进程目录，不当成功也不崩
    monkeypatch.setattr(os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError("gone")))
    assert REAL_LOCAL_PROCESS_CWD() is None                                                    # 都取不到：None（resolve 当「没有可信目录」→生活）


# ---------------------------------------------------------------- 适配器：主人、记进哪里、status 的依据
@pytest.mark.parametrize("platform,cwd,proc,key,why", [t for t in TABLE if t[0] in ("cli", "tui", "desktop")])
def test_adapter_records_into_the_right_workspace_and_status_says_why(tmp_path, monkeypatch, platform, cwd, proc, key, why):
    monkeypatch.setattr(HW, "local_process_cwd", _proc(proc))
    w = World(tmp_path, seed=False)
    a = w.adapter(cwd, platform=platform)
    st = a.status()
    assert st["workspace"]["key"] == key and st["workspace"]["reason"] == why
    text = "写入测试 %s %s" % (platform, key)
    turn(a, 1, text)
    a.sync_turn(text, "答", session_id="s1", messages=msgs(text))
    assert a.status()["recorded_turns"] == 1 and a.owner_basis == "local_session"            # tui、desktop 和 cli 一样：本机会话＝主人
    assert [t for r, t in rows(w.mem(key)) if r == "user"] == [text]


def test_a_gateway_platform_is_still_not_the_owner_unless_configured(tmp_path):
    w = World(tmp_path, seed=False)
    a = w.adapter(None, platform="telegram")
    turn(a, 1, "手机上的话")
    a.sync_turn("手机上的话", "答", session_id="s1", messages=msgs("手机上的话"))
    st = a.status()
    assert st["recorded_turns"] == 0 and st["skipped"].get("owner_unknown") == 1               # 没配主人：不记（网关平台的规矩不变）
    assert st["workspace"]["reason"] == "no_cwd_life"


def test_unresolved_still_refuses_tools_and_writes_with_the_reason(tmp_path):
    w = World(tmp_path)
    a = w.adapter("relative/x", platform="desktop")                                            # 给了但不合格：说不清，工具拒绝并写原因
    turn(a, 1, "普通")
    r = call(a, "recall", {"query": "咖喱"})
    assert r["status"] == "refused" and r["reason"] == "workspace_unresolved" and "cwd_not_absolute" in r["text"]
    a.sync_turn("普通", "答", session_id="s1", messages=msgs("普通"))
    assert a.status()["skipped"].get("workspace_unresolved") == 1


def test_read_scope_isolated_means_a_project_chat_without_cwd_only_sees_life(tmp_path):
    w = World(tmp_path, scope="isolated")                                                      # 文档里写明的后果：甲 + 没给目录的项目对话 = 生活
    a = w.adapter(None, platform="desktop")
    turn(a, 1, "普通")
    assert a.status()["workspace"]["key"] == "life"
    got = call(a, "recall", {"query": "咖喱 发布会"})["text"]
    assert "咖喱" in got and "发布会" not in got


# ---------------------------------------------------------------- 用本机 Hermes 源码里真正的 _memory_provider_init_kwargs 造参数
def _hermes_src():
    root = Path(os.environ.get("HERMES_AGENT_SRC") or (Path.home() / ".hermes" / "hermes-agent"))
    return root if (root / "agent" / "agent_init.py").is_file() and (root / "agent" / "agent_init_params.py").is_file() else None


def _real_kwargs_builder():
    root = _hermes_src()
    if root is None:
        return None
    mod = ast.parse((root / "agent" / "agent_init.py").read_text(encoding="utf-8"))
    fn = next((n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name == "_memory_provider_init_kwargs"), None)
    par = ast.parse((root / "agent" / "agent_init_params.py").read_text(encoding="utf-8"))
    ident = None
    for n in par.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_GATEWAY_IDENTITY_PARAMS" for t in n.targets):
            ident = ast.literal_eval(n.value)
    if fn is None or ident is None:
        return None
    fn.decorator_list = []
    ns = {"Any": object, "suppress": suppress, "get_hermes_home": lambda: Path("/unused"), "_GATEWAY_IDENTITY_PARAMS": ident}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "agent_init.py", "exec"), ns)       # noqa: S102  只取这一个函数，不导入 Hermes
    return ns["_memory_provider_init_kwargs"], ident


REAL = _real_kwargs_builder()


class FakeAgent:
    def __init__(self, ident, session_cwd):
        self.session_id, self.session_cwd, self._session_db = "sess-1", session_cwd, None
        self._emit_warning = self._emit_status = lambda *a, **k: None
        for i in ident:
            setattr(self, "_" + i, None)


@pytest.mark.skipif(REAL is None, reason="本机没有 Hermes 源码（~/.hermes/hermes-agent），用 HERMES_AGENT_SRC 指一个也行")
@pytest.mark.parametrize("platform,cwd,proc,key,why", [t for t in TABLE if t[0] in ("cli", "tui", "desktop", "telegram")])
def test_with_kwargs_built_by_the_real_hermes_function(tmp_path, monkeypatch, platform, cwd, proc, key, why):
    build, ident = REAL
    monkeypatch.setattr(HW, "local_process_cwd", _proc(proc))
    kwargs = build(FakeAgent(ident, cwd), platform)
    assert ("cwd" in kwargs) == (cwd is not None)                                               # Hermes 就是这样：有值才传
    w = World(tmp_path, seed=False)
    kwargs["hermes_home"] = str(w.home)
    from yuanwei.hermes_adapter import HermesAdapter
    a = HermesAdapter(dict(w.cfg), executor="inline", memory_factory=w.mem)
    a.initialize(kwargs.pop("session_id"), **kwargs)
    assert a.status()["workspace"]["key"] == key and a.status()["workspace"]["reason"] == why
    assert a.status()["active"] is True


@pytest.mark.skipif(REAL is None, reason="本机没有 Hermes 源码")
def test_the_real_hermes_function_still_leaves_cwd_out_when_session_cwd_is_empty():
    build, ident = REAL
    assert "cwd" not in build(FakeAgent(ident, None), "cli") and build(FakeAgent(ident, "/w"), "cli")["cwd"] == "/w"


# ================================================================== 恢复会话（/resume、--resume）
# Hermes：--resume 在 initialize 之前就把进程目录切到那个会话记的目录（hermes_cli/main.py:1562）；
# 会话中 /resume 的钩子 on_session_switch(reason="resume") 在 _restore_session_cwd 之前被调（cli_commands_mixin.py:855 对 :874），所以经典终端要等到下一轮开始再按进程目录重判。
class Cwd:
    """可以改的「进程目录」：模拟 Hermes 切目录。"""

    def __init__(self, v):
        self.v = v

    def __call__(self):
        return self.v


def _w(tmp_path, monkeypatch, start, **kw):
    cwd = Cwd(start)
    monkeypatch.setattr(HW, "local_process_cwd", cwd)
    return World(tmp_path, **kw), cwd


def test_startup_resume_judges_by_the_directory_hermes_already_switched_to(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/b/sub", seed=False)                                  # --resume：先 chdir 再 initialize
    a = w.adapter(None, platform="cli", session="old-session")
    assert a.status()["workspace"] == {"key": "proj:p-b", "kind": "project", "reason": "cwd_from_process"}
    turn(a, 1, "接着聊")
    a.sync_turn("接着聊", "答", session_id="old-session", messages=msgs("接着聊"))
    assert [t for r, t in rows(w.mem("proj:p-b")) if r == "user"] == ["接着聊"]


def test_mid_chat_resume_in_the_classic_terminal_rejudges_at_the_next_turn_after_hermes_changed_directory(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter(None, platform="cli", session="s1")
    assert a.status()["workspace"]["key"] == "proj:p-a"
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")                # 钩子：Hermes 还没切目录
    assert a._cwd_recheck is True and a.status()["workspace"]["key"] == "proj:p-a"                  # 此刻不判（进程目录是旧的）
    cwd.v = "/work/b"                                                                              # 钩子之后 Hermes 切目录（_restore_session_cwd）
    turn(a, 1, "恢复后说的第一句")
    assert a.status()["workspace"] == {"key": "proj:p-b", "kind": "project", "reason": "cwd_from_process"} and a._cwd_recheck is False
    a.sync_turn("恢复后说的第一句", "答", session_id="s2", messages=msgs("恢复后说的第一句"))
    assert [t for r, t in rows(w.mem("proj:p-b")) if r == "user"] == ["恢复后说的第一句"] and rows(w.mem("proj:p-a")) == []
    assert call(a, "recall", {"query": "x"}).get("reason") != "workspace_unresolved"                # 不再说「认不出项目」（恢复出来的新会话窗口另有它的规则）


def test_mid_chat_resume_into_the_same_directory_keeps_the_workspace_object_and_the_turn_state(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter(None, platform="cli", session="s1")
    acc_before, win_before = a.acc, a.win
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    turn(a, 1, "还在同一个目录")
    assert a.acc is acc_before and a.win is win_before and a.status()["workspace"]["key"] == "proj:p-a"                     # 键没变：不重建


def test_resume_with_a_cwd_in_the_hook_kwargs_uses_it_at_once(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter(None, platform="cli", session="s1")
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume", cwd="/work/b/x")
    assert a.status()["workspace"] == {"key": "proj:p-b", "kind": "project", "reason": "cwd_given"} and a._cwd_recheck is False


@pytest.mark.parametrize("platform", ["tui", "desktop", "local"])
def test_resume_on_the_other_local_platforms_without_a_directory_is_life_and_with_one_is_judged(tmp_path, monkeypatch, platform):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter("/work/a", platform=platform, session="s1")
    assert a.status()["workspace"]["key"] == "proj:p-a"
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    assert a.status()["workspace"] == {"key": "life", "kind": "life", "reason": "no_cwd_life"}      # 没目录：生活（不取进程目录）
    a.on_session_switch("s3", parent_session_id="s2", reset=False, reason="resume", cwd="/work/b")
    assert a.status()["workspace"]["key"] == "proj:p-b" and a.status()["workspace"]["reason"] == "cwd_given"
    turn(a, 1, "普通")
    assert call(a, "recall", {"query": "x"}).get("reason") != "workspace_unresolved"


def test_resume_can_recover_a_workspace_that_was_unresolved(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter("relative/bad", platform="desktop", session="s1")                                # 给了不合格的目录：说不清
    assert a.status()["workspace"]["key"] is None
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume", cwd="/work/a")
    assert a.status()["workspace"]["key"] == "proj:p-a"


def test_compression_branch_reset_and_rewind_do_not_rejudge_anything(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False)
    a = w.adapter(None, platform="cli", session="s1")
    cwd.v = "/work/b"                                                                              # 就算进程目录变了
    a.on_session_switch("s1-c", parent_session_id="s1", reset=False, reason="compression")
    a.on_session_switch("s1-b", parent_session_id="s1-c", reset=False, reason="branch")
    a.on_session_switch("s2", parent_session_id="", reset=True, reason="new_session")
    a.on_session_switch("s2", parent_session_id="", reset=False, rewound=True)
    turn(a, 1, "普通")
    assert a._cwd_recheck is False and a.status()["workspace"] == {"key": "proj:p-a", "kind": "project", "reason": "cwd_from_process"}


def test_gateway_platform_resume_is_still_unknown(tmp_path, monkeypatch):
    w, cwd = _w(tmp_path, monkeypatch, "/work/a", seed=False, owners={"telegram": ["42"]})
    a = w.adapter(None, platform="telegram", session="s1")
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume", cwd="/work/a")
    assert a.status()["workspace"] == {"key": None, "kind": None, "reason": "workspace_unknown_after_switch_resume"}


# ================================================================== 带着登录身份的 tui／desktop：和宿主主人的系统身份比
# 源码依据：tui_gateway/agent_factory.py:678 user_id=auth_user_id…；session_transports.py:38-47：WS 登录的 user_id 是 host_owner_subject()（签名登录的网页／面板／桌面版就是这台电脑的主人）；
# gateway/owner_identity.py:17 host_owner_subject()＝主人自己的系统身份；agent/agent_init.py:1195-1198 只有 user_id 有值才进 kwargs。
# 插件在 Hermes 进程里懒导入 gateway.owner_identity 比较：相等＝主人（host_owner_login）；不相等＝照网关规则要配 owners；导入或调用失败＝不当主人（login_identity_unverifiable）。
import types as _types


def _fake_owner_identity(monkeypatch, subject=None, boom=False, absent=False):
    for name in ("gateway", "gateway.owner_identity"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if absent:
        monkeypatch.setitem(sys.modules, "gateway", None)                                           # 导入失败（None 会让 import 抛 ImportError）
        return
    pkg, mod = _types.ModuleType("gateway"), _types.ModuleType("gateway.owner_identity")

    def host_owner_subject():
        if boom:
            raise RuntimeError("no such owner")
        return subject
    mod.host_owner_subject = host_owner_subject
    pkg.owner_identity = mod
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.owner_identity", mod)


def _say(a, text):
    turn(a, 1, text)
    a.sync_turn(text, "答", session_id="s1", messages=msgs(text))


@pytest.mark.parametrize("platform", ["tui", "desktop"])
def test_login_identity_equal_to_the_host_owner_is_the_owner_with_no_config(tmp_path, monkeypatch, platform):
    _fake_owner_identity(monkeypatch, subject="uid:501")
    w = World(tmp_path, seed=False)
    a = w.adapter("/work/a", platform=platform, user_id="uid:501")
    _say(a, "登录进来的主人说的")
    assert a.owner_basis == "host_owner_login" and a.status()["recorded_turns"] == 1
    assert [t for r, t in rows(w.mem("proj:p-a")) if r == "user"] == ["登录进来的主人说的"]
    assert "uid:501" not in json.dumps(a.status()) and "uid:501" not in (w.home / "yuanwei" / "status.json").read_text()      # 身份的值不进状态、不进状态文件


@pytest.mark.parametrize("platform", ["tui", "desktop"])
def test_login_identity_that_is_not_the_host_owner_needs_owners_config(tmp_path, monkeypatch, platform):
    _fake_owner_identity(monkeypatch, subject="uid:501")
    w = World(tmp_path, seed=False)
    a = w.adapter("/work/a", platform=platform, user_id="uid:999")
    _say(a, "别人的登录")
    assert a.status()["recorded_turns"] == 0 and a.status()["skipped"] == {"login_identity_needs_owner": 1} and rows(w.mem("proj:p-a")) == []
    w2 = World(tmp_path / "cfg", seed=False, owners={platform: ["uid:999"]})
    b = w2.adapter("/work/a", platform=platform, user_id="uid:999")
    _say(b, "配了主人")
    assert b.owner_basis == "configured_owner" and [t for r, t in rows(w2.mem("proj:p-a")) if r == "user"] == ["配了主人"]
    w3 = World(tmp_path / "other", seed=False, owners={platform: ["someone-else"]})
    c = w3.adapter("/work/a", platform=platform, user_id="uid:999")
    _say(c, "对不上")
    assert c.status()["skipped"] == {"not_owner": 1} and rows(w3.mem("proj:p-a")) == []


@pytest.mark.parametrize("how", [{"absent": True}, {"boom": True}, {"subject": ""}, {"subject": None}])
def test_when_the_host_owner_cannot_be_looked_up_the_login_is_not_taken_for_the_owner(tmp_path, monkeypatch, how):
    _fake_owner_identity(monkeypatch, **how)
    w = World(tmp_path, seed=False)
    a = w.adapter("/work/a", platform="desktop", user_id="uid:501")
    _say(a, "查不到主人")
    assert a.status()["skipped"] == {"login_identity_unverifiable": 1} and rows(w.mem("proj:p-a")) == []
    w2 = World(tmp_path / "cfg", seed=False, owners={"desktop": ["uid:501"]})                       # 配了 owners 也不放行：没法验证就是没法验证
    b = w2.adapter("/work/a", platform="desktop", user_id="uid:501")
    _say(b, "配了也一样")
    assert b.status()["skipped"] == {"login_identity_unverifiable": 1}


@pytest.mark.parametrize("platform", ["cli", "local", "tui", "desktop"])
def test_local_platform_without_a_login_identity_is_still_the_owner(tmp_path, monkeypatch, platform):
    _fake_owner_identity(monkeypatch, subject="uid:501")                                           # 不带 user_id 就不看它
    w = World(tmp_path, seed=False)
    a = w.adapter("/work/a", platform=platform)                                                    # 没有 user_id：旧令牌、stdio、PTY 子进程、经典终端
    turn(a, 1, "我自己")
    a.sync_turn("我自己", "答", session_id="s1", messages=msgs("我自己"))
    assert a.owner_basis == "local_session" and a.status()["recorded_turns"] == 1


def test_a_user_id_on_the_classic_terminal_does_not_change_the_owner_rule(tmp_path):
    w = World(tmp_path, seed=False)
    a = w.adapter("/work/a", platform="cli", user_id="whatever")                                   # 只有 tui、desktop 查登录身份
    turn(a, 1, "我自己")
    a.sync_turn("我自己", "答", session_id="s1", messages=msgs("我自己"))
    assert a.owner_basis == "local_session"
