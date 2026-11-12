"""自带向量引擎的测试小件：假的运行包（假 llama-server 脚本＋动态库＋链接）、假的钉住表、假下载（进程内的假响应，走真的 Fetcher 代码）、守护者的起停。绝不联网。"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

from yuanwei import enginepkg as EP
from yuanwei import enginerun as ER

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "engine_harness.py")
MODEL_BYTES = b"FAKE-GGUF-TESTED-" * 64
OFFICIAL_BYTES = b"FAKE-GGUF-OFFICIAL-" * 64
BIN_TEXT = "#!%s -B\nimport sys\nsys.path.insert(0, %r)\nimport fake_llama_server\nfake_llama_server.main()\n" % (sys.executable, HERE)
LIB_BYTES = b"FAKE-DYLIB" * 100


def sha(b):
    return hashlib.sha256(b).hexdigest()


def package_files(binary_text=BIN_TEXT):
    """{相对路径: (类型, 内容或目标, 可执行)}"""
    return {"build/bin/llama-server": ("file", binary_text.encode("utf-8"), True), "build/bin/libfake.dylib": ("file", LIB_BYTES, False),
            "build/bin/libfake.1.dylib": ("symlink", "libfake.dylib", False)}


def manifest_of(files):
    out = []
    for rel, (typ, content, ex) in sorted(files.items()):
        if typ == "file":
            out.append({"path": rel, "type": "file", "sha256": sha(content), "size": len(content), "exec": bool(ex)})
        else:
            out.append({"path": rel, "type": "symlink", "target": content})
    return out


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, (typ, content, ex) in files.items():
            zi = zipfile.ZipInfo(rel)
            if typ == "symlink":
                zi.external_attr = (0o120777 << 16)
                z.writestr(zi, content)
            else:
                zi.external_attr = ((0o100755 if ex else 0o100644) << 16)
                z.writestr(zi, content)
    return buf.getvalue()


def make_targz(files, extra=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for rel, (typ, content, ex) in files.items():
            if typ == "symlink":
                ti = tarfile.TarInfo(rel)
                ti.type, ti.linkname = tarfile.SYMTYPE, content
                t.addfile(ti)
            else:
                ti = tarfile.TarInfo(rel)
                ti.size, ti.mode = len(content), 0o755 if ex else 0o644
                t.addfile(ti, io.BytesIO(content))
        for ti, data in extra:
            t.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class FakeResp:
    def __init__(self, body, url="https://example.test/x", headers=None, chunk=None):
        self._b, self.url, self.headers = io.BytesIO(body), url, headers if headers is not None else {"Content-Length": str(len(body))}
        self.chunk = chunk

    def read(self, n=-1):
        return self._b.read(n if not self.chunk else min(n, self.chunk))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """假下载：{url: 字节}；opener(req, timeout) 返回假响应；记下每次请求的地址。"""

    def __init__(self, routes=None):
        self.routes, self.hits = dict(routes or {}), []

    def __call__(self, req, timeout=None):
        self.hits.append(req.full_url)
        if req.full_url not in self.routes:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
        v = self.routes[req.full_url]
        return v() if callable(v) else FakeResp(v, req.full_url)

    def fetcher(self, **kw):
        return EP.Fetcher(opener=self, **kw)


class Kit:
    """一套假的钉住表＋假压缩包＋假模型。"""

    def __init__(self, tmp_path, tag="bTEST", kind="zip", files=None, verified=True):
        self.files = files or package_files()
        self.manifest = manifest_of(self.files)
        self.archive = make_zip(self.files) if kind == "zip" else make_targz(self.files)
        self.url = "https://example.test/engine." + ("zip" if kind == "zip" else "tar.gz")
        self.model_url = "https://example.test/model-official.gguf"
        self.model_url2 = "https://example.test/model-official-2.gguf"
        self.tmp = tmp_path
        self.pins = {
            "engine": {"id": "llama.cpp-server", "tag": tag, "verified": verified, "license": "MIT", "release_page": "https://example.test/",
                       "assets": {"darwin-arm64": {"url": self.url, "kind": kind, "sha256": sha(self.archive), "size": len(self.archive), "max_total_bytes": 1 << 20, "max_files": 20,
                                                   "binary": "build/bin/llama-server", "manifest": self.manifest}}},
            "models": [{"id": "m@tested", "role": "reusable", "sha256": sha(MODEL_BYTES), "size": len(MODEL_BYTES), "file_name": "Qwen3-Embedding-0.6B-Q8_0.gguf", "sources": [], "verified": verified},
                       {"id": "m@official", "role": "downloadable", "sha256": sha(OFFICIAL_BYTES), "size": len(OFFICIAL_BYTES), "file_name": "Qwen3-Embedding-0.6B-Q8_0.gguf", "verified": verified,
                        "sources": [{"name": "a", "url": self.model_url}, {"name": "b", "url": self.model_url2}]}],
            "embedding_dim": 1024}
        self.net = FakeNet({self.url: self.archive, self.model_url: OFFICIAL_BYTES, self.model_url2: OFFICIAL_BYTES})
        self.pins_file = str(tmp_path / "pins.json")
        json.dump(self.pins, open(self.pins_file, "w"))

    def engine_dir(self, name="engine"):
        return str(self.tmp / name)

    def lmstudio_dir(self, name="lmstudio"):
        d = self.tmp / name / "models" / "Qwen" / "Qwen3-Embedding-0.6B-GGUF"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Qwen3-Embedding-0.6B-Q8_0.gguf").write_bytes(MODEL_BYTES)
        return str(self.tmp / name / "models")

    def install(self, engine_dir=None, model="reuse"):
        """装好程序和模型（直接调 enginepkg，不走向导）。返回 Paths。"""
        paths = EP.Paths(engine_dir or self.engine_dir())
        paths.ensure_dirs()
        EP.install_program(paths, self.pins, self.net.fetcher())
        if model == "reuse":
            d = self.lmstudio_dir()
            f = EP.locate_model(self.pins, dirs=[d])["found"]
            m = dict(f, source="reused", owned=False)
        else:
            m = dict(EP.download_model(paths, self.pins, self.net.fetcher()), source="downloaded")
        EP.write_receipt(paths, model=m)
        ER.set_lifecycle(paths, "active")
        return paths


def spawner_for(kit, engine_dir, cfg=None, child_env=None, procs=None):
    """假的「拉起守护者」：用测试入口脚本起一个真的守护者子进程（新会话）。procs 里记下它们。"""
    def go():
        env = ER.scrubbed_env({"YUANWEI_TEST_PINS": kit.pins_file, "YUANWEI_TEST_CFG": json.dumps(cfg or {}), "YUANWEI_TEST_CHILD_ENV": json.dumps(child_env or {})})
        p = subprocess.Popen([sys.executable, "-B", HARNESS, engine_dir], env=env, cwd=engine_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if procs is not None:
            procs.append(p)
        return p
    return go


def wait_for(cond, seconds=15.0, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def start_supervisor_thread(paths, kit, cfg=None, env_extra=None, host=None, args_table=None):
    """在线程里跑守护者（进程内；子进程里的 llama-server 是真的假服务）。返回 (supervisor, thread, 结果字典)。"""
    cfg = dict({"poll_seconds": 0.05, "ready_timeout_seconds": 15, "kill_grace_seconds": 2, "restart_delays": (0.05, 0.05, 0.05, 0.05, 0.05)}, **(cfg or {}))
    sup = ER.Supervisor(paths, pins=kit.pins, host=host, cfg=cfg, env_extra=env_extra, args_table=args_table)
    res = {}

    def run():
        res["r"] = sup.run()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return sup, th, res


def ready_state(paths, seconds=15.0):
    return wait_for(lambda: (lambda s: s if s and s.get("state") == "ready" else None)(ER.trusted_state(paths)), seconds)


def stop_all(paths, procs=()):
    try:
        ER.stop_engine(paths, wait=5)
    except Exception:
        pass
    for p in procs:
        if p.poll() is None:
            p.kill()
        try:
            p.wait(timeout=5)
        except Exception:
            pass


class PsHost(ER.Host):
    """沙箱里 /bin/ps 不让跑（Operation not permitted），所以孤儿的逻辑用「我们自己知道的进程」当 ps 的输出来测；真的 ps 在沙箱外另测。
    entries＝[(Popen 或 pid, 命令行文字)]；杀进程是真杀。"""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def ps_all(self):
        rows = []
        for p, cmd in self.entries:
            if hasattr(p, "poll"):
                if p.poll() is None:
                    rows.append((p.pid, os.getuid(), cmd))
            elif self.pid_alive(p):
                rows.append((p, os.getuid(), cmd))
        return rows
