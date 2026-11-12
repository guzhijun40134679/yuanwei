"""整理密钥改成全局一份（2026-11-12）：~/.yuanwei/keys/<变量名> 几个机器人共用；这个机器人自己的文件（<hermes 家目录>/yuanwei/keys/）单独给它设、优先；
读的先后：自己的 → 全局 → 环境变量；配置里没有 key_file 的不看两个文件。任何测试都不许读写真的 ~/.yuanwei/keys：HOME 由 conftest 指到临时目录。"""
import json
import os
import pathlib
import stat

import pytest

import conftest
from helpers_wizard import FAKE_KEY, FakeIO, FakeSystem, make_home
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei import keystore as KS
from yuanwei import wizard as WZ
from yuanwei.config import Config, api_key
from yuanwei.errors import ConfigError
from yuanwei.provider import OpenAICompatProvider

ENV = "DEEPSEEK_API_KEY"
BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
K_OWN, K_GLOBAL, K_ENV = "sk-OWNKEY00000000aaaaaaaa", "sk-GLOBALKEY000000bbbbbbbb", "sk-ENVKEY00000000cccccccc"


def _install(h, env=None):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=FakeSystem(env=env or {})) == 0


def _status(h, capsys, env=None):
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env=env or {})) == 0
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- 隔离
def test_global_key_dir_lives_in_the_temp_home_and_ignores_the_real_environment():
    d = KS.global_key_dir()
    assert d == os.path.join(os.environ["HOME"], ".yuanwei", "keys")
    real = os.path.join(conftest.REAL_HOME, ".yuanwei")
    assert os.path.realpath(os.environ["HOME"]) != os.path.realpath(conftest.REAL_HOME)
    assert not os.path.realpath(d).startswith(os.path.realpath(real))
    assert "YUANWEI_KEY_DIR" not in os.environ                                        # conftest 去掉了真实环境里的设置


def test_yuanwei_key_dir_moves_the_global_directory_like_the_engine_dir(tmp_path):
    other = str(tmp_path / "elsewhere")
    env = {"YUANWEI_KEY_DIR": other}
    assert KS.global_key_dir(env) == other and KS.global_key_file(ENV, env) == os.path.join(other, ENV)
    p = KS.write_global_key(ENV, FAKE_KEY, env)
    assert p == os.path.join(other, ENV) and not os.path.exists(KS.global_key_file(ENV))
    own = KS.key_file(make_home(tmp_path), ENV)
    assert KS.lookup(ENV, own, env) == (FAKE_KEY, "global_file") and KS.lookup(ENV, own, {}) == (None, "none")
    with pytest.raises(ConfigError):
        KS.global_key_file("bad name!", env)


def test_global_key_file_is_0600_in_0700_dir_and_written_atomically_without_touching_the_bot_dir(tmp_path):
    h = make_home(tmp_path)
    p = KS.write_global_key(ENV, FAKE_KEY)
    assert p == os.path.join(os.environ["HOME"], ".yuanwei", "keys", ENV)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
    assert os.listdir(os.path.dirname(p)) == [ENV] and not os.path.exists(os.path.join(h, "yuanwei"))
    with pytest.raises(ConfigError):
        KS.write_global_key(ENV, "bad value with spaces")
    assert open(p).read().strip() == FAKE_KEY and os.listdir(os.path.dirname(p)) == [ENV]
    assert KS.delete_global_key(ENV) is True and KS.delete_global_key(ENV) is False


# ---------------------------------------------------------------- 读的先后
@pytest.mark.parametrize("own,glob,env,expect", [
    (True, True, True, (K_OWN, "file")),
    (True, True, False, (K_OWN, "file")),
    (True, False, True, (K_OWN, "file")),
    (True, False, False, (K_OWN, "file")),
    (False, True, True, (K_GLOBAL, "global_file")),
    (False, True, False, (K_GLOBAL, "global_file")),
    (False, False, True, (K_ENV, "env")),
    (False, False, False, (None, "none")),
])
def test_lookup_order_own_file_then_global_file_then_environment_every_combination(tmp_path, own, glob, env, expect):
    h = make_home(tmp_path)
    own_path = KS.key_file(h, ENV)
    if own:
        KS.write_key(h, ENV, K_OWN)
    if glob:
        KS.write_global_key(ENV, K_GLOBAL)
    assert KS.lookup(ENV, own_path, {ENV: K_ENV} if env else {}) == expect
    assert KS.source(ENV, own_path, {ENV: K_ENV} if env else {}) == expect[1]


def test_config_without_key_file_never_looks_at_any_key_file_only_the_environment(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    KS.write_global_key(ENV, K_GLOBAL)
    KS.write_key(h, ENV, K_OWN)
    assert KS.lookup(ENV, None, {}) == (None, "none") and KS.lookup(ENV, None, {ENV: K_ENV}) == (K_ENV, "env")
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(ConfigError) as e:
        api_key(ENV, None)
    assert "环境变量" in str(e.value) and K_GLOBAL not in str(e.value)
    # 装好的配置里去掉 key_file（研究／调试用的写法）：状态、接头、key status 都只看环境变量
    _install(h)
    cfgp = os.path.join(h, "yuanwei", "yuanwei.toml")
    txt = open(cfgp, encoding="utf-8").read()
    assert "key_file" in txt
    assert HS.install_status(h)["organizer"]["key"]["source"] == "file"                  # 有 key_file：自己的文件优先
    open(cfgp, "w", encoding="utf-8").write("".join(l for l in txt.splitlines(True) if not l.startswith("key_file")))
    assert "key_file" not in open(cfgp, encoding="utf-8").read()
    assert HS.install_status(h)["organizer"]["key"]["source"] == "none"
    st = _status(h, capsys)
    assert st["source"] == "none" and st["config_reads_key_files"] is False
    assert st["global_file"]["present"] is True and st["this_bot_file"]["present"] is True            # 文件在不在照实说，只是不会被用
    from yuanwei.hermes_adapter import HermesAdapter
    a = HermesAdapter({"config_path": cfgp, "db": os.path.join(h, "yuanwei", "y.sqlite")})
    assert a._organizer_key()["source"] == "none"
    assert _status(h, capsys, {ENV: K_ENV})["source"] == "env"


def test_provider_with_key_file_falls_through_to_the_global_file_and_without_it_does_not(tmp_path):
    h = make_home(tmp_path)
    KS.write_global_key(ENV, K_GLOBAL)
    assert api_key(ENV, KS.key_file(h, ENV)) == K_GLOBAL
    with pytest.raises(ConfigError):
        api_key(ENV, None)


def test_global_file_with_loose_permissions_is_refused_and_later_sources_still_work(tmp_path, capsys):
    h = make_home(tmp_path)
    own = KS.key_file(h, ENV)
    g = KS.write_global_key(ENV, K_GLOBAL)
    os.chmod(g, 0o644)
    assert KS.lookup(ENV, own, {}) == (None, "file_unusable:bad_mode")
    assert KS.lookup(ENV, own, {ENV: K_ENV}) == (K_ENV, "env")                           # 后面找得到就不报
    KS.write_key(h, ENV, K_OWN)
    assert KS.lookup(ENV, own, {}) == (K_OWN, "file")
    st = _status(h, capsys)
    assert st["global_file"] == {"path": g, "present": True, "usable": False, "problem": "bad_mode", "checked": True} and st["source"] == "file"
    os.unlink(own)
    st = _status(h, capsys)
    assert st["source"] == "file_unusable:bad_mode" and K_GLOBAL not in json.dumps(st)
    with pytest.raises(ConfigError) as e:
        api_key(ENV, own)
    assert "bad_mode" in str(e.value) and K_GLOBAL not in str(e.value)


def test_unusable_own_file_falls_through_to_a_good_global_file_and_both_bad_reports_the_own_reason(tmp_path):
    h = make_home(tmp_path)
    own = KS.write_key(h, ENV, K_OWN)
    g = KS.write_global_key(ENV, K_GLOBAL)
    os.chmod(own, 0o640)
    assert KS.lookup(ENV, own, {}) == (K_GLOBAL, "global_file")
    os.chmod(g, 0o666)
    assert KS.lookup(ENV, own, {}) == (None, "file_unusable:bad_mode")
    open(own, "w").close(); os.chmod(own, 0o600)
    os.chmod(g, 0o600)
    assert KS.lookup(ENV, own, {}) == (K_GLOBAL, "global_file")                          # 自己的文件内容是空的：同样不能用，往后找
    open(g, "w").close()
    assert KS.lookup(ENV, own, {}) == (None, "file_unusable:bad_content")


# ---------------------------------------------------------------- key set / delete / status / test
def test_key_set_writes_the_global_file_by_default_and_this_bot_only_writes_the_bots_own(tmp_path, capsys):
    h = make_home(tmp_path)
    io = FakeIO(answers=["n"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._key_main(["set", "--hermes-home", h], io=io, sys_=FakeSystem()) == 0
    assert open(KS.global_key_file(ENV)).read().strip() == K_GLOBAL and not os.path.exists(os.path.join(h, "yuanwei", "keys"))
    assert K_GLOBAL not in io.text
    io2 = FakeIO(answers=["n"], secrets=[K_OWN, K_OWN])
    assert cli._key_main(["set", "--this-bot-only", "--hermes-home", h], io=io2, sys_=FakeSystem()) == 0
    assert open(KS.key_file(h, ENV)).read().strip() == K_OWN and open(KS.global_key_file(ENV)).read().strip() == K_GLOBAL      # 全局那份没被动
    assert K_OWN not in io2.text and "只给这个机器人" in io2.text and "单独的密钥文件" not in io2.text                      # 写自己的：不报「有单独密钥」
    # 覆盖要先问：全局已经有了，回答否就不动
    io3 = FakeIO(answers=["n"], secrets=[FAKE_KEY, FAKE_KEY])
    assert cli._key_main(["set", "--hermes-home", h], io=io3, sys_=FakeSystem()) == 1 and "已取消" in io3.text
    assert open(KS.global_key_file(ENV)).read().strip() == K_GLOBAL


def test_key_set_global_names_the_bots_that_keep_their_own_key_without_reading_it(tmp_path):
    main = make_home(tmp_path, "main")
    prof = pathlib.Path(main) / "profiles"
    writer, bob = make_home(prof, "writer"), make_home(prof, "bob")
    away = make_home(tmp_path, "away")                                                    # 不在主家目录下面：只有它自己当 --hermes-home 时才找得到
    own_main, own_writer, own_away = (KS.write_key(x, ENV, K_OWN) for x in (main, writer, away))
    for p in (own_main, own_writer, own_away):
        os.chmod(p, 0o000)                                                                # 读不了：只看在不在，读就会出错
    try:
        io = FakeIO(answers=["n"], secrets=[K_GLOBAL, K_GLOBAL])
        assert cli._key_main(["set", "--hermes-home", writer], io=io, sys_=FakeSystem()) == 0
        t = io.text
        assert "有自己单独的密钥文件" in t and "--this-bot-only" in t and "--hermes-home" in t
        assert own_main in t and own_writer in t and KS.key_file(bob, ENV) not in t and own_away not in t
        assert t.count(own_writer) == 1 and K_GLOBAL not in t and K_OWN not in t               # 主家目录和 profiles/* 各出现一次；给的 --hermes-home 和 profiles 里重复的不列两遍
        io2 = FakeIO(answers=["y", "n"], secrets=[K_GLOBAL, K_GLOBAL])
        assert cli._key_main(["set", "--hermes-home", away], io=io2, sys_=FakeSystem()) == 0
        assert own_away in io2.text and io2.text.count(own_away) == 1 and own_main not in io2.text
        io4 = FakeIO(answers=["y", "n"], secrets=[K_GLOBAL, K_GLOBAL])
        assert cli._key_main(["set", "--hermes-home", main], io=io4, sys_=FakeSystem()) == 0              # 给的是主家目录：机器人靠 profiles/* 找到
        assert own_writer in io4.text and own_main in io4.text and own_away not in io4.text
        ghost = pathlib.Path(main) / "profiles" / "ghost"                                                  # profiles 下面没有 config.yaml 的目录，列表找不到；自己当 --hermes-home 给就找得到
        os.makedirs(ghost)
        own_ghost = KS.write_key(str(ghost), ENV, K_OWN)
        io5 = FakeIO(answers=["y", "n"], secrets=[K_GLOBAL, K_GLOBAL])
        assert cli._key_main(["set", "--hermes-home", main], io=io5, sys_=FakeSystem()) == 0 and own_ghost not in io5.text
        io6 = FakeIO(answers=["y", "n"], secrets=[K_GLOBAL, K_GLOBAL])
        assert cli._key_main(["set", "--hermes-home", str(ghost)], io=io6, sys_=FakeSystem()) == 0 and own_ghost in io6.text and own_main in io6.text
        os.unlink(own_ghost)
    finally:
        for p in (own_main, own_writer, own_away):
            os.chmod(p, 0o600)
    # 没有谁有单独的：不出这一段
    for p in (own_main, own_writer, own_away):
        os.unlink(p)
    io3 = FakeIO(answers=["y", "n"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._key_main(["set", "--hermes-home", away], io=io3, sys_=FakeSystem()) == 0 and "单独的密钥文件" not in io3.text


def test_key_delete_global_by_default_and_this_bot_only_deletes_only_the_bots_own(tmp_path, capsys):
    h = make_home(tmp_path)
    g, own = KS.write_global_key(ENV, K_GLOBAL), KS.write_key(h, ENV, K_OWN)
    assert cli._key_main(["delete", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["scope"] == "global" and out["path"] == g and os.path.exists(g) and os.path.exists(own)
    assert cli._key_main(["delete", "--this-bot-only", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["scope"] == "this_bot" and out["path"] == own and os.path.exists(own)
    assert cli._key_main(["delete", "--this-bot-only", "--hermes-home", h, "--yes"], io=FakeIO(), sys_=FakeSystem()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and out["deleted"] is True and not os.path.exists(own) and os.path.exists(g)
    assert cli._key_main(["delete", "--hermes-home", h, "--yes"], io=FakeIO(), sys_=FakeSystem()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scope"] == "global" and out["deleted"] is True and not os.path.exists(g)
    assert cli._key_main(["delete", "--hermes-home", h, "--yes"], io=FakeIO(), sys_=FakeSystem()) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is False


def test_key_status_reports_every_source_and_says_separately_whether_each_file_is_there(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    st = _status(h, capsys)
    assert st["source"] == "none" and st["global_file"]["present"] is False and st["this_bot_file"]["present"] is False
    assert st["config_reads_key_files"] is True and st["this_bot_file"]["path"] == KS.key_file(h, ENV) and st["global_file"]["path"] == KS.global_key_file(ENV)
    open(os.path.join(h, ".env"), "w").write("OTHER=1\n%s=%s\n" % (ENV, FAKE_KEY))              # 小问题 26：Hermes 的 .env 里有这个名字
    assert _status(h, capsys)["source"] == "hermes_env_file"
    assert FAKE_KEY not in json.dumps(_status(h, capsys))
    st = _status(h, capsys, {ENV: K_ENV})
    assert st["source"] == "env"
    KS.write_global_key(ENV, K_GLOBAL)
    st = _status(h, capsys, {ENV: K_ENV})
    assert st["source"] == "global_file" and st["global_file"]["usable"] is True and st["this_bot_file"]["present"] is False
    KS.write_key(h, ENV, K_OWN)
    st = _status(h, capsys, {ENV: K_ENV})
    assert st["source"] == "file" and st["this_bot_file"]["usable"] is True and st["global_file"]["usable"] is True
    os.unlink(KS.key_file(h, ENV)); os.unlink(KS.global_key_file(ENV)); os.unlink(os.path.join(h, ".env"))
    KS.write_key(h, ENV, K_OWN)
    os.chmod(KS.key_file(h, ENV), 0o644)
    st = _status(h, capsys)
    assert st["source"] == "file_unusable:bad_mode" and st["this_bot_file"]["problem"] == "bad_mode"
    os.chmod(KS.key_file(h, ENV), 0o600)
    out = json.dumps(st)
    assert all(k not in out for k in (K_OWN, K_GLOBAL, K_ENV, FAKE_KEY))


def test_key_status_and_status_hermes_share_one_function_for_the_dotenv_name(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    for body, want in (("%s=%s\n" % (ENV, FAKE_KEY), "hermes_env_file"), ("# %s=%s\n%s=\n" % (ENV, FAKE_KEY, ENV), "none")):
        open(os.path.join(h, ".env"), "w").write(body)
        assert HS.install_status(h)["organizer"]["key"]["source"] == want and _status(h, capsys)["source"] == want == KS.source_with_dotenv(ENV, None, h, {})
    os.unlink(os.path.join(h, ".env"))
    os.mkdir(os.path.join(h, ".env"))                                                      # 读不了：写没核到
    assert HS.install_status(h)["organizer"]["key"]["source"] == "unchecked" and _status(h, capsys)["source"] == "unchecked"
    KS.write_global_key(ENV, K_GLOBAL)                                                      # 全局文件排在 .env 前面
    assert HS.install_status(h)["organizer"]["key"]["source"] == "global_file" and _status(h, capsys)["source"] == "global_file"


def test_key_test_uses_the_copy_that_would_really_be_used(tmp_path, monkeypatch):
    h = make_home(tmp_path)
    seen = []
    monkeypatch.setattr(KS, "check_key", lambda base_url, model, params, key, opener=None, timeout=30: (seen.append(key), "works")[1])
    KS.write_global_key(ENV, K_GLOBAL)
    for env, want in (({ENV: K_ENV}, K_GLOBAL),):
        assert cli._key_main(["test", "--hermes-home", h], io=FakeIO(answers=["y"]), sys_=FakeSystem(env=env)) == 0
        assert seen[-1] == want
    KS.write_key(h, ENV, K_OWN)
    assert cli._key_main(["test", "--hermes-home", h], io=FakeIO(answers=["y"]), sys_=FakeSystem()) == 0 and seen[-1] == K_OWN
    KS.delete_key(h, ENV); KS.delete_global_key(ENV)
    assert cli._key_main(["test", "--hermes-home", h], io=FakeIO(answers=["y"]), sys_=FakeSystem(env={ENV: K_ENV})) == 0 and seen[-1] == K_ENV
    io = FakeIO(answers=["y"])
    assert cli._key_main(["test", "--hermes-home", h], io=io, sys_=FakeSystem()) == 2 and "没有找到密钥" in io.text
    assert len(seen) == 3


# ---------------------------------------------------------------- 向导
def test_wizard_plan_names_the_global_path_and_the_entered_key_goes_there(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "1", "n", "1", "y", "y"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={})) == 0
    g = KS.global_key_file(ENV)
    assert "确认后存进原委的全局密钥文件 %s" % g in io.text and KS.key_file(h, ENV) not in io.text
    assert open(g).read().strip() == K_GLOBAL and not os.path.exists(os.path.join(h, "yuanwei", "keys"))
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    assert cfg.provider["key_file"] == KS.key_file(h, ENV)                                 # 配置里仍记这个机器人自己的路径，读的先后才对得上
    assert K_GLOBAL not in io.text


def test_wizard_sees_an_existing_global_key_as_already_there_and_asks_nothing_secret(tmp_path):
    h = make_home(tmp_path)
    KS.write_global_key(ENV, K_GLOBAL)
    io = FakeIO(["1", "2", "y", "y", "1", "y", "y"])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={})) == 0
    assert "已有：原委的全局密钥文件" in io.text and not any(a.startswith("[secret]") for a in io.asked)
    assert "没找到 %s" % ENV not in io.text
    assert WZ.key_state(FakeSystem(env={}), h, {"key_env": ENV}) == {"env": ENV, "source": "global_file", "global_path": KS.global_key_file(ENV), "global_problem": None}


def test_wizard_replacing_a_key_when_the_bot_has_its_own_file_warns_that_the_own_file_still_wins(tmp_path):
    h = make_home(tmp_path)
    KS.write_key(h, ENV, K_OWN)
    io = FakeIO(["1", "2", "y", "n", "1", "y", "y"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={})) == 0
    assert "这个机器人有自己单独的密钥文件" in io.text and "--this-bot-only" in io.text
    assert open(KS.global_key_file(ENV)).read().strip() == K_GLOBAL and open(KS.key_file(h, ENV)).read().strip() == K_OWN


def test_param_mode_install_does_not_write_any_key_file_and_records_the_bots_own_path(tmp_path):
    h = make_home(tmp_path)
    _install(h, {ENV: K_ENV})
    assert not os.path.exists(KS.global_key_dir()) and not os.path.exists(os.path.join(h, "yuanwei", "keys"))
    assert Config.load(os.path.join(h, "yuanwei", "yuanwei.toml")).provider["key_file"] == KS.key_file(h, ENV)


# ---------------------------------------------------------------- 两个机器人共用
def test_two_bots_share_one_global_key_and_a_bot_with_its_own_keeps_using_its_own(tmp_path, capsys):
    main = make_home(tmp_path, "main")
    writer = make_home(pathlib.Path(main) / "profiles", "writer")
    _install(main)
    _install(writer)
    KS.write_global_key(ENV, K_GLOBAL)
    for h in (main, writer):
        assert HS.install_status(h)["organizer"]["key"]["source"] == "global_file" and _status(h, capsys)["source"] == "global_file"
        cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
        assert api_key(ENV, cfg.provider["key_file"]) == K_GLOBAL
    from yuanwei.hermes_adapter import HermesAdapter
    for h in (main, writer):
        assert HermesAdapter({"config_path": os.path.join(h, "yuanwei", "yuanwei.toml"), "db": os.path.join(h, "yuanwei", "y.sqlite")})._organizer_key() == {"env": ENV, "source": "global_file"}
    KS.write_key(writer, ENV, K_OWN)                                                          # writer 单独设了：只它自己用自己的
    assert HS.install_status(writer)["organizer"]["key"]["source"] == "file" and HS.install_status(main)["organizer"]["key"]["source"] == "global_file"
    cfgw = Config.load(os.path.join(writer, "yuanwei", "yuanwei.toml"))
    assert api_key(ENV, cfgw.provider["key_file"]) == K_OWN
    p = OpenAICompatProvider("http://127.0.0.1:9/v1", ENV, {"model": "m"}, 1, key_file=KS.key_file(main, ENV))
    assert api_key(p.api_key_env, p.key_file) == K_GLOBAL


# ---------------------------------------------------------------- 不支持 0600 的系统
def test_system_without_0600_support_stores_no_file_reads_no_file_and_the_config_has_no_key_file(tmp_path, monkeypatch, capsys):
    h = make_home(tmp_path)
    g = KS.write_global_key(ENV, K_GLOBAL)                                                   # 先在「支持」时放一份，下面它不该被看
    monkeypatch.setattr(KS, "supported", lambda: False)
    with pytest.raises(ConfigError):
        KS.write_global_key(ENV, K_GLOBAL)
    with pytest.raises(ConfigError):
        KS.write_key(h, ENV, K_OWN)
    assert cli._key_main(["set", "--hermes-home", h], io=FakeIO(secrets=[K_GLOBAL, K_GLOBAL]), sys_=FakeSystem()) == 2
    assert "环境变量" in capsys.readouterr().err
    assert cli._key_main(["set", "--this-bot-only", "--hermes-home", h], io=FakeIO(secrets=[K_GLOBAL, K_GLOBAL]), sys_=FakeSystem()) == 2
    capsys.readouterr()
    _install(h, {ENV: K_ENV})
    assert "key_file" not in open(os.path.join(h, "yuanwei", "yuanwei.toml"), encoding="utf-8").read()
    assert WZ.key_state(FakeSystem(env={}), h, {"key_env": ENV})["source"] != "global_file"
    st = _status(h, capsys)
    assert st["source"] == "none" and st["config_reads_key_files"] is False and st["platform_supports_key_file"] is False
    assert _status(h, capsys, {ENV: K_ENV})["source"] == "env" and os.path.exists(g)
