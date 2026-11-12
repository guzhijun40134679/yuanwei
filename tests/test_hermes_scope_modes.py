"""交回点乙·第 2c 步：便条、目录两种自动先查，也照同一条读取范围读（甲 isolated、乙 life_all＝默认、丙 all_open）；读多个工作区时逐段标明出处；
许可（禁读口令、不是主人）对所有工作区同样生效；3 秒时限是总共的；整段装不下宿主这条路径的上限时从后面的工作区开始去掉并写明。全部自造数据，断网。"""
import re

import pytest

from helpers import Scripted
from helpers2 import organized
from test_hermes_workspace import World, call, turn
from yuanwei import hosts as H
from yuanwei import note as NT
from yuanwei.access import Access
from yuanwei.modes import ModeConfig, ModeRunner

BUD = {"life": ["周末在家做饭，聊了聊这个月的预算", "预算批下来了，晚上吃咖喱"], "proj:p-a": ["甲项目的预算要在周五前提交", "预算表给财务确认过了"], "proj:p-b": ["乙项目的预算被砍了一半", "预算削减以后推迟重构"]}


def note_world(tmp_path, scope, **kw):
    kw.setdefault("texts", BUD)
    w = World(tmp_path, scope=scope, **kw)
    w.cfg["mode"] = "note"
    return w


def read(a, q="这个月的预算怎么样了"):
    turn(a, 1, q)
    return a.prefetch(q, session_id="s1")


# ================================================================== 便条
def test_note_B_in_life_reads_life_and_all_projects_each_labelled(tmp_path):
    w = note_world(tmp_path, "life_all")
    t = read(w.adapter("/elsewhere"))
    assert "〔读取范围：生活、甲项目、乙项目〕" in t
    sec = {m: t[t.index("〔工作区：%s〕" % m):] for m in ("生活", "甲项目", "乙项目")}
    assert "咖喱" in sec["生活"].split("〔工作区：甲项目〕")[0]
    assert "甲项目的预算" in sec["甲项目"].split("〔工作区：乙项目〕")[0] and "乙项目的预算" in sec["乙项目"]
    assert "乙项目的预算" not in sec["甲项目"].split("〔工作区：乙项目〕")[0]                         # 每段只含它自己工作区的原话


def test_note_B_in_a_project_reads_only_that_project_and_looks_exactly_like_before(tmp_path):
    w = note_world(tmp_path, "life_all")
    t = read(w.adapter("/work/a"))
    assert "甲项目的预算" in t and "咖喱" not in t and "乙项目" not in t
    assert "〔读取范围：" not in t and "〔工作区：" not in t                                          # 只读一个工作区：和第 2 步一样，不加标题


def test_note_A_isolated_everyone_reads_only_their_own(tmp_path):
    w = note_world(tmp_path, "isolated")
    t = read(w.adapter("/elsewhere"))
    assert "咖喱" in t and "甲项目" not in t and "乙项目" not in t and "〔读取范围：" not in t


def test_note_C_all_open_reads_everything_from_a_project_and_the_current_one_comes_first(tmp_path):
    w = note_world(tmp_path, "all_open")
    t = read(w.adapter("/work/a"))
    assert "〔读取范围：甲项目、生活、乙项目〕" in t
    assert t.index("〔工作区：甲项目〕") < t.index("〔工作区：生活〕") < t.index("〔工作区：乙项目〕")


def test_note_cursors_for_other_workspaces_carry_the_workspace_prefix_and_resume_in_that_workspace(tmp_path):
    long = {k: ["%s第%d条：预算相关的一长串说明，用来把便条撑满。" % (k, i) + "补充说明" * 20 for i in range(30)] for k in BUD}
    w = note_world(tmp_path, "life_all", texts=long, mem_cfg=dict(note_chars=1800))
    a = w.adapter("/elsewhere")
    t = read(a, "预算相关的说明")
    curs = re.findall(r"W(\d+)~([0-9a-f]{16}:\d+:\d+)", t)
    assert {c[0] for c in curs} >= {"1", "2"}                                                        # 别的工作区的游标带前缀；本工作区的也带（范围里不止一个）
    for i, cur in curs:
        r = call(a, "get_messages", {"cursor": "W%s~%s" % (i, cur)})
        label = {"0": "生活", "1": "甲项目", "2": "乙项目"}[i]
        assert r["status"] == "ok" and "〔工作区：%s〕" % label in r["text"] or label in r["text"], (i, r)
    assert call(a, "get_messages", {"cursor": curs[0][1]})["status"] == "error"                        # 范围里不止一个工作区时，没前缀的游标不认


def test_banned_turn_gets_only_the_receipt_no_note_from_any_workspace(tmp_path):
    w = note_world(tmp_path, "all_open")
    a = w.adapter("/elsewhere")
    turn(a, 1, "#yuanwei-nomem\n预算怎么样")
    t = a.prefetch("#yuanwei-nomem\n预算怎么样", session_id="s1")
    assert t.startswith("本轮未新增读取长期记忆") and "甲项目" not in t and "乙项目" not in t and "咖喱" not in t


def test_a_gateway_non_owner_gets_no_note_even_though_other_workspaces_exist(tmp_path):
    w = note_world(tmp_path, "all_open", owners={"telegram": ["me"]})
    a = w.adapter(None, platform="telegram", user_id="me")
    turn(a, 1, "路人的话：预算怎么样", author_id="stranger")
    assert a.prefetch("预算怎么样", session_id="s1") == ""


def test_unresolved_workspace_gets_no_note(tmp_path):
    w = note_world(tmp_path, "all_open")
    a = w.adapter("relative/path")                                                                   # 给了不合格的 cwd，有项目库：认不出
    turn(a, 1, "预算怎么样")
    assert a.prefetch("预算怎么样", session_id="s1") == ""


def test_the_read_log_of_one_ticket_names_every_workspace_read_and_which_ones_were_not(tmp_path):
    w = note_world(tmp_path, "life_all")
    a = w.adapter("/elsewhere")
    read(a)
    rows = a.acc.log_rows("hermes", "s1", a._cur["s1"])
    start = [r for r in rows if r["kind"] == "start"][0]
    assert start["detail"]["summary"]["workspaces"] == ["life", "proj:p-a", "proj:p-b"]
    asm = [r for r in rows if r["kind"] == "assembled"]
    assert len(asm) == 1 and {sp["workspace"] for sp in asm[0]["detail"]["spans"]} == {"life", "proj:p-a", "proj:p-b"}


# ================================================================== 目录
def dir_world(tmp_path, scope):
    w = World(tmp_path, scope=scope, texts=BUD, threads=True)
    w.cfg["mode"] = "directory"
    return w


def test_directory_B_in_life_lists_every_workspaces_threads_labelled(tmp_path):
    t = read(dir_world(tmp_path, "life_all").adapter("/elsewhere"))
    assert "〔读取范围：生活、甲项目、乙项目〕" in t
    assert t.index("〔工作区：生活〕") < t.index("生活的主线") < t.index("〔工作区：甲项目〕") < t.index("甲项目的主线") < t.index("〔工作区：乙项目〕") < t.index("乙项目的主线")


def test_directory_B_in_a_project_and_A_isolated_show_only_their_own(tmp_path):
    for scope, cwd, own, others in (("life_all", "/work/a", "甲项目的主线", ("生活的主线", "乙项目的主线")), ("isolated", "/elsewhere", "生活的主线", ("甲项目的主线", "乙项目的主线"))):
        t = read(dir_world(tmp_path / scope, scope).adapter(cwd))
        assert own in t and not any(o in t for o in others) and "〔读取范围：" not in t, (scope, t)


def test_directory_C_in_a_project_reads_all(tmp_path):
    t = read(dir_world(tmp_path, "all_open").adapter("/work/b"))
    assert "〔读取范围：乙项目、生活、甲项目〕" in t and all(x in t for x in ("生活的主线", "甲项目的主线", "乙项目的主线"))


def test_directory_handed_state_is_kept_once_per_session_not_once_per_workspace(tmp_path):
    w = dir_world(tmp_path, "life_all")
    a = w.adapter("/elsewhere")
    first = read(a)
    assert first
    a.runner.confirm_injection({"kind": "directory", "epoch": a.acc.dir_get("hermes", "s1")["epoch"], "session_id": "s1", "branch": "", "turn_id": a._cur["s1"]}, ok=True)
    turn(a, 2, "第二问")
    assert a.prefetch("第二问", session_id="s1") == ""                                               # 已经交过：不再交（别的工作区的目录不会让它重交）


# ================================================================== ModeRunner 本身：总时限、装不下的处理
def two_mems(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return (organized(tmp_path, ["预算在家里聊过", "预算批下来了"], ws="life", name="s.sqlite", host="hermes"),
            organized(tmp_path, ["甲项目的预算", "预算表确认了"], ws="proj:a", name="s.sqlite", host="hermes"))


def runner2(tmp_path, **cfg):
    life, pa = two_mems(tmp_path)
    return life, pa, ModeRunner(life, Access(life), H.profile("hermes"), ModeConfig(mode="note", **cfg))


def test_runner_extras_share_one_total_deadline_and_a_slow_workspace_is_named_not_silently_dropped(tmp_path, monkeypatch):
    import time
    life, pa, r = runner2(tmp_path, auto_timeout_s=1.0)
    real = NT.analyze

    def slow(reader, text, cfg):
        time.sleep(0.6 if reader.ns == "life" else (3.0 if reader.ns == "proj:a" else 0))
        return real(reader, text, cfg)
    monkeypatch.setattr(NT, "analyze", slow)
    r.acc.begin_turn("hermes", "S", "T1", text="预算怎么样", origin="user")
    t0 = time.time()
    out = r.pre_answer("S", "T1", "预算怎么样", extras=[("proj:a", "甲项目", pa, 1)], primary=("life", "生活", 0))
    assert time.time() - t0 < 1.35                                                                   # 总共 1 秒：当前工作区用了 0.6，别的只剩 0.4；不是每个工作区各 1 秒（那要 1.6 秒）
    assert "预算" in out["inject"] and "甲项目的预算" not in out["inject"]
    assert "没读到：甲项目（超时）" in out["inject"]                                                    # 没读到的要说出来，不当成「没有」
    assert out["workspaces_not_read"] == [{"workspace": "甲项目", "why": "timeout"}]


def test_runner_extras_over_the_host_limit_drop_from_the_back_and_say_so(tmp_path):
    import dataclasses
    life, pa, _ = runner2(tmp_path)
    prof = H.profile("hermes")

    def run(limit, turn):
        tiny = dataclasses.replace(prof, paths=dict(prof.paths, hook_context=H.Path("hook_context", (H.Limit("cp", limit, "x"),), {}, False, "t")))
        r = ModeRunner(life, Access(life), tiny, ModeConfig(mode="note"))
        r.acc.begin_turn("hermes", "S", turn, text="预算怎么样", origin="user")
        return r.pre_answer("S", turn, "预算怎么样", extras=[("proj:a", "甲项目", pa, 1)], primary=("life", "生活", 0))
    both = run(420, "T1")
    assert len(both["inject"]) <= 420 and "甲项目的预算" in both["inject"] and both["workspaces_not_read"] == []              # 放得下：两个都在
    one = run(300, "T2")
    assert len(one["inject"]) <= 300 and "〔工作区：生活〕" in one["inject"] and "甲项目的预算" not in one["inject"]
    assert "没放进来：甲项目" in one["inject"] and one["workspaces_not_read"] == [{"workspace": "甲项目", "why": "over_limit"}]    # 放不下：从后面的工作区去掉，并写明
