"""小修30完整入口：所有安装、配置、身份和消息均为临时假数据。"""
import json
from pathlib import Path

import pytest

from helpers_wizard import FakeIO, FakeSystem, FAKE_KEY, make_home, tree_bytes
from yuanwei import cli, hermes_setup as HS, wizard as WZ, owner_diagnostics as OD

BASE = ["--config-method", "yaml", "--src-path", "/fake/src"]


def fake_system():
    return FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY})


def setup(home, *args, io=None):
    return cli._setup_main(["setup", "--hermes-home", str(home)] + BASE + list(args), io=io, sys_=fake_system())


def platforms(home, text="weixin:\n    enabled: true\n  telegram:\n    enabled: true"):
    with (Path(home) / "config.yaml").open("a") as f:
        f.write("platforms:\n  " + text + "\n")


def test_wizard_detects_each_platform_and_prompts_before_writing(tmp_path):
    h = make_home(tmp_path)
    platforms(h)
    io = FakeIO(["1", "2", "y", "y", "1", "telegram-me", "weixin-me", "y"])
    assert setup(h, io=io) == 0
    assert len([p for p in io.asked if "哪个账号是你本人" in p]) == 2
    cfg = json.loads((Path(h) / "yuanwei/config.json").read_text())
    assert cfg["owners"] == {"telegram": ["telegram-me"], "weixin": ["weixin-me"]}
    assert "/whoami" in io.text and "account_id" in io.text.lower()


def test_skipping_platform_says_not_recorded_then_independent_command_registers(tmp_path, capsys):
    h = make_home(tmp_path)
    platforms(h, "weixin:\n    enabled: true")
    io = FakeIO(["1", "2", "y", "y", "1", "", "y"])
    assert setup(h, io=io) == 0
    assert "这个平台上的话不会记" in io.text and "owners set weixin" in io.text
    before = (Path(h) / "config.yaml").read_bytes()
    assert cli.main(["owners", "set", "weixin", "--id", "me", "--hermes-home", h]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert json.loads((Path(h) / "yuanwei/config.json").read_text())["owners"] == {}
    assert cli.main(["owners", "set", "weixin", "--id", "me", "--hermes-home", h, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["restart_hermes"] is True
    assert cli.main(["owners", "list", "weixin", "--hermes-home", h]) == 0
    assert json.loads(capsys.readouterr().out)["owners"] == {"weixin": ["me"]}
    assert (Path(h) / "config.yaml").read_bytes() == before
    assert set(HS.install_status(h)["plugin_files"].values()) == {"ok"}
    assert cli.main(["owners", "delete", "weixin", "--hermes-home", h, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["owners"] == {"weixin": []}


def test_no_platform_has_no_extra_question_and_cancel_changes_nothing(tmp_path):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    io = FakeIO(["1", "2", "y", "y", "1", "n"])
    assert setup(h, io=io) == 1
    assert not any("账号" in p or "本机终端" in p for p in io.asked)
    assert tree_bytes(h) == before


@pytest.mark.parametrize("style", [["--host", "hermes", "--preset", "light"], ["hermes"]])
def test_noninteractive_owner_and_skip_flags(tmp_path, capsys, style):
    h = make_home(tmp_path)
    platforms(h)
    io = FakeIO(tty=False)
    assert setup(h, *style, "--owner", "weixin:me", "--skip-owner", "telegram", "--yes", io=io) == 0
    output = capsys.readouterr().out
    assert "telegram" in output and "不会记" in output and io.asked == []
    assert json.loads((Path(h) / "yuanwei/config.json").read_text())["owners"] == {"weixin": ["me"]}


def test_conflicting_skip_is_rejected_before_changes(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    assert setup(h, "hermes", "--owner", "weixin:me", "--skip-owner", "weixin", "--yes") == 2
    assert "owner_and_skip" in capsys.readouterr().err and tree_bytes(h) == before


def test_status_and_doctor_explain_missing_owner_without_ids_or_db_access(tmp_path, capsys, monkeypatch):
    from yuanwei import hermes_compat as HC, owners
    h = make_home(tmp_path)
    platforms(h, "weixin:\n    enabled: true")
    assert setup(h, "hermes", "--yes") == 0
    capsys.readouterr()
    assert owners.increment(h, "weixin") and owners.increment(h, "weixin")
    d = HS.install_status(h)["chat_owners"]
    assert d["missing"] == ["weixin"] and d["skipped"] == {"weixin": 2}
    assert "不会记" in str(d) and "2 条" in str(d)
    monkeypatch.setattr(HC, "doctor", lambda path: {"checks": [], "exit_code": 0})
    assert cli.main(["doctor", "hermes", "--hermes-home", h]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["chat_owners"] == d
    assert cli.main(["owners", "set", "weixin", "--id", "private-owner-13579", "--hermes-home", h, "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["doctor", "hermes", "--hermes-home", h]) == 0
    out = capsys.readouterr().out
    assert "private-owner-13579" not in out and json.loads(out)["chat_owners"]["missing"] == []


def test_bad_counter_is_reported_and_uninstall_removes_own_counters(tmp_path, capsys):
    from yuanwei import owners
    h = make_home(tmp_path)
    assert setup(h, "hermes", "--yes") == 0
    capsys.readouterr()
    assert owners.increment(h, "weixin")
    path = Path(h) / "yuanwei/owner-skips.json"
    path.write_text("broken")
    before = tree_bytes(h)
    d = OD.status(h, env={})
    assert d["counter_problem"] == "invalid_data" and "不能把它当作" in str(d)
    assert tree_bytes(h) == before
    HS.apply_uninstall(h)
    assert path.exists()
    HS.apply_uninstall(h, host_reloaded=True)
    assert not path.exists() and not (Path(h) / "yuanwei/owner-skips.lock").exists()


def test_skip_owner_rejects_malformed_platform(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    assert setup(h, "hermes", "--skip-owner", "weixin:typo", "--yes") == 2
    assert "bad_owner" in capsys.readouterr().err and tree_bytes(h) == before


def test_doctor_profile_uses_shared_source_and_selected_profile_settings(tmp_path, monkeypatch, capsys):
    from yuanwei import hermes_compat as HC
    home = tmp_path / "hermes"
    bot = home / "profiles" / "helper"
    bot.mkdir(parents=True)
    (bot / "config.yaml").write_text("platforms:\n  weixin:\n    enabled: true\n")
    seen = []
    monkeypatch.setattr(HC, "doctor", lambda path: seen.append(path) or {"checks": [], "exit_code": 0})
    assert cli.main(["doctor", "hermes", "--hermes-home", str(bot)]) == 1
    assert seen == [str(home / "hermes-agent")]
    assert json.loads(capsys.readouterr().out)["chat_owners"]["missing"] == ["weixin"]
    assert cli.main(["doctor", "hermes", "--hermes-home", str(bot), "--hermes-dir", "/fake/source"]) == 1
    assert seen[-1] == "/fake/source"


def test_independent_registration_is_used_by_reloaded_adapter(tmp_path, capsys):
    from test_hermes_workspace import World, turn, msgs, rows
    from yuanwei import owners
    w = World(tmp_path, seed=False)
    h = str(w.home)
    assert setup(h, "hermes", "--yes") == 0
    capsys.readouterr()
    config = w.home / "yuanwei/config.json"
    w.cfg.update(json.loads(config.read_text()))
    old = w.adapter(None, platform="weixin", user_id="fake-me", chat_type="dm")
    turn(old, 1, "登记之前不记", author_id="fake-me")
    old.sync_turn("登记之前不记", "答", session_id="s1", messages=msgs("登记之前不记"))
    old.shutdown()
    assert w.mems == {} and owners.counts(h) == {"weixin": 1}
    assert cli.main(["owners", "set", "weixin", "--id", "fake-me", "--hermes-home", h, "--yes"]) == 0
    capsys.readouterr()
    w.cfg.update(json.loads(config.read_text()))  # 模拟宿主重载原委自己的设置
    new = w.adapter(None, platform="weixin", user_id="fake-me", chat_type="dm")
    turn(new, 2, "登记之后正常记", author_id="fake-me")
    new.sync_turn("登记之后正常记", "答", session_id="s1", messages=msgs("登记之后正常记"))
    assert rows(w.mem("life")) == [("user", "登记之后正常记"), ("assistant", "答")]
    assert owners.counts(h) == {"weixin": 1}
    new.shutdown()
