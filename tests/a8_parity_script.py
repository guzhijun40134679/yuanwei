"""0.1.0a9 对照用：在一个临时目录里照固定剧本跑一遍 Hermes 接头（没有改口、没有插话的轮次），把库里存下的东西、锚点、跳过原因、计数、MCP 式 status 打成一份 JSON。
test_fix55_parity.py 用两套源码（0.1.0a8 的 21b88cf 和现在的）各跑一次，逐字节比。不连网，不碰真实的 ~/.hermes。用法：python3 a8_parity_script.py <src 目录> <tests 目录>"""
import json
import os
import sys
import tempfile

src, tests = sys.argv[1], sys.argv[2]
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
sys.path[:0] = [src, tests]

from helpers2 import organized                                   # noqa: E402
from hermes_clarify_contract import make_result                  # noqa: E402

from yuanwei import hosts as H                                   # noqa: E402
from yuanwei.access import Access                                # noqa: E402
from yuanwei.hermes_adapter import HermesAdapter                 # noqa: E402
from yuanwei.idsource import InProcessIdentity                   # noqa: E402
from yuanwei.tools import ToolSurface                            # noqa: E402

os.makedirs(tmp + "/m", exist_ok=True)
mem = organized(__import__("pathlib").Path(tmp + "/m"), host="hermes")
home = tmp + "/home"
a = HermesAdapter({"mode": "tools_only", "spawn": "off"}, memory=mem, executor="inline")
a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=home)
OLD = [{"role": "user", "content": "老的一轮"}, {"role": "assistant", "content": "好的"}]


def turn(n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


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
turn(9, "收尾")

c = mem.store.conn
turn_of = {r[0]: r[1] for r in c.execute("SELECT msg_id, turn_id FROM turn_messages")}
rows = [[r["role"], r["content"], r["is_long"], r["redacted"], r["host_msg_id"].endswith("-u"), r["turn"] is not None if "turn" in r.keys() else (r["msg_id"] in turn_of), r["session_id"]]
        for r in c.execute("SELECT * FROM messages ORDER BY seq")]
anchors = [[r["ord"], r["text"], r["start"], r["end"]] for r in c.execute("SELECT * FROM anchors ORDER BY seq, ord")]
st = a.status()
ids = InProcessIdentity()
acc = Access(mem)
sf = ToolSurface(mem, acc, H.profile("hermes"), ids)
tok = acc.turn_token("hermes", "s2", "1:abc")
acc.begin_turn("hermes", "s2", tok, text="问状态", origin="user")
ids.set("hermes", "s2", "", tok)
res = sf.call("status", {})
status_obj = json.loads(res.text.split("\n", 1)[1])
out = {"rows": rows, "anchors": anchors, "skipped": st["skipped"], "recorded_turns": st["recorded_turns"], "clarify": st["clarify"],
       "window_ops": [list(r) for r in c.execute("SELECT kind, source, outcome FROM window_ops ORDER BY rowid")],
       "turn_states": [list(r) for r in c.execute("SELECT state, refused FROM turns ORDER BY gen")],
       "mcp_status": {k: v for k, v in status_obj.items() if k not in ("cost_today",)}, "mcp_status_keys": sorted(status_obj)}
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
