"""自带向量引擎：钉住表填实（llama.cpp b11701，2026-11-11 主会话在真机上核过）。表里的事实、下载的跳转检查、启动参数、按真包结构仿的 tar.gz 解压（好的和坏的）、三个报错正则用真报错原文、向导文字。
全部断网：下载走进程内的假响应，解压用自造的小文件按真包的结构（顶层目录、42 个文件、18 个链接、可执行位）仿。"""
import copy
import hashlib
import io
import json
import os
import stat
import tarfile

import pytest

from helpers_engine import FakeNet, FakeResp, make_targz, sha
from yuanwei import catalog as CT
from yuanwei import enginepkg as EP
from yuanwei import enginerun as ER
from yuanwei import wizard as WZ

A = CT.PINS["engine"]["assets"]["darwin-arm64"]
FACT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "research", "personamem-pilot", "human-memory-comparison-2026-09-28", "圆桌", "产品打包", "真机事实-自带引擎-b11701-2026-11-11.json"))


# ================================================================== 表里的事实
def test_real_pins_are_verified_and_pass_the_gate():
    assert EP.pins_problems() == [] and EP.pins_problems(CT.PINS) == []
    e = CT.PINS["engine"]
    assert e["verified"] is True and e["tag"] == "b11701" and e["owner"] == "ggml-org" and e["repo"] == "ggml-org/llama.cpp" and e["license"] == "MIT"
    assert A["url"] == "https://github.com/ggml-org/llama.cpp/releases/download/b11701/llama-b11701-bin-macos-arm64.tar.gz" and A["kind"] == "tar.gz"
    assert A["size"] == 12249449 and A["sha256"] == "e4219b1442dcf0b68e860ea6f06d417163326391e8514b04019080f2444ff47c"
    assert A["top_dir"] == "llama-b11701" and A["binary"] == "llama-b11701/llama-server"
    assert "GitHub 发布记录" in e["source_note"] and "2026-11-11" in e["source_note"] and "SHA-256" in e["source_note"]
    assert not any("真机核" in str(v) for v in (e["source_note"], e["release_page"]))


def test_manifest_shape_sizes_links_and_exec_bits_match_the_real_package():
    m = A["manifest"]
    files = [x for x in m if x["type"] == "file"]
    links = [x for x in m if x["type"] == "symlink"]
    assert len(files) == 42 and len(links) == 18 and len({x["path"] for x in m}) == 60
    assert sum(x["size"] for x in files) == 32421654 and max(x["size"] for x in files) == 9250520
    assert all(x["path"].startswith("llama-b11701/") and "/" not in x["path"][len("llama-b11701/"):] and ".." not in x["path"] for x in m)
    assert all(len(x["sha256"]) == 64 and isinstance(x["exec"], bool) for x in files)
    by = {x["path"]: x for x in m}
    b = by[A["binary"]]
    assert b["type"] == "file" and b["exec"] is True and b["size"] == 49984 and b["sha256"] == "981b460a278e9a0c880cef32d6fab42785a119084456b16845304cf38c32cf69"
    assert by["llama-b11701/LICENSE"]["exec"] is False
    for x in links:                                                                    # 链接全在包内：目标是同一目录里的一个条目，没有绝对路径、没有 ..
        t = x["target"]
        assert not t.startswith("/") and ".." not in t and "/" not in t
        assert "llama-b11701/" + t in by, x
    assert by["llama-b11701/libllama.0.dylib"]["target"] == "libllama.0.7.0.dylib" if "llama-b11701/libllama.0.dylib" in by else True


def test_limits_have_a_documented_margin_over_the_real_values():
    assert A["max_files"] >= 61 + 10 and A["max_files"] <= 100                          # 实际 61 个条目（1 目录＋42 文件＋18 链接）；留 80
    assert A["max_total_bytes"] >= int(32421654 * 1.2) and A["max_total_bytes"] <= 32421654 * 2   # 实际 32,421,654；留 40 MiB
    text = open(os.path.join(os.path.dirname(CT.__file__), "catalog.py"), encoding="utf-8").read()
    assert "条目数留 80" in text and "40 MiB" in text                                    # 余量和依据写在表旁边


@pytest.mark.skipif(not os.path.exists(FACT), reason="真机事实 json 不在这台电脑上")
def test_table_equals_the_hand_checked_facts_file_entry_by_entry():
    f = json.load(open(FACT, encoding="utf-8"))["engine"]
    assert (A["url"], A["size"], A["sha256"], A["top_dir"], A["binary"]) == (f["url"], f["size"], f["sha256"], f["top_dir"], f["binary"])
    want = []
    for x in f["manifest"]:
        want.append({"path": x["path"], "type": "file", "size": x["size"], "sha256": x["sha256"], "exec": x["executable"]} if x["type"] == "file" else {"path": x["path"], "type": "symlink", "target": x["target"]})
    assert sorted(A["manifest"], key=lambda e: e["path"]) == sorted(want, key=lambda e: e["path"])


def test_model_pins_both_sources_the_same_file_and_the_wording_says_so():
    ms = {m["role"]: m for m in CT.PINS["models"]}
    for m in ms.values():
        assert m["sha256"] == "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439" and m["size"] == 639150592 and m["verified"] is True and m["file_name"] == "Qwen3-Embedding-0.6B-Q8_0.gguf"
    assert ms["reusable"]["sha256"] == CT.TESTED_EMBED_IDENTITY["model_file_sha256"]
    urls = [s["url"] for s in ms["downloadable"]["sources"]]
    assert urls == ["https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf", "https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/master/Qwen3-Embedding-0.6B-Q8_0.gguf"]
    assert EP.model_wording(ms["reusable"]["sha256"]) == "同一个模型文件（SHA-256 和测过的相同）"                 # 官方文件就是测过的那个：不再说「不是同一个文件」
    assert EP.acceptable_model_shas()[ms["reusable"]["sha256"]]["role"] == "reusable"
    assert EP.model_wording("f" * 64).startswith("官方发布的模型文件")                                          # 摘要对不上的才是别的文件（也进不了 acceptable）


def test_a_local_copy_of_the_tested_model_is_found_and_reused_by_size_and_sha(tmp_path):
    d = tmp_path / "lm" / "Qwen" / "x"
    d.mkdir(parents=True)
    f = d / "Qwen3-Embedding-0.6B-Q8_0.gguf"
    with open(f, "wb") as fh:
        fh.truncate(639150592)                                                          # 稀疏文件：大小对、不占磁盘；摘要用假的哈希函数给
    r = EP.locate_model(dirs=[str(tmp_path / "lm")], hasher=lambda p: "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439")
    assert r["found"]["path"] == str(f) and r["found"]["role"] == "reusable" and r["found"]["artifact_id"].endswith("@tested")
    g = d / "other.gguf"
    with open(g, "wb") as fh:
        fh.truncate(1000)
    f.unlink()
    assert EP.locate_model(dirs=[str(tmp_path / "lm")], hasher=lambda p: "0" * 64)["found"] is None


# ================================================================== 下载的跳转检查（Hugging Face 会 302 到 us.aws.cdn.hf.co 这类 CDN 主机）
def test_redirect_check_only_requires_https_so_the_cdn_hosts_are_not_blocked():
    import urllib.request
    h = EP.HTTPSOnlyRedirect()
    req = urllib.request.Request("https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf")
    for host in ("us.aws.cdn.hf.co", "release-assets.githubusercontent.com", "cdn-lfs.hf.co"):
        nr = h.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://%s/x/y?sig=1" % host)
        assert nr is not None and nr.full_url.startswith("https://" + host)
    with pytest.raises(EP.EngineError) as e:
        h.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "http://us.aws.cdn.hf.co/x")      # 跳到非 https：拒
    assert e.value.code == "not_https"


def test_fetcher_follows_a_cdn_final_url_and_refuses_a_non_https_final_url(tmp_path):
    body = b"x" * 1000
    net = FakeNet({"https://huggingface.co/m": lambda: FakeResp(body, "https://us.aws.cdn.hf.co/blob")})
    assert EP.Fetcher(opener=net).fetch("https://huggingface.co/m", str(tmp_path / "ok"), sha(body), 5000, 10) == 1000
    net2 = FakeNet({"https://huggingface.co/m": lambda: FakeResp(body, "http://evil.example/blob")})
    with pytest.raises(EP.EngineError) as e:
        EP.Fetcher(opener=net2).fetch("https://huggingface.co/m", str(tmp_path / "bad"), sha(body), 5000, 10)
    assert e.value.code == "not_https" and not os.path.exists(tmp_path / "bad")


# ================================================================== 启动参数
def test_engine_args_use_no_ui_keep_the_rest_and_say_checked_on_b11701():
    flat = [x for _n, f, _d in CT.ENGINE_ARGS for x in f]
    assert "--no-ui" in flat and "--no-webui" not in flat
    assert [n for n, _f, _d in CT.ENGINE_ARGS] == ["model", "embedding", "pooling", "host", "port", "ctx", "batch", "ubatch", "threads", "parallel", "alias", "gpu", "no_ui", "no_slots", "api_key_file"]
    assert flat == ["-m", "{model_path}", "--embedding", "--pooling", "last", "--host", "127.0.0.1", "--port", "{port}", "-c", "2048", "-b", "2048", "-ub", "2048", "-t", "4", "-np", "1",
                    "--alias", "{alias}", "-ngl", "99", "--no-ui", "--no-slots", "--api-key-file", "{keyfile}"]
    assert not any("真机核" in d for _n, _f, d in CT.ENGINE_ARGS)
    assert "--port" in flat and flat[flat.index("--port") + 1] == "{port}" and "0" != flat[flat.index("--port") + 1]       # 端口自己挑，不用 --port 0（理由见交回报告）
    src = open(os.path.join(os.path.dirname(CT.__file__), "catalog.py"), encoding="utf-8").read()
    assert "【真机核" not in src


# ================================================================== 解压：按真包的结构仿的 tar.gz
TOP = "llama-b11701"


def synthetic_package():
    """和真包同样的结构：顶层目录、42 个文件（可执行位照真清单）、18 个包内链接；内容是自造的小文件。返回 (files, manifest)。"""
    files, man = {}, []
    for x in A["manifest"]:
        if x["type"] == "file":
            content = ("fake:" + x["path"]).encode("utf-8")
            files[x["path"]] = ("file", content, x["exec"])
            man.append({"path": x["path"], "type": "file", "size": len(content), "sha256": sha(content), "exec": x["exec"]})
        else:
            files[x["path"]] = ("symlink", x["target"], False)
            man.append({"path": x["path"], "type": "symlink", "target": x["target"]})
    return files, man


def with_top_dir():
    ti = tarfile.TarInfo(TOP)
    ti.type, ti.mode = tarfile.DIRTYPE, 0o755
    return [(ti, None)]


def extract(tmp_path, files, manifest, name="x", **kw):
    arc = tmp_path / ("a-" + name + ".tar.gz")
    arc.write_bytes(make_targz(files, extra=kw.pop("extra", with_top_dir())))
    dest = tmp_path / name
    dest.mkdir()
    EP.safe_extract(str(arc), "tar.gz", str(dest), manifest, kw.get("max_files", A["max_files"]), kw.get("max_total_bytes", A["max_total_bytes"]))
    return dest


def test_a_package_with_the_real_structure_extracts_with_links_and_exec_bits_as_the_table_says(tmp_path):
    files, man = synthetic_package()
    dest = extract(tmp_path, files, man)
    assert EP.verify_tree(str(dest), man) == []
    n_links = 0
    for x in man:
        p = dest / x["path"]
        if x["type"] == "symlink":
            assert os.path.islink(p) and os.readlink(p) == x["target"]
            n_links += 1
        else:
            mode = stat.S_IMODE(os.lstat(p).st_mode)
            assert mode == (0o700 if x["exec"] else 0o600), (x["path"], oct(mode))               # 可执行位照清单设，不照包里的
    assert n_links == 18 and (dest / A["binary"]).stat().st_mode & 0o100


def test_the_whole_install_path_works_on_the_real_structure(tmp_path):
    files, man = synthetic_package()
    arc = make_targz(files, extra=with_top_dir())
    pins = copy.deepcopy(CT.PINS)
    a = pins["engine"]["assets"]["darwin-arm64"]
    a.update(manifest=man, sha256=sha(arc), size=len(arc), url="https://example.test/llama.tar.gz")
    pk = EP.install_program(EP.Paths(str(tmp_path / "e")), pins, FakeNet({"https://example.test/llama.tar.gz": arc}).fetcher())
    assert pk["binary"].endswith("llama-b11701/llama-server") and os.access(pk["binary"], os.X_OK) and EP.verify_tree(pk["dir"], man) == []


@pytest.mark.parametrize("bad,why", [
    ("link_outside", "link_target"), ("link_other_target", "link_target"), ("link_absolute", "link_target"),
    ("extra_file", "not_in_manifest"), ("missing_file", "missing"), ("wrong_size", "changed"), ("wrong_sha", "changed"),
    ("hardlink", "special"), ("too_many", "too_many"), ("dotdot_name", "dotdot")])
def test_bad_packages_are_refused_and_nothing_is_left_usable(tmp_path, bad, why):
    files, man = synthetic_package()
    link = next(k for k, v in files.items() if v[0] == "symlink")
    extra = with_top_dir()
    kw = {}
    if bad == "link_outside":
        files[link] = ("symlink", "../../etc/passwd", False)                                 # 链接指到包外（清单里也这么写都不行）
        man = [dict(e, target="../../etc/passwd") if e["path"] == link else e for e in man]
    elif bad == "link_other_target":
        files[link] = ("symlink", "libggml-cpu.0.27.0.dylib", False)                         # 包里的链接目标和清单不一样
    elif bad == "link_absolute":
        files[link] = ("symlink", "/usr/lib/libSystem.B.dylib", False)
        man = [dict(e, target="/usr/lib/libSystem.B.dylib") if e["path"] == link else e for e in man]
    elif bad == "extra_file":
        files[TOP + "/extra-file"] = ("file", b"surprise", False)
    elif bad == "missing_file":
        victim = next(k for k, v in files.items() if v[0] == "file")
        del files[victim]
    elif bad == "wrong_size":
        victim = next(k for k, v in files.items() if v[0] == "file")
        man = [dict(e, size=e["size"] + 1) if e["path"] == victim else e for e in man]
    elif bad == "wrong_sha":
        victim = next(k for k, v in files.items() if v[0] == "file")
        man = [dict(e, sha256="0" * 64) if e["path"] == victim else e for e in man]
    elif bad == "hardlink":
        ti = tarfile.TarInfo(TOP + "/hard")
        ti.type, ti.linkname = tarfile.LNKTYPE, A["binary"]
        extra = extra + [(ti, None)]
    elif bad == "too_many":
        kw["max_files"] = 60
    elif bad == "dotdot_name":
        files[TOP + "/../escape"] = ("file", b"x", False)
    with pytest.raises(EP.EngineError) as e:
        extract(tmp_path, files, man, extra=extra, **kw)
    assert e.value.code == "archive_unsafe" and why in (e.value.detail or "")
    assert not (tmp_path / "escape").exists()


def test_a_package_whose_archive_digest_is_wrong_is_never_extracted(tmp_path):
    files, man = synthetic_package()
    arc = make_targz(files, extra=with_top_dir())
    pins = copy.deepcopy(CT.PINS)
    pins["engine"]["assets"]["darwin-arm64"].update(manifest=man, sha256="1" * 64, size=len(arc), url="https://example.test/l.tar.gz")
    paths = EP.Paths(str(tmp_path / "e"))
    with pytest.raises(EP.EngineError) as e:
        EP.install_program(paths, pins, FakeNet({"https://example.test/l.tar.gz": arc}).fetcher())
    assert e.value.code == "sha256_mismatch" and EP.installed_package(paths, pins) is None


# ================================================================== 三个报错正则：用真机上的报错原文当样本（b11701）
ERR_ARGS = b"error: invalid argument: --no-such-flag\n"
ERR_PORT = b"E srv start: couldn't bind HTTP server socket, hostname: 127.0.0.1, port: 54321\nexiting due to HTTP server error\n"
ERR_MODEL = (b"E gguf_init_from_file: failed to open GGUF file '/nonexistent/Qwen3-Embedding-0.6B-Q8_0.gguf'\n"
             b"E llama_model_load: error loading model: llama_model_loader: failed to load model from /nonexistent/Qwen3-Embedding-0.6B-Q8_0.gguf\n")


def flags_of(raw):
    d = ER._Drain(io.BytesIO(raw))
    d.run()
    return d.flags


def test_args_rejected_regex_recognises_the_real_message_and_nothing_else_of_the_three():
    assert ER.RE_ARGS_REJECTED.search(ERR_ARGS) and not ER.RE_PORT_BUSY.search(ERR_ARGS) and not ER.RE_MODEL_LOAD.search(ERR_ARGS)
    assert flags_of(ERR_ARGS) == {"args_rejected"}


def test_port_busy_regex_recognises_the_real_message_and_nothing_else_of_the_three():
    assert ER.RE_PORT_BUSY.search(ERR_PORT.splitlines()[0]) and not ER.RE_ARGS_REJECTED.search(ERR_PORT) and not ER.RE_MODEL_LOAD.search(ERR_PORT)
    assert flags_of(ERR_PORT) == {"port_in_use"}


def test_model_load_regex_recognises_the_real_message_and_nothing_else_of_the_three():
    assert ER.RE_MODEL_LOAD.search(ERR_MODEL.splitlines()[1]) and not ER.RE_ARGS_REJECTED.search(ERR_MODEL) and not ER.RE_PORT_BUSY.search(ERR_MODEL)
    assert flags_of(ERR_MODEL) == {"model_load_failed"}
    assert ER.RE_MODEL_LOAD.search(b"E llama_model_load: error loading model: llama_model_loader: x")                          # 真报错那一行的两半，各自也认得
    assert ER.RE_MODEL_LOAD.search(b"llama_model_loader: failed to load model from /x/y.gguf")
    assert flags_of(b"srv  load_model: loading\nsrv  listening on http://127.0.0.1:50000\n") == set()          # 正常启动的日志不触发任何一类


# ================================================================== 向导和状态的文字
def test_wizard_shows_real_sizes_and_no_longer_says_the_pins_are_unchecked(tmp_path):
    from helpers_engine import Kit  # noqa: F401
    assert WZ._mb(A["size"]) == "约 12 MB"
    dm = [m for m in CT.PINS["models"] if m["role"] == "downloadable"][0]
    assert WZ._mb(dm["size"]) == "约 610 MB"
    vp = {"needs_program_download": True, "needs_model_download": True, "program_size": A["size"], "model_size": dm["size"], "engine_dir": "/e"}
    t = WZ.engine_consent_text(vp)
    assert "约 12 MB" in t and "约 610 MB" in t and "SHA-256" in t




def test_interactive_menu_with_the_real_pins_lists_the_real_sizes_and_does_not_block_the_builtin_engine(tmp_path):
    from helpers_wizard import FakeIO, make_home
    from test_engine_integration import BASE, W, _wizard_answers
    from yuanwei import cli
    x = W(tmp_path, model_dirs=False)
    x.sys.pins = None                                                                                 # 用真的钉住表（现在是核过的）；下载器仍是假的，没有同意下载，什么都不会发出去
    home = make_home(tmp_path, "h5")
    io = FakeIO(answers=_wizard_answers(["y", "1", "n"]))
    rc = cli._setup_main(["setup", "--hermes-home", home, "--engine-dir", x.engine_dir] + BASE, io=io, sys_=x.sys)
    t = io.text
    assert rc == 0 and x.kit.net.hits == [] and not os.path.exists(x.engine_dir)
    assert "现在选不了" not in t and "还没核过" not in t and "还没准备好" not in t
    assert "程序约 12 MB" in t and "模型约 610 MB" in t and "不下载，向量就先不开" in t
    assert "1) 自带（推荐）" in t


def test_non_interactive_plan_with_the_real_pins_has_no_unchecked_warning_and_gives_the_checked_not_strong_label(tmp_path, capsys):
    from test_engine_integration import W
    x = W(tmp_path)
    x.sys.pins = None
    rc = x.run()
    out = json.loads(capsys.readouterr().out)
    txt = "\n".join(out["plan"]["lines"])
    assert rc == 0 and "还没核过" not in txt and "现在装不上" not in txt and "约 12 MB" in txt and "约 610 MB" in txt
    line = txt.split("一致性：")[1].splitlines()[0]
    assert line.startswith("同一个模型文件；向量和 LM Studio 算的几乎一样") and "测过" not in line and "一致" not in line.replace("一样", "") and x.kit.net.hits == []
