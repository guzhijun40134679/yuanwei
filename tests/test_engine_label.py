"""自带引擎的一致性结果写进标签（2026-11-11 晚）：正式门槛一没过（抽样 1,000 条里 2 条低于 0.999，最小 0.998596；全量 11,588 条里 8 条低于 0.999，最小 0.997689，没有低于 0.995 的）。
用户定：不改规矩，自带程序照常给用，说明老实写；「测过」只给 LM Studio。新的一档：核过但严格标准没过——不写「测过」、不写「一致」。"""
import copy
import json
import os

import pytest

from yuanwei import catalog as CT
from yuanwei import enginepkg as EP
from yuanwei import hermes_setup as HS

SHA = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
A = CT.PINS["engine"]["assets"]["darwin-arm64"]
KEY = CT.consistency_key(SHA, "b11701", "6822d22ee55c", "5ffb9ffa5eac")
TEXT = "同一个模型文件；向量和 LM Studio 算的几乎一样（全量 11,588 句里 8 句略低于我们定的线，最低 0.9977），没过我们自己定的严格标准，所以不能当成和 LM Studio 那套完全一样"


def never_claims(text):
    assert "测过" not in text and "一致" not in text.replace("一样", ""), text


# ================================================================== 表和键
def test_the_consistency_table_has_exactly_the_checked_record_and_it_is_not_strong():
    assert list(CT.CONSISTENCY) == [KEY]
    r = CT.CONSISTENCY[KEY]
    assert r["strong"] is False and r["at"] == "2026-11-11" and r["report"] == "一致性核对记录-自带引擎-2026-11-11.md" and r["engine"] == "b11701" and r["pkg"] == "6822d22ee55c"
    assert (r["sample_n"], r["sample_min"], r["sample_below_0999"], r["full_n"], r["full_min"], r["full_below_0999"]) == (1000, 0.998596, 2, 11588, 0.997689, 8)


def test_the_key_matches_what_the_installed_package_and_recipe_really_are():
    assert EP.package_digest(A["manifest"])[:12] == "6822d22ee55c" and EP.recipe_digest()[:12] == "5ffb9ffa5eac" and CT.PINS["engine"]["tag"] == "b11701"
    st = EP.consistency_state(SHA)
    assert st["level"] == "checked_not_strong" and st["text"] == TEXT


def test_label_text_has_the_numbers_from_the_record_and_never_says_tested_or_consistent():
    t = EP.consistency_label(SHA)
    assert t == TEXT and "11,588" in t and "8 句" in t and "0.9977" in t and "没过我们自己定的严格标准" in t
    never_claims(t)


@pytest.mark.parametrize("what", ["engine_tag", "pkg", "recipe", "model"])
def test_a_different_engine_version_package_recipe_or_model_goes_back_to_unchecked(what):
    pins = copy.deepcopy(CT.PINS)
    args = None
    sha = SHA
    if what == "engine_tag":
        pins["engine"]["tag"] = "b11702"                                                              # 换了版本：这条结果不能自动算到新版本头上
    elif what == "pkg":
        pins["engine"]["assets"]["darwin-arm64"]["manifest"][0] = dict(pins["engine"]["assets"]["darwin-arm64"]["manifest"][0], sha256="0" * 64)
    elif what == "recipe":
        args = list(CT.ENGINE_ARGS) + [("extra", ["--flag"], "x")]
    else:
        sha = "f" * 64
    st = EP.consistency_state(sha, pins, args)
    assert st["level"] == "unchecked" and st["text"] == "一致性还没在真机上核" and st["record"] is None


def test_unverified_or_empty_pins_are_unchecked_not_an_error():
    assert EP.consistency_state(None)["level"] == "unchecked" and EP.consistency_state(SHA, {"engine": {}, "models": []})["level"] == "unchecked"


def test_only_a_strong_record_may_say_tested(monkeypatch):
    rec = dict(CT.CONSISTENCY[KEY], strong=True)
    monkeypatch.setitem(CT.CONSISTENCY, KEY, rec)
    assert EP.consistency_state(SHA)["level"] == "strong" and EP.consistency_label(SHA) == "测过（和 LM Studio 的向量一致）"


# ================================================================== 编码身份对表（向导核验后一行、status 的 vectors）
IDENT = {"provider": "llama.cpp-server;pooling=last", "model": "Qwen3-Embedding-0.6B-Q8_0", "model_file_sha256": SHA, "runtime_version": "llama.cpp-server@b11701;pkg=6822d22ee55c1ef6;recipe=5ffb9ffa5eac2222",
         "context_length": 2048, "dimension": 1024, "normalization": "client-float64-L2", "prefix": "none", "template": "none", "text_processing": "verbatim"}


def test_compare_to_tested_gives_the_new_tier_for_this_engine_and_stays_untested_for_other_runtimes():
    r = CT.compare_to_tested(IDENT)
    assert r["level"] == "checked_not_strong" and r["text"] == TEXT and r["differs"] == []
    never_claims(r["text"])
    assert CT.compare_to_tested(dict(IDENT, runtime_version="llama.cpp-server@b11702;pkg=6822d22ee55c1ef6;recipe=5ffb9ffa5eac2222"))["level"] == "unchecked"
    assert CT.compare_to_tested(dict(IDENT, runtime_version="garbage"))["level"] == "unchecked"
    assert CT.compare_to_tested(dict(IDENT, provider="ollama"))["level"] == "untested"                # 别的运行方式：照旧「没测过」
    lm = dict(CT.TESTED_EMBED_IDENTITY)
    assert CT.compare_to_tested(lm)["level"] == "tested"                                              # LM Studio 完全一样才是测过，没变


# ================================================================== 预设「推荐」的标签
def test_recommended_preset_is_no_longer_labelled_tested_as_a_whole():
    p = {x["id"]: x for x in CT.PRESETS}["recommended"]
    assert p["vectors"] == "auto" and p["label_text"] == "整理测过；向量：LM Studio 是测过的那一套，自带程序和它几乎一样、没过我们自己定的严格标准"
    assert "测过" in p["label_text"] and "一致" not in p["label_text"].replace("一样", "")
    assert {x["id"]: x for x in CT.PRESETS}["light"]["label_text"].startswith("整理测过")


def test_auto_resolves_to_the_builtin_engine_on_apple_silicon_so_the_label_must_be_split(tmp_path):
    from helpers_wizard import FakeHTTP, FakeSystem, make_home
    from yuanwei import wizard as WZ
    from types import SimpleNamespace
    s = FakeSystem(http=FakeHTTP("custom"), commands={}, env={}, platform={"system": "Darwin", "machine": "arm64", "arm64_hardware": True})
    a = SimpleNamespace(preset="recommended", provider=None, provider_url=None, provider_model=None, provider_param=[], api_key_env=None, vectors="auto", download_model=False,
                        allow_remote_embedding=False, install_numpy=False, engine_dir=None, engine_url=None, model_url=None, model_file=None, host="hermes", host_flag="hermes",
                        embedding_model=None, embedding_url=None)
    ch = WZ.resolve_args(a, s)
    assert ch["vectors"]["mode"] == "llamacpp"                                                         # auto＝支持就自带
    s2 = FakeSystem(http=FakeHTTP("custom"), commands={}, env={}, platform={"system": "Linux", "machine": "x86_64", "arm64_hardware": False})
    with pytest.raises(Exception):
        WZ.resolve_args(a, s2)                                                                         # 不支持：报错，不悄悄换成别的


def test_plan_and_wizard_headline_for_the_recommended_preset_split_the_labels(tmp_path, capsys):
    from helpers_wizard import FakeIO, make_home
    from test_engine_integration import BASE, W
    from yuanwei import cli
    x = W(tmp_path)
    x.sys.pins = None
    assert x.run() == 0
    txt = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    head = [l for l in txt.splitlines() if l.startswith(" 预设")][0]
    assert "〔整理测过；向量：LM Studio 是测过的那一套，自带程序和它几乎一样、没过我们自己定的严格标准〕" in head and "〔测过〕" not in head
    assert "一致性：" + TEXT in txt
    (tmp_path / "w2").mkdir()
    x2 = W(tmp_path / "w2")
    io = FakeIO(["1", "q"])
    cli._setup_main(["setup", "--hermes-home", make_home(tmp_path / "w2", "hh2"), "--engine-dir", x2.engine_dir] + BASE, io=io, sys_=x2.sys)
    menu = [l for l in io.out if l.strip().startswith("1) 推荐")][0]
    assert "整理测过；向量：LM Studio 是测过的那一套" in menu and "〔测过〕" not in menu


# ================================================================== 向导第 6 步的两项
def test_vector_step_keeps_recommending_the_builtin_engine_with_the_label_line_and_marks_lm_studio_as_the_tested_one(tmp_path):
    from helpers_wizard import FakeIO, make_home
    from test_engine_integration import BASE, W, _wizard_answers
    from yuanwei import cli
    x = W(tmp_path, model_dirs=False)
    x.sys.pins = None
    io = FakeIO(answers=_wizard_answers(["y", "1", "n"]))
    cli._setup_main(["setup", "--hermes-home", make_home(tmp_path, "h6"), "--engine-dir", x.engine_dir] + BASE, io=io, sys_=x.sys)
    t = io.text
    assert "1) 自带（推荐）" in t and "不用你装别的软件" in t
    assert "自带程序算的向量：" + TEXT in t
    assert "2) 用你电脑上已有的 LM Studio 或 Ollama（LM Studio：测过的就是它，150 次对话用的就是那一套）" in t
    assert t.index("1) 自带（推荐）") < t.index("自带程序算的向量") < t.index("2) 用你电脑上已有的 LM Studio")


# ================================================================== status 和 vectors status 的 engine 段
def test_vectors_status_engine_segment_carries_the_new_label_and_level(tmp_path):
    from yuanwei import enginerun as ER
    from test_engine_integration import W
    x = W(tmp_path)
    x.sys.pins = None
    x.paths.ensure_dirs()
    EP.write_receipt(x.paths, model={"path": "/nonexistent/m.gguf", "sha256": SHA, "source": "reused"})
    st = ER.engine_status(x.paths, None)
    assert st["installed"]["model_label"] == TEXT and st["installed"]["consistency_level"] == "checked_not_strong"
    never_claims(st["installed"]["model_label"])


def test_status_hermes_vectors_tested_comparison_for_a_managed_engine_identity(tmp_path):
    from yuanwei import embedwork as EW
    from yuanwei import Config
    from yuanwei.sqlstore import Store
    st = Store(str(tmp_path / "s.sqlite"))
    cfg = Config(retrieval="hybrid", embedding={"managed": True, "runtime": "llamacpp", "engine_dir": str(tmp_path / "e"), "model": "x", "identity": IDENT, "weak_identity": False})
    out = EW.status(st, str(tmp_path / "s.sqlite"), cfg)
    assert out["tested_comparison"]["level"] == "checked_not_strong" and out["tested_comparison"]["text"] == TEXT
    never_claims(out["tested_comparison"]["text"])


# ================================================================== 文档口径
DOCS = os.path.join(os.path.dirname(__file__), "..", "docs")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(p):
    return open(os.path.join(ROOT, p), encoding="utf-8").read()


def test_docs_say_the_builtin_engine_is_checked_but_not_strong_and_only_lm_studio_is_tested():
    for p in ("README.md", "docs/装到Hermes上.md", "docs/产品规则表.md"):
        t = read(p)
        assert "没过我们自己定的严格标准" in t or "没过严格标准" in t, p
    for p in ("README.md", "docs/装到Hermes上.md", "docs/产品规则表.md", "docs/让助手来装.md"):
        assert "强档" not in read(p), p                                                                 # 给用户看的文字里不再有「强档」，一律「严格标准」
    t = read("docs/装到Hermes上.md")
    assert "11,588" in t and "0.9977" in t and "测过的只有 LM Studio" in t
    assert "测过的只有 LM Studio" in read("README.md") and "测过的只有 LM Studio" in read("docs/装到Hermes上.md")
    assert "整理测过；向量：LM Studio 是测过的那一套" in read("docs/装到Hermes上.md") and "向量：LM Studio 是测过的那一套" in read("README.md")
