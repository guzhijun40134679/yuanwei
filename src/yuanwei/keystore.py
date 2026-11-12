"""整理用密钥的存放：原委自己的密钥文件（权限 0600，目录 0700）有两处——
· 全局的一份：~/.yuanwei/keys/<变量名>（环境变量 YUANWEI_KEY_DIR 可以改目录，只认绝对路径）。几个机器人共用；
· 这个机器人自己的一份：<hermes 家目录>/yuanwei/keys/<变量名>（单独给这个机器人设的，有它就先用它）。
读的先后：这个机器人自己的文件 → 全局文件 → 环境变量。**只有配置里有 key_file 的时候才看全局文件**（向导装的配置都有；没有 key_file 的配置，
比如研究和调试用的，只看环境变量，行为不变）。这个模块不打印、不返回错误文字里的密钥值；任何函数都只在返回值里给出值，且只有 lookup／read_key 会返回它。
文件不是 0600、或不属于当前用户：不使用，报「权限不对」（后面的来源找得到就用后面的，都找不到才报）。不支持 0600 的系统（非 POSIX）：不存文件，只认环境变量。
"""
import os
import re
import stat

from .errors import ConfigError

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MIN_LEN, MAX_LEN = 8, 512


def supported():
    return os.name == "posix"


def key_dir(home):
    return os.path.join(str(home), "yuanwei", "keys")


def _checked_name(name):
    if not NAME_RE.match(name or ""):
        raise ConfigError("变量名只能是字母、数字、下划线，且不以数字开头")
    return name


def key_file(home, name):
    return os.path.join(key_dir(home), _checked_name(name))


def global_key_dir(environ=None):
    """全局密钥目录：YUANWEI_KEY_DIR（先展开 ~；展开后必须是绝对路径，否则报错、不用），没有就是 ~/.yuanwei/keys。
    相对路径会随当前工作目录变，终端和后台服务可能落到不同的地方，所以不认。这个目录会被设成只有你能读（0700），要用专门放密钥的目录。"""
    environ = os.environ if environ is None else environ
    v = environ.get("YUANWEI_KEY_DIR")
    if not v:
        return os.path.join(os.path.expanduser("~"), ".yuanwei", "keys")
    d = os.path.expanduser(v)
    if not os.path.isabs(d):
        raise ConfigError("YUANWEI_KEY_DIR 要写绝对路径（可以用 ~ 开头）；现在这个写法会随当前目录变，原委不用它")
    return d


def global_dir_problem(environ=None):
    """全局密钥目录的设置有没有问题：None＝没有；否则是一句人话（不含任何密钥）。"""
    try:
        global_key_dir(environ)
    except ConfigError as e:
        return str(e)
    return None


def global_key_file(name, environ=None):
    return os.path.join(global_key_dir(environ), _checked_name(name))


def validate_value(v):
    """返回 (整理后的值 或 None, 原因类别 或 None)。原因只有类别，不回显任何字符。"""
    if not isinstance(v, str):
        return None, "not_text"
    v = v.strip()
    if not v:
        return None, "empty"
    if len(v) < MIN_LEN:
        return None, "too_short"
    if len(v) > MAX_LEN:
        return None, "too_long"
    if any(ch.isspace() for ch in v):
        return None, "has_whitespace"
    if not all(32 < ord(ch) < 127 for ch in v):
        return None, "not_ascii"
    return v, None


def check_file(path):
    """(能用吗, 原因类别)。类别：missing、not_a_file、bad_mode、bad_owner、dir_bad_mode。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False, "missing"
    except OSError:
        return False, "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return False, "not_a_file"
    if supported():
        if stat.S_IMODE(st.st_mode) != 0o600:
            return False, "bad_mode"
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return False, "bad_owner"
    return True, None


def write_key(home, name, value):
    """写这个机器人自己的密钥文件。原子写：先写同目录的临时文件（0600）再改名。中途失败不留半个文件。不支持 0600 的系统拒绝。"""
    return _write(key_dir(home), _checked_name(name), value)


def write_global_key(name, value, environ=None):
    """写全局密钥文件（~/.yuanwei/keys/<变量名>），做法和 write_key 完全一样。"""
    return _write(global_key_dir(environ), _checked_name(name), value)


def _write(d, name, value):
    if not supported():
        raise ConfigError("这个系统没法把文件权限设成只有你能读：原委不存密钥文件，请用环境变量")
    v, why = validate_value(value)
    if v is None:
        raise ConfigError("密钥格式不对（%s）" % why)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    path = os.path.join(d, name)
    tmp = path + ".tmp-%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(v + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class UnusableFile(ConfigError):
    """密钥文件不能用；why 是不含正文的原因类别（bad_mode、unreadable、bad_content 等）。"""

    def __init__(self, why, text):
        super().__init__(text)
        self.why = why


def read_key(path):
    ok, why = check_file(path)
    if not ok:
        raise UnusableFile(why, "密钥文件不能用（%s）" % why)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeError:                                                                # 内容不是 UTF-8：只报类别，不带任何正文
        raise UnusableFile("bad_content", "密钥文件的内容格式不对（bad_content）") from None
    except OSError:                                                                     # 检查之后文件没了、没有读权限等：只报类别
        raise UnusableFile("unreadable", "密钥文件不能用（unreadable）") from None
    v, bad = validate_value(text)
    if v is None:
        raise UnusableFile("bad_content", "密钥文件的内容格式不对（%s）" % bad)
    return v


def delete_key(home, name):
    return delete_file(key_file(home, name))


def delete_global_key(name, environ=None):
    return delete_file(global_key_file(name, environ))


def delete_file(path):
    """删一个密钥文件；本来就不在返回 False。"""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def file_report(path):
    """给 key status 用：{path, present, usable, problem}。这里只看文件类型、权限和属主，不看内容：usable 为真只表示「文件类型和权限过得去」，不代表里面的值合格（真正用的时候 lookup 会读一遍并核对格式，但任何地方都不显示值）。
    路径是 None（这份设置不读密钥文件）时 present 为假。"""
    if not path:
        return {"path": None, "present": False, "usable": False, "problem": None}
    ok, why = check_file(path)
    return {"path": path, "present": why != "missing", "usable": ok, "problem": None if ok or why == "missing" else why}


def lookup(env_name, key_path=None, environ=None):
    """(值 或 None, 来源)。来源：file（这个机器人自己的文件）、global_file（全局文件）、env、none、file_unusable:<原因>（有文件在、不能用，后面又都找不到）。
    先后：自己的文件 → 全局文件 → 环境变量。**没给 key_path（配置里没有 key_file）就不看两个文件，只看环境变量。**"""
    environ = os.environ if environ is None else environ
    why = None
    if key_path:
        w = None
        try:
            return read_key(key_path), "file"
        except UnusableFile as e:
            w = e.why
        if w != "missing":
            why = w
        try:
            gpath = global_key_file(env_name, environ)
        except ConfigError:                                                             # 变量名不合格、或 YUANWEI_KEY_DIR 不是绝对路径：不用全局文件
            gpath = None
        if gpath and os.path.abspath(gpath) != os.path.abspath(key_path):
            try:
                return read_key(gpath), "global_file"
            except UnusableFile as e:
                w = e.why
            if w != "missing" and why is None:
                why = w
    v = environ.get(env_name or "")
    if v:
        return v, "env"
    return None, ("file_unusable:" + why) if why else "none"


def source(env_name, key_path=None, environ=None):
    """只说来源，不返回值。"""
    _v, src = lookup(env_name, key_path, environ)
    return src


UNUSABLE_NOTE = "有一份密钥文件在，但不能用，后面的来源也没找到；这时没有去看 Hermes 的 .env，所以这不等于 Hermes 运行时一定没有可用的密钥（只是原委自己没找到能用的）。"


def source_with_dotenv(env_name, key_path, hermes_home, environ=None):
    """lookup 的来源，再加一层：都没有时看 Hermes 的 .env 里有没有这个名字（只看名字，不读值；Hermes 运行时会自己读它）。
    有就是 hermes_env_file；读不了是 unchecked；没有是 none（file_unusable:… 原样保留，这时不看 .env：这不等于 Hermes 运行时一定没有可用的密钥，见 UNUSABLE_NOTE）。status hermes 和 key status 共用这一个。"""
    src = source(env_name, key_path, environ)
    if src == "none" and env_name:
        d = dotenv_has_name(os.path.join(str(hermes_home), ".env"), env_name)
        return "hermes_env_file" if d else ("unchecked" if d is None else "none")
    return src


def dotenv_has_name(path, name):
    """只回答「这个文件里有没有 NAME=非空值 这一行」。值读完即丢，不返回、不记。读不了返回 None（没核到）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                s = ln.strip()
                if s.startswith("export "):
                    s = s[7:].lstrip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, _, val = s.partition("=")
                if k.strip() == name:
                    v = val.strip().strip("\"'")
                    return bool(v)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return False


def check_key(base_url, model, params, key, opener=None, timeout=30):
    """「试一下」：向整理用的供应商发一个最小的请求（会花一点点钱，调用方要先问过用户）。只返回 works／rejected／unverified：
    200 才是 works；401、403 是 rejected（对方拒绝了这把密钥）；其余（429、5xx、超时、连不上、回应读不了）一律 unverified，不是密钥的问题。不返回、不记录对方的任何正文。"""
    import json
    import urllib.error
    import urllib.request
    opener = opener or urllib.request.urlopen
    body = dict(params or {})
    for k in ("max_tokens", "max_completion_tokens"):
        if k in body:
            body[k] = 16
    if "max_tokens" not in body and "max_completion_tokens" not in body:
        body["max_tokens"] = 16
    body.update(model=model, messages=[{"role": "user", "content": "hi"}])
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with opener(req, timeout=timeout) as r:
            r.read()
            return "works" if 200 <= getattr(r, "status", 200) < 300 else "unverified"
    except urllib.error.HTTPError as e:
        return "rejected" if e.code in (401, 403) else "unverified"
    except (urllib.error.URLError, OSError, ValueError):
        return "unverified"
