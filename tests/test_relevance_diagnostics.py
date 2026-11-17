"""状态/doctor 仅用临时假配置；不运行真实模型或 Hermes。"""
import json

from helpers import make_mem
from helpers2 import IDENT
from yuanwei import Config, cli
from yuanwei import relevance as RL
from yuanwei import relevance_diagnostics as D
from yuanwei import hermes_compat as HC
from yuanwei import owner_diagnostics as OD
from yuanwei import hermes_setup as HS
from yuanwei import vectors as VX


def installed(tmp_path, cfg=None):
    home = tmp_path / "fake-hermes"
    d = home / "yuanwei"
    d.mkdir(parents=True)
    cp = d / "config.toml"
    content = 'retrieval = "hybrid"\n[embedding.identity]\n' + ''.join(k + ' = ' + json.dumps(v) + '\n' for k, v in IDENT.items())
    cp.write_text(content if cfg is None else cfg)
    (d / "config.json").write_text(json.dumps({"hermes_home": str(home), "config_path": str(cp), "owners": {}}))
    return home


def test_unknown_model_explains_loss_and_recommendation_in_memory_status(tmp_path):
    mem = make_mem(tmp_path, retrieval="hybrid", embedding={"identity": IDENT})
    try:
        state = mem.status()["retrieval"]["relevance"]
        assert state["status"] == "uncalibrated"
        assert "换了说法的内容暂时找不到" in state["message"]
        assert "关键词" in state["message"] and "推荐" in state["recommendation"]
        assert state["vector_fp"] == VX.identity_fp(IDENT)
    finally:
        mem.close()


def test_keyword_config_does_not_claim_semantic_failure():
    state = D.from_config(Config())
    assert state["status"] == "keyword" and state["threshold"] is None


def test_known_model_reports_exact_bound_floor(monkeypatch):
    fp = VX.identity_fp(IDENT)
    monkeypatch.setitem(RL.CALIBRATIONS, RL.model_key(IDENT), {"threshold": .67, "vector_fps": [fp], "evidence": "fake-test-only"})
    state = D.from_config(Config(retrieval="hybrid", embedding={"identity": IDENT}))
    assert state["status"] == "calibrated" and state["threshold"] == .67
    assert state["recommendation"] is None


def test_doctor_unknown_is_visible_and_exit_one_without_services(tmp_path, monkeypatch, capsys):
    h = installed(tmp_path)
    before = {p: p.read_bytes() for p in h.rglob('*') if p.is_file()}
    monkeypatch.setattr(HC, "doctor", lambda path: {"exit_code": 0})
    monkeypatch.setattr(OD, "status", lambda home: {"missing": []})
    assert cli.main(["doctor", "hermes", "--hermes-home", str(h), "--hermes-dir", "/fake/source"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["relevance"]["status"] == "uncalibrated"
    assert "换了说法" in report["relevance"]["message"]
    assert before == {p: p.read_bytes() for p in h.rglob('*') if p.is_file()}


def test_doctor_preserves_existing_error_exit(tmp_path, monkeypatch, capsys):
    h = installed(tmp_path)
    monkeypatch.setattr(HC, "doctor", lambda path: {"exit_code": 2})
    monkeypatch.setattr(OD, "status", lambda home: {"missing": []})
    assert cli.main(["doctor", "hermes", "--hermes-home", str(h)]) == 2
    assert json.loads(capsys.readouterr().out)["relevance"]["status"] == "uncalibrated"


def test_copied_home_never_opens_its_target_config(tmp_path):
    h = installed(tmp_path)
    p = h / "yuanwei/config.json"
    raw = json.loads(p.read_text())
    raw["hermes_home"] = str(tmp_path / "someone-else")
    raw["config_path"] = "/path/that/must/not/be/opened"
    p.write_text(json.dumps(raw))
    assert D.installed(str(h))["status"] == "unconfirmed"


def test_diagnostics_distinguish_missing_configuration_and_invalid_identity(tmp_path):
    assert D.installed(str(tmp_path / "absent"))["status"] == "not_installed"
    h = installed(tmp_path)
    (h / "yuanwei/config.toml").unlink()
    assert D.installed(str(h))["status"] == "unavailable"
    assert D.from_config(Config(retrieval="hybrid", embedding={"identity": {"bad": "input"}}))["status"] == "invalid_identity"


def test_hermes_status_exposes_relevance_even_without_database(tmp_path, monkeypatch):
    h = installed(tmp_path)
    # Only fake local installation files; no subprocess or model client.
    out = HS.install_status(str(h))
    assert out["relevance"]["status"] == "uncalibrated"
    assert any("换了说法" in line for line in out["notes"])


def test_missing_identity_is_configuration_error_not_keyword_fallback():
    state = D.from_config(Config(retrieval="hybrid"))
    assert state["status"] == "invalid_identity"
    assert "混合检索不能运行" in state["message"]
    assert "当前只保留关键词命中" not in state["message"]


def test_unlisted_runtime_of_known_model_shows_basis_and_doctor_exit_is_not_one(tmp_path, monkeypatch, capsys):
    other = dict(IDENT, runtime_version="old-runtime")
    monkeypatch.setitem(RL.CALIBRATIONS, RL.model_key(IDENT), {"threshold": .67, "vector_fps": [VX.identity_fp(IDENT)], "evidence": "fake-test-only"})
    state = D.from_config(Config(retrieval="hybrid", embedding={"identity": other}))
    assert state["status"] == "calibrated" and state["basis"] == "same_file_unlisted_runtime" and state["threshold"] == .67
    assert "当前运行程序未实测" in state["message"]
    mem = make_mem(tmp_path, retrieval="hybrid", embedding={"identity": other})
    try:
        assert mem.status()["retrieval"]["relevance"]["basis"] == "same_file_unlisted_runtime"
    finally:
        mem.close()
    cfg = 'retrieval = "hybrid"\n[embedding.identity]\n' + ''.join(k + ' = ' + json.dumps(v) + '\n' for k, v in other.items())
    h = installed(tmp_path / "doc", cfg=cfg)
    monkeypatch.setattr(HC, "doctor", lambda path: {"exit_code": 0})
    monkeypatch.setattr(OD, "status", lambda home: {"missing": []})
    assert cli.main(["doctor", "hermes", "--hermes-home", str(h), "--hermes-dir", "/fake/source"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["relevance"]["status"] == "calibrated" and report["relevance"]["basis"] == "same_file_unlisted_runtime"
