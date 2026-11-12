"""Hermes 记忆插件的核心（不 import Hermes）。装到 Hermes 里的插件文件（resources/hermes_plugin_init.py）只是把 Hermes 的 MemoryProvider 接口转给这个类。
接口照本机 Hermes v0.22.0 源码（agent/memory_provider.py、memory_manager.py、turn_context.py、run_agent.py）；0.20.0 的两份检出核过兼容（见兼容表第 9 节）。

* **读取身份是同进程的：** 宿主分发 handle_tool_call 时不带会话和轮次，身份来自这里的状态（InProcessIdentity）。on_turn_start 里把会话和这一轮的轮次标记一起设好；
  工具调用在发起时取走冻结的身份（补充施工单一）。**控制登记（固定口令）在 on_turn_start 做，不放在 prefetch 里**：Hermes 对简单提问（空、斜杠开头、yes／ok／hi 之类）不调预取。
* **一轮从哪来**（只认 Hermes 源码里真有的字段，兼容表第 9 节）：initialize 的 agent_context（cron、subagent、flush）和 platform（cron）整个不启用；on_turn_start 的 author_is_bot 为真的轮次不收。
  **on_turn_start 看不到** 后台完成、委派完成唤醒这类自注入的用户轮（宿主用 display_kind 和 _*_synthetic 标记区分它们，只在 messages 里）；所以**写入前**在 sync_turn 的 messages 里核
  当前这一轮的用户行：找不到、有这些标记、或后面还有带 display_kind 的用户行，就不记。读取许可不依赖这个（自注入的轮次没有用户的口令，读一次不泄露什么）。
* **工作区**（第 2b 步）：项目（用稳定编号做键）或「生活」，见 hermes_workspace.py；写入永远只写当前那个工作区；读取范围（甲全隔开／乙生活读全部、项目读自己＝默认／丙全打通）只来自用户的配置，
  工具参数里模型填的范围、工作区一律无效；读多个工作区时，每一节材料标明来自哪个工作区，开头一行写读了哪些。认不出工作区（说不清）：这一轮不读不写，不落进生活。
* **只记主人本人的话**：本机会话（platform 是 cli、local）就是主人（本机终端／已认证的本地路由，没有远程作者）；网关平台要用户在设置里配 owners，轮次的作者（author_id，缺了用初始化的 user_id、user_id_alt）
  对得上才算主人；Hermes 的配对／白名单只说「可以用机器人」，不当主人；机器人、别的真人、陌生人、认不出的一律不写入、不建立读取许可。
* **写入只收传进来的两段话**，不存 messages；配对用内容指纹（轮次号回退后会重复，不能当编号）。sync_turn 在 Hermes 自己的后台单线程里跑；会话结束、切换这类可能做重活的事交给自己的后台线程，不占宿主线程。
  原话落盘走第一段的 Memory.record（数据库忙时写本机待补文件，不丢），这里不另造一套。
"""
import json
import os
import queue
import secrets
import threading
import time

import re
import sqlite3

from . import hermes_compat as HC
from . import hermes_profile as HP
from . import hermes_workspace as HW
from . import hosts as H
from . import window as WN
from .access import Access
from .config import Config
from .idsource import InProcessIdentity
from .mcp.server import SCHEMAS
from .memory import Memory
from .modes import ModeConfig, ModeRunner
from .reader import purge_all
from .rules import CONTROL_TOOL_DESCRIPTIONS, RULES, TOOL_DESCRIPTIONS
from .tools import ALL_TOOLS, ToolSurface

HOST = "hermes"
CONTROL_TOOLS = ("window_off", "window_on", "window_state", "window_delete")      # 控制工具（只管开关，不读历史）
READ_TOOLS = ("recall", "get_messages", "attachments")
SYNTHETIC_USER_FLAGS = HC.OUR_SYNTHETIC_FLAGS           # 宿主的合成标记（agent/conversation_compression.py）；另外名字以 _synthetic 结尾的一律按模式认，宿主再加新的也不用改这里


def _flatten(content):
    """messages 里一行的正文：字符串，或多模态的文字部分（换行连起来，和宿主 sync 前的拍平一致）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _machinery(row):
    if row.get("display_kind"):
        return True
    return any(row.get(f) for f in SYNTHETIC_USER_FLAGS) or any(isinstance(k, str) and k.startswith("_") and k.endswith("_synthetic") and v for k, v in row.items())


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _Worker:
    """自己的后台线程：一个队列，先进先出；inline 时就地跑（测试用）。"""

    def __init__(self, kind):
        self.kind, self.q, self.t = kind, queue.Queue(), None
        self.errors = []

    def submit(self, fn):
        if self.kind == "inline":
            fn()
            return
        if self.t is None:
            self.t = threading.Thread(target=self._loop, daemon=True, name="yuanwei-hermes-worker")
            self.t.start()
        self.q.put(fn)

    def _loop(self):
        while True:
            fn = self.q.get()
            try:
                if fn is None:
                    return
                fn()
            except Exception as e:                                                       # noqa: BLE001
                self.errors.append(type(e).__name__)
            finally:
                self.q.task_done()

    def drain(self, timeout):
        if self.kind == "inline" or self.t is None:
            return True
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.01)
        return not self.q.unfinished_tasks

    def stop(self, timeout=5.0):
        ok = self.drain(timeout)
        if self.t is not None:
            self.q.put(None)
            self.t.join(1.0)
        return ok


def static_tool_schemas():
    """原委给 Hermes 的工具清单（名字、说明、参数）：固定的，不看设置和状态。插件在 initialize 之前也要交这一份——Hermes 在 add_provider 里（initialize 之前）调 get_tool_schemas，
    把工具名写进路由表，以后不再刷新；之前交空清单，模型调 recall 时 Hermes 回 Unknown tool。适配器初始化以后在「活着、读取工具功能开着」时交的就是这一份，所以前后名字集合一样。"""
    return [{"name": n, "description": TOOL_DESCRIPTIONS[n], "parameters": SCHEMAS[n]} for n in ALL_TOOLS] + \
           [{"name": n, "description": CONTROL_TOOL_DESCRIPTIONS[n], "parameters": {"type": "object", "properties": {}}} for n in CONTROL_TOOLS]


class HermesAdapter:
    def __init__(self, config, memory=None, executor="thread", host_base=None, memory_factory=None):
        self.cfg = dict(config or {})
        self._probe = HC.probe_features(host_base) if host_base is not None else {"base_ok": True, "features": {f: {"on": True, "reason": None} for f in HC.FEATURES}, "extra_abstract": []}
        self._home = None
        self._injected, self._factory = memory, memory_factory
        self._worker = _Worker(executor)
        self._lock = threading.Lock()
        self.active, self.inactive_reason = False, "not_initialized"
        self.session = ""
        self.platform = "cli"
        self._init_kw = {}
        self.ws = {"key": None, "kind": None, "label": None, "reason": "not_initialized"}
        self._cwd_recheck = False                                                           # 经典终端恢复会话：钩子在 Hermes 切目录之前被调，到下一轮开始再按进程目录重判一次
        self._mems, self._surfaces = {}, {}
        self._pending = {}                    # 会话 → [{token, number, fp, admitted, reason}]（还没配上 sync_turn 的轮次，先进先出）
        self._cur = {}                        # 会话 → 当前轮的轮次标记
        self._prefetched = set()
        self.evidence, self.owner_basis = None, None
        self.recorded_turns, self.skipped = 0, {}
        self.mem = self.acc = self.ids = self.runner = None
        self.win = None                       # WindowService（接头用的库里的窗口状态，不按工作区分区）
        self._win_ref = None                  # 当前会话对应的窗口（最近一次映射的结果；状态里给人看）
        self._cur_ref = {}                    # 会话 → 当前这条输入的 InputRef
        self._notices = []                    # 状态提交成功以后要提示用户的话（关上、打开、恢复一个关着的窗口）
        self._display = []                    # 要直接显示给用户的固定文字（删除的提示和回执）：transform_llm_output 钩子有就接在助手回复后面
        self.profile = {"ok": None, "basis": "not_checked", "recorded_home": None, "actual_home": None}     # 这份设置属于哪个家目录（hermes_profile.home_check 的结果）
        self.hook_registered = False          # 插件文件注册了钩子（不等于宿主真的会调）
        self._hook_calls = 0                  # 宿主真的调过钩子几次（>0 才说明这台能用直接显示）
        self.scope = self.cfg.get("read_scope") or HW.DEFAULT_SCOPE                        # 没配：用默认（乙）
        if self.scope not in HW.SCOPES:
            self.scope = "isolated"                                                         # 配了但认不得：按最严，不退回默认

    # ---------------------------------------------------------------- 工作区和内存
    def _open(self, key):
        if key in self._mems:
            return self._mems[key]
        if self._injected is not None and not self._mems and self._factory is None:
            m = self._injected
        elif self._factory is not None:
            m = self._factory(key)
        else:
            cp = self.cfg.get("config_path")
            m = Memory.open(self.cfg["db"], key, config=Config.load(cp) if cp else Config(), spawn=self.cfg.get("spawn", "subprocess"), config_path=cp, src_path=self.cfg.get("src_path"))
            try:
                m.kick_vectors()                                                                 # 开机补欠账：便宜的判断，没配向量或没有欠账什么都不做；拉不起来不影响启动
                m.warm_engine()                                                                  # 暖机：自带引擎没开就拉起（不等）；只在插件启动时做一次
            except Exception:                                                                    # noqa: BLE001
                pass
        self._mems[key] = m
        return m

    def _ws_exists(self, key):
        """别的工作区只读已经存在的（不为了「读」去建一个空的）。"""
        try:
            c = sqlite3.connect("file:%s?mode=ro" % self.mem.db_path, uri=True, timeout=3.0)
            try:
                return c.execute("SELECT 1 FROM workspaces WHERE ns=?", (key,)).fetchone() is not None
            finally:
                c.close()
        except Exception:                                                                    # noqa: BLE001
            return False

    def _set_workspace(self, ws):
        self.ws = ws
        if not ws["key"]:
            self.mem = self.acc = self.ids = self.runner = None
            self._surfaces = {}
            return
        if self._home:
            try:
                HW.remember(self._home, ws)
            except OSError:
                pass
        self.mem = self._open(ws["key"])
        self.acc = Access(self.mem)
        self.ids = InProcessIdentity()
        prof = H.profile(HOST)
        self._surfaces = {ws["key"]: ToolSurface(self.mem, self.acc, prof, self.ids)}
        self.runner = ModeRunner(self.mem, self.acc, prof, ModeConfig(mode=self.cfg.get("mode", "tools_only")))
        self.ids.set(HOST, self.session, "", None)
        dflt = self.cfg.get("window_default", "on")
        self.win = WN.WindowService(self.mem.store, clock=self.mem.clock, default_state=dflt if dflt in ("on", "off") else "off", settings=self._setting_block)   # 默认值认不得：按最严（关）
        from .deletion import DeletionService
        self.win.deleter = DeletionService(self.mem.db_path, store=self.mem.store, config_dir=self.mem.config_dir, clock=self.mem.clock)
        self.acc.windows = self.win

    def _setting_block(self, host, session_id):
        """被哪条设置挡住（设置优先，自然语言打不开设置）：workspace｜session｜None。"""
        r = self.acc.session_permission(host, session_id)
        return None if r["allowed"] else (r["denied_by"][0]["scope"] if r["denied_by"] else "workspace")

    def _surface_for(self, key):
        """别的工作区的读取面：权限（轮次、口令、来源）借当前工作区这一边的登记，数据读它自己的库；日志记在当前这一轮名下。"""
        if key in self._surfaces:
            return self._surfaces[key]
        if not self._ws_exists(key):
            return None
        m = self._open(key)
        borrowed = Access(m)
        borrowed.store, borrowed.ns, borrowed.mem = self.acc.store, self.acc.ns, self.acc.mem
        borrowed.windows = self.acc.windows                                                  # 一次读多个工作区：用同一份窗口状态
        sf = ToolSurface(m, borrowed, H.profile(HOST), self.ids)
        self._surfaces[key] = sf
        return sf

    # ---------------------------------------------------------------- 生命周期
    def initialize(self, session_id, **kw):
        ctx, plat = kw.get("agent_context"), kw.get("platform") or "cli"
        self.session, self.platform = session_id, plat
        self._home = kw.get("hermes_home")
        self._init_kw = {k: kw.get(k) for k in ("user_id", "user_id_alt", "chat_type", "cwd")}
        if ctx is None:
            self.inactive_reason = "agent_context_missing"                                  # 宿主没传：不知道是不是主对话，按说不清处理（这四版宿主都总会传，见 doctor 的 init_kwargs）
        elif ctx != "primary":
            self.inactive_reason = "agent_context_%s" % ctx
        elif plat == "cron":
            self.inactive_reason = "platform_cron"
        elif plat in HP.GROUP_ROOM_PLATFORMS:
            self.inactive_reason = HP.GROUP_ROOM                                            # 群聊：明确不记不查（配了 owners 也不放行），不靠「没配主人」碰巧挡住
        else:
            self.inactive_reason = None
        if self.inactive_reason is None:                                                     # 这份设置是不是这个家目录自己的：复制来的（机器人）不启用，绝不悄悄用别的档案的库
            self.profile = HP.home_check(self.cfg, kw.get("hermes_home") or self.cfg.get("_loaded_from"))
            if not self.profile["ok"]:
                self.inactive_reason = HP.UNCONFIRMED if self.profile.get("state") == HP.UNCONFIRMED else HP.COPIED
        self.active = self.inactive_reason is None
        if not self.active:
            self._dump_status()
            return
        self._set_workspace(HW.resolve(self._home or "", self._init_kw["cwd"], plat))
        if self.win is not None:                                                             # 绑定表里有这个号→同一窗口（重启、--resume）；没有→新窗口用默认值（见交回点甲问题 4）
            try:
                self._win_ref = self.win.bind_new(HOST, session_id, "", via="initialize")
            except Exception:                                                                # noqa: BLE001  绑定写不了：没有绑定＝窗口身份待确认，读取被拒，不默认开
                self._win_ref = None
        self._dump_status()

    def shutdown(self):
        self._worker.stop()
        if self._injected is None and self._factory is None:
            for m in self._mems.values():
                m.close()

    def drain(self, timeout=5.0):
        return self._worker.drain(timeout)

    # ---------------------------------------------------------------- 主人
    def _owner_check(self, author_id):
        """返回 (是不是主人, 依据或原因)。本机会话就是主人；网关平台要在设置里配 owners。Hermes 的配对／白名单不当主人。"""
        if self.platform in HP.GROUP_ROOM_PLATFORMS:
            return False, HP.GROUP_ROOM                                                         # 群聊：配了 owners 也不放行
        login = self.platform in HW.LOGIN_CHECKED_PLATFORMS and bool(self._init_kw.get("user_id"))     # tui、desktop 带着登录身份（WS 登录）
        if self.platform in HW.LOCAL_PLATFORMS and not login:
            return True, "local_session"
        if login:
            same = self._login_is_host_owner()
            if same is True:
                return True, "host_owner_login"                                                # 登录身份就是宿主主人的系统身份（Hermes 里每个签名登录都是主人）
            if same is None:
                return False, "login_identity_unverifiable"                                    # 这一版 Hermes 没有 host_owner_subject 或调不了：不当主人
        ids = [str(x) for x in ((self.cfg.get("owners") or {}).get(self.platform) or [])]
        if not ids:
            return False, "login_identity_needs_owner" if login else "owner_unknown"
        who = str(author_id) if author_id else (str(self._init_kw.get("user_id")) if self._init_kw.get("user_id") else None)
        if who is None:
            return False, "owner_unknown"
        if who in ids:
            return True, "configured_owner"
        alt = self._init_kw.get("user_id_alt")
        if not author_id and alt and str(alt) in ids:
            return True, "configured_owner_alt_id"
        if author_id and self._init_kw.get("user_id") == author_id and alt and str(alt) in ids:
            return True, "configured_owner_alt_id"
        return False, "not_owner"

    def _login_is_host_owner(self):
        """登录带来的 user_id 是不是宿主主人的系统身份：在 Hermes 进程里调 gateway.owner_identity.host_owner_subject() 比较。True／False；导入或调用失败（这一版没有）返回 None。
        只比较、只在内存里用，不写库、不打印、不进状态（状态里只有依据类别）。"""
        try:
            import importlib
            subject = importlib.import_module("gateway.owner_identity").host_owner_subject()
        except Exception:                                                                    # noqa: BLE001
            return None
        if not isinstance(subject, str) or not subject:
            return None
        return str(self._init_kw.get("user_id")) == subject

    # ---------------------------------------------------------------- 一轮开始：登记来源和控制（不放在 prefetch 里）
    def _adopt_workspace(self, ws):
        """换成重判出来的工作区。键没变就只更新依据，不重建（保住这一轮的登记状态）；变了才重建。"""
        if ws["key"] is not None and ws["key"] == self.ws["key"]:
            self.ws = ws
        else:
            self._set_workspace(ws)

    def _recheck_workspace_after_resume(self):
        """恢复会话（/resume）的钩子在 Hermes 把进程目录切到那个会话的目录**之前**被调（hermes_cli/cli_commands_mixin.py：_sync_agent_to_session 在前，_restore_session_cwd 在后），
        所以那一刻取到的进程目录是旧的；到下一轮开始（切目录早已做完）再按同一条规则重判。"""
        with self._lock:
            if not self._cwd_recheck:
                return
            self._cwd_recheck = False
            self._adopt_workspace(HW.resolve(self._home or "", None, self.platform))
            self._dump_status()

    def on_turn_start(self, turn_number, message, **kw):
        if self.active and self._cwd_recheck:
            self._recheck_workspace_after_resume()
        if not self.active or self.ws["key"] is None:
            return
        text = message if isinstance(message, str) else ""
        has_author = "author_is_bot" in kw
        is_bot = bool(kw.get("author_is_bot"))
        evidence = "author_fields" if has_author else "legacy_no_author_field"             # 老宿主（0.20.0）整个没有这几个字段，不是「明确写着不是机器人」
        fp = Access.text_fp(text)
        with self._lock:
            sid = self.session
            self.evidence = evidence
            token = self.acc.turn_token(HOST, sid, "%s:%s" % (turn_number, fp[:12]))
            lst = self._pending.setdefault(sid, [])
            if any(e["token"] == token for e in lst):                                         # 同一个事件重送：不再造新的一轮
                self.ids.set(HOST, sid, "", token)
                return
            cap = None
            try:
                cap = self.mem.store.take_capture()                                          # 事件进来的号：这一轮的用户话和助手回复都带它，删除时用来划边界
            except Exception:                                                                # noqa: BLE001  取不到（数据库忙）：之后按收到的时间比
                cap = None
            owner, why = (False, "bot_author") if is_bot else self._owner_check(kw.get("author_id"))
            if owner:
                self.owner_basis = why
                self.acc.begin_turn(HOST, sid, token, text=text, origin="user")
            else:
                self.acc.begin_turn(HOST, sid, token, text=text, origin="unknown")             # 只记一条「来源不明」日志；不开这一轮、不清已有的拒绝、不建立读取许可
            lst.append({"token": token, "number": turn_number, "fp": fp, "admitted": owner, "reason": None if owner else why, "capture": cap})
            self._cur[sid] = token
            self.ids.set(HOST, sid, "", token)
            if owner:
                self._window_input(sid, token, turn_number, fp, text, cap, lst[-1])

    def _window_input(self, sid, token, turn_number, fp, text, capture=None, entry=None):
        """这一条可信输入的窗口控制（口令、规则识别），在便条、目录、预取看窗口状态之前做完。关闭写失败时窗口进入「没确认已关」，读取被拦住，不说已关。"""
        ref = WN.InputRef(HOST, sid, token, "%s:%s" % (turn_number, fp[:12]), fp, "", self.ws["key"])
        self._cur_ref[sid] = ref
        res = self.win.process_input(ref, text, origin_verified=True, capture=capture)       # 确认开启、确认删除也只在这里认：接头只对用户本人的输入调它，工具参数、工具返回、子代理确认不了
        for t in (res.notice, res.receipt):
            if t:
                self._notices.append(t)
        if res.deletion is not None:
            if res.receipt:
                self._display.append("〔原委〕" + res.receipt)
            if entry is not None:                                                            # 确认删除那一轮不记（持久标记在库里；这里让 sync_turn 早早跳过）
                entry["no_record"] = True
            self._after_delete(res.deletion)

    def _after_delete(self, d):
        """第 1 阶段已经提交：清所有 Memory 的缓存；清理（阶段 2–5）交给自己的后台线程；完成时再给一条回执。"""
        for m in self._mems.values():
            try:
                m.purge_caches()
            except Exception:                                                                # noqa: BLE001
                pass
        purge_all(self.mem.db_path if self.mem is not None else None)
        if d is None or d.del_id is None or d.status == "complete":
            return
        db, cfgdir, clock, did = self.mem.db_path, self.mem.config_dir, self.mem.clock, d.del_id

        def work():
            from .deletion import DeletionService
            svc = DeletionService(db, config_dir=cfgdir, clock=clock)
            try:
                r = svc.finish(did)
            finally:
                svc.close()
            for m in list(self._mems.values()):
                try:
                    m.purge_caches()
                except Exception:                                                            # noqa: BLE001
                    pass
            self._notices.append(r.text)
            self._display.append("〔原委〕" + r.text)
        self._worker.submit(work)

    def display_hook(self, **kw):
        """transform_llm_output 的回调：有要直接显示的固定文字，就接在这一轮助手回复后面（模型改不了）。没有就返回 None，不动回复。"""
        self._hook_calls += 1
        with self._lock:
            texts, self._display = self._display, []
        if not texts:
            return None
        base = kw.get("response_text")
        return ((base if isinstance(base, str) else "") + "\n\n" + "\n".join(texts)).strip()

    def _take_notices(self):
        with self._lock:
            n, self._notices = self._notices, []
        return n

    def _on(self, feature):
        return self._probe["features"][feature]["on"]

    def prefetch(self, query, *, session_id="", **kw):
        if not self.active or self.ws["key"] is None or not self._on("prefetch"):
            return ""
        with self._lock:
            sid = session_id or self.session
            token = self._cur.get(sid)
            ent = next((e for e in self._pending.get(sid, []) if e["token"] == token), None)
            if token is None or ent is None or not ent["admitted"] or token in self._prefetched:
                return ""
            self._prefetched.add(token)
        extras, primary = [], None
        if self.cfg.get("mode", "tools_only") in ("note", "directory"):
            keys = HW.readable(self.scope, self.ws["key"], HW.known(self._home or ""))           # 便条、目录和读取工具照同一条读取范围读
            if len(keys) > 1:
                primary = (self.ws["key"], self._label(self.ws["key"]), keys.index(self.ws["key"]))
                extras = [(k, self._label(k), self._open(k), i) for i, k in enumerate(keys) if k != self.ws["key"] and self._ws_exists(k)]
        try:
            out = self.runner.pre_answer(sid, token, query if isinstance(query, str) else "", extras=extras or None, primary=primary if extras else None)
        except Exception:                                                                    # noqa: BLE001  预取出任何错都不让这一轮失败
            self._skip("prefetch_error")
            return ""
        text = out.get("inject") or ""
        notes = self._take_notices()
        return "\n".join(([text] if text else []) + notes) if notes else text

    def queue_prefetch(self, query, *, session_id=""):
        return None                                                                          # 不做后台预取：自动取材的 3 秒时限只在回答前这一次

    def recall_status(self):
        return None

    def system_prompt_block(self):
        if not self.active or self.cfg.get("resident_rule") is False:
            return ""
        return RULES[self.cfg.get("mode", "tools_only")]

    # ---------------------------------------------------------------- 工具
    def get_tool_schemas(self):
        if not self.active or not self._on("tools"):
            return []
        return static_tool_schemas()

    def _inactive_message(self):
        if self.inactive_reason == HP.COPIED:
            return HP.copied_message(self._home or self.profile.get("actual_home"))
        if self.inactive_reason == HP.UNCONFIRMED:
            return HP.unconfirmed_message(self._home or self.profile.get("actual_home"), self.cfg.get("db"))
        if self.inactive_reason == HP.GROUP_ROOM:
            return HP.GROUP_ROOM_MESSAGE
        return None

    def _j(self, status, text, reason=None):
        out = {"status": status, "text": text}
        if reason:
            out["reason"] = reason
        return json.dumps(out, ensure_ascii=False)

    def handle_tool_call(self, tool_name, args, **kw):
        if not self.active or not self._on("tools"):
            if not self.active and self.inactive_reason in (HP.COPIED, HP.UNCONFIRMED, HP.GROUP_ROOM):
                return self._j("refused", self._inactive_message(), self.inactive_reason)             # 工具名在 initialize 之前就已登记，调用仍会进来：明确拒绝并说原因
            return self._j("error", "原委的读取工具在这个上下文里没有启用。", "inactive" if not self.active else "feature_off_tools")
        h = kw.get("hermes_home")
        if h and self._home and os.path.realpath(str(h)) != os.path.realpath(str(self._home)):         # 宿主（v0.23 起）带上调用方的 profile：和这个实例绑定的不是同一个就拒绝，不重新绑定
            return self._j("refused", "这次调用属于另一个 Hermes profile，不是这个实例绑定的；没有读取。", "profile_mismatch")
        if self.ws["key"] is None:
            return self._j("refused", "没有认出这个对话属于哪个项目（%s）：这一轮不读也不写，也不会拿「生活」顶上。" % self.ws["reason"], "workspace_unresolved")
        args = args if isinstance(args, dict) else {}
        if tool_name in CONTROL_TOOLS:
            return self._control_tool(tool_name)
        keys = HW.readable(self.scope, self.ws["key"], HW.known(self._home or ""))
        if tool_name not in READ_TOOLS or self.win is None:
            return self._dispatch(tool_name, args, keys)
        return self._read_with_permit(tool_name, args, keys)

    def _dispatch(self, tool_name, args, keys):
        if len(keys) == 1 or tool_name in ("status",) or tool_name not in READ_TOOLS:
            res = self._surfaces[self.ws["key"]].call(tool_name, args)
            return self._j(res.status, res.text, res.reason)
        return self._multi(tool_name, args, keys)

    # ---------------------------------------------------------------- 读取许可（同进程也走同一套：签发 → 认领 → 读 → 返回前复核）
    def _read_with_permit(self, tool_name, args, keys):
        ident = self.ids.current()                                                           # 调用发起时冻结的身份；之后排队也不变
        pf = WN.params_fingerprint(args)
        scope, install, ns = sorted(keys), str(self._home or ""), self.ws["key"]
        p = self.win.issue_read_permit(ident, tool_name=tool_name, params_fp=pf, scope_set=scope, install_id=install, ns=ns)
        if p.refused:                                                                        # 窗口关着、暂停、设置挡住：不签发；照常走读取面，由它按原因给回执并登记「拒绝」
            return self._dispatch(tool_name, args, keys)
        c = self.win.claim_permit(p.secret, kind="read", host=ident.host, install_id=install, session_id=ident.session_id, branch=ident.branch or "", ns=ns, turn_id=ident.turn_id,
                                  tool_name=tool_name, params_fp=pf, scope_set=scope)
        if not c.ok:                                                                         # 认领前窗口变了：不读
            return self._j("refused", "%s。%s" % (self._window_refusal_text("window_closed" if c.reason == "window_closed" else "control_changed", ident)[0], "这个窗口的开关刚刚变了，没有读取。"), "window_changed")
        out = self._dispatch(tool_name, args, keys)
        chk = self.win.verify_before_return(p.secret)
        if chk.deliver:
            return out
        self.acc.block_after_read(ident, chk.reason)                                         # 已查询、结果挡住：照实记，不说成从没读过
        text, reason = self._window_refusal_text("window_closed" if chk.reason == "window_closed" else "control_changed", ident)
        return self._j("refused", text, reason)

    def _window_refusal_text(self, reason, ident):
        cur = self.acc.current_turn(ident.host, ident.session_id, ident.branch or "")
        r = self.acc._refusal(reason, cur, ident)
        return r["text"], r["reason"]

    # ---------------------------------------------------------------- 控制工具：只管开关，不读历史，不走 ToolSurface
    CONTROL_MESSAGES = {"on": "这个窗口的记忆读取是开着的。", "off": "这个窗口的记忆读取是关着的；要重新使用，请申请开启后让用户回复「确认开启」。"}

    def _control_tool(self, name):
        ident = self.ids.current()
        op = {"window_off": "close", "window_on": "open", "window_delete": "delete"}.get(name, "view")
        if self.win is None or ident is None:
            return self._j("refused", "没有可信的窗口身份，没有操作。", "identity_unbound")
        ns, install = self.ws["key"], str(self._home or "")
        pf = WN.params_fingerprint({})                                                      # 控制工具不收参数：模型填的任何参数都忽略，窗口只来自可信身份
        p = self.win.issue_control_permit(ident, op=op, params_fp=pf, install_id=install, ns=ns)
        if p.refused:
            return self._control_json(ident, {"state": "off", "blocked_by_setting": False, "reason": p.refused, "control_version": 0, "message": WN.PENDING_HINT})
        c = self.win.claim_permit(p.secret, kind="control", host=ident.host, install_id=install, session_id=ident.session_id, branch=ident.branch or "", ns=ns, turn_id=ident.turn_id, tool_name=op, params_fp=pf)
        if not c.ok:
            return self._j("refused", "控制许可没有通过核对，没有操作。", "control_permit_%s" % c.reason)
        if op == "close":
            ref = self._cur_ref.get(ident.session_id)
            seq = self.win.input_seq_of(ref) if ref is not None else None
            try:
                r = self.win.close(c.window_id, source="tool", op_id="op-" + secrets.token_hex(8), input_win_seq=seq)
            except Exception:                                                                # noqa: BLE001  写失败：不能说已关，窗口进入「没确认已关」，读取被拦住
                self.win._suspect.add((ident.host, ident.session_id, ident.branch or ""))
                return self._j("error", WN.CLOSE_FAILED_RECEIPT, "window_close_unconfirmed")
            self.win._suspect.discard((ident.host, ident.session_id, ident.branch or ""))
            if r.outcome == "applied":
                self._notices.append(WN.NOTICE_CLOSED)
        if op == "delete":
            d = self.win.request_delete(ident, ns=ns, install_id=install)                    # 只登记待确认，不删；确认只发生在用户的下一条输入里（整条「确认删除」）
            self.win.finish_control_permit(p.secret)
            if d.status == "waiting":
                self._display.append("〔原委〕" + d.prompt)                                   # 钩子有就直接显示；没有就靠 agent 转述（status 里写明）
            return self._control_json(ident, {"state": "waiting" if d.status == "waiting" else "off", "reason": d.reason, "pending_id": d.pending_id, "scope": d.scope,
                                              "message": d.prompt if d.status == "waiting" else WN.PENDING_HINT})
        if op == "open":
            o = self.win.request_open(ident, ns=ns)                                         # 只登记待确认，不打开；确认只发生在用户的下一条输入里
            self.win.finish_control_permit(p.secret)
            msg = {"waiting": o.prompt, "on": self.CONTROL_MESSAGES["on"], "blocked_by_setting": "这个窗口的记忆读取被设置挡住了，自然语言打不开设置。", "paused": WN.PENDING_HINT}[o.status]
            return self._control_json(ident, {"state": o.state, "blocked_by_setting": o.blocked_by_setting, "reason": o.reason, "control_version": o.control_version, "message": msg})
        self.win.finish_control_permit(p.secret)
        v = self.win.view(ident.host, ident.session_id, ident.branch or "")
        out = {"state": v.state, "blocked_by_setting": v.blocked_by_setting, "reason": v.reason, "control_version": v.control_version, "message": self.CONTROL_MESSAGES[v.state]}
        try:
            last = (self.win.deleter.status() or [None])[-1] if self.win.deleter is not None else None
            if last:
                out["last_deletion"] = {"state": last["state"], "code": last["code"], "messages": last["counts"].get("messages", 0)}
        except Exception:                                                                    # noqa: BLE001
            pass
        return self._control_json(ident, out)

    def _control_json(self, ident, d):
        return json.dumps(d, ensure_ascii=False)

    # ---------------------------------------------------------------- 读多个工作区：每一节标明来源，开头一行写读了哪些；范围只来自配置
    def _label(self, key):
        return (HW.known(self._home or "").get(key) or {}).get("label") or ("项目 %s" % key[5:] if key.startswith("proj:") else key)

    CURSOR = re.compile(r"(只给 cursor=)(\S+)")

    def _multi(self, tool, args, keys):
        order = list(keys)
        cursor = args.get("cursor")
        if tool in ("recall", "get_messages") and isinstance(cursor, str) and cursor:
            m = re.match(r"^W(\d+)~(.+)$", cursor)
            if not m or int(m.group(1)) >= len(order):
                return self._j("error", "续读游标不属于这一次读取范围。", "bad_cursor")
            order = [order[int(m.group(1))]]
            idxs = [int(m.group(1))]
            args = dict(args, cursor=m.group(2))
        else:
            idxs = list(range(len(order)))
        cur = self._surfaces[self.ws["key"]]
        first = cur.call(tool, dict(args)) if order[0] == self.ws["key"] else None
        if first is not None and first.status in ("refused",):
            return self._j(first.status, first.text, first.reason)                           # 当前这一轮不许读：别的工作区一样不读
        parts, read, errors = [], [], []
        for i, key in zip(idxs, order):
            sf = self._surfaces.get(key) or self._surface_for(key)
            if sf is None:
                continue
            res = first if (first is not None and key == self.ws["key"]) else sf.call(tool, dict(args))
            if res.status == "refused":
                return self._j(res.status, res.text, res.reason)
            if res.status != "ok":
                errors.append(res)
                continue
            text = self.CURSOR.sub(lambda mm, i=i: "%sW%d~%s" % (mm.group(1), i, mm.group(2)), res.text)
            parts.append("〔工作区：%s〕\n%s" % (self._label(key), text))
            read.append(self._label(key))
        if not parts:
            e = errors[0] if errors else None
            return self._j(e.status if e else "error", e.text if e else "没有读到。", e.reason if e else None)
        text = "〔读取范围：%s〕\n" % "、".join(read) + "\n".join(parts)
        ok, _d = H.fits(H.profile(HOST).paths["mcp_return"], text)
        if not ok:
            while len(parts) > 1 and not H.fits(H.profile(HOST).paths["mcp_return"], "〔读取范围：%s〕\n" % "、".join(read[:len(parts)]) + "\n".join(parts) + "\n（还有 %d 个工作区的结果没放进来，请单独再查）" % (len(read) - len(parts)))[0]:
                parts.pop()
            text = "〔读取范围：%s〕\n" % "、".join(read[:len(parts)]) + "\n".join(parts) + ("\n（还有 %d 个工作区的结果没放进来，请单独再查）" % (len(read) - len(parts)) if len(parts) < len(read) else "")
        return self._j("ok", text)

    # ---------------------------------------------------------------- 写入（只写当前那个工作区）
    def _skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1
        self._dump_status()

    def _dump_status(self):
        """把运行时的数留一份在 <hermes_home>/yuanwei/status.json（只有数和原因，没有原话），给 yuanwei status hermes 看。写不了不影响记忆。"""
        h = self._home
        if not h or not os.path.isdir(str(h)):
            return
        try:
            d = os.path.join(str(h), "yuanwei")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "status.json")
            tmp = "%s.tmp-%d-%d" % (p, os.getpid(), id(self) % 10000)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.status(), f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, p)
        except OSError:
            pass

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        if not self.active or not isinstance(user_content, str) or not user_content.strip():
            return
        if self.ws["key"] is None:
            self._skip("workspace_unresolved")                                               # 认不出工作区：不写，也不落进生活或别的项目
            return
        if not self._on("write"):                                                           # 宿主的 sync_turn 没有 messages：没法核这一轮是不是自注入的，写入这一项关掉
            self._skip("feature_off_write")
            return
        fp = Access.text_fp(user_content)
        with self._lock:
            ent, owner = None, None
            for sid in [session_id] + [s for s in self._pending if s != session_id]:
                for e in self._pending.get(sid, []):
                    if e["fp"] == fp:
                        ent, owner = e, sid
                        break
                if ent:
                    break
            if ent is None:
                self._skip("no_pending_turn")
                return
            self._pending[owner].remove(ent)
        if not ent["admitted"]:
            self._skip(ent["reason"] or "not_admitted")
            return
        if ent.get("no_record"):                                                             # 确认删除那一轮：用户那句和助手回复都不记（库里还有持久标记，重启、压缩换号、迟到回复都盖住）
            self._skip("delete_confirm_turn")
            return
        if isinstance(turn_author, dict) and turn_author.get("is_bot"):
            self._skip("bot_author")
            return
        if messages is None:
            self._skip("no_messages")
            return
        why = self._machinery_check(messages, user_content)
        if why:
            self._skip(why)
            return
        sid = session_id or owner
        uid = "%s-u" % ent["token"]
        t = _now()
        try:
            r = self.mem.record(HOST, sid, uid, "user", user_content, time=t, turn_id=ent["token"], capture=ent.get("capture"))            # 绑定这一轮：之后这一轮的读取把它排除，不当历史交回来
            if r.get("blocked"):                                                             # 被删除屏障挡住（确认删除那一轮、删除前开始的迟到写入）：不记
                self._skip("blocked_%s" % r["blocked"])
                return
            if isinstance(assistant_content, str) and assistant_content.strip():
                self.mem.record(HOST, sid, "%s-a-%s" % (ent["token"], Access.text_fp(assistant_content)[:8]), "assistant", assistant_content, time=t, turn_id=ent["token"], capture=ent.get("capture"))
        except Exception:                                                                    # noqa: BLE001
            self._skip("record_error")
            raise
        self.recorded_turns += 1
        self._dump_status()

    @staticmethod
    def _machinery_check(messages, user_text):
        want = user_text.replace("\r\n", "\n").strip()
        idx = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user" and _flatten(m.get("content")).replace("\r\n", "\n").strip() == want:
                idx = i
                break
        if idx is None:
            return "current_user_row_not_found"
        for m in messages[idx:]:
            if isinstance(m, dict) and m.get("role") == "user" and _machinery(m):
                return "machinery_row"
        return None

    # ---------------------------------------------------------------- 会话边界
    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kw):
        if not self.active or not new_session_id:
            return
        reason = kw.get("reason") or ""
        with self._lock:
            prev = self.session
            self.session = new_session_id
            moved = not rewound and not reset and reason not in ("compression", "branch")    # 恢复、切到别的会话（含没带 reason、没见过的 reason）
            if moved and self.platform in HW.LOCAL_PLATFORMS:
                self._rejudge_after_move(kw)                                                   # 本机平台：按 initialize 同一条规则重判，不再一律说不清
            if self.ws["key"] is None:
                return
            if rewound:
                return                                                                       # 撤销：同一窗口，状态不回滚，什么也不做
            self._window_switch(new_session_id, parent_session_id, prev, reset, reason)
            if reset or reason in ("compression", "branch"):
                pass                                                                         # 同一个对话继续（压缩、分叉）或同一个进程里开新对话：工作目录不变，工作区不变
            elif self.platform in HW.LOCAL_PLATFORMS:
                self._dump_status()                                                          # 本机平台：上面已经重判过
            else:
                self._set_workspace({"key": None, "kind": None, "label": None, "reason": "workspace_unknown_after_switch_%s" % (reason or "unknown")})   # 恢复、分叉别的会话：它的工作目录插件看不到，说不清
                self._dump_status()
                return
            tok = self._cur.get(parent_session_id) if parent_session_id else None
            if not reset and reason == "compression" and tok is not None and self._on("session_switch"):
                self.acc.inherit_turn(HOST, parent_session_id, new_session_id, tok)          # 压缩：同一轮继续，拒绝跟着走；分叉、新会话、恢复不继承
                self.ids.set(HOST, new_session_id, "", tok)
                self._cur[new_session_id] = tok
                self._pending[new_session_id] = self._pending.pop(parent_session_id, [])
            else:
                self.ids.set(HOST, new_session_id, "", None)
        if self.runner is not None and self.cfg.get("mode") == "directory":
            src = "compact" if reason == "compression" else ("clear" if reset else ("fork" if reason == "branch" else "resume"))
            self.runner.session_event(new_session_id, src)

    def _rejudge_after_move(self, kw):
        """恢复会话后的工作区（本机平台）：kwargs 里有 cwd 就用（cwd_given）；经典终端（cli）没有就取进程目录——但钩子在 Hermes 切目录之前被调，所以先不判，
        记一笔，到下一轮开始再判（见 _recheck_workspace_after_resume）；其他本机平台没目录就是生活（no_cwd_life）。网关平台不走这里（照原来的规则：说不清）。"""
        cwd = kw.get("cwd")
        if cwd is None and self.platform in HW.PROCESS_CWD_PLATFORMS:
            self._cwd_recheck = True
            return
        self._adopt_workspace(HW.resolve(self._home or "", cwd, self.platform))

    def _window_switch(self, new, parent, prev, reset, reason):
        """宿主编号换了，逻辑窗口怎么跟（对照表在交回点甲文档第 5 节）。压缩：同一窗口；分叉：复制此刻状态，之后各管各的；reset：新窗口用默认值；
        恢复（reason 是 resume，或缺、或没见过的值）：按绑定表查，查不到就暂停读取。parent_session_id 在 resume 时是「被离开的会话」，不当谱系用。"""
        if self.win is None:
            return
        p = parent or prev
        try:
            if reset:
                ref = self.win.bind_new(HOST, new, "", via="reset")
            elif reason == "compression":
                ref = self.win.bind_same(HOST, new, p, "", via="compression")
            elif reason == "branch":
                ref = self.win.bind_fork(HOST, new, p, "")
            else:
                ref = self.win.bind_restore(HOST, new, "")
                if not ref.paused and ref.state == "off" and new != prev:
                    self._notices.append(WN.NOTICE_RESTORED_CLOSED)
        except Exception:                                                                    # noqa: BLE001
            ref = None
        self._win_ref = ref

    def on_session_end(self, messages):
        if not self.active or self.ws["key"] is None:
            return
        sid, mem = self.session, self.mem
        self._worker.submit(lambda: mem.end_session(HOST, sid))                              # 可能要等数据库、做维护：交给自己的后台线程，不占宿主线程

    def on_pre_compress(self, messages):
        return ""

    def _window_status(self):
        if self.win is None or not self.session:
            return None
        try:
            w = self.win.resolve(HOST, self.session, "")
            return {"state": w.state, "control_version": w.control_version, "paused": w.paused, "pause_reason": w.pause_reason, "bound_via": w.bound_via}
        except Exception:                                                                    # noqa: BLE001
            return {"state": None, "paused": True, "pause_reason": "window_state_unreadable"}

    def _display_status(self):
        """直接显示（B）还是靠 agent 转述（A）：钩子注册了、而且宿主真的调过它，才算 B；注册了没被调过是 B_unconfirmed；没注册是 A。"""
        mode = "A" if not self.hook_registered else ("B" if self._hook_calls > 0 else "B_unconfirmed")
        return {"mode": mode, "hook_registered": bool(self.hook_registered), "hook_calls": self._hook_calls}

    def _organizer_key(self):
        """整理用密钥在**这个插件进程里**看不看得到（只说有没有、来源，不读值）。向导没法替代这个：它看的是自己的环境。"""
        try:
            from . import keystore as KS
            cp = self.cfg.get("config_path")
            p = (Config.load(cp).provider if cp else Config().provider) or {}
            env = p.get("api_key_env")
            if not env:
                return {"env": None, "source": "not_needed"}
            return {"env": env, "source": KS.source(env, p.get("key_file"))}
        except Exception as e:                                                                   # noqa: BLE001
            return {"env": None, "source": "unknown", "error": type(e).__name__}

    def status(self):
        return {"organizer_key": self._organizer_key(), "active": self.active, "inactive_reason": self.inactive_reason, "inactive_message": self._inactive_message(), "profile": dict(self.profile), "evidence": self.evidence, "owner_basis": self.owner_basis, "recorded_turns": self.recorded_turns,
                "skipped": dict(self.skipped), "mode": self.cfg.get("mode", "tools_only"), "read_scope": self.scope, "worker_errors": list(self._worker.errors),
                "workspace": {"key": self.ws["key"], "kind": self.ws["kind"], "reason": self.ws["reason"]},
                "window": self._window_status(), "direct_display": self._display_status(),
                "compat": {"off": {k: v["reason"] for k, v in self._probe["features"].items() if not v["on"]}, "extra_abstract": list(self._probe["extra_abstract"])}}
