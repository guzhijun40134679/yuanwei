"""清单第 56 条第一批（0.1.0a10）：今天记得对的轮次——普通、长句、密钥、提问卡、机器行、没登记、机器人、带图、口令、改口、插话、删除（申请＋确认）、删除之后的普通轮、压缩换号
（轮与轮之间、一轮当中）——存下的行、锚点、事件键、轮次号的关系、跳过原因、计数、窗口状态、MCP 式 status 和 0.1.0a9（91ee251）逐字节相同。
做法：把 91ee251 的 src 用 git archive 取到临时目录，用同一份剧本（a9_parity_script.py）在两套源码上各跑一次，比输出。
剧本里每次运行都会变的东西（随机本地令牌、照它们算的指纹和编号）已经在剧本里换成占位；先把旧源码跑两遍，确认剧本本身是稳定的，不然「相同」说明不了什么。"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
A9 = "91ee251"


def _run(src, variant="cli"):
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "a9_parity_script.py"), src, HERE, variant], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def a9_src(tmp_path_factory):
    if subprocess.run(["git", "-C", ROOT, "cat-file", "-e", A9], capture_output=True).returncode != 0:
        pytest.skip("本机 git 里没有 0.1.0a9 的提交")
    d = tmp_path_factory.mktemp("a9")
    tar = subprocess.run(["git", "-C", ROOT, "archive", A9, "src"], capture_output=True, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(tar)).extractall(str(d))
    return str(d / "src")


def test_the_script_is_stable_on_the_old_source_and_the_new_source_matches_it_byte_for_byte(a9_src):
    first, second = _run(a9_src), _run(a9_src)
    assert first == second, "剧本在同一套源码上两次不一样：先把剧本里随机的东西换成占位"
    new = _run(os.path.join(ROOT, "src"))
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(new.encode()).hexdigest()
    assert first == new


def test_the_script_really_covers_every_kind_of_turn(a9_src):
    got = json.loads(_run(os.path.join(ROOT, "src")))
    b, a = got["before_delete"], got["after_delete"]
    users = [r[6] for r in b["rows"] if r[5] == "user" and r[1] != "S1"]
    assert b["recorded_turns"] >= 12 and b["skipped"] == {"bot_author": 1, "machinery_row": 1, "no_pending_turn": 1}
    assert any("（用户答）靠窗那家" in u for u in users) and any("（带 1 张图）" in u for u in users) and any("（用户中途又说）不对，改成周六的" in u for u in users) and any("（中途插入·来源未核实）顺便看看高架" in u for u in users)
    assert any(u.startswith("我的 key 是 〔密钥已挡掉") for u in users) and any(u.startswith("很长的一句。") for u in users)
    assert {r[1] for r in b["rows"]} >= {"hs1", "hs1c", "hs1d", "hs-new", "hs-reset"} and sorted(x[3] for x in b["bindings"]) == ["compression", "compression", "initialize", "initialize", "reset"]
    assert [w[0] for w in b["windows"]] == ["off", "on", "on"] and b["new_chat_read"] == "ok"                                   # 库里有窗口关着时：没有历史的新对话、`/new` 照旧开着
    assert len(b["anchors"]) > 10 and len(b["turns"]) >= 14 and b["window_ops"] == [["close", "command", "applied", None]]
    assert len([x for x in b["token_keys"] if x[0] == "hs1d" and ":" in x[2]]) >= 1 and len([x for x in b["token_keys"] if ":" in x[2]]) >= 12     # 事件键（轮次号:指纹前缀）原样在
    assert a["deletions"][0][:3] == ["window", "chat", "complete"] and a["no_record"] == [["delete_confirm"]] and a["skipped"]["delete_confirm_turn"] == 1
    assert [r[6] for r in a["rows"] if r[1] == "hs1d"] == ["删除以后的普通一句", "好"] and a["delete_pending"] == [["confirmed", None]]


# ================================================================== 审后修补第五节：第二遍——换平台、装假宿主模块、initialize 带历史（库里没有关着的窗口）
# 微信（一对一、本人已登记）和桌面，宿主说这一轮是用户开的（origin＝user），再各来一遍没有当前事件（origin＝None）；另有口令开、大白话关两轮。
# 来源说明、技能命令、待办快照、重新生成这些本来就要变的不放进剧本。
@pytest.mark.parametrize("variant", ["weixin-user", "desktop-user", "weixin-none", "desktop-none"])
def test_second_pass_with_fake_host_modules_and_a_resumed_initialize_matches_a9_byte_for_byte(a9_src, variant):
    first, second = _run(a9_src, variant), _run(a9_src, variant)
    assert first == second, "剧本在同一套源码上两次不一样：先把剧本里随机的东西换成占位"
    new = _run(os.path.join(ROOT, "src"), variant)                                      # 脚本在新源码上还会自检假宿主模块真的起作用（origin 是 user／None）
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(new.encode()).hexdigest()
    assert first == new


@pytest.mark.parametrize("variant", ["weixin-user", "desktop-user", "weixin-none", "desktop-none"])
def test_second_pass_really_covers_every_kind_of_turn_and_the_open_and_close_steps(a9_src, variant):
    got = json.loads(_run(os.path.join(ROOT, "src"), variant))
    b, a = got["before_delete"], got["after_delete"]
    users = [r[6] for r in b["rows"] if r[5] == "user" and r[1] != "S1"]
    assert b["recorded_turns"] >= 14 and b["skipped"] == {"bot_author": 1, "machinery_row": 1, "no_pending_turn": 1}
    assert any("（用户答）靠窗那家" in u for u in users) and any("（带 1 张图）" in u for u in users) and any("（用户中途又说）不对，改成周六的" in u for u in users) and any("（中途插入·来源未核实）顺便看看高架" in u for u in users)
    assert any(u.startswith("我的 key 是 〔密钥已挡掉") for u in users) and any(u.startswith("很长的一句。") for u in users)
    assert any(u.startswith("#yuanwei-mem\n我回来了") for u in users) and any(u.startswith("这次别用记忆") for u in users)
    assert b["window_ops"] == [["close", "command", "applied", None], ["open", "command", "applied", None], ["close", "rule", "applied", None]]     # 口令关、口令开、大白话关
    assert [w[0] for w in b["windows"]] == ["off", "on", "on"] and b["new_chat_read"] == "ok" and b["window"]["state"] == "off"
    assert sorted(x[3] for x in b["bindings"]) == ["compression", "compression", "initialize", "initialize", "reset"]
    assert a["deletions"][0][:3] == ["window", "chat", "complete"] and a["no_record"] == [["delete_confirm"]] and a["skipped"]["delete_confirm_turn"] == 1
    assert a["delete_pending"] == [["confirmed", None]]
