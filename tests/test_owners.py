"""本人账号管理：只有假配置、临时文件与进程；不运行 Hermes、不访问网络。"""
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from yuanwei import owners as OW
from yuanwei import hermes_setup as HS


def installed(tmp_path, owners=None):
    home = tmp_path / "fake-hermes"
    directory = home / "yuanwei"
    directory.mkdir(parents=True)
    config = {"hermes_home": str(home), "owners": owners or {}, "db": str(directory / "yuanwei.sqlite"),
              "mode": "tools_only", "read_scope": "life_all", "unknown_preserved": {"x": 9}}
    cp = directory / "config.json"
    cp.write_text(json.dumps(config))
    cp.chmod(0o640)
    manifest = {"files": {"yuanwei/config.json": HS._sha(cp.read_bytes()), "plugins/yuanwei/__init__.py": "unchanged"}, "other": "keep"}
    (directory / "install.json").write_text(json.dumps(manifest))
    (directory / "install.json").chmod(0o600)
    (home / "config.yaml").write_text("platforms:\n  telegram:\n    enabled: true\n")
    return home


def snapshot(home):
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


def test_parse_trims_normalizes_deduplicates_and_preserves_id_colons():
    assert OW.parse([" Telegram : 123 ", "telegram:123", "wechat:wxid:abc", "wechat:second"]) == {
        "telegram": ["123"], "wechat": ["wxid:abc", "second"]}


@pytest.mark.parametrize("item", ["", "telegram", ":x", "telegram:", "telegram:  ", "*:a", "telegram:*", "telegram:a?", "telegram:[abc]", "telegram:a\nb", "tele\tgram:a", "\ntelegram:a", "telegram:a\u200bb", "../telegram:x", 4])
def test_parse_rejects_missing_fields_control_characters_and_wildcards(item):
    with pytest.raises(HS.SetupRefused):
        OW.parse([item])


def test_list_and_dry_run_are_readonly_and_list_has_registered_ids(tmp_path):
    h = installed(tmp_path, {"telegram": ["owner-a"], "wechat": ["wx-a"]})
    before = snapshot(h)
    assert OW.settings(h)["owners"] == {"telegram": ["owner-a"], "wechat": ["wx-a"]}
    assert OW.settings(h, platform="Telegram")["owners"] == {"telegram": ["owner-a"]}
    assert OW.settings(h, platform="discord")["owners"] == {"discord": []}
    result = OW.settings(h, "set", "telegram", ["owner-b"])
    assert result["would_change"] and not result["applied"] and not result["changed"]
    assert snapshot(h) == before


def test_set_changes_only_our_settings_preserves_fields_fingerprint_and_modes(tmp_path):
    h = installed(tmp_path, {"telegram": ["old"], "wechat": ["wx"]})
    before = snapshot(h)
    result = OW.settings(h, "set", "telegram", ["new", "new", "two"], apply=True)
    assert result["changed"] and result["restart_hermes"]
    config = json.loads((h / "yuanwei/config.json").read_bytes())
    old = json.loads(before["yuanwei/config.json"])
    assert config == dict(old, owners={"telegram": ["new", "two"], "wechat": ["wx"]})
    manifest = json.loads((h / "yuanwei/install.json").read_bytes())
    assert manifest["other"] == "keep" and manifest["files"]["plugins/yuanwei/__init__.py"] == "unchanged"
    assert manifest["files"]["yuanwei/config.json"] == HS._sha((h / "yuanwei/config.json").read_bytes())
    assert (h / "config.yaml").read_bytes() == before["config.yaml"]
    for rel, mode in (("config.json", 0o640), ("install.json", 0o600)):
        assert stat.S_IMODE((h / "yuanwei" / rel).stat().st_mode) == mode
        backup = next((h / "yuanwei").glob(rel + ".yuanwei-backup-*"))
        assert backup.read_bytes() == before["yuanwei/" + rel]
        assert stat.S_IMODE(backup.stat().st_mode) == mode
    assert not list((h / "yuanwei").glob(".config.json-*"))
    after = snapshot(h)
    assert not OW.settings(h, "set", "telegram", ["new", "two"], apply=True)["changed"]
    assert snapshot(h) == after


def test_delete_one_id_then_whole_platform_and_absent_are_idempotent(tmp_path):
    h = installed(tmp_path, {"telegram": ["a", "b"], "wechat": ["wx"]})
    OW.settings(h, "delete", "telegram", ["a"], apply=True)
    assert OW.settings(h)["owners"]["telegram"] == ["b"]
    OW.settings(h, "delete", "telegram", apply=True)
    assert OW.settings(h)["owners"] == {"wechat": ["wx"]}
    before = snapshot(h)
    assert not OW.settings(h, "delete", "telegram", apply=True)["changed"]
    assert snapshot(h) == before


@pytest.mark.parametrize("change,code", [("manual", "config_modified"), ("copied", "copied"), ("unconfirmed", "ownership_unconfirmed"), ("missing-manifest", "owner_install_unreadable")])
def test_refuse_modified_copied_unconfirmed_or_missing_install_record(tmp_path, change, code):
    h = installed(tmp_path)
    cp, mp = h / "yuanwei/config.json", h / "yuanwei/install.json"
    config = json.loads(cp.read_bytes())
    if change == "manual":
        config["mode"] = "note"
    elif change == "copied":
        config["hermes_home"] = str(tmp_path / "other-home")
    elif change == "unconfirmed":
        config.pop("hermes_home")
        config["db"] = "/custom/unconfirmed.sqlite"
    else:
        mp.unlink()
    cp.write_text(json.dumps(config))
    before = snapshot(h)
    with pytest.raises(HS.SetupRefused) as error:
        OW.settings(h, "set", "telegram", ["me"], apply=True)
    assert error.value.code == code
    assert snapshot(h) == before


def test_manifest_write_failure_restores_original_config(tmp_path, monkeypatch):
    h = installed(tmp_path)
    original = (h / "yuanwei/config.json").read_bytes()
    real = OW._atomic
    def fail_manifest(path, data, mode=0o600):
        if path.name == "install.json":
            raise OSError("synthetic failure")
        return real(path, data, mode)
    monkeypatch.setattr(OW, "_atomic", fail_manifest)
    with pytest.raises(HS.SetupRefused, match="写入失败"):
        OW.settings(h, "set", "telegram", ["me"], apply=True)
    assert (h / "yuanwei/config.json").read_bytes() == original
    assert json.loads((h / "yuanwei/install.json").read_bytes())["files"]["yuanwei/config.json"] == HS._sha(original)


def _many_increments(home, number):
    for _ in range(number):
        assert OW.increment(home, "wechat")


def test_counts_persist_across_processes_and_contain_only_platform_numbers(tmp_path):
    h = tmp_path / "fake-home"
    h.mkdir()
    before = snapshot(h)
    assert OW.counts(h) == {} and snapshot(h) == before
    assert OW.increment(h, "telegram")
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_many_increments, args=(str(h), 12)) for _ in range(3)]
    for process in workers:
        process.start()
    for process in workers:
        process.join(15)
        assert process.exitcode == 0
    assert OW.counts(h) == {"telegram": 1, "wechat": 36}
    assert set(snapshot(h)) == {"yuanwei/owner-skips.json", "yuanwei/owner-skips.lock"}
    assert (h / "yuanwei/owner-skips.lock").read_bytes() == b""
    assert stat.S_IMODE((h / "yuanwei/owner-skips.json").stat().st_mode) == 0o600
    assert not OW.increment(h, "invalid/platform")
    assert OW.counts(h) == {"telegram": 1, "wechat": 36}


@pytest.mark.parametrize("raw", ['{"wechat": true}', '{"../bad": 1}', '{"wechat": -1}', '{"wechat": "1"}', '[]', 'not-json'])
def test_corrupt_counts_are_not_reset_or_overwritten(tmp_path, raw):
    h = tmp_path / "fake-home"
    (h / "yuanwei").mkdir(parents=True)
    path = h / "yuanwei/owner-skips.json"
    path.write_text(raw)
    assert OW.counts(h) == {} and not OW.increment(h, "wechat")
    assert path.read_text() == raw


def test_symlinks_do_not_write_outside_settings_or_counter_directory(tmp_path):
    h = installed(tmp_path)
    cp = h / "yuanwei/config.json"
    external = tmp_path / "external"
    external.write_bytes(cp.read_bytes())
    cp.unlink()
    cp.symlink_to(external)
    with pytest.raises(HS.SetupRefused):
        OW.settings(h, "set", "telegram", ["me"], apply=True)
    counter = h / "yuanwei/owner-skips.json"
    counter.symlink_to(external)
    before = external.read_bytes()
    assert not OW.increment(h, "wechat") and OW.counts(h) == {}
    assert external.read_bytes() == before


def test_count_status_distinguishes_absent_invalid_and_unreadable(tmp_path, monkeypatch):
    h = tmp_path / "fake-home"
    assert OW.count_status(h) == {"counts": {}, "problem": None}
    (h / "yuanwei").mkdir(parents=True)
    path = h / "yuanwei/owner-skips.json"
    path.write_text('{"wechat": 7}')
    assert OW.count_status(h) == {"counts": {"wechat": 7}, "problem": None}
    path.write_text('{"wechat": "bad"}')
    assert OW.count_status(h) == {"counts": {}, "problem": "invalid_data"}
    original = path.read_bytes()
    read_bytes = Path.read_bytes
    def cannot_read(p):
        if p == path:
            raise PermissionError("synthetic")
        return read_bytes(p)
    monkeypatch.setattr(Path, "read_bytes", cannot_read)
    assert OW.count_status(h) == {"counts": {}, "problem": "unreadable"}
    assert OW.counts(h) == {} and read_bytes(path) == original


def test_count_status_reports_symlinks_as_unsafe_path(tmp_path):
    h = tmp_path / "fake-home"
    (h / "yuanwei").mkdir(parents=True)
    other = tmp_path / "other"
    other.write_text('{"wechat": 99}')
    (h / "yuanwei/owner-skips.json").symlink_to(other)
    assert OW.count_status(h) == {"counts": {}, "problem": "unsafe_path"}


def test_manifest_failure_and_failed_rollback_give_backup_recovery_steps(tmp_path, monkeypatch):
    h = installed(tmp_path)
    original_config = (h / "yuanwei/config.json").read_bytes()
    original_manifest = (h / "yuanwei/install.json").read_bytes()
    real = OW._atomic
    calls = []
    def fail_after_config(path, data, mode=0o600):
        calls.append(path.name)
        if len(calls) > 1:
            raise OSError("synthetic failure")
        return real(path, data, mode)
    monkeypatch.setattr(OW, "_atomic", fail_after_config)
    with pytest.raises(HS.SetupRefused) as error:
        OW.settings(h, "set", "telegram", ["me"], apply=True)
    assert error.value.code == "owner_settings_rollback_failed"
    assert "回滚没有完成" in str(error.value) and "先从备份恢复 config.json 和 install.json，再重试" in str(error.value)
    for filename, original in (("config.json", original_config), ("install.json", original_manifest)):
        backup = next((h / "yuanwei").glob(filename + ".yuanwei-backup-*"))
        assert backup.read_bytes() == original and str(backup) in str(error.value)
