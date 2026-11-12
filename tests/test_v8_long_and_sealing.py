"""V8 长消息与封步：超长条存全文、索引全文、尾部能命中、记「超长未整理」；长短混在一步和整步全是超长两种；确实超过 98 段的长条编号和跨度仍唯一完整；
轮次边界封步；空闲 30 分钟按下一次事件判，新消息先判旧步；中止或关闭的轮次能封，不补回复；跨日日期对。"""
import pytest

from helpers import Clock, Scripted, make_mem, say
from yuanwei import anchors as AN
from yuanwei import encoding as EN
from yuanwei.errors import NotVisible

LONG_TAIL = "尾巴里藏着唯一的暗号：琥珀猫头鹰"


def long_text(n_chars, tail=LONG_TAIL):
    unit = "这是一段用来凑长度的闲聊句子。"
    return (unit * (n_chars // len(unit) + 1))[:n_chars - len(tail)] + tail


def test_long_message_stored_whole_indexed_whole_and_marked(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    text = long_text(4000)
    r = mem.record("h", "S1", "L1", "assistant", text)
    assert r["is_long"] is True
    row = mem.store.message_row("w", r["msg_id"])
    assert row["content"] == text and len(row["content"]) == 4000            # 全文照存
    anchors = mem.store.anchors_of("w", r["msg_id"])
    assert "".join(text[a["start"]:a["end"]] for a in anchors) and any("琥珀猫头鹰" in a["text"] for a in anchors)        # 索引覆盖全文，尾部也在
    assert mem.status()["long_unorganized"] == 1                              # 状态里报「超长未整理」条数


def test_mixed_step_long_message_not_visible_until_step_ends(tmp_path):
    prov = Scripted("thread")
    mem = make_mem(tmp_path, provider=prov)
    u = mem.record("h", "S1", "a1", "user", "帮我看看这份日志，里面有个暗号。")["msg_id"]
    big = mem.record("h", "S1", "a2", "assistant", long_text(4000))["msg_id"]
    mem.record("h", "S1", "a3", "user", "谢谢，我看到了。")
    mem.record("h", "S1", "a4", "assistant", "不客气。")
    mem.end_session("h", "S1")
    # 步已封但还没整理：全文照样能查到、按编号也能取（V9：入库提交成功就可读）；旧格式截点上按步结束才可见
    assert big in [d["msg_id"] for d in mem.recall("琥珀猫头鹰")["delivered"]]
    assert mem.get_messages([big])["messages"][0]["msg_id"] == big
    legacy_before = dict(mem.store.cut("w"), index_version=mem.index_version)
    with pytest.raises(NotVisible):
        mem.get_messages([big], cut=legacy_before)
    snap_before = mem.cut()
    mem.run()
    # 整理请求里没有长条的全文，只有短条
    req = prov.calls[0]["text"]
    assert "琥珀猫头鹰" not in req and "帮我看看这份日志" in req
    # 步结束后，长条作为没挂接的原话进入同一可见范围；全文能查到
    out = mem.recall("琥珀猫头鹰暗号")
    assert big in [d["msg_id"] for d in out["delivered"]]
    assert LONG_TAIL in mem._reader.mp.decode(out["text"]) or "琥珀猫头鹰" in out["text"]
    a = mem.attachments(big)
    assert a["sentences"] == [] and "超长" in a["note"]
    # 历史截点不提前看到：旧格式截点（步结束之前）上它不可见；新格式截点（入库之后取的）上它一直可见
    old = mem.recall("琥珀猫头鹰暗号", cut=legacy_before)
    assert big not in [d["msg_id"] for d in old["delivered"]]
    assert big in [d["msg_id"] for d in mem.recall("琥珀猫头鹰暗号", cut=snap_before)["delivered"]]


def test_all_long_step_is_skipped_without_model_call_and_not_counted_as_abandon(tmp_path):
    prov = Scripted("skip")
    mem = make_mem(tmp_path, provider=prov)
    mem.record("h", "S1", "b1", "user", long_text(1600, "第一条超长暗号：紫水晶"))
    mem.record("h", "S1", "b2", "assistant", long_text(1800, "第二条超长暗号：青金石"))
    mem.end_session("h", "S1")
    assert mem.recall("青金石")["delivered"]                                   # 全部超长条也是入库就能查（不等跳过）
    r = mem.run()
    assert r["skipped"] == 1 and prov.calls == []                              # 不调模型
    st = mem.store.get_step("w", "step-0001")
    assert st["status"] == "skipped"
    state = mem.store.read_state("w")
    assert [x for x in state["tables"]["receipts"] if x.get("op") == "skip_step"]       # 一张「跳过」收据
    ws = mem.store.workspace("w")
    assert ws["consec_abandons"] == 0 and mem.pause_state() is None             # 不计放弃、不计入连续放弃
    assert mem.status()["abandoned_steps"] == 0
    assert mem.recall("青金石")["delivered"]                                     # 步结束后全文能查到
    # 下一步能接着做（k 的次序没被跳过的步卡住）
    say(mem, "S2", 2)
    mem.end_session("h", "S2")
    assert mem.run()["done"] == 1 and len(prov.calls) == 1


def test_get_messages_pages_long_message_without_loss(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"), get_messages_page_tokens=2000)
    text = long_text(6000)
    mid = mem.record("h", "S1", "p1", "assistant", text)["msg_id"]
    mem.record("h", "S1", "p0", "user", "短问题")
    mem.end_session("h", "S1")
    mem.run()
    got, cursor, pages = "", None, 0
    page = mem.get_messages([mid])
    while True:
        pages += 1
        for m in page["messages"]:
            if m["msg_id"] == mid:
                got += m["text"]
        if not page["truncated"]:
            break
        assert page["cursor"]
        again = mem.get_messages(cursor=page["cursor"])               # 同一个游标重取，内容相同
        assert again == mem.get_messages(cursor=page["cursor"])
        page = again
    assert pages > 1 and got == EN.load_mapping().encode(text)         # 分段取回，拼起来等于全文（编码后）
    first = mem.get_messages([mid])
    assert first["truncated"] and first["messages"][-1]["truncated"] is True and first["cursor"]


def test_over_98_segments_unique_and_complete(tmp_path):
    text = "".join("第%d句。" % i for i in range(1, 151))       # 150 句，不到 1500 字（正常整理）
    assert len(text) < 1500
    mem = make_mem(tmp_path, provider=Scripted("skip"), index_version=2)
    mid = mem.record("h", "S1", "c1", "user", text)["msg_id"]
    anchors = mem.store.anchors_of("w", mid)
    assert len(anchors) == 150
    assert len({a["anchor_id"] for a in anchors}) == 150 and len({a["ord"] for a in anchors}) == 150
    spans = [(a["start"], a["end"]) for a in anchors]
    assert all(text[s:e] == a["text"] for (s, e), a in zip(spans, anchors))
    assert spans == sorted(spans) and all(spans[i][1] <= spans[i + 1][0] for i in range(149))       # 跨度不重叠、按位置排
    assert "".join(text[s:e] for s, e in spans) == text                                # 句子之间没有漏字（这段文字没有空白）
    # 版本 1 不支持：拒收，不悄悄截断
    mem1 = make_mem(tmp_path, name="v1.sqlite", index_version=1)
    with pytest.raises(AN.AnchorError):
        mem1.record("h", "S1", "c1", "user", text)
    assert mem1.store.count_messages("w") == 0                                          # 事务回滚：没有半条数据
    # 排序关系：不超过 98 句的消息，两个版本的 order 次序一致
    short = "一句。二句。三句。"
    o1 = [a["ord"] for a in AN.anchors_for("m", 5, short, 1)]
    o2 = [a["ord"] for a in AN.anchors_for("m", 5, short, 2)]
    assert o1 == sorted(o1) and o2 == sorted(o2)


def test_seal_at_turn_boundary_and_oversized_turn_alone(tmp_path):
    mem = make_mem(tmp_path, max_step_messages=4, provider=Scripted("skip"))
    for i in range(1, 7):                                       # 3 轮，每轮 2 条
        mem.record("h", "S1", "t%d" % i, "user" if i % 2 else "assistant", "第%d条" % i)
    steps = mem.store.list_steps("w")
    assert len(steps) == 1 and len(steps[0]["msg_ids"]) == 4        # 攒到 4 条，在第 2 轮之后（轮次边界）封；第 3 轮还在攒
    assert mem.store.count_messages("w", "step_id IS NULL") == 2
    # 一轮自己就超阈值（按字数）：单独成一步，前面攒着的先封
    mem2 = make_mem(tmp_path, name="b.sqlite", max_step_chars=100, provider=Scripted("skip"))
    mem2.record("h", "S1", "u1", "user", "先问一个")
    mem2.record("h", "S1", "a1", "assistant", "答一个")
    mem2.record("h", "S1", "u2", "user", "再问一个大的" + "很" * 100)
    mem2.record("h", "S1", "a2", "assistant", "大的回复")
    mem2.record("h", "S1", "u3", "user", "最后一问")                    # 下一轮的用户话来了，大轮次早已完整
    st = mem2.store.list_steps("w")
    assert [len(s["msg_ids"]) for s in st] == [2, 2]                    # [先问先答] 先封，[大的一轮] 单独成一步
    assert mem2.store.count_messages("w", "step_id IS NULL") == 1


def test_idle_is_judged_at_next_event_and_new_message_judged_old_step_first(tmp_path):
    clock = Clock()
    mem = make_mem(tmp_path, clock=clock, provider=Scripted("skip"))
    mem.record("h", "S1", "i1", "user", "早上好，问个事")
    mem.record("h", "S1", "i2", "assistant", "请讲")
    clock.advance(minutes=29)
    mem.maintain()
    assert mem.store.list_steps("w") == []                           # 没到 30 分钟
    clock.advance(minutes=2)                                         # 现在空闲 31 分钟，但没有任何事件：不会自己封
    assert mem.store.list_steps("w") == []
    mem.record("h", "S1", "i3", "user", "我回来了")                   # 新消息到来：先按旧的活动时间判旧步（31 分钟前）→ 封旧步，再记新的
    steps = mem.store.list_steps("w")
    assert len(steps) == 1 and len(steps[0]["msg_ids"]) == 2
    assert mem.store.count_messages("w", "step_id IS NULL") == 1       # 新消息不在旧步里
    # 手动 run／maintain 也判
    clock.advance(minutes=31)
    assert mem.maintain() == []
    # i3 没有回复：这一轮不完整，空闲到期也不封（需要中止或关闭事件）
    assert mem.store.count_messages("w", "step_id IS NULL") == 1


def test_aborted_and_closed_turn_sealed_without_inventing_a_reply(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("h", "S1", "q1", "user", "第一问")
    mem.record("h", "S1", "q2", "assistant", "第一答")
    mem.record("h", "S1", "q3", "user", "第二问，还没答就中止了")
    assert mem.store.list_steps("w") == []
    mem.abort_turn("h", "S1")
    steps = mem.store.list_steps("w")
    assert len(steps) == 1 and len(steps[0]["msg_ids"]) == 3
    rows = [mem.store.message_row("w", m) for m in steps[0]["msg_ids"]]
    assert [r["role"] for r in rows] == ["user", "assistant", "user"]       # 没有补一条助手回复
    assert mem.store.count_messages("w") == 3
    # 会话关闭：最后一轮没有回复，也封
    mem.record("h", "S2", "r1", "user", "单独一句")
    mem.end_session("h", "S2")
    assert len(mem.store.list_steps("w")) == 2 and len(mem.store.list_steps("w")[1]["msg_ids"]) == 1


def test_steps_do_not_cross_sessions_hosts_or_branches(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("h1", "S1", "x1", "user", "宿主一会话一")
    mem.record("h2", "S1", "x1", "user", "宿主二会话一")        # 同一个会话名、同一个消息编号，不同宿主：是两条
    mem.record("h1", "S2", "x1", "user", "宿主一会话二")
    mem.record("h1", "S1", "y1", "user", "宿主一会话一的分支", branch="b1")
    mem.record("h1", "S1", "x2", "assistant", "宿主一会话一的回复")
    for h, s, b in (("h1", "S1", ""), ("h2", "S1", ""), ("h1", "S2", ""), ("h1", "S1", "b1")):
        mem.end_session(h, s, b)
    steps = mem.store.list_steps("w")
    assert len(steps) == 4
    for s in steps:
        rows = [mem.store.message_row("w", m) for m in s["msg_ids"]]
        assert {(r["host"], r["session_id"], r["branch"]) for r in rows} == {(s["host"], s["session_id"], s["branch"])}
    assert mem.store.count_messages("w") == 5


def test_cross_day_session_dates_are_kept_per_message(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    mem.record("h", "S1", "d1", "user", "周一晚上说的话", time="2027-05-03")
    mem.record("h", "S1", "d2", "assistant", "周一晚上的回复", time="2027-05-03")
    mem.record("h", "S1", "d3", "user", "周二早上接着说", time="2027-05-04")
    mem.record("h", "S1", "d4", "assistant", "周二早上的回复", time="2027-05-04")
    mem.end_session("h", "S1")
    mem.run()
    rows = [mem.store.message_row("w", m) for m in mem.store.list_steps("w")[0]["msg_ids"]]
    assert [r["msg_time"] for r in rows] == ["2027-05-03", "2027-05-03", "2027-05-04", "2027-05-04"]    # 每条保留自己的时间
    out = mem.recall("周一晚上 周二早上")
    lines = [l for l in out["text"].split("\n") if l.startswith("会话 ")]
    assert lines == ["会话 S1｜2027-05-03", "会话 S1｜2027-05-04"]                  # 日期变了另起一行
