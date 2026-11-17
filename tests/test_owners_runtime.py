"""小修30：消息未登记时仅保留平台计数；全部使用临时家目录和假消息。"""
import json
from pathlib import Path

import pytest

from test_hermes_workspace import World, call, msgs, rows, turn
from yuanwei import owners as OW
from yuanwei.access import Access


def all_bytes(root):
    return b"\n".join(p.read_bytes() for p in Path(root).rglob("*") if p.is_file())


def table_counts(mem):
    names = [r[0] for r in mem.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: mem.store.conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


def test_unknown_owner_never_opens_memory_and_keeps_only_platform_count(tmp_path):
    w = World(tmp_path, seed=False)
    person, text = "private-person-9876123", "未登记这句不准落盘 9876655"
    a = w.adapter(None, platform="weixin", session="weixin:" + person, user_id=person, chat_type="dm")
    assert w.mems == {} and a.mem is None
    turn(a, 1, text, author_id=person)
    turn(a, 1, text, author_id=person)  # 同实例重复事件只计一次
    for tool in ("recall", "get_messages", "window_state", "window_off", "window_delete", "status"):
        assert call(a, tool, {"query": text})["status"] == "refused"
    assert a.prefetch(text) == ""
    a.sync_turn(text, "答复同样不保留", session_id="weixin:" + person, messages=msgs(text))
    a.on_session_switch("other:" + person, reset=True)
    a.on_session_end(msgs(text))
    assert w.mems == {} and OW.counts(str(w.home)) == {"weixin": 1}
    raw = all_bytes(w.home)
    for forbidden in (person, text, "答复同样不保留", Access.text_fp(text), "weixin:" + person, "other:" + person):
        assert forbidden.encode() not in raw
    assert json.loads((w.home / "yuanwei" / "owner-skips.json").read_text()) == {"weixin": 1}


def test_counter_survives_adapter_restart_and_registered_owner_then_records(tmp_path):
    w = World(tmp_path, seed=False)
    a = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    turn(a, 1, "跳过一", author_id="me")
    a.shutdown()
    b = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    turn(b, 2, "跳过二", author_id="me")
    assert OW.counts(str(w.home)) == {"telegram": 2}
    w.cfg["owners"] = {"telegram": ["me"]}
    c = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    turn(c, 1, "登记以后正常记", author_id="me")
    c.sync_turn("登记以后正常记", "答", session_id="s1", messages=msgs("登记以后正常记"))
    assert [text for role, text in rows(w.mem("life")) if role == "user"] == ["登记以后正常记"]
    assert OW.counts(str(w.home)) == {"telegram": 2}
    assert c.status()["owner_skips"] == {"telegram": 2}


def test_nonowner_after_owner_leaves_no_token_capture_audit_or_read_trace(tmp_path):
    w = World(tmp_path, owners={"telegram": ["owner"]}, seed=False)
    a = w.adapter(None, platform="telegram", user_id="owner", chat_type="dm")
    turn(a, 1, "本人消息", author_id="owner")
    a.sync_turn("本人消息", "答", session_id="s1", messages=msgs("本人消息"))
    before = table_counts(w.mem("life"))
    text, stranger = "其他人的绝密内容 0019", "stranger-privacy-1938"
    turn(a, 2, text, author_id=stranger)
    assert call(a, "recall", {"query": text})["status"] == "refused"
    assert call(a, "window_off")["status"] == "refused"
    a.sync_turn(text, "其他人的回复", session_id="s1", messages=msgs(text))
    assert table_counts(w.mem("life")) == before
    assert OW.counts(str(w.home)) == {"telegram": 1}
    raw = all_bytes(tmp_path)
    for forbidden in (text, stranger, Access.text_fp(text), "其他人的回复"):
        assert forbidden.encode() not in raw
    turn(a, 3, "本人继续", author_id="owner")
    assert call(a, "recall", {"query": "本人"})["status"] == "ok"


@pytest.mark.parametrize("chat_type", ["group", "forum", "channel"])
def test_group_platform_blocks_even_configured_owner_without_opening_memory(tmp_path, chat_type):
    w = World(tmp_path, owners={"weixin": ["owner"]}, seed=False)
    a = w.adapter(None, platform="weixin", user_id="owner", chat_type=chat_type)
    turn(a, 1, "群聊正文", author_id="owner")
    a.sync_turn("群聊正文", "答", session_id="s1", messages=msgs("群聊正文"))
    assert not a.active and a.inactive_reason == "group_room_not_supported"
    assert call(a, "recall", {"query": "群聊"})["status"] == "refused"
    assert w.mems == {} and OW.counts(str(w.home)) == {}


def test_counter_write_failure_never_admits_unknown_message(tmp_path, monkeypatch):
    w = World(tmp_path, seed=False)
    monkeypatch.setattr(OW, "increment", lambda *args: False)
    a = w.adapter(None, platform="telegram", user_id="stranger", chat_type="dm")
    turn(a, 1, "不准因为计数失败记下来", author_id="stranger")
    assert call(a, "recall")["status"] == "refused" and w.mems == {}
    assert a.status()["owner_counter_errors"] == 1


def test_remote_new_session_does_not_borrow_owner_permission_and_defers_binding(tmp_path):
    w = World(tmp_path, owners={"telegram": ["owner"]}, seed=False)
    a = w.adapter(None, platform="telegram", user_id="owner", chat_type="dm")
    turn(a, 1, "本人消息", author_id="owner")
    before = table_counts(w.mem("life"))
    a.on_session_switch("new-session", reset=True, reason="new_session")
    assert call(a, "recall", {"query": "内容"})["status"] == "refused"
    assert table_counts(w.mem("life")) == before
    turn(a, 2, "本人新一轮", author_id="owner")
    assert call(a, "recall", {"query": "内容"})["status"] == "ok"


def test_confirmed_owner_compression_continues_reading_without_new_input(tmp_path):
    w = World(tmp_path, owners={"telegram": ["owner"]})
    a = w.adapter(None, platform="telegram", user_id="owner", chat_type="dm")
    turn(a, 1, "帮我查咖喱", author_id="owner")
    assert call(a, "recall", {"query": "咖喱"})["status"] == "ok"
    a.on_session_switch("compressed-session", parent_session_id="s1", reason="compression")
    result = call(a, "recall", {"query": "咖喱"})
    assert result["status"] == "ok" and "咖喱" in result["text"]
    a.sync_turn("帮我查咖喱", "答", session_id="compressed-session", messages=msgs("帮我查咖喱"))
    assert [text for role, text in rows(w.mem("life")) if role == "user"] == ["帮我查咖喱"]


def test_confirmed_owner_closed_window_stays_closed_immediately_after_compression(tmp_path):
    w = World(tmp_path, owners={"telegram": ["owner"]}, seed=False)
    a = w.adapter(None, platform="telegram", user_id="owner", chat_type="dm")
    turn(a, 1, "本人先关闭读取", author_id="owner")
    call(a, "window_off")
    a.on_session_switch("compressed-session", parent_session_id="s1", reason="compression")
    assert a.win.view("hermes", "compressed-session").state == "off"
    assert call(a, "recall", {"query": "内容"})["reason"] == "window_closed"


def test_unknown_owner_compression_never_persists_session_or_grants_reading(tmp_path):
    w = World(tmp_path, seed=False)
    a = w.adapter(None, platform="telegram", session="initial-private-user-9753", user_id="unknown-9753", chat_type="dm")
    turn(a, 1, "未登记的原话9753", author_id="unknown-9753")
    a.on_session_switch("compressed-private-user-9753", parent_session_id="initial-private-user-9753", reason="compression")
    assert call(a, "recall", {"query": "内容"})["status"] == "refused"
    a.sync_turn("未登记的原话9753", "答", session_id="compressed-private-user-9753", messages=msgs("未登记的原话9753"))
    assert w.mems == {} and OW.counts(str(w.home)) == {"telegram": 1}
    assert b"9753" not in all_bytes(tmp_path)
