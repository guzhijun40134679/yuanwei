"""逻辑窗口和读取许可（禁读按窗口，交回点乙）。

* **窗口**：持久保存的是逻辑窗口（开或关、控制版本、最近一次有效控制的来源），不是宿主那个会变的会话编号。宿主编号（宿主、会话、分支）映射到窗口，绑定一旦建立不改。
  窗口状态存在接头配置的那个库里，**不按工作区分区**（v10 的五张表都没有工作区列）；一次读多个工作区时用同一份窗口状态。
* **关：宽。** 口令 `#yuanwei-nomem`、规则识别（gate 的分句判法）、关闭工具，任一条成立就关；控制版本只在状态真的改变时加一；同一操作编号重送是幂等的。
  迟到的旧关闭（它依据的输入早于窗口里最后一次有效开启）不能把后来有效打开的窗口再关掉。
* **读取许可**：一次一张、唯一绑定、原子认领、返回前复核；窗口每关一次、开一次都换控制版本，旧许可永不复活。库里只存许可的哈希。
* **失败要报出来**：关闭时写窗口状态失败，结果是 close_failed，回执不能说「已关」（接头要把这一轮的读取拦住）；读窗口状态出错，窗口当「暂停」，不当「开」。
* **开只有两条路**（主会话裁定二，11-10）：口令 `#yuanwei-mem`（用户输入的第一物理行）；或者 agent 调 `window_on`（不带参数）申请，用户在这个窗口里的**下一条可信输入**第一行
  （去掉首尾空白、再去掉末尾的「。．.！!」）正好是「确认开启」才开。别的话一律不开，待确认作废。不从大白话里认「开」。agent 不能替用户确认：确认只认 process_input
  （接头只对用户本人的输入调它）；工具参数、工具返回、附件、子代理都确认不了。待确认存进库（window_pending），进程重启以后仍只对下一条输入有效。
"""
import calendar
import dataclasses
import hashlib
import json
import secrets
import threading
import time
from typing import Callable, NamedTuple, Optional, Sequence, Tuple

from . import control as CT
from . import gate as GT

# 固定文字（回执、提示）。给用户看的，状态提交成功以后才给。
CONFIRM_WORD = "确认开启"
DELETE_CONFIRM_WORD = "确认删除"
CONFIRM_PROMPT = "要在这个窗口重新使用长期记忆吗？回复「确认开启」即可。"
NOT_CONFIRMED_RECEIPT = "记忆仍是关着的；要打开，请让我再申请一次后回复「确认开启」，或者直接发 #yuanwei-mem。"
CLOSE_FAILED_RECEIPT = "没能确认这个窗口的记忆读取已经关闭；这一轮已先暂停读取。请再说一次，或在消息第一行写 #yuanwei-nomem。"
NOTICE_CLOSED = "这个窗口的记忆读取已关闭，直到你明确重新允许；此前已经交给模型的内容不会因此撤回。"
NOTICE_OPENED = "这个窗口的记忆读取已重新打开。"
NOTICE_RESTORED_CLOSED = "你恢复的这个窗口，记忆读取是关着的；要用请让我申请开启后回复「确认开启」，或在消息第一行写 #yuanwei-mem。"
CLOSED_HINT = "这个窗口的记忆读取是关着的；要重新使用，请让我申请开启后回复「确认开启」，或在消息第一行写 #yuanwei-mem。"
PENDING_HINT = "窗口身份待确认：没能确认这个对话对应哪个窗口，所以暂停读取（没有默认打开，也没有关整个工作区）。在消息第一行写 #yuanwei-mem 或 #yuanwei-nomem 可以明确这个窗口的开关。"

# 拒绝的类别：窗口关闭能被有效开启覆盖；其余自然语言覆盖不了
OVERRIDABLE = frozenset(["window_closed"])
NOT_OVERRIDABLE = frozenset(["window_close_unconfirmed", "window_identity_pending", "window_subagent", "window_state_unreadable", "workspace_setting", "session_setting", "connection_closed", "identity_unbound", "identity_incomplete"])


def refusal_class(reason):
    return "overridable" if reason in OVERRIDABLE else "not_overridable"


def _clock():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def params_fingerprint(args, skip=("permit",)):
    """业务参数指纹：许可字段本身不参与。未知字段照样参与（不会在核对之前被悄悄删掉）。"""
    d = {k: v for k, v in (args or {}).items() if k not in skip}
    return _sha(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str))


def is_confirm_line(text):
    """用户输入的第一物理行，去掉首尾空白、再去掉末尾的「。．.！!」，正好等于「确认开启」。第二行、引号、代码块、转述、「确认开启吧」「确认开启？」都不算。"""
    first = text.partition("\n")[0]
    return first.strip().rstrip("。．.！!") == CONFIRM_WORD


def is_delete_confirm(text):
    """确认删除：**整条消息**去掉首尾空白、再去掉末尾的「。．.！!」，正好是「确认删除」，没有别的非空文字。第二行、引号、代码块、「吧」、问号都不算。（确认开启的判法不变：只看第一行。）"""
    return text.strip().rstrip("。．.！!") == DELETE_CONFIRM_WORD


# ------------------------------------------------------------------ 数据
@dataclasses.dataclass(frozen=True)
class InputRef:
    """一条可信用户输入的定位；全部来自接头，不来自模型。input_key：Hermes 上是 turn_token 用的事件键，别的宿主是 inputs.input_key。不带原文（原文不存、不缓存）。"""
    host: str
    session_id: str
    turn_id: str
    input_key: str
    fp: str
    branch: str = ""
    ns: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class WindowRef:
    window_id: Optional[str]
    state: Optional[str]
    control_version: Optional[int]
    paused: bool
    pause_reason: Optional[str]            # window_identity_pending｜window_subagent｜window_state_unreadable
    bound_via: Optional[str] = None
    created: bool = False                  # 这次调用是不是刚建立了绑定


class WindowView(NamedTuple):
    state: str                             # 实际状态 "on"｜"off"（暂停时是 "off"：不能读）
    blocked_by_setting: bool
    reason: str                            # 固定原因码
    control_version: int


class CloseResult(NamedTuple):
    outcome: str                           # applied｜noop｜stale_ignored
    state: str
    control_version: int
    replayed: bool
    op_id: str


class InputControl(NamedTuple):
    window_id: Optional[str]
    win_seq: Optional[int]
    command: str                           # none｜nomem｜mem｜malformed
    steps: Tuple[tuple, ...]               # (kind, source, outcome)
    state_before: Optional[str]
    state_after: Optional[str]
    control_version: Optional[int]
    outcome: str                           # closed｜opened｜unchanged｜blocked_by_setting｜paused_identity｜close_failed
    reason: Optional[str]
    receipt: Optional[str]
    notice: Optional[str]
    deletion: Optional[object] = None      # 确认删除那一轮：DeleteResult（只有计数和固定文案）


class DeleteRequest(NamedTuple):
    status: str                            # "waiting"｜"paused"
    reason: str
    pending_id: Optional[str]
    prompt: Optional[str]                  # 固定提示（范围只有计数）
    scope: dict                            # workspaces、sessions、messages


class OpenRequest(NamedTuple):
    status: str                            # "on"（已经开着）｜"blocked_by_setting"｜"waiting"（等待用户确认）｜"paused"
    state: str                             # 实际状态
    blocked_by_setting: bool
    reason: str                            # 固定原因码
    control_version: int
    prompt: Optional[str]                  # 等待确认时：固定提示，agent 原样转告用户


class Permit(NamedTuple):
    secret: Optional[str]                  # 交给调用方一次；被拒时是 None
    permit_sha: Optional[str]
    window_id: Optional[str]
    control_version: Optional[int]
    expires_at: Optional[str]
    refused: Optional[str]                 # 拒绝原因码，None＝签发成功
    refusal_class: Optional[str]


class ClaimResult(NamedTuple):
    ok: bool
    reason: str
    window_id: Optional[str]
    control_version: Optional[int]


class ReturnCheck(NamedTuple):
    deliver: bool
    reason: str


# ------------------------------------------------------------------ 服务
class WindowService:
    def __init__(self, store, *, clock=None, default_state="on", settings=None):
        if default_state not in ("on", "off"):
            raise ValueError("默认状态只能是 on 或 off")
        self.store = store
        self.clock = clock or _clock
        self.default_state = default_state
        self.settings = settings or (lambda host, session_id: None)        # → "workspace"｜"session"｜None：被哪条设置挡住（设置优先）
        self.fault = None                                                  # 测试注入：fault(point)，point ∈ before_state_write
        self.deleter = None                                                # DeletionService：接头装上才有
        self._lock = threading.RLock()
        self._suspect = set()                                              # (宿主, 会话, 分支)：关闭写失败、还没确认已关的窗口。在一次成功的关闭（或明确的开）落地之前，进程内一律不许读

    def _fire(self, point):
        if self.fault is not None:
            self.fault(point)

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _ok_key(host, session_id, branch):
        return isinstance(host, str) and host != "" and isinstance(session_id, str) and session_id != "" and isinstance(branch, str)

    def _binding(self, c, host, session_id, branch):
        return c.execute("SELECT b.window_id, b.bound_via, w.state, w.control_version FROM window_bindings b JOIN windows w ON w.window_id=b.window_id "
                         "WHERE b.host=? AND b.session_id=? AND b.branch=?", (host, session_id, branch)).fetchone()

    @staticmethod
    def _ref(row, created=False):
        return WindowRef(row["window_id"], row["state"], row["control_version"], False, None, row["bound_via"], created)

    @staticmethod
    def _paused(reason):
        return WindowRef(None, None, None, True, reason)

    def _new_window(self, c, host, state, origin, parent, source, op_id=None):
        wid = "w-" + secrets.token_hex(8)
        now = self.clock()
        c.execute("INSERT INTO windows (window_id, state, control_version, ctl_source, ctl_op_id, ctl_at, origin, parent_window_id, host, created_at, input_seq) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                  (wid, state, 1, source, op_id, now, origin, parent, host, now))
        return wid

    def _bind(self, c, host, session_id, branch, wid, via, host_parent):
        c.execute("INSERT INTO window_bindings (host, session_id, branch, window_id, bound_via, host_parent_session_id, bound_at) VALUES (?,?,?,?,?,?,?)",
                  (host, session_id, branch, wid, via, host_parent or None, self.clock()))

    def _record_op(self, c, op_id, wid, kind, source, input_win_seq, vb, va, outcome, reason=None):
        c.execute("INSERT INTO window_ops (op_id, window_id, kind, source, input_win_seq, version_before, version_after, outcome, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (op_id, wid, kind, source, input_win_seq, vb, va, outcome, reason, self.clock()))

    # ---------------------------------------------------------------- 宿主编号 → 逻辑窗口
    def resolve(self, host, session_id, branch="", *, actor="main", _raw=False):
        """只读，不创建。子代理：暂停（不许读、不许开）；查不到绑定：暂停（身份待确认）；读状态出错：暂停（不当成开）。"""
        if actor != "main":
            return self._paused("window_subagent")
        if not self._ok_key(host, session_id, branch):
            return self._paused("window_identity_pending")
        if not _raw and (host, session_id, branch) in self._suspect:
            return self._paused("window_close_unconfirmed")
        try:
            with self._lock:
                row = self._binding(self.store.conn, host, session_id, branch)
        except Exception:                                                              # noqa: BLE001
            return self._paused("window_state_unreadable")
        return self._paused("window_identity_pending") if row is None else self._ref(row)

    def bind_new(self, host, session_id, branch="", *, via="initialize", state=None):
        """新窗口，用默认状态；这个宿主编号已经有绑定就原样返回（幂等，不改状态）。initialize 见到没见过的号、reset 用它。"""
        with self._lock, self.store.tx() as c:
            row = self._binding(c, host, session_id, branch)
            if row is not None:
                return self._ref(row)
            wid = self._new_window(c, host, state or self.default_state, "new", None, "default")
            self._bind(c, host, session_id, branch, wid, via, None)
            return WindowRef(wid, state or self.default_state, 1, False, None, via, True)

    def bind_same(self, host, new_session_id, from_session_id, branch="", *, new_branch=None, via="compression"):
        """压缩、同一窗口换分支标识：新编号绑到 from 那个会话的窗口。新编号已绑定：原样返回（绑定不改）；from 没有绑定：暂停。"""
        nb = branch if new_branch is None else new_branch
        with self._lock, self.store.tx() as c:
            row = self._binding(c, host, new_session_id, nb)
            if row is not None:
                return self._ref(row)
            src = self._binding(c, host, from_session_id, branch)
            if src is None:
                return self._paused("window_identity_pending")
            self._bind(c, host, new_session_id, nb, src["window_id"], via, from_session_id)
            return WindowRef(src["window_id"], src["state"], src["control_version"], False, None, via, True)

    def bind_fork(self, host, new_session_id, parent_session_id, branch=""):
        """分叉：新窗口，复制分叉这一刻父窗口的有效状态，之后各管各的。父没有绑定：暂停。"""
        with self._lock, self.store.tx() as c:
            row = self._binding(c, host, new_session_id, branch)
            if row is not None:
                return self._ref(row)
            par = self._binding(c, host, parent_session_id, branch)
            if par is None:
                return self._paused("window_identity_pending")
            wid = self._new_window(c, host, par["state"], "fork", par["window_id"], "fork_copy")
            self._bind(c, host, new_session_id, branch, wid, "branch", parent_session_id)
            return WindowRef(wid, par["state"], 1, False, None, "branch", True)

    def bind_restore(self, host, session_id, branch=""):
        """恢复别的会话：只按绑定表查；查到→那个窗口，查不到→暂停。不创建，不看宿主给的 parent。"""
        return self.resolve(host, session_id, branch)

    def bind_explicit(self, host, session_id, branch, *, state, source, op_id):
        """身份待确认的会话，由用户明确的口令（关或开）建立绑定。已有绑定：原样返回。"""
        if state not in ("on", "off"):
            raise ValueError("state 只能是 on 或 off")
        with self._lock, self.store.tx() as c:
            row = self._binding(c, host, session_id, branch)
            if row is not None:
                return self._ref(row)
            wid = self._new_window(c, host, state, "restore", None, source, op_id)
            self._bind(c, host, session_id, branch, wid, "explicit", None)
            self._record_op(c, op_id, wid, "close" if state == "off" else "open", source, None, 0, 1, "applied", "explicit_bind")
            return WindowRef(wid, state, 1, False, None, "explicit", True)

    # ---------------------------------------------------------------- 关、开（状态改变）
    def _row(self, c, wid):
        return c.execute("SELECT * FROM windows WHERE window_id=?", (wid,)).fetchone()

    def _change(self, wid, kind, source, op_id, input_win_seq):
        """kind＝close｜open。幂等（同 op_id 重送返回原结果）；状态真改变才加版本；迟到的旧操作（依据的输入早于窗口里最后一次相反的有效操作）不生效。"""
        want = "off" if kind == "close" else "on"
        opposite = "open" if kind == "close" else "close"
        with self._lock, self.store.tx() as c:
            prev = c.execute("SELECT * FROM window_ops WHERE op_id=?", (op_id,)).fetchone()
            w = self._row(c, wid)
            if w is None:
                raise KeyError("没有这个窗口")
            if prev is not None:
                if prev["confirmed_at"] is None:
                    c.execute("UPDATE window_ops SET confirmed_at=? WHERE op_id=?", (self.clock(), op_id))
                return CloseResult(prev["outcome"], w["state"], w["control_version"], True, op_id)
            if input_win_seq is not None:
                later = c.execute("SELECT MAX(input_win_seq) m FROM window_ops WHERE window_id=? AND kind=? AND outcome='applied'", (wid, opposite)).fetchone()["m"]
                if later is not None and later > input_win_seq:
                    self._record_op(c, op_id, wid, kind, source, input_win_seq, w["control_version"], w["control_version"], "stale_ignored", "later_%s" % opposite)
                    return CloseResult("stale_ignored", w["state"], w["control_version"], False, op_id)
            self._fire("before_state_write")
            if kind == "close":                                                       # 待确认登记以后窗口又被关了一次：待确认作废
                c.execute("UPDATE window_pending SET state='void', void_reason='closed_again', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), wid))
                c.execute("UPDATE delete_pending SET state='void', void_reason='closed_again', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), wid))
            if w["state"] == want:
                c.execute("UPDATE windows SET ctl_source=?, ctl_op_id=?, ctl_at=? WHERE window_id=?", (source, op_id, self.clock(), wid))
                self._record_op(c, op_id, wid, kind, source, input_win_seq, w["control_version"], w["control_version"], "noop")
                return CloseResult("noop", want, w["control_version"], False, op_id)
            nv = w["control_version"] + 1
            c.execute("UPDATE windows SET state=?, control_version=?, ctl_source=?, ctl_op_id=?, ctl_at=? WHERE window_id=?", (want, nv, source, op_id, self.clock(), wid))
            c.execute("UPDATE window_permits SET state='void', void_reason=?, finished_at=? WHERE window_id=? AND kind='read' AND state IN ('issued','claimed')", ("window_%s" % kind, self.clock(), wid))
            if source != "confirm":                                                   # 用户自己的确认不作废它自己；别的改动让版本变了，待确认作废
                c.execute("UPDATE window_pending SET state='void', void_reason='version_changed', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), wid))
                c.execute("UPDATE delete_pending SET state='void', void_reason='version_changed', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), wid))
            self._record_op(c, op_id, wid, kind, source, input_win_seq, w["control_version"], nv, "applied")
            return CloseResult("applied", want, nv, False, op_id)

    def close(self, window_id, *, source, op_id, input_win_seq=None):
        """source：command｜rule｜tool。写失败会抛异常，调用方不能当成已关。"""
        if source not in ("command", "rule", "tool"):
            raise ValueError("source 只能是 command、rule、tool")
        return self._change(window_id, "close", source, op_id, input_win_seq)

    def open_window(self, window_id, *, source, op_id, input_win_seq=None):
        """内部的「打开」动作：口令 #yuanwei-mem 和「确认开启」用它。设置挡住的判断在调用方（process_input）做。"""
        return self._change(window_id, "open", source, op_id, input_win_seq)

    def view(self, host, session_id, branch=""):
        """查看本窗口开关：只读控制记录，不读历史、不触发向量或整理。"""
        w = self.resolve(host, session_id, branch)
        if w.paused:
            return WindowView("off", False, {"window_identity_pending": "identity_pending", "window_subagent": "subagent", "window_state_unreadable": "state_unreadable", "window_close_unconfirmed": "close_unconfirmed"}[w.pause_reason], 0)
        try:
            blocked = self.settings(host, session_id)
        except Exception:                                                              # noqa: BLE001
            blocked = "workspace"                                                      # 读设置出错：当成被挡住，不当成开
        with self._lock:
            row = self._row(self.store.conn, w.window_id)
        if blocked:
            return WindowView(row["state"], True, "setting_%s" % blocked, row["control_version"])
        reason = "ok" if row["state"] == "on" else ("default_closed" if row["ctl_source"] == "default" else "closed_by_%s" % row["ctl_source"])
        with self._lock:
            waiting = self.store.conn.execute("SELECT 1 FROM window_pending WHERE window_id=? AND state='waiting'", (w.window_id,)).fetchone()
        if waiting and row["state"] == "off":
            reason = "awaiting_confirmation"
        return WindowView(row["state"], False, reason, row["control_version"])

    # ---------------------------------------------------------------- 一条可信输入的控制（口令、规则识别）
    @staticmethod
    def _plan(text):
        """纯函数：这条输入里有哪些控制步骤。口令在第一行，所以排在最前；规则认出禁读分句就再加一个「关」。"""
        ctl = CT.parse(text)
        steps = []
        if ctl.kind == "nomem":
            steps.append(("close", "command"))
        elif ctl.kind == "mem":
            steps.append(("open", "command"))
        rest = CT.task_view(text, ctl)
        if any(b for _c, b in GT.ban_clauses(rest)):
            steps.append(("close", "rule"))
        return ctl, steps

    def input_seq_of(self, ref):
        w = self.resolve(ref.host, ref.session_id, ref.branch or "")
        if w.paused:
            return None
        with self._lock:
            r = self.store.conn.execute("SELECT win_seq FROM window_inputs WHERE window_id=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND input_key=?",
                                        (w.window_id, ref.host, ref.session_id, ref.branch or "", ref.turn_id, ref.input_key)).fetchone()
        return r["win_seq"] if r else None

    def process_input(self, ref, text, *, origin_verified, actor="main", capture=None):
        """一条可信用户输入的控制：口令、规则识别，按先后逐步应用。禁读一律生效；开（目前只有口令）要来源核过、没被设置挡住。
        同一条输入重送：返回已有的结果，不再改状态（上次是 close_failed 的除外，会重试）。来源没核过：什么都不改。"""
        ctl, steps = self._plan(text)
        base = dict(window_id=None, win_seq=None, command=ctl.kind, steps=(), state_before=None, state_after=None, control_version=None, reason=None, receipt=None, notice=None)
        if not origin_verified:
            return InputControl(outcome="unchanged", **dict(base, reason="origin_unverified"))
        wants_close = any(k == "close" for k, _ in steps)
        created_explicit = False
        try:
            w = self.resolve(ref.host, ref.session_id, ref.branch or "", actor=actor, _raw=True)
            if w.paused and w.pause_reason == "window_identity_pending" and steps:
                first = steps[-1] if any(k == "close" for k, _ in steps) else steps[0]
                state = "off" if wants_close else "on"                                 # 同一条输入里有禁读就关；只有开口令才开
                if state == "on" and self.settings(ref.host, ref.session_id):
                    return InputControl(outcome="blocked_by_setting", **dict(base, reason="setting_%s" % self.settings(ref.host, ref.session_id)))
                w = self.bind_explicit(ref.host, ref.session_id, ref.branch or "", state=state, source=first[1], op_id="op-" + secrets.token_hex(8))
                created_explicit = w.created
            if w.paused:
                return InputControl(outcome="paused_identity", **dict(base, reason=w.pause_reason))
            wid = w.window_id
            with self._lock, self.store.tx() as c:
                got = c.execute("SELECT win_seq, result FROM window_inputs WHERE window_id=? AND host=? AND session_id=? AND branch=? AND turn_id=? AND input_key=?",
                                (wid, ref.host, ref.session_id, ref.branch or "", ref.turn_id, ref.input_key)).fetchone()
                new_input = got is None
                if got is not None and json.loads(got["result"]).get("outcome") != "close_failed":
                    d = json.loads(got["result"])
                    d["steps"] = tuple(tuple(x) for x in d["steps"])
                    if isinstance(d.get("deletion"), list):
                        from .deletion import DeleteResult
                        d["deletion"] = DeleteResult(*d["deletion"])
                    return InputControl(**d)
                if got is None:
                    c.execute("UPDATE windows SET input_seq=input_seq+1 WHERE window_id=?", (wid,))
                    seq = c.execute("SELECT input_seq FROM windows WHERE window_id=?", (wid,)).fetchone()["input_seq"]
                    c.execute("INSERT INTO window_inputs (window_id, win_seq, host, session_id, branch, turn_id, input_key, fp, ns, result, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (wid, seq, ref.host, ref.session_id, ref.branch or "", ref.turn_id, ref.input_key, ref.fp, ref.ns, "{}", self.clock()))
                else:
                    seq = got["win_seq"]
            before = self.resolve(ref.host, ref.session_id, ref.branch or "", _raw=True)
            done, blocked, reason, voided_other = [], None, None, False
            pend = self._pending_verdict(wid, seq, text) if new_input else None             # 待确认：只对「下一条输入」有效，不是确认就作废
            deletion = None
            if pend == "confirm_delete":
                deletion = self._do_delete(wid, seq, ref, capture)
            if pend == "confirm":
                blocked = self.settings(ref.host, ref.session_id)
                if blocked:                                                                  # 设置优先，确认也打不开设置
                    reason = "setting_%s" % blocked
                    done.append(("open", "confirm", "blocked_by_setting"))
                    self._resolve_pending(wid, "void", "blocked_by_setting", seq)
                else:
                    r = self._change(wid, "open", "confirm", "op-" + secrets.token_hex(8), seq)
                    done.append(("open", "confirm", r.outcome))
                    self._resolve_pending(wid, "opened", None, seq)
            elif pend == "void_other":
                voided_other = True
            for kind, source in steps:
                if kind == "open":
                    blocked = self.settings(ref.host, ref.session_id)
                    if blocked:
                        reason = "setting_%s" % blocked
                        done.append(("open", source, "blocked_by_setting"))
                        continue
                op = "op-" + secrets.token_hex(8)
                r = self._change(wid, kind, source, op, seq)
                done.append((kind, source, r.outcome))
            after = self.resolve(ref.host, ref.session_id, ref.branch or "", _raw=True)
            if before.state != after.state or created_explicit:
                outcome = "closed" if after.state == "off" else "opened"
            else:
                outcome = "blocked_by_setting" if blocked and not any(o == "applied" for _k, _s, o in done) else "unchanged"
            if after.state == "off" or any(k == "open" and o == "applied" for k, _s, o in done):      # 一次关闭（或明确的开）落地了：不再「没确认」
                self._suspect.discard((ref.host, ref.session_id, ref.branch or ""))
            res = InputControl(window_id=wid, win_seq=seq, command="confirm" if pend == "confirm" and not blocked else ctl.kind, steps=tuple(done), state_before=before.state, state_after=after.state,
                               control_version=after.control_version, outcome=outcome, reason=reason or ("pending_voided" if voided_other else None),
                               receipt=NOT_CONFIRMED_RECEIPT if voided_other and after.state == "off" else None, notice={"closed": NOTICE_CLOSED, "opened": NOTICE_OPENED}.get(outcome))
            if deletion is not None:
                st = {"complete": "deleted", "cleaning": "deleting"}.get(deletion.status, "delete_failed")
                res = res._replace(command="confirm_delete", outcome=st, reason=deletion.code, receipt=deletion.text, notice=None, deletion=deletion)
            with self._lock, self.store.tx() as c:
                c.execute("UPDATE window_inputs SET result=? WHERE window_id=? AND win_seq=?", (json.dumps(res._asdict(), ensure_ascii=False), wid, seq))
            return res
        except Exception:                                                              # noqa: BLE001
            if wants_close:
                self._suspect.add((ref.host, ref.session_id, ref.branch or ""))
                return InputControl(outcome="close_failed", **dict(base, reason="state_write_failed", receipt=CLOSE_FAILED_RECEIPT))      # 关闭写失败：不能说已关，接头要把这一轮拦住
            return InputControl(outcome="unchanged", **dict(base, reason="state_error"))

    # ---------------------------------------------------------------- 待确认开启（agent 申请，用户下一条输入第一行是「确认开启」才开）
    def _pending_verdict(self, wid, seq, text):
        """这条新输入对「待确认」的作用：None（没有待确认，或不是后面的输入）｜confirm｜void_other（不是确认：已作废）｜void（版本变了：已作废）。"""
        with self._lock, self.store.tx() as c:
            dp = c.execute("SELECT * FROM delete_pending WHERE window_id=? AND state='waiting' ORDER BY requested_at DESC, rowid DESC LIMIT 1", (wid,)).fetchone()
            if dp is not None:                                                            # 待确认删除：只认整条「确认删除」，开启的口令串用不了
                w = self._row(c, wid)
                if dp["control_version"] != w["control_version"]:
                    c.execute("UPDATE delete_pending SET state='void', void_reason='version_changed', resolved_at=?, resolved_win_seq=? WHERE pending_id=?", (self.clock(), seq, dp["pending_id"]))
                    return "void"
                if seq <= dp["request_win_seq"]:
                    return None
                if is_delete_confirm(text):
                    return "confirm_delete"
                c.execute("UPDATE delete_pending SET state='void', void_reason='next_input_not_confirm', resolved_at=?, resolved_win_seq=? WHERE pending_id=?", (self.clock(), seq, dp["pending_id"]))
                return "void_other"
            p = c.execute("SELECT * FROM window_pending WHERE window_id=? AND state='waiting' ORDER BY requested_at DESC, rowid DESC LIMIT 1", (wid,)).fetchone()
            if p is None:
                return None
            w = self._row(c, wid)
            if p["control_version"] != w["control_version"]:
                c.execute("UPDATE window_pending SET state='void', void_reason='version_changed', resolved_at=?, resolved_win_seq=? WHERE pending_id=?", (self.clock(), seq, p["pending_id"]))
                return "void"
            if seq <= p["request_win_seq"]:
                return None
            if is_confirm_line(text):
                return "confirm"
            c.execute("UPDATE window_pending SET state='void', void_reason='next_input_not_confirm', resolved_at=?, resolved_win_seq=? WHERE pending_id=?", (self.clock(), seq, p["pending_id"]))
            return "void_other"

    def _resolve_pending(self, wid, state, why, seq):
        with self._lock, self.store.tx() as c:
            c.execute("UPDATE window_pending SET state=?, void_reason=?, resolved_at=?, resolved_win_seq=? WHERE window_id=? AND state='waiting'", (state, why, self.clock(), seq, wid))

    def request_open(self, ident, *, ns=""):
        """agent 调 window_on（不带参数）。已经开着：直接返回；被设置挡住：返回，不登记；其余登记一条待确认（窗口、当前控制版本、申请这一轮的身份、窗口内输入序号），
        返回「等待用户确认」和固定提示，agent 原样转告用户。agent 不能替用户确认：这里只登记，确认只发生在 process_input 里。"""
        w = self.resolve(ident.host, ident.session_id, ident.branch or "")
        if w.paused:
            return OpenRequest("paused", "off", False, w.pause_reason, 0, None)
        try:
            blocked = self.settings(ident.host, ident.session_id)
        except Exception:                                                              # noqa: BLE001
            blocked = "workspace"
        with self._lock, self.store.tx() as c:
            row = self._row(c, w.window_id)
            if blocked:
                return OpenRequest("blocked_by_setting", row["state"], True, "setting_%s" % blocked, row["control_version"], None)
            if row["state"] == "on":
                return OpenRequest("on", "on", False, "ok", row["control_version"], None)
            c.execute("UPDATE window_pending SET state='void', void_reason='superseded', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), w.window_id))
            c.execute("UPDATE delete_pending SET state='void', void_reason='superseded', resolved_at=? WHERE window_id=? AND state='waiting'", (self.clock(), w.window_id))
            c.execute("INSERT INTO window_pending (pending_id, window_id, control_version, host, session_id, branch, ns, turn_id, request_win_seq, requested_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,'waiting')",
                      ("pd-" + secrets.token_hex(8), w.window_id, row["control_version"], ident.host, ident.session_id, ident.branch or "", ns, ident.turn_id or "", row["input_seq"], self.clock()))
            return OpenRequest("waiting", "off", False, "awaiting_confirmation", row["control_version"], CONFIRM_PROMPT)

    # ---------------------------------------------------------------- 删除这段对话：agent 申请，用户下一条输入整条是「确认删除」才删
    def request_delete(self, ident, *, ns="", install_id=""):
        """agent 调 window_delete（不带参数）。只登记一条待确认（窗口、库身份、申请时的输入序号和捕获序号、控制版本、操作号），返回固定提示（范围只有计数）。
        窗口关着也能申请；开启和删除两种待确认互相排他。agent 不能替用户确认：确认只发生在 process_input 里。"""
        from .deletion import DELETE_PROMPT
        w = self.resolve(ident.host, ident.session_id, ident.branch or "")
        if w.paused:
            return DeleteRequest("paused", w.pause_reason, None, None, {})
        scope = self.deleter.counts_for_window(w.window_id) if self.deleter is not None else {"workspaces": 0, "sessions": 0, "messages": 0}
        with self._lock, self.store.tx() as c:
            row = self._row(c, w.window_id)
            now = self.clock()
            c.execute("UPDATE window_pending SET state='void', void_reason='superseded', resolved_at=? WHERE window_id=? AND state='waiting'", (now, w.window_id))
            c.execute("UPDATE delete_pending SET state='void', void_reason='superseded', resolved_at=? WHERE window_id=? AND state='waiting'", (now, w.window_id))
            pid = "dp-" + secrets.token_hex(8)
            c.execute("INSERT INTO delete_pending (pending_id, window_id, db_id, control_version, host, session_id, branch, ns, turn_id, request_win_seq, request_capture, requested_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'waiting')",
                      (pid, w.window_id, self.store.db_id(), row["control_version"], ident.host, ident.session_id, ident.branch or "", ns, ident.turn_id or "", row["input_seq"],
                       self.store.take_capture_in(c), now))
        return DeleteRequest("waiting", "awaiting_delete_confirmation", pid, DELETE_PROMPT % (scope["workspaces"], scope["messages"]), scope)

    def _do_delete(self, wid, seq, ref, capture):
        """确认那一轮：同一个事务里消费待确认、登记任务、删行、立屏障（阶段 1）；之后的清理由接头交给后台线程（finish）。"""
        from .deletion import DELETE_FAILED, DeleteResult
        if self.deleter is None:
            return DeleteResult(None, "failed", "E_NO_DELETER", {}, None, None, DELETE_FAILED % "E_NO_DELETER")
        with self._lock:
            dp = self.store.conn.execute("SELECT pending_id FROM delete_pending WHERE window_id=? AND state='waiting' ORDER BY requested_at DESC, rowid DESC LIMIT 1", (wid,)).fetchone()
        if dp is None:
            return DeleteResult(None, "refused", "E_PENDING_GONE", {}, None, None, DELETE_FAILED % "E_PENDING_GONE")
        res = self.deleter.delete_conversation(wid, source="chat", operator="user", pending_id=dp["pending_id"], input_win_seq=seq, confirm_turn=(ref.host, ref.turn_id), capture=capture, finish=False)
        if res.status in ("failed", "refused"):                                           # 回滚了：待确认作废，用户要重来就得让 agent 重新申请
            with self._lock, self.store.tx() as c:
                c.execute("UPDATE delete_pending SET state='void', void_reason='confirm_failed', resolved_at=?, resolved_win_seq=? WHERE pending_id=? AND state='waiting'", (self.clock(), seq, dp["pending_id"]))
        return res

    # ---------------------------------------------------------------- 许可
    def _issue(self, kind, ident, tool_name, params_fp, scope_set, install_id, ns, call_id, ttl_s):
        def refuse(reason):
            return Permit(None, None, None, None, None, reason, refusal_class(reason))
        if ident is None:
            return refuse("identity_unbound")
        if not self._ok_key(ident.host, ident.session_id, ident.branch or ""):
            return refuse("identity_incomplete")
        if ident.turn_id is None:
            return refuse("turn_unproven")
        w = self.resolve(ident.host, ident.session_id, ident.branch or "")
        if w.paused:
            return refuse(w.pause_reason)
        if kind == "read":
            try:
                blocked = self.settings(ident.host, ident.session_id)
            except Exception:                                                          # noqa: BLE001
                blocked = "workspace"
            if blocked:
                return refuse("%s_setting" % blocked)
            if w.state != "on":
                return refuse("window_closed")
        secret = secrets.token_hex(24)
        sha = _sha(secret)
        now = calendar.timegm(time.strptime(self.clock(), "%Y-%m-%dT%H:%M:%SZ"))          # 过期按服务自己的时钟算（测试里的假时钟也一致）
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ttl_s))
        with self._lock, self.store.tx() as c:
            cur = self._row(c, w.window_id)
            if kind == "read" and cur["state"] != "on":
                return refuse("window_closed")
            g = c.execute("SELECT gen FROM turns WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?", (ns, ident.host, ident.session_id, ident.branch or "", ident.turn_id)).fetchone()
            c.execute("INSERT INTO window_permits (permit_sha, kind, window_id, host, install_id, scope_set, session_id, branch, ns, turn_id, turn_gen, input_win_seq, tool_name, params_fp, control_version, call_id, issued_at, expires_at, state) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'issued')",
                      (sha, kind, w.window_id, ident.host, install_id, json.dumps(sorted(scope_set or ())), ident.session_id, ident.branch or "", ns, ident.turn_id, g["gen"] if g else 0, cur["input_seq"],
                       tool_name, params_fp, cur["control_version"], call_id, self.clock(), exp))
            return Permit(secret, sha, w.window_id, cur["control_version"], exp, None, None)

    def issue_read_permit(self, ident, *, tool_name, params_fp, scope_set=(), install_id="", ns="", call_id=None, ttl_s=120):
        """窗口关着、暂停、设置挡住：不签发（返回带拒绝原因的 Permit）。关闭以后不再新增读取。"""
        return self._issue("read", ident, tool_name, params_fp, scope_set, install_id, ns, call_id, ttl_s)

    def issue_control_permit(self, ident, *, op, params_fp, scope_set=(), install_id="", ns="", call_id=None, ttl_s=120):
        """控制许可：关着的时候也能签发（否则永远打不开）；暂停的窗口不签发。op：close｜open（申请开启）｜view。"""
        return self._issue("control", ident, op, params_fp, scope_set, install_id, ns, call_id, ttl_s)

    def claim_permit(self, secret, *, kind, host, install_id, session_id, branch, ns, turn_id, tool_name, params_fp, scope_set=(), call_id=None):
        """原子认领一次（发生在任何查询、向量调用、缓存材料返回之前）。每个绑定字段都核；窗口控制版本变了→这张许可作废。绑定对不上只拒绝，不消耗许可。"""
        if not isinstance(secret, str) or not secret:
            return ClaimResult(False, "unknown_permit", None, None)
        sha = _sha(secret)
        with self._lock, self.store.tx() as c:
            p = c.execute("SELECT * FROM window_permits WHERE permit_sha=?", (sha,)).fetchone()
            if p is None:
                return ClaimResult(False, "unknown_permit", None, None)
            if p["kind"] != kind:
                return ClaimResult(False, "wrong_kind", p["window_id"], None)
            if p["state"] != "issued":
                return ClaimResult(False, "already_used" if p["state"] in ("claimed", "delivered", "blocked") else p["state"], p["window_id"], None)
            if p["expires_at"] < self.clock():
                c.execute("UPDATE window_permits SET state='expired', finished_at=? WHERE permit_sha=?", (self.clock(), sha))
                return ClaimResult(False, "expired", p["window_id"], None)
            bound = (("host", host), ("install_id", install_id), ("session_id", session_id), ("branch", branch or ""), ("ns", ns), ("turn_id", turn_id), ("tool_name", tool_name), ("params_fp", params_fp))
            for k, v in bound:
                if p[k] != v:
                    return ClaimResult(False, "mismatch_%s" % k, p["window_id"], None)
            if p["scope_set"] != json.dumps(sorted(scope_set or ())):
                return ClaimResult(False, "mismatch_scope_set", p["window_id"], None)
            if p["call_id"] is not None and p["call_id"] != call_id:
                return ClaimResult(False, "mismatch_call_id", p["window_id"], None)
            w = self._row(c, p["window_id"])
            if w["control_version"] != p["control_version"] or (kind == "read" and w["state"] != "on"):
                c.execute("UPDATE window_permits SET state='void', void_reason='version_changed', finished_at=? WHERE permit_sha=?", (self.clock(), sha))
                return ClaimResult(False, "version_changed" if w["state"] == "on" or kind == "control" else "window_closed", p["window_id"], w["control_version"])
            n = c.execute("UPDATE window_permits SET state='claimed', claimed_at=? WHERE permit_sha=? AND state='issued'", (self.clock(), sha)).rowcount
            if n != 1:
                return ClaimResult(False, "already_used", p["window_id"], None)
            return ClaimResult(True, "ok", p["window_id"], w["control_version"])

    def verify_before_return(self, secret):
        """返回前再核：同一窗口、同一张许可、控制版本没变、窗口还开着。不通过：许可记为 blocked，结果不能交（接头照实记「已查询」，回执说窗口关着）。"""
        sha = _sha(secret) if isinstance(secret, str) else ""
        with self._lock, self.store.tx() as c:
            p = c.execute("SELECT * FROM window_permits WHERE permit_sha=?", (sha,)).fetchone()
            if p is None or p["state"] != "claimed":
                return ReturnCheck(False, "permit_not_claimed")
            w = self._row(c, p["window_id"])
            if w["control_version"] != p["control_version"] or w["state"] != "on":
                c.execute("UPDATE window_permits SET state='blocked', void_reason=?, finished_at=? WHERE permit_sha=?", ("window_closed" if w["state"] != "on" else "version_changed", self.clock(), sha))
                return ReturnCheck(False, "window_closed" if w["state"] != "on" else "version_changed")
            c.execute("UPDATE window_permits SET state='delivered', finished_at=? WHERE permit_sha=?", (self.clock(), sha))
            return ReturnCheck(True, "ok")

    def finish_control_permit(self, secret):
        sha = _sha(secret) if isinstance(secret, str) else ""
        with self._lock, self.store.tx() as c:
            c.execute("UPDATE window_permits SET state='delivered', finished_at=? WHERE permit_sha=? AND kind='control' AND state='claimed'", (self.clock(), sha))

    def void_permit(self, secret, reason):
        sha = _sha(secret) if isinstance(secret, str) else ""
        with self._lock, self.store.tx() as c:
            return c.execute("UPDATE window_permits SET state='void', void_reason=?, finished_at=? WHERE permit_sha=? AND state IN ('issued','claimed')", (reason, self.clock(), sha)).rowcount == 1

    def void_window_permits(self, window_id, reason):
        with self._lock, self.store.tx() as c:
            return c.execute("UPDATE window_permits SET state='void', void_reason=?, finished_at=? WHERE window_id=? AND state IN ('issued','claimed')", (reason, self.clock(), window_id)).rowcount
