"""四种模式（默认只给工具）、回执、固定提醒、便条按宿主单位核大小、取材 3 秒超时、目录的先后（开始只标记、禁读先于目录、恢复／压缩后重交、注入失败不算已交）。第二段·交回点甲。全部自造数据。"""
import dataclasses
import threading

import pytest

from helpers import Clock, Scripted, make_mem, out_thread, sids_of
from helpers2 import TEXTS, Counter, fill, hybrid, organized
from yuanwei import hosts as H
from yuanwei import note as NT
from yuanwei import rules as RL
from yuanwei.access import Access
from yuanwei.modes import ModeConfig, ModeRunner

W1 = "本轮未新增读取长期记忆"
W3 = "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认"
W4 = "无法确认权限，已暂停新增读取"


def runner(tmp_path, mode, host="pi", mem=None, profile=None, **cfg):
    mem = mem or organized(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, profile or H.profile(host), ModeConfig(mode=mode, **cfg))
    return mem, acc, r


# ------------------------------------------------------------------ 只给工具、固定提醒
def test_tools_only_delivers_nothing_when_allowed_and_only_the_receipt_when_denied(tmp_path):
    mem, acc, r = runner(tmp_path, "tools_only")
    cnt = Counter(mem)
    try:
        a = r.on_user_prompt("S", "T1", "吃了什么烤鱼的事")
        assert a["inject"] is None and a["kind"] == "none"
        b = r.on_user_prompt("S", "T2", "#yuanwei-nomem\n吃了什么烤鱼的事")
        assert b["kind"] == "receipt" and b["inject"].startswith(W1) and b["reason"] == "control_nomem"
        assert cnt.calls == []                                                      # 默认模式一次读取都没有
    finally:
        cnt.close()


def test_reminder_is_exact_text_and_reads_nothing_and_depends_only_on_three_things(tmp_path):
    mem, acc, r = runner(tmp_path, "reminder")
    cnt = Counter(mem)
    try:
        a = r.on_user_prompt("S", "T1", "随便问点什么")
        assert a["inject"] == "需要历史依据时，可调用 recall 查证；本轮禁读时不要调用记忆读取工具。" == RL.REMINDER and a["kind"] == "reminder"
        assert cnt.calls == []
    finally:
        cnt.close()
    # 库是空的、最近没有任何更新，也照样给（发不发不看库里有什么）
    tmp = tmp_path / "empty"
    tmp.mkdir()
    em = make_mem(tmp, provider=Scripted("skip"))
    acc2 = Access(em)
    r2 = ModeRunner(em, acc2, H.profile("pi"), ModeConfig(mode="reminder"))
    assert r2.on_user_prompt("S", "T1", "你好")["inject"] == RL.REMINDER
    # 工具没装好：不给
    r3 = ModeRunner(em, acc2, H.profile("pi"), ModeConfig(mode="reminder", tools_installed=False))
    assert r3.on_user_prompt("S", "T2", "你好")["inject"] is None


def test_reminder_denied_turn_gets_the_receipt_not_the_reminder(tmp_path):
    mem, acc, r = runner(tmp_path, "reminder")
    a = r.on_user_prompt("S", "T1", "#yuanwei-nomem")
    assert a["kind"] == "receipt" and RL.REMINDER not in a["inject"] and a["inject"].startswith(W1)
    mem.set_read_permission(False)
    b = r.on_user_prompt("S", "T2", "普通一句")
    assert b["kind"] == "receipt" and RL.REMINDER not in b["inject"]


def test_unknown_origin_and_replay_events_do_nothing_and_do_not_clear_a_ban(tmp_path):
    mem, acc, r = runner(tmp_path, "reminder")
    r.on_user_prompt("S", "T1", "#yuanwei-nomem")
    x = r.on_user_prompt("S", "X", "定时任务来的提示", origin="unknown")
    assert x["inject"] is None and x["kind"] == "ignored" and x["registered"] is False
    y = r.on_user_prompt("S", "Y", "#yuanwei-nomem", replay=True)
    assert y["kind"] == "ignored"
    assert acc.start_read(__import__("yuanwei.access", fromlist=["ToolIdentity"]).ToolIdentity("pi", "S", "", "T1"), "recall")["reason"] == "control_nomem"


def test_registration_failure_gives_unknown_permission_receipt_and_no_read(tmp_path):
    mem, acc, r = runner(tmp_path, "note")
    cnt = Counter(mem)
    try:
        acc.open_turn("pi", "S", "T1")                          # 登记停在一半
        out = r.pre_answer("S", "T1", "吃了烤鱼")
        assert out["kind"] == "receipt" and out["inject"].startswith(W4) and cnt.calls == []
    finally:
        cnt.close()


# ------------------------------------------------------------------ 便条
def test_note_mode_delivers_the_note_and_registers_the_read(tmp_path):
    mem, acc, r = runner(tmp_path, "note")
    out = r.on_user_prompt("S", "T1", "吃了什么烤鱼")
    assert out["kind"] == "note" and TEXTS[1] in out["inject"] and len(out["inject"]) <= 8000
    rows = acc.log_rows("pi", "S", "T1")
    kinds = [x["kind"] for x in rows]
    assert kinds[0] == "start" and "search_entered" in kinds and "assembled" in kinds and kinds[-1] == "returned"
    assert rows[-1]["detail"]["delivery"] == "unknown" and rows[-1]["detail"]["path"] == "hook_context"


def test_note_denied_turn_never_reaches_the_reader_and_text_recognition_is_only_an_assist(tmp_path):
    mem, acc, r = runner(tmp_path, "note")
    cnt = Counter(mem)
    try:
        for i, t in enumerate(["#yuanwei-nomem\n吃了什么烤鱼", "不要用以前的聊天记录，吃了什么烤鱼", "吃了什么烤鱼"]):
            if i == 2:
                mem.set_read_permission(False)
            out = r.on_user_prompt("S", "T%d" % i, t)
            assert out["kind"] == "receipt", t
        assert cnt.calls == []
        assert not [x for x in acc.log_rows("pi", "S", "T0") if x["kind"] in ("start", "search_entered")]
    finally:
        cnt.close()


def test_note_free_gate_skips_greeting_after_permission_and_does_not_search(tmp_path):
    mem, acc, r = runner(tmp_path, "note")
    cnt = Counter(mem)
    try:
        out = r.on_user_prompt("S", "T1", "你好")
        assert out["inject"] is None and out["kind"] == "skipped"
        assert [c for c in cnt.calls if c in ("select", "candidates", "snapshot")] == []
        out2 = r.on_user_prompt("S", "T2", "你好", host_command=True)
        assert out2["kind"] == "skipped"
        denied = r.on_user_prompt("S", "T3", "#yuanwei-nomem")
        assert denied["kind"] == "receipt"                                              # 禁读先于免查门
    finally:
        cnt.close()


def test_note_final_text_is_measured_in_host_unit_and_shrinks_to_fit(tmp_path):
    emoji = ["烤鱼" + "😀" * 900 for _ in range(12)]
    mem = organized(tmp_path, texts=emoji)
    cc = H.profile("claude-code")
    acc = Access(mem)
    r = ModeRunner(mem, acc, cc, ModeConfig(mode="note"))
    out = r.on_user_prompt("S", "T1", "烤鱼")
    assert out["kind"] == "note"
    assert len(out["inject"].encode("utf-16-le")) // 2 <= 10000                          # 宿主按 UTF-16 数：8000 个码点的表情会是 16000，必须缩
    assert len(out["inject"]) < 6000


def test_note_time_budget_is_only_about_fetching_late_result_never_reaches_next_turn(tmp_path, monkeypatch):
    mem, acc, r = runner(tmp_path, "note", auto_timeout_s=0.2)
    release = threading.Event()
    orig = NT.analyze
    state = {"n": 0}

    def slow(reader, query, cfg, cut=None):
        state["n"] += 1
        if state["n"] == 1:
            release.wait(5)
        return orig(reader, query, cfg, cut)
    monkeypatch.setattr(NT, "analyze", slow)
    a = r.on_user_prompt("S", "T1", "吃了什么烤鱼")
    assert a["kind"] == "timeout" and a["inject"] is None
    kinds1 = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "timeout" in kinds1 and "returned" not in kinds1 and "search_entered" in kinds1          # 已经发生的读取照实记
    b = r.on_user_prompt("S", "T2", "预算批下来了吗")                                          # 上一个线程还没结束：同一会话不叠开，这一轮什么都不加（补充施工单一）
    assert b["kind"] == "busy" and b["inject"] is None
    release.set()
    r.join_workers(5)
    k1 = [x["kind"] for x in acc.log_rows("pi", "S", "T1")]
    assert "late_result_dropped" in k1 and "returned" not in k1
    b = r.on_user_prompt("S", "T3", "预算批下来了吗")
    assert b["kind"] == "note" and TEXTS[4] in b["inject"]
    for t in ("T2", "T3"):
        assert "late_result_dropped" not in [x["kind"] for x in acc.log_rows("pi", "S", t)]
    rets = [x for t in ("T1", "T2", "T3") for x in acc.log_rows("pi", "S", t) if x["kind"] == "returned"]
    assert len(rets) == 1 and rets[0]["turn_id"] == "T3"                                        # 迟到的 T1 结果没有交付给任何一轮


def test_timeout_never_turns_a_registration_failure_into_allow(tmp_path):
    mem, acc, r = runner(tmp_path, "note", auto_timeout_s=0.2)
    acc.open_turn("pi", "S", "T0")
    out = r.pre_answer("S", "T0", "吃了烤鱼")
    assert out["kind"] == "receipt" and out["inject"].startswith(W4)


def test_note_errors_do_not_fail_the_turn_and_are_not_reported_as_empty(tmp_path):
    mem, client = hybrid(tmp_path)
    mem._reader.embed_client = None
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="note"))
    out = r.on_user_prompt("S", "T1", "吃了什么烤鱼")
    assert out["inject"] is None and out["kind"] == "error" and out["error"] == "missing_vector"
    assert [x for x in acc.log_rows("pi", "S", "T1") if x["kind"] == "error"]


# ------------------------------------------------------------------ 目录
def thread_mem(tmp_path, n=3):
    names = ["主线甲", "主线乙", "主线丙", "主线丁"]

    def mode(request, k):
        return out_thread(sids_of(request["text"]), name=names[k - 1])
    mem = make_mem(tmp_path, provider=Scripted(mode))
    for i in range(n):
        s = "S%d" % i
        mem.record("h", s, s + "-1", "user", "第%d件事的话" % i, time="2027-01-%02d" % (i + 1))
        mem.record("h", s, s + "-2", "assistant", "好的", time="2027-01-%02d" % (i + 1))
        mem.end_session("h", s)
    mem.run()
    return mem


def test_directory_start_and_resume_only_mark_and_first_denied_prompt_never_reads_it(tmp_path):
    mem = thread_mem(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="directory"))
    cnt = Counter(mem)
    try:
        for src in ("startup", "resume", "clear", "compact", "fork"):
            assert r.session_event("S", src)["marked"] is True
        assert cnt.calls == [] and not acc.log_rows("pi", "S", "")
        out = r.on_user_prompt("S", "T1", "#yuanwei-nomem\n要不要看目录")
        assert out["kind"] == "receipt" and cnt.calls == []                             # 首句就禁读：不读目录
        assert acc.dir_get("pi", "S", "")["state"] == "needed"
        out2 = r.on_user_prompt("S", "T2", "这一轮可以了")
        assert out2["kind"] == "directory" and "主线甲" in out2["inject"] and len(out2["inject"]) <= 2000
        assert not [c for c in cnt.calls if c in ("select", "select_multi", "candidates")]      # 不先跑一遍便条检索再丢掉
    finally:
        cnt.close()
    with pytest.raises(ValueError):
        r.session_event("S", "weird")


def test_directory_not_handed_until_injection_is_confirmed_failures_redeliver(tmp_path):
    mem = thread_mem(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="directory"))
    r.session_event("S", "startup")
    a = r.on_user_prompt("S", "T1", "第一问")
    assert a["kind"] == "directory"
    r.confirm_injection(a, ok=False)                                                    # 注入失败：不算已交
    b = r.on_user_prompt("S", "T2", "第二问")
    assert b["kind"] == "directory"
    r.confirm_injection(b, ok=True)
    c = r.on_user_prompt("S", "T3", "第三问")
    assert c["kind"] == "none" and c["inject"] is None                                  # 已经交过：同一份不重复交
    r.session_event("S", "resume")                                                      # 恢复后即使内容没变也要重交
    d = r.on_user_prompt("S", "T4", "第四问")
    assert d["kind"] == "directory"
    r.confirm_injection(d, ok=True)
    r.session_event("S", "compact")
    assert r.on_user_prompt("S", "T5", "第五问")["kind"] == "directory"


def test_stale_confirmation_after_a_newer_boundary_does_not_mark_handed(tmp_path):
    mem = thread_mem(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="directory"))
    r.session_event("S", "startup")
    a = r.on_user_prompt("S", "T1", "第一问")
    r.session_event("S", "compact")                                                    # 注入还没确认，压缩又来了
    r.confirm_injection(a, ok=True)
    assert acc.dir_get("pi", "S", "")["state"] == "needed"


def test_compaction_in_the_same_turn_re_delivers_under_the_current_turns_permission(tmp_path):
    mem = thread_mem(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="directory"))
    r.session_event("S", "startup")
    a = r.on_user_prompt("S", "T1", "第一问")
    r.confirm_injection(a, ok=True)
    c = r.compact_continue("S", "T1")
    assert c["kind"] == "directory" and "主线甲" in c["inject"]
    r.on_user_prompt("S", "T2", "#yuanwei-nomem")
    cnt = Counter(mem)
    try:
        d = r.compact_continue("S", "T2")                                              # 禁读这一轮：沿用，只给回执
        assert d["kind"] == "receipt" and d["inject"].startswith(W1) and cnt.calls == []
    finally:
        cnt.close()
    assert r.compact_continue("S", "T-old")["kind"] == "ignored"


def test_directory_state_is_per_session_and_directory_read_is_logged_with_its_entry(tmp_path):
    mem = thread_mem(tmp_path)
    acc = Access(mem)
    r = ModeRunner(mem, acc, H.profile("pi"), ModeConfig(mode="directory"))
    a = r.on_user_prompt("S", "T1", "第一问")
    r.confirm_injection(a, ok=True)
    assert r.on_user_prompt("S", "T2", "第二问")["kind"] == "none"
    b = r.on_user_prompt("S2", "T1", "另一个会话的第一问")
    assert b["kind"] == "directory"                                                     # 另一个会话的状态自己管
    rows = acc.log_rows("pi", "S", "T1")
    assert rows[0]["kind"] == "start" and rows[0]["entry"] == "directory" and [x for x in rows if x["kind"] == "assembled"]


def test_directory_over_host_limit_shrinks_and_empty_workspace_has_nothing(tmp_path):
    mem = thread_mem(tmp_path, n=4)
    acc = Access(mem)
    tiny = dataclasses.replace(H.profile("pi"), paths=dict(H.profile("pi").paths, hook_context=H.Path("hook_context", (H.Limit("cp", 400, "x"),), {}, False, "t")))
    r = ModeRunner(mem, acc, tiny, ModeConfig(mode="directory"))
    out = r.on_user_prompt("S", "T1", "第一问")
    assert out["kind"] == "directory" and len(out["inject"]) <= 400
    (tmp_path / "x").mkdir()
    em = make_mem(tmp_path / "x", provider=Scripted("skip"))
    r2 = ModeRunner(em, Access(em), H.profile("pi"), ModeConfig(mode="directory"))
    assert r2.on_user_prompt("S", "T1", "问")["kind"] == "none"


def test_mode_names_and_rules(tmp_path):
    for m in ("tools_only", "reminder", "note", "directory"):
        assert ModeConfig(mode=m).mode == m
    with pytest.raises(ValueError):
        ModeConfig(mode="auto")
    assert ModeConfig().mode == "tools_only"                                            # 默认只给工具
    assert RL.RULES["tools_only"] == RL.RULES["reminder"] == RL.TOOLS_RULE and RL.NOTE_RULE == NT.RULE_LINE and RL.RULES["note"] == NT.RULE_LINE + RL.WINDOW_RULE + RL.DELETE_RULE      # 便条的常驻规矩＝旧文＋窗口开关那段（主会话裁定 11-10，方案 a）；便条正文里的 RULE_LINE 不动
