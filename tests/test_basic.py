"""最基本的流程：记 → 结束会话 → 整理 → 查。"""
from helpers import Clock, Scripted, make_mem, say


def test_record_end_run_recall(tmp_path):
    prov = Scripted("thread")
    mem = make_mem(tmp_path, provider=prov)
    say(mem, "S1", 6, prefix="橘子")
    mem.end_session("h", "S1")
    assert [s["status"] for s in mem.store.list_steps("w")] == ["sealed"]
    r = mem.run()
    assert r["status"] == "done" and r["done"] == 1
    assert len(prov.calls) == 1
    out = mem.recall("编号3的事情")
    assert out["delivered"] and "编号3" in out["text"]
    st = mem.status()
    assert st["done_steps"] == 1 and st["backlog"]["pending_steps"] == 0


def test_attachments_after_run(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("thread"))
    ids = say(mem, "S1", 4)
    mem.end_session("h", "S1")
    mem.run()
    a = mem.attachments(ids[0])
    assert a["sentences"] and a["sentences"][0]["attachments"][0]["kind"] == "occasion"
    assert a["sentences"][0]["attachments"][0]["confidence"] == "确定"


def test_role_other_than_user_assistant_rejected(tmp_path):
    mem = make_mem(tmp_path)
    for role in ("system", "tool", ""):
        try:
            mem.record("h", "S1", "x1", role, "hello")
        except ValueError:
            continue
        raise AssertionError("角色 %r 应该被拒收" % role)
