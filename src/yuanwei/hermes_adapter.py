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
* **写入只收传进来的两段话**，外加这一轮里用户在助手提问卡（Hermes 的 clarify 工具）里的回答：只认 clarify 这一个「问用户」工具的现行结果格式，
  按固定模板接在用户那条话后面（asked.py、hermes_clarify.py；清单第 53 条，0.1.0a8）；别的工具输出、messages 本身一律不存。配对用内容指纹（轮次号回退后会重复，不能当编号）。sync_turn 在 Hermes 自己的后台单线程里跑；会话结束、切换这类可能做重活的事交给自己的后台线程，不占宿主线程。
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

from . import asked as AS
from . import askcounts as AC
from . import control as CT
from . import hermes_clarify as HCL
from . import hermes_compat as HC
from . import hermes_midturn as HM
from . import hermes_profile as HP
from . import hermes_turn as HT
from . import hermes_workspace as HW
from . import hosts as H
from . import internal_errors as IE
from . import owners as OW
from . import skipreasons as SK
from . import window as WN
from .access import Access
from .config import Config
from .idsource import InProcessIdentity
from .mcp.server import SCHEMAS
from .memory import Memory
from .modes import ModeConfig, ModeRunner
from .reader import purge_all
from .storeerr import MessageConflict
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


_HINT_LINE = re.compile(r"\[Image attached(?: at)?: [^\r\n]+\]")


def _split_native_hints(text, count):
    """文字末尾恰好是 Hermes 追加的 N 行完整图片提示（前面隔一个空行）：返回提示前面的字；不是这个格式返回 None。"""
    caption, separator, suffix = text.rpartition("\n\n")
    hints = suffix.split("\n")
    if separator and len(hints) == count and all(_HINT_LINE.fullmatch(line) for line in hints):
        return caption
    return None


def _hinted_row_match(messages, sync_text):
    """只看最近的那一条用户行（不往回借历史行）。Hermes v0.23.2 真机上带图的一轮，这一行是列表：一个文字块「字＋空行＋每张图一行提示」加 N 个图片块；
    记忆那头拿到的不是 0.1.0a5 假定的「[N image(s)] 字」，而是下面两种（复现见交回报告）：
    * 附件路由（persist_user_message 是用户打的字）：开始和同步都是用户打的字，不带提示；
    * API 会话：开始和同步都是和这一行文字块完全一样的「字＋提示」。
    只认这两种、只认恰好 N 行完整提示；对不上返回 None，由调用方记原因。返回（开始登记用的原文字、图片数、行号、要存的字）。"""
    for idx in range(len(messages) - 1, -1, -1):
        row = messages[idx]
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        if not isinstance(row.get("content"), list):
            return None
        bits, count = [], 0
        for part in row["content"]:
            if isinstance(part, str):
                if part:
                    bits.append(part)
            elif isinstance(part, dict):
                kind = str(part.get("type") or "").strip().lower()
                if kind in ("image_url", "input_image"):
                    count += 1
                elif kind in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                    if part["text"]:
                        bits.append(part["text"])
                else:
                    return None
            else:
                return None
        if not count or len(bits) != 1:                                # 没有图片块，或文字块不止一个（宿主追加的别的文字块认不出来，不猜）
            return None
        text = bits[0]
        caption = _split_native_hints(text, count)
        if caption is None:
            return None
        if sync_text == text:                                          # API 会话：两头都带提示；提示不存
            return text, count, idx, caption
        if sync_text.strip() == caption:                               # 附件路由：记忆那头是用户打的字（宿主构造函数会去掉两头空白，记忆那头没去；逐字存记忆那头的）
            return sync_text, count, idx, sync_text
        return None
    return None


def _image_sync_match(messages, sync_text):
    """只凭真实图片块重建 Hermes 的同步文本；绝不剥掉用户字面的 [N images]。
    Hermes v0.23.2 的类型和拼接契约由 doctor 核验。只取文字和图片数量，不读图片 URL/路径。
    返回（开始登记用的原文字、图片数量、已经核对过的行号、要存的字）；未知块保守不认。
    先看最近那一行是不是真机上的两种带提示形状（_hinted_row_match）；不是再按 0.1.0a5 的「同一份列表两头用」认。
    """
    hit = _hinted_row_match(messages, sync_text)
    if hit:
        return hit
    for idx in range(len(messages) - 1, -1, -1):
        row = messages[idx]
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        # 最近的同文字普通行是字面输入，不能越过它借历史图片块解释这个标记。
        if _flatten(row.get("content")) == sync_text:
            return None
        if not isinstance(row.get("content"), list):
            continue
        bits, count, supported = [], 0, True
        for part in row["content"]:
            if isinstance(part, str):
                if part:
                    bits.append(part)
            elif isinstance(part, dict):
                kind = str(part.get("type") or "").strip().lower()
                if kind in ("image_url", "input_image"):
                    count += 1
                elif kind in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                    if part["text"]:
                        bits.append(part["text"])
                else:
                    supported = False
            else:
                supported = False
        text = "\n".join(bits)
        if supported and not count and text.strip() == sync_text:
            return None
        if not supported or not count:
            continue
        marker = "[%d image%s]" % (count, "" if count == 1 else "s")
        expected = " ".join(bit for bit in (marker, text.strip()) if bit)
        if sync_text == expected:
            return text, count, idx, _without_native_image_hints(text, count)
    return None



def _without_native_image_hints(text, count):
    """已证实有图片块后，只移走 native builder 追加的末尾 N 条完整路径提示。
    用户较早的同名字面提示保持；默认英文问句不能与真实输入区分，保留。
    手工构造与宿主完全同构的 payload 仍不可认证，不能把此规则称为来源签名。
    """
    caption, separator, suffix = text.rpartition("\n\n")
    hints = suffix.split("\n")
    if separator and len(hints) == count and all(re.fullmatch(r"\[Image attached(?: at)?: [^\r\n]+\]", line) for line in hints):
        return caption
    return text


def _synthetic_flag(row):
    """行上带宿主的合成标记（不含 display_kind）。"""
    return any(row.get(f) for f in SYNTHETIC_USER_FLAGS) or any(isinstance(k, str) and k.startswith("_") and k.endswith("_synthetic") and v for k, v in row.items())


def _machinery(row):
    if row.get("display_kind"):
        return True
    return _synthetic_flag(row)


_WELD_KEYS = ("_compressed_summary", "_merged_turn_prefix", "_absorbed_message_uids", "_absorbed_row_ids")      # 宿主把别的行并进来时留的记号：带着它们的待办快照行不是「只有一个机器标记」


def _only_todo_snapshot(row):
    """宿主压缩时插的待办快照行：role 是 user，只有 _todo_snapshot_synthetic 这一个机器标记（没有 display_kind、没有别的合成标记、没有并行的记号）。清单第 56 条第 5 节。"""
    if not isinstance(row, dict) or row.get("role") != "user" or not row.get("_todo_snapshot_synthetic"):
        return False
    if any(row.get(k) for k in _WELD_KEYS):
        return False
    return not _machinery({k: v for k, v in row.items() if k != "_todo_snapshot_synthetic"})


_PHYSICAL_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _with_lines(texts):
    """每段文字，加上它的每个物理行（多行的才加）：宿主把几次插话用换行连成一段，口令只认第一行，第二条插话里的口令就认不到。"""
    out = []
    for t in texts:
        out.append(t)
        lines = _PHYSICAL_BREAK.split(t)
        if len(lines) > 1:
            out.extend(x for x in lines if x.strip())
    return out


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
        self.host_cwd_dropped = 0                                                           # 问 Hermes「这一轮的文件夹」时，取到了但不能用、被丢掉的值有几个（只记个数）
        self._cwd_recheck = False                                                           # 经典终端恢复会话：钩子在 Hermes 切目录之前被调，到下一轮开始再按进程目录重判一次
        self._mems, self._surfaces = {}, {}
        self._pending = {}                    # 会话 → [{token, number, fp, admitted, reason}]（还没配上 sync_turn 的轮次，先进先出）
        self._cur = {}                        # 会话 → 当前轮的轮次标记
        self._suspect_wait = set()            # (宿主, 会话, 分支)：库打不开（self.win 还是 None）时发生的「没确认已关」；下一次建好窗口服务时并进它的 _suspect 再清空
        self._turn_failed = {}                # 会话 → True：这个会话的上一次开轮失败了、还没有新的开轮成功（清单第 56 条第 2 节）
        self._prefetched = set()
        self.evidence, self.owner_basis = None, None
        self.recorded_turns, self.skipped = 0, {}
        self.clarify = {"answers_kept": 0, "turns_with_answers": 0, "notes": {}}      # 这个实例里提问卡回答的计数（实例会换：重启、闲置、重新生成、换模型…，换了计数就没了；累计的在 clarify-counts.json，见 askcounts.py）
        self._owner_admitted = False
        self._ignored_seen = set()                 # 仅内存去重；不持久保存消息、作者或事件定位
        self._bound_session = None
        self._deferred_window_switch = None
        self._init_history = None                  # (会话号, 宿主说这个会话一开始是否带着历史)：只用来决定没见过的会话的新窗口从开还是从关开始（清单第 56 条第 8 节）
        self.owner_counter_errors = 0
        self.mem = self.acc = self.ids = self.runner = None
        self.win = None                       # WindowService（接头用的库里的窗口状态，不按工作区分区）
        self._win_ref = None                  # 当前会话对应的窗口（最近一次映射的结果；状态里给人看）
        self._cur_ref = {}                    # 会话 → 当前这条输入的 InputRef
        self._notices = []                    # 状态提交成功以后要提示用户的话（关上、打开、恢复一个关着的窗口）
        self._display = []                    # 要直接显示给用户的固定文字（删除的提示和回执）：transform_llm_output 钩子有就接在助手回复后面
        self._late = []                       # 同步时排下的提示（提问卡里的确认不算数、提问卡里打的「关」没关成）：(哪一种, 文字)；下一条本人输入处理完以后再放进 _notices、_display
                                              # （那一条已经让它不成问题了就不放）。只在内存里：Hermes 重启或会话闲置超过约一小时后换了新实例，这句提示会丢，删除、开启本身照旧不会发生
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
        self._surfaces = {ws["key"]: ToolSurface(self.mem, self.acc, prof, self.ids, home=self._home, writes=True)}
        self.runner = ModeRunner(self.mem, self.acc, self._runner_profile(prof), ModeConfig(mode=self.cfg.get("mode", "tools_only")))
        self.ids.set(HOST, self.session, "", None)
        dflt = self.cfg.get("window_default", "on")
        carried = set(self.win._suspect) if self.win is not None else set()                 # 「没确认已关」的标记要跟着走：换了新的窗口服务不能把它丢掉
        self.win = WN.WindowService(self.mem.store, clock=self.mem.clock, default_state=dflt if dflt in ("on", "off") else "off", settings=self._setting_block)   # 默认值认不得：按最严（关）
        self.win._suspect |= carried
        self.win._suspect |= self._suspect_wait                                             # 库打不开的那一轮留下的标记也并进来，然后清空
        self._suspect_wait.clear()
        from .deletion import DeletionService
        self.win.deleter = DeletionService(self.mem.db_path, store=self.mem.store, config_dir=self.mem.config_dir, clock=self.mem.clock)
        self.acc.windows = self.win

    def prefetch_limit(self):
        """便条（预取）的字符上限：照用户的 Hermes 设置读一次、记住；读不到退回保守的 10,000，不影响插件加载。"""
        if getattr(self, "_prefetch_limit", None) is None:
            self._prefetch_limit = HC.prefetch_limit()
        return self._prefetch_limit

    def _runner_profile(self, prof):
        return prof.with_hook_chars(self.prefetch_limit()["chars"])

    def _setting_block(self, host, session_id):
        """被哪条设置挡住（设置优先，自然语言打不开设置）：workspace｜session｜None。"""
        r = self.acc.session_permission(host, session_id)
        return None if r["allowed"] else (r["denied_by"][0]["scope"] if r["denied_by"] else "workspace")

    def _surface_for(self, key):
        """别区读取借当前轮许可，另核来源设置；来源记交付证据，当前区只记无内容的活动计数。"""
        if key in self._surfaces:
            return self._surfaces[key]
        if not self._ws_exists(key):
            return None
        m = self._open(key)
        from .cross_recall import SourceAccess
        borrowed = SourceAccess(m, self.acc, mirror_owner=True)      # 来源日志+owner活动计数；仍核来源禁读和删除版本
        sf = ToolSurface(m, borrowed, H.profile(HOST), self.ids, home=self._home, writes=True)
        self._surfaces[key] = sf
        return sf

    # ---------------------------------------------------------------- 生命周期
    def initialize(self, session_id, **kw):
        ctx, plat = kw.get("agent_context"), kw.get("platform") or "cli"
        self.session, self.platform = session_id, plat
        self._home = kw.get("hermes_home")
        rs, mc = kw.get("resumed"), kw.get("message_count")
        self._init_history = (session_id, rs is True or (isinstance(mc, int) and not isinstance(mc, bool) and mc >= 2))     # 不是布尔、不是整数的值当没有
        self._init_kw = {k: kw.get(k) for k in ("user_id", "user_id_alt", "chat_type", "chat_id", "cwd")}     # chat_id 只给「是不是一对一」用（Slack 私信的编号以 D 开头），只在内存里
        if ctx is None:
            self.inactive_reason = "agent_context_missing"                                  # 宿主没传：不知道是不是主对话，按说不清处理（这四版宿主都总会传，见 doctor 的 init_kwargs）
        elif ctx != "primary":
            self.inactive_reason = "agent_context_%s" % ctx
        elif plat == "cron":
            self.inactive_reason = "platform_cron"
        elif self._group_chat():
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
        self.ws = self._resolve_ws(self._init_kw["cwd"], plat)
        # 远程消息到来前不建库、不记窗口绑定：session_id 本身可能包含未登记者的账号。
        self._owner_admitted = plat in HW.LOCAL_PLATFORMS and self._owner_check(None)[0]
        if self._owner_admitted:
            self._prepare_owner_workspace()
        self._dump_status()

    def _group_chat(self):
        return self.platform in HP.GROUP_ROOM_PLATFORMS or (
            self.platform not in HW.LOCAL_PLATFORMS
            and str(self._init_kw.get("chat_type") or "").lower() in ("group", "forum", "channel"))

    def _prepare_owner_workspace(self):
        """只有本人确认后才创建记忆面和持久窗口绑定。"""
        if self.mem is None and self.ws["key"] is not None:
            self._set_workspace(self.ws)
        if self.win is not None and self._deferred_window_switch is not None:
            pending, self._deferred_window_switch = self._deferred_window_switch, None
            self._window_switch(*pending)
        if self.win is not None and self._bound_session != self.session:
            try:
                ih = self._init_history
                self._win_ref = self.win.bind_new(HOST, self.session, "", via="initialize", history=bool(ih and ih[0] == self.session and ih[1]))
                self._bound_session = self.session
                if getattr(self._win_ref, "history_closed", False):                                             # 一开始就带着历史、库里有窗口关着：这个新窗口从关开始，第一次开轮给一句提示（只在这次新绑定时）
                    self._notices.append(WN.NOTICE_HISTORY_CLOSED)
                    self._display.append("〔原委〕" + WN.NOTICE_HISTORY_CLOSED)
            except Exception:                                                                # noqa: BLE001
                self._win_ref = None

    def _resolve_ws(self, cwd, plat):
        """认工作区：全走 HW.resolve。宿主没传 cwd、平台是 local／tui／desktop（本机平台去掉经典终端，网关聊天平台不问）时，resolve 会问 Hermes 这一轮的文件夹（agent.runtime_cwd / gateway.session_policy，绝不用进程目录）；
        取到但不能用的值只记个数（host_cwd_dropped），不记内容。只在 initialize 和恢复会话的钩子里用；这一轮之外（整理用的会话、旁支和辅助会话等）按没取到处理；它们不在验证范围内，不承诺统一落入「生活」。"""
        def _drop(n):
            self.host_cwd_dropped += n
        return HW.resolve(self._home or "", cwd, plat, on_drop=_drop)

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
        if self._group_chat():
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
        所以那一刻取到的进程目录是旧的；到下一轮开始（切目录早已做完）再按同一条规则重判。
        只有这一步成功以后才把重判标记置 False；失败（库打不开）就把工作区、记忆面都退回原样（不留「工作区换了、库没换」的半截状态），标记留着，下一轮再试。"""
        with self._lock:
            if not self._cwd_recheck:
                return
            before = (self.ws, self.mem, self.acc, self.ids, self.runner, self._surfaces, self.win)
            try:
                self._adopt_workspace(HW.resolve(self._home or "", None, self.platform))
            except BaseException:
                self.ws, self.mem, self.acc, self.ids, self.runner, self._surfaces, self.win = before
                raise
            self._cwd_recheck = False
            self._dump_status()

    def on_turn_start(self, turn_number, message, **kw):
        """开一轮。整段出任何错：记内部错误（宿主会把异常吞进调试日志，不留这一笔就没人知道）、不许沿用上一轮的身份去读、这句话里有「关」就让读取先停住，然后原样抛出（清单第 56 条第 2 节）。"""
        try:
            registered = self._on_turn_start(turn_number, message, **kw)
        except Exception as e:                                                               # noqa: BLE001
            self._turn_start_failed(message, kw, e)
            raise
        if registered and self._turn_failed:
            tok = self._cur.get(self.session)
            if isinstance(tok, str) and not tok.startswith("ignored-"):
                self._turn_failed.pop(self.session, None)                                   # 有新的开轮成功了：只认已确认本人、真登记上了的这一轮（别人的消息、没登记上的不算）

    def _turn_start_failed(self, message, kw, exc):
        """开轮失败的收尾。每一步自己判空、自己吞错，不能盖掉原来的异常。"""
        try:
            IE.record(self._home, exc)
        except Exception:                                                                    # noqa: BLE001
            pass
        sid = self.session
        try:
            owner = (not kw.get("author_is_bot")) and bool(self._owner_check(kw.get("author_id"))[0])
        except Exception:                                                                    # noqa: BLE001
            owner = False
        try:
            with self._lock:
                self._cur.pop(sid, None)
                if self.ids is not None:
                    self.ids.set(HOST, sid, "", None)                                        # 不许沿用上一轮的身份去读
                if owner:
                    self._turn_failed[sid] = True
        except Exception:                                                                    # noqa: BLE001
            pass
        if not owner:
            return
        try:
            text = message if isinstance(message, str) else ""
            texts = [text]
            try:
                ctl = self._turn_facts(text)[2]                                              # 技能命令里用户打的字、来源说明后面的字：正式开轮时是按这份文字认「关」的，这里也要认
                if isinstance(ctl, str) and ctl != text:
                    texts.append(ctl)
            except Exception:                                                                # noqa: BLE001  取不到就只用原文
                pass
            wants_close = any(k == "close" for t in texts for k, _s in WN.WindowService._plan(t)[1])
            if wants_close:
                self._mark_suspect((HOST, sid, ""))                                          # 这句话里的「关」没处理成：在一次成功的关闭落地之前，进程内一律不许读（库打不开、self.win 是 None 时先记在 _suspect_wait 里）
                shown = "〔原委〕" + WN.CLOSE_FAILED_RECEIPT
                if shown not in self._display:                                               # 连着两次开轮失败：已有同样的一条就不再加，不重复说
                    self._display.append(shown)                                              # 这一轮回复后面就显示（预取在这一轮会提前返回，拿不出 _notices）
                if not any(x[0] == "close_failed" and x[1] == self.CLOSE_FAILED_START for x in self._late):
                    self._late.append(("close_failed", self.CLOSE_FAILED_START))             # 下一轮再说一遍（用过去时）；那一轮关成了、或明确开了就不说
        except Exception:                                                                    # noqa: BLE001
            pass

    def _mark_suspect(self, key):
        """「没确认已关」的标记：窗口服务在就记在它那里；库打不开（self.win 是 None）就先记在 _suspect_wait，建好窗口服务时并进去。"""
        if self.win is not None:
            self.win._suspect.add(key)
        else:
            self._suspect_wait.add(key)

    def _has_suspect(self, key):
        return key in self._suspect_wait or (self.win is not None and key in self.win._suspect)

    def _on_turn_start(self, turn_number, message, **kw):
        if self.active and self._cwd_recheck:
            self._recheck_workspace_after_resume()
        if not self.active or self.ws["key"] is None:
            return
        text = message if isinstance(message, str) else ""
        evidence = "author_fields" if "author_is_bot" in kw else "legacy_no_author_field"
        fp = Access.text_fp(text)
        with self._lock:
            sid = self.session
            self.evidence = evidence
            owner, why = (False, "bot_author") if kw.get("author_is_bot") else self._owner_check(kw.get("author_id"))
            self._owner_admitted = owner
            lst = self._pending.setdefault(sid, [])
            if not owner:
                # 拒绝的事件只在内存配对 sync_turn；不调用会持久写 turn_tokens/审计的 Access。
                event = (sid, str(turn_number), fp)
                if event in self._ignored_seen:
                    return
                self._ignored_seen.add(event)
                token = "ignored-" + secrets.token_hex(8)
                lst.append({"token": token, "number": turn_number, "fp": fp, "admitted": False, "reason": why, "capture": None})
                self._cur[sid] = token
                if self.ids is not None:
                    self.ids.set(HOST, sid, "", token)
                if self.platform not in HW.LOCAL_PLATFORMS and why in ("owner_unknown", "not_owner"):
                    try:
                        if OW.increment(self._home, self.platform) is False:
                            self.owner_counter_errors += 1
                    except Exception:                                                        # noqa: BLE001  计数失败不能放行消息
                        self.owner_counter_errors += 1
                self._dump_status()
                return
            self._prepare_owner_workspace()
            origin, alt_fp, ctl = self._turn_facts(text)
            token, reissued = self.acc.event_turn(HOST, sid, "%s:%s" % (turn_number, fp[:12]))     # 同一个事件重送还是同一个号；那一轮已经结束了再来同样的话（重新生成、/retry、压缩换号）是新的一轮；reissued＝登记在续号键上，窗口层只认「关」
            if any(e["token"] == token for e in lst):
                self.ids.set(HOST, sid, "", token)
                self._cur[sid] = token
                return True
            try:
                cap = self.mem.store.take_capture()
            except Exception:                                                                # noqa: BLE001
                cap = None
            self.owner_basis = why
            self.acc.begin_turn(HOST, sid, token, text=ctl, origin="user")
            lst.append({"token": token, "number": turn_number, "fp": fp, "admitted": True, "reason": None, "capture": cap, "origin": origin})        # origin：宿主说这一轮是谁开的（只有 user／host／None，不存文字）
            if alt_fp is not None:
                lst[-1]["alt_fp"] = alt_fp                                                   # 技能命令：用户打的那几个字的指纹（不存字）
            self._cur[sid] = token
            self.ids.set(HOST, sid, "", token)
            self._window_input(sid, token, turn_number, fp, ctl, cap, lst[-1], closes_only=reissued)
            return True

    def _turn_facts(self, text):
        """这一轮开始时向宿主问到的三样（出任何错都当不知道）：谁开的（origin）、技能命令里用户打的字的指纹（alt_fp）、交给口令判断的那段文字（ctl）。
        口令判断默认还是整段原文（和今天一样）；只有两种情况换成「去掉宿主加的东西以后的字」，而且**只认关**：技能命令里用户打的字、聊天平台开头那三行来源说明后面的字，
        第一行恰好是 #yuanwei-nomem 才换；开、确认开启、确认删除在这两处都不认（换的只会是一段第一行就是关口令的字）。"""
        origin, alt_fp, ctl = None, None, text
        try:
            origin = HT.turn_origin(text)
            if origin == "user" and (self.platform == "desktop" or self.platform not in HW.LOCAL_PLATFORMS):
                words = HT.typed_words(text)
                if words is not None:
                    alt_fp = Access.text_fp(words)
                    if CT.parse(words).kind == "nomem":
                        ctl = words
            if ctl == text and self._envelope_applies() and text.startswith(HM.ENVELOPE_HEAD):
                status, rest, bot = HM._strip_envelope(text)
                if status == "ok" and not bot and rest.strip() and CT.parse(rest).kind == "nomem":
                    ctl = rest
        except Exception as e:                                                               # noqa: BLE001
            IE.record(self._home, e)
        return origin, alt_fp, ctl

    def _envelope_applies(self):
        """聊天平台（非本机平台）的一对一对话：开头的来源说明是宿主加的，可以去掉。共用的会话、本机平台一个字都不动。"""
        return self.platform not in HW.LOCAL_PLATFORMS and self._private_chat()

    def _window_input(self, sid, token, turn_number, fp, text, capture=None, entry=None, closes_only=False):
        """这一条可信输入的窗口控制（口令、规则识别），在便条、目录、预取看窗口状态之前做完。关闭写失败时窗口进入「没确认已关」，读取被拦住，不说已关。"""
        self._retract_stale_receipts()                                                       # 上一轮排下、没显示出去的回执（宿主对被停下或空的回复不调显示钩子）：这一条处理之前先收回，免得说的和现在的状态对不上
        ref = WN.InputRef(HOST, sid, token, "%s:%s" % (turn_number, fp[:12]), fp, "", self.ws["key"])
        self._cur_ref[sid] = ref
        if entry is not None:
            entry["ref"] = ref                                                               # 这一轮的输入：sync_turn 时提问卡回答里认出「关」要按这一条的窗口和序号关（迟到的关不盖过后来有效的开）
        res = self.win.process_input(ref, text, origin_verified=True, capture=capture, closes_only=closes_only)       # 确认开启、确认删除也只在这里认：接头只对用户本人的输入调它，工具参数、工具返回、子代理确认不了
        for t in (res.notice, res.receipt):
            if t:
                self._notices.append(t)
        if closes_only and res.receipt in (WN.REISSUED_RECEIPT, WN.REISSUED_VOIDED_RECEIPT):
            self._display.append("〔原委〕" + res.receipt)                                    # 重新生成／重发没有照开、照删（或作废了等确认的申请）：当轮回复后面就看得见
        if res.state_after == "on":
            self._notices[:] = [n for n in self._notices if n != WN.NOTICE_HISTORY_CLOSED]    # 第一条输入自己就把窗口打开了：「这里先关着」那句收回
            self._display[:] = [d for d in self._display if d != "〔原委〕" + WN.NOTICE_HISTORY_CLOSED]
        late, self._late = self._late, []
        opened = res.command == "confirm" or res.state_after == "on" or any(k == "open" and o == "applied" for k, _s, o in (res.steps or ()))
        done = ({"delete"} if res.deletion is not None else set()) | ({"open"} if opened else set())
        for item in late:                                                                    # 上一轮提问卡里的确认不算数、关没关成：这一条已经让它不成问题了（真删了、窗口开着），就不再说
            kind, t = item[0], item[1]
            if kind == "close_failed":
                if (HOST, sid, "") in self.win._suspect:                                    # 关没关成：这一条处理完以后「没确认已关」的标记还在、还是这个对话才说（关上了、明确开了、换到别的对话都不说）
                    owed = item[2] if len(item) > 2 else ("note", "show")                   # 还欠谁：note＝助手还没听到，show＝用户还没看到；已经说到过的那一边不再说
                    if "note" in owed:
                        self._notices.append(t)
                    if "show" in owed:
                        self._display.append("〔原委〕" + t)
            elif kind not in done:
                self._notices.append(t)
                self._display.append("〔原委〕" + t)
        if res.deletion is not None:
            if res.receipt:
                self._display.append("〔原委〕" + res.receipt)
            if entry is not None:                                                            # 确认删除那一轮不记（持久标记在库里；这里让 sync_turn 早早跳过）
                entry["no_record"] = True
            self._after_delete(res.deletion)

    def _close_failed_texts(self):
        """「没确认已关」的四句固定文字：当轮回执，和三种「下一轮再说」。"""
        return (WN.CLOSE_FAILED_RECEIPT, self.CLOSE_FAILED_START, self.CLOSE_FAILED_LATE, self.CLOSE_FAILED_MIDTURN)

    def _retract_stale_receipts(self):
        """把上一轮剩在 _display、_notices 里的重发回执（两种）和「没确认已关」回执（_display 里带〔原委〕的那种）拿掉。别的来源放进去的文字不动。
        三种「下一轮再说」的文字（CLOSE_FAILED_START／LATE／MIDTURN）也收回，放回 _late，记着还欠谁：还在 _notices 里的＝助手还没听到（宿主跳过了 prefetch），
        还在 _display 里的＝用户还没看到（回复被停下）；由同一道标记判断决定这一条处理完以后还说不说，只补给还欠的那一边。
        _notices 里的 CLOSE_FAILED_RECEIPT（当轮口令写失败、那一轮 prefetch 没拿走）换成过去时的 CLOSE_FAILED_START 再说。"""
        gone = ("〔原委〕" + WN.REISSUED_RECEIPT, "〔原委〕" + WN.REISSUED_VOIDED_RECEIPT, "〔原委〕" + WN.CLOSE_FAILED_RECEIPT)
        later = (self.CLOSE_FAILED_START, self.CLOSE_FAILED_LATE, self.CLOSE_FAILED_MIDTURN)
        owed = {}
        for n in self._notices:
            t = self.CLOSE_FAILED_START if n == WN.CLOSE_FAILED_RECEIPT else n
            if t in later:
                owed.setdefault(t, set()).add("note")
        for t in later:
            if "〔原委〕" + t in self._display:
                owed.setdefault(t, set()).add("show")
        self._display[:] = [d for d in self._display if d not in gone + tuple("〔原委〕" + t for t in later)]
        self._notices[:] = [n for n in self._notices if n not in (WN.REISSUED_RECEIPT, WN.REISSUED_VOIDED_RECEIPT, WN.CLOSE_FAILED_RECEIPT) + later]
        for t, who in owed.items():
            if not any(x[0] == "close_failed" and x[1] == t for x in self._late):
                self._late.append(("close_failed", t, frozenset(who)))

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
        if not self.active or not self._owner_admitted or self.ws["key"] is None or not self._on("prefetch"):
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
        if tool_name == "window_off" and self._turn_failed.get(self.session) and self._cur.get(self.session) is None:
            return self._close_in_failed_turn()                                              # 正在跑的就是失败的那一轮（没有当前轮标记）才走这条；共用会话里别人那一轮调的不算，落到下面「本人没确认」那道拒绝
        if not self._owner_admitted:
            return self._j("refused", "没有确认这条消息来自本人，没有读取或操作记忆。", "turn_unproven")
        args = args if isinstance(args, dict) else {}
        if tool_name in CONTROL_TOOLS:
            return self._control_tool(tool_name)
        keys = HW.readable(self.scope, self.ws["key"], HW.known(self._home or ""))
        if tool_name not in READ_TOOLS or self.win is None:
            return self._dispatch(tool_name, args, keys)
        return self._read_with_permit(tool_name, args, keys)

    def _dispatch(self, tool_name, args, keys):
        if tool_name == "recall" and str(args.get("cursor", "")).startswith("M"):
            return self._multi(tool_name, args, keys)
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
        from .cross_recall import DELIVERY_TICKETS
        delivery_tickets = []
        binding = DELIVERY_TICKETS.set(delivery_tickets)
        try:
            out = self._dispatch(tool_name, args, keys)
        finally:
            DELIVERY_TICKETS.reset(binding)
        chk = self.win.verify_before_return(p.secret)
        if chk.deliver:
            return out
        for access, ticket in delivery_tickets:
            access.event(ticket, "blocked_at_return", {"reason": chk.reason})
            access.event(ticket, "injection_failed", {"reason": chk.reason})
        if not delivery_tickets:
            self.acc.block_after_read(ident, chk.reason)                                         # 已查询、结果挡住：照实记，不说成从没读过
        text, reason = self._window_refusal_text("window_closed" if chk.reason == "window_closed" else "control_changed", ident)
        return self._j("refused", text, reason)

    def _window_refusal_text(self, reason, ident):
        cur = self.acc.current_turn(ident.host, ident.session_id, ident.branch or "")
        r = self.acc._refusal(reason, cur, ident)
        return r["text"], r["reason"]

    # ---------------------------------------------------------------- 控制工具：只管开关，不读历史，不走 ToolSurface
    CONTROL_MESSAGES = {"on": "这个窗口的记忆读取是开着的。", "off": "这个窗口的记忆读取是关着的；要重新使用，请申请开启后让用户回复「确认开启」。"}

    def _close_in_failed_turn(self):
        """这个会话的上一次开轮失败了、还没有新的开轮成功：这一轮没有可信的轮次，关闭的许可签不出来（turn_unproven）。不能回「已关」，也不能装作没事：
        把窗口记成「没确认已关」（读取先停住），回和写失败那条一样的话。开、删照旧拒绝。"""
        self._mark_suspect((HOST, self.session, ""))
        return self._j("error", WN.CLOSE_FAILED_RECEIPT, "window_close_unconfirmed")

    def _control_tool(self, name):
        ident = self.ids.current() if self.ids is not None else None
        op = {"window_off": "close", "window_on": "open", "window_delete": "delete"}.get(name, "view")
        if self.win is None or ident is None:
            return self._j("refused", "没有可信的窗口身份，没有操作。", "identity_unbound")
        ns, install = self.ws["key"], str(self._home or "")
        pf = WN.params_fingerprint({})                                                      # 控制工具不收参数：模型填的任何参数都忽略，窗口只来自可信身份
        p = self.win.issue_control_permit(ident, op=op, params_fp=pf, install_id=install, ns=ns)
        if p.refused == "window_close_unconfirmed":
            if op != "close":
                return self._control_json(ident, {"state": "off", "blocked_by_setting": False, "reason": p.refused, "control_version": 0, "message": WN.CLOSE_FAILED_RECEIPT})
            if not self._close_unconfirmed(ident):                                       # 窗口没确认已关：用户这一轮明明要关，助手调 window_off 就真去关；关成了才解除标记，之后照今天（通知、查看结果）
                return self._j("error", WN.CLOSE_FAILED_RECEIPT, "window_close_unconfirmed")
        elif p.refused:
            return self._control_json(ident, {"state": "off", "blocked_by_setting": False, "reason": p.refused, "control_version": 0, "message": WN.PENDING_HINT})
        if p.refused is None:                                                                # （上面「没确认已关」的 close 已经自己关过，没有许可可认领）
            c = self.win.claim_permit(p.secret, kind="control", host=ident.host, install_id=install, session_id=ident.session_id, branch=ident.branch or "", ns=ns, turn_id=ident.turn_id, tool_name=op, params_fp=pf)
            if not c.ok:
                return self._j("refused", "控制许可没有通过核对，没有操作。", "control_permit_%s" % c.reason)
        if op == "close" and p.refused is None:
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

    def _close_unconfirmed(self, ident):
        """窗口处在「没确认已关」时助手调 window_off：这一轮是已证明的本人轮次，用户要关就真去关（照 window_off 平时的办法 close(source="tool")）。
        成功、而且再看窗口的真实状态（不看标记）确实是 off，才解除标记并返回 True；仍暂停、抛错、状态不是 off 都返回 False，标记不动。"""
        key = (ident.host, ident.session_id, ident.branch or "")
        try:
            w = self.win.resolve(*key, _raw=True)
            if w.paused:
                return False
            ref = self._cur_ref.get(ident.session_id)
            seq = self.win.input_seq_of(ref, _raw=True) if ref is not None else None
            r = self.win.close(w.window_id, source="tool", op_id="op-" + secrets.token_hex(8), input_win_seq=seq)
            after = self.win.resolve(*key, _raw=True)
            if after.paused or after.state != "off":
                return False
        except Exception:                                                                    # noqa: BLE001  写失败：不能说已关，标记不动
            return False
        self.win._suspect.discard(key)
        texts = self._close_failed_texts()                                                   # 关好了：同一条回复里不能一边说关好了一边说没能确认
        self._display[:] = [d for d in self._display if d not in tuple("〔原委〕" + t for t in texts)]
        self._notices[:] = [n for n in self._notices if n not in texts]
        self._late[:] = [x for x in self._late if x[0] != "close_failed"]
        if r.outcome == "applied":
            self._notices.append(WN.NOTICE_CLOSED)
        return True

    def _control_json(self, ident, d):
        return json.dumps(d, ensure_ascii=False)

    # ---------------------------------------------------------------- 读多个工作区：每一节标明来源，开头一行写读了哪些；范围只来自配置
    def _label(self, key):
        return (HW.known(self._home or "").get(key) or {}).get("label") or ("项目 %s" % key[5:] if key.startswith("proj:") else key)

    CURSOR = re.compile(r"(只给 cursor=)(\S+)")

    def _multi(self, tool, args, keys):
        from . import vectors as VX
        with VX.query_scope():
            if tool == "recall" and (self.mem.cfg.recall_ranked_pages or str(args.get("cursor", "")).startswith("M")):
                from . import cross_recall
                result = cross_recall.execute(self, args, keys)
                return self._j(result.status, result.text, result.reason)
            return self._multi_legacy(tool, args, keys)

    def _multi_legacy(self, tool, args, keys):
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
        parts, read, errors, empty, scanned = [], [], [], [], []
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
            scanned.append(self._label(key))
            if tool == "recall" and res.meta.get("selected_total") == 0:
                empty.append(self._label(key))
                continue
            text = self.CURSOR.sub(lambda mm, i=i: "%sW%d~%s" % (mm.group(1), i, mm.group(2)), res.text)
            parts.append("〔工作区：%s〕\n%s" % (self._label(key), text))
            read.append(self._label(key))
        if not parts:
            if empty and not errors:
                return self._j("ok", "（没有找到相关记录）")
            e = errors[0] if errors else None
            return self._j(e.status if e else "error", e.text if e else "没有读到。", e.reason if e else None)
        text = "〔读取范围：%s〕\n" % "、".join(scanned) + "\n".join(parts)
        ok, _d = H.fits(H.profile(HOST).paths["mcp_return"], text)
        if not ok:
            while len(parts) > 1 and not H.fits(H.profile(HOST).paths["mcp_return"], "〔读取范围：%s〕\n" % "、".join(read[:len(parts)]) + "\n".join(parts) + "\n（还有 %d 个工作区的结果没放进来，请单独再查）" % (len(read) - len(parts)))[0]:
                parts.pop()
            text = "〔读取范围：%s〕\n" % "、".join(read[:len(parts)]) + "\n".join(parts) + ("\n（还有 %d 个工作区的结果没放进来，请单独再查）" % (len(read) - len(parts)) if len(parts) < len(read) else "")
        return self._j("ok", text)

    # ---------------------------------------------------------------- 写入（只写当前那个工作区）
    def _skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1
        SK.record(self._home, why)                                                       # 实例会换（重启、闲置、重新生成、换模型…），换了计数就没了；原因另按库累计一份（只有原因和时间）
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
        if not self.active:
            return
        if not isinstance(user_content, str):
            self._skip("unsupported_user_content")
            return
        if not user_content.strip():
            self._skip("empty_user_content")
            return
        if self.ws["key"] is None:
            self._skip("workspace_unresolved")                                               # 认不出工作区：不写，也不落进生活或别的项目
            return
        if not self._on("write"):                                                           # 宿主的 sync_turn 没有 messages：没法核这一轮是不是自注入的，写入这一项关掉
            self._skip("feature_off_write")
            return
        if messages is None:
            self._skip("no_messages")
            return
        if not isinstance(messages, list) or not messages:
            self._skip("current_user_row_not_found")
            return
        messages, marks = HM.mark_rows(messages, _synthetic_flag)                           # 改口行、插话行（清单第 55 条）换成占位行；一条都没有就还是传进来的那份列表，下面和 0.1.0a8 走的路逐字节相同
        image = _image_sync_match(messages, user_content)
        original_text = image[0] if image else user_content
        fp = Access.text_fp(original_text)
        pair = None                                                                          # 没有改口配对（整句照旧找到）：None；T1：("t1", k)；T2：("t2", None)
        row_text = None                                                                      # 开头行的文字：技能命令时是整段脚手架（用户打的字只是它的一部分）；别的情形就是下面的 user_content
        with self._lock:
            ent, owner = None, None
            if image and sum(e["fp"] == fp for entries in self._pending.values() for e in entries) > 1:
                self._skip("ambiguous_pending_turn")
                return
            for sid in [session_id] + [s for s in self._pending if s != session_id]:
                for e in self._pending.get(sid, []):
                    if e["fp"] == fp:
                        ent, owner = e, sid
                        break
                if ent:
                    break
            if ent is not None and marks:                                                    # T0 配上了、这一轮开头行之后有中途的话：同一会话里同指纹的登记不止一条时，全一致取最新一条，不一致不记
                _w, idx0 = self._machinery_check(messages, user_content, image_index=image[2] if image else None)
                if idx0 is not None and any(i > idx0 for i in marks):
                    same = [e for e in self._pending.get(owner, []) if e["fp"] == fp]
                    if len(same) > 1:
                        if len({(bool(e["admitted"]), bool(e.get("no_record")), e.get("reason")) for e in same}) != 1:
                            self._skip("midturn_ambiguous")
                            return
                        ent = same[-1]
            if ent is None and not image:
                redirects = [row.get("content") for _i, (kind, row) in sorted(marks.items()) if kind == "redirect"]
                if redirects or HM.REDIRECT_SEP in user_content:
                    got = self._pair_midturn(user_content, redirects, messages, session_id)
                    if got[0] == "skip":
                        self._skip(got[1])
                        return
                    if got[0] == "found":
                        _tag, ent, owner, original_text, pair = got
            if ent is None and not image and pair is None:                                   # 技能命令：宿主交给记忆的是用户打的那几个字，开轮登记的是整段脚手架的指纹（第 7 节）
                got = self._pair_skill(fp, messages, session_id)
                if got is not None:
                    ent, owner, row_text = got
            if ent is None:
                self._skip("no_pending_turn")
                return
            self._pending[owner].remove(ent)
        if pair is not None:
            user_content = original_text                                                     # 存的是开头那一句；宿主拼上去的改口字样一个字都不存
            image = _image_sync_match(messages, user_content)                               # 带图、附件路由：交给记忆的是用户打的字（宿主把改口接在后面），行是「字＋提示」＋图片块——用还原出来的原句再配一次
        if row_text is None:
            row_text = user_content
        if not ent["admitted"]:
            self._skip(ent["reason"] or "not_admitted")
            return
        if ent.get("no_record"):                                                             # 确认删除那一轮：用户那句和助手回复都不记（库里还有持久标记，重启、压缩换号、迟到回复都盖住）
            self._skip("delete_confirm_turn")
            return
        if isinstance(turn_author, dict) and turn_author.get("is_bot"):
            self._skip("bot_author")
            return
        if ent.get("origin") == "host":                                                      # 宿主说这一轮是它自己开的（后台完成、唤醒…）：不管行里有没有标记，都不当用户的话（第 4 节）
            _w, hidx = self._machinery_check(messages, row_text, image_index=image[2] if image else None)
            if pair is None and ((hidx is not None and not any(i > hidx for i in marks)) or (hidx is None and not marks)):
                self._note_host_started(ent, session_id or owner)                           # 开头行之后有改口、插话行的（宿主开的轮里用户又说了话）：整轮不记，但不算「宿主自己开的」，留在没记里
            self._skip("machinery_row")
            return
        user_origin = ent.get("origin") == "user" and not self._host_opening(user_content)    # 这一轮许不许放宽：宿主证明是用户开的、而且开头句（技能命令时是用户打的字）不是宿主自己写的开头
        why, idx = self._machinery_check(messages, row_text, image_index=image[2] if image else None, user_origin=user_origin)
        opening_only = False
        if why == "current_user_row_not_found" and not (marks and not self._private_chat()) and self._opening_unproven_ok(messages, user_content, image, pair, user_origin):
            why, opening_only = None, True                                                   # 宿主证明是用户开的、开头行被压缩改写或并走了：只记开头句和回复（第 5 节 b）
        if why:
            if why == "machinery_row" and pair is None and idx is not None and isinstance(messages[idx], dict) and _machinery(messages[idx]) and not any(i > idx for i in marks):
                self._note_host_started(ent, session_id or owner)                           # 宿主自己开的轮（后台完成、委派完成唤醒…）：对账里单列，不算「没记」
            self._skip(why)
            return
        if not image:
            kind, shown = self._origin_note(user_content)                                    # 聊天平台一对一、开头三行来源说明：存去掉以后的字；是机器人发的不记（第 6 节）
            if kind == "bot":
                self._skip("bot_author")
                return
            user_content = shown
        sid = session_id or owner
        uid = "%s-u" % ent["token"]
        t = _now()
        if image:
            # 原文字逐字符保留；图片仅留数量，给后续多模态接入一个明确的文本占位。
            caption = image[3]
            user_content = (caption + "\n" if caption else "") + "（带 %d 张图）" % image[1]
        mid, mid_notes, cut = None, (), False
        positions = [] if opening_only else sorted(i for i in marks if i > idx)             # 开头行之后的占位行＝这一轮中途的话；开头行没找到时没有可靠的行位置，不收
        if positions or pair is not None:
            if not self._private_chat():
                if pair is None and {marks[i][0] for i in positions} <= {"redirect"}:
                    pass                                                                    # 共用会话、带图列表那条路、只有改口：照 0.1.0a8 记开头和回复，不收改口的字、不认口令
                else:
                    self._skip("midturn_shared_chat")                                       # 共用的会话里别人的话也会走改口、插话：这一轮照 0.1.0a8 不记
                    return
            else:
                mid, mid_notes, cut = self._midturn_rows(marks, positions, idx, pair, isinstance(messages[idx].get("content"), list))
        if opening_only:
            folded = AS.Folded((), (AS.NOTE_OPENING_ONLY,), 0, ())                           # 不收提问卡回答、不收中途的话，备注一笔
        else:
            folded = self._fold_clarify(messages[:idx + 1] if cut else messages, idx, user_content, original_text, mid=mid, extra_notes=mid_notes)     # 提问卡里的回答、中途又说的话（没有就是空的，这一轮和 0.1.0a8 逐字节相同）；cut：idx 很可能不是这一轮，提问卡也不读后面的
        user_content = AS.fold(user_content, folded.lines)
        try:
            try:
                r = self.mem.record(HOST, sid, uid, "user", user_content, time=t, turn_id=ent["token"], capture=ent.get("capture"))            # 绑定这一轮：之后这一轮的读取把它排除，不当历史交回来
            except MessageConflict:                                                          # 两个同步抢同一个轮次号（旧一轮的同步比重新生成的开轮还晚到）：这一对换一个本地新号记下来，不丢（只在 MessageConflict 时；清单第 56 条第 3 节）
                ent = dict(ent, token="t-" + secrets.token_hex(8))
                uid = "%s-u" % ent["token"]
                r = self.mem.record(HOST, sid, uid, "user", user_content, time=t, turn_id=ent["token"], capture=ent.get("capture"))
                self._note_conflict_token(sid, ent["token"])
            if r.get("blocked"):                                                             # 被删除屏障挡住（确认删除那一轮、删除前开始的迟到写入）：不记
                self._skip("blocked_%s" % r["blocked"])
                return
            self._clarify_after_record(ent, folded)
            if isinstance(assistant_content, str) and assistant_content.strip():
                self.mem.record(HOST, sid, "%s-a-%s" % (ent["token"], Access.text_fp(assistant_content)[:8]), "assistant", assistant_content, time=t, turn_id=ent["token"], capture=ent.get("capture"))
        except Exception:                                                                    # noqa: BLE001
            self._skip("record_error")
            raise
        self.recorded_turns += 1
        self._dump_status()

    # ---------------------------------------------------------------- 清单第 56 条第一批：谁开的这一轮、技能命令、来源说明
    def _pair_skill(self, fp, messages, session_id):
        """技能命令：同步给的字（用户打的那几个字）的指纹没配上任何登记时，找 alt_fp 对得上的登记；而且最后一条没标记的用户行的指纹必须正好等于那条登记的整段原文指纹
        （防止没同步过的旧技能登记配到后面的普通话上）。在 self._lock 里调。返回（登记, 会话, 开头行的文字）｜None。"""
        last = None
        for row in reversed(messages):
            if isinstance(row, dict) and row.get("role") == "user" and not _machinery(row):
                last = _flatten(row.get("content"))
                break
        if last is None:
            return None
        last_fp = Access.text_fp(last)
        for sid in [session_id] + [s for s in self._pending if s != session_id]:
            for e in self._pending.get(sid, []):
                if e.get("alt_fp") == fp and e["fp"] == last_fp and e["admitted"]:
                    return e, sid, last
        return None

    def _host_opening(self, text):
        """这句开头是不是宿主自己写的（两个放宽都不许放进去）：方括号开头、`Set up the '`、飞书会议邀请（HT.host_own_head）；
        或开头是聊天平台的来源说明而这份说明认不得（解不开、几份并在一起、不是白名单的一对一、机器人、去掉以后是空的）；或说明解得开、去掉以后的字是宿主自己写的开头。
        一对一解得开的一份说明、后面是用户的话：不算（去掉说明以后照记）。"""
        if HT.host_own_head(text):
            return True
        if isinstance(text, str) and text.startswith(HM.ENVELOPE_HEAD):
            kind, rest = self._origin_note(text)
            return kind != "ok" or HT.host_own_head(rest)
        return False

    @staticmethod
    def _opening_unproven_ok(messages, user_content, image, pair, user_origin):
        """开头行没找到时，只在这几条同时成立才记开头句和回复：宿主证明是用户开的、开头句不是宿主自己的开头（user_origin 已经把这两条算好）；配上的登记就是这一轮（不是改口配对）；
        不是带图（文字里没有图片提示行、最后一条用户行不是带图片块的列表）；从后往前第一条用户行（跳过待办快照）不是机器行。读不到用户行也不记。"""
        if not user_origin or image or pair is not None or _HINT_LINE.search(user_content):
            return False
        for row in reversed(messages):
            if isinstance(row, dict) and row.get("role") == "user":
                if _only_todo_snapshot(row):
                    continue
                return isinstance(row.get("content"), str) and not _machinery(row)         # 列表（带图片块）的行：不走这条路
        return False

    def _origin_note(self, text):
        """聊天平台一对一的开头来源说明（三行＋空行）：("ok", 去掉以后的字)｜("bot", 原文)｜(None, 原文)。认不得（残句、JSON 解不开、几份并在一起）、去掉以后是空的，都照今天原样存。
        指纹、令牌、配对一直用原文；只有最后交给 record 的字换了。"""
        if not text.startswith(HM.ENVELOPE_HEAD) or not self._envelope_applies():
            return None, text
        status, rest, bot = HM._strip_envelope(text)
        if status != "ok":
            return None, text
        if bot:
            return "bot", text
        return ("ok", rest) if rest.strip() else (None, text)

    # ---------------------------------------------------------------- 助手没答完时用户又发的话（清单第 55 条，0.1.0a9）
    def _pair_midturn(self, user_content, redirects, messages, session_id):
        """整句按指纹没配上（宿主把改口接在原句后面，交给记忆的是拼好的串）：T1 用真实的改口行还原原句，T2 退一步只认开头那一句。在 self._lock 里调。
        返回 ("none",)（和改口无关，照旧记 no_pending_turn）｜("skip", 原因)｜("found", 登记, 会话, 开头那一句, 配对）；配对是 ("t1", k) 或 ("t2", None)。
        等待表里同指纹有几条：admitted、no_record、reason 全一致就取最新一条，不一致记 midturn_ambiguous 不记。"""
        def known(text):
            f = Access.text_fp(text)
            return any(e["fp"] == f for entries in self._pending.values() for e in entries)
        kind, basis, k = HM.pair_t1(user_content, redirects, known) if redirects else ("none", None, None)
        if kind == "many":
            return "skip", "midturn_ambiguous"
        if kind == "one":
            opening, pair = basis, ("t1", k)
        else:
            if HM.REDIRECT_SEP not in user_content:
                return ("none",)
            t2 = HM.pair_t2(user_content, messages, known)
            if t2 is None:
                return "skip", "midturn_unmatched"
            opening, pair = t2[1], ("t2", None)
        f = Access.text_fp(opening)
        order = [session_id] + [s for s in self._pending if s != session_id]
        matches = [(sid, e) for sid in order for e in self._pending.get(sid, []) if e["fp"] == f]
        if len({(bool(e["admitted"]), bool(e.get("no_record")), e.get("reason")) for _s, e in matches}) != 1:
            return "skip", "midturn_ambiguous"
        owner = matches[0][0]
        return "found", [e for s_, e in matches if s_ == owner][-1], owner, opening, pair

    def _midturn_rows(self, marks, positions, idx, pair, row_is_list):
        """开头行之后的占位行 → (hermes_clarify.collect 要的 mid 字典｜None, 备注, 提问卡要不要只读到开头行)。
        开头行是字符串时，宿主一定把改口接进了原句：开头行之后的改口行必须正好是配上的最后 k 条（T0＝一条都没有；开头行是列表的照收）。
        * 对得上：全收；
        * T2（开头是按「内容＋分隔串」找到的，可靠）：改口的字不收、记 midturn_dropped:unmatched，插话照收（照 B2、B4、C，认不清那道闸照用）；
        * T0、T1 对不上：找到的很可能不是这一轮的开头行，中途的话一个字不收、记 unmatched，提问卡也只读到开头行（第三项真）。
        解析出任何错都不让这一轮丢：照没有中途的话存，记 midturn_dropped:internal_error 和内部错误。"""
        kind = pair[0] if pair else "t0"
        notes = ()
        if kind == "t2":
            positions, notes = [i for i in positions if marks[i][0] == "steer"], (AS.NOTE_MID_UNMATCHED,)
        elif not row_is_list:
            red = sorted(i for i, (k_, _row) in marks.items() if k_ == "redirect")
            k = pair[1] if kind == "t1" else 0
            if [i for i in red if i > idx] != (red[len(red) - k:] if k else []):
                return None, (AS.NOTE_MID_UNMATCHED,), True
        if not positions:
            return None, notes, False
        try:
            return HM.rows_items(marks, positions, self.platform in HW.LOCAL_PLATFORMS), notes, False
        except Exception as e:                                                               # noqa: BLE001
            IE.record(self._home, e)
            return None, notes + (AS.NOTE_MID_INTERNAL,), False

    def _note_conflict_token(self, sid, token):
        """同一个轮次号被两个同步抢：这一对换了本地新号记下，读取登记里备注一笔（只有固定原因）。写失败不影响。"""
        try:
            self.acc._log(HOST, sid, "", token, 0, "recorded_under_new_token", {"reason": "message_conflict"})
        except Exception:                                                                    # noqa: BLE001
            pass

    def _note_host_started(self, ent, sid):
        """宿主自己开的轮（开头行本身就是机器行）：在读取登记里记一行 not_recorded／host_started_turn（只有这个固定原因，没有原话），对账（sqlstore.turn_accounting）把它单列。写失败不影响。"""
        try:
            cur = self.acc._turn(HOST, sid, "", ent["token"])
            self.acc._log(HOST, sid, "", ent["token"], cur["gen"] if cur else 0, "not_recorded", {"reason": "host_started_turn"})
        except Exception:                                                                    # noqa: BLE001
            pass

    @staticmethod
    def _machinery_check(messages, user_text, image_index=None, user_origin=False):
        """user_origin：这一轮许不许放宽（sync_turn 先算好：宿主证明是用户开的、开头句不是宿主自己写的开头）。"""
        want = user_text.replace("\r\n", "\n").strip()
        idx = image_index
        for i in range(len(messages) - 1, -1, -1) if idx is None else ():
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user" and _flatten(m.get("content")).replace("\r\n", "\n").strip() == want:
                idx = i
                break
        if idx is None:
            return "current_user_row_not_found", None
        for i in range(idx, len(messages)):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user" and _machinery(m):
                if user_origin and i > idx and _only_todo_snapshot(m):
                    continue                                                                 # 许放宽：开头行后面的待办快照行不算机器行（别的机器行照旧整轮不记）
                return "machinery_row", idx
        return None, idx

    # ---------------------------------------------------------------- 提问卡里的回答（清单第 53 条，0.1.0a8）
    # 一对一的白名单：只列在 Hermes v0.23.2 源码里核过的（复核第 2 轮）。chat_type、chat_id 来自 initialize（gateway/session_execution.py 的
    # chat_type=src.chat_type、chat_id=src.chat_id → agent/agent_init_params.py 的 _GATEWAY_IDENTITY_PARAMS → agent/agent_init_memory_provider.py）。
    # weixin：plugins/platforms/weixin/adapter.py 的 _guess_chat_type 只给 dm／group；telegram：adapter.py 的 _normalize_chat_type 把 private 归成 dm；
    # discord：私信 dm、服务器帖子 thread；api_server：gateway/session_api.py 固定 dm。Slack 的多人私信（mpim）也标 dm（slack/adapter.py 的 is_dm、
    # shared_session），所以 Slack 另要 chat_id 以 D 开头（Hermes 自己也这样认 1:1 私信，slack/adapter.py 的 channel_id.startswith("D")）。
    PRIVATE_CHATS = {"weixin": ("dm",), "telegram": ("dm", "private"), "discord": ("dm",), "api_server": ("dm",)}

    def _private_chat(self):
        """一对一（或本机）才收提问卡里的回答。共用的会话里别的成员也能答主人的卡，Hermes 的结果里没有答题人，收进来就成了「主人的话」。
        本机平台都算；网关平台只认 PRIVATE_CHATS 里核过的组合，Slack 另要 chat_id 以 D 开头；别的平台（matrix、email……）和认不出的一律当共用。"""
        if self.platform in HW.LOCAL_PLATFORMS:
            return True
        ct = str(self._init_kw.get("chat_type") or "").strip().lower()
        if self.platform == "slack":
            return ct == "dm" and str(self._init_kw.get("chat_id") or "").startswith("D")
        return ct in self.PRIVATE_CHATS.get(self.platform, ())

    def _fold_clarify(self, messages, idx, opening, turn_text=None, mid=None, extra_notes=()):
        """这一轮提问卡里的回答、中途又说的话 → asked.Folded（提问卡的题和中途的话按行的先后排在一起，整轮只调一次 collect）。出任何错都不让这一轮丢：
        照 0.1.0a7 存，记 clarify_dropped:internal_error 和内部错误（只记类名、文件名和行号）。
        不是一对一（_private_chat）：什么都不接（照 0.1.0a7 存）、不认口令；这一轮有提问卡结果的，只记 clarify_dropped:shared_chat（有中途的话的轮次在 sync_turn 里早就整轮跳过了）。
        extra_notes：中途那部分在 sync_turn 里就定下的备注（配不上、解析出错）。"""
        cfg = getattr(self.mem, "cfg", None)
        limit = getattr(cfg, "long_limit", None) or AS.LONG_LIMIT
        try:
            f = HCL.fold_turn(messages, idx, opening, limit=limit, turn_text=turn_text, mid=mid)
        except Exception as e:                                                               # noqa: BLE001
            IE.record(self._home, e)
            return AS.Folded((), (HCL.NOTE_INTERNAL,) + tuple(extra_notes), 0, ())
        if not self._private_chat():
            return AS.Folded((), (HCL.NOTE_SHARED_CHAT,), 0, ()) if (f.lines or f.notes or f.all_texts) else AS.Folded((), (), 0, ())
        if extra_notes:
            f = f._replace(notes=f.notes + tuple(extra_notes))
        return f

    def _clarify_after_record(self, ent, folded):
        """用户那条已经记下（没被屏障挡住）以后：记计数；用户**打的**回答里认出「关」就关这个窗口（点选的不算；只走关的那半，来源 clarify_answer），关不成的排一句下一轮的提示。
        永不因为回答而打开，永不因为回答而确认删除（确认开启、确认删除只在 process_input 里认，这里不调它）。出错不影响这一轮的记录。"""
        notes = list(folded.notes) + [AS.NOTE_MID_KEPT] * folded.said                      # 中途的话存下一条记一次（清单第 55 条）
        try:
            ctl = self._close_from_answers(ent, folded.texts)                                # 只看用户打的字；点选的是助手写的字，不关
        except Exception:                                                                    # noqa: BLE001
            ctl = None
        if ctl:
            notes.append(ctl)
        try:
            if self._confirm_in_answers(ent, folded.all_texts):
                notes.append("clarify_control:confirm_not_counted")
        except Exception:                                                                    # noqa: BLE001
            pass
        if folded.said_texts:                                                                # 中途又说的话：每一段分别认，「关」照提问卡那条路关窗口（来源 midturn），别的口令不算数
            said = _with_lines(folded.said_texts)                                            # 连成一段的几次插话：每个物理行也各认一遍（只为「关」放宽，确认类的提示也按行看）
            try:
                ctl = self._close_from_answers(ent, said, source="midturn")
            except Exception:                                                                # noqa: BLE001
                ctl = None
            if ctl:
                notes.append(ctl)
            try:
                if self._confirm_in_answers(ent, said, source="midturn"):
                    notes.append("midturn_control:confirm_not_counted")
            except Exception:                                                                # noqa: BLE001
                pass
        if not notes and not folded.answers:
            return
        self.clarify["answers_kept"] += folded.answers
        self.clarify["turns_with_answers"] += 1 if folded.answers else 0
        for why in notes:
            self.clarify["notes"][why] = self.clarify["notes"].get(why, 0) + 1
        AC.record(self._home, folded.answers, notes)                                         # 不进 self.skipped：skipped 只表示「这一轮没记」

    # 下一轮才显示得出来（同步在回复显示之后），那时待确认已经作废了，所以用过去时（复核第 2 轮）
    CONFIRM_NOT_COUNTED = {"delete": "上一轮在提问卡里的「确认删除」不算数，那次没有删；要删请重新提出删除。",
                           "open": "上一轮在提问卡里的「确认开启」不算数，那次没有打开；要打开请重新申请，或在消息第一行写 #yuanwei-mem。"}

    CLOSE_FAILED_START = "上一轮你说的「关」没能确认已经生效；这个对话先暂停了读取。请再说一次，或在消息第一行写 #yuanwei-nomem。"
    CLOSE_FAILED_LATE = "上一轮在提问卡里打的「关」没能确认已经生效；这个对话先暂停了读取。请再说一次，或在消息第一行写 #yuanwei-nomem。"

    # 清单第 55 条：助手没答完时发的话里的口令。只有「关」算数（来源未核实的插话也允许关，只会更严）；确认删除、确认开启、#yuanwei-mem 都不算数，下一轮说一句。
    MIDTURN_NOT_COUNTED = {"delete": "上一轮你在助手没答完时发的「确认删除」不算数，那次没有删；要删请重新提出删除。",
                           "open": "上一轮你在助手没答完时发的「确认开启」不算数，那次没有打开；要打开请重新申请，或在消息第一行写 #yuanwei-mem。",
                           "mem": "上一轮你在助手没答完时发的 #yuanwei-mem 不算数，那次没有打开；要打开请在新消息的第一行写 #yuanwei-mem。"}
    CLOSE_FAILED_MIDTURN = "上一轮你在助手没答完时发的「关」没能确认已经生效；这个对话先暂停了读取。请再说一次，或在消息第一行写 #yuanwei-nomem。"
    CONTROL_NOTE = {"clarify_answer": "clarify_control", "midturn": "midturn_control"}

    def _confirm_in_answers(self, ent, texts, source="clarify_answer"):
        """有待确认的删除或开启、回答里又写了（或点了）「确认删除」「确认开启」：不改任何状态，只排一句固定的提示（下一条本人输入处理完以后
        给助手看、也直接显示给用户；那一条要是真的确认了，就不再说）。确认只在用户直接发的消息里认（process_input）；这里只读待确认表。返回有没有排提示。"""
        ref = ent.get("ref")
        if self.win is None or ref is None or not texts:
            return False
        mid = source == "midturn"
        say = self.MIDTURN_NOT_COUNTED if mid else self.CONFIRM_NOT_COUNTED
        want_delete = any(WN.is_delete_confirm(t) for t in texts)
        want_open = any(WN.is_confirm_line(t) for t in texts)
        want_mem = mid and any(k == "open" for t in texts for k, _s in WN.WindowService._plan(t)[1])        # #yuanwei-mem：中途发的开口令也不算数
        if not want_delete and not want_open and not want_mem:
            return False
        w = self.win.resolve(ref.host, ref.session_id, ref.branch or "")
        if w.paused:
            return False
        with self.win._lock:
            c = self.win.store.conn
            dp = c.execute("SELECT 1 FROM delete_pending WHERE window_id=? AND state='waiting'", (w.window_id,)).fetchone() is not None
            op = c.execute("SELECT 1 FROM window_pending WHERE window_id=? AND state='waiting'", (w.window_id,)).fetchone() is not None
        out = []
        if want_delete and dp:
            out.append(("delete", say["delete"]))
        if want_open and op:
            out.append(("open", say["open"]))
        if want_mem and not want_open and self.win.view(ref.host, ref.session_id, ref.branch or "").state == "off":
            out.append(("open", say["mem"]))                                                   # 窗口关着、用户中途发了 #yuanwei-mem：没有打开，说一句（开着就没什么可说的）
        self._late.extend(out)
        return bool(out)

    def _same_window_or_unknown(self, old_sid, old_branch, new_sid):
        """现在这个会话和提问卡所属的会话是不是同一个窗口；查不出（抛错、读状态出错）也当是——宁可多停一下读取。绑定不存在（身份待确认）算不是：那边本来就读不了。"""
        try:
            a = self.win.resolve(HOST, old_sid, old_branch, _raw=True)
            b = self.win.resolve(HOST, new_sid, "", _raw=True)
        except Exception:                                                                    # noqa: BLE001
            return True
        for r in (a, b):
            if r.window_id is None and r.pause_reason == "window_state_unreadable":
                return True
        return a.window_id is not None and a.window_id == b.window_id

    def _close_from_answers(self, ent, texts, source="clarify_answer"):
        ref = ent.get("ref")
        if self.win is None or ref is None or not texts:
            return None
        if not any(k == "close" for t in texts for k, _s in WN.WindowService._plan(t)[1]):
            return None
        key = (ref.host, ref.session_id, ref.branch or "")
        try:
            w = self.win.resolve(*key)
            if w.paused:
                return None                                                                  # 窗口身份待定、暂停读取：本来就读不了
            r = self.win.close(w.window_id, source=source, op_id="op-" + secrets.token_hex(8), input_win_seq=self.win.input_seq_of(ref))
        except Exception:                                                                    # noqa: BLE001  写失败：不能说已关，窗口进入「没确认已关」，读取被拦住
            self.win._suspect.add(key)
            if self.session != ref.session_id and self._same_window_or_unknown(ref.session_id, ref.branch or "", self.session):
                self.win._suspect.add((HOST, self.session, ""))                              # 一轮中间换了会话号（压缩）：标记也要在现在这个号上，下一轮才看得到、读取才停得住
            self._late.append(("close_failed", self.CLOSE_FAILED_MIDTURN if source == "midturn" else self.CLOSE_FAILED_LATE))   # 和直接发的「关」写失败一样要告诉用户；同步在回复显示之后，下一轮才出得去，用过去时
            return "%s:close_failed" % self.CONTROL_NOTE[source]
        if r.outcome == "applied":
            self._notices.append(WN.NOTICE_CLOSED)
            self._display.append("〔原委〕" + WN.NOTICE_CLOSED)                             # 和确认提示一样，也直接显示（下一轮回复后面）
            return "%s:closed" % self.CONTROL_NOTE[source]
        return None

    # ---------------------------------------------------------------- 会话边界
    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kw):
        if not self.active or not new_session_id:
            return
        reason = kw.get("reason") or ""
        with self._lock:
            prev = self.session
            self.session = new_session_id
            if reason in ("compression", "branch") and not reset and not rewound:      # 压缩、分叉：「没确认已关」的标记（含库打不开时先记在 _suspect_wait 里的）、开轮失败的标记跟到新会话（提前返回的路也要做到）
                parent = parent_session_id or prev
                if self._has_suspect((HOST, parent, "")):
                    self._mark_suspect((HOST, new_session_id, ""))
                if self.win is not None and reason == "compression" and self._turn_failed.get(parent):
                    self._turn_failed[new_session_id] = True
            moved = not rewound and not reset and reason not in ("compression", "branch")    # 恢复、切到别的会话（含没带 reason、没见过的 reason）
            trusted_continuation = (reason == "compression" and not reset and not rewound
                                    and self._owner_admitted and self.mem is not None)
            if self.platform not in HW.LOCAL_PLATFORMS and not trusted_continuation:
                # 只有已确认本人的同轮压缩沿用现有许可；其余换号等下一条本人消息。
                self._owner_admitted = False
                if moved:
                    self.ws = {"key": None, "kind": None, "label": None, "reason": "workspace_unknown_after_switch_%s" % (reason or "unknown")}
                    self._deferred_window_switch = None
                elif not rewound:
                    self._deferred_window_switch = (new_session_id, parent_session_id, prev, reset, reason)
                return
            if not self._owner_admitted:
                return
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
        记一笔，到下一轮开始再判（见 _recheck_workspace_after_resume）；local／tui／desktop 没目录：先问 Hermes 这一轮的文件夹（cwd_from_host_turn），取不到才是生活（no_cwd_life）。网关平台不走这里（照原来的规则：说不清）。"""
        cwd = kw.get("cwd")
        if cwd is None and self.platform in HW.PROCESS_CWD_PLATFORMS:
            self._cwd_recheck = True
            return
        self._adopt_workspace(self._resolve_ws(cwd, self.platform))                      # 没带 cwd 的非经典终端本机平台：同样问 Hermes 这一轮的文件夹，取不到才是生活

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
        self._bound_session = new

    def on_session_end(self, messages):
        if not self.active or not self._owner_admitted or self.mem is None or self.ws["key"] is None:
            return
        sid, mem = self.session, self.mem
        self._worker.submit(lambda: mem.end_session(HOST, sid))                              # 可能要等数据库、做维护：交给自己的后台线程，不占宿主线程

    def on_pre_compress(self, messages):
        return ""

    def _window_status(self):
        if self.win is None or not self.session or not self._owner_admitted:
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
            c = Config.load(cp) if cp else Config()
            p = c.provider or {}
            env = p.get("api_key_env")
            if not env:
                return {"env": None, "source": "not_needed"}
            out = {"env": env, "source": KS.source(env, p.get("key_file"))}
            if not c.organize:
                out["needed"] = False                                                                # 整理关着：不需要整理密钥（开着时这一项不出现，和以前一样）
            return out
        except Exception as e:                                                                   # noqa: BLE001
            return {"env": None, "source": "unknown", "error": type(e).__name__}

    def status(self):
        counter = OW.count_status(self._home) if self._home else {"counts": {}, "problem": None}
        return {"organizer_key": self._organizer_key(), "active": self.active, "inactive_reason": self.inactive_reason, "inactive_message": self._inactive_message(), "profile": dict(self.profile), "evidence": self.evidence, "owner_basis": self.owner_basis, "recorded_turns": self.recorded_turns,
                "skipped": dict(self.skipped), "clarify": {"answers_kept": self.clarify["answers_kept"], "turns_with_answers": self.clarify["turns_with_answers"], "notes": dict(self.clarify["notes"])}, "owner_skips": counter["counts"], "owner_counter_problem": counter["problem"], "owner_counter_errors": self.owner_counter_errors, "mode": self.cfg.get("mode", "tools_only"), "read_scope": self.scope, "worker_errors": list(self._worker.errors),
                "workspace": {"key": self.ws["key"], "kind": self.ws["kind"], "reason": self.ws["reason"]}, "host_cwd_dropped": self.host_cwd_dropped,
                "window": self._window_status(), "direct_display": self._display_status(),
                "compat": {"off": {k: v["reason"] for k, v in self._probe["features"].items() if not v["on"]}, "extra_abstract": list(self._probe["extra_abstract"])}, "prefetch_limit": dict(self.prefetch_limit())}
