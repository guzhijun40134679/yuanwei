"""安装向导：一问一答和全参数两种写法、不加 --yes 什么都不改、取消什么都不改、现有 setup hermes 不变、计划里列出每个选择、执行顺序、TOML。"""
import json
import os
import sys
import tomllib

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import cli
from yuanwei import hermes_setup as HS
from yuanwei.config import Config

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]


def run(argv, sys_=None, io=None):
    return cli._setup_main(argv, io=io, sys_=sys_ or FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY}))


def light_script():
    # 助手 1；预设 2（轻量）；用 DeepSeek y；已有密钥「用它吗」y；读取范围 1；只在本机终端 y；确认 y
    return ["1", "2", "y", "y", "1", "y", "y"]


def test_wizard_scripted_answers_and_all_flags_change_exactly_the_same_files(tmp_path):
    h1, h2 = make_home(tmp_path, "a"), make_home(tmp_path, "b")
    io = FakeIO(light_script())
    assert run(["setup", "--hermes-home", h1] + BASE, io=io) == 0
    assert run(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h2] + BASE) == 0
    t1, t2 = tree_bytes(h1), tree_bytes(h2)
    norm = lambda d: {k: v.replace(h1.encode(), b"HOME").replace(h2.encode(), b"HOME").replace(b"\"", b"'") for k, v in d.items() if not k.startswith("config.yaml.yuanwei-backup")}
    assert set(norm(t1)) == set(norm(t2)) and "yuanwei/yuanwei.toml" in t1
    for k in norm(t1):
        if k == "yuanwei/install.json":
            continue
        assert norm(t1)[k] == norm(t2)[k], k


def test_without_yes_nothing_changes_only_the_plan_is_printed(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
    assert run(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE, sys_=sysx) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and any("整理" in l for l in out["plan"]["lines"])
    assert tree_bytes(h) == before and sysx.ran == [] and sysx.http_fake.calls == []


def test_cancel_at_every_step_changes_nothing(tmp_path):
    full = light_script()
    for n in range(len(full)):
        h = make_home(tmp_path, "c%d" % n)
        before = tree_bytes(h)
        sysx = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})
        io = FakeIO(full[:n])                              # 回答到第 n 个就没了 → 取消（和输入 q、EOF 一样）
        rc = run(["setup", "--hermes-home", h] + BASE, sys_=sysx, io=io)
        assert rc == 1 and "什么都没改" in io.text, n
        assert tree_bytes(h) == before and sysx.ran == []
    h = str(tmp_path / "q")
    os.makedirs(h)
    io = FakeIO(["1", "q"])
    assert run(["setup", "--hermes-home", h] + BASE, io=io) == 1 and tree_bytes(h) == {}


def test_legacy_setup_hermes_tests_pass_unchanged_and_plan_output_has_no_new_keys(tmp_path, capsys):
    h = make_home(tmp_path)
    assert run(["setup", "hermes", "--hermes-home", h] + BASE) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"applied", "plan", "hint"} and "lines" not in out["plan"]
    assert not [a for a in out["plan"]["actions"] if "yuanwei.toml" in a.get("path", "")]
    assert run(["setup", "hermes", "--hermes-home", h, "--yes"] + BASE) == 0
    assert "yuanwei/yuanwei.toml" not in tree_bytes(h)


def test_setup_without_host_in_non_tty_refuses_with_exit_2(tmp_path, capsys):
    h = make_home(tmp_path)
    assert run(["setup", "--hermes-home", h] + BASE, io=FakeIO(tty=False)) == 2
    err = capsys.readouterr().err
    assert "not_a_terminal" in err and "--host hermes" in err and tree_bytes(h) == {"config.yaml": b"model:\n  default: x\n"}


def test_future_hosts_claude_code_codex_pi_are_listed_but_not_selectable(tmp_path, capsys):
    h = make_home(tmp_path)
    for host in ("claude-code", "codex", "pi"):
        assert run(["setup", "--host", host, "--preset", "light", "--hermes-home", h] + BASE) == 2
        assert "future_host" in capsys.readouterr().err
        assert run(["setup", host, "--hermes-home", h] + BASE) == 2
        assert "future_host" in capsys.readouterr().err
    io = FakeIO(["2"])
    assert run(["setup", "--hermes-home", h] + BASE, io=io) == 2 or "后续更新" in io.text


def test_plan_lists_every_choice_provider_model_params_key_source_vectors_files_and_the_one_setting_line(tmp_path, capsys):
    h = make_home(tmp_path)
    assert run(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    for must in ("DeepSeek", "deepseek-flash", "测过", "temperature", "reasoning_effort", "max_tokens", "人民币", "环境变量 DEEPSEEK_API_KEY", "不开", "yuanwei.sqlite", "yuanwei.toml", "memory.provider", "读取范围"):
        assert must in lines, must


def test_generated_toml_roundtrips_through_tomllib_and_config_loads_it(tmp_path):
    h = make_home(tmp_path)
    assert run(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE) == 0
    p = os.path.join(h, "yuanwei", "yuanwei.toml")
    d = tomllib.loads(open(p, encoding="utf-8").read())
    cfg = Config.load(p)
    assert cfg.retrieval == "keyword" and cfg.provider["params"] == {"temperature": 0, "reasoning_effort": "high", "max_tokens": 65536, "model": "deepseek-flash"}
    assert d["provider"]["id"] == "deepseek" and cfg.provider["api_key_env"] == "DEEPSEEK_API_KEY" and FAKE_KEY not in open(p).read()
    assert json.load(open(os.path.join(h, "yuanwei", "config.json")))["config_path"] == p


def test_user_given_config_toml_is_never_modified_lines_are_printed_instead(tmp_path, capsys):
    h = make_home(tmp_path)
    user = tmp_path / "mine.toml"
    user.write_text('retrieval = "keyword"\n', encoding="utf-8")
    assert run(["setup", "--host", "hermes", "--preset", "light", "--yes", "--config", str(user), "--hermes-home", h] + BASE) == 0
    assert user.read_text() == 'retrieval = "keyword"\n' and "yuanwei/yuanwei.toml" not in tree_bytes(h)
    assert json.load(open(os.path.join(h, "yuanwei", "config.json")))["config_path"] == str(user)


def test_uninstall_removes_toml_when_unmodified_and_keeps_key_file_and_db(tmp_path):
    from yuanwei import keystore as KS
    h = make_home(tmp_path)
    assert run(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE) == 0
    KS.write_key(h, "DEEPSEEK_API_KEY", FAKE_KEY)
    open(os.path.join(h, "yuanwei", "yuanwei.sqlite"), "w").write("db")
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--yes", "--config-method", "yaml"]) == 0
    assert cli._setup_main(["uninstall", "hermes", "--hermes-home", h, "--yes", "--host-reloaded", "--config-method", "yaml"]) == 0
    t = tree_bytes(h)
    assert "yuanwei/yuanwei.toml" not in t and "yuanwei/config.json" not in t
    assert "yuanwei/keys/DEEPSEEK_API_KEY" in t and "yuanwei/yuanwei.sqlite" in t


def test_non_light_preset_param_mode_requires_vectors_choice_and_custom_lists_what_is_missing(tmp_path, capsys):
    h = make_home(tmp_path)
    assert run(["setup", "--host", "hermes", "--preset", "recommended", "--hermes-home", h] + BASE) == 2
    assert "missing_vectors" in capsys.readouterr().err
    assert run(["setup", "--host", "hermes", "--preset", "custom", "--hermes-home", h] + BASE) == 2
    assert "missing_provider" in capsys.readouterr().err
    assert run(["setup", "--host", "hermes", "--preset", "custom", "--provider", "moonshot", "--hermes-home", h] + BASE) == 2
    err = capsys.readouterr().err
    assert "missing_provider_fields" in err and "--provider-model" in err and "moonshot-v1-32k" in err
