"""0.1.0a10 对照用（清单第 56 条第 10 节）：在一个临时目录里照固定剧本跑一遍 Hermes 接头，把「今天记得对的」各种轮次存下的东西打成一份 JSON：
普通轮、长句、密钥、提问卡、机器行、没登记、机器人、带图、口令、改口、插话、删除（申请＋确认）、删除之后的普通轮、压缩换号（轮与轮之间、一轮当中）。
test_fix56_parity.py 用两套源码（0.1.0a9 的 91ee251 和现在的）各跑一次，逐字节比。不连网，不碰真实的 ~/.hermes。用法：python3 a9_parity_script.py <src 目录> <tests 目录> [变体]
变体（审后修补第五节的第二遍）：默认 cli＝第一遍；weixin-user、desktop-user＝换平台（微信一对一、本人已登记；桌面）、装假宿主模块、当前事件是用户的话（origin＝user），
initialize 带 resumed=True、message_count=4（库里这时没有关着的窗口），另加口令开、大白话关两轮；weixin-none、desktop-none＝同样装假宿主模块、但没有当前事件（origin＝None）。
来源说明、技能命令、待办快照、重新生成这些本来就要变的不放进剧本。"""
import json
import os
import re
import sys
import tempfile

src, tests = sys.argv[1], sys.argv[2]
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
sys.path[:0] = [src, tests]
variant = sys.argv[3] if len(sys.argv) > 3 else "cli"
PLATFORM, _, ORIGIN_MODE = variant.partition("-")                       # cli／weixin／desktop；""／user／none
AUTHOR = "u1" if PLATFORM == "weixin" else None

from helpers2 import organized                                   # noqa: E402
from hermes_clarify_contract import make_result                  # noqa: E402
import hermes_midturn_contract as C                              # noqa: E402

from yuanwei import hosts as H                                   # noqa: E402
from yuanwei.access import Access                                # noqa: E402
from yuanwei.hermes_adapter import HermesAdapter                 # noqa: E402
from yuanwei.idsource import InProcessIdentity                   # noqa: E402
from yuanwei.tools import ToolSurface                            # noqa: E402

os.makedirs(tmp + "/m", exist_ok=True)
mem = organized(__import__("pathlib").Path(tmp + "/m"), host="hermes")
home = tmp + "/home"
VAR = None
if ORIGIN_MODE:
    import fix56_fakehost as FH                                          # noqa: E402

    class _MP:                                                           # 只有 FH.install 用到的 setitem；直接往 sys.modules 里放假的宿主模块
        @staticmethod
        def setitem(d, k, v):
            d[k] = v
    VAR = FH.install(_MP)
    if PLATFORM == "desktop":                                            # 桌面带登录身份：登录身份要等于宿主主人的系统身份才算主人
        import types
        oi = types.ModuleType("gateway.owner_identity")
        oi.host_owner_subject = lambda: "u1"
        sys.modules["gateway.owner_identity"] = oi
        sys.modules["gateway"].owner_identity = oi
cfg = {"mode": "tools_only", "spawn": "off"}
init = {"platform": PLATFORM, "agent_context": "primary", "hermes_home": home}
if PLATFORM == "weixin":
    cfg["owners"] = {"weixin": ["u1"]}
    init.update(user_id="u1", chat_type="dm", chat_id="c1")
elif PLATFORM == "desktop":
    init.update(user_id="u1")
a = HermesAdapter(cfg, memory=mem, executor="inline")
a.initialize("hs1", **dict(init, **({"resumed": True, "message_count": 4} if ORIGIN_MODE else {})))
OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]


def turn_on(ad, n, text, **kw):
    kw.setdefault("author_id", AUTHOR)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    if VAR is not None and ORIGIN_MODE == "user":
        VAR.set(FH.user_event(text))                                     # 宿主说这一轮是用户开的（origin＝user）；none 变体不放事件（origin＝None）
    ad.on_turn_start(n, text, **kw)


def turn(n, text, **kw):
    turn_on(a, n, text, **kw)


def plain(n, text, reply, extra=()):
    turn(n, text)
    a.sync_turn(text, reply, session_id="hs1", messages=OLD + list(extra) + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}])


plain(1, "我想吃烤鱼。今天去哪家？", "去老地方吧")
plain(2, "很长的一句。" * 400, "好长")
plain(3, "我的 key 是 sk-abcdefghijklmnopqrstuvwx1234567890", "已遮")
card = make_result([{"question": "几点？", "choices": ["六点", "七点"]}, {"question": "哪家？"}], {"q0": "七点", "q1": "靠窗那家"})
turn(4, "帮我订个餐厅")
a.sync_turn("帮我订个餐厅", "订好了", session_id="hs1", messages=OLD + [
    {"role": "user", "content": "帮我订个餐厅"}, {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "clarify", "arguments": "{}"}}]},
    {"role": "tool", "content": card, "tool_call_id": "c1", "name": "clarify", "tool_name": "clarify"}, {"role": "assistant", "content": "订好了"}])
turn(5, "后台完成")
a.sync_turn("后台完成", "知道了", session_id="hs1", messages=OLD + [{"role": "user", "content": "后台完成", "display_kind": "process_complete"}])
a.sync_turn("没登记过", "回复", session_id="hs1", messages=OLD + [{"role": "user", "content": "没登记过"}])
turn(6, "机器人说的", author_is_bot=True)
a.sync_turn("机器人说的", "回复", session_id="hs1", messages=OLD + [{"role": "user", "content": "机器人说的"}], turn_author={"id": "bot:x", "is_bot": True})
text = "看图"
row = [{"type": "text", "text": text + "\n\n[Image attached at: /synthetic/x.png]"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]
turn(7, text + "\n\n[Image attached at: /synthetic/x.png]")
a.sync_turn(text + "\n\n[Image attached at: /synthetic/x.png]", "看到了", session_id="hs1", messages=OLD + [{"role": "user", "content": row}])
turn(8, "#yuanwei-nomem")
a.sync_turn("#yuanwei-nomem", "好", session_id="hs1", messages=OLD + [{"role": "user", "content": "#yuanwei-nomem"}])
if ORIGIN_MODE:                                                          # 第二遍多两轮：口令开、大白话关（回到关着，后面的剧本和第一遍一样）
    plain(81, "#yuanwei-mem\n我回来了，推荐点吃的", "好的")
    plain(82, "这次别用记忆，随便聊聊", "好的")

# ---- 改口（redirect）：开头一句 → 工具 → 用户又发一句；插话（steer）
opening = "帮我查一下周五的天气"
m = list(OLD) + [{"role": "user", "content": opening}]
m += [{"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]}, {"role": "tool", "content": "工具输出", "tool_call_id": "r1", "name": "terminal"}]
user = C.begin_iteration(m, opening, "不对，改成周六的")
m.append({"role": "assistant", "content": "周六多云"})
turn(9, opening)
a.sync_turn(user, "周六多云", session_id="hs1", messages=m)
opening2 = "再帮我看一下路况"
m = list(OLD) + [{"role": "user", "content": opening2}]
m += [{"role": "assistant", "content": "", "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]}, {"role": "tool", "content": "工具输出", "tool_call_id": "s1", "name": "terminal"}]
m.append(C.steer_row(C.steer_join(["顺便看看高架"])))
m.append({"role": "assistant", "content": "高架正常"})
turn(10, opening2)
a.sync_turn(opening2, "高架正常", session_id="hs1", messages=m)
plain(11, "再来一句普通的", "好的")

# ---- 库里已经有窗口关着（上面第 8 轮的口令）：没有历史的全新对话照旧从「开」开始，`/new`（reset）照旧新窗口用默认值
b = HermesAdapter(cfg, memory=mem, executor="inline")
b.initialize("hs-new", **init)
turn_on(b, 1, "全新对话的第一句")
b.sync_turn("全新对话的第一句", "你好", session_id="hs-new", messages=OLD + [{"role": "user", "content": "全新对话的第一句"}, {"role": "assistant", "content": "你好"}])
b.on_session_switch("hs-reset", parent_session_id="hs-new", reset=True)
turn_on(b, 2, "重置以后的一句")
b.sync_turn("重置以后的一句", "好", session_id="hs-reset", messages=OLD + [{"role": "user", "content": "重置以后的一句"}, {"role": "assistant", "content": "好"}])
new_chat_read = json.loads(b.handle_tool_call("recall", {"query": "烤鱼"}))["status"]

# ---- 压缩换号：轮与轮之间（本机平台，同一窗口、同一轮不继承），再在一轮当中
a.on_session_switch("hs1c", parent_session_id="hs1", reason="compression")


def plain2(n, text, reply, sid):
    turn(n, text)
    a.sync_turn(text, reply, session_id=sid, messages=OLD + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}])


plain2(12, "压缩以后的第一句", "收到", "hs1c")
turn(13, "这一轮当中发生压缩")
a.on_session_switch("hs1d", parent_session_id="hs1c", reason="compression")
a.sync_turn("这一轮当中发生压缩", "还在这一轮", session_id="hs1d", messages=OLD + [{"role": "user", "content": "这一轮当中发生压缩"}, {"role": "assistant", "content": "还在这一轮"}])
plain2(14, "压缩之后的普通一句", "好", "hs1d")

c = mem.store.conn
req = {}


def dump():
    def q(sql):
        return [list(r) for r in c.execute(sql)]


    RND = re.compile(r"t-[0-9a-f]{16}")                                   # 宿主没登记的那一句（没有开轮记录）用的随机本地令牌：每次运行不同，换成占位再比
    msgs = q("SELECT host, session_id, branch, host_msg_id, seq, role, content, time_precision, fingerprint, is_long, step_id, msg_id FROM messages ORDER BY seq")
    msg_of = {r[11]: i for i, r in enumerate(msgs)}
    for r in msgs:
        if RND.search(r[3]):
            r[3], r[8], r[11] = RND.sub("t-?", r[3]), "?", "?"                  # 指纹和编号是照随机令牌算的，只比其余各列
    anchors = [[r["ord"], r["text"], r["start"], r["end"]] for r in c.execute("SELECT * FROM anchors ORDER BY seq, ord")]


    def nk(k):
        return "x?" if re.fullmatch(r"x[0-9a-f]{14}", k) else k              # 没有事件键的同步补出来的键（随机）


    tok_key = {r[0]: [r[1], r[2], nk(r[3])] for r in c.execute("SELECT turn_id, session_id, branch, event_key FROM turn_tokens")}
    tokens = sorted(tok_key.values())
    bound = sorted([tok_key.get(r[0], ["?"]) + [msg_of.get(r[1], -1)] for r in c.execute("SELECT turn_id, msg_id FROM turn_messages")], key=json.dumps)
    turns = [(tok_key.get(r[0], ["?"]) + list(r[1:])) for r in c.execute("SELECT turn_id, session_id, gen, state, origin, refused, refuse_reason, persistent FROM turns ORDER BY session_id, gen")]
    read_log = [[r[0], re.sub(r"[0-9a-f]{12,}", "H", r[1])] for r in c.execute("SELECT kind, detail FROM read_log ORDER BY id")]
    st = a.status()
    ids = InProcessIdentity()
    acc = Access(mem)
    sf = ToolSurface(mem, acc, H.profile("hermes"), ids)
    tok = acc.turn_token("hermes", "s2", "1:abc")
    acc.begin_turn("hermes", "s2", tok, text="问状态", origin="user")
    ids.set("hermes", "s2", "", tok)
    res = sf.call("status", {})
    status_obj = json.loads(res.text.split("\n", 1)[1])
    out = {"rows": msgs, "anchors": anchors, "token_keys": tokens, "turn_messages": bound, "turns": turns,
           "skipped": st["skipped"], "recorded_turns": st["recorded_turns"], "clarify": st["clarify"], "window": {k: st["window"][k] for k in ("state", "control_version", "paused", "pause_reason", "bound_via")},
           "windows": q("SELECT state, control_version, ctl_source, origin, host FROM windows ORDER BY created_at, rowid"),
           "bindings": q("SELECT host, session_id, branch, bound_via, host_parent_session_id FROM window_bindings ORDER BY session_id"),
           "window_ops": q("SELECT kind, source, outcome, reason FROM window_ops ORDER BY rowid"),
           "no_record": q("SELECT reason FROM no_record_turns ORDER BY rowid"), "deletions": q("SELECT conv_kind, source, state, counts FROM deletions ORDER BY rowid"),
           "delete_pending": q("SELECT state, void_reason FROM delete_pending ORDER BY rowid"), "deleted_steps": q("SELECT k FROM deleted_steps ORDER BY k"),
           "new_chat_read": new_chat_read, "delete_request": {k: v for k, v in req.items() if k not in ("window_id", "del_id", "pending_id")},
           "read_log": read_log,
           "mcp_status": {k: v for k, v in status_obj.items() if k not in ("cost_today",)}, "mcp_status_keys": sorted(status_obj)}
    return out


if ORIGIN_MODE and hasattr(a, "_turn_facts"):                            # 新源码上自检：假宿主模块真起作用了（旧源码没有这个方法，跳过；这里只断言、不输出）
    VAR.set(FH.user_event("自检") if ORIGIN_MODE == "user" else None)
    assert a._turn_facts("自检")[0] == ("user" if ORIGIN_MODE == "user" else None), ORIGIN_MODE
    VAR.set(None)

before_delete = dump()                                                  # 删除会把这一整段对话的原话和键都清掉（键换成假名），所以先拍一张

# ---- 删除：agent 申请、用户整条回复「确认删除」；确认那一轮不记；删除之后的普通轮照常记
turn(15, "请把这段对话在原委里的记忆删掉")
a.sync_turn("请把这段对话在原委里的记忆删掉", "好，我来申请", session_id="hs1d", messages=OLD + [{"role": "user", "content": "请把这段对话在原委里的记忆删掉"}, {"role": "assistant", "content": "好，我来申请"}])
req = json.loads(a.handle_tool_call("window_delete", {}))
turn(16, "确认删除")
a.sync_turn("确认删除", "已删除", session_id="hs1d", messages=OLD + [{"role": "user", "content": "确认删除"}, {"role": "assistant", "content": "已删除"}])
plain2(17, "删除以后的普通一句", "好", "hs1d")
turn(18, "收尾")


after_delete = dump()
print(json.dumps({"before_delete": before_delete, "after_delete": after_delete}, ensure_ascii=False, sort_keys=True))
