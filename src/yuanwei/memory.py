"""Memory：核心的 Python 接口。一个 SQLite 文件、多个工作区；每个 Memory 对象绑定一个工作区。

记（record）→ 封步（会话结束／空闲到期／攒够条数或字数，在轮次边界封）→ 整理（run；用户配置的模型）→ 找（recall／get_messages／attachments／ask）→ 导出（export）。

谁来整理：没有常驻服务。只有这些入口会顺带检查「有没有该封的步、该整理的步」并按需拉起后台整理：record、recall、get_messages、attachments、run；
status、export、安装、卸载不拉起，也不会开始付费整理。拉起之前先核库里记着的暂停和预算状态。整理进程持锁，做完积压就退出；同一工作区按封步顺序一步一步做。
"""
import base64
import contextlib
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

from . import anchors as AN
from . import gate as GT
from . import note as NT
from . import contract as WC
from . import ops
from . import resources as RS
from . import sealing as SE
from . import step as STEP
from . import storeerr as WS
from . import redact as RD
from . import vectors as VX
from . import embedwork as EW
from . import keystore as KS
from .ask import RetryableNetworkError
from .cards import LightConfig
from .config import Config
from .errors import ConfigError, DeletionIncomplete, HistoryDeleted, Paused, ProviderError, Refused, YuanweiError
from .storeerr import BlockedByDeletion, DeletionInterrupt, StoreError
from .reader import Reader, purge_all
from .sqlstore import AlreadySealed, Store, now_iso

_BAN_TEXT = "这一轮的长期记忆读取已按禁读要求停止，不再新读；这一轮里已经交给你的内容、本次对话里说过的话仍在上下文里。"
_SETTING_TEXT = "长期记忆读取被持续设置关闭（工作区或会话设置），不会新读；本次对话里说过的话仍在上下文里。"
_UNKNOWN_TEXT = "无法确认权限，已暂停新增读取。"


def refusal_text(reason, persistent=False):
    """给人看的拒绝文字，按实际原因分三种（核心先只回 status＝refused 和 reason，文字由这里按原因给；不再无条件写「用户明确说了」「这次没有查」）。完整回执在第二段接头落地。"""
    if reason in ("clause_ban", "turn_marked", "host_marker"):
        return _BAN_TEXT + (PERSIST_HINT if persistent else "")
    if reason in ("workspace_setting", "session_setting"):
        return _SETTING_TEXT
    if reason == "permission_unknown":
        return _UNKNOWN_TEXT
    return "读取被拒绝（原因：%s）。" % (reason,)


PERSIST_HINT = "（你说的像是「以后都别用」：这句只管这一轮；要一直不用，请用持续开关——命令 yuanwei read-permission set --scope workspace --deny，或接头里的对应开关。）"
PAUSE_ABANDONS = 3
BACKOFF_SECONDS = (60, 300, 1800)           # 同一步第 1、2、3 次失败之后要等的秒数；之后每次 2 小时
BACKOFF_LATER_SECONDS = 7200
MAX_STEP_FAILURES = 5                       # 同一步连续失败这么多次 → 持久暂停「整理连续失败」
MAX_RETRY_BUSY = 5


def _default_step_namer(k, session_id):
    return "step-%04d" % k


class Memory:
    def __init__(self, db_path, workspace, config=None, provider=None, embed_client=None, vector_identity=None, clock=None, step_namer=None, config_dir=None, spawn="off", config_path=None, src_path=None):
        if not isinstance(workspace, str) or not workspace:
            raise ConfigError("工作区名必须是非空文字")
        self.cfg = (config if isinstance(config, Config) else Config.from_dict(config) if isinstance(config, dict) else Config.load(config) if config else Config()).validate()
        self.db_path = os.path.abspath(db_path)
        self.ns = workspace
        self.provider = provider
        self.src_path = src_path                         # 装接头时记下的 yuanwei 源码目录：后台子进程要用它找到 yuanwei（装包的 Python 和 Hermes 的 Python 可能不是同一个）
        self.vector_config_error = None
        self._embed_injected = embed_client is not None
        if embed_client is None and self.cfg.retrieval == "hybrid":
            try:
                embed_client = VX.build_client(self.cfg.embedding, config_path=config_path or (config if isinstance(config, str) else None))       # 配置里有向量服务（向导写的）才造；没有就是 None，和以前一样
            except ProviderError as e:
                self.vector_config_error = str(e)
        self.embed_client = embed_client
        self.clock = clock or now_iso
        self.step_namer = step_namer or _default_step_namer
        self.spawn_mode = spawn
        self.config_path = config_path or (config if isinstance(config, str) else None)
        self.config_dir = config_dir or (self.db_path + ".d")
        self.store = Store(self.db_path)
        ws = self.store.ensure_workspace(self.ns, self.cfg.index_version, "L", 1, {"write_prompt": self.cfg.write_prompt}, self.clock())
        self.index_version = ws["index_version"]
        ident = vector_identity or (self.cfg.embedding or {}).get("identity")
        self.vector_fp = None
        if ident is not None:
            self.vector_fp = VX.identity_fp(ident)
            self.store.put_identity(self.vector_fp, ident)
        self._reader = Reader(self.store, self.ns, self.cfg, self.index_version, self.vector_fp, embed_client, clock=self.clock)
        self.last_flush_errors = []
        self._deletion_blocked = None                       # 有没做完的删除、恢复不了：这个工作区读被拒、写排队（见 _resume_deletions）
        self._resume_deletions()
        try:
            self.flush_pending_detail()                      # 打开时补写；补写出任何错都不能挡住打开，错记在 status()["flush_errors"]，原话还在待补文件里
        except Exception as e:                               # noqa: BLE001
            self.last_flush_errors = [repr(e)]

    @classmethod
    def open(cls, db_path, workspace, **kw):
        return cls(db_path, workspace, **kw)

    def close(self):
        self.store.close()

    # ---- 删除：重启恢复、清缓存
    def _resume_deletions(self):
        """打开时先把没做完的删除做完，再开放读写（Codex R4）。拿不到删除锁（别的进程在做）就跳过；做不完：这个工作区读被拒（deletion_incomplete）、写走待补队列。"""
        try:
            from .deletion import DeletionService
            svc = DeletionService(self.db_path, config_dir=self.config_dir, clock=self.clock, store=self.store)
            todo = svc.incomplete(self.ns)
            if not todo:
                return
            svc.resume_for(self.ns)
            left = svc.incomplete(self.ns)
            self._deletion_blocked = left or None
        except Exception:                                    # noqa: BLE001  恢复出任何错：按没做完处理，不放开
            self._deletion_blocked = ["unknown"]

    def purge_caches(self):
        """删除以后清缓存：本进程里同一个库的所有 Reader 的快照。"""
        purge_all(self.db_path)
        self.last_flush_errors = []

    def _deletion_refusal(self, **extra):
        return dict({"status": "refused", "reason": "deletion_incomplete", "text": "这个工作区有没做完的删除（清理中），暂时不开放读取；稍后再试。", "persistent": False}, **extra)

    # ================================================================ 记
    def record(self, host, session_id, message_id, role, text, time=None, branch="", time_precision=None, stable_id=None, turn_id=None, capture=None):
        """记一条原话。角色只收 user／assistant。同一个键（宿主、会话、分支、宿主给的消息编号）重送、内容相同：返回原收据；内容不同：抛 MessageConflict。
        数据库忙：短暂重试，再不行写进本机待补文件（下次启动补进库），收据里 pending=True。返回收据字典。
        入库提交成功的原话，不等整理就能用关键词检索和按编号读到（收据 status 只表示处理到了哪个阶段，不兼管能不能读）；还在待补文件里的（pending=True、raw_saved=False）还没入库，不在这个保证里。
        turn_id：可信接头给出的「这条消息属于哪一轮」（宿主、会话、分支、轮次一起绑定）。绑定和原话在同一个事务里落下（待补文件里的跟着原话一起补进去），之后这一轮的读取把它排除，不当历史交出。
        不给 turn_id 就不绑定、不排除（离线接口）。不能让模型自己报，也不按文字相同、最近几条去猜。"""
        if role not in ("user", "assistant"):
            raise ValueError("角色只收 user 和 assistant，实际 %r" % (role,))
        if not isinstance(text, str) or not text:
            raise ValueError("正文必须是非空文字")
        for name, v in (("host", host), ("session_id", session_id), ("message_id", message_id)):
            if not isinstance(v, str) or not v:
                raise ValueError("%s 必须是非空文字" % name)
        if time is not None and (not isinstance(time, str) or not time):
            raise ValueError("time 要么不给（未知），要么是非空文字")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise ValueError("turn_id 要么不给，要么是非空文字")
        branch = branch or ""
        precision = time_precision or (None if time is None else "day" if len(time) == 10 else "second")
        red = RD.redact(text)                                # 密钥过滤：在指纹、切片、待补、整理请求之前；出错就不入库（关门不放过）
        text = red.text
        if capture is None:
            try:
                capture = self.store.take_capture()          # 事件进来的号：删除时用来划边界
            except sqlite3.OperationalError as e:            # 数据库忙取不到号：这条走待补，之后按收到的时间和删除登记的时间比（秒级）
                if not self._is_busy(e):
                    raise
        why = self.store.blocked(host, session_id, branch, message_id, turn_id, capture, self.clock() if capture is None else None)
        if why:
            return self._blocked_receipt(why)
        args = dict(host=host, session_id=session_id, message_id=message_id, role=role, text=text, time=time, branch=branch, time_precision=precision, stable_id=stable_id, turn_id=turn_id,
                    capture=capture, rv=RD.RULES_VERSION, redacted=red.count, kinds=list(red.kinds))
        maint_err = None
        flush_errors = []
        try:
            self.maintain()                                  # 新消息到来，先按旧的活动时间判旧步，再记新的；这一步出任何错都不能绕过原话落盘
        except Exception as e:                               # noqa: BLE001
            maint_err = repr(e)
        kind, rcpt = self._place(args, flush_errors)       # 直写还是排队，由同一套排他协议判（见 _place）
        if kind == "saved" and rcpt.get("blocked"):
            return rcpt
        if kind == "queued":
            out = {"raw_saved": False, "pending": True, "duplicate": False, "msg_id": None, "seq": None, "status": "待补", "readable": False, "redacted": red.count,
                   "readable_note": "还在本机待补文件里，没有入库，现在查不到；补进库以后才能读到"}
            if maint_err:
                out["maintenance_error"] = maint_err
            if flush_errors:
                out["flush_errors"] = flush_errors
            return out
        if flush_errors:
            rcpt["flush_errors"] = flush_errors
        self._after_write(rcpt, maint_err)
        return rcpt

    @staticmethod
    def _blocked_receipt(why):
        return {"raw_saved": False, "pending": False, "duplicate": False, "msg_id": None, "seq": None, "status": "被删除屏障挡住", "readable": False, "blocked": why, "redacted": 0}

    def _insert(self, a):
        if a.get("rv") != RD.RULES_VERSION:                  # 旧待补行（v11 之前写的，没有规则版本）或旧规则版本的行：补进库之前再过一遍过滤（幂等）
            r = RD.redact(a["text"])
            a = dict(a, text=r.text, redacted=max(a.get("redacted") or 0, r.count) if r.count else (a.get("redacted") or 0), kinds=list(r.kinds) or a.get("kinds") or [], rv=RD.RULES_VERSION)
        mid = a["stable_id"] or "m-" + WC.sha256_hex("|".join((a["host"], a["session_id"], a["branch"], a["message_id"])))[:12]
        is_long = len(a["text"]) > self.cfg.long_limit
        iv = self.index_version
        last = None
        for i in range(MAX_RETRY_BUSY):
            try:
                row, dup = self.store.add_message(self.ns, a["host"], a["session_id"], a["branch"], a["message_id"], a["role"], a["text"], a["time"], a["time_precision"],
                                                  a.get("received_at") or self.clock(), mid, lambda seq, msg_id: AN.anchors_for(msg_id, seq, a["text"], iv), is_long, a.get("turn_id"),
                                                  capture_seq=a.get("capture"), redacted=a.get("redacted") or 0, redact_ver=a.get("rv") or 0, kinds=a.get("kinds") or (),
                                                  equiv=lambda old, new: RD.redact(old).text == new)
                break
            except BlockedByDeletion as e:
                return self._blocked_receipt(e.reason)
            except sqlite3.OperationalError as e:
                last = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                time_sleep = 0.2 * (i + 1)
                time.sleep(time_sleep)
        else:
            raise last
        return {"raw_saved": True, "pending": False, "readable": True, "duplicate": dup, "msg_id": row["msg_id"], "seq": row["seq"], "status": self._msg_status(row), "is_long": bool(row["is_long"]),
                "fingerprint": row["fingerprint"], "redacted": row.get("redacted", 0)}

    def _place(self, args, flush_errors):
        """新原话的去处：直接入库，还是排在更早的待补后面。返回 ("saved", 收据) 或 ("queued", None)。
        协议：「前面有没有待补」的检查和直接入库，都在追加锁里做（领取、追加用的是同一把锁，所以检查到入库之间不会有人领取或追加；不会因为一次不加锁的扫描读到 0 而插队）。
        有积压：先放开锁，试着补一次（补写者锁归补写者，补写里的领取自己会拿追加锁），再回来在锁里重新检查；还有积压就排队。追加锁已经拿着的时候，追加用不加锁的 _append_pending（同一进程再拿一次同一把 flock 会自己等自己）。
        检查本身出任何错（读不了待补、锁文件打不开）：看不清就当有积压、排队，保序优先。代价：追加锁在直接入库期间一直占着，同时记录的几个进程会在锁上排着一个一个入库，没有积压时不会有人因此被排进待补。"""
        try:
            r = self._place_locked(args, flush_errors, may_queue=False)
            if r is None:
                try:
                    flush_errors.extend(self.flush_pending_detail().get("errors") or [])
                except Exception as e:                       # noqa: BLE001  补写出任何错都不能让新原话丢掉：错写进回执，新原话照常落盘
                    flush_errors.append(repr(e))
                    self.last_flush_errors = list(flush_errors)
                r = self._place_locked(args, flush_errors, may_queue=True)
            return r
        except OSError as e:                                 # 追加锁本身开不了：不加锁排队（宁可保序），连排队都不行才直接入库
            flush_errors.append(repr(e))
            try:
                self._append_pending(dict(args, received_at=self.clock()))
                return "queued", None
            except OSError as e2:
                flush_errors.append(repr(e2))
                return "saved", self._insert(args)

    def _place_locked(self, args, flush_errors, may_queue):
        with self._pending_lock():
            try:
                backlog = self._has_pending()
            except Exception as e:                           # noqa: BLE001  看不清就当有积压
                flush_errors.append(repr(e))
                backlog = True
            if backlog and not may_queue:
                return None
            if backlog:
                self._append_pending(dict(args, received_at=self.clock()))       # 待补里记下收到的时间，补进去以后空闲判断不会被补写的时刻刷新
                return "queued", None
            if self._deletion_blocked:                       # 有没做完的删除：写进待补队列，不入库
                self._append_pending(dict(args, received_at=self.clock()))
                return "queued", None
            try:
                return "saved", self._insert(args)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                self._append_pending(dict(args, received_at=self.clock()))
                return "queued", None

    def _msg_status(self, row):
        if not row["step_id"]:
            return "已保存"
        st = self.store.get_step(self.ns, row["step_id"])
        return {"sealed": "已封步", "failed": "已封步", "done": "已整理", "abandoned": "已放弃", "skipped": "已跳过"}.get(st["status"], "已封步")

    def message_status(self, message_id):
        """原话处理到了哪个阶段：已保存／已封步／已整理／已放弃（整理放弃）／已跳过（超长跳过）。只表示阶段，不兼管能不能读：已入库的原话在任何阶段都能按关键词和编号读到。"""
        row = self.store.message_row(self.ns, message_id)
        return None if row is None else self._msg_status(row)

    # ---- 待补文件。两把锁、各管各的：
    #   追加锁（短）：追加一条、领取（把当前文件改名成下一个编号的批次）。补写期间照样能追加。
    #   补写者锁（长）：覆盖领取、消费、清理的全过程，同一时刻只有一个补写者；拿不到就返回 busy，Memory.open 照常打开。用操作系统的文件锁，进程没了锁就没了，下一位补写者接上遗留批次。
    # 顺序：领取的批次带单调递增的序号（文件名里的数字，不是随机名）；补的时候按批次序号、批次内按原顺序；补不进去就停在那一条，这一条和后面的留在原批次原位置；
    # 新追加的永远在所有已领取批次之后。非 POSIX 没有进程间锁，只列为未验证，不据原子改名宣称有并发保证。
    def _pending_path(self):
        return os.path.join(self.config_dir, "pending-%s.jsonl" % WC.sha256_hex(self.ns)[:10])

    @contextlib.contextmanager
    def _file_lock(self, suffix, blocking):
        """进程间文件锁。yield True＝拿到；False＝没拿到（只有 blocking=False 才会）。非 POSIX：不加锁，一律 yield True。"""
        os.makedirs(self.config_dir, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            yield True
            return
        f = open(self._pending_path() + suffix, "a+")
        got = False
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                got = True
            except OSError:
                got = False
            try:
                yield got
            finally:
                if got:
                    fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

    def _pending_lock(self):
        return self._file_lock(".lock", True)

    def _flusher_lock(self):
        return self._file_lock(".flush.lock", False)

    def _append_pending(self, args):
        """追加一行（调用方已拿着追加锁）。文件结尾没有换行（上次追加被截断）：先补一个换行，新的一条才不会接在残缺行后面被一起当成坏行。"""
        p = self._pending_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        lead = b""
        try:
            if os.path.getsize(p) > 0:
                with open(p, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        lead = b"\n"
        except FileNotFoundError:
            pass
        with open(p, "ab") as f:
            f.write(lead + (json.dumps(args, ensure_ascii=False) + "\n").encode("utf-8"))

    def _write_pending(self, args):
        with self._pending_lock():
            self._append_pending(args)

    @staticmethod
    def _claim_tail(name):
        return name.rsplit(".claim-", 1)[-1]

    @classmethod
    def _is_legacy(cls, name):
        """0c6a58b 留下的随机名领取文件：.claim- 后面正好 8 位十六进制（哪怕恰好全是数字）。编号批次是 12 位数字。"""
        t = cls._claim_tail(name)
        return len(t) == 8 and all(c in "0123456789abcdef" for c in t)

    @classmethod
    def _batch_no(cls, name):
        t = cls._claim_tail(name)
        return int(t) if t.isdigit() and not cls._is_legacy(name) else None

    def _claim_names(self):
        d, base = os.path.dirname(self._pending_path()), os.path.basename(self._pending_path())
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return d, []
        return d, [n for n in names if n.startswith(base + ".claim-")]

    def _numbered(self):
        d, names = self._claim_names()
        got = [(self._batch_no(n), n) for n in names if self._batch_no(n) is not None]
        return [os.path.join(d, n) for _no, n in sorted(got)]

    def _legacy(self):
        d, names = self._claim_names()
        got = []
        for n in names:
            if self._is_legacy(n):
                try:
                    got.append((os.path.getmtime(os.path.join(d, n)), n))
                except FileNotFoundError:
                    continue
        return [os.path.join(d, n) for _t, n in sorted(got)]

    def _batches(self):
        """已领取的批次文件：先是 0c6a58b 留下的随机名文件（按修改时间），再是编号批次（按序号从小到大）。目录或文件正在变化也不报错。"""
        return self._legacy() + self._numbered()

    def pending_count(self):
        """待补的条数，按原始字节数行（不要求正文能解码）。给状态展示用的瞬时近似；保序的判断不用它（见 _place，在追加锁里用 _has_pending）。"""
        n = 0
        for p in self._batches() + [self._pending_path()]:
            try:
                with open(p, "rb") as f:
                    n += sum(1 for l in f.read().split(b"\n") if l.strip())
            except FileNotFoundError:                        # 正常的领取、清理期间文件会被改名或删掉
                continue
        return n

    def _has_pending(self):
        """（追加锁里调）有没有更早的待补。领取要拿同一把锁，所以锁里扫描不会碰上「已经改名、两边都没数到」的窗口。"""
        return self.pending_count() > 0

    def _claim_current(self):
        """（持补写者锁时调）把当前待补文件原子改名成下一个编号的批次；之后追加的写进新的当前文件，永远排在所有批次后面。"""
        with self._pending_lock():
            p = self._pending_path()
            if os.path.exists(p) and os.path.getsize(p) > 0:
                last = max([self._batch_no(os.path.basename(b)) for b in self._numbered()] or [0])
                os.replace(p, "%s.claim-%012d" % (p, last + 1))

    def _review_path(self):
        return self._pending_path() + ".review"

    def pending_review_count(self):
        """待核文件的行数（程序没能接收、需要核查的项：内容冲突、读不出的行、别的解析或校验错误；不再自动重试）。中断以后同一项可能记两行，所以这不是去重后的问题数。"""
        try:
            with open(self._review_path(), "rb") as f:          # 按原始字节数行：待核文件自己写到一半（半个汉字）也不报错
                return sum(1 for l in f.read().split(b"\n") if l.strip())
        except FileNotFoundError:
            return 0

    def _set_aside(self, raw, e):
        """把这一项原样挪进待核文件：原行、原因、挪出的时间。先写待核再从批次里去掉——万一中途死掉，宁可多一份也不丢。
        raw 是原始字节。能按 UTF-8 解开就存原文（line）；解不开就存 base64（line_b64），原始字节原样可还原，不用 errors="ignore" 或替换字符丢字节。"""
        rec = {"reason": repr(e), "at": self.clock()}
        try:
            rec["line"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            rec["line_b64"] = base64.b64encode(raw).decode("ascii")
        p = self._review_path()
        lead = b""
        try:
            if os.path.getsize(p) > 0:
                with open(p, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        lead = b"\n"                         # 上一条待核写到一半：先补换行，新的一条不接在残行后面，残行原样留着
        except FileNotFoundError:
            pass
        with open(p, "ab") as f:
            f.write(lead + (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _is_busy(e):
        return isinstance(e, sqlite3.OperationalError) and any(w in str(e).lower() for w in ("locked", "busy"))

    @staticmethod
    def _stops(e):
        """数据库本身的问题（忙、磁盘、库损坏等）：停在这一条，这一条和后面的原样留着。程序没能接收的项（冲突、读不出的行、校验错；IntegrityError、DataError 只是程序的分类，不说明原话本身有错）：挪进待核、接着补后面的。"""
        return isinstance(e, sqlite3.DatabaseError) and not isinstance(e, (sqlite3.IntegrityError, sqlite3.DataError))

    def flush_pending_detail(self):
        """把待补文件里的原话补进库（按原来的键，重送不会重复）。返回 {"status": "done"｜"stopped"｜"busy", "inserted": n, "left": 还没补进去的条数, "set_aside": 这次挪进待核的条数, "errors": [...]}（busy 只有前三项）。
        busy＝已有补写者在补，稍后再补。stopped＝数据库忙等，停在那一条（这一条和后面的都留在原位置）。
        程序没能接收、需要核查的项（内容冲突、读不出的行、校验错）：原样挪进待核文件（解不开的存 base64），保留原异常，接着补后面的。
        返回 done 只说明这一次领取的批次补完了，不等于待补已全部清零；之后排进来的要等下一次打开、记录或显式补写。"""
        with self._flusher_lock() as got:
            if not got:
                return {"status": "busy", "inserted": 0, "left": self.pending_count()}
            if self._deletion_blocked:                       # 有没做完的删除、这个进程里恢复不了：不补写（写走待补队列，等恢复）
                return {"status": "busy", "inserted": 0, "left": self.pending_count(), "deletion_incomplete": True}
            self._claim_current()
            done, aside, errors, dropped = 0, 0, [], 0
            for bp in self._batches():
                with open(bp, "rb") as f:
                    lines = [l for l in f.read().split(b"\n") if l.strip()]          # 按原始字节逐行；单行解不开只隔离这一行
                for k, line in enumerate(lines):
                    try:
                        a = json.loads(line.decode("utf-8"))
                        if not isinstance(a, dict):
                            raise TypeError("待补行不是一个对象")
                        r = self._insert(a)
                        if r.get("blocked"):                 # 被删除屏障挡住的迟到行：丢掉并计数，不进待核
                            dropped += 1
                        else:
                            done += 1
                    except Exception as e:                   # noqa: BLE001
                        if self._stops(e):
                            if not self._is_busy(e):
                                errors.append(repr(e))
                            tmp = bp + ".tmp"
                            with open(tmp, "wb") as f:
                                for b in lines[k:]:
                                    f.write(b + b"\n")
                            st = os.stat(bp)
                            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))           # 替换前把原批次的修改时间带过去，旧随机名批次靠它排先后
                            os.replace(tmp, bp)              # 这一条和后面的留在原批次、原位置
                            self.last_flush_errors = errors
                            return {"status": "stopped", "inserted": done, "left": self.pending_count(), "set_aside": aside, "errors": errors, "dropped_by_deletion": dropped}
                        self._set_aside(line, e)
                        aside += 1
                        errors.append(repr(e))
                os.remove(bp)
            self.last_flush_errors = errors
            return {"status": "done", "inserted": done, "left": self.pending_count(), "set_aside": aside, "errors": errors, "dropped_by_deletion": dropped}

    def flush_pending(self):
        """补写；返回补进去的条数（busy、中途补不进去时返回已补进去的条数）。"""
        return self.flush_pending_detail()["inserted"]

    # ================================================================ 封步
    def end_session(self, host, session_id, branch=""):
        """会话结束事件：这个会话里没封的话都封进步（最后一轮没有回复也按中止封，不补回复）。"""
        self._event(host, session_id, branch or "", "end")
        return self.maintain()

    def abort_turn(self, host, session_id, branch=""):
        """中止事件：没有最终回复的轮次按中止封，不补回复。"""
        self._event(host, session_id, branch or "", "abort")
        return self.maintain()

    def _event(self, host, session_id, branch, kind):
        ws = self.store.workspace(self.ns)
        self.store.add_event(self.ns, host, session_id, branch, kind, ws["last_seq"], self.clock())

    def _after_write(self, rcpt, maint_err=None):
        """原话已经安全保存以后：封该封的步，再按需拉起整理（record 是允许的入口；spawn="off" 仍然关闭）。这里任何失败都只记进回执，不推翻已经保存原话的回执。"""
        try:
            self.maintain()
        except Exception as e:                               # noqa: BLE001
            maint_err = maint_err or repr(e)
        if maint_err:
            rcpt["maintenance_error"] = maint_err
        try:
            sp = self.check_and_spawn()
            if sp.get("action") != "none":
                rcpt["spawn"] = sp["action"]
        except Exception as e:                               # noqa: BLE001
            rcpt["spawn_error"] = repr(e)
        try:
            vk = self.kick_vectors()
            if vk.get("action") in ("spawned", "ran"):
                rcpt["vec_spawn"] = vk["action"]
        except Exception as e:                               # noqa: BLE001  向量补算拉不起来不能影响记原话
            rcpt["vec_spawn_error"] = type(e).__name__

    def maintain(self, now=None):
        """检查并封该封的步（空闲到期、攒够条数或字数、会话结束）。空闲只在这里（下一次允许的事件或手动 run）判，没有常驻进程在第 30 分钟自动封。返回新封的步（字典列表）。
        别的连接抢先封了同一批消息：封步那笔整个回滚（AlreadySealed），这里重读后再算，不会建第二份步。"""
        now = SE.parse_ts(now) if isinstance(now, str) else (now or SE.parse_ts(self.clock()))
        out = []
        for _ in range(8):
            try:
                self._maintain_once(now, out)
                return out
            except AlreadySealed:
                continue
        raise StoreError("封步一直被别的封步者抢先，重读了 8 次仍没封成")

    def _maintain_once(self, now, out):
        msgs = self.store.unsealed_messages(self.ns)
        groups = {}
        for m in msgs:
            groups.setdefault((m["host"], m["session_id"], m["branch"]), []).append(m)
        for key, ms in groups.items():
            evs = self.store.events(self.ns, *key)
            end_at = max((e["at_seq"] for e in evs if e["kind"] == "end"), default=None)
            abort_at = max((e["at_seq"] for e in evs if e["kind"] == "abort"), default=None)
            turns = SE.build_turns(ms, end_at, abort_at)
            ended = end_at is not None and end_at >= ms[-1]["seq"]
            aborted = abort_at is not None and abort_at >= ms[-1]["seq"]
            last_act = max(SE.parse_ts(m["received_at"]) for m in ms)
            idle = (now - last_act) >= dt.timedelta(minutes=self.cfg.idle_minutes)
            complete = []
            for t in turns:
                if not t["complete"]:
                    break
                complete.append(t)
            steps, _rest = SE.pack_turns(complete, self.cfg.max_step_messages, self.cfg.max_step_chars, ended or aborted or idle)
            reason = "session_end" if ended else "abort" if aborted else "idle" if idle else "threshold"
            for turns_in_step in steps:
                step_msgs = [m for t in turns_in_step for m in t["msgs"]]
                org = [m["msg_id"] for m in step_msgs if not m["is_long"]]
                long_ids = [m["msg_id"] for m in step_msgs if m["is_long"]]
                text, sha = RS.write_prompt(self.cfg.write_prompt)
                sid_session = key[1]
                snap = self.cfg.write_snapshot()
                out.append(self.store.seal_step(self.ns, key[0], key[1], key[2], org, long_ids, lambda k, s=sid_session: self.step_namer(k, s), self.clock(), reason,
                                                self.cfg.write_prompt, sha, Config.snapshot_version(snap), json.dumps(snap, sort_keys=True)))

    # ================================================================ 整理
    def _lock_path(self):
        return self.db_path + ".run.lock"

    @contextlib.contextmanager
    def _run_lock(self):
        try:
            import fcntl
        except ImportError:                                  # 非 POSIX：退回独占创建锁文件
            fd = None
            try:
                fd = os.open(self._lock_path(), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                yield False
                return
            try:
                yield True
            finally:
                os.close(fd)
                os.remove(self._lock_path())
            return
        f = open(self._lock_path(), "a+")
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

    def lock_held(self):
        with self._run_lock() as got:
            return not got

    @staticmethod
    def backoff_seconds(failures):
        """同一步已经连续失败 failures 次之后，下一次重试前要等多久：1 分钟、5 分钟、30 分钟，之后每次 2 小时。"""
        if failures <= 0:
            return 0
        return BACKOFF_SECONDS[failures - 1] if failures <= len(BACKOFF_SECONDS) else BACKOFF_LATER_SECONDS

    def _fail_step(self, step, error):
        """这一步记失败，不放弃：失败次数加一、记时间和最后的错误；连续失败到上限 → 持久暂停「整理连续失败」。"""
        n = step["failures"] + 1
        self.store.update_step(self.ns, step["step_id"], status="failed", failures=n, last_error=error, last_failed_at=self.clock())
        self.store.archive_failed_attempts(self.ns, step["step_id"])          # 这类失败的发送不占重试的发送次数
        if n >= MAX_STEP_FAILURES:
            self._pause("step_failures")

    def retry_due(self, step):
        """失败的步：到了可以重试的时间没有。返回 (到了没有, 可以重试的时间 ISO 或 None)。"""
        if step["status"] != "failed" or not step["failures"] or not step.get("last_failed_at"):
            return True, None
        due = SE.parse_ts(step["last_failed_at"]) + dt.timedelta(seconds=self.backoff_seconds(step["failures"]))
        return SE.parse_ts(self.clock()) >= due, due.strftime("%Y-%m-%dT%H:%M:%SZ")

    def pause_state(self):
        ws = self.store.workspace(self.ns)
        if ws["paused"] == "daily_budget" and ws["budget_day"] != self.clock()[:10]:
            self.store.set_workspace(self.ns, paused=None, paused_at=None, budget_day=None)       # 每日软阈值到了新的一天自己解除；连续放弃的暂停不会
            ws = self.store.workspace(self.ns)
        if not ws["paused"]:
            return None
        out = {"reason": ws["paused"], "since": ws["paused_at"], "consecutive_abandons": ws["consec_abandons"]}
        if ws["paused"] == "step_failures":
            bad = [s for s in self.store.list_steps(self.ns, "failed") if s["failures"] >= MAX_STEP_FAILURES]
            if bad:
                s = bad[0]
                out["detail"] = {"label": "整理连续失败", "step_id": s["step_id"], "failures": s["failures"], "last_error": s["last_error"]}
        return out

    def resume(self):
        """明确的恢复命令：解除暂停（连续放弃的暂停只有它能解除），并把连续放弃计数清零。"""
        self.store.set_workspace(self.ns, paused=None, paused_at=None, budget_day=None, consec_abandons=0)
        self.store.reset_failed_steps(self.ns)                # 解除时清零失败步的失败计数和退避
        return True

    def _pause(self, reason):
        self.store.set_workspace(self.ns, paused=reason, paused_at=self.clock(), budget_day=self.clock()[:10] if reason == "daily_budget" else None)

    def pending_steps(self):
        return [s for s in self.store.list_steps(self.ns) if s["status"] in ("sealed", "failed")]

    # ---- 费用账：写不进库（数据库忙、被锁）不能丢账、也不能让已经拿到手的模型回应作废
    def _cost_pending_path(self):
        return self.db_path + ".cost-pending.jsonl"

    def _add_cost(self, ns, step_id, ts, day, model, fingerprint, usage, cny, unknown, note=None, currency="CNY"):
        row = dict(ns=ns, step_id=step_id, ts=ts, day=day, model=model, fingerprint=fingerprint, usage=usage, cny=cny, unknown=unknown, note=note, currency=currency)
        self._flush_cost_pending()
        try:
            self.store.add_cost(**row)
        except Exception as e:                                                              # noqa: BLE001  钱已经花了：先记进旁边的文件，下次再补进库
            try:
                with open(self._cost_pending_path(), "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            except OSError:
                pass
            sys.stderr.write("费用账写不进库（%s），已记进 %s，下次补\n" % (type(e).__name__, os.path.basename(self._cost_pending_path())))

    def _flush_cost_pending(self):
        p = self._cost_pending_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
            left = []
            for l in lines:
                try:
                    self.store.add_cost(**json.loads(l))
                except ValueError:
                    continue                                                                 # 坏行丢掉（不是我们写的格式）
                except Exception:                                                            # noqa: BLE001  还是写不进：留着
                    left.append(l)
            if left:
                with open(p, "w", encoding="utf-8") as f:
                    f.write("\n".join(left) + "\n")
            else:
                os.unlink(p)
        except OSError:
            pass

    def cost_pending_count(self):
        try:
            with open(self._cost_pending_path(), encoding="utf-8") as f:
                return sum(1 for l in f if l.strip())
        except OSError:
            return 0

    def _pricing(self):
        """这个整理模型的价格：{currency, in_miss, in_hit, out}；没有价格表就是 None（只记用量，不估价）。
        配置里没有指定模型（旧写法）按 DeepSeek 的人民币价格算，和以前一样；deepseek-flash 的单价仍然取自 Config.price_*。"""
        from . import catalog as CT
        params = (self.cfg.provider or {}).get("params") or {}
        model = params.get("model")
        legacy = {"currency": "CNY", "in_miss": self.cfg.price_in_miss_cny_per_m, "in_hit": self.cfg.price_in_hit_cny_per_m, "out": self.cfg.price_out_cny_per_m}
        if model is None or model == "deepseek-flash":
            return legacy
        pr = CT.price(model)
        return dict(pr) if pr else None

    def _cost_rows_for(self, step_id, res, unknown_bytes=None):
        day = self.clock()[:10]
        usage = res.get("usage") if isinstance(res, dict) else None
        model = res.get("model") if isinstance(res, dict) else None
        fp = res.get("fingerprint") if isinstance(res, dict) else None
        pr = self._pricing()
        if isinstance(res, dict) and usage:
            if pr is None:
                self._add_cost(self.ns, step_id, self.clock(), day, model, fp, usage, 0.0, False, "没有价格表：只记用量，不估价", currency="NONE")
                return
            out = usage.get("completion_tokens", 0)
            if pr["currency"] == "CNY":
                miss = usage.get("prompt_cache_miss_tokens")
                hit = usage.get("prompt_cache_hit_tokens")
            else:
                hit = usage.get("prompt_cache_hit_tokens")
                if hit is None:
                    hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                miss = usage.get("prompt_cache_miss_tokens")
            if miss is None:
                miss = usage.get("prompt_tokens", 0) - (hit or 0)
            amount = ((miss or 0) * pr["in_miss"] + (hit or 0) * pr["in_hit"] + out * pr["out"]) / 1e6
            self._add_cost(self.ns, step_id, self.clock(), day, model, fp, usage, amount, False, currency=pr["currency"])
        elif isinstance(res, dict):
            self._add_unknown_cost(step_id, model, fp, unknown_bytes or 200000, "回应里没有用量：按最坏估计记")

    def _add_unknown_cost(self, step_id, model, fp, nbytes, note):
        """不知道花了多少（回应没有用量、网络失败不知道是否计费）：有价格表就按最坏估计记（标 unknown）；没有价格表只记一行，金额 0，不估价。"""
        pr = self._pricing()
        if pr is None:
            self._add_cost(self.ns, step_id, self.clock(), self.clock()[:10], model, fp, None, 0.0, True, "这家没有价格表：不估价。" + note, currency="NONE")
        else:
            self._add_cost(self.ns, step_id, self.clock(), self.clock()[:10], model, fp, None, self._worst_send(pr, nbytes), True, note, currency=pr["currency"])

    def _worst_send(self, pr, nbytes):
        return (nbytes * pr["in_miss"] + self.cfg.worst_out_factor * 65536 * pr["out"]) / 1e6

    def worst_send_cny(self, nbytes):
        pr = self._pricing()
        if pr is None or pr["currency"] != "CNY":
            return None
        return self._worst_send(pr, nbytes)

    def worst_step_cny(self, step):
        """开始新的一步之前的最坏估计（人民币）：每步最多三发，每发按输入字节数（卡片上限＋这一步原话的粗估）和两倍最大输出。
        模型不是人民币计价、或没有价格表：返回 None（每日上限 daily_cap_cny 只管人民币行，对这家不起作用；不报一个假数，也不因此卡住）。"""
        chars = 0
        for mid in step["msg_ids"]:
            m = self.store.message_row(self.ns, mid)
            chars += len(m["content"])
        est_tokens = self.cfg.light.cards_tokens + 4000 + int(chars * 0.52)
        nbytes = int(est_tokens / 0.52 * 3)
        w = self.worst_send_cny(nbytes)
        return None if w is None else STEP.MAX_SENDS * w

    def _make_judge(self, step_id):
        provider = self.provider
        if provider is None:
            raise ConfigError("没有配置整理用的模型提供方（provider）")

        def judge(request):
            try:
                res = provider(request)
            except RetryableNetworkError:
                self._add_unknown_cost(step_id, None, None, len(request["text"].encode("utf-8")) + 1000, "网络失败：不知道是否计费，按最坏估计记")
                raise
            if isinstance(res, dict):
                self._cost_rows_for(step_id, res, len(request["text"].encode("utf-8")) + 1000)
                return res["text"]
            return res
        return judge

    def run(self, max_steps=None):
        """把积压的步整理掉（持锁；同一工作区按封步顺序一步一步做）。返回 {"status", "done", "abandoned", "skipped", "stopped_at", "reason"}。
        status：idle（没有活）／done（做完积压）／paused／busy（别人在整理）／stopped（出错停下）。"""
        out = {"status": "idle", "done": 0, "abandoned": 0, "skipped": 0, "stopped_at": None, "reason": None}
        with self._run_lock() as got:
            if not got:
                out["status"] = "busy"
                return out
            self.maintain()
            self._flush_cost_pending()
            n = 0
            for step in self.pending_steps():
                p = self.pause_state()
                if p:
                    out.update(status="paused", reason=p["reason"])
                    return out
                if max_steps is not None and n >= max_steps:
                    break
                due_ok, due_at = self.retry_due(step)
                if not due_ok:                                 # 退避中：不发请求，后面的步也不抢先
                    out.update(status="backoff", stopped_at=step["step_id"], reason="到 %s 才重试（已连续失败 %d 次）" % (due_at, step["failures"]), retry_at=due_at)
                    return out
                worst = self.worst_step_cny(step) if self.cfg.daily_cap_cny is not None else None
                if worst is not None:                                   # 没有价格表、或不是人民币计价的模型：上限不起作用，不核、不暂停
                    spent = self.store.cost_sum(self.ns, self.clock()[:10])["cny"]
                    if spent + worst > self.cfg.daily_cap_cny:
                        self._pause("daily_budget")
                        out.update(status="paused", reason="daily_budget")
                        return out
                r = self._run_one(step)
                n += 1
                if r == "done":
                    out["done"] += 1
                elif r == "abandoned":
                    out["abandoned"] += 1
                elif r == "skipped":
                    out["skipped"] += 1
                elif r == "deletion_interrupt":                    # 整理途中发生了删除：这一轮作废（不是失败），调用方下次再拉起
                    out.update(status="interrupted", stopped_at=step["step_id"], reason="deletion")
                    return out
                else:
                    out.update(status="stopped", stopped_at=step["step_id"], reason=r)
                    return out
                if self.pause_state():
                    p = self.pause_state()
                    out.update(status="paused", reason=p["reason"])
                    return out
            out["status"] = "done" if n else "idle"
        return out

    def _run_one(self, step):
        try:
            return self._run_one_guarded(step)
        except DeletionInterrupt:
            return "deletion_interrupt"

    def _run_one_guarded(self, step):
        ns, sid = self.ns, step["step_id"]
        gs = self.store.guarded(ns)                                  # 记下这一步开始时的删除版本；之后每次写都核，变了就作废
        if self.store.get_step(ns, sid) is None or any(self.store.get_message(ns, m) is None for m in step["msg_ids"]):
            raise DeletionInterrupt("这一步或它的原话已经被删")
        now = self.clock()
        if not step["msg_ids"]:
            r = ops.apply_skip(gs, ns, sid, step["session_id"], step["k"], step["long_msg_ids"], "这一步的消息都是超长条，只存全文和检索，不送去整理", self.clock)
            if r["status"] == "commit_failed":
                self._fail_step(step, r.get("error"))
                return "skip 提交失败：%s" % r.get("error")
            gs.end_step(ns, sid, "skipped", now, "all_long")
            return "skipped"
        messages = [self.store.get_message(ns, mid) for mid in step["msg_ids"]]
        prompt_version = step["prompt_version"] or self.cfg.write_prompt
        text, sha = RS.write_prompt(prompt_version)
        if step["prompt_sha256"] and step["prompt_sha256"] != sha:
            raise ConfigError("步 %s 登记的提示词指纹和资源不符" % sid)
        bound = self._step_config(step)
        if isinstance(bound, str):
            return bound                                           # 还原不了封步时的配置、而且当前配置和它不一致：发送前拒绝，不静默用当前配置
        light, allow_edit, strict = bound
        try:
            judge = self._make_judge(sid)
            res = STEP.run_step(gs, ns, sid, step["session_id"], step["k"], messages, judge, self.clock, config=light,
                                allow_summary_edit=allow_edit, prompt_text=text, strict_grouping=strict)
        except (WS.StepConflict, WS.StepOrderError, WS.StaleBase) as e:
            self._fail_step(step, repr(e))
            return "%s：%s" % (type(e).__name__, e)
        status = res["status"]
        if status in ("committed", "replayed"):
            gs.update_step(ns, sid, failures=0, last_error=None, last_failed_at=None)       # 成功一步，失败计数清零
            gs.end_step(ns, sid, "done", now)
            self.store.set_workspace(ns, consec_abandons=0)
            return "done"
        if status == "abandoned":
            gs.end_step(ns, sid, "abandoned", now, "abandoned")
            return "abandoned"
        if status in ("q1_failed", "q2_failed"):
            only_network = not res.get("reasons")
            if only_network:                                  # 发送次数用在网络类失败上：这是基础设施问题，不记放弃
                self._fail_step(step, res.get("error"))
                return "网络类失败用完了这一步的发送次数：%s" % (res.get("error"),)
            return self._abandon(step, res, gs)
        self._fail_step(step, res.get("error"))
        return "%s：%s" % (status, res.get("error"))

    def _step_config(self, step):
        """这一步该用的写入配置：封步时存的完整快照（执行和重试都按它）。快照和这一步登记的指纹对不上，或者没有快照、当前配置和登记的指纹不一致：
        返回一句拒绝的话（发送前拒绝，这一步状态不变、不记失败）；不把旧步的指纹改成当前值来「修齐」。"""
        snap_text, want = step.get("config_snapshot"), step.get("config_version")
        if snap_text:
            try:
                snap = json.loads(snap_text)
                light = LightConfig(**snap["light"])
                got = Config.snapshot_version(snap)
                ok = got == want
                allow_edit, strict = snap["allow_summary_edit"], snap["strict_grouping"]
            except Exception as e:                           # noqa: BLE001
                return "步 %s 封步时存的配置快照读不出来（%r），拒绝发送" % (step["step_id"], e)
            if not ok:
                return "步 %s 的配置快照和登记的配置指纹对不上（快照 %s，登记 %s），拒绝发送" % (step["step_id"], got, want)
            return light, allow_edit, strict
        if want and want != self.cfg.config_version():
            return "步 %s 封步时的配置（%s）没有存快照、还原不了，当前配置（%s）和它不一致，拒绝发送；请用封步时的配置打开" % (step["step_id"], want, self.cfg.config_version())
        return self.cfg.light, self.cfg.allow_summary_edit, self.cfg.strict_grouping

    def _abandon(self, step, res, gs=None):
        gs = gs or self.store
        ns, sid = self.ns, step["step_id"]
        binding = self.store.get_step_binding(ns, sid)
        reason = "整理的输出两次都不合格（%s），按规矩放弃这一步：" % res["status"] + "；".join(str(x) for x in res.get("reasons", [])[:3])
        op = {"op": "abandon_step", "op_id": "abandon-" + sid, "operator": "auto", "step_id": sid, "session_id": step["session_id"], "k": step["k"], "reason": reason,
              "msg_fp_digest": binding["msg_fp_digest"]}
        r = STEP.apply_operation(gs, ns, op, self.clock)
        if r["status"] not in ("committed", "replayed"):
            self._fail_step(step, "放弃没能提交：%r" % (r,))
            return "放弃没能提交"
        gs.end_step(ns, sid, "abandoned", self.clock(), "abandoned")
        ws = self.store.workspace(ns)
        n = ws["consec_abandons"] + 1
        self.store.set_workspace(ns, consec_abandons=n)
        if n >= PAUSE_ABANDONS:
            self._pause("consecutive_abandons")
        return "abandoned"

    # ================================================================ 谁来整理（允许的入口顺带调用）
    def check_and_spawn(self):
        """检查有没有该封的步、该整理的步；有、没有暂停、没有人在整理，就拉起整理。只由允许的入口调用（status／导出／安装／卸载不调）。返回 {"action","reason"}。"""
        self.maintain()
        if not self.pending_steps():
            return {"action": "none", "reason": "没有积压"}
        p = self.pause_state()
        if p:
            return {"action": "none", "reason": "paused:" + p["reason"]}
        first = self.pending_steps()[0]
        due_ok, due_at = self.retry_due(first)
        if not due_ok:
            return {"action": "none", "reason": "backoff:" + due_at}
        cap = self.cfg.daily_cap_cny
        if cap is not None and self.store.cost_sum(self.ns, self.clock()[:10])["cny"] >= cap:      # 拉起之前也核预算：当天已记账达到线就暂停，不拉起
            self._pause("daily_budget")
            return {"action": "none", "reason": "paused:daily_budget"}
        if self.lock_held():
            return {"action": "none", "reason": "已有进程在整理"}
        if self.spawn_mode == "inline":
            return {"action": "ran", "result": self.run()}
        if self.spawn_mode == "subprocess":
            self._spawn(["--db", self.db_path, "--workspace", self.ns] + (["--config", self.config_path] if self.config_path else []) + ["run"], "run")
            return {"action": "spawned", "reason": "后台整理已拉起"}
        return {"action": "none", "reason": "spawn 关着"}

    # ---- 后台子进程：用 sys.executable -m yuanwei；带上 PYTHONPATH（装包的 Python 和 Hermes 的 Python 可能不是同一个）；输出不丢，写进 <库>.spawn.log，status 里能看到
    def _spawn_log_path(self):
        return self.db_path + ".spawn.log"

    def _spawn_env(self):
        env = dict(os.environ)
        if self.src_path:
            env["PYTHONPATH"] = self.src_path + ((os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        return env

    def _spawn(self, args, tag):
        lp = self._spawn_log_path()
        try:
            if os.path.getsize(lp) > 200000:                         # 日志只留最后 50 KB
                with open(lp, "rb") as f:
                    f.seek(-50000, os.SEEK_END)
                    tail = f.read()
                with open(lp, "wb") as f:
                    f.write(tail)
        except OSError:
            pass
        with open(lp, "ab") as lf:
            lf.write(("\n--- %s %s\n" % (self.clock(), tag)).encode("utf-8"))
            lf.flush()
            return subprocess.Popen([sys.executable, "-m", "yuanwei"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=lf, start_new_session=True, env=self._spawn_env())

    def spawn_log_tail(self, lines=5, width=300):
        """后台子进程最近的输出（过一遍密钥过滤，每行截短）；没有就是空表。"""
        try:
            with open(self._spawn_log_path(), "rb") as f:
                f.seek(0, os.SEEK_END)
                n = f.tell()
                f.seek(max(0, n - 20000))
                txt = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        out = [RD.redact(l).text[:width] for l in txt.splitlines() if l.strip() and not l.startswith("--- ")]
        return out[-lines:]

    # ---- 向量补算
    def vectors_enabled(self):
        emb = self.cfg.embedding or {}
        if emb.get("managed"):                               # 自带引擎：没有固定的地址，端口每次变；要有引擎目录和编码身份
            return bool(self.cfg.retrieval == "hybrid" and self.vector_fp and emb.get("engine_dir") and emb.get("identity"))
        return bool(self.cfg.retrieval == "hybrid" and self.vector_fp and emb.get("base_url") and emb.get("model") and emb.get("identity"))

    def _worker_client(self):
        if self._embed_injected:
            return self.embed_client
        from . import enginerun as ER
        return ER.resolve_client(self.cfg.embedding, "worker", config_path=self.config_path)

    def run_vectors(self, client=None, **kw):
        """补算一轮（同步，持 <库>.vec.lock）。后台子进程 yuanwei vectors backfill 调的就是它。"""
        if not self.vectors_enabled():
            return {"status": "disabled"}
        return EW.run_backfill(self.store, self.db_path, self.cfg.embedding, self.clock, client or self._worker_client(),
                               max_seconds=(self.cfg.embedding or {}).get("worker_max_run_seconds", 600), **kw)

    def warm_engine(self):
        """暖机（插件启动时调一次）：自带引擎没开就拉起、开着就算数；不等、不抛错。空闲退出以后不会因为插件还活着就再拉起来：只在这里和真用到的时候才拉。"""
        emb = self.cfg.embedding or {}
        if not emb.get("managed") or not self.vectors_enabled():
            return {"action": "none"}
        from . import enginerun as ER
        try:
            c = ER.resolve_client(emb, "query", config_path=self.config_path)
            ER.ensure(c.paths, c.expect(), c.host, c.spawner, 0.0)
            return {"action": "ready"}
        except ER.EngineDown as e:
            return {"action": "starting", "reason": e.reason}
        except Exception as e:                                                     # noqa: BLE001  暖机失败不影响任何事
            return {"action": "error", "reason": type(e).__name__}

    def kick_vectors(self):
        """只做便宜的判断，再非阻塞地拉起补算：没配向量、没有欠账（库的签名和上次补完时一样）、在退避里、有人在补，都不拉起。不抛错的路径：调用方只把原因记进回执。"""
        if not self.vectors_enabled():
            return {"action": "none", "reason": "没有配置向量"}
        if self.store.meta_get("vec_done_sig") == EW.signature(self.store, self.vector_fp):
            return {"action": "none", "reason": "没有欠账"}
        waiting, b = EW.in_backoff(self.store, self.clock())
        if waiting:
            return {"action": "none", "reason": "backoff:" + b["next_try_at"]}
        if EW.lock_held(self.db_path):
            return {"action": "none", "reason": "已有进程在补算"}
        if self.spawn_mode == "inline":
            return {"action": "ran", "result": self.run_vectors()}
        if self.spawn_mode == "subprocess":
            self._spawn(["vectors", "backfill", "--db", self.db_path] + (["--config", self.config_path] if self.config_path else []), "vectors")
            return {"action": "spawned", "reason": "后台补算向量已拉起"}
        return {"action": "none", "reason": "spawn 关着"}

    # ================================================================ 读取
    def cut(self):
        """新格式的当下截点：{cut_format, ns, raw_seq, org_seq, index_version}（原话上界和整理上界从同一次查询里取）。"""
        return self._reader.current_cut()

    def excluded_ids(self, host, session_id, branch, turn_id):
        """这个工作区里，宿主、会话、分支、轮次一起对上的那一轮绑定的消息编号集合（本轮排除用）。五样缺一不对就是空集合。"""
        return self.store.turn_excluded(self.ns, host, session_id, branch or "", turn_id)

    def _read_view(self, host, session_id, branch, turn_id):
        """本轮排除的读取视图。只有宿主、会话、轮次都明确给了（可信接头给的）才排除；没给就是没有轮次上下文，不做本轮排除。"""
        if not (host and session_id and turn_id):
            return self._reader
        ex = self.excluded_ids(host, session_id, branch, turn_id)
        return self._reader.scoped(ex) if ex else self._reader

    # ================================================================ 读取权限：一处统一检查，所有模型能访问的读取入口都先过它
    # 在搜索、向量调用、拉起后台整理之前；没过就直接拒，不进检索。来源（任一说不许就不许；拿不到权限上下文时不当成允许）：
    #   1 调用方给的可信状态：history_allowed=False、轮次标记（mark_turn_refused，或 note 认出禁读时记下的）
    #   2 产品自己的持续设置：工作区一级、会话一级，存库（set_read_permission／clear_read_permission／read_permission）
    #   3 文字识别（只有 note 收到用户这一句话，识别始终执行，和 note_free_gate 无关）
    # 工具调用省略 turn_id／session_id 时，用最近一次 note／begin_turn 声明的轮次和会话（current_turn）；传 history_allowed=True 不能解除任何已经成立的禁读。
    def begin_turn(self, turn_id, session_id=None):
        """接头在每条用户消息开始时声明这一轮（note 会自动做）。之后省略 turn_id 的读取调用按这一轮算。"""
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id 必须是非空文字")
        self.store.set_current_turn(self.ns, turn_id, session_id, self.clock())

    def end_turn(self, turn_id=None):
        self.store.clear_current_turn(self.ns, turn_id)

    def mark_turn_refused(self, turn_id, session_id=None):
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id 必须是非空文字")
        self.store.mark_turn(self.ns, turn_id, True, self.clock())
        self.store.set_current_turn(self.ns, turn_id, session_id, self.clock())

    def turn_refused(self, turn_id):
        return bool(turn_id) and self.store.turn_refused(self.ns, turn_id)

    def set_read_permission(self, allowed, scope="workspace", session_id=None):
        """持续设置。scope＝workspace（整个工作区）或 session（要给 session_id）。allowed=False 就是持续禁读；True 只是写明允许，不能覆盖别处的禁止。"""
        key = self._setting_key(scope, session_id)
        self.store.set_read_setting(self.ns, scope, key, bool(allowed), self.clock())
        return self.read_permission(session_id if scope == "session" else None)

    def clear_read_permission(self, scope="workspace", session_id=None):
        key = self._setting_key(scope, session_id)
        n = self.store.clear_read_setting(self.ns, scope, key)
        return {"cleared": n, "now": self.read_permission(session_id if scope == "session" else None)}

    @staticmethod
    def _setting_key(scope, session_id):
        if scope == "workspace":
            return ""
        if scope == "session":
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("scope=session 时要给 session_id")
            return session_id
        raise ValueError("scope 只能是 workspace 或 session")

    def read_permission(self, session_id=None):
        """现在的持续设置和有效结果（给界面、命令行、status 看）。"""
        rows = self.store.read_settings(self.ns)
        deny = [r for r in rows if not r["allowed"] and (r["scope"] == "workspace" or (r["scope"] == "session" and r["key"] == session_id))]
        return {"allowed": not deny, "settings": rows, "denied_by": [{"scope": r["scope"], "key": r["key"]} for r in deny]}

    def _permission(self, turn_id=None, session_id=None, history_allowed=True, text=None):
        """统一的读取权限检查，返回 {"allowed": bool, "reason": …, "persistent": bool}。拿不到权限上下文（读库出错）：不当成允许。"""
        try:
            if history_allowed is False:
                if turn_id:
                    self.mark_turn_refused(turn_id, session_id)
                return {"allowed": False, "reason": "host_marker", "persistent": False}
            cur = self.store.current_turn(self.ns)
            eff_turn = turn_id or (cur["turn_id"] if cur else None)
            eff_session = session_id or (cur["session_id"] if cur and (not turn_id or turn_id == cur["turn_id"]) else None)
            if eff_turn and self.store.turn_refused(self.ns, eff_turn):
                return {"allowed": False, "reason": "turn_marked", "persistent": False}
            p = self.read_permission(eff_session)
            if not p["allowed"]:
                return {"allowed": False, "reason": "%s_setting" % p["denied_by"][0]["scope"], "persistent": False}
            if text is not None:
                tp = GT.text_permission(text)
                if not tp["allowed"]:
                    if eff_turn:
                        self.mark_turn_refused(eff_turn, eff_session)
                    return {"allowed": False, "reason": tp["rule"], "persistent": tp["persistent"]}
            return {"allowed": True, "reason": None, "persistent": False}
        except ValueError:
            raise
        except Exception:                                                            # noqa: BLE001  拿不到权限上下文：拒，不猜成允许
            return {"allowed": False, "reason": "permission_unknown", "persistent": False}

    @staticmethod
    def _refused(reason, persistent=False, **extra):
        return dict({"status": "refused", "reason": reason, "text": refusal_text(reason, persistent), "persistent": persistent}, **extra)

    # 下面的 recall／get_messages／attachments／note 都可以另给 host、branch：和 session_id、turn_id 一起对上这个工作区里绑定过的那一轮，就排除这一轮的消息（只排当前这一轮；
    # 同一会话里更早的原话照常可读）。host、session_id、turn_id 缺任何一个，就当没有轮次上下文：不做本轮排除。
    def _check_ver(self, dv0):
        """返回前再核一次：读取开始以后这个工作区发生过删除，就不交旧结果。"""
        if self.store.del_ver(self.ns) != dv0:
            raise HistoryDeleted("历史已删除，请重新查询")

    def recall(self, query=None, budget=None, cursor=None, history_allowed=True, cut=None, limit_chars=None, turn_id=None, session_id=None, host=None, branch=""):
        perm = self._permission(turn_id, session_id, history_allowed)
        if not perm["allowed"]:
            return self._refused(perm["reason"], delivered=[], cursor=None, has_more=False)
        if self._deletion_blocked:
            return self._deletion_refusal(delivered=[], cursor=None, has_more=False)
        if isinstance(query, str):
            query = RD.redact_query(query)
        dv0 = self.store.del_ver(self.ns)
        self.check_and_spawn()
        r = self._read_view(host, session_id, branch, turn_id).recall(query, budget=budget, cursor=cursor, cut=cut, limit_chars=limit_chars)
        r["backlog"] = self.backlog()
        self._check_ver(dv0)
        return r

    def get_messages(self, ids=None, before=0, after=0, cursor=None, cut=None, encode=True, history_allowed=True, turn_id=None, session_id=None, host=None, branch=""):
        perm = self._permission(turn_id, session_id, history_allowed)
        if not perm["allowed"]:                                                      # 旧的续读游标也在这里被挡，不进游标查找
            return self._refused(perm["reason"], messages=[], cursor=None, truncated=False)
        if self._deletion_blocked:
            return self._deletion_refusal(messages=[], cursor=None, truncated=False)
        dv0 = self.store.del_ver(self.ns)
        self.check_and_spawn()
        r = self._read_view(host, session_id, branch, turn_id).get_messages(ids, before, after, cursor, cut, encode)
        self._check_ver(dv0)
        return r

    def attachments(self, message_id, cut=None, history_allowed=True, turn_id=None, session_id=None, host=None, branch=""):
        perm = self._permission(turn_id, session_id, history_allowed)
        if not perm["allowed"]:
            return self._refused(perm["reason"], sentences=[])
        if self._deletion_blocked:
            return self._deletion_refusal(sentences=[])
        dv0 = self.store.del_ver(self.ns)
        self.check_and_spawn()
        r = self._read_view(host, session_id, branch, turn_id).attachments(message_id, cut)
        self._check_ver(dv0)
        return r

    def note(self, query, host_command=False, turn_id=None, history_allowed=True, cut=None, session_id=None, host=None, branch=""):
        """自动档的便条：用户每说一句调一次。顺序：读取权限（调用方标记、轮次标记、持续设置、文字识别禁读；始终执行）→ 免查门（只管整句寒暄应答和显式标明的宿主命令；
        note_free_gate 关着就不免查）→ 拉起后台整理 → 检索、装料 → 最相关的过了相关度门才交一张便条（两个分数门默认关＝预览候选）。
        返回 {"status","note"（便条全文，没有就是空）,"gates","delivered","unshown","listed","later","cursor","hint","chars","est_tokens",…}；
        status：ok／refused／skipped_greeting／skipped_command／no_candidates／below_threshold。出错（缺向量、绑定不符）抛错，不冒充「没有便条」。"""
        c = self.cfg
        if not isinstance(query, str) or not query.strip():
            raise ValueError("便条的查询必须是非空文字")
        turn_id = turn_id or "auto-" + uuid.uuid4().hex[:12]                       # 每次 note 就是新的一轮；没给轮次编号就自己起一个，之后省略 turn_id 的读取调用按这一轮算
        self.begin_turn(turn_id, session_id)
        perm = self._permission(turn_id, session_id, history_allowed, text=query)
        if not perm["allowed"]:
            r = self._refused(perm["reason"], perm["persistent"])
            return {"status": "refused", "note": r["text"], "reason": perm["reason"], "persistent": perm["persistent"], "gates": {"permission": {"allowed": False, "reason": perm["reason"]}},
                    "delivered": [], "unshown": [], "cursor": None}
        if self._deletion_blocked:
            return {"status": "refused", "note": "", "reason": "deletion_incomplete", "persistent": False, "gates": {"permission": {"allowed": False, "reason": "deletion_incomplete"}}, "delivered": [], "unshown": [], "cursor": None}
        free = GT.free_check(query, host_command=host_command) if c.note_free_gate else {"decision": "check", "rule": None}
        if free["decision"] != "check":
            return {"status": "skipped_greeting" if free["decision"] == "skip_greeting" else "skipped_command", "note": "", "gates": {"permission": {"allowed": True}, "free": free},
                    "delivered": [], "unshown": [], "cursor": None}
        dv0 = self.store.del_ver(self.ns)
        self.check_and_spawn()
        an = NT.analyze(self._read_view(host, session_id, branch, turn_id), query, c, cut=cut)
        r = NT.finalize(an, c, c.note_tau_kw, c.note_tau_vec, persist=NT.make_persist(self._reader, an), max_listed=c.note_max_listed)
        self._check_ver(dv0)
        r["gates"]["free"] = free
        r["gates"]["permission"] = {"allowed": True}
        r["backlog"] = self.backlog()
        return r

    def answer_request(self, question, asked_at, as_of=None, tz_label=None):
        """取材（参照档）并拼好回答请求，不发。返回 {"request","selection","material"}。asked_at：提问日 YYYY-MM-DD；时区显式给（tz_label 不给就按配置的 answer_timezone）。
        离线接口，没有轮次上下文：不做本轮排除。"""
        from . import answer as AW
        self.check_and_spawn()
        question = RD.redact_query(question)
        sel = self._reader.select(question, cut=as_of)
        tz = tz_label or {"Asia/Shanghai": "北京时间", "Asia/Bangkok": "泰国时间"}.get(self.cfg.answer_timezone, self.cfg.answer_timezone)
        material = sel["material"] or "（没有找到相关记录）"
        return {"request": AW.build_request(material, asked_at, question, tz), "selection": sel, "material": material}

    def ask(self, question, asked_at, as_of=None, tz_label=None):
        """根据记忆提问：取材 → 回答请求（R1 回答提示＋闭环加句）→ 提供方 → 严格读 JSON。回答器由 provider 注入；一次发送，格式不合格就把原因照实返回（不悄悄重试）。"""
        from . import answer as AW
        if self.provider is None:
            raise ConfigError("没有配置模型提供方（provider）")
        ar = self.answer_request(question, asked_at, as_of, tz_label)
        req, sel = ar["request"], ar["selection"]
        res = self.provider({"text": req["user"], "body_bytes": req["bytes"], "kind": "answer"})
        raw = res["text"] if isinstance(res, dict) else res
        parsed = AW.parse_answer(raw)
        return {"ok": parsed["ok"], "answer": parsed["answer"], "code": parsed["code"], "problems": parsed["problems"], "raw": raw,
                "delivered": [r["msg_id"] for r in sel["selected"]], "cut": sel["cut"],
                "config": {"retrieval": self.cfg.retrieval, "first_budget": self.cfg.first_budget, "total_budget": self.cfg.total_budget, "pool_cap": self.cfg.pool_cap},
                "request_sha16": req["material_sha16"]}

    # ================================================================ 状态、导出
    def backlog(self):
        steps = self.store.list_steps(self.ns)
        pend = [s for s in steps if s["status"] in ("sealed", "failed")]
        unsealed = self.store.unsealed_messages(self.ns)
        oldest = None
        unfinished = self.store.conn.execute("SELECT MIN(received_at) FROM messages WHERE ns=? AND visible_at IS NULL", (self.ns,)).fetchone()[0]       # 所属的步还没结束的最早一条（原话入库就能读，这只说整理还欠着）
        if unfinished:
            oldest = unfinished
        return {"pending_steps": len(pend), "unsealed_messages": len(unsealed), "oldest_unfinished_received_at": oldest}

    def status(self):
        """不拉起整理、不改任何东西。cut 说的是这一刻的截点（原话上界、整理上界）；backlog、failed_steps、paused、lock_held 这些队列、失败次数、运行中、暂停是读取这一刻的当前运营状态，不是某个旧截点的状态。"""
        steps = self.store.list_steps(self.ns)
        by = {}
        for s in steps:
            by[s["status"]] = by.get(s["status"], 0) + 1
        ws = self.store.workspace(self.ns)
        cost = self.store.cost_sum(self.ns)
        return {"workspace": self.ns, "cut": self.cut(), "backlog": self.backlog(), "pending_writes": self.pending_count(), "pending_review": self.pending_review_count(), "flush_errors": list(getattr(self, "last_flush_errors", [])), "failed_steps": by.get("failed", 0),
                "abandoned_steps": by.get("abandoned", 0), "skipped_steps": by.get("skipped", 0), "done_steps": by.get("done", 0),
                "long_unorganized": self.store.count_messages(self.ns, "is_long=1"), "paused": self.pause_state(),
                "retrieval": {"mode": self.cfg.retrieval, "index_version": self.index_version, "vector_identity": self.vector_fp,
                              "vectors": self.store.vector_count(self.vector_fp) if self.vector_fp else 0, "first_budget": self.cfg.first_budget,
                              "total_budget": self.cfg.total_budget, "pool_cap": self.cfg.pool_cap, "neighbors": self.cfg.neighbors},
                "write": {"prompt": self.cfg.write_prompt, "config_version": self.cfg.config_version(), "store_kind": ws["store_kind"], "store_version": ws["store_version"]},
                "cost": cost, "cost_today": self.cost_today(), "read_permission": self.read_permission(), "lock_held": self.lock_held(),
                "redaction": self._redaction_status(ws), "deletion": self._deletion_status(),
                "organizer": self._organizer_status(), "vectors": self._vectors_status(), "spawn_log_tail": self.spawn_log_tail(),
                "operational": {"scope": "current", "note": "backlog、pending_*、failed_steps、abandoned_steps、skipped_steps、done_steps、paused、lock_held 是读取这一刻的当前运营状态，不是 cut 这个截点上的状态"}}

    def _organizer_status(self):
        """整理供应商：模型、标签、密钥来源（只写来源，不写值）、费用口径（有没有价格表、预算上限管不管用）。"""
        from . import catalog as CT
        p = self.cfg.provider or {}
        params = p.get("params") or {}
        pr = self._pricing()
        row = CT.provider(p.get("id")) if p.get("id") else None
        return {"id": p.get("id"), "label": p.get("label") or (row["label"] if row else None), "model": params.get("model"),
                "key": {"env": p.get("api_key_env"), "source": KS.source(p.get("api_key_env"), p.get("key_file")) if p.get("api_key_env") else "not_needed"},
                "cost": {"currency": pr["currency"] if pr else None, "price_table": pr is not None,
                         "budget_enforced": bool(pr and pr["currency"] == "CNY" and self.cfg.daily_cap_cny is not None),
                         "note": None if pr else "没有这个模型的价格表：只记用量，不估价；预算上限对它不起作用"}}

    def _vectors_status(self):
        try:
            d = EW.status(self.store, self.db_path, self.cfg)
            if self.vector_config_error:
                d["config_error"] = self.vector_config_error
            if d.get("enabled"):
                reads = {}
                for r in self.store.conn.execute("SELECT detail FROM read_log WHERE ns=? AND kind='retrieval'", (self.ns,)):
                    try:
                        x = json.loads(r[0])
                        k = x.get("used")
                        reads[k] = reads.get(k, 0) + 1
                        if x.get("reason"):
                            reads["reason:" + x["reason"]] = reads.get("reason:" + x["reason"], 0) + 1
                    except (ValueError, AttributeError):
                        pass
                d["reads"] = reads
                try:
                    import numpy  # noqa: F401
                    d["numpy"] = {"in_this_process": True}
                except ImportError:
                    d["numpy"] = {"in_this_process": False}
            return d
        except Exception as e:                               # noqa: BLE001  状态读不出来要报出来，不当成没问题
            return {"enabled": None, "error": type(e).__name__}

    def _redaction_status(self, ws):
        j = json.loads(ws["redact_json"]) if ws.get("redact_json") else {}
        pend = 0
        for p in self._batches() + [self._pending_path()]:
            try:
                with open(p, "rb") as f:
                    for l in f.read().split(b"\n"):
                        if l.strip():
                            try:
                                pend += int(json.loads(l.decode("utf-8")).get("redacted") or 0)
                            except Exception:                  # noqa: BLE001
                                pass
            except FileNotFoundError:
                continue
        return {"rules_version": RD.RULES_VERSION, "stored": dict(j.get("by_kind") or {}), "stored_messages": j.get("messages", 0), "pending_blocks": pend}

    def _deletion_status(self):
        try:
            from .deletion import DeletionService
            svc = DeletionService(self.db_path, config_dir=self.config_dir, clock=self.clock, store=self.store)
            return {"incomplete": svc.incomplete(self.ns), "blocked": bool(self._deletion_blocked), "tasks": svc.status()[-5:]}
        except Exception as e:                               # noqa: BLE001
            return {"incomplete": [], "blocked": bool(self._deletion_blocked), "error": type(e).__name__}

    def cost_today(self):
        """当天已记的账（按时钟的日期）；软阈值没设就写没设。给用户自己看，不改任何东西。"""
        day = self.clock()[:10]
        d = self.store.cost_sum(self.ns, day)
        cap = self.cfg.daily_cap_cny
        return {"day": day, "cny": d["cny"], "calls": d["calls"], "unknown_calls": d["unknown_calls"], "by_currency": d["by_currency"], "cap_cny": cap, "cap_set": cap is not None}

    def export(self, path, as_of=None):
        """带格式版本的 UTF-8 JSONL：第一行是头（格式版本、工作区、截点、存储种类、提示词），之后每行一条记录：message／org（整理结果的每一行）／step。"""
        from . import view as LV  # noqa: F401
        dv0 = self.store.del_ver(self.ns)
        cut = as_of or self.cut()
        snap = self._reader.snapshot(cut)
        lines = [{"type": "header", "format": "yuanwei-export/1", "workspace": self.ns, "cut": snap.cut, "store_kind": "L", "index_version": self.index_version,
                  "write_prompt": self.cfg.write_prompt}]
        for r in snap.rows:
            lines.append({"type": "message", "msg_id": r["msg_id"], "session_id": r["session_id"], "seq": r["seq"], "role": r["role"], "time": r["msg_time"], "text": r["content"],
                          "fingerprint": r["fingerprint"], "is_long": bool(r["is_long"])})
        for t in LS_TABLES:
            for rec in snap.state["tables"][t]:
                lines.append({"type": "org", "table": t, "record": rec})
        from . import cuts as CU
        done = {}
        for rc in snap.state["tables"]["receipts"]:                                       # 新格式：这个截点里有收据的步，状态也按收据说（不读实时的步状态）
            if rc["kind"] == "step":
                done[rc["step_id"]] = "done"
            elif rc.get("op") in ("abandon_step", "skip_step"):
                done[rc["step_id"]] = "abandoned" if rc["op"] == "abandon_step" else "skipped"
        for s in self.store.list_steps(self.ns):
            if CU.is_v2(snap.cut):
                if s["step_id"] not in done:
                    continue
                status = done[s["step_id"]]
            else:
                if s["vclock_end"] is None or s["vclock_end"] > snap.cut["vclock"]:
                    continue
                status = s["status"]
            lines.append({"type": "step", "step_id": s["step_id"], "k": s["k"], "session_id": s["session_id"], "status": status, "msg_ids": s["msg_ids"],
                          "long_msg_ids": s["long_msg_ids"], "prompt_version": s["prompt_version"], "prompt_sha256": s["prompt_sha256"], "config_version": s["config_version"]})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l, ensure_ascii=False, sort_keys=True) + "\n")
        if self.store.del_ver(self.ns) != dv0:                 # 写的时候发生了删除：不交这份导出
            os.remove(tmp)
            raise HistoryDeleted("历史已删除，请重新导出")
        os.replace(tmp, path)
        return {"path": path, "records": len(lines), "cut": snap.cut}


from .lstate import TABLES as LS_TABLES  # noqa: E402
