"""交回点乙·第 2 步：yuanwei setup hermes / uninstall hermes（施工单第 6 节＋补充 §4 卸载顺序）。一律在临时目录、假 HOME 里，不碰真实的 ~/.hermes。"""
import hashlib
import json
import os
import shutil

import pytest

from yuanwei import cli
from yuanwei import hermes_setup as HS

CFG0 = "model:\n  default: some-model\nmemory:\n  memory_enabled: true\n  provider: honcho\n  memory_char_limit: 2200\nother:\n  x: 1\n"


def home(tmp_path, cfg=CFG0):
    h = tmp_path / "hh"
    h.mkdir()
    if cfg is not None:
        (h / "config.yaml").write_text(cfg)
    return h


def opts(h, tmp_path, **kw):
    d = dict(hermes_home=str(h), db=str(tmp_path / "mem.sqlite"), mode="tools_only", src_path="/pkg/src", replace_provider=False, hermes_cmd=None, config_method="yaml")
    d.update(kw)
    return d


def snapshot(h):
    out = {}
    for root, _dirs, files in os.walk(h):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, h)] = open(p, "rb").read()
    return out


def test_preview_lists_files_and_lines_and_changes_nothing(tmp_path):
    h = home(tmp_path)
    before = snapshot(h)
    plan = HS.plan_install(**opts(h, tmp_path, replace_provider=True))
    assert snapshot(h) == before
    kinds = {(a["kind"], os.path.basename(a["path"])) for a in plan["actions"]}
    assert ("write_file", "__init__.py") in kinds and ("write_file", "plugin.yaml") in kinds and ("write_file", "config.json") in kinds and ("edit_config", "config.yaml") in kinds
    edit = [a for a in plan["actions"] if a["kind"] == "edit_config"][0]
    assert edit["replace"] == ["  provider: honcho", "  provider: yuanwei"] and edit["insert"] is None
    assert plan["backups"] and plan["notes"]


def test_apply_writes_only_our_files_edits_one_line_and_backs_up_first(tmp_path):
    h = home(tmp_path)
    r = HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    assert r["applied"] and (h / "plugins" / "yuanwei" / "__init__.py").exists() and (h / "plugins" / "yuanwei" / "plugin.yaml").exists() and (h / "yuanwei" / "config.json").exists()
    new = (h / "config.yaml").read_text()
    assert new == CFG0.replace("  provider: honcho\n", "  provider: yuanwei\n")                      # 只有这一行变了
    backups = [p for p in os.listdir(h) if p.startswith("config.yaml.yuanwei-backup-")]
    assert len(backups) == 1 and (h / backups[0]).read_text() == CFG0
    cfg = json.loads((h / "yuanwei" / "config.json").read_text())
    assert cfg["db"] == str(tmp_path / "mem.sqlite") and "workspace" not in cfg and cfg["mode"] == "tools_only" and cfg["src_path"] == "/pkg/src"
    assert cfg["read_scope"] == "life_all" and cfg["owners"] == {}                      # 默认读取范围＝乙（用户定）：生活能翻全部，项目只翻自己；没配主人


def test_existing_other_provider_needs_an_explicit_replace_flag(tmp_path):
    h = home(tmp_path)
    before = snapshot(h)
    with pytest.raises(HS.SetupRefused) as e:
        HS.apply_install(**opts(h, tmp_path, replace_provider=False))
    assert e.value.code == "other_provider_set" and snapshot(h) == before                     # Hermes 一次只能有一个外部记忆提供者：不悄悄换掉用户原来的


def test_no_memory_block_and_no_provider_line_cases(tmp_path):
    h = home(tmp_path, "model:\n  default: a\n")
    HS.apply_install(**opts(h, tmp_path))
    assert (h / "config.yaml").read_text() == "model:\n  default: a\nmemory:\n  provider: yuanwei\n"
    h2 = home(tmp_path / "b", None) if (tmp_path / "b").mkdir() is None else None
    HS.apply_install(**opts(h2, tmp_path))
    assert (h2 / "config.yaml").read_text() == "memory:\n  provider: yuanwei\n"
    (tmp_path / "c").mkdir()
    h3 = home(tmp_path / "c", "memory:\n  memory_enabled: true\nother: 1\n")
    HS.apply_install(**opts(h3, tmp_path))
    assert (h3 / "config.yaml").read_text() == "memory:\n  provider: yuanwei\n  memory_enabled: true\nother: 1\n"


def test_setup_twice_is_idempotent_and_does_not_make_a_second_backup(tmp_path):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    snap = snapshot(h)
    r = HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    assert r["changed"] is False and snapshot(h) == snap


def test_a_plugin_dir_we_did_not_install_is_not_overwritten(tmp_path):
    h = home(tmp_path)
    (h / "plugins" / "yuanwei").mkdir(parents=True)
    (h / "plugins" / "yuanwei" / "__init__.py").write_text("# somebody else's plugin\n")
    with pytest.raises(HS.SetupRefused) as e:
        HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    assert e.value.code == "plugin_dir_not_ours" and (h / "plugins" / "yuanwei" / "__init__.py").read_text() == "# somebody else's plugin\n"


def test_user_edited_plugin_file_blocks_reinstall_and_deletion(tmp_path):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    p = h / "plugins" / "yuanwei" / "__init__.py"
    p.write_text(p.read_text() + "\n# my own tweak\n")
    with pytest.raises(HS.SetupRefused) as e:
        HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    assert e.value.code == "plugin_file_modified"
    r = HS.apply_uninstall(hermes_home=str(h), host_reloaded=True)
    assert "my own tweak" in p.read_text() and any(x["status"] == "kept_modified" for x in r["steps"])


def test_uninstall_order_neutral_shell_then_config_then_delete_only_after_reload(tmp_path):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    r1 = HS.apply_uninstall(hermes_home=str(h), host_reloaded=False)
    assert [s["step"] for s in r1["steps"]][:2] == ["neutral_shell", "revert_config"]
    assert (h / "config.yaml").read_text() == CFG0                                      # 原来的提供者还原
    shell = (h / "plugins" / "yuanwei" / "__init__.py").read_text()
    assert "is_available" in shell and "return False" in shell and "register_memory_provider" in shell
    assert (h / "plugins" / "yuanwei").exists() and r1["pending"] == ["delete_files_after_host_reload"]
    r2 = HS.apply_uninstall(hermes_home=str(h), host_reloaded=True)
    assert not (h / "plugins" / "yuanwei").exists() and not (h / "yuanwei" / "config.json").exists()
    assert (h / "config.yaml").read_text() == CFG0
    r3 = HS.apply_uninstall(hermes_home=str(h), host_reloaded=True)                      # 卸载两次
    assert r3["changed"] is False


def test_uninstall_does_not_touch_a_provider_the_user_changed_later_or_other_lines(tmp_path):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    (h / "config.yaml").write_text((h / "config.yaml").read_text().replace("provider: yuanwei", "provider: retaindb") + "extra: 2\n")
    HS.apply_uninstall(hermes_home=str(h), host_reloaded=True)
    assert "provider: retaindb" in (h / "config.yaml").read_text() and "extra: 2" in (h / "config.yaml").read_text()


def test_uninstall_when_we_inserted_the_provider_line_removes_exactly_that_line(tmp_path):
    h = home(tmp_path, "memory:\n  memory_enabled: true\nother: 1\n")
    HS.apply_install(**opts(h, tmp_path))
    HS.apply_uninstall(hermes_home=str(h), host_reloaded=True)
    assert (h / "config.yaml").read_text() == "memory:\n  memory_enabled: true\nother: 1\n"
    h2 = home(tmp_path / "x", "model:\n  default: a\n") if (tmp_path / "x").mkdir() is None else None
    HS.apply_install(**opts(h2, tmp_path))
    HS.apply_uninstall(hermes_home=str(h2), host_reloaded=True)
    assert (h2 / "config.yaml").read_text() in ("model:\n  default: a\n", "model:\n  default: a\nmemory:\n")       # 我们新加的 memory 块里只有我们那一行：整块撤掉或留空壳都可以，不动别的


def test_install_manifest_records_hashes_and_previous_value_not_secrets(tmp_path):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    m = json.loads((h / "yuanwei" / "install.json").read_text())
    assert m["previous_provider"] == "honcho" and m["files"]["plugins/yuanwei/__init__.py"] == hashlib.sha256((h / "plugins" / "yuanwei" / "__init__.py").read_bytes()).hexdigest()
    assert set(m) == {"version", "previous_provider", "provider_line_inserted", "memory_block_inserted", "files", "config_backup", "config_method", "config_format_version"}


def test_cli_setup_preview_needs_yes_and_never_reads_the_real_home(tmp_path, monkeypatch, capsys):
    h = home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    before = snapshot(h)
    rc = cli.main(["setup", "hermes", "--hermes-home", str(h), "--db", str(tmp_path / "m.sqlite"), "--replace-provider", "--config-method", "yaml"])
    out = json.loads(capsys.readouterr().out)
    assert rc in (0, None) and out["applied"] is False and out["plan"]["actions"] and snapshot(h) == before
    rc = cli.main(["setup", "hermes", "--hermes-home", str(h), "--db", str(tmp_path / "m.sqlite"), "--replace-provider", "--config-method", "yaml", "--yes"])
    assert json.loads(capsys.readouterr().out)["applied"] is True and (h / "plugins" / "yuanwei" / "__init__.py").exists()
    cli.main(["uninstall", "hermes", "--hermes-home", str(h), "--config-method", "yaml", "--yes"])
    out = json.loads(capsys.readouterr().out)
    assert out["pending"] == ["delete_files_after_host_reload"]
    cli.main(["uninstall", "hermes", "--hermes-home", str(h), "--config-method", "yaml", "--yes", "--host-reloaded"])
    assert not (h / "plugins" / "yuanwei").exists()


# ================================================================== 装接头尽量用 Hermes 自己的配置命令
FAKE_HERMES = r'''#!{py}
import os, re, sys
home = os.environ["HERMES_HOME"]
cfg = os.path.join(home, "config.yaml")
log = os.path.join(home, "fake-hermes-calls.log")
open(log, "a").write(" ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
def read():
    return open(cfg).read() if os.path.exists(cfg) else ""
def get():
    m = re.search(r"^memory:\n(?:[ \t]+.*\n)*?[ \t]+provider:\s*(\S+)", read(), re.M)
    return m.group(1) if m else ""
if a[:2] == ["config", "set"] and "--help" in a:
    print("usage: hermes config set [-h] [--force] [key] [value]"); sys.exit(0)
if a[:2] == ["config", "get"]:
    print("⚠ noise line"); print(get()); sys.exit(0)
if a[:2] == ["config", "path"]:
    print(cfg); sys.exit(0)
if a[:2] == ["config", "set"] and a[2] == "memory.provider":
    t = read()
    if re.search(r"^\s+provider:", t, re.M):
        t = re.sub(r"^(\s+)provider:.*$", lambda m: m.group(1) + "provider: " + a[3], t, count=1, flags=re.M)
    elif re.search(r"^memory:", t, re.M):
        t = re.sub(r"^memory:\n", "memory:\n  provider: " + a[3] + "\n", t, count=1, flags=re.M)
    else:
        t += "memory:\n  provider: " + a[3] + "\n"
    open(cfg, "w").write(t); sys.exit(0)
if a[:2] == ["config", "unset"]:
    t = re.sub(r"^\s+provider:.*\n", "", read(), count=1, flags=re.M); open(cfg, "w").write(t); sys.exit(0)
sys.exit(2)
'''


def fake_hermes(tmp_path):
    import sys
    p = tmp_path / "bin" / "hermes"
    p.parent.mkdir()
    p.write_text(FAKE_HERMES.format(py=sys.executable))
    p.chmod(0o755)
    return str(p)


def test_install_prefers_hermes_own_config_command_and_does_not_edit_the_yaml_itself(tmp_path, monkeypatch):
    h = home(tmp_path, CFG0 + "_config_version: 52\n")
    cmd = fake_hermes(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(h))
    plan = HS.plan_install(**opts(h, tmp_path, replace_provider=True, hermes_cmd=cmd, config_method="auto"))
    acts = [a for a in plan["actions"] if a["kind"] == "hermes_config_set"]
    assert acts and acts[0]["command"][-3:] == ["config", "set", "memory.provider"] or acts[0]["command"][-4:] == ["config", "set", "memory.provider", "yuanwei"]
    assert not [a for a in plan["actions"] if a["kind"] == "edit_config"]
    r = HS.apply_install(**opts(h, tmp_path, replace_provider=True, hermes_cmd=cmd, config_method="auto"))
    assert r["applied"] and "provider: yuanwei" in (h / "config.yaml").read_text()
    m = json.loads((h / "yuanwei" / "install.json").read_text())
    assert m["config_method"] == "hermes_cli" and m["previous_provider"] == "honcho" and m["config_format_version"] == 52
    calls = (h / "fake-hermes-calls.log").read_text()
    assert "config set memory.provider yuanwei" in calls


def test_uninstall_through_hermes_cli_restores_the_previous_value_or_unsets(tmp_path, monkeypatch):
    h = home(tmp_path)
    cmd = fake_hermes(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(h))
    HS.apply_install(**opts(h, tmp_path, replace_provider=True, hermes_cmd=cmd, config_method="auto"))
    HS.apply_uninstall(hermes_home=str(h), host_reloaded=True, hermes_cmd=cmd)
    assert "provider: honcho" in (h / "config.yaml").read_text()
    (tmp_path / "b").mkdir()
    h2 = home(tmp_path / "b", "model:\n  default: a\nmemory:\n  memory_enabled: true\n")
    monkeypatch.setenv("HERMES_HOME", str(h2))
    HS.apply_install(**opts(h2, tmp_path, hermes_cmd=cmd, config_method="auto"))
    HS.apply_uninstall(hermes_home=str(h2), host_reloaded=True, hermes_cmd=cmd)
    assert "provider" not in (h2 / "config.yaml").read_text()
    assert "unset" in (h2 / "fake-hermes-calls.log").read_text()


def test_a_hermes_without_config_set_falls_back_to_the_line_edit_and_the_plan_says_so(tmp_path):
    h = home(tmp_path)
    plan = HS.plan_install(**opts(h, tmp_path, replace_provider=True, hermes_cmd=str(tmp_path / "no-such-hermes"), config_method="auto"))
    assert [a["kind"] for a in plan["actions"] if a["kind"] in ("edit_config", "hermes_config_set")] == ["edit_config"]
    assert any("config set" in n for n in plan["notes"])


def test_forced_yaml_method_records_the_config_format_version_too(tmp_path):
    h = home(tmp_path, "_config_version: 50\nmemory:\n  provider: honcho\n")
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    m = json.loads((h / "yuanwei" / "install.json").read_text())
    assert m["config_format_version"] == 50 and m["config_method"] == "yaml_edit"


def test_doctor_compares_the_recorded_config_format_with_the_hosts_current_one(tmp_path):
    from yuanwei import hermes_compat as HC
    h = home(tmp_path, "_config_version: 50\nmemory:\n  provider: honcho\n")
    HS.apply_install(**opts(h, tmp_path, replace_provider=True))
    note = HC.config_format_note(str(h), host_format=52)
    assert note["recorded"] == 50 and note["host"] == 52 and note["changed"] is True and "重新" in note["advice"]
    assert HC.config_format_note(str(h), host_format=50)["changed"] is False


def test_setup_records_read_scope_owners_and_defaults_the_db_into_the_profile_home(tmp_path):
    h = home(tmp_path)
    kw = opts(h, tmp_path, replace_provider=True, read_scope="isolated", owners={"telegram": ["123"], "whatsapp": ["+86138"]})
    kw.pop("db")
    HS.apply_install(**kw)
    cfg = json.loads((h / "yuanwei" / "config.json").read_text())
    assert cfg["read_scope"] == "isolated" and cfg["owners"] == {"telegram": ["123"], "whatsapp": ["+86138"]}
    assert cfg["db"] == str(h / "yuanwei" / "yuanwei.sqlite")                           # 库跟着这个分身的 hermes_home 走


def test_setup_rejects_a_bad_read_scope_and_the_cli_takes_owner_flags(tmp_path, capsys):
    h = home(tmp_path)
    with pytest.raises(HS.SetupRefused) as e:
        HS.plan_install(**opts(h, tmp_path, replace_provider=True, read_scope="everything"))
    assert e.value.code == "bad_read_scope"
    cli.main(["setup", "hermes", "--hermes-home", str(h), "--replace-provider", "--config-method", "yaml", "--read-scope", "all_open", "--owner", "telegram:1", "--owner", "telegram:2", "--yes"])
    cfg = json.loads((h / "yuanwei" / "config.json").read_text())
    assert cfg["read_scope"] == "all_open" and cfg["owners"] == {"telegram": ["1", "2"]}
