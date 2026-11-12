"""身份绑定和读取权限（施工单第 2 节）：登记表的键、轮次交替、迟到、恢复／分叉、登记没完成、来源不明、重复事件、持续设置、连接句柄。
全部自造数据，没有网络。「拒绝」都要发生在搜索之前（用读取层计数器看）。"""
import pytest

from helpers import Clock, make_mem
from yuanwei import access as A
from yuanwei.access import Access, ToolIdentity

W1 = "本轮未新增读取长期记忆"
W2 = "已停止后续读取；此前已有材料交入本轮"
W3 = "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认"
W4 = "无法确认权限，已暂停新增读取"


_ACC = [None]


def mk(tmp_path, **kw):
    mem = make_mem(tmp_path, **kw)
    _ACC[0] = Access(mem)
    return mem, _ACC[0]


def ident(host="h", session="S1", turn=None, branch=""):
    """测试里的「可信分发点」：不给轮次时，身份在这一刻绑定到这个会话**此刻**的当前轮（身份在调用发起时就冻结，之后轮次再换，这份身份就是旧的）。
    没有登记过的会话绑一个不存在的轮次号。给了轮次就照给的用。生产里这个绑定只来自宿主（插件轮次、逐轮连接能力），不来自 turns 表里的最新一轮。"""
    if turn is None:
        cur = _ACC[0].current_turn(host, session, branch)
        turn = cur["turn_id"] if cur else "unregistered-in-test"
    return ToolIdentity(host, session, branch, turn)


def ok_read(acc, i, entry="recall"):
    t = acc.start_read(i, entry)
    assert not isinstance(t, dict), t
    return t


def refused(acc, i, entry="recall"):
    r = acc.start_read(i, entry)
    assert isinstance(r, dict) and r["status"] == "refused", r
    return r


def test_keys_separate_workspace_host_session_branch_and_turn_unique_within(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S1", "1")
    acc.begin_turn("h", "S2", "1")                      # 各会话都从 1 开始：不是全库的轮次编号
    acc.begin_turn("g", "S1", "1")
    acc.begin_turn("h", "S1", "1", branch="b")
    assert acc.current_turn("h", "S1")["gen"] == 1
    acc.begin_turn("h", "S1", "2")
    assert acc.current_turn("h", "S1")["turn_id"] == "2" and acc.current_turn("h", "S1")["gen"] == 2
    assert acc.current_turn("h", "S2")["turn_id"] == "1" and acc.current_turn("g", "S1")["gen"] == 1
    assert acc.current_turn("h", "S1", "b")["turn_id"] == "1"
    mem2 = make_mem(tmp_path, ws="other")               # 同一个库、另一个工作区：互不可见
    assert Access(mem2).current_turn("h", "S1") is None


def test_same_turn_id_reused_in_other_session_is_not_a_duplicate(tmp_path):
    mem, acc = mk(tmp_path)
    r1 = acc.begin_turn("h", "S1", "t", text="#yuanwei-nomem")
    r2 = acc.begin_turn("h", "S2", "t", text="普通一句")
    assert r1["turn"]["refused"] and not r2["turn"]["refused"] and r2["created"]
    refused(acc, ident("h", "S1"))
    ok_read(acc, ident("h", "S2"))


def test_two_sessions_interleaved_a_denied_b_new_turn_a_omitting_turn_still_denied(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "A", "1", text="#yuanwei-nomem\n正文")
    refused(acc, ident("h", "A"))
    acc.begin_turn("h", "B", "1", text="普通")           # B 声明新一轮
    ok_read(acc, ident("h", "B"))
    r = refused(acc, ident("h", "A"))                   # A 省略轮次也还是被拒（按 A 自己的当前轮）
    assert r["reason"] == "control_nomem"
    refused(acc, ident("h", "A", turn="1"))             # 带着轮次也一样
    acc.begin_turn("h", "B", "2")
    refused(acc, ident("h", "A"))


def test_same_session_two_turns_late_call_with_trusted_turn_tag_is_stale(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1")
    acc.begin_turn("h", "S", "T2")
    r = refused(acc, ident("h", "S", turn="T1"))
    assert r["reason"] == "stale_turn" and r["receipt"] == W4
    ok_read(acc, ident("h", "S", turn="T2"))
    r2 = refused(acc, ToolIdentity("h", "S"))           # 没有可信的轮次标记：拒绝，不按最新一轮算（补充施工单一）
    assert r2["reason"] == "turn_unproven" and r2["receipt"] == W4


def test_late_result_from_t1_is_not_reinterpreted_as_t2(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1")
    tk = ok_read(acc, ident("h", "S"))
    acc.mark_search(tk)
    acc.begin_turn("h", "S", "T2")                      # 读取还没返回，下一轮就登记了
    out = acc.finish_read(tk, "旧结果", "mcp_return", host="claude-code")
    assert out["status"] == "refused" and out["reason"] == "stale_turn" and out["receipt"] == W4                 # 身份在发起时绑定的是 T1
    kinds = [r["kind"] for r in acc.log_rows("h", "S", "T1")]
    assert "blocked_at_return" in kinds and "returned" not in kinds
    assert not [r for r in acc.log_rows("h", "S", "T2") if r["kind"] in ("returned", "search_entered")]


def test_registration_not_finished_or_failed_is_refused_and_old_allow_not_reused(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1")
    ok_read(acc, ident("h", "S"))
    acc.open_turn("h", "S", "T2")                       # 钩子只登记了一半（超时、被杀）
    r = refused(acc, ident("h", "S"))
    assert r["reason"] == "registration_incomplete" and r["receipt"] == W4
    acc.complete_turn("h", "S", "T2")
    ok_read(acc, ident("h", "S"))


def test_registration_failure_in_the_middle_leaves_refusal(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1")
    acc.fault = lambda point: (_ for _ in ()).throw(RuntimeError("boom")) if point == "between_open_and_complete" else None
    with pytest.raises(RuntimeError):
        acc.begin_turn("h", "S", "T2")
    acc.fault = None
    assert refused(acc, ident("h", "S"))["reason"] == "registration_incomplete"


def test_unregistered_unbound_and_incomplete_identity_refused(tmp_path):
    mem, acc = mk(tmp_path)
    assert refused(acc, ident("h", "S"))["reason"] == "turn_unregistered"
    assert refused(acc, None)["reason"] == "identity_unbound"
    for bad in (ToolIdentity("", "S"), ToolIdentity("h", ""), ToolIdentity("h", None)):
        assert refused(acc, bad)["reason"] == "identity_incomplete"
    for r in (refused(acc, None), refused(acc, ident("h", "S"))):
        assert r["receipt"] == W4 and r["text"].startswith(W4)
    with pytest.raises(ValueError):
        acc.begin_turn("h", "S", "")                    # 轮次缺失：登记不成立，不是 None 也不是默认值
    with pytest.raises(ValueError):
        acc.begin_turn("h", "", "t")


def test_duplicate_event_does_not_clear_a_standing_refusal_or_reopen_old_turn(tmp_path):
    mem, acc = mk(tmp_path)
    r1 = acc.begin_turn("h", "S", "T1", text="#yuanwei-nomem")
    r1b = acc.begin_turn("h", "S", "T1", text="普通一句，重送")
    assert r1["created"] and not r1b["created"] and r1b["turn"]["refused"]
    refused(acc, ident("h", "S"))
    acc.begin_turn("h", "S", "T2", text="普通")
    ok_read(acc, ident("h", "S"))
    r1c = acc.begin_turn("h", "S", "T1", text="普通一句，迟到的重送")      # T1 的重送不能把 T1 变成当前轮，也不影响 T2
    assert not r1c["created"] and acc.current_turn("h", "S")["turn_id"] == "T2"
    ok_read(acc, ident("h", "S"))


def test_unknown_origin_never_starts_a_turn_never_clears_ban_and_its_control_text_is_ignored(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1", text="#yuanwei-nomem")
    r = acc.begin_turn("h", "S", "X1", text="定时任务提示", origin="unknown")
    assert r["registered"] is False and r["ignored"] == "origin_unknown"
    assert acc.current_turn("h", "S")["turn_id"] == "T1"
    refused(acc, ident("h", "S"))                       # 禁读没被清掉
    acc.begin_turn("h", "S", "T2", text="普通")
    acc.begin_turn("h", "S", "X2", text="#yuanwei-nomem", origin="unknown")
    ok_read(acc, ident("h", "S"))                       # 来源不明的口令文字不当真人的控制
    assert acc.current_turn("h", "S")["turn_id"] == "T2"
    assert any(r["kind"] == "unknown_origin_ignored" for r in acc.log_rows("h", "S", "T2"))
    with pytest.raises(ValueError):
        acc.begin_turn("h", "S", "X3", text="x", origin="human?")


def test_replayed_history_never_executes_old_command_or_opens_a_turn(tmp_path):
    mem, acc = mk(tmp_path)
    r = acc.begin_turn("h", "S", "T0", text="#yuanwei-nomem", replay=True)
    assert r["registered"] is False and r["ignored"] == "replay"
    assert acc.current_turn("h", "S") is None


def test_compaction_and_resume_inside_the_same_turn_inherit_the_refusal(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1", text="#yuanwei-nomem")
    c = acc.continue_turn("h", "S", "T1", "compact")
    assert c["continued"] and c["turn"]["refused"]
    refused(acc, ident("h", "S"))
    acc.begin_turn("h", "S", "T2")
    assert acc.continue_turn("h", "S", "T2", "resume")["turn"]["refused"] is False
    assert acc.continue_turn("h", "S", "T1", "compact")["continued"] is False        # 旧轮次的续跑不能把它变回当前轮
    assert acc.current_turn("h", "S")["turn_id"] == "T2"
    assert acc.continue_turn("h", "S", "T-unknown", "compact")["continued"] is False


def test_fork_and_new_session_are_built_fresh_and_borrow_no_allow_from_other_branch(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1", text="普通")
    r = refused(acc, ident("h", "S", branch="fork-1"))     # 分叉只登记在自己的键下；没登记就是拒绝，不借主干的允许
    assert r["reason"] == "turn_unregistered"
    refused(acc, ident("h", "S2"))
    acc.begin_turn("h", "S", "T1", branch="fork-1", text="#yuanwei-nomem")
    refused(acc, ident("h", "S", branch="fork-1"))
    ok_read(acc, ident("h", "S"))                          # 主干不受影响
    acc.begin_turn("h", "S-forked", "T1")                  # 新会话不继承父会话这一轮的拒绝
    ok_read(acc, ident("h", "S-forked"))


def test_session_setting_is_per_session_and_workspace_setting_applies_to_all(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "A", "1")
    acc.begin_turn("h", "B", "1")
    acc.set_session_permission("h", "A", allowed=False)
    r = refused(acc, ident("h", "A"))
    assert r["reason"] == "session_setting" and r["receipt"] == W1
    ok_read(acc, ident("h", "B"))
    mem.set_read_permission(False)                         # 工作区一级
    assert refused(acc, ident("h", "B"))["reason"] == "workspace_setting"
    mem.clear_read_permission()
    ok_read(acc, ident("h", "B"))
    assert acc.clear_session_permission("h", "A")["cleared"] == 1
    ok_read(acc, ident("h", "A"))


def test_allow_setting_cannot_lift_any_ban_and_legacy_plain_session_key_also_denies(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "A", "1")
    acc.set_session_permission("h", "A", allowed=False)
    acc.set_session_permission("h", "A", allowed=True)     # 同一个键：后写的覆盖先写的（它们是同一个设置）
    ok_read(acc, ident("h", "A"))
    mem.set_read_permission(False, "session", "A")         # 第一段的旧写法（只有会话号）：也拒，宁可多拒
    assert refused(acc, ident("h", "A"))["reason"] == "session_setting"
    mem.set_read_permission(False)
    acc.set_session_permission("h", "A", allowed=True)
    assert refused(acc, ident("h", "A"))["reason"] == "workspace_setting"


def test_stage1_current_turn_is_not_a_security_binding(tmp_path):
    mem, acc = mk(tmp_path)
    mem.begin_turn("some-turn", "S")                       # 第一段的最近一轮便利：绑定路径不认它
    assert refused(acc, ident("h", "S"))["reason"] == "turn_unregistered"
    acc.begin_turn("h", "S", "T")
    mem.mark_turn_refused("T", "S")                        # 第一段的轮次标记也不是绑定路径的来源
    ok_read(acc, ident("h", "S"))


def test_deny_mid_read_blocks_return_and_receipt_says_queried_but_not_delivered(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T")
    tk = ok_read(acc, ident("h", "S"))
    acc.mark_search(tk)
    acc.deny_turn("h", "S", "T")
    out = acc.finish_read(tk, "材料", "mcp_return", host="claude-code")
    assert out["status"] == "refused" and out["reason"] == "control_changed"
    assert out["receipt"] == W3
    r = refused(acc, ident("h", "S"))                      # 后面还没开始的读取也停
    assert r["receipt"] == W3


def test_receipts_four_wordings(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1", text="#yuanwei-nomem")
    assert refused(acc, ident("h", "S"))["receipt"] == W1               # 控制在任何查询之前生效
    acc.begin_turn("h", "S", "T2")
    tk = ok_read(acc, ident("h", "S"))
    acc.mark_search(tk)
    acc.deny_turn("h", "S", "T2")
    assert refused(acc, ident("h", "S"))["receipt"] == W3               # 已经搜过，没有交出去
    acc.begin_turn("h", "S", "T3")
    assert refused(acc, ident("h", "S", turn="T2"))["receipt"] == W4    # 迟到的旧轮次：身份对不上，权限不明
    assert refused(acc, None)["receipt"] == W4


def test_receipt_w2_only_when_delivery_is_confirmed_by_a_verified_rule(tmp_path):
    mem, acc = mk(tmp_path)
    import dataclasses
    from yuanwei import hosts as H
    base = H.profile("hermes")
    verified = dataclasses.replace(base, paths={"mcp_return": dataclasses.replace(base.paths["mcp_return"], verified=True)})
    acc.begin_turn("h", "S", "T")
    tk = ok_read(acc, ident("h", "S"))
    acc.mark_search(tk)
    out = acc.finish_read(tk, "小材料", "mcp_return", profile=verified)
    assert out["status"] == "ok" and out["delivery"] == "confirmed_by_rule"
    acc.deny_turn("h", "S", "T")
    assert refused(acc, ident("h", "S"))["receipt"] == W2
    acc.begin_turn("h", "S", "T2")
    tk = ok_read(acc, ident("h", "S"))
    acc.mark_search(tk)
    out = acc.finish_read(tk, "小材料", "mcp_return", host="hermes")      # 固定版本没验证：返回了不等于宿主收到了完整材料
    assert out["status"] == "ok" and out["delivery"] == "unknown"
    acc.deny_turn("h", "S", "T2")
    assert refused(acc, ident("h", "S"))["receipt"] == W3


def test_persistent_wording_is_pointed_at_the_standing_switch(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T", text="以后都不要用以前的聊天记录")           # 文字识别只是辅助，这里认出来了
    r = refused(acc, ident("h", "S"))
    assert r["reason"] == "clause_ban" and r["persistent"] and "read-permission set" in r["text"]


def test_text_assist_can_be_turned_off_but_command_always_works(tmp_path):
    mem = make_mem(tmp_path)
    acc = Access(mem, text_assist=False)
    acc.begin_turn("h", "S", "T", text="不要用以前的聊天记录")
    ok_read(acc, ident("h", "S"))
    acc.begin_turn("h", "S", "T2", text="#yuanwei-nomem\n不要")
    refused(acc, ident("h", "S"))


def test_malformed_command_pauses_the_turn_and_returns_the_hint(tmp_path):
    mem, acc = mk(tmp_path)
    r = acc.begin_turn("h", "S", "T", text="#yuanwei-nomem 请别查")
    assert r["turn"]["refused"] and r["control"]["kind"] == "malformed" and "#yuanwei-nomem" in r["receipt"]
    rr = refused(acc, ident("h", "S"))
    assert rr["reason"] == "control_malformed" and "#yuanwei-nomem" in rr["text"]


def test_control_metadata_is_recorded_without_storing_the_raw_text(tmp_path):
    mem, acc = mk(tmp_path)
    r = acc.begin_turn("h", "S", "T", text="#yuanwei-nomem\n秘密正文XYZ")
    t = acc.current_turn("h", "S")
    assert t["control_kind"] == "nomem" and t["control_span"] == "0-14"
    rows = mem.store.conn.execute("SELECT * FROM turns").fetchall()
    assert not any("秘密正文XYZ" in str(v) for row in rows for v in tuple(row))
    assert mem.store.count_messages(mem.ns) == 0                           # 绑定不写原话；要不要保存仍走 record


def test_connection_handle_claim_rules(tmp_path):
    mem, acc = mk(tmp_path)
    h = acc.register_connection("pi", "S1")
    assert len(h) >= 32
    ident1 = acc.claim_connection(h, "proc-A")
    assert (ident1.host, ident1.session_id, ident1.branch, ident1.turn_id) == ("pi", "S1", "", None)
    assert acc.claim_connection(h, "proc-A").session_id == "S1"           # 同一个进程再认领一次：幂等
    with pytest.raises(A.ConnectionRefused) as e:
        acc.claim_connection(h, "proc-B")                                   # 第二个进程拿同一个句柄：拒绝
    assert e.value.code == "connection_claimed"
    with pytest.raises(A.ConnectionRefused) as e:
        acc.claim_connection("0" * 32, "proc-A")
    assert e.value.code == "connection_unknown"
    acc.close_connection(h)
    with pytest.raises(A.ConnectionRefused) as e:
        acc.claim_connection(h, "proc-A")
    assert e.value.code == "connection_closed"
    h2 = acc.register_connection("pi", "S2")
    mem2 = make_mem(tmp_path, ws="other")
    with pytest.raises(A.ConnectionRefused):
        Access(mem2).claim_connection(h2, "proc-A")                          # 别的工作区的句柄不能用


def test_connection_handles_are_random_and_not_derivable_from_session(tmp_path):
    mem, acc = mk(tmp_path)
    hs = {acc.register_connection("pi", "S1") for _ in range(20)}
    assert len(hs) == 20 and all("S1" not in h for h in hs)


def test_read_log_records_the_four_things_and_is_not_part_of_the_corpus(tmp_path):
    mem, acc = mk(tmp_path)
    mem.record("h", "S", "m1", "user", "今天去了海边", time="2027-01-01")
    acc.begin_turn("h", "S", "T")
    tk = ok_read(acc, ident("h", "S"), entry="recall")
    acc.mark_search(tk)
    acc.mark_vector(tk, 2)
    acc.assembled(tk, spans=[{"msg_id": "m-x", "start": 0, "end": 5}], text="材料文字")
    out = acc.finish_read(tk, "材料文字", "mcp_return", host="hermes")
    assert out["status"] == "ok"
    rows = acc.log_rows("h", "S", "T")
    kinds = [r["kind"] for r in rows]
    assert kinds[:4] == ["start", "search_entered", "vector_called", "assembled"] and kinds[-1] == "returned"
    start = rows[0]
    assert start["entry"] == "recall" and start["detail"]["identity"]["session_id"] == "S"
    asm = [r for r in rows if r["kind"] == "assembled"][0]["detail"]
    assert asm["spans"][0]["msg_id"] == "m-x" and len(asm["fingerprint"]) == 16 and "材料文字" not in str(asm)
    assert [r for r in rows if r["kind"] == "vector_called"][0]["detail"]["calls"] == 2
    ret = rows[-1]["detail"]
    assert ret["delivery"] == "unknown" and ret["fingerprint"] == asm["fingerprint"] and ret["limits"]
    # 日志不进记忆语料：不在导出里，也不影响可见原话和锚点
    import json, os
    p = str(tmp_path / "x.jsonl")
    mem.export(p)
    assert "read_log" not in open(p, encoding="utf-8").read() and "start" not in {json.loads(l).get("type") for l in open(p, encoding="utf-8")}
    assert mem.store.count_messages(mem.ns) == 1


def test_oversize_final_text_is_not_returned_and_not_marked_delivered(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T")
    tk = ok_read(acc, ident("h", "S"))
    out = acc.finish_read(tk, "字" * 20001, "mcp_return", host="pi")
    assert out["status"] == "error" and out["reason"] == "return_too_large"
    assert [r for r in acc.log_rows("h", "S", "T") if r["kind"] == "returned"] == []
    assert [r for r in acc.log_rows("h", "S", "T") if r["kind"] == "oversize_blocked"]


def test_permission_read_error_fails_closed_as_unknown(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T")
    mem.store.conn.execute("DROP TABLE read_settings")
    r = refused(acc, ident("h", "S"))
    assert r["reason"] == "permission_unknown" and r["receipt"] == W4


def test_log_failure_at_start_fails_closed(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T")
    mem.store.conn.execute("ALTER TABLE read_log RENAME TO read_log_x")
    r = refused(acc, ident("h", "S"))
    assert r["reason"] == "permission_unknown"


def test_record_is_not_closed_by_a_read_ban_and_unknown_origin_writes_nothing(tmp_path):
    """记录、控制、读取三个准入分别判：禁读不关写入；来源不明的事件什么都不写（不猜角色、不转进别的索引）。"""
    mem, acc = mk(tmp_path)
    acc.begin_turn("h", "S", "T1", text="#yuanwei-nomem\n一句要保存的话")
    mem.set_read_permission(False)
    r = mem.record("h", "S", "m1", "user", "#yuanwei-nomem\n一句要保存的话", time="2027-01-01")
    assert r["raw_saved"] is True and mem.store.count_messages(mem.ns) == 1
    assert mem.store.list_messages(mem.ns)[0]["content"].startswith("#yuanwei-nomem")          # 原始提交照原样存
    before = mem.store.count_messages(mem.ns)
    anchors = lambda: mem.store.conn.execute("SELECT COUNT(*) c FROM anchors WHERE ns=?", (mem.ns,)).fetchone()["c"]
    a0 = anchors()
    acc.begin_turn("h", "S", "X1", text="后台回报的一段话", origin="unknown")
    acc.begin_turn("h", "S", "R1", text="回放的旧话", replay=True)
    assert mem.store.count_messages(mem.ns) == before
    assert anchors() == a0
