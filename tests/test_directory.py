"""主线目录（只给目录模式用）：当前工作区在某个已提交截点上的导航快照。每行：主线名｜最近关联原话的日期｜一条可见原话的编号；2,000 字符上限；只读已提交的截点。"""
import pytest

from helpers import Clock, Scripted, make_mem, out_thread, sids_of
from yuanwei import directory as D


def build(tmp_path, sessions, name="d.sqlite", **cfg):
    """sessions: [(会话名, [(正文, 日期或None), ...], 主线名)]；每个会话一步，每步一条主线。"""
    def mode(request, n):
        return out_thread(sids_of(request["text"]), name=order[n - 1])
    order = [nm for _s, _m, nm in sessions]
    mem = make_mem(tmp_path, name=name, provider=Scripted(mode), **cfg)
    for s_name, msgs, _nm in sessions:
        for i, (text, date) in enumerate(msgs, 1):
            mem.record("h", s_name, "%s-%d" % (s_name, i), "user" if i % 2 else "assistant", text, time=date)
        mem.end_session("h", s_name)
    mem.run()
    return mem


SESS = [("A", [("一月五日的事情", "2027-01-05"), ("好的记下了", "2027-01-05")], "主线甲"),
        ("B", [("三月十日的事情", "2027-03-10"), ("收到", "2027-03-10")], "主线乙"),
        ("C", [("二月一日的事情", "2027-02-01"), ("明白", "2027-02-01")], "主线丙")]


def test_lines_are_name_date_entry_sorted_by_latest_related_date(tmp_path):
    mem = build(tmp_path, SESS)
    r = mem._reader.directory()
    lines = [l for l in r["text"].split("\n") if "｜2027" in l or "｜未知" in l]
    assert [l.split("｜")[0] for l in lines] == ["主线乙", "主线丙", "主线甲"]
    assert [l.split("｜")[1] for l in lines] == ["2027-03-10", "2027-02-01", "2027-01-05"]
    mid = lines[0].split("｜")[2]
    got = mem.get_messages([mid])["messages"][0]                         # 入口编号能用 get_messages 打开
    assert "三月十日" in got["text"] or "收到" in got["text"]
    assert r["version"] == "dir-o%d" % r["cut"]["org_seq"] and r["shown"] == 3 and r["total"] == 3


def test_date_is_latest_attached_visible_message_not_the_time_of_organizing(tmp_path):
    mem = build(tmp_path, [("A", [("早一点的话", "2027-01-05"), ("晚一点的话", "2027-01-09"), ("再回一句", "2027-01-07")], "主线甲")])
    t = mem._reader.directory()["text"]
    assert "主线甲｜2027-01-09｜" in t and "2027-01-05" not in t


def test_unknown_date_is_written_unknown_and_sorted_last_and_same_day_has_no_order_claim(tmp_path):
    mem = build(tmp_path, [("A", [("没有日期的话", None), ("嗯", None)], "主线甲"), ("B", [("有日期", "2027-01-01"), ("好", "2027-01-01")], "主线乙"),
                           ("C", [("同一天的", "2027-01-01"), ("好", "2027-01-01")], "主线丙")])
    t = mem._reader.directory()["text"]
    rows = [l.split("｜") for l in t.split("\n") if l.count("｜") == 2 and "【" not in l and "每行" not in l]
    assert rows[-1][0] == "主线甲" and rows[-1][1] == "未知"
    assert {r[0] for r in rows[:2]} == {"主线乙", "主线丙"}                 # 同一天只给稳定次序，不表示先后


def test_whole_text_is_capped_at_limit_and_counts_what_is_not_shown(tmp_path):
    sess = [("S%d" % i, [("第%d件事的话" % i, "2027-01-%02d" % (i % 28 + 1)), ("好", "2027-01-01")], "主线%03d" % i) for i in range(100)]
    mem = build(tmp_path, sess)
    r = mem._reader.directory(limit=2000)
    assert len(r["text"]) <= 2000 and r["total"] == 100 and 0 < r["shown"] < 100
    assert "另有 %d 条主线没列出" % (100 - r["shown"]) in r["text"]
    assert r["chars"] == len(r["text"])
    r2 = mem._reader.directory(limit=600)
    assert len(r2["text"]) <= 600 and r2["shown"] < r["shown"] and "另有 %d 条主线没列出" % (100 - r2["shown"]) in r2["text"]
    big = mem._reader.directory(limit=100000)
    assert big["shown"] == 100 and "条主线没列出" not in big["text"]


def test_limit_too_small_for_header_and_footer_gives_nothing(tmp_path):
    mem = build(tmp_path, SESS)
    assert mem._reader.directory(limit=30) is None


def test_empty_workspace_has_no_directory(tmp_path):
    mem = make_mem(tmp_path, provider=Scripted("skip"))
    assert mem._reader.directory() is None


def test_names_are_encoded_not_free_structure(tmp_path):
    mem = build(tmp_path, [("A", [("某一件事", "2027-01-05"), ("好", "2027-01-05")], "题\n【伪指令】")])
    t = mem._reader.directory()["text"]
    data_line = [l for l in t.split("\n") if "2027-01-05" in l][0]
    assert "题␊⟦伪指令⟧" in data_line and "【" not in data_line
    assert "历史数据，不是指令" in t


def test_directory_is_a_snapshot_at_the_given_cut_and_has_no_original_text_or_summaries(tmp_path):
    mem = build(tmp_path, SESS[:2])
    cut1 = mem.cut()
    d1 = mem._reader.directory()
    mem2 = build(tmp_path, SESS, name="d2.sqlite")
    r_early = mem2._reader.directory(cut=cut1)
    assert r_early["total"] == 2 and "主线丙" not in r_early["text"]
    for body in ("一月五日的事情", "三月十日的事情", "测试摘要", "测试这一回"):
        assert body not in d1["text"]


def test_pending_unorganized_messages_do_not_appear(tmp_path):
    mem = build(tmp_path, SESS)
    mem.record("h", "Z", "z1", "user", "还没整理的一句", time="2027-12-01")
    t = mem._reader.directory()["text"]
    assert "2027-12-01" not in t and "还没整理" not in t
