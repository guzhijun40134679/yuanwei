"""真机试装修补（一）丙（2026-11-11）：问题 4–8 的小修——密钥来源（外面看）、向导第 5 步「用它吗」、装完提示里的完整路径和 --help、备份保留原权限、LM Studio 编码身份。"""
import hashlib
import json
import os
import stat
import sys

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import catalog as CT
from yuanwei import cli
from yuanwei import embedsetup as ES
from yuanwei import hermes_setup as HS
from yuanwei import wizard as WZ

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
NEW_KEY = "sk-NEWKEY9999999999zzzzzzXYZ"
DOCS = os.path.join(os.path.dirname(__file__), "..", "docs")
README = os.path.join(os.path.dirname(__file__), "..", "README.md")
README_ZH = os.path.join(os.path.dirname(__file__), "..", "README.zh-CN.md")


def _light(h, sysx=None):
    return cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=sysx or FakeSystem(env={}))


# ---------------------------------------------------------------- 8 外面看密钥来源：环境变量里没有，再查 Hermes 的 .env 里有没有这个名字
def test_status_from_outside_sees_the_name_in_hermes_dotenv_without_reading_the_value(tmp_path, monkeypatch):
    h = make_home(tmp_path)
    assert _light(h) == 0
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert HS.install_status(h)["organizer"]["key"] == {"env": "DEEPSEEK_API_KEY", "source": "none"}
    open(os.path.join(h, ".env"), "w").write("OTHER=1\nDEEPSEEK_API_KEY=%s\n" % FAKE_KEY)
    st = HS.install_status(h)
    assert st["organizer"]["key"]["source"] == "hermes_env_file" and FAKE_KEY not in json.dumps(st)
    open(os.path.join(h, ".env"), "w").write("# DEEPSEEK_API_KEY=%s\nDEEPSEEK_API_KEY=\n" % FAKE_KEY)            # 只有注释、或空值：不算
    assert HS.install_status(h)["organizer"]["key"]["source"] == "none"
    os.unlink(os.path.join(h, ".env"))
    os.mkdir(os.path.join(h, ".env"))                                                                                  # 读不了：写没核到，不当成没有
    assert HS.install_status(h)["organizer"]["key"]["source"] == "unchecked"


def test_status_order_file_then_env_then_hermes_dotenv(tmp_path, monkeypatch):
    from yuanwei import keystore as KS
    h = make_home(tmp_path)
    assert _light(h) == 0
    open(os.path.join(h, ".env"), "w").write("DEEPSEEK_API_KEY=%s\n" % FAKE_KEY)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    assert HS.install_status(h)["organizer"]["key"]["source"] == "env"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    KS.write_key(h, "DEEPSEEK_API_KEY", NEW_KEY)
    assert HS.install_status(h)["organizer"]["key"]["source"] == "file"


# ---------------------------------------------------------------- 9 向导第 5 步：已有密钥时问「用它吗」
def KS_global():
    from yuanwei import keystore as KS
    return KS.global_key_file("DEEPSEEK_API_KEY")


def _flip_tty(io):
    state = {"n": 0}
    orig = io.tty

    def tty():
        state["n"] += 1
        return True if state["n"] == 1 else False
    io.tty = tty
    return io


def test_existing_key_asks_whether_to_use_it_and_yes_keeps_it_without_asking_for_a_new_one(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "y", "1", "y", "y"], secrets=[])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    assert any("用它吗" in a for a in io.asked) and not any(a.startswith("[secret]") for a in io.asked)
    assert not os.path.exists(os.path.join(h, "yuanwei", "keys", "DEEPSEEK_API_KEY")) and not os.path.exists(KS_global())


def test_answering_n_takes_a_new_hidden_key_and_stores_it_in_a_0600_file(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "n", "1", "y", "y"], secrets=[NEW_KEY, NEW_KEY])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    from yuanwei import keystore as KS
    own = os.path.join(h, "yuanwei", "keys", "DEEPSEEK_API_KEY")
    kp = KS.global_key_file("DEEPSEEK_API_KEY")                                                          # 输入的密钥存全局，不存进这个机器人自己的目录
    assert open(kp).read().strip() == NEW_KEY and stat.S_IMODE(os.stat(kp).st_mode) == 0o600 and not os.path.exists(own)
    assert NEW_KEY not in io.text and FAKE_KEY not in io.text and "确认后存进" in io.text and kp in io.text
    assert KS.lookup("DEEPSEEK_API_KEY", own, {"DEEPSEEK_API_KEY": FAKE_KEY}) == (NEW_KEY, "global_file")  # 读的时候新的（全局）排在环境变量前面


def test_answering_n_when_the_new_key_cannot_be_entered_keeps_the_old_one_and_says_so(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "n", "1", "y", "y"], secrets=["bad", "bad", "bad", "bad", "bad", "bad"])     # 三次格式都不对
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    assert "还是用已有的那一把" in io.text and not os.path.exists(os.path.join(h, "yuanwei", "keys", "DEEPSEEK_API_KEY")) and not os.path.exists(KS_global())


def test_answering_n_in_a_non_terminal_gives_the_command_and_changes_nothing_about_the_key(tmp_path):
    h = make_home(tmp_path)
    io = _flip_tty(FakeIO(["1", "2", "y", "n", "1", "y", "y"], secrets=[NEW_KEY, NEW_KEY]))
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    assert "yuanwei key set --api-key-env DEEPSEEK_API_KEY" in io.text and not os.path.exists(os.path.join(h, "yuanwei", "keys", "DEEPSEEK_API_KEY")) and not os.path.exists(KS_global())
    assert not any(a.startswith("[secret]") for a in io.asked)


# ---------------------------------------------------------------- 10 装完提示里写完整路径；--help 列出用户命令
def test_finish_text_names_the_full_path_of_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(WZ, "entry_command", lambda: "/opt/yw/venv/bin/yuanwei")
    t = WZ.finish_text()
    assert "/opt/yw/venv/bin/yuanwei status hermes" in t and "/opt/yw/venv/bin/yuanwei vectors status" in t and "/opt/yw/venv/bin/yuanwei uninstall hermes" in t
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "y", "1", "y", "y"])
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})) == 0
    assert "/opt/yw/venv/bin/yuanwei status hermes" in io.text


def test_entry_command_is_this_executable_then_the_one_beside_python_then_python_dash_m(tmp_path, monkeypatch):
    exe = tmp_path / "venv" / "bin"
    exe.mkdir(parents=True)
    (exe / "yuanwei").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(exe / "yuanwei"), "setup"])
    assert WZ.entry_command() == str(exe / "yuanwei")
    (tmp_path / "with space").mkdir()
    (tmp_path / "with space" / "yuanwei").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "with space" / "yuanwei")])
    assert WZ.entry_command() == "'%s'" % (tmp_path / "with space" / "yuanwei")                           # 路径里有空格：加引号，能直接复制去运行
    monkeypatch.setattr(sys, "argv", ["/somewhere/pytest"])
    monkeypatch.setattr(sys, "executable", str(exe / "python"))
    assert WZ.entry_command() == str(exe / "yuanwei")                                                     # 和 Python 放在一起的那个
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other" / "python"))
    assert WZ.entry_command() == "%s -m yuanwei" % (tmp_path / "other" / "python")                        # 都没有：用当前 Python 起


def test_top_level_help_lists_the_user_commands_and_does_not_ask_for_db_and_workspace(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for must in ("setup", "status hermes", "key set", "vectors", "conversations", "secrets scan", "uninstall hermes", "doctor hermes"):
        assert must in out, must
    assert "required" not in out and "usage: yuanwei [-h] --db" not in out
    assert cli.main(["-h"]) == 0 and cli.main(["help"]) == 0
    capsys.readouterr()
    assert cli.main([]) == 2 and "setup" in capsys.readouterr().out                                       # 什么都不给：也是这份说明（退出码 2）
    with pytest.raises(SystemExit) as e:
        cli.main(["setup", "-h"])                                                                         # 各命令自己的 -h 照旧
    assert e.value.code == 0
    with pytest.raises(SystemExit):
        cli.main(["--db", "x"])                                                                           # 研究用的写法照旧要 --workspace 和子命令


# ---------------------------------------------------------------- 10 文档：python3 -m pip、先建环境、完整路径
@pytest.mark.parametrize("path", [README, README_ZH, os.path.join(DOCS, "装到Hermes上.md")])
def test_install_docs_use_python3_m_pip_a_venv_under_dot_yuanwei_and_full_paths(path):
    t = open(path, encoding="utf-8").read()
    assert "python3 -m venv ~/.yuanwei/venv" in t and "~/.yuanwei/venv/bin/pip install" in t
    assert "~/.yuanwei/venv/bin/yuanwei" in t
    import re
    assert not re.search(r"(?m)^\s*(?<!python3 -m )pip install", t) and "`pip install" not in t           # 不再写光秃秃的 pip install


# ---------------------------------------------------------------- 11 备份保留原文件的权限
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_config_backup_keeps_the_original_file_mode(tmp_path, mode):
    h = make_home(tmp_path)
    cfg = os.path.join(h, "config.yaml")
    os.chmod(cfg, mode)
    assert _light(h) == 0
    backs = [n for n in os.listdir(h) if n.startswith("config.yaml.yuanwei-backup-")]
    assert len(backs) == 1 and stat.S_IMODE(os.stat(os.path.join(h, backs[0])).st_mode) == mode
    assert open(os.path.join(h, backs[0]), "rb").read() == b"model:\n  default: x\n"                     # 内容是改之前的原样


def test_copy_with_same_mode_never_leaves_a_half_written_file_and_refuses_to_overwrite(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x")
    os.chmod(src, 0o600)
    HS.copy_with_same_mode(src, dst)
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600 and dst.read_text() == "x"
    with pytest.raises(FileExistsError):
        HS.copy_with_same_mode(src, dst)


# ---------------------------------------------------------------- 12 LM Studio 的编码身份
LMS_MODEL = CT.runtime("lmstudio")["model"]
REL = "Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"
RT_LS = ("Available Runtimes\n  llama.cpp-mac-arm64-apple-metal-advsimd@2.53.0\n  llama.cpp-mac-arm64-apple-metal-advsimd@2.73.0  ✓\n  mlx-llm-mac-arm64-apple-metal-advsimd@1.0.0\n")
ART = " _     __  __ ____  \n| |   |  \\/  / ___| \n"                                                                   # lms version 打印的字符画


def lms_fake(ps_ctx=2048, runtime_out=RT_LS, models=None, ps=True, rel=REL):
    def lms(argv):
        if argv[1:3] == ["runtime", "ls"]:
            return (0, runtime_out, "") if runtime_out is not None else (1, "", "boom")
        if argv[1] == "version":
            return 0, ART, ""
        if argv[1] == "ls":
            return 0, json.dumps(models if models is not None else [{"modelKey": LMS_MODEL, "quantization": "Q8_0", "sizeBytes": 639000000, "path": rel, "maxContextLength": 32768}]), ""
        if argv[1] == "ps":
            return (0, json.dumps([{"identifier": LMS_MODEL, "modelKey": LMS_MODEL, "path": rel, "contextLength": ps_ctx}]), "") if ps else (0, "[]", "")
        return 0, "", ""
    return lms


def lms_world(tmp_path, content=b"fake-gguf-bytes", where=(".lmstudio", "models"), **kw):
    home = tmp_path / "lmhome"
    f = home.joinpath(*where) / REL
    f.parent.mkdir(parents=True)
    f.write_bytes(content)
    http = FakeHTTP("lmstudio", models=[LMS_MODEL])
    s = FakeSystem(http=http, commands={"lms": lms_fake(**kw)}, env={"HOME": str(home)})
    return s, home, hashlib.sha256(content).hexdigest()


def verify(s):
    return ES.verify(s, CT.runtime("lmstudio"), LMS_MODEL, "http://127.0.0.1:1234/v1", cmd_path="/fake/bin/lms", expected_dim=CT.EMBED_MODEL_DIM)


def test_lmstudio_relative_path_is_joined_onto_the_models_dir_and_identity_is_strong(tmp_path):
    s, home, sha = lms_world(tmp_path)
    r = verify(s)
    assert r["ok"] and r["weak"] is False and r["weak_reasons"] == []
    i = r["identity"]
    assert i["model_file_sha256"] == sha and i["runtime_version"] == "llama.cpp-mac-arm64-apple-metal-advsimd@2.73.0" and i["context_length"] == 2048    # 上下文取加载实例的，不是 32768


def test_downloads_folder_from_settings_json_wins_and_only_that_key_is_used(tmp_path):
    home = tmp_path / "lmhome"
    custom = tmp_path / "elsewhere" / "models"
    (custom / os.path.dirname(REL)).mkdir(parents=True)
    (custom / REL).write_bytes(b"custom-location")
    (home / ".lmstudio").mkdir(parents=True)
    (home / ".lmstudio" / "settings.json").write_text(json.dumps({"downloadsFolder": str(custom), "somethingElse": "ignored", "token": "SHOULD-NEVER-BE-USED"}))
    s = FakeSystem(http=FakeHTTP("lmstudio", models=[LMS_MODEL]), commands={"lms": lms_fake()}, env={"HOME": str(home)})
    r = verify(s)
    assert r["identity"]["model_file_sha256"] == hashlib.sha256(b"custom-location").hexdigest() and r["weak"] is False
    assert ES.lms_models_dirs(s)[0] == str(custom) and len(ES.lms_models_dirs(s)) == 3


def test_old_models_location_is_still_found(tmp_path):
    s, home, sha = lms_world(tmp_path, where=(".cache", "lm-studio", "models"))
    assert verify(s)["identity"]["model_file_sha256"] == sha


def test_model_file_not_found_means_weak_identity_and_says_which_part(tmp_path):
    s, home, sha = lms_world(tmp_path, rel="No/Such/model.gguf")
    r = verify(s)
    assert r["ok"] and r["weak"] and "model_file_sha256" in r["weak_reasons"] and r["identity"]["model_file_sha256"].startswith("weak:")
    assert "取不到模型文件的 SHA-256" in WZ.weak_note({"weak_reasons": r["weak_reasons"]})


def test_runtime_version_comes_from_the_checked_line_never_from_the_version_art(tmp_path):
    s, home, sha = lms_world(tmp_path, runtime_out=None)                                                  # lms runtime ls 失败
    r = verify(s)
    assert r["identity"]["runtime_version"] == ES.WEAK and "runtime_version" in r["weak_reasons"] and r["weak"]
    assert "LM" not in r["identity"]["runtime_version"] and "__" not in json.dumps(r["identity"])        # 字符画哪怕一个字也没进身份
    s2, home2, _ = lms_world(tmp_path / "b", runtime_out="llama.cpp@1.0.0\nmlx-llm@2.0.0\n")             # 没有 ✓：不猜
    assert verify(s2)["identity"]["runtime_version"] == ES.WEAK
    two = "llama.cpp-mac@2.73.0 ✓\nmlx-llm-mac@1.0.0 ✓\n"                                                # 两类引擎各有 ✓：向量走 llama.cpp 那一类
    s3, _, _ = lms_world(tmp_path / "c", runtime_out=two)
    assert verify(s3)["identity"]["runtime_version"] == "llama.cpp-mac@2.73.0"
    s4, _, _ = lms_world(tmp_path / "d", runtime_out="mlx-a@1.0.0 ✓\nmlx-b@2.0.0 ✓\n")                    # 挑不出唯一的：不猜
    assert verify(s4)["identity"]["runtime_version"] == ES.WEAK


def test_without_a_loaded_instance_the_max_context_is_used_but_marked_as_an_upper_bound(tmp_path):
    s, home, sha = lms_world(tmp_path, ps=False)
    r = verify(s)
    assert r["identity"]["context_length"] == 32768 and r["weak"] and r["weak_reasons"] == ["context_length_is_upper_bound"]
    assert "上限" in WZ.weak_note({"weak_reasons": r["weak_reasons"]})


def test_identity_differs_from_the_tested_one_when_the_runtime_or_context_differs(tmp_path):
    """测过的身份是 llama.cpp …@2.53.0、上下文 2048。运行包是 2.73.0 时身份里的 runtime_version 就不一样，指纹也不一样：只有完全一样才能写「测过」。"""
    s, home, sha = lms_world(tmp_path)
    cur = verify(s)["identity"]
    tested = dict(cur, runtime_version="llama.cpp-mac-arm64-apple-metal-advsimd@2.53.0")
    from yuanwei import vectors as VX
    assert VX.identity_fp(cur) != VX.identity_fp(tested)
    assert VX.identity_fp(dict(cur, context_length=2048)) == VX.identity_fp(cur)


# ---------------------------------------------------------------- 12 续：「测过」按身份比对三档（主会话 11-11 定）
TESTED = CT.TESTED_EMBED_IDENTITY


def test_the_tested_identity_constant_is_exactly_what_was_decided():
    from yuanwei import vectors as VX
    assert TESTED == {"provider": "lmstudio", "model": "text-embedding-qwen3-embedding-0.6b",
                      "model_file_sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
                      "runtime_version": "llama.cpp-mac-arm64-apple-metal-advsimd@2.53.0", "context_length": 2048,
                      "dimension": 1024, "normalization": "l2", "prefix": "none", "template": "none", "text_processing": "verbatim"}
    assert set(TESTED) == set(VX.IDENTITY_FIELDS)


def test_compare_three_levels_and_which_items_differ():
    t = CT.compare_to_tested(dict(TESTED))
    assert t["level"] == "tested" and t["differs"] == [] and t["text"] == "测过（和测过的设置完全一样）"
    e = CT.compare_to_tested(dict(TESTED, runtime_version="llama.cpp-mac-arm64-apple-metal-advsimd@2.73.0"))
    assert e["level"] == "engine_differs" and e["differs"] == ["runtime_version"]
    assert e["text"] == "同一个模型文件和设置；LM Studio 的引擎版本不同（测过的是 2.53.0，你的是 2.73.0），算出的向量可能有极小差别，这个版本没核过"
    for bad, names in ((dict(TESTED, model_file_sha256="a" * 64), ["model_file_sha256"]), (dict(TESTED, context_length=4096), ["context_length"]), (dict(TESTED, dimension=768), ["dimension"]),
                       (dict(TESTED, model_file_sha256="a" * 64, context_length=32768, runtime_version="x@9"), ["model_file_sha256", "runtime_version", "context_length"])):
        r = CT.compare_to_tested(bad)
        assert r["level"] == "untested" and r["differs"] == names and r["text"].startswith("没测过")
    r = CT.compare_to_tested(dict(TESTED, model_file_sha256="weak:abc"), weak=True)
    assert r["level"] == "untested" and "模型文件" in r["text"]
    r = CT.compare_to_tested(dict(TESTED, runtime_version="unreported"), weak=True)                       # 取不到运行包：弱，不是「引擎版本不同」
    assert r["level"] == "untested" and "引擎版本" in r["text"]
    assert CT.compare_to_tested(dict(TESTED), weak=True)["level"] == "untested"                         # 逐项都一样但标了弱（比如上下文只取到上限）：不写测过
    for other in ("ollama", "llamacpp", "custom"):
        r = CT.compare_to_tested(dict(TESTED, provider=other))
        assert r["level"] == "untested" and r["applicable"] is False and r["text"].startswith("没测过")
    assert CT.compare_to_tested(None)["level"] == "untested"


def _tested_world(tmp_path, monkeypatch, runtime=RT_LS, **kw):
    s, home, sha = lms_world(tmp_path, runtime_out=runtime, **kw)
    monkeypatch.setitem(CT.TESTED_EMBED_IDENTITY, "model_file_sha256", sha)                         # 测试里的模型文件是假的：把「测过的 SHA」换成它的，只为走通比对
    return s


def test_verify_gives_tested_when_everything_is_the_same_as_the_tested_setup(tmp_path, monkeypatch):
    s = _tested_world(tmp_path, monkeypatch, runtime=RT_LS.replace("@2.73.0  ✓", "@2.73.0").replace("@2.53.0", "@2.53.0  ✓"))
    r = verify(s)
    assert r["tested"]["level"] == "tested" and r["identity"]["runtime_version"].endswith("@2.53.0")


def test_verify_gives_engine_differs_when_only_the_runtime_pack_differs(tmp_path, monkeypatch):
    r = verify(_tested_world(tmp_path, monkeypatch))                                                   # 带 ✓ 的是 2.73.0
    assert r["tested"]["level"] == "engine_differs" and "你的是 2.73.0" in r["tested"]["text"]


def test_verify_gives_untested_for_wrong_file_weak_or_different_context(tmp_path, monkeypatch):
    s, home, sha = lms_world(tmp_path)                                                                 # 假文件的 SHA 不是测过的
    r = verify(s)
    assert r["tested"]["level"] == "untested" and "model_file_sha256" in r["tested"]["differs"] and "模型文件" in r["tested"]["text"]
    r = verify(_tested_world(tmp_path / "b", monkeypatch, ps_ctx=4096))
    assert r["tested"]["level"] == "untested" and "context_length" in r["tested"]["differs"] and "加载的上下文长度" in r["tested"]["text"]
    r = verify(_tested_world(tmp_path / "c", monkeypatch, ps=False))                                    # 只取到上限：弱
    assert r["weak"] and r["tested"]["level"] == "untested"
    r = verify(_tested_world(tmp_path / "d", monkeypatch, runtime=None))                                # 运行包取不到：弱，不是「引擎版本不同」
    assert r["weak"] and r["tested"]["level"] == "untested"


def _lms_setup(tmp_path, monkeypatch, **kw):
    s = _tested_world(tmp_path, monkeypatch, **kw)
    s.commands["py"] = lambda argv: (0, "2.0", "")
    s.env["DEEPSEEK_API_KEY"] = FAKE_KEY
    h = make_home(tmp_path, "hh")
    return s, h, ["setup", "--host", "hermes", "--preset", "recommended", "--vectors", "lmstudio", "--hermes-home", h, "--hermes-python", "/fake/bin/py"] + BASE


def test_plan_final_line_and_status_all_say_the_same_tier(tmp_path, monkeypatch, capsys):
    s, h, args = _lms_setup(tmp_path, monkeypatch)
    assert cli._setup_main(args, sys_=s) == 0
    plan = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "测过吗（核验前的预判，以核验后为准）：同一个模型文件和设置；LM Studio 的引擎版本不同" in plan
    assert cli._setup_main(args + ["--yes", "--install-numpy"], sys_=s) == 0
    capsys.readouterr()
    st = HS.install_status(h)
    tc = st["vectors"]["tested_comparison"]
    assert tc["level"] == "engine_differs" and tc["differs"] == ["runtime_version"] and "引擎版本不同" in tc["text"]


def test_wizard_prints_the_tier_line_after_verification(tmp_path, monkeypatch):
    s, h, _ = _lms_setup(tmp_path, monkeypatch)
    io = FakeIO(["1", "1", "y", "y", "y", "y", "1", "y", "y"])                                          # 助手、推荐、DeepSeek、用已有密钥、开向量、用 LM Studio、范围、本机、确认
    cli._setup_main(["setup", "--hermes-home", h, "--hermes-python", "/fake/bin/py"] + BASE, io=io, sys_=s)
    assert "向量：同一个模型文件和设置；LM Studio 的引擎版本不同" in io.text and "测过吗（核验前的预判" in io.text


def test_other_runtimes_stay_untested_in_plan_and_status(tmp_path, capsys):
    from test_vector_setup import mk_sys, ollama_cmds
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http), py=lambda a: (0, "2.0", ""))
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "recommended", "--vectors", "ollama", "--hermes-home", h, "--hermes-python", "/fake/bin/py"] + BASE, sys_=s) == 0
    assert "测过吗：没测过（Ollama 没有测过的向量身份）" in "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "recommended", "--vectors", "ollama", "--yes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"] + BASE, sys_=s) == 0
    tc = HS.install_status(h)["vectors"]["tested_comparison"]
    assert tc["level"] == "untested" and tc["applicable"] is False
