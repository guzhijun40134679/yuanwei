"""身份源：读取工具从哪里知道「这次调用属于哪个会话」。**只能由可信的启动端或宿主同进程代码构造和更新，模型填的参数永远不是身份源。**

* InProcessIdentity：同进程的宿主插件（Hermes 记忆插件）在自己的事件里设置会话；读取工具和插件在同一个进程里，身份来自插件实例里的状态。
* ConnectionIdentity：独立的 MCP 进程。启动端（接头）先登记一个连接句柄、放进这个进程的环境变量；进程启动时认领，句柄只能给一个进程用，会话结束后连接关闭。
  只有「环境变量确实由可信的启动端为这条连接设置」才成立；写死在静态配置里的环境变量不是连接身份（静态登记的 Claude Code、Codex 就是这样，所以读取工具不开放）。
* NoIdentity：没有可信的身份源（静态 stdio 登记的宿主）。
**身份在调用发起、排队之前取好并冻结**（ToolSurface.call／MCP 服务的调用入口都先调 current()，再进排队）：current() 只读内存里的冻结值，不碰数据库，所以可以在任何线程里取。
身份里的轮次标记（turn_id）只来自宿主真的给的东西：同进程插件在自己的轮次事件里设的轮次（InProcessIdentity.set 的第四个参数）、逐轮的连接能力（register_connection 的 turn_id）。
没有轮次标记（会话级句柄、插件没给）：读取被拒（turn_unproven），不按「最新一轮」算。「一个会话一个 MCP 进程」不等于「一轮一个」，Pi 的这一条要用逐轮能力，拿不出来就是待核。
"""
from .access import Access, ConnectionRefused, ToolIdentity


class NoIdentity:
    def current(self):
        return None


class InProcessIdentity:
    def __init__(self):
        self._ident = None

    def set(self, host, session_id, branch="", turn_id=None):
        """插件在自己的轮次事件里调：会话和这一轮的轮次标记一起设；调用在发起时取走这一份冻结的身份。"""
        self._ident = ToolIdentity(host, session_id, branch, turn_id)

    def clear(self):
        self._ident = None

    def current(self):
        return self._ident


class ConnectionIdentity:
    def __init__(self, access, handle, claimant):
        self.access, self.handle = access, handle
        self.error = None
        self._ident = None
        try:
            self._ident = access.claim_connection(handle, claimant)
        except ConnectionRefused as e:
            self.error = e.code

    def current(self):
        """认领成功的冻结身份（带连接能力的哈希）。连接关没关不在这里查（这里不碰数据库）：开始读取和返回前，Access 按身份里的连接能力核。"""
        return self._ident


class UnboundCall:
    """没有身份的调用：带着原因（call_unregistered、call_conflict、call_id_invalid、call_mismatch…），读取一律被拒。"""

    def __init__(self, reason):
        self.reason = reason


class CallBoundIdentity:
    """Claude Code、Codex：身份来自**这一次调用**，不来自连接。调用前钩子（可信）按宿主给的调用编号登记了（会话、分支、轮次）；MCP 读取时，宿主放进 _meta 的编号才是钥匙，
    工具参数不是 _meta、不能提供这个编号。没登记、有冲突、编号不合格、和宿主给的轮次信息对不上，都没有身份。不查「最新一轮」。
    * Claude Code：_meta["claudecode/toolUseId"] ＝ 钩子的 tool_use_id（真机核过 9＋3＋… 次，逐次相同）。
    * Codex：_meta["callId"] ＝ 钩子的 tool_use_id；同时 _meta["x-codex-turn-metadata"] 里的 turn_id、session_id 必须和登记的一致（钩子字段和 _meta 字段对不上就拒绝）。"""

    def __init__(self, access, host):
        if host not in ("claude-code", "codex"):
            raise ValueError("CallBoundIdentity 只给 claude-code、codex")
        self.access, self.host = access, host
        self._conn = None

    def current(self):
        return None                                                                   # 没有「环境里的」身份

    def _ro(self):
        """自己的只读连接：查登记表不和工具调用里的读取共用一条连接。"""
        if self._conn is None:
            import sqlite3
            self._conn = sqlite3.connect("file:%s?mode=ro" % self.access.mem.db_path, uri=True, timeout=3.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _lookup(self, call_id):
        try:
            return self.access.identity_for_call(self.host, call_id, conn=self._ro())
        except Exception:                                                             # noqa: BLE001  读不了登记表：没有身份，不猜
            return None, "call_registry_unreadable"

    def for_call(self, meta):
        meta = meta if isinstance(meta, dict) else {}
        if self.host == "claude-code":
            return self._lookup(meta.get("claudecode/toolUseId"))
        ident, why = self._lookup(meta.get("callId"))
        if ident is None:
            return None, why
        tm = meta.get("x-codex-turn-metadata")
        if not isinstance(tm, dict) or not isinstance(tm.get("turn_id"), str) or not isinstance(tm.get("session_id"), str):
            return None, "call_meta_incomplete"
        if tm["turn_id"] != ident.turn_id or tm["session_id"] != ident.session_id:
            return None, "call_mismatch"
        for k in ("sessionId", "threadId"):                                          # _meta 里另外两处带的会话号，有就必须一致
            if k in meta and meta[k] != ident.session_id:
                return None, "call_mismatch"
        return ident, None
