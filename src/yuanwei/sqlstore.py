"""SQLite 存储：一个文件，多个工作区（命名空间 ns）。

分开存（每样一张表）：原话（messages）、步（steps，同时就是整理队列）、步绑定（bindings）、整理结果（org_rows：主线、回、摘要版本、句子、挂接、缺口、收据，
只追加）、两问的判断记录（judgments）、放弃与跳过记录（收据里 kind=op）、暂停状态（workspaces 的几列）、费用账（cost_ledger）、续读游标（cursors）、
锚点（anchors，可重建，带索引版本）、向量缓存（vector_identities／vectors，选装，绑定编码身份）。
带 schema 版本表和迁移框架（v1；v2 给 steps 加了 last_failed_at，失败步的退避用；v3 加 turn_marks，记「这一轮不许读历史」的轮次标记；v4 加 read_settings（工作区／会话一级的持续读取设置）和 current_turn（最近一次声明的轮次）；v5 给 steps 加 config_snapshot，封步时存完整的写入配置快照；v6（第二段）加绑定表：turns＝按（工作区、宿主、会话、分支）分开的轮次登记，read_log＝读取登记，connections＝连接句柄（只存哈希），dir_state＝目录模式的「要不要重交」标记；v7（第二段补充一）：connections 加 turn_id＝逐轮能力，新表 turn_tokens＝同一个宿主事件重送时复用同一个轮次号；v8（第二段乙）：inputs＝每条用户输入各一行（来源待核／核过／拒绝），tool_calls＝宿主给的调用编号到（会话、分支、轮次）的登记；v9：turn_messages＝某一轮绑定了哪些消息，本轮排除用；v10：窗口层——windows＝逻辑窗口的开关和控制版本，window_bindings＝宿主编号到窗口，window_inputs＝每条可信输入在窗口层的处理结果，window_ops＝控制操作（只追加），window_permits＝控制许可和读取许可，window_pending＝「待确认开启」（agent 申请、用户下一条输入第一行是「确认开启」才开）；这六张表不带工作区列作键，窗口状态不按工作区分区）。

写入都是短事务（BEGIN IMMEDIATE）；调模型时不占写事务。整理结果的提交在同一个事务里重核 expected_prev_seq（别的进程抢先提交就报 StaleBase）。
可见性（两种截点格式，见 cuts.py）：
  新格式 {cut_format: 2, ns, raw_seq, org_seq}：原话入库提交成功（seq ≤ raw_seq）就可读，不等整理；整理按 created_seq ≤ org_seq，整理结果和收据一次提交，org_seq 随之前进，之后的新截点就看得到，不等步结束。
  旧格式 {vclock, org_seq}：每条原话有 visible_at（它所属的步结束时的 vclock），只交 visible_at ≤ vclock 的；visible_at 和 vclock 照旧维护（旧截点、步的结束记录用）。
v9 加 turn_messages：可信接头给出的「某一轮绑定了哪些消息」（本轮排除用）。
"""
import array
import contextlib
import json
import sqlite3
import time

from . import contract as WC
from . import lstate as LS
import hashlib

from .errors import BadCut, HistoryDeleted
from .storeerr import BlockedByDeletion, DeletionInterrupt, MessageConflict, PublishedRange, StepConflict, StoreError

SCHEMA_VERSION = 12
CUT_FORMAT = 2

MIGRATIONS = {
    1: [
        """CREATE TABLE workspaces (
            ns TEXT PRIMARY KEY, created_at TEXT, store_kind TEXT NOT NULL, store_version INTEGER NOT NULL, index_version INTEGER NOT NULL,
            last_seq INTEGER NOT NULL DEFAULT 0, org_seq INTEGER NOT NULL DEFAULT 0, vclock INTEGER NOT NULL DEFAULT 0,
            paused TEXT, paused_at TEXT, consec_abandons INTEGER NOT NULL DEFAULT 0, budget_day TEXT, config TEXT)""",
        """CREATE TABLE messages (
            ns TEXT NOT NULL, msg_id TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, host_msg_id TEXT NOT NULL,
            seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, msg_time TEXT, time_precision TEXT, received_at TEXT NOT NULL,
            fingerprint TEXT NOT NULL, is_long INTEGER NOT NULL DEFAULT 0, step_id TEXT, visible_at INTEGER,
            PRIMARY KEY (ns, msg_id), UNIQUE (ns, host, session_id, branch, host_msg_id), UNIQUE (ns, seq))""",
        "CREATE INDEX messages_step ON messages (ns, step_id)",
        """CREATE TABLE steps (
            ns TEXT NOT NULL, step_id TEXT NOT NULL, k INTEGER NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL,
            status TEXT NOT NULL, msg_ids TEXT NOT NULL, long_msg_ids TEXT NOT NULL, sealed_at TEXT, sealed_reason TEXT, ended_at TEXT, vclock_end INTEGER,
            prompt_version TEXT, prompt_sha256 TEXT, config_version TEXT, failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, note TEXT,
            PRIMARY KEY (ns, step_id), UNIQUE (ns, k))""",
        "CREATE TABLE bindings (ns TEXT NOT NULL, step_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (ns, step_id))",
        "CREATE TABLE org_rows (id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, tbl TEXT NOT NULL, key TEXT NOT NULL, created_seq INTEGER NOT NULL, data TEXT NOT NULL)",
        "CREATE INDEX org_rows_ns ON org_rows (ns, id)",
        "CREATE TABLE judgments (ns TEXT NOT NULL, step_id TEXT NOT NULL, question TEXT NOT NULL, attempt INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (ns, step_id, question, attempt))",
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, kind TEXT NOT NULL, at_seq INTEGER NOT NULL, ts TEXT)",
        """CREATE TABLE cost_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, step_id TEXT, ts TEXT NOT NULL, day TEXT NOT NULL, model TEXT, fingerprint TEXT,
            usage TEXT, cny REAL NOT NULL, unknown INTEGER NOT NULL DEFAULT 0, note TEXT)""",
        "CREATE TABLE cursors (cursor_id TEXT PRIMARY KEY, ns TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT, data TEXT NOT NULL)",
        """CREATE TABLE anchors (ns TEXT NOT NULL, anchor_id TEXT NOT NULL, msg_id TEXT NOT NULL, ord INTEGER NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL,
            start INTEGER NOT NULL, end INTEGER NOT NULL, index_version INTEGER NOT NULL, PRIMARY KEY (ns, anchor_id))""",
        "CREATE INDEX anchors_msg ON anchors (ns, msg_id)",
        "CREATE TABLE vector_identities (fp TEXT PRIMARY KEY, data TEXT NOT NULL)",
        "CREATE TABLE vectors (fp TEXT NOT NULL, text_sha TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (fp, text_sha))",
    ],
    2: [
        "ALTER TABLE steps ADD COLUMN last_failed_at TEXT",
    ],
    3: [
        "CREATE TABLE turn_marks (ns TEXT NOT NULL, turn_id TEXT NOT NULL, refused INTEGER NOT NULL, created_at TEXT, PRIMARY KEY (ns, turn_id))",
    ],
    4: [
        "CREATE TABLE read_settings (ns TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, allowed INTEGER NOT NULL, set_at TEXT, PRIMARY KEY (ns, scope, key))",
        "CREATE TABLE current_turn (ns TEXT PRIMARY KEY, turn_id TEXT NOT NULL, session_id TEXT, at TEXT)",
    ],
    5: [
        "ALTER TABLE steps ADD COLUMN config_snapshot TEXT",
    ],
    6: [
        """CREATE TABLE turns (
            ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL, gen INTEGER NOT NULL,
            state TEXT NOT NULL, origin TEXT NOT NULL, refused INTEGER NOT NULL DEFAULT 0, refuse_reason TEXT, persistent INTEGER NOT NULL DEFAULT 0,
            control_kind TEXT, control_span TEXT, created_at TEXT, completed_at TEXT, closed_at TEXT,
            PRIMARY KEY (ns, host, session_id, branch, turn_id), UNIQUE (ns, host, session_id, branch, gen))""",
        """CREATE TABLE read_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL,
            gen INTEGER NOT NULL, call_id TEXT, entry TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL, ts TEXT)""",
        "CREATE INDEX read_log_turn ON read_log (ns, host, session_id, branch, turn_id, id)",
        """CREATE TABLE connections (
            handle_sha TEXT PRIMARY KEY, ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, created_at TEXT,
            claimed_by TEXT, claimed_at TEXT, closed_at TEXT)""",
        """CREATE TABLE dir_state (
            ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT,
            directory_version TEXT, updated_at TEXT, PRIMARY KEY (ns, host, session_id, branch))""",
    ],
    7: [
        "ALTER TABLE connections ADD COLUMN turn_id TEXT",
        """CREATE TABLE turn_tokens (
            ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, event_key TEXT NOT NULL, turn_id TEXT NOT NULL, created_at TEXT,
            PRIMARY KEY (ns, host, session_id, branch, event_key))""",
    ],
    8: [
        """CREATE TABLE inputs (
            ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL, seq INTEGER NOT NULL,
            input_key TEXT NOT NULL, fp TEXT NOT NULL, state TEXT NOT NULL, label TEXT, ctl_kind TEXT, ctl_span TEXT, ban_reason TEXT, ban_persistent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT, resolved_at TEXT,
            PRIMARY KEY (ns, host, session_id, branch, turn_id, seq), UNIQUE (ns, host, session_id, branch, turn_id, input_key))""",
        """CREATE TABLE tool_calls (
            ns TEXT NOT NULL, host TEXT NOT NULL, call_id TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL, gen INTEGER NOT NULL,
            conflict INTEGER NOT NULL DEFAULT 0, created_at TEXT, PRIMARY KEY (ns, host, call_id))""",
    ],
    9: [
        """CREATE TABLE turn_messages (
            ns TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL, msg_id TEXT NOT NULL,
            PRIMARY KEY (ns, host, session_id, branch, turn_id, msg_id))""",
    ],
    10: [
        """CREATE TABLE windows (
            window_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('on','off')), control_version INTEGER NOT NULL,
            ctl_source TEXT NOT NULL, ctl_op_id TEXT, ctl_at TEXT,
            origin TEXT NOT NULL CHECK (origin IN ('new','fork','restore')), parent_window_id TEXT, host TEXT NOT NULL, created_at TEXT NOT NULL,
            input_seq INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE window_bindings (
            host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, window_id TEXT NOT NULL,
            bound_via TEXT NOT NULL, host_parent_session_id TEXT, bound_at TEXT NOT NULL,
            PRIMARY KEY (host, session_id, branch))""",
        """CREATE TABLE window_inputs (
            window_id TEXT NOT NULL, win_seq INTEGER NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, turn_id TEXT NOT NULL,
            input_key TEXT NOT NULL, fp TEXT NOT NULL, ns TEXT, result TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (window_id, win_seq), UNIQUE (window_id, host, session_id, branch, turn_id, input_key))""",
        """CREATE TABLE window_ops (
            op_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('close','open')), source TEXT NOT NULL, input_win_seq INTEGER,
            version_before INTEGER NOT NULL, version_after INTEGER NOT NULL, outcome TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, confirmed_at TEXT)""",
        """CREATE TABLE window_permits (
            permit_sha TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('read','control')), window_id TEXT NOT NULL, host TEXT NOT NULL, install_id TEXT NOT NULL, scope_set TEXT NOT NULL,
            session_id TEXT NOT NULL, branch TEXT NOT NULL, ns TEXT NOT NULL, turn_id TEXT NOT NULL, turn_gen INTEGER NOT NULL, input_win_seq INTEGER NOT NULL,
            tool_name TEXT NOT NULL, params_fp TEXT NOT NULL, control_version INTEGER NOT NULL, call_id TEXT, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('issued','claimed','delivered','blocked','void','expired')), claimed_at TEXT, finished_at TEXT, void_reason TEXT)""",
        "CREATE INDEX window_permits_window ON window_permits (window_id, state)",
        """CREATE TABLE window_pending (
            pending_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, control_version INTEGER NOT NULL,
            host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, ns TEXT, turn_id TEXT NOT NULL, request_win_seq INTEGER NOT NULL,
            requested_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('waiting','opened','void')), void_reason TEXT, resolved_at TEXT, resolved_win_seq INTEGER)""",
        "CREATE INDEX window_pending_window ON window_pending (window_id, state)",
    ],
    11: [
        "ALTER TABLE workspaces ADD COLUMN del_ver INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workspaces ADD COLUMN step_hwm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workspaces ADD COLUMN thread_hwm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workspaces ADD COLUMN occ_hwm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workspaces ADD COLUMN gap_hwm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE workspaces ADD COLUMN redact_json TEXT",
        "ALTER TABLE messages ADD COLUMN capture_seq INTEGER",
        "ALTER TABLE messages ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN redact_ver INTEGER NOT NULL DEFAULT 0",
        "UPDATE workspaces SET step_hwm = COALESCE((SELECT MAX(k) FROM steps WHERE steps.ns = workspaces.ns), 0)",
        "CREATE TABLE store_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
        "INSERT INTO store_meta VALUES ('db_id', lower(hex(randomblob(16))))",
        "CREATE TABLE captures (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL)",
        "INSERT INTO captures VALUES (1, 0)",
        """CREATE TABLE deletions (
            del_id TEXT PRIMARY KEY, window_id TEXT, conv_kind TEXT NOT NULL CHECK (conv_kind IN ('window','legacy')), conv_sha TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('command','chat')), state TEXT NOT NULL CHECK (state IN ('registered','files_cleaned','compacted','checkpointed','complete')),
            code TEXT, floor_capture INTEGER NOT NULL, requested_at TEXT, registered_at TEXT NOT NULL, completed_at TEXT, counts TEXT NOT NULL, plan TEXT,
            pending_id TEXT, input_win_seq INTEGER, db_id TEXT NOT NULL)""",
        "CREATE TABLE del_scope (del_id TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, PRIMARY KEY (host, session_id, branch, del_id))",
        "CREATE TABLE del_keys (key_sha TEXT PRIMARY KEY, del_id TEXT NOT NULL)",
        "CREATE TABLE no_record_turns (turn_id TEXT PRIMARY KEY, host TEXT NOT NULL, window_id TEXT, reason TEXT NOT NULL, del_id TEXT, capture_seq INTEGER)",
        "CREATE TABLE deleted_steps (ns TEXT NOT NULL, k INTEGER NOT NULL, del_id TEXT NOT NULL, PRIMARY KEY (ns, k))",
        """CREATE TABLE delete_pending (
            pending_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, db_id TEXT NOT NULL, control_version INTEGER NOT NULL,
            host TEXT NOT NULL, session_id TEXT NOT NULL, branch TEXT NOT NULL, ns TEXT, turn_id TEXT NOT NULL,
            request_win_seq INTEGER NOT NULL, request_capture INTEGER NOT NULL, requested_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('waiting','confirmed','void')), void_reason TEXT, resolved_at TEXT, resolved_win_seq INTEGER, del_id TEXT)""",
        "CREATE INDEX delete_pending_window ON delete_pending (window_id, state)",
    ],
    12: [
        "ALTER TABLE cost_ledger ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'",
    ],
}


class AlreadySealed(StoreError):
    """封步时发现这批消息里已经有被别的封步者取得的：整笔回滚，调用方重读后再封。"""


class ResponseLost(RuntimeError):
    """测试用：提交已生效，响应丢失。"""


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path, busy_ms=3000):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=busy_ms / 1000.0, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA secure_delete=ON")                 # 删除释放的页置零；不用 FAST。读回核实，核实不了就不继续
        if self.conn.execute("PRAGMA secure_delete").fetchone()[0] != 1:
            raise StoreError("secure_delete 没能打开（读回不是 1）：不继续")
        self.fault = None          # 测试注入：fault(point) 里可以抛异常，point ∈ before_commit／after_commit
        self._migrate()

    @classmethod
    def open_readonly(cls, path):
        """只读打开（status 用）：不迁移、不写任何东西。"""
        s = object.__new__(cls)
        s.path, s.fault = path, None
        s.conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3.0, isolation_level=None, check_same_thread=False)
        s.conn.row_factory = sqlite3.Row
        return s

    # ---------------------------------------------------------------- 事务与迁移
    @contextlib.contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _fire(self, point):
        if self.fault is not None:
            self.fault(point)

    @contextlib.contextmanager
    def read_tx(self):
        """一致的读事务（快照用）：里面的几次查询看到同一个时点。已经在事务里就直接用。"""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        finally:
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- 删除相关：捕获序号、删除版本、屏障、守卫
    def take_capture(self):
        """全库单调的捕获序号：事件进来的那一刻取号，号带到待补、整理、回调，删除时用来划边界。"""
        with self.tx() as c:
            return self.take_capture_in(c)

    @staticmethod
    def take_capture_in(c):
        c.execute("UPDATE captures SET n = n + 1 WHERE id = 1")
        return c.execute("SELECT n FROM captures WHERE id = 1").fetchone()[0]

    def del_ver(self, ns):
        r = self.conn.execute("SELECT del_ver FROM workspaces WHERE ns=?", (ns,)).fetchone()
        return r[0] if r else 0

    def db_id(self):
        r = self.conn.execute("SELECT v FROM store_meta WHERE k='db_id'").fetchone()
        return r[0] if r else ""

    @staticmethod
    def key_sha(host, session_id, branch, message_id):
        return hashlib.sha256("\x1f".join((host, session_id, branch or "", message_id)).encode("utf-8")).hexdigest()

    @staticmethod
    def _blocked(c, host, session_id, branch, message_id, turn_id, capture, received_at=None):
        if turn_id and c.execute("SELECT 1 FROM no_record_turns WHERE turn_id=?", (turn_id,)).fetchone():
            return "no_record_turn"
        if c.execute("SELECT 1 FROM del_keys WHERE key_sha=?", (Store.key_sha(host, session_id, branch, message_id),)).fetchone():
            return "deleted_key"
        if capture is not None and c.execute("SELECT 1 FROM del_scope s JOIN deletions d ON d.del_id=s.del_id WHERE s.host=? AND s.session_id=? AND s.branch=? AND ? <= d.floor_capture",
                                             (host, session_id, branch or "", capture)).fetchone():
            return "before_barrier"
        if capture is None and received_at is not None and c.execute("SELECT 1 FROM del_scope s JOIN deletions d ON d.del_id=s.del_id WHERE s.host=? AND s.session_id=? AND s.branch=? AND ? <= d.registered_at",
                                                                      (host, session_id, branch or "", received_at)).fetchone():
            return "before_barrier"                      # 没有捕获序号（数据库忙时没取到号的、v11 之前写的待补行）：按收到的时间和删除登记的时间比
        return None

    def blocked(self, host, session_id, branch, message_id, turn_id, capture, received_at=None):
        """预检（不权威）：这条写入会不会被删除的屏障挡住。返回原因或 None。"""
        return self._blocked(self.conn, host, session_id, branch, message_id, turn_id, capture, received_at)

    @staticmethod
    def _check_guard(c, guard):
        if guard is None:
            return
        ns, dv = guard
        r = c.execute("SELECT del_ver FROM workspaces WHERE ns=?", (ns,)).fetchone()
        if r is None or r[0] != dv:
            raise DeletionInterrupt("工作区 %r 在这件事进行中发生了删除（删除版本 %s → %s）" % (ns, dv, r[0] if r else None))

    def guarded(self, ns):
        """整理用的存储代理：记下此刻的删除版本，每个写方法在事务里核一次，变了就抛 DeletionInterrupt。"""
        return GuardedStore(self, ns, self.del_ver(ns))

    def check_cut_v1(self, ns, cut):
        if (cut.get("del_ver") or 0) != self.del_ver(ns):
            raise HistoryDeleted("历史已删除，请重新查询")

    def schema_version(self):
        try:
            r = self.conn.execute("SELECT version FROM schema_meta").fetchone()
        except sqlite3.OperationalError:
            return 0
        return r["version"] if r else 0

    def _migrate(self):
        cur = self.schema_version()
        if cur > SCHEMA_VERSION:
            raise StoreError("数据库的 schema 版本 %d 比这个程序认识的 %d 新" % (cur, SCHEMA_VERSION))
        if cur == SCHEMA_VERSION:
            return
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
            row = c.execute("SELECT version FROM schema_meta").fetchone()
            have = row["version"] if row else 0
            if have == 0 and not row:
                c.execute("INSERT INTO schema_meta VALUES (0)")
            for v in range(have + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[v]:
                    c.execute(stmt)
                c.execute("UPDATE schema_meta SET version=?", (v,))

    def close(self):
        self.conn.close()

    # ---------------------------------------------------------------- 工作区
    def ensure_workspace(self, ns, index_version, kind="L", store_version=1, config=None, now=None):
        with self.tx() as c:
            r = c.execute("SELECT * FROM workspaces WHERE ns=?", (ns,)).fetchone()
            if r is None:
                c.execute("INSERT INTO workspaces (ns, created_at, store_kind, store_version, index_version, config) VALUES (?,?,?,?,?,?)",
                          (ns, now, kind, store_version, index_version, _dumps(config or {})))
                r = c.execute("SELECT * FROM workspaces WHERE ns=?", (ns,)).fetchone()
        if r["store_kind"] != kind:
            raise StoreError("工作区 %r 是 %s 存储，不能当 %s 用（一个工作区只用一种存储，不混写）" % (ns, r["store_kind"], kind))
        return dict(r)

    def workspace(self, ns):
        r = self.conn.execute("SELECT * FROM workspaces WHERE ns=?", (ns,)).fetchone()
        return dict(r) if r else None

    def set_workspace(self, ns, **cols):
        keys = list(cols)
        with self.tx() as c:
            c.execute("UPDATE workspaces SET %s WHERE ns=?" % ",".join("%s=?" % k for k in keys), [cols[k] for k in keys] + [ns])

    def cut(self, ns):
        """旧格式的当下截点：可见原话的 vclock 和整理提交序号（同一次读取里取，不拆开）。新读取用 cut_v2。"""
        r = self.conn.execute("SELECT vclock, org_seq FROM workspaces WHERE ns=?", (ns,)).fetchone()
        if r is None:
            return {"vclock": 0, "org_seq": 0}
        out = {"vclock": r["vclock"], "org_seq": r["org_seq"]}
        dv = self.del_ver(ns)
        if dv:
            out["del_ver"] = dv
        return out

    def cut_v2(self, ns, index_version):
        """新格式的当下截点：原话上界 raw_seq（已入库的最大序号）和整理上界 org_seq，从同一条工作区记录、同一次查询里取。"""
        r = self.conn.execute("SELECT last_seq, org_seq, del_ver FROM workspaces WHERE ns=?", (ns,)).fetchone()
        out = {"cut_format": CUT_FORMAT, "ns": ns, "raw_seq": r["last_seq"] if r else 0, "org_seq": r["org_seq"] if r else 0, "index_version": index_version}
        if r is not None and r["del_ver"]:                           # 只有发生过删除才带：没删除过的工作区截点一字不变
            out["del_ver"] = r["del_ver"]
        return out

    def check_cut_v2(self, ns, cut):
        """调用方自己传的新格式截点：是这个工作区的、两个上界都不超过现在、索引版本对得上。不合格抛 BadCut（不悄悄换成当前状态）。
        两个上界组合不一致（整理记录引用了原话上界之外的原话）要读了整理状态才查得出，在 Reader 的快照里查。"""
        w = self.conn.execute("SELECT last_seq, org_seq, index_version, del_ver FROM workspaces WHERE ns=?", (ns,)).fetchone()
        if cut.get("ns") != ns:
            raise BadCut("截点是工作区 %r 的，不能用在 %r" % (cut.get("ns"), ns))
        if w is None:
            raise BadCut("工作区 %r 不存在" % ns)
        if (cut.get("del_ver") or 0) != w["del_ver"]:
            raise HistoryDeleted("历史已删除，请重新查询")
        if cut["raw_seq"] > w["last_seq"] or cut["org_seq"] > w["org_seq"]:
            raise BadCut("截点超前：原话上界 %d（现在 %d）、整理上界 %d（现在 %d）" % (cut["raw_seq"], w["last_seq"], cut["org_seq"], w["org_seq"]))
        if cut["index_version"] != w["index_version"]:
            raise BadCut("截点的索引版本 %d 和工作区的 %d 不同" % (cut["index_version"], w["index_version"]))

    # ---------------------------------------------------------------- 原话
    @staticmethod
    def _contract(r):
        return {"ns": r["ns"], "msg_id": r["msg_id"], "session_id": r["session_id"], "seq": r["seq"], "role": r["role"], "content": r["content"],
                "msg_time": r["msg_time"], "received_at": r["received_at"], "fingerprint": r["fingerprint"]}

    def add_message(self, ns, host, session_id, branch, host_msg_id, role, content, msg_time, time_precision, received_at, msg_id, anchors_fn, is_long, turn_id=None,
                    capture_seq=None, redacted=0, redact_ver=0, kinds=(), equiv=None):
        """记一条原话。返回 (msg 行 dict, 是不是重送)。同键同文返回原来的；同键异文抛 MessageConflict。anchors_fn(seq, msg_id) 在事务里算锚点行。
        turn_id：可信接头给出的「这条消息属于哪一轮」，和原话在同一个事务里绑定（本轮排除用）；重送时也补上（同一条消息只绑一次，重复无害）。"""
        with self.tx() as c:
            old = c.execute("SELECT * FROM messages WHERE ns=? AND host=? AND session_id=? AND branch=? AND host_msg_id=?", (ns, host, session_id, branch, host_msg_id)).fetchone()
            if old is not None:
                if (old["role"], old["msg_time"]) != (role, msg_time) or (old["content"] != content and not (equiv is not None and equiv(old["content"], content))):
                    raise MessageConflict("同一条消息（%s／%s／%s／%s）已经记过，内容不同，拒绝覆盖" % (host, session_id, branch or "-", host_msg_id))
                if turn_id:
                    c.execute("INSERT OR IGNORE INTO turn_messages (ns,host,session_id,branch,turn_id,msg_id) VALUES (?,?,?,?,?,?)", (ns, host, session_id, branch, turn_id, old["msg_id"]))
                return dict(old), True
            why = self._blocked(c, host, session_id, branch, host_msg_id, turn_id, capture_seq, received_at)
            if why:
                raise BlockedByDeletion(why)
            if c.execute("SELECT 1 FROM messages WHERE ns=? AND msg_id=?", (ns, msg_id)).fetchone():
                raise MessageConflict("消息编号 %r 已被另一条消息占用" % msg_id)
            ws = c.execute("SELECT last_seq FROM workspaces WHERE ns=?", (ns,)).fetchone()
            seq = ws["last_seq"] + 1
            m = WC.make_message(ns, session_id, seq, msg_id, role, content, msg_time, received_at)
            WC.check_message(m)
            c.execute("UPDATE workspaces SET last_seq=? WHERE ns=?", (seq, ns))
            c.execute("INSERT INTO messages (ns,msg_id,host,session_id,branch,host_msg_id,seq,role,content,msg_time,time_precision,received_at,fingerprint,is_long,capture_seq,redacted,redact_ver) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ns, msg_id, host, session_id, branch, host_msg_id, seq, role, content, msg_time, time_precision, received_at, m["fingerprint"], 1 if is_long else 0, capture_seq, redacted, redact_ver))
            if redacted:                                               # 只在新行入库的这一次累计（重送不重复计）
                row = c.execute("SELECT redact_json FROM workspaces WHERE ns=?", (ns,)).fetchone()
                j = json.loads(row[0]) if row and row[0] else {"v": redact_ver, "messages": 0, "by_kind": {}}
                j["v"], j["messages"] = redact_ver, j.get("messages", 0) + 1
                for k in kinds:
                    j["by_kind"][k] = j["by_kind"].get(k, 0) + 1
                c.execute("UPDATE workspaces SET redact_json=? WHERE ns=?", (_dumps(j), ns))
            for a in anchors_fn(seq, msg_id):
                c.execute("INSERT INTO anchors (ns,anchor_id,msg_id,ord,seq,text,start,end,index_version) VALUES (?,?,?,?,?,?,?,?,?)",
                          (ns, a["anchor_id"], msg_id, a["ord"], seq, a["text"], a["start"], a["end"], a["index_version"]))
            if turn_id:
                c.execute("INSERT OR IGNORE INTO turn_messages (ns,host,session_id,branch,turn_id,msg_id) VALUES (?,?,?,?,?,?)", (ns, host, session_id, branch, turn_id, msg_id))
            row = c.execute("SELECT * FROM messages WHERE ns=? AND msg_id=?", (ns, msg_id)).fetchone()
            return dict(row), False

    def turn_excluded(self, ns, host, session_id, branch, turn_id):
        """某个工作区、宿主、会话、分支、轮次绑定的消息编号集合。五样都对上才算；不靠文字相同、最近几条、当天或最后一个序号去猜。"""
        if not (host and session_id is not None and turn_id):
            return frozenset()
        return frozenset(r["msg_id"] for r in self.conn.execute("SELECT msg_id FROM turn_messages WHERE ns=? AND host=? AND session_id=? AND branch=? AND turn_id=?",
                                                                (ns, host, session_id, branch or "", turn_id)))

    def save_message(self, msg, guard=None):
        """研究的存储接口：run_step 用。已有同指纹返回 duplicate；不同报冲突；没有就按合同消息直接加（宿主、分支记空）。"""
        WC.check_message(msg)
        ns = msg["ns"]
        with self.tx() as c:
            self._check_guard(c, guard)
            old = c.execute("SELECT fingerprint FROM messages WHERE ns=? AND msg_id=?", (ns, msg["msg_id"])).fetchone()
            if old is not None:
                if old["fingerprint"] == msg["fingerprint"]:
                    return "duplicate"
                raise MessageConflict("同一消息 ID %r 的内容不同，拒绝覆盖" % msg["msg_id"])
            ws = c.execute("SELECT last_seq FROM workspaces WHERE ns=?", (ns,)).fetchone()
            if ws is None:
                raise StoreError("工作区 %r 不存在" % ns)
            if msg["seq"] <= ws["last_seq"]:                      # 这个序号已经在发布过的原话范围里：不许往旧截点里补洞（补写用实际入库时拿到的新序号）
                raise PublishedRange("原话序号 %d 不大于这个工作区已入库的最大序号 %d，而且不是已有消息的重送：不能往已发布的范围里补洞" % (msg["seq"], ws["last_seq"]))
            c.execute("INSERT INTO messages (ns,msg_id,host,session_id,branch,host_msg_id,seq,role,content,msg_time,time_precision,received_at,fingerprint,is_long) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ns, msg["msg_id"], "", msg["session_id"], "", msg["msg_id"], msg["seq"], msg["role"], msg["content"], msg["msg_time"], None, msg["received_at"], msg["fingerprint"], 0))
            c.execute("UPDATE workspaces SET last_seq=? WHERE ns=?", (msg["seq"], ns))
            return "new"

    def get_message(self, ns, msg_id):
        r = self.conn.execute("SELECT * FROM messages WHERE ns=? AND msg_id=?", (ns, msg_id)).fetchone()
        return self._contract(r) if r else None

    def message_row(self, ns, msg_id):
        r = self.conn.execute("SELECT * FROM messages WHERE ns=? AND msg_id=?", (ns, msg_id)).fetchone()
        return dict(r) if r else None

    def message_by_host_key(self, ns, host, session_id, branch, host_msg_id):
        r = self.conn.execute("SELECT * FROM messages WHERE ns=? AND host=? AND session_id=? AND branch=? AND host_msg_id=?", (ns, host, session_id, branch, host_msg_id)).fetchone()
        return dict(r) if r else None

    def list_messages(self, ns, session_id=None):
        q = "SELECT * FROM messages WHERE ns=?" + (" AND session_id=?" if session_id is not None else "") + " ORDER BY seq"
        return [self._contract(r) for r in self.conn.execute(q, (ns,) + ((session_id,) if session_id is not None else ()))]

    def unsealed_messages(self, ns):
        return [dict(r) for r in self.conn.execute("SELECT * FROM messages WHERE ns=? AND step_id IS NULL ORDER BY seq", (ns,))]

    def visible_messages(self, ns, vclock):
        return [dict(r) for r in self.conn.execute("SELECT * FROM messages WHERE ns=? AND visible_at IS NOT NULL AND visible_at<=? ORDER BY seq", (ns, vclock))]

    def raw_messages(self, ns, raw_seq):
        """新格式截点：序号不大于原话上界的全部原话（入库提交成功就算，不看所属的步结束了没有）。"""
        return [dict(r) for r in self.conn.execute("SELECT * FROM messages WHERE ns=? AND seq<=? ORDER BY seq", (ns, raw_seq))]

    def raw_anchors(self, ns, raw_seq, index_version):
        return [dict(r) for r in self.conn.execute("SELECT a.* FROM anchors a JOIN messages m ON m.ns=a.ns AND m.msg_id=a.msg_id WHERE a.ns=? AND a.index_version=? AND m.seq<=? ORDER BY a.ord",
                                                   (ns, index_version, raw_seq))]

    def count_messages(self, ns, where="1=1", args=()):
        return self.conn.execute("SELECT COUNT(*) FROM messages WHERE ns=? AND %s" % where, (ns,) + tuple(args)).fetchone()[0]

    def visible_anchors(self, ns, vclock, index_version):
        q = ("SELECT a.* FROM anchors a JOIN messages m ON m.ns=a.ns AND m.msg_id=a.msg_id "
             "WHERE a.ns=? AND a.index_version=? AND m.visible_at IS NOT NULL AND m.visible_at<=? ORDER BY a.ord")
        return [dict(r) for r in self.conn.execute(q, (ns, index_version, vclock))]

    def anchors_of(self, ns, msg_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM anchors WHERE ns=? AND msg_id=? ORDER BY ord", (ns, msg_id))]

    # ---------------------------------------------------------------- 步（同时是队列）
    def seal_step(self, ns, host, session_id, branch, msg_ids, long_ids, step_id_fn, sealed_at, reason, prompt_version=None, prompt_sha256=None, config_version=None, config_snapshot=None):
        """封一步：这一步的消息和顺序从此固定。msg_ids 是要整理的，long_ids 是超长未整理的。k 是工作区里下一个步序号。
        「核消息仍未封 → 分配步号 → 占有这批消息 → 插入步」在同一个短事务里：占有用条件更新，实际改的行数不是 1 就说明已被别的封步者取得，整笔回滚并抛 AlreadySealed。"""
        with self.tx() as c:
            k = max(c.execute("SELECT COALESCE(MAX(k),0) FROM steps WHERE ns=?", (ns,)).fetchone()[0], c.execute("SELECT step_hwm FROM workspaces WHERE ns=?", (ns,)).fetchone()[0]) + 1       # 步号不复用：高水位
            step_id = step_id_fn(k)
            for mid in list(msg_ids) + list(long_ids):
                n = c.execute("UPDATE messages SET step_id=? WHERE ns=? AND msg_id=? AND step_id IS NULL", (step_id, ns, mid)).rowcount
                if n != 1:
                    raise AlreadySealed("消息 %r 已经被别的封步者取得（或不存在）" % (mid,))
            c.execute("INSERT INTO steps (ns,step_id,k,host,session_id,branch,status,msg_ids,long_msg_ids,sealed_at,sealed_reason,prompt_version,prompt_sha256,config_version,config_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ns, step_id, k, host, session_id, branch, "sealed", json.dumps(msg_ids), json.dumps(long_ids), sealed_at, reason, prompt_version, prompt_sha256, config_version, config_snapshot))
            return self._step(c.execute("SELECT * FROM steps WHERE ns=? AND step_id=?", (ns, step_id)).fetchone())

    @staticmethod
    def _step(r):
        if r is None:
            return None
        d = dict(r)
        d["msg_ids"], d["long_msg_ids"] = json.loads(d["msg_ids"]), json.loads(d["long_msg_ids"])
        return d

    def get_step(self, ns, step_id):
        return self._step(self.conn.execute("SELECT * FROM steps WHERE ns=? AND step_id=?", (ns, step_id)).fetchone())

    def list_steps(self, ns, status=None):
        q = "SELECT * FROM steps WHERE ns=?" + (" AND status=?" if status else "") + " ORDER BY k"
        return [self._step(r) for r in self.conn.execute(q, (ns,) + ((status,) if status else ()))]

    def update_step(self, ns, step_id, guard=None, **cols):
        keys = list(cols)
        with self.tx() as c:
            self._check_guard(c, guard)
            c.execute("UPDATE steps SET %s WHERE ns=? AND step_id=?" % ",".join("%s=?" % k for k in keys), [cols[k] for k in keys] + [ns, step_id])

    def reset_failed_steps(self, ns):
        with self.tx() as c:
            c.execute("UPDATE steps SET failures=0, last_failed_at=NULL, status='sealed' WHERE ns=? AND status='failed'", (ns,))

    def end_step(self, ns, step_id, status, ended_at, note=None, guard=None):
        """步结束（done／abandoned／skipped）：vclock 加一，这一步的原话（含超长条）从此可见。已经结束的重复调用是空操作。"""
        with self.tx() as c:
            self._check_guard(c, guard)
            r = c.execute("SELECT * FROM steps WHERE ns=? AND step_id=?", (ns, step_id)).fetchone()
            if r is None:
                raise StoreError("没有这一步：%r" % step_id)
            if r["status"] in ("done", "abandoned", "skipped"):
                return r["vclock_end"]
            vclock = c.execute("SELECT vclock FROM workspaces WHERE ns=?", (ns,)).fetchone()[0] + 1
            c.execute("UPDATE workspaces SET vclock=? WHERE ns=?", (vclock, ns))
            c.execute("UPDATE steps SET status=?, ended_at=?, vclock_end=?, note=? WHERE ns=? AND step_id=?", (status, ended_at, vclock, note, ns, step_id))
            c.execute("UPDATE messages SET visible_at=? WHERE ns=? AND step_id=?", (vclock, ns, step_id))
            return vclock

    # ---------------------------------------------------------------- 步绑定、判断记录
    def get_step_binding(self, ns, step_id):
        r = self.conn.execute("SELECT data FROM bindings WHERE ns=? AND step_id=?", (ns, step_id)).fetchone()
        return json.loads(r["data"]) if r else None

    def bind_step(self, ns, step_id, binding, guard=None):
        with self.tx() as c:
            self._check_guard(c, guard)
            r = c.execute("SELECT data FROM bindings WHERE ns=? AND step_id=?", (ns, step_id)).fetchone()
            if r:
                return json.loads(r["data"])
            c.execute("INSERT INTO bindings VALUES (?,?,?)", (ns, step_id, _dumps(binding)))
            return binding

    def save_judgment(self, ns, step_id, question, rec, guard=None):
        if question not in ("q1", "q2"):
            raise ValueError("question 只能是 q1 或 q2")
        with self.tx() as c:
            self._check_guard(c, guard)
            n = c.execute("SELECT COUNT(*) FROM judgments WHERE ns=? AND step_id=? AND question=?", (ns, step_id, question)).fetchone()[0] + 1
            rec = dict(rec, attempt=n, question=question)
            c.execute("INSERT INTO judgments VALUES (?,?,?,?,?)", (ns, step_id, question, n, _dumps(rec)))
            return n

    def update_judgment(self, ns, step_id, question, no, patch, guard=None):
        with self.tx() as c:
            self._check_guard(c, guard)
            r = c.execute("SELECT data FROM judgments WHERE ns=? AND step_id=? AND question=? AND attempt=?", (ns, step_id, question, no)).fetchone()
            rec = json.loads(r["data"])
            rec.update(patch)
            c.execute("UPDATE judgments SET data=? WHERE ns=? AND step_id=? AND question=? AND attempt=?", (_dumps(rec), ns, step_id, question, no))

    def archive_failed_attempts(self, ns, step_id, statuses=("network_error", "model_failed", "requested"), guard=None):
        """基础设施类失败（网络、模型出错、发送前登记了但没有回应）的尝试：从「已用的发送次数」里挪出去（改记到 q1~f1 这类归档名下，记录还在，审计用），
        这样退避以后重试时，发送次数不会被上一轮断网用光。格式不合格被拒收的尝试照旧算数（它们是真发出去、真被拒的）。返回挪走的条数。"""
        n = 0
        with self.tx() as c:
            self._check_guard(c, guard)
            for r in c.execute("SELECT question, attempt, data FROM judgments WHERE ns=? AND step_id=? AND question IN ('q1','q2')", (ns, step_id)).fetchall():
                if json.loads(r["data"]).get("status") not in statuses:
                    continue
                k = 1
                while c.execute("SELECT 1 FROM judgments WHERE ns=? AND step_id=? AND question=? AND attempt=?", (ns, step_id, "%s~f%d" % (r["question"], k), r["attempt"])).fetchone():
                    k += 1
                c.execute("UPDATE judgments SET question=? WHERE ns=? AND step_id=? AND question=? AND attempt=?", ("%s~f%d" % (r["question"], k), ns, step_id, r["question"], r["attempt"]))
                n += 1
            # 留下的同问记录重新按 1…n 编号，保持「第几次＝条数」
            for q in ("q1", "q2"):
                rows = c.execute("SELECT attempt, data FROM judgments WHERE ns=? AND step_id=? AND question=? ORDER BY attempt", (ns, step_id, q)).fetchall()
                c.execute("DELETE FROM judgments WHERE ns=? AND step_id=? AND question=?", (ns, step_id, q))
                for i, r in enumerate(rows, 1):
                    rec = json.loads(r["data"])
                    rec["attempt"] = i
                    c.execute("INSERT INTO judgments VALUES (?,?,?,?,?)", (ns, step_id, q, i, _dumps(rec)))
        return n

    def list_judgments(self, ns, step_id, question=None):
        q = "SELECT data FROM judgments WHERE ns=? AND step_id=?" + (" AND question=?" if question else "") + " ORDER BY question, attempt"
        return [json.loads(r["data"]) for r in self.conn.execute(q, (ns, step_id) + ((question,) if question else ()))]

    # ---------------------------------------------------------------- 整理结果
    def read_state(self, ns):
        w = self.conn.execute("SELECT org_seq FROM workspaces WHERE ns=?", (ns,)).fetchone()
        st = LS.empty_state(ns)
        if w is None:
            return st
        st["seq"] = w["org_seq"]
        for r in self.conn.execute("SELECT tbl, data FROM org_rows WHERE ns=? ORDER BY id", (ns,)):
            st["tables"][r["tbl"]].append(json.loads(r["data"]))
        h = self.conn.execute("SELECT thread_hwm, occ_hwm, gap_hwm FROM workspaces WHERE ns=?", (ns,)).fetchone()
        if h is not None and any(h):                                   # 删除留下的编号高水位：只有发生过删除才有
            st["hwm"] = {"thread": h[0], "occasion": h[1], "gap": h[2]}
        dk = [r[0] for r in self.conn.execute("SELECT k FROM deleted_steps WHERE ns=?", (ns,))]
        if dk:
            st["deleted_ks"] = sorted(dk)
        return st

    def commit(self, cs, guard=None):
        """整份校验、整份一次提交（一个事务）。校验用事务里重读的状态，所以别的进程抢先提交会报 StaleBase。返回收据。"""
        ns = cs["ns"]
        self._fire("before_commit")
        with self.tx() as c:
            self._check_guard(c, guard)
            state = self.read_state(ns)
            LS.validate_changeset(cs, state, self.get_message)
            for t in LS.TABLES:
                for r in cs["inserts"].get(t, []):
                    c.execute("INSERT INTO org_rows (ns,tbl,key,created_seq,data) VALUES (?,?,?,?,?)", (ns, t, r["key"], r["created_seq"], _dumps(r)))
            c.execute("UPDATE workspaces SET org_seq=? WHERE ns=?", (cs["new_seq"], ns))
        self._fire("after_commit")
        return cs["inserts"]["receipts"][0]

    # ---------------------------------------------------------------- 事件、暂停、费用
    def add_event(self, ns, host, session_id, branch, kind, at_seq, ts):
        with self.tx() as c:
            c.execute("INSERT INTO events (ns,host,session_id,branch,kind,at_seq,ts) VALUES (?,?,?,?,?,?,?)", (ns, host, session_id, branch, kind, at_seq, ts))

    def events(self, ns, host, session_id, branch):
        return [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE ns=? AND host=? AND session_id=? AND branch=? ORDER BY id", (ns, host, session_id, branch))]

    def add_cost(self, ns, step_id, ts, day, model, fingerprint, usage, cny, unknown, note=None, currency="CNY"):
        """cny 列的字面意思不改：金额按 currency 列的币种解读（CNY／USD／NONE）。NONE＝这个模型没有价格表：金额写 0，只记用量，不估价。"""
        with self.tx() as c:
            c.execute("INSERT INTO cost_ledger (ns,step_id,ts,day,model,fingerprint,usage,cny,unknown,note,currency) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (ns, step_id, ts, day, model, fingerprint, _dumps(usage) if usage is not None else None, cny, 1 if unknown else 0, note, currency))

    def cost_sum(self, ns, day=None):
        """原有三个键：cny＝只加 CNY 行；calls、unknown_calls＝所有行。另有 by_currency：各币种分开（不混加）；NONE 只数调用和词元。"""
        args = (ns,) + ((day,) if day else ())
        where = "WHERE ns=?" + (" AND day=?" if day else "")
        by = {}
        for r in self.conn.execute("SELECT currency, COALESCE(SUM(cny),0), COUNT(*), COALESCE(SUM(unknown),0) FROM cost_ledger %s GROUP BY currency" % where, args):
            by[r[0]] = {"amount": r[1], "calls": r[2], "unknown_calls": r[3]}
        none = by.get("NONE")
        if none is not None:
            tin = tout = 0
            for (u,) in self.conn.execute("SELECT usage FROM cost_ledger %s AND currency='NONE' AND usage IS NOT NULL" % where, args):
                try:
                    d = json.loads(u)
                    tin += int(d.get("prompt_tokens") or 0)
                    tout += int(d.get("completion_tokens") or 0)
                except (ValueError, TypeError, AttributeError):
                    pass
            none.pop("amount", None)
            none.update(tokens_in=tin, tokens_out=tout)
        return {"cny": by.get("CNY", {}).get("amount", 0), "calls": sum(v["calls"] for v in by.values()), "unknown_calls": sum(v["unknown_calls"] for v in by.values()), "by_currency": by}

    def cost_sum_all(self, day=None):
        """全库（所有工作区）的费用，按币种分开（不混加）。day 给了只算那一天（账里的日期是 UTC 日期）。返回 {"calls","unknown_calls","by_currency":{币种: {amount, calls, unknown_calls}}}；NONE（没有价格表）只数调用和词元。"""
        where, args = ("WHERE day=?", (day,)) if day else ("", ())
        by = {}
        for r in self.conn.execute("SELECT currency, COALESCE(SUM(cny),0), COUNT(*), COALESCE(SUM(unknown),0) FROM cost_ledger %s GROUP BY currency" % where, args):
            by[r[0]] = {"amount": r[1], "calls": r[2], "unknown_calls": r[3]}
        none = by.get("NONE")
        if none is not None:
            tin = tout = 0
            for (u,) in self.conn.execute("SELECT usage FROM cost_ledger %s %s currency='NONE' AND usage IS NOT NULL" % (where, "AND" if where else "WHERE"), args):
                try:
                    d = json.loads(u)
                    tin += int(d.get("prompt_tokens") or 0)
                    tout += int(d.get("completion_tokens") or 0)
                except (ValueError, TypeError, AttributeError):
                    pass
            none.pop("amount", None)
            none.update(tokens_in=tin, tokens_out=tout)
        return {"calls": sum(v["calls"] for v in by.values()), "unknown_calls": sum(v["unknown_calls"] for v in by.values()), "by_currency": by}

    def unrecorded_calls(self):
        """收到过模型回应（判断记录里有原始输出）、可是这一步在费用账里一行都没有的次数。正常是 0；不是 0 说明有整理调用没记上账。"""
        n = 0
        for r in self.conn.execute("SELECT ns, step_id, data FROM judgments"):
            try:
                if json.loads(r["data"]).get("raw_output") is None:
                    continue
            except ValueError:
                continue
            if self.conn.execute("SELECT 1 FROM cost_ledger WHERE ns=? AND step_id=? LIMIT 1", (r["ns"], r["step_id"])).fetchone() is None:
                n += 1
        return n

    def cost_rows(self, ns):
        return [dict(r) for r in self.conn.execute("SELECT * FROM cost_ledger WHERE ns=? ORDER BY id", (ns,))]

    # ---------------------------------------------------------------- 轮次标记
    def mark_turn(self, ns, turn_id, refused, created_at=None):
        with self.tx() as c:
            c.execute("INSERT INTO turn_marks VALUES (?,?,?,?) ON CONFLICT(ns, turn_id) DO UPDATE SET refused = MAX(refused, excluded.refused)", (ns, turn_id, 1 if refused else 0, created_at))

    def turn_refused(self, ns, turn_id):
        r = self.conn.execute("SELECT refused FROM turn_marks WHERE ns=? AND turn_id=?", (ns, turn_id)).fetchone()
        return bool(r and r["refused"])

    # ---------------------------------------------------------------- 持续的读取设置、最近声明的轮次
    def set_read_setting(self, ns, scope, key, allowed, set_at=None):
        with self.tx() as c:
            c.execute("INSERT INTO read_settings VALUES (?,?,?,?,?) ON CONFLICT(ns, scope, key) DO UPDATE SET allowed=excluded.allowed, set_at=excluded.set_at", (ns, scope, key, 1 if allowed else 0, set_at))

    def clear_read_setting(self, ns, scope, key):
        with self.tx() as c:
            return c.execute("DELETE FROM read_settings WHERE ns=? AND scope=? AND key=?", (ns, scope, key)).rowcount

    def read_settings(self, ns):
        return [dict(r) for r in self.conn.execute("SELECT scope, key, allowed, set_at FROM read_settings WHERE ns=? ORDER BY scope, key", (ns,))]

    def set_current_turn(self, ns, turn_id, session_id=None, at=None):
        with self.tx() as c:
            c.execute("INSERT INTO current_turn VALUES (?,?,?,?) ON CONFLICT(ns) DO UPDATE SET turn_id=excluded.turn_id, session_id=excluded.session_id, at=excluded.at", (ns, turn_id, session_id, at))

    def clear_current_turn(self, ns, turn_id=None):
        with self.tx() as c:
            if turn_id is None:
                c.execute("DELETE FROM current_turn WHERE ns=?", (ns,))
            else:
                c.execute("DELETE FROM current_turn WHERE ns=? AND turn_id=?", (ns, turn_id))

    def current_turn(self, ns):
        r = self.conn.execute("SELECT turn_id, session_id, at FROM current_turn WHERE ns=?", (ns,)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------------- 游标
    def put_cursor(self, cursor_id, ns, kind, created_at, data):
        with self.tx() as c:
            c.execute("INSERT INTO cursors VALUES (?,?,?,?,?)", (cursor_id, ns, kind, created_at, _dumps(data)))

    def get_cursor(self, cursor_id):
        r = self.conn.execute("SELECT * FROM cursors WHERE cursor_id=?", (cursor_id,)).fetchone()
        if r is None:
            return None
        d = json.loads(r["data"])
        if isinstance(d, dict) and d.get("void") == "history_deleted":
            raise HistoryDeleted("历史已删除，请重新查询")
        return dict(r, data=d)

    def update_cursor(self, cursor_id, data):
        with self.tx() as c:
            c.execute("UPDATE cursors SET data=? WHERE cursor_id=?", (_dumps(data), cursor_id))

    # ---------------------------------------------------------------- 向量缓存
    def put_identity(self, fp, identity):
        with self.tx() as c:
            r = c.execute("SELECT data FROM vector_identities WHERE fp=?", (fp,)).fetchone()
            if r is None:
                c.execute("INSERT INTO vector_identities VALUES (?,?)", (fp, _dumps(identity)))

    def identity(self, fp):
        r = self.conn.execute("SELECT data FROM vector_identities WHERE fp=?", (fp,)).fetchone()
        return json.loads(r["data"]) if r else None

    def put_vectors(self, fp, items, keep_first=False):
        """items：{text_sha: [float]}。同键同值是空操作；同键异值报 VectorConflict 一类错误（StoreError）；keep_first＝先到的为准（同一句话再算一次浮点可能差几个末位）。"""
        with self.tx() as c:
            self.put_vectors_in(c, fp, items, keep_first)

    @staticmethod
    def put_vectors_in(c, fp, items, keep_first=False):
        for tsha, vec in items.items():
            blob = array.array("d", vec).tobytes()
            r = c.execute("SELECT vec FROM vectors WHERE fp=? AND text_sha=?", (fp, tsha)).fetchone()
            if r is not None:
                if bytes(r["vec"]) != blob and not keep_first:
                    raise StoreError("向量缓存里同一个键（%s…）已有不同的向量值，拒绝覆盖" % tsha[:10])
                continue
            c.execute("INSERT INTO vectors VALUES (?,?,?,?)", (fp, tsha, len(vec), blob))

    def get_vectors(self, fp, text_shas):
        out = {}
        shas = list(text_shas)
        for i in range(0, len(shas), 500):
            chunk = shas[i:i + 500]
            q = "SELECT text_sha, vec FROM vectors WHERE fp=? AND text_sha IN (%s)" % ",".join("?" * len(chunk))
            for r in self.conn.execute(q, [fp] + chunk):
                a = array.array("d")
                a.frombytes(bytes(r["vec"]))
                out[r["text_sha"]] = a.tolist()
        return out

    def meta_get(self, k, default=None):
        r = self.conn.execute("SELECT v FROM store_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def meta_set(self, k, v):
        with self.tx() as c:
            c.execute("INSERT INTO store_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def vector_count(self, fp):
        return self.conn.execute("SELECT COUNT(*) FROM vectors WHERE fp=?", (fp,)).fetchone()[0]


class GuardedStore:
    """整理一步时用的存储代理：每个写方法在事务里核「这个工作区的删除版本还是开始时那个」，变了就抛 DeletionInterrupt（不是失败）。读方法原样转给 Store。"""
    _GUARDED = ("bind_step", "save_judgment", "update_judgment", "archive_failed_attempts", "update_step", "end_step", "commit", "save_message")

    def __init__(self, store, ns, del_ver):
        self._store, self._ns, self.guard_ver = store, ns, del_ver

    def __getattr__(self, name):
        fn = getattr(self._store, name)
        if name in self._GUARDED:
            def call(*a, **kw):
                kw["guard"] = (self._ns, self.guard_ver)
                return fn(*a, **kw)
            return call
        return fn


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
