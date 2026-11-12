"""读取权限：note、recall、get_messages（含旧的续读游标）、attachments 统一先过同一个检查，在搜索、向量调用、拉起后台整理之前。
用计数器和会炸的假件断言「没发生」，不只看返回是不是空。全部自造数据。"""
import pytest

from helpers import Clock, Scripted, make_mem
from yuanwei import Config, Memory
from yuanwei.memory import PERSIST_HINT, refusal_text


def organize(mem, session, texts, date="2027-02-01"):
    for i, t in enumerate(texts, 1):
        mem.record("h", session, "%s-%d" % (session, i), "user" if i % 2 else "assistant", t, time=date)
    mem.end_session("h", session)
    mem.run()


class Watch:
    """把读取层、拉起整理、向量客户端换成计数器：被调用就记一次（并可以选择直接炸）。"""

    def __init__(self, mem):
        self.mem, self.calls = mem, []
        rd = mem._reader
        for name in ("select", "snapshot", "recall", "get_messages", "attachments", "_prepare_query_vector"):
            orig = getattr(rd, name)
            setattr(rd, name, self._wrap(name, orig))
        orig_spawn = mem.check_and_spawn
        mem.check_and_spawn = self._wrap("check_and_spawn", orig_spawn)
        orig_cursor = mem.store.get_cursor
        mem.store.get_cursor = self._wrap("get_cursor", orig_cursor)

    def _wrap(self, name, fn):
        def inner(*a, **k):
            self.calls.append(name)
            return fn(*a, **k)
        return inner

    def reset(self):
        self.calls.clear()


def mk(tmp_path, **cfg):
    mem = make_mem(tmp_path, provider=Scripted("skip"), **cfg)
    organize(mem, "S1", ["今天去了海边捡贝壳", "海边风很大，贝壳很漂亮", "晚上吃了烤鱼", "烤鱼很好吃", "明天要开会讨论预算", "预算已经批下来了"])
    return mem


def all_entries(mem, mid, cursor, **kw):
    """四个入口各调一次（带同样的权限上下文）。"""
    return {"note": mem.note("贝壳", **{k: v for k, v in kw.items() if k in ("turn_id", "history_allowed", "session_id")}),
            "recall": mem.recall("贝壳", **kw), "get_messages": mem.get_messages([mid], **kw), "get_messages_cursor": mem.get_messages(cursor=cursor, **kw),
            "attachments": mem.attachments(mid, **kw)}


def tool_entries(mem, mid, cursor, **kw):
    """模型能直接调的工具入口（不含 note：note 每次都开新的一轮）。"""
    return {
        "recall": mem.recall("贝壳", **kw), "get_messages": mem.get_messages([mid], **kw), "get_messages_cursor": mem.get_messages(cursor=cursor, **kw), "attachments": mem.attachments(mid, **kw)}


def setup(tmp_path, **cfg):
    mem = mk(tmp_path, **cfg)
    mid = next(iter(mem._reader.snapshot().by_id))
    cur = mem._reader.store.put_cursor("c" * 16, "w", "get_messages", None, {"cut": mem.cut(), "chunks": [{"msg_id": mid, "session_id": "S1", "date": "2027-02-01", "role": "user",
                                                                                                         "content": "x", "fingerprint": mem.store.message_row("w", mid)["fingerprint"]}]})
    return mem, mid, "c" * 16 + ":0:0"


# ------------------------------------------------------------------ 禁读那一轮：四个入口都在检索、向量、拉起之前被拒
def test_denied_turn_blocks_every_entry_before_search_vectors_or_spawn(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    w = Watch(mem)
    mem.note("这一轮别用以前的对话", turn_id="T1", session_id="S9")
    assert w.calls == []                                                           # note 自己也在检索、向量、拉起、游标查找之前就拒了
    for name, r in all_entries(mem, mid, old_cursor, turn_id="T1", session_id="S9").items():
        assert r["status"] == "refused", name
    assert w.calls == []


def test_omitting_turn_or_passing_allowed_true_cannot_bypass_an_established_ban(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    w = Watch(mem)
    assert mem.note("不要参考我们之前聊的", turn_id="T1")["status"] == "refused"
    for kw in ({}, {"history_allowed": True}, {"turn_id": "T1", "history_allowed": True}):
        for name, r in tool_entries(mem, mid, old_cursor, **kw).items():
            assert r["status"] == "refused", (name, kw)
    assert w.calls == []
    mem.end_turn("T1")
    assert mem.get_messages([mid], history_allowed=True)["messages"]


def test_host_marker_false_denies_every_entry_and_marks_the_turn(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    w = Watch(mem)
    for name, r in all_entries(mem, mid, old_cursor, history_allowed=False, turn_id="T2").items():
        assert r["status"] == "refused", name
    assert w.calls == [] and mem.turn_refused("T2")
    assert mem.recall("贝壳", turn_id="T2")["status"] == "refused"                  # 之后只给轮次编号也拒


def test_old_cursor_is_blocked_before_the_cursor_lookup(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    assert mem.get_messages(cursor=old_cursor)["messages"]                         # 没禁的时候游标是好的
    mem.set_read_permission(False)
    w = Watch(mem)
    r = mem.get_messages(cursor=old_cursor)
    assert r["status"] == "refused" and "get_cursor" not in w.calls and w.calls == []


# ------------------------------------------------------------------ 免查门关着，禁读照样认
def test_text_ban_is_recognised_even_with_the_free_gate_off(tmp_path):
    mem, mid, old_cursor = setup(tmp_path, note_free_gate=False)
    w = Watch(mem)
    r = mem.note("这轮不要用记忆功能", turn_id="T3")
    assert r["status"] == "refused" and r["note"] == refusal_text("clause_ban") and w.calls == []
    assert mem.attachments(mid, turn_id="T3")["status"] == "refused"
    assert mem.note("你好")["status"] != "refused"                                  # 免查门关着：寒暄也去检索，但不是被拒


# ------------------------------------------------------------------ 持续设置
def test_workspace_setting_persists_across_processes_and_blocks_all_entries(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    state = mem.set_read_permission(False)
    assert state["allowed"] is False and state["denied_by"] == [{"scope": "workspace", "key": ""}]
    mem.close()
    mem2 = make_mem(tmp_path, provider=Scripted("skip"))
    w = Watch(mem2)
    assert mem2.read_permission()["allowed"] is False and mem2.status()["read_permission"]["allowed"] is False
    for name, r in all_entries(mem2, mid, old_cursor).items():
        assert r["status"] == "refused" and r["reason"] == "workspace_setting", name
    assert w.calls == []
    assert mem2.clear_read_permission()["now"]["allowed"] is True
    assert mem2.recall("贝壳")["delivered"]


def test_session_setting_only_blocks_that_session_and_allow_does_not_override_a_deny(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    mem.set_read_permission(False, scope="session", session_id="SA")
    assert mem.recall("贝壳", session_id="SA")["status"] == "refused"
    assert mem.recall("贝壳", session_id="SB")["delivered"]
    assert mem.read_permission("SA")["allowed"] is False and mem.read_permission("SB")["allowed"] is True
    mem.set_read_permission(True)                                                  # 工作区写明允许，不能解除会话那一级的禁止
    assert mem.recall("贝壳", session_id="SA")["status"] == "refused"
    mem.begin_turn("T5", "SA")                                                     # 省略 session_id 的工具调用，按最近声明的轮次所在的会话算
    assert mem.get_messages([mid])["status"] == "refused"
    with pytest.raises(ValueError):
        mem.set_read_permission(False, scope="session")
    with pytest.raises(ValueError):
        mem.set_read_permission(False, scope="elsewhere")
    mem.clear_read_permission(scope="session", session_id="SA")
    assert mem.get_messages([mid])["messages"]


def test_persistent_wording_refuses_this_turn_points_to_the_switch_and_changes_no_setting(tmp_path):
    mem = mk(tmp_path)
    r = mem.note("从现在起别用我的记忆", turn_id="T6")
    assert r["status"] == "refused" and r["persistent"] is True and r["note"].endswith(PERSIST_HINT)
    assert mem.read_permission()["settings"] == [] and mem.read_permission()["allowed"] is True       # 不自动替用户改持续设置
    mem.end_turn("T6")
    assert mem.note("查一下贝壳", turn_id="T7")["status"] == "ok"                      # 下一轮照常


def test_note_without_turn_id_still_starts_its_own_turn(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    assert mem.note("别看聊天记录")["status"] == "refused"
    assert mem.get_messages([mid])["status"] == "refused"                           # 没给轮次编号：note 自己起了一个，之后的工具调用按它算
    assert mem.note("查一下贝壳")["status"] == "ok"                                  # 下一次 note 是新的一轮
    assert mem.get_messages([mid])["messages"]


# ------------------------------------------------------------------ 拿不到权限上下文：不当成允许
def test_unreadable_permission_state_is_a_refusal_not_an_allow(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    w = Watch(mem)
    def boom(*a, **k):
        raise RuntimeError("读不了库")
    mem.store.read_settings = boom
    for name, r in all_entries(mem, mid, old_cursor).items():
        assert r["status"] == "refused" and r["reason"] == "permission_unknown", name
    assert w.calls == []


def test_allowed_path_still_works_and_searches(tmp_path):
    mem, mid, old_cursor = setup(tmp_path)
    w = Watch(mem)
    r = mem.note("查一下贝壳", turn_id="T8")
    assert r["status"] == "ok" and "check_and_spawn" in w.calls and "snapshot" in w.calls
    assert mem.recall("贝壳", turn_id="T8")["delivered"]


def test_status_reports_the_read_permission(tmp_path):
    mem = mk(tmp_path)
    assert mem.status()["read_permission"] == {"allowed": True, "settings": [], "denied_by": []}
    mem.set_read_permission(False)
    assert mem.status()["read_permission"]["allowed"] is False
