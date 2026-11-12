"""Hermes 的工作区：**项目**（hermes project，用项目的稳定编号做键）或**「生活」**（没挂项目的对话）。分身（profile）不是用户要管的东西：每个分身有自己的 hermes_home，
我们的库跟着它走，天然分开。

Hermes 不把「当前是哪个项目」传给记忆插件：initialize 的 agent_workspace 恒为 "hermes"，v0.22 起只传 cwd（会话工作目录，有才传）。Hermes 自己把会话归到项目靠 cwd 对着
$HERMES_HOME/projects.db 里项目的文件夹（hermes_cli/projects_db.py: project_for_path：文件夹等于或是祖先，最长的赢，归档的不算）；`hermes project use` 只是界面上的「当前项目」，
不跟会话绑定，不用。这里照同一条规则从 cwd 推项目，**只读** projects.db（sqlite 的只读模式，只 SELECT 两张表），取项目的 id（稳定）和 name（只作显示标签，改名不换键）。

认不出（说不清）就是认不出：库打不开、表结构变了、归档的项目管着这个目录、**给了的** cwd 不合格，都返回 key=None 和原因；调用方这一轮不写也不读，不许猜着落进「生活」或别的项目。
只有「确定没挂项目」才是生活：没有 projects.db（没建过项目、或老版本没有这个功能）；有 projects.db、没有任何未归档项目管这个 cwd；没给工作目录又拿不到可信的目录（见下）。

**没给 cwd 怎么办（2026-11-11 真机试装后改，依据：Hermes agent/agent_init.py 的 _memory_provider_init_kwargs 只有 agent.session_cwd 有值才传 cwd；经典终端建 agent 时不给）：**
本机平台（cli、local、tui、desktop）没给 cwd 时——平台是 cli：取 Hermes 进程此刻的工作目录（见 local_process_cwd，优先用 Hermes 自己的 resolve_agent_cwd），照项目的规则判；
其他本机平台（tui、desktop、local）：记进「生活」（它们跑在另一个后台进程里，进程目录不是用户的项目目录）；取不到、是 "/"、不是绝对路径：记进「生活」。
网关平台（手机等）没给 cwd：照旧记进「生活」。给了 cwd 的，项目判定一个字不改。
resolve 返回的 reason：成功认出时写依据——cwd_given（宿主给了）、cwd_from_process（用 Hermes 进程的目录）、no_cwd_life（没给、也没有可信目录，记进生活）；认不出时写原因。
注意：读取范围选「甲（每个工作区只翻自己）」时，没给工作目录的项目对话会落进「生活」，只翻得到「生活」里的话。
"""
import json
import os
import sqlite3

LIFE = "life"
# 本机的会话（由这台电脑的主人在用）。依据见交回报告：cli＝经典终端（cli.py/hermes_cli 本机进程）；local＝网关里的本机来源（gateway/config.py Platform.LOCAL）；
# tui＝hermes --tui 或桌面版里的终端、desktop＝桌面版聊天窗口（tui_gateway/agent_factory.py _resolve_session_platform）。
LOCAL_PLATFORMS = ("cli", "local", "tui", "desktop")
LOGIN_CHECKED_PLATFORMS = ("tui", "desktop")                # 这两个平台的 agent 可能带着登录身份（agent_factory.py：user_id=auth_user_id＝宿主主人的系统账号）：带了就不自动当主人，照网关的规则要配 owners
PROCESS_CWD_PLATFORMS = ("cli",)                           # 只有这个平台的 Hermes 进程目录就是用户的目录
SCOPES = ("isolated", "life_all", "all_open")
DEFAULT_SCOPE = "life_all"      # 乙（用户定）：生活能读生活和所有项目，项目只读自己；甲 isolated＝全隔开，丙 all_open＝全打通，都是可选设置
LABELS_FILE = os.path.join("yuanwei", "workspaces.json")


def _norm(path):
    """Hermes 的 _normalize_path：绝对、展开 ~、去掉末尾分隔符（不解析符号链接）。"""
    p = os.path.abspath(os.path.expanduser(str(path).strip()))
    return p.rstrip("/\\") or p


def _owns(folder, target):
    stem = folder.rstrip("/\\")
    return target == folder or target.startswith(stem + os.sep) or target.startswith(stem + "/")


def _unresolved(reason):
    return {"key": None, "kind": None, "label": None, "reason": reason}


def local_process_cwd():
    """Hermes 进程里「智能体在哪个目录干活」。优先问 Hermes 自己（agent.runtime_cwd.resolve_agent_cwd：配置的目录，否则启动目录），问不到就用 os.getcwd()。
    取不到返回 None。经典终端会在 initialize 之前 chdir（--in、恢复会话，见 hermes_cli/main.py），所以这一刻取到的就是用户在的目录。"""
    try:
        import importlib
        p = importlib.import_module("agent.runtime_cwd").resolve_agent_cwd()
        return str(p)
    except Exception:                                                                    # noqa: BLE001  不在 Hermes 里、或这一版没有这个函数：退回进程目录
        pass
    try:
        return os.getcwd()
    except OSError:
        return None


def _usable_process_cwd(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = os.path.expanduser(raw.strip())
    if not os.path.isabs(p):
        return None
    n = os.path.normpath(p)
    return None if n == os.sep else n                                                    # 根目录不是任何人的项目目录


def resolve(hermes_home, cwd, platform, process_cwd=None):
    """process_cwd：取进程目录的办法（默认 local_process_cwd；测试用来注入）。"""
    plat = platform or "cli"
    if cwd is not None:
        return _resolve_with_cwd(hermes_home, cwd, "cwd_given")                                       # 给了 cwd：判定照旧，只是成功时写上依据
    if plat in PROCESS_CWD_PLATFORMS:
        raw = (process_cwd or local_process_cwd)()
        usable = _usable_process_cwd(raw)
        if usable is not None:
            return _resolve_with_cwd(hermes_home, usable, "cwd_from_process")
    return {"key": LIFE, "kind": "life", "label": "生活", "reason": "no_cwd_life"}      # 没给、又没有可信目录：生活（本机平台和网关平台都一样）


def _resolve_with_cwd(hermes_home, cwd, basis):
    db = os.path.join(str(hermes_home), "projects.db")
    life = {"key": LIFE, "kind": "life", "label": "生活", "reason": basis}
    if not os.path.exists(db):
        return life                                                                      # 没有 projects.db：没有任何项目（老版本也没有这个功能）
    if not isinstance(cwd, str) or not cwd.strip():
        return _unresolved("cwd_invalid")
    if not os.path.isabs(os.path.expanduser(cwd.strip())):
        return _unresolved("cwd_not_absolute")
    target = _norm(cwd)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        try:
            cur = conn.execute("SELECT pf.project_id, pf.path, p.archived, p.name FROM project_folders pf JOIN projects p ON p.id = pf.project_id")
            data = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return _unresolved("projects_db_schema_changed" if "no such" in str(e) else "projects_db_unreadable")
    except sqlite3.DatabaseError:
        return _unresolved("projects_db_unreadable")
    except Exception:                                                                    # noqa: BLE001
        return _unresolved("projects_db_unreadable")
    owners = [(len(_norm(f)), pid, int(arch or 0), name) for pid, f, arch, name in data if _owns(_norm(f), target)]
    if not owners:
        return life                                                                      # 有项目库、没有任何项目管这个目录：确定没挂项目
    best_active = max((o for o in owners if not o[2]), default=None)
    best_any = max(owners)
    if best_any[2] and (best_active is None or best_any[0] > best_active[0]):
        return _unresolved("project_archived")                                           # 归档的项目管着这里（Hermes 自己不算它）：说不清
    return {"key": "proj:%s" % best_active[1], "kind": "project", "label": str(best_active[3]), "reason": basis}


# ---------------------------------------------------------------- 登记簿（我们自己的文件，只存键和显示名，不存内容）
def _labels_path(hermes_home):
    return os.path.join(str(hermes_home), LABELS_FILE)


def known(hermes_home):
    try:
        with open(_labels_path(hermes_home), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                                                    # noqa: BLE001
        return {}


def remember(hermes_home, ws):
    """认出一个工作区就记一笔（键、种类、显示名；改名只改显示名）。认不出的不记。"""
    if not ws or not ws.get("key"):
        return
    d = known(hermes_home)
    if d.get(ws["key"]) == {"kind": ws["kind"], "label": ws["label"]}:
        return
    d[ws["key"]] = {"kind": ws["kind"], "label": ws["label"]}
    p = _labels_path(hermes_home)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = "%s.tmp-%d" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)


def readable(scope, current, known_ws):
    """当前这个工作区在这个读取范围下能读哪些（当前的排第一）。范围只来自用户的配置；认不得的值按最严。
    甲 isolated：只自己。乙 life_all：生活读所有（生活＋全部项目），项目只读自己。丙 all_open：全部。"""
    if scope == "all_open":
        life_first = [current] + ([LIFE] if LIFE in known_ws and current != LIFE else []) + sorted(k for k in known_ws if k not in (current, LIFE))
        return life_first
    if scope == "life_all" and current == LIFE:
        return [LIFE] + sorted(k for k in known_ws if k != LIFE)
    return [current]
