"""读取权限的绑定层（第二段·交回点甲）：把「这次读取属于哪个会话、哪一轮、许不许读」做成显式绑定，不靠第一段的 current_turn（它每个工作区只有一行）。

三类调用方，各管各的：
* **登记方（可信）**：接头在宿主的真实事件上调 begin_turn／continue_turn／close_turn／deny_turn／register_connection。参数来自宿主的字段，不来自模型。
* **读取方**：MCP 服务、插件工具在每次读取前后调 start_read／mark_search／mark_vector／assembled／finish_read。身份（ToolIdentity）只能由可信的身份源给
  （连接句柄、同进程的插件状态），模型填的参数一律不采信，也不能用来解除禁读。
* **回执**：拒绝时按本轮的读取登记（read_log）选四种措辞之一，不靠函数返回就认定宿主收到了。

登记表 turns 的键：（工作区、宿主、会话、分支、轮次）；每个（工作区、宿主、会话、分支）里 gen 递增，当前轮＝gen 最大的那一行。
各会话都从 1 开始的计数不是全库的轮次编号。迟到的调用、迟到的读取结果、重复的事件、来源不明的事件，都不会把旧轮当成当前轮，也不会清掉已经成立的拒绝。
"""
import dataclasses
import hashlib
import json
import secrets
import threading
import uuid

from . import control as CT
from . import gate as GT
from . import hosts as H
from . import window as WN
from .errors import YuanweiError
from .memory import PERSIST_HINT

W1 = "本轮未新增读取长期记忆"
W2 = "已停止后续读取；此前已有材料交入本轮"
W3 = "已停止后续读取；本轮此前已查询，材料未交出／交付情况无法确认"
W4 = "无法确认权限，已暂停新增读取"
TAIL_KNOWN = "本次对话里说过的话、此前已经交给你的内容仍在上下文里，不能说成已经撤回；这只管本插件的读取入口。"
TAIL_UNKNOWN = "没有查到可信的会话或轮次登记，所以没有新读；这只管本插件的读取入口。"
TAIL_CLOSED = "连接已关闭，本次结果未交付；此前已经读取或交付的内容不会因此撤回。这只管本插件的读取入口。"

# 控制已经成立的原因（回执按读取登记在 W1／W2／W3 里选）；其余原因都是身份或权限不明（W4）
KNOWN_CONTROL = frozenset(["control_nomem", "control_malformed", "clause_ban", "host_marker", "workspace_setting", "session_setting", "control_changed", "window_closed"])
# 回执按本轮读取登记选措辞的原因：控制已成立的，加上「连接已关闭」（那一次读取查过什么就照实说，但这不是用户的控制，文字尾巴用「没有可信登记」那句）
STATE_RECEIPT = KNOWN_CONTROL | {"connection_closed"}


class ConnectionRefused(YuanweiError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class ToolIdentity:
    """一次读取的身份。只能由可信的身份源在**调用发起、排队之前**构造并冻结：turn_id 只有宿主确实给了可信的轮次标记（同进程插件的轮次、逐轮的连接能力）才有；
    没有就是 None，读取被拒（turn_unproven）——不再按「最新一轮」算，否则迟到的旧调用会借新一轮的许可。conn 是连接能力的哈希（有的话，读取开始和返回前都要核它没关）。"""
    host: str
    session_id: str
    branch: str = ""
    turn_id: str = None
    conn: str = None


class Ticket:
    def __init__(self, call_id, ident, gen, entry):
        self.call_id, self.ident, self.gen, self.entry = call_id, ident, gen, entry
        self.searched = False
        self.vector_calls = 0
        self.assembled_fp = None
        self.timed_out = False
        self.del_ver = None                   # 读取开始时这个工作区的删除版本：返回前再核


def _sha16(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class Access:
    def __init__(self, mem, text_assist=True):
        self.mem = mem
        self.store = mem.store
        self.ns = mem.ns
        self.text_assist = text_assist
        self.fault = None                     # 测试注入：fault(point)，point ∈ between_open_and_complete
        self.windows = None                   # WindowService（禁读按窗口）：接头装上才有；没装就不核窗口，行为和以前一字不差

    # ------------------------------------------------------------------ 小件
    def _now(self):
        return self.mem.clock()

    @staticmethod
    def _need_str(name, v, allow_empty=False):
        if not isinstance(v, str) or (not v and not allow_empty):
            raise ValueError("%s 必须是%s文字" % (name, "" if allow_empty else "非空"))
        return v

    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        d["refused"] = bool(d["refused"])
        d["persistent"] = bool(d["persistent"])
        return d

    def current_turn(self, host, session_id, branch=""):
        r = self.store.conn.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? ORDER BY gen DESC LIMIT 1", (self.ns, host, session_id, branch)).fetchone()
        return self._row(r)

    def _turn(self, host, session_id, branch, turn_id):
        r = self.store.conn.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
        return self._row(r)

    # ------------------------------------------------------------------ 读取日志
    def _log(self, host, session_id, branch, turn_id, gen, kind, detail, call_id=None, entry=None, store=None):
        st = store or self.store
        with st.tx() as c:
            c.execute("INSERT INTO read_log (ns, host, session_id, branch, turn_id, gen, call_id, entry, kind, detail, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (self.ns, host, session_id, branch, turn_id, gen, call_id, entry, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True), self._now()))

    def _log_ticket(self, t, kind, detail, store=None):
        self._log(t.ident.host, t.ident.session_id, t.ident.branch, t.ident.turn_id or self._turn_of_gen(t), t.gen, kind, detail, t.call_id, t.entry, store)

    def _turn_of_gen(self, t):
        r = self.store.conn.execute("SELECT turn_id FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND gen=?", (self.ns, t.ident.host, t.ident.session_id, t.ident.branch, t.gen)).fetchone()
        return r["turn_id"] if r else ""

    def log_rows(self, host, session_id, turn_id, branch=""):
        out = []
        for r in self.store.conn.execute("SELECT * FROM read_log WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? ORDER BY id", (self.ns, host, session_id, branch, turn_id)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ 登记方：轮次
    def open_turn(self, host, session_id, turn_id, branch=""):
        """第一步：登记「有这一轮」，状态是 registering（还没完成）。读取在 complete_turn 之前一律被拒。同键重送：原样返回，不改任何东西。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id), self._need_str("branch", branch, True)
        with self.store.tx() as c:
            row = self._turn(host, session_id, branch, turn_id)
            if row is not None:
                cur = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()["g"]
                return {"created": False, "stale": row["gen"] < cur, "turn": row}
            g = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()["g"]
            gen = (g or 0) + 1
            now = self._now()
            c.execute("UPDATE turns SET state='closed', closed_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND state IN ('open','registering')", (now, self.ns, host, session_id, branch))
            c.execute("INSERT INTO turns (ns, host, session_id, branch, turn_id, gen, state, origin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.ns, host, session_id, branch, turn_id, gen, "registering", "user", now))
        return {"created": True, "stale": False, "turn": self._turn(host, session_id, branch, turn_id)}

    def _control_of(self, text):
        """解析这一条用户输入里的控制：固定口令（只认第一物理行）和文字识别的禁读。只返回元数据，不存原话。"""
        ctl, refused, reason, persistent = CT.Control("none", None, None), False, None, False
        if text is not None:
            ctl = CT.parse(text)
            if ctl.kind == "nomem":
                refused, reason = True, "control_nomem"
            elif ctl.kind == "malformed":
                refused, reason = True, "control_malformed"
            elif self.text_assist:
                tp = GT.text_permission(text) if text.strip() else {"allowed": True, "persistent": False}
                if not tp["allowed"]:
                    refused, reason, persistent = True, "clause_ban", bool(tp["persistent"])
        return ctl, refused, reason, persistent

    def complete_turn(self, host, session_id, turn_id, branch="", text=None, origin="user", replay=False):
        """第二步：解析固定口令（只认这条可信用户提交的第一物理行）、记下控制元数据，把这一轮改成 open。不存原话（要不要保存仍走 record）。"""
        if origin not in ("user", "unknown"):
            raise ValueError("origin 只能是 user 或 unknown，实际 %r" % (origin,))
        row = self._turn(host, session_id, branch, turn_id)
        if row is None:
            raise ValueError("没有登记过这一轮：先 open_turn")
        if row["state"] != "registering":
            return {"registered": True, "created": False, "turn": row, "control": {"kind": row["control_kind"] or "none"}, "receipt": None}
        if replay or origin == "unknown":                                  # 未知来源、回放的事件不能完成登记：这一轮停在「登记中」，读取仍被拒；口令文字也不当控制
            self._log(host, session_id, branch, turn_id, row["gen"], "replay_ignored" if replay else "unknown_origin_ignored", {"event_turn_id": turn_id, "via": "complete_turn"})
            return {"registered": False, "created": False, "ignored": "replay" if replay else "origin_unknown", "turn": row, "control": {"kind": "none"}, "receipt": None}
        ctl, refused, reason, persistent = self._control_of(text)
        with self.store.tx() as c:
            cur = c.execute("SELECT state FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
            if cur["state"] == "registering":
                c.execute("UPDATE turns SET state='open', refused=?, refuse_reason=?, persistent=?, control_kind=?, control_span=?, completed_at=? "
                          "WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?",
                          (1 if refused else 0, reason, 1 if persistent else 0, ctl.kind, None if ctl.span is None else "%d-%d" % ctl.span, self._now(), self.ns, host, session_id, branch, turn_id))
        row = self._turn(host, session_id, branch, turn_id)
        receipt = ctl.hint if ctl.kind == "malformed" else (W1 if refused else None)
        return {"registered": True, "created": True, "turn": row, "control": {"kind": ctl.kind, "span": ctl.span, "hint": ctl.hint}, "receipt": receipt}

    def begin_turn(self, host, session_id, turn_id, branch="", text=None, origin="user", replay=False):
        """接头在每条**可信用户提交**事件上调一次（open＋complete）。来源不明、回放历史的事件：不开新一轮、不清掉已有的拒绝、里面的口令文字不算控制，只记一条日志。
        origin 由接头按这家宿主的**真实字段**判断（见 idchain），判断不了就是 unknown。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id), self._need_str("branch", branch, True)
        if origin not in ("user", "unknown"):
            raise ValueError("origin 只能是 user 或 unknown，实际 %r" % (origin,))
        if replay or origin == "unknown":
            cur = self.current_turn(host, session_id, branch)
            self._log(host, session_id, branch, cur["turn_id"] if cur else "", cur["gen"] if cur else 0,
                      "replay_ignored" if replay else "unknown_origin_ignored", {"event_turn_id": turn_id})
            return {"registered": False, "created": False, "ignored": "replay" if replay else "origin_unknown", "turn": cur, "control": {"kind": "none"}, "receipt": None}
        o = self.open_turn(host, session_id, turn_id, branch)
        if not o["created"]:                                              # 重送：不改任何东西（尤其不清掉已经成立的拒绝）；还停在 registering 的重送可以接着完成
            if o["turn"]["state"] == "registering" and not o["stale"]:
                pass
            else:
                return {"registered": True, "created": False, "stale": o["stale"], "turn": o["turn"], "control": {"kind": o["turn"]["control_kind"] or "none"}, "receipt": None}
        if self.fault:
            self.fault("between_open_and_complete")
        r = self.complete_turn(host, session_id, turn_id, branch, text=text, origin=origin, replay=replay)
        r["created"] = o["created"] or r["created"]
        return r

    def continue_turn(self, host, session_id, turn_id, reason, branch=""):
        """同一轮里的压缩、恢复、续跑：继承这一轮的拒绝，不新开一轮。轮次不是当前轮（旧的、没登记的）就不算续跑。"""
        row = self._turn(host, session_id, branch, turn_id)
        cur = self.current_turn(host, session_id, branch)
        if row is None or cur is None or cur["turn_id"] != turn_id:
            return {"continued": False, "turn": row}
        self._log(host, session_id, branch, turn_id, row["gen"], "continued", {"reason": reason})
        return {"continued": True, "turn": row}

    def inherit_turn(self, host, from_session, to_session, turn_id, branch=""):
        """同一轮里会话编号换了（Hermes 压缩时 on_session_switch 给子会话）：把父会话**当前轮**连同拒绝状态带到新会话的键下。
        父会话的旧轮次、不存在的轮次不继承（返回 None）；分叉、新会话不调这个，所以借不到别处的允许。重复继承不新开、不改。"""
        self._need_str("host", host), self._need_str("from_session", from_session), self._need_str("to_session", to_session), self._need_str("turn_id", turn_id)
        with self.store.tx() as c:
            src = c.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? ORDER BY gen DESC LIMIT 1", (self.ns, host, from_session, branch)).fetchone()
            if src is None or src["turn_id"] != turn_id:
                return None
            dup = c.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, to_session, branch, turn_id)).fetchone()
            if dup is not None:
                return self._row(dup)
            g = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, to_session, branch)).fetchone()["g"]
            now = self._now()
            c.execute("UPDATE turns SET state='closed', closed_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND state IN ('open','registering')", (now, self.ns, host, to_session, branch))
            c.execute("INSERT INTO turns (ns, host, session_id, branch, turn_id, gen, state, origin, refused, refuse_reason, persistent, control_kind, control_span, created_at, completed_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.ns, host, to_session, branch, turn_id, (g or 0) + 1, src["state"], src["origin"], src["refused"], src["refuse_reason"], src["persistent"], src["control_kind"], src["control_span"], now, src["completed_at"]))
        return self._turn(host, to_session, branch, turn_id)

    def close_turn(self, host, session_id, turn_id, branch=""):
        with self.store.tx() as c:
            c.execute("UPDATE turns SET state='closed', closed_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND state IN ('open','registering')",
                      (self._now(), self.ns, host, session_id, branch, turn_id))
        return self._turn(host, session_id, branch, turn_id)

    def deny_turn(self, host, session_id, turn_id, branch="", reason="host_marker"):
        """宿主或调用方可信地标记「这一轮不许读」。标了就不能撤。"""
        with self.store.tx() as c:
            n = c.execute("UPDATE turns SET refused=1, refuse_reason=COALESCE(refuse_reason, ?) WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?",
                          (reason, self.ns, host, session_id, branch, turn_id)).rowcount
        if not n:
            raise ValueError("没有登记过这一轮：%r" % (turn_id,))
        return self._turn(host, session_id, branch, turn_id)

    # ------------------------------------------------------------------ 持续设置（带宿主的会话键）
    @staticmethod
    def session_key(host, session_id):
        return host + "\x1f" + session_id

    def set_session_permission(self, host, session_id, allowed):
        self._need_str("host", host), self._need_str("session_id", session_id)
        self.store.set_read_setting(self.ns, "session", self.session_key(host, session_id), bool(allowed), self._now())
        return self.session_permission(host, session_id)

    def clear_session_permission(self, host, session_id):
        return {"cleared": self.store.clear_read_setting(self.ns, "session", self.session_key(host, session_id))}

    def session_permission(self, host, session_id):
        rows = self.store.read_settings(self.ns)
        deny = [r for r in rows if not r["allowed"] and (r["scope"] == "workspace" or (r["scope"] == "session" and r["key"] in (self.session_key(host, session_id), session_id)))]
        return {"allowed": not deny, "denied_by": [{"scope": r["scope"], "key": r["key"].replace("\x1f", ":")} for r in deny]}

    # ------------------------------------------------------------------ 连接句柄
    def register_connection(self, host, session_id, branch="", turn_id=None):
        """可信的启动端（接头）为一个会话登记一个连接，拿到随机句柄；句柄只放在它启动的那个 MCP 进程的环境里。库里只存哈希。
        turn_id 给了就是**逐轮的能力**：这个句柄只证明这一轮，轮次一换就过期（stale_turn）；不给就是会话级句柄，只证明会话、不证明轮次，读取会被拒（turn_unproven，待核）。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("branch", branch, True)
        if turn_id is not None:
            self._need_str("turn_id", turn_id)
        h = secrets.token_hex(16)
        with self.store.tx() as c:
            c.execute("INSERT INTO connections (handle_sha, ns, host, session_id, branch, created_at, turn_id) VALUES (?,?,?,?,?,?,?)",
                      (hashlib.sha256(h.encode()).hexdigest(), self.ns, host, session_id, branch, self._now(), turn_id))
        return h

    def claim_connection(self, handle, claimant):
        """MCP 进程启动时认领句柄：一个句柄只给一个进程用；换一个进程拿同一个句柄、关掉了的句柄、别的工作区的句柄、没登记过的句柄都拒绝。"""
        self._need_str("claimant", claimant)
        sha = hashlib.sha256((handle or "").encode()).hexdigest()
        with self.store.tx() as c:
            r = c.execute("SELECT * FROM connections WHERE handle_sha=? AND ns=?", (sha, self.ns)).fetchone()
            if r is None:
                raise ConnectionRefused("connection_unknown", "连接句柄没有登记过（或不属于这个工作区）")
            if r["closed_at"]:
                raise ConnectionRefused("connection_closed", "这个连接已经关闭")
            if r["claimed_by"] is not None and r["claimed_by"] != claimant:
                raise ConnectionRefused("connection_claimed", "这个连接句柄已经被另一个进程认领")
            if r["claimed_by"] is None:
                c.execute("UPDATE connections SET claimed_by=?, claimed_at=? WHERE handle_sha=?", (claimant, self._now(), sha))
        return ToolIdentity(r["host"], r["session_id"], r["branch"], r["turn_id"], conn=sha)

    def close_connection(self, handle):
        with self.store.tx() as c:
            c.execute("UPDATE connections SET closed_at=? WHERE handle_sha=? AND ns=?", (self._now(), hashlib.sha256((handle or "").encode()).hexdigest(), self.ns))

    def connection_open(self, handle):
        """连接还没关闭、而且工作区对得上。每次读取前核一次（会话结束后连接就不能再读）。"""
        return self._conn_live(hashlib.sha256((handle or "").encode()).hexdigest())

    def _conn_live(self, sha):
        r = self.store.conn.execute("SELECT closed_at FROM connections WHERE handle_sha=? AND ns=?", (sha, self.ns)).fetchone()
        return r is not None and not r["closed_at"]

    # ------------------------------------------------------------------ 接头生成的轮次号：同一个事件重送要复用
    def turn_token(self, host, session_id, event_key, branch=""):
        """宿主没有稳定轮次号的事件（Hermes、Pi），接头用事件自己的稳定键（宿主事件里能认出「就是同一个事件」的那个）换一个轮次号：同一个键永远得到同一个号，
        所以事件重送、进程重启都不会造出新的一轮。不同的键、不同的会话、不同的分支得到不同的号。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("event_key", event_key), self._need_str("branch", branch, True)
        with self.store.tx() as c:
            r = c.execute("SELECT turn_id FROM turn_tokens WHERE ns=? AND host=? AND session_id=? AND branch=? AND event_key=?", (self.ns, host, session_id, branch, event_key)).fetchone()
            if r is not None:
                return r["turn_id"]
            tok = "t-" + secrets.token_hex(8)
            c.execute("INSERT INTO turn_tokens (ns, host, session_id, branch, event_key, turn_id, created_at) VALUES (?,?,?,?,?,?,?)", (self.ns, host, session_id, branch, event_key, tok, self._now()))
        return tok

    # ------------------------------------------------------------------ 持续禁读的回执：每个宿主轮次只送一次
    def claim_receipt(self, host, session_id, branch, turn_id):
        """这一轮的持续禁读回执还没送过就登记一笔并返回 True；已经送过返回 False（同一轮多次钩子、压缩续跑不再刷出第二句）。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id)
        branch = branch or ""
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM read_log WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND kind='receipt_sent'", (self.ns, host, session_id, branch, turn_id)).fetchone():
                return False
            t = c.execute("SELECT gen FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
            c.execute("INSERT INTO read_log (ns, host, session_id, branch, turn_id, gen, call_id, entry, kind, detail, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (self.ns, host, session_id, branch, turn_id, t["gen"] if t else 0, None, None, "receipt_sent", "{}", self._now()))
        return True

    # ------------------------------------------------------------------ 读取：评估、回执、开始、过程登记、结束
    def _evaluate(self, ident):
        """返回 (reason 或 None, 当前轮行或 None)。顺序：工作区设置 → 身份 → 连接没关 → 登记 → 有可信轮次标记 → 轮次对得上 → 本轮控制 → 会话设置。读库出任何错都当权限不明（不猜成允许）。"""
        try:
            rows = self.store.read_settings(self.ns)
            if any(not r["allowed"] and r["scope"] == "workspace" for r in rows):
                cur = None
                if ident is not None and isinstance(ident.host, str) and ident.host and isinstance(ident.session_id, str) and ident.session_id:
                    cur = self.current_turn(ident.host, ident.session_id, ident.branch or "")
                return "workspace_setting", cur
            if ident is None:
                return "identity_unbound", None
            if not (isinstance(ident.host, str) and ident.host and isinstance(ident.session_id, str) and ident.session_id and isinstance(ident.branch or "", str)):
                return "identity_incomplete", None
            branch = ident.branch or ""
            cur = self.current_turn(ident.host, ident.session_id, branch)
            if ident.conn is not None and not self._conn_live(ident.conn):
                return "connection_closed", cur
            if cur is None:
                return "turn_unregistered", None
            if ident.turn_id is None:                                                  # 没有可信的轮次标记：不拿最新一轮顶上（迟到的旧调用会借到新一轮的许可）
                return "turn_unproven", cur
            if ident.turn_id != cur["turn_id"]:
                return "stale_turn", cur
            if cur["state"] == "registering":
                return "registration_incomplete", cur
            if cur["state"] == "closed":
                return "turn_closed", cur
            if cur["refused"]:
                return cur["refuse_reason"] or "host_marker", cur
            if cur["origin"] == "inputs":                                              # 逐条核过来源的轮次：有一条还在待核、或有一条不是用户本人、或没有任何一条，都不许读
                st = self._input_counts(cur)
                if st.get("pending"):
                    return "source_pending", cur
                if not st or any(k != "human" for k in st):
                    return "source_unverified", cur
            if any(not r["allowed"] and r["scope"] == "session" and r["key"] in (self.session_key(ident.host, ident.session_id), ident.session_id) for r in rows):
                return "session_setting", cur
            if self.windows is not None:                                               # 窗口：宿主编号 → 逻辑窗口；暂停（身份待确认、子代理、读状态出错、没确认已关）和关着都不许读。
                w = self.windows.resolve(ident.host, ident.session_id, branch)         # 放在本轮控制之后：这一轮自己的禁读（口令、规则）仍按原来的原因和回执说，之后的轮次才说「窗口关着」
                if w.paused:
                    return w.pause_reason, cur
                if w.state != "on":
                    return "window_closed", cur
            return None, cur
        except Exception:                                                              # noqa: BLE001
            return "permission_unknown", None

    def _reads_state(self, cur):
        """这一轮的读取登记 → "none"｜"confirmed"｜"unconfirmed"。查过但没交出、交付不明的算 unconfirmed（比 confirmed 严）。"""
        if cur is None:
            return "none"
        calls = {}
        for r in self.store.conn.execute("SELECT call_id, kind, detail FROM read_log WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND call_id IS NOT NULL ORDER BY id",
                                         (self.ns, cur["host"], cur["session_id"], cur["branch"], cur["turn_id"])):
            c = calls.setdefault(r["call_id"], {"touched": False, "confirmed": False, "inj_failed": False})
            if r["kind"] in ("search_entered", "vector_called", "assembled"):
                c["touched"] = True
            elif r["kind"] == "returned":
                c["touched"] = True
                c["confirmed"] = json.loads(r["detail"]).get("delivery") == "confirmed_by_rule"
            elif r["kind"] == "injection_failed":
                c["touched"] = True
                c["inj_failed"] = True                                                 # 宿主没收下：不能再算交付
        touched = [c for c in calls.values() if c["touched"]]
        if not touched:
            return "none"
        return "confirmed" if all(c["confirmed"] and not c["inj_failed"] for c in touched) else "unconfirmed"

    def _refusal(self, reason, cur, ident=None):
        try:
            state = self._reads_state(cur) if reason in STATE_RECEIPT else None
        except Exception:                                                              # noqa: BLE001
            reason, state = "permission_unknown", None
        if reason in STATE_RECEIPT:
            receipt = {"none": W1, "confirmed": W2, "unconfirmed": W3}[state]
        else:
            receipt = W4
        persistent = bool(cur and cur.get("persistent") and reason == "clause_ban")
        text = receipt + "。" + (TAIL_KNOWN if reason in KNOWN_CONTROL else (TAIL_CLOSED if reason == "connection_closed" else TAIL_UNKNOWN))
        if reason == "control_malformed":
            text = CT.MALFORMED_HINT + " " + text
        if reason == "window_closed":
            text += WN.CLOSED_HINT
        elif reason == "window_close_unconfirmed":
            text += WN.CLOSE_FAILED_RECEIPT
        elif reason in ("window_identity_pending", "window_state_unreadable", "window_subagent"):
            text += WN.PENDING_HINT
        if persistent:
            text += PERSIST_HINT
        return {"status": "refused", "reason": reason, "receipt": receipt, "text": text, "persistent": persistent}

    def check_permission(self, ident):
        """只问许不许读（不开读取、不登记「开始」）：许可返回 None；不许返回拒绝字典（同 start_read）。给不读记忆的模式（只给工具、固定提醒）判断「这一轮要不要交回执」用。"""
        reason, cur = self._evaluate(ident)
        return None if reason is None else self._refusal(reason, cur, ident)

    def start_read(self, ident, entry, summary=None):
        """每次读取开始前调一次：通过就返回 Ticket；没通过返回拒绝字典（status＝refused，带 reason、按读取登记选的回执 receipt、文字 text）。
        拒绝发生在任何搜索、向量调用、拉起整理之前。开始本身也要登记，登记不了就当权限不明（不猜成允许）。"""
        reason, cur = self._evaluate(ident)
        if reason is not None:
            self._log_refused(ident, cur, "refused", {"reason": reason, "entry": entry})
            return self._refusal(reason, cur, ident)
        tk = Ticket(uuid.uuid4().hex[:12], ident, cur["gen"], entry)
        try:
            tk.del_ver = self.store.del_ver(self.ns)
        except Exception:                                                              # noqa: BLE001
            return self._refusal("permission_unknown", None)
        try:
            self._log_ticket(tk, "start", {"identity": dataclasses.asdict(ident), "turn_gen": cur["gen"], "entry": entry, "summary": summary or {}, "entered_search": False})
        except Exception:                                                              # noqa: BLE001
            return self._refusal("permission_unknown", None)
        return tk

    def _log_refused(self, ident, cur, kind, detail):
        try:
            if ident is not None and isinstance(ident.host, str) and ident.host and isinstance(ident.session_id, str) and ident.session_id:
                self._log(ident.host, ident.session_id, ident.branch or "", cur["turn_id"] if cur else "", cur["gen"] if cur else 0, kind, detail)
        except Exception:                                                              # noqa: BLE001
            pass

    def event(self, tk, kind, detail):
        """给调用方登记别的读取事件（比如返回前就发现装不下）。登记失败不影响读取结果。"""
        try:
            self._log_ticket(tk, kind, detail)
        except Exception:                                                              # noqa: BLE001
            pass

    def mark_search(self, tk):
        if not tk.searched:
            tk.searched = True
            self._log_ticket(tk, "search_entered", {})

    def mark_vector(self, tk, calls):
        tk.vector_calls += calls
        self._log_ticket(tk, "vector_called", {"calls": calls})

    def assembled(self, tk, spans, text):
        """已经组装好的材料或页：只记消息跨度和内容指纹，不再复制原话。"""
        tk.assembled_fp = _sha16(text)
        self._log_ticket(tk, "assembled", {"spans": spans, "fingerprint": tk.assembled_fp, "chars": len(text)})

    def finish_read(self, tk, text, path_name, host=None, profile=None):
        """返回前再核一次：本次绑定变了（轮次换了）、禁读成立了，就丢掉结果、不交付。最终文字还要过这家宿主这条路径的限制，超了不交。
        交付状态：固定版本验证过的规则（path.verified）且没超限，记 confirmed_by_rule；其余记 unknown——不能只凭函数返回了就认定宿主收到了完整材料。"""
        prof = profile or H.profile(host or tk.ident.host)
        path = prof.paths[path_name]
        if tk.del_ver is not None and self.store.del_ver(self.ns) != tk.del_ver:        # 组装的时候删除发生了：不交旧结果
            self._log_ticket(tk, "blocked_at_return", {"reason": "history_deleted", "cause": "deletion", "assembled": tk.assembled_fp})
            return {"status": "error", "reason": "history_deleted", "text": "历史已删除，请重新查询。"}
        reason, cur = self._evaluate(tk.ident)
        cause = reason
        if reason is None and cur["gen"] != tk.gen:
            reason = "turn_changed"
        elif reason in KNOWN_CONTROL:
            cause, reason = reason, "control_changed"
        if reason is not None:
            self._log_ticket(tk, "blocked_at_return", {"reason": reason, "cause": cause, "assembled": tk.assembled_fp})
            ref = self._refusal(reason, self.current_turn(tk.ident.host, tk.ident.session_id, tk.ident.branch or "") if reason != "turn_changed" else None)
            if reason == "turn_changed":
                ref["receipt"], ref["text"] = W4, W4 + "。" + TAIL_UNKNOWN
            return ref
        ok, detail = H.fits(path, text)
        if not ok:
            self._log_ticket(tk, "oversize_blocked", {"limits": detail, "chars": len(text)})
            return {"status": "error", "reason": "return_too_large", "limits": detail, "text": "返回内容超过宿主这条路径的单次上限，没有交付（本插件的错误，不是空结果）。"}
        delivery = "confirmed_by_rule" if path.verified else "unknown"
        self._log_ticket(tk, "returned", {"delivery": delivery, "fingerprint": _sha16(text), "chars": len(text), "limits": detail, "path": path_name, "host": prof.name})
        return {"status": "ok", "delivery": delivery, "text": text, "limits": detail}

    def block_after_read(self, ident, reason):
        """读取已经走完、但返回前窗口变了（关了，或关了又开）：这一轮最后一次读取记成「交付失败」（回执按「已查询、材料未交出」说），并登记原因。没有读取记录就只登记原因。"""
        host, sid, br, tid = ident.host, ident.session_id, ident.branch or "", ident.turn_id
        cur = self._turn(host, sid, br, tid)
        r = self.store.conn.execute("SELECT call_id FROM read_log WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND call_id IS NOT NULL ORDER BY id DESC LIMIT 1", (self.ns, host, sid, br, tid)).fetchone()
        self._log(host, sid, br, tid, cur["gen"] if cur else 0, "blocked_at_return", {"reason": reason}, call_id=r["call_id"] if r else None)
        if r:
            self._log(host, sid, br, tid, cur["gen"] if cur else 0, "injection_failed", {}, call_id=r["call_id"])

    def release_receipt(self, host, session_id, branch, turn_id):
        """回执「尝试注入了」但宿主没收下：撤掉这一轮的「已送」登记，同一轮里下一次钩子还能再送。"""
        with self.store.tx() as c:
            return c.execute("DELETE FROM read_log WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND kind='receipt_sent'", (self.ns, host, session_id, branch or "", turn_id)).rowcount

    def injection_failed(self, host, session_id, branch, turn_id, call_id=None):
        """接头报告：生成了、也尝试注入了，但宿主没有收下。这一次读取不再算交付（回执会按「交付情况无法确认」说）。"""
        cur = self._turn(host, session_id, branch or "", turn_id)
        self._log(host, session_id, branch or "", turn_id, cur["gen"] if cur else 0, "injection_failed", {}, call_id=call_id)

    # ------------------------------------------------------------------ 逐条输入的来源核对（补充施工单·乙 §2）
    # 三件事分开判断：谁触发了这一轮、这一条消息从哪来、这次工具调用有没有读取许可。这里管第二件：每条新输入先登记「来源待核」，核对前这一轮不许读，
    # 核到是用户本人才解析这一条的固定口令；别的来源（peer、sdk、后台通知、核不出来）不产生读取许可、不清掉已有的禁读、文字不当控制。
    @staticmethod
    def text_fp(text):
        return hashlib.sha256(text.replace("\r\n", "\n").strip().encode("utf-8", "surrogatepass")).hexdigest()

    def register_input(self, host, session_id, turn_id, text, branch="", event_id=None):
        """每条新的用户提交事件调一次。同一个 turn_id 下可以有好几条输入（排队合并）：每条一行，键＝宿主给的事件编号（没有就是「内容指纹#第几次出现」，
        所以一字不差的两条是两条；同一个事件重送、又没有事件编号时分不出来，按新输入算——多出来的一条对不上记录，这一轮就一直不许读，偏保守）。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id), self._need_str("branch", branch, True)
        if not isinstance(text, str):
            raise ValueError("text 必须是文字")
        if event_id is not None:
            self._need_str("event_id", event_id)
        fp = self.text_fp(text)
        ctl, refused, reason, persistent = self._control_of(text)
        with self.store.tx() as c:
            t = c.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
            now = self._now()
            if t is None:
                g = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()["g"]
                c.execute("UPDATE turns SET state='closed', closed_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND state IN ('open','registering')", (now, self.ns, host, session_id, branch))
                c.execute("INSERT INTO turns (ns, host, session_id, branch, turn_id, gen, state, origin, created_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (self.ns, host, session_id, branch, turn_id, (g or 0) + 1, "open", "inputs", now, now))
            else:
                mx = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()["g"]
                if t["origin"] != "inputs":
                    return {"registered": False, "ignored": "turn_mode_conflict"}
                if t["state"] == "closed" or t["gen"] < mx:
                    return {"registered": False, "ignored": "stale_turn"}
            n_same = c.execute("SELECT COUNT(*) n FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND fp=?", (self.ns, host, session_id, branch, turn_id, fp)).fetchone()["n"]
            key = event_id or "%s#%d" % (fp[:16], n_same)
            if c.execute("SELECT 1 FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND input_key=?", (self.ns, host, session_id, branch, turn_id, key)).fetchone():
                return {"registered": True, "created": False, "input_key": key, "fp": fp}
            seq = (c.execute("SELECT MAX(seq) s FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()["s"] or 0) + 1
            c.execute("INSERT INTO inputs (ns, host, session_id, branch, turn_id, seq, input_key, fp, state, ctl_kind, ctl_span, ban_reason, ban_persistent, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.ns, host, session_id, branch, turn_id, seq, key, fp, "pending", ctl.kind, None if ctl.span is None else "%d-%d" % ctl.span, reason, 1 if persistent else 0, now))
        return {"registered": True, "created": True, "input_key": key, "seq": seq, "fp": fp}

    def inputs_of(self, host, session_id, turn_id, branch=""):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? ORDER BY seq", (self.ns, host, session_id, branch or "", turn_id))]

    def _input_counts(self, cur):
        out = {}
        for r in self.store.conn.execute("SELECT state, COUNT(*) n FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? GROUP BY state", (self.ns, cur["host"], cur["session_id"], cur["branch"], cur["turn_id"])):
            out[r["state"]] = r["n"]
        return out

    def resolve_inputs(self, host, session_id, turn_id, verdicts, branch="", unmatched=0):
        """接头拿记录核完以后调：verdicts＝[{"input_key", "verdict": "human"|"other"|"fail", "label"}]。晚到的结果不能重新打开已经结束或已换代次的轮次；
        已经核过的条目不会被改（不翻案）。human 才解析控制：禁读并进这一轮已有的禁读（只加不减），后来的普通文字清不掉。unmatched>0＝记录里有我们没收到的条目（覆盖不全），记一条失败。"""
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id)
        branch = branch or ""
        applied, hints = 0, []
        with self.store.tx() as c:
            t = c.execute("SELECT * FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
            mx = c.execute("SELECT MAX(gen) g FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()["g"]
            if t is None or t["origin"] != "inputs" or t["state"] == "closed" or t["gen"] < mx:
                c.execute("INSERT INTO read_log (ns, host, session_id, branch, turn_id, gen, call_id, entry, kind, detail, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (self.ns, host, session_id, branch, turn_id, t["gen"] if t else 0, None, None, "source_result_ignored", "{}", self._now()))
                return {"applied": 0, "ignored": "stale_turn" if t is not None else "turn_unregistered", "hints": []}
            now = self._now()
            for v in verdicts:
                if v.get("verdict") not in ("human", "other", "fail"):
                    raise ValueError("verdict 只能是 human、other、fail")
                row = c.execute("SELECT * FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND input_key=?", (self.ns, host, session_id, branch, turn_id, v["input_key"])).fetchone()
                if row is None or row["state"] != "pending":
                    continue
                st = {"human": "human", "other": "rejected", "fail": "failed"}[v["verdict"]]
                c.execute("UPDATE inputs SET state=?, label=?, resolved_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND seq=?",
                          (st, (v.get("label") or None), now, self.ns, host, session_id, branch, turn_id, row["seq"]))
                applied += 1
                if st == "human" and row["ban_reason"]:
                    c.execute("UPDATE turns SET refused=1, refuse_reason=COALESCE(refuse_reason, ?), persistent=CASE WHEN ? THEN 1 ELSE persistent END, "
                              "control_kind=CASE WHEN control_kind IS NULL OR control_kind='none' THEN ? ELSE control_kind END, control_span=COALESCE(control_span, ?) "
                              "WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?",
                              (row["ban_reason"], row["ban_persistent"], row["ctl_kind"], row["ctl_span"], self.ns, host, session_id, branch, turn_id))
                    if row["ctl_kind"] == "malformed":
                        hints.append(CT.MALFORMED_HINT)
            for k in range(int(unmatched or 0)):
                seq = (c.execute("SELECT MAX(seq) s FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()["s"] or 0) + 1
                c.execute("INSERT INTO inputs (ns, host, session_id, branch, turn_id, seq, input_key, fp, state, label, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.ns, host, session_id, branch, turn_id, seq, "unmatched#%d" % seq, "", "failed", "record_has_unreceived_entry", now, now))
        return {"applied": applied, "hints": hints}

    def expire_inputs(self, host, session_id, turn_id, branch="", max_age_s=5.0):
        """在写定的时限里核不出来的待核条目，算失败（以后有新证据可以再核，但不能重新打开已经换代次的轮次）。"""
        import datetime as dt
        now = dt.datetime.strptime(self._now(), "%Y-%m-%dT%H:%M:%SZ")
        n = 0
        with self.store.tx() as c:
            for r in c.execute("SELECT seq, created_at FROM inputs WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND state='pending'", (self.ns, host, session_id, branch or "", turn_id)).fetchall():
                try:
                    age = (now - dt.datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
                except Exception:                                                      # noqa: BLE001
                    age = max_age_s + 1
                if age >= max_age_s:
                    c.execute("UPDATE inputs SET state='failed', label='deadline', resolved_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND seq=?",
                              (self._now(), self.ns, host, session_id, branch or "", turn_id, r["seq"]))
                    n += 1
        return n

    # ------------------------------------------------------------------ 调用编号 → 会话、分支、轮次（调用前钩子登记，MCP 读取时按编号查）
    def register_call(self, host, call_id, session_id, turn_id, branch=""):
        """调用前钩子在放行宿主继续这次调用之前登记。同一个编号再来：绑定相同＝幂等；绑定不同＝标冲突（这个编号从此作废，读取时拒绝）。"""
        self._need_str("host", host), self._need_str("call_id", call_id), self._need_str("session_id", session_id), self._need_str("turn_id", turn_id), self._need_str("branch", branch, True)
        if len(call_id) > 200:
            raise ValueError("call_id 太长")
        with self.store.tx() as c:
            t = c.execute("SELECT gen FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (self.ns, host, session_id, branch, turn_id)).fetchone()
            if t is None:
                return {"registered": False, "reason": "turn_unregistered"}
            row = c.execute("SELECT * FROM tool_calls WHERE ns=? AND host=? AND call_id=?", (self.ns, host, call_id)).fetchone()
            if row is not None:
                if (row["session_id"], row["branch"], row["turn_id"]) == (session_id, branch, turn_id) and not row["conflict"]:
                    return {"registered": True, "created": False}
                c.execute("UPDATE tool_calls SET conflict=1 WHERE ns=? AND host=? AND call_id=?", (self.ns, host, call_id))
                return {"registered": False, "reason": "call_conflict"}
            c.execute("INSERT INTO tool_calls (ns, host, call_id, session_id, branch, turn_id, gen, created_at) VALUES (?,?,?,?,?,?,?,?)", (self.ns, host, call_id, session_id, branch, turn_id, t["gen"], self._now()))
        return {"registered": True, "created": True}

    def identity_for_call(self, host, call_id, conn=None):
        """MCP 读取时按宿主放进 _meta 的调用编号查（conn＝调用方自己的只读连接，不给就用主连接）。返回 (ToolIdentity 或 None, 原因)。没登记、有冲突、编号不合格都没有身份。"""
        if not isinstance(call_id, str) or not call_id or len(call_id) > 200:
            return None, "call_id_invalid"
        r = (conn or self.store.conn).execute("SELECT * FROM tool_calls WHERE ns=? AND host=? AND call_id=?", (self.ns, host, call_id)).fetchone()
        if r is None:
            return None, "call_unregistered"
        if r["conflict"]:
            return None, "call_conflict"
        return ToolIdentity(host, r["session_id"], r["branch"], r["turn_id"]), None

    # ------------------------------------------------------------------ 目录模式的「要不要重交」标记（每个会话、分支各一行）
    # 状态：needed＝该交（开始、恢复、清空、压缩、分叉事件只做这一件：标记，不读）；composed＝已组装、等接头确认注入；handed＝接头确认注入成功；failed＝注入失败（下次还该交）。
    # epoch 每次边界事件加一：确认只认当时的 epoch，边界事件之后才到的旧确认不能把新的「该交」改成已交。
    def dir_get(self, host, session_id, branch=""):
        r = self.store.conn.execute("SELECT * FROM dir_state WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()
        return dict(r) if r else None

    def dir_mark_needed(self, host, session_id, branch, reason):
        self._need_str("host", host), self._need_str("session_id", session_id), self._need_str("branch", branch, True)
        with self.store.tx() as c:
            r = c.execute("SELECT epoch FROM dir_state WHERE ns=? AND host=? AND session_id=? AND branch=?", (self.ns, host, session_id, branch)).fetchone()
            epoch = (r["epoch"] + 1) if r else 1
            c.execute("INSERT INTO dir_state (ns, host, session_id, branch, epoch, state, reason, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(ns, host, session_id, branch) DO UPDATE SET epoch=excluded.epoch, state='needed', reason=excluded.reason, updated_at=excluded.updated_at",
                      (self.ns, host, session_id, branch, epoch, "needed", reason, self._now()))
        return epoch

    def dir_ensure(self, host, session_id, branch=""):
        row = self.dir_get(host, session_id, branch)
        if row is None:
            self.dir_mark_needed(host, session_id, branch, "first_turn")
            row = self.dir_get(host, session_id, branch)
        return row

    def dir_composed(self, host, session_id, branch, epoch, version):
        with self.store.tx() as c:
            return c.execute("UPDATE dir_state SET state='composed', directory_version=?, updated_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND epoch=?",
                             (version, self._now(), self.ns, host, session_id, branch, epoch)).rowcount

    def dir_confirm(self, host, session_id, branch, epoch, ok):
        """接头确认注入成不成功。epoch 对不上（后来又有边界事件）就不改，返回 False。"""
        with self.store.tx() as c:
            return bool(c.execute("UPDATE dir_state SET state=?, updated_at=? WHERE ns=? AND host=? AND session_id=? AND branch=? AND epoch=? AND state='composed'",
                                  ("handed" if ok else "failed", self._now(), self.ns, host, session_id, branch, epoch)).rowcount)
