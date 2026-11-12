"""yuanwei setup hermes／uninstall hermes（施工单第 6 节＋乙补充 §4 卸载顺序）。

* 先列计划（改哪几个文件、加哪几行），不带 --yes 什么都不改；改之前备份 config.yaml；只管自己加的那几项：
  $HERMES_HOME/plugins/yuanwei/{__init__.py,plugin.yaml}、$HERMES_HOME/yuanwei/{config.json,install.json}、config.yaml 里 memory.provider 这一行。
* Hermes 一次只能有一个外部记忆提供者：原来设了别家，要显式 --replace-provider 才换，原值记在 install.json，卸载时还原（用户后来又改了就不动）。
* 同名插件目录不是我们装的、或我们装的文件被用户改过：拒绝覆盖，卸载也不删。
* 卸载顺序：① 先把插件文件换成空壳（还没重启的 Hermes 不报错）② 撤掉 config.yaml 里自己的那一行 ③ 确认 Hermes 已经重新加载（--host-reloaded）以后才删文件。
  不碰别的接头，也不碰 Hermes 全局的信任记录。装的时候提醒用户重启 Hermes。
* config.yaml 用逐行文本编辑（基础包只用标准库，没有 YAML 库）：只认顶层 memory: 块和块里的 provider: 行；别的行一个字节都不动。
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

PLUGIN_REL = "plugins/yuanwei"
MEMORY_PROVIDER_KEY = "memory.provider"                    # Hermes 设置里选外部记忆提供者的键（hermes_compat 的 provider_activation 核它）
FILES = {"plugins/yuanwei/__init__.py": "init", "plugins/yuanwei/plugin.yaml": "yaml", "yuanwei/config.json": "config"}
SENTINELS = {"", "default", "builtin", "built-in", "none", "null", "~"}
RES = Path(__file__).parent / "resources"


class SetupRefused(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def plugin_init_text():
    return (RES / "hermes_plugin_init.py.txt").read_text(encoding="utf-8")


def neutral_text():
    return (RES / "hermes_plugin_neutral.py.txt").read_text(encoding="utf-8")


def plugin_yaml_text():
    return (RES / "hermes_plugin.yaml").read_text(encoding="utf-8")


def _sha(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode("utf-8")).hexdigest()


def config_json_text(opts):
    d = {"hermes_home": opts.get("hermes_home"), "db": opts["db"], "read_scope": opts["read_scope"], "owners": opts["owners"], "mode": opts["mode"], "spawn": opts.get("spawn") or "subprocess", "src_path": opts.get("src_path"), "config_path": opts.get("config_path")}
    return json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


# ---------------------------------------------------------------- config.yaml 的逐行编辑
_MEM = re.compile(r"^memory:\s*(#.*)?$")
_PROV = re.compile(r"^(\s+)provider:\s*(.*?)\s*(#.*)?$")


def _block(lines):
    """顶层 memory: 块 → (起始行号, 结束行号（不含）) 或 None。块里到下一个顶层（零缩进、非注释、非空）的行为止。"""
    for i, ln in enumerate(lines):
        if _MEM.match(ln.rstrip("\n")):
            j = i + 1
            while j < len(lines):
                s = lines[j].rstrip("\n")
                if s.strip() and not s.startswith((" ", "\t", "#")):
                    break
                j += 1
            return i, j
    return None


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _provider_line(lines, blk):
    i, j = blk
    for k in range(i + 1, j):
        m = _PROV.match(lines[k].rstrip("\n"))
        if m:
            return k, m.group(1), _unquote(m.group(2))
    return None


def _child_indent(lines, blk):
    i, j = blk
    for k in range(i + 1, j):
        s = lines[k].rstrip("\n")
        if s.strip() and not s.lstrip().startswith("#"):
            return s[: len(s) - len(s.lstrip())]
    return "  "


def _edit_for_install(text, replace_provider):
    """返回 (新文本, 描述)。描述里 replace／insert／append 说明这一步做了什么；已经是 yuanwei 就是 noop。"""
    lines = text.splitlines(keepends=True)
    blk = _block(lines)
    if blk is None:
        add = ("" if (not text or text.endswith("\n")) else "\n") + "memory:\n  provider: yuanwei\n"
        return text + add, {"kind": "append_block", "insert": "memory:\n  provider: yuanwei", "replace": None, "previous": None, "block_inserted": True, "line_inserted": True}
    pl = _provider_line(lines, blk)
    if pl is None:
        ind = _child_indent(lines, blk)
        new = lines[: blk[0] + 1] + ["%sprovider: yuanwei\n" % ind] + lines[blk[0] + 1:]
        return "".join(new), {"kind": "insert_line", "insert": "%sprovider: yuanwei" % ind, "replace": None, "previous": None, "block_inserted": False, "line_inserted": True}
    k, ind, val = pl
    if val == "yuanwei":
        return text, {"kind": "noop", "insert": None, "replace": None, "previous": None, "block_inserted": False, "line_inserted": False}
    if val.lower() not in SENTINELS and not replace_provider:
        raise SetupRefused("other_provider_set", "memory.provider 已经是 %r：Hermes 一次只用一个外部记忆提供者。要换，加 --replace-provider（原值会记下，卸载时还原）" % val)
    old = lines[k].rstrip("\n")
    lines[k] = "%sprovider: yuanwei\n" % ind
    return "".join(lines), {"kind": "replace_line", "insert": None, "replace": [old, "%sprovider: yuanwei" % ind], "previous": val, "block_inserted": False, "line_inserted": False}


def _edit_for_uninstall(text, previous, line_inserted, block_inserted):
    """撤掉自己的那一行。返回 (新文本, 状态)。当前值不是 yuanwei（用户后来改过）就不动。"""
    lines = text.splitlines(keepends=True)
    blk = _block(lines)
    if blk is None:
        return text, "absent"
    pl = _provider_line(lines, blk)
    if pl is None:
        return text, "absent"
    k, ind, val = pl
    if val != "yuanwei":
        return text, "kept_user_changed"
    if line_inserted:
        del lines[k]
        blk2 = _block(lines)
        if block_inserted and blk2 is not None and all(not l.strip() for l in lines[blk2[0] + 1: blk2[1]]):
            del lines[blk2[0]: blk2[1]]
        return "".join(lines), "removed_line"
    lines[k] = "%sprovider: %s\n" % (ind, previous)
    return "".join(lines), "restored_previous"


# ---------------------------------------------------------------- Hermes 自己的配置命令（有就用，没有才逐行改）
def _run(cmd, home, *args, timeout=30):
    env = dict(os.environ, HERMES_HOME=str(home))
    return subprocess.run([cmd] + list(args), capture_output=True, text=True, timeout=timeout, env=env)


def _cli_supports_config_set(cmd):
    if not cmd:
        return False
    try:
        r = subprocess.run([cmd, "config", "set", "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "usage" in (r.stdout + r.stderr).lower()


def _last_value(out):
    for ln in reversed(out.splitlines()):
        ln = ln.strip()
        if ln and not ln.startswith("⚠"):
            return _unquote(ln)
    return ""


def _cli_get(cmd, home):
    r = _run(cmd, home, "config", "get", MEMORY_PROVIDER_KEY)
    return _last_value(r.stdout) if r.returncode == 0 else ""


def _cli_config_path(cmd, home):
    try:
        r = _run(cmd, home, "config", "path")
        v = _last_value(r.stdout) if r.returncode == 0 else ""
        return Path(v) if v else Path(home) / "config.yaml"
    except (OSError, subprocess.SubprocessError):
        return Path(home) / "config.yaml"


def _format_version(text):
    m = re.search(r"^_config_version:\s*(\d+)", text or "", re.M)
    return int(m.group(1)) if m else None


def _choose_method(config_method, hermes_cmd):
    if config_method not in ("auto", "hermes", "yaml"):
        raise SetupRefused("bad_config_method", "config_method 只能是 auto、hermes、yaml")
    if config_method == "yaml":
        return "yaml_edit"
    ok = _cli_supports_config_set(hermes_cmd)
    if config_method == "hermes" and not ok:
        raise SetupRefused("hermes_cli_unavailable", "找不到能用的 hermes 命令（hermes config set）")
    return "hermes_cli" if ok else "yaml_edit"


# ---------------------------------------------------------------- 安装
def _paths(home):
    h = Path(home)
    return h, h / "config.yaml", h / "yuanwei" / "install.json"


def _manifest(home):
    _, _, mp = _paths(home)
    if not mp.exists():
        return None
    return json.loads(mp.read_text(encoding="utf-8"))


def _contents(opts):
    out = {"plugins/yuanwei/__init__.py": plugin_init_text(), "plugins/yuanwei/plugin.yaml": plugin_yaml_text(), "yuanwei/config.json": config_json_text(opts)}
    if opts.get("toml_text"):                                       # 向导写的配置（供应商、向量）：也是我们装的文件，卸载时按「没被改过才删」处理
        out["yuanwei/yuanwei.toml"] = opts["toml_text"]
    return out


def copied_info(h):
    """这个家目录里已有的原委设置是不是复制来的（别的家目录的）：是就返回 home_check 的结果，不是或没有设置返回 None。"""
    from . import hermes_profile as HP
    try:
        cfg = json.loads((Path(h) / "yuanwei" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    r = HP.home_check(cfg, str(h))
    return r if (not r["ok"] and r["state"] == "copied") else None                    # 只有「能认出是别处的」才算；归属没法确认的（ownership_unconfirmed）不当成复制来的


def unconfirmed_info(h):
    """已有的原委设置没记归属、库又在自定义位置、路径证明不了属于这个家目录：返回 {"db": 它指着的库, ...}；不是这种情形返回 None。"""
    from . import hermes_profile as HP
    try:
        cfg = json.loads((Path(h) / "yuanwei" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    r = HP.home_check(cfg, str(h))
    return dict(r, db=cfg.get("db")) if (not r["ok"] and r["state"] == HP.UNCONFIRMED) else None


def _unconfirmed_state(hermes_home, h, db, db_is_default):
    info = unconfirmed_info(h)
    if not info:
        return None
    t = "这个目录里已有的原委设置没有记录它属于哪个家目录，库又在自定义位置（%s），原委没法确认它属于这里，所以插件现在没有启用它。" % (info["db"] or "不明")
    if db_is_default:
        t += "这次装会登记当前家目录，并改用这个目录自己的新库（%s）；旧库不会被动。要继续用旧库，请加 --db %s 再装。" % (db, info["db"] or "<库文件>")
    else:
        t += "这次装用你用 --db 指定的库（%s），并登记当前家目录；装好以后才启用。" % db
    return t


def _copied_state(hermes_home, h, db):
    info = copied_info(h)
    if not info:
        return None
    return "这个目录里已有的原委设置是从别的 Hermes（%s）复制来的，指着那边的库；装好以后会改成这个目录自己的设置和自己的库，复制来的旧设置会先备份。" % (info["recorded_home"] or "不明")


def _source_label(recorded_home):
    """复制来源的短名（进备份文件名）：家目录的目录名去掉开头的点（~/.hermes → hermes，profiles/writer → writer）；只留字母数字和 -_。"""
    import re
    base = os.path.basename(str(recorded_home or "").rstrip("/\\")).lstrip(".")
    return re.sub(r"[^A-Za-z0-9_-]", "", base) or "unknown"


def copied_library_files(db):
    """库文件和它的附属：同一个目录里，名字等于库名、或以「库名-」「库名.」开头的（-wal、-shm、.d 待补目录、.cost-pending.jsonl 等）。已经是备份名（含 .copied-from-）的不算。
    **不以库文件本身存在为前提**：只剩待补目录（`.d`，里面有原话）时，新库一打开也会把它补进去（memory.py 的 flush_pending_detail），所以也要挪走。
    目录不存在＝没有遗留（返回空）；别的读目录失败（权限、IO 错）**不能当成空**：抛 SetupRefused，拒绝继续。"""
    d, base = os.path.dirname(db), os.path.basename(db)
    try:
        return sorted(n for n in os.listdir(d) if (n == base or n.startswith(base + "-") or n.startswith(base + ".")) and ".copied-from-" not in n)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        raise SetupRefused("copied_library_unreadable", "读不了 %s（%s）：没法确认里面有没有从别处复制来的记忆库遗留，所以没有改任何设置。请先处理好这个目录的权限，再重新装。" % (d, type(e).__name__))


def _copied_library_action(h, db, db_is_default):
    """机器人目录里有一份从别处复制来的库，而这份设置是复制来的：计划里写明「不沿用，挪到备份名」。用户自己用 --db 指定的库不动。"""
    info = copied_info(h)
    if not info or not db_is_default:
        return None
    files = copied_library_files(db)
    if not files:
        return None
    src = _source_label(info["recorded_home"])
    return {"kind": "move_copied_library", "dir": os.path.dirname(db), "files": files, "source": src, "backup_pattern": "<原名>.copied-from-%s-<时间>" % src, "main_present": os.path.basename(db) in files}


def _move_copied_library(a, ts):
    """先挪库文件本身，再挪附属文件；rename 同目录内完成，权限、内容照旧，不删。任何一个挪不动：已挪的挪回去，整个拒绝。返回 [(原名, 备份名)]。
    挪回去也失败的：如实说「没有回滚完」，并写出原名和现在的备份名（东西在哪里）。"""
    done = []
    try:
        for n in a["files"]:
            src = os.path.join(a["dir"], n)
            if not os.path.lexists(src):
                continue
            dst = "%s.copied-from-%s-%s" % (src, a["source"], ts)
            k = 1
            while os.path.lexists(dst):
                dst = "%s.copied-from-%s-%s-%d" % (src, a["source"], ts, k)
                k += 1
            os.rename(src, dst)
            done.append((src, dst))
    except OSError as e:
        stuck = []
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError as e2:
                stuck.append((src, dst, type(e2).__name__))
        if stuck:
            raise SetupRefused("copied_library_move_failed_rollback_incomplete",
                               "没能把复制来的记忆库全部挪开（%s），而且没有回滚完：下面这些文件现在在备份名下，没有挪回原处（原名 → 现在的位置，回滚失败的原因）：%s。"
                               "设置没有改。请你自己把它们挪回原名（或另做处理），再重新装。" % (type(e).__name__, "；".join("%s → %s（%s）" % t for t in stuck)))
        raise SetupRefused("copied_library_move_failed", "没能把复制来的记忆库挪开（%s）：已经挪的都挪回去了，什么都没改。" % type(e).__name__)
    return done


def plan_install(hermes_home, db=None, mode="tools_only", src_path=None, replace_provider=False, config_path=None, spawn=None, hermes_cmd=None, config_method="auto", read_scope=None, owners=None, toml_text=None):
    from . import hermes_workspace as HW
    read_scope = read_scope or HW.DEFAULT_SCOPE                                  # 不指定：乙（生活能翻全部，项目只翻自己）
    if read_scope not in HW.SCOPES:
        raise SetupRefused("bad_read_scope", "读取范围只能是 %s（甲 isolated＝全隔开、乙 life_all＝生活翻全部项目翻自己、丙 all_open＝全打通），不是 %r" % ("、".join(HW.SCOPES), read_scope))
    owners = {str(k): [str(x) for x in v] for k, v in (owners or {}).items()}
    db_is_default = not db
    db = db or str(Path(hermes_home) / "yuanwei" / "yuanwei.sqlite")              # 库跟着这个分身的 hermes_home 走；工作区是库里的一个分区
    from . import hermes_profile as HP
    opts = dict(hermes_home=HP.norm(hermes_home), db=db, mode=mode, src_path=src_path, config_path=config_path, spawn=spawn, read_scope=read_scope, owners=owners, toml_text=toml_text)   # 记下「这份设置属于哪个家目录」：复制到别的档案以后插件认得出来
    method = _choose_method(config_method, hermes_cmd)
    h, cfgp, _ = _paths(hermes_home)
    man = _manifest(hermes_home)
    want = _contents(opts)
    plugin_dir = h / PLUGIN_REL
    if plugin_dir.exists() and man is None:
        raise SetupRefused("plugin_dir_not_ours", "%s 已经存在、不是原委装的：不覆盖" % plugin_dir)
    actions, notes, unchanged = [], [], []
    for rel, text in want.items():
        p = h / rel
        cur = p.read_bytes() if p.exists() else None
        if man is not None and cur is not None:
            recorded = man["files"].get(rel)
            if recorded and _sha(cur) != recorded and _sha(cur) != _sha(text) and rel not in ("yuanwei/config.json", "yuanwei/yuanwei.toml"):
                raise SetupRefused("plugin_file_modified", "%s 被改过（和装的时候的指纹不同）：不覆盖，也不会在卸载时删" % p)
            if recorded and _sha(cur) != recorded and rel in ("yuanwei/config.json", "yuanwei/yuanwei.toml") and _sha(cur) != _sha(text):
                raise SetupRefused("config_modified", "%s 被改过：不覆盖" % p)
        if cur is not None and _sha(cur) == _sha(text):
            unchanged.append(str(p))
            continue
        act = {"kind": "write_file", "path": str(p), "sha256": _sha(text), "new": cur is None}
        if cur is not None:
            act["backup"] = "%s.yuanwei-backup-<时间>" % p                    # 已有、内容要换：先备份再换（权限和原文件一样）
        actions.append(act)
    backups = []
    if method == "hermes_cli":
        cfgp = _cli_config_path(hermes_cmd, hermes_home)
        text = cfgp.read_text(encoding="utf-8") if cfgp.exists() else ""
        val = _cli_get(hermes_cmd, hermes_home)
        if val == "yuanwei":
            new_text, edit = text, {"kind": "noop", "insert": None, "replace": None, "previous": None, "block_inserted": False, "line_inserted": False}
        else:
            if val.lower() not in SENTINELS and not replace_provider:
                raise SetupRefused("other_provider_set", "memory.provider 已经是 %r：Hermes 一次只用一个外部记忆提供者。要换，加 --replace-provider（原值会记下，卸载时还原）" % val)
            new_text = text
            edit = {"kind": "cli_set", "insert": None, "replace": None, "previous": (val or None) if val.lower() not in SENTINELS else None, "block_inserted": False, "line_inserted": False}
            actions.append({"kind": "hermes_config_set", "command": [hermes_cmd, "config", "set", MEMORY_PROVIDER_KEY, "yuanwei"], "env": {"HERMES_HOME": str(hermes_home)}, "previous": val or None})
            if cfgp.exists():
                backups.append(str(cfgp) + ".yuanwei-backup-<时间>")
    else:
        text = cfgp.read_text(encoding="utf-8") if cfgp.exists() else ""
        new_text, edit = _edit_for_install(text, replace_provider)
        if edit["kind"] != "noop":
            actions.append({"kind": "edit_config", "path": str(cfgp), **{k: edit[k] for k in ("insert", "replace")}})
            if cfgp.exists():
                backups.append(str(cfgp) + ".yuanwei-backup-<时间>")
    notes = ["装好以后要重启 Hermes，新的记忆提供者才会加载。", "只改 config.yaml 里 memory.provider 这一行；原来的值记在 install.json，卸载时还原。", "不改 CLAUDE.md、AGENTS.md、SOUL.md 一类常驻说明文件；常驻规矩由插件的 system_prompt_block 提供（默认开，config.json 的 resident_rule 可关）。"]
    pname = HP.profile_name(hermes_home)
    if pname:
        notes.insert(0, "这是给机器人 %s 装：它会有自己单独的一个库（%s），和主 Hermes、别的机器人互不相干。这一版只管你和这个机器人的一对一，群聊和机器人之间互发的消息不记也不查。" % (pname, db))
    copied = _copied_state(hermes_home, h, db)
    unconfirmed = _unconfirmed_state(hermes_home, h, db, db_is_default)
    if unconfirmed:
        notes.insert(0, unconfirmed)
    lib = _copied_library_action(h, db, db_is_default)
    if lib:
        actions.insert(0, lib)
        notes.insert(0, "这个机器人目录里有一份从别处复制来的记忆库（%s%s，里面可能有主 Hermes 的对话），**不沿用**：确认后把它连同附属文件（%s）挪到同目录的 <原名>.copied-from-%s-<时间> 备份名（不删、权限照旧），这个机器人从空库开始；不要的话你自己删。" % (db, "" if lib["main_present"] else "；只剩待补文件等遗留，没有库文件本身，新库一打开也会把待补的原话补进去，所以同样挪走", "、".join(lib["files"]), lib["source"]))
    if copied:
        notes.insert(0, copied)
    if method == "hermes_cli":
        notes.append("设置用 Hermes 自己的命令改（hermes config set %s yuanwei），不直接改它的 YAML。" % MEMORY_PROVIDER_KEY)
    else:
        notes.append("没有找到能用的 hermes config set 命令，改用逐行编辑 config.yaml（只动 memory.provider 这一行）。")
    return {"profile": pname, "copied_from": (copied_info(h) or {}).get("recorded_home") if copied else None, "actions": actions, "unchanged": unchanged, "backups": backups, "notes": notes, "config_method": method, "config_format_version": _format_version(text), "_edit": edit, "_new_text": new_text, "_want": want, "_man": man, "_cfgp": cfgp, "_cmd": hermes_cmd}


def apply_install(**opts):
    plan = plan_install(**opts)
    h, _cfgp0, mp = _paths(opts["hermes_home"])
    cfgp = plan["_cfgp"] if plan["config_method"] == "hermes_cli" else _cfgp0
    if not plan["actions"]:
        return {"applied": True, "changed": False, "plan": _public(plan)}
    man = plan["_man"] or {"version": 1, "previous_provider": None, "provider_line_inserted": False, "memory_block_inserted": False, "files": {}, "config_backup": None}
    man["config_method"], man["config_format_version"] = plan["config_method"], plan["config_format_version"]
    ts = time.strftime("%Y%m%dT%H%M%S")
    for a in plan["actions"]:
        if a["kind"] == "move_copied_library":                                  # 用户确认（--yes／向导里确认计划）以后才到这里；最先做：挪不动就整个拒绝，别的什么都还没改
            a["moved"] = [[x, y] for x, y in _move_copied_library(a, ts)]
    edit = plan["_edit"]
    if edit["kind"] != "noop":
        if cfgp.exists():
            b = _unique_backup(cfgp, time.strftime("%Y%m%dT%H%M%S"))
            copy_with_same_mode(cfgp, b)
            man["config_backup"] = b.name
        man["previous_provider"] = edit["previous"]
        man["provider_line_inserted"] = edit["line_inserted"]
        man["memory_block_inserted"] = edit["block_inserted"]
        if edit["kind"] == "cli_set":
            r = _run(plan["_cmd"], opts["hermes_home"], "config", "set", MEMORY_PROVIDER_KEY, "yuanwei")
            if r.returncode != 0 or _cli_get(plan["_cmd"], opts["hermes_home"]) != "yuanwei":
                raise SetupRefused("hermes_config_set_failed", "hermes config set 没有成功（备份在 %s）：%s" % (man["config_backup"], (r.stderr or r.stdout)[-200:]))
        else:
            cfgp.parent.mkdir(parents=True, exist_ok=True)
            cfgp.write_text(plan["_new_text"], encoding="utf-8")
    for a in plan["actions"]:
        if a["kind"] == "write_file":
            p = Path(a["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            rel = os.path.relpath(a["path"], h)
            if p.exists() and not a.get("new"):
                b = _unique_backup(p, time.strftime("%Y%m%dT%H%M%S"))
                copy_with_same_mode(p, b)
                a["backup_made"] = b.name
            p.write_text(plan["_want"][rel], encoding="utf-8")
    for rel, text in plan["_want"].items():
        man["files"][rel] = _sha(text)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"applied": True, "changed": True, "plan": _public(plan), "restart_hermes": True}


def _unique_backup(path, ts):
    b = Path("%s.yuanwei-backup-%s" % (path, ts))
    n = 1
    while b.exists():
        b = Path("%s.yuanwei-backup-%s-%d" % (path, ts, n))
        n += 1
    return b


def copy_with_same_mode(src, dst):
    """备份：内容照抄，权限和原文件一样（config.yaml 是 0600 的话备份也是 0600）。先用 0600 建出来再写，没有「先公开、后收紧」的空档；原委写的备份一律走这里。"""
    import stat
    mode = stat.S_IMODE(os.stat(src).st_mode)
    data = Path(src).read_bytes()
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(dst))
        raise
    os.chmod(str(dst), mode)


def _public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}


# ---------------------------------------------------------------- 卸载
def apply_uninstall(hermes_home, host_reloaded=False, dry_run=False, hermes_cmd=None):
    h, cfgp, mp = _paths(hermes_home)
    man = _manifest(hermes_home)
    steps, changed, pending = [], False, []
    if man is None:
        return {"changed": False, "steps": steps, "pending": pending, "note": "没有找到原委的安装记录（install.json）：什么都没动"}
    files = man["files"]
    ini = h / "plugins/yuanwei/__init__.py"
    # ① 空壳
    if ini.exists():
        cur = _sha(ini.read_bytes())
        if cur == _sha(neutral_text()):
            steps.append({"step": "neutral_shell", "status": "already"})
        elif cur != files.get("plugins/yuanwei/__init__.py"):
            steps.append({"step": "neutral_shell", "status": "kept_modified"})
        else:
            if not dry_run:
                ini.write_text(neutral_text(), encoding="utf-8")
                files["plugins/yuanwei/__init__.py"] = _sha(neutral_text())
            steps.append({"step": "neutral_shell", "status": "would_replace" if dry_run else "replaced"})
            changed = True
    else:
        steps.append({"step": "neutral_shell", "status": "absent"})
    # ② 配置
    via_cli = man.get("config_method") == "hermes_cli" and hermes_cmd and _cli_supports_config_set(hermes_cmd)
    if via_cli and not man.get("reverted"):
        cur = _cli_get(hermes_cmd, hermes_home)
        if cur != "yuanwei":
            st = "absent" if cur == "" and man["previous_provider"] is None else "kept_user_changed"
        elif dry_run:
            st = "would_restore_previous" if man["previous_provider"] else "would_unset"
        else:
            r = _run(hermes_cmd, hermes_home, "config", *(["set", MEMORY_PROVIDER_KEY, man["previous_provider"]] if man["previous_provider"] else ["unset", MEMORY_PROVIDER_KEY]))
            st = ("restored_previous" if man["previous_provider"] else "removed_line") if r.returncode == 0 else "failed"
        steps.append({"step": "revert_config", "status": st})
        if st in ("restored_previous", "removed_line"):
            changed = True
            man["previous_provider"], man["provider_line_inserted"], man["memory_block_inserted"], man["reverted"] = None, False, False, True
    elif cfgp.exists() and not man.get("reverted") and (man["previous_provider"] is not None or man["provider_line_inserted"] or man.get("config_method") == "hermes_cli"):
        line_ins = man["provider_line_inserted"] or (man.get("config_method") == "hermes_cli" and man["previous_provider"] is None)
        new_text, st = _edit_for_uninstall(cfgp.read_text(encoding="utf-8"), man["previous_provider"], line_ins, man["memory_block_inserted"])
        if st in ("removed_line", "restored_previous") and not dry_run:
            cfgp.write_text(new_text, encoding="utf-8")
        steps.append({"step": "revert_config", "status": st})
        changed = changed or st in ("removed_line", "restored_previous")
        if st in ("removed_line", "restored_previous") and not dry_run:
            man["previous_provider"], man["provider_line_inserted"], man["memory_block_inserted"], man["reverted"] = None, False, False, True
    else:
        steps.append({"step": "revert_config", "status": "nothing_to_revert"})
    # ③ 删文件（要用户确认 Hermes 已经重新加载）
    left = []
    for rel in list(files):
        p = h / rel
        if not p.exists():
            continue
        left.append(rel)
    if not host_reloaded:
        if left:
            pending.append("delete_files_after_host_reload")
        if not dry_run:
            mp.write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return {"changed": changed, "steps": steps, "pending": pending}
    kept = False
    for rel in left:
        p = h / rel
        ok = _sha(p.read_bytes()) in (files.get(rel), _sha(neutral_text()))
        if not ok:
            steps.append({"step": "delete:" + rel, "status": "kept_modified"})
            kept = True
            continue
        if not dry_run:
            p.unlink()
        steps.append({"step": "delete:" + rel, "status": "would_delete" if dry_run else "deleted"})
        changed = True
    st_file = h / "yuanwei" / "status.json"                                              # 运行时留下的数（只有数和原因）：是我们写的，删；记忆库、工作区名字不是装的东西，不动
    if st_file.exists() and not kept:
        if not dry_run:
            st_file.unlink()
        steps.append({"step": "delete:yuanwei/status.json", "status": "would_delete" if dry_run else "deleted"})
        changed = True
    if not dry_run and not kept:
        for d in (h / "plugins/yuanwei", h / "yuanwei"):
            try:
                if d == h / "yuanwei" and mp.exists():
                    mp.unlink()
                    changed = True
                d.rmdir()
            except OSError:
                pass
    elif not dry_run:
        mp.write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"changed": changed, "steps": steps, "pending": pending}


def uninstall_plan_text(res, home):
    """`uninstall hermes` 不加 --yes 时打印给人看的计划（res 是 apply_uninstall 的 dry_run 结果，加了 engine 等）。最后一行写明什么都没改。"""
    h = Path(str(home))
    L = ["卸载原委（Hermes 家目录 %s）——计划：" % h]
    if res.get("note"):
        L.append("  " + res["note"])
    say = {
        ("neutral_shell", "would_replace"): "会把插件文件 plugins/yuanwei/__init__.py 换成一个什么都不做的空壳（Hermes 重启以后就不再加载原委）。",
        ("neutral_shell", "already"): "插件文件已经是空壳，不用换。",
        ("neutral_shell", "kept_modified"): "插件文件 plugins/yuanwei/__init__.py 被你改过：不动它。",
        ("neutral_shell", "absent"): "没有找到插件文件，不用换。",
        ("revert_config", "would_unset"): "会撤回设置文件里 memory.provider 这一行（装之前它是空的）。",
        ("revert_config", "would_restore_previous"): "会把设置文件里的 memory.provider 改回装之前的值。",
        ("revert_config", "removed_line"): "会撤回设置文件 config.yaml 里 memory.provider 这一行（装之前它是空的）。",
        ("revert_config", "restored_previous"): "会把设置文件 config.yaml 里的 memory.provider 改回装之前的值。",
        ("revert_config", "kept_user_changed"): "设置文件里的 memory.provider 现在不是原委（你后来改过）：不动它。",
        ("revert_config", "absent"): "设置文件里没有 memory.provider 这一行，不用撤回。",
        ("revert_config", "nothing_to_revert"): "设置文件不用撤回（已经撤回过，或原委没动过它）。",
    }
    for st in res.get("steps") or []:
        k = (st["step"], st["status"])
        if st["step"].startswith("delete:"):
            f = st["step"][7:]
            L.append("  " + {"would_delete": "会删掉我们装的文件：%s。", "kept_modified": "文件 %s 被你改过：不删。"}.get(st["status"], "%s：" + st["status"]) % f)
        elif k in say:
            L.append("  " + say[k])
        else:
            L.append("  %s：%s" % (st["step"], st["status"]))
    if "delete_files_after_host_reload" in (res.get("pending") or []):
        L.append("  我们装的文件（插件文件、yuanwei/config.json、yuanwei.toml、运行状态文件、安装记录）要等 Hermes 重启、不再引用旧插件以后才删：重启后再加 --host-reloaded --yes 运行一次。")
    eng = res.get("engine")
    if eng:
        L.append("  自带向量引擎（%s）：%s" % (eng.get("dir"), eng.get("note") or "会解除这个安装对它的登记"))
    L.append("  不删：你的记忆库（yuanwei/yuanwei.sqlite）和工作区名字、密钥文件（这个机器人自己的 yuanwei/keys/ 和全局的 ~/.yuanwei/keys/）、config.yaml 的备份和重装时留下的文件备份（*.yuanwei-backup-*）。")
    L.append("这是计划，什么都没改；加 --yes 才执行。")
    return "\n".join(L)


# ---------------------------------------------------------------- 看状态（只读）
def _current_provider(h):
    cfgp = Path(h) / "config.yaml"
    if not cfgp.exists():
        return None
    lines = cfgp.read_text(encoding="utf-8").splitlines(keepends=True)
    blk = _block(lines)
    pl = _provider_line(lines, blk) if blk else None
    return pl[2] if pl else None


def cost_status(store, db, organizer_cost=None):
    """今天和累计花了多少（整理调模型的费用账，所有工作区合计），按币种分开、不混加。日期是 UTC 日期（账里就是这样记的）。
    unrecorded_calls：收到过模型回应、可是费用账里没有这一步的行（正常是 0）；pending：因为数据库忙暂存在旁边文件里、还没补进库的行数。"""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    pend = 0
    try:
        with open(db + ".cost-pending.jsonl", encoding="utf-8") as f:
            pend = sum(1 for l in f if l.strip())
    except OSError:
        pass
    total = store.cost_sum_all()
    out = {"day_utc": today, "today": store.cost_sum_all(today), "total": total, "unrecorded_calls": store.unrecorded_calls(), "pending_rows": pend}
    if not total["calls"]:
        out["note"] = "费用账里还没有记录（还没有调过整理用的模型，或者调了但没记上，见 unrecorded_calls）"
    if (organizer_cost or {}).get("price_table") is False:
        out["note"] = "这家没有价格表：只记用量（词元），不估价"
    return out


def _status_extra(out, h, cpath, db, notes):
    """status hermes 的两段：整理供应商（名称、标签、密钥来源、费用口径）和向量。只读：不迁移库、不写任何东西。"""
    from . import catalog as CT
    from . import embedwork as EW
    from . import keystore as KS
    from .config import Config
    from .sqlstore import Store
    cfg = Config.load(cpath)
    p = cfg.provider or {}
    params = p.get("params") or {}
    model = params.get("model")
    pr = CT.price(model) if model and model != "deepseek-flash" else ({"currency": "CNY"} if model in (None, "deepseek-flash") else None)
    row = CT.provider(p.get("id")) if p.get("id") else None
    env = p.get("api_key_env")
    ksrc = KS.source_with_dotenv(env, p.get("key_file"), h) if env else "not_needed"   # 先自己的文件、再全局文件、再环境变量；都没有再查 Hermes 的 .env 里有没有这个名字（只看名字，不读值；Hermes 运行时会自己读它，所以插件里看到的来源是 env）
    if ksrc.startswith("file_unusable"):
        notes.append(KS.UNUSABLE_NOTE)
    out["organizer"] = {"id": p.get("id"), "label": p.get("label") or (row["label"] if row else None), "model": model,
                        "key": {"env": env, "source": ksrc},
                        "cost": {"currency": pr["currency"] if pr else None, "price_table": pr is not None, "note": None if pr else "没有这个模型的价格表：只记用量，不估价；预算上限对它不起作用"}}
    if db and os.path.exists(db):
        st = Store.open_readonly(db)
        try:
            out["cost"] = cost_status(st, db, out["organizer"]["cost"])
        except Exception as e:                                                   # noqa: BLE001  读不出要报出来，不当成没花钱
            out["cost"] = {"error": type(e).__name__}
        finally:
            st.conn.close()
    if cfg.retrieval == "hybrid" and db and os.path.exists(db):
        st = Store.open_readonly(db)
        try:
            out["vectors"] = EW.status(st, db, cfg)
        finally:
            st.conn.close()
    else:
        out["vectors"] = {"enabled": False}


def install_status(hermes_home, sys_=None, hermes_python=None):
    """`yuanwei status hermes`（给了 sys_ 才会去查 Hermes 运行环境里有没有 NumPy：要跑 Hermes 的启动命令和那个 Python，所以只有命令行入口给）：装没装、插件文件有没有被改、memory.provider 现在是谁、读取范围和主人（只报平台和个数）、认得的工作区和各自记了几条、运行时的数。只读。"""
    from . import hermes_workspace as HW
    h, _cfgp, _mp = _paths(hermes_home)
    man = _manifest(hermes_home)
    notes = []
    out = {"installed": man is not None, "hermes_home": str(h), "notes": notes}
    if man is None:
        out["note"] = "没有找到原委的安装记录（install.json）：没装，或者不是用 yuanwei setup hermes 装的"
    files = {}
    for rel, sha in ((man or {}).get("files") or {}).items():
        p = h / rel
        files[rel] = "missing" if not p.exists() else ("ok" if _sha(p.read_bytes()) == sha else "modified")
    out["plugin_files"] = files
    prov = _current_provider(h)
    out["provider_active"], out["provider_is_ours"] = prov, prov == "yuanwei"
    if man is not None and prov != "yuanwei":
        notes.append("memory.provider 现在是 %r，不是原委：原委没有在用（可能是你后来改了，或者还没重启 Hermes）" % prov)
    for rel, st in files.items():
        if st != "ok":
            notes.append("%s %s（和装的时候不一样）：重装前要你自己确认" % (rel, {"modified": "被改过", "missing": "不见了"}[st]))
    cj = h / "yuanwei" / "config.json"
    cfg = {}
    cpath = None
    if cj.exists():
        try:
            c = json.loads(cj.read_text(encoding="utf-8"))
            from . import hermes_profile as HP
            chk = HP.home_check(c, str(h))
            out["profile"] = {"name": HP.profile_name(h), "check": chk["basis"], "recorded_home": chk["recorded_home"], "copied": (not chk["ok"]) and chk["state"] == "copied", "state": chk["state"]}
            if not chk["ok"]:                                                    # 复制来的、或归属没法确认的设置：不读它指着的那个库（可能是别的档案的），插件也不会启用
                if chk["state"] == HP.UNCONFIRMED:
                    out["ownership_unconfirmed"] = True
                    out["profile"]["state"] = HP.UNCONFIRMED
                    notes.append(HP.unconfirmed_message(str(h), c.get("db")))
                else:
                    out["copied_from_other_profile"] = True
                    out["profile"]["state"] = "copied"
                    notes.append(HP.copied_message(str(h)))
                c = {}
            cpath = c.get("config_path")
            cfg = {"read_scope": c.get("read_scope"), "owners": {k: len(v) for k, v in (c.get("owners") or {}).items()}, "mode": c.get("mode"), "db": c.get("db")}
        except (OSError, ValueError):
            notes.append("config.json 读不了")
    out["config"] = cfg
    if cpath:
        try:
            _status_extra(out, h, cpath, cfg.get("db"), notes)
        except Exception as e:                                                   # noqa: BLE001  读不出来要报出来，不当成没问题
            notes.append("供应商／向量段读不出来（%s）" % type(e).__name__)
    ws = []
    counts = {}
    db = cfg.get("db")
    if db and os.path.exists(db):
        import sqlite3
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
            try:
                counts = {r[0]: r[1] for r in c.execute("SELECT ns, COUNT(*) FROM messages GROUP BY ns")}
            finally:
                c.close()
        except sqlite3.Error:
            notes.append("库文件读不了（可能正在写，稍后再看）")
    for k, v in sorted(HW.known(str(h)).items()):
        ws.append({"key": k, "kind": v["kind"], "label": v["label"], "messages": counts.get(k, 0)})
    out["workspaces"] = ws
    rt = h / "yuanwei" / "status.json"
    try:
        out["runtime"] = json.loads(rt.read_text(encoding="utf-8")) if rt.exists() else None
    except (OSError, ValueError):
        out["runtime"] = None
    if out["runtime"] is None:
        notes.append("还没有运行时的数（Hermes 还没加载过插件，或加载后没有对话）")
    if sys_ is not None:
        try:
            from . import embedsetup as ES
            ns = ES.numpy_status(sys_, str(h), explicit=hermes_python)
            ns["needed_by_vectors"] = bool((out.get("vectors") or {}).get("enabled"))
            if not ns["needed_by_vectors"] and ns["has_numpy"] is not True:
                ns["note"] = "（你没有开向量，不影响）" + (ns["note"] or "")
            out["numpy_in_hermes"] = ns
        except Exception as e:                                                   # noqa: BLE001  查不了要报出来，不当成没问题
            out["numpy_in_hermes"] = {"has_numpy": None, "note": "没能查（%s）" % type(e).__name__}
    return out
