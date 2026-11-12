"""Hermes 机器人（配置档案）各用各的库；群聊和机器人互发的消息明确不记不查（施工单 2026-11-11 晚）。
一律在临时目录里，不碰真实的 ~/.hermes。复制来的设置：不启用、不碰别的档案的库；为机器人单独装以后：库在它自己的目录下。"""
import json
import os
import shutil
import sqlite3

import pytest

from helpers2 import organized as _organized
from yuanwei import cli
from yuanwei import hermes_profile as HP
from yuanwei import hermes_setup as HS
from yuanwei.hermes_adapter import HermesAdapter

CFG_YAML = "model:\n  default: x\n"


def turn(a, n, text, **kw):
    kw.setdefault("author_id", None)
    kw.setdefault("author_name", None)
    kw.setdefault("author_is_bot", False)
    a.on_turn_start(n, text, **kw)


def call(a, name, args=None):
    return json.loads(a.handle_tool_call(name, args or {}))


def msgs(text):
    return [{"role": "user", "content": text}, {"role": "assistant", "content": "答"}]


def stored(mem):
    return [(r["role"], r["content"]) for r in mem.store.list_messages(mem.ns) if r["session_id"] == "hs1"]


def organized(p, **kw):
    p.mkdir(parents=True, exist_ok=True)
    return _organized(p, **kw)


def mk_home(p):
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text(CFG_YAML, encoding="utf-8")
    return p


def install(home, **kw):
    d = dict(hermes_home=str(home), db=None, mode="tools_only", src_path="/pkg/src", replace_provider=False, hermes_cmd=None, config_method="yaml")
    d.update(kw)
    return HS.apply_install(**d)


def tree(h):
    """h 下所有文件的（大小，修改时间）；机器人的目录嵌在 profiles/ 里，不算进主 Hermes 自己的。"""
    out = {}
    for root, _d, files in os.walk(h):
        if os.path.relpath(root, h).split(os.sep)[0] == "profiles":
            continue
        for f in files:
            p = os.path.join(root, f)
            st = os.stat(p)
            out[os.path.relpath(p, h)] = (st.st_size, st.st_mtime_ns)
    return out


class NeverOpen:
    """记忆工厂：只要有人想为这一份设置打开库，就失败。"""
    def __call__(self, key):
        raise AssertionError("复制来的设置不该打开任何库")


def adapter_for(cfg, home, platform="cli", **kw):
    a = HermesAdapter(dict(cfg, spawn="off"), memory_factory=NeverOpen(), executor="inline")
    a.initialize("hs1", platform=platform, agent_context="primary", hermes_home=str(home), **kw)
    return a


# ================================================================ 1 认出复制来的设置
def test_recorded_home_matching_keeps_the_default_profile_active(tmp_path):
    home = mk_home(tmp_path / "hh")
    mem = organized(tmp_path / "m", host="hermes")
    a = HermesAdapter({"spawn": "off", "hermes_home": str(home), "db": str(home / "yuanwei" / "yuanwei.sqlite")}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(home))
    st = a.status()
    assert st["active"] is True and st["inactive_reason"] is None and st["profile"]["basis"] == "recorded_home" and st["profile"]["ok"] is True
    turn(a, 1, "吃了什么烤鱼")
    a.sync_turn("吃了什么烤鱼", "烤鱼", session_id="hs1", messages=msgs("吃了什么烤鱼"))
    assert ("user", "吃了什么烤鱼") in stored(mem)


def test_recorded_home_of_another_profile_is_not_activated_and_never_touches_that_profiles_library(tmp_path):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    db = default / "yuanwei" / "yuanwei.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    before = tree(default)
    a = adapter_for({"hermes_home": str(default), "db": str(db), "mode": "tools_only"}, bot)
    st = a.status()
    assert st["active"] is False and st["inactive_reason"] == "copied_from_other_profile" and st["profile"]["ok"] is False
    assert "从别的 Hermes 复制来的" in st["inactive_message"] and "yuanwei setup --hermes-home %s" % bot in st["inactive_message"]
    turn(a, 1, "机器人里的一句话")
    a.sync_turn("机器人里的一句话", "答", session_id="hs1", messages=msgs("机器人里的一句话"))
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] == "copied_from_other_profile" and "复制来的" in r["text"]
    assert a.get_tool_schemas() == []
    a.shutdown()
    after = tree(default)
    assert after == before                                                                     # 别的档案的目录一个字节没动（库没打开、没建）
    assert not os.path.exists(str(db) + "-wal") and os.path.getsize(db) == 0


def test_old_config_without_recorded_home_is_judged_by_where_its_library_lives(tmp_path):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    other_db = str(default / "yuanwei" / "yuanwei.sqlite")
    own_db = str(bot / "yuanwei" / "yuanwei.sqlite")
    a = adapter_for({"db": other_db}, bot)                                                      # 装在这一版之前、没有 hermes_home：库指着别的档案
    assert a.status()["inactive_reason"] == "copied_from_other_profile" and a.status()["profile"]["basis"] == "db_path"
    mem = organized(tmp_path / "m", host="hermes")
    b = HermesAdapter({"spawn": "off", "db": own_db}, memory=mem, executor="inline")
    b.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(bot))
    assert b.status()["active"] is True and b.status()["profile"]["basis"] == "db_path"
    inside = HermesAdapter({"spawn": "off", "db": str(bot / "data" / "mine.sqlite")}, memory=mem, executor="inline")           # 没记家目录、库在这个家目录自己的目录里：路径能证明属于这里
    inside.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(bot))
    assert inside.status()["active"] is True and inside.status()["profile"]["basis"] == "path_inside_home"
    recorded = HermesAdapter({"spawn": "off", "hermes_home": str(bot), "db": str(tmp_path / "custom" / "x.sqlite")}, memory=mem, executor="inline")   # 已经登记了归属的自定义库：照常用
    recorded.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(bot))
    assert recorded.status()["active"] is True and recorded.status()["profile"]["basis"] == "recorded_home"


# ================================================================ R2：旧设置没记归属、库在自定义位置：不启用，要明确确认
def _unconfirmed_cfg(tmp_path):
    """主 Hermes 的老设置：没有 hermes_home，库和配置都在自定义位置（不在任何 <X>/yuanwei/ 标准位置，也不在任何家目录里面）。"""
    return {"db": str(tmp_path / "elsewhere" / "main.sqlite"), "config_path": str(tmp_path / "elsewhere" / "main.toml"), "mode": "tools_only", "spawn": "off"}


def test_r2_old_custom_library_config_copied_to_another_profile_is_not_enabled_and_never_touches_the_library(tmp_path):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    cfg = _unconfirmed_cfg(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "main.sqlite").write_bytes(b"")
    before = tree(tmp_path / "elsewhere")
    a = adapter_for(cfg, bot)
    st = a.status()
    assert st["active"] is False and st["inactive_reason"] == "ownership_unconfirmed" and st["profile"]["basis"] == "ownership_unconfirmed"
    assert "归属没法确认" in st["inactive_message"] and "复制来的" not in st["inactive_message"] and cfg["db"] in st["inactive_message"]       # 不说成「确定复制来的」
    assert "yuanwei setup --hermes-home %s --db %s" % (bot, cfg["db"]) in st["inactive_message"] and "yuanwei setup --hermes-home %s（旧库不会被动）" % bot in st["inactive_message"]
    turn(a, 1, "一句话")
    a.sync_turn("一句话", "答", session_id="hs1", messages=msgs("一句话"))
    r = call(a, "recall", {"query": "一句话"})
    assert r["status"] == "refused" and r["reason"] == "ownership_unconfirmed" and a.get_tool_schemas() == []
    a.shutdown()
    assert tree(tmp_path / "elsewhere") == before and (tmp_path / "elsewhere" / "main.sqlite").read_bytes() == b""     # 原库没被碰
    same = adapter_for(cfg, default)                                                                                 # 主 Hermes 自己用这份旧设置：同样先确认（没法证明是它的）
    assert same.status()["inactive_reason"] == "ownership_unconfirmed"


def test_r2_a_custom_path_inside_a_bots_directory_does_not_prove_the_main_hermes_owns_it(tmp_path):
    default = mk_home(tmp_path / "hh")
    mk_home(default / "profiles" / "photograph")
    a = adapter_for({"db": str(default / "profiles" / "photograph" / "data" / "x.sqlite"), "spawn": "off"}, default)
    assert a.status()["inactive_reason"] == "ownership_unconfirmed"                                              # 在主 Hermes 的家目录下面，但是在某个机器人的目录里


def test_r2_cli_entry_points_agree_status_installed_and_default_database(tmp_path, capsys):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    cfg = _unconfirmed_cfg(tmp_path)
    (bot / "yuanwei").mkdir()
    (bot / "yuanwei" / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "main.sqlite").write_bytes(b"")
    assert cli._installed(str(bot)) is None
    st = HS.install_status(str(bot))
    assert st["ownership_unconfirmed"] is True and st["config"].get("db") is None and st["profile"]["state"] == "ownership_unconfirmed" and not st.get("copied_from_other_profile")
    assert any("归属没法确认" in n for n in st["notes"])
    rc = cli.main(["conversations", "list", "--hermes-home", str(bot)])                                           # 默认库：不去碰设置里的自定义库
    err = capsys.readouterr().err
    assert rc != 0 and "ownership_unconfirmed" in err and "归属没法确认" in err


def test_r2_explicit_confirmation_registers_the_home_and_then_it_can_be_used(tmp_path):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    cfg = _unconfirmed_cfg(tmp_path)
    (bot / "yuanwei").mkdir()
    (bot / "yuanwei" / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "elsewhere").mkdir()
    custom_db = str(tmp_path / "elsewhere" / "main.sqlite")
    plan = HS.plan_install(**dict(PLAN_KW, hermes_home=str(bot), db=custom_db))                                   # 用户明确要继续用自定义库
    assert any("没有记录它属于哪个家目录" in n and "你用 --db 指定的库" in n for n in plan["notes"]) and not [a for a in plan["actions"] if a["kind"] == "move_copied_library"]
    plan2 = HS.plan_install(**dict(PLAN_KW, hermes_home=str(bot)))                                                # 没给 --db：换成自己的新库，旧库不动，计划里写明
    assert any("改用这个目录自己的新库" in n and "旧库不会被动" in n and "--db %s" % custom_db in n for n in plan2["notes"])
    install(bot, db=custom_db)
    c2 = json.loads((bot / "yuanwei" / "config.json").read_text())
    assert c2["hermes_home"] == HP.norm(bot) and c2["db"] == custom_db
    mem = organized(tmp_path / "m", host="hermes")
    a = HermesAdapter(dict(c2, spawn="off"), memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(bot))
    assert a.status()["active"] is True and a.status()["profile"]["basis"] == "recorded_home"
    assert cli._installed(str(bot)) is not None and not HS.install_status(str(bot)).get("ownership_unconfirmed")
    other = adapter_for(c2, default)                                                                              # 登记以后复制到别的档案：现在认得出是复制来的（不再是「没法确认」）
    assert other.status()["inactive_reason"] == "copied_from_other_profile"


def test_when_the_host_gives_no_home_the_home_the_plugin_loaded_the_config_from_is_used(tmp_path):
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    a = HermesAdapter({"spawn": "off", "hermes_home": str(default), "_loaded_from": str(bot)}, memory_factory=NeverOpen(), executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary")
    assert a.status()["inactive_reason"] == "copied_from_other_profile"


def test_profile_name_and_listing_follow_the_directory_shape(tmp_path):
    root = mk_home(tmp_path / "hh")
    mk_home(root / "profiles" / "photograph")
    mk_home(root / "profiles" / "writer")
    (root / "profiles" / ".deleted").mkdir()
    (root / "profiles" / "no-config").mkdir()
    assert HP.profile_name(root / "profiles" / "photograph") == "photograph" and HP.profile_name(root) is None and HP.profile_name(root / "profiles" / ".deleted") is None
    assert [n for n, _d in HP.list_profiles(root)] == ["photograph", "writer"] and [n for n, _d in HP.list_profiles(root / "profiles" / "writer")] == ["photograph", "writer"]


# ================================================================ 2 为机器人单独装
def test_installing_for_a_copied_bot_gives_it_its_own_library_and_leaves_the_default_alone(tmp_path):
    default = mk_home(tmp_path / "hh")
    install(default)
    dcfg = json.loads((default / "yuanwei" / "config.json").read_text())
    assert dcfg["hermes_home"] == HP.norm(default) and dcfg["db"] == str(default / "yuanwei" / "yuanwei.sqlite")      # 装的时候记下了家目录
    (default / "yuanwei" / "yuanwei.sqlite").write_bytes(b"")
    bot = tmp_path / "hh" / "profiles" / "photograph"                                           # Hermes 新建机器人：整份复制设置
    bot.mkdir(parents=True)
    for rel in ("plugins", "yuanwei", "config.yaml"):
        src = default / rel
        shutil.copytree(src, bot / rel) if src.is_dir() else shutil.copy2(src, bot / rel)
    (bot / "yuanwei" / "yuanwei.sqlite").unlink()                                               # 这一条测的是「没带着库文件」；带着库的情形下面单测
    default_before = tree(default)
    assert HS.copied_info(bot) is not None and cli._installed(str(bot)) is None
    status = HS.install_status(str(bot))
    assert status["copied_from_other_profile"] is True and status["config"].get("db") is None and any("复制来的" in n for n in status["notes"])
    plan = HS.plan_install(**dict(hermes_home=str(bot), db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml"))
    assert plan["profile"] == "photograph" and plan["copied_from"] == HP.norm(default) and any("这是给机器人 photograph 装" in n and "单独的一个库" in n for n in plan["notes"])
    assert plan["actions"] and all(a["path"].startswith(str(bot)) for a in plan["actions"] if a["kind"] == "write_file")
    install(bot)
    cfg = json.loads((bot / "yuanwei" / "config.json").read_text())
    assert cfg["hermes_home"] == HP.norm(bot) and cfg["db"] == str(bot / "yuanwei" / "yuanwei.sqlite")
    assert [p for p in os.listdir(bot / "yuanwei") if p.startswith("config.json.yuanwei-backup-")]    # 复制来的 config.json 先备份
    assert HS.copied_info(bot) is None and cli._installed(str(bot)) is not None
    assert tree(default) == default_before                                                      # 默认档案没被碰
    a = HermesAdapter(dict(cfg, spawn="off"), executor="inline", memory_factory=lambda key: organized(tmp_path / "m2", host="hermes"))
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(bot))
    assert a.status()["active"] is True and a.status()["profile"]["ok"] is True


def _copied_bot(tmp_path, with_library=True, extras=True):
    """主 Hermes 装好以后，Hermes 新建机器人把设置整份复制过去（--clone 连 yuanwei/ 目录里的库文件也带过去）。返回 (默认, 机器人)。"""
    default = mk_home(tmp_path / "hh")
    install(default)
    bot = tmp_path / "hh" / "profiles" / "photograph"
    shutil.copytree(default / "plugins", bot / "plugins")
    shutil.copytree(default / "yuanwei", bot / "yuanwei")
    shutil.copy2(default / "config.yaml", bot / "config.yaml")
    if with_library:
        lib = bot / "yuanwei" / "yuanwei.sqlite"
        lib.write_bytes(b"main-hermes-memories")
        os.chmod(lib, 0o640)
        if extras:
            (bot / "yuanwei" / "yuanwei.sqlite-wal").write_bytes(b"wal")
            (bot / "yuanwei" / "yuanwei.sqlite-shm").write_bytes(b"shm")
            (bot / "yuanwei" / "yuanwei.sqlite.d").mkdir()
            (bot / "yuanwei" / "yuanwei.sqlite.d" / "x.json").write_text("{}")
            (bot / "yuanwei" / "yuanwei.sqlite.cost-pending.jsonl").write_text("{}\n")
    return default, bot


PLAN_KW = dict(db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml")


def test_plan_lists_the_copied_library_as_not_kept_and_moves_nothing_until_confirmed(tmp_path):
    default, bot = _copied_bot(tmp_path)
    before = tree(bot)
    plan = HS.plan_install(hermes_home=str(bot), **PLAN_KW)                                      # 只列计划（没确认）
    act = [a for a in plan["actions"] if a["kind"] == "move_copied_library"]
    assert len(act) == 1 and act[0]["files"] == ["yuanwei.sqlite", "yuanwei.sqlite-shm", "yuanwei.sqlite-wal", "yuanwei.sqlite.cost-pending.jsonl", "yuanwei.sqlite.d"]
    assert act[0]["source"] == "hh" and act[0]["backup_pattern"] == "<原名>.copied-from-hh-<时间>"
    assert any("这个机器人目录里有一份从别处复制来的记忆库" in n and "不沿用" in n and ".copied-from-hh-<时间>" in n and "你自己删" in n for n in plan["notes"])
    assert tree(bot) == before and (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"main-hermes-memories"       # 计划阶段不动
    from helpers_wizard import FakeHTTP, FakeIO
    from test_vector_setup import BASE, mk_sys
    import contextlib as _c
    args = ["setup", "--host", "hermes", "--preset", "light", "--hermes-home", str(bot)] + BASE + ["--vectors", "none"]
    assert cli._setup_main(args, io=FakeIO(tty=False), sys_=mk_sys(FakeHTTP("deepseek"))) == 0                              # 没加 --yes：命令行只列计划
    assert (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"main-hermes-memories" and not [n for n in os.listdir(bot / "yuanwei") if ".copied-from-" in n]


def test_wizard_declined_plan_leaves_the_copied_library_where_it_is_and_confirmed_plan_moves_it(tmp_path):
    from helpers_wizard import FakeSystem, FAKE_KEY, FakeIO
    default, bot = _copied_bot(tmp_path)
    s = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY}, commands={"hermes": lambda a: (0, "", "")})
    base = ["setup", "--hermes-home", str(bot), "--config-method", "yaml", "--src-path", "/x"]
    io = FakeIO(["1", "2", "y", "y", "1", "y", "n"])                                             # 轻量预设走到最后的「确认执行吗」，答 n
    assert cli._setup_main(base, io=io, sys_=s) == 1
    assert "这个机器人目录里有一份从别处复制来的记忆库" in io.text and "不沿用" in io.text and "已取消，什么都没改" in io.text    # 走到了计划、答了不确认
    assert (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"main-hermes-memories" and not [n for n in os.listdir(bot / "yuanwei") if ".copied-from-" in n]
    io2 = FakeIO(["1", "2", "y", "y", "1", "y", "y"])                                            # 这次确认
    assert cli._setup_main(base, io=io2, sys_=s) == 0
    assert not (bot / "yuanwei" / "yuanwei.sqlite").exists() or (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() != b"main-hermes-memories"
    assert [n for n in os.listdir(bot / "yuanwei") if n.startswith("yuanwei.sqlite.copied-from-hh-")]


def test_confirmed_install_moves_the_copied_library_and_all_its_companions_aside_and_starts_empty(tmp_path):
    default, bot = _copied_bot(tmp_path)
    default_before = tree(default)
    r = install(bot)
    names = sorted(os.listdir(bot / "yuanwei"))
    stamp = [n for n in names if n.startswith("yuanwei.sqlite.copied-from-hh-")]
    assert len(stamp) == 1 and (bot / "yuanwei" / stamp[0]).read_bytes() == b"main-hermes-memories"            # 内容原样、没删
    assert oct(os.stat(bot / "yuanwei" / stamp[0]).st_mode & 0o777) == "0o640"                       # 权限照旧
    for companion in ("yuanwei.sqlite-wal", "yuanwei.sqlite-shm", "yuanwei.sqlite.d", "yuanwei.sqlite.cost-pending.jsonl"):
        assert companion not in names and any(n.startswith(companion + ".copied-from-hh-") for n in names), companion
    assert any(n.startswith("yuanwei.sqlite.d.copied-from-hh-") and os.path.isdir(bot / "yuanwei" / n) and os.path.exists(bot / "yuanwei" / n / "x.json") for n in names)
    assert "yuanwei.sqlite" not in names                                                         # 机器人从空库开始（库文件还没建）
    assert any(a["kind"] == "move_copied_library" and a["moved"] for a in r["plan"]["actions"])
    cfg = json.loads((bot / "yuanwei" / "config.json").read_text())
    assert cfg["hermes_home"] == HP.norm(bot) and cfg["db"] == str(bot / "yuanwei" / "yuanwei.sqlite")
    assert tree(default) == default_before                                                       # 主 Hermes 一个字节没动
    again = HS.plan_install(hermes_home=str(bot), **PLAN_KW)                                     # 再装一次：这次库已经是它自己的（没有复制来的库），不再挪
    assert not [a for a in again["actions"] if a["kind"] == "move_copied_library"]


def test_no_library_copied_means_no_move_and_own_library_or_custom_db_is_never_moved(tmp_path):
    default, bot = _copied_bot(tmp_path, with_library=False)
    assert not [a for a in HS.plan_install(hermes_home=str(bot), **PLAN_KW)["actions"] if a["kind"] == "move_copied_library"]
    install(bot)
    assert not [n for n in os.listdir(bot / "yuanwei") if ".copied-from-" in n]
    (bot / "yuanwei" / "yuanwei.sqlite").write_bytes(b"own-memories")                           # 它自己的库（设置已经是它自己的）：重装不挪
    install(bot)
    assert (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"own-memories"
    default2, bot2 = _copied_bot(tmp_path / "second")
    custom = tmp_path / "second" / "custom.sqlite"
    custom.write_bytes(b"user-chosen")
    plan = HS.plan_install(hermes_home=str(bot2), **dict(PLAN_KW, db=str(custom)))               # 用户自己用 --db 指定的库：不动
    assert not [a for a in plan["actions"] if a["kind"] == "move_copied_library"]
    assert (bot2 / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"main-hermes-memories"


def test_a_move_that_fails_is_rolled_back_and_nothing_else_is_changed(tmp_path, monkeypatch):
    default, bot = _copied_bot(tmp_path)
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise PermissionError("no")
        return real(src, dst)
    cfg_before = (bot / "yuanwei" / "config.json").read_bytes()
    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(HS.SetupRefused) as e:
        install(bot)
    monkeypatch.setattr(os, "rename", real)
    assert e.value.code == "copied_library_move_failed"
    assert not [n for n in os.listdir(bot / "yuanwei") if ".copied-from-" in n] and (bot / "yuanwei" / "yuanwei.sqlite").read_bytes() == b"main-hermes-memories"
    assert (bot / "yuanwei" / "config.json").read_bytes() == cfg_before                          # 设置没改


def test_default_profile_install_is_unchanged_and_idempotent(tmp_path):
    default = mk_home(tmp_path / "hh")
    install(default)
    assert HS.plan_install(**dict(hermes_home=str(default), db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml"))["actions"] == []
    plan = HS.plan_install(**dict(hermes_home=str(default), db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml"))
    assert plan["profile"] is None and plan["copied_from"] is None and not any("机器人" in n for n in plan["notes"])


def test_wizard_plan_says_it_is_for_the_bot_and_lists_profiles_to_choose_from(tmp_path):
    from helpers_wizard import FakeIO
    from yuanwei import wizard as WZ
    root = mk_home(tmp_path / "hh")
    bot = mk_home(root / "profiles" / "photograph")
    io = FakeIO(["2"])
    args = type("A", (), {"hermes_home": None})()
    assert WZ.choose_home(args, io, str(root)) == str(bot) and "机器人 photograph" in io.text and "主 Hermes" in io.text
    assert WZ.choose_home(type("A", (), {"hermes_home": str(root)})(), FakeIO([]), str(root)) == str(root)                 # 明确指定了：不问
    assert WZ.choose_home(args, FakeIO([]), str(bot)) == str(bot)                                                          # 已经在某个机器人的环境里：不问
    assert WZ.choose_home(args, FakeIO([]), str(mk_home(tmp_path / "alone"))) == str(tmp_path / "alone")                  # 没有机器人：不问


# ================================================================ 3 群聊明确不记不查
@pytest.mark.parametrize("owners", [{}, {"bot_room": ["u1"]}])
def test_group_room_is_explicitly_neither_recorded_nor_read_even_with_owners_configured(tmp_path, owners):
    mem = organized(tmp_path / "m", host="hermes")
    a = HermesAdapter({"spawn": "off", "owners": owners}, memory=mem, executor="inline")
    a.initialize("hs1", platform="bot_room", agent_context="primary", hermes_home=str(tmp_path / "home"), user_id="u1")
    st = a.status()
    assert st["active"] is False and st["inactive_reason"] == "group_room_not_supported" and "群聊里不用原委的记忆" in st["inactive_message"]
    turn(a, 1, "User (user): 群里说的话\n@alice: 好", author_id="u1")
    a.sync_turn("User (user): 群里说的话", "答", session_id="hs1", messages=msgs("User (user): 群里说的话"))
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused" and r["reason"] == "group_room_not_supported" and "群聊里不用原委的记忆" in r["text"]
    assert a.get_tool_schemas() == [] and stored(mem) == [] and a.status()["recorded_turns"] == 0
    assert a._owner_check("u1") == (False, "group_room_not_supported")                          # 兜底：就算以后别处调到，也不放行


# ================================================================ 4 机器人互发：读的一侧也拒绝
def test_bot_initiated_turn_cannot_read_even_after_an_admitted_turn_in_the_same_session(tmp_path):
    mem = organized(tmp_path / "m", host="hermes")
    a = HermesAdapter({"spawn": "off"}, memory=mem, executor="inline")
    a.initialize("hs1", platform="cli", agent_context="primary", hermes_home=str(tmp_path / "home"))
    turn(a, 1, "主人问烤鱼")
    assert call(a, "recall", {"query": "烤鱼"})["status"] != "refused"                         # 对照：用户本人的这一轮读得到
    turn(a, 2, "机器人发来的问题", author_id="bot:x", author_name="x", author_is_bot=True)
    r = call(a, "recall", {"query": "烤鱼"})
    assert r["status"] == "refused"
    r2 = call(a, "get_messages", {"ids": ["S1-1"]})
    assert r2["status"] == "refused"
    a.sync_turn("机器人发来的问题", "答", session_id="hs1", messages=msgs("机器人发来的问题"), turn_author={"id": "bot:x", "name": "x", "is_bot": True})
    assert ("user", "机器人发来的问题") not in stored(mem)


# ================================================================ 向导：给机器人装的计划和实际安装
def test_wizard_plan_and_apply_for_a_bot_say_so_and_put_everything_under_its_own_directory(tmp_path, capsys):
    from helpers_wizard import FakeHTTP, FakeIO, FakeSystem, make_home
    from test_vector_setup import BASE, mk_sys
    root = make_home(tmp_path, "hh")
    bot = os.path.join(root, "profiles", "photograph")
    make_home(tmp_path / "hh" / "profiles", "photograph")
    s = mk_sys(FakeHTTP("deepseek"))
    args = ["setup", "--host", "hermes", "--preset", "light", "--hermes-home", bot] + BASE + ["--vectors", "none"]
    assert cli._setup_main(args, io=FakeIO(tty=False), sys_=s) == 0
    out = json.loads(capsys.readouterr().out)
    lines = "\n".join(out["plan"]["lines"])
    assert out["applied"] is False and "这是给机器人 photograph 装：它会有自己单独的一个库" in lines and "群聊、机器人之间互发的消息不记也不查" in lines
    assert not os.path.exists(os.path.join(bot, "yuanwei"))                                      # 只列计划，什么都没写
    assert cli._setup_main(args + ["--yes"], io=FakeIO(tty=False), sys_=s) == 0
    capsys.readouterr()
    cfg = json.load(open(os.path.join(bot, "yuanwei", "config.json")))
    assert cfg["hermes_home"] == HP.norm(bot) and cfg["db"] == os.path.join(bot, "yuanwei", "yuanwei.sqlite") and cfg["config_path"] == os.path.join(bot, "yuanwei", "yuanwei.toml")
    assert not os.path.exists(os.path.join(root, "yuanwei"))                                      # 主 Hermes 的目录里没有任何原委文件


def test_installed_plugin_file_refuses_to_run_on_a_copied_config_and_touches_no_library(tmp_path, monkeypatch):
    from test_hermes_adapter import load_plugin
    from test_hermes_compat import BASE as FAKE_BASE_SOURCE                                      # 假的 MemoryProvider 基类（沙箱里读不了 ~/.hermes，也不该读）
    base_path = tmp_path / "memory_provider.py"
    base_path.write_text(FAKE_BASE_SOURCE)
    base, ns = load_plugin(str(base_path), tmp_path)
    default = mk_home(tmp_path / "hh")
    bot = mk_home(tmp_path / "hh" / "profiles" / "photograph")
    db = default / "yuanwei" / "yuanwei.sqlite"
    (bot / "yuanwei").mkdir()
    (bot / "yuanwei" / "config.json").write_text(json.dumps({"hermes_home": str(default), "db": str(db), "mode": "tools_only", "spawn": "off",
                                                              "src_path": os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))}))
    monkeypatch.setenv("HERMES_HOME", str(bot))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    for kw in ({"hermes_home": str(bot)}, {}):                                                   # 宿主给了家目录／没给（插件用读到设置的那个目录）
        prov = ns.YuanweiMemoryProvider()
        assert prov.is_available() is True
        assert sorted(s["name"] for s in prov.get_tool_schemas())                               # 初始化之前仍交固定清单（宿主在 add_provider 里就要）
        prov.initialize("hs1", platform="cli", agent_context="primary", **kw)
        st = prov.status()
        assert st["active"] is False and st["inactive_reason"] == "copied_from_other_profile" and "复制来的" in st["inactive_message"]
        assert prov.get_tool_schemas() == []                                                     # 初始化以后不交工具
        prov.on_turn_start(1, "一句话", author_id=None, author_name=None, author_is_bot=False)
        prov.sync_turn("一句话", "答", session_id="hs1", messages=msgs("一句话"))
        out = json.loads(prov.handle_tool_call("recall", {"query": "一句话"}))
        assert out["status"] == "refused" and out["reason"] == "copied_from_other_profile"
        prov.shutdown()
    assert not db.exists() and not (default / "yuanwei").exists()                              # 别的档案的库没被建出来


def test_docs_say_each_bot_has_its_own_library_needs_its_own_install_and_group_chat_is_not_recorded():
    root = os.path.join(os.path.dirname(__file__), "..")
    guide = open(os.path.join(root, "docs", "装到Hermes上.md"), encoding="utf-8").read()
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    rules = open(os.path.join(root, "docs", "产品规则表.md"), encoding="utf-8").read()
    for t in (guide, readme):
        assert "新建的机器人要单独装一次" in t or "新建的机器人要单独装" in t or "Hermes 里新建的机器人要单独装一次" in t
    assert "yuanwei setup --hermes-home ~/.hermes/profiles/<机器人名>" in guide and "group_room_not_supported" in guide and "回到一对一里说一句" in guide and "回到一对一里说一句" in readme
    assert "## 19 每个机器人一个库" in rules and "copied_from_other_profile" in rules and "bot_room" in rules
    assert "向导里所有标「未核」的东西" not in guide                                              # 旧的过时说法已按试装记录改准


def test_interactive_wizard_without_hermes_home_asks_which_one_and_installs_for_the_chosen_bot(tmp_path, monkeypatch):
    from helpers_wizard import FakeSystem, FAKE_KEY, FakeIO
    root = mk_home(tmp_path / "hh")
    bot = mk_home(root / "profiles" / "photograph")
    monkeypatch.setenv("HERMES_HOME", str(root))
    s = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY}, commands={"hermes": lambda a: (0, "", "")})
    io = FakeIO(["2", "1", "2", "y", "y", "1", "y", "y"])                                        # 第一问：装给哪一个（2＝机器人 photograph）；后面和平常一样
    assert cli._setup_main(["setup", "--config-method", "yaml", "--src-path", "/x"], io=io, sys_=s) == 0
    assert "机器人 photograph" in io.text and "这是给机器人 photograph 装" in io.text
    assert (bot / "yuanwei" / "config.json").exists() and not (root / "yuanwei").exists()


# ================================================================ R3：只剩复制来的待补目录（没有库文件本身）也要挪走
def _pending_dir_only(bot):
    """造一个真的待补目录：在机器人的默认库位置开一个库，往 life 的待补文件里追加一条原话，关掉，再把库文件本身删掉（只剩 .d）。"""
    from yuanwei import Memory
    from yuanwei.config import Config
    db = str(bot / "yuanwei" / "yuanwei.sqlite")
    m = Memory.open(db, "life", config=Config(), spawn="off")
    m._append_pending({"host": "hermes", "session_id": "old", "branch": "", "message_id": "o1", "role": "user", "text": "主 Hermes 里说过的一句话", "time": "2026-11-01", "time_precision": "day",
                       "stable_id": None, "turn_id": None, "capture": None, "received_at": "2026-11-01T00:00:00Z"})
    assert m.pending_count() == 1
    m.close()
    for n in os.listdir(bot / "yuanwei"):
        if n.startswith("yuanwei.sqlite") and not n.endswith(".d"):
            os.unlink(bot / "yuanwei" / n)
    assert sorted(n for n in os.listdir(bot / "yuanwei") if n.startswith("yuanwei.sqlite")) == ["yuanwei.sqlite.d"]
    return db


def test_r3_only_a_pending_directory_is_moved_aside_and_not_replayed_into_the_new_library(tmp_path):
    from yuanwei import Memory
    from yuanwei.config import Config
    default, bot = _copied_bot(tmp_path, with_library=False)
    db = _pending_dir_only(bot)
    default_before = tree(default)
    plan = HS.plan_install(hermes_home=str(bot), **PLAN_KW)
    act = [a for a in plan["actions"] if a["kind"] == "move_copied_library"]
    assert len(act) == 1 and act[0]["files"] == ["yuanwei.sqlite.d"] and act[0]["main_present"] is False
    assert any("只剩待补文件等遗留，没有库文件本身" in n for n in plan["notes"])
    install(bot)
    names = os.listdir(bot / "yuanwei")
    assert "yuanwei.sqlite.d" not in names and any(n.startswith("yuanwei.sqlite.d.copied-from-hh-") for n in names)
    backup = [n for n in names if n.startswith("yuanwei.sqlite.d.copied-from-hh-")][0]
    assert any(f.startswith("pending-") for f in os.listdir(bot / "yuanwei" / backup))                         # 旧的待补文件在备份位置，没删
    m = Memory.open(db, "life", config=Config(), spawn="off")                                              # 新库第一次打开：不会补进旧消息
    try:
        assert m.pending_count() == 0 and [r for r in m.store.list_messages(m.ns)] == []
    finally:
        m.close()
    assert tree(default) == default_before                                                                  # 来源档案不变


def test_r3_no_leftovers_installs_as_usual_and_a_missing_directory_is_no_leftover(tmp_path):
    default, bot = _copied_bot(tmp_path, with_library=False)
    assert not [a for a in HS.plan_install(hermes_home=str(bot), **PLAN_KW)["actions"] if a["kind"] == "move_copied_library"]
    install(bot)
    assert not [n for n in os.listdir(bot / "yuanwei") if ".copied-from-" in n]
    assert HS.copied_library_files(str(tmp_path / "no-such-dir" / "yuanwei.sqlite")) == []                  # 目录确实不存在：没有遗留


def test_r3_a_directory_that_cannot_be_listed_is_not_taken_for_empty_and_refuses_before_changing_anything(tmp_path, monkeypatch):
    default, bot = _copied_bot(tmp_path, with_library=False)
    cfg_before = (bot / "yuanwei" / "config.json").read_bytes()
    real = os.listdir

    def flaky(p="."):
        if os.path.abspath(str(p)) == str(bot / "yuanwei"):
            raise PermissionError(13, "denied")
        return real(p)
    monkeypatch.setattr(os, "listdir", flaky)
    with pytest.raises(HS.SetupRefused) as e:
        install(bot)
    monkeypatch.setattr(os, "listdir", real)
    assert e.value.code == "copied_library_unreadable" and "没法确认" in str(e.value) and "PermissionError" in str(e.value)
    assert (bot / "yuanwei" / "config.json").read_bytes() == cfg_before                                      # 设置没有重新绑定


# ================================================================ 建议 1：挪库回滚也失败时如实说
def test_a_move_that_fails_and_whose_rollback_also_fails_says_where_the_files_are(tmp_path, monkeypatch):
    default, bot = _copied_bot(tmp_path)
    real = os.rename
    calls = {"n": 0}

    def hostile(src, dst):
        calls["n"] += 1
        if calls["n"] == 3 or calls["n"] > 3:                                                                # 第 3 次挪失败；之后的挪回去也全失败
            raise PermissionError(13, "denied")
        return real(src, dst)
    cfg_before = (bot / "yuanwei" / "config.json").read_bytes()
    monkeypatch.setattr(os, "rename", hostile)
    with pytest.raises(HS.SetupRefused) as e:
        install(bot)
    monkeypatch.setattr(os, "rename", real)
    msg = str(e.value)
    assert e.value.code == "copied_library_move_failed_rollback_incomplete" and "没有回滚完" in msg and "什么都没改" not in msg
    moved = [n for n in os.listdir(bot / "yuanwei") if ".copied-from-hh-" in n]
    assert moved and all(n in msg for n in moved)                                                            # 回执里写了每个还在备份名下的文件
    assert "yuanwei.sqlite → " in msg or "yuanwei.sqlite" in msg
    assert (bot / "yuanwei" / "config.json").read_bytes() == cfg_before and "设置没有改" in msg


def test_docs_describe_the_unconfirmed_old_config_and_the_leftover_directory_rules():
    root = os.path.join(os.path.dirname(__file__), "..", "docs")
    guide = open(os.path.join(root, "装到Hermes上.md"), encoding="utf-8").read()
    rules = open(os.path.join(root, "产品规则表.md"), encoding="utf-8").read()
    assert "ownership_unconfirmed" in guide and "不是「确定复制来的」" in guide and "ownership_unconfirmed" in rules
    assert "不以库文件本身存在为前提" in rules and "copied_library_unreadable" in rules and "没有回滚完" in rules
