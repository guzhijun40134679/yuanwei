"""自带向量引擎：向导、命令、和现有机制（部分向量、删除屏障、读取登记、卸载）的衔接。假系统、假下载（走真的下载代码）、假 llama-server；不联网。"""
import json
import os
import subprocess
import sys
import time
import tomllib

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT, TEXTS, FakeEmbedClient, fill, hybrid
from helpers_engine import Kit, MODEL_BYTES, OFFICIAL_BYTES, spawner_for, stop_all, wait_for
from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home, tree_bytes
from test_deletion_core import delete
from yuanwei import Config, Memory
from yuanwei import catalog as CT
from yuanwei import cli
from yuanwei import embedsetup as ES
from yuanwei import embedwork as EW
from yuanwei import enginepkg as EP
from yuanwei import enginerun as ER
from yuanwei import vectors as VX
from yuanwei import wizard as WZ

ARM = {"system": "Darwin", "machine": "arm64", "arm64_hardware": True}
BASE = ["--config-method", "yaml", "--src-path", "/x/src", "--hermes-python", "/fake/bin/py"]


def py_ok(argv):
    return 0, "2.0.0\n", ""


def _unverified_copy_of_real_pins():
    import copy
    p = copy.deepcopy(CT.PINS)
    p["engine"]["verified"] = False
    return p


class W:
    """一次向导的全套假东西。"""

    def __init__(self, tmp_path, platform=ARM, verified=True, model_dirs=True, real_pins=False):
        self.tmp = tmp_path
        self.kit = Kit(tmp_path, verified=verified)
        self.home = make_home(tmp_path)
        self.procs = []
        self.engine_dir = str(tmp_path / "engine")
        self.sys = FakeSystem(http=FakeHTTP("custom", models=[]), commands={"py": py_ok}, env={"DEEPSEEK_API_KEY": FAKE_KEY}, platform=platform)
        self.sys.pins = _unverified_copy_of_real_pins() if real_pins else self.kit.pins        # 真表现在核过了（修补：自带引擎钉住）：「没核过」的情形用它的副本，把 verified 改回假
        self.sys.fetcher = self.kit.net.fetcher()
        self.sys.engine_spawner = spawner_for(self.kit, self.engine_dir, cfg={"poll_seconds": 0.05}, procs=self.procs)
        self.lm = self.kit.lmstudio_dir() if model_dirs else str(tmp_path / "no-models")
        self.paths = EP.Paths(self.engine_dir)

    def args(self, *more, preset="recommended", vectors="llamacpp"):
        a = ["setup", "--host", "hermes", "--preset", preset, "--hermes-home", self.home, "--engine-dir", self.engine_dir] + BASE
        if vectors:
            a += ["--vectors", vectors]
        return a + list(more)

    def run(self, *more, **kw):
        return cli._setup_main(self.args(*more, **kw), io=FakeIO(tty=False), sys_=self.sys)

    def toml(self):
        return tomllib.load(open(os.path.join(self.home, "yuanwei", "yuanwei.toml"), "rb"))

    def close(self):
        stop_all(self.paths, self.procs)


@pytest.fixture
def w(tmp_path, monkeypatch):
    x = W(tmp_path)
    monkeypatch.setattr(EP, "DEFAULT_MODEL_DIRS", (x.lm,))
    yield x
    x.close()


def code_of(capsys, rc):
    out = capsys.readouterr()
    try:
        return rc, json.loads(out.err)["error"] if out.err.strip().startswith("{") else None, out
    except ValueError:
        return rc, None, out


# ================================================================ 参数写法
def test_unsupported_platform_refuses_llamacpp_and_auto_with_the_unsupported_sentence(tmp_path, capsys):
    x = W(tmp_path, platform={"system": "Linux", "machine": "x86_64", "arm64_hardware": False})
    for v in ("llamacpp", "auto"):
        rc = x.run("--yes", vectors=v)
        out = capsys.readouterr()
        assert rc == 2 and "engine_unsupported" in out.err and "暂不支持" in out.err and "LM Studio" in out.err
    assert x.kit.net.hits == [] and not os.path.exists(os.path.join(x.home, "yuanwei"))


def test_platform_detection_uses_hardware_not_python_architecture():
    assert ES.engine_supported({"system": "Darwin", "machine": "x86_64", "arm64_hardware": True})              # Python 在 Rosetta 下
    assert not ES.engine_supported({"system": "Darwin", "machine": "x86_64", "arm64_hardware": False})
    assert not ES.engine_supported({"system": "Linux", "machine": "arm64", "arm64_hardware": True})


def test_real_unverified_pins_refuse_everything_and_change_nothing(tmp_path, capsys):
    x = W(tmp_path, real_pins=True)
    before = tree_bytes(x.home)
    rc = x.run("--yes", "--download-model")
    out = capsys.readouterr()
    assert rc == 2 and "pins_unverified" in out.err and x.kit.net.hits == [] and tree_bytes(x.home) == before and not os.path.exists(x.engine_dir)


def test_plan_without_yes_lists_everything_and_changes_nothing(w, capsys):
    before = tree_bytes(w.home)
    rc = w.run()
    out = json.loads(capsys.readouterr().out)
    txt = "\n".join(out["plan"]["lines"])
    assert rc == 0 and out["applied"] is False and "自带（llama.cpp 服务程序" in txt and "需要下载" in txt and "还没同意" in txt and "本机已有" in txt
    assert "一致性：一致性还没在真机上核" in txt and w.kit.net.hits == [] and tree_bytes(w.home) == before and not os.path.exists(w.engine_dir)


def test_yes_alone_is_not_consent_to_download(w, capsys):
    before = tree_bytes(w.home)
    rc = w.run("--yes")
    out = capsys.readouterr()
    assert rc == 2 and "download_not_consented" in out.err and w.kit.net.hits == [] and tree_bytes(w.home) == before and w.procs == []


def test_full_install_reusing_the_existing_model_file_end_to_end(w, capsys):
    rc = w.run("--yes", "--download-model")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["applied"] and [s for s in out["steps"] if s.startswith(("engine", "verify", "install"))] == ["engine_program", "engine_model", "verify", "install"]
    assert w.kit.net.hits == [w.kit.url]                                                                       # 程序下载了；模型是本机现成的，没有下载
    t = w.toml()
    e = t["embedding"]
    assert t["retrieval"] == "hybrid" and t["vector_fallback"] == "partial" and e["managed"] is True and e["runtime"] == "llamacpp" and "base_url" not in e and e["engine_dir"] == w.engine_dir
    assert set(e["identity"]) == set(VX.IDENTITY_FIELDS) and e["identity"]["context_length"] == 2048 and e["identity"]["model_file_sha256"] == EP.sha256_file(os.path.join(w.lm, "Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"))
    assert e["identity"]["runtime_version"] == EP.expected_runtime_version(w.kit.pins) and e["weak_identity"] is False and e["idle_exit_seconds"] == 900 and e["cold_wait_seconds"] == 0
    rc_ = EP.read_receipt(w.paths)
    assert rc_["model"]["source"] == "reused" and rc_["model"]["owned"] is False and ER.list_users(w.paths) == [WZ.user_id(w.home)]
    assert ER.lifecycle_state(w.paths) == "active"
    cfg = Config.load(os.path.join(w.home, "yuanwei", "yuanwei.toml"))
    assert cfg.embedding["managed"] is True and cfg.retrieval == "hybrid"


def test_full_install_downloads_the_model_when_none_is_found_and_marks_it_owned(tmp_path, monkeypatch, capsys):
    x = W(tmp_path, model_dirs=False)
    monkeypatch.setattr(EP, "DEFAULT_MODEL_DIRS", (x.lm,))
    try:
        rc = x.run("--yes", "--download-model")
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and x.kit.net.hits == [x.kit.url, x.kit.model_url]
        m = EP.read_receipt(x.paths)["model"]
        assert m["source"] == "downloaded" and m["owned"] is True and m["sha256"] == EP.sha256_file(m["path"]) and open(m["path"], "rb").read() == OFFICIAL_BYTES
        assert "不是同一个" in EP.model_wording(m["sha256"], x.kit.pins)                                       # 官方文件和测过的摘要不同：不许写「同一个模型文件」
    finally:
        x.close()


def test_failure_at_verify_writes_no_config_and_keeps_downloaded_files(tmp_path, monkeypatch, capsys):
    x = W(tmp_path)
    monkeypatch.setattr(EP, "DEFAULT_MODEL_DIRS", (x.lm,))
    x.sys.engine_spawner = spawner_for(x.kit, x.engine_dir, cfg={"poll_seconds": 0.05}, child_env={"FAKE_LLAMA_DIM": "768"}, procs=x.procs)
    try:
        before = tree_bytes(x.home)
        rc = x.run("--yes", "--download-model")
        out = capsys.readouterr()
        assert rc == 2 and "verify_failed" in out.err and "dimension" in out.err and tree_bytes(x.home) == before
        assert EP.installed_package(x.paths, x.kit.pins) is not None                                           # 已下载并核验过的程序留着
    finally:
        x.close()


def test_sha_mismatch_download_stops_everything_before_any_config(tmp_path, monkeypatch, capsys):
    x = W(tmp_path)
    x.kit.net.routes[x.kit.url] = b"not the engine"
    try:
        before = tree_bytes(x.home)
        rc = x.run("--yes", "--download-model")
        out = capsys.readouterr()
        assert rc == 2 and "engine_sha256_mismatch" in out.err and tree_bytes(x.home) == before and EP.installed_package(x.paths, x.kit.pins) is None and x.procs == []
    finally:
        x.close()


def test_engine_url_and_model_url_must_be_https_and_do_not_change_the_pinned_hash(tmp_path, capsys):
    x = W(tmp_path)
    rc = x.run("--yes", "--download-model", "--engine-url", "http://mirror.test/e.zip")
    assert rc == 2 and "not_https" in capsys.readouterr().err
    rc = x.run("--yes", "--download-model", "--model-url", "http://mirror.test/m.gguf")
    assert rc == 2 and "not_https" in capsys.readouterr().err


def test_model_file_flag_with_wrong_sha_is_refused_in_the_plan_stage_and_in_apply(w, tmp_path, capsys):
    f = tmp_path / "mine.gguf"
    f.write_bytes(b"other bytes")
    rc = w.run("--yes", "--download-model", "--model-file", str(f))
    out = capsys.readouterr()
    assert rc == 2 and "model_file_sha_mismatch" in out.err or "download_not_consented" in out.err or "engine_" in out.err
    assert not os.path.exists(os.path.join(w.home, "yuanwei", "yuanwei.toml"))


# ================================================================ 一问一答
def _interactive(w, answers, tmp_path):
    home = make_home(tmp_path, "h2")
    import argparse
    a = cli._setup_main.__globals__  # noqa: F841  只为确认模块可导入
    io = FakeIO(answers=answers)
    rc = cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", w.engine_dir] + BASE, io=io, sys_=w.sys)
    return rc, io, home


def _wizard_answers(vec_answers):
    # 第 2 步助手；第 3 步预设（推荐）；第 4 步 DeepSeek 用这个；第 5 步密钥已有（环境里有）→ 问「用它吗」y；第 6 步 开吗＋向量选择…；第 7 步范围、是不是只在本机
    return ["1", "1", "y", "y"] + vec_answers + ["1", "y", "y"]


def test_interactive_offers_builtin_first_on_apple_silicon_marks_recommended_and_shows_sizes_and_consent(w, tmp_path):
    rc, io, home = _interactive(w, _wizard_answers(["y", "1", "y"]), tmp_path)
    t = io.text
    assert rc == 0 and t.index("1) 自带（推荐）") < t.index("2) 用你电脑上已有的 LM Studio 或 Ollama") < t.index("3) 先不开向量")
    assert "不用你装别的软件" in t and "要下载：" in t and "向量程序 llama.cpp" in t and "MIT" in t and "本机已有（同一个模型文件（SHA-256 和测过的相同））" in t and "校验码" in t
    assert os.path.exists(os.path.join(home, "yuanwei", "yuanwei.toml"))
    w.close()


def test_interactive_declining_the_download_downloads_nothing_and_turns_vectors_off(w, tmp_path):
    rc, io, home = _interactive(w, _wizard_answers(["y", "1", "n"]), tmp_path)
    assert rc == 0 and w.kit.net.hits == [] and "不下载，向量就先不开" in io.text
    t = tomllib.load(open(os.path.join(home, "yuanwei", "yuanwei.toml"), "rb"))
    assert t["retrieval"] == "keyword" and "embedding" not in t


def test_interactive_cancel_at_every_new_prompt_changes_nothing_and_downloads_nothing(w, tmp_path):
    full = _wizard_answers(["y", "1", "y"])
    for k in range(len(full)):
        home = make_home(tmp_path, "c%d" % k)
        before = tree_bytes(home)
        io = FakeIO(answers=full[:k])
        rc = cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", w.engine_dir] + BASE, io=io, sys_=w.sys)
        assert rc == 1 and tree_bytes(home) == before and w.kit.net.hits == [] and not os.path.exists(w.engine_dir), k


def test_interactive_on_unverified_pins_says_not_ready_and_lets_the_user_pick_something_else(tmp_path):
    x = W(tmp_path, real_pins=True)
    home = make_home(tmp_path, "h3")
    io = FakeIO(answers=_wizard_answers(["y", "1", "3"]))
    rc = cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", x.engine_dir] + BASE, io=io, sys_=x.sys)
    assert rc == 0 and "现在选不了" in io.text and "还没准备好" in io.text and x.kit.net.hits == [] and not os.path.exists(x.engine_dir)


def test_interactive_on_unsupported_platform_says_unsupported_and_keeps_the_old_flow(tmp_path):
    x = W(tmp_path, platform={"system": "Linux", "machine": "x86_64", "arm64_hardware": False})
    home = make_home(tmp_path, "h4")
    io = FakeIO(answers=_wizard_answers(["y", "2"]))
    cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", x.engine_dir] + BASE, io=io, sys_=x.sys)
    assert "暂不支持" in io.text and "自带（推荐）" not in io.text


# ================================================================ 命令
def test_vectors_remove_engine_lists_without_yes_and_removes_with_yes(w, capsys):
    w.run("--yes", "--download-model")
    capsys.readouterr()
    rc = cli._vectors_main(["remove-engine", "--hermes-home", w.home, "--engine-dir", w.engine_dir], sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["applied"] is False and out["users"] == [WZ.user_id(w.home)] and "加 --yes" in out["hint"] and os.path.isdir(w.paths.bin)
    rc = cli._vectors_main(["remove-engine", "--yes", "--hermes-home", w.home, "--engine-dir", w.engine_dir], sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["applied"] and not os.path.isdir(w.paths.bin) and ER.lifecycle_state(w.paths) == "disabled"


def test_r1_cli_stop_and_remove_engine_exit_3_and_delete_nothing_when_a_registered_child_cannot_be_confirmed_gone(w, capsys):
    w.run("--yes", "--download-model")
    capsys.readouterr()
    ER.stop_engine(w.paths, wait=8)
    wait_for(lambda: not EP.lock_is_held(w.paths.serve_lock), 8)
    EP.write_json_atomic(w.paths.children, {"run_ids": ["7" * 32]})                                     # 守护者没了，登记还在

    class NoPs(ER.Host):
        def ps_all(self):
            return None
    w.sys.engine_host = NoPs()
    rc = cli._vectors_main(["stop", "--hermes-home", w.home, "--engine-dir", w.engine_dir], sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["stopped"] is False and out["reason"] == "process_list_unreadable"
    rc = cli._vectors_main(["remove-engine", "--yes", "--hermes-home", w.home, "--engine-dir", w.engine_dir], sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["applied"] is False and out["stopped"] is False and os.path.isdir(w.paths.bin) and os.path.exists(w.paths.receipt)
    assert EP.read_json(w.paths.children)["run_ids"] == ["7" * 32] and ER.lifecycle_state(w.paths) == "removing"


def test_vectors_stop_when_nothing_runs_reports_stopped_and_status_has_engine_segment_read_only(w, capsys, monkeypatch):
    monkeypatch.setattr(CT, "PINS", w.kit.pins)
    w.run("--yes", "--download-model")
    capsys.readouterr()
    cli._vectors_main(["stop", "--hermes-home", w.home, "--engine-dir", w.engine_dir])
    capsys.readouterr()
    wait_for(lambda: not EP.lock_is_held(w.paths.serve_lock), 8)
    before = sorted(os.listdir(w.paths.run))
    rc = cli._vectors_main(["status", "--hermes-home", w.home, "--db", os.path.join(w.home, "yuanwei", "yuanwei.sqlite"), "--config", os.path.join(w.home, "yuanwei", "yuanwei.toml")])
    capsys.readouterr()
    st = EW_status_for(w)
    assert st["engine"]["process"]["running"] is False and st["engine"]["installed"]["program"] and sorted(os.listdir(w.paths.run)) == before


def EW_status_for(w):
    from yuanwei.sqlstore import Store
    db = os.path.join(w.home, "yuanwei", "yuanwei.sqlite")
    st = Store(db)
    try:
        return EW.status(st, db, Config.load(os.path.join(w.home, "yuanwei", "yuanwei.toml")))
    finally:
        st.close()


def test_reverify_clears_the_blocked_state_and_switching_to_other_runtime_unbinds_and_keeps_files(w, capsys):
    w.run("--yes", "--download-model")
    capsys.readouterr()
    EP.write_json_atomic(w.paths.backoff, {"failures": 1, "next_try_at": time.time() + 10 ** 6, "blocked": True, "last_error": "args_rejected"})
    ER.stop_engine(w.paths, wait=8)
    wait_for(lambda: not EP.lock_is_held(w.paths.serve_lock), 8)
    rc = cli._vectors_main(["setup", "--reverify", "--yes", "--hermes-home", w.home, "--engine-dir", w.engine_dir, "--hermes-python", "/fake/bin/py"], io=FakeIO(tty=False), sys_=w.sys)
    assert rc == 0 and not os.path.exists(w.paths.backoff)
    assert ER.trusted_state(w.paths)["state"] in ("ready", "starting")
    io = FakeIO(tty=False)
    rc = cli._vectors_main(["setup", "--vectors", "none", "--yes", "--hermes-home", w.home, "--engine-dir", w.engine_dir], io=io, sys_=w.sys)
    assert rc == 0 and ER.list_users(w.paths) == [] and os.path.isdir(w.paths.bin) and "remove-engine" in io.text


def test_r1_leaving_the_engine_does_not_say_it_stopped_when_the_stop_could_not_be_confirmed(w, monkeypatch):
    w.run("--yes", "--download-model")
    ER.stop_engine(w.paths, wait=8)
    wait_for(lambda: not EP.lock_is_held(w.paths.serve_lock), 8)
    EP.write_json_atomic(w.paths.children, {"run_ids": ["8" * 32]})

    class NoPs(ER.Host):
        def ps_all(self):
            return None
    monkeypatch.setattr(ER, "Host", NoPs)                                                 # 离开引擎时没传 host：用默认的
    monkeypatch.setattr(CT, "PINS", w.kit.pins)
    io = FakeIO(tty=False)
    WZ._leave_engine({"managed": True, "engine_dir": w.engine_dir}, w.home, io)
    assert "已经停了它" not in io.text and "没能确认它已经停了" in io.text and "process_list_unreadable" in io.text


def test_uninstall_unbinds_this_user_and_leaves_the_engine_to_other_users(w, capsys):
    w.run("--yes", "--download-model")
    capsys.readouterr()
    ER.register_user(w.paths, "hermes:other")
    rc = cli._setup_main(["uninstall", "hermes", "--hermes-home", w.home, "--config-method", "yaml", "--yes"], io=FakeIO(tty=False), sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["engine"]["remaining_users"] == ["hermes:other"] and "还在用" in out["engine"]["note"]
    assert ER.list_users(w.paths) == ["hermes:other"] and os.path.isdir(w.paths.bin) and ER.trusted_state(w.paths) is not None


def test_uninstall_last_user_stops_engine_asks_to_delete_default_no_and_prints_how(w, capsys):
    w.run("--yes", "--download-model")
    capsys.readouterr()
    rc = cli._setup_main(["uninstall", "hermes", "--hermes-home", w.home, "--config-method", "yaml", "--yes"], io=FakeIO(tty=False), sys_=w.sys)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["engine"]["stopped"] is True and "remove-engine --yes" in out["engine"]["note"] and os.path.isdir(w.paths.bin)
    io = FakeIO(answers=[""], tty=True)                                                                      # 终端里问，默认否
    w.run("--yes", "--download-model")
    capsys.readouterr()
    cli._setup_main(["uninstall", "hermes", "--hermes-home", w.home, "--config-method", "yaml", "--yes"], io=io, sys_=w.sys)
    capsys.readouterr()
    assert any("要不要一起删掉自带的向量引擎" in p for p in io.asked) and os.path.isdir(w.paths.bin)


# ================================================================ 和现有机制衔接
def test_all_client_construction_goes_through_the_resolver():
    root = os.path.join(os.path.dirname(__file__), "..", "src", "yuanwei")
    for f in ("memory.py", "cli.py"):
        assert "HttpEmbeddingClient(" not in open(os.path.join(root, f), encoding="utf-8").read(), f
    emb = {"managed": True, "engine_dir": "/tmp/x-engine", "identity": dict(IDENT, dimension=1024)}
    assert isinstance(VX.build_client(emb), ER.ManagedClient) and isinstance(ER.resolve_client(emb, "worker"), ER.ManagedClient)
    plain = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "identity": IDENT}
    assert isinstance(ER.resolve_client(plain, "query"), VX.GuardedClient) and isinstance(ER.resolve_client(plain, "worker"), VX.HttpEmbeddingClient)
    with pytest.raises(VX.ProviderError):
        ER.resolve_client({"base_url": "http://example.com/v1", "model": "m", "identity": IDENT}, "worker")


def test_managed_config_without_base_url_enables_vectors_and_plain_config_unchanged(tmp_path):
    emb = {"managed": True, "engine_dir": str(tmp_path / "e"), "identity": IDENT}
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=emb), provider=Scripted("skip"), clock=Clock())
    assert mem.vectors_enabled() is True and isinstance(mem.embed_client, ER.ManagedClient)
    mem2 = Memory.open(str(tmp_path / "m2.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding={"base_url": None, "model": None, "identity": IDENT}),
                       provider=Scripted("skip"), clock=Clock())
    assert mem2.vectors_enabled() is False


def test_engine_down_is_a_partial_vector_read_registered_with_engine_starting_and_does_not_wait(tmp_path):
    spawned = []
    emb = {"managed": True, "engine_dir": str(tmp_path / "e"), "identity": IDENT}
    cl = ER.resolve_client(emb, "query", spawner=lambda: spawned.append(1))
    mem, _c = hybrid(tmp_path, client=cl, vector_fallback="partial", embedding=emb)
    t0 = time.monotonic()
    r = mem.recall("预算又变了")
    assert time.monotonic() - t0 < 2.0 and r.get("status") != "error" and spawned == [1]
    i = mem._reader.retrieval_log[-1]
    assert i["used"] == "keyword_fallback" and i["reason"] == "engine_starting"


def test_backfill_with_the_managed_client_still_discards_a_batch_when_a_deletion_happens_while_waiting_for_the_engine(tmp_path):
    kit = Kit(tmp_path)
    paths = kit.install()
    procs = []
    try:
        ident = dict(EP.build_identity("bTEST", EP.package_digest(kit.manifest), EP.recipe_digest(), EP.read_receipt(paths)["model"]["sha256"], 1024, "l2"))
        emb = {"managed": True, "engine_dir": paths.root, "identity": ident, "backoff_seconds": [10, 30, 120, 600, 1800]}
        sp = spawner_for(kit, paths.root, cfg={"poll_seconds": 0.05}, procs=procs)

        class Hook(ER.ManagedClient):
            on_call = None

            def embed_batch(self, texts):
                if Hook.on_call and texts[0] not in EW.PROBE_SENTENCES:
                    f, Hook.on_call = Hook.on_call, None
                    f()
                return super().embed_batch(texts)
        cl = Hook(emb, "worker", spawner=sp)
        mem = Memory.open(str(tmp_path / "b.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=emb), provider=Scripted("skip"), clock=Clock(),
                          embed_client=cl, spawn="off")
        mem.record("h", "S1", "a1", "user", "蓝色标记甲：这一句要被删掉。")
        mem.record("h", "S2", "b1", "user", "绿色保留乙：这一句留着。")
        mem.end_session("h", "S1")
        mem.end_session("h", "S2")
        Hook.on_call = lambda: delete(mem, "S1")                                                              # 等引擎起来、算向量之前：这一段对话被删
        out = mem.run_vectors()
        assert out["discarded_by_delete"] >= 1
        fp = VX.identity_fp(ident)
        key = VX.text_key("蓝色标记甲：这一句要被删掉。")
        assert key not in mem.store.get_vectors(fp, {key})
        assert VX.text_key("绿色保留乙：这一句留着。") in mem.store.get_vectors(fp, {VX.text_key("绿色保留乙：这一句留着。")})
    finally:
        stop_all(paths, procs)


def test_backfill_engine_unavailable_goes_into_the_existing_backoff(tmp_path):
    emb = {"managed": True, "engine_dir": str(tmp_path / "e"), "identity": IDENT, "backoff_seconds": [10, 30, 120, 600, 1800]}
    cl = ER.resolve_client(emb, "worker", spawner=lambda: None)
    cl.wait = 0.2
    mem = Memory.open(str(tmp_path / "b.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=emb), provider=Scripted("skip"), clock=Clock(), embed_client=cl,
                      spawn="off")
    mem.record("h", "S1", "a1", "user", "一句话。")
    mem.end_session("h", "S1")
    out = mem.run_vectors()
    assert out["status"] == "failed" and out["reason"] == "engine_starting"
    b = EW.backoff_state(mem.store)
    assert b["failures"] == 1 and b["last_error"] == "engine_starting"


def test_warm_engine_spawns_once_at_plugin_start_only_for_managed_and_never_raises(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ER, "spawn_supervisor", lambda *a, **k: calls.append(1))
    emb = {"managed": True, "engine_dir": str(tmp_path / "e"), "identity": IDENT}
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=emb), provider=Scripted("skip"), clock=Clock())
    r = mem.warm_engine()
    assert r["action"] == "starting" and calls == [1]
    plain = Memory.open(str(tmp_path / "p.sqlite"), "w", config=Config(), provider=Scripted("skip"), clock=Clock())
    assert plain.warm_engine() == {"action": "none"}
    adapter_src = open(os.path.join(os.path.dirname(__file__), "..", "src", "yuanwei", "hermes_adapter.py"), encoding="utf-8").read()
    assert "m.warm_engine()" in adapter_src and adapter_src.count("warm_engine") == 1                         # 只在打开时调一次，别处不调


def test_engine_directory_never_holds_conversation_text_after_a_backfill(tmp_path):
    kit = Kit(tmp_path)
    paths = kit.install()
    procs = []
    try:
        ident = EP.build_identity("bTEST", EP.package_digest(kit.manifest), EP.recipe_digest(), EP.read_receipt(paths)["model"]["sha256"], 1024, "l2")
        emb = {"managed": True, "engine_dir": paths.root, "identity": ident}
        cl = ER.resolve_client(emb, "worker", spawner=spawner_for(kit, paths.root, cfg={"poll_seconds": 0.05}, procs=procs))
        mem = Memory.open(str(tmp_path / "b.sqlite"), "w", config=Config(retrieval="hybrid", vector_fallback="partial", index_version=2, embedding=emb), provider=Scripted("skip"), clock=Clock(),
                          embed_client=cl, spawn="off")
        marker = "独一无二的对话暗号紫色大象"
        mem.record("h", "S1", "a1", "user", marker + "。")
        mem.end_session("h", "S1")
        assert mem.run_vectors()["computed"] >= 1
        for dp, _dn, fns in os.walk(paths.root):
            if dp.startswith(paths.bin) or dp.startswith(paths.models):
                continue
            for f in fns:
                assert marker.encode() not in open(os.path.join(dp, f), "rb").read(), f
    finally:
        stop_all(paths, procs)


def test_interactive_consent_names_model_size_and_license_when_no_local_model_exists(tmp_path, monkeypatch):
    x = W(tmp_path, model_dirs=False)
    monkeypatch.setattr(EP, "DEFAULT_MODEL_DIRS", (x.lm,))
    try:
        home = make_home(tmp_path, "h5")
        io = FakeIO(answers=_wizard_answers(["y", "1", "n"]))
        cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", x.engine_dir] + BASE, io=io, sys_=x.sys)
        assert "向量模型 Qwen3-Embedding-0.6B Q8_0" in io.text and "Apache-2.0" in io.text and "MIT" in io.text and "约 1 MB" in io.text and x.kit.net.hits == []
    finally:
        x.close()
