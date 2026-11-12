"""安装向导 yuanwei setup：一问一答，和「全部用参数、不问话」的写法，底下是同一套（resolve → plan → apply）。

* 确认计划之前不写任何文件、不改设置、不下载、不存密钥；取消（q、Ctrl-C、EOF）什么都不改。
* 只有这三类事在确认之前发生：读（环境、本机服务的只读请求）、用户点头的「试一下」（向整理供应商发一个最小请求）、装 NumPy 之前的询问。
* 执行顺序固定，任何一步失败后面都不做：① 下载模型（用户同意过）→ ② NumPy（用户同意过）→ ③ 向量核验 → ④ 本机大模型的长输入探针 → ⑤ 存密钥 → ⑥ 写文件、改 memory.provider。
* 预设、供应商、运行方式都是 catalog 里的小表。密钥不进命令行参数，只在交互式终端里收（不回显），存进 0600 的密钥文件；参数写法只收变量名。
"""
import contextlib
import getpass
import json
import os
import shlex
import sys

from . import catalog as CT
from . import embedsetup as ES
from . import enginepkg as EP
from . import enginerun as ER
from . import hermes_profile as HP
from . import hermes_setup as HS
from . import keystore as KS
from . import vectors as VX

FUTURE_HOSTS = ("claude-code", "codex", "pi")
NEW_FLAGS = ("preset", "provider", "provider_url", "provider_model", "provider_param", "api_key_env", "vectors", "embedding_model", "embedding_url", "download_model", "allow_remote_embedding",
             "install_numpy", "hermes_python", "ollama_cmd", "lms_cmd", "ignore_probe_failure", "try_key", "engine_url", "model_url", "model_file", "engine_dir")


class Cancel(Exception):
    pass


class IO:
    def __init__(self, stdin=None, stdout=None):
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout

    def tty(self):
        try:
            return self.stdin.isatty() and self.stdout.isatty()
        except (AttributeError, ValueError):
            return False

    def say(self, text=""):
        print(text, file=self.stdout)

    def ask(self, prompt, default=None):
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise Cancel()
        v = line.strip()
        if v.lower() == "q":
            raise Cancel()
        return v if v else (default if default is not None else "")

    def secret(self, prompt):
        try:
            return getpass.getpass(prompt, stream=self.stdout)
        except (EOFError, KeyboardInterrupt):
            raise Cancel()


def yes(v, default):
    v = (v or "").strip().lower()
    if not v:
        return default
    return v in ("y", "yes", "是", "好", "同意", "确认")


# ================================================================ 解析参数（不问话）
def _numpy_refuse(why):
    """装 NumPy 没成功，或装完以后没能确认：两种说法分开，不把「没能确认」说成「没成功」或「成功」。"""
    if why == "unconfirmed_after_install":
        _refuse("numpy_unconfirmed", "装 NumPy 的命令跑完了，但装完以后没能确认它在 Hermes 的 Python 里（查询没跑起来）。不知道装上没有，所以没改任何配置；可以运行 yuanwei status hermes 再查一次。")
    _refuse("numpy_install_failed", "往 Hermes 实际用的 Python 环境里装 NumPy 没成功（%s）。没改任何配置。" % why)


def _refuse(code, msg):
    raise HS.SetupRefused(code, msg)


def uses_new_path(a):
    return any(getattr(a, k, None) not in (None, False, []) for k in NEW_FLAGS)


def _provider_from_row(row, url=None, model=None, extra_params=None, key_env=None):
    d = {"id": row["id"], "name": row["name"], "label": row["label"], "base_url": url or row["base_url"], "model": model or row["model"], "params": dict(row["params"]),
         "key_env": key_env or row["key_env"], "verified": row["verified"], "local_only": row["local_only"], "note": row["note"]}
    d["params"].update(extra_params or {})
    return d


def _parse_params(items):
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k:
            _refuse("bad_provider_param", "--provider-param 要写成 键=值（值按 JSON 读），例如 temperature=0")
        try:
            out[k] = json.loads(v)
        except ValueError:
            _refuse("bad_provider_param", "--provider-param %s 的值不是 JSON" % k)
    return out


def _check_url(url, local_only, what):
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        _refuse("bad_url", "%s 的地址要以 http:// 或 https:// 开头" % what)
    if local_only:
        if not VX.is_local_url(url):
            _refuse("not_local", "%s 的地址只许本机（127.0.0.1、localhost、[::1]），现在不是" % what)
    elif not VX.is_local_url(url) and not url.startswith("https://"):
        _refuse("insecure_url", "%s 的地址不在本机，只许 https://" % what)


def resolve_args(a, sys_=None):
    """不问话的写法：把参数变成选择（choices）。缺什么、不合规就报 SetupRefused（退出码 2），不猜。"""
    if a.host in FUTURE_HOSTS:
        _refuse("future_host", "%s 是后续更新，现在还不能装；现在只有 hermes。" % a.host)
    if a.host != "hermes":
        _refuse("bad_host", "助手只能是 hermes")
    if a.api_key_env and not KS.NAME_RE.match(a.api_key_env):
        _refuse("bad_env_name", "--api-key-env 只收环境变量的名字（字母、数字、下划线），不收密钥本身。")
    pid = a.preset or "light"
    preset = CT.preset(pid)
    if preset is None:
        _refuse("bad_preset", "预设只能是 %s" % "、".join(p["id"] for p in CT.PRESETS))
    # ---- 整理供应商
    prov_id = a.provider or (preset["provider"] if preset["provider"] != "ask" else None)
    if prov_id is None:
        _refuse("missing_provider", "自选预设要同时给 --provider（%s）" % "、".join(r["id"] for r in CT.PROVIDERS))
    row = CT.provider(prov_id)
    if row is None:
        _refuse("bad_provider", "没有这个供应商 %r；可选：%s" % (prov_id, "、".join(r["id"] for r in CT.PROVIDERS)))
    if pid == "local" and prov_id != "local":
        _refuse("local_preset_needs_local_provider", "全本机预设的整理只能用本机大模型（local）")
    url = a.provider_url or row["base_url"]
    model = a.provider_model or row["model"]
    missing = [n for n, v in (("--provider-url", url), ("--provider-model", model)) if not v]
    if missing:
        ex = ("（示例写法：%s）" % row["example_model"]) if row.get("example_model") else ""
        _refuse("missing_provider_fields", "%s 要同时给 %s%s" % (row["name"], "、".join(missing), ex))
    _check_url(url, row["local_only"] or pid == "local", "整理用的模型")
    prov = _provider_from_row(row, url, model, _parse_params(a.provider_param), a.api_key_env)
    if row["key_env"] is None:
        prov["key_env"] = None
    # ---- 向量
    vm = a.vectors
    if vm is None:
        if preset["vectors"] in ("auto",):
            _refuse("missing_vectors", "预设 %s 要同时给 --vectors llamacpp|ollama|lmstudio|custom|none（llamacpp＝自带，现在只支持 Mac 的 M 系列）" % pid)
        vm = "none"
    vec = {"mode": vm, "download": bool(a.download_model), "allow_remote": bool(a.allow_remote_embedding), "install_numpy": bool(a.install_numpy)}
    if vm in ("auto", "llamacpp"):
        info = (sys_ or ES.System()).platform_info()
        if not ES.engine_supported(info):
            _refuse("engine_unsupported", "这台电脑暂不支持自带向量引擎（现在只支持 Mac 的 M 系列芯片，别的系统在后续更新里）。可以用 LM Studio 或 Ollama：--vectors lmstudio｜ollama")
        vm = vec["mode"] = "llamacpp"
        rt = CT.runtime("llamacpp")
        vec.update(model=rt["model"], url=None, engine_dir=getattr(a, "engine_dir", None), engine_url=getattr(a, "engine_url", None), model_url=getattr(a, "model_url", None),
                   model_file=getattr(a, "model_file", None))
        for k in ("engine_url", "model_url"):
            if vec[k] and not str(vec[k]).lower().startswith("https://"):
                _refuse("not_https", "--%s 只许 https 地址" % k.replace("_", "-"))
        return {"host": "hermes", "preset": preset, "provider": prov, "vectors": vec, "key": {"choice": "env_or_file", "value": None, "test": False}}
    if vm == "custom" and not (a.embedding_url and a.embedding_model):
        _refuse("missing_embedding_fields", "--vectors custom 要同时给 --embedding-url 和 --embedding-model")
    if vm != "none":
        rt = CT.runtime(vm) if vm != "custom" else None
        vec["model"] = a.embedding_model or (rt["model"] if rt else None)
        vec["url"] = a.embedding_url or (rt["base_url"] if rt else None)
        if not VX.is_local_url(vec["url"]) and not a.allow_remote_embedding:
            _refuse("remote_embedding", "向量服务的地址不在本机：要用别处的服务，必须加 --allow-remote-embedding（索引的文字和查询会发给那个服务）")
        if pid == "local" and not VX.is_local_url(vec["url"]):
            _refuse("not_local", "全本机预设的向量服务只许本机地址")
        if not VX.is_local_url(vec["url"]):
            _check_url(vec["url"], False, "向量服务")
    return {"host": "hermes", "preset": preset, "provider": prov, "vectors": vec, "key": {"choice": "env_or_file", "value": None, "test": False}}


# ================================================================ 检查环境、做计划（只读）
WINDOWS_NOTICE = "注意：原委这一版只在 Mac 上测过，Windows 没测过。Windows 上已知可能出问题：同时写库时的文件锁不起作用、命令行输出可能乱码或报错、整理时可能闪出黑色窗口。可以继续装，但可能遇到这些问题。"


def platform_notice(sys_):
    """只提醒、不拦：返回 None（Mac）或 {"system", "text"}。系统名从 sys_.platform_info() 取（测试里注入）；取不到就按「没测过的系统」提醒，不当成 Mac。"""
    try:
        name = (sys_.platform_info() or {}).get("system")
    except Exception:                                                  # noqa: BLE001
        name = None
    if name == "Darwin":
        return None
    if name == "Windows":
        return {"system": name, "text": WINDOWS_NOTICE}
    name = name or "未知"
    return {"system": name, "text": "注意：原委这一版只在 Mac 上测过，这个系统（%s）没测过，可能出问题。" % name}


def check_env(sys_, home, hermes_cmd=None):
    from . import hermes_compat as HC
    info = {"python": {"version": "%d.%d.%d" % sys.version_info[:3], "path": sys.executable, "ok": sys.version_info >= (3, 11)}}
    hdir = os.path.join(home, "hermes-agent")
    found = os.path.isdir(home) or bool(hermes_cmd or sys_.which("hermes"))
    comp = None
    if os.path.isdir(hdir):
        try:
            rep = HC.doctor(hdir)
            comp = {"exit_code": rep.get("exit_code"), "changed": [c["id"] for c in rep.get("checks", []) if c.get("status") != "ok"][:10]}
        except Exception as e:                                         # noqa: BLE001
            comp = {"error": type(e).__name__}
    info["hermes"] = {"found": found, "home": home, "source_dir": hdir if os.path.isdir(hdir) else None, "compat": comp}
    return info


def key_state(sys_, home, prov):
    """整理密钥有没有（只查有没有，不读出值）。返回 {env, source, global_path, global_problem}：file（这个机器人自己的文件）／global_file（全局文件）／env／hermes_dotenv／not_needed／none／unchecked（.env 读不了）。
    global_path 是向导要存进去的全局文件路径（这个系统不支持 0600 文件时、或环境变量 YUANWEI_KEY_DIR 写得不对时是 None：不存文件；后一种 global_problem 是那句人话）。"""
    env = prov.get("key_env")
    if not env:
        return {"env": None, "source": "not_needed", "global_path": None, "global_problem": None}
    kp = KS.key_file(home, env) if KS.supported() else None
    prob = KS.global_dir_problem(sys_.env) if KS.supported() else None                   # YUANWEI_KEY_DIR 写得不对：不存、不看全局文件，向导会说明
    gp = KS.global_key_file(env, sys_.env) if (KS.supported() and not prob) else None
    base = {"env": env, "global_path": gp, "global_problem": prob}
    v, src = KS.lookup(env, kp, sys_.env)
    if v:
        return dict(base, source=src)
    d = KS.dotenv_has_name(os.path.join(home, ".env"), env)
    if d:
        return dict(base, source="hermes_dotenv")
    if d is None:
        return dict(base, source="unchecked")
    return dict(base, source=src if src.startswith("file_unusable") else "none")


def vector_plan(sys_, ch, home, cmds=None, hermes_cmd=None, hermes_python=None):
    """向量那一段的只读检查：检测、模型在不在、要不要下载、NumPy。返回 dict（不改任何东西）。"""
    vec = ch["vectors"]
    out = {"mode": vec["mode"]}
    if vec["mode"] == "none":
        return out
    if vec["mode"] == "llamacpp":
        return _engine_plan(sys_, vec, home, hermes_cmd, hermes_python, out)
    rt = CT.runtime(vec["mode"]) if vec["mode"] != "custom" else ES.custom_runtime(vec["url"], vec["model"])
    if vec["mode"] != "custom":
        det = {d["id"]: d for d in ES.detect(sys_, cmds=cmds)}[vec["mode"]]
        out["detected"] = det
        out["cmd_path"] = det["cmd_path"]
    else:
        out["cmd_path"] = None
    pres = ES.model_present(sys_, rt, vec["model"], vec["url"])
    out["service_up"] = pres is not None
    out["model_present"] = pres
    out["needs_download"] = pres is False
    out["tested_preview"] = ES.preview_tested(sys_, rt, vec["model"], vec["url"], out["cmd_path"]) if (vec["mode"] == "lmstudio" and pres) else None
    _numpy_state(sys_, home, hermes_cmd, hermes_python, out)
    return out


def _numpy_state(sys_, home, hermes_cmd, hermes_python, out):
    """找 Hermes 实际导入包的环境、查有没有 NumPy、算出装的命令。写进 out：hermes_env、hermes_python（装包环境的 python）、numpy（True/False/None）、numpy_install_cmd。只读。"""
    env = ES.find_hermes_env(sys_, home, hermes_cmd, hermes_python)
    out["hermes_env"] = env
    out["hermes_python"] = env["python"]
    out["numpy"] = ES.has_numpy(sys_, env["python"])
    out["numpy_install_cmd"] = ES.numpy_install_command(env)
    return out


def numpy_consent_text(vp):
    """装 NumPy 之前写给用户看的：完整命令、会丢的提醒、丢了怎么办。"""
    cmd = vp.get("numpy_install_cmd")
    if not cmd:
        return ("原委没找到往那个环境里装 NumPy 的办法（那个环境里没有 pip，也没找到 Hermes 自带的 uv）：请你自己装好 NumPy，或者用 --hermes-python 指一个装得了的环境。没有 NumPy 向量用不了，读取会自动只用关键词。")
    import shlex
    return ("会运行：%s\n这一条是现在就把 NumPy 装进当前环境。另外已经告诉 Hermes 原委需要 NumPy（插件文件 plugin.yaml 里有依赖声明），它以后重建环境时会自动带上；"
            "万一还是丢了，向量会自动退回只用关键词（不会出错），再运行上面这一条命令就能装回。" % shlex.join(cmd))


def numpy_lines(vp, vec):
    """计划里关于 NumPy 的几行（None＝没核到）。"""
    py = vp.get("hermes_python")
    if vp.get("numpy") is False:
        L = ["             Hermes 实际用的 Python 环境（%s）里没有 NumPy：%s" % (py, "确认后替你装进去（你已同意）" if vec["install_numpy"] else "还没同意；不同意向量就关掉")]
        L += ["             " + t for t in numpy_consent_text(vp).splitlines()]
        return L
    if vp.get("numpy") is None:
        return ["             没能确认 Hermes 的 Python 里有没有 NumPy（找不到 Hermes 实际用的 Python 环境）：可以用 --hermes-python 告诉原委它在哪；装好以后用 yuanwei vectors status 看；没有的话读取会自动只用关键词"]
    return []


def engine_paths(sys_, vec):
    return EP.Paths(vec.get("engine_dir") or getattr(sys_, "engine_dir", None) or EP.default_engine_dir(sys_.env))


def _engine_plan(sys_, vec, home, hermes_cmd, hermes_python, out):
    """自带引擎那一段的只读检查：钉住的值核没核、程序和模型装了没有、本机有没有现成的模型文件、空闲空间、NumPy。不下载、不启动。"""
    pins = EP.get_pins(sys_.pins)
    paths = engine_paths(sys_, vec)
    out["managed"] = True
    out["engine_dir"] = paths.root
    out["pins_problems"] = EP.pins_problems(pins)
    out["consistency"] = EP.consistency_state(next((m["sha256"] for m in EP.model_records(pins) if m.get("sha256")), None), pins)       # 官方文件和测过的是同一个文件，同一个摘要
    prog_ok = not EP.pins_problems(pins, need_model=False)
    out["program_installed"] = bool(prog_ok and EP.installed_package(paths, pins, hash_files=False))
    rc = EP.read_receipt(paths)
    out["model_installed"] = bool((rc.get("model") or {}).get("path") and os.path.exists(rc["model"]["path"]) and rc["model"].get("sha256") in EP.acceptable_model_shas(pins))
    out["model_found"] = None
    out["unreadable_dirs"] = []
    if not out["model_installed"]:
        try:
            r = EP.locate_model(pins, extra_file=vec.get("model_file"))
        except EP.EngineError as e:
            out["model_file_error"] = e.code
            r = {"found": None, "unreadable": []}
        out["model_found"] = r["found"]
        if r["found"]:
            out["model_found_wording"] = EP.model_wording(r["found"]["sha256"], pins)
        out["unreadable_dirs"] = r["unreadable"]
    out["needs_program_download"] = not out["program_installed"]
    out["needs_model_download"] = not out["model_installed"] and not out["model_found"]
    a = EP.asset(pins) or {}
    dm = [m for m in EP.model_records(pins) if m.get("role") == "downloadable"]
    out["program_size"] = a.get("size")
    out["model_size"] = (dm[0].get("size") if dm else None)
    out["free_bytes"] = sys_.free_bytes(os.path.dirname(paths.root) if os.path.isdir(os.path.dirname(paths.root)) else os.path.expanduser("~"))
    _numpy_state(sys_, home, hermes_cmd, hermes_python, out)
    return out


def _mb(n):
    return "约 %d MB" % max(1, round(n / 1048576)) if n else "大小还没核"


def engine_consent_text(vp):
    L = ["要下载："]
    if vp.get("needs_program_download"):
        L.append("  · 向量程序 llama.cpp（官方发布页，MIT 许可）%s" % _mb(vp.get("program_size")))
    if vp.get("needs_model_download"):
        L.append("  · 向量模型 Qwen3-Embedding-0.6B Q8_0（通义千问官方发布，Apache-2.0 许可）%s" % _mb(vp.get("model_size")))
    elif vp.get("model_found"):
        L.append("  · 向量模型：本机已有（%s），直接用它，不用下载；原委只读它，不会动它" % vp.get("model_found_wording"))
    L.append("都放在 %s。下载来的文件会先核对原委这一版钉住的校验码（SHA-256），对不上就删掉、不会运行。" % vp.get("engine_dir"))
    return "\n".join(L)


def tested_lines(vec, vp):
    """计划里向量那一行下面：和「测过的身份」的比对。只有 LM Studio 有测过的身份，别的运行方式照旧「没测过」。"""
    if vec["mode"] != "lmstudio":
        return ["             测过吗：没测过（%s 没有测过的向量身份）" % ((CT.runtime(vec["mode"]) or {"name": "这个向量服务"})["name"])] if vec["mode"] in ("ollama", "custom") else []
    pv = vp.get("tested_preview")
    if pv:
        return ["             测过吗（核验前的预判，以核验后为准）：%s" % pv["text"]]
    return ["             测过吗：核验以后会和测过的身份（模型文件、加载上下文 2048、引擎 @2.53.0 等）逐项比对：全一样才写「测过」；只有引擎版本不同会写明；别的写「没测过」并列出哪几项不同"]


def tested_line_after(res):
    t = (res or {}).get("tested")
    return ("向量：%s" % t["text"]) if t else None


def build_lines(ch, home, db, plan, key, vp, env=None):
    prov, vec, pre = ch["provider"], ch["vectors"], ch["preset"]
    L = ["将要做的事（还没有改任何东西）：", " 助手        Hermes（家目录 %s）" % home, " 预设        %s〔%s〕" % (pre["name"], pre.get("label_text") or pre["label"])]
    pp = plan["plan"]
    if pp.get("profile"):
        L.append(" 机器人      这是给机器人 %s 装：它会有自己单独的一个库，和主 Hermes、别的机器人互不相干；这一版只管你和它的一对一，群聊、机器人之间互发的消息不记也不查" % pp["profile"])
    for n in pp.get("notes") or []:
        if n.startswith(("这个目录里已有的原委设置是从别的 Hermes", "这个目录里已有的原委设置没有记录它属于哪个家目录", "这个机器人目录里有一份从别处复制来的记忆库")):
            L.append(" 注意        " + n)
    comp = ((env or {}).get("hermes") or {}).get("compat")
    if comp and comp.get("exit_code"):
        L.append("             和 Hermes 的接口核对：有 %d 项和原委预期不一样（%s）：先别装，这是原委要跟进的事" % (len(comp.get("changed") or []), "、".join(comp.get("changed") or []) or comp.get("error") or "?"))
    pr = " · ".join("%s=%s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in prov["params"].items())
    L.append(" 整理        %s · %s 〔%s〕 · 参数 %s" % (prov["name"], prov["model"], prov["label"], pr))
    priced = CT.price(prov["model"]) if prov["model"] else None
    if prov["id"] == "deepseek" or priced:
        L.append("             费用口径：%s，有价格表" % ("人民币" if (priced or {"currency": "CNY"})["currency"] == "CNY" else "美元，和人民币分开记账、不加在一起"))
    else:
        L.append("             费用口径：没有价格表。原委的费用估算和每日预算上限对这一家不准、可能不起作用；花了多少以你在对方平台上看到的为准")
    if prov["label"] == CT.UNTESTED:
        L.append("             %s" % CT.UNTESTED_NOTE)
    if not prov["verified"] and prov["id"] not in ("deepseek", "local", "manual"):
        L.append("             地址和模型名未核（真机试装时核）")
    src = {"file": "这个机器人自己的密钥文件", "global_file": "原委的全局密钥文件（几个机器人共用）", "env": "环境变量 %s" % key["env"], "hermes_dotenv": "Hermes 的设置文件里的 %s（原委不复制它，没看它的值）" % key["env"], "not_needed": "不需要",
           "none": "没有 %s（整理没有它没法跑；原话照样存。在你自己的终端里运行 yuanwei key set --api-key-env %s 来设）" % (key["env"], key["env"]), "unchecked": "没核到（Hermes 的设置文件读不了，或在外部密钥源里）"}.get(key["source"], key["source"])
    L.append("             密钥：%s" % src)
    if ch["key"]["choice"] == "enter":
        L.append("             密钥：确认后存进原委的全局密钥文件 %s（权限只有你能读；几个机器人共用这一份）" % (key.get("global_path") or KS.global_key_file(key["env"])))
    if vec["mode"] == "none":
        L.append(" 向量        不开（只用关键词；以后可以 yuanwei vectors setup）")
    elif vec["mode"] == "llamacpp":
        L.append(" 向量        自带（llama.cpp 服务程序 · Qwen3-Embedding-0.6B Q8_0）· 放在 %s" % vp.get("engine_dir"))
        if vp.get("pins_problems"):
            L.append("             这一版的钉住的下载地址和校验码还没核过（%s）：现在装不上自带引擎，确认执行时会被拒绝、什么都不改" % "、".join(vp["pins_problems"][:4]))
        dl = vp.get("needs_program_download") or vp.get("needs_model_download")
        if dl:
            L.append("             需要下载：%s%s %s" % ("程序 %s " % _mb(vp.get("program_size")) if vp.get("needs_program_download") else "", "模型 %s" % _mb(vp.get("model_size")) if vp.get("needs_model_download") else "",
                                                    "（你已同意）" if vec["download"] else "：还没同意，不加 --download-model 就不会下载，装不上向量"))
        if vp.get("model_found"):
            L.append("             模型：本机已有（%s），直接用，只读" % vp.get("model_found_wording"))
        L.append("             装好后会核验：启动一次，算几句测试的话，核对维度（应为 %d）、同句两次一致、相近句更近；记下编码身份（含运行包和启动配方的摘要）" % CT.EMBED_MODEL_DIM)
        L.append("             一致性：%s" % (vp.get("consistency") or {}).get("text", CT.LABEL_UNCHECKED))
        L += numpy_lines(vp, vec)
    else:
        rtn = (CT.runtime(vec["mode"]) or {"name": "自己的向量服务"})["name"]
        L.append(" 向量        %s（%s）· %s%s" % (rtn, vec["url"], vec["model"], "" if VX.is_local_url(vec["url"]) else " · 远端：索引的文字和查询会发给那个服务"))
        if vp.get("needs_download"):
            L.append("             需要下载模型（约 600 MB）%s" % ("（你已同意）" if vec["download"] else "：还没同意，不加 --download-model 就不会下载，装不上向量"))
        elif vp.get("model_present"):
            L.append("             模型已经在本机")
        elif vp.get("model_present") is None:
            L.append("             本机服务没有响应：确认后会核验，核不过就不装、什么都不改")
        L.append("             装好后会核验：算几句测试的话、核对维度%s、记下编码身份" % ("（应为 %d）" % CT.EMBED_MODEL_DIM if vec["model"] == CT.runtime("lmstudio")["model"] or vec["model"] == CT.runtime("ollama")["model"] else ""))
        L += tested_lines(vec, vp)
        L += numpy_lines(vp, vec)
    L.append(" Hermes 依赖  插件文件 plugin.yaml 里声明了需要 numpy>=1.26（一律声明，不管开不开向量，用关键词的人也会多装约 5 MB）：Hermes 以后重建它的包环境时会自动带上。"
             "按 Hermes 源码，下次启动或更新时发现环境和声明对不上就会重建；在 Hermes v0.22.0（+10397，2026.9.24）上已确认（真机重建两次，NumPy 都自己装上了），其他发行方式没验证")
    L.append(" 库          %s" % db)
    L.append(" 读取范围    %s" % (plan.get("read_scope_label") or "乙"))
    wf = [a for a in plan["plan"]["actions"] if a["kind"] == "write_file"]
    new_files = [a["path"] for a in wf if a.get("new")]
    upd = [a for a in wf if not a.get("new")]
    L.append(" 新放的文件  %s" % ("、".join(new_files) if new_files else "（没有）"))
    if upd:
        L.append(" 更新的文件  已经有、内容要换成新的（换之前先各备份一份，备份名是原文件名加 .yuanwei-backup-<时间>，权限和原文件一样；卸载不会删这些备份）：")
        for a in upd:
            L.append("             %s" % a["path"])
    same = plan["plan"].get("unchanged") or []
    if same:
        L.append(" 不变的文件  内容和已有的一样，不动、不备份：%s" % "、".join(same))
    for a in plan["plan"]["actions"]:
        if a["kind"] in ("edit_config", "hermes_config_set"):
            L.append(" 会改的设置  config.yaml 里 memory.provider 一行（先备份）")
            break
    L.append(" 不会做      不装 Ollama／LM Studio，%s，不改别的说明文件" % ("装好后会按需在后台启动向量程序（闲置 15 分钟自己退出）" if vec["mode"] == "llamacpp" else "不启动任何服务"))
    return L


# ================================================================ 生成 TOML
def _tv(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_tv(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join("%s = %s" % (json.dumps(k, ensure_ascii=False), _tv(x)) for k, x in v.items()) + " }"
    raise TypeError("不能写进 TOML：%r" % (v,))


def to_toml(top, tables):
    """固定形状：顶层标量，再每个表一段（值只有字符串、数、布尔、列表、内联表）。None 的项不写。"""
    out = ["# 原委配置（yuanwei setup 写的）。里面没有密钥，只有变量名和密钥文件的路径。\n"]
    for k, v in top.items():
        if v is not None:
            out.append("%s = %s\n" % (k, _tv(v)))
    for name, d in tables.items():
        out.append("\n[%s]\n" % name)
        for k, v in d.items():
            if v is not None:
                out.append("%s = %s\n" % (k, _tv(v)))
    return "".join(out)


def build_toml(ch, home, identity=None, weak=False):
    prov, vec = ch["provider"], ch["vectors"]
    p = {"id": prov["id"], "label": prov["label"], "base_url": prov["base_url"], "timeout": 600, "params": dict({"model": prov["model"]}, **prov["params"])}
    if prov["key_env"]:
        p["api_key_env"] = prov["key_env"]
        if KS.supported():
            p["key_file"] = KS.key_file(home, prov["key_env"])
    tables = {"provider": p}
    top = {"retrieval": "keyword"}
    if vec["mode"] == "llamacpp" and identity:
        top = {"retrieval": "hybrid", "vector_fallback": "partial"}
        tables["embedding"] = {"managed": True, "runtime": "llamacpp", "engine_dir": vec["engine_dir_resolved"], "model": vec["model"], "timeout_query": 5, "timeout_worker": 60, "backfill_batch": 16,
                               "breaker_seconds": 30, "worker_max_run_seconds": 600, "idle_exit_seconds": CT.ENGINE_DEFAULTS["idle_exit_seconds"], "cold_wait_seconds": CT.ENGINE_DEFAULTS["cold_wait_seconds"],
                               "weak_identity": False, "identity": identity}
    elif vec["mode"] != "none" and identity:
        top = {"retrieval": "hybrid", "vector_fallback": "partial"}
        tables["embedding"] = {"base_url": vec["url"], "model": vec["model"], "runtime": vec["mode"], "timeout_query": 5, "timeout_worker": 60, "backfill_batch": 16, "breaker_seconds": 30,
                               "worker_max_run_seconds": 600, "allow_remote": bool(vec["allow_remote"]) if not VX.is_local_url(vec["url"]) else None, "weak_identity": bool(weak), "identity": identity}
    return to_toml(top, tables)


# ================================================================ 做计划、执行
def _planned_toml(ch, home, vp, toml_path):
    """计划里 yuanwei.toml 的新内容。向量选择和已有设置一样（运行方式、模型、地址）时，沿用已有的编码身份——这样内容没变的就不会被列成「更新」；
    选择变了、或已有设置里没有身份：用「还没核验」的版本（内容会变，列进「更新」）。读不了已有设置也一样。"""
    vec = ch["vectors"]
    ident, weak = None, False
    if vec["mode"] != "none" and os.path.exists(toml_path):
        try:
            import tomllib
            with open(toml_path, "rb") as f:
                old = tomllib.load(f)
            emb = old.get("embedding") or {}
            same = emb.get("runtime") == vec["mode"] and emb.get("model") == vec["model"] and (vec["mode"] == "llamacpp" or emb.get("base_url") == vec.get("url"))
            if same and isinstance(emb.get("identity"), dict):
                ident, weak = emb["identity"], bool(emb.get("weak_identity"))
        except Exception:                                                               # noqa: BLE001  读不了就当没有
            ident = None
    if ident is None:
        return build_toml(ch, home)
    ch2 = dict(ch, vectors=dict(vec, engine_dir_resolved=vp.get("engine_dir") or vec.get("engine_dir_resolved")))
    try:
        return build_toml(ch2, home, ident, weak)
    except Exception:                                                                   # noqa: BLE001
        return build_toml(ch, home)


def make_plan(ch, sys_, home, args, hermes_cmd):
    """只读：返回 {"lines", "plan"（HS.plan_install 的公开部分）, "key", "vp", "env"}。"""
    env = check_env(sys_, home, hermes_cmd)
    if not env["python"]["ok"]:
        _refuse("python_too_old", "这个 Python 是 %s，原委要 3.11 以上。请换一个更新的 Python 再运行（pip install 和 yuanwei setup 要用同一个）。" % env["python"]["version"])
    if not env["hermes"]["found"]:
        _refuse("hermes_not_found", "没有找到 Hermes。如果它装在别处，用 --hermes-home 指一下家目录；还没装 Hermes 的话请先装好。原委现在只接 Hermes。")
    cmds = {k: v for k, v in (("ollama", getattr(args, "ollama_cmd", None)), ("lmstudio", getattr(args, "lms_cmd", None))) if v}
    vp = vector_plan(sys_, ch, home, cmds, hermes_cmd, getattr(args, "hermes_python", None))
    key = key_state(sys_, home, ch["provider"])
    toml_path = os.path.join(home, "yuanwei", "yuanwei.toml")
    user_cfg = getattr(args, "config_path", None)
    toml_text = None if user_cfg else _planned_toml(ch, home, vp, toml_path)          # 计划阶段先算好新内容，和已有文件比；向量的编码身份要核验以后才知道，已有的设置里选择没变就沿用它的身份来比（核验后身份若变了，那时才更新并备份）
    kw = dict(hermes_home=home, db=args.db, read_scope=args.read_scope, owners=_owners(args), mode=args.mode, src_path=args.src_path, replace_provider=args.replace_provider,
              config_path=user_cfg or toml_path, spawn=args.spawn, hermes_cmd=hermes_cmd, config_method=args.config_method, toml_text=toml_text)
    plan = {"plan": HS._public(HS.plan_install(**kw)), "kw": kw}
    from . import hermes_workspace as HW
    plan["read_scope_label"] = {"isolated": "甲（每个工作区只翻自己）", "life_all": "乙（生活能翻全部，项目只翻自己）", "all_open": "丙（全部互通）"}.get(args.read_scope or HW.DEFAULT_SCOPE)
    db = args.db or os.path.join(home, "yuanwei", "yuanwei.sqlite")
    lines = build_lines(ch, home, db, plan, key, vp, env)
    if user_cfg:
        lines.append(" 配置        你用 --config 给了自己的配置文件：原委不改它。请你自己把下面几行加进去：")
        lines += ["   " + l for l in build_toml(ch, home).splitlines() if l.strip() and not l.startswith("#")]
    return {"lines": lines, "plan": plan, "key": key, "vp": vp, "env": env}


def _owners(args):
    owners = {}
    for o in args.owner or []:
        plat, _, ident = o.partition(":")
        if not plat or not ident:
            _refuse("bad_owner", "--owner 要写成 平台:编号，例如 telegram:123456")
        owners.setdefault(plat, []).append(ident)
    return owners


def apply_plan(ch, pl, sys_, home, args, io=None):
    """执行。任何一步失败：抛 SetupRefused（带原因类别），后面的都不做。返回结果字典。"""
    vec, prov, vp = ch["vectors"], ch["provider"], pl["vp"]
    steps = []
    identity, weak, weak_reasons, tested = None, False, [], None
    rt = None
    if vec["mode"] == "llamacpp":
        identity = _apply_engine(ch, vp, sys_, home, args, steps)
    elif vec["mode"] != "none":
        rt = CT.runtime(vec["mode"]) if vec["mode"] != "custom" else ES.custom_runtime(vec["url"], vec["model"])
        if vp.get("needs_download"):
            if not vec["download"]:
                _refuse("download_not_consented", "本机没有向量模型 %s，而你没有同意下载（要加 --download-model）。没改任何东西。" % vec["model"])
            if not vp.get("cmd_path"):
                _refuse("runtime_cmd_missing", "找不到 %s 的命令，没法替你下载。没改任何东西。" % rt["name"])
            ok, why = ES.pull(sys_, rt, vec["model"], vp["cmd_path"], timeout=getattr(args, "pull_timeout", None) or 1800)
            if not ok:
                _refuse("download_failed", "下载没成功（%s）。没改任何配置；已经下载的部分留着，原委不删。" % why)
            steps.append("download")
        if vp.get("numpy") is False:
            if not vec["install_numpy"]:
                _refuse("numpy_not_consented", "Hermes 的 Python（%s）里没有 NumPy，而你没有同意替它装（--install-numpy）。向量用不了；没改任何东西。" % vp.get("hermes_python"))
            ok, why = ES.install_numpy(sys_, vp["hermes_env"])
            if not ok:
                _numpy_refuse(why)
            steps.append("numpy")
        exp = CT.EMBED_MODEL_DIM if vec["model"] in (CT.runtime("lmstudio")["model"], CT.runtime("ollama")["model"]) else None
        res = ES.verify(sys_, rt, vec["model"], vec["url"], cmd_path=vp.get("cmd_path"), expected_dim=exp, allow_remote=vec["allow_remote"])
        if not res["ok"]:
            _refuse("verify_failed", "向量核验没通过（%s%s）。没改任何配置。" % (res["reason"], "，维度 %s" % res["dimension"] if res.get("dimension") else ""))
        identity, weak, weak_reasons, tested = res["identity"], res["weak"], list(res.get("weak_reasons") or []), res.get("tested")
        steps.append("verify")
    if prov["id"] == "local" and not getattr(args, "ignore_probe_failure", False):
        r = ES.long_input_probe(sys_, prov["base_url"], prov["model"], prov["params"])
        if r != "ok":
            _refuse("long_input_probe_failed", "本机大模型的长输入探针没通过（%s）：它可能会悄悄把很长的输入截掉，整理会乱。请调大它的上下文长度，或加 --ignore-probe-failure 自己承担。没改任何配置。" % r)
        steps.append("long_probe")
    if ch["key"]["choice"] == "enter":
        KS.write_global_key(prov["key_env"], ch["key"]["value"], sys_.env)
        steps.append("key")
    kw = dict(pl["plan"]["kw"])
    if kw.get("config_path") and not getattr(args, "config_path", None):
        kw["toml_text"] = build_toml(ch, home, identity, weak)
    res = HS.apply_install(**kw)
    if identity:
        _store_probe(home, args, kw, identity, res, sys_, rt, vec)
    if vec["mode"] == "llamacpp":
        ER.register_user(engine_paths(sys_, vec), user_id(home), {"config": kw.get("config_path")})          # 登记这个使用方：卸载别的使用方时不会停掉这里在用的引擎
    steps.append("install")
    if identity and tested is None:
        tested = CT.compare_to_tested(identity, weak)
    out = {"applied": True, "steps": steps, "install": res, "weak_identity": weak, "weak_reasons": weak_reasons, "tested": tested}
    if identity and getattr(args, "config_path", None):
        out["identity"] = identity                                            # 用户自己的配置文件：原委不改它，把要加的身份给出来
    return out


def user_id(home):
    import hashlib
    return "hermes:" + hashlib.sha256(os.path.abspath(str(home)).encode("utf-8")).hexdigest()[:16]


def _apply_engine(ch, vp, sys_, home, args, steps):
    """自带引擎的执行：同意下载 → 程序（下载、核校验码、安全解压、整包核对）→ 模型（现成的或下载的）→ NumPy → 核验。任何一步失败：抛 SetupRefused，配置一个字没写。
    已经下载并核验过的文件留着，原委不删。返回编码身份。"""
    vec = ch["vectors"]
    pins = EP.get_pins(sys_.pins)
    paths = engine_paths(sys_, vec)
    vec["engine_dir_resolved"] = paths.root
    if vp.get("pins_problems"):
        _refuse("pins_unverified", "这一版自带引擎钉住的下载地址／校验码还没核过（%s），装不上。没改任何东西；可以先用 LM Studio 或 Ollama。" % "、".join(vp["pins_problems"][:4]))
    if (vp.get("needs_program_download") or vp.get("needs_model_download")) and not vec["download"]:
        _refuse("download_not_consented", "自带向量引擎需要下载文件，而你没有同意（要加 --download-model，它的意思是「同意计划里列出的全部下载」）。没改任何东西。")
    if vp.get("numpy") is False:
        if not vec["install_numpy"]:
            _refuse("numpy_not_consented", "Hermes 的 Python（%s）里没有 NumPy，而你没有同意替它装（--install-numpy）。向量用不了；没改任何东西。" % vp.get("hermes_python"))
    fetcher = sys_.fetcher or EP.Fetcher()
    free = vp.get("free_bytes")
    try:
        with EP.flock(paths.lifecycle_lock, blocking=True, timeout=10) as got:                  # 安装、重核、移除同一个引擎要互斥
            if not got:
                _refuse("engine_busy", "引擎正在被别的操作使用（安装或移除），稍后再试。没改任何东西。")
            paths.ensure_dirs()
            pk = EP.install_program(paths, pins, fetcher, vec.get("engine_url"), free_bytes=free)
            steps.append("engine_program")
            rc = EP.read_receipt(paths)
            m = rc.get("model") if (rc.get("model") or {}).get("sha256") in EP.acceptable_model_shas(pins) and os.path.exists((rc.get("model") or {}).get("path", "")) else None
            if m and EP.verify_model_file(m["path"], m["sha256"]):
                m = None
            if not m:
                found = (vp.get("model_found") or EP.locate_model(pins, extra_file=vec.get("model_file"))["found"])
                if found:
                    m = {"path": found["path"], "sha256": found["sha256"], "artifact_id": found["artifact_id"], "role": found["role"], "source": "reused", "owned": False}
                else:
                    d = EP.download_model(paths, pins, fetcher, vec.get("model_url"), free_bytes=free)
                    m = dict(d, source="downloaded")
                steps.append("engine_model")
            EP.write_receipt(paths, model=m)
            with contextlib.suppress(OSError):
                os.unlink(paths.backoff)                                                       # 用户主动重核：解除冷却
            ER.set_lifecycle(paths, "active")
    except EP.EngineError as e:
        _refuse(e.code if e.code.startswith("engine_") or e.code == "pins_unverified" else "engine_" + e.code, "自带引擎没准备好（%s）。没改任何配置；已经下载并核验过的文件留着，原委不删。" % e.code)
    if vp.get("numpy") is False:
        ok, why = ES.install_numpy(sys_, vp["hermes_env"])
        if not ok:
            _numpy_refuse(why)
        steps.append("numpy")
    res = ER.verify_managed(paths, pins, host=sys_.engine_host, spawner=sys_.engine_spawner, opener=sys_.engine_opener, wait=60.0, emb_extra={"engine_dir": paths.root})
    if not res["ok"]:
        _refuse("verify_failed", "向量核验没通过（%s%s）。没改任何配置。" % (res["reason"], "，维度 %s" % res["dimension"] if res.get("dimension") else ""))
    steps.append("verify")
    return res["identity"]


def _store_probe(home, args, kw, identity, res, sys_, rt, vec):
    """核验时的探针向量记进库（之后补算用它发现「同名换了模型」）。库在这里才第一次被建出来也行：只写 store_meta。"""
    from .sqlstore import Store
    from . import embedwork as EW
    db = kw["db"] or os.path.join(home, "yuanwei", "yuanwei.sqlite")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    if vec["mode"] == "llamacpp":
        client = ER.resolve_client({"managed": True, "engine_dir": vec["engine_dir_resolved"], "identity": identity, "timeout_worker": 30}, "worker", host=sys_.engine_host, opener=sys_.engine_opener,
                                   spawner=sys_.engine_spawner)
    else:
        client = VX.HttpEmbeddingClient(vec["url"], vec["model"], timeout=30, dimension=identity["dimension"], opener=sys_.opener)
    st = Store(db)
    try:
        fp = VX.identity_fp(identity)
        st.put_identity(fp, identity)
        EW.store_probe(st, fp, client.embed_batch(list(EW.PROBE_SENTENCES)))
    except ProviderErrorTypes:
        pass
    finally:
        st.close()


ProviderErrorTypes = (VX.ProviderError,)

WEAK_WHY = {"model_file_sha256": "取不到模型文件的 SHA-256", "runtime_version": "取不到运行包的版本", "context_length": "取不到加载时的上下文长度", "context_length_is_upper_bound": "只取到模型的上限、不是加载时用的上下文长度"}


def weak_note(res):
    """装完提醒：编码身份比较弱，以及哪几项取不到。"""
    why = "、".join(WEAK_WHY.get(k, k) for k in (res.get("weak_reasons") or []))
    return "（提醒：向量的编码身份比较弱——%s，换了模型文件、运行包或上下文长度可能认不出来，也不能写「测过」。）" % (why or "取不到模型文件的 SHA-256")


def entry_command():
    """当前这个 yuanwei 可执行文件的完整路径（装完的提示里写它：yuanwei 常常不在系统路径里，比如装在 ~/.yuanwei/venv/bin/）。
    顺序：这次就是用它启动的（sys.argv[0] 是名叫 yuanwei 的文件）；和当前 Python 放在同一个目录里的 yuanwei；都没有就写 <当前 Python> -m yuanwei。"""
    import shlex
    a0 = sys.argv[0] if sys.argv else ""
    if a0 and os.path.basename(a0) in ("yuanwei", "yuanwei.exe") and os.path.isfile(a0):
        return shlex.quote(os.path.abspath(a0))
    sib = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "yuanwei")
    if os.path.isfile(sib):
        return shlex.quote(sib)
    return "%s -m yuanwei" % shlex.quote(sys.executable)


def finish_text():
    y = entry_command()
    return ("装好了。请你自己重启 Hermes（原委不替你重启），新的记忆才会加载。\n"
            "看状态：%s status hermes（里面有「向量」「整理供应商」两项）；只看向量：%s vectors status。\n"
            "向量会在后台慢慢补算：库里已有的原话要一条条算，条数多的话要一阵子；算好之前「找」只用关键词，状态里会写还差多少。\n"
            "卸载：%s uninstall hermes（不会删你的记忆库，也不会删密钥文件；要删全局密钥文件用 %s key delete，这个机器人自己单独的那份加 --this-bot-only）。" % (y, y, y, y))


# ================================================================ 一问一答
def interactive(args, io, sys_, home, hermes_cmd):
    """问完返回 choices；取消抛 Cancel。不写任何东西。"""
    io.say("原委安装向导（预览版）。接下来问你几件事，最后列出计划，你确认了才会改东西。随时输入 q 退出，退出前什么都不会改。\n")
    env = check_env(sys_, home, hermes_cmd)
    io.say("第 1 步　检查环境")
    if env["python"]["ok"]:
        io.say("  Python %s（%s）✓" % (env["python"]["version"], env["python"]["path"]))
        pn = platform_notice(sys_)
        if pn:
            io.say("  " + pn["text"])
    else:
        _refuse("python_too_old", "这个 Python 是 %s，原委要 3.11 以上。请换一个更新的 Python 再运行（pip install 和 yuanwei setup 要用同一个）。" % env["python"]["version"])
    h = env["hermes"]
    if not h["found"]:
        _refuse("hermes_not_found", "没有找到 Hermes。如果它装在别处，用 --hermes-home 指一下家目录；还没装 Hermes 的话请先装好。原委现在只接 Hermes。")
    comp = h["compat"]
    io.say("  找到 Hermes：家目录 %s；和原委的接口核对：%s" % (home, "没核（找不到它的源码目录）" if comp is None else ("全部对 ✓" if comp.get("exit_code") == 0 else "有 %d 项和原委预期不一样：先别装，这是原委要跟进的事" % len(comp.get("changed") or []))))
    io.say("\n第 2 步　选助手")
    io.say("  1) Hermes\n  （Claude Code、Codex、Pi：后续更新，现在还不能选。）")
    if io.ask("选 [1]：", "1") != "1":
        _refuse("future_host", "那几个是后续更新，现在只有 Hermes。")
    io.say("\n第 3 步　选预设\n  先选一套配置，后面每一步都按它给默认值，你也可以逐项改。")
    for i, p in enumerate(CT.PRESETS, 1):
        io.say("  %d) %s〔%s〕：%s" % (i, p["name"], p.get("label_text") or p["label"], p["summary"]))
    pi = _pick(io, "选 [1]：", len(CT.PRESETS), 1)
    preset = CT.PRESETS[pi - 1]
    io.say("\n第 4 步　整理用的供应商和模型")
    if preset["provider"] in ("deepseek",):
        row = CT.provider("deepseek")
        io.say("  整理是原委用一个大模型把你的对话归到各条主线上。这一套用 %s %s〔%s〕：%s" % (row["name"], row["model"], row["label"], row["note"]))
        if not yes(io.ask("用这个吗？ [Y/n]："), True):
            row = _choose_provider(io, preset)
    else:
        row = _choose_provider(io, preset)
    url, model = row["base_url"], row["model"]
    if row["id"] in ("manual", "local") or not url:
        url = io.ask("地址（兼容 OpenAI 的接口，到 /v1 为止）[%s]：" % (row["base_url"] or ""), row["base_url"] or "")
    if not model:
        ex = ("（示例写法：%s）" % row["example_model"]) if row.get("example_model") else ""
        model = io.ask("模型名%s：" % ex)
        if not model:
            _refuse("missing_provider_fields", "没有模型名。")
    _check_url(url, row["local_only"], "整理用的模型")
    prov = _provider_from_row(row, url, model)
    if prov["label"] in (CT.UNTESTED, CT.EXPERIMENT):
        io.say("  这一家%s。原委的费用估算和每日预算上限对它**不准、可能不起作用**：花了多少钱以你在对方平台上看到的为准。" % ("没测过" if prov["label"] == CT.UNTESTED else "是实验配置"))
        if not yes(io.ask("继续吗？ [y/N]："), False):
            raise Cancel()
    if prov["id"] == "openai-luna":
        io.say("  这一家按美元记账，和 DeepSeek 的人民币分开，不会加在一起。")
    if prov["id"] == "local" or preset["id"] == "local":
        io.say("  整理用的模型要在你电脑上跑，而且每一步的请求里有主线卡片和这一步的对话，需要模型能读下很长的输入（约 3 万个词元以上；本机服务默认的上下文长度常常只有几千，会在你不知情的时候把输入截掉，整理就会乱）。"
               "内存要求取决于模型：一个量化后的 7B 左右模型大约要 8–16 GB 空闲内存，更大的要更多（粗估，没测过；数字真机试装时核）。你的电脑内存是否够，请自己确认。")
    # ---- 密钥
    io.say("\n第 5 步　整理用的密钥")
    key = {"choice": "env_or_file", "value": None, "test": False}
    ks = key_state(sys_, home, prov)
    can_enter = not ks.get("global_problem")                                              # YUANWEI_KEY_DIR 写得不对时，向导没地方存新输入的密钥
    if ks.get("global_problem"):
        io.say("  %s现在不能在这里输入新的密钥；请改好这个环境变量（要写绝对路径，终端和 Hermes 服务用同一个设置），或者直接用环境变量／Hermes 的设置文件里的密钥。" % (ks["global_problem"] + "。" if not ks["global_problem"].endswith("。") else ks["global_problem"]))
    if ks["source"] == "not_needed":
        io.say("  本机大模型不需要密钥。")
    elif ks["source"] in ("file", "global_file", "env", "hermes_dotenv"):
        io.say("  已有：%s（这里只报来源，不显示它的值）。" % {"file": "这个机器人自己的密钥文件", "global_file": "原委的全局密钥文件", "env": "环境变量 %s" % ks["env"], "hermes_dotenv": "Hermes 的设置文件里的 %s" % ks["env"]}[ks["source"]])
        if not yes(io.ask("用它吗？ [Y/n]："), True):                                      # 不一定想用已有的那一把（比如它是要作废的）：另输一把，存进原委的全局密钥文件（只有你能读；几个机器人共用），读的时候它排在环境变量和 Hermes 设置文件前面；这个机器人要是有自己单独的密钥文件，那份还排在它前面
            if not can_enter:
                io.say("  这次还是用已有的那一把。")
            elif not io.tty():
                io.say("  这里不是终端，不能安全地收密钥。请在你自己的终端里运行：yuanwei key set --api-key-env %s（设好以后原委就用新的那把；在那之前还是用已有的）" % ks["env"])
            else:
                key = _enter_key(io, key)
                if key["choice"] != "enter":
                    io.say("  没输成，这次还是用已有的那一把（以后可以 yuanwei key set --api-key-env %s 换）。" % ks["env"])
                elif ks["source"] == "file":
                    io.say("  注意：这个机器人有自己单独的密钥文件，眼下用的就是它；那份能用的时候会一直先用它，不用新输的这把。新输的存进全局，别的机器人用得上。要换这个机器人自己的：yuanwei key set --this-bot-only --hermes-home %s --api-key-env %s。" % (shlex.quote(str(home)), ks["env"]))
    else:
        io.say("  没找到 %s%s。整理没有它没法跑（原话照样存，只是不整理）。现在设置吗？\n"
               "    1) 现在在这里输入（输入时不显示，存在你电脑上只有你能读的文件里，几个机器人共用）\n    2) 我自己去设（写进 Hermes 的设置文件 %s，或在启动 Hermes 的终端里 export；不要把密钥贴到聊天里）\n    3) 以后再说"
               % (ks["env"], "（Hermes 的设置文件读不了，没核到）" if ks["source"] == "unchecked" else "", os.path.join(home, ".env")))
        c = _pick(io, "选 [1]：", 3, 1)
        if c == 1:
            if not can_enter:
                io.say("  先改好上面说的环境变量，再运行 yuanwei key set。")
            elif not io.tty():
                io.say("  这里不是终端，不能安全地收密钥。请在你自己的终端里运行：yuanwei key set")
            else:
                key = _enter_key(io, key)
                if key["value"] is not None and yes(io.ask("要试一下这把密钥能不能用吗？会向 %s 发一个最小的请求，花不到一分钟的钱…… 只告诉你「能用」或「不能用」。 [y/N]：" % prov["name"]), False):
                    r = KS.check_key(prov["base_url"], prov["model"], prov["params"], key["value"], opener=sys_.opener)
                    io.say("  " + {"works": "能用。", "rejected": "不能用（对方拒绝了这把密钥）。", "unverified": "没验成（网络或对方的服务出了问题，不是密钥的问题）。"}[r])
    # ---- 向量
    vec = {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
    io.say("\n第 6 步　向量")
    if preset["vectors"] == "none":
        io.say("  这一套不开向量（只用关键词）。以后随时可以 yuanwei vectors setup。")
    else:
        vec = _vector_questions(io, sys_, home, hermes_cmd, args, preset)
    io.say("\n第 7 步　读取范围、哪个账号是你")
    io.say("  能翻哪些工作区？\n    1) 乙（默认）：在「生活」里能翻生活和所有项目；在项目里只翻这个项目。\n    2) 甲：每个工作区只翻自己。\n    3) 丙：全部互通。")
    sc = _pick(io, "选 [1]：", 3, 1)
    args.read_scope = {1: None, 2: "isolated", 3: "all_open"}[sc]
    if not yes(io.ask("你是不是只在本机终端里和 Hermes 聊？ [Y/n]："), True):
        io.say("  请输入你在聊天软件上的账号，格式 平台:编号，例如 telegram:123456；可以输多个，空行结束。不配的话，聊天软件里的话一条都不会记。")
        while True:
            o = io.ask("账号：")
            if not o:
                break
            args.owner = list(args.owner or []) + [o]
    return {"host": "hermes", "preset": preset, "provider": prov, "vectors": vec, "key": key}


def _pick(io, prompt, n, default):
    while True:
        v = io.ask(prompt, str(default))
        if v.isdigit() and 1 <= int(v) <= n:
            return int(v)
        io.say("  请输入 1 到 %d。" % n)


def _choose_provider(io, preset):
    rows = [r for r in CT.PROVIDERS if not (r["id"] == "local" and preset["id"] != "local") and not (preset["id"] == "local" and r["id"] != "local")]
    io.say("  整理是原委用一个大模型把你的对话归到各条主线上。现在支持这些（都兼容 OpenAI 的接口）：")
    for i, r in enumerate(rows, 1):
        io.say("    %d) %s　%s〔%s〕%s" % (i, r["name"], r["model"] or "（模型名你自己填）", r["label"], "　" + r["note"] if r["label"] != CT.UNTESTED else "　" + CT.UNTESTED_NOTE))
    return rows[_pick(io, "选 [1]：", len(rows), 1) - 1]


def _enter_key(io, key):
    for _ in range(3):
        a = io.secret("请输入密钥（输入时屏幕上什么都不会显示）：")
        v, why = KS.validate_value(a)
        if v is None:
            io.say("  密钥格式不对（%s）。" % why)
            continue
        b = io.secret("再输入一次：")
        if b.strip() != v:
            io.say("  两次不一致。")
            continue
        return {"choice": "enter", "value": v, "test": False}
    io.say("  三次都没成功，这一项先跳过（以后可以 yuanwei key set）。")
    return key


def _vector_questions(io, sys_, home, hermes_cmd, args, preset):
    io.say("  向量让「找旧话」更准，尤其是聊得久了以后。在 150 次对话的离线对比里，只用关键词时，依据的交到从 154 降到 145（共 158）；少的全出在后一批（只有一份故事、一种向量模型）。"
           "要多下载一个约 600 MB 的模型（Qwen3-Embedding-0.6B），算向量都在你自己的电脑上，原话不会发到外面。不开就只用关键词。")
    if not yes(io.ask("开吗？ [Y/n]："), True):
        return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
    if ES.engine_supported(sys_.platform_info()):
        pick = _engine_question(io, sys_, home, hermes_cmd, args)
        if pick is not None:
            return pick
    else:
        io.say("  （这台电脑暂不支持「自带」向量引擎：现在只支持 Mac 的 M 系列芯片，别的系统在后续更新里。可以用 LM Studio 或 Ollama。）")
    cmds = {k: v for k, v in (("ollama", getattr(args, "ollama_cmd", None)), ("lmstudio", getattr(args, "lms_cmd", None))) if v}
    det = ES.detect(sys_, cmds=cmds)
    here = [d for d in det if d["present"]]
    if not here:
        io.say("  本机没有找到 Ollama 或 LM Studio。原委不替你安装。去这里装一个，装好再运行一次 yuanwei setup：")
        for d in det:
            io.say("    %s：%s　%s" % (d["name"], d["official_url"], d["blurb"]))
        io.say("  （网址真机试装时核）\n    1) 先不开向量，以后再开\n    2) 取消")
        if _pick(io, "选 [1]：", 2, 1) == 2:
            raise Cancel()
        return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
    if len(here) == 1:
        d = here[0]
        if not d["service_up"]:
            io.say("  找到了 %s 的命令，但本机服务没有响应（地址 %s）。请先把它启动起来（原委不替你启动），再运行 yuanwei setup；或者现在先选「不开，以后再开」。" % (d["name"], d["base_url"]))
            if _pick(io, "1) 不开，以后再开　2) 取消  选 [1]：", 2, 1) == 2:
                raise Cancel()
            return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
        io.say("  发现本机有 %s（命令在，服务响应）。%s" % (d["name"], d["blurb"]))
        if not yes(io.ask("用它吗？ [Y/n]："), True):
            return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
        pickd = d
    else:
        io.say("  发现 Ollama 和 LM Studio：")
        for i, d in enumerate(det, 1):
            io.say("    %d) %s　%s" % (i, d["name"], d["blurb"]))
        pickd = det[_pick(io, "用哪个？ [1]：", len(det), 1) - 1]
    vec = {"mode": pickd["id"], "model": pickd["model"], "url": pickd["base_url"], "download": False, "allow_remote": False, "install_numpy": False}
    rt = CT.runtime(pickd["id"])
    pres = ES.model_present(sys_, rt, vec["model"], vec["url"])
    if pres is False:
        io.say("  需要下载模型 %s（约 600 MB，由你电脑上的 %s 去它的官方模型库下载，原委自己不联网）。下载大概要几分钟，磁盘要有约 1.5 GB 空闲。" % (vec["model"], pickd["name"]))
        if not yes(io.ask("下载吗？ [y/N]："), False):
            io.say("  不下载，向量就先不开。")
            return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
        vec["download"] = True
    nv = _numpy_state(sys_, home, hermes_cmd, getattr(args, "hermes_python", None), {})
    if not _numpy_question(io, nv, vec):
        return {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
    return vec


def _numpy_question(io, nv, vec):
    """NumPy 这一步的问话。返回 True＝向量继续（有、或同意装、或没核到），False＝向量关掉。"""
    hn = nv.get("numpy")
    if hn is False:
        io.say("  Hermes 实际用的那个 Python 环境（%s）里没有 NumPy，向量用不了。" % nv.get("hermes_python"))
        io.say("  " + numpy_consent_text(nv).replace("\n", "\n  "))
        if nv.get("numpy_install_cmd") and yes(io.ask("要我替它装进去吗？ [y/N]："), False):
            vec["install_numpy"] = True
            return True
        io.say("  不装，向量就关掉。以后想开：先自己装好 NumPy，再运行 yuanwei vectors setup。")
        return False
    if hn is None:
        io.say("  没能确认 Hermes 的 Python 里有没有 NumPy（找不到 Hermes 实际用的 Python 环境；可以用 --hermes-python 告诉原委它在哪）。装好以后用 yuanwei vectors status 看；没有的话，读取会自动只用关键词，并在状态里写明。")
    return True


def _engine_question(io, sys_, home, hermes_cmd, args):
    """支持自带引擎的电脑上，向量一步先问怎么算。返回 vec（选了自带）、{"mode":"none",…}（先不开）、或 None（用 LM Studio／Ollama，接着走原来的检测）。"""
    pins = EP.get_pins(sys_.pins)
    none = {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
    while True:
        probs = EP.pins_problems(pins)
        a = EP.asset(pins) or {}
        dm = [m for m in EP.model_records(pins) if m.get("role") == "downloadable"]
        io.say("  怎么算向量？\n    1) 自带（推荐）：原委替你准备一个小程序（llama.cpp，官方发布）和一个模型（Qwen3-Embedding-0.6B），全在你自己的电脑上算，不用你装别的软件，也不用你去开任何服务。"
               "需要下载：程序%s，模型%s（你电脑上如果已经有同一个模型文件，比如 LM Studio 下过的，就直接用它，不再下载）。%s\n"
               "%s\n    2) 用你电脑上已有的 LM Studio 或 Ollama（LM Studio：测过的就是它，150 次对话用的就是那一套）\n    3) 先不开向量，以后再开（yuanwei vectors setup）" % (_mb(a.get("size")), _mb(dm[0].get("size") if dm else None),
                                                                                    "〔这一版自带引擎的下载地址和校验码还没核过，现在选不了〕" if probs else "",
                                                                                    "    自带程序算的向量：" + EP.consistency_label(CT.TESTED_EMBED_IDENTITY["model_file_sha256"], pins) + "。"))
        c = _pick(io, "选 [%d]：" % (2 if probs else 1), 3, 2 if probs else 1)
        if c == 3:
            return none
        if c == 2:
            return None
        if probs:
            io.say("  这一版自带引擎还没准备好（下载地址和校验码要发布前在真机上核）。请选 2 或 3。")
            continue
        rt = CT.runtime("llamacpp")
        vec = {"mode": "llamacpp", "model": rt["model"], "url": None, "download": False, "allow_remote": False, "install_numpy": False, "engine_dir": getattr(args, "engine_dir", None),
               "engine_url": None, "model_url": None, "model_file": None}
        vp = _engine_plan(sys_, vec, home, hermes_cmd, getattr(args, "hermes_python", None), {"mode": "llamacpp"})
        io.say("  " + engine_consent_text(vp).replace("\n", "\n  "))
        if vp.get("unreadable_dirs"):
            io.say("  （没能查看这些目录：%s；那里有没有现成的模型文件没核到。）" % "、".join(vp["unreadable_dirs"]))
        if vp.get("needs_program_download") or vp.get("needs_model_download"):
            if vp.get("free_bytes") is not None:
                io.say("  你的磁盘现在有 %s 空闲。" % _mb(vp["free_bytes"]))
            if not yes(io.ask("下载吗？ [y/N]："), False):
                io.say("  不下载，向量就先不开。")
                return none
            vec["download"] = True
        if not _numpy_question(io, vp, vec):
            return none
        return vec


def choose_home(args, io, home):
    """没用 --hermes-home 指定、这台电脑的 Hermes 又有机器人（profiles）时，问装给哪一个：主 Hermes 和每个机器人各有自己的家目录和库。
    已经指定了、或者当前就在某个机器人的环境里（HERMES_HOME 指着它），不问。只看目录结构列出机器人。"""
    if getattr(args, "hermes_home", None) or HP.profile_name(home):
        return home
    profs = HP.list_profiles(home)
    if not profs:
        return home
    root = HP.default_root(home)
    io.say("这台电脑的 Hermes 里有机器人（每个机器人是一个单独的档案，有自己的目录和记忆库）。这次装给哪一个？")
    io.say("  1) 主 Hermes（%s）" % root)
    for i, (n, d) in enumerate(profs, 2):
        io.say("  %d) 机器人 %s（%s）" % (i, n, d))
    i = _pick(io, "选 [1]：", len(profs) + 1, 1)
    return root if i == 1 else profs[i - 2][1]


def run_wizard(args, io, sys_, home, hermes_cmd):
    """完整的一问一答：问 → 计划 → 确认 → 执行。返回退出码。"""
    try:
        home = choose_home(args, io, home)
        ch = interactive(args, io, sys_, home, hermes_cmd)
        pl = make_plan(ch, sys_, home, args, hermes_cmd)
        io.say("\n第 8 步　计划")
        for l in pl["lines"]:
            io.say(l)
        if not yes(io.ask("确认执行吗？ [y/N]："), False):
            raise Cancel()
        io.say("\n执行中……")
        res = apply_plan(ch, pl, sys_, home, args, io)
        io.say("\n" + finish_text())
        if res.get("weak_identity"):
            io.say(weak_note(res))
        if tested_line_after(res):
            io.say(tested_line_after(res))
        return 0
    except Cancel:
        io.say("\n已取消，什么都没改。")
        return 1


# ================================================================ yuanwei vectors setup（以后再开向量／换模型／服务变了重新核验）
def provider_from_cfg(cfg):
    p = dict(cfg.provider or {})
    params = dict(p.get("params") or {})
    model = params.pop("model", None)
    row = CT.provider(p.get("id")) or {}
    return {"id": p.get("id") or "custom", "name": row.get("name") or p.get("id") or "自定义", "label": p.get("label") or row.get("label") or CT.UNTESTED, "base_url": p.get("base_url"),
            "model": model, "params": params, "key_env": p.get("api_key_env"), "verified": row.get("verified", False), "local_only": row.get("local_only", False), "note": row.get("note", "")}


def _leave_engine(old, home, io):
    """从自带引擎换成别的方式（或关掉向量）：只解绑这个使用方；别的使用方还在用就不停引擎。文件不删，说怎么删。"""
    if not (old or {}).get("managed") or not old.get("engine_dir"):
        return
    try:
        r = ER.unbind_user(EP.Paths(old["engine_dir"]), user_id(home))
    except EP.EngineError:
        io.say("（自带向量引擎正忙，没能解除登记；稍后再试 yuanwei vectors stop。）")
        return
    if r["remaining"]:
        state = "别的使用方还在用它，没有停它；"
    elif r["stopped"]:
        state = "已经停了它；"
    else:
        state = "没能确认它已经停了（原因：%s），可以稍后用 yuanwei vectors stop 再试；" % (r.get("reason") or "未知")
    io.say("自带向量引擎的文件还在（%s）；%s不要了用 yuanwei vectors remove-engine 删。" % (old["engine_dir"], state))


def vectors_setup(a, io, sys_, home, inst, cfg):
    from types import SimpleNamespace
    prov = provider_from_cfg(cfg)
    old = cfg.embedding or {}
    cmd = None
    try:
        if a.reverify and old.get("managed"):
            vec = {"mode": "llamacpp", "model": old.get("model") or CT.runtime("llamacpp")["model"], "url": None, "download": bool(a.download_model), "allow_remote": False, "install_numpy": bool(a.install_numpy),
                   "engine_dir": old.get("engine_dir") or getattr(a, "engine_dir", None), "engine_url": getattr(a, "engine_url", None), "model_url": getattr(a, "model_url", None),
                   "model_file": getattr(a, "model_file", None)}
        elif a.vectors == "none":
            vec = {"mode": "none", "download": False, "allow_remote": False, "install_numpy": False}
        elif a.vectors in ("llamacpp", "auto"):
            if not ES.engine_supported(sys_.platform_info()):
                _refuse("engine_unsupported", "这台电脑暂不支持自带向量引擎（现在只支持 Mac 的 M 系列芯片）。可以用 LM Studio 或 Ollama：--vectors lmstudio｜ollama")
            vec = {"mode": "llamacpp", "model": CT.runtime("llamacpp")["model"], "url": None, "download": bool(a.download_model), "allow_remote": False, "install_numpy": bool(a.install_numpy),
                   "engine_dir": getattr(a, "engine_dir", None), "engine_url": getattr(a, "engine_url", None), "model_url": getattr(a, "model_url", None), "model_file": getattr(a, "model_file", None)}
        elif a.reverify and old.get("runtime") and old.get("base_url"):
            vec = {"mode": old["runtime"], "model": old["model"], "url": old["base_url"], "download": False, "allow_remote": bool(old.get("allow_remote")), "install_numpy": bool(a.install_numpy)}
        elif a.vectors:
            vec = {"mode": a.vectors, "download": bool(a.download_model), "allow_remote": bool(a.allow_remote_embedding), "install_numpy": bool(a.install_numpy)}
            rt = CT.runtime(a.vectors)
            vec["model"] = a.embedding_model or (rt["model"] if rt else None)
            vec["url"] = a.embedding_url or (rt["base_url"] if rt else None)
            if not vec["model"] or not vec["url"]:
                _refuse("missing_embedding_fields", "--vectors custom 要同时给 --embedding-url 和 --embedding-model")
            if not VX.is_local_url(vec["url"]) and not a.allow_remote_embedding:
                _refuse("remote_embedding", "向量服务的地址不在本机：要用别处的服务，必须加 --allow-remote-embedding（索引的文字和查询会发给那个服务）")
        elif io.tty():
            args2 = SimpleNamespace(ollama_cmd=a.ollama_cmd, lms_cmd=a.lms_cmd, hermes_python=a.hermes_python)
            vec = _vector_questions(io, sys_, home, None, args2, None)
        else:
            _refuse("missing_vectors", "不在终端里：请用 --vectors ollama|lmstudio|custom 说明要用哪个（只想看计划就不加 --yes）")
        ch = {"host": "hermes", "preset": {"id": "vectors", "name": "向量", "label": ""}, "provider": prov, "vectors": vec, "key": {"choice": "env_or_file", "value": None, "test": False}}
        cmds = {k: v for k, v in (("ollama", a.ollama_cmd), ("lmstudio", a.lms_cmd)) if v}
        vp = vector_plan(sys_, ch, home, cmds, None, a.hermes_python)
        lines = []
        if vec["mode"] == "none":
            lines.append(" 向量        关掉（只用关键词）")
        else:
            if vec["mode"] == "llamacpp":
                lines.append(" 向量        自带（llama.cpp 服务程序）· %s · 放在 %s%s" % (vec["model"], vp.get("engine_dir"),
                                                                           "；需要下载（%s）" % ("你已同意" if vec["download"] else "还没同意：--download-model") if (vp.get("needs_program_download") or vp.get("needs_model_download")) else ""))
                if vp.get("pins_problems"):
                    lines.append("             这一版的钉住的下载地址和校验码还没核过（%s）：现在装不上" % "、".join(vp["pins_problems"][:4]))
            else:
                lines.append(" 向量        %s · %s（%s）%s" % ((CT.runtime(vec["mode"]) or {"name": "自己的向量服务"})["name"], vec["model"], vec["url"],
                                                        "；需要下载模型" + ("（你已同意）" if vec["download"] else "：还没同意（--download-model）") if vp.get("needs_download") else ""))
        user_toml = inst.get("config_path") and os.path.abspath(inst["config_path"]) != os.path.abspath(os.path.join(home, "yuanwei", "yuanwei.toml"))
        kw = dict(hermes_home=home, db=inst.get("db"), read_scope=inst.get("read_scope"), owners=inst.get("owners") or {}, mode=inst.get("mode") or "tools_only", src_path=inst.get("src_path"),
                  replace_provider=False, config_path=inst.get("config_path") or os.path.join(home, "yuanwei", "yuanwei.toml"), spawn=inst.get("spawn"), hermes_cmd=None, config_method="yaml", toml_text=None)
        io.say("将要做的事（还没有改任何东西）：")
        for l in lines:
            io.say(l)
        if user_toml:
            io.say(" 你用自己的配置文件（%s）：原委不改它。请你自己加这几行：" % inst["config_path"])
            ident_note = "（核验后会给出 identity 那一行）"
            io.say("   retrieval = \"hybrid\"\n   vector_fallback = \"partial\"\n   [embedding] 段：base_url、model、runtime、identity %s" % ident_note)
        if not (a.yes or io.tty()):
            io.say("加 --yes 才会真的改")
            return 0
        if io.tty() and not a.yes:
            if not yes(io.ask("确认执行吗？ [y/N]："), False):
                raise Cancel()
        elif not a.yes:
            io.say("加 --yes 才会真的改")
            return 0
        args2 = SimpleNamespace(config_path="__user__" if user_toml else None, pull_timeout=a.pull_timeout, ignore_probe_failure=True, hermes_python=a.hermes_python)
        pl = {"plan": {"kw": kw}, "vp": vp}
        if vec["mode"] == "none":
            kw["toml_text"] = build_toml(ch, home) if not user_toml else None
            res = HS.apply_install(**kw) if kw["toml_text"] else {"changed": False}
            io.say("已关掉向量。")
            _leave_engine(old, home, io)
            return 0
        res = apply_plan(ch, pl, sys_, home, args2, io)
        if vec["mode"] != "llamacpp":
            _leave_engine(old, home, io)
        io.say("装好了。向量会在后台慢慢补算；看进度：%s vectors status。服务变了以后重新核验：%s vectors setup --reverify。" % (entry_command(), entry_command()))
        if res.get("weak_identity"):
            io.say(weak_note(res))
        if tested_line_after(res):
            io.say(tested_line_after(res))
        return 0
    except Cancel:
        io.say("已取消，什么都没改。")
        return 1
