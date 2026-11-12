"""照 Codex 复核（2026-11-12）补全局密钥的 R1／R2 和四条建议。
R1：key status／key test 查整理实际用的那份 key_file（配置里记的），不是重新拼出来的标准路径；provider、status hermes、key status、key test 四处选同一把。
R2：sdist 不带 tests/（MANIFEST.in 里 prune tests）。任何测试都不许读写真的 ~/.yuanwei/keys、~/.hermes：HOME 由 conftest 指到临时目录；请求用假接收器。"""
import contextlib
import io as _io
import json
import os
import pathlib
import shlex

import pytest

from helpers_wizard import FakeIO, FakeSystem, make_home
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei import keystore as KS
from yuanwei import wizard as WZ
from yuanwei.config import Config
from yuanwei.errors import ConfigError
from yuanwei.provider import OpenAICompatProvider

ENV = "DEEPSEEK_API_KEY"
OTHER = "OTHER_PROVIDER_KEY"
BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
K_A, K_STD, K_GLOBAL, K_ENV = "sk-CUSTOMFILE-A000aaaaaaaa", "sk-STANDARDOWN00bbbbbbbb", "sk-GLOBALFILE-B00cccccccc", "sk-ENVKEY0000000dddddddd"


def _install(h, env=None):
    with contextlib.redirect_stdout(_io.StringIO()):
        assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=FakeSystem(env=env or {})) == 0


def _write_file(path, value, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.chmod(path, mode)
    return path


def _cfgp(h):
    return os.path.join(h, "yuanwei", "yuanwei.toml")


def _point_key_file_at(h, path):
    """把装好的配置里的 key_file 改指向 path（一份自定义文件）。"""
    lines = open(_cfgp(h), encoding="utf-8").read().splitlines(True)
    out, n = [], 0
    for ln in lines:
        if ln.startswith("key_file"):
            ln, n = 'key_file = "%s"\n' % path, n + 1
        out.append(ln)
    assert n == 1
    open(_cfgp(h), "w", encoding="utf-8").write("".join(out))
    assert Config.load(_cfgp(h)).provider["key_file"] == path


class Receiver:
    """假的请求接收器：记下 key test 实际拿去试的是哪把（替掉 check_key，不发任何网络请求）。"""

    def __init__(self, monkeypatch):
        self.keys = []
        monkeypatch.setattr(KS, "check_key", lambda base_url, model, params, key, opener=None, timeout=30: (self.keys.append(key), "works")[1])


def _provider_key(h):
    """整理 provider 真的会发出去的那把（假 opener 接住 Authorization）。"""
    prov = Config.load(_cfgp(h)).provider
    seen = {}

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "model": "m"}).encode()

    def opener(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return R()

    OpenAICompatProvider(prov["base_url"], prov["api_key_env"], opener=opener, key_file=prov.get("key_file"))({"body_bytes": b"{}"})
    return seen["auth"].replace("Bearer ", "")


def _key_status(h, capsys, env=None, extra=()):
    assert cli._key_main(["status", "--hermes-home", h] + list(extra), io=FakeIO(), sys_=FakeSystem(env=env or {})) == 0
    return json.loads(capsys.readouterr().out)


def _key_test(h, extra=(), env=None):
    io = FakeIO(answers=["y"])
    rc = cli._key_main(["test", "--hermes-home", h] + list(extra), io=io, sys_=FakeSystem(env=env or {}))
    return rc, io.text


def _four_places(h, capsys, monkeypatch):
    """provider、status hermes、key status、key test 各选了哪一把：返回 (provider 的值, status hermes 的来源, key status 的来源, key test 的值)。"""
    rc = Receiver(monkeypatch)
    p = _provider_key(h)
    hs = HS.install_status(h)["organizer"]["key"]["source"]
    ks = _key_status(h, capsys)["source"]
    code, _t = _key_test(h)
    assert code == 0
    return p, hs, ks, rc.keys[-1]


# ================================================================ R1
def test_r1_custom_key_file_A_wins_over_global_B_and_all_four_places_agree(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    a = _write_file(str(tmp_path / "custom" / "mykey"), K_A)
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    _point_key_file_at(h, a)
    assert not os.path.exists(KS.key_file(h, ENV))                                          # 标准位置是空的
    p, hs, ks, kt = _four_places(h, capsys, monkeypatch)
    assert (p, kt) == (K_A, K_A) and hs == "file" and ks == "file"                          # 旧写法这里会是 global_file／B
    st = _key_status(h, capsys)
    assert st["basis"] == "installed_config" and st["this_bot_file"]["path"] == a and st["this_bot_file"]["checked"] is True
    assert st["this_bot_only_manages"] == KS.key_file(h, ENV)
    assert any("不是标准位置" in n and "不会动那份自定义文件" in n for n in st["notes"])
    assert K_A not in json.dumps(st) and K_GLOBAL not in json.dumps(st)


def test_r1_custom_file_missing_or_unusable_falls_to_global_B_everywhere(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    a = str(tmp_path / "custom" / "mykey")
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    _point_key_file_at(h, a)                                                                  # A 不存在
    assert _four_places(h, capsys, monkeypatch) == (K_GLOBAL, "global_file", "global_file", K_GLOBAL)
    _write_file(a, K_A, 0o644)                                                                # A 在，但权限不对：不用，照样用 B
    assert _four_places(h, capsys, monkeypatch) == (K_GLOBAL, "global_file", "global_file", K_GLOBAL)
    st = _key_status(h, capsys)
    assert st["this_bot_file"]["path"] == a and st["this_bot_file"]["problem"] == "bad_mode"


def test_r1_custom_file_A_alone_is_found_even_without_a_global_file(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    a = _write_file(str(tmp_path / "custom" / "mykey"), K_A)
    _point_key_file_at(h, a)
    st = _key_status(h, capsys)
    assert st["source"] == "file" and st["global_file"]["present"] is False                  # 旧写法这里会报 none


def test_r1_config_without_key_file_reads_neither_A_nor_B(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    _write_file(KS.key_file(h, ENV), K_STD)
    _write_file(str(tmp_path / "custom" / "mykey"), K_A)
    txt = open(_cfgp(h), encoding="utf-8").read()
    open(_cfgp(h), "w", encoding="utf-8").write("".join(l for l in txt.splitlines(True) if not l.startswith("key_file")))
    rc = Receiver(monkeypatch)
    st = _key_status(h, capsys)
    assert st["source"] == "none" and st["config_reads_key_files"] is False and st["this_bot_file"]["checked"] is False and st["global_file"]["checked"] is False
    code, text = _key_test(h)
    assert code == 2 and "没有找到密钥" in text and rc.keys == []                           # 一个请求也没发
    assert _key_test(h, env={ENV: K_ENV})[0] == 0 and rc.keys == [K_ENV]


def test_r1_not_installed_is_a_preview_of_the_standard_path_and_says_so(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _write_file(KS.key_file(h, ENV), K_STD)
    st = _key_status(h, capsys)
    assert st["basis"] == "preview_not_installed" and st["source"] == "file" and st["this_bot_file"]["path"] == KS.key_file(h, ENV)
    assert any("预览" in n for n in st["notes"])
    rc = Receiver(monkeypatch)
    code, text = _key_test(h)
    assert code == 0 and rc.keys == [K_STD] and "预览" in text
    os.unlink(KS.key_file(h, ENV))
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    assert _key_status(h, capsys)["source"] == "global_file" and _key_test(h)[0] == 0 and rc.keys[-1] == K_GLOBAL


def test_r1_standard_path_positive_case_all_four_agree_and_no_custom_note(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    _write_file(KS.key_file(h, ENV), K_STD)
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    assert _four_places(h, capsys, monkeypatch) == (K_STD, "file", "file", K_STD)
    st = _key_status(h, capsys)
    assert st["basis"] == "installed_config" and "this_bot_only_manages" not in st and "notes" not in st
    os.unlink(KS.key_file(h, ENV))
    assert _four_places(h, capsys, monkeypatch) == (K_GLOBAL, "global_file", "global_file", K_GLOBAL)


def test_r1_explicit_api_key_env_that_differs_from_the_config_is_labelled_and_does_not_use_the_configs_key_file(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    a = _write_file(str(tmp_path / "custom" / "mykey"), K_A)
    _point_key_file_at(h, a)
    _write_file(KS.global_key_file(OTHER), K_GLOBAL)
    st = _key_status(h, capsys, extra=["--api-key-env", OTHER])
    assert st["basis"] == "named_variable_differs_from_config" and st["env"] == OTHER and st["source"] == "global_file"
    assert st["this_bot_file"]["path"] == KS.key_file(h, OTHER) and a not in json.dumps(st)
    assert any("你用 --api-key-env 指定的变量 %s" % OTHER in n and ENV in n for n in st["notes"])
    rc = Receiver(monkeypatch)
    code, text = _key_test(h, ["--api-key-env", OTHER])
    assert code == 0 and rc.keys == [K_GLOBAL] and "指定的变量 %s" % OTHER in text and "不是当前整理配置" in text
    # 指定的和配置里一样：就是在核配置，不带这句话
    st2 = _key_status(h, capsys, extra=["--api-key-env", ENV])
    assert st2["basis"] == "installed_config" and st2["source"] == "file" and st2["this_bot_file"]["path"] == a


def test_r1_this_bot_only_stays_on_the_standard_path_and_says_the_custom_file_is_not_touched(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    a = _write_file(str(tmp_path / "custom" / "mykey"), K_A)
    _point_key_file_at(h, a)
    std = _write_file(KS.key_file(h, ENV), K_STD)
    g = _write_file(KS.global_key_file(ENV), K_GLOBAL)
    assert cli._key_main(["delete", "--this-bot-only", "--yes", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["path"] == std and r["deleted"] is True and "不会动那份自定义文件" in r["note"]
    assert not os.path.exists(std) and open(a).read().strip() == K_A and open(g).read().strip() == K_GLOBAL
    io = FakeIO(answers=["n"], secrets=[K_STD, K_STD])
    assert cli._key_main(["set", "--this-bot-only", "--hermes-home", h], io=io, sys_=FakeSystem()) == 0
    assert open(std).read().strip() == K_STD and open(a).read().strip() == K_A and "不会动那份自定义文件" in io.text
    cli._key_main(["delete", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem())              # 不带 --this-bot-only：没有 note
    assert "note" not in json.loads(capsys.readouterr().out)


# ================================================================ R2
def test_r2_manifest_prunes_tests_from_the_sdist():
    root = pathlib.Path(__file__).resolve().parent.parent
    lines = [l.strip() for l in (root / "MANIFEST.in").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert "prune tests" in lines


# ================================================================ 建议 1：读文件出错也算不能用
def test_s1_non_utf8_global_file_is_unusable_and_the_environment_still_wins_without_leaking_bytes(tmp_path):
    h = make_home(tmp_path)
    g = KS.global_key_file(ENV)
    os.makedirs(os.path.dirname(g), mode=0o700, exist_ok=True)
    with open(g, "wb") as f:
        f.write(b"sk-\xff\xfe\xfdSECRETBYTES0000\n")
    os.chmod(g, 0o600)
    own = KS.key_file(h, ENV)
    assert KS.lookup(ENV, own, {ENV: K_ENV}) == (K_ENV, "env")
    v, src = KS.lookup(ENV, own, {})
    assert v is None and src == "file_unusable:bad_content"
    with pytest.raises(ConfigError) as e:
        KS.read_key(g)
    assert "SECRETBYTES" not in str(e.value) and "bad_content" in str(e.value)


def test_s1_read_failure_after_the_check_falls_through_to_the_environment(tmp_path, monkeypatch):
    h = make_home(tmp_path)
    g = _write_file(KS.global_key_file(ENV), K_GLOBAL)
    own = KS.key_file(h, ENV)
    real_open = open

    def boom(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(g):
            raise PermissionError(13, "denied " + K_GLOBAL)                                   # 错误文字里就算有值也不能传出去
        return real_open(path, *a, **k)

    monkeypatch.setattr(KS, "open", boom, raising=False)
    assert KS.lookup(ENV, own, {ENV: K_ENV}) == (K_ENV, "env")                                # 全局读失败，环境变量里有：用环境变量
    assert KS.lookup(ENV, own, {}) == (None, "file_unusable:unreadable")
    with pytest.raises(ConfigError) as e:
        KS.read_key(g)
    assert K_GLOBAL not in str(e.value)


def test_s1_own_file_read_failure_falls_to_the_global_file(tmp_path, monkeypatch):
    h = make_home(tmp_path)
    own = _write_file(KS.key_file(h, ENV), K_STD)
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    real_open = open

    def boom(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(own):
            raise OSError(5, "io")
        return real_open(path, *a, **k)

    monkeypatch.setattr(KS, "open", boom, raising=False)
    assert KS.lookup(ENV, own, {}) == (K_GLOBAL, "global_file")


def test_s1_unusable_file_means_the_dotenv_is_not_consulted_and_the_status_says_that_is_not_proof(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    _write_file(KS.key_file(h, ENV), K_STD, 0o644)                                            # 在、但权限不对
    open(os.path.join(h, ".env"), "w", encoding="utf-8").write("%s=%s\n" % (ENV, K_ENV))
    assert KS.source_with_dotenv(ENV, KS.key_file(h, ENV), h, {}) == "file_unusable:bad_mode"   # 不看 .env
    st = _key_status(h, capsys)
    assert st["source"] == "file_unusable:bad_mode" and any("不等于 Hermes 运行时一定没有可用的密钥" in n for n in st["notes"])
    hs = HS.install_status(h)
    assert hs["organizer"]["key"]["source"] == "file_unusable:bad_mode" and any("不等于 Hermes 运行时一定没有可用的密钥" in n for n in hs["notes"])


# ================================================================ 建议 2：提醒带条件、带准确的家目录
def test_s2_own_key_notes_are_conditional_and_say_the_scan_is_not_exhaustive(tmp_path):
    main = make_home(tmp_path, "main")
    own = KS.write_key(main, ENV, K_STD)
    t = "\n".join(cli._own_key_notes(main, None, ENV))
    assert own in t and "能用的话，会先用它" in t and "key status" in t and "--hermes-home" in t
    assert "没有查全" in t and "profiles/*" in t
    assert "会继续用自己的" not in t                                                            # 旧的无条件说法
    assert cli._own_key_notes(make_home(tmp_path, "empty"), None, ENV) == []


def test_s2_wizard_replace_key_hint_carries_the_current_hermes_home(tmp_path):
    h = make_home(tmp_path, "my home")                                                        # 路径里有空格：要带引号
    KS.write_key(h, ENV, K_STD)
    io = FakeIO(["1", "2", "y", "n", "1", "y", "y"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={})) == 0
    assert "yuanwei key set --this-bot-only --hermes-home %s --api-key-env %s" % (shlex.quote(h), ENV) in io.text
    assert "那份能用的时候会一直先用它" in io.text


# ================================================================ 建议 3：YUANWEI_KEY_DIR 只认绝对路径
def test_s3_key_dir_expands_tilde_and_rejects_relative_paths(tmp_path):
    assert KS.global_key_dir({"YUANWEI_KEY_DIR": "~/secrets"}) == os.path.join(os.path.expanduser("~"), "secrets")
    assert os.path.isabs(KS.global_key_dir({"YUANWEI_KEY_DIR": "~/secrets"}))
    assert KS.global_key_dir({"YUANWEI_KEY_DIR": str(tmp_path / "abs")}) == str(tmp_path / "abs")
    for bad in ("relative/dir", "./here", "keys", "~nosuchuser_zz/x"):
        with pytest.raises(ConfigError) as e:
            KS.global_key_dir({"YUANWEI_KEY_DIR": bad})
        assert "绝对路径" in str(e.value)
        assert KS.global_dir_problem({"YUANWEI_KEY_DIR": bad})
        with pytest.raises(ConfigError):
            KS.write_global_key(ENV, K_GLOBAL, {"YUANWEI_KEY_DIR": bad})
    assert KS.global_dir_problem({}) is None and KS.global_dir_problem({"YUANWEI_KEY_DIR": ""}) is None


def test_s3_relative_key_dir_is_not_used_for_lookup_and_the_status_says_why(tmp_path, capsys, monkeypatch):
    h = make_home(tmp_path)
    _install(h)
    monkeypatch.chdir(tmp_path)
    _write_file(str(tmp_path / "keys" / ENV), K_GLOBAL)                                       # 相对路径 "keys" 若被用，会读到它
    env = {"YUANWEI_KEY_DIR": "keys", ENV: K_ENV}
    assert KS.lookup(ENV, KS.key_file(h, ENV), env) == (K_ENV, "env")
    st = _key_status(h, capsys, env=env)
    assert st["source"] == "env" and "绝对路径" in st["global_dir_problem"] and st["global_file"]["path"] is None and "global_dir" not in st
    err = cli._key_main(["delete", "--yes", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env=env))
    assert err == 2 and "bad_key_dir" in capsys.readouterr().err
    assert cli._key_main(["delete", "--this-bot-only", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem(env=env)) == 0   # 只管自己的那份不受影响


def test_s3_status_shows_the_final_absolute_dir_and_the_wizard_refuses_to_store_into_a_bad_dir(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    env = {"YUANWEI_KEY_DIR": "~/vault"}
    st = _key_status(h, capsys, env=env)
    assert st["global_dir"] == os.path.join(os.path.expanduser("~"), "vault") and st["global_file"]["path"] == os.path.join(st["global_dir"], ENV)
    ks = WZ.key_state(FakeSystem(env={"YUANWEI_KEY_DIR": "rel"}), h, {"key_env": ENV})
    assert ks["global_path"] is None and "绝对路径" in ks["global_problem"] and ks["source"] == "none"
    h2 = make_home(tmp_path, "h2")
    io = FakeIO(["1", "2", "y", "1", "1", "y", "y"], secrets=[K_GLOBAL, K_GLOBAL])
    assert cli._setup_main(["setup", "--hermes-home", h2] + BASE, io=io, sys_=FakeSystem(env={"YUANWEI_KEY_DIR": "rel"})) == 0
    assert "绝对路径" in io.text and not any(a.startswith("[secret]") for a in io.asked) and "先改好上面说的环境变量" in io.text and not os.path.exists(os.path.join(h2, "yuanwei", "keys"))


# ================================================================ 建议 4：措辞
def test_s4_status_says_it_only_reports_the_source_and_never_claims_the_content_is_valid(tmp_path, capsys):
    h = make_home(tmp_path)
    _install(h)
    _write_file(KS.global_key_file(ENV), K_GLOBAL)
    st = _key_status(h, capsys)
    assert "只报来源，不显示密钥的值" in st["note_values"] and "不代表里面的值合格" in st["note_values"]
    assert "内容有效" not in json.dumps(st, ensure_ascii=False)
    # file_report 的 usable 只看类型和权限：空内容的 0600 文件也是 usable，但真正读的时候会被挡
    empty = _write_file(str(tmp_path / "e" / "k"), "")
    assert KS.file_report(empty)["usable"] is True
    os.unlink(KS.global_key_file(ENV))
    assert KS.lookup(ENV, empty, {}) == (None, "file_unusable:bad_content")
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "src" / "yuanwei" / "wizard.py").read_text(encoding="utf-8")
    assert "没有读出它的值" not in src and "这里只报来源，不显示它的值" in src
