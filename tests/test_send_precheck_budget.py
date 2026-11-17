"""整理的费用怎么挡（施工单 1.7 节，N2）：每一次请求（第一问、第二问、每一次技术重问）在最终拼好以后、真正发出之前，按它的实际字节数核预算；
超了就挡在发出之前；被挡的不算已发送，续跑沿用原计数和封步时绑定的配置。用假上游和临时账本，不联网、不花钱。"""
import json

import pytest

from helpers import Clock, Scripted, make_mem, out_skip, out_thread, sids_of
from yuanwei import Config, Memory
from yuanwei.ask import BudgetBlocked
from yuanwei.cards import LightConfig

BIG = 1e9


def body_len(mem, text):
    """最终请求体字节：和提供方同一个序列化函数、同一份参数。"""
    from yuanwei import provider as PV
    return len(PV.write_request_bytes(text, (mem.cfg.provider or {}).get("params")))


def open_mem(tmp_path, provider, name="m.sqlite", cap=BIG, clock=None, **cfg):
    return Memory.open(str(tmp_path / name), "w", config=Config(daily_cap_cny=cap, **cfg), provider=provider, clock=clock or Clock(), spawn="off")


def two_threads(mem):
    for i, name in ((1, "甲"), (2, "乙")):
        mem.record("h", "T%d" % i, "u1", "user", "Thread %s sentence one. Thread %s sentence two." % (name, name), time="2027-01-0%d" % i)
        mem.record("h", "T%d" % i, "a1", "assistant", "ok", time="2027-01-0%d" % i)
        mem.end_session("h", "T%d" % i)
    mem.run()


def gap_mode(request, n):
    text = request["text"]
    sids = sids_of(text)
    if request["question"] == "q2":
        return json.dumps({"新建": {"主线": [], "回": []}, "去处": [{"句": sids_of(text.split("要重判的句子：")[1]), "不记": True}], "缺口": []}, ensure_ascii=False)
    if "Hello there" in text.split("这次对话：")[-1]:
        return json.dumps({"新建": {"主线": [], "回": []}, "去处": [], "缺口": [{"句": sids, "层": "主线", "已知主线": None, "词": ["测试主线"]}], "改摘要": []}, ensure_ascii=False)
    return out_thread(sids) if "Thread" in text.split("这次对话：")[-1] else out_skip(sids)


def add_gap_session(mem):
    mem.record("h", "S3", "u1", "user", "Hello there. How are you today?", time="2027-01-03")
    mem.record("h", "S3", "a1", "assistant", "Great. See you soon, my friend.", time="2027-01-03")
    mem.end_session("h", "S3")


def prov_checking_order(holder):
    """提供方：每次被调用时，发送前核对必须已经比调用次数多记一条（先核、后发）。"""
    calls = []

    def mode(request, n):
        mem = holder["mem"]
        assert len([c for c in mem.send_checks if not c["blocked"]]) == len(calls) + 1 and mem.send_checks[-1]["blocked"] is False, "发出之前还没核预算"
        calls.append(request["text"])
        return gap_mode(request, n)
    p = Scripted(mode)
    p.texts = calls
    return p


# ---------------------------------------------------------------- 第一问短、第二问长：每一次完整请求的字节都在发出之前核过
def test_every_request_bytes_are_checked_before_it_is_sent_q1_short_q2_long(tmp_path):
    holder = {}
    prov = prov_checking_order(holder)
    mem = open_mem(tmp_path, prov, light=LightConfig(max_threads=1))
    holder["mem"] = mem
    two_threads(mem)
    holder["offset"] = len(mem.send_checks)
    add_gap_session(mem)
    mem.run()
    qs = [c["question"] for c in mem.send_checks[holder["offset"]:]]
    assert qs == ["q1", "q2"]
    last2 = mem.send_checks[holder["offset"]:]
    sent = prov.texts[-2:]
    assert [c["bytes"] for c in last2] == [body_len(mem, t) for t in sent]       # 核的就是实际发出去的那份完整请求的字节数
    assert last2[1]["bytes"] > last2[0]["bytes"]                                        # 第二问比第一问长（追加了补找候选、整场对话、要重判的句子）
    assert all(c["blocked"] is False and c["worst_cny"] > 0 for c in last2)


def test_over_budget_second_request_is_blocked_before_sending_and_not_counted(tmp_path):
    # 先在大预算下跑一遍，量出每次请求的最坏费用和第一问花的钱
    holder = {}
    dry = prov_checking_order(holder)
    md = open_mem(tmp_path, dry, name="dry.sqlite", light=LightConfig(max_threads=1))
    holder["mem"] = md
    two_threads(md)
    off = len(md.send_checks)
    add_gap_session(md)
    md.run()
    w1, w2 = [c["worst_cny"] for c in md.send_checks[off:]]
    spent_after_q1 = 0.01                                                              # 假上游每次回报用量：1000 未命中输入、1000 输出
    # 预算只够第一问：第一问放行，第二问在发出之前被挡
    cap = spent_after_q1 + w2 - 0.00001
    assert w1 <= cap
    holder2 = {}
    prov = prov_checking_order(holder2)
    mem = open_mem(tmp_path, prov, light=LightConfig(max_threads=1))
    holder2["mem"] = mem
    two_threads(mem)                                                                   # 前两步在大预算里跑完，花的钱记在账上
    mem.cfg.daily_cap_cny = cap + mem.cost_today()["cny"]                              # 把已经花掉的前两步算进去
    holder2["offset"] = len(mem.send_checks)
    add_gap_session(mem)
    r = mem.run()
    assert r["status"] == "paused" and r["reason"] == "daily_budget" and r["stopped_at"] == "step-0003"
    sid = "step-0003"
    assert len(mem.store.list_judgments("w", sid, "q1")) == 1                          # 第一问发了
    assert mem.store.list_judgments("w", sid, "q2") == []                              # 第二问被挡：不算已发送，没有判断记录
    assert prov.texts[-1] == dry.texts[-2]                                             # 提供方最后一次收到的是第一问（和不设限时发出去的第一问逐字相同）
    assert mem.send_checks[-1]["question"] == "q2" and mem.send_checks[-1]["blocked"] is True
    assert mem.send_checks[-1]["bytes"] == body_len(md, dry.texts[-1])          # 挡下的就是那份完整的第二问请求（字节数和不设限时发出去的一样）
    st = mem.store.get_step("w", sid)
    assert st["status"] == "sealed" and st["failures"] == 0 and st["last_error"] is None   # 不算失败、不改状态
    # 续跑：隔天预算重置，沿用原来的调用次数；第一问不重发，只发第二问
    mem.clock.advance(days=1)
    mem.cfg.daily_cap_cny = BIG
    n_before = len(prov.texts)
    rr = mem.run()
    assert rr["done"] == 1, rr
    assert len(prov.texts) == n_before + 1 and "要重判的句子" in prov.texts[-1]
    assert len(mem.store.list_judgments("w", sid, "q1")) == 1 and len(mem.store.list_judgments("w", sid, "q2")) == 1
    rc = [x for x in mem.store.read_state("w")["tables"]["receipts"] if x["key"] == "step:" + sid][0]
    assert rc["q1_sends"] == 1 and rc["q2_sends"] == 1


# ---------------------------------------------------------------- 技术重问的反馈让请求变长
def test_retry_feedback_makes_the_request_longer_and_it_is_checked_again(tmp_path):
    seen = []

    def mode(request, n):
        seen.append(request["text"])
        return "这不是 JSON" if n == 1 else out_skip(sids_of(request["text"]))
    mem = open_mem(tmp_path, Scripted(mode))
    mem.record("h", "S1", "u1", "user", "你好")
    mem.record("h", "S1", "a1", "assistant", "你好呀")
    mem.end_session("h", "S1")
    mem.run()
    c = mem.send_checks
    assert len(c) == 2 and c[1]["bytes"] > c[0]["bytes"]                                # 重问带着反馈，请求变长
    assert [x["bytes"] for x in c] == [body_len(mem, t) for t in seen]            # 每一次都核了实际字节
    w1, w2 = c[0]["worst_cny"], c[1]["worst_cny"]
    # 预算刚好够第一次、不够带反馈的重问：重问被挡在发出之前，不算一次发送
    mem2 = open_mem(tmp_path, Scripted(lambda r, n: "这不是 JSON"), name="b.sqlite", cap=w1 + 0.01 + (w2 - w1) / 2)
    mem2.record("h", "S1", "u1", "user", "你好")
    mem2.record("h", "S1", "a1", "assistant", "你好呀")
    mem2.end_session("h", "S1")
    r = mem2.run()
    assert r["status"] == "paused" and r["reason"] == "daily_budget"
    assert len(mem2.store.list_judgments("w", "step-0001", "q1")) == 1                  # 只有第一次发了；被挡的重问没留记录
    assert mem2.send_checks[-1]["blocked"] is True and mem2.send_checks[-1]["bytes"] == c[1]["bytes"]
    assert w2 > w1


# ---------------------------------------------------------------- 旧步绑定的卡片额度和现在的配置不一样
def test_old_step_bound_card_budget_differs_from_current_config(tmp_path):
    small = LightConfig(cards_tokens=60, max_threads=1)

    def run_with(open_fn, name, light):
        prov = Scripted(gap_mode)
        mem = open_fn(tmp_path, prov, name)
        return mem, prov

    def legacy(tmp_path, prov, name):
        return Memory.open(str(tmp_path / name), "w", config=Config(daily_cap_cny=BIG, index_version=2, split_version="split-1", ruler_version=1, light=LightConfig(cards_tokens=60, max_threads=1)),
                           provider=prov, clock=Clock(), spawn="off")

    def new(tmp_path, prov, name, light=None):
        return Memory.open(str(tmp_path / name), "w", config=Config(daily_cap_cny=BIG, light=light or LightConfig(cards_tokens=16000, max_threads=1)), provider=prov, clock=Clock(), spawn="off")
    # 基线：一直用旧配置（卡片额度 60）
    pb = Scripted(gap_mode)
    mb = legacy(tmp_path, pb, "b.sqlite")
    two_threads(mb)
    add_gap_session(mb)
    mb.run()
    base_texts = [c["text"] for c in pb.calls]
    base_checks = [c["bytes"] for c in mb.send_checks]
    # 升级：旧配置封好、新配置（卡片额度 16000，尺子 2）执行
    pu = Scripted(gap_mode)
    mu = legacy(tmp_path, pu, "u.sqlite")
    two_threads(mu)
    add_gap_session(mu)
    mu.close()
    pu2 = Scripted(gap_mode)
    mu2 = new(tmp_path, pu2, "u.sqlite")
    snap = json.loads(mu2.store.get_step("w", "step-0003")["config_snapshot"])
    assert snap["light"]["cards_tokens"] == 60 and "ruler_version" not in snap          # 封步时绑定的卡片额度是 60，旧尺子
    mu2.run()
    assert [c["text"] for c in pu2.calls] == base_texts[-len(pu2.calls):]               # 请求文字和基线逐字相同：用的是绑定的配置，不是现在的
    assert [c["bytes"] for c in mu2.send_checks] == base_checks[-len(mu2.send_checks):]  # 核的字节数也相同
    assert all(c["blocked"] is False for c in mu2.send_checks)
    # 预算挡下时，续跑还是用绑定的配置
    pc = Scripted(gap_mode)
    mc = legacy(tmp_path, pc, "c.sqlite")
    two_threads(mc)
    add_gap_session(mc)
    mc.close()
    pc2 = Scripted(gap_mode)
    mc2 = new(tmp_path, pc2, "c.sqlite")
    mc2.cfg.daily_cap_cny = 0.0001                                                      # 几乎没有预算：第一问就挡
    r = mc2.run()
    assert r["status"] == "paused" and pc2.calls == [] and mc2.store.list_judgments("w", "step-0003") == []
    mc2.clock.advance(days=1)
    mc2.cfg.daily_cap_cny = BIG
    assert mc2.run()["done"] == 1
    assert [c["text"] for c in pc2.calls] == base_texts[-len(pc2.calls):]               # 续跑沿用封步时绑定的配置


def test_cap_not_set_means_no_check_and_no_cost_estimate(tmp_path):
    mem = Memory.open(str(tmp_path / "n.sqlite"), "w", config=Config(), provider=Scripted("skip"), clock=Clock(), spawn="off")
    mem.record("h", "S1", "u1", "user", "你好")
    mem.record("h", "S1", "a1", "assistant", "你好呀")
    mem.end_session("h", "S1")
    assert mem.run()["done"] == 1
    assert mem.send_checks and mem.send_checks[0]["worst_cny"] is None and mem.send_checks[0]["blocked"] is False


def test_budget_blocked_exception_is_part_of_the_ask_contract():
    assert issubclass(BudgetBlocked, Exception)


# ---------------------------------------------------------------- Codex 审查 S4：开整理前的整步粗估只是提示，不拦（不绕开粗估，走真实入口）
def test_step_estimate_is_only_a_hint_a_short_first_request_can_be_sent_under_a_small_daily_cap(tmp_path):
    prov = Scripted("skip")
    mem = Memory.open(str(tmp_path / "s4.sqlite"), "w", config=Config(daily_cap_cny=4.0), provider=prov, clock=Clock(), spawn="off")      # 4 元：粗估（约 4.1 元）超过它，但真实的第一问很短
    mem.record("h", "S1", "u1", "user", "hi")
    mem.record("h", "S1", "a1", "assistant", "ok")
    mem.end_session("h", "S1")
    step = mem.pending_steps()[0]
    assert mem.worst_step_cny(step) > 4.0                                           # 粗估确实超过上限（没有被替换）
    r = mem.run()
    assert r["done"] == 1 and len(prov.calls) == 1                                  # 但没有被它拦住：第一问按实际字节核过、放行
    assert r["step_estimate_hint"]["over_cap"] is True
    assert mem.send_checks and mem.send_checks[0]["blocked"] is False


# ---------------------------------------------------------------- Codex 审查 S5：核的字节数 ＝ 最终请求体字节（JSON 转义算进去），没有固定余量
def test_checked_bytes_equal_the_final_request_body_bytes_with_many_quotes_and_backslashes(tmp_path):
    from yuanwei import provider as PV
    prov = Scripted("skip")
    mem = open_mem(tmp_path, prov)
    nasty = "A" + '"' * 700 + "\\" * 300 + "B"
    mem.record("h", "S1", "u1", "user", nasty)
    mem.record("h", "S1", "a1", "assistant", "ok")
    mem.end_session("h", "S1")
    mem.run()
    text = prov.calls[0]["text"]
    body = PV.write_request_bytes(text, mem.cfg.provider["params"])
    assert mem.send_checks[0]["bytes"] == len(body)                                  # 等于实际请求体字节
    assert len(body) > len(text.encode("utf-8"))                                     # 而且比「正文 UTF-8」大（转义），不是同一个量
