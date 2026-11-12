"""命令行：yuanwei。名字照 03 接口草案换成 yuanwei。输出一律是 JSON；出错写 JSON 到 stderr，退出码 2（用法／输入）、3（找不到／还没整理完）、4（暂停）。
remember 手动补记这一版不开放；原话用 record 记（可信的写入入口在命令行和 Python）。"""
import argparse
import json
import os
import sys

from .config import Config
from .errors import ConfigError as ConfigError_, BadCursor, HistoryDeleted, MissingVector, NotFound, NotVisible, Paused, Refused, YuanweiError
from .memory import Memory
from .provider import OpenAICompatProvider
from .storeerr import StoreError


ARGV_SECRET_MESSAGE = "命令行参数里好像有密钥。原委不从命令行收密钥：请用 `yuanwei key set` 交互输入，或用 `--api-key-env 变量名`。"


def argv_has_secret(argv):
    """argv 的每一项都过一遍和正文同一套的密钥过滤；只要有一项会被遮，就是「命令行里有密钥」。"""
    from . import redact as RD
    return any(isinstance(a, str) and RD.redact(a).count for a in argv)


def _argv_guard(argv):
    """有密钥就不进 argparse（它报「不认识的参数」时会把值原样打印出来）：给一句固定的报错，不回显任何参数，退出码 2。没有返回 None。"""
    if argv_has_secret(argv):
        print(json.dumps({"error": "secret_in_argv", "message": ARGV_SECRET_MESSAGE}, ensure_ascii=False), file=sys.stderr)
        return 2
    return None


class SafeParser(argparse.ArgumentParser):
    """第二道防线：argparse 的报错、用法提示在输出前过一遍密钥过滤（报错里常带着用户敲的参数值）。"""

    def error(self, message):
        from . import redact as RD
        sys.stderr.write(RD.redact(self.format_usage()).text)
        sys.stderr.write(RD.redact("%s: error: %s\n" % (self.prog, message)).text)
        sys.exit(2)


USER_HELP = """yuanwei：原委，给 AI 助手用的长期记忆（预览版）

给你自己用的命令（每个后面加 -h 看它自己的说明）：
  setup                 安装向导：一问一答，最后列出计划，你确认了才改东西（不问话的写法：setup --host hermes --preset recommended --yes）
  status hermes         看装得怎么样：装没装、读取范围、各工作区、整理用密钥从哪来、向量、Hermes 里有没有 NumPy、运行以来记了几轮
  key set|status|test|delete        整理用的密钥（输入时不显示；只放在你电脑上只有你能读的文件里）
  vectors setup|status|backfill|prune|serve|stop|remove-engine      本机向量（setup 开或换，status 看进度）
  conversations list|delete|status|resume [hermes]    管对话：看有哪些、删一段（加 --yes 才真的删）；不给 --db 就用 Hermes 接头的库，也可以写成 conversations list hermes
  secrets scan [hermes] 扫一遍库里有没有漏过滤的密钥（只报数，不显示内容）；不给 --db 就用 Hermes 接头的库，也可以写成 secrets scan hermes
  uninstall hermes      卸载（不删你的记忆库和密钥）
  doctor hermes         核对本机 Hermes 的接口有没有和原委预期的不一样

研究和调试用的命令（要先给 --db 库文件 和 --workspace 工作区名）：
  record、end-session、abort-turn、run、resume、recall、get-messages、attachments、read-permission、ask、status、export
  例：yuanwei --db 库文件 --workspace 工作区名 recall "要查的话"
"""


def _provider(cfg):
    p = cfg.provider
    return OpenAICompatProvider(p["base_url"], p.get("api_key_env"), p.get("params"), p.get("timeout", 600), key_file=p.get("key_file"))


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def build_parser():
    ap = SafeParser(prog="yuanwei", description="原委：长期记忆（预览版）", allow_abbrev=False)
    ap.add_argument("--db", required=True, help="SQLite 文件")
    ap.add_argument("--workspace", required=True, help="工作区名")
    ap.add_argument("--config", help="TOML 配置文件")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="记一条原话")
    r.add_argument("--host", required=True)
    r.add_argument("--session", required=True)
    r.add_argument("--message-id", required=True)
    r.add_argument("--role", required=True, choices=["user", "assistant"])
    r.add_argument("--text")
    r.add_argument("--input", help="JSON 文件：{\"text\":…}（正文放文件里，避免命令行转义）")
    r.add_argument("--time")
    r.add_argument("--branch", default="")
    e = sub.add_parser("end-session", help="会话结束事件")
    e.add_argument("--host", required=True)
    e.add_argument("--session", required=True)
    e.add_argument("--branch", default="")
    a = sub.add_parser("abort-turn", help="中止事件")
    a.add_argument("--host", required=True)
    a.add_argument("--session", required=True)
    a.add_argument("--branch", default="")
    ru = sub.add_parser("run", help="把积压的步整理掉（持锁）")
    ru.add_argument("--max-steps", type=int)
    sub.add_parser("resume", help="解除暂停")
    rc = sub.add_parser("recall", help="查记忆（参照档）")
    rc.add_argument("query", nargs="?")
    rc.add_argument("--budget", type=int)
    rc.add_argument("--cursor")
    rc.add_argument("--turn-id")
    rc.add_argument("--session")
    gm = sub.add_parser("get-messages", help="按编号取原话")
    gm.add_argument("--id", action="append", dest="ids")
    gm.add_argument("--before", type=int, default=0)
    gm.add_argument("--after", type=int, default=0)
    gm.add_argument("--cursor")
    gm.add_argument("--turn-id")
    gm.add_argument("--session")
    at = sub.add_parser("attachments", help="看一条消息挂到哪里")
    at.add_argument("--message", required=True)
    at.add_argument("--turn-id")
    at.add_argument("--session")
    rp = sub.add_parser("read-permission", help="持续的读取设置：get 看、set 设（--deny／--allow）、clear 清；不改写入")
    rp.add_argument("action", choices=["get", "set", "clear"])
    rp.add_argument("--scope", choices=["workspace", "session"], default="workspace")
    rp.add_argument("--session")
    rp.add_argument("--host", help="会话一级的设置按（宿主、会话）记：接头带上可信的宿主名和会话编号。不给就是第一段的写法（只有会话号）")
    rp.add_argument("--deny", action="store_true")
    rp.add_argument("--allow", action="store_true")
    ak = sub.add_parser("ask", help="根据记忆提问（要配置模型）")
    ak.add_argument("question")
    ak.add_argument("--asked-at", required=True, help="提问日 YYYY-MM-DD")
    sub.add_parser("status", help="状态（不拉起整理）")
    ex = sub.add_parser("export", help="导出 JSONL")
    ex.add_argument("--out", required=True)
    return ap


def _hermes_home_default():
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _add_new_setup_args(sp):
    """向导和不问话写法新增的参数。密钥本身没有参数：只收变量名（--api-key-env），密钥只在交互式终端里输入。"""
    sp.add_argument("--host", dest="host_flag", default=None, choices=["hermes", "claude-code", "codex", "pi"], help="助手；只有 hermes（别的是后续更新）。给了助手名就不问话")
    sp.add_argument("--preset", default=None, choices=[p["id"] for p in _catalog().PRESETS], help="预设：recommended 推荐｜light 轻量｜custom 自选｜local 全本机（实验）；参数写法下不给＝light")
    sp.add_argument("--provider", default=None, help="整理用的供应商（%s）" % "、".join(r["id"] for r in _catalog().PROVIDERS))
    sp.add_argument("--provider-url", default=None)
    sp.add_argument("--provider-model", default=None)
    sp.add_argument("--provider-param", action="append", default=[], metavar="键=值", help="覆盖要发的参数，值按 JSON 读；只发列出来的参数")
    sp.add_argument("--api-key-env", default=None, help="整理用密钥从哪个环境变量读（只收变量名，不收密钥本身）")
    sp.add_argument("--vectors", default=None, choices=["none", "llamacpp", "auto", "ollama", "lmstudio", "custom"], help="向量；llamacpp＝自带（现在只支持 Mac 的 M 系列），auto＝支持就自带、不支持就报错；不给：light 不开，recommended／local 要明确给")
    sp.add_argument("--embedding-model", default=None)
    sp.add_argument("--embedding-url", default=None)
    sp.add_argument("--download-model", action="store_true", help="同意下载计划里列出的全部东西（自带引擎：程序和模型；Ollama／LM Studio：向量模型）。--yes 不等于同意下载")
    sp.add_argument("--engine-url", default=None, help="自带引擎：换程序压缩包的下载位置（只许 https；校验码仍然是原委钉住的那一个）")
    sp.add_argument("--model-url", default=None, help="自带引擎：换模型文件的下载位置（只许 https；校验码仍然是原委钉住的那一个）")
    sp.add_argument("--model-file", default=None, help="自带引擎：用你自己的一个模型文件（SHA-256 要在原委钉住的表里，对不上就拒绝）")
    sp.add_argument("--engine-dir", default=None, help="自带引擎放哪里（默认 ~/.yuanwei/engine）")
    sp.add_argument("--allow-remote-embedding", action="store_true", help="允许向量服务在本机以外：索引的文字和查询会发给那个服务")
    sp.add_argument("--install-numpy", action="store_true", help="同意往 Hermes 自己的 Python 里装 NumPy")
    sp.add_argument("--hermes-python", default=None, help="Hermes 自己用的 Python（查它有没有 NumPy）")
    sp.add_argument("--ollama-cmd", default=None)
    sp.add_argument("--lms-cmd", default=None)
    sp.add_argument("--pull-timeout", type=int, default=None, help="下载最长等多少秒（默认 1800）")
    sp.add_argument("--ignore-probe-failure", action="store_true", help="本机大模型的长输入探针没通过也照装（自己承担）")


def _catalog():
    from . import catalog
    return catalog


def _setup_main(argv, io=None, sys_=None):
    """装、卸接头：yuanwei setup（向导）、yuanwei setup hermes …（现有写法）、yuanwei setup --host hermes --preset … --yes（不问话）、yuanwei uninstall hermes …。不带 --yes 只列计划、什么都不改。"""
    if _argv_guard(argv):
        return 2
    from . import hermes_setup as HS
    from . import wizard as WZ
    from . import embedsetup as ES
    ap = SafeParser(prog="yuanwei", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "uninstall"):
        sp = sub.add_parser(name, allow_abbrev=False)
        if name == "setup":
            sp.add_argument("host", nargs="?", default=None, choices=["hermes", "claude-code", "codex", "pi"])
        else:
            sp.add_argument("host", choices=["hermes"])
        sp.add_argument("--hermes-home", default=None, help="Hermes 的家目录（不给就用 $HERMES_HOME，再没有就是 ~/.hermes）")
        sp.add_argument("--yes", action="store_true", help="确认；不给只列计划")
        sp.add_argument("--hermes-cmd", default=None, help="hermes 命令的路径（不给就在 PATH 里找）；有 hermes config set 就用它改设置")
        sp.add_argument("--config-method", default="auto", choices=["auto", "hermes", "yaml"], help="改设置的办法：auto＝有 hermes config set 就用它，没有才逐行改；hermes＝只用它；yaml＝只逐行改")
        if name == "setup":
            sp.add_argument("--db", default=None, help="库文件；不给就放在这个 Hermes 分身的家目录下（<hermes_home>/yuanwei/yuanwei.sqlite），工作区是库里的分区")
            sp.add_argument("--read-scope", default=None, choices=["isolated", "life_all", "all_open"], help="读取范围。不给＝乙 life_all：在「生活」里能翻生活和所有项目，在项目里只翻这个项目自己；isolated（甲）＝每个工作区只读自己；all_open（丙）＝全部互通。写入永远只写当前工作区")
            sp.add_argument("--owner", action="append", default=[], metavar="平台:编号", help="聊天平台上哪个账号是你本人，例如 telegram:123456（可重复）。本机终端不用配")
            sp.add_argument("--mode", default="tools_only", choices=["tools_only", "reminder", "note", "directory"])
            sp.add_argument("--src-path", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            sp.add_argument("--config", dest="config_path")
            sp.add_argument("--spawn", default=None, choices=["off", "subprocess"])
            sp.add_argument("--replace-provider", action="store_true", help="memory.provider 原来是别家时，确认换成原委（原值记下，卸载时还原）")
            _add_new_setup_args(sp)
        else:
            sp.add_argument("--host-reloaded", action="store_true", help="确认 Hermes 已经重启、不再引用旧插件：第三步才删文件")
            sp.add_argument("--json", action="store_true", help="给机器读的 JSON（不给就打印给人看的计划）")
    a = ap.parse_args(argv)
    home = a.hermes_home or _hermes_home_default()
    import shutil
    cmd = a.hermes_cmd or (shutil.which("hermes") if a.config_method != "yaml" else None)
    try:
        if a.cmd == "setup":
            host = a.host or a.host_flag
            if a.host and a.host_flag and a.host != a.host_flag:
                print(json.dumps({"error": "bad_host", "message": "位置参数和 --host 给了不同的助手"}, ensure_ascii=False), file=sys.stderr)
                return 2
            io = io or WZ.IO()
            sys_ = sys_ or ES.System()
            if not host:
                if not io.tty():
                    print(json.dumps({"error": "not_a_terminal", "message": "没给助手名，又不在终端里，没法一问一答。请用参数写法，例如：yuanwei setup --host hermes --preset light --yes（先不加 --yes，只列计划）"}, ensure_ascii=False), file=sys.stderr)
                    return 2
                return WZ.run_wizard(a, io, sys_, home, cmd)
            a.host = host
            if WZ.uses_new_path(a):
                ch = WZ.resolve_args(a, sys_)
                pl = WZ.make_plan(ch, sys_, home, a, cmd)
                pn = WZ.platform_notice(sys_)                                  # 只在 Mac 以外的系统上有；Mac 上输出和以前逐字节一样
                if not a.yes:
                    body = dict(pl["plan"]["plan"], lines=(pl["lines"][:1] + [pn["text"]] + pl["lines"][1:]) if pn else pl["lines"])
                    if pn:
                        body["platform"] = {"system": pn["system"], "tested": False}
                    _out({"applied": False, "plan": body, "hint": "加 --yes 才会真的改"})
                else:
                    r = WZ.apply_plan(ch, pl, sys_, home, a)
                    r = dict(r, restart_hermes=True)
                    if pn:
                        r.update(platform_notice=pn["text"], platform={"system": pn["system"], "tested": False})
                    _out(r)
                return 0
            if host != "hermes":
                print(json.dumps({"error": "future_host", "message": "%s 是后续更新，现在还不能装；现在只有 hermes。" % host}, ensure_ascii=False), file=sys.stderr)
                return 2
            owners = {}
            for o in a.owner:
                plat, _, ident = o.partition(":")
                if not plat or not ident:
                    print(json.dumps({"error": "bad_owner", "message": "--owner 要写成 平台:编号，例如 telegram:123456"}, ensure_ascii=False), file=sys.stderr)
                    return 2
                owners.setdefault(plat, []).append(ident)
            kw = dict(hermes_home=home, db=a.db, read_scope=a.read_scope, owners=owners, mode=a.mode, src_path=a.src_path, replace_provider=a.replace_provider, config_path=a.config_path, spawn=a.spawn, hermes_cmd=cmd, config_method=a.config_method)
            if not a.yes:
                _out({"applied": False, "plan": HS._public(HS.plan_install(**kw)), "hint": "加 --yes 才会真的改"})
            else:
                _out(HS.apply_install(**kw))
        else:
            res = dict(HS.apply_uninstall(home, host_reloaded=a.host_reloaded, dry_run=not a.yes, hermes_cmd=cmd), applied=a.yes)
            eng = _uninstall_engine(home, a.yes, io or WZ.IO(), sys_)
            if eng:
                res["engine"] = eng
            if not a.yes:
                res["would_change"] = res.pop("changed")                               # 只列计划时没有改任何东西：这一项叫 would_change（会不会改），不叫 changed
                if not a.json:
                    print(HS.uninstall_plan_text(res, home))
                    return 0
            _out(res)
    except HS.SetupRefused as e:
        print(json.dumps({"error": e.code, "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


def _uninstall_engine(home, applied, io, sys_):
    """卸载这个安装时对自带向量引擎的处理：只解除这个使用方的登记（别的使用方还在用就不停引擎）；问要不要一起删引擎的文件（默认否；非终端或没 --yes 不删，只说怎么删）。"""
    inst = _installed(home)
    try:
        cfg = Config.load(inst["config_path"]) if inst and inst.get("config_path") and os.path.exists(inst["config_path"]) else None
    except Exception:                                                                   # noqa: BLE001
        cfg = None
    emb = (cfg.embedding if cfg else None) or {}
    if not emb.get("managed") or not emb.get("engine_dir"):
        return None
    from . import enginepkg as EP
    from . import enginerun as ER
    from . import wizard as WZ
    paths = EP.Paths(emb["engine_dir"])
    out = {"dir": paths.root, "users": ER.list_users(paths)}
    if not applied:
        out["note"] = "加 --yes 卸载时会解除这个安装对自带向量引擎的登记；别的安装还在用它就不会停它"
        return out
    try:
        r = ER.unbind_user(paths, WZ.user_id(home))
    except EP.EngineError as e:
        out["error"] = e.code
        return out
    out.update(remaining_users=r["remaining"], stopped=r["stopped"])
    if r["remaining"]:
        out["note"] = "别的安装还在用自带向量引擎，没有停它，也不会删它的文件"
    elif io.tty() and WZ.yes(io.ask("要不要一起删掉自带的向量引擎？约 %d MB，在 %s；你电脑上原来就有的模型文件（比如 LM Studio 里的）不会动。[y/N]：" % (ER.owned_bytes(paths) // 1048576, paths.root)), False):
        out["removed"] = ER.remove_engine(paths)
    else:
        out["note"] = "自带向量引擎的文件还在（%s）；不要了用 yuanwei vectors remove-engine --yes 删" % paths.root
    return out


def _installed(home):
    """装好的配置：config.json 里的库和配置文件路径；读不了返回 None。"""
    try:
        with open(os.path.join(home, "yuanwei", "config.json"), encoding="utf-8") as f:
            c = json.load(f)
        if not isinstance(c, dict):
            return None
        from . import hermes_profile as HP
        if not HP.home_check(c, home)["ok"]:                                            # 复制来的设置（指着别的档案的库）：当没装，不去碰它指的库和配置；要用就对这个目录跑一次 setup
            return None
        return c
    except (OSError, ValueError):
        return None


def _vectors_main(argv, io=None, sys_=None):
    """yuanwei vectors setup|status|backfill|prune。"""
    if _argv_guard(argv):
        return 2
    from . import embedsetup as ES
    from . import embedwork as EW
    from . import hermes_setup as HS
    from . import vectors as VX
    from . import wizard as WZ
    from .sqlstore import Store
    ap = SafeParser(prog="yuanwei vectors", allow_abbrev=False)
    ap.add_argument("action", choices=["setup", "status", "backfill", "prune", "serve", "stop", "remove-engine"])
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", dest="config_path", default=None)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--check", action="store_true", help="status：去探一次本机向量服务（只读请求），看编码身份还对不对")
    ap.add_argument("--reverify", action="store_true", help="setup：服务变了以后重新核验、记新的编码身份")
    ap.add_argument("--vectors", default=None, choices=["llamacpp", "auto", "ollama", "lmstudio", "custom", "none"])
    ap.add_argument("--engine-dir", default=None)
    ap.add_argument("--engine-url", default=None)
    ap.add_argument("--model-url", default=None)
    ap.add_argument("--model-file", default=None)
    ap.add_argument("--idle-exit-seconds", type=float, default=None, help="serve（内部用）：闲置多久自己退出")
    ap.add_argument("--ready-timeout", type=float, default=None, help="serve（内部用）：等就绪最长多少秒")
    ap.add_argument("--embedding-model", default=None)
    ap.add_argument("--embedding-url", default=None)
    ap.add_argument("--download-model", action="store_true")
    ap.add_argument("--allow-remote-embedding", action="store_true")
    ap.add_argument("--install-numpy", action="store_true")
    ap.add_argument("--hermes-python", default=None)
    ap.add_argument("--ollama-cmd", default=None)
    ap.add_argument("--lms-cmd", default=None)
    ap.add_argument("--pull-timeout", type=int, default=None)
    a = ap.parse_args(argv)
    home = a.hermes_home or _hermes_home_default()
    inst = _installed(home)
    db = a.db or (inst or {}).get("db")
    cp = a.config_path or (inst or {}).get("config_path")
    sys_ = sys_ or ES.System()
    if a.action in ("serve", "stop", "remove-engine"):
        return _engine_main(a, home, inst, cp, sys_)
    try:
        if not db or not os.path.exists(db) and a.action != "setup":
            print(json.dumps({"error": "no_db", "message": "找不到库：用 --db 指一下，或先 yuanwei setup"}, ensure_ascii=False), file=sys.stderr)
            return 2
        cfg = Config.load(cp) if cp else Config()
        if a.action == "status":
            st = Store.open_readonly(db)
            try:
                out = EW.status(st, db, cfg)
                if a.check and out.get("enabled"):
                    out["check"] = _vectors_check(st, cfg, sys_, db)
            finally:
                st.conn.close()
            _out(out)
            return 0
        if a.action == "backfill":
            st = Store(db)
            try:
                from .sqlstore import now_iso
                emb = cfg.embedding or {}
                if cfg.retrieval != "hybrid" or not emb.get("identity"):
                    _out({"status": "disabled"})
                    return 0
                from . import enginerun as ER
                client = ER.resolve_client(emb, "worker", config_path=cp)
                _out(EW.run_backfill(st, db, emb, now_iso, client, max_seconds=emb.get("worker_max_run_seconds", 600)))
            finally:
                st.close()
            return 0
        if a.action == "prune":
            emb = cfg.embedding or {}
            if not emb.get("identity"):
                print(json.dumps({"error": "no_identity", "message": "配置里没有向量的编码身份，不知道该留哪个"}, ensure_ascii=False), file=sys.stderr)
                return 2
            fp = VX.identity_fp(emb["identity"])
            st = Store(db)
            try:
                olds = [(r[0][:10], r[1]) for r in st.conn.execute("SELECT fp, COUNT(*) FROM vectors WHERE fp != ? GROUP BY fp", (fp,))]
                if not a.yes:
                    _out({"applied": False, "stale_identities": [{"fp10": f, "count": n} for f, n in olds], "hint": "加 --yes 才会真的删（删了不能恢复，向量是缓存，可以重算）"})
                else:
                    from .deletion import DeletionService
                    _out({"applied": True, "removed": DeletionService(db, store=st).prune_vectors(fp)})
            finally:
                st.close()
            return 0
        # setup
        if not inst:
            print(json.dumps({"error": "not_installed", "message": "还没有装原委：先 yuanwei setup"}, ensure_ascii=False), file=sys.stderr)
            return 2
        io = io or WZ.IO()
        return WZ.vectors_setup(a, io, sys_, home, inst, cfg)
    except HS.SetupRefused as e:
        print(json.dumps({"error": e.code, "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (YuanweiError, StoreError, ValueError) as e:
        print(json.dumps({"error": getattr(e, "code", type(e).__name__), "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _engine_main(a, home, inst, cp, sys_):
    """yuanwei vectors serve（守护者，内部用）｜stop｜remove-engine [--yes]。引擎目录：--engine-dir，或装好的配置里记的。"""
    from . import enginepkg as EP
    from . import enginerun as ER
    emb = {}
    try:
        if cp and os.path.exists(cp):
            emb = Config.load(cp).embedding or {}
    except Exception:                                                                   # noqa: BLE001
        emb = {}
    d = a.engine_dir or emb.get("engine_dir") or EP.default_engine_dir(sys_.env)
    paths = EP.Paths(d)
    try:
        if a.action == "serve":
            r = ER.serve_main(d, idle_exit=a.idle_exit_seconds, ready_timeout=a.ready_timeout, pins=sys_.pins)
            _out({"serve": r})
            return 0
        if a.action == "stop":
            r = ER.stop_engine(paths, sys_.engine_host, pins=sys_.pins)
            _out(r)
            return 0 if r["stopped"] else 3
        users = ER.list_users(paths)
        if not a.yes:
            _out({"applied": False, "dir": d, "users": users, "bytes_owned": ER.owned_bytes(paths),
                  "hint": "加 --yes 才会真的删。会停掉引擎、删掉原委下载的程序和模型（你原来就有的模型文件不会动）；%s" % ("还有这些使用方在用它：%s，删了以后它们的向量会退回只用关键词" % "、".join(users) if users else "现在没有登记的使用方")})
            return 0
        r = ER.remove_engine(paths, sys_.engine_host, pins=sys_.pins)
        _out(dict(r, applied=r["stopped"]))
        return 0 if r["stopped"] else 3
    except EP.EngineError as e:
        print(json.dumps({"error": e.code, "message": "引擎正忙或出错（%s）" % e.code}, ensure_ascii=False), file=sys.stderr)
        return 2


def _vectors_check(st, cfg, sys_, db):
    """只读地问一次服务：现在的编码身份和配置里记的比。不一样就标 drift（写 store_meta）。"""
    from . import catalog as CT
    from . import embedsetup as ES
    from . import embedwork as EW
    from . import vectors as VX
    emb = cfg.embedding or {}
    ident = emb["identity"]
    if emb.get("managed"):                                                              # 自带引擎：只读地看现在跑着的那个（不启动、不续命、不修复）
        from . import enginepkg as EP
        from . import enginerun as ER
        s = ER.trusted_state(EP.Paths(emb["engine_dir"]))
        if s is None:
            return {"ok": None, "reason": "engine_not_running"}
        diff = sorted(k for k in ("model_file_sha256", "runtime_version", "context_length") if s.get(k) != ident.get(k))
        return {"ok": not diff, "reason": "identity_differs" if diff else None, "differs": diff, "weak": False}
    rt = CT.runtime(emb.get("runtime")) or ES.custom_runtime(emb["base_url"], emb["model"])
    res = ES.verify(sys_, rt, emb["model"], emb["base_url"], cmd_path=sys_.which(rt.get("cmd")) if rt.get("cmd") else None, expected_dim=ident.get("dimension"), allow_remote=bool(emb.get("allow_remote")))
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    diff = sorted(k for k in VX.IDENTITY_FIELDS if res["identity"].get(k) != ident.get(k))
    return {"ok": not diff, "reason": "identity_differs" if diff else None, "differs": diff, "weak": res["weak"]}


def _key_main(argv, io=None, sys_=None):
    """yuanwei key set|status|test|delete。密钥只在交互式终端里输入（不回显）。默认存／删**全局**的一份（~/.yuanwei/keys/<变量名>，几个机器人共用）；
    加 --this-bot-only 才动这个机器人自己的一份（<hermes 家目录>/yuanwei/keys/<变量名>，配合 --hermes-home 指定是哪个）。读的先后：自己的 → 全局 → 环境变量。任何输出里都没有密钥的值。"""
    if _argv_guard(argv):
        return 2
    from . import catalog as CT
    from . import embedsetup as ES
    from . import keystore as KS
    from . import wizard as WZ
    ap = SafeParser(prog="yuanwei key", allow_abbrev=False)
    ap.add_argument("action", choices=["set", "status", "test", "delete"])
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--api-key-env", default=None, help="变量名；不给就取装好的配置里的，没有配置默认 DEEPSEEK_API_KEY")
    ap.add_argument("--this-bot-only", action="store_true", help="set／delete 只动这个机器人自己的那份密钥文件（用 --hermes-home 指定是哪个机器人），不动全局的")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    home = a.hermes_home or _hermes_home_default()
    inst = _installed(home)
    prov = {}
    has_config = False
    try:
        if inst and inst.get("config_path") and os.path.exists(inst["config_path"]):
            prov = dict(Config.load(inst["config_path"]).provider or {})
            has_config = True
    except Exception:                                                                   # noqa: BLE001
        prov = {}
    if not prov.get("base_url"):
        has_config = False
        d = CT.provider("deepseek")
        prov = {"base_url": d["base_url"], "params": dict(d["params"], model=d["model"]), "api_key_env": d["key_env"]}
    cfg_env = prov.get("api_key_env") if has_config else None
    env = a.api_key_env or prov.get("api_key_env") or "DEEPSEEK_API_KEY"
    io = io or WZ.IO()
    sys_ = sys_ or ES.System()
    try:
        std_own = KS.key_file(home, env)                                                # 这个机器人自己的标准位置（--this-bot-only 管的就是它）
    except ConfigError_ as e:
        print(json.dumps({"error": "bad_env_name", "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    dir_problem = KS.global_dir_problem(sys_.env)                                       # YUANWEI_KEY_DIR 写得不对（不是绝对路径）：不用全局文件，status 会说明；要动全局文件的命令报错
    global_path = None if dir_problem else KS.global_key_file(env, sys_.env)
    if dir_problem and not (a.this_bot_only or a.action in ("status", "test")):
        print(json.dumps({"error": "bad_key_dir", "message": dir_problem}, ensure_ascii=False), file=sys.stderr)
        return 2
    # 查哪一份「自己的文件」：
    # · 已经装好、读到了配置：用配置里实际记的 key_file（整理 provider 和 status hermes 用的就是它）；配置里没有 key_file：不读任何密钥文件，只看环境变量；
    # · 没装：按装好以后的样子（标准位置）预览；
    # · 用 --api-key-env 指定的变量和配置里的不一样：配置里记的 key_file 是给配置那个变量的，不能拿来查别的变量，这时按标准位置查，并写明查的是指定的变量。
    cfg_key_file = prov.get("key_file") if has_config else None
    differs = bool(has_config and a.api_key_env and a.api_key_env != cfg_env)
    if not has_config:
        basis, reads_files, use_path = "preview_not_installed", KS.supported(), (std_own if KS.supported() else None)
    elif differs:
        basis, reads_files = "named_variable_differs_from_config", KS.supported() and bool(cfg_key_file)
        use_path = std_own if reads_files else None
    else:
        basis, reads_files = "installed_config", KS.supported() and bool(cfg_key_file)
        use_path = cfg_key_file if reads_files else None
    basis_note = {"preview_not_installed": "还没装好原委的配置：这是按「装好以后」的标准位置做的预览，不是在核一份实际在用的配置。",
                  "named_variable_differs_from_config": "查的是你用 --api-key-env 指定的变量 %s，不是当前整理配置用的 %s；不能当成「当前整理配置能不能用」的结论。" % (env, cfg_env or "（没有）"),
                  "installed_config": None}[basis]
    custom = bool(basis == "installed_config" and cfg_key_file and os.path.abspath(cfg_key_file) != os.path.abspath(std_own))
    custom_note = "配置里记的这个机器人自己的密钥文件是 %s，不是标准位置；--this-bot-only 只管标准位置 %s，不会动那份自定义文件。" % (cfg_key_file, std_own) if custom else None
    target = std_own if a.this_bot_only else global_path
    if a.action == "status":
        src = KS.source_with_dotenv(env, use_path, home, sys_.env)
        rep = {"env": env, "source": src, "basis": basis,
               "global_file": dict(KS.file_report(global_path), checked=bool(reads_files and global_path)),
               "this_bot_file": dict(KS.file_report(use_path or std_own), checked=bool(reads_files)),                 # checked 为假：这份设置不读密钥文件，这里只是照实说文件在不在（标准位置）
               "config_reads_key_files": reads_files, "order": "这个机器人自己的文件 → 全局文件 → 环境变量 → Hermes 的 .env（只看名字）" if reads_files else "环境变量 → Hermes 的 .env（这份设置不读密钥文件）",
               "platform_supports_key_file": KS.supported(), "note_values": "这里只报来源，不显示密钥的值；文件那几项只看类型和权限，不代表里面的值合格。"}
        if dir_problem:
            rep["global_dir_problem"] = dir_problem
        elif reads_files:
            rep["global_dir"] = os.path.dirname(global_path)
        if custom:
            rep["this_bot_only_manages"] = std_own
        notes = [n for n in (basis_note, custom_note, KS.UNUSABLE_NOTE if src.startswith("file_unusable") else None) if n]
        if notes:
            rep["notes"] = notes
        _out(rep)
        return 0
    if a.action == "delete":
        if not a.yes:
            r = {"applied": False, "env": env, "scope": "this_bot" if a.this_bot_only else "global", "path": target, "file_present": os.path.lexists(target), "hint": "加 --yes 才会删密钥文件"}
        else:
            r = {"applied": True, "env": env, "scope": "this_bot" if a.this_bot_only else "global", "path": target, "deleted": KS.delete_file(target)}
        if a.this_bot_only and custom_note:
            r["note"] = custom_note
        _out(r)
        return 0
    if not io.tty():
        print(json.dumps({"error": "not_a_terminal", "message": "请在你自己的终端里运行这条命令，不要通过助手运行。助手不该经手密钥。"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        if a.action == "set":
            if not KS.supported():
                print(json.dumps({"error": "unsupported_platform", "message": "这个系统没法把文件权限设成只有你能读：原委不存密钥文件，请用环境变量。"}, ensure_ascii=False), file=sys.stderr)
                return 2
            if os.path.lexists(target) and not WZ.yes(io.ask("%s 已经有%s密钥文件了，覆盖吗？ [y/N]：" % (env, "这个机器人自己的" if a.this_bot_only else "全局的")), False):
                raise WZ.Cancel()
            key = WZ._enter_key(io, {"choice": "env_or_file", "value": None, "test": False})
            if key["value"] is None:
                io.say("没有存。")
                return 1
            if a.this_bot_only:
                KS.write_key(home, env, key["value"])
                io.say("已存（只有你能读的文件，只给这个机器人：%s）。" % std_own)
                if custom_note:
                    io.say(custom_note + "（所以整理现在实际用的还是那份自定义文件。）")
            else:
                KS.write_global_key(env, key["value"], sys_.env)
                io.say("已存（只有你能读的文件，几个机器人共用：%s）。" % global_path)
                for ln in _own_key_notes(home, a.hermes_home, env):
                    io.say(ln)
            if WZ.yes(io.ask("要试一下这把密钥能不能用吗？会向 %s 发一个最小的请求，花不到一分钱，只告诉你「能用」或「不能用」。 [y/N]：" % prov["base_url"]), False):
                io.say(_key_test_text(prov, key["value"], sys_))
            return 0
        if a.action == "test":
            v, src = KS.lookup(env, use_path, sys_.env)
            if basis_note:
                io.say(basis_note)
            if not v:
                io.say("没有找到密钥（%s）。" % src)
                return 2
            io.say("用的是：%s（只说来源，不显示密钥的值）。" % {"file": "这个机器人自己的密钥文件%s" % (" " + use_path if use_path else ""), "global_file": "全局密钥文件 %s" % global_path, "env": "环境变量 " + env}.get(src, src))
            if not WZ.yes(io.ask("会向 %s 发一个最小的请求，花不到一分钱，只告诉你「能用」或「不能用」。继续吗？ [y/N]：" % prov["base_url"]), False):
                raise WZ.Cancel()
            io.say(_key_test_text(prov, v, sys_))
            return 0
    except WZ.Cancel:
        io.say("已取消，什么都没改。")
        return 1
    return 0


def _own_key_notes(home, given_home, env):
    """写了全局密钥以后：本机哪些机器人有自己单独的这份密钥文件。只看文件在不在，不读内容，所以不知道它们能不能用；能用的话会先用它，不用刚存的全局这把。
    找法：主家目录、它下面的 profiles/*（hermes_profile.list_profiles），再加用户给的 --hermes-home；没有穷举（别处的家目录、配置里记了别的密钥路径的，这里查不到）。"""
    from . import hermes_profile as HP
    from . import keystore as KS
    homes = [HP.default_root(home)] + [h for _n, h in HP.list_profiles(home)]
    if given_home:
        homes.append(given_home)
    seen, found = set(), []
    for h in homes:
        k = HP.norm(h)
        if k in seen:
            continue
        seen.add(k)
        p = KS.key_file(h, env)
        if os.path.lexists(p):
            found.append((h, p))
    if not found:
        return []
    out = ["这些机器人有自己单独的密钥文件；能用的话，会先用它，不用刚存的全局这把（这里只看了文件在不在，没有看它能不能用）。实际用哪把，看 yuanwei key status（加上对应的 --hermes-home）："]
    for h, p in found:
        out.append("  · %s（%s）" % (p, h))
    out.append("想让它们改用全局的，把它们自己的那份删掉：yuanwei key delete --this-bot-only --hermes-home <上面括号里的目录> --api-key-env %s --yes" % env)
    out.append("上面只查了主家目录、它下面的 profiles/*、和你给的 --hermes-home，没有查全：别处的 Hermes 家目录、或设置里记了别的密钥文件路径的机器人，这里看不到。")
    return out


def _key_test_text(prov, value, sys_):
    from . import keystore as KS
    p = dict(prov.get("params") or {})
    model = p.pop("model", None)
    r = KS.check_key(prov["base_url"], model, p, value, opener=sys_.opener)
    return {"works": "能用。", "rejected": "不能用（对方拒绝了这把密钥）。", "unverified": "没验成（网络或对方的服务出了问题，不是密钥的问题）。"}[r]


def _doctor_main(argv):
    """yuanwei doctor hermes：读本机装着的 Hermes 源码（只读），逐项核我们依赖的接口。退出码 0＝全对，1＝有变了的，2＝找不到目录。"""
    from . import hermes_compat as HC
    ap = SafeParser(prog="yuanwei doctor", allow_abbrev=False)
    ap.add_argument("host", choices=["hermes"])
    ap.add_argument("--hermes-dir", default=os.path.expanduser("~/.hermes/hermes-agent"), help="Hermes 的安装（源码）目录")
    a = ap.parse_args(argv[1:])
    rep = HC.doctor(a.hermes_dir)
    _out(rep)
    return rep["exit_code"]


def _status_hermes_main(argv, sys_=None):
    """yuanwei status hermes：只读，装没装、读取范围、认得的工作区、运行时的数。"""
    from . import hermes_setup as HS
    ap = SafeParser(prog="yuanwei status", allow_abbrev=False)
    ap.add_argument("host", choices=["hermes"])
    ap.add_argument("--hermes-home", default=None, help="Hermes 的家目录（不给就用 $HERMES_HOME，再没有就是 ~/.hermes）")
    ap.add_argument("--hermes-python", default=None, help="Hermes 实际用的装包环境的 Python（查 NumPy 用；不给就问 Hermes 自己）")
    a = ap.parse_args(argv[1:])
    from . import embedsetup as ES
    _out(HS.install_status(a.hermes_home or _hermes_home_default(), sys_=sys_ or ES.System(), hermes_python=a.hermes_python))
    return 0


def _drop_hermes_word(argv, idx):
    """`conversations list hermes`、`secrets scan hermes`：第 idx 个位置上的 hermes 只是在说「用 Hermes 接头的库」，不是参数。"""
    argv = list(argv)
    if len(argv) > idx and argv[idx] == "hermes":
        del argv[idx]
    return argv


def _ownership_problem(home):
    """config.json 在、但它的归属有问题：(错误码, 人话)；没问题或没有设置返回 None。和插件启动时是同一个检查（hermes_profile.home_check）。"""
    from . import hermes_profile as HP
    try:
        with open(os.path.join(home, "yuanwei", "config.json"), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict):
        return None
    r = HP.home_check(c, home)
    if r["ok"]:
        return None
    if r["state"] == HP.UNCONFIRMED:
        return HP.UNCONFIRMED, HP.unconfirmed_message(home, c.get("db"))
    return HP.COPIED, HP.copied_message(home)


def _db_or_error(a):
    """用户给了 --db 就用它；没给就用本机装着的 Hermes 接头的库（config.json 里的 db）。返回 (库路径, 来源)；都没有：报错说怎么写，返回 (None, None)。来源写进输出，用户看得见用的是哪个库。"""
    if a.db:
        return a.db, "--db"
    home = a.hermes_home or _hermes_home_default()
    inst = _installed(home)
    db = (inst or {}).get("db")
    if db and os.path.exists(db):
        return db, "Hermes 接头的库（%s）" % os.path.join(home, "yuanwei", "config.json")
    prob = _ownership_problem(home)
    if prob:                                                                            # 设置在，但归属有问题（复制来的／没法确认）：说清原因，不说成「没装」
        print(json.dumps({"error": prob[0], "message": prob[1]}, ensure_ascii=False), file=sys.stderr)
        return None, None
    print(json.dumps({"error": "no_db", "message": "没给 --db，也没在 %s 找到装好的原委 Hermes 接头的库。请用 --db 库文件 指一下；装了 Hermes 接头的话，可以写成 yuanwei conversations list hermes、yuanwei secrets scan hermes（Hermes 家目录不在默认位置时加 --hermes-home）。" % home}, ensure_ascii=False), file=sys.stderr)
    return None, None


def _conversations_main(argv):
    """yuanwei conversations list|delete|status|resume：在打开 Memory 之前分流（不整理、不调模型）。delete 不带 --yes 只列要删什么。退出码 0 成功、2 用法、3 找不到、5 清理中。"""
    from .deletion import DeletionService
    ap = SafeParser(prog="yuanwei conversations", allow_abbrev=False)
    ap.add_argument("action", choices=["list", "delete", "status", "resume"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--db", default=None, help="库文件；不给就用 Hermes 接头的库（<hermes 家目录>/yuanwei/config.json 里记的）。也可以写成 conversations list hermes")
    ap.add_argument("--hermes-home", default=None, help="Hermes 的家目录（不给就用 $HERMES_HOME，再没有就是 ~/.hermes）")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--json", action="store_true")
    argv = _drop_hermes_word(argv, 1)
    a = ap.parse_args(argv)
    a.db, dbsrc = _db_or_error(a)
    if a.db is None:
        return 2
    svc = DeletionService(a.db)
    try:
        if a.action == "list":
            rows = [dict(r._asdict(), head_note="只在你自己的终端里显示") for r in svc.list_conversations()]
            _out({"db": os.path.abspath(a.db), "db_source": dbsrc, "conversations": rows})
        elif a.action == "delete":
            if not a.target:
                print(json.dumps({"error": "usage", "message": "要给对话编号"}, ensure_ascii=False), file=sys.stderr)
                return 2
            if not a.yes:
                p = svc.preview(a.target)
                _out({"applied": False, "plan": p._asdict(), "db": os.path.abspath(a.db), "db_source": dbsrc, "hint": "加 --yes 才会真的删；删了找不回来（Hermes 自己的会话记录、你导出的文件、已发给模型的内容不归原委管）"})
                return 0
            r = svc.delete_conversation(a.target, source="command", operator="cli")
            _out(dict(r._asdict(), applied=True, db=os.path.abspath(a.db), db_source=dbsrc))
            return 0 if r.status == "complete" else (3 if r.code == "E_NOT_FOUND" else 5)
        elif a.action == "status":
            _out({"db": os.path.abspath(a.db), "db_source": dbsrc, "deletions": svc.status(a.target)})
        else:
            rs = svc.resume()
            _out({"db": os.path.abspath(a.db), "db_source": dbsrc, "resumed": [r._asdict() for r in rs]})
            return 0 if all(r.status == "complete" for r in rs) else 5
        return 0
    except NotFound as e:
        print(json.dumps({"error": e.code, "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    finally:
        svc.close()


def _secrets_main(argv):
    from . import secretscan
    ap = SafeParser(prog="yuanwei secrets", allow_abbrev=False)
    ap.add_argument("action", choices=["scan"])
    ap.add_argument("--db", default=None, help="库文件；不给就用 Hermes 接头的库（<hermes 家目录>/yuanwei/config.json 里记的）。也可以写成 secrets scan hermes")
    ap.add_argument("--hermes-home", default=None, help="Hermes 的家目录（不给就用 $HERMES_HOME，再没有就是 ~/.hermes）")
    ap.add_argument("--workspace")
    argv = _drop_hermes_word(argv, 1)
    a = ap.parse_args(argv)
    a.db, dbsrc = _db_or_error(a)
    if a.db is None:
        return 2
    rep = secretscan.scan(a.db, a.workspace)
    _out(dict(rep._asdict(), db=os.path.abspath(a.db), db_source=dbsrc))
    return 0 if rep.complete else 5


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    bad = _argv_guard(raw)
    if bad:
        return bad
    if not raw or raw[0] in ("-h", "--help", "help"):
        print(USER_HELP)
        return 0 if raw else 2
    if raw and raw[0] == "conversations":
        return _conversations_main(raw[1:])
    if raw and raw[0] == "secrets":
        return _secrets_main(raw[1:])
    if raw and raw[0] in ("setup", "uninstall"):
        return _setup_main(raw)
    if raw and raw[0] == "vectors":
        return _vectors_main(raw[1:])
    if raw and raw[0] == "key":
        return _key_main(raw[1:])
    if raw and raw[0] == "doctor":
        return _doctor_main(raw)
    if raw[:2] == ["status", "hermes"]:
        return _status_hermes_main(raw)
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config) if args.config else Config()
    spawn = "off" if args.cmd in ("run", "status", "export", "resume", "read-permission") else "subprocess"
    provider = _provider(cfg) if args.cmd in ("run", "ask") else None
    try:
        mem = Memory.open(args.db, args.workspace, config=cfg, provider=provider, spawn=spawn, config_path=args.config)
        if args.cmd == "record":
            text = args.text
            if args.input:
                with open(args.input, encoding="utf-8") as f:
                    text = json.load(f)["text"]
            _out(mem.record(args.host, args.session, args.message_id, args.role, text, time=args.time, branch=args.branch))
        elif args.cmd == "end-session":
            _out({"sealed": len(mem.end_session(args.host, args.session, args.branch))})
        elif args.cmd == "abort-turn":
            _out({"sealed": len(mem.abort_turn(args.host, args.session, args.branch))})
        elif args.cmd == "run":
            mem.maintain()
            _out(mem.run(args.max_steps))
        elif args.cmd == "resume":
            _out({"resumed": mem.resume()})
        elif args.cmd == "recall":
            _out(mem.recall(args.query, budget=args.budget, cursor=args.cursor, turn_id=args.turn_id, session_id=args.session))
        elif args.cmd == "get-messages":
            _out(mem.get_messages(args.ids, args.before, args.after, args.cursor, turn_id=args.turn_id, session_id=args.session))
        elif args.cmd == "attachments":
            _out(mem.attachments(args.message, turn_id=args.turn_id, session_id=args.session))
        elif args.cmd == "read-permission":
            if args.host is not None:
                from .access import Access
                if args.scope != "session" or not args.session or not args.host:
                    raise ValueError("--host 只用在 --scope session，而且要同时给非空的 --host 和 --session")
                acc = Access(mem)
                if args.action == "get":
                    _out(acc.session_permission(args.host, args.session))
                elif args.action == "set":
                    if args.deny == args.allow:
                        raise ValueError("set 要且只要给 --deny 或 --allow 之一")
                    _out(acc.set_session_permission(args.host, args.session, args.allow))
                else:
                    _out(acc.clear_session_permission(args.host, args.session))
            elif args.action == "get":
                _out(mem.read_permission(args.session))
            elif args.action == "set":
                if args.deny == args.allow:
                    raise ValueError("set 要且只要给 --deny 或 --allow 之一")
                _out(mem.set_read_permission(args.allow, args.scope, args.session))
            else:
                _out(mem.clear_read_permission(args.scope, args.session))
        elif args.cmd == "ask":
            _out(mem.ask(args.question, args.asked_at))
        elif args.cmd == "status":
            _out(mem.status())
        elif args.cmd == "export":
            _out(mem.export(args.out))
        return 0
    except (NotFound, NotVisible) as e:
        print(json.dumps({"error": e.code, "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except Paused as e:
        print(json.dumps({"error": e.code, "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 4
    except (YuanweiError, StoreError, ValueError) as e:
        print(json.dumps({"error": getattr(e, "code", type(e).__name__), "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
