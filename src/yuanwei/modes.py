"""四种模式（可配，按家设，默认「只给工具」）和每轮回答前交什么。

* tools_only（默认）：回答前什么都不交，常驻规矩加工具说明让模型按需调 recall。
* reminder：每轮交一句固定提醒（不读记忆；发不发只看工具装好没有、模式开没开、可信授权状态）。
* note：便条（Memory.note 的流程：读取权限 → 免查门 → 检索装料），按这家宿主的单位核最终文字，超了缩小再算。
* directory：只在会话开始、恢复、压缩这些边界交一份主线目录（2,000 字符）；边界事件只标记，第一条用户输入过了读取权限才读、才交；注入失败不算已交。
禁读的那一轮四种模式都**什么都不交、只交回执**。自动先查的取材有 3 秒时限：超时就什么都不加、记日志，不让这一轮失败，迟到的结果只记一条「迟到丢弃」、不会交给任何一轮；
身份或禁读登记失败仍然拒绝读取，不因为没超时就允许。取材超时不记成「检索质量为零」。
**读多个工作区（第 2c 步，Hermes 的读取范围）**：pre_answer 可以带 extras（范围里除当前工作区以外要一起读的）。便条、目录和读取工具照同一条范围读：权限、禁读、回执只看当前这一轮（一次登记、一次返回前复核）；
3 秒是总共的；开头一行写读了哪些，每段前标工作区，续读游标带工作区序号前缀；没读到的（超时、忙）和因宿主上限放不下的，在最后一行写明，不当成「没有」。不带 extras 时一个字节都不变。
"""
import dataclasses
import threading
import time

from . import gate as GT
from . import hosts as H
from . import note as NT
from .access import ToolIdentity
from .reader import Reader
from .rules import REMINDER
from .sqlstore import Store

MODES = ("tools_only", "reminder", "note", "directory")
SESSION_SOURCES = ("startup", "resume", "clear", "compact", "fork")


@dataclasses.dataclass
class ModeConfig:
    mode: str = "tools_only"
    tools_installed: bool = True
    auto_timeout_s: float = 3.0
    directory_chars: int = 2000

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("模式只能是 %s，实际 %r" % (MODES, self.mode))


class _Flag:
    """额外工作区的取材线程自己的超时标记（不借用这一轮的票据，免得它的超时把整轮标成超时）。"""
    timed_out = False


class _CountingClient:
    """向量客户端的计数外壳：取材线程里发生过几次向量调用，记下来（超时的线程也要记账）。其余属性原样转给里面的客户端。"""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def embed_batch(self, texts):
        self.calls += 1
        return self.inner.embed_batch(texts)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class ModeRunner:
    def __init__(self, mem, access, profile, cfg=None):
        self.mem, self.acc, self.profile, self.cfg = mem, access, profile, cfg or ModeConfig()
        self.host = profile.name
        self._workers = []
        self._live = {}                                                                    # (会话, 分支) → 还没结束的取材线程：同一个会话不叠开
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 事件入口
    def on_user_prompt(self, session_id, turn_id, text, branch="", origin="user", host_command=False, replay=False):
        """可信用户提交事件：先登记（解析固定口令），再决定回答前交什么。来源不明、回放的事件什么都不做。"""
        reg = self.acc.begin_turn(self.host, session_id, turn_id, branch, text=text, origin=origin, replay=replay)
        if not reg["registered"]:
            return {"inject": None, "kind": "ignored", "reason": reg["ignored"], "registered": False}
        return self.pre_answer(session_id, turn_id, text, branch, host_command)

    def session_event(self, session_id, source, branch=""):
        """会话开始、恢复、清空、压缩、分叉：目录模式只标记「该交」，不读、不交。"""
        if source not in SESSION_SOURCES:
            raise ValueError("会话事件来源只能是 %s，实际 %r" % (SESSION_SOURCES, source))
        if self.cfg.mode != "directory":
            return {"marked": False}
        return {"marked": True, "epoch": self.acc.dir_mark_needed(self.host, session_id, branch, source)}

    def compact_continue(self, session_id, turn_id, branch=""):
        """同一轮里压缩后直接续跑：继承这一轮的拒绝；允许时（目录模式）在下一次模型请求前补交，不等下一条用户消息。"""
        c = self.acc.continue_turn(self.host, session_id, turn_id, "compact", branch)
        if not c["continued"]:
            return {"inject": None, "kind": "ignored", "registered": False}
        if self.cfg.mode == "directory":
            self.acc.dir_mark_needed(self.host, session_id, branch, "compact")
        return self.pre_answer(session_id, turn_id, None, branch, False, skip_note=True)

    def confirm_injection(self, result, ok):
        """接头报告宿主有没有收下这次注入。**生成了、尝试注入了，和宿主确认收到了，是两件事**：没有报告的，不算已交。
        没收下（ok 为假）：登记「注入失败」，这一次读取之后按「交付情况无法确认」说，不冒称已交付；回执类的注入没收下，撤掉「这一轮已送」，下一次钩子还能再送。
        目录模式另外把「要不要重交」标记改成失败（下次还该交）。返回有没有改动任何状态。"""
        kind, sid, br, tid = result.get("kind"), result.get("session_id"), result.get("branch", ""), result.get("turn_id")
        changed = False
        if kind == "directory" and result.get("epoch") is not None:
            changed = self.acc.dir_confirm(self.host, sid, br, result["epoch"], ok)
        if not ok and tid is not None and kind in ("note", "directory", "receipt", "reminder"):
            if result.get("call_id"):
                self.acc.injection_failed(self.host, sid, br, tid, result["call_id"])
                changed = True
            if kind == "receipt":
                changed = bool(self.acc.release_receipt(self.host, sid, br, tid)) or changed
        return changed

    def join_workers(self, timeout=5.0):
        """等还在跑的取材线程结束（测试和关闭用）。"""
        for t in list(self._workers):
            t.join(timeout)
        self._workers = [t for t in self._workers if t.is_alive()]

    # ---------------------------------------------------------------- 回执：持续禁读每个宿主轮次只送一次
    def _receipt(self, base, ref, ident):
        persistent = ref["reason"] in ("session_setting", "workspace_setting", "window_closed", "window_identity_pending", "window_state_unreadable", "window_subagent", "window_close_unconfirmed") or ref.get("persistent")
        if persistent and ident.turn_id is not None and not self.acc.claim_receipt(self.host, ident.session_id, ident.branch or "", ident.turn_id):
            return dict(base, inject=None, kind="receipt_already_sent", receipt=ref["receipt"], reason=ref["reason"])
        return dict(base, inject=ref["text"], kind="receipt", receipt=ref["receipt"], reason=ref["reason"])

    # ---------------------------------------------------------------- 回答前交什么
    def pre_answer(self, session_id, turn_id, text, branch="", host_command=False, skip_note=False, extras=None, primary=None):
        """extras＝[(键, 显示名, 它的 Memory, 游标序号)…]：范围里除当前工作区以外要一起读的；primary＝(当前工作区的键, 显示名, 游标序号)。"""
        ident = ToolIdentity(self.host, session_id, branch, turn_id)
        base = {"session_id": session_id, "branch": branch, "turn_id": turn_id, "registered": True}
        mode = self.cfg.mode
        if mode in ("tools_only", "reminder"):
            ref = self.acc.check_permission(ident)
            if ref:
                return self._receipt(base, ref, ident)
            if mode == "reminder" and self.cfg.tools_installed:
                return dict(base, inject=REMINDER, kind="reminder")
            return dict(base, inject=None, kind="none")
        entry = mode
        extras = list(extras or [])
        summary = {"mode": mode}
        if extras:
            summary["workspaces"] = [(primary or (None,))[0]] + [e[0] for e in extras]
        tk = self.acc.start_read(ident, entry, summary)
        if isinstance(tk, dict):
            return self._receipt(base, tk, ident)
        try:
            if mode == "note":
                return dict(base, call_id=tk.call_id, **self._note(tk, text, host_command, extras, primary))
            return dict(base, call_id=tk.call_id, **self._directory(tk, ident, session_id, branch, extras, primary))
        except Exception as e:                                                             # noqa: BLE001  自动先查出任何错都不让这一轮失败：什么都不加，记日志
            self.acc.event(tk, "error", {"type": type(e).__name__})
            return dict(base, inject=None, kind="error", error=getattr(e, "code", type(e).__name__))

    # ---------------------------------------------------------------- 本轮排除：非线程里的读取（目录收紧字符上限后重取）也用同一个排除视图
    def _scoped(self, mem, tk):
        i = tk.ident
        ex = mem.excluded_ids(i.host, i.session_id, i.branch or "", i.turn_id)
        return mem._reader.scoped(ex) if ex else mem._reader

    # ---------------------------------------------------------------- 取材：3 秒时限，只管取材
    def _key(self, tk):
        return (tk.ident.session_id, tk.ident.branch or "")

    def _busy(self, tk):
        """同一个会话里上一次超时的取材线程还没结束：这一次不再叠开新的，记一笔、什么都不加。"""
        with self._lock:
            t = self._live.get(self._key(tk))
            if t is not None and t.is_alive():
                self.acc.event(tk, "auto_fetch_skipped_busy", {"since_worker": t.name})
                return True
            self._live.pop(self._key(tk), None)
            return False

    def _with_deadline(self, tk, fn, mem=None, timeout=None, mark=True):
        """mem＝读哪个工作区（默认当前这个）；timeout＝这一次最多等多久（默认配置里的 3 秒；多个工作区时是总时限里剩下的）；mark＝超时要不要标在这一轮的票据上（只有当前工作区这一次才标）。"""
        if self._busy(tk):
            return "busy", None
        box, lock = {}, threading.Lock()
        done = threading.Event()
        mem = mem or self.mem
        flag = tk if mark else _Flag()
        counter = _CountingClient(mem._reader.embed_client)

        def work():
            store = Store(mem.db_path)
            try:
                reader = Reader(store, mem.ns, mem.cfg, mem.index_version, mem.vector_fp, counter if counter.inner is not None else None, clock=mem.clock)
                i = tk.ident
                ex = store.turn_excluded(mem.ns, i.host, i.session_id, i.branch or "", i.turn_id)       # 本轮排除：按这个工作区自己的绑定查，别的工作区不拿这个工作区的编号
                rv = reader.scoped(ex) if ex else reader
                box["v"] = fn(rv)
                for info in list(reader.retrieval_log):                                    # 混合模式：这一次用了什么检索，进读取登记（登记失败不影响读取）
                    try:
                        self.acc._log_ticket(tk, "retrieval", info, store=store)
                    except Exception:                                                      # noqa: BLE001
                        pass
            except BaseException as e:                                                     # noqa: BLE001
                box["e"] = e
            finally:
                with lock:
                    done.set()
                    late = flag.timed_out
                if counter.calls:                                                          # 超时不等于没花向量的钱：这个线程里发生过的向量调用照样记账（迟到的也记）
                    try:
                        self.acc._log_ticket(tk, "vector_called", {"calls": counter.calls, "late": bool(late)}, store=store)
                    except Exception:                                                      # noqa: BLE001
                        pass
                if late:
                    try:
                        self.acc._log_ticket(tk, "late_result_dropped", {"error": type(box["e"]).__name__ if "e" in box else None}, store=store)
                    except Exception:                                                      # noqa: BLE001
                        pass
                store.close()
        t = threading.Thread(target=work, daemon=True, name="yuanwei-fetch")
        self._workers.append(t)
        with self._lock:
            self._live[self._key(tk)] = t
        t.start()
        limit_s = self.cfg.auto_timeout_s if timeout is None else timeout
        if done.wait(limit_s):
            t.join()
            self._workers = [w for w in self._workers if w is not t]
            with self._lock:
                if self._live.get(self._key(tk)) is t:
                    del self._live[self._key(tk)]
            if "e" in box:
                raise box["e"]
            return "ok", box["v"]
        with lock:
            if not done.is_set():
                flag.timed_out = True
        if flag.timed_out:
            self.acc.event(tk, "timeout", {"seconds": limit_s, "worker_still_running": True,
                                           "note": "超时不等于后台任务已取消，也不等于没发生向量调用；迟到的结果丢弃，向量调用照样记账"})
            return "timeout", None
        t.join()
        if "e" in box:
            raise box["e"]
        return "ok", box["v"]

    # ---------------------------------------------------------------- 便条
    def _note(self, tk, text, host_command, extras=(), primary=None):
        mem, cfg = self.mem, self.mem.cfg
        if text is None or not str(text).strip():
            raise ValueError("便条的查询必须是非空文字")
        free = GT.free_check(text, host_command=host_command) if cfg.note_free_gate else {"decision": "check", "rule": None}
        if free["decision"] != "check":
            self.acc.event(tk, "skipped", {"rule": free["rule"]})
            return {"inject": None, "kind": "skipped", "free": free}
        if self._busy(tk):
            return {"inject": None, "kind": "busy"}
        mem.check_and_spawn()
        self.acc.mark_search(tk)
        t0 = time.monotonic()
        state, an = self._with_deadline(tk, lambda reader: NT.analyze(reader, text, cfg))
        if state in ("timeout", "busy"):
            return {"inject": None, "kind": state}
        if extras:
            return self._note_multi(tk, text, an, extras, primary, t0)
        path = self.profile.paths["hook_context"]
        n = cfg.note_chars
        while True:
            c2 = dataclasses.replace(cfg, note_chars=n)
            r = NT.finalize(an, c2, cfg.note_tau_kw, cfg.note_tau_vec, persist=None, max_listed=cfg.note_max_listed)
            if not r["note"] or H.fits(path, r["note"])[0]:
                break
            n = int(n * 0.85)
            if n < 300:
                self.acc.event(tk, "oversize_blocked", {"path": "hook_context"})
                return {"inject": None, "kind": "error", "error": "return_too_large"}
        if not r["note"]:
            return {"inject": None, "kind": "note_none", "note_status": r["status"]}
        if r["unshown"]:
            r = NT.finalize(an, c2, cfg.note_tau_kw, cfg.note_tau_vec, persist=NT.make_persist(mem._reader, an), max_listed=cfg.note_max_listed)
        rows = an["snapshot"].by_id
        spans = [{"msg_id": m, "start": 0, "end": len(rows[m]["content"])} for m in r["delivered"]]
        self.acc.assembled(tk, spans, r["note"])
        out = self.acc.finish_read(tk, r["note"], "hook_context", profile=self.profile)
        if out["status"] != "ok":
            return {"inject": out["text"] if out["status"] == "refused" else None, "kind": "receipt" if out["status"] == "refused" else "error",
                    "receipt": out.get("receipt"), "reason": out.get("reason")}
        return {"inject": out["text"], "kind": "note", "note": r, "delivery": out["delivery"]}

    # ---------------------------------------------------------------- 多个工作区：同一票据、同一个总时限、一次返回前复核
    def _read_extras(self, tk, fn, extras, t0):
        """逐个读额外的工作区；总时限里剩多少用多少。返回 (读到的[(键,显示名,序号,Memory,结果)], 没读到的[{workspace, why}])。"""
        got, missed = [], []
        for key, label, mem, idx in extras:
            left = self.cfg.auto_timeout_s - (time.monotonic() - t0)
            if left <= 0:
                missed.append({"workspace": label, "why": "timeout"})
                continue
            state, v = self._with_deadline(tk, lambda reader, mem=mem: fn(reader, mem), mem=mem, timeout=left, mark=False)
            if state == "ok":
                got.append((key, label, idx, mem, v))
            else:
                missed.append({"workspace": label, "why": state})
        return got, missed

    @staticmethod
    def _assemble(read_labels, sections, missed, dropped):
        notes = []
        if missed:
            notes.append("（没读到：%s）" % "、".join("%s（%s）" % (m["workspace"], {"timeout": "超时", "busy": "上一次取材还没结束"}.get(m["why"], m["why"])) for m in missed))
        if dropped:
            notes.append("（没放进来：%s——放不下这家宿主这一路的上限，需要时用工具单独查）" % "、".join(dropped))
        body = "\n".join("〔工作区：%s〕\n%s" % (lab, txt) for lab, txt in sections)
        return "\n".join(x for x in ("〔读取范围：%s〕" % "、".join(read_labels), body, "\n".join(notes)) if x)

    def _note_multi(self, tk, text, an, extras, primary, t0):
        mem, cfg = self.mem, self.mem.cfg
        pkey, plabel, pidx = primary or (None, "当前工作区", 0)
        got, missed = self._read_extras(tk, lambda reader, m: NT.analyze(reader, text, cfg), extras, t0)
        parts = [(pkey, plabel, pidx, mem, an)] + [(k, lab, i, m, v) for (k, lab, i, m, v) in got]
        path = self.profile.paths["hook_context"]
        dropped = []
        while True:
            k = len(parts)
            n = cfg.note_chars if k == 1 else cfg.note_chars // k
            while True:
                c2 = dataclasses.replace(cfg, note_chars=n)
                rs = [NT.finalize(a, c2, cfg.note_tau_kw, cfg.note_tau_vec, persist=None, max_listed=cfg.note_max_listed) for (_k, _l, _i, _m, a) in parts]
                text_out = self._assemble([p[1] for p in parts], [(p[1], r["note"]) for p, r in zip(parts, rs) if r["note"]], missed, dropped) if any(r["note"] for r in rs) else ""
                if not text_out or H.fits(path, text_out)[0]:
                    break
                n = int(n * 0.85)
                if n < 300:
                    break
            if not text_out or H.fits(path, text_out)[0]:
                break
            if len(parts) > 1:
                dropped.insert(0, parts.pop()[1])                                              # 还是放不下：从后面的工作区开始去掉，并写明
                continue
            self.acc.event(tk, "oversize_blocked", {"path": "hook_context"})
            return {"inject": None, "kind": "error", "error": "return_too_large"}
        if not text_out:
            return {"inject": None, "kind": "note_none", "note_status": rs[0]["status"], "workspaces_not_read": missed}
        final = []
        for (key, label, idx, m, a), r in zip(parts, rs):
            if r["note"] and r["unshown"]:
                r = NT.finalize(a, c2, cfg.note_tau_kw, cfg.note_tau_vec, persist=NT.make_persist(m._reader, a), max_listed=cfg.note_max_listed)
                if r["cursor"]:
                    r = dict(r, note=r["note"].replace(r["cursor"], "W%d~%s" % (idx, r["cursor"])))      # 续读游标带工作区序号：工具那一头照它找对工作区
            final.append((key, label, m, a, r))
        spans = []
        for key, label, m, a, r in final:
            rows = a["snapshot"].by_id
            spans += [{"workspace": key, "msg_id": mid, "start": 0, "end": len(rows[mid]["content"])} for mid in r["delivered"]]
        text_out = self._assemble([f[1] for f in final], [(f[1], f[4]["note"]) for f in final if f[4]["note"]], missed, dropped)
        if not H.fits(path, text_out)[0]:
            self.acc.event(tk, "oversize_blocked", {"path": "hook_context"})
            return {"inject": None, "kind": "error", "error": "return_too_large"}
        self.acc.assembled(tk, spans, text_out)
        out = self.acc.finish_read(tk, text_out, "hook_context", profile=self.profile)
        if out["status"] != "ok":
            return {"inject": out["text"] if out["status"] == "refused" else None, "kind": "receipt" if out["status"] == "refused" else "error",
                    "receipt": out.get("receipt"), "reason": out.get("reason")}
        return {"inject": out["text"], "kind": "note", "note": final[0][4], "delivery": out["delivery"], "workspaces_not_read": missed + [{"workspace": d, "why": "over_limit"} for d in dropped]}

    # ---------------------------------------------------------------- 目录
    def _directory(self, tk, ident, session_id, branch, extras=(), primary=None):
        row = self.acc.dir_ensure(self.host, session_id, branch)
        if row["state"] == "handed":
            self.acc.event(tk, "skipped", {"rule": "directory_already_handed", "epoch": row["epoch"]})
            return {"inject": None, "kind": "none", "epoch": row["epoch"]}
        if extras:
            return self._directory_multi(tk, row, session_id, branch, extras, primary)
        path = self.profile.paths["hook_context"]
        limit = self.cfg.directory_chars
        state, d = self._with_deadline(tk, lambda reader: reader.directory(limit=limit))
        if state in ("timeout", "busy"):
            return {"inject": None, "kind": state}
        while d is not None and not H.fits(path, d["text"])[0]:
            limit = int(limit * 0.8)
            d = self._scoped(self.mem, tk).directory(cut=d["cut"], limit=limit)
        if d is None:
            self.acc.event(tk, "skipped", {"rule": "no_directory"})
            return {"inject": None, "kind": "none", "epoch": row["epoch"]}
        self.acc.assembled(tk, [], d["text"])
        out = self.acc.finish_read(tk, d["text"], "hook_context", profile=self.profile)
        if out["status"] != "ok":
            return {"inject": out["text"] if out["status"] == "refused" else None, "kind": "receipt" if out["status"] == "refused" else "error", "receipt": out.get("receipt"), "reason": out.get("reason")}
        self.acc.dir_composed(self.host, session_id, branch, row["epoch"], d["version"])
        return {"inject": out["text"], "kind": "directory", "epoch": row["epoch"], "version": d["version"], "delivery": out["delivery"]}

    def _directory_multi(self, tk, row, session_id, branch, extras, primary):
        """多个工作区的目录：总共不超过一份目录的字符上限（每个工作区平分），「已交过」的状态按会话记一份，不是每个工作区一份。"""
        pkey, plabel, pidx = primary or (None, "当前工作区", 0)
        path = self.profile.paths["hook_context"]
        t0 = time.monotonic()
        limit = max(1, self.cfg.directory_chars // (1 + len(extras)))
        state, d = self._with_deadline(tk, lambda reader: reader.directory(limit=limit))
        if state in ("timeout", "busy"):
            return {"inject": None, "kind": state}
        got, missed = self._read_extras(tk, lambda reader, m: reader.directory(limit=limit), extras, t0)
        parts = [[pkey, plabel, pidx, self.mem, d]] + [[k, lab, i, m, v] for (k, lab, i, m, v) in got]
        dropped = []
        while True:
            sections = [(p[1], p[4]["text"]) for p in parts if p[4] is not None]
            if not sections:
                self.acc.event(tk, "skipped", {"rule": "no_directory"})
                return {"inject": None, "kind": "none", "epoch": row["epoch"], "workspaces_not_read": missed}
            text_out = self._assemble([p[1] for p in parts], sections, missed, dropped)
            if H.fits(path, text_out)[0]:
                break
            if limit > 200:
                limit = int(limit * 0.8)
                for p in parts:
                    if p[4] is not None:
                        p[4] = self._scoped(p[3], tk).directory(cut=p[4]["cut"], limit=limit)
            elif len(parts) > 1:
                dropped.insert(0, parts.pop()[1])
            else:
                self.acc.event(tk, "oversize_blocked", {"path": "hook_context"})
                return {"inject": None, "kind": "error", "error": "return_too_large"}
        self.acc.assembled(tk, [], text_out)
        out = self.acc.finish_read(tk, text_out, "hook_context", profile=self.profile)
        if out["status"] != "ok":
            return {"inject": out["text"] if out["status"] == "refused" else None, "kind": "receipt" if out["status"] == "refused" else "error", "receipt": out.get("receipt"), "reason": out.get("reason")}
        version = "+".join(p[4]["version"] for p in parts if p[4] is not None)
        self.acc.dir_composed(self.host, session_id, branch, row["epoch"], version)
        return {"inject": out["text"], "kind": "directory", "epoch": row["epoch"], "version": version, "delivery": out["delivery"],
                "workspaces_not_read": missed + [{"workspace": x, "why": "over_limit"} for x in dropped]}
