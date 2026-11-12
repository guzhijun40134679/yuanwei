"""小问题 28（2026-11-12）：Hermes 桌面版走网关自己的会话管理，建助手时不传 cwd（Hermes 上游 #144869），原委初始化时拿不到文件夹，所有项目的对话都落进「生活」。
但这一轮正在进行时 Hermes 自己知道文件夹：agent.runtime_cwd.scoped_session_cwd()，其次 gateway.session_policy.active_policy().cwd。
规矩：只补「宿主没传 cwd」这一种；平台不是经典终端（cli）才问；只在主对话里问（agent_context == primary）；**绝不用进程目录**；取到的值要展开 ~、是绝对路径、本机上真有这个目录，
否则当没取到；import 失败、函数改名、抛异常都当没取到，退回「生活」，插件照常启用；取到以后照 projects.db 判，依据写 cwd_from_host_turn；被丢掉的值只记个数。
这里用假的 agent.runtime_cwd、gateway.session_policy 模块（塞进 sys.modules），不 import 真的 Hermes。"""
import os
import sys
import types

import pytest

from test_hermes_workspace import World, call, make_home, msgs, rows, turn
from yuanwei import hermes_workspace as HW
from yuanwei.hermes_adapter import HermesAdapter


class Boom(Exception):
    pass


class Host:
    """假的 Hermes 模块。scoped／policy 是「这一轮」里两个来源的返回值（Boom 实例＝调用时抛）；calls 记下谁被问过。"""

    def __init__(self, monkeypatch, scoped="", policy=None, policy_object=True):
        self.calls = []
        self.process_calls = []
        rc = types.ModuleType("agent.runtime_cwd")
        sp = types.ModuleType("gateway.session_policy")

        def scoped_session_cwd():
            self.calls.append("scoped")
            if isinstance(self.scoped, Exception):
                raise self.scoped
            return self.scoped

        def active_policy():
            self.calls.append("policy")
            if isinstance(self.policy, Exception):
                raise self.policy
            if self.policy is None:
                return None
            return types.SimpleNamespace(cwd=self.policy) if policy_object else self.policy

        def resolve_agent_cwd():                                                                 # 进程目录那条：这条路绝不许被走到
            self.process_calls.append("resolve_agent_cwd")
            return "/should/never/be/used"
        self.scoped, self.policy = scoped, policy
        rc.scoped_session_cwd, rc.resolve_agent_cwd = scoped_session_cwd, resolve_agent_cwd
        sp.active_policy = active_policy
        for name, mod in (("agent", types.ModuleType("agent")), ("agent.runtime_cwd", rc), ("gateway", types.ModuleType("gateway")), ("gateway.session_policy", sp)):
            monkeypatch.setitem(sys.modules, name, mod)


def world(tmp_path, scope="isolated"):
    """两个真实存在的项目文件夹（取到的值要本机上真有这个目录）；家目录（测试里是临时 HOME）不归任何项目。"""
    w = World(tmp_path / "w", scope=scope, seed=False)
    pa, pb = tmp_path / "proj-a", tmp_path / "proj-b"
    (pa / "sub").mkdir(parents=True)
    pb.mkdir(parents=True)
    w.home = make_home(tmp_path, [("p-a", "甲项目", [str(pa)], 0), ("p-b", "乙项目", [str(pb)], 0)], name="home2")
    w.pa, w.pb = pa, pb
    return w


def init(w, platform="desktop", cwd=None, agent_context="primary", session="s1"):
    a = HermesAdapter(dict(w.cfg), executor="inline", memory_factory=w.mem)
    a.initialize(session, platform=platform, agent_context=agent_context, hermes_home=str(w.home), cwd=cwd)
    return a


def ws(a):
    s = a.status()["workspace"]
    return s["key"], s["reason"]


# ---------------------------------------------------------------- 宿主传了 cwd：照用，后备不被调
def test_host_given_cwd_is_used_and_the_fallback_is_never_called(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=Boom("不该被问"), policy=Boom("不该被问"))
    a = init(w, cwd=str(w.pb))
    assert ws(a) == ("proj:p-b", "cwd_given") and h.calls == []
    a2 = init(w, cwd="/elsewhere/not/a/project")                                                # 给了的，哪怕不存在：照旧判（生活），后备不碰
    assert ws(a2) == ("life", "cwd_given") and h.calls == []


# ---------------------------------------------------------------- 没传：问 Hermes 这一轮的文件夹
def test_desktop_without_cwd_asks_the_turn_scope_and_finds_the_project(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped=str(w.pa))
    a = init(w)
    assert ws(a) == ("proj:p-a", "cwd_from_host_turn") and a.status()["host_cwd_dropped"] == 0
    Host(monkeypatch, scoped=str(w.pa / "sub"))                                                 # 项目里的子目录也算这个项目
    assert ws(init(w)) == ("proj:p-a", "cwd_from_host_turn")
    Host(monkeypatch, scoped=str(w.pb))
    assert ws(init(w)) == ("proj:p-b", "cwd_from_host_turn")


@pytest.mark.parametrize("platform", ["desktop", "tui", "local"])
def test_every_non_cli_local_platform_asks(tmp_path, monkeypatch, platform):
    w = world(tmp_path)
    Host(monkeypatch, scoped=str(w.pb))
    assert ws(init(w, platform=platform)) == ("proj:p-b", "cwd_from_host_turn")


def test_second_source_active_policy_is_used_when_the_first_is_empty(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped="", policy=str(w.pb))
    assert ws(init(w)) == ("proj:p-b", "cwd_from_host_turn") and h.calls == ["scoped", "policy"]
    h2 = Host(monkeypatch, scoped=str(w.pa), policy=str(w.pb))                                  # 第一个取到了就不问第二个
    assert ws(init(w)) == ("proj:p-a", "cwd_from_host_turn") and h2.calls == ["scoped"]


def test_a_bad_first_value_falls_through_to_the_second_source(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="relative/x", policy=str(w.pb))
    a = init(w)
    assert ws(a) == ("proj:p-b", "cwd_from_host_turn") and a.status()["host_cwd_dropped"] == 1


def test_home_directory_means_life_not_a_project(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="~")                                                               # Hermes 给非项目对话记的是 ~：展开后是家目录，不归任何项目
    a = init(w)
    assert ws(a) == ("life", "cwd_from_host_turn") and a.status()["host_cwd_dropped"] == 0
    assert ws(init(w)) == ("life", "cwd_from_host_turn")


def test_tilde_subfolder_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    w = World(tmp_path / "w", seed=False)
    w.home = make_home(tmp_path, [("p-w", "工作", [str(home / "work")], 0)], name="home2")
    Host(monkeypatch, scoped="~/work")
    assert ws(init(w)) == ("proj:p-w", "cwd_from_host_turn")


# ---------------------------------------------------------------- 取到的值不能用：当没取到（记个数），退回生活
@pytest.mark.parametrize("bad,counted", [("relative/path", 1), ("proj-nowhere-xyz", 1), ("/no/such/dir/anywhere", 1), ("/", 1), (123, 1), (["x"], 1), ("", 0), ("   ", 0), (None, 0)])
def test_unusable_values_are_treated_as_not_obtained(tmp_path, monkeypatch, bad, counted):
    w = world(tmp_path)
    Host(monkeypatch, scoped=bad)
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life") and a.active and a.status()["host_cwd_dropped"] == counted


def test_a_relative_path_is_dropped_even_if_it_exists_relative_to_the_process_directory(tmp_path, monkeypatch):
    w = world(tmp_path)
    monkeypatch.chdir(tmp_path)                                                                 # 进程目录下恰好有 proj-a：相对路径会被「解析成」项目甲，这正是不许的
    Host(monkeypatch, scoped="proj-a")
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life") and a.status()["host_cwd_dropped"] == 1


def test_a_file_is_not_a_directory(tmp_path, monkeypatch):
    w = world(tmp_path)
    f = tmp_path / "afile.txt"
    f.write_text("x")
    Host(monkeypatch, scoped=str(f))
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life") and a.status()["host_cwd_dropped"] == 1


def test_status_counts_only_numbers_never_the_dropped_path(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="/secret/place/that/does/not/exist")
    a = init(w)
    import json
    assert "/secret/place" not in json.dumps(a.status(), ensure_ascii=False)


def test_archived_project_found_through_the_fallback_is_still_unresolved(tmp_path, monkeypatch):
    w = world(tmp_path)
    w.home = make_home(tmp_path, [("p-old", "旧", [str(w.pa)], 1)], name="home3")
    Host(monkeypatch, scoped=str(w.pa))
    a = init(w)
    assert ws(a) == (None, "project_archived")                                                  # 判定规则一个字不改


# ---------------------------------------------------------------- 出任何错：退回现在的行为，插件照常启用
def test_import_failure_means_life_and_the_plugin_still_works(tmp_path, monkeypatch):
    w = world(tmp_path)
    for name in ("agent", "agent.runtime_cwd", "gateway", "gateway.session_policy"):
        monkeypatch.setitem(sys.modules, name, None)                                            # None 放进 sys.modules：import 它们会 ImportError
    a = init(w)
    assert a.active and ws(a) == ("life", "no_cwd_life") and a.status()["host_cwd_dropped"] == 0
    turn(a, 1, "普通话")
    a.sync_turn("普通话", "答", session_id="s1", messages=msgs("普通话"))
    assert a.status()["recorded_turns"] == 1


def test_function_renamed_or_raising_means_life_and_the_plugin_still_works(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped=Boom("炸了"), policy=Boom("也炸了"))
    a = init(w)
    assert a.active and ws(a) == ("life", "no_cwd_life")
    h = Host(monkeypatch)
    del sys.modules["agent.runtime_cwd"].scoped_session_cwd                                      # 改名了：AttributeError
    del sys.modules["gateway.session_policy"].active_policy
    a2 = init(w)
    assert a2.active and ws(a2) == ("life", "no_cwd_life") and h.calls == []
    h3 = Host(monkeypatch, scoped=Boom("炸"), policy=str(w.pb))                                  # 第一个炸了，第二个还能用
    assert ws(init(w)) == ("proj:p-b", "cwd_from_host_turn")
    assert h3.calls == ["scoped", "policy"]


def test_active_policy_none_or_without_cwd_means_life(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="", policy=None)                                                   # 不在网关的一轮里：active_policy() 是 None
    assert ws(init(w)) == ("life", "no_cwd_life")
    Host(monkeypatch, scoped="", policy=types.SimpleNamespace(other=1), policy_object=False)    # 没有 cwd 这个属性
    assert ws(init(w)) == ("life", "no_cwd_life")


def test_the_injected_asker_raising_cannot_break_resolve(tmp_path):
    w = world(tmp_path)

    def boom():
        raise Boom("x")
    r = HW.resolve(str(w.home), None, "desktop", host_cwd=boom)
    assert r["key"] == "life" and r["reason"] == "no_cwd_life"


# ---------------------------------------------------------------- 绝不用进程目录
def test_never_uses_the_process_directory_even_when_it_is_a_project(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped="", policy=None)                                               # 这一轮里问不到
    monkeypatch.setattr(os, "getcwd", lambda: str(w.pa))                                        # 进程目录恰好是项目甲
    monkeypatch.setenv("TERMINAL_CWD", str(w.pa))
    monkeypatch.setattr(HW, "local_process_cwd", lambda: (_ for _ in ()).throw(Boom("不许问进程目录")))
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life")                                                     # 不是项目甲
    assert h.process_calls == []                                                                # Hermes 的 resolve_agent_cwd 也没被调


def test_never_uses_the_process_directory_when_the_value_is_bad_either(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped="relative/x", policy="/no/such")
    monkeypatch.setattr(os, "getcwd", lambda: str(w.pb))
    monkeypatch.setattr(HW, "local_process_cwd", lambda: str(w.pb))
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life") and h.process_calls == []


def test_never_uses_the_process_directory_on_the_resume_hook_either(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped="", policy=None)
    a = init(w)
    monkeypatch.setattr(os, "getcwd", lambda: str(w.pb))
    monkeypatch.setattr(HW, "local_process_cwd", lambda: str(w.pb))
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    assert ws(a) == ("life", "no_cwd_life") and h.process_calls == []


# ---------------------------------------------------------------- 网关聊天平台：不问，照旧记进生活
@pytest.mark.parametrize("platform", ["telegram", "discord", "slack", "weixin", "api_server", "acp", "webhook", "sms"])
def test_gateway_chat_platforms_never_ask_even_if_the_turn_folder_is_a_project(tmp_path, monkeypatch, platform):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=str(w.pa), policy=str(w.pa))                                   # 「这一轮的文件夹」就是项目甲，也不许用
    a = init(w, platform=platform)
    assert ws(a) == ("life", "no_cwd_life") and h.calls == [] and a.status()["host_cwd_dropped"] == 0


def test_gateway_chat_platform_resume_hook_never_asks_either(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=str(w.pb))
    a = init(w, platform="telegram")
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    assert h.calls == [] and ws(a)[0] != "proj:p-b"


def test_resolve_itself_only_asks_for_local_non_cli_platforms(tmp_path):
    w = world(tmp_path)
    assert HW.HOST_TURN_PLATFORMS == tuple(p for p in HW.LOCAL_PLATFORMS if p not in HW.PROCESS_CWD_PLATFORMS) == ("local", "tui", "desktop")

    def ask():
        return str(w.pa), 0
    for plat in ("local", "tui", "desktop"):
        assert HW.resolve(str(w.home), None, plat, host_cwd=ask)["key"] == "proj:p-a"
    for plat in ("cli", "telegram", "discord", "api_server", "acp", "webhook", "bot_room", "cron"):
        r = HW.resolve(str(w.home), None, plat, host_cwd=ask, process_cwd=lambda: None)
        assert r["key"] == "life" and r["reason"] == "no_cwd_life", plat


# ---------------------------------------------------------------- 经典终端：行为不变
def test_cli_never_asks_the_host_and_keeps_using_the_process_directory(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=Boom("cli 不该问"), policy=Boom("cli 不该问"))
    monkeypatch.setattr(HW, "local_process_cwd", lambda: str(w.pb))
    a = init(w, platform="cli")
    assert ws(a) == ("proj:p-b", "cwd_from_process") and h.calls == []
    monkeypatch.setattr(HW, "local_process_cwd", lambda: None)
    assert ws(init(w, platform="cli")) == ("life", "no_cwd_life") and h.calls == []             # cli 取不到进程目录：生活，也不去问宿主这一轮
    Host(monkeypatch, scoped=str(w.pa))
    assert ws(init(w, platform="cli", cwd=str(w.pb))) == ("proj:p-b", "cwd_given")


def test_cli_resume_hook_still_waits_for_the_next_turn_and_does_not_ask_the_host(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=Boom("cli 不该问"), policy=Boom("cli 不该问"))
    monkeypatch.setattr(HW, "local_process_cwd", lambda: str(w.pa))
    a = init(w, platform="cli")
    monkeypatch.setattr(HW, "local_process_cwd", lambda: str(w.pb))
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    turn(a, 1, "一句话")
    assert ws(a) == ("proj:p-b", "cwd_from_process") and h.calls == []


# ---------------------------------------------------------------- cron、子代理、群聊、agent_context 缺：不问
@pytest.mark.parametrize("ctx,plat,reason", [("cron", "desktop", "agent_context_cron"), ("subagent", "desktop", "agent_context_subagent"), (None, "desktop", "agent_context_missing"),
                                              ("primary", "cron", "platform_cron"), ("primary", "bot_room", "group_room_not_supported")])
def test_non_primary_contexts_never_use_the_fallback(tmp_path, monkeypatch, ctx, plat, reason):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=str(w.pa), policy=str(w.pa))
    a = init(w, platform=plat, agent_context=ctx)
    assert not a.active and a.inactive_reason == reason and h.calls == []
    assert a.status()["workspace"]["key"] is None


# ---------------------------------------------------------------- 恢复会话的钩子：同样的后备
def test_resume_hook_without_cwd_uses_the_same_fallback_on_desktop(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="")
    a = init(w)
    assert ws(a) == ("life", "no_cwd_life")
    Host(monkeypatch, scoped=str(w.pb))                                                         # 会话被挪进项目乙：这一轮里 Hermes 说的文件夹变了
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    assert ws(a) == ("proj:p-b", "cwd_from_host_turn")


def test_resume_hook_with_cwd_uses_it_and_does_not_ask(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=str(w.pa))
    a = init(w)
    h.calls.clear()
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume", cwd=str(w.pb))
    assert ws(a) == ("proj:p-b", "cwd_given") and h.calls == []


def test_resume_hook_fallback_failure_goes_back_to_life(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped=str(w.pa))
    a = init(w)
    assert ws(a)[0] == "proj:p-a"
    Host(monkeypatch, scoped=Boom("炸"), policy=Boom("炸"))
    a.on_session_switch("s2", parent_session_id="s1", reset=False, reason="resume")
    assert a.active and ws(a) == ("life", "no_cwd_life")


def test_compression_and_new_session_do_not_ask_again(tmp_path, monkeypatch):
    w = world(tmp_path)
    h = Host(monkeypatch, scoped=str(w.pa))
    a = init(w)
    h.calls.clear()
    a.on_session_switch("s1-c", parent_session_id="s1", reset=False, reason="compression")
    a.on_session_switch("s3", parent_session_id="", reset=True, reason="new_session")
    assert h.calls == [] and ws(a) == ("proj:p-a", "cwd_from_host_turn")                         # 这次不做中途换工作区


# ---------------------------------------------------------------- 端到端：桌面版两个项目各聊一句，分进两个工作区
def test_two_desktop_projects_are_written_into_two_workspaces(tmp_path, monkeypatch):
    w = world(tmp_path)
    for scoped, key, text in ((str(w.pa), "proj:p-a", "项目甲里的一句话"), (str(w.pb), "proj:p-b", "项目乙里的一句话"), ("~", "life", "家里的一句话")):
        Host(monkeypatch, scoped=scoped)
        a = init(w)
        turn(a, 1, text)
        a.sync_turn(text, "答", session_id="s1", messages=msgs(text))
        assert a.status()["recorded_turns"] == 1 and a.owner_basis == "local_session"
    for key, text in (("proj:p-a", "项目甲里的一句话"), ("proj:p-b", "项目乙里的一句话"), ("life", "家里的一句话")):
        assert [t for r, t in rows(w.mem(key)) if r == "user"] == [text]
    assert HW.known(str(w.home)).keys() >= {"proj:p-a", "proj:p-b", "life"}


def test_nothing_obtained_still_records_into_life_as_before(tmp_path, monkeypatch):
    w = world(tmp_path)
    Host(monkeypatch, scoped="", policy=None)
    a = init(w)
    turn(a, 1, "还是生活")
    a.sync_turn("还是生活", "答", session_id="s1", messages=msgs("还是生活"))
    assert [t for r, t in rows(w.mem("life")) if r == "user"] == ["还是生活"] and ws(a) == ("life", "no_cwd_life")
    assert call(a, "recall", {"query": "生活"})["status"] != "refused"


# ---------------------------------------------------------------- 纯函数层
def test_host_turn_cwd_returns_a_normalized_absolute_path_and_counts_drops(tmp_path, monkeypatch):
    d = tmp_path / "x" / "y"
    d.mkdir(parents=True)
    Host(monkeypatch, scoped=str(tmp_path / "x" / "y" / ".." / "y") + "/")
    assert HW.host_turn_cwd() == (str(d), 0)
    Host(monkeypatch, scoped="/nope", policy="/nope2")
    assert HW.host_turn_cwd() == (None, 2)
    for name in ("agent", "agent.runtime_cwd", "gateway", "gateway.session_policy"):
        monkeypatch.setitem(sys.modules, name, None)
    assert HW.host_turn_cwd() == (None, 0)
