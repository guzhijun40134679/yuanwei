"""清点第 56 条第一批·审后修补二：开轮失败的四个洞。回执当轮就看得见、技能命令和来源说明里的「关」也认、「没确认已关」跟着换窗口服务和压缩分叉走、
明确的开能解除、清失败标记只认登记成功的本人输入、重送的提前返回补当前轮标记。假宿主，临时目录，不联网。"""
import json
import sqlite3

import pytest

import fix56_fakehost as FH
import hermes_skill_contract as SC
from helpers2 import organized
from test_fix55_midturn import _chat_adapter, env
from test_fix56_skill import BODY, desktop
from test_fix56_turnstart import broken_token, call, mk, turn
from test_window import mk as wmk
from yuanwei import hermes_workspace as HW
from yuanwei import window as WN
from yuanwei.hermes_adapter import HermesAdapter

Q = {"query": "烤鱼"}
SHOWN = "〔原委〕" + WN.CLOSE_FAILED_RECEIPT


def fail_turn(a, monkeypatch, n, text, **kw):
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, n, text, **kw)
    monkeypatch.setattr(a.acc, "event_turn", real)


# ================================================================== 1. 两份文字都判「有没有关」
def test_a_skill_command_whose_typed_words_are_the_close_command_is_marked_when_the_turn_fails(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, "#yuanwei-nomem\n帮我写一句口号")
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, raw)
    assert ("hermes", "hs1", "") in a.win._suspect                                          # 旧代码：只看整段原文，整段里没有「关」，不标
    turn(a, 3, "再问一句")
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


def test_an_envelope_in_front_of_the_close_command_is_marked_when_the_turn_fails(tmp_path, monkeypatch):
    mem, a = _chat_adapter(tmp_path)
    turn(a, 1, "普通一句", author_id="u1")
    fail_turn(a, monkeypatch, 2, env("#yuanwei-nomem\n帮我订个餐厅"), author_id="u1")
    assert ("hermes", "hs1", "") in a.win._suspect
    turn(a, 3, "再问一句", author_id="u1")
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


def test_a_plain_failed_turn_without_a_close_is_not_marked(tmp_path, monkeypatch):
    mem, a = desktop(tmp_path)
    raw = SC.single("demo", BODY, "帮我写一句口号")
    FH.install(monkeypatch, FH.user_event(raw))
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, raw)
    assert a.win._suspect == set() and a._late == []


def test_when_the_extra_facts_cannot_be_read_only_the_original_text_is_used(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    monkeypatch.setattr(a, "_turn_facts", lambda text: (_ for _ in ()).throw(RuntimeError("facts broke")))
    with pytest.raises(RuntimeError):
        turn(a, 2, "#yuanwei-nomem")                                                        # 开轮本身就是被它弄坏的：原文里的关照样标
    assert ("hermes", "hs1", "") in a.win._suspect


# ================================================================== 2. 回执当轮看得见
def test_the_receipt_is_shown_with_this_turns_reply_and_said_again_next_turn_in_past_tense(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    a.display_hook(response_text="x")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    assert WN.CLOSE_FAILED_RECEIPT not in a._notices                                        # 不再只放 _notices
    assert a.prefetch("接着聊", session_id="hs1") == ""                                      # 这一轮预取提前返回，拿不出来
    assert a.display_hook(response_text="回复") == "回复\n\n" + SHOWN
    assert ("close_failed", a.CLOSE_FAILED_START) in a._late
    turn(a, 3, "再问一句")                                                                   # 下一轮：用过去时再说一遍，不和「已关闭」打架
    out = a.prefetch("接着聊", session_id="hs1")
    assert a.CLOSE_FAILED_START in out and "已关闭" not in out
    assert a.display_hook(response_text="回复二") == "回复二\n\n〔原委〕" + a.CLOSE_FAILED_START
    assert "上一轮你说的「关」" in a.CLOSE_FAILED_START and "没能确认" in a.CLOSE_FAILED_START


def test_the_next_turn_says_nothing_when_the_close_lands_or_an_explicit_open_lands(tmp_path, monkeypatch):
    for i, word in enumerate(("#yuanwei-nomem", "#yuanwei-mem")):
        mem, a = mk(tmp_path / ("c%d" % i))
        turn(a, 1, "普通一句")
        fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
        a.display_hook(response_text="x")
        turn(a, 3, word)
        assert a.CLOSE_FAILED_START not in a.prefetch("q", session_id="hs1")
        assert a.display_hook(response_text="y") is None or a.CLOSE_FAILED_START not in a.display_hook(response_text="y")


# ================================================================== 4. 明确的开能解除
def test_an_explicit_open_clears_the_unconfirmed_close_even_when_the_window_row_is_still_on(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    assert a.win._suspect
    turn(a, 3, "再说一句")
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"
    assert a.win.view("hermes", "hs1").state == "off"
    turn(a, 4, "#yuanwei-mem")                                                              # 窗口行一直是 on：开是 noop，但这是明确的开
    assert a.win._suspect == set()
    assert call(a, "recall", Q)["status"] == "ok"


def test_open_and_close_in_the_same_input_is_as_today(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    turn(a, 3, "#yuanwei-mem\n别用记忆")                                                     # 有开又有关：关落地，窗口真的关着
    assert a.win.view("hermes", "hs1").state == "off" and mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "off"
    assert call(a, "recall", Q)["status"] == "refused"


def test_an_open_blocked_by_a_setting_or_a_regeneration_does_not_clear_it(tmp_path):
    svc, st, clk = wmk(tmp_path, settings=lambda host, sid: "session")
    svc.bind_new("hermes", "S1")
    svc._suspect.add(("hermes", "S1", ""))
    from test_window import ref
    r = svc.process_input(ref(), "#yuanwei-mem", origin_verified=True)
    assert r.steps == (("open", "command", "blocked_by_setting"),) and ("hermes", "S1", "") in svc._suspect
    svc2, st2, clk2 = wmk(tmp_path, name="w2.db")
    svc2.bind_new("hermes", "S1")
    svc2._suspect.add(("hermes", "S1", ""))
    svc2.process_input(ref(), "#yuanwei-mem", origin_verified=True, closes_only=True)       # 重新生成：没有开的步，不解除
    assert ("hermes", "S1", "") in svc2._suspect
    svc2.process_input(ref(), "#yuanwei-mem", origin_verified=True)
    assert svc2._suspect == set()


# ================================================================== 3. 标记跟着走
def test_the_unconfirmed_mark_survives_a_workspace_switch_to_a_new_window_service(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    made = {"fail": False}
    (tmp_path / "m2").mkdir(exist_ok=True)
    other = organized(tmp_path / "m2", host="hermes", ws="proj:other")

    def factory(key):
        if key == "proj:other":
            if made["fail"]:
                raise OSError("库打不开")
            return other
        return mem
    a = HermesAdapter({"spawn": "off"}, memory_factory=factory, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(a, 1, "普通一句")
    old_win = a.win
    a._cwd_recheck = True
    monkeypatch.setattr(HW, "resolve", lambda home, cwd, plat, on_drop=None: {"key": "proj:other", "kind": "project", "label": "别的项目", "reason": "test"})
    made["fail"] = True
    with pytest.raises(OSError):
        turn(a, 2, "#yuanwei-nomem")                                                        # 重判失败那一轮带着「关」
    assert ("hermes", "hs1", "") in old_win._suspect
    made["fail"] = False
    turn(a, 3, "再来")                                                                      # 下一轮重判成功：换了新的窗口服务
    assert a.win is not old_win and a.ws["key"] == "proj:other"
    assert ("hermes", "hs1", "") in a.win._suspect                                          # 旧代码：新的服务里没有这个标记，读取悄悄恢复
    assert call(a, "recall", Q)["reason"] == "window_close_unconfirmed"


@pytest.mark.parametrize("reason", ["compression", "branch"])
def test_the_unconfirmed_mark_follows_a_compression_or_a_branch_to_the_new_session(tmp_path, monkeypatch, reason):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    a.on_session_switch("hs-new", parent_session_id="hs1", reason=reason)
    assert ("hermes", "hs-new", "") in a.win._suspect
    assert bool(a._turn_failed.get("hs-new")) is (reason == "compression")                        # 开轮失败的标记只在压缩时抄过去
    if reason == "compression":
        r = json.loads(a.handle_tool_call("window_off", {}))
        assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT              # 子会话里这一轮助手调 window_off：回「没确认」，不回「off」
    turn(a, 3, "继续")
    r = call(a, "recall", Q)
    assert r["status"] == "refused" and r["reason"] == "window_close_unconfirmed"


def test_the_mark_follows_on_the_early_return_path_of_a_chat_platform_too(tmp_path, monkeypatch):
    mem, a = _chat_adapter(tmp_path)
    turn(a, 1, "普通一句", author_id="u1")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem", author_id="u1")
    a.on_session_switch("hs-b", parent_session_id="hs1", reason="branch")                  # 聊天平台的分叉：下面提前返回，等下一条本人消息
    assert ("hermes", "hs-b", "") in a.win._suspect


def test_a_switch_that_is_not_a_compression_or_a_branch_does_not_carry_the_mark(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem")
    a.on_session_switch("hs-r", parent_session_id="hs1", reset=True)
    assert ("hermes", "hs-r", "") not in a.win._suspect and "hs-r" not in a._turn_failed


# ================================================================== 5. 清失败标记只认登记成功的本人输入
def test_someone_elses_message_does_not_clear_the_failed_mark(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    a.initialize("sB", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="1")
    turn(a, 1, "主人的一句", author_id="u1")
    fail_turn(a, monkeypatch, 2, "#yuanwei-nomem", author_id="u1")
    assert a._turn_failed == {"sB": True}
    turn(a, 3, "别人的一句", author_id="u2")                                                 # 别人的消息：登记成 ignored-…，旧代码里这就把标记清了
    assert a._cur["sB"].startswith("ignored-") and a._turn_failed == {"sB": True}
    turn(a, 4, "主人又来", author_id="u1")
    assert a._turn_failed == {}


# ================================================================== 6. 重送的提前返回补当前轮标记
def test_a_resent_event_while_the_registration_is_waiting_restores_the_current_token(tmp_path):
    mem, a = mk(tmp_path)
    turn(a, 1, "问个问题")
    tok = a._cur["hs1"]
    a._cur["hs1"] = "something-else"
    turn(a, 1, "问个问题")
    assert a._cur["hs1"] == tok and a.ids.current().turn_id == tok
