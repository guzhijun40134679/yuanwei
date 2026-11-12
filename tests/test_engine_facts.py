"""自带向量引擎：现在就能核的「事实」和假 llama-server 替身的自检（交回点乙；骨架清单已经换成 test_engine_*.py 里的真测试）。
事实测试：对现有代码或本机系统跑真东西（身份字段、flock、zipfile、本机回环下载）。每一条只记「这台机器、这次」的观测，不推广成对所有环境的结论。
替身自检：只证明测试用的假 llama-server 自己是对的；它的样子是施工方凭记忆假定的，没对着真的 llama.cpp 核过。
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

# ================================================================ ② 事实测试（现在就跑；对现有代码和本机系统）
def test_fact_identity_fields_are_exactly_ten_and_extra_fields_are_rejected():
    """设计 §8：IDENTITY_FIELDS 恰好十个，加字段会被拒；所以自带引擎的信息只能折进现有十个字段里。折进去以后指纹和 LM Studio 的不同。"""
    from yuanwei import vectors as VX
    from yuanwei.errors import VectorIdentityMismatch
    managed = {"provider": "llama.cpp-server;pooling=last", "model": "Qwen3-Embedding-0.6B-Q8_0", "model_file_sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
               "runtime_version": "llama.cpp-server@bTEST", "context_length": 4096, "dimension": 1024, "normalization": "l2", "prefix": "none", "template": "none", "text_processing": "verbatim"}
    assert len(VX.IDENTITY_FIELDS) == 10 and set(managed) == set(VX.IDENTITY_FIELDS)
    fp_managed = VX.identity_fp(managed)
    lms = dict(managed, provider="lmstudio", model="text-embedding-qwen3-embedding-0.6b", runtime_version="LM Studio test")
    assert VX.identity_fp(lms) != fp_managed
    with pytest.raises(VectorIdentityMismatch):
        VX.check_identity(dict(managed, pooling="last"))                     # 多一个字段：拒绝
    for k in ("runtime_version", "model_file_sha256", "provider", "model"):  # 版本、文件摘要、池化（在 provider 里）、量化（在 model 里）任何一个变，指纹变
        assert VX.identity_fp(dict(managed, **{k: managed[k] + "x"})) != fp_managed, k
    with pytest.raises(VectorIdentityMismatch):
        VX.check_identity(dict(managed, runtime_version="未知"))               # 「未知」不放行


_HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
if sys.argv[3] == "no_fcntl":
    sys.modules["fcntl"] = None           # 放回缺陷：退回到「独占创建文件」的锁
from yuanwei import embedwork as EW
import time
with EW._lock(sys.argv[2]) as got:
    print("GOT" if got else "BUSY", flush=True)
    time.sleep(60)
"""


def _src():
    return os.path.join(os.path.dirname(__file__), "..", "src")


def _hold_and_kill(lock_path, mode="normal"):
    p = subprocess.Popen([sys.executable, "-B", "-c", _HOLDER, _src(), lock_path, mode], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "GOT"
        from yuanwei import embedwork as EW
        held_while_alive = None
        if mode == "normal":
            with EW._lock(lock_path) as second:
                held_while_alive = not second                                    # 持有者活着：别人拿不到
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    return held_while_alive


def test_fact_flock_is_released_when_the_holder_is_killed(tmp_path):
    """设计 §6.2：持锁的真进程被 kill -9，锁立刻可拿，不需要「清过期的锁」。（现有的 embedwork._lock 在 POSIX 上用 flock。）"""
    lock = str(tmp_path / "serve.lock")
    assert _hold_and_kill(lock) is True
    assert not os.path.exists(lock + ".x")
    from yuanwei import embedwork as EW
    with EW._lock(lock) as got:
        assert got is True


def test_fact_without_flock_a_killed_holder_leaves_a_stale_lock(tmp_path):
    """对照：退回「独占创建文件」的锁（非 POSIX 的做法）时，kill -9 以后锁文件还在、别人拿不到。证明上一条测的确实是 flock 的性质，不是巧合。"""
    lock = str(tmp_path / "serve.lock")
    _hold_and_kill(lock, "no_fcntl")
    assert os.path.exists(lock + ".x")                                           # 过期的锁留下了：没人持有，却再也拿不到
    from yuanwei import embedwork as EW


def test_fact_zipfile_extractall_drops_exec_bit(tmp_path):
    """设计 §4.3、§9.1：zipfile.extractall 不保留可执行位（即使压缩包里记了 0755），所以解压后必须自己 chmod。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zi = zipfile.ZipInfo("build/bin/llama-server")
        zi.external_attr = 0o755 << 16
        z.writestr(zi, b"#!/bin/sh\n")
    with zipfile.ZipFile(buf) as z:
        z.extractall(str(tmp_path))
    mode = os.stat(str(tmp_path / "build" / "bin" / "llama-server")).st_mode & 0o777
    assert mode & 0o111 == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="只在 macOS 上有意义")
def test_fact_urllib_download_has_no_quarantine_attr_on_this_mac(tmp_path):
    """观测（只记这一台、这一次，不推广）：用 Python 的 urllib 从本机回环地址下载的文件，没有 com.apple.quarantine（有 com.apple.provenance）。不是「Python 下载的永远不会被隔离」，更不是「不会被系统拦」：真的 llama-server 能不能直接跑要真机核。"""
    import http.server
    body = b"x" * 4096

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        dest = str(tmp_path / "dl.bin")
        urllib.request.urlretrieve("http://127.0.0.1:%d/x" % srv.server_port, dest)
    finally:
        srv.shutdown()
    r = subprocess.run(["xattr", "-l", dest], capture_output=True, text=True)
    assert r.returncode == 0, "xattr 没跑成：不能当作「没有隔离属性」"
    assert "com.apple.quarantine" not in r.stdout


# ================================================================ ③ 假 llama-server 的行为（只证明替身自己对）
FAKE = os.path.join(os.path.dirname(__file__), "fake_llama_server.py")


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class _Fake:
    def __init__(self, tmp_path, extra_args=(), env=None):
        self.tmp, self.port = tmp_path, _free_port()
        self.model = str(tmp_path / "m.gguf")
        open(self.model, "wb").write(b"fake")
        e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        e.update(env or {})
        self.args = ["-m", self.model, "--embedding", "--pooling", "last", "--host", "127.0.0.1", "--port", str(self.port), "-c", "4096", "-b", "4096", "-ub", "4096", "-t", "4",
                     "-np", "1", "--alias", "qwen3-test", "-ngl", "99", "--no-ui"] + list(extra_args)
        self.p = subprocess.Popen([sys.executable, "-B", FAKE] + self.args, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=2) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None

    def wait_up(self, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.p.poll() is not None:
                return False
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()
        self.p.stderr.close()


@pytest.fixture
def fake(tmp_path):
    made = []

    def make(extra_args=(), env=None):
        f = _Fake(tmp_path, extra_args, env)
        made.append(f)
        assert f.wait_up(), "假服务没起来"
        return f
    yield make
    for f in made:
        f.close()


def test_fake_llama_health_is_503_while_loading_then_200(fake):
    f = fake(env={"FAKE_LLAMA_START_DELAY": "1.0"})
    assert f.get("/health")[0] == 503
    end = time.monotonic() + 5
    while time.monotonic() < end and f.get("/health")[0] != 200:
        time.sleep(0.1)
    assert f.get("/health") == (200, {"status": "ok"})


def test_fake_llama_embeddings_are_accepted_by_the_existing_client_and_checks(fake):
    """现有的 HttpEmbeddingClient 和 verify 里的检查（check_vector、同句两次一致、相近句更近）接受它的返回。"""
    from yuanwei import vectors as VX
    from yuanwei import embedsetup as ES
    from helpers_wizard import embed_text
    f = fake()
    cl = VX.HttpEmbeddingClient(f.url + "/v1", "qwen3-test", timeout=5, dimension=1024)
    v3 = cl.embed_batch([ES.SENT_A, ES.SENT_B, ES.SENT_C])
    v1 = cl.embed_batch([ES.SENT_A])
    assert len(v3) == 3 and len(v3[0]) == 1024
    assert ES._cos(v3[0], v1[0]) >= 0.9999 and ES._cos(v3[0], v3[1]) > ES._cos(v3[0], v3[2])
    assert v3[0] == pytest.approx(embed_text(ES.SENT_A))                          # 替身的算法和现有测试替身是同一个，没有各写各的
    code, js = f.get("/v1/models")
    assert code == 200 and js["data"][0]["id"] == "qwen3-test"


def test_fake_llama_unknown_flag_exits_1_with_invalid_argument(tmp_path):
    r = subprocess.run([sys.executable, "-B", FAKE, "--no-such-flag"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "invalid argument" in r.stderr


def test_fake_llama_die_and_exit_modes(tmp_path):
    p = subprocess.run([sys.executable, "-B", FAKE, "-m", str(tmp_path / "x"), "--embedding"], env=dict(os.environ, FAKE_LLAMA_DIE_AT_START="1"), capture_output=True, timeout=20)
    assert p.returncode == -signal.SIGKILL                                          # 模拟「被系统拦住」：被 SIGKILL，没有任何输出
    assert p.stderr == b""
    q = subprocess.run([sys.executable, "-B", FAKE, "-m", str(tmp_path / "x"), "--embedding"], env=dict(os.environ, FAKE_LLAMA_EXIT_AT_START="7"), capture_output=True, timeout=20)
    assert q.returncode == 7


def test_fake_llama_crash_after_n_requests(fake):
    from yuanwei import vectors as VX
    f = fake(env={"FAKE_LLAMA_CRASH_AFTER": "2"})
    cl = VX.HttpEmbeddingClient(f.url + "/v1", "qwen3-test", timeout=5)
    cl.embed_batch(["一"])
    cl.embed_batch(["二"])
    f.p.wait(timeout=10)
    assert f.p.returncode == 1
    with pytest.raises(VX.ServiceDown):
        cl.embed_batch(["三"])


def test_fake_llama_dump_env_lists_names_only_and_dump_argv_matches(tmp_path, fake):
    env_file, argv_file = str(tmp_path / "env.json"), str(tmp_path / "argv.json")
    f = fake(env={"FAKE_LLAMA_DUMP_ENV": env_file, "FAKE_LLAMA_DUMP_ARGV": argv_file, "SOMETHING_SECRET_NAME": "hunter2-value"})
    names = json.load(open(env_file))
    assert "SOMETHING_SECRET_NAME" in names and "hunter2-value" not in open(env_file).read()
    assert json.load(open(argv_file)) == f.args


def test_fake_llama_pooling_none_gives_different_vectors_when_breaking_is_on(tmp_path):
    """演示「池化方式写错，向量就不同」：POOLING_BREAKS 下，不传 --pooling last 的假服务给出不同的向量。"""
    sys.path.insert(0, os.path.dirname(__file__))
    import fake_llama_server as FL
    from yuanwei import embedsetup as ES
    os.environ["FAKE_LLAMA_POOLING_BREAKS"] = "1"
    try:
        a, b = FL.embed_text("今天去海边", 1024, "last"), FL.embed_text("今天去海边", 1024, "none")
    finally:
        del os.environ["FAKE_LLAMA_POOLING_BREAKS"]
    assert ES._cos(a, b) < 0.5


def test_fake_llama_embed_text_is_the_same_algorithm_as_the_shared_test_helper():
    from helpers_wizard import embed_text
    import fake_llama_server as FL
    assert FL.embed_text("同一个算法", 1024) == pytest.approx(embed_text("同一个算法", 1024))
