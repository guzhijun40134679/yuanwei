"""38 补（11-19 桌面版带图消息没存）：形状来自对 Hermes v0.23.2 真函数的复现（见交回报告第 1 步）。

真机上带图的一轮，宿主交给记忆的是两种形状（都不是 0.1.0a5 测的「同一份列表两头用」）：
* 附件路由（桌面版附件、微信等）：开始和同步拿到的都是用户打的字（persist_user_message 是字符串）；
  messages 里当前那一行是列表：一个文字块「用户打的字＋\\n\\n＋每张图一行提示」＋N 个图片块。
* API 会话：开始和同步拿到的都是「用户打的字＋\\n\\n＋提示行」；messages 里当前那一行的文字块也是这一串。
纯自造数据，不连网。宿主构造函数从获准的 Hermes 源码里按 AST 取出来真调。
"""
import ast
import json
import os
import sys
import types
from pathlib import Path

import pytest

from hermes_image_contract import DOCTOR_SOURCES, SOURCES, TURN_CONTEXT_PERSIST_LINE
from test_fix38_images import HERMES_SRC, content, host_functions, native_content
from test_hermes_adapter import mk, stored, turn
from yuanwei import hermes_compat as HC
from yuanwei import hermes_setup as HS

DEFAULT_ASK = "What do you see in this image?"


def api_content(monkeypatch, text, count, tmp_path=None):
    """Hermes 真的 restore_api_images（从源码按 AST 取出）：一个 API 会话的消息变成「文字＋提示」。committed 路径用自造的。"""
    path = HERMES_SRC / "gateway" / "session_api_media.py"
    if not path.is_file():
        pytest.skip("没核：没有获准的 Hermes API 图片源码")
    tree = ast.parse(path.read_text())
    wanted = {"_data_url_bytes", "_image_urls", "restore_api_images", "_MIME_EXT"}
    nodes = [n for n in tree.body if (n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) else "") in wanted]
    ns = {"base64": __import__("base64")}
    exec(compile("from __future__ import annotations\n" + ast.unparse(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), ns)
    fake = types.ModuleType("gateway.session_ingress_media")
    fake.restore_native_media = lambda media: ["/synthetic/native-inputs/api_%032d.png" % i for i in range(count)]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_ingress_media", fake)
    url = "data:image/png;base64," + __import__("base64").b64encode(b"synthetic").decode()
    request = ([{"type": "text", "text": text}] if text else []) + [{"type": "image_url", "image_url": {"url": url}} for _ in range(count)]
    return ns["restore_api_images"](request, [])


def flatten(row):
    return "\n".join(p["text"] for p in row if p.get("type") == "text")


def run(a, mem, sync_text, row_content, answer="图片回复", start_text=None):
    turn(a, 1, sync_text if start_text is None else start_text)
    a.sync_turn(sync_text, answer, session_id="hs1", messages=[{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好"},
                                                                 {"role": "user", "content": row_content}])
    return stored(mem)


# ------------------------------------------------------------ 附件路由：记忆拿到的是用户打的字，行里是「字＋提示」
@pytest.mark.parametrize("text,count", [("这是测试文字", 1), ("两张图的字", 2), ("What do you see in this image?", 1),
                                        ("我写的伪尾注\n\n[Image attached at: /literal/user-text.png]", 1)])
def test_attachment_route_typed_text_memory_and_row_with_hints(tmp_path, text, count):
    row = native_content(tmp_path, text, count)                      # Hermes 真的 build_native_content_parts
    assert flatten(row) != text                                      # 行里多了提示，记忆那头没有：真机上就是这样对不上的
    mem, a = mk(tmp_path / "mem")
    rows = run(a, mem, text, row)
    assert rows == [("hs1", "user", text + "\n（带 %d 张图）" % count), ("hs1", "assistant", "图片回复")]
    assert "synthetic-image" not in str(rows)
    assert a.recorded_turns == 1
    a.shutdown()


def test_attachment_route_keeps_typed_text_exactly_including_edge_whitespace(tmp_path):
    typed = "  原字\n保留  "
    row = native_content(tmp_path, typed, 1)                         # 宿主构造函数会把字两头的空白去掉；记忆那头没去
    mem, a = mk(tmp_path / "mem")
    assert run(a, mem, typed, row)[0][2] == typed + "\n（带 1 张图）"
    a.shutdown()


# ------------------------------------------------------------ API 会话：两头都是「字＋提示」
@pytest.mark.parametrize("text,count", [("桌面版带图的字", 1), ("两张图", 2)])
def test_api_route_both_ends_carry_hints_which_are_not_stored(tmp_path, monkeypatch, text, count):
    row = api_content(monkeypatch, text, count)
    sync_text = flatten(row)
    assert sync_text.startswith(text + "\n\n[Image attached at: ") and sync_text.count("[Image attached at: ") == count
    mem, a = mk(tmp_path / "mem")
    rows = run(a, mem, sync_text, row)
    assert rows == [("hs1", "user", text + "\n（带 %d 张图）" % count), ("hs1", "assistant", "图片回复")]
    assert "/synthetic/" not in str(rows)
    a.shutdown()


def test_api_route_image_only_keeps_the_indistinguishable_default_ask(tmp_path, monkeypatch):
    row = api_content(monkeypatch, "", 1)                            # 没有文字块：宿主插一句默认问句
    mem, a = mk(tmp_path / "mem")
    rows = run(a, mem, flatten(row), row)
    # 记忆和行里都没有「是用户写的还是宿主补的」的区别（和附件路由纯图同一个限制，见 0.1.0a5 文档）：保留这句，路径不存。
    assert rows[0] == ("hs1", "user", DEFAULT_ASK + "\n（带 1 张图）")
    assert "/synthetic/" not in str(rows)
    a.shutdown()


# ------------------------------------------------------------ 不放宽：对不上的照旧不存，记原因
@pytest.mark.parametrize("name,row_text,image_count", [
    ("提示行比图片块多一行", "字\n\n[Image attached at: /a.png]\n[Image attached at: /b.png]", 1),
    ("提示行比图片块少一行", "字\n\n[Image attached at: /a.png]", 2),
    ("提示行不完整", "字\n\n[Image attached at: /a.png", 1),
    ("提示行后面还有字", "字\n\n[Image attached at: /a.png]\n后面还有", 1),
    ("提示前只隔一个换行", "字\n[Image attached at: /a.png]", 1),
])
def test_hint_shapes_that_do_not_match_exactly_are_not_stored(tmp_path, name, row_text, image_count):
    row = content(row_text, image_count)
    mem, a = mk(tmp_path / "mem")
    rows = run(a, mem, "字", row)
    assert rows == [], name
    assert a.status()["skipped"].get("current_user_row_not_found") == 1
    a.shutdown()


def test_extra_text_part_after_the_hints_is_not_guessed(tmp_path):
    row = native_content(tmp_path, "字", 1) + [{"type": "text", "text": "后来多出来的一块"}]
    mem, a = mk(tmp_path / "mem")
    assert run(a, mem, "字", row) == []
    assert a.status()["skipped"].get("current_user_row_not_found") == 1
    a.shutdown()


def test_older_image_row_is_never_borrowed_for_a_plain_latest_row(tmp_path):
    old = native_content(tmp_path, "旧图的字", 1)
    mem, a = mk(tmp_path / "mem")
    turn(a, 1, "旧图的字")
    a.sync_turn("旧图的字", "不能配到旧行", session_id="hs1", messages=[
        {"role": "user", "content": old}, {"role": "assistant", "content": "旧回答"}, {"role": "user", "content": "这一轮另一句话"}])
    assert stored(mem) == []
    assert a.status()["skipped"].get("current_user_row_not_found") == 1
    a.shutdown()


def test_plain_text_equal_to_an_older_image_caption_is_stored_as_plain(tmp_path):
    old = native_content(tmp_path, "同一句字", 1)
    mem, a = mk(tmp_path / "mem")
    turn(a, 1, "同一句字")
    a.sync_turn("同一句字", "回答", session_id="hs1", messages=[
        {"role": "user", "content": old}, {"role": "assistant", "content": "旧回答"}, {"role": "user", "content": "同一句字"}])
    assert stored(mem)[0][2] == "同一句字"
    a.shutdown()


def test_attachment_route_row_with_machinery_flag_is_still_refused(tmp_path):
    row = native_content(tmp_path, "字", 1)
    mem, a = mk(tmp_path / "mem")
    turn(a, 1, "字")
    a.sync_turn("字", "不能存", session_id="hs1", messages=[{"role": "user", "content": row, "_todo_snapshot_synthetic": True}])
    assert stored(mem) == []
    assert a.status()["skipped"].get("machinery_row") == 1
    a.shutdown()


def test_attachment_route_two_pending_turns_with_same_text_is_not_guessed(tmp_path):
    row = native_content(tmp_path, "相同文字", 1)
    mem, a = mk(tmp_path / "mem")
    turn(a, 1, "相同文字")
    turn(a, 2, "相同文字")
    a.sync_turn("相同文字", "不能存", session_id="hs1", messages=[{"role": "user", "content": row}])
    assert stored(mem) == []
    assert a.status()["skipped"].get("ambiguous_pending_turn") == 1
    a.shutdown()


# ------------------------------------------------------------ 跳过原因按库累计（每一轮 Hermes 新起一个实例）
def test_skip_reasons_survive_a_new_adapter_instance_each_turn(tmp_path):
    from yuanwei import skipreasons as SR
    mem1, a1 = mk(tmp_path / "mem")
    home = str(tmp_path / "mem" / "home")
    a1.sync_turn("没有登记过的一轮的原话", "答", session_id="hs1", messages=[{"role": "user", "content": "没有登记过的一轮的原话"}])
    assert a1.status()["skipped"] == {"no_pending_turn": 1}
    a1.shutdown()
    mem2, a2 = mk(tmp_path / "mem")                                  # 下一轮：新实例，实例里的计数从零开始
    assert a2.status()["skipped"] == {}
    turn(a2, 1, "字")
    a2.sync_turn("字", "答", session_id="hs1", messages=[])
    assert a2.status()["skipped"] == {"current_user_row_not_found": 1}
    a2.shutdown()
    got = SR.read(home)
    assert got["problem"] is None
    assert got["totals"] == {"no_pending_turn": 1, "current_user_row_not_found": 1}
    assert [r["why"] for r in got["recent"]] == ["no_pending_turn", "current_user_row_not_found"]
    assert all(r["t"].endswith("Z") for r in got["recent"])
    assert "没有登记过的一轮的原话" not in json.dumps(got, ensure_ascii=False)
    for p in Path(home, "yuanwei").iterdir():
        if p.name.startswith("skip"):
            assert "原话" not in p.read_text(encoding="utf-8")


def test_skip_reason_file_is_bounded_and_rejects_odd_reasons(tmp_path):
    from yuanwei import skipreasons as SR
    home = tmp_path / "h"
    home.mkdir()
    for i in range(SR.KEEP + 7):
        assert SR.record(str(home), "r%d" % (i % 3))
    assert SR.record(str(home), "这里是用户原话，不是原因") is True
    got = SR.read(str(home))
    assert len(got["recent"]) == SR.KEEP
    assert sum(got["totals"].values()) == SR.KEEP + 8
    assert got["totals"]["other"] == 1 and "这里是用户原话，不是原因" not in json.dumps(got, ensure_ascii=False)


def test_skip_reason_file_unreadable_is_reported_not_zero(tmp_path):
    from yuanwei import skipreasons as SR
    home = tmp_path / "h"
    (home / "yuanwei").mkdir(parents=True)
    (home / "yuanwei" / SR.FILE).write_text("{坏了", encoding="utf-8")
    got = SR.read(str(home))
    assert got["problem"] == "invalid_data" and got["totals"] == {}
    assert SR.record(str(home), "x") is False                        # 坏文件不覆盖
    assert (home / "yuanwei" / SR.FILE).read_text(encoding="utf-8") == "{坏了"


def test_status_hermes_shows_the_accumulated_skip_reasons(tmp_path):
    mem, a = mk(tmp_path / "mem")
    turn(a, 1, "字")
    a.sync_turn("字", "答", session_id="hs1", messages=[])
    a.shutdown()
    st = HS.install_status(str(tmp_path / "mem" / "home"))
    assert st["skip_reasons"]["totals"] == {"current_user_row_not_found": 1}
    assert st["skip_reasons"]["recent"][0]["why"] == "current_user_row_not_found"


# ------------------------------------------------------------ 体检：turn_context 里取 persist_user_message 的那一行
def doctor_check(sources, dep="memory_sync_text"):
    return {c["id"]: c for c in HC.doctor_from_reader(sources.get)["checks"]}[dep]


def test_doctor_ok_with_the_persist_line_present():
    assert doctor_check(DOCTOR_SOURCES)["status"] == "ok"


@pytest.mark.parametrize("old,new", [
    ("persist_user_message if persist_user_message is not None else user_message", "user_message"),
    ("persist_user_message if persist_user_message is not None else user_message", "persist_user_message or user_message"),
    ("persist_user_message if persist_user_message is not None else user_message", "user_message if user_message is not None else persist_user_message"),
])
def test_doctor_reports_changed_when_the_persist_line_changes(old, new):
    src = dict(DOCTOR_SOURCES)
    assert old in src["agent/turn_context.py"]
    src["agent/turn_context.py"] = src["agent/turn_context.py"].replace(old, new)
    c = doctor_check(src)
    assert c["status"] == "changed" and "turn_context" in c["detail"]


def test_doctor_missing_persist_line_is_unchecked_not_ok():
    src = dict(DOCTOR_SOURCES)
    src["agent/turn_context.py"] = SOURCES["agent/turn_context.py"]            # 有 _memory_query_text，但没有那一行
    c = doctor_check(src)
    assert c["status"] == "missing" and "没核" in c["detail"]


def test_doctor_reports_changed_default_ask_and_hint_format_of_the_api_restore():
    for old, new in (("What do you see in this image?", "Describe the image."), ("[Image attached at: {next(paths)}]", "[Picture at: {next(paths)}]")):
        src = dict(DOCTOR_SOURCES)
        assert old in src["gateway/session_api_media.py"]
        src["gateway/session_api_media.py"] = src["gateway/session_api_media.py"].replace(old, new)
        assert doctor_check(src)["status"] == "changed", old


def test_installed_hermes_has_exactly_the_frozen_persist_line():
    path = HERMES_SRC / "agent" / "turn_context.py"
    if not path.is_file():
        pytest.skip("没核：本机没有获准的 Hermes 安装源码")
    lines = [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "original_user_message" for t in n.targets)]
    assert [ast.unparse(n) for n in lines] == [ast.unparse(ast.parse(TURN_CONTEXT_PERSIST_LINE).body[0])]
