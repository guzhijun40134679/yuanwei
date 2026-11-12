"""自带向量引擎：运行——守护者（看管 llama-server）、锁和状态、使用方登记、在途请求、客户端、停止／移除、状态。

要点（对应 Codex 核对 R2–R5）：
* 一个守护者 ＝ 一把 flock（run/serve.lock，永远不删不换）。进程一死锁就没了；状态文件只在锁被持有时才可信。
* 每次拉起 llama-server 有唯一的启动代号（run_id）：它进状态文件、进 --alias（命令行上），客户端核对「回应里的模型名就是这个代号」。
* 本机专用的随机凭据（--api-key-file，不放命令行）；客户端只连 127.0.0.1、不走代理、不跟跳转；凭据只发给「状态里写着的、子进程还活着的」那个端口。
* llama-server 的原始 stdout／stderr 绝不落盘：标准输出丢掉；标准错误在内存里有界地读，只提取固定的类别（参数被拒、端口被占），其余一概丢弃。
* 停止走私有控制文件（run/control.json，带目标启动代号）；孤儿进程只认「我们自己登记过的启动代号」，拿不准的不杀、只报告。
* 空闲退出：请求进入时在 run/inflight/ 登记、做完释放；守护者在 admit.lock 里确认没有在途请求才宣布退出（draining），客户端准入时看到 draining 就不用这个实例；间隔用单调时钟。
* 重启预算：「从没就绪」和「就绪几秒就崩」共用一个连续失败上限（5 次）和一个总上限（20 次）；稳定跑满 60 秒清零；放弃后写冷却（600 秒，再失败 1800 秒），冷却内不再拉起。
* 被系统拦：不凭隔离属性下结论；先核完整性，再照正常流程启动，尊重系统真实的反应；一启动就被 SIGKILL 且没有输出：只记「原因不明」，不断言。绝不去改属性或签名。
* 所有外界（进程、时间、ps、xattr、codesign）走 Host 这一个对象；外部命令只用写死的系统路径、参数是列表、不经 shell。
"""
import contextlib
import errno
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import catalog as CT
from . import enginepkg as EP
from . import vectors as VX
from .errors import ProviderError

PS, XATTR, CODESIGN, SYSCTL = "/bin/ps", "/usr/bin/xattr", "/usr/bin/codesign", "/usr/sbin/sysctl"
ALIAS_RE = re.compile(r"^yuanwei-[0-9a-f]{32}$")
RE_ARGS_REJECTED = re.compile(rb"invalid argument|unknown argument|unrecognized (?:argument|option)", re.I)
RE_PORT_BUSY = re.compile(rb"address already in use|couldn't bind|failed to bind|bind.*failed", re.I)
RE_MODEL_LOAD = re.compile(rb"failed to load model|error loading model", re.I)


class EngineDown(VX.ServiceDown):
    """引擎现在不能用：reason 是类别。"""


def defaults(emb=None):
    d = dict(CT.ENGINE_DEFAULTS)
    for k in list(d):
        if emb and emb.get(k) is not None:
            d[k] = emb[k]
    return d


def src_path():
    """yuanwei 包所在的目录（给子进程的 PYTHONPATH 用）：谁在跑就是谁，不依赖配置。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ---------------------------------------------------------------- 外界
class Host:
    """真实的外界：测试里整个或逐项换成假的。外部命令只用写死的系统路径；xattr 只许 -l；codesign 只许只读的 -d。"""

    ALLOWED = {PS: {"-axww", "-o", "-p"}, XATTR: {"-l"}, CODESIGN: {"-d", "--verbose=2"}, SYSCTL: {"-n"}}

    def mono(self):
        return time.monotonic()

    def time(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def uid(self):
        return os.getuid()

    def popen(self, argv, **kw):
        return subprocess.Popen(list(argv), shell=False, **kw)

    def pid_alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError as e:
            return e.errno == errno.EPERM
        return True

    def kill(self, pid, sig):
        os.kill(pid, sig)

    def _run_ro(self, argv, timeout=10):
        exe = argv[0]
        if exe not in self.ALLOWED or any(a.startswith("-") and a not in self.ALLOWED[exe] for a in argv[1:]):
            raise ValueError("命令不在白名单里")
        try:
            r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, shell=False, cwd="/")
            return r.returncode, r.stdout
        except (OSError, subprocess.TimeoutExpired):
            return None, ""

    def ps_all(self):
        """[(pid, uid, command)]。读不出来返回 None（不是空列表：不能把「没读到」当成「没有」）。"""
        rc, out = self._run_ro([PS, "-axww", "-o", "pid=,uid=,command="])
        if rc != 0:
            return None
        rows = []
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
            if m:
                rows.append((int(m.group(1)), int(m.group(2)), m.group(3)))
        return rows

    def proc_start(self, pid):
        rc, out = self._run_ro([PS, "-o", "lstart=", "-p", str(pid)])
        return out.strip() or None if rc == 0 else None

    def xattr_list(self, path):
        rc, out = self._run_ro([XATTR, "-l", path])
        return None if rc is None else [l.split(":")[0] for l in out.splitlines() if re.match(r"^[\w.]+:", l)]

    def codesign_info(self, path):
        """只读地看签名（不改）。返回 {"signed": bool}；命令不在或没跑成返回 None。"""
        if not os.path.exists(CODESIGN):
            return None
        rc, _o = self._run_ro([CODESIGN, "-d", "--verbose=2", path])
        return None if rc is None else {"signed": rc == 0}

    def hardware_arm64(self):
        rc, out = self._run_ro([SYSCTL, "-n", "hw.optional.arm64"]) if os.path.exists(SYSCTL) else (None, "")
        return None if rc != 0 else out.strip() == "1"


# ---------------------------------------------------------------- 本机回环的 HTTP（不走代理、不跟跳转）
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class LoopbackOpener:
    def __init__(self):
        self._op = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, req, timeout=None):
        host = urllib.parse.urlsplit(req.full_url).hostname
        if host != "127.0.0.1":
            raise ProviderError("只许连 127.0.0.1")
        return self._op.open(req, timeout=timeout)


def http_get(opener, url, key=None, timeout=2.0):
    """(状态码, JSON 或 None)；连不上 (None, None)。"""
    req = urllib.request.Request(url, headers=({"Authorization": "Bearer " + key} if key else {}))
    try:
        with opener(req, timeout=timeout) as r:
            raw, code = r.read(), getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ProviderError):
        return None, None
    try:
        return code, json.loads(raw.decode("utf-8"))
    except ValueError:
        return code, None


# ---------------------------------------------------------------- 状态、生命周期、使用方
def trusted_state(paths):
    """server.json：只在守护者持锁时才可信；没人持锁＝过期，一律当没有。"""
    if not EP.lock_is_held(paths.serve_lock):
        return None
    st = EP.read_json(paths.state)
    return st if isinstance(st, dict) else None


def lifecycle_state(paths):
    return (EP.read_json(paths.lifecycle, {}) or {}).get("state", "active")


def set_lifecycle(paths, state):
    EP.write_json_atomic(paths.lifecycle, {"state": state})


def register_user(paths, user_id, info=None):
    with EP.flock(paths.lifecycle_lock, blocking=True, timeout=5) as got:
        if not got:
            raise EP.EngineError("engine_busy")
        u = EP.read_json(paths.users, {"users": {}}) or {"users": {}}
        u["users"][user_id] = dict(info or {}, at=iso(time.time()))
        EP.write_json_atomic(paths.users, u)
        if lifecycle_state(paths) != "active":
            set_lifecycle(paths, "active")


def list_users(paths):
    return sorted(((EP.read_json(paths.users, {}) or {}).get("users") or {}))


def unbind_user(paths, user_id, host=None, wait=None, pins=None, grace=None):
    """卸载一个使用方。还有别的使用方就不动引擎；没有了才停（闲置本来也会 900 秒退出）。返回 {"remaining": [...], "stopped": bool|None}。"""
    with EP.flock(paths.lifecycle_lock, blocking=True, timeout=5) as got:
        if not got:
            raise EP.EngineError("engine_busy")
        u = EP.read_json(paths.users, {"users": {}}) or {"users": {}}
        u["users"].pop(user_id, None)
        EP.write_json_atomic(paths.users, u)
        remaining = sorted(u["users"])
    if remaining:
        return {"remaining": remaining, "stopped": None}
    r = stop_engine(paths, host=host, wait=wait, pins=pins, grace=grace)
    out = {"remaining": [], "stopped": r["stopped"]}
    if not r["stopped"]:
        out["reason"] = r.get("reason")
    return out


def _registered_run_ids(paths):
    ch = EP.read_json(paths.children, {}) or {}
    return [r for r in (ch.get("run_ids") or []) if isinstance(r, str)] if isinstance(ch, dict) else []


def stop_engine(paths, host=None, wait=None, target="*", pins=None, grace=None):
    """停止分两步，两步都确认了才算停：
    ① 守护者：走私有控制文件，交给守护者执行；等它放掉 serve.lock。守护者不响应就报告（stop_timeout），绝不去 kill 别的进程。
    ② 它登记过的子进程（children.json 里的启动代号）：守护者被强杀时锁放了、llama-server 可能还活着。持着 serve.lock（不让新守护者起来）核对并结束它们，
       确认退出了才删登记。进程列表读不到、归属不能确认、结束失败、等超时：一律 stopped=False（带原因），登记留着。"""
    host = host or Host()
    wait = CT.ENGINE_DEFAULTS["stop_wait_seconds"] if wait is None else wait
    was_running = EP.lock_is_held(paths.serve_lock)
    if was_running:
        EP.write_json_atomic(paths.control, {"id": uuid.uuid4().hex, "cmd": "stop", "target": target, "at": host.time()})
        end = host.mono() + wait
        while EP.lock_is_held(paths.serve_lock):
            if host.mono() >= end:
                return {"stopped": False, "was_running": True, "reason": "stop_timeout"}
            host.sleep(0.05)
    if not _registered_run_ids(paths):
        return {"stopped": True, "was_running": was_running}
    with EP.flock(paths.serve_lock) as got:                                              # 清理期间不许有新的守护者起来
        if not got:
            return {"stopped": False, "was_running": True, "reason": "supervisor_restarted"}
        r = settle_children(paths, pins, host, grace)
    if not r["ok"]:
        return {"stopped": False, "was_running": was_running, "reason": r["reason"], "children_pending": r["pending"]}
    return {"stopped": True, "was_running": was_running}


def remove_engine(paths, host=None, wait=None, keep_models_not_owned=True, pins=None, grace=None):
    """整个移除：先进入持久的「正在移除」状态（挡住并发的 ensure／暖机／重启），停守护者，确认停了再删自己拥有的文件。
    停不下来就不继续删。锁文件（serve.lock／lifecycle.lock／admit.lock）永远不删。返回 {"removed": [...], "users": [...], "stopped": bool}。"""
    host = host or Host()
    with EP.flock(paths.lifecycle_lock, blocking=True, timeout=5) as got:
        if not got:
            raise EP.EngineError("engine_busy")
        users = list_users(paths)
        set_lifecycle(paths, "removing")
        r = stop_engine(paths, host, wait, pins=pins, grace=grace)
        if not r["stopped"]:                                                              # 没能确认已停（含登记的子进程）：不进删除那几步、不报完成
            return {"removed": [], "users": users, "stopped": False, "reason": r.get("reason")}
        removed = []
        rc = EP.read_receipt(paths)
        import shutil
        for d in (paths.bin, paths.staging):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(os.path.basename(d))
        m = rc.get("model") or {}
        if m.get("owned") and m.get("path") and os.path.dirname(os.path.abspath(m["path"])) == os.path.abspath(paths.models):
            with contextlib.suppress(OSError):
                os.unlink(m["path"])
            removed.append("model")
        for f in (paths.receipt, paths.state, paths.control, paths.backoff, paths.last_exit, paths.children, paths.activity, paths.users):
            with contextlib.suppress(OSError):
                os.unlink(f)
        for n in os.listdir(paths.run) if os.path.isdir(paths.run) else []:
            if n.startswith("key-"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(paths.run, n))
        set_lifecycle(paths, "disabled")
        return {"removed": removed, "users": users, "stopped": True}


def owned_bytes(paths):
    n = 0
    for d in (paths.bin, paths.models):
        for dp, _dn, fns in os.walk(d):
            for f in fns:
                with contextlib.suppress(OSError):
                    n += os.lstat(os.path.join(dp, f)).st_size
    return n


# ---------------------------------------------------------------- 登记过的子进程：核对、结束、确认退出
def pinned_binary(paths, pins=None):
    """钉住的程序路径（按钉住表算，不看磁盘现状）；算不出来返回 None。"""
    pins = EP.get_pins(pins)
    a = EP.asset(pins) or {}
    tag = (pins.get("engine") or {}).get("tag")
    if not a or not tag or not a.get("binary") or not a.get("manifest"):
        return None
    return os.path.join(paths.package_dir(tag, EP.package_digest(a["manifest"])), a["binary"])


def log_event(paths, host, event, **kv):
    try:
        os.makedirs(paths.logs, mode=0o700, exist_ok=True)
        fd = os.open(paths.supervisor_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(at=iso(host.time()), event=event, **{k: v for k, v in kv.items() if isinstance(v, (str, int, float, bool, type(None)))}), ensure_ascii=False) + "\n")
    except OSError:
        pass


def settle_children(paths, pins, host, grace=None, report_suspects=False):
    """children.json 里登记过的启动代号：找到「还活着、而且确实是我们的」进程，请它结束，等它退，用重新读进程列表确认，确认了才删登记。
    归属条件（三个都要）：命令行以钉住的程序路径开头、带 --alias yuanwei-<登记过的启动代号>、用户是当前用户。只凭 PID 或进程名一概不杀。
    不为清理拉起任何服务实例。调用方负责保证没有活着的守护者（持着 serve.lock，或者本身就是刚拿到锁的守护者）。
    返回 {"ok": bool, "reason": 类别|None, "pending": [启动代号], "suspects": [pid]}；ok=False 时登记原样留着（只删确认退了的）。
    原因类别：process_list_unreadable｜binary_unknown｜child_signal_failed｜child_not_exiting｜child_not_owned。
    report_suspects＝没登记也扫一遍进程列表，把「长得像但认不准」的 PID 报出来（守护者启动时用；不杀）。"""
    grace = CT.ENGINE_DEFAULTS["kill_grace_seconds"] if grace is None else grace
    ch = EP.read_json(paths.children, {})
    ch = ch if isinstance(ch, dict) else {}
    ids = {r for r in (ch.get("run_ids") or []) if isinstance(r, str)}
    out = {"ok": True, "reason": None, "pending": [], "suspects": []}
    if not ids and not report_suspects:
        return out
    binary = pinned_binary(paths, pins)
    if not binary:
        return dict(out, ok=not ids, reason="binary_unknown" if ids else None, pending=sorted(ids))
    pat = re.compile("^" + re.escape(binary) + r" .*--alias (yuanwei-[0-9a-f]{32})(?: |$)")

    def scan():
        rows = host.ps_all()
        if rows is None:
            return None
        mine, foreign, suspects = {}, {}, []
        for pid, uid, cmd in rows:
            m = pat.search(cmd)
            if not m or pid == os.getpid():
                continue
            rid = m.group(1)[len("yuanwei-"):]
            if rid in ids and uid == host.uid():
                mine[pid] = rid
            elif rid in ids:
                foreign[pid] = rid                                                       # 登记过的启动代号，但用户不同：不杀、也不能说它退了
                suspects.append(pid)
            else:
                suspects.append(pid)                                                     # 没登记过：不是我们拉起的（或登记丢了）：不杀，只报告
        return mine, foreign, suspects

    st = {"mine": {}, "foreign": {}}

    def fail(reason, pending=None):
        return {"ok": False, "reason": reason, "pending": sorted(ids if pending is None else pending), "suspects": out["suspects"]}

    def signal_all(procs, sig):
        bad = False
        for pid in procs:
            try:
                host.kill(pid, sig)
            except ProcessLookupError:
                pass                                                                     # 已经没了：正好
            except OSError:
                bad = True                                                               # 发不出信号：不能当它退了
        return bad

    def look():
        """重新读进程列表，更新 st。读不到返回 False。"""
        s = scan()
        if s is None:
            return False
        st["mine"], st["foreign"], out["suspects"] = s
        return True

    def wait_gone():
        end = host.mono() + grace
        while True:
            host.sleep(0.05)
            if not look():
                return False
            if not st["mine"] or host.mono() >= end:
                return True

    if not look():
        return fail("process_list_unreadable")
    seen = set(st["mine"].values())                                                      # 见过、要请它结束的启动代号
    if st["mine"]:
        if signal_all(list(st["mine"]), signal.SIGTERM):
            return fail("child_signal_failed")
        if not wait_gone():
            return fail("process_list_unreadable")
        if st["mine"]:                                                                   # 宽限时间到了还没退：SIGKILL，再等一个宽限（和守护者自己停子进程的做法一样）
            if signal_all(list(st["mine"]), signal.SIGKILL):
                return fail("child_signal_failed")
            if not wait_gone():
                return fail("process_list_unreadable")
            if st["mine"]:
                return fail("child_not_exiting", set(st["mine"].values()) | set(st["foreign"].values()))
    left = set(st["foreign"].values())                                                   # 最后一次读列表里还在的、但不是我们能处理的
    if ids or left:
        EP.write_json_atomic(paths.children, dict(ch, run_ids=sorted(left)))             # 确认退了（最后一次读列表里没有）才删登记
    for rid in sorted(seen):
        log_event(paths, host, "orphan_terminated", run_id=rid)
    if left:
        return fail("child_not_owned", left)
    return out


# ---------------------------------------------------------------- 在途请求、准入
def _touch_activity(paths):
    with contextlib.suppress(OSError):
        with open(paths.activity, "w") as f:
            f.write(uuid.uuid4().hex)


def admit(paths, st, host, pid=None):
    """请求进入：在 admit.lock 里确认守护者没有宣布退出（draining）、状态还是同一轮，再登记在途。返回登记文件的路径。"""
    with EP.flock(paths.admit_lock, blocking=True, timeout=2, sleep=host.sleep, mono=host.mono) as got:
        if not got:
            raise EngineDown("admit_busy", "engine_starting")
        cur = trusted_state(paths)
        if not cur or cur.get("state") != "ready" or cur.get("draining") or cur.get("run_id") != st.get("run_id"):
            raise EngineDown("engine_starting", "engine_starting")
        os.makedirs(paths.inflight, mode=0o700, exist_ok=True)
        p = os.path.join(paths.inflight, "%d-%s" % (pid or os.getpid(), uuid.uuid4().hex))
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _touch_activity(paths)
    return p


def release(paths, token):
    with contextlib.suppress(OSError):
        os.unlink(token)
    _touch_activity(paths)


# ---------------------------------------------------------------- 拉起守护者
def scrubbed_env(extra=None):
    """给守护者和 llama-server 的环境：只有这几项，没有任何 *_API_KEY、HERMES_*。"""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if k in os.environ}
    keep.setdefault("PATH", "/usr/bin:/bin")
    keep.update(extra or {})
    return keep


def spawn_supervisor(engine_dir, host=None, extra_args=(), pythonpath=None):
    """脱离调用进程的新会话，拉起守护者。固定工作目录（引擎的 run 目录），python -P（不把当前目录放进导入路径），参数是列表。"""
    host = host or Host()
    paths = EP.Paths(engine_dir)
    paths.ensure_dirs()
    env = scrubbed_env({"PYTHONPATH": pythonpath or src_path()})
    argv = [sys.executable, "-P", "-m", "yuanwei", "vectors", "serve", "--engine-dir", paths.root] + list(extra_args)
    return host.popen(argv, cwd=paths.run, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _backoff_active(paths, now):
    b = EP.read_json(paths.backoff, {}) or {}
    return bool(b.get("next_try_at")) and now < b["next_try_at"], b


def ensure(paths, expect, host=None, spawner=None, wait=0.0, opener=None):
    """要用引擎的人先调它。返回 ready 的状态字典；用不了就抛 EngineDown(reason)。
    expect：{"model_file_sha256","runtime_version","context_length"}（引擎自己决定的三项）：已经在跑的实例也要核，对不上报 engine_identity_conflict。
    wait 秒内等它就绪（后台补算等得起；查询默认 0）。没人在跑就拉起一个（拉起前看：停用状态、退避冷却）。"""
    host = host or Host()
    deadline = host.mono() + max(0.0, wait)
    spawned = False
    while True:
        lc = lifecycle_state(paths)
        if lc != "active":
            raise EngineDown("engine_disabled", "engine_disabled")
        st = trusted_state(paths)
        if st is not None:
            for k in ("model_file_sha256", "runtime_version", "context_length"):
                if k in (expect or {}) and st.get(k) is not None and st.get(k) != expect[k]:
                    raise EngineDown("engine_identity_conflict", "engine_identity_conflict")
            if st.get("state") == "ready" and not st.get("draining"):
                return st
            if st.get("state") == "failed":
                raise EngineDown("engine_failed", "engine_failed")
        else:
            cool, b = _backoff_active(paths, host.time())
            if cool:
                raise EngineDown("engine_failed", "engine_failed")
            if not spawned:
                with EP.flock(paths.lifecycle_lock) as got:
                    if got and lifecycle_state(paths) == "active":
                        try:
                            (spawner or (lambda: spawn_supervisor(paths.root, host)))()
                        except Exception as e:                                           # noqa: BLE001  拉起失败：只报类别
                            raise EngineDown("engine_spawn_error:" + type(e).__name__, "engine_stopped")
                        spawned = True
                    elif not got:
                        raise EngineDown("engine_busy", "engine_stopped")
        if host.mono() >= deadline:
            raise EngineDown("engine_starting", "engine_starting")
        host.sleep(0.05)


class ManagedClient:
    """自带引擎的向量客户端（查询和补算两侧都用它）：固定回环地址、不走代理、不跟跳转；带本轮凭据；核对回应里的模型名就是本轮的启动代号。
    embed_batch 和 HttpEmbeddingClient 同一个接口。引擎没开：不抛网络错，抛 EngineDown(reason)（reason 是 engine_starting 等类别）。"""

    def __init__(self, emb, role="query", host=None, opener=None, spawner=None, config_path=None):
        self.emb = emb or {}
        self.paths = EP.Paths(self.emb["engine_dir"])
        self.identity = self.emb.get("identity") or {}
        self.dim = self.identity.get("dimension")
        self.role = role
        self.host = host or Host()
        self.opener = opener or LoopbackOpener()
        d = defaults(self.emb)
        self.wait = float(d["cold_wait_seconds"]) if role == "query" else 60.0
        self.timeout = float(self.emb.get("timeout_query", 5)) if role == "query" else float(self.emb.get("timeout_worker", 60))
        self.breaker = float(self.emb.get("breaker_seconds", d["breaker_seconds"]))
        raw = spawner or (lambda: spawn_supervisor(self.paths.root, self.host, extra_args=self._extra()))
        self._last_spawn = None

        def throttled():                                                           # 同一个客户端 3 秒内不重复拉起（每次查询都拉一个没有意义；守护者自己也会因为抢不到锁退出）
            now = self.host.mono()
            if self._last_spawn is not None and now - self._last_spawn < 3.0:
                return None
            self._last_spawn = now
            return raw()
        self.spawner = throttled
        self._fail = None                 # (run_id, mono, reason)：断路器按启动代号失效
        self.run_id = None
        self.last_error = None

    def _extra(self):
        out = []
        for k, flag in (("idle_exit_seconds", "--idle-exit-seconds"), ("ready_timeout_seconds", "--ready-timeout")):
            if self.emb.get(k) is not None:
                out += [flag, str(self.emb[k])]
        return out

    def expect(self):
        return {k: self.identity[k] for k in ("model_file_sha256", "runtime_version", "context_length") if k in self.identity}

    def embed_batch(self, texts):
        host = self.host
        st = None
        try:
            st = ensure(self.paths, self.expect(), host, self.spawner, self.wait)
        except EngineDown as e:
            self.last_error = e.reason
            raise
        if self.run_id is not None and self.run_id != st.get("run_id") and self.role == "worker":
            self.run_id = st.get("run_id")
            raise EngineDown("engine_restarted", "engine_starting")             # 补算同一批里换了实例：整批失败，不混身份
        self.run_id = st.get("run_id")
        if self._fail and self._fail[0] == self.run_id and host.mono() - self._fail[1] < self.breaker:
            raise EngineDown(self._fail[2], self._fail[2])                         # 同一轮启动内的失败：断路器；换了启动代号就不挡
        if not host.pid_alive(st.get("child_pid") or -1):
            raise EngineDown("engine_starting", "engine_starting")                # 子进程不在了：不向这个端口发正文
        token = admit(self.paths, st, host)
        try:
            return self._post(st, texts)
        finally:
            release(self.paths, token)

    def _post(self, st, texts):
        kf = st.get("key_file")
        if not kf or os.path.dirname(os.path.abspath(kf)) != self.paths.run:
            raise EngineDown("engine_identity_mismatch", "engine_identity_mismatch")
        try:
            with open(kf, encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            raise EngineDown("engine_starting", "engine_starting")
        alias = st.get("alias")
        body = json.dumps({"model": alias, "input": texts}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/embeddings" % int(st["port"]), data=body,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                obj = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            reason = "engine_identity_mismatch" if (300 <= e.code < 400 or e.code in (401, 403)) else "service_down"
            if e.code >= 500 or e.code == 429 or reason != "service_down":
                self._fail = (st.get("run_id"), self.host.mono(), reason)
                raise EngineDown("HTTP %d" % e.code, reason)
            raise VX.Rejected("向量服务出错：HTTP %d" % e.code)
        except (urllib.error.URLError, OSError) as e:
            self._fail = (st.get("run_id"), self.host.mono(), "service_down")
            raise EngineDown(type(e).__name__, "service_down")
        except ValueError as e:
            raise ProviderError("向量服务出错：%s" % type(e).__name__)
        if not isinstance(obj, dict) or obj.get("model") != alias:                 # 回应不是本轮那个实例发的
            self._fail = (st.get("run_id"), self.host.mono(), "engine_identity_mismatch")
            raise EngineDown("alias_mismatch", "engine_identity_mismatch")
        return VX.parse_embeddings_response(obj, len(texts), self.dim)


def resolve_client(emb, role="query", config_path=None, clock=time.monotonic, opener=None, host=None, spawner=None):
    """四处造向量客户端的地方收成这一处。managed：ManagedClient；否则照旧（HttpEmbeddingClient，查询路径外面加断路器）。"""
    emb = emb or {}
    if emb.get("managed"):
        if not emb.get("engine_dir") or not emb.get("identity"):
            raise ProviderError("自带向量引擎的配置里缺 engine_dir 或 identity")
        return ManagedClient(emb, role, host=host, opener=opener, spawner=spawner, config_path=config_path)
    url = emb.get("base_url")
    if not url or not emb.get("model"):
        return None
    if not VX.is_local_url(url) and not emb.get("allow_remote"):
        raise ProviderError("向量服务地址不在本机，而且没有许可远端（allow_remote）")
    dim = (emb.get("identity") or {}).get("dimension")
    key = (os.environ.get(emb["api_key_env"]) or None) if emb.get("api_key_env") else None
    if role == "query":
        inner = VX.HttpEmbeddingClient(url, emb["model"], api_key=key, timeout=emb.get("timeout_query", 5), dimension=dim, opener=opener)
        return VX.GuardedClient(inner, emb.get("breaker_seconds", 30), clock)
    return VX.HttpEmbeddingClient(url, emb["model"], api_key=key, timeout=emb.get("timeout_worker", 60), dimension=dim, opener=opener)


# ---------------------------------------------------------------- 守护者
class _Drain(threading.Thread):
    """在内存里有界地读 llama-server 的标准错误：只提取固定类别，其余一概丢弃，不落盘、不转发。"""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.flags = set()

    def run(self):
        try:
            for line in iter(lambda: self.stream.readline(4096), b""):
                if RE_ARGS_REJECTED.search(line):
                    self.flags.add("args_rejected")
                elif RE_PORT_BUSY.search(line):
                    self.flags.add("port_in_use")
                elif RE_MODEL_LOAD.search(line):
                    self.flags.add("model_load_failed")
        except (OSError, ValueError):
            pass


class Supervisor:
    """看管一个 llama-server。run() 返回结束原因的类别。所有时间走 host（单调时钟算间隔，墙钟只用来写给人看的时间和跨进程的冷却）。"""

    def __init__(self, paths, pins=None, host=None, opener=None, cfg=None, args_table=None, env_extra=None):
        self.paths, self.pins = paths, EP.get_pins(pins)
        self.host = host or Host()
        self.opener = opener or LoopbackOpener()
        self.cfg = dict(CT.ENGINE_DEFAULTS, **(cfg or {}))
        self.args_table = args_table if args_table is not None else CT.ENGINE_ARGS
        self.env_extra = env_extra or {}
        self.sid = None
        self.child = None
        self.drain = None
        self.run_id = None
        self.consecutive = 0
        self.restarts = 0
        self.model_stat = None
        self.last_error = None
        self.ready_mono = None
        self._seen_inflight = {}
        self.last_act = None
        self.idle_since = None
        self.last_control = None

    # ---- 状态（只动自己这一轮的文件）
    def _publish(self, **kw):
        cur = EP.read_json(self.paths.state, {}) or {}
        if cur.get("supervisor_id") not in (None, self.sid):
            return
        cur.update(self.base_state)
        cur.update(kw)
        EP.write_json_atomic(self.paths.state, cur)

    def _log(self, event, **kv):
        log_event(self.paths, self.host, event, **kv)

    def _last_exit(self, reason, code=None):
        EP.write_json_atomic(self.paths.last_exit, {"at": iso(self.host.time()), "reason": reason, "code": code, "supervisor_id": self.sid})

    # ---- 入口
    def run(self):
        with EP.flock(self.paths.serve_lock, blocking=True, timeout=self.cfg.get("lock_wait_seconds", 0.5), sleep=self.host.sleep, mono=self.host.mono) as got:   # 别人正好在问「有没有人持锁」时（共享锁，微秒级）重试一小会儿；真有守护者持着就还是很快退出
            if not got:
                return "busy"
            self.sid = uuid.uuid4().hex
            self.start_wall = self.host.time()
            with contextlib.suppress(OSError):
                os.unlink(self.paths.state)                                        # 我们持着锁：以前那一轮肯定已经死了，它留下的状态文件是过期的
            self.base_state = {"schema": 1, "supervisor_id": self.sid, "supervisor_pid": os.getpid(), "supervisor_start": self.host.proc_start(os.getpid()),
                               "started_at": iso(self.host.time())}
            try:
                return self._main()
            except BaseException as e:                                                   # noqa: BLE001  看不明白的异常：杀子进程、记类别、退出；不吞
                self._kill_child()
                self._log("supervisor_error", error=type(e).__name__)
                self._last_exit("supervisor_error", type(e).__name__)
                self._publish(state="failed", last_error="supervisor_error", port=None, child_pid=None)
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                return "supervisor_error"
            finally:
                self._cleanup()

    def _cleanup(self):
        self._kill_child()
        cur = EP.read_json(self.paths.state, {}) or {}
        if cur.get("supervisor_id") == self.sid:
            if cur.get("state") not in ("failed",):
                with contextlib.suppress(OSError):
                    os.unlink(self.paths.state)
        for n in os.listdir(self.paths.run) if os.path.isdir(self.paths.run) else []:
            if n.startswith("key-"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.paths.run, n))

    def _main(self):
        if lifecycle_state(self.paths) != "active":
            self._last_exit("disabled")
            return "disabled"
        cool, _b = _backoff_active(self.paths, self.host.time())                     # 持锁以后重读冷却：冷却里不起（--reverify 会先清掉它）
        if cool:
            self._last_exit("cooldown")
            return "cooldown"
        if self._stop_requested():
            self._last_exit("stopped")
            return "stopped"
        self._reap_orphans()
        self._publish(state="starting", port=None, child_pid=None, run_id=None, restarts=0, last_error=None)
        total = 0
        while True:
            ok, code, retriable = self._launch_once()
            if ok:
                kind, code, retriable = self._monitor()
                if kind in ("idle", "stop"):
                    self._last_exit("idle_exit" if kind == "idle" else "stopped")
                    self._log(kind)
                    return kind
            self._kill_child()
            self.last_error = code
            if code == "stop_requested":
                self._last_exit("stopped")
                return "stop"
            total += 1
            self.consecutive += 1
            self._log("failure", code=code, consecutive=self.consecutive, total=total)
            if not retriable:
                prior = (EP.read_json(self.paths.backoff, {}) or {}).get("failures", 0)
                EP.write_json_atomic(self.paths.backoff, {"failures": prior + 1, "next_try_at": self.host.time() + 365 * 86400, "blocked": True, "last_error": code})   # 要人处理：只有 --reverify 才解除
                self._publish(state="failed", last_error=code, port=None, child_pid=None)
                self._last_exit("failed", code)
                return code
            if self.consecutive >= self.cfg["max_consecutive_failures"] or total >= self.cfg["max_total_restarts"]:
                prior = (EP.read_json(self.paths.backoff, {}) or {}).get("failures", 0)
                cool = self.cfg["cooldown_seconds"][min(prior, len(self.cfg["cooldown_seconds"]) - 1)]
                EP.write_json_atomic(self.paths.backoff, {"failures": prior + 1, "next_try_at": self.host.time() + cool, "last_error": code, "cooldown_seconds": cool})
                self._publish(state="failed", last_error=code, port=None, child_pid=None)
                self._last_exit("crash_giveup", code)
                return "crash_giveup"
            self.restarts += 1
            delay = self.cfg["restart_delays"][min(self.consecutive - 1, len(self.cfg["restart_delays"]) - 1)]
            self._publish(state="starting", last_error=code, port=None, child_pid=None, restarts=self.restarts)
            end = self.host.mono() + delay
            while self.host.mono() < end:
                if self._stop_requested():
                    self._last_exit("stopped")
                    return "stop"
                self.host.sleep(min(0.1, self.cfg["poll_seconds"]))

    # ---- 控制
    def _stop_requested(self):
        if lifecycle_state(self.paths) != "active":                                   # 正在移除／已停用：不再运行
            return True
        c = EP.read_json(self.paths.control)
        if not isinstance(c, dict) or c.get("cmd") != "stop" or c.get("id") == self.last_control:
            return False
        if c.get("target") not in ("*", self.sid):
            return False
        if not isinstance(c.get("at"), (int, float)) or c["at"] < self.start_wall:    # 本轮启动之前写的停止指令不认（陈旧的指令不能停新一轮）
            return False
        self.last_control = c.get("id")
        return True

    # ---- 孤儿
    def _reap_orphans(self):
        """上一轮守护者留下的子进程：只认「我们登记过的启动代号＋钉住的程序路径＋当前用户」，请它结束并确认退了才删登记（settle_children）。
        没退干净／读不到进程列表：登记留着、状态里标出来，不假装收拾完了。"""
        r = settle_children(self.paths, self.pins, self.host, self.cfg["kill_grace_seconds"], report_suspects=True)
        if r["reason"] == "process_list_unreadable":
            self._publish(orphan_check="unknown")
        elif not r["ok"]:
            self._publish(orphan_check="unconfirmed", orphan_reason=r["reason"])
            self._log("orphan_unconfirmed", reason=r["reason"], n=len(r["pending"]))
        if r["suspects"]:
            self._publish(orphan_suspects=r["suspects"][:5])
            self._log("orphan_suspects", n=len(r["suspects"]))

    # ---- 一次启动
    def _launch_once(self):
        a = EP.asset(self.pins)
        rc = EP.read_receipt(self.paths)
        pk = EP.installed_package(self.paths, self.pins, hash_files=True)             # 整包核：每次启动核清单里每个文件、不许有清单外的文件
        if not pk:
            return False, "package_changed", False
        model = rc.get("model") or {}
        acc = EP.acceptable_model_shas(self.pins)
        if model.get("sha256") not in acc or not model.get("path"):
            return False, "model_file_missing", False
        bad = EP.verify_model_file(model["path"], model["sha256"])
        if bad:
            return False, bad, False
        self.model_stat = EP.model_file_stat(model["path"])
        rdig = EP.recipe_digest(self.args_table)
        self.base_state.update(model_file_sha256=model["sha256"], runtime_version=EP.expected_runtime_version(self.pins, self.args_table), context_length=CT.ENGINE_CTX,
                               package_digest=pk["pkg_digest"], recipe_digest=rdig)
        # 端口、启动代号、本轮凭据
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        self.run_id = uuid.uuid4().hex
        alias = "yuanwei-" + self.run_id
        for n in os.listdir(self.paths.run):
            if n.startswith("key-"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.paths.run, n))
        keyfile = os.path.join(self.paths.run, "key-" + self.run_id)
        key = secrets.token_hex(32)
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        argv = [pk["binary"]]
        fill = {"model_path": model["path"], "port": str(port), "alias": alias, "keyfile": keyfile}
        for _n, frag, _d in self.args_table:
            argv += [x.format(**fill) if "{" in x else x for x in frag]
        ch = EP.read_json(self.paths.children, {}) or {}
        ids = (ch.get("run_ids") or [])[-19:] + [self.run_id]
        EP.write_json_atomic(self.paths.children, {"run_ids": ids})                   # 先登记再 Popen：Popen 成功后守护者立刻被杀，新守护者也认得这个启动代号
        t0 = self.host.mono()
        try:
            self.child = self.host.popen(argv, cwd=self.paths.run, env=scrubbed_env(self.env_extra), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except PermissionError:
            return False, "not_executable", False
        except OSError:
            return False, "not_executable", False
        self.drain = _Drain(self.child.stderr)
        self.drain.start()
        self._publish(state="starting", run_id=self.run_id, alias=alias, port=port, child_pid=self.child.pid, child_start=self.host.proc_start(self.child.pid), key_file=keyfile,
                      ready_at=None, draining=False)
        # 等就绪：/health 返回 200，并且 /v1/models（带凭据）说的名字就是本轮的启动代号
        end = t0 + self.cfg["ready_timeout_seconds"]
        while True:
            rcd = self.child.poll()
            if rcd is not None:
                return self._classify_exit(rcd, self.host.mono() - t0, started=False)
            if self._stop_requested():
                return False, "stop_requested", True
            code, _js = http_get(self.opener, "http://127.0.0.1:%d/health" % port)
            if code == 200:
                c2, js = http_get(self.opener, "http://127.0.0.1:%d/v1/models" % port, key=key)
                ids = [m.get("id") for m in ((js or {}).get("data") or []) if isinstance(m, dict)]
                if c2 == 200 and alias in ids:
                    break
                if c2 in (200, 401, 403):
                    return False, "identity_mismatch", False
            if self.host.mono() > end:
                return False, "health_timeout", True
            self.host.sleep(0.1)
        self.ready_mono = self.host.mono()
        self.idle_since = self.ready_mono
        self.last_act = None
        self._publish(state="ready", ready_at=iso(self.host.time()), last_error=None)
        self._log("ready", run_id=self.run_id, port=port)
        return True, None, True

    def _classify_exit(self, rc, elapsed, started):
        flags = self.drain.flags if self.drain else set()
        if "args_rejected" in flags:
            return False, "args_rejected", False
        if "port_in_use" in flags:
            return False, "port_in_use", True
        if rc == -signal.SIGKILL and elapsed < 3.0 and not flags:
            return False, "startup_killed_unknown", False                  # 被 SIGKILL 且没有任何输出：原因不明，不断言是安全设置
        if "model_load_failed" in flags:
            return False, "model_load_failed", True
        return False, ("signal_%d" % -rc) if rc < 0 else ("exit_%d" % rc), True

    def _kill_child(self):
        c = self.child
        if c is None:
            return
        self.child = None
        if c.poll() is None:
            with contextlib.suppress(OSError):
                c.terminate()
            end = self.host.mono() + self.cfg["kill_grace_seconds"]
            while c.poll() is None and self.host.mono() < end:
                self.host.sleep(0.05)
            if c.poll() is None:
                with contextlib.suppress(OSError):
                    c.kill()
                with contextlib.suppress(Exception):
                    c.wait(timeout=5)
        else:
            with contextlib.suppress(Exception):
                c.wait(timeout=1)
        if self.drain is not None:
            self.drain.join(timeout=1)
        ch = EP.read_json(self.paths.children, {}) or {}
        if self.run_id and self.run_id in (ch.get("run_ids") or []) and c.poll() is not None:
            ch["run_ids"] = [r for r in ch["run_ids"] if r != self.run_id]
            EP.write_json_atomic(self.paths.children, ch)

    # ---- 看管
    def _inflight_count(self):
        n, now = 0, self.host.mono()
        try:
            names = os.listdir(self.paths.inflight)
        except OSError:
            return 0
        live = set()
        for name in names:
            m = re.match(r"^(\d+)-[0-9a-f]+$", name)
            p = os.path.join(self.paths.inflight, name)
            if not m:
                continue
            first = self._seen_inflight.setdefault(name, now)
            if not self.host.pid_alive(int(m.group(1))) or now - first > self.cfg["request_max_seconds"]:
                with contextlib.suppress(OSError):
                    os.unlink(p)                                               # 客户端死了或太久没完：清掉，不让它永远拖着不退出
                continue
            live.add(name)
            n += 1
        for k in list(self._seen_inflight):
            if k not in live:
                del self._seen_inflight[k]
        return n

    def _idle_ready_to_exit(self):
        try:
            act = open(self.paths.activity).read()
        except OSError:
            act = None
        if act != self.last_act:
            self.last_act, self.idle_since = act, self.host.mono()
        if self._inflight_count() > 0:
            self.idle_since = self.host.mono()
            return False
        if self.host.mono() - self.idle_since < self.cfg["idle_exit_seconds"]:
            return False
        with EP.flock(self.paths.admit_lock, blocking=True, timeout=2, sleep=self.host.sleep, mono=self.host.mono) as got:
            if not got:
                return False
            try:
                act2 = open(self.paths.activity).read()
            except OSError:
                act2 = None
            if act2 != act or self._inflight_count() > 0:
                self.last_act, self.idle_since = act2, self.host.mono()
                return False
            self._publish(draining=True)                                       # 在锁里宣布：之后进来的客户端看到 draining 就不用这个实例
            return True

    def _monitor(self):
        """返回 (kind, code, retriable)：kind＝idle｜stop｜crash。"""
        while True:
            self.host.sleep(self.cfg["poll_seconds"])
            if self._stop_requested():
                return "stop", None, False
            rcd = self.child.poll()
            if rcd is not None:
                _k, code, ret = self._classify_exit(rcd, 999.0, started=True)
                return "crash", code, ret
            try:
                if self.model_stat is not None and EP.model_file_stat((EP.read_receipt(self.paths).get("model") or {})["path"]) != self.model_stat:
                    return "crash", "model_file_changed", False                # 运行中外部文件被替换：不再接受这个实例的结果，要求重核
            except (OSError, KeyError):
                return "crash", "model_file_missing", False
            if self.consecutive and self.host.mono() - self.ready_mono >= self.cfg["stable_seconds"]:
                self.consecutive = 0                                           # 稳定跑满：清零；顺手清冷却记录
                with contextlib.suppress(OSError):
                    os.unlink(self.paths.backoff)
            if self._idle_ready_to_exit():
                return "idle", None, False


# ---------------------------------------------------------------- 核验（向导和 vectors setup --reverify 用）
def verify_managed(paths, pins=None, host=None, spawner=None, opener=None, wait=60.0, expected_dim=CT.EMBED_MODEL_DIM, emb_extra=None):
    """启动（或沿用）引擎，算几句探针句：维度、确定性、相近句比无关句近、范数。返回 {"ok","reason","identity","weak":False,"probe","dimension"}。
    探针只证明基本接口和很小范围的行为；不等于强身份来源证明，也不等于真实检索一致（那是一致性核对工具的事）。"""
    from . import embedsetup as ES
    pins = EP.get_pins(pins)
    r = {"ok": False, "reason": None, "identity": None, "weak": False, "probe": None, "dimension": None}
    rc = EP.read_receipt(paths)
    model, pk = rc.get("model") or {}, EP.installed_package(paths, pins)
    if not pk or not model.get("sha256"):
        r["reason"] = "not_installed"
        return r
    expect = {"model_file_sha256": model["sha256"], "runtime_version": EP.expected_runtime_version(pins), "context_length": CT.ENGINE_CTX}
    probe_emb = dict(emb_extra or {}, engine_dir=paths.root, identity=dict(expect, dimension=None), managed=True)
    cl = ManagedClient(probe_emb, "worker", host=host, opener=opener, spawner=spawner)
    cl.wait = wait
    try:
        v3 = cl.embed_batch([ES.SENT_A, ES.SENT_B, ES.SENT_C])
        v1 = cl.embed_batch([ES.SENT_A])
        for v in v3 + v1:
            VX.check_vector(v, None)
    except EngineDown as e:
        r["reason"] = e.reason or "service_down"
        return r
    except ProviderError:
        r["reason"] = "bad_response"
        return r
    dim = len(v3[0])
    if any(len(v) != dim for v in v3 + v1):
        r["reason"] = "dimension"
        return r
    if expected_dim is not None and dim != expected_dim:
        r["reason"], r["dimension"] = "dimension", dim
        return r
    r["dimension"] = dim
    if ES._cos(v3[0], v1[0]) < 0.9999:
        r["reason"] = "nondeterministic"
        return r
    if not ES._cos(v3[0], v3[1]) > ES._cos(v3[0], v3[2]):
        r["reason"] = "semantic"
        return r
    import math
    norm = math.sqrt(sum(x * x for x in v3[0]))
    ident = EP.build_identity(pins["engine"]["tag"], EP.package_digest(EP.asset(pins)["manifest"]), EP.recipe_digest(), model["sha256"], dim, "l2" if abs(norm - 1.0) < 1e-3 else "none")
    try:
        VX.identity_fp(ident)
    except Exception:                                                                # noqa: BLE001
        r["reason"] = "no_identity"
        return r
    r.update(ok=True, identity=ident, probe=[list(v3[0]), list(v3[1])])
    return r


# ---------------------------------------------------------------- 状态（只读：不启动、不续命、不修复）
HINT_KILLED = ("向量程序一启动就被系统结束了，原因不明（可能是系统的安全检查，也可能是别的）。可以到「系统设置 → 隐私与安全性」看有没有关于 llama-server 的提示，"
               "有的话按 Apple 的办法处理；也可以改用 LM Studio（yuanwei vectors setup）。原委不会去改系统的安全设置或程序的签名。处理完以后运行 yuanwei vectors setup --reverify。")


def engine_status(paths, pins=None, host=None, platform_supported=None):
    host = host or Host()
    pins = EP.get_pins(pins)
    out = {"managed": True, "platform_supported": platform_supported, "dir": paths.root}
    try:
        rc = EP.read_receipt(paths)
        pk = EP.installed_package(paths, pins, hash_files=False) if not EP.pins_problems(pins, need_model=False) else None
        m = rc.get("model") or {}
        out["installed"] = {"program": bool(pk), "version": (pins.get("engine") or {}).get("tag"), "model": bool(m.get("path")) and os.path.exists(m.get("path") or ""),
                            "model_source": m.get("source"), "model_dir_name": os.path.basename(os.path.dirname(m["path"])) if m.get("path") else None,
                            "model_wording": EP.model_wording(m["sha256"], pins) if m.get("sha256") else None,
                            "model_label": EP.consistency_label(m["sha256"], pins) if m.get("sha256") else None,
                            "consistency_level": EP.consistency_state(m["sha256"], pins)["level"] if m.get("sha256") else None, "bytes_owned": owned_bytes(paths)}
        running = EP.lock_is_held(paths.serve_lock)
        st = EP.read_json(paths.state) if running else None
        out["process"] = {"running": running, "state": (st or {}).get("state", "unknown" if running else "stopped"), "since": (st or {}).get("started_at"), "restarts": (st or {}).get("restarts"),
                          "port": (st or {}).get("port") if running else None, "draining": bool((st or {}).get("draining"))}
        le = EP.read_json(paths.last_exit)
        out["last_exit"] = {"at": le.get("at"), "reason": le.get("reason")} if le else None
        b = EP.read_json(paths.backoff)
        out["backoff"] = {"next_try_at": iso(b["next_try_at"]) if b and b.get("next_try_at") else None}
        lerr = (st or {}).get("last_error") or ((le or {}).get("code") if le else None) or (b or {}).get("last_error")
        out["last_error"] = lerr
        out["orphan_suspects"] = (st or {}).get("orphan_suspects") or []
        out["lifecycle"] = lifecycle_state(paths)
        out["users"] = list_users(paths)
        out["hint"] = HINT_KILLED if lerr == "startup_killed_unknown" else (
            "向量程序不认某个启动参数（这一版的参数名可能变了）：这是原委要跟进的事，先用关键词。" if lerr == "args_rejected" else None)
    except Exception as e:                                                           # noqa: BLE001  读不出来要报出来，不当成没问题
        out["process"] = {"running": None, "state": "unknown", "error": type(e).__name__}
    return out


# ---------------------------------------------------------------- yuanwei vectors serve（守护者入口；内部用）
def serve_main(engine_dir, idle_exit=None, ready_timeout=None, pins=None, host=None):
    cfg = {}
    if idle_exit is not None:
        cfg["idle_exit_seconds"] = idle_exit
    if ready_timeout is not None:
        cfg["ready_timeout_seconds"] = ready_timeout
    paths = EP.Paths(engine_dir)
    paths.ensure_dirs()
    return Supervisor(paths, pins=pins, host=host, cfg=cfg).run()
