"""交回点乙·第 2c 步：`yuanwei status hermes`（只读）——装没装、插件文件有没有被改、memory.provider 现在是谁、读取范围和主人（只报平台和个数，不报编号）、认得的工作区和各自记了几条、
运行时数（记了几轮、每类跳过各几次、当前工作区）。运行时的数由插件在 `<hermes_home>/yuanwei/status.json` 里留一份（只有数和原因，没有任何原话）。全部自造数据，断网。"""
import json
import os

import pytest

from helpers import Scripted, make_mem
from test_hermes_setup import CFG0, home, opts, snapshot
from test_hermes_workspace import World, msgs, turn
from yuanwei import cli
from yuanwei import hermes_setup as HS


def install(tmp_path, **kw):
    h = home(tmp_path)
    HS.apply_install(**opts(h, tmp_path, replace_provider=True, **kw))
    return h


def test_status_before_install_says_not_installed_and_reads_only(tmp_path):
    h = home(tmp_path)
    before = snapshot(h)
    st = HS.install_status(str(h))
    assert st["installed"] is False and "没有" in st["note"] and st["workspaces"] == [] and snapshot(h) == before


def test_status_after_install_reports_files_provider_scope_and_owner_counts_without_ids(tmp_path):
    h = install(tmp_path, owners={"telegram": ["12345", "67890"]})
    before = snapshot(h)
    st = HS.install_status(str(h))
    assert st["installed"] is True and st["provider_active"] == "yuanwei" and st["provider_is_ours"] is True
    assert set(st["plugin_files"].values()) == {"ok"}
    assert st["config"]["read_scope"] == "life_all" and st["config"]["owners"] == {"telegram": 2}
    assert "12345" not in json.dumps(st, ensure_ascii=False) and st["config"]["mode"] == "tools_only"
    assert snapshot(h) == before                                                                     # 只读：什么都没改


def test_status_notices_a_modified_or_missing_plugin_file_and_a_provider_the_user_changed_later(tmp_path):
    h = install(tmp_path)
    (h / "plugins" / "yuanwei" / "__init__.py").write_text("# 我改过\n")
    os.remove(h / "plugins" / "yuanwei" / "plugin.yaml")
    (h / "config.yaml").write_text(CFG0.replace("  provider: honcho\n", "  provider: mem0\n"))
    st = HS.install_status(str(h))
    assert st["plugin_files"]["plugins/yuanwei/__init__.py"] == "modified" and st["plugin_files"]["plugins/yuanwei/plugin.yaml"] == "missing"
    assert st["provider_active"] == "mem0" and st["provider_is_ours"] is False
    assert any("不是原委" in n for n in st["notes"])


def test_status_lists_known_workspaces_with_message_counts_and_the_runtime_numbers(tmp_path):
    w = World(tmp_path, scope="life_all", owners={"telegram": ["me"]})
    a = w.adapter(None, platform="telegram", user_id="me", chat_type="dm")
    turn(a, 1, "陌生人说的", author_id="stranger")
    a.sync_turn("陌生人说的", "答", session_id="s1", messages=msgs("陌生人说的"))
    turn(a, 2, "我自己说的", author_id="me")
    a.sync_turn("我自己说的", "答", session_id="s1", messages=msgs("我自己说的"))
    cfg = {"hermes_home": str(w.home), "db": w.mem("life").db_path, "read_scope": "life_all", "owners": {"telegram": ["me"]}, "mode": "tools_only"}
    (w.home / "yuanwei").mkdir(exist_ok=True)
    (w.home / "yuanwei" / "config.json").write_text(json.dumps(cfg))
    st = HS.install_status(str(w.home))
    assert {x["label"] for x in st["workspaces"]} == {"生活", "甲项目", "乙项目"}
    life = [x for x in st["workspaces"] if x["key"] == "life"][0]
    assert life["messages"] == 2 + 2                                                                 # 种子 2 条＋这次主人的一问一答
    rt = st["runtime"]
    assert rt["recorded_turns"] == 1 and rt["skipped"] == {"not_owner": 1} and rt["workspace"]["key"] == "life" and rt["read_scope"] == "life_all"
    assert "陌生人" not in json.dumps(st, ensure_ascii=False) and "我自己说的" not in json.dumps(st, ensure_ascii=False)


def test_the_runtime_status_file_has_only_numbers_and_reasons(tmp_path):
    w = World(tmp_path, scope="isolated")
    a = w.adapter("/work/a")
    turn(a, 1, "一句不能出现在状态文件里的话")
    a.sync_turn("一句不能出现在状态文件里的话", "答", session_id="s1", messages=msgs("一句不能出现在状态文件里的话"))
    raw = (w.home / "yuanwei" / "status.json").read_text(encoding="utf-8")
    d = json.loads(raw)
    assert d["recorded_turns"] == 1 and d["workspace"]["key"] == "proj:p-a" and "不能出现" not in raw
    b = w.adapter("relative/path")                                                                   # 给了但认不出工作区：原因也要写出来
    assert json.loads((w.home / "yuanwei" / "status.json").read_text())["workspace"]["reason"] == "cwd_not_absolute"
    assert b.status()["workspace"]["key"] is None


def test_cli_status_hermes_prints_json_and_changes_nothing(tmp_path, capsys):
    h = install(tmp_path)
    before = snapshot(h)
    assert cli.main(["status", "hermes", "--hermes-home", str(h)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is True and out["provider_active"] == "yuanwei"
    assert snapshot(h) == before
    assert cli.main(["status", "hermes", "--hermes-home", str(tmp_path / "nothing")]) == 0           # 没装：照样说清楚，不是报错
    assert json.loads(capsys.readouterr().out)["installed"] is False


def test_uninstall_removes_the_runtime_status_file_but_keeps_the_users_memory_and_workspace_labels(tmp_path):
    h = install(tmp_path)
    (h / "yuanwei" / "status.json").write_text("{}")
    (h / "yuanwei" / "yuanwei.sqlite").write_bytes("用户的记忆库".encode())
    (h / "yuanwei" / "workspaces.json").write_text("{}")
    HS.apply_uninstall(str(h))                                                                       # ① 空壳 ② 配置
    r = HS.apply_uninstall(str(h), host_reloaded=True)                                               # ③ 删我们装的文件
    assert not (h / "yuanwei" / "status.json").exists() and not (h / "plugins" / "yuanwei").exists() and not (h / "yuanwei" / "config.json").exists()
    assert (h / "yuanwei" / "yuanwei.sqlite").read_bytes() == "用户的记忆库".encode() and (h / "yuanwei" / "workspaces.json").exists()       # 记忆不是我们装的东西，不删
    assert (h / "config.yaml").read_text() == CFG0


def test_a_skip_alone_is_written_to_the_status_file_and_the_file_never_holds_owner_ids(tmp_path):
    w = World(tmp_path, scope="isolated", owners={"telegram": ["owner-9981"]})
    a = w.adapter(None, platform="telegram", user_id="owner-9981", chat_type="group")
    turn(a, 1, "群里别人的话", author_id="stranger-1")
    a.sync_turn("群里别人的话", "答", session_id="s1", messages=msgs("群里别人的话"), turn_author={"id": "stranger-1", "name": "路人", "is_bot": False})
    raw = (w.home / "yuanwei" / "status.json").read_text(encoding="utf-8")
    d = json.loads(raw)
    assert d["recorded_turns"] == 0 and d["skipped"] == {"not_owner": 1}                              # 没有记下任何一轮，只有跳过：也要写出来
    assert "owner-9981" not in raw and "stranger-1" not in raw and "群里别人的话" not in raw
