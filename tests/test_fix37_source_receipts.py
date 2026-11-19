"""37 来源复核：跨区续读回执、便条取材后撤权/删除；全部临时合成库。"""
import json

import pytest

from test_fix37_pending import PendingWorld, saved
from test_hermes_workspace import call, turn
from yuanwei.deletion import DeletionService


def test_foreign_cursor_only_read_is_counted_in_current_turn_receipt(tmp_path):
    w = PendingWorld(tmp_path)
    source = w.adapter("/work/a", session="A")
    rows = saved(source, "A", "紫檀盒" + "合成文字" * 100)
    page = source.mem._reader.get_messages([r["msg_id"] for r in rows], page_tokens=30)
    assert page["cursor"]
    owner = w.adapter("/elsewhere", session="B")
    turn(owner, 1, "普通提问")
    result = call(owner, "get_messages", {"cursor": "W1~" + page["cursor"]})
    assert result["status"] == "ok" and "合成文字" in result["text"]
    logs = owner.acc.log_rows("hermes", "B", owner._cur["B"])
    assert any(r["kind"] == "returned" for r in logs)
    # owner 只镜像活动说明，不复制外区的原话、编号或内容指纹。
    detail = json.dumps([r["detail"] for r in logs], ensure_ascii=False)
    assert "合成文字" not in detail
    assert not any(r["msg_id"] in detail for r in rows)
    assert not any("fingerprint" in r["detail"] for r in logs)
    call(owner, "window_off")
    blocked = call(owner, "recall", {"query": "紫檀盒"})
    assert blocked["status"] == "refused"
    assert "本轮此前已查询" in blocked["text"] and "本轮未新增读取长期记忆" not in blocked["text"]


@pytest.mark.parametrize("mutation", ["permission", "deletion"])
@pytest.mark.parametrize("point", ["after_fetch", "assembled", "after_finish"])
def test_foreign_note_is_not_delivered_after_source_changes(tmp_path, monkeypatch, mutation, point):
    w = PendingWorld(tmp_path)
    source = w.adapter("/work/a", session="A")
    mid = saved(source, "A")[0]["msg_id"]
    w.cfg["mode"] = "note"
    owner = w.adapter("/elsewhere", session="B")
    turn(owner, 1, "紫檀盒")
    changed = []

    def mutate():
        if changed:
            return
        if mutation == "permission":
            source.mem.set_read_permission(False)
        else:
            service = DeletionService(source.mem.db_path, config_dir=source.mem.config_dir, clock=source.mem.clock)
            try:
                assert service.delete_conversation(source._win_ref.window_id).status == "complete"
            finally:
                service.close()
        changed.append(True)

    if point == "after_fetch":
        original = owner.runner._with_deadline

        def after_fetch(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("mem") is source.mem:
                mutate()
            return result
        monkeypatch.setattr(owner.runner, "_with_deadline", after_fetch)
    elif point == "assembled":
        original = owner.acc.assembled

        def after_assembled(*args, **kwargs):
            result = original(*args, **kwargs)
            mutate()
            return result
        monkeypatch.setattr(owner.acc, "assembled", after_assembled)
    else:
        original = owner.acc.finish_read

        def after_finish(*args, **kwargs):
            result = original(*args, **kwargs)
            mutate()
            return result
        monkeypatch.setattr(owner.acc, "finish_read", after_finish)
    result = owner.prefetch("紫檀盒", session_id="B")
    assert changed, "必须实际走到来源取材后的竞态窗口"
    assert mid not in result and "紫檀盒放在蓝色抽屉里" not in result
    if point == "after_finish":
        logs = owner.acc.log_rows("hermes", "B", owner._cur["B"])
        assert any(r["kind"] == "injection_failed" for r in logs)
