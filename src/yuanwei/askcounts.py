"""提问卡回答的计数，按 Hermes 数据目录累计（清单第 53 条，0.1.0a8）。

Hermes 每一轮都新起一个原委实例，实例里的计数会被下一轮冲掉；这里照 skipreasons、internal_errors 的做法累计到
<hermes_home>/yuanwei/clarify-counts.json：总数（存下的回答条数、有回答存下的轮数、各种 clarify_* 备注各几次），加最近 KEEP 次（时间、回答条数、备注名）。
**只有计数、固定的备注名和时间，没有任何原话、题目、编号、路径。** 写不了不影响记忆；坏文件不覆盖，读的时候报问题而不当成零。

clarify_* 备注**不是**「这一轮没记」：这一轮照常记了，备注说的是提问卡那一部分怎么了（见 MEANING）。没记的轮次在 skip-reasons.json 里。
0.1.0a9（清单第 55 条）起，助手没答完时用户又发的话（midturn_*、steer_dropped:*）的备注和「存下几条」（midturn_kept）也记在这里，同样不是「这一轮没记」。
"""
import fcntl
import json
import os
import re
import time
from pathlib import Path

FILE = "clarify-counts.json"
LOCK = "clarify-counts.lock"
KEEP = 30
MAX_NAMES = 100
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
MEANING = ("clarify_* 不是「这一轮没记」：这一轮照常记了，备注说的是提问卡那一部分怎么了——dropped:*＝那部分回答没收（shared_chat 不是一对一、unknown_shape 格式认不出、"
           "bad_json 结果解不开（多半是 Hermes 外发前的密钥清洗弄坏的）、host_rewritten 被 Hermes 换成了固定占位、unpaired 配不上、turn_unclear 认不清是哪一轮、"
           "bad_text 有坏字符、tag_in_text 助手的字里有标签、internal_error 原委自己出错）；options_trimmed＝太长，去掉了选项行；"
           "long＝去掉选项还超过长消息线；control:closed＝用户打的回答里认出「关」、关上了窗口；control:close_failed＝没关成：本进程里先暂停读取，并提示你再说一次；control:confirm_not_counted＝回答里的确认不算数，删除、开启都没发生。"
           "这两种的提示通常在下一轮出现，Hermes 重启过或对话闲置超过约一小时的，提示可能没有（删除、开启照旧不会发生，只是少了这句说明）。"
           "answers_kept 是存下的回答条数，turns_with_answers 是有回答存下的轮数。"
           "midturn_*、steer_dropped:* 说的是「助手没答完时你又发的话」（清单第 55 条，0.1.0a9）那一部分，这一轮同样照常记了：midturn_kept＝存下一条（每条记一次）；"
           "midturn_dropped:*＝那部分没收（unmatched 和开头那句对不上、只记了开头和回复；turn_unclear 认不清是哪一轮；unknown_envelope 聊天平台的来源说明认不得；from_bot 是机器人发的；"
           "bad_text 有坏字符；internal_error 原委自己出错）；steer_dropped:unknown_shape＝插话外壳对不上，host_note＝宿主自己插的看板便条；"
           "midturn_control:closed／close_failed／confirm_not_counted＝同上面提问卡那几条，只是来源是中途的话。"
           "opening_only:row_not_found（清单第 56 条，0.1.0a10）＝宿主说这一轮是你开的，但它在压缩里把开头那一行改写或并走了，找不到那一行：只记了开头那句话和助手的回复，"
           "这一轮里提问卡的回答、中途又说的话都没收。"
           "整轮没记的（midturn_ambiguous、midturn_unmatched、midturn_shared_chat）在 skip-reasons.json 里。")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean(data):
    totals, recent = data.get("totals"), data.get("recent")
    if not isinstance(totals, dict) or not isinstance(recent, list):
        raise ValueError("format")
    for k, n in totals.items():
        if not isinstance(k, str) or not _NAME.fullmatch(k) or type(n) is not int or n < 0:
            raise ValueError("format")
    out = []
    for r in recent:
        if (not isinstance(r, dict) or not isinstance(r.get("t"), str) or type(r.get("answers")) is not int or not isinstance(r.get("notes"), list)
                or not all(isinstance(x, str) and _NAME.fullmatch(x) for x in r["notes"])):
            raise ValueError("format")
        out.append({"t": r["t"], "answers": r["answers"], "notes": list(r["notes"])})
    return {"totals": totals, "recent": out}


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
    """只读。{"totals": {名字: 次数}, "recent": [{t, answers, notes}…最早在前], "problem": None 或固定类别, "meaning": 说明}；读不了不冒充零。"""
    empty = {"totals": {}, "recent": [], "meaning": MEANING}
    if not home:
        return dict(empty, problem=None)
    directory = Path(home) / "yuanwei"
    path = directory / FILE
    if directory.is_symlink() or path.is_symlink():
        return dict(empty, problem="unsafe_path")
    try:
        return dict(_load(path), problem=None, meaning=MEANING)
    except OSError:
        return dict(empty, problem="unreadable")
    except ValueError:
        return dict(empty, problem="invalid_data")


def record(home, answers, notes, now=None):
    """记一轮：answers＝这一轮存下的回答条数，notes＝备注名（可重复）。两样都没有就什么也不写。写成功返回 True；写不了返回 False，不抛，不影响记忆。"""
    if not home:
        return False
    try:
        n = answers if type(answers) is int and answers > 0 else 0
        names = [x if isinstance(x, str) and _NAME.fullmatch(x) else "other" for x in (notes or ())]     # 只收固定格式的名字；别的一律记成 other，绝不把字符串原样落盘
        if not n and not names:
            return False
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
                tot = data["totals"]

                def bump(k, by):
                    if k in tot or len(tot) < MAX_NAMES:
                        tot[k] = tot.get(k, 0) + by
                if n:
                    bump("answers_kept", n)
                    bump("turns_with_answers", 1)
                for x in names:
                    bump(x, 1)
                data["recent"] = (data["recent"] + [{"t": now or _now(), "answers": n, "notes": names}])[-KEEP:]
                tmp = directory / ("%s.tmp-%d" % (FILE, os.getpid()))
                tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
                os.replace(tmp, path)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, ValueError, ImportError):
        return False
