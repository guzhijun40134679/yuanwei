"""一次查询共享：独立临时数据库、真实查询准备链、假向量服务。"""
import pytest
from helpers2 import IDENT, FakeEmbedClient, hybrid
from yuanwei import Config, Memory
from yuanwei import vectors as VX
from yuanwei import enginerun as ER

pytestmark = pytest.mark.usefixtures("calibrated_test_vectors")


def test_same_identity_across_three_stores_counts_one_actual_batch(tmp_path):
    pairs = [hybrid(tmp_path, name=f"{i}.sqlite") for i in range(3)]
    with VX.query_scope():
        results = [m._reader.select_multi("新的预算问题", ["海边情况"]) for m, c in pairs]
    assert [r["stats"]["vector_calls"] for r in results] == [1, 0, 0]
    assert sum(len(c.calls) for m, c in pairs) == 1
    fp = VX.identity_fp(IDENT)
    keys = {VX.text_key(t) for t in ["新的预算问题", "海边情况"]}
    assert all(len(m.store.get_vectors(fp, keys)) == 2 for m, c in pairs)


def test_different_full_identity_is_not_shared_even_same_model_file(tmp_path):
    a, ca = hybrid(tmp_path, name="a.sqlite")
    b, cb = hybrid(tmp_path, name="b.sqlite")
    changed = dict(IDENT, runtime_version="other")
    with VX.query_scope():
        VX.ensure_query_vectors(a.store, IDENT, ["问题"], ca)
        VX.ensure_query_vectors(b.store, changed, ["问题"], cb)
    assert len(ca.calls) == len(cb.calls) == 1


def test_existing_query_cache_seeds_scope_without_call(tmp_path):
    a, ca = hybrid(tmp_path, name="a.sqlite")
    b, cb = hybrid(tmp_path, name="b.sqlite")
    VX.register(a.store, IDENT, {"问题": [1, 0, 0, 0]})
    with VX.query_scope():
        VX.ensure_query_vectors(a.store, IDENT, ["问题"], ca)
        VX.ensure_query_vectors(b.store, IDENT, ["问题"], cb)
    assert ca.calls == cb.calls == []
    assert b.store.get_vectors(VX.identity_fp(IDENT), [VX.text_key("问题")])[VX.text_key("问题")] == [1, 0, 0, 0]


class Down:
    def __init__(self): self.calls = 0
    def embed_batch(self, texts):
        self.calls += 1
        raise VX.ServiceDown("timeout", reason="engine_starting")


def test_timeout_shared_but_reason_logged_per_workspace_and_next_call_retries(tmp_path):
    pairs = [hybrid(tmp_path, name=f"{i}.sqlite", client=Down(), vector_fallback="partial") for i in range(3)]
    with VX.query_scope():
        results = [m._reader.select_multi("预算最新消息") for m, c in pairs]
    assert [r["stats"]["vector_calls"] for r in results] == [1, 0, 0]
    assert all(r["stats"]["retrieval"]["reason"] == "engine_starting" for r in results)
    assert all(r["selected"] for r in results)
    pairs[1][0]._reader.select_multi("预算最新消息")
    assert [c.calls for m, c in pairs] == [1, 1, 0]


def test_scope_does_not_leak_success_to_later_recall(tmp_path):
    a, ca = hybrid(tmp_path, name="a.sqlite")
    b, cb = hybrid(tmp_path, name="b.sqlite")
    a._reader.select_multi("新的问题")
    b._reader.select_multi("新的问题")
    assert len(ca.calls) == len(cb.calls) == 1


@pytest.mark.parametrize("timeout", [None, 5, 14])
def test_http_managed_and_resolved_client_timeout_defaults_and_explicit_values(tmp_path, timeout):
    emb = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "identity": IDENT}
    if timeout is not None: emb["timeout_query"] = timeout
    expected = 10 if timeout is None else timeout
    assert VX.build_client(emb).inner.timeout == expected
    assert ER.resolve_client(emb, "query").inner.timeout == expected
    managed = dict(emb, engine_dir=str(tmp_path / "engine"))
    assert ER.ManagedClient(managed).timeout == expected


def test_explicit_old_toml_unchanged_status_advises(tmp_path):
    path = tmp_path / "old.toml"
    path.write_text('[embedding]\ntimeout_query = 5\n')
    before = path.read_bytes()
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config.load(path), spawn="off")
    info = mem.status()["retrieval"]["query_timeout"]
    assert info == {"seconds": 5, "advisory": "查询超时设得偏紧，可以改成 10"}
    assert path.read_bytes() == before
    assert VX.query_timeout_status({}) == {"seconds": 10, "advisory": None}


def test_timeout_actual_read_logs_keep_reason_for_each_source(tmp_path):
    from yuanwei.access import Access
    from yuanwei.idsource import InProcessIdentity
    from yuanwei.tools import ToolSurface
    from yuanwei import hosts as H
    pairs = [hybrid(tmp_path, name=f"log{i}.sqlite", client=Down(), vector_fallback="partial") for i in range(3)]
    logs = []
    with VX.query_scope():
        for mem, client in pairs:
            access = Access(mem)
            ids = InProcessIdentity()
            ids.set("pi", "S", "", "T")
            access.begin_turn("pi", "S", "T")
            result = ToolSurface(mem, access, H.profile("pi"), ids).call("recall", {"query": "预算最新消息"})
            assert result.status == "ok"
            logs.append([r["detail"] for r in access.log_rows("pi", "S", "T") if r["kind"] == "retrieval"])
    assert all(rows and rows[0]["reason"] == "engine_starting" for rows in logs)
    assert [c.calls for m, c in pairs] == [1, 0, 0]


def test_hermes_status_and_doctor_surface_old_timeout_without_new_error(tmp_path, monkeypatch, capsys):
    import json
    from test_relevance_diagnostics import installed
    from yuanwei import cli, hermes_compat as HC, owner_diagnostics as OD, hermes_setup as HS
    home = installed(tmp_path, cfg='[embedding]\ntimeout_query = 5\n')
    before = (home / "yuanwei/config.toml").read_bytes()
    state = HS.install_status(str(home))
    assert "查询超时设得偏紧，可以改成 10" in state["notes"]
    monkeypatch.setattr(HC, "doctor", lambda path: {"exit_code": 0})
    monkeypatch.setattr(OD, "status", lambda home: {"missing": []})
    assert cli.main(["doctor", "hermes", "--hermes-home", str(home)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["relevance"]["query_timeout"]["seconds"] == 5
    assert report["relevance"]["query_timeout"]["advisory"]
    assert (home / "yuanwei/config.toml").read_bytes() == before
