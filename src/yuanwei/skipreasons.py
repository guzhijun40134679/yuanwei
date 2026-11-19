"""写入跳过的原因，按 Hermes 数据目录累计。

Hermes 每一轮都新起一个原委实例，实例里的计数（status.json 的 skipped）会被下一轮覆盖，真机上第一次跳过的原因就这样丢了。
这里把每次跳过累计到 <hermes_home>/yuanwei/skip-reasons.json：按原因的总次数，加最近 KEEP 次（原因＋时间）。
只有固定格式的原因和时间，没有任何原话、编号、路径。写不了不影响记忆；坏文件不覆盖，读的时候报问题而不当成零。
写法照 owners.increment：进程锁下重读、原子换文件，多个实例和重启后继续累计。
"""
import fcntl
import json
import os
import re
import time
from pathlib import Path

FILE = "skip-reasons.json"
LOCK = "skip-reasons.lock"
KEEP = 30                                                # 最近几次
MAX_REASONS = 100                                        # 总次数表里最多几种原因（原因是代码里的固定名字，不会多）
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,80}")                # 原因名都是代码里或 Hermes 给的固定词（可带 - . :）；中文、空格、斜杠（路径）都不收


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean(data):
    totals, recent = data.get("totals"), data.get("recent")
    if not isinstance(totals, dict) or not isinstance(recent, list):
        raise ValueError("format")
    for why, n in totals.items():
        if not isinstance(why, str) or not _REASON.fullmatch(why) or type(n) is not int or n < 0:
            raise ValueError("format")
    for r in recent:
        if not isinstance(r, dict) or not isinstance(r.get("why"), str) or not isinstance(r.get("t"), str) or not _REASON.fullmatch(r["why"]):
            raise ValueError("format")
    return {"totals": totals, "recent": [{"t": r["t"], "why": r["why"]} for r in recent]}


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
    """只读。{"totals": {原因: 次数}, "recent": [{"t", "why"}…最早在前], "problem": None 或固定类别}；读不了不冒充零条。"""
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


def record(home, reason, now=None):
    """记一次跳过。写成功返回 True；写不了（没有目录、坏文件、符号链接）返回 False，不抛，不影响记忆。"""
    if not home:
        return False
    try:
        why = reason if isinstance(reason, str) and _REASON.fullmatch(reason) else "other"       # 只收固定格式的原因名；别的一律记成 other，绝不把字符串原样落盘
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
                if why in data["totals"] or len(data["totals"]) < MAX_REASONS:
                    data["totals"][why] = data["totals"].get(why, 0) + 1
                data["recent"] = (data["recent"] + [{"t": now or _now(), "why": why}])[-KEEP:]
                tmp = directory / ("%s.tmp-%d" % (FILE, os.getpid()))
                tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
                os.replace(tmp, path)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, ValueError, ImportError):
        return False
