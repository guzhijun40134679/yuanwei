"""预设、整理供应商、白名单参数：就是两张小表。"""
import json
import os
import tomllib

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home, tree_bytes
from yuanwei import catalog as CT
from yuanwei import cli
from yuanwei import embedsetup as ES
from yuanwei import vectors as VX
from yuanwei.config import Config
from yuanwei.provider import write_request_bytes, PARAMETERS

BASE = ["--config-method", "yaml", "--src-path", "/x/src"]
SYS = lambda **kw: FakeSystem(env={"DEEPSEEK_API_KEY": FAKE_KEY, "OPENAI_API_KEY": FAKE_KEY}, **kw)


def plan(args, h, capsys, sys_=None):
    rc = cli._setup_main(["setup", "--host", "hermes", "--hermes-home", h] + BASE + args, sys_=sys_ or SYS())
    return rc, capsys.readouterr()


def test_four_presets_exist_as_data_with_labels():
    assert [p["id"] for p in CT.PRESETS] == ["recommended", "light", "custom", "local"]
    for p in CT.PRESETS:
        assert p["name"] and p["label"] in CT.LABELS and "provider" in p and "vectors" in p and p["summary"]
    assert CT.preset("local")["label"] == CT.EXPERIMENT and CT.preset("custom")["label"] == CT.UNTESTED


def test_adding_a_preset_row_shows_up_in_the_wizard_without_code_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(CT, "PRESETS", CT.PRESETS + [{"id": "demo", "name": "演示预设", "label": CT.UNTESTED, "provider": "deepseek", "vectors": "none", "summary": "加一行就多一个选项"}])
    h = make_home(tmp_path)
    io = FakeIO(["1"])
    cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=SYS())
    assert "演示预设" in io.text and "加一行就多一个选项" in io.text


def test_provider_table_rows_have_name_url_model_params_label():
    for r in CT.PROVIDERS:
        for k in ("id", "name", "base_url", "model", "params", "label", "key_env"):
            assert k in r, (r["id"], k)
        assert r["label"] in CT.LABELS and isinstance(r["params"], dict)
    ids = [r["id"] for r in CT.PROVIDERS]
    assert ids == ["deepseek", "openai-luna", "moonshot", "bailian", "zhipu", "siliconflow", "openrouter", "manual", "local"]
    assert CT.provider("deepseek")["label"] == CT.TESTED and CT.provider("openai-luna")["label"] == CT.TRIED
    for i in ("moonshot", "bailian", "zhipu", "siliconflow", "openrouter", "manual"):
        r = CT.provider(i)
        assert r["label"] == CT.UNTESTED and r["verified"] is False and (r["model"] is None) and r["example_model"]


def test_untested_rows_use_the_addresses_the_main_session_gave():
    want = {"moonshot": "https://api.moonshot.cn/v1", "bailian": "https://dashscope.aliyuncs.com/compatible-mode/v1", "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "siliconflow": "https://api.siliconflow.cn/v1", "openrouter": "https://openrouter.ai/api/v1", "openai-luna": "https://api.openai.com/v1"}
    for k, u in want.items():
        assert CT.provider(k)["base_url"] == u


def test_deepseek_request_bytes_identical_to_current_write_request_bytes(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=SYS()) == 0
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    assert write_request_bytes("要整理的话", cfg.provider["params"]) == write_request_bytes("要整理的话", PARAMETERS)


def test_luna_row_sends_reasoning_effort_and_max_completion_tokens_and_never_temperature(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "custom", "--provider", "openai-luna", "--yes", "--hermes-home", h] + BASE, sys_=SYS()) == 0
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    body = json.loads(write_request_bytes("x", cfg.provider["params"]))
    assert body["model"] == "gpt-6-luna" and body["reasoning_effort"] == "high" and body["max_completion_tokens"] == 65536
    assert "temperature" not in body and "max_tokens" not in body and set(body) == {"model", "reasoning_effort", "max_completion_tokens", "messages"}
    assert cfg.provider["api_key_env"] == "OPENAI_API_KEY"


def test_only_listed_params_are_sent_nothing_else(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "custom", "--provider", "moonshot", "--provider-model", "m1", "--provider-param", "top_p=0.5", "--yes", "--hermes-home", h] + BASE, sys_=SYS()) == 0
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    assert set(json.loads(write_request_bytes("x", cfg.provider["params"]))) == {"model", "temperature", "top_p", "messages"}


def test_untested_rows_are_labeled_and_show_the_not_tested_sentence(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "custom", "--provider", "zhipu", "--provider-model", "glm-x"], h, capsys)
    lines = "\n".join(json.loads(out.out)["plan"]["lines"])
    assert rc == 0 and "没测过：整理的提示词只在 DeepSeek 上调过" in lines and "地址和模型名未核" in lines and "YUANWEI_ZHIPU_API_KEY" in lines
    io = FakeIO(["1", "3"])
    cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=SYS())
    assert "没测过：整理的提示词只在 DeepSeek 上调过" in io.text and "（模型名你自己填）" in io.text


def test_manual_provider_asks_url_model_and_params_and_validates_https_for_non_local(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "custom", "--provider", "manual", "--provider-url", "http://example.com/v1", "--provider-model", "m"], h, capsys)
    assert rc == 2 and "insecure_url" in out.err
    rc, out = plan(["--preset", "custom", "--provider", "manual", "--provider-url", "https://example.com/v1"], h, capsys)
    assert rc == 2 and "missing_provider_fields" in out.err and "--provider-model" in out.err
    rc, out = plan(["--preset", "custom", "--provider", "manual", "--provider-url", "http://127.0.0.1:8000/v1", "--provider-model", "m"], h, capsys)
    assert rc == 0
    rc, out = plan(["--preset", "custom", "--provider", "manual", "--provider-url", "https://example.com/v1", "--provider-model", "m"], h, capsys)
    assert rc == 0


def test_provider_param_override_parses_json_values_and_rejects_garbage(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--provider-param", "temperature=0.2", "--provider-param", "stop=[\"x\"]"], h, capsys)
    assert rc == 0 and "temperature=0.2" in "\n".join(json.loads(out.out)["plan"]["lines"])
    rc, out = plan(["--provider-param", "temperature=notjson"], h, capsys)
    assert rc == 2 and "bad_provider_param" in out.err
    rc, out = plan(["--provider-param", "novalue"], h, capsys)
    assert rc == 2 and "bad_provider_param" in out.err


def test_non_loopback_hostname_resolving_to_a_lan_address_is_not_local():
    for u in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1", "http://127.5.5.5/v1"):
        assert VX.is_local_url(u), u
    for u in ("http://my-mac.local:11434/v1", "http://0.0.0.0:11434", "http://192.168.1.5:11434", "https://example.com/v1", "http://localhost.evil.com/v1", "not a url", ""):
        assert not VX.is_local_url(u), u


def test_light_preset_downloads_nothing_and_sets_keyword_retrieval(tmp_path):
    h = make_home(tmp_path)
    sysx = SYS()
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=sysx) == 0
    assert sysx.ran == [] and sysx.http_fake.calls == []
    d = tomllib.load(open(os.path.join(h, "yuanwei", "yuanwei.toml"), "rb"))
    assert d["retrieval"] == "keyword" and "embedding" not in d and "vector_fallback" not in d


def test_preset_flag_selects_defaults_and_explicit_flags_override_them(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "light", "--provider", "openai-luna"], h, capsys)
    lines = "\n".join(json.loads(out.out)["plan"]["lines"])
    assert rc == 0 and "gpt-6-luna" in lines and "美元" in lines and "不开" in lines
    rc, out = plan(["--preset", "light", "--vectors", "none", "--provider-model", "deepseek-other"], h, capsys)
    assert "deepseek-other" in "\n".join(json.loads(out.out)["plan"]["lines"])


def test_custom_preset_without_provider_flags_reports_exactly_what_is_missing(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "custom"], h, capsys)
    assert rc == 2 and "missing_provider" in out.err
    rc, out = plan(["--preset", "custom", "--provider", "openrouter"], h, capsys)
    assert rc == 2 and "--provider-model" in out.err and "deepseek/deepseek-chat" in out.err and "--provider-url" not in out.err


def test_recommended_preset_requires_vectors_choice_in_non_interactive_mode(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "recommended"], h, capsys)
    assert rc == 2 and "missing_vectors" in out.err
    rc, out = plan(["--preset", "recommended", "--vectors", "none"], h, capsys)
    assert rc == 0


def test_local_preset_allows_only_loopback_addresses_for_llm_and_embedding(tmp_path, capsys):
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "local", "--vectors", "none", "--provider-url", "https://example.com/v1", "--provider-model", "m"], h, capsys)
    assert rc == 2 and "not_local" in out.err
    rc, out = plan(["--preset", "local", "--vectors", "none", "--provider-url", "http://127.0.0.1:11434/v1"], h, capsys)
    assert rc == 2 and "missing_provider_fields" in out.err                          # 本机大模型的模型名要自己填
    rc, out = plan(["--preset", "local", "--vectors", "custom", "--embedding-url", "http://192.168.1.5:1/v1", "--embedding-model", "m", "--allow-remote-embedding",
                    "--provider-model", "m"], h, capsys)
    assert rc == 2 and "not_local" in out.err
    rc, out = plan(["--preset", "local", "--vectors", "none", "--provider-model", "llama-x"], h, capsys)
    assert rc == 0 and "没有价格表" in "\n".join(json.loads(out.out)["plan"]["lines"])
    rc, out = plan(["--preset", "local", "--provider", "deepseek", "--vectors", "none"], h, capsys)
    assert rc == 2


def test_vectors_none_writes_no_embedding_section(tmp_path):
    h = make_home(tmp_path)
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "recommended", "--vectors", "none", "--yes", "--hermes-home", h] + BASE, sys_=SYS()) == 0
    d = tomllib.load(open(os.path.join(h, "yuanwei", "yuanwei.toml"), "rb"))
    assert d["retrieval"] == "keyword" and "embedding" not in d


def test_light_preset_label_says_organizing_tested_and_keyword_only_untested(tmp_path, capsys):
    assert CT.preset("light")["label_text"] == "整理测过；检索只用关键词，答题效果没测过"
    h = make_home(tmp_path)
    rc, out = plan(["--preset", "light"], h, capsys)
    assert "轻量〔整理测过；检索只用关键词，答题效果没测过〕" in "\n".join(json.loads(out.out)["plan"]["lines"])
    io = FakeIO(["1", "q"])
    cli._setup_main(["setup", "--hermes-home", h] + BASE, io=io, sys_=SYS())
    assert "整理测过；检索只用关键词，答题效果没测过" in io.text
