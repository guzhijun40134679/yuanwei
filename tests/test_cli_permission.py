"""持续开关接进各家的命令：yuanwei read-permission set --deny --scope session --host <宿主> --session <会话>。第二段·交回点甲。"""
import json

import pytest

from helpers import make_mem
from yuanwei import cli
from yuanwei.access import Access


def run(tmp_path, *args, capsys=None):
    rc = cli.main(["--db", str(tmp_path / "m.sqlite"), "--workspace", "w", *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_host_scoped_session_setting_set_get_clear(tmp_path, capsys):
    make_mem(tmp_path).close()
    rc, out, err = run(tmp_path, "read-permission", "set", "--deny", "--scope", "session", "--host", "claude-code", "--session", "S1", capsys=capsys)
    assert rc == 0 and json.loads(out)["allowed"] is False
    mem = make_mem(tmp_path)
    acc = Access(mem)
    assert acc.session_permission("claude-code", "S1")["allowed"] is False
    assert acc.session_permission("claude-code", "S2")["allowed"] is True and acc.session_permission("codex", "S1")["allowed"] is True
    mem.close()
    rc, out, _ = run(tmp_path, "read-permission", "get", "--scope", "session", "--host", "claude-code", "--session", "S1", capsys=capsys)
    assert rc == 0 and json.loads(out)["allowed"] is False
    rc, out, _ = run(tmp_path, "read-permission", "clear", "--scope", "session", "--host", "claude-code", "--session", "S1", capsys=capsys)
    assert rc == 0 and json.loads(out)["cleared"] == 1
    assert Access(make_mem(tmp_path)).session_permission("claude-code", "S1")["allowed"] is True


def test_without_host_the_first_stage_behaviour_is_unchanged(tmp_path, capsys):
    make_mem(tmp_path).close()
    rc, out, _ = run(tmp_path, "read-permission", "set", "--deny", "--scope", "session", "--session", "S1", capsys=capsys)
    assert rc == 0
    mem = make_mem(tmp_path)
    assert mem.read_permission("S1")["allowed"] is False                         # 第一段的写法：只有会话号


def test_host_needs_a_session_scope_and_a_session(tmp_path, capsys):
    make_mem(tmp_path).close()
    rc, out, err = run(tmp_path, "read-permission", "set", "--deny", "--scope", "workspace", "--host", "pi", capsys=capsys)
    assert rc == 2 and "--host" in err
    rc, out, err = run(tmp_path, "read-permission", "set", "--deny", "--scope", "session", "--host", "pi", capsys=capsys)
    assert rc == 2
    rc, out, err = run(tmp_path, "read-permission", "set", "--deny", "--scope", "session", "--host", "", "--session", "S", capsys=capsys)
    assert rc == 2
