"""本机向量的检测、下载、核验（安装向导和 yuanwei vectors setup 用）。

* 所有会碰外界的东西（命令在不在、跑命令、HTTP、磁盘空间）都走 System 这一个对象，测试里换成假的（假 PATH、假脚本、进程内的假服务）。
* 检测只读：命令在不在 PATH、本机服务的探测地址有没有响应。不启动、不安装、不下载。
* 下载只在用户同意之后，由本机的 Ollama／LM Studio 自己去做（调它们的命令）；原委自己不联网下载。
* 核验：服务响应、模型在、算几句固定的话（维度、确定性、相近句比无关句更近、范数），组装编码身份。取不到的项不写「未知」：
  取不到模型文件 sha256（或运行包版本、加载上下文长度）就用服务自己报的名字、量化、大小、摘要代替，并标「弱身份」——换了模型文件可能认不出来。
* 默认值（模型名、命令、地址）全部在 catalog.RUNTIMES，标「真机试装时核」。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from . import catalog as CT
from . import vectors as VX
from .errors import ProviderError

SENT_A, SENT_B, SENT_C = "我喜欢喝咖啡。", "我爱喝咖啡。", "火车几点出发？"
WEAK = "unreported"


class System:
    """真实的外界：测试里换成假的。"""

    def __init__(self, env=None, opener=None):
        self.env = os.environ if env is None else env
        self.opener = opener or urllib.request.urlopen
        # 自带向量引擎的外界（测试里换成假的）：钉住表、下载器、进程外界、本机回环的开口、拉起守护者的办法
        self.pins = None
        self.fetcher = None
        self.engine_host = None
        self.engine_opener = None
        self.engine_spawner = None
        self.engine_dir = None

    def platform_info(self):
        """{"system","machine","arm64_hardware"}：硬件是不是 M 系列用 sysctl 问（Python 在 Rosetta 下跑时 platform.machine() 会报 x86_64）。"""
        import platform
        from . import enginerun as ER
        return {"system": platform.system(), "machine": platform.machine(), "arm64_hardware": ER.Host().hardware_arm64() if platform.system() == "Darwin" else None}

    def which(self, cmd):
        return shutil.which(cmd, path=self.env.get("PATH")) if cmd else None

    def run(self, argv, timeout=None):
        """跑一个命令，返回 (退出码, 标准输出, 标准错误)。命令起不来返回 (None, "", 类别)。"""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=dict(self.env))
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return None, "", "timeout"
        except OSError as e:
            return None, "", type(e).__name__

    def http(self, method, url, body=None, timeout=2.0):
        """(状态码, 解析后的 JSON 或 None)。连不上返回 (None, None)。只在调用方确认过地址许可之后用。"""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=timeout) as r:
                raw = r.read()
                code = getattr(r, "status", 200)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError):
            return None, None
        try:
            return code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return code, None

    def free_bytes(self, path):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def find_uv(self, interpreter, hermes_home, wanted=None):
        return find_uv_in(interpreter, hermes_home, wanted)


def _origin(url):
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url


# ---------------------------------------------------------------- 检测（只读）
def detect(sys_, runtimes=None, cmds=None):
    """每种运行方式一条：{id, name, tested, cmd_path, service_up, base_url, model, official_url, blurb}。cmds＝{id: 命令路径}（覆盖 PATH 里找）。"""
    out = []
    for rt in (runtimes or CT.RUNTIMES):
        if rt.get("managed"):                       # 自带引擎不在这里检测（它没有命令和固定地址）
            continue
        cmd = (cmds or {}).get(rt["id"]) or rt["cmd"]
        path = sys_.which(cmd) if not os.path.isabs(cmd) else (cmd if os.path.exists(cmd) else None)
        up = False
        if VX.is_local_url(rt["base_url"]):
            code, _ = sys_.http("GET", _origin(rt["base_url"]) + rt["probe_path"], timeout=2.0)
            up = code is not None and 200 <= code < 300
        out.append({"id": rt["id"], "name": rt["name"], "tested": rt["tested"], "cmd_path": path, "service_up": up, "base_url": rt["base_url"], "model": rt["model"],
                    "official_url": rt["official_url"], "blurb": rt["blurb"], "present": bool(path) or up})
    return out


def model_present(sys_, rt, model, base_url=None):
    """True／False／None（服务没响应，没核到）。"""
    code, js = sys_.http("GET", _origin(base_url or rt["base_url"]) + rt["models_path"], timeout=3.0)
    if code is None or not (200 <= code < 300) or js is None:
        return None
    if rt["id"] == "ollama":
        names = [m.get("name") or m.get("model") for m in (js.get("models") or []) if isinstance(m, dict)]
    else:
        names = [m.get("id") for m in (js.get("data") or []) if isinstance(m, dict)]
    return model in names


def pull(sys_, rt, model, cmd_path, timeout=None):
    """让本机的运行方式下载模型（只在用户同意之后调）。返回 (成功, 原因类别)。"""
    argv = [a.format(cmd=cmd_path, model=model) for a in rt["pull_cmd"]]
    rc, _o, err = sys_.run(argv, timeout=timeout)
    if rc == 0:
        return True, None
    return False, "timeout" if err == "timeout" else ("not_started" if rc is None else "exit_%s" % rc)


# ---------------------------------------------------------------- 核验
def _cos(a, b):
    import math
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _weak_digest(info):
    return "weak:" + hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _lms_is(m, model):
    return model in (m.get("modelKey"), m.get("path"), m.get("identifier"))


def lms_runtime_version(sys_, cmd_path):
    """`lms runtime ls` 里带 ✓ 的那一行（正在用的引擎，例如 llama.cpp-mac-arm64-apple-metal-advsimd@2.73.0）。取不到返回 None；绝不取 `lms version` 的字符画。
    有几行带 ✓ 时优先 llama.cpp 那一类（向量走它）；还是挑不出唯一的一行就不猜（None）。"""
    rc, so, _e = sys_.run([cmd_path, "runtime", "ls"], timeout=15)
    if rc != 0 or not isinstance(so, str):
        return None
    toks = []
    for line in so.splitlines():
        if "✓" in line:
            m = re.search(r"[A-Za-z0-9._+-]+@\d+(?:\.\d+)*[A-Za-z0-9.+-]*", line)
            if m:
                toks.append(m.group(0))
    llama = [t for t in toks if t.startswith("llama.cpp")]
    pool = llama or toks
    return pool[0] if len(pool) == 1 else None


def lms_models_dirs(sys_):
    """LM Studio 存模型文件的目录，按先后：settings.json 里的 downloadsFolder（只读这一个键），~/.lmstudio/models，旧位置 ~/.cache/lm-studio/models。"""
    home = (getattr(sys_, "env", None) or {}).get("HOME") or os.path.expanduser("~")
    dirs = []
    try:
        with open(os.path.join(home, ".lmstudio", "settings.json"), encoding="utf-8") as f:
            v = json.load(f).get("downloadsFolder")
        v = v.strip() if isinstance(v, str) else ""
        if v.startswith("~/"):
            v = os.path.join(home, v[2:])
        if v and os.path.isabs(v):
            dirs.append(v)
    except (OSError, ValueError, AttributeError):
        pass
    dirs += [os.path.join(home, ".lmstudio", "models"), os.path.join(home, ".cache", "lm-studio", "models")]
    return dirs


def lms_model_file(sys_, path):
    """lms ls --json 里的 path 是相对模型目录的相对路径：拼到目录上找到文件。绝对路径就直接用。找不到返回 None（身份会标「弱」）。"""
    if not isinstance(path, str) or not path:
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    for d in lms_models_dirs(sys_):
        p = os.path.join(d, path)
        if os.path.isfile(p):
            return p
    return None


def _runtime_facts(sys_, rt, model, base_url, cmd_path):
    """向运行方式问：模型文件 sha256、运行包版本、加载上下文长度、量化等。取不到的是 None。【真机试装时核：各家返回的字段名】"""
    origin = _origin(base_url)
    facts = {"file_sha256": None, "runtime_version": None, "context_length": None, "context_length_is_max": False, "descr": {}}
    if rt["id"] == "ollama":
        code, js = sys_.http("GET", origin + "/api/version", timeout=3.0)
        if js and isinstance(js.get("version"), str):
            facts["runtime_version"] = js["version"]
        code, js = sys_.http("POST", origin + "/api/show", {"model": model}, timeout=5.0)
        if isinstance(js, dict):
            m = re.search(r"sha256-([0-9a-f]{64})", js.get("modelfile") or "")
            facts["file_sha256"] = m.group(1) if m else None
            det = js.get("details") or {}
            facts["descr"] = {"name": model, "quantization": det.get("quantization_level"), "family": det.get("family"), "parameter_size": det.get("parameter_size")}
            for k, v in (js.get("model_info") or {}).items():
                if k.endswith(".context_length") and isinstance(v, int):
                    facts["context_length"] = v
        code, js = sys_.http("GET", origin + "/api/tags", timeout=3.0)
        for m in (js or {}).get("models") or []:
            if isinstance(m, dict) and (m.get("name") == model or m.get("model") == model):
                facts["descr"]["digest"], facts["descr"]["size"] = m.get("digest"), m.get("size")
    else:
        if cmd_path:
            facts["runtime_version"] = lms_runtime_version(sys_, cmd_path)
            rc, so, _e = sys_.run([cmd_path, "ls", "--json"], timeout=15)
            if rc == 0:
                try:
                    for m in json.loads(so):
                        if isinstance(m, dict) and _lms_is(m, model):
                            facts["descr"] = {"name": model, "quantization": m.get("quantization"), "size": m.get("sizeBytes"), "path_tail": os.path.basename(str(m.get("path") or ""))}
                            fp = lms_model_file(sys_, m.get("path"))
                            if fp:
                                facts["file_sha256"] = _file_sha(fp)
                            if isinstance(m.get("maxContextLength"), int):
                                facts["context_length"], facts["context_length_is_max"] = m["maxContextLength"], True
                except (ValueError, TypeError):
                    pass
            rc, so, _e = sys_.run([cmd_path, "ps", "--json"], timeout=15)                    # 加载进来的那个实例的上下文长度才是算向量时真的用的；maxContextLength 只是这个模型的上限
            if rc == 0:
                try:
                    for m in json.loads(so):
                        if isinstance(m, dict) and _lms_is(m, model) and isinstance(m.get("contextLength"), int):
                            facts["context_length"], facts["context_length_is_max"] = m["contextLength"], False
                            break
                except (ValueError, TypeError):
                    pass
    return facts


def verify(sys_, rt, model, base_url, cmd_path=None, expected_dim=None, allow_remote=False, timeout=30):
    """核验一个向量服务。返回 {"ok", "reason", "identity", "weak", "probe"}；reason 只有类别（service_down、model_missing、bad_response、dimension、nondeterministic、semantic、no_identity）。
    本函数只调 embed_batch 两次（算 3 句＋1 句），不写任何东西。"""
    r = {"ok": False, "reason": None, "identity": None, "weak": False, "probe": None, "dimension": None}
    if not VX.is_local_url(base_url) and not allow_remote:
        r["reason"] = "remote_not_allowed"
        return r
    pres = model_present(sys_, rt, model, base_url)
    if pres is None:
        r["reason"] = "service_down"
        return r
    if pres is False:
        r["reason"] = "model_missing"
        return r
    client = VX.HttpEmbeddingClient(base_url, model, timeout=timeout, opener=sys_.opener)
    try:
        v3 = client.embed_batch([SENT_A, SENT_B, SENT_C])
        v1 = client.embed_batch([SENT_A])
        for v in v3 + v1:
            VX.check_vector(v, None)
    except VX.ServiceDown:
        r["reason"] = "service_down"
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
    if _cos(v3[0], v1[0]) < 0.9999:
        r["reason"] = "nondeterministic"
        return r
    if not _cos(v3[0], v3[1]) > _cos(v3[0], v3[2]):
        r["reason"] = "semantic"
        return r
    import math
    norm = math.sqrt(sum(x * x for x in v3[0]))
    facts = _runtime_facts(sys_, rt, model, base_url, cmd_path)
    reasons = []
    sha = facts["file_sha256"]
    if not sha:
        reasons.append("model_file_sha256")
        sha = _weak_digest(facts["descr"] or {"name": model})
    rv = facts["runtime_version"] or WEAK
    if rv == WEAK:
        reasons.append("runtime_version")
    cl = facts["context_length"] if facts["context_length"] is not None else WEAK
    if cl == WEAK:
        reasons.append("context_length")
    elif facts.get("context_length_is_max"):
        reasons.append("context_length_is_upper_bound")                                # 只取到模型的上限，不是加载时用的长度：身份标弱
    weak = bool(reasons)
    ident = {"provider": rt["id"] if rt.get("id") else "custom", "model": model, "model_file_sha256": sha, "runtime_version": rv, "context_length": cl, "dimension": dim,
             "normalization": "l2" if abs(norm - 1.0) < 1e-3 else "none", "prefix": "none", "template": "none", "text_processing": "verbatim"}
    try:
        VX.identity_fp(ident)
    except Exception:                                                       # noqa: BLE001
        r["reason"] = "no_identity"
        return r
    r.update(ok=True, identity=ident, weak=weak, weak_reasons=reasons, tested=CT.compare_to_tested(ident, weak), probe=[list(v3[0]), list(v3[1])])
    return r


def preview_tested(sys_, rt, model, base_url, cmd_path):
    """计划阶段的预判（只读：问 lms 命令、读模型文件算 SHA-256，不算向量）：LM Studio 的身份和测过的比，是哪一档。维度、归一化等要核验时才知道，这里按测过的值假设。
    不是 LM Studio、没有命令、取不到：返回 None（计划里只写核验以后会比对）。"""
    if rt.get("id") != "lmstudio" or not cmd_path:
        return None
    facts = _runtime_facts(sys_, rt, model, base_url, cmd_path)
    reasons = []
    sha = facts["file_sha256"]
    if not sha:
        reasons.append("model_file_sha256")
        sha = _weak_digest(facts["descr"] or {"name": model})
    rv = facts["runtime_version"] or WEAK
    if rv == WEAK:
        reasons.append("runtime_version")
    cl = facts["context_length"] if facts["context_length"] is not None else WEAK
    if cl == WEAK:
        reasons.append("context_length")
    elif facts.get("context_length_is_max"):
        reasons.append("context_length_is_upper_bound")
    t = CT.TESTED_EMBED_IDENTITY
    ident = {"provider": "lmstudio", "model": model, "model_file_sha256": sha, "runtime_version": rv, "context_length": cl, "dimension": t["dimension"], "normalization": t["normalization"],
             "prefix": "none", "template": "none", "text_processing": "verbatim"}
    return CT.compare_to_tested(ident, bool(reasons))


def custom_runtime(base_url, model):
    """手填的向量服务：只给地址和模型名，探测和模型列表都走 OpenAI 兼容的 /v1/models。"""
    return {"id": "custom", "name": "自己的向量服务", "tested": False, "cmd": None, "base_url": base_url, "probe_path": "/v1/models", "models_path": "/v1/models", "model": model,
            "pull_cmd": None, "official_url": None, "blurb": ""}


# ---------------------------------------------------------------- NumPy（要装在 Hermes 实际导入包的那个环境里）
# 两样东西要分清：「跑 Hermes 的解释器」（Hermes 自己的 tools/python-…，带 -I 启动，本身不装第三方包）；和「装包的环境」（installs/…/venv，Hermes 的启动代码把它的 site-packages 放进 sys.path）。
# NumPy 要装在后一个里，查有没有 NumPy 也要用 Hermes 实际导入的那条路。找的办法（2026-11-11 真机试装后改）：
#   1. --hermes-python；2. 问 Hermes 自己（hermes --print-runtime-command 给出启动命令，换成 site 模块跑一下，看到它真实的 sys.path，从里面找带 pyvenv.cfg 的环境）；3. 旧的两条路（hermes 脚本第一行的解释器、<家目录>/hermes-agent/venv）。
# 每个候选都核：真的是 Python（跑一段代码、读回版本）、版本不低于 3.11、而且它的 site-packages 在 Hermes 实际的 sys.path 里（问不到 Hermes 的 sys.path 时没法核这一条，标「没核到对得上」）。/bin/sh 之类不是 Python 的一律不收。
MIN_PY = (3, 11)
PY_PROBE_TAG = "YWPROBE"
PY_PROBE = ("import sys, site, json, importlib.util; "
            "print('%s' + json.dumps({'v': list(sys.version_info[:3]), 'sp': list(site.getsitepackages()), 'pip': importlib.util.find_spec('pip') is not None}))" % PY_PROBE_TAG)
NUMPY_REQ = "numpy>=1.26"


def probe_python(sys_, python):
    """跑一次，确认这真的是个 Python：返回 {"python","version":(a,b,c),"site_packages":[...],"has_pip":bool}；起不来、不是 Python、读不回结果都是 None。"""
    if not isinstance(python, str) or not python:
        return None
    rc, out, _e = sys_.run([python, "-I", "-c", PY_PROBE], timeout=30)
    if rc != 0 or not isinstance(out, str):
        return None
    for line in reversed(out.splitlines()):
        if line.startswith(PY_PROBE_TAG):
            try:
                d = json.loads(line[len(PY_PROBE_TAG):])
                v = tuple(int(x) for x in d["v"])
                sp = [str(x) for x in d["sp"]]
                if len(v) != 3:
                    return None
                return {"python": python, "version": v, "site_packages": sp, "has_pip": bool(d.get("pip"))}
            except (ValueError, TypeError, KeyError):
                return None
    return None


def _real(p):
    try:
        return os.path.realpath(p)
    except OSError:
        return p


def parse_site_path(text):
    """从 `python -m site` 的输出里取 sys.path（一个 Python 列表的写法）。读不出返回 None。"""
    if not isinstance(text, str):
        return None
    i = text.find("sys.path = [")
    if i < 0:
        return None
    j = text.find("]", i)
    if j < 0:
        return None
    import ast
    try:
        lst = ast.literal_eval(text[i + len("sys.path = "):j + 1])
    except (ValueError, SyntaxError):
        return None
    return [str(x) for x in lst] if isinstance(lst, list) else None


def hermes_runtime(sys_, hermes_cmd):
    """问 Hermes 自己：{"interpreter": 跑它的解释器, "sys_path": 它实际的 sys.path}。问不到的项是 None（不当成「没有」）。
    办法：hermes --print-runtime-command --module site 给出启动命令（JSON 列表，第一个是解释器、带 -I），照它的原样跑一遍 site 模块，读 sys.path。"""
    out = {"interpreter": None, "sys_path": None, "command_ok": False}
    if not hermes_cmd:
        return out
    rc, so, _e = sys_.run([hermes_cmd, "--print-runtime-command", "--module", "site"], timeout=60)
    if rc != 0 or not isinstance(so, str):
        return out
    cmd = None
    for line in reversed(so.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                cmd = json.loads(line)
            except ValueError:
                cmd = None
            break
    if not (isinstance(cmd, list) and len(cmd) >= 4 and all(isinstance(x, str) for x in cmd) and cmd[1] == "-I" and cmd[2] == "-c"):
        return out
    out["interpreter"], out["command_ok"] = cmd[0], True
    rc, so, _e = sys_.run(cmd, timeout=120)
    if rc == 0:
        out["sys_path"] = parse_site_path(so)
    return out


def _env_roots_from_path(sys_path):
    """sys.path 里每个 .../lib/pythonX.Y/site-packages（或 Lib/site-packages），往上找带 pyvenv.cfg 的环境根，返回 [(环境根, site-packages)]，保持 sys.path 的顺序。"""
    seen, out = set(), []
    for entry in sys_path or []:
        if not entry.rstrip("/\\").endswith("site-packages"):
            continue
        for up in (3, 2):
            root = entry
            for _ in range(up):
                root = os.path.dirname(root)
            if root and root not in seen and os.path.isfile(os.path.join(root, "pyvenv.cfg")):
                seen.add(root)
                out.append((root, entry))
                break
    return out


def _venv_python(root):
    for rel in (("bin", "python"), ("bin", "python3"), ("Scripts", "python.exe")):
        p = os.path.join(root, *rel)
        if os.path.exists(p):
            return p                                                                    # 不解析符号链接：venv 里的 python 就是靠这个路径才认得自己的 site-packages
    return None


def _accept(sys_, python, source, sys_path, tried):
    """核一个候选：返回 env 字典，或 None（原因记进 tried）。"""
    info = probe_python(sys_, python)
    if info is None:
        tried.append({"source": source, "python": python, "rejected": "not_python"})
        return None
    if info["version"] < MIN_PY:
        tried.append({"source": source, "python": python, "rejected": "too_old", "version": ".".join(map(str, info["version"]))})
        return None
    match = None
    if sys_path is not None:
        want = {_real(p) for p in sys_path}
        match = any(_real(p) in want for p in info["site_packages"])
        if not match:
            tried.append({"source": source, "python": python, "rejected": "not_hermes_environment"})
            return None
    return {"python": python, "source": source, "version": ".".join(map(str, info["version"])), "has_pip": info["has_pip"], "site_packages": info["site_packages"], "matches_hermes": match}


def find_hermes_env(sys_, hermes_home, hermes_cmd=None, explicit=None):
    """装包的环境。返回 {"python": 路径或 None, "source", "version", "has_pip", "interpreter": 跑 Hermes 的解释器（问到才有）, "matches_hermes": True｜None(没法核)｜（False 的候选不收）,
    "uv": Hermes 自带的 uv（有才有）, "tried": [被拒绝的候选和原因]}。找不到：python 是 None，tried 里写原因。"""
    tried = []
    cmd = hermes_cmd or sys_.which("hermes")
    rt = hermes_runtime(sys_, cmd) if cmd else {"interpreter": None, "sys_path": None, "command_ok": False}
    sp = rt["sys_path"]
    found = None
    if explicit:
        found = _accept(sys_, explicit, "explicit", sp, tried)
    else:
        if sp:
            for root, _entry in _env_roots_from_path(sp):
                py = _venv_python(root)
                if py:
                    found = _accept(sys_, py, "hermes_runtime", sp, tried)
                    if found:
                        break
        if found is None:
            if cmd and os.path.isfile(cmd):
                try:
                    with open(cmd, "rb") as f:
                        first = f.readline().decode("utf-8", "replace").strip()
                    if first.startswith("#!"):
                        parts = first[2:].split()
                        if parts and os.path.isabs(parts[0]) and os.path.exists(parts[0]) and not parts[0].endswith("env"):
                            found = _accept(sys_, parts[0], "launcher_shebang", sp, tried)
                except OSError:
                    pass
            if found is None:
                cand = os.path.join(str(hermes_home), "hermes-agent", "venv", "bin", "python")
                if os.path.exists(cand):
                    found = _accept(sys_, cand, "legacy_venv", sp, tried)
    out = {"python": None, "source": None, "version": None, "has_pip": None, "matches_hermes": None, "site_packages": [], "interpreter": rt["interpreter"], "hermes_path_known": sp is not None, "uv": None, "tried": tried}
    if found:
        out.update(found)
        out["uv"] = sys_.find_uv(rt["interpreter"], hermes_home, _pyvenv_uv_version(found["python"]))
    return out


def find_hermes_python(sys_, hermes_home, hermes_cmd=None, explicit=None):
    """装包环境里的 python 路径（核过的）。找不到返回 None（没核到）。"""
    return find_hermes_env(sys_, hermes_home, hermes_cmd, explicit)["python"]


def _pyvenv_uv_version(python):
    """pyvenv.cfg 里 `uv = x.y.z` 那一行（环境是哪一版 uv 建的）。只读这一行。"""
    try:
        root = os.path.dirname(os.path.dirname(python))
        with open(os.path.join(root, "pyvenv.cfg"), encoding="utf-8", errors="replace") as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip() == "uv":
                    return v.strip() or None
    except OSError:
        pass
    return None


def find_uv_in(interpreter, hermes_home, wanted=None):
    """Hermes 自带的 uv：<tools>/uv-<版本>-…/uv。tools 目录取跑 Hermes 的解释器的上上上级（tools/python-…/bin/python3），再试 <家目录>/tools。优先环境建它时用的那一版，否则最新的一版。"""
    import glob
    dirs = []
    if interpreter:
        dirs.append(os.path.dirname(os.path.dirname(os.path.dirname(interpreter))))
    dirs.append(os.path.join(str(hermes_home), "tools"))
    cands = []
    for d in dirs:
        for p in glob.glob(os.path.join(glob.escape(d), "uv-*", "uv")):
            m = re.search(r"uv-(\d+(?:\.\d+)*)", os.path.basename(os.path.dirname(p)))
            if m and os.path.isfile(p) and os.access(p, os.X_OK):
                cands.append((tuple(int(x) for x in m.group(1).split(".")), m.group(1), p))
    if not cands:
        return None
    if wanted:
        for _v, ver, p in cands:
            if ver == wanted:
                return p
    return sorted(cands)[-1][2]


def numpy_install_command(env):
    """装 NumPy 的命令（一个列表）。环境里有 pip 才用 pip；没有就用 Hermes 自带的 uv：<uv> pip install --python <环境的 python> numpy。都没有返回 None。"""
    if not env or not env.get("python"):
        return None
    if env.get("has_pip"):
        return [env["python"], "-m", "pip", "install", NUMPY_REQ]
    if env.get("uv"):
        return [env["uv"], "pip", "install", "--python", env["python"], NUMPY_REQ]
    return None


def has_numpy(sys_, python):
    """True／False／None（找不到 Python，没核到）。用的是装包环境自己的 Python（带 -I，和 Hermes 启动方式一样不读用户目录）。"""
    if not python:
        return None
    rc, out, _e = sys_.run([python, "-I", "-c", "import numpy; print(numpy.__version__)"], timeout=60)
    if rc is None:
        return None
    return rc == 0


def install_numpy(sys_, env):
    """用户同意以后才调：往 Hermes 实际导入包的那个环境里装 NumPy。env 是 find_hermes_env 的结果。返回 (成功, 原因类别)。装完再用同一个办法查一遍：查到才算成功；查出没有是 still_missing；查不了（进程起不来）是 unconfirmed_after_install，不当成功。"""
    cmd = numpy_install_command(env)
    if cmd is None:
        return False, "no_installer"
    rc, _o, err = sys_.run(cmd, timeout=900)
    if rc != 0:
        return False, "timeout" if err == "timeout" else ("not_started" if rc is None else "exit_%s" % rc)
    got = has_numpy(sys_, env["python"])
    if got is True:
        return True, None                                                                # 只有查到了才算确认
    return False, ("still_missing" if got is False else "unconfirmed_after_install")      # None＝装完以后查的进程起不来：没能确认，既不说缺包也不说成功


def plugin_declares_numpy(hermes_home):
    """装在 <家目录>/plugins/yuanwei/plugin.yaml 里的依赖声明有没有 numpy：True／False／None（文件不在或读不了，没核到）。
    不用 YAML 库：只看 python_dependencies（或 pip_dependencies）下面列的条目。"""
    try:
        with open(os.path.join(str(hermes_home), "plugins", "yuanwei", "plugin.yaml"), encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^(?:python_dependencies|pip_dependencies):[ \t]*\n((?:[ \t]+-[ \t]*.*\n?)*)", text, re.M)
    return bool(m and re.search(r"^[ \t]+-[ \t]*[\"']?numpy(?![A-Za-z0-9_])", m.group(1), re.M | re.I))


def numpy_status(sys_, hermes_home, hermes_cmd=None, explicit=None):
    """`yuanwei status hermes` 的一项：Hermes 运行环境里有没有 NumPy。用和向导同一个办法找环境、同一个办法查。只读（只跑查询，不装）。"""
    env = find_hermes_env(sys_, hermes_home, hermes_cmd, explicit)
    has = has_numpy(sys_, env["python"])
    out = {"has_numpy": has, "python": env["python"], "interpreter": env["interpreter"], "found_by": env["source"], "matches_hermes": env["matches_hermes"], "install_command": None, "note": None, "plugin_declares_numpy": plugin_declares_numpy(hermes_home)}
    if env["python"] is None:
        out["note"] = "没找到 Hermes 实际用的 Python 环境，没能确认有没有 NumPy；可以用 --hermes-python 告诉原委它在哪（没有 NumPy 时读取会自动只用关键词）"
        out["tried"] = env["tried"]
    elif has is False:
        out["install_command"] = numpy_install_command(env)
        decl = out["plugin_declares_numpy"]
        why = "Hermes 可能重建了它的包环境（重建以后新环境里没有我们装的 NumPy）。"
        if decl is True:
            why += "原委的插件文件里已经声明了需要 NumPy，Hermes 下次重建或同步环境时会带上；现在就要用，"
        else:
            why += "原委的插件文件里没有找到 NumPy 的依赖声明（可能是装得比较早：重新运行 yuanwei setup 会更新插件文件，以后 Hermes 重建环境时才会带上）；现在就要用，"
        out["note"] = ("这个环境里没有 NumPy：向量用不了，读取已自动退回只用关键词。" + why + "运行 install_command 那一条装回（要你自己运行）"
                       if out["install_command"] else "这个环境里没有 NumPy，也没找到装的办法（没有 pip，也没找到 Hermes 自带的 uv）：向量用不了，读取已自动退回只用关键词。" + why + "请自己装")
    elif has is None:
        out["note"] = "没能运行那个 Python，没核到有没有 NumPy"
    return out


# ---------------------------------------------------------------- 长输入探针（全本机：防止本机服务悄悄把输入截掉）
PROBE_CODE = "青鸟7391"
PROBE_FILLER = "这是一段用来占位的文字，内容和问题无关。" * 2000          # 约 4 万个汉字，按 1.5 字一个词元估约 2.7 万词元（粗估，没测）


def long_input_probe(sys_, base_url, model, params, timeout=300):
    """向本机大模型发一段很长的输入（暗号放在最前面），问它暗号是什么。返回 ("ok"｜"truncated"｜"service_down"｜"bad_response")。不花钱。"""
    body = dict(params or {})
    body.pop("max_tokens", None)
    body.update(model=model, max_tokens=64, messages=[{"role": "user", "content": "暗号：%s。\n%s\n\n请只回答文档开头的暗号，别的都不要说。" % (PROBE_CODE, PROBE_FILLER)}])
    code, js = sys_.http("POST", base_url.rstrip("/") + "/chat/completions", body, timeout=timeout)
    if code is None:
        return "service_down"
    try:
        text = js["choices"][0]["message"]["content"]
    except (TypeError, KeyError, IndexError):
        return "bad_response"
    return "ok" if PROBE_CODE in (text or "") else "truncated"


def engine_supported(info):
    """自带引擎现在只支持 Mac 的 M 系列芯片：系统是 Darwin，并且硬件是 arm64（Rosetta 下 machine 报 x86_64 但硬件是 arm64 也算）。"""
    if (info or {}).get("system") != "Darwin":
        return False
    return (info.get("machine") == "arm64") or bool(info.get("arm64_hardware"))
