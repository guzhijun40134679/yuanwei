"""本人账号的小设置与未登记消息计数；不读取 Hermes 会话、密钥或记忆库。"""
import contextlib
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
from pathlib import Path

from . import hermes_profile as HP
from . import hermes_setup as HS

_PLATFORM = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def platform_name(value):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith("C") for c in value):
        raise HS.SetupRefused("bad_owner", "平台名要写成字母、数字、下划线或短横线。")
    value = value.strip().lower()
    if not _PLATFORM.fullmatch(value):
        raise HS.SetupRefused("bad_owner", "平台名要以字母开头，只能含字母、数字、下划线或短横线。")
    return value


def _ident(value):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith("C") for c in value):
        raise HS.SetupRefused("bad_owner", "账号编号不能为空或带控制字符。")
    value = value.strip()
    if not value or any(c in value for c in "*?[]"):
        raise HS.SetupRefused("bad_owner", "请填写具体的本人账号编号，不能使用空值或通配符。")
    return value


def parse(items):
    """--owner 可重复；第一个冒号分平台，编号里的其他冒号原样保留。"""
    out = {}
    for item in items or []:
        if not isinstance(item, str):
            raise HS.SetupRefused("bad_owner", "--owner 要写成 平台:编号。")
        plat, sep, ident = item.partition(":")
        if not sep:
            raise HS.SetupRefused("bad_owner", "--owner 要写成 平台:编号，例如 telegram:123456。")
        plat, ident = platform_name(plat), _ident(ident)
        if ident not in out.setdefault(plat, []):
            out[plat].append(ident)
    return out


def _owner_map(raw):
    if not isinstance(raw, dict):
        raise HS.SetupRefused("bad_owner_config", "原委设置里的 owners 不是平台到账号列表的映射。")
    out = {}
    for plat, ids in raw.items():
        if not isinstance(ids, list):
            raise HS.SetupRefused("bad_owner_config", "原委设置里的每个平台都要对应账号列表。")
        p = platform_name(plat)
        out.setdefault(p, [])
        for ident in ids:
            value = _ident(ident)
            if value not in out[p]:
                out[p].append(value)
    return out


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _atomic(path, data, mode=0o600):
    fd, tmp = tempfile.mkstemp(prefix=".%s-" % path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def settings(home, action="list", platform=None, ids=None, apply=False):
    """list 显示已登记编号；set 替换该平台名单；delete 删指定编号或整个平台。

    修改默认只列计划；写入只改原委 config.json 与 install.json 的指纹，并备份二者。
    已运行的插件不会热加载；返回 restart_hermes 提醒用户自行重启。
    """
    if action not in ("list", "set", "delete"):
        raise HS.SetupRefused("bad_owner_action", "操作只能是 list、set 或 delete。")
    plat = platform_name(platform) if platform is not None else None
    if action != "list" and plat is None:
        raise HS.SetupRefused("bad_owner", "登记或删除时要指定平台。")
    if ids is not None and not isinstance(ids, (list, tuple)):
        raise HS.SetupRefused("bad_owner", "账号编号要按列表提供。")
    values = list(dict.fromkeys(_ident(v) for v in (ids or [])))
    if action == "set" and not values:
        raise HS.SetupRefused("bad_owner", "登记时至少填一个本人编号；清空请用 delete。")
    if action == "list" and values:
        raise HS.SetupRefused("bad_owner", "查看时不用填写账号编号。")
    directory = Path(home) / "yuanwei"
    cp, mp = directory / "config.json", directory / "install.json"
    if directory.is_symlink() or cp.is_symlink() or mp.is_symlink():
        raise HS.SetupRefused("owner_settings_symlink", "原委设置是符号链接：没有读取或修改。")
    try:
        original = cp.read_bytes()
        config = json.loads(original)
    except (OSError, ValueError):
        raise HS.SetupRefused("owner_settings_unreadable", "没有可读的原委 config.json；请先为这个 Hermes 安装原委。")
    if not isinstance(config, dict):
        raise HS.SetupRefused("owner_settings_unreadable", "原委 config.json 格式不对。")
    check = HP.home_check(config, str(home))
    if not check["ok"]:
        raise HS.SetupRefused(check["state"], "这份原委设置不属于当前 Hermes 或归属无法确认；没有读取本人名单或修改。")
    owners = _owner_map(config.get("owners") or {})
    if action == "list":
        return {"action": action, "owners": owners if plat is None else {plat: owners.get(plat, [])}, "applied": False, "changed": False}
    proposed = {p: list(v) for p, v in owners.items()}
    if action == "set":
        proposed[plat] = values
    elif values:
        remaining = [v for v in proposed.get(plat, []) if v not in values]
        if remaining:
            proposed[plat] = remaining
        else:
            proposed.pop(plat, None)
    else:
        proposed.pop(plat, None)
    changed = proposed != owners
    try:
        old_manifest = mp.read_bytes()
        manifest = json.loads(old_manifest)
    except (OSError, ValueError):
        raise HS.SetupRefused("owner_install_unreadable", "没有可读的原委安装记录；没有改设置。")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise HS.SetupRefused("owner_install_unreadable", "原委安装记录格式不对；没有改设置。")
    if manifest["files"].get("yuanwei/config.json") != HS._sha(original):
        raise HS.SetupRefused("config_modified", "原委 config.json 被改过或没有安装指纹：不覆盖，请先核对设置。")
    out = {"action": action, "platform": plat, "owners": {plat: proposed.get(plat, [])}, "applied": bool(apply),
           "changed": bool(apply and changed), "would_change": changed, "restart_hermes": bool(apply and changed)}
    if not apply or not changed:
        return out
    config["owners"] = proposed
    new_config = _bytes(config)
    manifest["files"]["yuanwei/config.json"] = HS._sha(new_config)
    new_manifest = _bytes(manifest)
    mode_c, mode_m = stat.S_IMODE(cp.stat().st_mode), stat.S_IMODE(mp.stat().st_mode)
    ts = time.strftime("%Y%m%dT%H%M%S")
    backups = [HS._unique_backup(p, ts) for p in (cp, mp)]
    try:
        # 防止列计划/读取后用户改过设置；不覆盖读到的版本之外的内容。
        if cp.read_bytes() != original or mp.read_bytes() != old_manifest:
            raise HS.SetupRefused("owner_settings_changed", "设置在这次操作中发生变化，请重试。")
        for src, backup in zip((cp, mp), backups):
            HS.copy_with_same_mode(src, backup)
        _atomic(cp, new_config, mode_c)
        try:
            _atomic(mp, new_manifest, mode_m)
        except OSError:
            try:
                _atomic(cp, original, mode_c)
            except OSError as rollback_error:
                raise HS.SetupRefused("owner_settings_rollback_failed",
                                      "安装指纹写入失败，而且设置回滚没有完成。请先从备份恢复 config.json 和 install.json，再重试；备份分别是 %s、%s。"
                                      % tuple(str(p) for p in backups)) from rollback_error
            raise
    except OSError as exc:
        raise HS.SetupRefused("owner_settings_write_failed", "本人账号设置写入失败；原文件备份保留在 yuanwei 目录（%s）。" % type(exc).__name__) from exc
    out["backups"] = [str(p) for p in backups]
    return out


def _read_counts(path):
    if path.is_symlink():
        raise ValueError("symlink")
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("counter format")
    for platform, n in data.items():
        if not isinstance(platform, str) or not _PLATFORM.fullmatch(platform) or type(n) is not int or n < 0:
            raise ValueError("counter format")
    return data


def count_status(home):
    """只读诊断；问题只报固定类别，损坏/不可读不冒充零条。"""
    if not home:
        return {"counts": {}, "problem": None}
    directory = Path(home) / "yuanwei"
    path = directory / "owner-skips.json"
    if directory.is_symlink() or path.is_symlink():
        return {"counts": {}, "problem": "unsafe_path"}
    try:
        return {"counts": _read_counts(path), "problem": None}
    except OSError:
        return {"counts": {}, "problem": "unreadable"}
    except ValueError:
        return {"counts": {}, "problem": "invalid_data"}


def counts(home):
    """兼容只取计数的调用方；展示状态请用 count_status 区分读取失败。"""
    return count_status(home)["counts"]


def increment(home, platform):
    """仅平台与数量；进程锁下重读并原子换文件，重启/多个实例继续累计。

    写不了返回 False，坏计数文件不覆盖；不影响拒绝未登记账号的消息。
    """
    if not home:
        return False
    try:
        import fcntl
        platform = platform_name(platform)
        directory = Path(home) / "yuanwei"
        if not Path(home).is_dir() or directory.is_symlink():
            return False
        directory.mkdir(exist_ok=True)
        lock = directory / "owner-skips.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock, flags, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                path = directory / "owner-skips.json"
                data = _read_counts(path)
                data[platform] = data.get(platform, 0) + 1
                _atomic(path, _bytes(data))
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, ValueError, ImportError, HS.SetupRefused):
        return False
