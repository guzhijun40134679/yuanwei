"""本人账号的只读诊断：不打开记忆库，不查密钥，不输出账号编号。"""
import json
import shlex
from pathlib import Path

from . import hermes_platforms as platforms
from . import hermes_profile as profile
from . import owners as owner_settings


def status(home, owners=None, env=None):
    notes = []
    if owners is None:
        owners = {}
        try:
            cfg = json.loads((Path(home) / "yuanwei/config.json").read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and profile.home_check(cfg, home)["ok"]:
                owners = cfg.get("owners") or {}
            else:
                notes.append("本人账号设置归属没有确认，不能用于这个 Hermes。")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            notes.append("本人账号设置读不了，未确认登记情况。")
    if not isinstance(owners, dict) or any(not isinstance(ids, list) for ids in owners.values()):
        notes.append("本人账号设置格式不对，未确认登记情况。")
        owners = {}
    found = platforms.discover(home, env=env)
    notes.extend(found["notes"])
    counter = owner_settings.count_status(home)
    counts = counter["counts"]
    if counter["problem"]:
        notes.append("未登记消息计数读不了（%s），不能把它当作没有漏记；原计数文件没有改动。" % counter["problem"])
    rows = {r["platform"]: dict(r) for r in found["platforms"]}
    for p in sorted(set(owners) | set(counts)):
        rows.setdefault(p, {"platform": p, "name": platforms.label(p), "source": "registered_or_seen", "hint": ""})
    missing = []
    for p, row in rows.items():
        row["registered"] = len(owners.get(p) or [])
        row["unregistered_skipped"] = counts.get(p, 0)
        detected = row["source"] != "registered_or_seen"
        row["detected"] = detected
        if not row["registered"]:
            row["message"] = "%s：%s还没登记本人账号，这个平台的话不会记；以后用 yuanwei owners set %s --id 本人编号 --hermes-home %s --yes 补登记。" % (row["name"], "检测到平台配置，但" if detected else "", p, shlex.quote(str(home)))
            if detected:
                missing.append(p)
            notes.append(row["message"])
        else:
            row["message"] = "%s：已登记 %d 个本人账号，只记本人私聊；改过登记后请重启 Hermes。" % (row["name"], row["registered"])
        if counts.get(p):
            row["skipped_message"] = "%s上有 %d 条未登记为本人的消息没记（只留平台名和计数）。" % (row["name"], counts[p])
            notes.append(row["skipped_message"])
    return {"platforms": [rows[p] for p in sorted(rows)], "missing": missing, "skipped": counts, "counter_problem": counter["problem"], "notes": notes}
