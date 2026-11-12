"""自带向量引擎：程序和模型的准备（R1、R6）。全部用假的钉住表、假下载（走真的 Fetcher 代码）、假运行包；不联网、不运行真的 llama.cpp。"""
import os
import stat
import tarfile
import threading
import time
import urllib.error
import zipfile

import pytest

from helpers_engine import FakeNet, FakeResp, Kit, MODEL_BYTES, OFFICIAL_BYTES, make_targz, make_zip, manifest_of, package_files, sha
from yuanwei import catalog as CT
from yuanwei import enginepkg as EP
from yuanwei import vectors as VX


# ---------------------------------------------------------------- R1：钉住的值没核过就不放行，没有任何开关
def test_unverified_pins_refuse_download_and_no_env_flag_bypasses(tmp_path, monkeypatch):
    import copy
    pins = copy.deepcopy(CT.PINS)                                                      # 真表现在核过了；没核过的情形用它的副本（verified 改回假）
    pins["engine"]["verified"] = False
    for m in pins["models"]:
        m["verified"] = False
    assert EP.pins_problems(pins)
    net = FakeNet()
    monkeypatch.setenv("YUANWEI_ALLOW_UNVERIFIED_PINS", "1")                          # 老设计里的开关：现在什么都不做
    paths = EP.Paths(str(tmp_path / "e"))
    with pytest.raises(EP.EngineError) as e:
        EP.install_program(paths, pins, net.fetcher())
    assert e.value.code == "pins_unverified" and net.hits == []
    with pytest.raises(EP.EngineError):
        EP.download_model(paths, pins, net.fetcher())
    assert net.hits == []
    import inspect
    src = open(EP.__file__, encoding="utf-8").read() + open(os.path.join(os.path.dirname(EP.__file__), "enginerun.py"), encoding="utf-8").read()
    assert "ALLOW_UNVERIFIED" not in src


def test_a_single_none_cell_or_verified_false_blocks_even_when_other_cells_are_filled(tmp_path):
    k = Kit(tmp_path)
    assert EP.pins_problems(k.pins) == []
    for path in (("engine", "assets", "darwin-arm64", "sha256"), ("engine", "assets", "darwin-arm64", "manifest"), ("engine", "tag"), ("engine", "verified")):
        import copy
        p = copy.deepcopy(k.pins)
        d = p
        for x in path[:-1]:
            d = d[x]
        d[path[-1]] = None if path[-1] != "verified" else False
        assert EP.pins_problems(p), path
    import copy
    p = copy.deepcopy(k.pins)
    p["engine"]["assets"]["darwin-arm64"]["url"] = "http://example.test/x.zip"
    assert "engine.url_not_https" in EP.pins_problems(p)


# ---------------------------------------------------------------- R1：下载
def test_fetch_https_only_and_http_redirect_refused(tmp_path):
    f = EP.Fetcher(opener=FakeNet())
    with pytest.raises(EP.EngineError) as e:
        f.fetch("http://example.test/x", str(tmp_path / "a"), "0" * 64, 10, 10)
    assert e.value.code == "not_https" and not os.path.exists(str(tmp_path / "a"))
    h = EP.HTTPSOnlyRedirect()
    import urllib.request
    with pytest.raises(EP.EngineError) as e:
        h.redirect_request(urllib.request.Request("https://a.test/"), None, 302, "x", {}, "http://evil.test/y")
    assert e.value.code == "not_https"


def test_fetch_wrong_hash_deletes_file_and_reports_only_category(tmp_path):
    net = FakeNet({"https://example.test/x?sig=SECRETSIG": b"hello"})
    dest = str(tmp_path / "d")
    with pytest.raises(EP.EngineError) as e:
        EP.Fetcher(opener=net).fetch("https://example.test/x?sig=SECRETSIG", dest, sha(b"other"), 100, 10)
    assert e.value.code == "sha256_mismatch" and not os.path.exists(dest)
    assert "SECRETSIG" not in str(e.value) and "SECRETSIG" not in repr(e.value.detail)
    with pytest.raises(EP.EngineError) as e2:
        EP.Fetcher(opener=FakeNet()).fetch("https://example.test/missing?sig=SECRETSIG", dest, sha(b"x"), 100, 10)
    assert e2.value.code == "http_404" and "SECRETSIG" not in str(e2.value)


def test_fetch_limits_bytes_while_streaming_even_without_content_length_and_limits_time(tmp_path):
    big = b"x" * 5000
    net = FakeNet({"https://example.test/big": lambda: FakeResp(big, headers={}, chunk=100)})
    dest = str(tmp_path / "d")
    with pytest.raises(EP.EngineError) as e:
        EP.Fetcher(opener=net).fetch("https://example.test/big", dest, sha(big), 1000, 10)
    assert e.value.code == "too_large" and not os.path.exists(dest)
    net2 = FakeNet({"https://example.test/cl": lambda: FakeResp(b"abc", headers={"Content-Length": "999999999"})})
    with pytest.raises(EP.EngineError) as e:
        EP.Fetcher(opener=net2).fetch("https://example.test/cl", dest, sha(b"abc"), 1000, 10)
    assert e.value.code == "too_large"
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 5.0
        return clock["t"]
    with pytest.raises(EP.EngineError) as e:
        EP.Fetcher(opener=FakeNet({"https://example.test/slow": lambda: FakeResp(big, headers={}, chunk=10)}), mono=mono).fetch("https://example.test/slow", dest, sha(big), 10 ** 6, 12)
    assert e.value.code == "timeout" and not os.path.exists(dest)


def test_fetch_success_writes_0600_and_progress(tmp_path):
    data = b"y" * 3000
    seen = []
    dest = str(tmp_path / "ok")
    n = EP.Fetcher(opener=FakeNet({"https://example.test/ok": lambda: FakeResp(data, chunk=1000)})).fetch("https://example.test/ok", dest, sha(data), 10000, 10, progress=lambda a, b: seen.append(a))
    assert n == 3000 and open(dest, "rb").read() == data and stat.S_IMODE(os.stat(dest).st_mode) == 0o600 and seen[-1] == 3000


def test_user_url_changes_only_the_place_never_the_pinned_hash(tmp_path):
    k = Kit(tmp_path)
    k.net.routes["https://mirror.test/e.zip"] = b"not the engine"
    paths = EP.Paths(k.engine_dir())
    with pytest.raises(EP.EngineError) as e:
        EP.install_program(paths, k.pins, k.net.fetcher(), url_override="https://mirror.test/e.zip")
    assert e.value.code == "sha256_mismatch" and EP.installed_package(paths, k.pins) is None
    k.net.routes["https://mirror.test/e.zip"] = k.archive
    assert EP.install_program(paths, k.pins, k.net.fetcher(), url_override="https://mirror.test/e.zip")["tag"] == "bTEST"      # 内容对上了才算，换地址不换摘要


# ---------------------------------------------------------------- R1：安全解压
def _extract(tmp_path, archive, kind, files, **lim):
    arc = str(tmp_path / "arc")
    open(arc, "wb").write(archive)
    out = str(tmp_path / "out")
    os.mkdir(out, 0o700)
    EP.safe_extract(arc, kind, out, manifest_of(files) if isinstance(files, dict) else files, lim.get("max_files", 50), lim.get("max_total", 1 << 20))
    return out


@pytest.mark.parametrize("kind", ["zip", "tar.gz"])
def test_extract_good_package_sets_exec_by_manifest_and_private_perms(tmp_path, kind):
    files = package_files()
    out = _extract(tmp_path, make_zip(files) if kind == "zip" else make_targz(files), kind, files)
    b, lib = os.path.join(out, "build/bin/llama-server"), os.path.join(out, "build/bin/libfake.dylib")
    assert stat.S_IMODE(os.stat(b).st_mode) == 0o700 and stat.S_IMODE(os.stat(lib).st_mode) == 0o600
    assert os.readlink(os.path.join(out, "build/bin/libfake.1.dylib")) == "libfake.dylib" and EP.verify_tree(out, manifest_of(files)) == []


def _bad_zip(entries):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("names", [["/abs/llama-server"], ["../evil"], ["a/../../evil"], ["x", "x"], ["Readme", "README"]])
def test_extract_rejects_absolute_dotdot_duplicate_and_case_collision(tmp_path, names):
    entries = [(n, b"d") for n in names]
    man = [{"path": n, "type": "file", "sha256": sha(b"d"), "size": 1, "exec": False} for n in set(names)]
    with pytest.raises(EP.EngineError) as e:
        _extract(tmp_path, _bad_zip(entries), "zip", man)
    assert e.value.code == "archive_unsafe"
    assert not os.path.exists(str(tmp_path / "evil")) and not os.path.exists("/abs")


def test_extract_rejects_hardlink_fifo_device_in_tar(tmp_path):
    files = package_files()
    for typ in (tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
        ti = tarfile.TarInfo("build/bin/extra")
        ti.type = typ
        if typ == tarfile.LNKTYPE:
            ti.linkname = "build/bin/libfake.dylib"
        with pytest.raises(EP.EngineError) as e:
            _extract(tmp_path / "x" if False else tmp_path, make_targz(files, extra=[(ti, None)]), "tar.gz", files)
        assert e.value.code == "archive_unsafe"
        import shutil
        shutil.rmtree(str(tmp_path / "out"), ignore_errors=True)


def test_extract_rejects_symlink_chain_escape_and_write_through_symlink(tmp_path):
    files = {"a/ok": ("file", b"1", False), "link": ("symlink", "../../etc", False)}
    with pytest.raises(EP.EngineError):
        _extract(tmp_path, make_zip(files), "zip", files)
    import shutil
    shutil.rmtree(str(tmp_path / "out"), ignore_errors=True)
    files2 = {"l": ("symlink", "d", False), "l/x": ("file", b"1", False)}
    with pytest.raises(EP.EngineError) as e:
        _extract(tmp_path, make_zip(files2), "zip", files2)
    assert e.value.code == "archive_unsafe"
    shutil.rmtree(str(tmp_path / "out"), ignore_errors=True)
    files3 = {"l": ("symlink", "d", False)}                              # 目标和清单写的不一样
    man = [{"path": "l", "type": "symlink", "target": "other"}]
    with pytest.raises(EP.EngineError):
        _extract(tmp_path, make_zip(files3), "zip", man)


def test_extract_limits_count_and_actual_bytes_not_header_claims(tmp_path):
    files = {"f%d" % i: ("file", b"x" * 100, False) for i in range(5)}
    with pytest.raises(EP.EngineError):
        _extract(tmp_path, make_zip(files), "zip", files, max_files=3)
    import shutil
    shutil.rmtree(str(tmp_path / "out"), ignore_errors=True)
    with pytest.raises(EP.EngineError) as e:
        _extract(tmp_path, make_zip(files), "zip", files, max_total=250)
    assert e.value.code == "archive_unsafe"


def test_extract_rejects_file_not_in_manifest_and_wrong_content(tmp_path):
    files = package_files()
    arc = make_zip(dict(files, **{"build/bin/extra.dylib": ("file", b"x", False)}))
    with pytest.raises(EP.EngineError):
        _extract(tmp_path, arc, "zip", files)
    import shutil
    shutil.rmtree(str(tmp_path / "out"), ignore_errors=True)
    bad = dict(files)
    bad["build/bin/libfake.dylib"] = ("file", b"TAMPERED", False)
    with pytest.raises(EP.EngineError):
        _extract(tmp_path, make_zip(bad), "zip", files)


# ---------------------------------------------------------------- R1：整包核验（只改动态库、清单外文件、硬链接）
def test_changing_only_a_dylib_fails_package_verification_though_main_binary_is_unchanged(tmp_path):
    k = Kit(tmp_path)
    paths = k.install()
    pk = EP.installed_package(paths, k.pins)
    assert pk and EP.verify_tree(pk["dir"], k.manifest) == []
    lib = os.path.join(pk["dir"], "build/bin/libfake.dylib")
    os.chmod(lib, 0o600)
    open(lib, "ab").write(b"!")
    assert any(p.startswith("changed:") for p in EP.verify_tree(pk["dir"], k.manifest))
    assert EP.installed_package(paths, k.pins) is None


def test_extra_loadable_file_missing_file_changed_link_and_hardlink_are_all_caught(tmp_path):
    k = Kit(tmp_path)
    paths = k.install()
    d = EP.installed_package(paths, k.pins)["dir"]
    extra = os.path.join(d, "build/bin/libextra.dylib")
    open(extra, "wb").write(b"x")
    assert "extra:build/bin/libextra.dylib" in EP.verify_tree(d, k.manifest)
    os.unlink(extra)
    link = os.path.join(d, "build/bin/libfake.1.dylib")
    os.unlink(link)
    os.symlink("evil.dylib", link)
    assert any(p.startswith("link_changed") for p in EP.verify_tree(d, k.manifest))
    os.unlink(link)
    os.symlink("libfake.dylib", link)
    lib = os.path.join(d, "build/bin/libfake.dylib")
    os.link(lib, os.path.join(d, "hard"))
    probs = EP.verify_tree(d, k.manifest)
    assert any(p.startswith("hardlinked") for p in probs)


def test_install_program_end_to_end_second_call_downloads_nothing_and_staging_is_cleaned(tmp_path):
    k = Kit(tmp_path)
    paths = EP.Paths(k.engine_dir())
    pk = EP.install_program(paths, k.pins, k.net.fetcher(), free_bytes=10 ** 9)
    assert k.net.hits == [k.url] and os.access(pk["binary"], os.X_OK) and os.listdir(paths.staging) == []
    assert EP.read_receipt(paths)["package"]["tag"] == "bTEST"
    EP.install_program(paths, k.pins, k.net.fetcher())
    assert k.net.hits == [k.url]


def test_two_installs_at_once_do_not_share_a_staging_dir_and_both_end_with_one_good_package(tmp_path):
    k = Kit(tmp_path)
    paths = EP.Paths(k.engine_dir())
    errs = []

    def go():
        try:
            EP.install_program(paths, k.pins, k.net.fetcher())
        except Exception as e:                                                          # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == [] and EP.installed_package(paths, k.pins) is not None and os.listdir(paths.staging) == []


def test_disk_full_is_reported_before_downloading(tmp_path):
    k = Kit(tmp_path)
    with pytest.raises(EP.EngineError) as e:
        EP.install_program(EP.Paths(k.engine_dir()), k.pins, k.net.fetcher(), free_bytes=10)
    assert e.value.code == "disk_full" and k.net.hits == []


# ---------------------------------------------------------------- R1：复用本机的模型文件（只认 SHA-256，只读）
def test_reuse_by_sha256_in_fake_lmstudio_dir_and_nothing_is_modified(tmp_path):
    k = Kit(tmp_path)
    d = k.lmstudio_dir()
    f = os.path.join(d, "Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf")
    os.chmod(f, 0o644)
    before = (os.stat(f).st_mtime_ns, os.stat(f).st_mode, open(f, "rb").read(), sorted(os.listdir(os.path.dirname(f))))
    r = EP.locate_model(k.pins, dirs=[d])
    assert r["found"]["path"] == f and r["found"]["role"] == "reusable" and k.net.hits == []
    assert before == (os.stat(f).st_mtime_ns, os.stat(f).st_mode, open(f, "rb").read(), sorted(os.listdir(os.path.dirname(f))))


def test_same_name_and_same_size_but_different_content_is_not_reused(tmp_path):
    k = Kit(tmp_path)
    d = tmp_path / "lm" / "x"
    d.mkdir(parents=True)
    (d / "Qwen3-Embedding-0.6B-Q8_0.gguf").write_bytes(b"Z" * len(MODEL_BYTES))
    assert EP.locate_model(k.pins, dirs=[str(tmp_path / "lm")])["found"] is None


def test_only_files_of_pinned_size_are_hashed(tmp_path):
    k = Kit(tmp_path)
    d = tmp_path / "lm"
    d.mkdir()
    for i in range(3):
        (d / ("other%d.gguf" % i)).write_bytes(b"q" * (7 + i))
    (d / "note.txt").write_bytes(MODEL_BYTES)
    calls = []
    EP.locate_model(k.pins, dirs=[str(d)], hasher=lambda p: calls.append(p) or EP.sha256_file(p))
    assert calls == []


def test_ollama_blob_by_name_and_content_and_wrong_content_under_right_name_is_refused(tmp_path):
    k = Kit(tmp_path)
    d = tmp_path / "ollama" / "blobs"
    d.mkdir(parents=True)
    (d / ("sha256-" + sha(MODEL_BYTES))).write_bytes(MODEL_BYTES)
    assert EP.locate_model(k.pins, dirs=[str(d)])["found"]["sha256"] == sha(MODEL_BYTES)
    d2 = tmp_path / "ollama2" / "blobs"
    d2.mkdir(parents=True)
    (d2 / ("sha256-" + sha(MODEL_BYTES))).write_bytes(b"Z" * 50)               # 名字说是，内容不是
    assert EP.locate_model(k.pins, dirs=[str(d2)])["found"] is None


def test_unreadable_dir_is_reported_not_treated_as_empty(tmp_path):
    k = Kit(tmp_path)
    d = tmp_path / "locked"
    d.mkdir()
    os.chmod(d, 0)
    try:
        r = EP.locate_model(k.pins, dirs=[str(d)])
    finally:
        os.chmod(d, 0o700)
    assert r["found"] is None and r["unreadable"] == ["locked"]


def test_model_file_flag_with_wrong_sha256_is_refused(tmp_path):
    k = Kit(tmp_path)
    f = tmp_path / "mine.gguf"
    f.write_bytes(b"something else")
    with pytest.raises(EP.EngineError) as e:
        EP.locate_model(k.pins, extra_file=str(f))
    assert e.value.code == "model_file_sha_mismatch"
    f.write_bytes(MODEL_BYTES)
    assert EP.locate_model(k.pins, extra_file=str(f))["found"]["artifact_id"] == "m@tested"


def test_same_size_same_mtime_different_content_is_caught_by_full_hash_at_cold_start(tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(MODEL_BYTES)
    st = os.stat(f)
    assert EP.verify_model_file(str(f), sha(MODEL_BYTES)) is None
    f.write_bytes(b"X" * len(MODEL_BYTES))
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))                       # 大小、修改时间都和原来一样
    assert EP.verify_model_file(str(f), sha(MODEL_BYTES)) == "model_file_changed"
    os.unlink(f)
    assert EP.verify_model_file(str(f), sha(MODEL_BYTES)) == "model_file_missing"


def test_download_model_second_source_used_when_first_fails_and_wrong_hash_deleted(tmp_path):
    k = Kit(tmp_path)
    paths = EP.Paths(k.engine_dir())
    k.net.routes[k.model_url] = b"corrupted"
    d = EP.download_model(paths, k.pins, k.net.fetcher())
    assert k.net.hits == [k.model_url, k.model_url2] and open(d["path"], "rb").read() == OFFICIAL_BYTES and d["owned"] and os.listdir(paths.staging) == []
    k.net.routes[k.model_url] = b"bad"
    k.net.routes[k.model_url2] = b"bad2"
    paths2 = EP.Paths(k.engine_dir("e2"))
    with pytest.raises(EP.EngineError) as e:
        EP.download_model(paths2, k.pins, k.net.fetcher())
    assert e.value.code == "sha256_mismatch" and os.listdir(paths2.models) == []


# ---------------------------------------------------------------- R6：标签、身份、配方
def test_tested_label_is_per_digest_one_file_passing_does_not_make_the_other_tested(tmp_path):
    k = Kit(tmp_path)
    a, b = sha(MODEL_BYTES), sha(OFFICIAL_BYTES)
    saved = dict(CT.CONSISTENCY)
    CT.CONSISTENCY.clear()
    try:
        assert EP.consistency_label(a, k.pins) == EP.consistency_label(b, k.pins) == "一致性还没在真机上核"
        key = CT.consistency_key(a, k.pins["engine"]["tag"], EP.package_digest(k.pins["engine"]["assets"]["darwin-arm64"]["manifest"]), EP.recipe_digest())
        CT.CONSISTENCY[key] = {"strong": True}
        assert "测过" in EP.consistency_label(a, k.pins) and "测过" not in EP.consistency_label(b, k.pins)
    finally:
        CT.CONSISTENCY.clear()
        CT.CONSISTENCY.update(saved)
    assert "同一个模型文件" in EP.model_wording(a, k.pins) and "同一个模型文件" not in EP.model_wording(b, k.pins) and "不是同一个" in EP.model_wording(b, k.pins)


def test_recipe_digest_covers_every_launch_arg_and_text_processing():
    base = EP.recipe_digest()
    for i, (n, frag, d) in enumerate(CT.ENGINE_ARGS):
        if n in ("model", "port", "alias", "api_key_file"):
            continue
        mod = [(n2, (list(f2) + ["x"]) if i2 == i else f2, d2) for i2, (n2, f2, d2) in enumerate(CT.ENGINE_ARGS)]
        assert EP.recipe_digest(mod) != base, n
    old = dict(CT.RECIPE_TEXT)
    try:
        CT.RECIPE_TEXT["prefix"] = "query: "
        assert EP.recipe_digest() != base
    finally:
        CT.RECIPE_TEXT.clear()
        CT.RECIPE_TEXT.update(old)


def test_managed_identity_has_ten_fields_strong_and_every_ingredient_changes_the_fingerprint():
    ident = EP.build_identity("bT", "p" * 64, "r" * 64, "m" * 64, 1024, "l2")
    assert set(ident) == set(VX.IDENTITY_FIELDS) and VX.identity_fp(ident) and ident["context_length"] == 2048 and "pooling=last" in ident["provider"]
    fp = VX.identity_fp(ident)
    for args in (("bT2", "p" * 64, "r" * 64, "m" * 64), ("bT", "q" * 64, "r" * 64, "m" * 64), ("bT", "p" * 64, "s" * 64, "m" * 64), ("bT", "p" * 64, "r" * 64, "n" * 64)):
        assert VX.identity_fp(EP.build_identity(*args, 1024, "l2")) != fp
    lms = dict(ident, provider="lmstudio")
    assert VX.identity_fp(lms) != fp


def test_engine_ctx_is_2048_and_args_have_no_verbose_flags_and_disable_webui_and_slots_and_use_key_file():
    flat = [x for _n, f, _d in CT.ENGINE_ARGS for x in f]
    assert CT.ENGINE_CTX == 2048 and flat[flat.index("-c") + 1] == "2048" and ["--pooling", "last"] == flat[flat.index("--pooling"):flat.index("--pooling") + 2]
    assert "--no-ui" in flat and "--no-webui" not in flat and "--no-slots" in flat and "--api-key-file" in flat and "--api-key" not in flat
    assert flat[flat.index("--host") + 1] == "127.0.0.1"
    assert not any(x in ("-v", "--verbose", "--log-verbosity", "--log-file", "--log-disable") for x in flat)
