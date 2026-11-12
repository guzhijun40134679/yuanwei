"""密钥（0600 文件、不回显、先文件后环境变量、不进任何输出）和环境检查（Python、Hermes、整理密钥有没有）。"""
import json
import os
import stat
import subprocess
import sys

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import cli
from yuanwei import keystore as KS
from yuanwei import wizard as WZ
from yuanwei.config import Config, api_key
from yuanwei.errors import ConfigError

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]


def test_key_set_refuses_when_stdin_or_stdout_is_not_a_terminal(tmp_path, capsys):
    h = make_home(tmp_path)
    assert cli._key_main(["set", "--hermes-home", h], io=FakeIO(tty=False), sys_=FakeSystem()) == 2
    err = capsys.readouterr().err
    assert "自己的终端" in err and not os.path.exists(os.path.join(h, "yuanwei", "keys"))


def test_key_input_is_not_echoed_uses_getpass_like_input(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(answers=["n"], secrets=[FAKE_KEY, FAKE_KEY])
    assert cli._key_main(["set", "--hermes-home", h], io=io, sys_=FakeSystem()) == 0
    assert all(FAKE_KEY not in a for a in io.asked) and FAKE_KEY not in io.text
    assert sum(1 for a in io.asked if a.startswith("[secret]")) == 2                 # 两次都走不回显的接口
    assert open(KS.global_key_file("DEEPSEEK_API_KEY")).read().strip() == FAKE_KEY            # 默认存全局，不存进这个机器人自己的目录
    assert not os.path.exists(os.path.join(h, "yuanwei", "keys"))


def test_key_file_is_0600_in_0700_dir_and_written_atomically(tmp_path):
    h = make_home(tmp_path)
    p = KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
    assert [f for f in os.listdir(os.path.dirname(p))] == ["DEEPSEEK_API_KEY"]        # 没有留临时文件
    with pytest.raises(ConfigError):
        KS.write_key(h, "DEEPSEEK_API_KEY", "bad value with spaces")
    assert open(p).read().strip() == FAKE_KEY                                          # 失败不动旧文件
    assert [f for f in os.listdir(os.path.dirname(p))] == ["DEEPSEEK_API_KEY"]


def test_key_file_with_loose_permissions_or_foreign_owner_is_refused_and_status_says_so(tmp_path, capsys):
    h = make_home(tmp_path)
    p = KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    os.chmod(p, 0o644)
    ok, why = KS.check_file(p)
    assert not ok and why == "bad_mode"
    v, src = KS.lookup("DEEPSEEK_API_KEY", p, {})
    assert v is None and src == "file_unusable:bad_mode"
    with pytest.raises(ConfigError) as e:
        api_key("DEEPSEEK_API_KEY", p)
    assert FAKE_KEY not in str(e.value)
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    out = capsys.readouterr().out
    assert "bad_mode" in out and FAKE_KEY not in out


def test_key_value_validation_rejects_whitespace_non_ascii_too_short_without_echo():
    for bad, why in (("abc def ghijk", "has_whitespace"), ("密钥密钥密钥密钥密钥", "not_ascii"), ("short", "too_short"), ("", "empty"), ("x" * 600, "too_long")):
        v, w = KS.validate_value(bad)
        assert v is None and w == why
    assert KS.validate_value("  " + FAKE_KEY + "\n") == (FAKE_KEY, None)


def test_key_confirmation_mismatch_retries_up_to_three_times_then_cancels(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(secrets=[FAKE_KEY, "sk-OTHER-1234567890abcdef"] * 3)
    r = WZ._enter_key(io, {"choice": "env_or_file", "value": None, "test": False})
    assert r["value"] is None and "三次都没成功" in io.text and not os.path.exists(os.path.join(h, "yuanwei"))
    assert cli._key_main(["set", "--hermes-home", h], io=FakeIO(secrets=[FAKE_KEY, "sk-OTHER-1234567890abcdef"] * 3), sys_=FakeSystem()) == 1
    assert not os.path.exists(os.path.join(h, "yuanwei", "keys", "DEEPSEEK_API_KEY"))


def test_lookup_order_file_then_env_and_status_reports_source_only(tmp_path, capsys):
    h = make_home(tmp_path)
    p = KS.key_file(h, "DEEPSEEK_API_KEY")
    assert KS.lookup("DEEPSEEK_API_KEY", p, {"DEEPSEEK_API_KEY": "sk-ENVKEY-1234567890"}) == ("sk-ENVKEY-1234567890", "env")
    KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    assert KS.lookup("DEEPSEEK_API_KEY", p, {"DEEPSEEK_API_KEY": "sk-ENVKEY-1234567890"}) == (FAKE_KEY, "file")
    KS.delete_key(h, "DEEPSEEK_API_KEY")
    with pytest.raises(ConfigError) as e:
        api_key("DEEPSEEK_API_KEY", p)
    assert "DEEPSEEK_API_KEY" in str(e.value)
    KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    assert cli._key_main(["status", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    out = capsys.readouterr().out
    assert '"source": "file"' in out and FAKE_KEY not in out


def test_param_mode_has_no_api_key_flag_and_api_key_env_reads_only_the_named_variable(tmp_path, capsys):
    h = make_home(tmp_path)
    sec = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    for flag in ("--api-key", "--key"):
        assert cli._setup_main(["setup", "--host", "hermes", flag, sec, "--hermes-home", h] + BASE, sys_=FakeSystem()) == 2      # 参数里有密钥：固定报错，不进 argparse
        out = capsys.readouterr()
        assert sec not in out.out + out.err and "不从命令行收密钥" in out.err
    for flag in ("--api-key", "--key"):                                                                                         # 不像密钥的值：argparse 不认识这个参数，报错也不回显值
        with pytest.raises(SystemExit):
            cli._setup_main(["setup", "--host", "hermes", flag, "plain-value", "--hermes-home", h] + BASE, sys_=FakeSystem())
        capsys.readouterr()
    sysx = FakeSystem(env={"MY_OWN_VAR": FAKE_KEY, "DEEPSEEK_API_KEY": "sk-should-not-be-looked-at"})
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--api-key-env", "MY_OWN_VAR", "--yes", "--hermes-home", h] + BASE, sys_=sysx) == 0
    out = capsys.readouterr().out
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    assert cfg.provider["api_key_env"] == "MY_OWN_VAR" and FAKE_KEY not in out and "should-not" not in out


def test_key_test_asks_before_sending_and_declines_sends_nothing(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP()
    io = FakeIO(answers=["n"], secrets=[FAKE_KEY, FAKE_KEY])
    assert cli._key_main(["set", "--hermes-home", h], io=io, sys_=FakeSystem(http=http)) == 0
    assert http.calls == []
    http2 = FakeHTTP()
    io2 = FakeIO(answers=["n"])
    assert cli._key_main(["test", "--hermes-home", h], io=io2, sys_=FakeSystem(http=http2)) == 1
    assert http2.calls == [] and "已取消" in io2.text


def test_key_test_reports_only_works_or_not_works_or_unverified(tmp_path):
    h = make_home(tmp_path)
    KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    for status, word in ((200, "能用。"), (401, "不能用"), (403, "不能用"), (429, "没验成"), (500, "没验成")):
        http = FakeHTTP(ports=None, chat_status=status, chat_reply="BODY-SHOULD-NOT-APPEAR")
        io = FakeIO(answers=["y"])
        assert cli._key_main(["test", "--hermes-home", h], io=io, sys_=FakeSystem(http=http)) == 0
        assert word in io.text and "BODY-SHOULD" not in io.text and FAKE_KEY not in io.text
        assert http.posted("chat/completions")[0]["auth"] is True
    http = FakeHTTP(down=True)
    io = FakeIO(answers=["y"])
    assert cli._key_main(["test", "--hermes-home", h], io=io, sys_=FakeSystem(http=http)) == 0 and "没验成" in io.text


def test_key_delete_requires_yes_and_uninstall_does_not_remove_key_file(tmp_path, capsys):
    h = make_home(tmp_path)
    p = KS.write_global_key("DEEPSEEK_API_KEY", FAKE_KEY)
    assert cli._key_main(["delete", "--hermes-home", h], io=FakeIO(), sys_=FakeSystem()) == 0
    assert os.path.exists(p)
    assert cli._key_main(["delete", "--hermes-home", h, "--yes"], io=FakeIO(), sys_=FakeSystem()) == 0
    assert not os.path.exists(p)


def test_windows_like_platform_falls_back_to_env_only_and_says_so(tmp_path, monkeypatch, capsys):
    h = make_home(tmp_path)
    monkeypatch.setattr(KS, "supported", lambda: False)
    with pytest.raises(ConfigError):
        KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    io = FakeIO(secrets=[FAKE_KEY, FAKE_KEY])
    assert cli._key_main(["set", "--hermes-home", h], io=io, sys_=FakeSystem()) == 2
    assert "环境变量" in capsys.readouterr().err and not os.path.exists(os.path.join(h, "yuanwei", "keys"))


def test_background_process_reads_key_file_itself_not_from_argv_or_environment(tmp_path):
    """真起一个子进程（只连本机假上游，没有联网）：它的命令行和环境里都没有密钥，它自己从文件读到并带上 Authorization。"""
    import http.server
    import threading
    h = make_home(tmp_path)
    KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            body = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from yuanwei.provider import OpenAICompatProvider\n"
                "p = OpenAICompatProvider('http://127.0.0.1:%d/v1', 'DEEPSEEK_API_KEY', {'model': 'm'}, 10, key_file=%r)\n"
                "print(p({'text': 'hi'})['text'])\n") % (os.path.join(os.path.dirname(__file__), "..", "src"), srv.server_address[1], KS.key_file(h, "DEEPSEEK_API_KEY"))
        env = {k: v for k, v in os.environ.items() if "KEY" not in k}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
        assert seen["auth"] == "Bearer " + FAKE_KEY
        assert FAKE_KEY not in code and "DEEPSEEK_API_KEY" not in env
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 环境检查
def test_python_too_old_gives_clear_message_and_stops(tmp_path, monkeypatch, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=FakeSystem()) == 2
    assert "python_too_old" in capsys.readouterr().err and tree_bytes(h) == before


def test_hermes_not_found_gives_clear_message_and_stops(tmp_path, capsys):
    h = str(tmp_path / "nothere")
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=FakeSystem()) == 2
    err = capsys.readouterr().err
    assert "hermes_not_found" in err and "--hermes-home" in err and not os.path.exists(h)


def test_missing_key_gives_clear_message_and_three_choices(tmp_path):
    h = make_home(tmp_path)
    io = FakeIO(["1", "2", "y", "3", "1", "y", "n"])          # 助手、轻量、用DeepSeek、密钥选「以后再说」、范围、本机、不确认
    cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=FakeSystem())
    t = io.text
    assert "没找到 DEEPSEEK_API_KEY" in t and "现在在这里输入" in t and "我自己去设" in t and "以后再说" in t and "没有 DEEPSEEK_API_KEY（整理没有它没法跑" in t


def test_fake_key_value_never_appears_in_any_output_log_file_or_exception(tmp_path, capsys):
    h = make_home(tmp_path)
    open(os.path.join(h, ".env"), "w").write("OTHER=1\nDEEPSEEK_API_KEY=%s\n" % FAKE_KEY)
    sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
    io = FakeIO(["1", "2", "y", "y", "1", "y", "y"])                                       # …已有密钥：用它 y
    assert cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=sysx) == 0
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=sysx) == 0
    out = capsys.readouterr()
    assert FAKE_KEY not in io.text and FAKE_KEY not in out.out and FAKE_KEY not in out.err
    for rel, b in tree_bytes(h).items():
        if rel == ".env":
            continue
        assert FAKE_KEY.encode() not in b, rel


def test_dotenv_scan_returns_only_a_bool_and_does_not_keep_values(tmp_path):
    f = tmp_path / ".env"
    f.write_text("export DEEPSEEK_API_KEY=%s\n" % FAKE_KEY)
    r = KS.dotenv_has_name(str(f), "DEEPSEEK_API_KEY")
    assert r is True and isinstance(r, bool)
    f.write_text("DEEPSEEK_API_KEY=\n# DEEPSEEK_API_KEY=abc\n")
    assert KS.dotenv_has_name(str(f), "DEEPSEEK_API_KEY") is False
    assert KS.dotenv_has_name(str(tmp_path / "nope"), "X") is False


def test_own_environment_missing_but_dotenv_has_name_is_reported_as_found_not_unset(tmp_path, capsys):
    h = make_home(tmp_path)
    open(os.path.join(h, ".env"), "w").write("DEEPSEEK_API_KEY=%s\n" % FAKE_KEY)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE, sys_=FakeSystem(env={})) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "Hermes 的设置文件里的 DEEPSEEK_API_KEY" in lines and "没有 DEEPSEEK_API_KEY（整理没有它" not in lines and FAKE_KEY not in lines


def test_unreadable_dotenv_or_external_secret_source_is_reported_as_not_checked_not_as_unset(tmp_path, capsys):
    h = make_home(tmp_path)
    os.makedirs(os.path.join(h, ".env"))                      # .env 是个目录：读不了
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE, sys_=FakeSystem(env={})) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "没核到" in lines and "没有 DEEPSEEK_API_KEY（整理没有它" not in lines
