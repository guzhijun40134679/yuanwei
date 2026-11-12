"""yuanwei secrets scan：只读，只给数，不打印内容，不改库（库文件、待补文件字节不变）。打不开、解不开、坏行都进 errors，complete=false；没扫到不能算成零。"""
import base64
import json
import os
import sqlite3
from typing import NamedTuple, Optional

from . import contract as WC
from . import redact as RD


class ScanReport(NamedTuple):
    rules_version: int
    complete: bool
    scope: dict
    workspaces: list
    files: list
    errors: list


def _hits(text):
    r = RD.redact(text)
    return r.count, r.kinds


def scan(db_path, workspace: Optional[str] = None, config_dir: Optional[str] = None) -> ScanReport:
    db_path = os.path.abspath(db_path)
    config_dir = config_dir or (db_path + ".d")
    errors, wss, files = [], [], []
    scope = {"db": "ok", "workspaces": [], "files": ["pending", "claim", "review"]}
    nss = []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        c.row_factory = sqlite3.Row
        nss = [r[0] for r in c.execute("SELECT ns FROM workspaces ORDER BY ns")]
    except sqlite3.Error as e:
        scope["db"] = "unreadable"
        errors.append({"source": "db", "code": "E_DB_" + type(e).__name__, "count": 1})
        c = None
    if workspace is not None:
        if workspace not in nss and c is not None:
            errors.append({"source": "workspace", "code": "E_NO_SUCH_WORKSPACE", "count": 1})
        nss = [workspace]
    scope["workspaces"] = list(nss)
    if c is not None:
        for ns in nss:
            scanned = with_hits = 0
            by_kind, bad = {}, 0
            try:
                for r in c.execute("SELECT content FROM messages WHERE ns=?", (ns,)):
                    scanned += 1
                    n, kinds = _hits(r["content"])
                    if n:
                        with_hits += 1
                        for k in kinds:
                            by_kind[k] = by_kind.get(k, 0) + 1
                for r in c.execute("SELECT data FROM org_rows WHERE ns=? AND tbl='sentences'", (ns,)):
                    try:
                        n, kinds = _hits(json.loads(r["data"]).get("text", ""))
                        if n:
                            for k in kinds:
                                by_kind[k] = by_kind.get(k, 0) + 1
                    except Exception:                                            # noqa: BLE001
                        bad += 1
            except sqlite3.Error as e:
                errors.append({"source": "messages:" + ns, "code": "E_DB_" + type(e).__name__, "count": 1})
            if bad:
                errors.append({"source": "sentences:" + ns, "code": "E_BAD_ROW", "count": bad})
            wss.append({"ns": ns, "messages_scanned": scanned, "messages_with_hits": with_hits, "hits_by_kind": by_kind})
        c.close()
    for ns in nss:
        base = os.path.basename(os.path.join(config_dir, "pending-%s.jsonl" % WC.sha256_hex(ns)[:10]))
        try:
            names = sorted(x for x in os.listdir(config_dir) if x.startswith(base) and not x.endswith(".lock"))
        except FileNotFoundError:
            continue
        except OSError:
            errors.append({"source": "files:" + ns, "code": "E_LISTDIR", "count": 1})
            continue
        for name in names:
            kind = "review" if name.endswith(".review") else ("claim" if ".claim-" in name else "pending")
            rec = {"kind": kind, "ns": ns, "lines_scanned": 0, "lines_with_hits": 0, "unparsable": 0}
            try:
                with open(os.path.join(config_dir, name), "rb") as f:
                    lines = [l for l in f.read().split(b"\n") if l.strip()]
            except OSError:
                errors.append({"source": kind, "code": "E_OPEN", "count": 1})
                continue
            for l in lines:
                rec["lines_scanned"] += 1
                try:
                    obj = json.loads(l.decode("utf-8"))
                    if kind == "review":
                        if "line" in obj:
                            obj = json.loads(obj["line"])
                        elif "line_b64" in obj:
                            obj = json.loads(base64.b64decode(obj["line_b64"]).decode("utf-8"))
                        else:
                            raise ValueError("no line")
                    n, _k = _hits(obj["text"])
                    if n:
                        rec["lines_with_hits"] += 1
                except Exception:                                                # noqa: BLE001
                    rec["unparsable"] += 1
            if rec["unparsable"]:
                errors.append({"source": kind, "code": "E_BAD_LINE", "count": rec["unparsable"]})
            files.append(rec)
    return ScanReport(RD.RULES_VERSION, not errors, scope, wss, files, errors)
