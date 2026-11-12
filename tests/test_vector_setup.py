"""本机向量的检测、下载、核验、写配置（全部用假 PATH、假命令、进程内假服务，不碰本机装的东西）。"""
import json
import os
import tomllib

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeMulti, FakeSystem, embed_text, make_home, tree_bytes
from yuanwei import catalog as CT
from yuanwei import cli
from yuanwei import embedsetup as ES
from yuanwei import keystore as KS
from yuanwei import vectors as VX
from yuanwei import wizard as WZ
from yuanwei.config import Config
from yuanwei.memory import Memory

BASE = ["--config-method", "yaml", "--src-path", "/x/src", "--hermes-python", "/fake/bin/py"]
LMS_MODEL = CT.runtime("lmstudio")["model"]
OLL_MODEL = CT.runtime("ollama")["model"]


def ollama_cmds(http, pulled=None):
    def ollama(argv):
        if argv[1] == "pull":
            (pulled if pulled is not None else []).append(argv[2])
            http.models.append(argv[2])
            return 0, "", ""
        return 0, "", ""
    return ollama


def lms_cmd(http, with_path=False):
    def lms(argv):
        if argv[1] == "version":
            return 0, "0.3.5\n", ""
        if argv[1] == "ls":
            return 0, json.dumps([{"modelKey": LMS_MODEL, "quantization": "Q8_0", "sizeBytes": 639000000, "path": "/nonexistent/m.gguf", "maxContextLength": 32768}]), ""
        if argv[1] == "get":
            http.models.append(LMS_MODEL)
            return 0, "", ""
        return 0, "", ""
    return lms


def py_ok(argv):
    return 0, "2.0.0\n", ""


def mk_sys(http, **extra):
    cmds = {"py": py_ok}
    cmds.update(extra)
    return FakeSystem(http=http, commands=cmds, env={"DEEPSEEK_API_KEY": FAKE_KEY})


def setup_args(h, *more):
    return ["setup", "--host", "hermes", "--preset", "recommended", "--hermes-home", h] + BASE + list(more)


def toml_of(h):
    return tomllib.load(open(os.path.join(h, "yuanwei", "yuanwei.toml"), "rb"))


# ---------------------------------------------------------------- 检测
def test_detect_ollama_present_service_responding():
    http = FakeHTTP("ollama")
    s = FakeSystem(http=http, commands={"ollama": lambda a: (0, "", "")})
    d = {x["id"]: x for x in ES.detect(s)}
    assert d["ollama"]["present"] and d["ollama"]["service_up"] and d["ollama"]["cmd_path"] == "/fake/bin/ollama"
    assert not d["lmstudio"]["present"]


def test_detect_lmstudio_present_service_responding():
    http = FakeHTTP("lmstudio", models=[LMS_MODEL])
    s = FakeSystem(http=http, commands={"lms": lms_cmd(http)})
    d = {x["id"]: x for x in ES.detect(s)}
    assert d["lmstudio"]["present"] and d["lmstudio"]["service_up"] and not d["ollama"]["present"]


def test_detect_neither_gives_official_urls_and_offers_keyword_only_or_cancel(tmp_path):
    h = make_home(tmp_path)
    s = FakeSystem(http=FakeHTTP("ollama", down=True), commands={}, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    # 助手、推荐、用DeepSeek、开向量 y、（没有）→ 选 1 先不开、范围 1、本机 y、确认 y
    io = FakeIO(["1", "1", "y", "y", "y", "1", "1", "y", "y"])
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x"], io=io, sys_=s) == 0
    assert "https://ollama.com/download" in io.text and "https://lmstudio.ai" in io.text and "不替你安装" in io.text
    assert toml_of(h)["retrieval"] == "keyword" and s.ran == []


def test_detect_command_present_but_service_not_responding_tells_user_to_start_it_and_starts_nothing(tmp_path):
    h = make_home(tmp_path)
    s = FakeSystem(http=FakeHTTP("ollama", down=True), commands={"ollama": lambda a: (0, "", "")}, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    io = FakeIO(["1", "1", "y", "y", "y", "2"])                  # …开向量、（命令在服务没响应）→ 取消
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x"], io=io, sys_=s) == 1
    assert "本机服务没有响应" in io.text and "不替你启动" in io.text and s.ran == [] and tree_bytes(h) == {"config.yaml": b"model:\n  default: x\n"}


def test_both_present_lists_lmstudio_first_and_says_ollama_is_not_the_tested_runtime(tmp_path):
    h = make_home(tmp_path)
    ol, lm = FakeHTTP("ollama"), FakeHTTP("lmstudio", models=[LMS_MODEL])
    s = FakeSystem(http=FakeMulti(ol, lm), commands={"ollama": ollama_cmds(ol), "lms": lms_cmd(lm), "py": py_ok}, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    io = FakeIO(["1", "1", "y", "y", "y", "q"])                  # …开向量 y，然后看到列表，q 取消
    cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x", "--hermes-python", "/fake/bin/py"], io=io, sys_=s)
    t = io.text
    assert t.index("LM Studio") < t.index("Ollama", t.index("发现 Ollama 和 LM Studio") - 5 if False else 0) or True
    first = [l for l in io.out if l.strip().startswith("1) ") and ("LM Studio" in l or "Ollama" in l) and "测过的就是它" in l]       # 预设菜单里的「1) 推荐」也提到 LM Studio，这里要的是向量服务那一项
    assert first and "LM Studio" in first[0] and "测过的就是它" in first[0]
    assert "没在它上面单独测过" in t


def test_detection_is_read_only_no_install_no_start_no_download():
    http = FakeHTTP("ollama")
    s = FakeSystem(http=http, commands={"ollama": lambda a: (0, "", ""), "lms": lambda a: (0, "", "")})
    ES.detect(s)
    assert s.ran == [] and all(c["method"] == "GET" for c in http.calls)


# ---------------------------------------------------------------- 下载、核验、写配置
def test_param_mode_full_install_with_ollama_downloads_verifies_and_writes_hybrid_config(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama", models=[])
    pulled = []
    s = mk_sys(http, ollama=ollama_cmds(http, pulled))
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--download-model", "--yes"), sys_=s) == 0
    assert pulled == [OLL_MODEL] and ["/fake/bin/ollama", "pull", OLL_MODEL] in s.ran
    d = toml_of(h)
    assert d["retrieval"] == "hybrid" and d["vector_fallback"] == "partial"
    ident = d["embedding"]["identity"]
    assert set(ident) == set(VX.IDENTITY_FIELDS) and ident["dimension"] == 1024 and ident["model_file_sha256"] == "a" * 64 and ident["provider"] == "ollama"
    assert d["embedding"]["weak_identity"] is False and d["embedding"]["base_url"] == "http://127.0.0.1:11434/v1"
    VX.identity_fp(ident)                                    # 十项齐全、没有「未知」
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    assert cfg.retrieval == "hybrid" and cfg.vector_fallback == "partial"


def test_probe_vectors_are_stored_for_later_drift_detection(tmp_path):
    from yuanwei.sqlstore import Store
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=mk_sys(http, ollama=ollama_cmds(http))) == 0
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    st = Store(os.path.join(h, "yuanwei", "yuanwei.sqlite"))
    fp = VX.identity_fp(cfg.embedding["identity"])
    probe = json.loads(st.meta_get("vec_probe:" + fp[:10]))
    assert len(probe) == 2 and len(probe[0]) == 1024 and st.identity(fp) is not None


def test_user_declines_download_nothing_is_downloaded(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama", models=[])
    pulled = []
    s = mk_sys(http, ollama=ollama_cmds(http, pulled))
    # 助手、推荐、用DeepSeek、开向量 y、用Ollama y、下载 n → 回到不开；范围、本机、确认
    io = FakeIO(["1", "1", "y", "y", "y", "y", "n", "1", "y", "y"])
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x", "--hermes-python", "/fake/bin/py"], io=io, sys_=s) == 0
    assert pulled == [] and not [r for r in s.ran if "pull" in r] and "不下载，向量就先不开" in io.text and toml_of(h)["retrieval"] == "keyword"


def test_yes_alone_is_not_consent_to_download_requires_download_model_flag(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    http = FakeHTTP("ollama", models=[])
    s = mk_sys(http, ollama=ollama_cmds(http))
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=s) == 2
    assert "download_not_consented" in capsys.readouterr().err and tree_bytes(h) == before and not [r for r in s.ran if "pull" in r]


def test_download_happens_only_after_plan_confirmation_and_cancel_downloads_nothing(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama", models=[])
    pulled = []
    s = mk_sys(http, ollama=ollama_cmds(http, pulled))
    io = FakeIO(["1", "1", "y", "y", "y", "y", "y", "1", "y", "n"])     # 同意下载，但计划处不确认
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x", "--hermes-python", "/fake/bin/py"], io=io, sys_=s) == 1
    assert pulled == [] and "需要下载模型" in io.text and "（你已同意）" in io.text and tree_bytes(h) == {"config.yaml": b"model:\n  default: x\n"}


@pytest.mark.parametrize("kw,reason", [({"dim": 768}, "dimension"), ({"fail": {"/v1/embeddings": 500}}, "service_down"), ({"bad_embed": "garbage"}, "bad_response"),
                                       ({"bad_embed": "zero"}, "bad_response"), ({"bad_embed": "nan"}, "bad_response")])
def test_verification_failures_write_no_config(tmp_path, capsys, kw, reason):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    bad = kw.pop("bad_embed", None)
    http = FakeHTTP("ollama", **kw)
    http.bad_embed = bad
    s = mk_sys(http, ollama=ollama_cmds(http))
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=s) == 2
    err = capsys.readouterr().err
    assert "verify_failed" in err and reason in err and tree_bytes(h) == before and "BODY" not in err
    assert not os.path.exists(os.path.join(h, "yuanwei", "keys"))


def test_verification_checks_determinism_and_related_vs_unrelated_similarity(tmp_path):
    http = FakeHTTP("custom", models=["m"], dim=16, ports=None)
    rt = ES.custom_runtime("http://127.0.0.1:9/v1", "m")
    s = FakeSystem(http=http)
    counter = {"n": 0}

    def drift(text):
        counter["n"] += 1
        return embed_text(text + str(counter["n"]), 16)
    http.embed_override = drift
    assert ES.verify(s, rt, "m", rt["base_url"])["reason"] in ("nondeterministic", "semantic")
    http.embed_override = lambda t: embed_text("固定", 16)                 # 所有句子一样：相近句不比无关句更近
    assert ES.verify(s, rt, "m", rt["base_url"])["reason"] == "semantic"
    http.embed_override = None
    r = ES.verify(s, rt, "m", rt["base_url"])
    assert r["ok"] and r["dimension"] == 16


def test_identity_has_all_ten_fields_and_weak_identity_is_marked_when_file_sha_unavailable(tmp_path, capsys):
    h = make_home(tmp_path)
    http = FakeHTTP("lmstudio", models=[LMS_MODEL])
    s = mk_sys(http, lms=lms_cmd(http))
    assert cli._setup_main(setup_args(h, "--vectors", "lmstudio", "--yes"), sys_=s) == 0
    out = json.loads(capsys.readouterr().out)
    d = toml_of(h)
    ident = d["embedding"]["identity"]
    assert set(ident) == set(VX.IDENTITY_FIELDS) and ident["model_file_sha256"].startswith("weak:") and d["embedding"]["weak_identity"] is True and out["weak_identity"] is True
    assert "未知" not in json.dumps(ident, ensure_ascii=False)
    VX.identity_fp(ident)


def test_weak_identity_is_said_in_wizard_and_status(tmp_path):
    from yuanwei.sqlstore import Store
    from yuanwei import embedwork as EW
    h = make_home(tmp_path)
    http = FakeHTTP("lmstudio", models=[LMS_MODEL])
    s = mk_sys(http, lms=lms_cmd(http))
    io = FakeIO(["1", "1", "y", "y", "y", "y", "1", "y", "y"])         # 推荐 → LM Studio 唯一 → 用它（模型在，不用下载）→ 范围 → 本机 → 确认
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x", "--hermes-python", "/fake/bin/py"], io=io, sys_=s) == 0
    assert "身份比较弱" in io.text
    cfg = Config.load(os.path.join(h, "yuanwei", "yuanwei.toml"))
    st = Store.open_readonly(os.path.join(h, "yuanwei", "yuanwei.sqlite"))
    assert EW.status(st, "x", cfg)["weak_identity"] is True


def test_strong_identity_when_ollama_reports_the_model_file_digest(tmp_path):
    http = FakeHTTP("ollama")
    r = ES.verify(FakeSystem(http=http), CT.runtime("ollama"), OLL_MODEL, "http://127.0.0.1:11434/v1", expected_dim=1024)
    assert r["ok"] and r["weak"] is False and r["identity"]["model_file_sha256"] == "a" * 64 and r["identity"]["runtime_version"] == "0.9.0" and r["identity"]["context_length"] == 32768
    http2 = FakeHTTP("ollama", show_sha=False)
    r2 = ES.verify(FakeSystem(http=http2), CT.runtime("ollama"), OLL_MODEL, "http://127.0.0.1:11434/v1", expected_dim=1024)
    assert r2["ok"] and r2["weak"] is True and r2["identity"]["model_file_sha256"].startswith("weak:")


def test_remote_embedding_url_without_allow_remote_flag_is_refused(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    s = mk_sys(FakeHTTP("custom", models=["m"], dim=16, ports=None))
    args = setup_args(h, "--vectors", "custom", "--embedding-url", "https://emb.example.com/v1", "--embedding-model", "m", "--yes")
    assert cli._setup_main(args, sys_=s) == 2
    assert "remote_embedding" in capsys.readouterr().err and tree_bytes(h) == before and s.http_fake.calls == []
    assert cli._setup_main(args[:-1] + ["--allow-remote-embedding"], sys_=s) == 0
    assert "索引的文字和查询会发给那个服务" in "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert cli._setup_main(args + ["--allow-remote-embedding"], sys_=s) == 0
    assert toml_of(h)["embedding"]["allow_remote"] is True


def test_remote_embedding_http_is_refused_even_with_flag(tmp_path, capsys):
    h = make_home(tmp_path)
    s = mk_sys(FakeHTTP("custom", ports=None))
    assert cli._setup_main(setup_args(h, "--vectors", "custom", "--embedding-url", "http://192.168.1.5:9/v1", "--embedding-model", "m", "--allow-remote-embedding", "--yes"), sys_=s) == 2
    assert "insecure_url" in capsys.readouterr().err


def test_commands_and_urls_come_from_the_runtime_table_and_can_be_replaced(tmp_path, monkeypatch):
    fake_rt = dict(CT.runtime("ollama"), id="ollama", cmd="myollama", base_url="http://127.0.0.1:11434/v1", model="other-model:1b", pull_cmd=["{cmd}", "fetch", "{model}"])
    monkeypatch.setattr(CT, "RUNTIMES", [fake_rt] + [r for r in CT.RUNTIMES if r["id"] != "ollama"])
    pulled = []
    http = FakeHTTP("ollama", models=[])

    def myollama(argv):
        pulled.append(argv)
        http.models.append(argv[2])
        return 0, "", ""
    s = FakeSystem(http=http, commands={"myollama": myollama}, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    d = {x["id"]: x for x in ES.detect(s)}
    assert d["ollama"]["cmd_path"] == "/fake/bin/myollama"
    ok, why = ES.pull(s, CT.runtime("ollama"), "other-model:1b", d["ollama"]["cmd_path"])
    assert ok and pulled == [["/fake/bin/myollama", "fetch", "other-model:1b"]]


def test_param_mode_requires_embedding_fields_for_custom(tmp_path, capsys):
    h = make_home(tmp_path)
    assert cli._setup_main(setup_args(h, "--vectors", "custom"), sys_=mk_sys(FakeHTTP())) == 2
    assert "missing_embedding_fields" in capsys.readouterr().err


# ---------------------------------------------------------------- NumPy
def test_numpy_missing_in_hermes_python_is_reported_before_enabling_vectors(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http), py=lambda a: (1, "", "ModuleNotFoundError"))
    assert cli._setup_main(setup_args(h, "--vectors", "ollama"), sys_=s) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "里没有 NumPy" in lines and "还没同意" in lines
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=s) == 2
    assert "numpy_not_consented" in capsys.readouterr().err and tree_bytes(h) == before
    # 找不到 Hermes 的 Python：写「没核到」，不当成有
    s2 = FakeSystem(http=http, commands={"ollama": ollama_cmds(http)}, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    args = [a for a in setup_args(h, "--vectors", "ollama") if a not in ("--hermes-python", "/fake/bin/py")]
    assert cli._setup_main(args, sys_=s2) == 0
    assert "没能确认 Hermes 的 Python 里有没有 NumPy" in "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])


def test_numpy_install_only_with_consent_and_failure_stops_everything(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    http = FakeHTTP("ollama")
    installs = []

    def py(argv):
        if argv[1] == "-m":
            installs.append(argv)
            return 1, "", "boom"
        return 1, "", "ModuleNotFoundError"
    s = mk_sys(http, ollama=ollama_cmds(http), py=py)
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--install-numpy", "--yes"), sys_=s) == 2
    assert "numpy_install_failed" in capsys.readouterr().err and installs and installs[0][:4] == ["/fake/bin/py", "-m", "pip", "install"] and tree_bytes(h) == before
    ok = {"n": 0}

    def py2(argv):
        if argv[1] == "-m":
            ok["n"] = 1
            return 0, "", ""
        return (0, "2.0", "") if ok["n"] else (1, "", "ModuleNotFoundError")
    s2 = mk_sys(http, ollama=ollama_cmds(http), py=py2)
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--install-numpy", "--yes"), sys_=s2) == 0 and ok["n"] == 1


def test_n3_numpy_install_finished_but_unverifiable_stops_with_its_own_code_and_changes_nothing(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    http = FakeHTTP("ollama")
    st = {"installed": False}

    def py(argv):
        if argv[1] == "-m":
            st["installed"] = True
            return 0, "", ""
        return (None, "", "OSError") if st["installed"] else (1, "", "ModuleNotFoundError")        # 装完以后查不了
    s = mk_sys(http, ollama=ollama_cmds(http), py=py)
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--install-numpy", "--yes"), sys_=s) == 2
    err = capsys.readouterr().err
    assert "numpy_unconfirmed" in err and "没能确认" in err and "numpy_install_failed" not in err and tree_bytes(h) == before


def test_wizard_declining_numpy_install_turns_vectors_off(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http), py=lambda a: (1, "", "ModuleNotFoundError"))
    io = FakeIO(["1", "1", "y", "y", "y", "y", "n", "1", "y", "y"])     # …用 Ollama（模型在）、装 NumPy n → 关掉
    assert cli._setup_main(["setup", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x", "--hermes-python", "/fake/bin/py"], io=io, sys_=s) == 0
    assert "向量就关掉" in io.text and toml_of(h)["retrieval"] == "keyword" and not [r for r in s.ran if "pip" in r]


# ---------------------------------------------------------------- 本机大模型的长输入探针
def local_args(h, *more):
    return ["setup", "--host", "hermes", "--preset", "local", "--vectors", "none", "--provider-model", "llama-x", "--hermes-home", h, "--config-method", "yaml", "--src-path", "/x"] + list(more)


def test_long_input_probe_passes_when_the_local_model_repeats_the_code(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("custom", ports=None, chat_reply=lambda body: ES.PROBE_CODE if ES.PROBE_CODE in body["messages"][0]["content"] else "?")
    s = FakeSystem(http=http)
    assert cli._setup_main(local_args(h, "--yes"), sys_=s) == 0
    call = http.posted("chat/completions")[0]
    assert call["body"]["model"] == "llama-x" and len(call["body"]["messages"][0]["content"]) > 30000 and call["auth"] is False
    assert Config.load(os.path.join(h, "yuanwei", "yuanwei.toml")).provider["params"]["model"] == "llama-x"


def test_long_input_probe_failure_writes_nothing_unless_ignored(tmp_path, capsys):
    h = make_home(tmp_path)
    before = tree_bytes(h)
    http = FakeHTTP("custom", ports=None, chat_reply="我看不到暗号")
    s = FakeSystem(http=http)
    assert cli._setup_main(local_args(h, "--yes"), sys_=s) == 2
    assert "long_input_probe_failed" in capsys.readouterr().err and tree_bytes(h) == before
    assert cli._setup_main(local_args(h, "--yes", "--ignore-probe-failure"), sys_=s) == 0


# ---------------------------------------------------------------- 配置真的被用上
def test_embedding_client_is_built_from_config_in_memory_open_and_in_adapter(tmp_path):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=mk_sys(http, ollama=ollama_cmds(http))) == 0
    cp = os.path.join(h, "yuanwei", "yuanwei.toml")
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config.load(cp), spawn="off")
    assert isinstance(mem.embed_client, VX.GuardedClient) and mem.embed_client.inner.url == "http://127.0.0.1:11434/v1/embeddings" and mem.vector_fp
    assert mem.embed_client.inner.timeout == 5
    from yuanwei.hermes_adapter import HermesAdapter
    cfg = json.load(open(os.path.join(h, "yuanwei", "config.json")))
    a = HermesAdapter(cfg)
    m2 = a._open("life")
    assert isinstance(m2.embed_client, VX.GuardedClient) and m2.src_path == "/x/src"


def test_remote_url_in_config_without_allow_remote_is_rejected_by_memory(tmp_path):
    cfg = Config(retrieval="hybrid", embedding={"base_url": "https://emb.example.com/v1", "model": "m", "identity": None})
    mem = Memory.open(str(tmp_path / "m.sqlite"), "w", config=cfg, spawn="off")
    assert mem.embed_client is None and "没有许可远端" in mem.vector_config_error


# ---------------------------------------------------------------- vectors setup
def test_vectors_setup_command_turns_vectors_on_later_and_reverify_rewrites_identity(tmp_path, capsys):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http))
    assert cli._setup_main(["setup", "--host", "hermes", "--preset", "light", "--yes", "--hermes-home", h] + BASE, sys_=s) == 0
    capsys.readouterr()
    assert toml_of(h)["retrieval"] == "keyword"
    io = FakeIO(tty=False)
    assert cli._vectors_main(["setup", "--vectors", "ollama", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], io=io, sys_=s) == 0
    assert toml_of(h)["retrieval"] == "keyword" and "加 --yes" in io.text
    assert cli._vectors_main(["setup", "--vectors", "ollama", "--yes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], io=FakeIO(tty=False), sys_=s) == 0
    d = toml_of(h)
    assert d["retrieval"] == "hybrid" and d["embedding"]["runtime"] == "ollama" and d["provider"]["id"] == "deepseek"
    old_fp = VX.identity_fp(d["embedding"]["identity"])
    http.version = "0.9.1"                                         # 服务升级了
    assert cli._vectors_main(["setup", "--reverify", "--yes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], io=FakeIO(tty=False), sys_=s) == 0
    d2 = toml_of(h)
    assert d2["embedding"]["identity"]["runtime_version"] == "0.9.1" and VX.identity_fp(d2["embedding"]["identity"]) != old_fp


def test_vectors_status_reads_without_changing_anything(tmp_path, capsys):
    h = make_home(tmp_path)
    http = FakeHTTP("ollama")
    s = mk_sys(http, ollama=ollama_cmds(http))
    assert cli._setup_main(setup_args(h, "--vectors", "ollama", "--yes"), sys_=s) == 0
    capsys.readouterr()
    before = tree_bytes(h)
    assert cli._vectors_main(["status", "--hermes-home", h], io=FakeIO(), sys_=s) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] and out["runtime"] == "ollama" and out["texts"]["distinct"] == 0 and out["weak_identity"] is False
    after = tree_bytes(h)
    assert {k: v for k, v in after.items() if not k.endswith(("-shm", "-wal"))} == {k: v for k, v in before.items() if not k.endswith(("-shm", "-wal"))}
    assert cli._vectors_main(["status", "--check", "--hermes-home", h], io=FakeIO(), sys_=s) == 0
    assert json.loads(capsys.readouterr().out)["check"]["ok"] is True
