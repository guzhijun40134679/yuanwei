"""污染闭包影响面实测（自造的多对话库，没有真数据）：10 段对话、3 个话题，整理时遇到同话题就挂到已有的回、不同话题新建主线；删掉其中一段，数清别的整理文字被清成了多少占位。
这条测试同时钉住几条性质：别的对话的原话一句不动、按原话查找不受影响、占位文字不带被删内容。数字在交回报告里单列一节。"""
import json
import re

from helpers import Clock, Scripted, make_mem, say, sids_of
from yuanwei.deletion import DeletionService, legacy_conv_id

TOPICS = {"A": "烤鱼", "B": "跑步", "C": "报税"}
ORDER = ["A", "B", "A", "C", "A", "B", "C", "A", "B", "C"]          # 10 段对话


def provider(req, n):
    text = req["text"]
    sids = sids_of(text)
    topic = next((t for t, w in TOPICS.items() if w in text.rsplit("这次对话：", 1)[-1]), "A")
    word = TOPICS[topic]
    cards = text.rsplit("这次对话：", 1)[0]
    m = re.search(r"【主线 (th-[^】]+)】%s" % word, cards)
    occ = re.search(r"【回 (oc-[^】]+)】", cards[m.end():]) if m else None
    if occ:
        return json.dumps({"新建": {"主线": [], "回": []}, "去处": [{"句": sids, "挂": [{"回": occ.group(1), "把握": "确定"}]}], "缺口": [], "改摘要": []}, ensure_ascii=False)
    return json.dumps({"新建": {"主线": [{"代号": "T1", "名称": word, "说明": "关于%s的主线" % word}], "回": [{"代号": "N1", "主线": "T1", "标题": "%s这一回" % word, "摘要": "说了%s" % word}]},
                       "去处": [{"句": sids, "挂": [{"回": "N1", "把握": "确定"}]}], "缺口": [], "改摘要": []}, ensure_ascii=False)


def build(tmp_path):
    clock = Clock()
    mem = make_mem(tmp_path, provider=Scripted(provider), clock=clock)
    for i, t in enumerate(ORDER):
        say(mem, "D%02d" % i, 6, prefix="第%d段说%s的事，" % (i, TOPICS[t]), date="2027-01-%02d" % (i + 1))
        mem.end_session("h", "D%02d" % i)
        clock.advance(hours=3)
        mem.run()
    return mem


def org_text_cells(mem):
    st = mem.store.read_state("w")
    n = 0
    for t, f in (("thread_names", "name"), ("thread_descs", "text"), ("occ_titles", "title"), ("occ_summaries", "text"), ("attachments", "reason")):
        n += len([r for r in st["tables"][t] if (r.get("reason") if t == "attachments" else True)])
    return n


def placeholders(mem):
    st = mem.store.read_state("w")
    marks = ("〔标题：来源对话已删除〕", "这条摘要的来源被删了", "〔主线名称：来源对话已删除〕", "〔主线说明：来源对话已删除〕", "〔理由已清除：来源对话已删除〕")
    return sum(1 for t in st["tables"].values() for r in t for v in r.values() if isinstance(v, str) and v in marks)


def measure(tmp_path, session):
    mem = build(tmp_path)
    cells0 = org_text_cells(mem)
    other_msgs = [m for m in mem.store.list_messages("w") if m["session_id"] != session]
    svc = DeletionService(mem.db_path, config_dir=mem.config_dir, clock=mem.clock)
    cid = legacy_conv_id("h", session, "")
    prev = svc.preview(cid)
    r = svc.delete_conversation(cid)
    after = [m for m in mem.store.list_messages("w") if m["session_id"] != session]
    return {"删的对话": session, "话题": TOPICS[ORDER[int(session[1:])]], "删掉的原话": r.counts["messages"], "整理文字格子（删前，主线名／说明／回标题／摘要／挂接理由）": cells0,
            "删后变成占位的格子": placeholders(mem), "别的步被清了自由文字": r.counts["impact_steps_tainted"], "别的回的标题和摘要被换墓碑": r.counts["impact_occasions_tombstoned"],
            "主线名和说明被清": r.counts["impact_threads_named_cleared"], "挂接理由被清": r.counts["impact_attachments_reason_cleared"], "判断记录清成作废": r.counts["impact_judgments_scrubbed"],
            "预览和实际一致": prev.impact == {k[7:]: v for k, v in r.counts.items() if k.startswith("impact_")}, "别的对话原话是否一句没动": [m["content"] for m in other_msgs] == [m["content"] for m in after]}, mem, r


def test_impact_of_deleting_one_conversation_is_measured_and_other_original_text_is_untouched(tmp_path):
    rows = []
    for i, s in enumerate(("D00", "D03", "D09")):
        (tmp_path / str(i)).mkdir()
        row, mem, r = measure(tmp_path / str(i), s)
        rows.append(row)
        assert row["别的对话原话是否一句没动"] and row["预览和实际一致"] and r.status == "complete"
        assert mem.recall("跑步")["delivered"] or mem.recall("烤鱼")["delivered"]                          # 按原话查找不受影响
        assert "来源对话已删除" in "".join(json.dumps(x, ensure_ascii=False) for x in mem.store.read_state("w")["tables"]["thread_names"]) or row["主线名和说明被清"] == 0
    print("\nIMPACT", json.dumps(rows, ensure_ascii=False, indent=1))
