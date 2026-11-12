"""命令行参数里的密钥：不进 argparse（它报「不认识的参数」时会把值原样打印出来），给一句固定的报错，退出码 2；argparse 的报错和用法提示也先过密钥过滤（第二道防线）。"""
import json
import os

import pytest

from helpers_wizard import FAKE_KEY, FakeSystem, make_home, tree_bytes
from yuanwei import cli

SEC = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
MSG = "命令行参数里好像有密钥。原委不从命令行收密钥：请用 `yuanwei key set` 交互输入，或用 `--api-key-env 变量名`。"
BASE = ["--db", "/nonexistent/x.sqlite", "--workspace", "w"]
# 每个子命令各一条基础命令；密钥放在不同位置
COMMANDS = {
    "setup": ["setup", "--host", "hermes", "--preset", "light"],
    "setup-legacy": ["setup", "hermes"],
    "uninstall": ["uninstall", "hermes"],
    "key": ["key", "status"],
    "vectors": ["vectors", "status"],
    "conversations": ["conversations", "list", "--db", "/nonexistent/x.sqlite"],
    "secrets": ["secrets", "scan", "--db", "/nonexistent/x.sqlite"],
    "doctor": ["doctor", "hermes"],
    "status-hermes": ["status", "hermes"],
    "record": BASE + ["record", "--host", "h", "--session", "s", "--message-id", "m", "--role", "user"],
    "recall": BASE + ["recall"],
    "status": BASE + ["status"],
}
PLACES = {
    "flag_value": lambda s: ["--api-key", s],
    "positional": lambda s: [s],
    "equals_form": lambda s: ["--x=" + s],
    "known_flag_value": lambda s: ["--hermes-home", s],
    "api_key_env_value": lambda s: ["--api-key-env", s],
    "api_key_equals": lambda s: ["--api-key=" + s],
}


@pytest.mark.parametrize("name", sorted(COMMANDS))
@pytest.mark.parametrize("place", sorted(PLACES))
def test_secret_in_argv_gives_fixed_error_exit_2_and_never_appears_in_output(name, place, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = COMMANDS[name] + PLACES[place](SEC)
    rc = cli.main(argv)
    out = capsys.readouterr()
    assert rc == 2
    assert SEC not in out.out and SEC not in out.err and "A1b2C3d4E5f6G7h8I9j0K1l2" not in out.out + out.err
    assert json.loads(out.err)["message"] == MSG and json.loads(out.err)["error"] == "secret_in_argv" and out.out == ""
    assert os.listdir(str(tmp_path)) == []                                  # 什么都没跑、没建任何东西


def test_secret_in_argv_does_not_run_the_command_even_if_the_value_would_be_a_valid_path(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    bogus = str(tmp_path / SEC)                                              # 密钥放在 --hermes-home 里：不过滤的话 argparse 会收下它当路径，然后真的去装
    assert cli.main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", bogus, "--config-method", "yaml", "--src-path", "/x"]) == 2
    out = capsys.readouterr()
    assert SEC not in out.out + out.err and not os.path.exists(bogus) and tree_bytes(h) == before


def test_direct_entry_points_have_the_same_guard(tmp_path, capsys):
    h = make_home(tmp_path)
    for fn, argv in ((cli._setup_main, ["setup", "--host", "hermes", "--api-key", SEC, "--hermes-home", h]), (cli._key_main, ["set", "--x=" + SEC]), (cli._vectors_main, ["status", SEC])):
        assert fn(argv) == 2
        out = capsys.readouterr()
        assert SEC not in out.out + out.err and "不从命令行收密钥" in out.err


def test_api_key_env_with_a_variable_name_still_works_in_every_spelling(tmp_path, capsys):
    h = make_home(tmp_path)
    sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
    for spelling in (["--api-key-env", "DEEPSEEK_API_KEY"], ["--api-key-env=DEEPSEEK_API_KEY"]):
        assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x"] + spelling, sys_=sysx) == 0
        assert capsys.readouterr().err == ""
    assert cli.main(["key", "status", "--hermes-home", h, "--api-key-env", "DEEPSEEK_API_KEY"]) == 0
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().out


def test_argparse_errors_and_usage_are_redacted_as_the_second_line_of_defense(capsys):
    """不经过入口的 argv 过滤，直接让 argparse 报错：错误里带着一串密钥形状的值，输出前也会被遮掉。"""
    ap = cli.SafeParser(prog="yuanwei demo")
    ap.add_argument("mode", choices=["a", "b"])
    with pytest.raises(SystemExit) as e:
        ap.parse_args([SEC])
    assert e.value.code == 2
    out = capsys.readouterr()
    assert SEC not in out.out + out.err and "invalid choice" in out.err and "密钥已挡掉" in out.err
    ap2 = cli.SafeParser(prog="yuanwei demo2 %s" % SEC)                       # 用法行里带着也会被遮
    ap2.add_argument("x", choices=["a"])
    with pytest.raises(SystemExit):
        ap2.parse_args(["zzz"])
    out = capsys.readouterr()
    assert SEC not in out.out + out.err


def test_every_parser_in_cli_is_a_safe_parser():
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "yuanwei", "cli.py"), encoding="utf-8").read()
    assert "argparse.ArgumentParser(" not in src.replace("class SafeParser(argparse.ArgumentParser)", "")
    assert len(re.findall(r"SafeParser\(prog=", src)) >= 8
