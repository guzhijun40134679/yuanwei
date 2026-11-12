"""窗口层（yuanwei.window）：存储 v10、宿主编号到窗口的映射、关（口令、规则、工具）、开（口令；agent 申请＋用户回复「确认开启」）、待确认、读取许可。
设计第 2 版＋主会话裁定一、二（11-10）。全部自造文字，假时钟，没有网络。"""
import json
import sqlite3

import pytest

from yuanwei import sqlstore as SQ
from yuanwei import window as W
from yuanwei.access import ToolIdentity
from window_open_table import CONFIRM_OK, NOT_CONFIRM, ROWS

H = "hermes"


class Clk:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.t))

    def tick(self, s):
        self.t += s


def mk(tmp_path, name="w.db", **kw):
    st = SQ.Store(str(tmp_path / name))
    clk = Clk()
    return W.WindowService(st, clock=clk, **kw), st, clk


_n = [0]


def ref(session="S1", text_key=None, branch="", ns="ns1"):
    _n[0] += 1
    return W.InputRef(H, session, "t%d" % _n[0], text_key or "k%d" % _n[0], "fp%d" % _n[0], branch, ns)


def feed(svc, text, session="S1", origin=True, **kw):
    return svc.process_input(ref(session), text, origin_verified=origin, **kw)


def state(svc, session="S1"):
    return svc.view(H, session).state


def ident(session="S1", turn="T1", branch=""):
    return ToolIdentity(H, session, branch, turn)


def request(svc, session="S1", turn="T1"):
    return svc.request_open(ident(session, turn), ns="ns1")


# ================================================================== 存储 v10
@pytest.mark.parametrize("old", list(range(1, 10)))
def test_v10_opens_any_older_db_and_adds_empty_window_tables(tmp_path, monkeypatch, old):
    p = str(tmp_path / "old.db")
    monkeypatch.setattr(SQ, "SCHEMA_VERSION", old)
    s = SQ.Store(p)
    assert s.schema_version() == old
    names_before = {r["name"] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    s.conn.close()
    monkeypatch.undo()
    s2 = SQ.Store(p)
    assert s2.schema_version() == 12 == SQ.SCHEMA_VERSION
    names = {r["name"] for r in s2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names_before <= names                                                              # 旧表一张不少
    for t in ("windows", "window_bindings", "window_inputs", "window_ops", "window_permits", "window_pending"):
        assert t in names and s2.conn.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()["n"] == 0


def test_window_tables_are_not_partitioned_by_workspace(tmp_path):
    s = SQ.Store(str(tmp_path / "a.db"))
    for t in ("windows", "window_bindings", "window_ops"):
        assert "ns" not in [r["name"] for r in s.conn.execute("PRAGMA table_info(%s)" % t)], t
    for t in ("window_inputs", "window_permits", "window_pending"):                            # 这三张的 ns 只是信息列，不在主键里
        pk = [r["name"] for r in s.conn.execute("PRAGMA table_info(%s)" % t) if r["pk"]]
        assert "ns" not in pk, t


def test_two_workspaces_in_one_db_file_share_one_window_state(tmp_path):
    p = str(tmp_path / "shared.db")
    a, b = W.WindowService(SQ.Store(p)), W.WindowService(SQ.Store(p))                          # 两个工作区的 Memory 各有自己的连接，同一个库文件
    a.bind_new(H, "S1")
    feed(a, "别用记忆")
    assert b.view(H, "S1").state == "off" and a.view(H, "S1").state == "off"
    feed(b, "#yuanwei-mem")
    assert a.view(H, "S1").state == "on"


# ================================================================== 映射
def test_unbound_identity_is_pending_not_open(tmp_path):
    svc, _, _ = mk(tmp_path)
    r = svc.resolve(H, "never-seen")
    assert r.paused and r.pause_reason == "window_identity_pending" and r.window_id is None
    assert svc.view(H, "never-seen") == W.WindowView("off", False, "identity_pending", 0)
    assert svc.bind_restore(H, "never-seen").paused                                              # 恢复不创建
    assert svc.resolve(H, "never-seen").paused


def test_bind_new_is_idempotent_and_uses_the_default_state(tmp_path):
    svc, _, _ = mk(tmp_path, default_state="off")
    a = svc.bind_new(H, "S1")
    assert a.created and a.state == "off" and a.control_version == 1
    b = svc.bind_new(H, "S1")
    assert not b.created and b.window_id == a.window_id
    assert mk(tmp_path, name="x.db")[0].bind_new(H, "S2").state == "on"                          # 默认值是开


def test_binding_is_immutable(tmp_path):
    svc, st, _ = mk(tmp_path)
    a = svc.bind_new(H, "S1")
    svc.bind_new(H, "S2")
    r = svc.bind_same(H, "S1", "S2")                                                             # 已绑定的号：原样返回，不改去别的窗口
    assert r.window_id == a.window_id
    assert svc.bind_fork(H, "S1", "S2").window_id == a.window_id


def test_compression_keeps_the_same_window_and_a_closed_one_stays_closed(tmp_path):
    svc, _, _ = mk(tmp_path)
    a = svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    r = svc.bind_same(H, "S2", "S1", via="compression")
    assert r.window_id == a.window_id and r.state == "off" and svc.view(H, "S2").state == "off"
    assert svc.bind_same(H, "S2", "S1").window_id == a.window_id                                  # 重复：幂等
    assert svc.bind_same(H, "S1", "S1").window_id == a.window_id                                  # 就地压缩（号没变）：什么也不做


def test_compression_from_an_unbound_parent_pauses(tmp_path):
    svc, _, _ = mk(tmp_path)
    r = svc.bind_same(H, "S2", "Sx")
    assert r.paused and r.pause_reason == "window_identity_pending"


def test_branch_forks_a_copy_of_the_current_state_and_each_side_goes_its_own_way(tmp_path):
    svc, st, _ = mk(tmp_path)
    p = svc.bind_new(H, "P")
    feed(svc, "别用记忆", "P")
    c = svc.bind_fork(H, "C", "P")
    assert c.window_id != p.window_id and c.state == "off" and c.control_version == 1            # 复制分叉那一刻的状态；父关着，分出来的不会自己变开
    row = st.conn.execute("SELECT * FROM windows WHERE window_id=?", (c.window_id,)).fetchone()
    assert row["parent_window_id"] == p.window_id and row["origin"] == "fork" and row["ctl_source"] == "fork_copy"
    feed(svc, "#yuanwei-mem", "C")
    assert svc.view(H, "C").state == "on" and svc.view(H, "P").state == "off"                    # 之后各管各的
    feed(svc, "#yuanwei-nomem", "C")
    feed(svc, "#yuanwei-mem", "P")
    assert svc.view(H, "C").state == "off" and svc.view(H, "P").state == "on"


def test_fork_from_an_unbound_parent_pauses(tmp_path):
    svc, _, _ = mk(tmp_path)
    assert svc.bind_fork(H, "C", "Px").paused


def test_reset_makes_a_new_window_with_the_default_and_does_not_inherit_the_close(tmp_path):
    svc, st, _ = mk(tmp_path)
    p = svc.bind_new(H, "P")
    feed(svc, "别用记忆", "P")
    n = svc.bind_new(H, "N", via="reset")
    assert n.window_id != p.window_id and n.state == "on"
    assert st.conn.execute("SELECT parent_window_id FROM windows WHERE window_id=?", (n.window_id,)).fetchone()[0] is None


def test_restore_known_maps_to_that_sessions_window_unknown_pauses(tmp_path):
    svc, _, _ = mk(tmp_path)
    a = svc.bind_new(H, "A")
    feed(svc, "别用记忆", "A")
    svc.bind_new(H, "B")
    assert svc.bind_restore(H, "A").window_id == a.window_id and svc.bind_restore(H, "A").state == "off"   # 回到一个关着的窗口：仍是关着
    assert svc.bind_restore(H, "Z").paused


def test_branch_id_alone_does_not_change_the_window(tmp_path):
    svc, _, _ = mk(tmp_path)
    a = svc.bind_new(H, "S1", "")
    r = svc.bind_same(H, "S1", "S1", "", new_branch="b2", via="branch_id")                       # 同一窗口只换分支标识：仍是同一个逻辑窗口
    assert r.window_id == a.window_id
    feed(svc, "别用记忆")
    assert svc.resolve(H, "S1", "b2").state == "off"


def test_subagent_cannot_resolve_open_or_confirm(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    assert svc.resolve(H, "S1", actor="subagent").pause_reason == "window_subagent"
    n = st.conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    r = svc.process_input(ref(), "#yuanwei-mem", origin_verified=True, actor="subagent")
    assert r.outcome == "paused_identity" and r.reason == "window_subagent"
    r = svc.process_input(ref(), "别用记忆", origin_verified=True, actor="subagent")
    assert r.outcome == "paused_identity"
    assert st.conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == n and state(svc) == "on"


def test_pending_identity_is_resolved_only_by_an_explicit_command(tmp_path):
    svc, _, _ = mk(tmp_path)
    r = feed(svc, "你好，今天吃什么", "Z")
    assert r.outcome == "paused_identity" and svc.resolve(H, "Z").paused
    r = feed(svc, "别用记忆", "Z")                                                                  # 用户明确的关：为这个号建立绑定
    assert r.outcome == "closed" and svc.view(H, "Z").state == "off"
    feed(svc, "确认开启", "Y")
    assert svc.resolve(H, "Y").paused                                                             # 光说确认开启建立不了绑定
    assert feed(svc, "#yuanwei-mem", "X").outcome == "opened" and svc.view(H, "X").state == "on"


# ================================================================== 关：口令、规则、工具
def test_close_by_command_rule_and_tool_each_close_and_bump_the_version_once(tmp_path):
    for how in ("command", "rule", "tool"):
        svc, _, _ = mk(tmp_path, name="%s.db" % how)
        w = svc.bind_new(H, "S1")
        if how == "command":
            r = feed(svc, "#yuanwei-nomem\n帮我查一下")
            assert r.steps[0][:2] == ("close", "command")
        elif how == "rule":
            r = feed(svc, "这次不要参考我的记忆")
            assert r.steps[0][:2] == ("close", "rule")
        else:
            r = svc.close(w.window_id, source="tool", op_id="op-x")
            assert r.outcome == "applied"
        v = svc.view(H, "S1")
        assert v.state == "off" and v.control_version == 2 and v.reason == "closed_by_%s" % how


def test_close_is_idempotent_and_never_bumps_on_a_closed_window(tmp_path):
    svc, st, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    a = svc.close(w.window_id, source="tool", op_id="op-1")
    b = svc.close(w.window_id, source="tool", op_id="op-1")                                       # 同一操作编号重送
    c = svc.close(w.window_id, source="rule", op_id="op-2")                                       # 已关着再关（新编号）
    assert (a.outcome, a.replayed) == ("applied", False) and (b.outcome, b.replayed) == ("applied", True) and c.outcome == "noop"
    assert svc.view(H, "S1").control_version == 2
    row = st.conn.execute("SELECT ctl_source, ctl_op_id FROM windows").fetchone()
    assert (row["ctl_source"], row["ctl_op_id"]) == ("rule", "op-2")                               # 来源记最新一次
    assert st.conn.execute("SELECT COUNT(*) FROM window_ops WHERE op_id='op-1'").fetchone()[0] == 1


def test_hook_closes_first_then_server_confirms_with_the_same_op_id(tmp_path):
    svc, st, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    svc.close(w.window_id, source="tool", op_id="op-h", input_win_seq=1)                          # 接头先关
    r = svc.close(w.window_id, source="tool", op_id="op-h", input_win_seq=1)                      # 服务端再确认：同一个操作编号
    assert r.replayed and svc.view(H, "S1").control_version == 2
    assert st.conn.execute("SELECT confirmed_at FROM window_ops WHERE op_id='op-h'").fetchone()[0] is not None


def test_a_late_old_close_cannot_close_a_window_that_was_validly_opened_later(tmp_path):
    svc, st, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    feed(svc, "别用记忆")                                                                          # 输入序号 1，关
    feed(svc, "#yuanwei-mem")                                                                      # 输入序号 2，开
    assert state(svc) == "on"
    r = svc.close(w.window_id, source="tool", op_id="op-late", input_win_seq=1)                   # 迟到的、依据输入 1 的旧关闭确认
    assert r.outcome == "stale_ignored" and state(svc) == "on"
    assert st.conn.execute("SELECT outcome FROM window_ops WHERE op_id='op-late'").fetchone()[0] == "stale_ignored"
    assert svc.close(w.window_id, source="tool", op_id="op-new", input_win_seq=3).outcome == "applied"   # 之后更新的输入照样能关


def test_origin_not_verified_changes_nothing_not_even_a_ban(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    assert feed(svc, "#yuanwei-nomem", origin=False).outcome == "unchanged" and state(svc) == "on"
    feed(svc, "#yuanwei-nomem")
    r = feed(svc, "#yuanwei-mem", origin=False)                                                    # 通知、机器人、来源核不出来：口令打不开
    assert r.outcome == "unchanged" and r.reason == "origin_unverified" and state(svc) == "off"


def test_same_input_resent_returns_the_same_result_without_changing_state_again(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    r1 = ref()
    a = svc.process_input(r1, "别用记忆", origin_verified=True)
    b = svc.process_input(r1, "别用记忆", origin_verified=True)
    assert a == b and svc.view(H, "S1").control_version == 2


def test_mem_command_only_counts_on_the_first_physical_line(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "#yuanwei-nomem")
    for t in ("请问 #yuanwei-mem 是什么", "你好\n#yuanwei-mem", "\n#yuanwei-mem", " #yuanwei-mem", "> #yuanwei-mem", "`#yuanwei-mem`", "```\n#yuanwei-mem\n```", "“#yuanwei-mem”", "#yuanwei-mem吧", "#yuanwei-memx"):
        feed(svc, t)
        assert state(svc) == "off", t
    for t in ("#yuanwei-mem", "#YuanWei-MEM", "#yuanwei-mem   ", "#yuanwei-mem\r\n你好"):
        feed(svc, "#yuanwei-nomem")
        r = feed(svc, t)
        assert r.outcome == "opened" and state(svc) == "on", t


def test_mem_command_followed_by_a_ban_line_ends_closed_and_records_both_steps(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "#yuanwei-nomem")
    r = feed(svc, "#yuanwei-mem\n别用记忆")
    assert [s[:2] for s in r.steps] == [("open", "command"), ("close", "rule")] and state(svc) == "off"     # 同一条输入里先开后关：最后的表态生效，记两步


def test_ban_after_a_prior_close_in_one_input_then_command_open_is_not_possible(tmp_path):
    """口令永远在第一行，所以「先关后开」在同一条输入里只会是：口令开、后面规则关。第一行是禁读口令时，后面的话不能把它翻回去。"""
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "#yuanwei-nomem\n#yuanwei-mem")
    assert state(svc) == "off"


def test_malformed_command_does_not_touch_the_window(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    r = feed(svc, "#yuanwei-mem 加点字")
    assert r.command == "malformed" and state(svc) == "on" and r.outcome == "unchanged"


def test_command_open_is_blocked_by_a_setting_and_the_window_stays_closed(tmp_path):
    blocked = {"v": "session"}
    svc, _, _ = mk(tmp_path, settings=lambda h, s: blocked["v"])
    svc.bind_new(H, "S1")
    feed(svc, "#yuanwei-nomem")
    r = feed(svc, "#yuanwei-mem")
    assert r.outcome == "blocked_by_setting" and r.reason == "setting_session" and state(svc) == "off"
    v = svc.view(H, "S1")
    assert v.blocked_by_setting and v.reason == "setting_session" and v.state == "off"             # 窗口状态和设置分开报
    blocked["v"] = None
    assert feed(svc, "#yuanwei-mem").outcome == "opened"


# ================================================================== 关闭写失败：回执不能说「已关」
def test_close_write_failure_is_reported_not_claimed_and_reads_are_blocked_until_a_close_lands(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")

    def boom(point):
        raise sqlite3.OperationalError("disk I/O error")
    svc.fault = boom
    r = feed(svc, "#yuanwei-nomem")
    assert r.outcome == "close_failed" and r.reason == "state_write_failed"
    assert "已关闭" not in (r.receipt or "") and "没能确认" in r.receipt and r.notice is None     # 回执不能说「已关」
    rr = svc.resolve(H, "S1")
    assert rr.paused and rr.pause_reason == "window_close_unconfirmed"                              # 没确认已关：进程内一律不许读（不是「开」）
    assert svc.issue_read_permit(ident(), tool_name="recall", params_fp="f").refused == "window_close_unconfirmed"
    svc.fault = None
    r = feed(svc, "再说一次：别用记忆")                                                             # 之后一次成功的关闭落地：解除
    assert r.outcome == "closed" and state(svc) == "off" and not svc.resolve(H, "S1").paused


def test_unreadable_window_state_means_paused_not_open(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    st.conn.execute("DROP TABLE window_bindings")
    r = svc.resolve(H, "S1")
    assert r.paused and r.pause_reason == "window_state_unreadable"
    assert svc.issue_read_permit(ident(), tool_name="recall", params_fp="f").secret is None


# ================================================================== 开：agent 申请＋用户回复「确认开启」
def test_request_open_states_on_blocked_and_waiting(tmp_path):
    blocked = {"v": None}
    svc, st, _ = mk(tmp_path, settings=lambda h, s: blocked["v"])
    svc.bind_new(H, "S1")
    o = request(svc)
    assert (o.status, o.state, o.reason) == ("on", "on", "ok") and o.prompt is None                  # 开着：直接返回，不登记
    assert st.conn.execute("SELECT COUNT(*) FROM window_pending").fetchone()[0] == 0
    feed(svc, "别用记忆")
    blocked["v"] = "workspace"
    o = request(svc)
    assert (o.status, o.blocked_by_setting, o.reason) == ("blocked_by_setting", True, "setting_workspace")      # 被设置挡住：不登记申请
    assert st.conn.execute("SELECT COUNT(*) FROM window_pending").fetchone()[0] == 0
    blocked["v"] = None
    o = request(svc)
    assert (o.status, o.state, o.reason, o.prompt) == ("waiting", "off", "awaiting_confirmation", "要在这个窗口重新使用长期记忆吗？回复「确认开启」即可。")
    assert state(svc) == "off" and svc.view(H, "S1").reason == "awaiting_confirmation"


def test_waiting_row_records_window_version_request_turn_seq_time_and_state(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc, turn="T-req")
    p = st.conn.execute("SELECT * FROM window_pending").fetchone()
    w = st.conn.execute("SELECT * FROM windows").fetchone()
    assert (p["window_id"], p["control_version"], p["turn_id"], p["request_win_seq"], p["state"]) == (w["window_id"], 2, "T-req", 1, "waiting")
    assert p["requested_at"] and p["host"] == H and p["session_id"] == "S1" and p["ns"] == "ns1"


@pytest.mark.parametrize("line", CONFIRM_OK)
def test_confirm_lines_open_the_window_once(tmp_path, line):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    v0 = svc.view(H, "S1").control_version
    r = feed(svc, line)
    assert r.outcome == "opened" and r.command == "confirm" and r.notice == W.NOTICE_OPENED
    v = svc.view(H, "S1")
    assert v.state == "on" and v.control_version == v0 + 1                                          # 控制版本加一
    assert st.conn.execute("SELECT state FROM window_pending").fetchone()[0] == "opened"            # 待确认清掉
    assert feed(svc, "确认开启").outcome == "unchanged"                                              # 再说没有用


@pytest.mark.parametrize("line", NOT_CONFIRM)
def test_not_a_confirm_line_voids_the_pending_and_the_window_stays_closed(tmp_path, line):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    r = feed(svc, line)
    assert state(svc) == "off" and r.outcome != "opened"
    assert st.conn.execute("SELECT state, void_reason FROM window_pending").fetchone()[:] == ("void", "next_input_not_confirm")
    assert r.receipt == W.NOT_CONFIRMED_RECEIPT == "记忆仍是关着的；要打开，请让我再申请一次后回复「确认开启」，或者直接发 #yuanwei-mem。"
    assert feed(svc, "确认开启").outcome == "unchanged" and state(svc) == "off"                      # 待确认已作废：之后的确认话没有用


def test_saying_confirm_without_a_pending_request_does_nothing(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    r = feed(svc, "确认开启")
    assert state(svc) == "off" and r.outcome == "unchanged" and r.receipt is None and r.command == "none"
    assert st.conn.execute("SELECT COUNT(*) FROM window_ops WHERE kind='open'").fetchone()[0] == 0


def test_confirm_only_counts_on_the_very_next_input_of_the_same_window(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    svc.bind_new(H, "S2")
    feed(svc, "别用记忆", "S1")
    feed(svc, "别用记忆", "S2")
    request(svc, "S1")
    feed(svc, "确认开启", "S2")                                                                      # 别的窗口的输入确认不了
    assert state(svc, "S1") == "off" and state(svc, "S2") == "off"
    assert feed(svc, "确认开启", "S1").outcome == "opened"


def test_confirm_line_followed_by_a_ban_line_is_still_processed_in_order(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    r = feed(svc, "确认开启\n别用记忆了")                                                             # 第一行后面还有别的话，照常处理
    assert [s[:2] for s in r.steps] == [("open", "confirm"), ("close", "rule")] and state(svc) == "off"


def test_the_confirm_turn_itself_can_read(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    feed(svc, "确认开启\n帮我查一下上次的端口")
    assert svc.issue_read_permit(ident(), tool_name="recall", params_fp="f").secret                  # 这一轮就已经能读了


def test_pending_is_voided_when_the_window_is_closed_again_or_the_version_changes(tmp_path):
    svc, st, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    svc.close(w.window_id, source="tool", op_id="op-again")                                           # 已经关着又被关了一次
    assert st.conn.execute("SELECT state, void_reason FROM window_pending").fetchone()[:] == ("void", "closed_again")
    assert feed(svc, "确认开启").outcome == "unchanged"
    request(svc)
    feed(svc, "#yuanwei-mem")                                                                        # 控制版本变了（口令开）
    feed(svc, "#yuanwei-nomem")
    assert st.conn.execute("SELECT COUNT(*) FROM window_pending WHERE state='waiting'").fetchone()[0] == 0
    assert feed(svc, "确认开启").outcome == "unchanged"


def test_a_new_request_supersedes_the_old_one(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc, turn="T1")
    request(svc, turn="T2")
    assert [r[:] for r in st.conn.execute("SELECT turn_id, state FROM window_pending ORDER BY rowid")] == [("T1", "void"), ("T2", "waiting")]
    assert feed(svc, "确认开启").outcome == "opened"


def test_the_confirm_survives_a_process_restart_but_only_for_the_next_input(tmp_path):
    p = str(tmp_path / "r.db")
    a = W.WindowService(SQ.Store(p))
    a.bind_new(H, "S1")
    feed(a, "别用记忆")
    request(a)
    b = W.WindowService(SQ.Store(p))                                                                 # 进程重启：新服务、同一个库
    assert b.view(H, "S1").reason == "awaiting_confirmation"
    assert feed(b, "确认开启").outcome == "opened"
    c = W.WindowService(SQ.Store(p))
    feed(c, "别用记忆")
    request(c)
    d = W.WindowService(SQ.Store(p))
    feed(d, "随便聊一句")                                                                              # 重启以后的下一条不是确认：作废
    assert feed(W.WindowService(SQ.Store(p)), "确认开启").outcome == "unchanged" and state(d) == "off"


def test_a_setting_blocks_the_confirm_too(tmp_path):
    blocked = {"v": None}
    svc, st, _ = mk(tmp_path, settings=lambda h, s: blocked["v"])
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    request(svc)
    blocked["v"] = "session"
    r = feed(svc, "确认开启")
    assert r.outcome == "blocked_by_setting" and state(svc) == "off"
    assert st.conn.execute("SELECT state, void_reason FROM window_pending").fetchone()[:] == ("void", "blocked_by_setting")


def test_agent_cannot_confirm_by_any_tool_side_call(tmp_path):
    """agent 这一侧能做的只有 request_open（登记）；它没有任何办法让状态变开：反复申请、在同一轮里再调、不管参数。"""
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    for _ in range(3):
        assert request(svc).status == "waiting"
    assert state(svc) == "off" and st.conn.execute("SELECT COUNT(*) FROM window_ops WHERE kind='open'").fetchone()[0] == 0
    assert not hasattr(svc, "confirm") and not hasattr(svc, "request_open_with_quote")


def test_paused_or_subagent_windows_cannot_request(tmp_path):
    svc, _, _ = mk(tmp_path)
    o = svc.request_open(ident("Zz"), ns="n")
    assert o.status == "paused" and o.reason == "window_identity_pending" and o.prompt is None


# ================================================================== 判表：关着的窗口里，这些句子一句也打不开
def test_no_table_sentence_can_open_a_closed_window_with_or_without_a_pending_request(tmp_path):
    svc, _, _ = mk(tmp_path)
    for i, row in enumerate(ROWS):
        for pending in (False, True):
            s = "S%d%s" % (i, pending)
            svc.bind_new(H, s)
            feed(svc, "#yuanwei-nomem", s)
            if pending:
                request(svc, s)
            r = feed(svc, row["text"], s)
            assert state(svc, s) == "off", (row["id"], pending)
            assert not any(k == "open" and o == "applied" for k, _src, o in r.steps), row["id"]


# ================================================================== 读取许可
def test_no_read_permit_while_closed_but_a_control_permit_can_still_be_issued(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    p = svc.issue_read_permit(ident(), tool_name="recall", params_fp="f")
    assert p.secret is None and p.refused == "window_closed" and p.refusal_class == "overridable"   # 窗口关闭：能被有效开启覆盖
    c = svc.issue_control_permit(ident(), op="open", params_fp=W.params_fingerprint({}))
    assert c.secret and c.refused is None                                                              # 关着的时候也能调控制，否则永远打不开
    assert W.refusal_class("window_identity_pending") == "not_overridable" == W.refusal_class("workspace_setting")


def claim_args(**over):
    d = dict(kind="read", host=H, install_id="inst", session_id="S1", branch="", ns="ns1", turn_id="T1", tool_name="recall", params_fp="f", scope_set=("ns1",))
    d.update(over)
    return d


def issue(svc, **over):
    d = dict(tool_name="recall", params_fp="f", scope_set=("ns1",), install_id="inst", ns="ns1")
    d.update(over)
    return svc.issue_read_permit(ident(), **d)


def test_permit_is_one_use_and_claim_is_atomic(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    p = issue(svc)
    assert svc.claim_permit(p.secret, **claim_args()).ok
    again = svc.claim_permit(p.secret, **claim_args())
    assert not again.ok and again.reason == "already_used"
    assert svc.verify_before_return(p.secret).deliver
    assert not svc.claim_permit(p.secret, **claim_args()).ok


@pytest.mark.parametrize("field,value", [("host", "other"), ("install_id", "other"), ("session_id", "S2"), ("branch", "b"), ("ns", "ns2"), ("turn_id", "T2"),
                                         ("tool_name", "get_messages"), ("params_fp", "g"), ("scope_set", ("ns1", "ns2"))])
def test_permit_is_bound_to_each_field(tmp_path, field, value):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    p = issue(svc)
    r = svc.claim_permit(p.secret, **claim_args(**{field: value}))
    assert not r.ok and r.reason.startswith("mismatch_")
    assert svc.claim_permit(p.secret, **claim_args()).ok                                              # 绑定对不上只拒绝，不消耗


def test_permit_call_id_binding_and_expiry(tmp_path):
    svc, _, clk = mk(tmp_path)
    svc.bind_new(H, "S1")
    p = issue(svc, call_id="c-1")
    assert svc.claim_permit(p.secret, **claim_args(call_id="c-2")).reason == "mismatch_call_id"
    assert svc.claim_permit(p.secret, **claim_args(call_id="c-1")).ok
    q = issue(svc)
    clk.tick(10_000)
    assert svc.claim_permit(q.secret, **claim_args()).reason == "expired"


def test_unknown_and_wrong_kind_permits(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    assert svc.claim_permit("nope", **claim_args()).reason == "unknown_permit"
    assert svc.claim_permit("", **claim_args()).reason == "unknown_permit"
    rp = issue(svc)
    cp = svc.issue_control_permit(ident(), op="close", params_fp="f", install_id="inst", ns="ns1", scope_set=("ns1",))
    assert svc.claim_permit(cp.secret, **claim_args()).reason == "wrong_kind"                         # 控制许可不能拿来读
    assert svc.claim_permit(rp.secret, **claim_args(kind="control")).reason == "wrong_kind"           # 读取许可不能拿来开
    assert svc.claim_permit(rp.secret, **claim_args()).ok


def test_close_then_reopen_kills_the_old_permit_and_blocks_the_in_flight_result(tmp_path):
    svc, st, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    old = issue(svc)                                                                                   # 版本 1 签发
    feed(svc, "#yuanwei-nomem")                                                                        # 版本 2
    feed(svc, "#yuanwei-mem")                                                                          # 版本 3：现在又开着了
    r = svc.claim_permit(old.secret, **claim_args())
    assert not r.ok and r.reason in ("void", "version_changed")                                       # 旧许可永不复活
    inflight = issue(svc)
    assert svc.claim_permit(inflight.secret, **claim_args()).ok
    feed(svc, "#yuanwei-nomem")
    feed(svc, "#yuanwei-mem")                                                                          # 在途中关了又开：返回时现在是开着的，也不能交
    chk = svc.verify_before_return(inflight.secret)
    assert not chk.deliver
    assert st.conn.execute("SELECT state FROM window_permits WHERE permit_sha=?", (W._sha(inflight.secret),)).fetchone()[0] == "void"


def test_close_in_flight_blocks_the_result(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    p = issue(svc)
    assert svc.claim_permit(p.secret, **claim_args()).ok
    feed(svc, "别用记忆")
    chk = svc.verify_before_return(p.secret)
    assert not chk.deliver and chk.reason in ("permit_not_claimed", "window_closed")


def test_two_windows_same_query_permits_do_not_cross(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "A")
    svc.bind_new(H, "B")
    pa = svc.issue_read_permit(ident("A"), tool_name="recall", params_fp="same", scope_set=("ns1",), install_id="inst", ns="ns1")
    pb = svc.issue_read_permit(ident("B"), tool_name="recall", params_fp="same", scope_set=("ns1",), install_id="inst", ns="ns1")
    feed(svc, "别用记忆", "A")                                                                        # A 关了，B 还开着；两边在查同一句话
    a_try = svc.claim_permit(pa.secret, **claim_args(session_id="A", params_fp="same"))
    assert not a_try.ok                                                                                # A 排队中的调用拿不到结果
    cross = svc.claim_permit(pb.secret, **claim_args(session_id="A", params_fp="same"))
    assert not cross.ok and cross.reason == "mismatch_session_id"                                      # 拿 B 的许可冒充 A：对不上
    assert svc.claim_permit(pb.secret, **claim_args(session_id="B", params_fp="same")).ok


def test_params_fingerprint_ignores_the_permit_field_but_not_unknown_fields():
    a = W.params_fingerprint({"query": "x"})
    assert W.params_fingerprint({"query": "x", "permit": "model-filled"}) == a
    assert W.params_fingerprint({"query": "x", "surprise": 1}) != a


def test_control_permit_claim_is_atomic_and_issued_while_closed(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    feed(svc, "别用记忆")
    c = svc.issue_control_permit(ident(), op="open", params_fp="p", install_id="i", ns="ns1")
    a = dict(kind="control", host=H, install_id="i", session_id="S1", branch="", ns="ns1", turn_id="T1", tool_name="open", params_fp="p")
    assert svc.claim_permit(c.secret, **a).ok and svc.claim_permit(c.secret, **a).reason == "already_used"


def test_void_helpers(tmp_path):
    svc, _, _ = mk(tmp_path)
    w = svc.bind_new(H, "S1")
    p = issue(svc)
    assert svc.void_permit(p.secret, "test") and not svc.void_permit(p.secret, "test")
    q = issue(svc)
    assert svc.void_window_permits(w.window_id, "test") >= 1
    assert not svc.claim_permit(q.secret, **claim_args()).ok


def test_identity_without_a_turn_marker_gets_no_permit(tmp_path):
    svc, _, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    assert svc.issue_read_permit(ToolIdentity(H, "S1", "", None), tool_name="recall", params_fp="f").refused == "turn_unproven"
    assert svc.issue_read_permit(None, tool_name="recall", params_fp="f").refused == "identity_unbound"


def test_process_input_result_is_json_round_trippable_for_the_resend_path(tmp_path):
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    r = ref()
    a = svc.process_input(r, "#yuanwei-nomem\n别用记忆", origin_verified=True)
    json.dumps(a._asdict(), ensure_ascii=False)
    assert svc.process_input(r, "#yuanwei-nomem\n别用记忆", origin_verified=True) == a


def test_version_check_holds_even_if_nothing_voided_the_permit(tmp_path):
    """旧许可作废靠两道：关、开时把在途的许可标 void，以及认领和返回前各核一次控制版本。这里绕过第一道（直接改版本，不标 void），第二道仍要拦住。"""
    svc, st, _ = mk(tmp_path)
    svc.bind_new(H, "S1")
    p = issue(svc)
    st.conn.execute("UPDATE windows SET control_version=control_version+1")
    r = svc.claim_permit(p.secret, **claim_args())
    assert not r.ok and r.reason == "version_changed"
    q = issue(svc)
    assert svc.claim_permit(q.secret, **claim_args()).ok
    st.conn.execute("UPDATE windows SET control_version=control_version+1")
    chk = svc.verify_before_return(q.secret)
    assert not chk.deliver and chk.reason == "version_changed"
    z = issue(svc)
    assert svc.claim_permit(z.secret, **claim_args()).ok
    st.conn.execute("UPDATE windows SET state='off'")
    assert not svc.verify_before_return(z.secret).deliver
