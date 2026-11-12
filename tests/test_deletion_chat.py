"""在对话里删：agent 申请、用户整条回复「确认删除」才删；确认那一轮不记；提示和回执；直接显示的两种模式（删除与密钥过滤·交回点乙）。假宿主，没有网络。"""
import json

import pytest

import delete_confirm_table as CT
import window_open_table as WT
from test_hermes_adapter import call, mk, msgs, stored, turn
from yuanwei import window as W
from yuanwei.hermes_adapter import HermesAdapter


def talk(a, n, text, reply="好的", **kw):
    turn(a, n, text)
    a.sync_turn(text, reply, session_id=kw.get("session", "hs1"), messages=msgs(text))


def setup_chat(tmp_path):
    mem, a = mk(tmp_path)
    talk(a, 1, "今天聊聊我养的猫")
    talk(a, 2, "它最爱吃烤鱼")
    return mem, a


def request(a, n):
    talk(a, n, "请把这段对话在原委里的记忆删掉", "好，我来申请")
    return call(a, "window_delete")


def n_deletions(mem):
    return mem.store.conn.execute("SELECT COUNT(*) FROM deletions").fetchone()[0]


def test_agent_requests_then_user_exact_confirm_deletes(tmp_path):
    mem, a = setup_chat(tmp_path)
    assert len(stored(mem)) == 4
    r = request(a, 3)
    assert r["state"] == "waiting" and r["reason"] == "awaiting_delete_confirmation" and r["scope"]["messages"] == 6
    assert "回复「确认删除」即可" in r["message"] and "从这个窗口分叉出去的对话里，分叉之前那段属于这个窗口，也会一起删掉。" in r["message"] and "烤鱼" not in r["message"]
    assert n_deletions(mem) == 0 and len(stored(mem)) == 6
    turn(a, 4, "确认删除")
    assert stored(mem) == [] and n_deletions(mem) == 1
    assert mem.store.conn.execute("SELECT state FROM deletions").fetchone()[0] == "complete"
    # 别的会话（预置的 S1）里的原话不动
    assert [m for m in mem.store.list_messages(mem.ns) if m["session_id"] == "S1"]


@pytest.mark.parametrize("row", [r for r in CT.ROWS if not r["confirm"]][:14] + [r for r in CT.ROWS if not r["confirm"]][-6:], ids=lambda r: r["id"])
def test_non_confirm_inputs_void_the_pending_and_delete_nothing(tmp_path, row):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, row["text"])
    assert n_deletions(mem) == 0 and len(stored(mem)) == 6
    turn(a, 5, "确认删除")                                                      # 待确认已经作废：之后单独说确认也不删
    assert n_deletions(mem) == 0


def test_no_sentence_of_the_open_table_confirms_and_open_pending_is_unaffected(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, WT.ROWS[0]["text"])
    assert n_deletions(mem) == 0


def test_tool_param_cannot_confirm_and_subagent_cannot_request(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    r = json.loads(a.handle_tool_call("window_delete", {"confirm": True, "text": "确认删除", "window_id": "x"}))
    assert r["state"] == "waiting" and n_deletions(mem) == 0
    sub = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    sub.initialize("sub1", platform="cli", agent_context="subagent", hermes_home=str(tmp_path / "home2"))
    assert json.loads(sub.handle_tool_call("window_delete", {}))["reason"] in ("inactive", "feature_off_tools") and n_deletions(mem) == 0
    cron = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    cron.initialize("c1", platform="cron", agent_context="primary", hermes_home=str(tmp_path / "home3"))
    assert json.loads(cron.handle_tool_call("window_delete", {}))["reason"] in ("inactive", "feature_off_tools")


def test_open_and_delete_pendings_are_mutually_exclusive_and_words_do_not_cross(tmp_path):
    mem, a = setup_chat(tmp_path)
    call(a, "window_off")
    talk(a, 3, "继续")
    call(a, "window_on")                                                          # 开启待确认
    d = call(a, "window_delete")                                                  # 删除申请顶掉它
    assert d["state"] == "waiting"
    turn(a, 4, "确认开启")
    assert a.win.view("hermes", "hs1").state == "off" and n_deletions(mem) == 0   # 开启口令打不开，也删不了
    request(a, 5)
    call(a, "window_on")                                                          # 再申请开启：删除待确认作废
    turn(a, 6, "确认删除")
    assert n_deletions(mem) == 0


def test_repeat_confirm_returns_same_result_and_never_deletes_twice(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    ref = a._cur_ref["hs1"]
    again = a.win.process_input(ref, "确认删除", origin_verified=True)
    assert again.deletion is not None and again.deletion.del_id and n_deletions(mem) == 1
    talk(a, 5, "又说了一句")
    turn(a, 6, "确认删除")                                                          # 第二次说确认：没有待确认，不删
    assert n_deletions(mem) == 1


def test_pending_is_bound_to_window_db_identity_and_input_seq(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    row = dict(mem.store.conn.execute("SELECT * FROM delete_pending").fetchone())
    assert row["db_id"] == mem.store.db_id() and row["window_id"] == a._win_ref.window_id and row["request_win_seq"] >= 1 and row["request_capture"] >= 1 and row["state"] == "waiting"
    mem.store.conn.execute("UPDATE delete_pending SET db_id='别的库'")           # 别的库的待确认消费不了
    turn(a, 4, "确认删除")
    assert n_deletions(mem) == 0 and mem.store.conn.execute("SELECT void_reason FROM delete_pending").fetchone()[0] == "confirm_failed"


def test_pending_after_restart_is_valid_only_for_the_next_input(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    mem2, a2 = None, HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem, executor="inline")
    a2.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))       # 重启：同一个库、同一个会话号
    turn(a2, 4, "确认删除")
    assert n_deletions(mem) == 1
    mem_b, b = setup_chat(tmp_path / "b")
    request(b, 3)
    b2 = HermesAdapter(dict(mode="tools_only", spawn="off"), memory=mem_b, executor="inline")
    b2.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "b" / "home"))
    turn(b2, 4, "等一下")
    turn(b2, 5, "确认删除")
    assert n_deletions(mem_b) == 0


def test_closed_window_can_still_request_and_confirm_without_reading_history(tmp_path):
    mem, a = setup_chat(tmp_path)
    call(a, "window_off")
    talk(a, 3, "继续说")
    mem.check_and_spawn = lambda: (_ for _ in ()).throw(AssertionError("删除不能拉起整理"))
    d = call(a, "window_delete")
    assert d["state"] == "waiting"
    turn(a, 4, "确认删除")
    assert n_deletions(mem) == 1 and a.win.view("hermes", "hs1").state == "off"        # 删除不改窗口开关


def test_confirm_turn_not_recorded_across_duplicate_event_compression_late_reply_and_restart(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    tok = a._cur["hs1"]
    turn(a, 4, "确认删除")                                                          # 重复事件
    a.on_session_switch("hs2", parent_session_id="hs1", reason="compression")        # 压缩换号
    a.sync_turn("确认删除", "已经删掉了", session_id="hs2", messages=msgs("确认删除"))     # 迟到的助手回复
    assert stored(mem) == [] and [m for m in mem.store.list_messages(mem.ns) if m["session_id"] == "hs2"] == []
    r = mem.record("hermes", "hs2", tok + "-a-xxxx", "assistant", "迟到", turn_id=tok)
    assert r["blocked"] == "no_record_turn"                                          # 持久标记：重启以后也一样（在库里）
    assert mem.store.conn.execute("SELECT COUNT(*) FROM no_record_turns").fetchone()[0] == 1
    talk(a, 5, "这是确认之后的新话", session="hs2")                                    # 下一条真正的新输入照常记
    assert [x[2] for x in [(m["session_id"], m["role"], m["content"]) for m in mem.store.list_messages(mem.ns)] if x[0] == "hs2"] == ["这是确认之后的新话", "好的"]


def test_receipts_reach_the_model_the_same_turn_and_later_through_window_state(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    turn(a, 4, "确认删除")
    text = a.prefetch("确认删除", session_id="hs1")
    assert "已删掉这段对话在原委里记下的 6 条原话" in text and "Hermes 自己的会话记录也不归原委管" in text
    s = call(a, "window_state")
    assert s["last_deletion"]["state"] == "complete" and s["last_deletion"]["messages"] == 6


def test_direct_display_modes_A_B_unconfirmed_B_and_fixed_text_appended(tmp_path):
    mem, a = setup_chat(tmp_path)
    assert a.status()["direct_display"] == {"mode": "A", "hook_registered": False, "hook_calls": 0}
    a.hook_registered = True
    assert a.status()["direct_display"]["mode"] == "B_unconfirmed"
    request(a, 3)                                                                    # 申请的提示进「要直接显示」的队列
    out = a.display_hook(response_text="好，请回复确认删除。", session_id="hs1", model="m", platform="cli")
    assert out.startswith("好，请回复确认删除。") and "要删掉这段对话在原委里记下的全部内容吗" in out
    assert a.status()["direct_display"]["mode"] == "B" and a.display_hook(response_text="x") is None
    turn(a, 4, "确认删除")
    out2 = a.display_hook(response_text="已处理。")
    assert "已删掉这段对话" in out2 or "已从原委里撤销" in out2


def test_plugin_file_registers_the_display_hook_only_when_the_loader_offers_register_hook(tmp_path):
    from yuanwei.hermes_setup import plugin_init_text as plugin_source
    src = plugin_source() if callable(plugin_source) else ""
    assert "transform_llm_output" in src and "display_hook" in src


def test_failed_confirm_voids_pending_and_says_nothing_changed(tmp_path):
    mem, a = setup_chat(tmp_path)
    request(a, 3)
    a.win.deleter.fault = lambda point: (_ for _ in ()).throw(RuntimeError("boom")) if point == "in_tx" else None
    turn(a, 4, "确认删除")
    assert n_deletions(mem) == 0 and len(stored(mem)) == 6
    assert "没有被改动" in "".join(a._notices) and "已删掉" not in "".join(a._notices)
    a.win.deleter.fault = None
    turn(a, 5, "确认删除")
    assert n_deletions(mem) == 0


def _load_plugin(tmp_path):
    import abc
    import importlib.util
    import sys
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
    base.MemoryProvider = MemoryProvider
    saved = {k: sys.modules.get(k) for k in ("agent", "agent.memory_provider")}
    sys.modules["agent"], sys.modules["agent.memory_provider"] = types.ModuleType("agent"), base
    try:
        f = tmp_path / "plugin_init.py"
        f.write_text(__import__("yuanwei.hermes_setup", fromlist=["x"]).plugin_init_text(), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("plug_fake", str(f))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_fake_host_plugin_register_probes_register_hook(tmp_path):
    mod = _load_plugin(tmp_path)

    class CtxWith:
        def __init__(self):
            self.hooks, self.prov = [], None

        def register_memory_provider(self, p):
            self.prov = p

        def register_hook(self, name, cb):
            self.hooks.append((name, cb))

    class CtxWithout:
        def register_memory_provider(self, p):
            self.prov = p
    c1, c2 = CtxWith(), CtxWithout()
    mod.register(c1)
    mod.register(c2)
    assert c1.hooks and c1.hooks[0][0] == "transform_llm_output" and c1.prov._hook_registered is True
    assert c2.prov._hook_registered is False
    assert c1.prov.display_hook(response_text="x") is None                              # 没初始化：不改回复

    class Boom(CtxWith):
        def register_hook(self, name, cb):
            raise RuntimeError("加载器不支持")
    c3 = Boom()
    mod.register(c3)
    assert c3.prov._hook_registered is False                                             # 注册出错：退回 A，插件照常加载


def test_late_reply_of_a_turn_started_before_delete_is_blocked_by_its_own_capture(tmp_path):
    from yuanwei.deletion import DeletionService, legacy_conv_id
    mem, a = setup_chat(tmp_path)
    turn(a, 3, "删除之前开始的一轮")                                              # 取了捕获序号，助手回复还没来
    DeletionService(mem.db_path, config_dir=mem.config_dir, clock=mem.clock).delete_conversation(a._win_ref.window_id)
    a.sync_turn("删除之前开始的一轮", "迟到的回复", session_id="hs1", messages=msgs("删除之前开始的一轮"))
    assert stored(mem) == [] and a.skipped.get("blocked_before_barrier") == 1
    talk(a, 4, "删除之后的新一轮")                                                 # 新一轮的号大于地板：照常记
    assert [x[2] for x in stored(mem)][0] == "删除之后的新一轮"


def test_docs_state_the_boundary_and_do_not_claim_forensic_erasure_or_agent_cannot_bypass():
    import os
    d = os.path.join(os.path.dirname(__file__), "..", "docs")
    rules = open(os.path.join(d, "产品规则表.md"), encoding="utf-8").read()
    hermes = open(os.path.join(d, "装到Hermes上.md"), encoding="utf-8").read()
    for t in (rules, hermes):
        assert "助手自己删不了" in t and "只指对话里这组工具的约定" in t
    assert "Hermes 自己的会话记录" in hermes and "磁盘备份" in hermes and "shell" in rules
    assert "Time Machine" in rules and "不能保证你在回复前一定看到了准确的范围提示" in rules
    assert "彻底删除" not in rules.split("## 13")[1]
    assert "认不出的格式可能会漏" in hermes and "中文写的密码" in rules and "分叉之前那段属于这个窗口，也会一起删掉" in rules
