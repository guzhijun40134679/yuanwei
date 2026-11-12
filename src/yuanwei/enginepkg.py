"""自带向量引擎：程序和模型的「准备」——钉住的值、下载、安全解压、整包核验、复用本机现成的模型文件。

信任根是代码里钉住的表（catalog.PINS），不是磁盘上的任何收据（engine.json 只是安装收据，不自证真实性）。
* 钉住的值没核过（verified 为假、或必填的格子是 None）：下载和启动一律被拒。没有任何环境变量或参数能放过；测试靠注入假的钉住表（pins=）和假下载器（fetcher=）。
* 下载：只用 https；跳转每一步重新核 https、最多 5 次；下载过程中就限字节数和总时长；失败一律删掉重来（第一版不做断点续传）；最后核整个文件的 SHA-256。
  用户给的下载地址（--engine-url／--model-url）只换下载位置，摘要永远是钉住的那一个。
* 解压：全新的私有暂存目录；拒绝绝对路径、.. 、重复和大小写冲突、硬链接、设备／FIFO；符号链接只放行清单里写了的、且目标在包内；
  限制文件数、单文件大小、总字节数（按实际写出的字节算，不信压缩包头）；权限由程序设定；解完核「整棵树恰好等于清单」。
* 整包核验：每次启动前核清单里每个文件的 SHA-256、每个链接的目标，并且不许有清单外的文件。
* 错误只有类别（EngineError.code），没有对方的正文、没有地址里的签名串。
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import catalog as CT

PLATFORM_KEY = "darwin-arm64"
CHUNK = 1 << 20


class EngineError(Exception):
    """code 是类别（pins_unverified、not_https、sha256_mismatch、archive_unsafe……）；str(e) 也只有类别。"""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path, chunk=CHUNK):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 目录
def default_engine_dir(env=None):
    env = os.environ if env is None else env
    return env.get("YUANWEI_ENGINE_DIR") or os.path.join(os.path.expanduser("~"), ".yuanwei", "engine")


class Paths:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.bin = os.path.join(self.root, "bin")
        self.models = os.path.join(self.root, "models")
        self.staging = os.path.join(self.root, "staging")
        self.run = os.path.join(self.root, "run")
        self.inflight = os.path.join(self.run, "inflight")
        self.logs = os.path.join(self.root, "logs")
        self.receipt = os.path.join(self.root, "engine.json")
        self.serve_lock = os.path.join(self.run, "serve.lock")
        self.lifecycle_lock = os.path.join(self.run, "lifecycle.lock")
        self.admit_lock = os.path.join(self.run, "admit.lock")
        self.state = os.path.join(self.run, "server.json")
        self.control = os.path.join(self.run, "control.json")
        self.backoff = os.path.join(self.run, "backoff.json")
        self.last_exit = os.path.join(self.run, "last_exit.json")
        self.lifecycle = os.path.join(self.run, "lifecycle.json")
        self.users = os.path.join(self.run, "users.json")
        self.children = os.path.join(self.run, "children.json")
        self.activity = os.path.join(self.run, "activity")
        self.supervisor_log = os.path.join(self.logs, "supervisor.log")

    def ensure_dirs(self):
        for d in (self.root, self.bin, self.models, self.staging, self.run, self.inflight, self.logs):
            os.makedirs(d, mode=0o700, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass

    def package_dir(self, tag, pkg_digest):
        return os.path.join(self.bin, "%s-%s" % (re.sub(r"[^A-Za-z0-9._-]", "_", str(tag)), pkg_digest[:16]))


def write_json_atomic(path, obj, mode=0o600):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def flock(path, blocking=False, timeout=0.0, sleep=time.sleep, mono=time.monotonic, shared=False):
    """文件锁（flock）。锁文件本身永远不删、不换（调用方不许删它）。拿不到时 yield False。进程一死锁就自动没了。
    shared＝共享锁：只用来「问有没有人持着独占锁」（lock_is_held）；多个人同时问不会互相挡住。"""
    import fcntl
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    f = open(path, "a+")
    got = False
    try:
        end = mono() + timeout
        while True:
            try:
                fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if not blocking or mono() >= end:
                    break
                sleep(0.02)
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def lock_is_held(path):
    """只读地问：有没有人持（独占）锁。没有锁文件＝没人（不顺手建一个）。
    问的人用共享锁：两个人同时问不会互相挡住、让对方以为「有人持着」（用独占锁问会这样：误报「在跑」）；
    守护者拿独占锁时遇到别人正好在问，要重试一小会儿（见 Supervisor.run），不能因为一次问讯就当成「已经有守护者」而退出。"""
    if not os.path.exists(path):
        return False
    with flock(path, shared=True) as got:
        return not got


# ---------------------------------------------------------------- 钉住的值
def get_pins(pins=None):
    return pins if pins is not None else CT.PINS


def asset(pins, platform_key=PLATFORM_KEY):
    return ((get_pins(pins).get("engine") or {}).get("assets") or {}).get(platform_key)


def pins_problems(pins=None, need_engine=True, need_model=True, platform_key=PLATFORM_KEY):
    """没核过的、缺的格子（类别列表）；空表示可以用。凡是没核过就不放行，没有例外。"""
    pins = get_pins(pins)
    out = []
    if need_engine:
        eng = pins.get("engine") or {}
        a = asset(pins, platform_key) or {}
        if not eng.get("verified"):
            out.append("engine.verified")
        for k in CT.PIN_REQUIRED_ENGINE:
            v = a.get(k) if k not in ("tag",) else eng.get("tag")
            if v in (None, "", [], {}):
                out.append("engine." + k)
        if a.get("url") and not str(a["url"]).startswith("https://"):
            out.append("engine.url_not_https")
    if need_model:
        ok = False
        for m in pins.get("models") or []:
            if m.get("verified") and all(m.get(k) not in (None, "") for k in CT.PIN_REQUIRED_MODEL):
                ok = True
                for s in m.get("sources") or []:
                    if s.get("url") and not str(s["url"]).startswith("https://"):
                        out.append("model.url_not_https")
        if not ok:
            out.append("model.verified")
    return out


def model_records(pins=None):
    return [m for m in (get_pins(pins).get("models") or [])]


def acceptable_model_shas(pins=None):
    """可以认的模型文件摘要（可复用的和可下载的都算）。"""
    out = {}
    for m in model_records(pins):
        if m.get("sha256"):
            out.setdefault(m["sha256"], m)                              # 两条登记摘要相同（官方文件就是测过的那个）：先登记的（可复用的）算数，本机已有的文件按「同一个文件」说
    return out


def consistency_state(sha, pins=None, args_table=None):
    """这个模型文件配这一版引擎（版本、运行包摘要）和这套启动配方的一致性状态：{"level": strong｜checked_not_strong｜unchecked, "record", "text"}。
    表按「模型摘要｜引擎版本｜运行包摘要｜配方摘要」登记：换了任何一项都不沿用，回到 unchecked。取不到（钉住表没核过、没有这个文件的摘要）也是 unchecked。"""
    pins = get_pins(pins)
    a = asset(pins)
    if not sha or not a or not a.get("manifest") or not (pins.get("engine") or {}).get("tag"):
        return {"level": CT.UNCHECKED, "record": None, "text": CT.LABEL_UNCHECKED}
    return CT.engine_consistency_state(sha, pins["engine"]["tag"], package_digest(a["manifest"]), recipe_digest(args_table))


def consistency_label(sha, pins=None, args_table=None):
    """界面标签：只有强档过了才是「测过」；核过但没过强档写专门的一档；没核过写「一致性还没在真机上核」。一个文件过了不连带别的文件，一个引擎版本过了不连带别的版本。"""
    return consistency_state(sha, pins, args_table)["text"]


def model_wording(sha, pins=None):
    """界面上怎么称呼这个文件：摘要和测过的那个相同才能写「同一个模型文件」。"""
    rec = acceptable_model_shas(pins).get(sha)
    if (rec and rec["role"] == "reusable") or sha == CT.TESTED_EMBED_IDENTITY["model_file_sha256"]:
        return "同一个模型文件（SHA-256 和测过的相同）"
    return "官方发布的模型文件（和测过的不是同一个文件）"


# ---------------------------------------------------------------- 下载
class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise EngineError("not_https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """真下载器。测试里整个换成假的（有 fetch(url, dest, expect_sha256, max_bytes, max_seconds, progress) 就行），或者给 opener 换成进程内的假响应。"""

    def __init__(self, opener=None, mono=time.monotonic):
        self._opener = opener
        self.mono = mono

    def _open(self, req, timeout):
        if self._opener is not None:
            return self._opener(req, timeout=timeout)
        return urllib.request.build_opener(HTTPSOnlyRedirect()).open(req, timeout=timeout)

    def fetch(self, url, dest, expect_sha256, max_bytes, max_seconds, progress=None):
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            raise EngineError("not_https")
        t0 = self.mono()
        h = hashlib.sha256()
        n = 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                resp = self._open(urllib.request.Request(url, headers={"User-Agent": "yuanwei-engine"}), timeout=30)
            except EngineError:
                raise
            except urllib.error.HTTPError as e:
                raise EngineError("http_%d" % e.code)
            except (urllib.error.URLError, OSError, ValueError):
                raise EngineError("connect_failed")
            with resp:
                final = getattr(resp, "url", None) or getattr(resp, "geturl", lambda: None)()
                if final and not str(final).lower().startswith("https://"):
                    raise EngineError("not_https")
                try:
                    cl = int(resp.headers.get("Content-Length")) if getattr(resp, "headers", None) and resp.headers.get("Content-Length") else None
                except (TypeError, ValueError):
                    cl = None
                if cl is not None and cl > max_bytes:
                    raise EngineError("too_large")
                with os.fdopen(fd, "wb", closefd=False) as out:
                    while True:
                        if self.mono() - t0 > max_seconds:
                            raise EngineError("timeout")
                        try:
                            b = resp.read(CHUNK)
                        except (urllib.error.URLError, OSError):
                            raise EngineError("connect_failed")
                        if not b:
                            break
                        n += len(b)
                        if n > max_bytes:
                            raise EngineError("too_large")
                        h.update(b)
                        out.write(b)
                        if progress:
                            progress(n, cl)
            if h.hexdigest() != expect_sha256:
                raise EngineError("sha256_mismatch")
            return n
        except BaseException:
            os.close(fd)
            fd = None
            with contextlib.suppress(OSError):
                os.unlink(dest)
            raise
        finally:
            if fd is not None:
                os.close(fd)


# ---------------------------------------------------------------- 清单、安全解压、整包核验
def package_digest(manifest):
    return hashlib.sha256(canon(sorted(manifest, key=lambda e: e["path"])).encode("utf-8")).hexdigest()


def _clean_name(name):
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise EngineError("archive_unsafe", "bad_name")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise EngineError("archive_unsafe", "absolute")
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise EngineError("archive_unsafe", "dotdot")
    return "/".join(parts)


def _within(root, rel_target, from_rel):
    base = os.path.dirname(from_rel)
    t = os.path.normpath(os.path.join(base, rel_target))
    return None if t.startswith("..") or os.path.isabs(t) else t


def _members(archive_path, kind):
    """统一成 (name, type, size, mode, opener, linkname)。type：file｜dir｜symlink；别的（硬链接、设备、FIFO）直接抛 archive_unsafe。"""
    out = []
    if kind == "zip":
        z = zipfile.ZipFile(archive_path)
        for zi in z.infolist():
            mode = (zi.external_attr >> 16) & 0xFFFF
            if zi.is_dir():
                t = "dir"
            elif stat.S_ISLNK(mode):
                t = "symlink"
            elif stat.S_IFMT(mode) == 0 or stat.S_ISREG(mode):             # 没写文件类型位的按普通文件算（有些打包工具不写）
                t = "file"
            else:
                raise EngineError("archive_unsafe", "special")
            out.append((zi.filename, t, zi.file_size, mode, (lambda zi=zi: z.open(zi)), None))
        return out, z.close
    if kind == "tar.gz":
        tf = tarfile.open(archive_path, "r:gz")
        for ti in tf.getmembers():
            if ti.isdir():
                t = "dir"
            elif ti.issym():
                t = "symlink"
            elif ti.isreg():
                t = "file"
            else:
                raise EngineError("archive_unsafe", "special")           # 硬链接、设备、FIFO 一律拒
            out.append((ti.name, t, ti.size, ti.mode, (lambda ti=ti: tf.extractfile(ti)), ti.linkname if ti.issym() else None))
        return out, tf.close
    raise EngineError("archive_unsafe", "kind")


def safe_extract(archive_path, kind, dest_root, manifest, max_files, max_total_bytes, max_file_bytes=None):
    """把压缩包解到 dest_root（必须是刚建好的空目录）。解完核整棵树恰好等于清单。"""
    want = {e["path"]: e for e in manifest}
    limit_file = max_file_bytes or max_total_bytes
    members, closer = _members(archive_path, kind)
    try:
        if len(members) > max_files:
            raise EngineError("archive_unsafe", "too_many")
        seen, seen_fold, links, total = set(), set(), {}, 0
        for name, typ, size, mode, opener, linkname in members:
            rel = _clean_name(name)
            if rel in seen or rel.casefold() in seen_fold:
                raise EngineError("archive_unsafe", "duplicate")
            seen.add(rel)
            seen_fold.add(rel.casefold())
            parent = os.path.dirname(rel)
            while parent:                                                       # 不许穿过链接父目录写东西
                if parent in links:
                    raise EngineError("archive_unsafe", "through_symlink")
                parent = os.path.dirname(parent)
            dst = os.path.join(dest_root, rel)
            if typ == "dir":
                os.makedirs(dst, mode=0o700, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
            ent = want.get(rel)
            if ent is None:
                raise EngineError("archive_unsafe", "not_in_manifest")
            if typ == "symlink":
                if ent["type"] != "symlink":
                    raise EngineError("archive_unsafe", "type")
                if linkname is None:
                    with opener() as f:
                        linkname = f.read(4097).decode("utf-8", "replace")
                if linkname != ent.get("target") or _within(dest_root, linkname, rel) is None:
                    raise EngineError("archive_unsafe", "link_target")
                os.symlink(linkname, dst)
                links[rel] = linkname
                continue
            if ent["type"] != "file":
                raise EngineError("archive_unsafe", "type")
            if size > limit_file:
                raise EngineError("archive_unsafe", "file_too_large")
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            got = 0
            try:
                with os.fdopen(fd, "wb") as out, opener() as src:
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        got += len(b)
                        total += len(b)
                        if got > limit_file or total > max_total_bytes:
                            raise EngineError("archive_unsafe", "too_large")
                        out.write(b)
            except EngineError:
                raise
            os.chmod(dst, 0o700 if ent.get("exec") else 0o600)                   # 权限由程序定，不照搬包里的
    finally:
        closer()
    problems = verify_tree(dest_root, manifest)
    if problems:
        raise EngineError("archive_unsafe", problems[0])


def verify_tree(root, manifest, hash_files=True):
    """整棵树核对清单。返回问题类别列表（空＝没问题）：missing、changed、extra、link_changed、not_regular、hardlinked。"""
    problems = []
    want = {e["path"]: e for e in manifest}
    for rel, e in want.items():
        p = os.path.join(root, rel)
        try:
            st = os.lstat(p)
        except OSError:
            problems.append("missing:" + rel)
            continue
        if e["type"] == "symlink":
            if not stat.S_ISLNK(st.st_mode) or os.readlink(p) != e.get("target"):
                problems.append("link_changed:" + rel)
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append("not_regular:" + rel)
            continue
        if st.st_nlink != 1:
            problems.append("hardlinked:" + rel)
        if hash_files and (st.st_size != e["size"] or sha256_file(p) != e["sha256"]):
            problems.append("changed:" + rel)
        elif not hash_files and st.st_size != e["size"]:
            problems.append("changed:" + rel)
    for dp, dns, fns in os.walk(root, followlinks=False):
        for n in fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
            rel = os.path.relpath(os.path.join(dp, n), root)
            if rel not in want:
                problems.append("extra:" + rel)
    return problems


# ---------------------------------------------------------------- 安装收据
def read_receipt(paths):
    return read_json(paths.receipt, {}) or {}


def write_receipt(paths, **parts):
    cur = read_receipt(paths)
    cur.update(parts)
    cur["schema"] = 1
    write_json_atomic(paths.receipt, cur)
    return cur


def installed_package(paths, pins=None, hash_files=True):
    """已装好的程序：钉住的清单对得上才算（不信收据）。返回 {"dir","binary","pkg_digest","tag"} 或 None。"""
    pins = get_pins(pins)
    a = asset(pins)
    if not a or pins_problems(pins, need_model=False):
        return None
    d = paths.package_dir(pins["engine"]["tag"], package_digest(a["manifest"]))
    if not os.path.isdir(d) or verify_tree(d, a["manifest"], hash_files=hash_files):
        return None
    return {"dir": d, "binary": os.path.join(d, a["binary"]), "pkg_digest": package_digest(a["manifest"]), "tag": pins["engine"]["tag"]}


def install_program(paths, pins=None, fetcher=None, url_override=None, progress=None, free_bytes=None, lifecycle_ok=None):
    """下载（或沿用）、核验、解压、整包核对、放到位。钉住的值没核过：pins_unverified。返回 installed_package 的结果。"""
    pins = get_pins(pins)
    bad = pins_problems(pins, need_model=False)
    if bad:
        raise EngineError("pins_unverified", ",".join(bad))
    paths.ensure_dirs()
    have = installed_package(paths, pins)
    if have:
        return have
    a = asset(pins)
    if free_bytes is not None and free_bytes < a["size"] + a["max_total_bytes"] + (200 << 20):
        raise EngineError("disk_full")
    fetcher = fetcher or Fetcher()
    stg = tempfile.mkdtemp(dir=paths.staging, prefix="p-")
    try:
        arc = os.path.join(stg, "archive")
        fetcher.fetch(url_override or a["url"], arc, a["sha256"], a["size"], CT.ENGINE_DEFAULTS["download_max_seconds"], progress)
        out = os.path.join(stg, "x")
        os.mkdir(out, 0o700)
        safe_extract(arc, a["kind"], out, a["manifest"], a["max_files"], a["max_total_bytes"])
        dest = paths.package_dir(pins["engine"]["tag"], package_digest(a["manifest"]))
        if os.path.exists(dest):
            if verify_tree(dest, a["manifest"]):
                shutil.rmtree(dest, ignore_errors=True)
            else:
                return installed_package(paths, pins)
        try:
            os.replace(out, dest)
        except OSError:                                                                  # 同时有别的安装先放好了：核它是好的就沿用，不是好的才报错
            if verify_tree(dest, a["manifest"]):
                raise EngineError("package_changed")
    finally:
        shutil.rmtree(stg, ignore_errors=True)
    pk = installed_package(paths, pins)
    if not pk:
        raise EngineError("package_changed")
    write_receipt(paths, package={"tag": pk["tag"], "pkg_digest": pk["pkg_digest"], "dir": pk["dir"]})
    return pk


# ---------------------------------------------------------------- 模型文件
DEFAULT_MODEL_DIRS = ("~/.lmstudio/models", "~/.cache/lm-studio/models", "~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B-GGUF", "~/.ollama/models/blobs")
OLLAMA_BLOB = re.compile(r"^sha256-([0-9a-f]{64})$")


def locate_model(pins=None, dirs=None, extra_file=None, hasher=sha256_file, max_depth=6):
    """在本机找现成的模型文件。只认 SHA-256 在钉住的表里的；名字像、大小像都不算。只读，不挪、不改。
    返回 {"found": {path, sha256, artifact_id, role}|None, "unreadable": [目录名], "hashed": n}。"""
    pins = get_pins(pins)
    acc = acceptable_model_shas(pins)
    sizes = {m["size"] for m in acc.values() if m.get("size")}
    out = {"found": None, "unreadable": [], "hashed": 0}

    def consider(path, sha_hint=None):
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if sha_hint is not None and sha_hint not in acc:
            return None
        if sha_hint is None:
            if sizes:
                if st.st_size not in sizes:
                    return None
            elif not re.search(r"qwen3-embedding.*0\.?6b", os.path.basename(path), re.I):
                return None
        out["hashed"] += 1
        try:
            sha = hasher(path)
        except OSError:
            return None
        if sha in acc:
            return {"path": os.path.abspath(path), "sha256": sha, "artifact_id": acc[sha]["id"], "role": acc[sha]["role"]}
        return None

    if extra_file:
        r = consider_file_strict(extra_file, acc, hasher)
        out["found"] = r
        return out
    for d in dirs if dirs is not None else DEFAULT_MODEL_DIRS:
        d = os.path.expanduser(d)
        if not os.path.exists(d):
            continue
        if not os.path.isdir(d) or not os.access(d, os.R_OK | os.X_OK):
            out["unreadable"].append(os.path.basename(d.rstrip("/")))
            continue
        base_depth = d.rstrip("/").count("/")
        for dp, dns, fns in os.walk(d, followlinks=False, onerror=lambda e, d=d: out["unreadable"].append(os.path.basename(d.rstrip("/")))):
            if dp.count("/") - base_depth >= max_depth:
                dns[:] = []
            for fn in sorted(fns):
                m = OLLAMA_BLOB.match(fn)
                if m:
                    r = consider(os.path.join(dp, fn), sha_hint=m.group(1))
                elif fn.lower().endswith(".gguf"):
                    r = consider(os.path.join(dp, fn))
                else:
                    r = None
                if r:
                    out["found"] = r
                    return out
    return out


def consider_file_strict(path, acc, hasher=sha256_file):
    """用户用 --model-file 明确给的文件：摘要不在钉住的表里就拒绝（model_file_sha_mismatch）。"""
    try:
        sha = hasher(path)
    except OSError:
        raise EngineError("model_file_unreadable")
    if sha not in acc:
        raise EngineError("model_file_sha_mismatch")
    return {"path": os.path.abspath(path), "sha256": sha, "artifact_id": acc[sha]["id"], "role": acc[sha]["role"]}


def download_model(paths, pins=None, fetcher=None, url_override=None, progress=None, free_bytes=None):
    """下载可以下载的那个模型文件（钉住的摘要）。返回 {path, sha256, artifact_id, role, owned: True}。"""
    pins = get_pins(pins)
    cand = [m for m in model_records(pins) if m.get("role") == "downloadable" and m.get("verified") and m.get("sha256") and m.get("size") and (m.get("sources") or url_override)]
    if not cand:
        raise EngineError("pins_unverified", "model.downloadable")
    m = cand[0]
    paths.ensure_dirs()
    if free_bytes is not None and free_bytes < int(m["size"] * 1.2) + (200 << 20):
        raise EngineError("disk_full")
    fetcher = fetcher or Fetcher()
    urls = [url_override] if url_override else [s["url"] for s in m["sources"] if s.get("url")]
    if not urls:
        raise EngineError("pins_unverified", "model.url")
    dest = os.path.join(paths.models, "%s-%s" % (m["sha256"][:12], os.path.basename(m["file_name"])))
    if os.path.exists(dest) and sha256_file(dest) == m["sha256"]:
        return {"path": dest, "sha256": m["sha256"], "artifact_id": m["id"], "role": m["role"], "owned": True}
    last = None
    for u in urls:
        stg = tempfile.mkdtemp(dir=paths.staging, prefix="m-")
        try:
            part = os.path.join(stg, "model.part")
            fetcher.fetch(u, part, m["sha256"], m["size"], CT.ENGINE_DEFAULTS["download_max_seconds"], progress)
            os.replace(part, dest)
            os.chmod(dest, 0o600)
            return {"path": dest, "sha256": m["sha256"], "artifact_id": m["id"], "role": m["role"], "owned": True}
        except EngineError as e:
            last = e
        finally:
            shutil.rmtree(stg, ignore_errors=True)
    raise last or EngineError("connect_failed")


def model_file_stat(path):
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def verify_model_file(path, expect_sha, hasher=sha256_file):
    """冷启动时核：整文件 SHA-256 等于钉住的、核前后文件身份一致。返回 None 表示通过，否则类别。"""
    try:
        a = model_file_stat(path)
    except OSError:
        return "model_file_missing"
    try:
        sha = hasher(path)
    except OSError:
        return "model_file_missing"
    try:
        b = model_file_stat(path)
    except OSError:
        return "model_file_missing"
    if a != b:
        return "model_file_changed"
    return None if sha == expect_sha else "model_file_changed"


# ---------------------------------------------------------------- 配方、身份
def recipe(args_table=None):
    """完整的启动配方（参数模板＋文字处理＋归一化）：有版本号；摘要折进编码身份。"""
    tab = args_table if args_table is not None else CT.ENGINE_ARGS
    return {"version": CT.RECIPE_VERSION, "args": [[n, list(f)] for n, f, _d in tab], "text": dict(CT.RECIPE_TEXT), "ctx": CT.ENGINE_CTX}


def recipe_digest(args_table=None):
    return hashlib.sha256(canon(recipe(args_table)).encode("utf-8")).hexdigest()


def build_identity(tag, pkg_digest, rdigest, model_sha, dimension, normalization):
    """十个字段一个都不加：池化折进 provider，量化折进 model，版本、运行包摘要、配方摘要折进 runtime_version。"""
    return {"provider": "llama.cpp-server;pooling=last", "model": "Qwen3-Embedding-0.6B-Q8_0", "model_file_sha256": model_sha,
            "runtime_version": "llama.cpp-server@%s;pkg=%s;recipe=%s" % (tag, pkg_digest[:16], rdigest[:16]), "context_length": CT.ENGINE_CTX,
            "dimension": dimension, "normalization": normalization, "prefix": "none", "template": "none", "text_processing": "verbatim"}


def expected_runtime_version(pins=None, args_table=None):
    pins = get_pins(pins)
    a = asset(pins)
    return "llama.cpp-server@%s;pkg=%s;recipe=%s" % (pins["engine"]["tag"], package_digest(a["manifest"])[:16], recipe_digest(args_table)[:16])
