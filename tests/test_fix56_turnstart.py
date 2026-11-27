"""清单第 56 条第一批·第 2 节：开轮失败要看得见、要关得住。
假宿主、假库故障（monkeypatch），临时目录，不联网，不碰真实的 ~/.hermes。"""
import json
import sqlite3

import pytest

from helpers2 import organized
from yuanwei import hermes_workspace as HW
from yuanwei import window as WN
from yuanwei.hermes_adapter import HermesAdapter

OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]


def mk(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"mode": "tools_only", "spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    return mem, a


def turn(a, n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


def call(a, name, args=None):
    return json.loads(a.handle_tool_call(name, args or {}))


def broken_token(a, monkeypatch):
    def boom(*x, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(a.acc, "event_turn", boom)


def test_desktop_event_turn_failure_is_recorded_reraised_and_the_old_identity_is_not_reused(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "接着聊聊")
    r = call(a, "recall", {"query": "烤鱼"})                                              # 旧代码里这里沿用第 1 轮的身份读到 ok
    assert r["status"] == "refused" and r["reason"] == "turn_unproven"
    ie = json.load(open(tmp_path / "home" / "yuanwei" / "internal-errors.json", encoding="utf-8"))
    assert any(v["class"] == "OperationalError" for v in ie["totals"].values())
    assert "接着聊聊" not in json.dumps(ie, ensure_ascii=False)                         # 只有类名和位置，没有原话
    monkeypatch.setattr(a.acc, "event_turn", real)
    turn(a, 3, "又一轮")                                                                  # 下一轮开成功：恢复正常
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_nomem_turn_that_failed_to_start_keeps_reads_stopped_until_a_close_or_an_explicit_open(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "#yuanwei-nomem")
    assert a.display_hook(response_text="回复") == "回复\n\n〔原委〕" + WN.CLOSE_FAILED_RECEIPT      # 审后修补：回执放进当轮回复后面显示（不再只放 _notices，那一轮的预取拿不出来）
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "refused"
    monkeypatch.setattr(a.acc, "event_turn", real)
    turn(a, 3, "再问一句普通的")                                                           # 下一轮开成功了，但「关」没落地：仍然读不到
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] == "window_close_unconfirmed"
    turn(a, 4, "#yuanwei-nomem")                                                          # 成功关：不再「没确认已关」，窗口真的关着
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "control_nomem"
    turn(a, 5, "#yuanwei-mem")                                                            # 明确开（窗口真关着，开是落地的）
    assert call(a, "recall", {"query": "烤鱼"})["status"] == "ok"


def test_nomem_turn_that_failed_to_start_then_a_successful_close_lands(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "不要用记忆 #yuanwei-nomem")
    monkeypatch.setattr(a.acc, "event_turn", real)
    turn(a, 3, "#yuanwei-nomem")                                                          # 成功关闭
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] == "control_nomem"
    assert a.win.view("hermes", "hs1").state == "off"


def test_window_off_in_a_failed_turn_gets_the_close_failed_receipt_never_off(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "请把记忆关掉")
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT and r["reason"] == "window_close_unconfirmed"
    assert ("hermes", "hs1", "") in a.win._suspect
    w = a.win.view("hermes", "hs1")
    assert w.state == "off" and w.reason == "close_unconfirmed"                            # 读取停着，原因是「没确认已关」，不是「被工具关了」
    assert mem.store.conn.execute("SELECT state FROM windows").fetchone()[0] == "on"       # 库里窗口状态没动：没有假装关了
    assert json.loads(a.handle_tool_call("window_on", {}))["reason"] == "turn_unproven"      # 开照旧拒绝
    assert json.loads(a.handle_tool_call("window_delete", {}))["reason"] == "turn_unproven"  # 删照旧拒绝
    monkeypatch.setattr(a.acc, "event_turn", real)
    turn(a, 3, "又一轮")
    assert call(a, "recall", {"query": "烤鱼"})["reason"] == "window_close_unconfirmed"   # 读取仍停着，直到成功关或明确开


def test_window_off_after_a_successful_turn_start_is_unchanged(tmp_path, monkeypatch):
    mem, a = mk(tmp_path)
    turn(a, 1, "普通一句")
    real = a.acc.event_turn
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "接着")
    monkeypatch.setattr(a.acc, "event_turn", real)
    turn(a, 3, "再来一句")                                                                # 新的开轮成功：失败标记清掉
    assert a._turn_failed == {}
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["state"] == "off" and a.win.view("hermes", "hs1").reason == "closed_by_tool"


def test_remote_first_sentence_with_an_unopenable_library(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    state = {"fail": True, "n": 0}
    (tmp_path / "m").mkdir(exist_ok=True)

    def factory(key):
        state["n"] += 1
        if state["fail"]:
            raise OSError("库打不开")
        return organized(tmp_path / "m", host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory_factory=factory, executor="inline")
    a.initialize("sB", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="1")
    with pytest.raises(OSError):
        turn(a, 1, "#yuanwei-nomem 第一句", author_id="u1")
    assert a.mem is None and a.ids is None and a.win is None                              # 各项自己判空，没有二次出错盖掉原来的异常
    r = json.loads(a.handle_tool_call("window_off", {}))
    assert r["status"] == "error" and r["text"] == WN.CLOSE_FAILED_RECEIPT
    assert json.loads(a.handle_tool_call("window_on", {}))["reason"] == "identity_unbound"
    assert json.loads(a.handle_tool_call("window_delete", {}))["reason"] == "identity_unbound"
    ie = json.load(open(tmp_path / "home" / "yuanwei" / "internal-errors.json", encoding="utf-8"))
    assert any(v["class"] == "OSError" for v in ie["totals"].values())
    state["fail"] = False
    turn(a, 2, "第二句", author_id="u1")                                                  # 库能开了：开轮成功，失败标记清掉
    assert a._turn_failed == {} and a.mem is not None


def test_non_owner_failed_turn_does_not_touch_the_owners_window(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    a.initialize("sB", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="1")
    turn(a, 1, "主人的一句", author_id="u1")
    broken_token(a, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        turn(a, 2, "#yuanwei-nomem", author_id="u1")                                       # 主人本人：停读
    assert ("hermes", "sB", "") in a.win._suspect
    a.win._suspect.clear()
    a._turn_failed.clear()

    def boom_owner(*x, **k):
        raise sqlite3.OperationalError("x")
    monkeypatch.setattr(a, "_owner_check", lambda author: (_ for _ in ()).throw(RuntimeError("owner check broke")))
    with pytest.raises(RuntimeError):
        turn(a, 3, "#yuanwei-nomem", author_id="u1")
    assert a.win._suspect == set() and a._turn_failed == {}                                  # 认不出是不是本人：不替本人动窗口，原来的异常照抛


def test_resume_recheck_failure_leaves_no_half_state_and_the_flag_stays(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    mem = organized(tmp_path, host="hermes")
    made = {"fail": False}
    (tmp_path / "m2").mkdir(exist_ok=True)

    def factory(key):
        if key == "proj:other" and made["fail"]:
            raise OSError("库打不开")
        return mem if key != "proj:other" else organized(tmp_path / "m2", host="hermes", ws="proj:other")
    a = HermesAdapter({"spawn": "off"}, memory_factory=factory, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(a, 1, "普通一句")
    old = (a.ws, a.mem, a.acc, a.ids, a.runner, a._surfaces, a.win)
    a._cwd_recheck = True
    monkeypatch.setattr(HW, "resolve", lambda home, cwd, plat, on_drop=None: {"key": "proj:other", "kind": "project", "label": "别的项目", "reason": "test"})
    made["fail"] = True
    with pytest.raises(OSError):
        turn(a, 2, "恢复以后的第一句")
    assert (a.ws, a.mem, a.acc, a.ids, a.runner, a._surfaces, a.win) == old                # 工作区和库一起退回，没有「工作区换了、库没换」
    assert a._cwd_recheck is True
    made["fail"] = False
    turn(a, 3, "再来")                                                                    # 下一轮再试，成功以后才置 False
    assert a._cwd_recheck is False and a.ws["key"] == "proj:other"
