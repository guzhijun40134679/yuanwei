"""便条在 Hermes 里的字符上限：照用户的 Hermes 设置定（没开 spill、开了 spill、读不到三种），读设置失败不影响插件加载，doctor 能看出名字变了。全部用假设置，不碰真实 Hermes。"""
import pytest

from helpers2 import organized
from test_hermes_compat import host, status
from yuanwei import hermes_compat as HC
from yuanwei import hosts as H
from yuanwei.access import Access
from yuanwei.modes import ModeConfig, ModeRunner


def cfg(spill=None, max_chars=None, **memory):
    out = {"memory": dict(memory), "hooks": {"output_spill": {}}}
    if spill is not None:
        out["memory"]["prefetch_spill_enabled"] = spill
    if max_chars is not None:
        out["hooks"]["output_spill"]["max_chars"] = max_chars
    return out


# ------------------------------------------------------------------ 三种情况
def test_spill_not_enabled_uses_ten_times_max_chars_with_a_100000_floor():
    r = HC.prefetch_limit(lambda: cfg(), default_max_chars=10000)
    assert (r["chars"], r["basis"], r["max_chars"], r["reason"]) == (100000, "no_spill", 10000, None)       # 两项都没写：默认关、max_chars 取默认
    assert HC.prefetch_limit(lambda: cfg(False, 20000))["chars"] == 200000                                  # 10 倍比 100,000 大
    assert HC.prefetch_limit(lambda: cfg(False, 3000))["chars"] == 100000                                   # 10 倍比 100,000 小：取 100,000
    assert HC.prefetch_limit(lambda: {})["basis"] == "no_spill"                                             # 设置是空字典：照默认（关）
    assert HC.prefetch_limit(lambda: cfg(None, None, other=1), default_max_chars=10000)["chars"] == 100000


def test_spill_enabled_uses_max_chars_itself():
    r = HC.prefetch_limit(lambda: cfg(True), default_max_chars=10000)
    assert (r["chars"], r["basis"]) == (10000, "spill")
    assert HC.prefetch_limit(lambda: cfg(True, 6000))["chars"] == 6000


def test_unreadable_settings_fall_back_to_10000_and_never_raise():
    def boom():
        raise RuntimeError("config.yaml 坏了")
    for loader, why in ((boom, "RuntimeError"), (lambda: None, "字典"), (lambda: [1], "字典"),
                        (lambda: cfg("yes"), "布尔"), (lambda: cfg(False, "10000"), "正整数"), (lambda: cfg(False, 0), "正整数"), (lambda: cfg(False, True), "正整数")):
        r = HC.prefetch_limit(loader, default_max_chars=10000)
        assert r["chars"] == 10000 and r["basis"] == "fallback" and why in r["reason"], (loader, r)
    r = HC.prefetch_limit()                                                                                  # 不在 Hermes 进程里：hermes_cli 导入不了
    assert r["chars"] == 10000 and r["basis"] == "fallback" and "读不到 Hermes 设置" in r["reason"]


# ------------------------------------------------------------------ 套到宿主路径上
def test_profile_with_hook_chars_replaces_only_the_hermes_hook_limit():
    base = H.profile("hermes")
    p = base.with_hook_chars(100000)
    assert p.paths["hook_context"].limits[0].unit == "cp" and p.paths["hook_context"].limits[0].threshold == 100000
    assert p.paths["hook_context"].overhead == base.paths["hook_context"].overhead and p.paths["mcp_return"] == base.paths["mcp_return"]
    assert base.paths["hook_context"].limits[0].threshold == 10000                                            # 原对象不变
    assert H.char_cap(p.paths["hook_context"]) == 100000 - 300


def eng(i, n):
    out, j = "needle %d" % i, 0
    while len(out) < n:
        out += " word%d" % ((i * 7 + j) % 97)
        j += 1
    return out[:n]


def test_english_note_uses_the_wider_limit_when_spill_is_off_and_stays_at_10000_when_on(tmp_path):
    texts = [eng(i, 300) for i in range(120)]
    sizes = {}
    for name, limit in (("off", 100000), ("on", 10000)):
        (tmp_path / name).mkdir()
        mem = organized(tmp_path / name, texts=texts)
        prof = H.profile("hermes").with_hook_chars(limit)
        out = ModeRunner(mem, Access(mem), prof, ModeConfig(mode="note")).on_user_prompt("S", "T1", "needle")
        assert out["kind"] == "note" and H.fits(prof.paths["hook_context"], out["inject"])[0]
        sizes[name] = len(out["inject"])
    assert sizes["on"] <= 10000 - 300 and sizes["off"] > 20000                                                 # 没开 spill：用到词元额度（约 4,200）；开了：被 1 万字挡住


# ------------------------------------------------------------------ 适配器：读设置失败不影响插件加载
def adapter(tmp_path, monkeypatch, fn):
    from test_hermes_adapter import mk
    monkeypatch.setattr(HC, "prefetch_limit", fn)
    return mk(tmp_path)


def test_adapter_runner_profile_follows_the_settings(tmp_path, monkeypatch):
    mem, a = adapter(tmp_path / "a", monkeypatch, lambda: {"chars": 100000, "basis": "no_spill", "max_chars": 10000, "reason": None})
    assert a.runner.profile.paths["hook_context"].limits[0].threshold == 100000
    assert a.status()["prefetch_limit"]["basis"] == "no_spill"
    assert a._surfaces[a.ws["key"]].profile.paths["hook_context"].limits[0].threshold == 10000                  # 工具那一路（mcp_return）和它的 profile 不动


def test_adapter_loads_and_falls_back_when_the_settings_cannot_be_read(tmp_path, monkeypatch):
    def boom():
        raise OSError("读不到")
    real = HC.prefetch_limit
    mem, a = adapter(tmp_path / "b", monkeypatch, lambda: real(boom))                              # 读设置的函数内部出错：返回保守值，不抛
    assert a.active and a.runner.profile.paths["hook_context"].limits[0].threshold == 10000
    assert a.status()["prefetch_limit"]["basis"] == "fallback"
    from test_hermes_adapter import mk
    mem2, a2 = mk(tmp_path / "c")                                                                                # 不打补丁：不在 Hermes 进程里，hermes_cli 导入不了，真的读不到
    assert a2.active and a2.runner.profile.paths["hook_context"].limits[0].threshold == 10000
    assert a2.status()["prefetch_limit"]["basis"] == "fallback"


# ------------------------------------------------------------------ doctor：Hermes 改了这几个名字要看得出来
@pytest.mark.parametrize("edit,why", [
    ({"agent/memory_manager.py": ("prefetch_spill_enabled", "prefetch_spill_renamed")}, "prefetch_spill_enabled"),
    ({"tools/hook_output_spill.py": ("DEFAULT_MAX_CHARS", "DEFAULT_CHARS")}, "DEFAULT_MAX_CHARS"),
    ({"hermes_cli/config.py": ("def load_config", "def read_config")}, "load_config"),
    ({"tools/hook_output_spill.py": None}, "tools/hook_output_spill.py"),
    ({"hermes_cli/config.py": None}, "hermes_cli/config.py"),
])
def test_doctor_notices_renamed_or_missing_names(tmp_path, edit, why):
    ok = HC.doctor(host(tmp_path / "ok"))
    assert status(ok)["prefetch_spill_limit"] == "ok"
    r = HC.doctor(host(tmp_path / "x", **edit))
    c = next(x for x in r["checks"] if x["id"] == "prefetch_spill_limit")
    assert c["status"] == "changed" and why in c["detail"] and "prefetch" in r["affected_features"] and r["exit_code"] == 1
    assert [k for k, v in status(r).items() if v != "ok"] == ["prefetch_spill_limit"]
