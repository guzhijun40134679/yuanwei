"""向导、向量、密钥测试用的假外界：进程内的假 HTTP 服务（假 Ollama／LM Studio／整理供应商）、假系统（假 PATH、假命令）、脚本化输入。绝不联网，不碰本机已装的东西。"""
import hashlib
import io as _io
import json
import os
import urllib.error

from yuanwei import embedsetup as ES

FAKE_KEY = "sk-FAKEKEY1234567890abcdefXYZ"


def embed_text(text, dim=1024):
    """确定性的假向量：字符袋哈希。字重合多的两句余弦高（相近句比无关句近）。"""
    v = [0.0] * dim
    for ch in text:
        v[int(hashlib.sha1(ch.encode("utf-8")).hexdigest(), 16) % dim] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """假服务。runtime：ollama／lmstudio／custom；models＝服务里有的模型名；down＝整个连不上；fail＝{路径片段: 状态码}；dim＝向量维度；
    embed_override(text)->向量 可以换掉算法（模拟换了模型）。calls 记下每一次请求（方法、路径、解析后的请求体、请求头里有没有 Authorization）。"""

    def __init__(self, runtime="ollama", models=("qwen3-embedding:0.6b",), dim=1024, down=False, fail=None, chat_reply="ok", chat_status=200, show_sha=True, version="0.9.0", ports="auto"):
        self.runtime, self.models, self.dim, self.down, self.fail = runtime, list(models), dim, down, dict(fail or {})
        self.ports = {"ollama": {11434}, "lmstudio": {1234}}.get(runtime) if ports == "auto" else ports      # 只在这些端口上有服务（None＝哪儿都有）
        self.chat_reply, self.chat_status, self.show_sha, self.version = chat_reply, chat_status, show_sha, version
        self.calls = []
        self.embed_override = None
        self.bad_embed = None            # 可设成 "zero"／"nan"／"garbage"

    def __call__(self, req, timeout=None):
        url = req.full_url
        path = "/" + url.split("//", 1)[1].split("/", 1)[1] if "//" in url and "/" in url.split("//", 1)[1] else "/"
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        auth = req.get_header("Authorization")
        self.calls.append({"method": req.get_method(), "path": path, "body": body, "auth": bool(auth), "url": url})
        port = int(url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1]) if ":" in url.split("//", 1)[1].split("/", 1)[0].replace("[::1]", "") else 80
        if self.down or (self.ports is not None and port not in self.ports):
            raise urllib.error.URLError("refused")
        for frag, code in self.fail.items():
            if frag in path:
                raise urllib.error.HTTPError(url, code, "err", {}, _io.BytesIO(b"{}"))
        js = self._route(req.get_method(), path, body)
        if js is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, _io.BytesIO(b"{}"))
        return _Resp(200, json.dumps(js).encode("utf-8"))

    def _route(self, method, path, body):
        if path == "/api/version":
            return {"version": self.version} if self.runtime == "ollama" else None
        if path == "/api/tags":
            return {"models": [{"name": m, "model": m, "digest": "d" * 64, "size": 639000000} for m in self.models]} if self.runtime == "ollama" else None
        if path == "/api/show":
            if body and body.get("model") in self.models:
                return {"modelfile": ("FROM /models/blobs/sha256-%s\n" % ("a" * 64)) if self.show_sha else "FROM x\n", "details": {"quantization_level": "Q8_0", "family": "qwen3"},
                        "model_info": {"qwen3.context_length": 32768}}
            return None
        if path == "/v1/models":
            return {"data": [{"id": m} for m in self.models]}
        if path == "/v1/embeddings":
            texts = body["input"]
            if self.bad_embed == "garbage":
                return {"nope": 1}
            out = []
            for i, t in enumerate(texts):
                if self.bad_embed == "zero":
                    v = [0.0] * self.dim
                elif self.bad_embed == "nan":
                    v = [float("nan")] * self.dim
                else:
                    v = (self.embed_override or (lambda x: embed_text(x, self.dim)))(t)
                out.append({"index": i, "embedding": v})
            return {"data": out}
        if path.endswith("/chat/completions"):
            if self.chat_status != 200:
                raise urllib.error.HTTPError("x", self.chat_status, "e", {}, _io.BytesIO(b"{}"))
            return {"choices": [{"message": {"content": self.chat_reply(body) if callable(self.chat_reply) else self.chat_reply}, "finish_reason": "stop"}]}
        return None

    def posted(self, frag):
        return [c for c in self.calls if frag in c["path"]]


class FakeMulti:
    """两个假服务一起开（Ollama 在 11434、LM Studio 在 1234）：按端口分发。calls 是两边合起来的。"""

    def __init__(self, *services):
        self.services = services

    def __call__(self, req, timeout=None):
        last = None
        for s in self.services:
            try:
                return s(req, timeout)
            except urllib.error.URLError as e:
                if getattr(e, "code", None) is not None:
                    raise
                last = e
        raise last

    @property
    def calls(self):
        return [c for s in self.services for c in s.calls]

    def posted(self, frag):
        return [c for c in self.calls if frag in c["path"]]


class FakeSystem(ES.System):
    """假系统：env 是个字典（假 PATH）；commands＝{命令名: 函数(argv)->(rc, out, err)}；which 只认 commands 里有的；run 只跑 commands 里的，记下每次。"""

    def __init__(self, http=None, commands=None, env=None, files=None, free=10 * 2 ** 30, platform=None):
        self.http_fake = http or FakeHTTP()
        super().__init__(env=dict(env or {}), opener=self.http_fake)
        self._platform = platform or {"system": "Linux", "machine": "x86_64", "arm64_hardware": False}        # 默认「不支持自带引擎」：原来的向导测试不受影响
        self.commands = dict(commands or {})
        self.ran = []
        self._free = free
        self.py_version, self.py_pip = (3, 12, 0), True

    def platform_info(self):
        return dict(self._platform)

    def which(self, cmd):
        return ("/fake/bin/" + cmd) if cmd in self.commands else None

    def run(self, argv, timeout=None):
        self.ran.append(list(argv))
        name = os.path.basename(argv[0])
        if name not in self.commands:
            return None, "", "FileNotFoundError"
        if list(argv[1:4]) == ["-I", "-c", ES.PY_PROBE]:                                        # 「这真的是个 Python 吗」的核查：假 Python 按 py_version／py_pip 回答，不交给各个测试的命令函数
            return 0, "YWPROBE" + json.dumps({"v": list(self.py_version), "sp": ["/fake/site/" + name], "pip": self.py_pip}) + "\n", ""
        return self.commands[name](argv)

    def find_uv(self, interpreter, hermes_home, wanted=None):
        return "/fake/bin/uv" if "uv" in self.commands else None

    def free_bytes(self, path):
        return self._free


class FakeIO:
    """脚本化输入：answers 是依次给的回答；secrets 是依次给的不回显输入。q／用完＝取消。out 是打印出来的全部文字。"""

    def __init__(self, answers=(), secrets=(), tty=True):
        self.answers, self.secrets, self._tty, self.out, self.asked = list(answers), list(secrets), tty, [], []

    def tty(self):
        return self._tty

    def say(self, text=""):
        self.out.append(str(text))

    def ask(self, prompt, default=None):
        from yuanwei.wizard import Cancel
        self.asked.append(prompt)
        if not self.answers:
            raise Cancel()
        v = str(self.answers.pop(0)).strip()
        if v.lower() == "q":
            raise Cancel()
        return v if v else (default if default is not None else "")

    def secret(self, prompt):
        from yuanwei.wizard import Cancel
        self.asked.append("[secret]" + prompt)
        if not self.secrets:
            raise Cancel()
        return self.secrets.pop(0)

    @property
    def text(self):
        return "\n".join(self.out)


def tree_bytes(root):
    """目录里全部文件的 (相对路径, 字节) 字典，用来核「什么都没改」。"""
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def make_home(tmp_path, name="hh"):
    h = tmp_path / name
    h.mkdir(parents=True, exist_ok=True)
    (h / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    return str(h)
