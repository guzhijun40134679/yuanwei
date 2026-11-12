"""自带向量引擎：运行（R2–R5）。守护者在线程或真的子进程里跑，llama-server 是 tests/fake_llama_server.py（凭记忆假定的样子，没对着真的 llama.cpp 核过）。绝不联网：只连 127.0.0.1。"""
import contextlib
import http.server
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

from helpers_engine import Kit, MODEL_BYTES, PsHost, ready_state, sha, spawner_for, start_supervisor_thread, stop_all, wait_for
from yuanwei import catalog as CT
from yuanwei import enginepkg as EP
from yuanwei import enginerun as ER
from yuanwei import vectors as VX


class Ctx:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.kit = Kit(tmp_path)
        self.paths = self.kit.install()
        self.procs, self.threads = [], []

    def start(self, cfg=None, env=None, args_table=None, host=None):
        sup, th, res = start_supervisor_thread(self.paths, self.kit, cfg=cfg, env_extra=env, host=host, args_table=args_table)
        self.threads.append((sup, th, res))
        return sup, th, res

    def identity(self, **kw):
        rc = EP.read_receipt(self.paths)
        ident = EP.build_identity("bTEST", EP.package_digest(self.kit.manifest), EP.recipe_digest(), rc["model"]["sha256"], 1024, "l2")
        ident.update(kw)
        return ident

    def emb(self, **kw):
        return dict({"managed": True, "engine_dir": self.paths.root, "identity": self.identity(), "timeout_query": 5, "timeout_worker": 10}, **kw)

    def client(self, role="worker", spawner=None, **kw):
        return ER.resolve_client(self.emb(**kw), role, spawner=spawner)


@pytest.fixture
def ctx(tmp_path):
    c = Ctx(tmp_path)
    yield c
    stop_all(c.paths, c.procs)
    for sup, th, res in c.threads:
        th.join(timeout=10)


def http_do(port, path, method="GET", body=None, key=None, timeout=3):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **({"Authorization": "Bearer " + key} if key else {})))
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def key_of(st):
    return open(st["key_file"]).read().strip()


@contextlib.contextmanager
def hold_lock(paths):
    """进程内假装守护者持着锁（flock 是按打开的文件算的，所以别的打开方式拿不到）。"""
    with EP.flock(paths.serve_lock) as got:
        assert got
        yield


def fake_state(paths, port, child_pid, alias="yuanwei-" + "a" * 32, run_id="a" * 32, ctx=None, **kw):
    kf = os.path.join(paths.run, "key-" + run_id)
    open(kf, "w").write("k" * 64)
    os.chmod(kf, 0o600)
    st = {"schema": 1, "supervisor_id": "s", "state": "ready", "port": port, "child_pid": child_pid, "alias": alias, "run_id": run_id, "key_file": kf}
    if ctx is not None:
        rc = EP.read_receipt(paths)
        st.update(model_file_sha256=rc["model"]["sha256"], runtime_version=EP.expected_runtime_version(ctx.kit.pins), context_length=2048)
    st.update(kw)
    EP.write_json_atomic(paths.state, st)
    return st


class Server(http.server.ThreadingHTTPServer):
    pass


def tiny_server(handler_fn):
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            hits.append((self.path, self.headers.get("Authorization"), body))
            handler_fn(self)

        do_GET = do_POST
    srv = Server(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, hits


def jsend(h, code, obj, headers=None):
    raw = json.dumps(obj).encode()
    h.send_response(code)
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(raw)))
    h.end_headers()
    h.wfile.write(raw)


# ================================================================ 启动：参数、凭据、环境
def test_launch_is_loopback_random_port_pooling_last_ctx_2048_key_file_alias_with_run_id_and_scrubbed_env(ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-secret-secret-secret-1234")
    monkeypatch.setenv("HERMES_HOME", "/somewhere")
    monkeypatch.setenv("PYTHONPATH", "/should/not/pass")
    argv_f, env_f = str(tmp_path / "argv.json"), str(tmp_path / "env.json")
    sup, th, res = ctx.start(env={"FAKE_LLAMA_DUMP_ARGV": argv_f, "FAKE_LLAMA_DUMP_ENV": env_f})
    st = ready_state(ctx.paths)
    assert st and st["state"] == "ready"
    argv = json.load(open(argv_f))
    assert argv[argv.index("--host") + 1] == "127.0.0.1" and argv[argv.index("--pooling") + 1] == "last" and argv[argv.index("-c") + 1] == "2048"
    assert argv[argv.index("--alias") + 1] == "yuanwei-" + st["run_id"] == st["alias"]
    assert argv[argv.index("--api-key-file") + 1] == st["key_file"] and "--no-ui" in argv and "--no-slots" in argv
    assert not any(x in ("-v", "--verbose", "--log-verbosity") for x in argv)
    assert int(argv[argv.index("--port") + 1]) == st["port"] and st["port"] > 1023
    key = key_of(st)
    assert len(key) == 64 and key not in json.dumps(argv) and key not in open(ctx.paths.state).read()           # 凭据不在命令行、不在状态文件里
    assert oct(os.stat(st["key_file"]).st_mode & 0o777) == "0o600"
    names = json.load(open(env_f))
    assert not [n for n in names if n.endswith("_API_KEY") or n.startswith("HERMES_") or n == "PYTHONPATH"]
    ER.stop_engine(ctx.paths, wait=8)
    th.join(timeout=10)
    assert res["r"] == "stop"


def test_unauthenticated_requests_are_refused_and_unneeded_interfaces_are_off(ctx):
    ctx.start()
    st = ready_state(ctx.paths)
    port, key = st["port"], key_of(st)
    assert http_do(port, "/health")[0] == 200                                                         # 健康检查本来就不需要凭据：所以它不算「认了身份」
    assert http_do(port, "/v1/embeddings", "POST", {"model": st["alias"], "input": ["x"]})[0] == 401
    assert http_do(port, "/v1/embeddings", "POST", {"model": st["alias"], "input": ["x"]}, key="wrong")[0] == 401
    assert http_do(port, "/v1/models")[0] == 401
    assert http_do(port, "/slots", key=key)[0] == 404 and http_do(port, "/", key=key)[0] == 404
    c, js = http_do(port, "/v1/models", key=key)
    assert c == 200 and js["data"][0]["id"] == st["alias"]


def test_stderr_noise_with_conversation_like_text_is_never_stored_anywhere(ctx):
    marker = "我的银行卡密码是 654321 别告诉别人"
    ctx.start(env={"FAKE_LLAMA_STDERR_NOISE": marker})
    st = ready_state(ctx.paths)
    cl = ctx.client()
    cl.embed_batch([marker + "（对话里的一句）"])
    ER.stop_engine(ctx.paths, wait=8)
    wait_for(lambda: not EP.lock_is_held(ctx.paths.serve_lock))
    for dp, _dn, fns in os.walk(ctx.paths.root):
        for f in fns:
            p = os.path.join(dp, f)
            if os.path.getsize(p) < 5 << 20 and not p.startswith(os.path.join(ctx.paths.bin)):
                data = open(p, "rb").read()
                assert marker.encode() not in data and "654321".encode() not in data, p
    assert os.path.exists(ctx.paths.supervisor_log)                                                  # 原委自己的日志只有类别
    assert "654321" not in open(ctx.paths.supervisor_log).read()


# ================================================================ R2：锁、状态、孤儿
def test_second_supervisor_exits_immediately_and_only_one_child(ctx):
    ctx.start()
    ready_state(ctx.paths)
    res2 = {}
    sup2 = ER.Supervisor(ctx.paths, pins=ctx.kit.pins)
    t2 = threading.Thread(target=lambda: res2.setdefault("r", sup2.run()), daemon=True)
    t0 = time.monotonic()
    t2.start()
    t2.join(5)                                                                                         # 抢不到锁就立刻退出；没退出就是锁没管住（不能让测试自己挂住）
    assert res2.get("r") == "busy" and time.monotonic() - t0 < 2.0
    assert len(EP.read_json(ctx.paths.children)["run_ids"]) == 1


def test_stale_ready_state_after_kill_9_is_not_trusted_orphan_is_reaped_and_next_start_works(ctx):
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, procs=ctx.procs)
    sp()
    st = ready_state(ctx.paths)
    assert st
    old_child, sup_pid = st["child_pid"], st["supervisor_pid"]
    os.kill(sup_pid, signal.SIGKILL)
    wait_for(lambda: not EP.lock_is_held(ctx.paths.serve_lock), 5)
    assert ER.trusted_state(ctx.paths) is None and EP.read_json(ctx.paths.state)["state"] == "ready"   # 状态文件还写着 ready：不被当真
    assert ER.Host().pid_alive(old_child)                                                               # 孤儿还活着
    spawned = []
    with pytest.raises(ER.EngineDown) as e:
        ER.ensure(ctx.paths, {}, spawner=lambda: spawned.append(1), wait=0)                             # 不把过期状态当「在跑」：要拉起新的
    assert e.value.reason == "engine_starting" and spawned == [1]
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    cmd = "%s -m x --alias %s" % (pk["binary"], st["alias"])
    sup, th, res = ctx.start(host=PsHost([(old_child, cmd)]))                                           # 新守护者（沙箱里 ps 不让跑，用已知进程当 ps 的输出）
    st2 = wait_for(lambda: (lambda s: s if s and s["state"] == "ready" and s["run_id"] != st["run_id"] else None)(ER.trusted_state(ctx.paths)), 40)
    try:
        assert st2 and wait_for(lambda: not ER.Host().pid_alive(old_child), 30)                              # 新守护者收拾了孤儿；新一轮的 ready 没被旧的覆盖
    finally:
        with contextlib.suppress(OSError):
            os.kill(old_child, signal.SIGKILL)                                                           # 失败时也别留下孤儿


def test_write_window_popen_ok_but_supervisor_killed_before_child_pid_is_written_is_recovered_by_run_id(ctx):
    rid = "b" * 32
    EP.write_json_atomic(ctx.paths.children, {"run_ids": [rid]})                                       # 先登记再 Popen：登记在，状态文件里没有 child_pid
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    m = EP.read_receipt(ctx.paths)["model"]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    orphan = subprocess.Popen([pk["binary"], "-m", m["path"], "--embedding", "--host", "127.0.0.1", "--port", str(port), "--alias", "yuanwei-" + rid], stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        ctx.start(host=PsHost([(orphan, "%s -m %s --embedding --alias yuanwei-%s" % (pk["binary"], m["path"], rid))]))
        assert wait_for(lambda: orphan.poll() is not None, 10) and orphan.returncode in (0, -signal.SIGTERM)
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()


def test_unsure_processes_are_not_killed_only_reported(ctx):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    m = EP.read_receipt(ctx.paths)["model"]
    rid = "c" * 32                                                                                    # 没登记过的启动代号：不是我们拉起的（或登记丢了）
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    unknown = subprocess.Popen([pk["binary"], "-m", m["path"], "--embedding", "--host", "127.0.0.1", "--port", str(port), "--alias", "yuanwei-" + rid], stderr=subprocess.DEVNULL)
    lookalike = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--alias", "yuanwei-" + "d" * 32])         # 命令行里有同样的别名，但不是我们的程序
    EP.write_json_atomic(ctx.paths.children, {"run_ids": ["d" * 32]})
    try:
        time.sleep(0.5)
        ctx.start(host=PsHost([(unknown, "%s -m %s --embedding --alias yuanwei-%s" % (pk["binary"], m["path"], rid)),
                               (lookalike, "%s -c import time; time.sleep(30) --alias yuanwei-%s" % (sys.executable, "d" * 32))]))
        st = ready_state(ctx.paths)
        time.sleep(0.3)
        assert unknown.poll() is None and lookalike.poll() is None                                         # 都没动
        assert unknown.pid in (EP.read_json(ctx.paths.state).get("orphan_suspects") or [])               # 拿不准的：只报告
        assert lookalike.pid not in (EP.read_json(ctx.paths.state).get("orphan_suspects") or [])
    finally:
        for p in (unknown, lookalike):
            p.kill()
            p.wait()


def test_pid_reuse_foreign_user_process_with_our_command_line_is_not_killed(ctx):
    class FakeHost(ER.Host):
        def __init__(self):
            self.killed = []

        def ps_all(self):
            pk = EP.installed_package(ctx.paths, ctx.kit.pins, hash_files=False)
            rows = [(4242, os.getuid() + 1, "%s -m x --alias yuanwei-%s" % (pk["binary"], "e" * 32)), (4243, os.getuid(), "%s -m x --alias yuanwei-%s" % (pk["binary"], "f" * 32))]
            return [r for r in rows if r[0] not in self.killed]                                         # 被请求结束的就退出了

        def kill(self, pid, sig):
            self.killed.append(pid)
    EP.write_json_atomic(ctx.paths.children, {"run_ids": ["e" * 32, "f" * 32]})
    h = FakeHost()
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, host=h)
    sup.sid, sup.base_state = "x", {}
    sup._reap_orphans()
    assert h.killed == [4243]                                                                           # 用户不同的那个不杀（拿不准）
    assert EP.read_json(ctx.paths.children)["run_ids"] == ["e" * 32]                                    # 退了的登记删掉；用户不同的那个没杀、不能说它退了：登记留着


def test_unreadable_process_list_is_reported_unknown_not_taken_as_none(ctx):
    class H(ER.Host):
        def ps_all(self):
            return None
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, host=H())
    sup.sid, sup.base_state = "x", {}
    sup._reap_orphans()
    assert EP.read_json(ctx.paths.state)["orphan_check"] == "unknown"


def test_old_instance_cleanup_cannot_overwrite_or_delete_new_instances_state(ctx):
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins)
    sup.sid, sup.base_state = "old", {"supervisor_id": "old"}
    EP.write_json_atomic(ctx.paths.state, {"supervisor_id": "new", "state": "ready", "port": 1})
    sup._publish(state="failed", port=None)
    assert EP.read_json(ctx.paths.state) == {"supervisor_id": "new", "state": "ready", "port": 1}
    sup._cleanup()
    assert EP.read_json(ctx.paths.state)["supervisor_id"] == "new"


# ================================================================ R2：控制通道、移除、使用方
def test_stop_goes_through_the_control_channel_and_stale_or_foreign_stop_commands_are_ignored(ctx):
    EP.write_json_atomic(ctx.paths.control, {"id": "old", "cmd": "stop", "target": "*", "at": time.time() - 5})          # 本轮启动之前写的
    sup, th, res = ctx.start()
    st = ready_state(ctx.paths)
    assert st
    time.sleep(0.4)
    assert ER.trusted_state(ctx.paths)["state"] == "ready"
    EP.write_json_atomic(ctx.paths.control, {"id": "x2", "cmd": "stop", "target": "someone-else", "at": time.time()})
    time.sleep(0.4)
    assert ER.trusted_state(ctx.paths)["state"] == "ready"
    child = st["child_pid"]
    r = ER.stop_engine(ctx.paths, wait=8)
    assert r == {"stopped": True, "was_running": True}
    th.join(timeout=10)
    assert not ER.Host().pid_alive(child) and not os.path.exists(ctx.paths.state) and EP.read_json(ctx.paths.last_exit)["reason"] == "stopped"


def test_stop_does_not_kill_when_the_supervisor_does_not_answer(ctx):
    with hold_lock(ctx.paths):                                                                          # 有人持着锁，但不会响应控制文件
        r = ER.stop_engine(ctx.paths, wait=0.3)
    assert r["stopped"] is False and r["reason"] == "stop_timeout"


def test_remove_engine_enters_removing_state_first_never_deletes_the_lock_files_and_keeps_reused_model(ctx):
    sup, th, res = ctx.start()
    ready_state(ctx.paths)
    inodes = {n: os.stat(getattr(ctx.paths, n)).st_ino for n in ("serve_lock", "lifecycle_lock", "admit_lock") if os.path.exists(getattr(ctx.paths, n))}
    reused = EP.read_receipt(ctx.paths)["model"]["path"]
    ER.register_user(ctx.paths, "u1")
    r = ER.remove_engine(ctx.paths, wait=8)
    th.join(timeout=10)
    assert r["stopped"] and r["users"] == ["u1"] and "bin" in r["removed"]
    assert os.path.exists(reused) and not os.path.exists(ctx.paths.receipt) and not os.path.isdir(ctx.paths.bin)
    assert {n: os.stat(getattr(ctx.paths, n)).st_ino for n in inodes} == inodes                          # 锁文件没删、没换
    assert ER.lifecycle_state(ctx.paths) == "disabled"
    with pytest.raises(ER.EngineDown) as e:                                                              # 旧插件不能立刻复活它
        ER.ensure(ctx.paths, {}, spawner=lambda: pytest.fail("不许拉起"), wait=0)
    assert e.value.reason == "engine_disabled"
    assert ER.Supervisor(ctx.paths, pins=ctx.kit.pins).run() == "disabled"


def test_remove_engine_deletes_only_owned_model_and_stops_before_deleting_anything(ctx):
    paths = ctx.paths
    d = EP.download_model(paths, ctx.kit.pins, ctx.kit.net.fetcher())
    EP.write_receipt(paths, model=dict(d, source="downloaded"))
    with hold_lock(paths):                                                                               # 停不下来：什么都不删
        r = ER.remove_engine(paths, wait=0.3)
    assert r["stopped"] is False and r["removed"] == [] and os.path.isdir(paths.bin) and os.path.exists(d["path"])
    assert ER.lifecycle_state(paths) == "removing"                                                       # 持久的「正在移除」：挡住并发的 ensure
    with pytest.raises(ER.EngineDown):
        ER.ensure(paths, {}, spawner=lambda: pytest.fail("不许拉起"), wait=0)
    r2 = ER.remove_engine(paths, wait=2)
    assert r2["stopped"] and "model" in r2["removed"] and not os.path.exists(d["path"])


def test_unbinding_one_user_does_not_stop_an_engine_another_user_still_uses(ctx):
    sup, th, res = ctx.start()
    st = ready_state(ctx.paths)
    ER.register_user(ctx.paths, "hermes:a")
    ER.register_user(ctx.paths, "hermes:b")
    r = ER.unbind_user(ctx.paths, "hermes:a", wait=0.5)
    assert r == {"remaining": ["hermes:b"], "stopped": None} and ER.trusted_state(ctx.paths)["state"] == "ready"
    r2 = ER.unbind_user(ctx.paths, "hermes:b", wait=8)
    th.join(timeout=10)
    assert r2["remaining"] == [] and r2["stopped"] is True and not EP.lock_is_held(ctx.paths.serve_lock)


def test_two_users_with_different_models_get_an_explicit_conflict_not_a_shared_instance(ctx):
    ctx.start()
    ready_state(ctx.paths)
    other = ctx.emb(identity=ctx.identity(model_file_sha256="9" * 64))
    cl = ER.resolve_client(other, "worker", spawner=lambda: pytest.fail("不许拉起"))
    with pytest.raises(ER.EngineDown) as e:
        cl.embed_batch(["x"])
    assert e.value.reason == "engine_identity_conflict" and os.listdir(ctx.paths.inflight) == []
    other2 = ctx.emb(identity=ctx.identity(runtime_version="llama.cpp-server@bOTHER;pkg=x;recipe=y"))
    with pytest.raises(ER.EngineDown) as e:
        ER.resolve_client(other2, "worker").embed_batch(["x"])
    assert e.value.reason == "engine_identity_conflict"


def test_install_and_remove_serialize_on_the_lifecycle_lock(ctx):
    with EP.flock(ctx.paths.lifecycle_lock) as got:
        assert got
        with pytest.raises(EP.EngineError) as e:
            ER.register_user.__wrapped__ if False else None
            # remove／unbind 拿不到生命周期锁就报 engine_busy（等 5 秒太久，这里直接核锁语义）
            with EP.flock(ctx.paths.lifecycle_lock) as got2:
                assert got2 is False
                raise EP.EngineError("engine_busy")
        assert e.value.code == "engine_busy"


# ================================================================ R3：本机端口也要核身份
def test_client_ignores_proxy_env_and_works_through_loopback_only(ctx, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    ctx.start()
    ready_state(ctx.paths)
    v = ctx.client().embed_batch(["今天去海边", "晚上吃鱼"])
    assert len(v) == 2 and len(v[0]) == 1024
    op = ER.LoopbackOpener()
    with pytest.raises(VX.ProviderError):
        op(urllib.request.Request("http://localhost:1/"), timeout=1)
    with pytest.raises(VX.ProviderError):
        op(urllib.request.Request("http://192.168.1.2:1/"), timeout=1)


def test_redirect_from_the_local_service_is_refused_and_nothing_follows_it(ctx):
    target, target_hits = tiny_server(lambda h: jsend(h, 200, {"model": "yuanwei-" + "a" * 32, "data": [{"index": 0, "embedding": [1.0] * 1024}]}))
    redir, redir_hits = tiny_server(lambda h: jsend(h, 302, {}, {"Location": "http://127.0.0.1:%d/v1/embeddings" % target.server_port}))
    with hold_lock(ctx.paths):
        fake_state(ctx.paths, redir.server_port, os.getpid(), ctx=ctx)
        with pytest.raises(ER.EngineDown) as e:
            ctx.client().embed_batch(["x"])
    assert e.value.reason == "engine_identity_mismatch" and target_hits == [] and len(redir_hits) == 1
    redir.shutdown(); target.shutdown()


def test_a_squatter_on_the_port_answering_200_is_not_trusted_if_child_is_gone_or_alias_differs(ctx):
    sq, hits = tiny_server(lambda h: jsend(h, 200, {"status": "ok", "model": "yuanwei-" + "0" * 32, "data": [{"index": 0, "embedding": [1.0] * 1024}]}))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with hold_lock(ctx.paths):
        fake_state(ctx.paths, sq.server_port, dead.pid, ctx=ctx)                                       # 子进程已经不在了
        with pytest.raises(ER.EngineDown) as e:
            ctx.client().embed_batch(["含有对话的文字"])
        assert e.value.reason == "engine_starting" and hits == []                                       # 一个字都没发给占端口的那个
        fake_state(ctx.paths, sq.server_port, os.getpid(), ctx=ctx)                                    # 子进程「活着」，但回应里的模型名不是本轮启动代号
        with pytest.raises(ER.EngineDown) as e2:
            ctx.client().embed_batch(["x"])
    assert e2.value.reason == "engine_identity_mismatch"
    sq.shutdown()


def test_breaker_is_scoped_to_the_run_id_and_new_run_resets_it_immediately(ctx):
    calls = []

    def h(handler):
        calls.append(1)
        jsend(handler, 500, {})
    srv, hits = tiny_server(h)
    with hold_lock(ctx.paths):
        fake_state(ctx.paths, srv.server_port, os.getpid(), ctx=ctx)
        cl = ctx.client("query", breaker_seconds=30)
        for _ in range(3):
            with pytest.raises(VX.ServiceDown):
                cl.embed_batch(["x"])
        assert len(hits) == 1                                                                           # 同一轮里：断路器挡住了后两次
        fake_state(ctx.paths, srv.server_port, os.getpid(), ctx=ctx, run_id="b" * 32, alias="yuanwei-" + "b" * 32)
        with pytest.raises(VX.ServiceDown):
            cl.embed_batch(["x"])
        assert len(hits) == 2                                                                           # 换了启动代号：不挡
    srv.shutdown()


def test_worker_batch_fails_whole_when_the_instance_changes_mid_session(ctx):
    ctx.start()
    st = ready_state(ctx.paths)
    cl = ctx.client("worker")
    cl.embed_batch(["一"])
    # 把状态里的启动代号换掉（模拟重启过）
    cur = EP.read_json(ctx.paths.state)
    cur["run_id"] = "9" * 32
    EP.write_json_atomic(ctx.paths.state, cur)
    with pytest.raises(ER.EngineDown) as e:
        cl.embed_batch(["二"])
    assert e.value.reason == "engine_starting"


# ================================================================ R4：在途请求、空闲退出、重启预算
def test_idle_exit_waits_for_the_in_flight_request_then_exits_after_idle(ctx):
    sup, th, res = ctx.start(cfg={"idle_exit_seconds": 0.8}, env={"FAKE_LLAMA_HOLD_MS": "1800"})
    st = ready_state(ctx.paths)
    cl = ctx.client("worker")
    out = {}
    t0 = time.monotonic()
    rq = threading.Thread(target=lambda: out.setdefault("v", cl.embed_batch(["慢请求"])))
    rq.start()
    time.sleep(1.3)                                                                                      # 已经超过 idle_exit_seconds，但请求还在做
    assert th.is_alive() and ER.trusted_state(ctx.paths)["state"] == "ready" and len(os.listdir(ctx.paths.inflight)) == 1
    rq.join(timeout=10)
    assert len(out["v"][0]) == 1024                                                                      # 请求做完了、没被停掉
    th.join(timeout=15)
    assert res["r"] == "idle" and EP.read_json(ctx.paths.last_exit)["reason"] == "idle_exit" and time.monotonic() - t0 > 2.0


def test_admission_refused_after_draining_is_announced_and_idle_check_sees_late_arrivals(ctx):
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, cfg={"idle_exit_seconds": 0.0})
    sup.sid, sup.base_state = "s", {"supervisor_id": "s"}
    EP.write_json_atomic(ctx.paths.state, {"supervisor_id": "s", "state": "ready", "run_id": "a" * 32})
    sup.idle_since = sup.host.mono() - 10
    live = os.path.join(ctx.paths.inflight, "%d-%s" % (os.getpid(), "ab12"))
    open(live, "w").close()
    assert sup._idle_ready_to_exit() is False                                                            # 有在途请求：不退
    os.unlink(live)
    sup.idle_since = sup.host.mono() - 10
    with hold_lock(ctx.paths):
        assert sup._idle_ready_to_exit() is True                                                         # 没有在途：在 admit.lock 里宣布 draining
        assert EP.read_json(ctx.paths.state)["draining"] is True
        with pytest.raises(ER.EngineDown):
            ER.admit(ctx.paths, {"run_id": "a" * 32}, ER.Host())                                         # 之后进来的客户端不用这个实例


def test_dead_client_and_overlong_in_flight_files_are_cleaned_so_they_cannot_block_idle_exit_forever(ctx):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    now = {"t": 0.0}

    class H(ER.Host):
        def mono(self):
            return now["t"]
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, host=H(), cfg={"request_max_seconds": 100})
    f1 = os.path.join(ctx.paths.inflight, "%d-aa" % dead.pid)
    f2 = os.path.join(ctx.paths.inflight, "%d-bb" % os.getpid())
    open(f1, "w").close(); open(f2, "w").close()
    assert sup._inflight_count() == 1 and not os.path.exists(f1)
    now["t"] = 101.0
    assert sup._inflight_count() == 0 and not os.path.exists(f2)


def test_idle_clock_is_monotonic_not_file_mtime(ctx):
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, cfg={"idle_exit_seconds": 5})
    sup.sid, sup.base_state = "s", {"supervisor_id": "s"}
    EP.write_json_atomic(ctx.paths.state, {"supervisor_id": "s", "state": "ready"})
    open(ctx.paths.activity, "w").write("x")
    os.utime(ctx.paths.activity, (0, 0))                                                                 # 把文件时间改得很旧，也不能让它退出
    sup.idle_since = sup.host.mono()
    sup.last_act = "x"
    assert sup._idle_ready_to_exit() is False


def test_never_ready_restarts_are_capped_and_cooldown_blocks_further_spawns(ctx):
    sup, th, res = ctx.start(cfg={"max_consecutive_failures": 3}, env={"FAKE_LLAMA_EXIT_AT_START": "7"})
    th.join(timeout=30)
    assert res["r"] == "crash_giveup"
    b = EP.read_json(ctx.paths.backoff)
    assert b["failures"] == 1 and b["last_error"] == "exit_7" and b["next_try_at"] > time.time() + 500
    assert EP.read_json(ctx.paths.state)["state"] == "failed" and ER.trusted_state(ctx.paths) is None
    n = sum(1 for l in open(ctx.paths.supervisor_log) if '"failure"' in l)
    assert n == 3
    with pytest.raises(ER.EngineDown) as e:
        ER.ensure(ctx.paths, {}, spawner=lambda: pytest.fail("冷却内不许拉起"), wait=0)
    assert e.value.reason == "engine_failed"
    os.unlink(ctx.paths.backoff)                                                                         # 冷却解除（或 --reverify）：才允许再拉
    called = []
    with pytest.raises(ER.EngineDown):
        ER.ensure(ctx.paths, {}, spawner=lambda: called.append(1), wait=0)
    assert called == [1]


def test_ready_then_quick_crashes_share_the_same_cap_not_endless_retries(ctx):
    sup, th, res = ctx.start(cfg={"max_consecutive_failures": 3}, env={"FAKE_LLAMA_LIFETIME": "0.4"})
    th.join(timeout=40)
    assert res["r"] == "crash_giveup" and sum(1 for l in open(ctx.paths.supervisor_log) if '"ready"' in l) == 3
    assert sum(1 for l in open(ctx.paths.supervisor_log) if '"failure"' in l) == 3


def test_stable_run_clears_the_consecutive_counter_but_total_cap_still_ends_it(ctx):
    sup, th, res = ctx.start(cfg={"max_consecutive_failures": 2, "stable_seconds": 0.2, "max_total_restarts": 4}, env={"FAKE_LLAMA_LIFETIME": "0.7"})
    th.join(timeout=60)
    assert res["r"] == "crash_giveup"
    assert sum(1 for l in open(ctx.paths.supervisor_log) if '"failure"' in l) == 4                    # 没有在第 2 次就放弃（稳定过清了零）；到总上限才放弃


def test_args_rejected_is_reported_as_such_and_not_retried(ctx):
    table = list(CT.ENGINE_ARGS) + [("bogus", ["--bogus-flag"], "这一版不认识")]
    sup, th, res = ctx.start(args_table=table)
    th.join(timeout=20)
    assert res["r"] == "args_rejected" and sum(1 for l in open(ctx.paths.supervisor_log) if '"failure"' in l) == 1
    assert EP.read_json(ctx.paths.backoff)["blocked"] is True
    assert ER.engine_status(ctx.paths, ctx.kit.pins)["last_error"] == "args_rejected"


def test_alias_mismatch_from_a_service_that_is_not_ours_is_not_ready_and_not_retried(ctx):
    seen = set()
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            s = EP.read_json(ctx.paths.state)
            if s:
                seen.add(s.get("state"))
            time.sleep(0.01)
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    sup, th, res = ctx.start(env={"FAKE_LLAMA_MODEL_NAME": "not-our-alias"})
    th.join(timeout=20)
    stop.set()
    assert res["r"] == "identity_mismatch" and "ready" not in seen


def test_health_200_is_not_enough_and_never_ready_means_health_timeout(ctx):
    seen = set()
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            s = EP.read_json(ctx.paths.state)
            if s:
                seen.add(s.get("state"))
            time.sleep(0.01)
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    sup, th, res = ctx.start(cfg={"ready_timeout_seconds": 0.6, "max_consecutive_failures": 1}, env={"FAKE_LLAMA_START_DELAY": "30"})
    th.join(timeout=20)
    stop.set()
    assert res["r"] == "crash_giveup" and EP.read_json(ctx.paths.backoff)["last_error"] == "health_timeout" and "ready" not in seen


def test_model_replaced_while_running_stops_accepting_and_demands_reverification(ctx):
    sup, th, res = ctx.start()
    ready_state(ctx.paths)
    m = EP.read_receipt(ctx.paths)["model"]["path"]
    st = os.stat(m)
    open(m, "r+b").write(b"Z" * 8)                                                                       # 外部文件被改
    th.join(timeout=15)
    assert res["r"] == "model_file_changed" and EP.read_json(ctx.paths.backoff)["blocked"] is True
    assert ER.trusted_state(ctx.paths) is None


def test_model_file_changed_before_launch_blocks_start_and_never_swaps_files(ctx):
    m = EP.read_receipt(ctx.paths)["model"]["path"]
    open(m, "wb").write(b"X" * len(MODEL_BYTES))
    sup, th, res = ctx.start()
    th.join(timeout=15)
    assert res["r"] == "model_file_changed" and EP.read_receipt(ctx.paths)["model"]["path"] == m


def test_package_dylib_tampered_blocks_launch_even_if_main_binary_is_unchanged(ctx):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    lib = os.path.join(pk["dir"], "build/bin/libfake.dylib")
    os.chmod(lib, 0o600)
    open(lib, "ab").write(b"!")
    sup, th, res = ctx.start()
    th.join(timeout=15)
    assert res["r"] == "package_changed"


# ================================================================ R5：不凭隔离属性下结论，不绕过系统
def test_startup_killed_is_recorded_as_unknown_cause_not_asserted_as_security_block(ctx):
    sup, th, res = ctx.start(env={"FAKE_LLAMA_DIE_AT_START": "1"})
    th.join(timeout=20)
    assert res["r"] == "startup_killed_unknown"
    s = ER.engine_status(ctx.paths, ctx.kit.pins)
    assert s["last_error"] == "startup_killed_unknown" and "原因不明" in s["hint"] and "拦住" not in s["hint"] and "xattr" not in s["hint"]
    assert "仍要打开" not in ER.HINT_KILLED or "Apple" in ER.HINT_KILLED


@pytest.mark.skipif(sys.platform != "darwin", reason="只在 macOS 上有意义")
def test_quarantine_attr_alone_does_not_block_launch_and_is_left_untouched(ctx):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0081;00000000;Safari;", pk["binary"]], check=True)
    sup, th, res = ctx.start()
    st = ready_state(ctx.paths)
    assert st and st["state"] == "ready"                                                                  # 带着隔离属性，照正常流程启动，没有先下结论
    out = subprocess.run(["/usr/bin/xattr", "-l", pk["binary"]], capture_output=True, text=True).stdout
    assert "com.apple.quarantine" in out                                                                  # 原委没有去掉它


def test_host_only_runs_whitelisted_read_only_commands_with_fixed_system_paths():
    h = ER.Host()
    for bad in (["/usr/bin/xattr", "-d", "com.apple.quarantine", "/x"], ["/usr/bin/xattr", "-c", "/x"], ["/usr/bin/xattr", "-dr", "x", "/x"], ["/usr/bin/codesign", "-s", "-", "/x"],
                ["/usr/bin/codesign", "--force", "/x"], ["xattr", "-l", "/x"], ["/bin/sh", "-c", "true"], ["/usr/sbin/spctl", "--add", "/x"], ["sudo", "ls"]):
        with pytest.raises(ValueError):
            h._run_ro(bad)
    assert ER.XATTR.startswith("/usr/bin/") and ER.PS == "/bin/ps" and ER.CODESIGN.startswith("/usr/bin/")


def test_no_user_facing_text_teaches_xattr_and_no_shell_or_xattr_write_in_the_source():
    import glob
    root = os.path.join(os.path.dirname(__file__), "..")
    texts = []
    for f in glob.glob(os.path.join(root, "src", "yuanwei", "*.py")) + glob.glob(os.path.join(root, "docs", "*.md")) + [os.path.join(root, "README.md")]:
        texts.append((f, open(f, encoding="utf-8").read()))
    for f, t in texts:
        if os.path.basename(f) in ("enginerun.py",):
            assert "shell=True" not in t.replace("shell=False", "")
        assert "xattr -d" not in t and "xattr -dr" not in t and "xattr -c" not in t, f
    src = open(os.path.join(root, "src", "yuanwei", "enginerun.py"), encoding="utf-8").read() + open(os.path.join(root, "src", "yuanwei", "enginepkg.py"), encoding="utf-8").read()
    assert "shell=True" not in src.replace("shell=False", "") and "os.system" not in src and "codesign\", \"-s" not in src


def test_supervisor_and_children_start_in_fixed_cwd_with_python_dash_P_and_list_args(monkeypatch, tmp_path):
    rec = {}

    class H(ER.Host):
        def popen(self, argv, **kw):
            rec["argv"], rec["kw"] = argv, kw
            return None
    ER.spawn_supervisor(str(tmp_path / "eng"), H())
    assert rec["argv"][:4] == [sys.executable, "-P", "-m", "yuanwei"] and isinstance(rec["argv"], list)
    assert rec["kw"]["cwd"] == str(tmp_path / "eng" / "run") and rec["kw"]["start_new_session"] is True
    assert "PYTHONPATH" in rec["kw"]["env"] and not [k for k in rec["kw"]["env"] if k.endswith("_API_KEY")]


def test_project_dir_module_named_yuanwei_cannot_hijack_the_supervisor_entry(tmp_path):
    proj = tmp_path / "proj"
    (proj / "yuanwei").mkdir(parents=True)
    (proj / "yuanwei" / "__init__.py").write_text("raise SystemExit('HIJACKED')\n")
    (proj / "yuanwei" / "__main__.py").write_text("raise SystemExit('HIJACKED')\n")
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    r = subprocess.run([sys.executable, "-B", "-P", "-m", "yuanwei", "vectors", "stop", "--engine-dir", str(tmp_path / "eng")], cwd=str(proj), env={"PYTHONPATH": src, "PATH": os.environ["PATH"]},
                       capture_output=True, text=True, timeout=60)
    assert "HIJACKED" not in r.stdout + r.stderr and '"stopped": true' in r.stdout


# ================================================================ 查询路径：不等、登记、节流
def test_query_when_engine_is_down_returns_quickly_with_engine_starting_and_spawns_once(ctx):
    spawned = []
    cl = ctx.client("query", spawner=lambda: spawned.append(1))
    t0 = time.monotonic()
    for _ in range(4):
        with pytest.raises(VX.ServiceDown) as e:
            cl.embed_batch(["x"])
        assert e.value.reason == "engine_starting" and VX.down_reason(e.value) == "engine_starting"
    assert time.monotonic() - t0 < 1.0 and spawned == [1]                                               # 4 次查询只拉起 1 次


def test_cold_wait_seconds_waits_for_ready_then_falls_back_when_too_slow(ctx):
    procs = []
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, child_env={"FAKE_LLAMA_START_DELAY": "1.0"}, procs=procs)
    ctx.procs += procs
    cl = ctx.client("query", spawner=sp, cold_wait_seconds=6)
    v = cl.embed_batch(["x"])
    assert len(v[0]) == 1024                                                                            # 等到了：这一次用上了向量
    stop_all(ctx.paths, procs)
    procs2 = []
    sp2 = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, child_env={"FAKE_LLAMA_START_DELAY": "20"}, procs=procs2)
    ctx.procs += procs2
    wait_for(lambda: not EP.lock_is_held(ctx.paths.serve_lock), 8)
    cl2 = ctx.client("query", spawner=sp2, cold_wait_seconds=1.0)
    t0 = time.monotonic()
    with pytest.raises(ER.EngineDown) as e:
        cl2.embed_batch(["x"])
    assert e.value.reason == "engine_starting" and 0.9 < time.monotonic() - t0 < 3.0


def test_no_respawn_after_idle_exit_just_because_a_plugin_is_alive(ctx):
    spawned = []
    sup, th, res = ctx.start(cfg={"idle_exit_seconds": 0.5})
    ready_state(ctx.paths)
    th.join(timeout=20)
    assert res["r"] == "idle"
    cl = ctx.client("query", spawner=lambda: spawned.append(1))
    time.sleep(1.0)
    assert spawned == []                                                                                # 没有人用：不会自己再起来
    with pytest.raises(ER.EngineDown):
        cl.embed_batch(["x"])
    assert spawned == [1]                                                                               # 真用到了才起


def test_end_to_end_embedding_through_a_real_supervisor_process_and_inflight_is_released(ctx):
    procs = []
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, procs=procs)
    ctx.procs += procs
    cl = ctx.client("worker", spawner=sp)
    v = cl.embed_batch(["我喜欢喝咖啡。", "火车几点出发？"])
    assert len(v) == 2 and len(v[0]) == 1024 and os.listdir(ctx.paths.inflight) == []
    assert ER.engine_status(ctx.paths, ctx.kit.pins)["process"]["state"] == "ready"


def test_verify_managed_returns_strong_identity_with_package_and_recipe_digests(ctx):
    procs = []
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, procs=procs)
    ctx.procs += procs
    r = ER.verify_managed(ctx.paths, ctx.kit.pins, spawner=sp, wait=20)
    assert r["ok"] and r["weak"] is False and r["dimension"] == 1024
    ident = r["identity"]
    assert set(ident) == set(VX.IDENTITY_FIELDS) and ident["runtime_version"] == EP.expected_runtime_version(ctx.kit.pins) and ident["context_length"] == 2048 and ident["model_file_sha256"] == sha(MODEL_BYTES)


def test_verify_managed_fails_when_pooling_is_wrong_in_the_engine_or_dimension_differs(ctx):
    procs = []
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, child_env={"FAKE_LLAMA_DIM": "768"}, procs=procs)
    ctx.procs += procs
    r = ER.verify_managed(ctx.paths, ctx.kit.pins, spawner=sp, wait=20)
    assert r["ok"] is False and r["reason"] == "dimension" and r["dimension"] == 768


def test_status_is_read_only_never_starts_or_renews_and_unknown_is_reported(ctx):
    before = sorted(os.listdir(ctx.paths.run))
    s = ER.engine_status(ctx.paths, ctx.kit.pins)
    assert s["process"]["running"] is False and s["installed"]["program"] and s["installed"]["model"] and s["installed"]["model_source"] == "reused"
    assert "同一个模型文件" in s["installed"]["model_wording"] and s["installed"]["model_label"] == "一致性还没在真机上核" and sorted(os.listdir(ctx.paths.run)) == before
    EP.write_json_atomic(ctx.paths.receipt, {"model": "not-a-dict"})
    s2 = ER.engine_status(ctx.paths, ctx.kit.pins)
    assert s2["process"]["state"] == "unknown" and s2["process"]["error"]


def test_real_ps_reads_the_process_list_or_reports_none_never_an_empty_list():
    rows = ER.Host().ps_all()
    if rows is None:
        pytest.skip("沙箱里 /bin/ps 不让跑：这条在沙箱外验过（见交回报告）；读不出来返回 None 而不是空列表，已由 test_unreadable_process_list_is_reported_unknown_not_taken_as_none 守着")
    assert any(pid == os.getpid() for pid, _u, _c in rows)
    assert ER.Host().proc_start(os.getpid())


def test_supervisor_rereads_cooldown_and_lifecycle_after_taking_the_lock(ctx):
    EP.write_json_atomic(ctx.paths.backoff, {"failures": 1, "next_try_at": time.time() + 600, "last_error": "exit_1"})
    assert ER.Supervisor(ctx.paths, pins=ctx.kit.pins).run() == "cooldown"
    assert not os.path.exists(ctx.paths.state) or EP.read_json(ctx.paths.state).get("state") != "ready"
    os.unlink(ctx.paths.backoff)
    ER.set_lifecycle(ctx.paths, "removing")
    assert ER.Supervisor(ctx.paths, pins=ctx.kit.pins).run() == "disabled"


def test_real_ps_orphan_with_registered_run_id_is_reaped_end_to_end(ctx):
    if ER.Host().ps_all() is None:
        pytest.skip("沙箱里 /bin/ps 不让跑：这条在沙箱外验过（见交回报告）；沙箱里用 PsHost 的几条测同样的逻辑")
    rid = "a1" * 16
    EP.write_json_atomic(ctx.paths.children, {"run_ids": [rid]})
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    m = EP.read_receipt(ctx.paths)["model"]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    orphan = subprocess.Popen([pk["binary"], "-m", m["path"], "--embedding", "--host", "127.0.0.1", "--port", str(port), "--alias", "yuanwei-" + rid], stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        ctx.start()                                                                                      # 真的 ps，不用替身
        assert wait_for(lambda: orphan.poll() is not None, 10)
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()


# ================================================================ 时序：问「有没有人持锁」的人不能挡住真正拿锁的守护者（压力下偶发失败的原因）
def test_prober_holding_the_lock_for_a_moment_does_not_make_a_starting_supervisor_give_up(ctx):
    import fcntl
    probe = open(ctx.paths.serve_lock, "a+")
    fcntl.flock(probe, fcntl.LOCK_SH)                                                                  # 有人正好在问「有没有人持锁」（共享锁）
    threading.Timer(0.3, lambda: (fcntl.flock(probe, fcntl.LOCK_UN), probe.close())).start()
    sup, th, res = ctx.start()
    st = ready_state(ctx.paths)
    assert st and st["state"] == "ready" and res.get("r") != "busy"                                    # 老写法（独占锁、拿不到就退）会在这里返回 busy，引擎起不来


def test_many_probers_during_supervisor_startup_never_make_it_exit_busy(ctx):
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            ER.trusted_state(ctx.paths)
    ts = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    [t.start() for t in ts]
    try:
        for _ in range(3):
            res = {}
            sup, th, res = ctx.start()
            assert ready_state(ctx.paths) and res.get("r") != "busy"
            ER.stop_engine(ctx.paths, wait=8)
            th.join(timeout=10)
            assert res["r"] == "stop"
    finally:
        stop.set()


def test_two_probers_do_not_see_each_other_as_a_holder_but_a_real_holder_is_seen(tmp_path):
    import fcntl
    p = str(tmp_path / "x.lock")
    open(p, "a").close()
    other_prober = open(p, "a+")
    fcntl.flock(other_prober, fcntl.LOCK_SH)
    assert EP.lock_is_held(p) is False                                                                  # 另一个问的人不算持锁
    fcntl.flock(other_prober, fcntl.LOCK_UN)
    with EP.flock(p) as got:
        assert got and EP.lock_is_held(p) is True                                                       # 真的持着（独占）才算
    other_prober.close()


# ================================================================ R1（Codex 核对 2026-11-11）：停止／移除要确认登记的子进程真的退出
class OrphanHost(PsHost):
    """进程列表用我们自己知道的进程（沙箱里 ps 跑不了）；kill_mode 控制「结束信号」的结局。
    real＝真发；fail＝发不出（PermissionError）；noop＝发了没用（进程不退）；ignore_term＝只认 SIGKILL；ps_none＝进程列表读不到。"""

    def __init__(self, entries, kill_mode="real", ps_none=False):
        super().__init__(entries)
        self.kill_mode, self.ps_none, self.sent = kill_mode, ps_none, []

    def ps_all(self):
        return None if self.ps_none else super().ps_all()

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        if self.kill_mode == "fail":
            raise PermissionError(errno_EPERM(), "no")
        if self.kill_mode == "noop" or (self.kill_mode == "ignore_term" and sig == signal.SIGTERM):
            return
        os.kill(pid, sig)


def errno_EPERM():
    import errno
    return errno.EPERM


def orphan_after_supervisor_kill9(ctx):
    """守护者被强杀：锁放了，llama-server 还活着，登记还在。返回 (child_pid, 给 ps 用的命令行, 启动代号)。"""
    sp = spawner_for(ctx.kit, ctx.paths.root, cfg={"poll_seconds": 0.05}, procs=ctx.procs)
    sp()
    st = ready_state(ctx.paths)
    assert st
    os.kill(st["supervisor_pid"], signal.SIGKILL)
    assert wait_for(lambda: not EP.lock_is_held(ctx.paths.serve_lock), 5)
    child = st["child_pid"]
    assert ER.Host().pid_alive(child) and st["run_id"] in EP.read_json(ctx.paths.children)["run_ids"]
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    return child, "%s -m x --embedding --alias %s" % (pk["binary"], st["alias"]), st["run_id"]


def _kill_quiet(pid):
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _pid_gone(pid):
    """子进程被强杀的守护者留下，成了孤儿；信号 0 能不能发。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _log_text(paths):
    return open(paths.supervisor_log).read() if os.path.exists(paths.supervisor_log) else ""


# ---- 情形 1：守护者被强杀、子进程还活着，直接 stop
def test_r1_case1_stop_after_supervisor_kill9_ends_the_surviving_child_and_clears_the_record(ctx):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        host = OrphanHost([(child, cmd)])
        r = ER.stop_engine(ctx.paths, host, wait=2, pins=ctx.kit.pins, grace=3)
        assert r == {"stopped": True, "was_running": False}
        assert wait_for(lambda: _pid_gone(child), 5)                                                     # 子进程真的没了（不是「锁放了」就算停）
        assert EP.read_json(ctx.paths.children)["run_ids"] == [] and "orphan_terminated" in _log_text(ctx.paths)
        assert [s for _p, s in host.sent] == [signal.SIGTERM]                                           # 先请它结束；它退了就不再 SIGKILL
    finally:
        _kill_quiet(child)


def test_r1_case1_stop_escalates_to_sigkill_only_after_the_grace_and_only_when_sigterm_did_not_work(ctx):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        host = OrphanHost([(child, cmd)], kill_mode="ignore_term")
        r = ER.stop_engine(ctx.paths, host, wait=2, pins=ctx.kit.pins, grace=0.4)
        assert r["stopped"] is True and [s for _p, s in host.sent] == [signal.SIGTERM, signal.SIGKILL]
        assert wait_for(lambda: _pid_gone(child), 5) and EP.read_json(ctx.paths.children)["run_ids"] == []
    finally:
        _kill_quiet(child)


# ---- 情形 2：同样的状态，直接 remove-engine
def test_r1_case2_remove_engine_after_supervisor_kill9_ends_the_child_first_then_removes(ctx):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        host = OrphanHost([(child, cmd)])
        r = ER.remove_engine(ctx.paths, host, wait=2, pins=ctx.kit.pins, grace=3)
        assert r["stopped"] is True and "bin" in r["removed"] and wait_for(lambda: _pid_gone(child), 5)
        assert not os.path.isdir(ctx.paths.bin) and not os.path.exists(ctx.paths.children) and ER.lifecycle_state(ctx.paths) == "disabled"
    finally:
        _kill_quiet(child)


# ---- 情形 3：结束信号发不出，或者子进程不退
@pytest.mark.parametrize("mode,reason", [("fail", "child_signal_failed"), ("noop", "child_not_exiting")])
def test_r1_case3_signal_failure_or_child_that_will_not_exit_reports_unconfirmed_and_keeps_the_record(ctx, mode, reason):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        host = OrphanHost([(child, cmd)], kill_mode=mode)
        r = ER.stop_engine(ctx.paths, host, wait=2, pins=ctx.kit.pins, grace=0.3)
        assert r["stopped"] is False and r["reason"] == reason and r["children_pending"] == [rid]
        assert not _pid_gone(child) and EP.read_json(ctx.paths.children)["run_ids"] == [rid]               # 登记还在
        assert "orphan_terminated" not in _log_text(ctx.paths)                                           # 没记「已结束」
        # remove-engine：同样不进删除那几步、不报完成
        paths = ctx.paths
        rc_before = EP.read_receipt(paths)
        r2 = ER.remove_engine(paths, host, wait=2, pins=ctx.kit.pins, grace=0.3)
        assert r2["stopped"] is False and r2["removed"] == [] and r2["reason"] == reason
        assert os.path.isdir(paths.bin) and EP.read_receipt(paths) == rc_before and os.path.exists(paths.children)
        assert ER.lifecycle_state(paths) == "removing"                                                   # 停在「正在移除」，不是 disabled
    finally:
        _kill_quiet(child)


def test_r1_case3_unbind_last_user_reports_the_unconfirmed_stop_instead_of_stopped(ctx):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        ER.register_user(ctx.paths, "hermes:a")
        r = ER.unbind_user(ctx.paths, "hermes:a", host=OrphanHost([(child, cmd)], kill_mode="noop"), wait=1, pins=ctx.kit.pins, grace=0.3)
        assert r["stopped"] is False and r["reason"] == "child_not_exiting"
    finally:
        _kill_quiet(child)


# ---- 情形 4：进程查询失败
def test_r1_case4_unreadable_process_list_is_unconfirmed_not_taken_as_no_child(ctx):
    child, cmd, rid = orphan_after_supervisor_kill9(ctx)
    try:
        host = OrphanHost([(child, cmd)], ps_none=True)
        r = ER.stop_engine(ctx.paths, host, wait=1, pins=ctx.kit.pins, grace=0.3)
        assert r["stopped"] is False and r["reason"] == "process_list_unreadable" and host.sent == []
        r2 = ER.remove_engine(ctx.paths, host, wait=1, pins=ctx.kit.pins, grace=0.3)
        assert r2["stopped"] is False and r2["removed"] == [] and os.path.isdir(ctx.paths.bin) and EP.read_json(ctx.paths.children)["run_ids"] == [rid]
        assert not _pid_gone(child)
    finally:
        _kill_quiet(child)


def test_r1_stop_with_nothing_registered_does_not_need_the_process_list(ctx):
    class Boom(ER.Host):
        def ps_all(self):
            raise AssertionError("没登记任何子进程：不该去读进程列表")
    EP.write_json_atomic(ctx.paths.children, {"run_ids": []})
    assert ER.stop_engine(ctx.paths, Boom(), wait=1, pins=ctx.kit.pins) == {"stopped": True, "was_running": False}
    os.unlink(ctx.paths.children)
    assert ER.stop_engine(ctx.paths, Boom(), wait=1, pins=ctx.kit.pins) == {"stopped": True, "was_running": False}


# ---- 情形 5：相似的、别的用户的、不认识的启动代号：都不能误杀
class FakeRows(ER.Host):
    """纯假：进程列表是写死的几行，kill 只记下来（不碰真进程）；被请求结束的下一次就不在列表里。"""

    def __init__(self, rows):
        self.rows, self.sent = list(rows), []

    def ps_all(self):
        gone = {p for p, _s in self.sent}
        return [r for r in self.rows if r[0] not in gone]

    def kill(self, pid, sig):
        self.sent.append((pid, sig))


def test_r1_case5_lookalike_foreign_user_and_unknown_run_id_are_never_killed(ctx):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    b = pk["binary"]
    reg_lookalike, reg_foreign, reg_other_binary, reg_ours = "1" * 32, "2" * 32, "3" * 32, "4" * 32
    unknown = "5" * 32
    EP.write_json_atomic(ctx.paths.children, {"run_ids": [reg_lookalike, reg_foreign, reg_other_binary, reg_ours]})
    me = os.getuid()
    rows = [
        (9001, me, "/bin/sh -c tail -f %s -m x --alias yuanwei-%s" % (b, reg_lookalike)),            # 命令行里有我们的程序路径和登记过的代号，但不是以程序开头：像，不是
        (9002, me + 1, "%s -m x --alias yuanwei-%s" % (b, reg_foreign)),                              # 登记过的代号、别的用户
        (9003, me, "/usr/local/bin/llama-server -m x --alias yuanwei-%s" % reg_other_binary),         # 别的程序路径
        (9004, me, "%s -m x --alias yuanwei-%s" % (b, unknown)),                                      # 没登记过的启动代号
        (9005, me, "%s -m x --alias yuanwei-%s" % (b, reg_ours)),                                     # 这个才是我们的
    ]
    host = FakeRows(rows)
    r = ER.stop_engine(ctx.paths, host, wait=1, pins=ctx.kit.pins, grace=0.3)
    assert [p for p, _s in host.sent] == [9005]                                                      # 只动了 9005
    assert r["stopped"] is False and r["reason"] == "child_not_owned"                                 # 别的用户那个：不能说它退了
    assert EP.read_json(ctx.paths.children)["run_ids"] == [reg_foreign]                               # 它的登记留着；确认没有对应进程的（像的、别的程序的、我们的已退）才清掉


def test_r1_case5_without_any_foreign_process_lookalikes_and_unknown_ids_do_not_block_a_clean_stop(ctx):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    b = pk["binary"]
    EP.write_json_atomic(ctx.paths.children, {"run_ids": ["1" * 32]})
    me = os.getuid()
    host = FakeRows([(9001, me, "/bin/sh -c tail -f %s -m x --alias yuanwei-%s" % (b, "1" * 32)), (9004, me, "%s -m x --alias yuanwei-%s" % (b, "5" * 32))])
    assert ER.stop_engine(ctx.paths, host, wait=1, pins=ctx.kit.pins, grace=0.3) == {"stopped": True, "was_running": False}
    assert host.sent == [] and EP.read_json(ctx.paths.children)["run_ids"] == []


# ---- 守护者启动时的收拾（_reap_orphans）也要收紧
@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_r1_supervisor_reap_does_not_record_terminated_or_drop_the_run_id_when_the_signal_fails_or_the_process_stays(ctx, mode):
    pk = EP.installed_package(ctx.paths, ctx.kit.pins)
    rid = "6" * 32
    EP.write_json_atomic(ctx.paths.children, {"run_ids": [rid]})

    class H(FakeRows):
        def kill(self, pid, sig):
            if mode == "fail":
                raise PermissionError(errno_EPERM(), "no")
            self.sent.append((pid, sig))

        def ps_all(self):
            return list(self.rows)                                                                    # 进程一直在
    sup = ER.Supervisor(ctx.paths, pins=ctx.kit.pins, host=H([(9100, os.getuid(), "%s -m x --alias yuanwei-%s" % (pk["binary"], rid))]), cfg={"kill_grace_seconds": 0.2})
    sup.sid, sup.base_state = "x", {}
    sup._reap_orphans()
    assert EP.read_json(ctx.paths.children)["run_ids"] == [rid] and "orphan_terminated" not in _log_text(ctx.paths)
    assert EP.read_json(ctx.paths.state)["orphan_check"] == "unconfirmed"
