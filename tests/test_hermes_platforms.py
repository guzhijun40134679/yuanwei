"""平台发现只用临时假配置；不读取真实安装，不调用 Hermes 或网络。"""
import json
from pathlib import Path

import pytest

from yuanwei import hermes_platforms as P


def put(home, name, text):
    home.mkdir(parents=True, exist_ok=True)
    (home / name).write_text(text, encoding="utf-8")


def names(result):
    return [p["platform"] for p in result["platforms"]]


@pytest.mark.parametrize("body", [
    "platforms:\n  weixin:\n    enabled: true\n",
    "gateway:\n  platforms:\n    weixin:\n      enabled: true\n",
    "gateway:\n  weixin:\n    enabled: true\n",
    "weixin:\n  enabled: true\n",
    json.dumps({"platforms": {"weixin": {"enabled": True}}}),
    "'platforms':\n  \"weixin\":\n    enabled: 'yes' # note\n",
])
def test_enabled_shapes_and_fixed_identity_hint(tmp_path, body):
    put(tmp_path, "config.yaml", body)
    r = P.discover(tmp_path, env={})
    assert names(r) == ["weixin"]
    row = r["platforms"][0]
    assert row["state"] == "enabled" and row["source"] == "config.yaml"
    assert row["name"] == "微信" and "/whoami" in row["hint"] and "机器人编号" in row["hint"]


def test_false_overrides_environment_and_legacy(tmp_path):
    put(tmp_path, "gateway.json", json.dumps({"platforms": {"weixin": {"enabled": True}}}))
    put(tmp_path, "config.yaml", "platforms:\n  weixin:\n    enabled: false\n")
    put(tmp_path, ".env", "WEIXIN_ACCOUNT_ID=fake-bot\nWEIXIN_TOKEN=fake-secret\n")
    assert names(P.discover(tmp_path, env={"WEIXIN_ACCOUNT_ID": "fake", "WEIXIN_TOKEN": "fake"})) == []


def test_config_precedence_matches_platform_merging(tmp_path):
    put(tmp_path, "config.yaml", "gateway:\n  platforms:\n    telegram:\n      enabled: true\nplatforms:\n  telegram:\n    enabled: false\ntelegram:\n  enabled: true\n")
    assert names(P.discover(tmp_path, env={})) == ["telegram"]


def test_weixin_dotenv_candidate_has_no_values_or_allowlisted_users(tmp_path):
    put(tmp_path, ".env", "export WEIXIN_ACCOUNT_ID='PRIVATE-BOT-ID'\nWEIXIN_TOKEN=PRIVATE-SECRET # ignored\nWEIXIN_ALLOWED_USERS=PRIVATE-PERSON\nUNRELATED_KEY=PRIVATE-OTHER\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == ["weixin"]
    assert r["platforms"][0]["state"] == "configured"
    assert r["platforms"][0]["source"] == "hermes_dotenv"
    assert "PRIVATE" not in json.dumps(r) and any("不能据此确认" in n for n in r["notes"])


def test_empty_disabled_and_incomplete_environment_do_not_prompt(tmp_path):
    put(tmp_path, ".env", "WEIXIN_ACCOUNT_ID=fake\nWEIXIN_TOKEN='' # empty\nTELEGRAM_BOT_TOKEN=\nWHATSAPP_ENABLED=false\nDISCORD_BOT_TOKEN=   # empty\n")
    assert names(P.discover(tmp_path, env={})) == []


def test_process_environment_overrides_same_home_dotenv_without_values(tmp_path):
    put(tmp_path, ".env", "TELEGRAM_BOT_TOKEN=fake\n")
    assert names(P.discover(tmp_path, env={"TELEGRAM_BOT_TOKEN": ""})) == []
    r = P.discover(tmp_path, env={"DISCORD_BOT_TOKEN": "NEVER-PRINT"})
    assert names(r) == ["discord", "telegram"] and "NEVER-PRINT" not in json.dumps(r)


def test_profile_does_not_borrow_parent_dotenv_or_environment(tmp_path):
    put(tmp_path, ".env", "TELEGRAM_BOT_TOKEN=fake-parent\n")
    profile = tmp_path / "profiles" / "helper"
    profile.mkdir(parents=True)
    assert names(P.discover(profile, env={"TELEGRAM_BOT_TOKEN": "fake-parent"})) == []
    assert names(P.discover(profile, env={"HERMES_HOME": str(tmp_path), "TELEGRAM_BOT_TOKEN": "fake-parent"})) == []
    assert names(P.discover(profile, env={"HERMES_HOME": str(profile), "TELEGRAM_BOT_TOKEN": "fake-profile"})) == ["telegram"]


def test_no_platform_has_no_prompt_and_nonchat_surfaces_excluded(tmp_path):
    assert P.discover(tmp_path, env={}) == {"platforms": [], "notes": []}
    put(tmp_path, "config.yaml", json.dumps({"platforms": {n: {"enabled": True} for n in P.EXCLUDED}, "model": {"enabled": True}}))
    assert names(P.discover(tmp_path, env={})) == []


def test_registered_plugin_name_from_platform_map(tmp_path):
    put(tmp_path, "config.yaml", "platforms:\n  custom-chat:\n    enabled: true\n")
    assert names(P.discover(tmp_path, env={})) == ["custom-chat"]


@pytest.mark.parametrize("body", [
    "platforms: {weixin: {enabled: true}}\n",
    "platforms:\n  weixin: &platform\n    enabled: true\n",
    "platforms:\n  weixin:\n    enabled: !custom true\n",
    "{invalid json",
])
def test_unsupported_yaml_or_invalid_json_reports_detection_limit(tmp_path, body):
    put(tmp_path, "config.yaml", body)
    assert P.discover(tmp_path, env={})["notes"]


def test_whitelist_or_secret_body_is_not_treated_as_platform(tmp_path):
    put(tmp_path, "config.yaml", "model:\n  enabled: true\nplatforms:\n  weixin:\n    allow_from:\n      - NEVER-PRINT-PERSON\n    token: NEVER-PRINT-TOKEN\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == [] and "NEVER-PRINT" not in json.dumps(r)


def test_multiline_dotenv_values_do_not_create_fake_platform(tmp_path):
    put(tmp_path, ".env", 'UNRELATED="multi\nTELEGRAM_BOT_TOKEN=not-real\nend"\n')
    assert names(P.discover(tmp_path, env={})) == []


def test_discovery_reads_only_named_files_in_requested_home(tmp_path, monkeypatch):
    put(tmp_path, "config.yaml", "platforms:\n  telegram:\n    enabled: true\n")
    original = Path.open
    seen = []
    def guarded(path, *args, **kwargs):
        assert path.parent == tmp_path and path.name in {"config.yaml", "gateway.json", ".env"}
        seen.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    assert names(P.discover(tmp_path, env={})) == ["telegram"] and seen == ["config.yaml"]


@pytest.mark.parametrize("platform,groups", sorted(P.ENV_GROUPS.items()))
def test_known_environment_platforms_are_candidates(platform, groups, tmp_path):
    env = {key: ("true" if key.endswith("_ENABLED") else "FAKE-NOT-PRINTED") for key in groups[0]}
    r = P.discover(tmp_path, env=env)
    assert names(r) == [platform]
    assert r["platforms"][0]["state"] == "configured" and "FAKE-NOT-PRINTED" not in json.dumps(r)


@pytest.mark.parametrize("body", [
    "description: |\n  platforms:\n    weixin:\n      enabled: true\n",
    "platforms:\n  weixin:\n    token: |\n      enabled: true\n",
    "platforms:\n  weixin:\n    description: >-\n      enabled: true\n",
])
def test_block_scalar_content_cannot_enable_platform(tmp_path, body):
    put(tmp_path, "config.yaml", body)
    assert names(P.discover(tmp_path, env={})) == []


def test_block_scalar_and_list_content_skipped_but_real_setting_seen(tmp_path):
    put(tmp_path, "config.yaml", "platforms:\n  weixin:\n    description: |\n      enabled: false\n    allow_from:\n      - enabled: false\n    enabled: true\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == ["weixin"] and r["notes"] == []


@pytest.mark.parametrize("body", [
    "platforms:\n  weixin:\n    enabled: true\n    enabled: false\n",
    "platforms:\n  weixin:\n    enabled: true\nplatforms:\n  weixin:\n    enabled: false\n",
    '{"platforms":{"weixin":{"enabled":true,"enabled":false}}}',
    "platforms:\n  weixin:\n    enabled: true\n---\nplatforms:\n  weixin:\n    enabled: false\n",
    "platforms:\n  weixin:\n    enabled: true\n  other: &base\n    enabled: true\n",
    "platforms:\n  weixin:\n    enabled: true\n  other: {enabled: false}\n",
])
def test_duplicate_or_complex_config_does_not_assert_enabled(tmp_path, body):
    put(tmp_path, "gateway.json", '{"platforms":{"weixin":{"enabled":true}}}')
    put(tmp_path, "config.yaml", body)
    r = P.discover(tmp_path, env={})
    assert names(r) == [] and r["notes"]


@pytest.mark.parametrize("env,expected", [
    ({"MATRIX_HOMESERVER":"fake", "MATRIX_PASSWORD":"fake"}, ["matrix"]),
    ({"MATRIX_HOMESERVER":"fake", "MATRIX_ACCESS_TOKEN":"fake"}, ["matrix"]),
    ({"MATRIX_HOMESERVER":"fake"}, []),
    ({"YUANBAO_APP_KEY":"fake", "YUANBAO_APP_SECRET":"fake"}, ["yuanbao"]),
    ({"EMAIL_ADDRESS":"fake", "EMAIL_PASSWORD":"fake", "EMAIL_SMTP_HOST":"fake"}, []),
    ({"EMAIL_ADDRESS":"fake", "EMAIL_PASSWORD":"fake", "EMAIL_SMTP_HOST":"fake", "EMAIL_IMAP_HOST":"fake"}, ["email"]),
    ({"TWILIO_ACCOUNT_SID":"fake"}, ["sms"]),
])
def test_source_checked_alternate_and_required_credentials(tmp_path, env, expected):
    # 独立写出已核源码的场景，避免仅按实现的 ENV_GROUPS 生成期待值。
    assert names(P.discover(tmp_path, env=env)) == expected


@pytest.mark.parametrize("flag,expected", [("false", []), ("0", []), ("no", []), ("", ["whatsapp"])])
def test_whatsapp_explicit_environment_disable_overrides_yaml(tmp_path, flag, expected):
    put(tmp_path, "config.yaml", "platforms:\n  whatsapp:\n    enabled: true\n")
    assert names(P.discover(tmp_path, env={"WHATSAPP_ENABLED": flag})) == expected


def test_malformed_quoted_boolean_is_not_enabled(tmp_path):
    put(tmp_path, "config.yaml", "platforms:\n  weixin:\n    enabled: 'true\"\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == [] and r["notes"]


@pytest.mark.parametrize("unrelated", [
    "toolsets: [all]\nmodel: {default: x}\n",
    "tools:\n  names: [one, two]\n  options: {mode: x}\n",
    "gateway:\n  quick_commands: {help: text}\n  display: [brief]\n",
    "model:\n  nested:\n    platforms: {weixin: {enabled: false}}\n",
    "model: {default: x}\nmodel: {default: y}\n",
])
def test_unrelated_flow_configuration_does_not_hide_enabled_platform(tmp_path, unrelated):
    put(tmp_path, "config.yaml", unrelated + "platforms:\n  weixin:\n    enabled: true\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == ["weixin"] and r["notes"] == []


def test_unrelated_json_object_duplicate_does_not_hide_platform(tmp_path):
    put(tmp_path, "config.yaml", '{"model":{"default":"x","default":"y"},"platforms":{"weixin":{"enabled":true}}}')
    r = P.discover(tmp_path, env={})
    assert names(r) == ["weixin"] and r["notes"] == []


def test_anchor_in_unrelated_section_cannot_be_merged_into_platform_silently(tmp_path):
    put(tmp_path, "config.yaml", "defaults: &defaults\n  enabled: false\nplatforms:\n  weixin:\n    <<: *defaults\n    enabled: true\n")
    r = P.discover(tmp_path, env={})
    assert names(r) == [] and r["notes"]
