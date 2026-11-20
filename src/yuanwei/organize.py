"""整理开关（0.1.0a7）：`yuanwei organize on|off|status [hermes]`。

「整理」是原委用一个大模型把原话归到各条主线上。关掉以后：原话、切句、索引、向量、封步都照常，但不拉起整理、不发请求、不花钱、不要密钥；
查回来的原话不挂「未整理」；关着时封好的步留着，再打开时按封步顺序补。默认开着。

开关是 Config 的 `organize`，按机器人（Hermes 家目录）存在这个机器人的 yuanwei.toml 里，所有工作区一起生效。
改它只动 yuanwei.toml 和安装记录 install.json 里它的指纹（备份二者）；不加 --yes 只列计划。用户自己的配置文件（--config 给的）原委不改，只说要加哪一行。
不读 Hermes 会话、密钥或记忆库的内容（关着时「封好几步」只数库里步的状态）。
"""
import contextlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

from . import hermes_profile as HP
from . import hermes_setup as HS

ACTIONS = ("on", "off", "status")
LINE_OFF = "organize = false"


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


def edit_toml(text, off):
    """给原委自己写的 yuanwei.toml 改整理开关：关＝顶层加一行 `organize = false`（在第一个 [表] 之前），开＝把这一行去掉（回到默认的样子）。
    改完用 tomllib 重读：organize 的值对、别的项一个没变，才交回；不对就抛 ValueError。"""
    import tomllib
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    first = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    kept = [l for i, l in enumerate(lines) if not (i < first and re.match(r"\s*organize\s*=", l))]
    first = next((i for i, l in enumerate(kept) if l.lstrip().startswith("[")), len(kept))
    if off:
        at = first
        while at > 0 and kept[at - 1].strip() == "":
            at -= 1
        if at > 0 and not kept[at - 1].endswith("\n"):
            kept[at - 1] += "\n"
        kept.insert(at, LINE_OFF + "\n")
    new = "".join(kept)
    after = tomllib.loads(new)
    got = after.pop("organize", True)
    before.pop("organize", None)
    if got is not (not off) or after != before:
        raise ValueError("改完的设置和预期不一样")
    return new


def waiting_steps(db):
    """库里封了但还没整理的步数（所有工作区；状态是 sealed 或 failed）。只读；库不在或读不了返回 None（不冒充 0）。"""
    if not db or not os.path.exists(db):
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        try:
            return c.execute("SELECT COUNT(*) FROM steps WHERE status IN ('sealed','failed')").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return None


def state_text(on, waiting):
    if on:
        return "整理：开"
    t = "整理：关"
    if waiting:
        t += "；关着时封好的 %d 步，打开后按封步顺序补" % waiting
    elif waiting == 0:
        t += "；现在没有封好等着补的步"
    return t


def _load(home):
    """读这个机器人的安装设置。返回 (config.json 内容, config.json 路径, install.json 路径, 原始字节)。读不了或归属不对抛 HS.SetupRefused。"""
    directory = Path(home) / "yuanwei"
    cp, mp = directory / "config.json", directory / "install.json"
    if directory.is_symlink() or cp.is_symlink() or mp.is_symlink():
        raise HS.SetupRefused("organize_settings_symlink", "原委设置是符号链接：没有读取或修改。")
    try:
        raw = cp.read_bytes()
        config = json.loads(raw)
    except (OSError, ValueError):
        raise HS.SetupRefused("organize_settings_unreadable", "没有可读的原委 config.json；请先为这个 Hermes 安装原委。")
    if not isinstance(config, dict):
        raise HS.SetupRefused("organize_settings_unreadable", "原委 config.json 格式不对。")
    check = HP.home_check(config, str(home))
    if not check["ok"]:
        raise HS.SetupRefused(check["state"], "这份原委设置不属于当前 Hermes 或归属无法确认；没有读取或修改。")
    return config, cp, mp, raw


def read_state(home):
    """只读：{"on": bool, "config_path", "ours": 这份配置是不是原委自己写的 yuanwei.toml, "waiting": 封了没整理的步数或 None, "text"}。"""
    from .config import Config
    config, _cp, _mp, _raw = _load(home)
    cpath = config.get("config_path")
    if not cpath:
        on = True                                                    # 没有配置文件：默认配置，整理开着
    else:
        try:
            on = Config.load(cpath).organize
        except Exception as e:                                       # noqa: BLE001  读不出来要报出来，不当成开着
            raise HS.SetupRefused("organize_config_unreadable", "读不了原委的配置文件（%s）：不知道整理现在是开是关。" % type(e).__name__)
    ours = bool(cpath) and os.path.realpath(cpath) == os.path.realpath(str(Path(home) / "yuanwei" / "yuanwei.toml"))
    waiting = waiting_steps(config.get("db"))
    return {"on": on, "config_path": cpath, "ours": ours, "waiting": waiting, "text": state_text(on, waiting), "db": config.get("db")}


def settings(home, action="status", apply=False):
    """status 看；on／off 改。改默认只列计划；加 apply（--yes）才写。已运行的 Hermes 不会热加载：返回 restart_hermes 提醒用户自行重启。"""
    if action not in ACTIONS:
        raise HS.SetupRefused("bad_organize_action", "操作只能是 on、off 或 status。")
    st = read_state(home)
    out = {"action": action, "organize": "on" if st["on"] else "off", "text": st["text"], "sealed_steps_waiting": st["waiting"], "config_path": st["config_path"], "applied": False,
           "changed": False, "would_change": False}
    if action == "status":
        return out
    want_on = action == "on"
    out["would_change"] = st["on"] != want_on
    if not out["would_change"]:
        out["note"] = "整理本来就是%s的，没有要改的。" % ("开" if want_on else "关")
        return out
    if not st["ours"]:
        out["editable"] = False
        out["hint"] = ("你用的是自己的配置文件（%s）：原委不改它。要%s整理，请你自己%s，然后重启 Hermes。"
                       % (st["config_path"], "打开" if want_on else "关掉", "把顶层的 `organize = false` 这一行删掉（或改成 organize = true）" if want_on else "在它顶层（第一个 [表] 之前）加一行 `organize = false`"))
        return out
    directory = Path(home) / "yuanwei"
    cp, mp = directory / "config.json", directory / "install.json"
    tp = directory / "yuanwei.toml"
    try:
        old_toml = tp.read_bytes()
        old_manifest = mp.read_bytes()
        manifest = json.loads(old_manifest)
    except (OSError, ValueError):
        raise HS.SetupRefused("organize_install_unreadable", "没有可读的原委配置或安装记录；没有改设置。")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise HS.SetupRefused("organize_install_unreadable", "原委安装记录格式不对；没有改设置。")
    if manifest["files"].get("yuanwei/yuanwei.toml") != HS._sha(old_toml):
        raise HS.SetupRefused("config_modified", "原委 yuanwei.toml 被改过或没有安装指纹：不覆盖，请先核对设置。")
    try:
        new_toml = edit_toml(old_toml.decode("utf-8"), off=not want_on).encode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise HS.SetupRefused("organize_toml_unsafe", "没能安全地改 yuanwei.toml（%s）；没有改设置。" % type(e).__name__)
    out["restart_hermes"] = bool(apply)
    if not apply:
        out["hint"] = "加 --yes 才会真的改"
        return out
    manifest["files"]["yuanwei/yuanwei.toml"] = HS._sha(new_toml)
    new_manifest = (json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    mode_t, mode_m = stat.S_IMODE(tp.stat().st_mode), stat.S_IMODE(mp.stat().st_mode)
    ts = time.strftime("%Y%m%dT%H%M%S")
    backups = [HS._unique_backup(p, ts) for p in (tp, mp)]
    try:
        if tp.read_bytes() != old_toml or mp.read_bytes() != old_manifest:
            raise HS.SetupRefused("organize_settings_changed", "设置在这次操作中发生变化，请重试。")
        for src, backup in zip((tp, mp), backups):
            HS.copy_with_same_mode(src, backup)
        _atomic(tp, new_toml, mode_t)
        try:
            _atomic(mp, new_manifest, mode_m)
        except OSError:
            try:
                _atomic(tp, old_toml, mode_t)
            except OSError as rollback_error:
                raise HS.SetupRefused("organize_settings_rollback_failed",
                                      "安装指纹写入失败，而且设置回滚没有完成。请先从备份恢复 yuanwei.toml 和 install.json，再重试；备份分别是 %s、%s。"
                                      % tuple(str(p) for p in backups)) from rollback_error
            raise
    except OSError as exc:
        raise HS.SetupRefused("organize_settings_write_failed", "整理开关写入失败；原文件备份保留在 yuanwei 目录（%s）。" % type(exc).__name__) from exc
    out.update(applied=True, changed=True, organize="on" if want_on else "off", backups=[str(p) for p in backups],
               text=state_text(want_on, st["waiting"]))
    return out
