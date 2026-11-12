"""假的 llama-server（测试用，不是真的 llama.cpp）。照设计里假定的命令行和接口写，只用标准库，监听本机回环地址。

它只是「我们假定的 llama-server 的样子」：命令行参数名、接口路径、返回格式都是施工方凭记忆写的，**没有对着真的 llama.cpp 核过**（设计里全部标「真机核」）。
所以用它测的是原委自己的逻辑（拉起、锁、退避、空闲退出、核验流程），不是 llama.cpp 的行为。

用法（和真的一样）：python fake_llama_server.py -m <文件> --embedding --pooling last --host 127.0.0.1 --port N -c 4096 -b 4096 -ub 4096 -t 4 -np 1 --alias NAME -ngl 99 --no-webui
参数名不认识：打印 error 并以退出码 1 退出（模拟「参数名在这个版本里变了」）。

行为由环境变量控制（FAKE_LLAMA_*）：
  START_DELAY=秒       启动后这么久以内 /health 回 503（模拟加载模型）
  CRASH_AFTER=n        处理 n 个 /v1/embeddings 请求以后直接退出（退出码 1，模拟崩溃）
  DIE_AT_START=signal  一启动就给自己发 SIGKILL（模拟被系统拦住）
  EXIT_AT_START=n      一启动就以退出码 n 退出
  DUMP_ENV=路径        把收到的环境变量名（不含值）写进这个文件
  DUMP_ARGV=路径       把命令行参数（JSON）写进这个文件
  LOG_BODIES=1         把请求里的文字写到标准错误（模拟「开了详细日志会把正文打出来」）
  DIM=n                向量维度（默认 1024）
  POOLING_BREAKS=1     不按 last 池化时给出不同的向量（用来演示池化方式写错会让向量不同）
  MODEL_NAME=名字      /v1/models 里报的模型名（默认用 --alias）
  STDERR_NOISE=文字    启动后往标准错误写一行这样的文字（模拟「真实风格的日志行里带正文」；原委不许把它存下来）
  LIFETIME=秒         启动后活这么久就自己退出（退出码 1）：模拟「就绪几秒就崩」
  HOLD_MS=毫秒         每个 /v1/embeddings 请求先睡这么久（模拟在途请求）
带 --api-key-file：除了 /health，所有接口都要 Authorization: Bearer <文件内容>，否则 401。不带 --no-slots：/slots 返回 200；不带 --no-webui：GET / 返回 200。
"""
import hashlib
import http.server
import json
import os
import signal
import sys
import threading
import time

KNOWN_FLAGS = {"--embedding": 0, "--pooling": 1, "--host": 1, "--port": 1, "-m": 1, "-c": 1, "-b": 1, "-ub": 1, "-t": 1, "-np": 1, "--alias": 1, "-ngl": 1, "--no-webui": 0, "--no-ui": 0, "--no-slots": 0, "--api-key-file": 1}


def embed_text(text, dim=1024, pooling="last"):
    """和 helpers_wizard.embed_text 同一个算法（字符袋哈希、L2 归一）；pooling 不是 last 且 POOLING_BREAKS 打开时，换一种算法。"""
    v = [0.0] * dim
    salt = "" if (pooling == "last" or not os.environ.get("FAKE_LLAMA_POOLING_BREAKS")) else "mean:"
    for ch in text:
        v[int(hashlib.sha1((salt + ch).encode("utf-8")).hexdigest(), 16) % dim] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def parse_args(argv):
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a not in KNOWN_FLAGS:
            sys.stderr.write("error: invalid argument: %s\n" % a)
            sys.exit(1)
        n = KNOWN_FLAGS[a]
        opts[a] = argv[i + 1] if n else True
        i += 1 + n
    return opts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ
    if env.get("FAKE_LLAMA_DUMP_ARGV"):
        with open(env["FAKE_LLAMA_DUMP_ARGV"], "w") as f:
            json.dump(argv, f)
    if env.get("FAKE_LLAMA_DUMP_ENV"):
        with open(env["FAKE_LLAMA_DUMP_ENV"], "w") as f:
            json.dump(sorted(env.keys()), f)
    opts = parse_args(argv)
    if env.get("FAKE_LLAMA_DIE_AT_START"):
        os.kill(os.getpid(), signal.SIGKILL)
    if env.get("FAKE_LLAMA_EXIT_AT_START"):
        sys.exit(int(env["FAKE_LLAMA_EXIT_AT_START"]))
    if "-m" not in opts or not os.path.isfile(opts["-m"]):
        sys.stderr.write("error: failed to load model: %s\n" % opts.get("-m"))
        sys.exit(1)
    if "--embedding" not in opts:
        sys.stderr.write("error: embeddings are disabled\n")
        sys.exit(1)
    pooling = opts.get("--pooling", "none")
    dim = int(env.get("FAKE_LLAMA_DIM", "1024"))
    alias = env.get("FAKE_LLAMA_MODEL_NAME") or opts.get("--alias") or os.path.basename(opts["-m"])
    t0 = time.monotonic()
    delay = float(env.get("FAKE_LLAMA_START_DELAY", "0"))
    crash_after = int(env["FAKE_LLAMA_CRASH_AFTER"]) if env.get("FAKE_LLAMA_CRASH_AFTER") else None
    state = {"n": 0}
    lock = threading.Lock()
    key = open(opts["--api-key-file"]).read().strip() if "--api-key-file" in opts else None
    if env.get("FAKE_LLAMA_STDERR_NOISE"):
        sys.stderr.write("log: %s\n" % env["FAKE_LLAMA_STDERR_NOISE"])
        sys.stderr.flush()
    hold = float(env.get("FAKE_LLAMA_HOLD_MS", "0")) / 1000.0

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, obj):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

        def _authed(self):
            if key is None or self.path == "/health":
                return True
            if self.headers.get("Authorization") == "Bearer " + key:
                return True
            self._send(401, {"error": {"message": "Invalid API Key", "code": 401}})
            return False

        def do_GET(self):
            if not self._authed():
                return
            if self.path == "/slots":
                return self._send(404 if "--no-slots" in opts else 200, {"slots": []})
            if self.path == "/":
                return self._send(404 if ("--no-ui" in opts or "--no-webui" in opts) else 200, {"webui": True})
            if self.path == "/health":
                if time.monotonic() - t0 < delay:
                    return self._send(503, {"error": {"message": "Loading model", "code": 503}})
                return self._send(200, {"status": "ok"})
            if self.path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": alias, "object": "model"}]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            if self.path != "/v1/embeddings":
                return self._send(404, {"error": "not found"})
            if time.monotonic() - t0 < delay:
                return self._send(503, {"error": {"message": "Loading model", "code": 503}})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            if hold:
                time.sleep(hold)
            if env.get("FAKE_LLAMA_LOG_BODIES"):
                for t in texts:
                    sys.stderr.write("prompt: %s\n" % t)
                sys.stderr.flush()
            out = [{"object": "embedding", "index": i, "embedding": embed_text(t, dim, pooling)} for i, t in enumerate(texts)]
            self._send(200, {"object": "list", "data": out, "model": alias, "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)}})
            with lock:
                state["n"] += 1
                if crash_after is not None and state["n"] >= crash_after:
                    sys.stderr.flush()
                    os._exit(1)

    if env.get("FAKE_LLAMA_LIFETIME"):
        threading.Timer(float(env["FAKE_LLAMA_LIFETIME"]), lambda: os._exit(1)).start()
    srv = http.server.ThreadingHTTPServer((opts.get("--host", "127.0.0.1"), int(opts.get("--port", "8080"))), H)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
