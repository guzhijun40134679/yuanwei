"""清单第 56 条第一批·第 8 节：没见过的会话一开始就带着历史、而库里有窗口关着 → 新窗口从「关」开始。
假宿主，临时库，不读 Hermes 的 state.db，不联网。"""
import contextlib
import json
import sqlite3

import pytest

from helpers2 import organized
from yuanwei import window as WN
from yuanwei.hermes_adapter import HermesAdapter

Q = {"query": "烤鱼"}


def turn(a, n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


def call(a, name, args=None):
    return json.loads(a.handle_tool_call(name, args or {}))


def boot(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return organized(tmp_path, host="hermes")


def start(tmp_path, mem, session, **kw):
    a = HermesAdapter({"mode": "tools_only", "spawn": "off"}, memory=mem, executor="inline")
    a.initialize(session, platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"), **kw)
    return a


def closed_library(tmp_path, close=True):
    """原来的对话 hs1：用过一轮；close 为真就关着。"""
    mem = boot(tmp_path)
    a = start(tmp_path, mem, "hs1")
    turn(a, 1, "#yuanwei-nomem" if close else "你好")
    return mem, a


def row(mem, sid):
    return mem.store.conn.execute("SELECT w.state, w.origin, w.parent_window_id, w.ctl_source, w.control_version FROM window_bindings b JOIN windows w ON w.window_id=b.window_id WHERE b.session_id=?", (sid,)).fetchone()


def shown(b):
    return [t for t in b._display] + [t for t in b._notices]


def test_branch_of_a_closed_chat_starts_closed(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "分出来以后的第一句")
    r = tuple(row(mem, "hs2"))
    assert r == ("off", "fork", None, "history_default", 1)
    x = call(b, "recall", Q)
    assert x["status"] == "refused" and x["reason"] == "window_closed"
    assert row(mem, "hs1")["state"] == "off"                                              # 原来的窗口不受影响


def test_message_count_two_or_more_alone_counts_as_history(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", message_count=2)
    turn(b, 1, "第一句")
    assert row(mem, "hs2")["state"] == "off"
    c = start(tmp_path, mem, "hs3", resumed=False, message_count=9)
    turn(c, 1, "第一句")
    assert row(mem, "hs3")["state"] == "off"


def test_nothing_closed_in_the_library_means_open_as_today(tmp_path):
    mem, a = closed_library(tmp_path, close=False)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "第一句")
    assert tuple(row(mem, "hs2")) == ("on", "new", None, "default", 1)
    assert call(b, "recall", Q)["status"] == "ok"
    assert b._notices == [] or WN.NOTICE_HISTORY_CLOSED not in b._notices


@pytest.mark.parametrize("kw", [{}, {"resumed": False, "message_count": 0}, {"resumed": False, "message_count": 1}, {"message_count": 1}])
def test_a_brand_new_chat_without_history_starts_open_even_if_another_window_is_closed(tmp_path, kw):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", **kw)
    turn(b, 1, "第一句")
    assert tuple(row(mem, "hs2")) == ("on", "new", None, "default", 1)
    assert call(b, "recall", Q)["status"] == "ok"
    assert WN.NOTICE_HISTORY_CLOSED not in shown(b) and not any(WN.NOTICE_HISTORY_CLOSED in t for t in b._display)


@pytest.mark.parametrize("kw", [{"resumed": "yes"}, {"resumed": 1}, {"resumed": None}, {"message_count": "5"}, {"message_count": True}, {"message_count": 3.0}, {"message_count": None},
                                {"resumed": "true", "message_count": [4]}])
def test_resumed_or_message_count_missing_or_of_the_wrong_type_counts_as_no_history(tmp_path, kw):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", **kw)
    turn(b, 1, "第一句")
    assert row(mem, "hs2")["state"] == "on"


def test_a_session_that_is_already_bound_is_not_touched_when_it_restarts_with_history(tmp_path):
    mem, a = closed_library(tmp_path, close=False)
    b = start(tmp_path, mem, "hs2")
    turn(b, 1, "新对话")
    assert row(mem, "hs2")["state"] == "on"
    turn(a, 2, "#yuanwei-nomem")                                                           # 现在有窗口关着了
    b2 = start(tmp_path, mem, "hs2", resumed=True, message_count=6)                        # hs2 重启：已绑过，原样返回，不变成关
    turn(b2, 3, "接着聊")
    assert tuple(row(mem, "hs2")) == ("on", "new", None, "default", 1)
    assert not any(WN.NOTICE_HISTORY_CLOSED in t for t in b2._display)
    a2 = start(tmp_path, mem, "hs1", resumed=True, message_count=6)                        # 关着的老会话重启：还是关，也没有这句提示
    turn(a2, 3, "接着聊")
    assert row(mem, "hs1")["state"] == "off" and row(mem, "hs1")["origin"] == "new"
    assert not any(WN.NOTICE_HISTORY_CLOSED in t for t in a2._display)


def test_the_notice_is_given_once_and_not_again_after_a_restart(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "第一句")
    assert b._notices.count(WN.NOTICE_HISTORY_CLOSED) == 1 and b._display == ["〔原委〕" + WN.NOTICE_HISTORY_CLOSED]
    assert "#yuanwei-mem" in WN.NOTICE_HISTORY_CLOSED and "带着以前的内容" in WN.NOTICE_HISTORY_CLOSED
    assert b.display_hook(response_text="好的").endswith(WN.NOTICE_HISTORY_CLOSED)
    turn(b, 2, "第二句")
    assert b._notices.count(WN.NOTICE_HISTORY_CLOSED) == 1 and b._display == []
    b2 = start(tmp_path, mem, "hs2", resumed=True, message_count=6)                        # 进程重启
    turn(b2, 3, "第三句")
    assert WN.NOTICE_HISTORY_CLOSED not in b2._notices and b2._display == []


def test_the_user_can_open_it_with_the_command_and_the_close_tools_still_work(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "#yuanwei-mem")
    assert row(mem, "hs2")["state"] == "on" and row(mem, "hs2")["control_version"] == 2
    assert call(b, "recall", Q)["status"] == "ok"
    r = call(b, "window_off")
    assert r["state"] == "off" and r["reason"] == "closed_by_tool"
    turn(b, 2, "再说一句")
    assert call(b, "recall", Q)["reason"] == "window_closed"
    turn(b, 3, "#yuanwei-mem")
    assert call(b, "recall", Q)["status"] == "ok"


def test_delete_request_and_confirm_work_in_a_history_closed_window(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "请把这段对话在原委里的记忆删掉")
    r = call(b, "window_delete")
    assert r["state"] == "waiting" and r["reason"] == "awaiting_delete_confirmation"
    turn(b, 2, "确认删除")
    assert mem.store.conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 1
    assert row(mem, "hs2")["state"] == "off"                                               # 删除没有把读取重新打开
    assert row(mem, "hs1")["state"] == "off"
    v = b.win.view("hermes", "hs2")
    assert v.state == "off" and v.reason in ("closed_by_history_default", "awaiting_confirmation")


def test_status_view_of_the_window_names_the_source(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    turn(b, 1, "第一句")
    v = b.win.view("hermes", "hs2")
    assert (v.state, v.reason) == ("off", "closed_by_history_default")
    assert b.status()["window"]["state"] == "off" and b.status()["window"]["paused"] is False


def test_a_read_error_while_judging_binds_nothing_reads_stop_and_the_next_turn_binds(tmp_path, monkeypatch):
    mem, a = closed_library(tmp_path)
    real_tx = mem.store.tx
    state = {"fail": True}

    @contextlib.contextmanager
    def tx():
        with real_tx() as c:
            class P:
                def __getattr__(self, k):
                    return getattr(c, k)

                def execute(self, sql, *args):
                    if state["fail"] and "state='off'" in sql:
                        raise sqlite3.OperationalError("disk I/O error")
                    return c.execute(sql, *args)
            yield P()
    monkeypatch.setattr(mem.store, "tx", tx)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)                         # 本机平台在 initialize 里就绑；这里判断读库出错
    assert row(mem, "hs2") is None
    turn(b, 1, "第一句")                                                                    # 第一轮再试，还是出错
    assert row(mem, "hs2") is None                                                         # 没绑，也没默认开
    assert b._win_ref is None and b._bound_session is None
    x = call(b, "recall", Q)
    assert x["status"] == "refused" and x["reason"] in ("window_identity_pending", "identity_unbound")
    assert not any(WN.NOTICE_HISTORY_CLOSED in t for t in b._display)
    state["fail"] = False
    turn(b, 2, "第二句")                                                                    # 下一轮能绑，而且从关开始
    assert tuple(row(mem, "hs2")) == ("off", "fork", None, "history_default", 1)
    assert call(b, "recall", Q)["reason"] == "window_closed"
    assert b._display == ["〔原委〕" + WN.NOTICE_HISTORY_CLOSED]


def test_remote_platform_binds_at_the_first_owner_message_and_the_history_flag_follows_the_session(tmp_path):
    mem, a = closed_library(tmp_path)
    r = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    r.initialize("sT", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1", chat_type="dm", chat_id="1", resumed=True, message_count=4)
    assert row(mem, "sT") is None                                                          # 本人消息到来之前不建绑定
    turn(r, 1, "第一句", author_id="u1")
    assert tuple(row(mem, "sT")) == ("off", "fork", None, "history_default", 1)
    assert r._display == ["〔原委〕" + WN.NOTICE_HISTORY_CLOSED]
    assert call(r, "recall", Q)["status"] == "refused"


def test_remote_non_owner_never_creates_a_window(tmp_path):
    mem, a = closed_library(tmp_path)
    r = HermesAdapter({"spawn": "off", "owners": {"telegram": ["u1"]}}, memory=mem, executor="inline")
    r.initialize("sT", platform="telegram", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u2", chat_type="dm", chat_id="2", resumed=True, message_count=4)
    turn(r, 1, "我不是主人", author_id="u2")
    assert row(mem, "sT") is None


def test_bind_new_core_rules(tmp_path):
    mem, a = closed_library(tmp_path)
    w = a.win
    r = w.bind_new("hermes", "x1", "", history=True)
    assert r.state == "off" and r.history_closed and r.created
    assert w.bind_new("hermes", "x1", "", history=False).state == "off"                    # 已绑过：原样返回
    assert not w.bind_new("hermes", "x1", "", history=True).history_closed
    assert w.bind_new("hermes", "x2", "", history=False).state == "on"
    assert w.bind_new("hermes", "x3", "", history="yes").state == "on"                      # 不是布尔真：当没有
    assert w.bind_new("hermes", "x4", "", via="reset").state == "on"                        # 其他调用路径不变
    assert w.bind_new("hermes", "x5", "", history=True, state="on").state == "on"           # 调用方明确指定了状态：照指定
    assert mem.store.conn.execute("SELECT COUNT(*) FROM window_ops").fetchone()[0] == 1    # 只有第一次的关，没有多记操作


def test_compression_child_with_history_follows_the_closed_parent(tmp_path):
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs1-child", resumed=True, message_count=8)                  # 轮外压缩换号：宿主直接给子会话 initialize，不通知我们
    turn(b, 1, "压缩以后的第一句")
    assert row(mem, "hs1-child")["state"] == "off"
    assert call(b, "recall", Q)["status"] == "refused"


def test_the_history_flag_belongs_to_the_session_it_was_given_for(tmp_path):
    """防御：initialize 的历史标记只对那个会话有效；当前会话号变了（别的路径换号）就不沿用。"""
    mem, a = closed_library(tmp_path)
    b = start(tmp_path, mem, "hs2", resumed=True, message_count=4)
    b._display.clear()
    b._notices.clear()
    b.session = "hs-other"
    b._bound_session = None
    b._prepare_owner_workspace()
    assert row(mem, "hs-other")["state"] == "on"
    assert b._display == [] and b._notices == []
