"""库结构 v6：加绑定表（turns、read_log、connections、dir_state）。旧库能升上来，旧代码打不开新库。"""
import sqlite3

import pytest

import yuanwei.sqlstore as SQ
from helpers import make_mem
from yuanwei.storeerr import StoreError


def tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_new_db_is_v6_and_has_binding_tables(tmp_path):
    s = SQ.Store(str(tmp_path / "a.sqlite"))
    assert s.schema_version() == SQ.SCHEMA_VERSION == 12
    assert {"turns", "read_log", "connections", "dir_state"} <= tables(str(tmp_path / "a.sqlite"))
    s.close()


def test_v5_db_upgrades_and_keeps_its_data(tmp_path, monkeypatch):
    p = str(tmp_path / "old.sqlite")
    from helpers import make_old_db
    make_old_db(p, 5, deny_read=True)
    assert "turns" not in tables(p)
    mem2 = make_mem(tmp_path, name="old.sqlite")
    assert mem2.store.schema_version() == 12 and "turns" in tables(p)
    assert [r["content"] for r in mem2.store.list_messages(mem2.ns)] == ["旧库里的一句话"]
    assert mem2.read_permission()["allowed"] is False
    mem2.close()


def test_old_code_cannot_open_a_v6_db(tmp_path, monkeypatch):
    p = str(tmp_path / "new.sqlite")
    SQ.Store(p).close()
    monkeypatch.setattr(SQ, "SCHEMA_VERSION", 5)
    with pytest.raises(StoreError):
        SQ.Store(p)
