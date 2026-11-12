"""向导在 Mac 以外的系统上提醒「只在 Mac 上测过」：只提醒、不拦；Mac 上输出一个字都不变。系统名一律注入（FakeSystem 的 platform），不看真机。"""
import json

import pytest

from helpers_wizard import FAKE_KEY, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import cli

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
WIN_TEXT = "注意：原委这一版只在 Mac 上测过，Windows 没测过。Windows 上已知可能出问题：同时写库时的文件锁不起作用、命令行输出可能乱码或报错、整理时可能闪出黑色窗口。可以继续装，但可能遇到这些问题。"
LINUX_TEXT = "注意：原委这一版只在 Mac 上测过，这个系统（Linux）没测过，可能出问题。"
MARK = "只在 Mac 上测过"
PLATFORMS = {
    "Darwin": {"system": "Darwin", "machine": "arm64", "arm64_hardware": True},
    "Windows": {"system": "Windows", "machine": "AMD64", "arm64_hardware": None},
    "Linux": {"system": "Linux", "machine": "x86_64", "arm64_hardware": False},
}


def sysx(name):
    return FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY}, platform=PLATFORMS[name])


def plan_only(tmp_path, capsys, name, tag):
    h = make_home(tmp_path, tag)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE, sys_=sysx(name)) == 0
    return json.loads(capsys.readouterr().out)


def wizard_out(tmp_path, name, tag, last="n"):
    h = make_home(tmp_path, tag)
    io = FakeIO(["1", "2", "y", "y", "1", "y", last])
    rc = cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=sysx(name))
    return rc, io.text, h


@pytest.mark.parametrize("name,text", [("Windows", WIN_TEXT), ("Linux", LINUX_TEXT)])
def test_plan_only_output_has_the_notice_line_on_non_mac(tmp_path, capsys, name, text):
    out = plan_only(tmp_path, capsys, name, "p")
    assert text in out["plan"]["lines"] and out["plan"]["lines"].count(text) == 1
    assert out["plan"]["platform"] == {"system": name, "tested": False}
    assert out["applied"] is False


@pytest.mark.parametrize("name,text", [("Windows", WIN_TEXT), ("Linux", LINUX_TEXT)])
def test_yes_output_has_the_notice_on_non_mac(tmp_path, capsys, name, text):
    h = make_home(tmp_path, "y")
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=sysx(name)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and out["platform_notice"] == text and out["platform"] == {"system": name, "tested": False}


@pytest.mark.parametrize("name,text", [("Windows", WIN_TEXT), ("Linux", LINUX_TEXT)])
def test_wizard_step1_has_the_notice_right_after_the_python_line_on_non_mac(tmp_path, name, text):
    rc, out, _ = wizard_out(tmp_path, name, "w")
    lines = out.splitlines()
    py = next(i for i, l in enumerate(lines) if l.startswith("  Python "))
    assert lines[py + 1] == "  " + text and out.count(text) == 1
    assert lines.index("第 2 步　选助手") > py + 1                      # 提醒在第 1 步里，不在后面


def test_darwin_has_no_notice_anywhere(tmp_path, capsys):
    out = plan_only(tmp_path, capsys, "Darwin", "d")
    assert MARK not in "\n".join(out["plan"]["lines"]) and "platform" not in out["plan"]
    h = make_home(tmp_path, "dy")
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=sysx("Darwin")) == 0
    y = json.loads(capsys.readouterr().out)
    assert "platform_notice" not in y and "platform" not in y and MARK not in json.dumps(y, ensure_ascii=False)
    _, wiz, _ = wizard_out(tmp_path, "Darwin", "dw")
    assert MARK not in wiz and "没测过" not in wiz.split("第 2 步")[0]


def test_darwin_plan_is_the_linux_plan_minus_exactly_the_notice_line(tmp_path, capsys):
    d = plan_only(tmp_path, capsys, "Darwin", "same")
    l = plan_only(tmp_path, capsys, "Linux", "same2")
    norm = lambda o, tag: [x.replace(str(tmp_path / tag), "HOME") for x in o["plan"]["lines"]]
    ll = norm(l, "same2")
    ll.remove(LINUX_TEXT)
    assert norm(d, "same") == ll                                                 # 提醒是唯一的差别（其余行逐字相同）


def test_notice_only_does_not_refuse_ask_extra_or_change_defaults(tmp_path):
    # Windows 和 Mac 回答同一串话，走完同样的步数、问同样的问题、装出同样的文件
    rc_d, out_d, hd = wizard_out(tmp_path, "Darwin", "nd", last="y")
    rc_w, out_w, hw = wizard_out(tmp_path, "Windows", "nw", last="y")
    assert rc_d == rc_w == 0
    io_d, io_w = FakeIO(["1", "2", "y", "y", "1", "y", "y"]), FakeIO(["1", "2", "y", "y", "1", "y", "y"])
    h1, h2 = make_home(tmp_path, "q1"), make_home(tmp_path, "q2")
    cli._setup_main(["setup", "--hermes-home", h1] + BASE, io=io_d, sys_=sysx("Darwin"))
    cli._setup_main(["setup", "--hermes-home", h2] + BASE, io=io_w, sys_=sysx("Windows"))
    assert io_d.asked == io_w.asked and io_w.answers == [] == io_d.answers       # 问话一模一样、没多问一句
    norm = lambda d, h: {k: v.replace(h.encode(), b"HOME") for k, v in d.items() if not k.startswith("config.yaml.yuanwei-backup")}
    t1, t2 = norm(tree_bytes(h1), h1), norm(tree_bytes(h2), h2)
    assert set(t1) == set(t2) and "yuanwei/yuanwei.toml" in t1
    assert all(t1[k] == t2[k] for k in t1 if k not in ("yuanwei/install.json", "yuanwei/config.json"))   # 默认值（toml）一个字不变
    strip = lambda t, h: t.replace("  " + WIN_TEXT + "\n", "").replace(h, "HOME")
    assert strip(out_w, hw) == strip(out_d, hd)


def test_unknown_system_is_reported_as_untested_not_assumed_to_be_a_mac(tmp_path, capsys):
    s = FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY}, platform={"system": "", "machine": "", "arm64_hardware": None})
    h = make_home(tmp_path, "u")
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--hermes-home", h] + BASE, sys_=s) == 0
    lines = json.loads(capsys.readouterr().out)["plan"]["lines"]
    assert "注意：原委这一版只在 Mac 上测过，这个系统（未知）没测过，可能出问题。" in lines
