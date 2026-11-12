"""测试用小工具：自造数据、假提供方、可控的时钟。不碰研究数据。"""
import datetime as dt
import json
import re

from yuanwei import Config, Memory

SID_RE = re.compile(r"^(s\d+)（", re.M)


class Clock:
    def __init__(self, start="2027-01-01T08:00:00Z"):
        self.t = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))

    def __call__(self):
        return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)

    def tick(self):
        self.t += dt.timedelta(seconds=1)


def sids_of(text):
    return SID_RE.findall(text.split("这次对话：", 1)[-1])


def out_skip(sids):
    return json.dumps({"新建": {"主线": [], "回": []}, "去处": [{"句": sids, "不记": True}], "缺口": [], "改摘要": []}, ensure_ascii=False)


def out_thread(sids, name="测试主线"):
    return json.dumps({"新建": {"主线": [{"代号": "T1", "名称": name, "说明": "测试用的主线说明"}], "回": [{"代号": "N1", "主线": "T1", "标题": "测试这一回", "摘要": "测试摘要"}]},
                       "去处": [{"句": sids, "挂": [{"回": "N1", "把握": "确定"}]}], "缺口": [], "改摘要": []}, ensure_ascii=False)


class Scripted:
    """假提供方：mode 是 skip／thread／garbage，或一个函数 (request, call_no) → 文字。记录每次调用。"""

    def __init__(self, mode="skip", usage=True):
        self.mode, self.calls, self.usage = mode, [], usage

    def __call__(self, request):
        self.calls.append({"step_id": request.get("step_id"), "question": request.get("question"), "text": request["text"]})
        n = len(self.calls)
        if callable(self.mode):
            raw = self.mode(request, n)
        elif self.mode == "garbage":
            raw = "这不是 JSON"
        elif self.mode == "thread":
            raw = out_thread(sids_of(request["text"]))
        else:
            raw = out_skip(sids_of(request["text"]))
        if not self.usage:
            return raw
        return {"text": raw, "model": "fake-model", "fingerprint": "fp-test", "usage": {"prompt_cache_miss_tokens": 1000, "prompt_cache_hit_tokens": 0, "completion_tokens": 1000}}


def make_mem(tmp_path, ws="w", provider=None, clock=None, name="m.sqlite", spawn="off", **cfg):
    cfg.setdefault("index_version", 2)
    return Memory.open(str(tmp_path / name), ws, config=Config(**cfg), provider=provider, clock=clock or Clock(), spawn=spawn)


def say(mem, session, n, host="h", date="2027-01-01", prefix="", role_cycle=("user", "assistant"), start=1):
    """在一个会话里记 n 条（用户、助手交替），正文是自造的。返回消息编号列表。"""
    ids = []
    for i in range(start, start + n):
        role = role_cycle[(i - 1) % 2]
        r = mem.record(host, session, "%s-m%03d" % (session, i), role, "%s第%d句话：今天聊聊编号%d的事情。" % (prefix, i, i), time=date)
        ids.append(r["msg_id"])
    return ids


def make_old_db(path, version, ns="w", host="h", session="S", host_msg_id="m1", content="旧库里的一句话", msg_time="2027-01-01", deny_read=False):
    """造一个指定版本（v1…v10）的旧库：只用 MIGRATIONS 里这个版本以前的语句，再直接插一条原话（和锚点）。v11 起的列、表这时候还没有。"""
    import sqlite3
    from yuanwei import anchors as AN
    from yuanwei import contract as WC
    from yuanwei.sqlstore import MIGRATIONS
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    c.execute("INSERT INTO schema_meta VALUES (?)", (version,))
    for v in range(1, version + 1):
        for s in MIGRATIONS[v]:
            c.execute(s)
    c.execute("INSERT INTO workspaces (ns, created_at, store_kind, store_version, index_version, last_seq, config) VALUES (?,?,?,?,?,?,?)", (ns, "2027-01-01T08:00:00Z", "L", 1, 2, 1, "{}"))
    msg_id = "m-" + WC.sha256_hex("|".join((host, session, "", host_msg_id)))[:12]
    m = WC.make_message(ns, session, 1, msg_id, "user", content, msg_time, "2027-01-01T08:00:00Z")
    c.execute("INSERT INTO messages (ns,msg_id,host,session_id,branch,host_msg_id,seq,role,content,msg_time,time_precision,received_at,fingerprint,is_long) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (ns, msg_id, host, session, "", host_msg_id, 1, "user", content, msg_time, "day", "2027-01-01T08:00:00Z", m["fingerprint"], 0))
    for a in AN.anchors_for(msg_id, 1, content, 2):
        c.execute("INSERT INTO anchors (ns,anchor_id,msg_id,ord,seq,text,start,end,index_version) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ns, a["anchor_id"], msg_id, a["ord"], 1, a["text"], a["start"], a["end"], a["index_version"]))
    if deny_read:
        c.execute("INSERT INTO read_settings VALUES (?,?,?,?,?)", (ns, "workspace", "", 0, None))
    c.close()
