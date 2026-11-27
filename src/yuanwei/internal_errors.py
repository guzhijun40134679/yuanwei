"""没归类的内部错误，按 Hermes 数据目录累计。

起因（11-19 真机）：工具遇到没归类的异常只回类名（`〔yuanwei｜错误｜internal_error〕PackError`），真机上没法查是哪一行抛的。
这里把每次累计到 <hermes_home>/yuanwei/internal-errors.json（0.1.0a8 起也记写入时提问卡回答那一步出的错；卸载第三步删掉它）：按「异常类名＋抛出处」的总次数，加最近 KEEP 次（时间＋哪一项）。
**不存异常的消息文字**（它可能带着原话）；抛出处只存文件名（不带目录）和行号。
写法照 skipreasons：进程锁下重读、原子换文件，多个实例和重启后继续累计；写不了不影响记忆；坏文件不覆盖，读的时候报问题而不当成零。
"""
import fcntl
import json
import os
import re
import time
from pathlib import Path

FILE = "internal-errors.json"
LOCK = "internal-errors.lock"
KEEP = 30                                                # 最近几次
MAX_KINDS = 100                                          # 总次数表里最多几种（类名＋抛出处是代码里的固定东西，不会多）
_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_WHERE = re.compile(r"[A-Za-z0-9_.-]{1,80}:[0-9]{1,7}")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def describe(exc):
    """异常 → (类名, 抛出处 "文件名:行号" 或 "unknown")。只取类名和最内层栈帧的文件名、行号；不碰异常的消息文字。"""
    cls = type(exc).__name__
    cls = cls if _CLASS.fullmatch(cls) else "other"
    where = "unknown"
    tb = getattr(exc, "__traceback__", None)
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is not None:
        name = os.path.basename(last.tb_frame.f_code.co_filename or "")
        cand = "%s:%d" % (name, last.tb_lineno)
        where = cand if _WHERE.fullmatch(cand) else "unknown"
    return cls, where


def _key(cls, where):
    return "%s@%s" % (cls, where)


def _clean(data):
    totals, recent = data.get("totals"), data.get("recent")
    if not isinstance(totals, dict) or not isinstance(recent, list):
        raise ValueError("format")
    out_totals = {}
    for k, v in totals.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            raise ValueError("format")
        cls, where, n = v.get("class"), v.get("where"), v.get("count")
        if not (isinstance(cls, str) and _CLASS.fullmatch(cls) and isinstance(where, str) and (where == "unknown" or _WHERE.fullmatch(where)) and type(n) is int and n >= 0):
            raise ValueError("format")
        out_totals[k] = {"class": cls, "where": where, "count": n}
    out_recent = []
    for r in recent:
        if not isinstance(r, dict) or not isinstance(r.get("t"), str) or not isinstance(r.get("key"), str):
            raise ValueError("format")
        out_recent.append({"t": r["t"], "key": r["key"]})
    return {"totals": out_totals, "recent": out_recent}


def _load(path):
    if path.is_symlink():
        raise ValueError("symlink")
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {"totals": {}, "recent": []}
    if not isinstance(data, dict):
        raise ValueError("format")
    return _clean(data)


def read(home):
    """只读。{"totals": {键: {class, where, count}}, "recent": [{"t", "key"}…最早在前], "problem": None 或固定类别}；读不了不冒充零条。"""
    empty = {"totals": {}, "recent": []}
    if not home:
        return dict(empty, problem=None)
    directory = Path(home) / "yuanwei"
    path = directory / FILE
    if directory.is_symlink() or path.is_symlink():
        return dict(empty, problem="unsafe_path")
    try:
        return dict(_load(path), problem=None)
    except OSError:
        return dict(empty, problem="unreadable")
    except ValueError:
        return dict(empty, problem="invalid_data")


def record(home, exc, now=None):
    """记一次内部错误。写成功返回 True；写不了（没有目录、坏文件、符号链接）返回 False，不抛，不影响记忆。"""
    if not home:
        return False
    try:
        cls, where = describe(exc)
        key = _key(cls, where)
        directory = Path(home) / "yuanwei"
        if not Path(home).is_dir() or directory.is_symlink():
            return False
        directory.mkdir(exist_ok=True)
        fd = os.open(directory / LOCK, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                path = directory / FILE
                data = _load(path)
                if key in data["totals"] or len(data["totals"]) < MAX_KINDS:
                    ent = data["totals"].setdefault(key, {"class": cls, "where": where, "count": 0})
                    ent["count"] += 1
                data["recent"] = (data["recent"] + [{"t": now or _now(), "key": key}])[-KEEP:]
                tmp = directory / ("%s.tmp-%d" % (FILE, os.getpid()))
                tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
                os.replace(tmp, path)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, ValueError, ImportError):
        return False
