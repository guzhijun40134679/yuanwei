"""0.1.0a7·A：正式的「关掉整理」开关（Config.organize，默认开；yuanwei organize on|off|status；向导里的「不整理」）。

关着：原话、切句、索引、向量、封步照常；不拉起整理、不发请求、不花钱、不要密钥；查回来不挂「未整理」；状态里说「整理：关」和关着时封好的步数。
再打开：关着时封好的步按封步顺序补，和「边写边整理」得到完全一样的结果（等价测试）。
全部用自造的数据、假模型、临时目录和假系统；不联网，不读用户的对话、Hermes 的会话库和原委的库。"""
import json
import os
import re
import stat

import pytest

import conftest  # noqa: F401
from helpers import Clock, Scripted, make_mem, out_thread, sids_of
from helpers_wizard import FakeIO, FakeSystem, make_home
from test_hermes_workspace import World, call, turn
from yuanwei import catalog as CT
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei import hosts as H
from yuanwei import organize as OG
from yuanwei import rules as RULES
from yuanwei import wizard as WZ
from yuanwei.access import Access
from yuanwei.config import Config
from yuanwei.errors import ConfigError
from yuanwei.idsource import InProcessIdentity
from yuanwei.memory import Memory
from yuanwei.tools import ToolSurface

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
WORDS = ("未整理", "待整理")


class NoCall:
    """假提供方：只要被调用就失败——关着整理时不许有任何请求。"""

    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        raise AssertionError("整理关着，不许发请求")


def named(request, n):
    return out_thread(sids_of(request["text"]), name="主线%d" % n)


def sub(tmp_path, name):
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def surface(mem, host="pi"):
    acc = Access(mem)
    ids = InProcessIdentity()
    ids.set(host, "S", "", "T1")
    acc.begin_turn(host, "S", "T1", text=None)
    return ToolSurface(mem, acc, H.profile(host), ids)


# ================================================================== 设置本身
def test_default_is_on_and_organize_is_not_part_of_the_step_snapshot():
    c = Config()
    assert c.organize is True and Config(organize=False).organize is False
    assert Config(organize=False).write_snapshot() == c.write_snapshot() and Config(organize=False).config_version() == c.config_version()        # 不影响整理输入的指纹
    for bad in ("false", 0, None):
        with pytest.raises(ConfigError):
            Config(organize=bad).validate()


def test_toml_organize_false_loads_and_absent_means_on(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('organize = false\nretrieval = "keyword"\n')
    assert Config.load(str(p)).organize is False
    p.write_text('retrieval = "keyword"\n')
    assert Config.load(str(p)).organize is True


# ================================================================== 关着时的行为
def story(mem, host="h", n=3):
    """三个会话，每个 4 句。"""
    for s in range(1, n + 1):
        for i in range(1, 5):
            mem.record(host, "S%d" % s, "S%d-m%d" % (s, i), "user" if i % 2 else "assistant", "第%d段对话第%d句：聊聊橘子和苹果%d" % (s, i, s * 10 + i), time="2027-01-0%d" % s)
        mem.end_session(host, "S%d" % s)


def test_off_still_writes_seals_but_never_spawns_requests_or_needs_a_key(tmp_path, monkeypatch):
    prov = NoCall()
    mem = make_mem(tmp_path, provider=prov, organize=False, spawn="subprocess")
    monkeypatch.setattr(Memory, "_spawn", lambda self, args, tag: (_ for _ in ()).throw(AssertionError("整理关着，不许拉起后台整理")))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    mem.vectors_enabled = lambda: False                                              # 向量补算不在这件事里（这里没配向量）
    story(mem)
    rcpt = mem.record("h", "S4", "S4-m1", "user", "再来一句橘子", time="2027-01-04")
    assert rcpt["raw_saved"] and "spawn" not in rcpt and "spawn_error" not in rcpt
    steps = mem.store.list_steps("w")
    assert [s["status"] for s in steps] == ["sealed"] * 3 and prov.calls == []       # 封步照常（三个会话各一步），一个请求都没发
    assert mem.check_and_spawn() == {"action": "none", "reason": "organize_off"}
    r = mem.run()
    assert r["status"] == "organize_off" and r["done"] == 0 and prov.calls == []
    assert mem.store.list_steps("w") == steps                                       # run 什么都没改
    mem.record("h", "S9", "S9-m1", "user", "没封的一句", time="2027-01-09")
    mem.record("h", "S9", "S9-m2", "assistant", "收到", time="2027-01-09")
    mem.clock = Clock("2027-03-01T00:00:00Z")                                       # 离上一条半小时以上：空闲到期
    assert mem.run()["status"] == "organize_off" and len(mem.store.list_steps("w")) == 3          # run 不是封步的入口：关着时它什么都不做（封步在记、读的入口里照常做）
    assert len(mem.maintain()) == 1                                                  # 封步本身照常


def test_off_status_says_off_and_how_many_sealed_steps_wait_and_has_no_backlog(tmp_path):
    mem = make_mem(tmp_path, provider=NoCall(), organize=False)
    story(mem)
    st = mem.status()
    assert st["organize"] == {"on": False, "text": "整理：关", "sealed_steps_waiting": 3, "waiting_text": "关着时封好的 3 步，打开后按封步顺序补"}
    assert st["backlog"] is None and st["organizer"]["enabled"] is False and st["organizer"]["key"]["needed"] is False
    assert not any(w in json.dumps(st, ensure_ascii=False) for w in WORDS)
    on = make_mem(sub(tmp_path, "on"), provider=Scripted("skip"))
    story(on)
    assert on.status()["organize"] == {"on": True, "text": "整理：开"} and on.status()["backlog"]["pending_steps"] == 3


def test_off_status_tool_says_organize_off_and_does_not_show_the_backlog_words(tmp_path):
    mem = make_mem(tmp_path, provider=NoCall(), organize=False)
    story(mem)
    r = surface(mem).call("status", {})
    assert r.status == "ok" and "整理已关" in r.text and "关着时封好的 3 步，打开后按封步顺序补" in r.text
    assert not any(w in r.text for w in WORDS) and '"backlog"' not in r.text and "long_unorganized" not in r.text
    on = make_mem(sub(tmp_path, "on"), provider=Scripted("skip"))
    story(on)
    t = surface(on).call("status", {}).text
    assert '"backlog"' in t and "整理已关" not in t                                 # 开着时输出和以前一样


def test_tool_descriptions_and_resident_rules_never_say_unorganized():
    for name, text in RULES.TOOL_DESCRIPTIONS.items():
        assert not any(w in text for w in WORDS), name
    for mode, text in RULES.RULES.items():
        assert not any(w in text for w in WORDS), mode


def test_off_recall_has_no_unorganized_tail_and_on_still_has_it(tmp_path):
    off = make_mem(sub(tmp_path, "off"), provider=NoCall(), organize=False)
    on = make_mem(sub(tmp_path, "on"), provider=Scripted("skip"), spawn="off")
    for m in (off, on):
        for i, t in enumerate(["今天聊了橘子", "橘子很甜", "苹果也不错"], 1):
            m.record("h", "S1", "m%d" % i, "user" if i % 2 else "assistant", t, time="2027-01-01")
    t_on = on.recall("橘子")["text"]
    t_off = off.recall("橘子")["text"]
    assert "未整理：" in t_on and not any(w in t_off for w in WORDS)
    assert "橘子" in t_off
    assert not any(w in off.note("橘子在哪")["note"] for w in WORDS)
    assert not any(w in surface(off).call("recall", {"query": "橘子"}).text for w in WORDS)
    rc = off.record("h", "S1", "m9", "user", "再来一句", time="2027-01-01")
    assert "未整理" not in rc["readable_note"] and "整理已关" in rc["readable_note"]


def test_off_attachments_says_organize_off_not_no_result(tmp_path):
    off = make_mem(tmp_path, provider=NoCall(), organize=False)
    rc = off.record("h", "S1", "m1", "user", "今天聊了橘子", time="2027-01-01")
    a = off.attachments(rc["msg_id"])
    assert "整理已关" in a["note"] and not any(w in json.dumps(a, ensure_ascii=False) for w in WORDS)


def test_off_delivers_every_direct_hit_exactly_like_an_organized_store_without_the_theme_tricks(tmp_path, monkeypatch):
    """同一段原话：关着整理（库里没有主线），和「整理过、但关掉主线往前排和补同一回」（把读取用的挂接表换成空的），交出的一样（同样的编号、同样的次序、同样的材料文字）。
    没有现成的开关能关这两招，所以在测试里把 reader.attach_maps 换成返回空表；再核「开着主线招数时确实多交了同回的话」，证明这一对不是空转。"""
    from yuanwei import reader as RD
    script = [("user", "采购计划：先买橘子"), ("assistant", "橘子预算两百"), ("user", "再说会议：周五开会"), ("assistant", "会议室订好了"), ("user", "晚饭吃什么"), ("assistant", "吃面")]

    def build(path, **cfg):
        m = make_mem(path, neighbors=0, **cfg)
        for i, (role, t) in enumerate(script, 1):
            m.record("h", "S1", "m%d" % i, role, t, time="2027-01-01")
        m.end_session("h", "S1")
        m.run()
        return m
    off = build(sub(tmp_path, "off"), provider=NoCall(), organize=False)
    org = build(sub(tmp_path, "org"), provider=Scripted(lambda request, n: out_thread(sids_of(request["text"]), name="采购")))
    q = "橘子预算"
    sel_off = off._reader.select(q)
    with_tricks = org._reader.select(q)
    org._reader._snaps.clear()
    monkeypatch.setattr(RD, "attach_maps", lambda state: ({}, {}))
    no_tricks = org._reader.select(q)
    hit_ids = [r["msg_id"] for r in sel_off["selected"]]
    assert hit_ids and [r["msg_id"] for r in no_tricks["selected"]] == hit_ids and no_tricks["material"] == sel_off["material"]
    assert set(hit_ids) < {r["msg_id"] for r in with_tricks["selected"]}                   # 不是空转：开着招数时同回的话也交了，直接命中的一条没少
    assert len(with_tricks["selected"]) > len(hit_ids)


# ================================================================== 等价：关着写、后打开补 ＝ 边写边整理
def drive(path, organize_while_writing, provider):
    """同一段合成历史、同一个假时钟脚本：四个会话；第一个会话 10 句、每 4 句（两轮）按条数封一步；第二个会话 6 句（前两轮按条数封，最后一轮停半小时以上、靠空闲封）；第三个会话用结束事件封；第四个会话只有两条超长条，整步跳过。返回 (mem, clock)。"""
    clk = Clock()
    mem = make_mem(path, provider=provider, clock=clk, spawn="inline", organize=organize_while_writing, max_step_messages=4)
    mid = [0]

    def rec(sess, role, text, date):
        clk.tick()
        mid[0] += 1
        mem.record("h", sess, "%s-m%03d" % (sess, mid[0]), role, text, time=date)
    for i in range(1, 11):
        rec("S1", "user" if i % 2 else "assistant", "橘子计划第%d句：买橘子%d斤，预算%d" % (i, i, i * 7), "2027-01-01")
    mem.end_session("h", "S1")
    for i in range(1, 7):
        rec("S2", "user" if i % 2 else "assistant", "苹果会议第%d句：周%d开会" % (i, i), "2027-01-02")
    clk.advance(minutes=45)                                                           # S2 最后一轮空闲到期；下一次记录时才判
    rec("S3", "user", "第三个会话：旅行去清迈", "2027-01-03")
    rec("S3", "assistant", "清迈的行程可以安排", "2027-01-03")
    mem.end_session("h", "S3")
    rec("S4", "user", "超长的一句：" + "橘子苹果香蕉" * 300, "2027-01-04")                   # 超长条（超过 1500 个字）不送去整理：这个会话整步都是超长条，封成「整步跳过」
    rec("S4", "assistant", "超长的回答：" + "清迈曼谷" * 400, "2027-01-04")
    mem.end_session("h", "S4")
    mem.run()
    return mem, clk


def snapshot_of(mem):
    st = mem.store.read_state("w")
    steps = [(s["step_id"], s["k"], s["status"], s["msg_ids"], s["long_msg_ids"], s["config_version"], s["sealed_reason"], s["prompt_sha256"]) for s in mem.store.list_steps("w")]
    msgs = [tuple(r) for r in mem.store.conn.execute("SELECT msg_id, step_id, seq, visible_at FROM messages WHERE ns='w' ORDER BY seq")]
    return {"tables": st["tables"], "steps": steps, "messages": msgs}


def test_writing_with_organize_off_then_turning_it_on_equals_organizing_while_writing(tmp_path):
    prov_a = Scripted(named)
    mem_a, _ = drive(sub(tmp_path, "a"), True, prov_a)                                   # 甲：边写边整理
    assert prov_a.calls and sorted({s["status"] for s in mem_a.store.list_steps("w")}) == ["done", "skipped"]

    prov_b = NoCall()
    mem_b, clk_b = drive(sub(tmp_path, "b"), False, prov_b)                              # 乙：关着写完
    n_steps = len(mem_b.store.list_steps("w"))
    assert prov_b.calls == [] and n_steps == len(mem_a.store.list_steps("w")) >= 4
    assert [s["status"] for s in mem_b.store.list_steps("w")] == ["sealed"] * n_steps
    assert mem_b.store.read_state("w")["tables"]["threads"] == [] and mem_b.status()["organize"]["sealed_steps_waiting"] == n_steps
    mem_b.close()
    prov_b2 = Scripted(named)                                                        # 再打开：同一个库文件，整理开着，按封步顺序补
    mem_b2 = make_mem(sub(tmp_path, "b"), provider=prov_b2, clock=clk_b, spawn="off", organize=True, max_step_messages=4)
    r = mem_b2.run()
    assert r["status"] == "done" and r["done"] + r["skipped"] == n_steps and r["abandoned"] == 0 and r["skipped"] == 1

    cut = lambda calls: [(c["step_id"], c["question"], c["text"]) for c in calls]    # noqa: E731
    assert cut(prov_b2.calls) == cut(prov_a.calls)                                   # 整理请求逐字节相同、顺序相同
    sa, sb = snapshot_of(mem_a), snapshot_of(mem_b2)
    assert sb["steps"] == sa["steps"] and sb["messages"] == sa["messages"]           # 封步一样、每条原话归哪一步一样、可见序号一样
    for t in sa["tables"]:
        assert sb["tables"][t] == sa["tables"][t], t                                 # 主线、回、摘要、挂接、收据：一张表一张表地逐条相同
    assert sa["tables"]["threads"] and sa["tables"]["attachments"] and sa["tables"]["occ_summaries"]
    for q in ("橘子", "清迈 行程", "苹果会议"):
        assert mem_b2.recall(q)["text"] == mem_a.recall(q)["text"]


def test_the_equivalence_test_would_notice_a_step_that_was_organized_out_of_order_or_with_the_wrong_state(tmp_path):
    """放回缺陷：补的时候把封步顺序倒过来（或者让某一步看不到前面已提交的主线），等价测试里的比较必须变红。这里直接造一份被破坏的「乙」，验证比较函数会报不同。"""
    prov_a = Scripted(named)
    mem_a, _ = drive(sub(tmp_path, "a"), True, prov_a)
    mem_b, clk_b = drive(sub(tmp_path, "b"), False, NoCall())
    mem_b.close()
    prov_bad = Scripted(named)
    mem_bad = make_mem(sub(tmp_path, "b"), provider=prov_bad, clock=clk_b, spawn="off", organize=True, max_step_messages=4)
    steps = mem_bad.store.list_steps("w")
    mem_bad.pending_steps = lambda: list(reversed([s for s in steps if s["status"] in ("sealed", "failed")]))        # 倒序补
    try:
        mem_bad.run()
    except Exception:                                                                 # noqa: BLE001  倒序会因为步序号对不上被存储拒收：也算「被发现」
        pass
    cut = lambda calls: [(c["step_id"], c["question"], c["text"]) for c in calls]    # noqa: E731
    assert cut(prov_bad.calls) != cut(prov_a.calls) or snapshot_of(mem_bad)["tables"] != snapshot_of(mem_a)["tables"]


# ================================================================== 开着时中途关掉
def test_turning_off_in_the_file_between_steps_lets_the_running_step_finish_and_stops_before_the_next(tmp_path):
    toml = tmp_path / "y.toml"
    toml.write_text('retrieval = "keyword"\nindex_version = 2\n')

    def fn(request, n):
        if n == 1:
            toml.write_text('retrieval = "keyword"\nindex_version = 2\norganize = false\n')          # 用户在第 1 步进行中 `yuanwei organize off`
        return named(request, n)
    prov = Scripted(fn)
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config.load(str(toml)), provider=prov, clock=Clock(), spawn="off", config_path=str(toml))
    for s in range(1, 4):
        for i, role in enumerate(("user", "assistant"), 1):
            mem.record("h", "S%d" % s, "S%d-m%d" % (s, i), role, "第%d个会话第%d句：橘子" % (s, i), time="2027-01-0%d" % s)
        mem.end_session("h", "S%d" % s)
    assert [s["status"] for s in mem.store.list_steps("w")] == ["sealed"] * 3
    r = mem.run()
    assert r["status"] == "organize_off" and r["done"] == 1 and len(prov.calls) == 1             # 正在做的那一步做完了（不是放弃），后面不再做
    assert [s["status"] for s in mem.store.list_steps("w")] == ["done", "sealed", "sealed"]
    toml.write_text('retrieval = "keyword"\nindex_version = 2\n')                                   # 再打开
    r2 = mem.run()
    assert r2["status"] == "done" and r2["done"] == 2 and [s["status"] for s in mem.store.list_steps("w")] == ["done"] * 3


# ================================================================== yuanwei organize on|off|status
def install(h, *extra, env=None):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + list(extra) + BASE, sys_=FakeSystem(env=env or {})) == 0


def files(h):
    return {os.path.relpath(os.path.join(d, f), h): open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(h) for f in fs if "yuanwei-backup" not in f}


def run_cli(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, (json.loads(out.out) if out.out.strip() else None), out.err


def test_organize_command_plan_then_off_then_on_restores_the_install_byte_for_byte(tmp_path, capsys):
    h = make_home(tmp_path)
    install(h)
    original = files(h)
    toml = os.path.join(h, "yuanwei", "yuanwei.toml")
    rc, st, _ = run_cli(["organize", "status", "--hermes-home", h], capsys)
    assert rc == 0 and st["organize"] == "on" and st["text"] == "整理：开" and not st["applied"]
    rc, plan, _ = run_cli(["organize", "off", "--hermes-home", h], capsys)                         # 不加 --yes：只列计划
    assert rc == 0 and plan["would_change"] is True and plan["applied"] is False and plan["changed"] is False and files(h) == original
    rc, done, _ = run_cli(["organize", "off", "--hermes-home", h, "--yes"], capsys)
    assert rc == 0 and done["changed"] and done["applied"] and done["restart_hermes"] and done["organize"] == "off"
    assert Config.load(toml).organize is False and "organize = false" in open(toml, encoding="utf-8").read()
    man = json.load(open(os.path.join(h, "yuanwei", "install.json"), encoding="utf-8"))
    assert man["files"]["yuanwei/yuanwei.toml"] == HS._sha(open(toml, "rb").read())               # 安装记录里的指纹跟着改了
    assert len(done["backups"]) == 2 and all(os.path.exists(b) for b in done["backups"])
    assert stat.S_IMODE(os.stat(toml).st_mode) == stat.S_IMODE(os.stat(done["backups"][0]).st_mode)
    for rel, data in original.items():                                                              # 除了 yuanwei.toml 和 install.json，别的文件一个字没动
        if rel not in ("yuanwei/yuanwei.toml", "yuanwei/install.json"):
            assert files(h)[rel] == data, rel
    rc, st2, _ = run_cli(["organize", "status", "--hermes-home", h], capsys)
    assert st2["organize"] == "off" and "整理：关" in st2["text"]
    rc, again, _ = run_cli(["organize", "off", "--hermes-home", h, "--yes"], capsys)
    assert again["would_change"] is False and again["changed"] is False                              # 已经是关的：不改、不再备份
    rc, back, _ = run_cli(["organize", "on", "--hermes-home", h, "--yes"], capsys)
    assert back["changed"] and Config.load(toml).organize is True
    assert files(h)["yuanwei/yuanwei.toml"] == original["yuanwei/yuanwei.toml"]                      # 打开后和原来逐字一样
    assert json.load(open(os.path.join(h, "yuanwei", "install.json"), encoding="utf-8"))["files"] == man["files"] | {"yuanwei/yuanwei.toml": HS._sha(original["yuanwei/yuanwei.toml"])}


def test_organize_command_refuses_a_hand_edited_toml_and_never_edits_a_users_own_config(tmp_path, capsys):
    h = make_home(tmp_path)
    install(h)
    toml = os.path.join(h, "yuanwei", "yuanwei.toml")
    with open(toml, "a", encoding="utf-8") as f:
        f.write("# 用户自己加的一行\n")
    before = files(h)
    rc, _, err = run_cli(["organize", "off", "--hermes-home", h, "--yes"], capsys)
    assert rc == 2 and "config_modified" in err and files(h) == before
    h2 = make_home(tmp_path, "h2")
    own = tmp_path / "mine.toml"
    own.write_text('retrieval = "keyword"\n')
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h2, "--config", str(own)] + BASE, sys_=FakeSystem(env={})) == 0
    rc, out, _ = run_cli(["organize", "off", "--hermes-home", h2, "--yes"], capsys)
    assert rc == 0 and out["editable"] is False and "organize = false" in out["hint"] and out["changed"] is False
    assert own.read_text() == 'retrieval = "keyword"\n'


def test_edit_toml_only_touches_the_organize_line_and_refuses_when_the_result_is_not_as_expected():
    text = '# 头\nretrieval = "hybrid"\nvector_fallback = "partial"\n\n[provider]\nid = "x"\n\n[embedding]\nidentity = { "a" = 1 }\n'
    off = OG.edit_toml(text, off=True)
    assert off == '# 头\nretrieval = "hybrid"\nvector_fallback = "partial"\norganize = false\n\n[provider]\nid = "x"\n\n[embedding]\nidentity = { "a" = 1 }\n'
    assert OG.edit_toml(off, off=True) == off and OG.edit_toml(off, off=False) == text
    assert OG.edit_toml('x = 1', off=True) == 'x = 1\norganize = false\n'
    with pytest.raises(ValueError):
        OG.edit_toml("not = [valid", off=True)


def test_status_hermes_and_key_status_do_not_treat_a_missing_key_as_a_problem_when_off(tmp_path, capsys):
    h = make_home(tmp_path)
    install(h)
    st = HS.install_status(h)
    assert st["organize"] == {"on": True, "text": "整理：开"} and "enabled" not in st["organizer"] and "needed" not in st["organizer"]["key"]
    assert st["organizer"]["key"]["source"] == "none"
    cli.main(["organize", "off", "--hermes-home", h, "--yes"])
    capsys.readouterr()
    st = HS.install_status(h)
    assert st["organize"]["on"] is False and st["organize"]["text"].startswith("整理：关")
    assert st["organizer"]["enabled"] is False and st["organizer"]["key"]["needed"] is False
    # key status：关着时说明不需要密钥
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env={})) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["organize"] == "off" and rep["organizer_key_needed"] is False and any("整理已关" in n for n in rep["notes"])
    # 开着时的 key status 和以前一样（没有这两项）
    cli.main(["organize", "on", "--hermes-home", h, "--yes"])
    capsys.readouterr()
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env={})) == 0
    rep = json.loads(capsys.readouterr().out)
    assert "organize" not in rep and "organizer_key_needed" not in rep


def test_an_unusable_key_file_is_a_note_only_while_organize_is_on(tmp_path, capsys):
    from yuanwei import keystore as KS
    h = make_home(tmp_path)
    install(h)
    g = KS.write_global_key("DEEPSEEK_API_KEY", "sk-GLOBALKEY000000bbbbbbbb")
    os.chmod(g, 0o644)                                                                             # 权限太松：密钥文件「不能用」
    assert HS.install_status(h)["organizer"]["key"]["source"].startswith("file_unusable")
    assert any(KS.UNUSABLE_NOTE == n for n in HS.install_status(h)["notes"])
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env={})) == 0
    assert KS.UNUSABLE_NOTE in json.loads(capsys.readouterr().out)["notes"]
    cli.main(["organize", "off", "--hermes-home", h, "--yes"])
    capsys.readouterr()
    assert KS.UNUSABLE_NOTE not in HS.install_status(h)["notes"]                                    # 整理关着：不需要密钥，密钥文件不能用不算问题
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env={})) == 0
    notes = json.loads(capsys.readouterr().out)["notes"]
    assert KS.UNUSABLE_NOTE not in notes and any("整理已关" in n for n in notes)


def test_status_hermes_shows_waiting_steps_when_off(tmp_path, capsys):
    h = make_home(tmp_path)
    install(h)
    cli.main(["organize", "off", "--hermes-home", h, "--yes"])
    capsys.readouterr()
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    db = json.load(open(os.path.join(h, "yuanwei", "config.json"), encoding="utf-8"))["db"]
    mem = Memory.open(db, "life", config=cfg, provider=NoCall(), clock=Clock(), spawn="off")
    story(mem, n=2)
    mem.close()
    st = HS.install_status(h)
    assert st["organize"]["text"] == "整理：关；关着时封好的 2 步，打开后按封步顺序补"
    rc, out, _ = run_cli(["organize", "status", "--hermes-home", h], capsys)
    assert out["sealed_steps_waiting"] == 2


# ================================================================== 向导和不问话的安装
def test_param_install_with_no_organize_writes_the_line_and_a_reinstall_keeps_it(tmp_path, capsys):
    h = make_home(tmp_path)
    install(h, "--no-organize")
    toml = os.path.join(h, "yuanwei", "yuanwei.toml")
    text = open(toml, encoding="utf-8").read()
    assert Config.load(toml).organize is False and text.count("organize = false") == 1
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):                                                           # 不加 --yes：只列计划
        cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h, "--no-organize"] + BASE, sys_=FakeSystem(env={}))
    plan = json.loads(buf.getvalue())
    lines = "\n".join(plan["plan"]["lines"])
    assert "整理        关（不整理）" in lines and "密钥：" not in lines and "DEEPSEEK_API_KEY" not in lines
    install(h)                                                                                      # 重装不带 --no-organize：沿用已有的关，不悄悄打开
    assert open(toml, encoding="utf-8").read() == text and Config.load(toml).organize is False
    # 全新安装（没给 --no-organize）：开着，配置文件里没有这一行，和以前逐字一样
    h2 = make_home(tmp_path, "h2")
    install(h2)
    assert "organize" not in open(os.path.join(h2, "yuanwei", "yuanwei.toml"), encoding="utf-8").read()
    # 和 yuanwei organize off 改出来的逐字一样
    cli.main(["organize", "off", "--hermes-home", h2, "--yes"])
    capsys.readouterr()
    assert open(os.path.join(h2, "yuanwei", "yuanwei.toml"), encoding="utf-8").read().replace(h2, h) == text


def test_wizard_has_a_no_organize_choice_that_skips_provider_and_key_and_the_default_path_is_unchanged(tmp_path):
    rows = [r for r in CT.PROVIDERS if r["id"] != "local"]
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "n", str(len(rows) + 1), "1", "y", "y"])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={})) == 0
    assert "不整理" in io.text and "第 5 步　整理用的密钥" not in io.text and not any(a.startswith("[secret]") for a in io.asked)
    toml = os.path.join(h, "yuanwei", "yuanwei.toml")
    assert Config.load(toml).organize is False
    # 默认的一路（回答和以前一样）：整理开着
    h2 = make_home(tmp_path, "h2")
    io2 = FakeIO(["1", "2", "y", "1", "n", "1", "y", "y"], secrets=["sk-FAKEKEY1234567890abcdefXYZ", "sk-FAKEKEY1234567890abcdefXYZ"])
    assert cli._setup_main(["setup", "--hermes-home", h2] + BASE, io=io2, sys_=FakeSystem(env={})) == 0
    assert Config.load(os.path.join(h2, "yuanwei", "yuanwei.toml")).organize is True and "第 5 步　整理用的密钥" in io2.text


def test_changing_vectors_keeps_organize_off(tmp_path):
    h = make_home(tmp_path)
    install(h, "--no-organize")
    toml = os.path.join(h, "yuanwei", "yuanwei.toml")
    assert cli._vectors_main(["setup", "--vectors", "none", "--yes", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env={})) == 0
    assert Config.load(toml).organize is False                                                     # 改向量重写 yuanwei.toml：整理开关不能被冲掉


def test_off_install_does_not_probe_a_local_model(tmp_path, monkeypatch):
    from yuanwei import embedsetup as ES
    called = []
    monkeypatch.setattr(ES, "long_input_probe", lambda *a, **k: called.append(a) or "ok")
    h = make_home(tmp_path)
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cli._setup_main(["setup", "--host", "hermes", "--preset", "local", "--yes", "--no-organize", "--vectors", "none", "--hermes-home", h, "--provider-url", "http://127.0.0.1:1/v1", "--provider-model", "m"] + BASE, sys_=FakeSystem(env={}))
    assert rc == 0 and called == []


# ================================================================== Hermes 接头
def test_hermes_adapter_with_organize_off_records_seals_and_never_spawns(tmp_path, monkeypatch):
    w = World(tmp_path, scope="isolated", mem_cfg={"organize": False}, seed=False)
    monkeypatch.setattr(Memory, "_spawn", lambda self, args, tag: (_ for _ in ()).throw(AssertionError("整理关着，不许拉起后台整理")))
    a = w.adapter("/elsewhere")
    turn(a, 1, "橘子很甜")
    a.sync_turn("橘子很甜", "是的", session_id="s1", messages=[{"role": "user", "content": "橘子很甜"}, {"role": "assistant", "content": "是的"}])
    turn(a, 2, "普通")                                                                              # 下一轮：上一轮存的话才算历史
    r = call(a, "recall", {"query": "橘子"})
    assert r["status"] == "ok" and "橘子" in r["text"] and not any(x in r["text"] for x in WORDS)
    assert "整理已关" in call(a, "status")["text"]
