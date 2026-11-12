"""Hermes 的「机器人」＝配置档案（profile）：每个有自己的家目录 `<根>/profiles/<名字>/`。

起因（2026-11-11 真机）：在 Hermes 桌面版新建机器人时，Hermes 把默认档案的设置整份复制过去（`plugins/yuanwei/`、`yuanwei/config.json`、`yuanwei.toml`、`install.json`，
`memory.provider` 也是 yuanwei）；config.json 里 `db`、`config_path` 是绝对路径，复制过去还指着默认档案的库——机器人和主 Hermes 读写同一个库。

做法：装的时候把「这份设置属于哪个家目录」写进 config.json（`hermes_home`）；插件初始化时拿它和宿主这一刻给的家目录比。不一样＝复制来的：不启用，
**绝不悄悄用别的档案的库**。老的设置文件（装在这一版之前、没有 `hermes_home`）按库路径的写法推断：库在 `<X>/yuanwei/yuanwei.sqlite`，X 不是当前家目录＝复制来的。
库在这个家目录自己的目录里面的，算属于这里；库放在别的自定义位置、又没记家目录的，**归属没法确认：不启用**（`ownership_unconfirmed`，不说成确定复制来的），要用户重新确认安装（`setup --hermes-home <它>`，要继续用那个库就加 `--db <库>`），装的时候才登记家目录。
这一版只管「你和某一个机器人的一对一」；群聊（平台 bot_room）明确不记不查（见 GROUP_ROOM_PLATFORMS）。
"""
import os

COPIED = "copied_from_other_profile"
UNCONFIRMED = "ownership_unconfirmed"
GROUP_ROOM = "group_room_not_supported"
GROUP_ROOM_PLATFORMS = ("bot_room",)             # Hermes 群聊里成员机器人回话那一轮的平台（gateway/session_hosted_rpc.py: source='bot_room', platform='bot_room'）


def norm(p):
    return os.path.realpath(os.path.expanduser(str(p)))


def profile_name(home):
    """家目录是 `<根>/profiles/<名字>` 就返回名字（名字不以点开头）；否则 None（主 Hermes 或别的）。"""
    if not home:
        return None
    p = norm(home)
    name, parent = os.path.basename(p), os.path.dirname(p)
    if os.path.basename(parent) == "profiles" and name and not name.startswith("."):
        return name
    return None


def default_root(home):
    """主 Hermes 的家目录：给的是某个机器人的目录就取它上两级；否则就是它自己。"""
    p = norm(home)
    return os.path.dirname(os.path.dirname(p)) if profile_name(p) else p


def list_profiles(home):
    """本机有哪些机器人：[(名字, 家目录)]，按名字排。只看目录结构（`<根>/profiles/<名字>/` 里有 config.yaml 的目录）；Hermes 自己怎么认的没有对着核。"""
    base = os.path.join(default_root(home), "profiles")
    out = []
    try:
        for n in sorted(os.listdir(base)):
            d = os.path.join(base, n)
            if not n.startswith(".") and os.path.isdir(d) and os.path.isfile(os.path.join(d, "config.yaml")):
                out.append((n, d))
    except OSError:
        pass
    return out


def _owner_of_path(p, leaf):
    """`<X>/yuanwei/<leaf>` 形状的路径返回 X；别的形状返回 None。"""
    if not isinstance(p, str) or not p:
        return None
    n = norm(p)
    d = os.path.dirname(n)
    if os.path.basename(n) == leaf and os.path.basename(d) == "yuanwei":
        return os.path.dirname(d)
    return None


def _inside(path, home):
    """path 在 home 目录里面，而且不在 home 下面的 profiles/ 里（主 Hermes 的家目录下面放着各个机器人的目录，那些不算主 Hermes 自己的）。"""
    p, h = norm(path), norm(home)
    if not (p == h or p.startswith(h.rstrip(os.sep) + os.sep)):
        return False
    rel = os.path.relpath(p, h).split(os.sep)
    return rel[0] != "profiles"


def home_check(cfg, actual_home):
    """这份设置是不是属于这个家目录。返回 {"ok", "state", "basis", "recorded_home", "actual_home"}。
    state（ok 为假时）：copied＝能认出是别的家目录的（记的家目录对不上，或库／配置在别的家目录的标准位置）｜ownership_unconfirmed＝归属没法确认（没记家目录，库又在自定义位置、
    路径也证明不了属于这个家目录）：这种**不启用**，但也不说成「确定复制来的」。
    basis：recorded_home（装的时候记了家目录）｜db_path（没记，库在 <X>/yuanwei/ 的标准位置）｜path_inside_home（没记，库在这个家目录自己的目录里面）｜
    ownership_unconfirmed｜no_library_path（设置里没有库路径，没有什么可保护的）｜not_checked（宿主没给家目录，没法比）。"""
    cfg = cfg or {}
    rec = cfg.get("hermes_home")
    rec = rec if isinstance(rec, str) and rec else None
    if not actual_home:
        return {"ok": True, "state": None, "basis": "not_checked", "recorded_home": rec, "actual_home": None}
    actual = norm(actual_home)
    if rec:
        same = norm(rec) == actual
        return {"ok": same, "state": None if same else "copied", "basis": "recorded_home", "recorded_home": rec, "actual_home": actual}
    owners = [o for o in (_owner_of_path(cfg.get("db"), "yuanwei.sqlite"), _owner_of_path(cfg.get("config_path"), "yuanwei.toml")) if o]
    if owners:
        same = all(o == actual for o in owners)
        return {"ok": same, "state": None if same else "copied", "basis": "db_path", "recorded_home": owners[0], "actual_home": actual}
    paths = [p for p in (cfg.get("db"), cfg.get("config_path")) if isinstance(p, str) and p]
    if not paths:
        return {"ok": True, "state": None, "basis": "no_library_path", "recorded_home": None, "actual_home": actual}
    if all(_inside(p, actual) for p in paths):
        return {"ok": True, "state": None, "basis": "path_inside_home", "recorded_home": None, "actual_home": actual}
    return {"ok": False, "state": UNCONFIRMED, "basis": "ownership_unconfirmed", "recorded_home": None, "actual_home": actual}


def copied_message(home):
    return ("这个机器人的设置是从别的 Hermes 复制来的，原委还没为它装；要用的话，对它的目录跑一次向导：yuanwei setup --hermes-home %s（装好以后它有自己单独的一个库）" % (home or "<它的目录>"))


def unconfirmed_message(home, db=None):
    """旧设置没记归属、库在自定义位置：不启用，要用户明确确认。不说成「确定是复制来的」。"""
    where = "（它指着 %s）" % db if db else ""
    h = home or "<这个家目录>"
    return ("这份旧设置的归属没法确认%s：它没有记录属于哪个家目录，库又在自定义位置，原委没法证明它属于这里，所以先不启用，免得悄悄用到别的档案的库。"
            "请对这个机器人（或主 Hermes）重新确认安装一次：要继续用那个库，运行 yuanwei setup --hermes-home %s%s；要这个目录用自己新的库，运行 yuanwei setup --hermes-home %s（旧库不会被动）。"
            % (where, h, " --db %s" % db if db else " --db <库文件>", h))


GROUP_ROOM_MESSAGE = "群聊里不用原委的记忆：这一版只管你和某一个机器人的一对一，群聊里的话不记也不查。想让它记住群里定的事，回到一对一里说一句。"
