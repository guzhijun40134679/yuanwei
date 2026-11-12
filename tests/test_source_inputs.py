"""交回点乙·第 1 步（补充施工单·乙 §1–§4 的核心部分）：回执小改、逐条输入的来源核对、Claude Code 记录解析、Codex 来源判定、调用编号绑定、工作区筛选。
假事件和假记录只用真机核对记录里看到过的字段名（Claude Code：type、promptId、message.content、turnOrigin、origin.kind、isMeta；hook 的 prompt_id、tool_use_id；
Codex：turn_attribution 的 turn_id、turn_trigger、initiating_agent_path，_meta 的 callId、x-codex-turn-metadata{turn_id,session_id,thread_id,turn_trigger,thread_source}）。
全部自造数据，没有网络。"""
import dataclasses
import json
import os

import pytest

from helpers2 import TEXTS, Counter, organized
from yuanwei import hosts as H
from yuanwei import sources as SR
from yuanwei.access import TAIL_CLOSED, TAIL_UNKNOWN, Access, ToolIdentity
from yuanwei.idsource import CallBoundIdentity
from yuanwei.modes import ModeConfig, ModeRunner
from yuanwei.tools import ToolSurface
from yuanwei.workspace import WorkspaceFilter

W1 = "本轮未新增读取长期记忆"
W2 = "已停止后续读取；此前已有材料交入本轮"
W3 = "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认"
W4 = "无法确认权限，已暂停新增读取"
CC, CX = "claude-code", "codex"


def mk(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mem = organized(tmp_path)
    return mem, Access(mem)


def ident(host, session, turn):
    return ToolIdentity(host, session, "", turn)


def reg_human(acc, host, session, turn, text="普通的一句"):
    r = acc.register_input(host, session, turn, text)
    acc.resolve_inputs(host, session, turn, [{"input_key": r["input_key"], "verdict": "human", "label": "human"}])
    return r


# ================================================================== §1 回执
def test_connection_closed_uses_its_own_tail_not_the_unknown_one(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn("pi", "S", "T1")
    h = acc.register_connection("pi", "S", turn_id="T1")
    ids = __import__("yuanwei.idsource", fromlist=["x"]).ConnectionIdentity(acc, h, "inst-1")
    acc.close_connection(h)
    r = acc.start_read(ids.current(), "recall")
    assert r["reason"] == "connection_closed" and r["text"].endswith(TAIL_CLOSED) and TAIL_UNKNOWN not in r["text"]
    assert "连接已关闭，本次结果未交付；此前已经读取或交付的内容不会因此撤回。这只管本插件的读取入口。" in r["text"]


def verified_profile(host="pi"):
    base = H.profile(host)
    p = base.paths["hook_context"]
    return dataclasses.replace(base, paths=dict(base.paths, hook_context=dataclasses.replace(p, verified=True)))


def delivered_note(tmp_path, turn):
    mem = organized(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, verified_profile(), ModeConfig(mode="note"))
    out = r.on_user_prompt("S", turn, "吃了什么烤鱼")
    assert out["kind"] == "note" and out["delivery"] == "confirmed_by_rule"
    return mem, acc, r, out


def test_injection_confirmed_then_denied_says_materials_were_handed_in(tmp_path):
    mem, acc, r, out = delivered_note(tmp_path, "T1")
    r.confirm_injection(out, True)
    acc.deny_turn("pi", "S", "T1")
    assert acc.check_permission(ident("pi", "S", "T1"))["receipt"] == W2


def test_injection_failed_never_claims_delivered(tmp_path):
    mem, acc, r, out = delivered_note(tmp_path, "T1")
    assert r.confirm_injection(out, False) is True                       # 生成了、尝试注入了，但宿主没收下
    acc.deny_turn("pi", "S", "T1")
    assert acc.check_permission(ident("pi", "S", "T1"))["receipt"] == W3  # 不能说「此前已有材料交入本轮」
    assert "injection_failed" in [x["kind"] for x in acc.log_rows("pi", "S", "T1")]


def test_receipt_injection_failed_lets_the_next_hook_send_it_again_but_confirmed_does_not(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="tools_only"))
    mem.set_read_permission(False)
    a = r.on_user_prompt("S", "T1", "普通")
    assert a["kind"] == "receipt"
    r.confirm_injection(a, False)
    b = r.compact_continue("S", "T1")
    assert b["kind"] == "receipt" and b["inject"].startswith(W1)         # 没收下：同一轮里还能再送
    r.confirm_injection(b, True)
    assert r.compact_continue("S", "T1")["kind"] == "receipt_already_sent"


def test_model_tool_calls_refused_each_time_with_their_own_reason_even_after_the_turn_receipt(tmp_path):
    mem = organized(tmp_path)
    acc = Access(mem)
    ids = __import__("yuanwei.idsource", fromlist=["x"]).InProcessIdentity()
    surf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    r = ModeRunner(mem, acc, H.profile("hermes"), ModeConfig(mode="tools_only"))
    mem.set_read_permission(False)
    ids.set("hermes", "S", "", "T1")
    assert r.on_user_prompt("S", "T1", "x")["kind"] == "receipt"
    assert r.compact_continue("S", "T1")["kind"] == "receipt_already_sent"
    for _ in range(2):                                                    # 模型主动调用：每次都要得到这一次的原因，不被「一轮一句」吞掉
        res = surf.call("recall", {"query": "烤鱼"})
        assert res.status == "refused" and res.reason == "workspace_setting" and W1 in res.text


# ================================================================== §2.1 每条输入先「来源待核」
def test_new_input_is_pending_and_reads_are_paused_until_verified(tmp_path):
    mem, acc = mk(tmp_path)
    cnt = Counter(mem)
    try:
        r = acc.register_input(CC, "S", "P1", "吃了什么烤鱼")
        assert r["registered"] and r["created"]
        got = acc.start_read(ident(CC, "S", "P1"), "recall")
        assert got["reason"] == "source_pending" and got["receipt"] == W4 and cnt.calls == []
        acc.resolve_inputs(CC, "S", "P1", [{"input_key": r["input_key"], "verdict": "human", "label": "human"}])
        assert not isinstance(acc.start_read(ident(CC, "S", "P1"), "recall"), dict)
    finally:
        cnt.close()


def test_human_input_with_the_fixed_command_is_applied_only_after_verification(tmp_path):
    mem, acc = mk(tmp_path)
    r = acc.register_input(CC, "S", "P1", "#yuanwei-nomem\n烤鱼")
    assert acc.current_turn(CC, "S")["refused"] is False                  # 核对前什么控制都没生效（这时本来就因为待核不许读）
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": r["input_key"], "verdict": "human"}])
    t = acc.current_turn(CC, "S")
    assert t["refused"] is True and t["refuse_reason"] == "control_nomem" and t["control_kind"] == "nomem"
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["receipt"] == W1


@pytest.mark.parametrize("verdict", ["other", "fail"])
def test_non_human_or_unverifiable_input_gets_no_permission_and_its_text_is_not_a_control(tmp_path, verdict):
    mem, acc = mk(tmp_path)
    r = acc.register_input(CC, "S", "P1", "#yuanwei-nomem\n别的会话发来的")
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": r["input_key"], "verdict": verdict, "label": "peer"}])
    t = acc.current_turn(CC, "S")
    assert t["refused"] is False and t["control_kind"] in (None, "none")   # 文字不当真人的控制，也不自动改任何设置
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"


def test_peer_input_never_clears_an_existing_ban_and_a_later_normal_text_cannot_either(tmp_path):
    mem, acc = mk(tmp_path)
    r1 = acc.register_input(CC, "S", "P1", "#yuanwei-nomem")
    r2 = acc.register_input(CC, "S", "P1", "后来排队进来的普通一句")
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": r1["input_key"], "verdict": "human"}, {"input_key": r2["input_key"], "verdict": "human"}])
    assert acc.current_turn(CC, "S")["refused"] is True                    # 后来的普通文字清不掉禁读
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "control_nomem"


def test_a_second_queued_input_in_the_same_turn_pauses_reads_again(tmp_path):
    mem, acc = mk(tmp_path)
    reg_human(acc, CC, "S", "P1", "第一句")
    assert not isinstance(acc.start_read(ident(CC, "S", "P1"), "recall"), dict)
    r2 = acc.register_input(CC, "S", "P1", "同一个 prompt_id 下排队合并进来的第二句")
    assert r2["created"] and r2["seq"] == 2
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_pending"
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": r2["input_key"], "verdict": "human"}])
    assert not isinstance(acc.start_read(ident(CC, "S", "P1"), "recall"), dict)


def test_human_plus_peer_in_one_turn_is_not_readable(tmp_path):
    mem, acc = mk(tmp_path)
    a = acc.register_input(CC, "S", "P1", "用户的话")
    b = acc.register_input(CC, "S", "P1", "别的会话发来的话")
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": a["input_key"], "verdict": "human"}, {"input_key": b["input_key"], "verdict": "other"}])
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"


def test_identical_inputs_are_two_inputs_and_an_event_id_makes_a_resend_idempotent(tmp_path):
    mem, acc = mk(tmp_path)
    a = acc.register_input(CC, "S", "P1", "一字不差")
    b = acc.register_input(CC, "S", "P1", "一字不差")
    assert a["input_key"] != b["input_key"] and b["created"] and a["fp"] == b["fp"]
    c = acc.register_input(CC, "S", "P2", "x", event_id="evt-1")
    d = acc.register_input(CC, "S", "P2", "x", event_id="evt-1")
    assert c["created"] and d["created"] is False and len(acc.inputs_of(CC, "S", "P2")) == 1


def test_a_late_source_result_cannot_reopen_a_finished_or_replaced_turn(tmp_path):
    mem, acc = mk(tmp_path)
    r = acc.register_input(CC, "S", "P1", "旧的一轮")
    acc.register_input(CC, "S", "P2", "新的一轮")
    out = acc.resolve_inputs(CC, "S", "P1", [{"input_key": r["input_key"], "verdict": "human"}])
    assert out["applied"] == 0 and out["ignored"] == "stale_turn"
    assert acc.inputs_of(CC, "S", "P1")[0]["state"] == "pending"
    assert "source_result_ignored" in [x["kind"] for x in acc.log_rows(CC, "S", "P1")]
    late = acc.register_input(CC, "S", "P1", "旧一轮里又来一句")
    assert late["registered"] is False and late["ignored"] == "stale_turn"
    acc.close_turn(CC, "S", "P2")
    assert acc.register_input(CC, "S", "P2", "关了的轮次里又来一句")["ignored"] == "stale_turn"


def test_resolved_inputs_are_never_flipped_and_deadline_fails_the_unresolved_ones(tmp_path):
    mem, acc = mk(tmp_path)
    a = acc.register_input(CC, "S", "P1", "甲")
    b = acc.register_input(CC, "S", "P1", "乙")
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": a["input_key"], "verdict": "human"}])
    acc.resolve_inputs(CC, "S", "P1", [{"input_key": a["input_key"], "verdict": "other"}])      # 不翻案
    assert acc.inputs_of(CC, "S", "P1")[0]["state"] == "human"
    assert acc.expire_inputs(CC, "S", "P1", max_age_s=0.0) == 1
    assert acc.inputs_of(CC, "S", "P1")[1]["state"] == "failed"
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"
    assert acc.expire_inputs(CC, "S", "P1", max_age_s=0.0) == 0


def test_input_rows_hold_metadata_only_no_raw_text(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "这是一句只能存在原话层的秘密文字")
    rows = [dict(r) for r in mem.store.conn.execute("SELECT * FROM inputs")]
    assert rows and all("秘密文字" not in json.dumps(r, ensure_ascii=False) for r in rows)


def test_inputs_turns_and_two_step_turns_do_not_mix(tmp_path):
    mem, acc = mk(tmp_path)
    acc.begin_turn(CC, "S", "P1", text="普通")
    assert acc.register_input(CC, "S", "P1", "x")["ignored"] == "turn_mode_conflict"


# ================================================================== §2.3 Claude Code 记录解析（字段照真机记录）
def entry(prompt_id, text, to="human", kind="human", **extra):
    e = {"type": "user", "promptId": prompt_id, "uuid": "u-" + text[:3], "message": {"role": "user", "content": text}, "turnOrigin": to, "origin": {"kind": kind}, "promptSource": "sdk", "isMeta": False}
    e.update(extra)
    return e


def lines(*es):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in es)


V = "2.1.317"


def test_cc_human_entry_is_matched_to_the_registered_input_and_peer_sdk_task_notification_are_not_human(tmp_path):
    for to, kind in (("peer", "peer"), ("sdk", "sdk"), ("task_notification", "task_notification")):
        mem, acc = mk(tmp_path / to)
        acc.register_input(CC, "S", "P1", "来源不是用户本人的话")
        out = SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "来源不是用户本人的话", to, kind)), V)
        assert out["state"] == "resolved" and acc.inputs_of(CC, "S", "P1")[0]["state"] == "rejected"
        assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"
    mem, acc = mk(tmp_path / "h")
    acc.register_input(CC, "S", "P1", "用户亲手打的话")
    SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "用户亲手打的话")), V)
    assert not isinstance(acc.start_read(ident(CC, "S", "P1"), "recall"), dict)


def test_cc_tool_results_and_other_prompts_are_skipped(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "问题")
    raw = lines(entry("P0", "上一轮的话", "peer", "peer"), entry("P1", "问题"),
                {"type": "user", "promptId": "P1", "message": {"content": [{"type": "tool_result", "content": "x"}]}, "turnOrigin": "sdk"},
                {"type": "assistant", "promptId": "P1", "message": {"content": "回复"}})
    out = SR.verify_cc(acc, CC, "S", "P1", raw, V)
    assert out["unmatched"] == 0 and acc.inputs_of(CC, "S", "P1")[0]["state"] == "human"


def test_cc_record_has_an_entry_we_never_received_so_the_turn_is_not_covered(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "用户的话")
    out = SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "用户的话"), entry("P1", "没有触发钩子的别的来源", "peer", "peer")), V)
    assert out["unmatched"] == 1
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"


def test_cc_empty_record_matches_nothing_and_is_not_all_human(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "用户的话")
    out = SR.verify_cc(acc, CC, "S", "P1", "", V)
    assert out["state"] == "resolved" and acc.inputs_of(CC, "S", "P1")[0]["state"] == "pending"      # 一条都对不上：还是待核
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_pending"
    acc.expire_inputs(CC, "S", "P1", max_age_s=0.0)
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"


def test_cc_first_input_landed_second_not_yet(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "第一句")
    acc.register_input(CC, "S", "P1", "第二句")
    SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "第一句")), V)
    st = [i["state"] for i in acc.inputs_of(CC, "S", "P1")]
    assert st == ["human", "pending"]
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_pending"
    SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "第一句"), entry("P1", "第二句")), V)
    assert [i["state"] for i in acc.inputs_of(CC, "S", "P1")] == ["human", "human"]
    assert not isinstance(acc.start_read(ident(CC, "S", "P1"), "recall"), dict)


def test_cc_two_identical_texts_with_different_sources_are_matched_in_order(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "一字不差")
    acc.register_input(CC, "S", "P1", "一字不差")
    SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "一字不差"), entry("P1", "一字不差", "peer", "peer")), V)
    assert [i["state"] for i in acc.inputs_of(CC, "S", "P1")] == ["human", "rejected"]


def test_cc_fingerprint_that_never_matches_is_a_failure_after_the_deadline_not_a_pass(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "钩子收到的原文")
    out = SR.verify_cc(acc, CC, "S", "P1", lines(entry("P1", "记录里被包装过的另一个样子")), V)
    assert acc.inputs_of(CC, "S", "P1")[0]["state"] == "pending" and out["state"] == "resolved"        # 对不上：仍是待核，不是放行
    acc.expire_inputs(CC, "S", "P1", max_age_s=0.0)
    assert acc.start_read(ident(CC, "S", "P1"), "recall")["reason"] == "source_unverified"


def test_cc_partial_last_line_is_pending_bad_line_and_unknown_version_and_missing_fields_fail(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CC, "S", "P1", "用户的话")
    good = lines(entry("P1", "用户的话"))
    assert SR.verify_cc(acc, CC, "S", "P1", good.rstrip("\n")[:-5], V)["state"] == "pending"           # 最后一行只写了一半
    assert acc.inputs_of(CC, "S", "P1")[0]["state"] == "pending"
    assert SR.verify_cc(acc, CC, "S", "P1", "{坏的一行\n" + good, V)["state"] == "failed"              # 中间的坏行不能跳过
    mem2, acc2 = mk(tmp_path / "b")
    acc2.register_input(CC, "S", "P1", "用户的话")
    assert SR.verify_cc(acc2, CC, "S", "P1", good, "2.1.300")["state"] == "failed"                     # 命令行 2.1.300 没有可持久核对的记录
    mem3, acc3 = mk(tmp_path / "c")
    acc3.register_input(CC, "S", "P1", "用户的话")
    old = lines({"type": "user", "promptId": "P1", "message": {"content": "用户的话"}, "turnOrigin": "human"})   # 更早的版本：没有 origin
    SR.verify_cc(acc3, CC, "S", "P1", old, V)
    assert acc3.inputs_of(CC, "S", "P1")[0]["state"] == "failed" and acc3.inputs_of(CC, "S", "P1")[0]["label"] == "fields_missing"
    mem4, acc4 = mk(tmp_path / "d")
    acc4.register_input(CC, "S", "P1", "用户的话")
    SR.verify_cc(acc4, CC, "S", "P1", lines(entry("P1", "用户的话", "human", "peer")), V)
    assert acc4.inputs_of(CC, "S", "P1")[0]["label"] == "labels_conflict"


def test_cc_transcript_path_must_be_the_session_file(tmp_path):
    assert SR.cc_transcript_path_ok("/x/y/sess-1.jsonl", "sess-1")
    assert not SR.cc_transcript_path_ok("/x/y/other.jsonl", "sess-1")
    assert not SR.cc_transcript_path_ok("sess-1.jsonl", "sess-1")
    assert not SR.cc_transcript_path_ok(None, "sess-1")


# ================================================================== §2.4 Codex 来源判定
HOOK = {"session_id": "s", "turn_id": "t", "prompt": "x", "cwd": "/w"}
ATTR = {"turn_id": "t", "turn_trigger": "composer", "initiating_agent_path": None, "parent_turn_id": None, "root_turn_id": "t"}
META = {"turn_id": "t", "session_id": "s", "thread_id": "s", "turn_trigger": "composer", "thread_source": "user"}
VX = "0.167.0-alpha"


def verdict(**kw):
    a = dict(hook_event=HOOK, record_attr=ATTR, meta_turn=META, engine_version=VX)
    a.update(kw)
    return SR.codex_source_verdict(**a)


def test_codex_composer_from_a_user_thread_is_human_only_when_every_condition_holds():
    assert verdict()["verdict"] == "human"
    assert verdict(engine_version="0.162.1")["verdict"] == "fail"                                        # 命令行 exec 版本没核过来源
    assert verdict(record_attr=None)["verdict"] == "human"                                               # MCP 没带的证明项从记录取，记录没有就只用 _meta；两边至少有一边
    assert verdict(record_attr=None, meta_turn=None)["verdict"] == "fail"


def test_codex_subagent_evidence_in_the_hook_is_not_overridden_by_an_empty_record_field():
    assert verdict(hook_event=dict(HOOK, agent_id="a1"))["verdict"] == "other"
    assert verdict(hook_event=dict(HOOK, agent_type="x"))["verdict"] == "other"
    assert verdict(record_attr=dict(ATTR, initiating_agent_path=["root", "sub"]))["verdict"] == "other"
    assert verdict(meta_turn=dict(META, thread_source="subagent"))["verdict"] == "other"
    assert verdict(meta_turn=dict(META, thread_source="guardian_review"))["verdict"] == "other"


def test_codex_missing_field_is_not_the_same_as_explicitly_empty():
    gone = {k: v for k, v in ATTR.items() if k != "initiating_agent_path"}
    assert verdict(record_attr=gone)["verdict"] == "fail" and verdict(record_attr=gone)["reason"] == "initiating_agent_path_missing"
    assert verdict(record_attr=dict(ATTR, initiating_agent_path=None))["verdict"] == "human"
    assert verdict(record_attr=dict(ATTR, initiating_agent_path=""))["verdict"] == "human"
    assert verdict(meta_turn={k: v for k, v in META.items() if k != "thread_source"})["reason"] == "thread_source_missing"
    assert verdict(session_thread_source="user", meta_turn={k: v for k, v in META.items() if k != "thread_source"})["verdict"] == "human"


def test_codex_record_and_meta_must_agree():
    assert verdict(record_attr=dict(ATTR, turn_id="other"))["reason"] == "record_meta_mismatch_turn_id"
    assert verdict(record_attr=dict(ATTR, turn_trigger="exec"))["reason"] == "record_meta_mismatch_turn_trigger"


@pytest.mark.parametrize("trig,want,why", [
    ("composer", "human", None), ("remote_ios", "fail", "trigger_not_enabled_remote_ios"), ("edit_user_message", "fail", "trigger_not_enabled_edit_user_message"),
    ("exec", "fail", "trigger_not_opened_exec"), ("queue", "fail", None), ("capacity_retry_manual", "fail", None), ("resume_interrupted_task", "fail", None), ("send_user_message_async_question", "fail", None),
    ("guardian_review", "other", None), ("app_tool_send_message", "other", None), ("app_tool_create_thread", "other", None), ("automation_heartbeat_scheduled", "other", None),
    ("a_value_never_seen", "fail", "turn_trigger_unknown")])
def test_codex_trigger_table(trig, want, why):
    v = verdict(record_attr=dict(ATTR, turn_trigger=trig), meta_turn=dict(META, turn_trigger=trig))
    assert v["verdict"] == want and (why is None or v["reason"] == why)


def test_codex_candidate_triggers_are_off_by_default_and_can_be_enabled_one_by_one():
    kw = dict(record_attr=dict(ATTR, turn_trigger="remote_ios"), meta_turn=dict(META, turn_trigger="remote_ios"))
    assert verdict(**kw)["verdict"] == "fail"
    assert verdict(enabled_candidates=frozenset(["remote_ios"]), **kw)["verdict"] == "human"
    assert verdict(enabled_candidates=frozenset(["remote_ios"]), record_attr=dict(ATTR, turn_trigger="exec"), meta_turn=dict(META, turn_trigger="exec"))["verdict"] == "fail"


def test_codex_without_per_input_evidence_only_a_single_input_turn_is_supported(tmp_path):
    mem, acc = mk(tmp_path)
    acc.register_input(CX, "s", "t", "第一句")
    out = SR.verify_single_input(acc, CX, "s", "t", verdict())
    assert out["state"] == "resolved" and not isinstance(acc.start_read(ident(CX, "s", "t"), "recall"), dict)
    acc.register_input(CX, "s", "t", "同一轮里追加的第二句")
    assert acc.start_read(ident(CX, "s", "t"), "recall")["reason"] == "source_pending"
    out = SR.verify_single_input(acc, CX, "s", "t", verdict())
    assert out["state"] == "failed" and acc.start_read(ident(CX, "s", "t"), "recall")["reason"] == "source_unverified"       # 一个判定不能套到两条输入上
    mem2, acc2 = mk(tmp_path / "x")
    acc2.register_input(CX, "s", "t", "只有一句")
    SR.verify_single_input(acc2, CX, "s", "t", verdict(meta_turn=dict(META, thread_source="subagent")))
    assert acc2.start_read(ident(CX, "s", "t"), "recall")["reason"] == "source_unverified"


# ================================================================== §3 调用编号绑定
def call_surface(tmp_path, host):
    mem, acc = mk(tmp_path)
    surf = ToolSurface(mem, acc, H.with_call_binding(H.profile(host)), CallBoundIdentity(acc, host))
    return mem, acc, surf


def test_default_profiles_still_do_not_open_the_read_tools_and_call_binding_is_explicit():
    assert H.profile(CC).tools_opened is False and H.profile(CX).tools_opened is False
    assert H.with_call_binding(H.profile(CC)).tools_opened is True
    with pytest.raises(ValueError):
        H.with_call_binding(H.profile("pi"))


def test_call_registry_idempotent_conflict_and_unregistered_turn(tmp_path):
    mem, acc = mk(tmp_path)
    assert acc.register_call(CC, "toolu_1", "S", "P1")["reason"] == "turn_unregistered"
    reg_human(acc, CC, "S", "P1")
    assert acc.register_call(CC, "toolu_1", "S", "P1")["created"] is True
    assert acc.register_call(CC, "toolu_1", "S", "P1")["created"] is False
    assert acc.register_call(CC, "toolu_1", "S2", "P1")["reason"] == "turn_unregistered"
    reg_human(acc, CC, "S2", "P1")
    assert acc.register_call(CC, "toolu_1", "S2", "P1")["reason"] == "call_conflict"                 # 同一个编号绑到别处：作废
    assert acc.identity_for_call(CC, "toolu_1") == (None, "call_conflict")
    assert acc.identity_for_call(CC, "nope") == (None, "call_unregistered")
    assert acc.identity_for_call(CC, "")[1] == "call_id_invalid" and acc.identity_for_call(CC, None)[1] == "call_id_invalid"


def test_claude_code_call_is_bound_by_the_meta_tool_use_id_not_by_arguments(tmp_path):
    mem, acc, surf = call_surface(tmp_path, CC)
    reg_human(acc, CC, "S", "P1")
    cnt = Counter(mem)
    try:
        assert surf.call("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_x"}).reason == "call_unregistered"
        assert acc.register_call(CC, "toolu_x", "S", "P1")["registered"]
        ok = surf.call("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_x"})
        assert ok.status == "ok" and TEXTS[1] in ok.text
        cnt.reset()
        for meta in ({}, {"claudecode/toolUseId": ""}, {"claudecode/toolUseId": 5}, {"toolUseId": "toolu_x"}, None):
            r = surf.call("recall", {"query": "烤鱼"}, meta=meta)
            assert r.status == "refused" and r.reason in ("call_id_invalid", "call_unregistered")
        spoof = surf.call("recall", {"query": "烤鱼", "claudecode/toolUseId": "toolu_x", "tool_use_id": "toolu_x", "session_id": "S", "turn_id": "P1"}, meta={})   # 参数不是 _meta
        assert spoof.status == "refused" and cnt.calls == []
    finally:
        cnt.close()


def test_a_call_registered_for_an_old_turn_is_refused_when_that_turn_is_no_longer_current(tmp_path):
    mem, acc, surf = call_surface(tmp_path, CC)
    reg_human(acc, CC, "S", "P1")
    acc.register_call(CC, "toolu_old", "S", "P1")
    reg_human(acc, CC, "S", "P2")
    r = surf.call("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_old"})
    assert r.status == "refused" and r.reason == "stale_turn"


def test_a_call_in_a_turn_whose_source_is_pending_or_not_human_is_refused_before_any_search(tmp_path):
    mem, acc, surf = call_surface(tmp_path, CC)
    cnt = Counter(mem)
    try:
        acc.register_input(CC, "S", "P1", "还没核来源")
        acc.register_call(CC, "toolu_a", "S", "P1")
        assert surf.call("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_a"}).reason == "source_pending"
        acc.resolve_inputs(CC, "S", "P1", [{"input_key": acc.inputs_of(CC, "S", "P1")[0]["input_key"], "verdict": "other", "label": "peer"}])
        assert surf.call("recall", {"query": "烤鱼"}, meta={"claudecode/toolUseId": "toolu_a"}).reason == "source_unverified"
        assert cnt.calls == []
    finally:
        cnt.close()


def codex_meta(call_id, turn, session, **kw):
    tm = dict(META, turn_id=turn, session_id=session, thread_id=session)
    m = {"callId": call_id, "threadId": session, "sessionId": session, "x-codex-turn-metadata": tm, "itemId": "i", "windowId": "w"}
    m.update(kw)
    return m


def test_codex_call_needs_the_hook_registration_and_the_meta_turn_info_to_agree(tmp_path):
    mem, acc, surf = call_surface(tmp_path, CX)
    reg_human(acc, CX, "s1", "t1")
    acc.register_call(CX, "exec-1", "s1", "t1")
    assert surf.call("recall", {"query": "烤鱼"}, meta=codex_meta("exec-1", "t1", "s1")).status == "ok"
    for bad, why in ((codex_meta("exec-1", "t9", "s1"), "call_mismatch"), (codex_meta("exec-1", "t1", "s9"), "call_mismatch"),
                     (codex_meta("exec-1", "t1", "s1", threadId="s9"), "call_mismatch"), (codex_meta("exec-1", "t1", "s1", sessionId="s9"), "call_mismatch"),
                     (codex_meta("exec-2", "t1", "s1"), "call_unregistered"), ({"callId": "exec-1"}, "call_meta_incomplete"),
                     ({"callId": "exec-1", "x-codex-turn-metadata": "oops"}, "call_meta_incomplete"), ({"x-codex-turn-metadata": META}, "call_id_invalid")):
        r = surf.call("recall", {"query": "烤鱼"}, meta=bad)
        assert r.status == "refused" and r.reason == why, (bad, r.reason)


def test_binding_a_hook_tool_use_id_equal_to_the_codex_call_id_is_the_only_bridge(tmp_path):
    """真机核过：Codex 调用前钩子的 tool_use_id 等于 MCP 的 _meta.callId。只找到「同一个 turn_id」的记录不放行：没有这个编号的登记就是没登记。"""
    mem, acc, surf = call_surface(tmp_path, CX)
    reg_human(acc, CX, "s1", "t1")
    r = surf.call("recall", {"query": "烤鱼"}, meta=codex_meta("exec-never-registered", "t1", "s1"))
    assert r.status == "refused" and r.reason == "call_unregistered"


# ================================================================== §4 工作区筛选
def test_workspace_filter_inside_sibling_prefix_symlink_missing_and_worktree(tmp_path):
    root = tmp_path / "a" / "b"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "a" / "bc").mkdir()
    (tmp_path / "a" / "other").mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(str(root), str(tmp_path / "link-in"))
    os.symlink(str(tmp_path / "elsewhere"), str(root / "sub" / "link-out"))
    wf = WorkspaceFilter(str(root))
    assert wf.inside(str(root)) == (True, "inside") and wf.inside(str(root / "sub"))[0] is True
    assert wf.inside(str(tmp_path / "a" / "bc")) == (False, "outside")                                   # /a/b 和 /a/bc 不是一回事
    assert wf.inside(str(tmp_path / "a" / "other"))[0] is False and wf.inside(str(tmp_path / "a"))[0] is False
    assert wf.inside(str(tmp_path / "link-in")) == (True, "inside")                                       # 符号链接解析以后再比
    assert wf.inside(str(root / "sub" / "link-out"))[0] is False                                          # 指到工作区外的链接不算
    assert wf.inside(None) == (False, "cwd_missing") and wf.inside("") == (False, "cwd_invalid") and wf.inside(5)[1] == "cwd_invalid"
    assert wf.inside("relative/path") == (False, "cwd_not_absolute")
    assert WorkspaceFilter(str(root), extra_roots=[str(tmp_path / "elsewhere")]).inside(str(tmp_path / "elsewhere"))[0] is True     # 工作树要明确列进来
    with pytest.raises(ValueError):
        WorkspaceFilter("relative")
