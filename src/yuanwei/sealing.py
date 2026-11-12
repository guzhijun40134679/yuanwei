"""封步：把一串没封的原话切成「步」。纯函数，不碰数据库。

规则（暂定的工程参数，效果在开发复验里看）：
  * 取最先到的：会话结束、空闲 N 分钟、攒到 M 条消息或 C 字；
  * 在轮次边界封：一轮＝一条用户话加它后面的助手回复；到阈值时等这一轮的回复到了再封；一轮自己就超阈值，单独成一步；
  * 没有最终回复的轮次：收到明确的中止或会话关闭事件，保留已经拿到的真实原话，按中止的轮次封，不补回复；
  * 不跨会话、不跨宿主、不跨分支（调用方按 (宿主, 会话, 分支) 分好组再传进来）。
"""
import datetime as dt


def parse_ts(s):
    """ISO 时间 → UTC 的 datetime；认 Z 结尾。读不了抛 ValueError。"""
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def build_turns(msgs, ended_at_seq=None, aborted_at_seq=None):
    """把按 seq 排好的消息分成轮次。每轮 {"msgs","complete","aborted"}。
    complete：这一轮后面已经有助手回复（或下一轮的用户话已经来了）；ended_at_seq：会话关闭事件的位置，之前的消息都算关闭，最后一轮没回复也按中止封；
    aborted_at_seq：中止事件的位置，之前的未完成轮次按中止封。"""
    turns = []
    for m in msgs:
        if m["role"] == "user" or not turns:
            turns.append({"msgs": [m], "has_reply": m["role"] == "assistant"})
        else:
            turns[-1]["msgs"].append(m)
            if m["role"] == "assistant":
                turns[-1]["has_reply"] = True
    out = []
    for i, t in enumerate(turns):
        last_seq = t["msgs"][-1]["seq"]
        nxt = i + 1 < len(turns)
        closed_by_event = (ended_at_seq is not None and last_seq <= ended_at_seq) or (aborted_at_seq is not None and last_seq <= aborted_at_seq)
        complete = t["has_reply"] or nxt or closed_by_event
        out.append({"msgs": t["msgs"], "complete": bool(complete), "aborted": bool(complete and not t["has_reply"]) })
    return out


def _size(turn):
    return len(turn["msgs"]), sum(len(m["content"]) for m in turn["msgs"])


def pack_turns(turns, max_messages, max_chars, final):
    """把完整的轮次装进步。返回 (步的列表（每步是轮次列表）, 剩下没封的轮次)。
    一轮自己就达到阈值：先把前面攒着的封掉，这一轮单独成一步；攒到阈值：这一轮之后封。final＝True（会话结束／空闲到期）：剩下的也封。"""
    steps, cur = [], []

    def totals(ts):
        n = sum(_size(t)[0] for t in ts)
        c = sum(_size(t)[1] for t in ts)
        return n, c
    for t in turns:
        n, c = _size(t)
        if n >= max_messages or c >= max_chars:
            if cur:
                steps.append(cur)
                cur = []
            steps.append([t])
            continue
        cur.append(t)
        tn, tc = totals(cur)
        if tn >= max_messages or tc >= max_chars:
            steps.append(cur)
            cur = []
    if final and cur:
        steps.append(cur)
        cur = []
    return steps, cur
