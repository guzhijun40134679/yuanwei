"""宿主交付路径：每条路径登记单位、阈值、包装开销、截断或转存行为；单位不混算。第二段·交回点甲。"""
import dataclasses

import pytest

from yuanwei import hosts as H


def test_units_measure():
    assert H.measure("cp", "你好😀") == 3
    assert H.measure("utf16", "你好😀") == 4
    assert H.measure("bytes", "你好😀") == 6 + 4
    assert H.measure("codex_tok", "你好😀") == (10 + 3) // 4
    assert H.measure("claude_tok_proxy", "你好😀") == 10
    assert H.measure("bytes", "\ud800") == 3                  # 孤立代理字符：按 surrogatepass 数（保守，不抛错）
    assert H.measure("bytes", "") == 0
    with pytest.raises(ValueError):
        H.measure("lines", "x")


def test_four_hosts_and_unknown_host_is_error():
    assert set(H.PROFILES) == {"claude-code", "codex", "hermes", "pi"}
    with pytest.raises(ValueError):
        H.profile("nope")


def test_identity_chain_status_per_host_is_declared():
    assert H.profile("claude-code").identity == "unproven"
    assert H.profile("codex").identity == "unproven"
    assert H.profile("hermes").identity == "in_process"
    assert H.profile("pi").identity == "launch_bound"
    for n in H.PROFILES:
        assert not H.profile(n).tools_opened or H.profile(n).identity != "unproven"
    assert H.profile("claude-code").tools_opened is False and H.profile("codex").tools_opened is False


def test_no_fixed_rule_is_marked_verified_in_this_stage():
    for n, p in H.PROFILES.items():
        for name, path in p.paths.items():
            assert path.verified is False, (n, name)          # 真机上交付是否真的到，第三段才验证


def test_claude_code_paths():
    p = H.profile("claude-code")
    mcp = {(l.unit, l.threshold) for l in p.paths["mcp_return"].limits}
    assert ("utf16", 50000) in mcp and ("claude_tok_proxy", 25000) in mcp
    hook = {(l.unit, l.threshold) for l in p.paths["hook_context"].limits}
    assert ("utf16", 10000) in hook


def test_pi_mcp_path_is_20kb_by_utf8_bytes_conservative():
    lim = H.profile("pi").paths["mcp_return"].limits
    assert [(l.unit, l.threshold) for l in lim] == [("bytes", 20000)]


def test_codex_hook_unit_and_required_limit():
    p = H.profile("codex")
    assert [(l.unit, l.threshold) for l in p.paths["hook_context"].limits] == [("codex_tok", 2500)]     # 宿主默认，不假定装接头已经调高
    assert H.codex_required_limit(8000) == 8000 and H.codex_required_limit(8000, wrapper_chars=120) == 8120
    cfg = H.profile("codex").configured(codex_hook_limit=H.codex_required_limit(8000))
    assert cfg.paths["hook_context"].limits[0].threshold == 8000
    assert H.profile("codex").paths["hook_context"].limits[0].threshold == 2500          # 原对象不变


def test_fits_boundary_and_overhead():
    path = H.Path("t", (H.Limit("cp", 100, "spill"),), overhead={"cp": 10})
    ok, d = H.fits(path, "a" * 90)
    assert ok and d[0]["value"] == 100
    ok, d = H.fits(path, "a" * 91)
    assert not ok and d[0]["value"] == 101 and d[0]["threshold"] == 100


def test_all_limits_must_hold():
    path = H.Path("t", (H.Limit("cp", 100, "spill"), H.Limit("bytes", 120, "spill")), overhead={})
    assert H.fits(path, "a" * 100)[0]
    assert not H.fits(path, "你" * 50)[0]              # 50 个码点，150 字节
    assert H.fits(path, "你" * 40)[0]


def test_conservative_counting_differs_for_supplementary_chars():
    cc_hook = H.profile("claude-code").paths["hook_context"]
    assert H.fits(cc_hook, "中" * 8000)[0]
    assert not H.fits(cc_hook, "😀" * 8000)[0]          # 8000 个增补平面字符按 UTF-16 是 16000，超过 10000
    cx = H.profile("codex").configured(codex_hook_limit=8000).paths["hook_context"]
    assert H.fits(cx, "😀" * 8000)[0]                   # 4 字节 ÷ 4 ＝ 每字 1，正好 8000


def test_profiles_are_immutable_and_configured_returns_copy():
    p = H.profile("pi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.identity = "in_process"


def test_every_path_records_unit_threshold_overhead_and_behaviour():
    for n, p in H.PROFILES.items():
        for name, path in p.paths.items():
            assert path.limits and path.source
            for l in path.limits:
                assert l.unit in H.UNITS and isinstance(l.threshold, int) and l.on_exceed
            assert isinstance(path.overhead, dict)
