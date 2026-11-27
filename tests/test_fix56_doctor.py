"""清单第 56 条第一批·第 9 节：体检里加一条「谁开的这一轮」，只报在不在（状态恒为 ok，不影响别的判断）。合成的 Hermes 目录树，只读，不联网。"""
import os
import subprocess

import pytest

from test_hermes_compat import REAL, git_reader, host, status
from yuanwei import hermes_compat as HC

SESSION_CONTEXT = '''
from contextvars import ContextVar
from typing import Any
_SESSION_TURN_EVENT: ContextVar[Any] = ContextVar("HERMES_SESSION_TURN_EVENT", default=None)
'''
FILTERS = '''
def display_kind_for_event(event):
    return None
'''
EVENT = '''
from dataclasses import dataclass
@dataclass
class MessageEvent:
    text: str
    internal: bool = False
'''


def with_files(tmp_path, **files):
    root = host(tmp_path)
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(text)
    return root


def full(tmp_path, **override):
    d = {"gateway/session_context.py": SESSION_CONTEXT, "gateway/response_filters.py": FILTERS, "gateway/platforms/event.py": EVENT}
    d.update(override)
    return with_files(tmp_path, **d)


def entry(r):
    return next(c for c in r["checks"] if c["id"] == "turn_origin")


def test_the_dependency_is_listed_with_features_and_a_check_result():
    d = next(x for x in HC.DEPENDENCIES if x["id"] == "turn_origin")
    assert d["affects"] and d["what"] and d["where"] and "只报在不在" in d["if_changed"]


def test_all_present_says_so(tmp_path):
    r = HC.doctor(full(tmp_path))
    c = entry(r)
    assert c["status"] == "ok" and "在：_SESSION_TURN_EVENT、display_kind_for_event、MessageEvent.text、MessageEvent.internal" in c["detail"] and "不在" not in c["detail"].split("（")[0]
    assert r["exit_code"] == 0 and r["summary"]["changed"] == 0 and r["summary"]["missing"] == 0


def test_nothing_present_still_ok_and_names_each_missing_file(tmp_path):
    r = HC.doctor(host(tmp_path))                                                        # 合成宿主里没有这三个文件
    c = entry(r)
    assert c["status"] == "ok" and r["exit_code"] == 0 and "turn_origin" not in r["affected_features"]
    for name in ("gateway/session_context.py", "gateway/response_filters.py", "gateway/platforms/event.py"):
        assert name in c["detail"] and "读不到" in c["detail"]
    assert "在：无" in c["detail"]


ALL4 = ["_SESSION_TURN_EVENT", "display_kind_for_event", "MessageEvent.text", "MessageEvent.internal"]


@pytest.mark.parametrize("rel,text,missing", [
    ("gateway/session_context.py", "X = 1\n", ["_SESSION_TURN_EVENT"]),
    ("gateway/response_filters.py", "def something_else(event):\n    return None\n", ["display_kind_for_event"]),
    ("gateway/platforms/event.py", "class MessageEvent:\n    text: str\n", ["MessageEvent.internal"]),
    ("gateway/platforms/event.py", "class MessageEvent:\n    internal: bool = False\n", ["MessageEvent.text"]),
    ("gateway/platforms/event.py", "class Other:\n    text: str\n    internal: bool = False\n", ["MessageEvent.text", "MessageEvent.internal"]),
])
def test_each_missing_piece_is_named_and_nothing_else_is_affected(tmp_path, rel, text, missing):
    r = HC.doctor(full(tmp_path, **{rel: text}))
    c = entry(r)
    present_part, miss_part = c["detail"].split("不在或读不到：")
    for m in missing:
        assert (m in miss_part) or (m.startswith("MessageEvent") and "MessageEvent" in miss_part)
        assert m not in present_part
    for ok in ALL4:
        if ok not in missing and not (ok.startswith("MessageEvent") and "MessageEvent" in miss_part and not any(x.startswith("MessageEvent.") for x in missing)):
            assert ok in present_part, (ok, c["detail"])
    assert c["status"] == "ok" and r["exit_code"] == 0 and r["affected_features"] == []


def test_an_unreadable_file_is_reported_as_unreadable_not_as_present(tmp_path):
    c = entry(HC.doctor(full(tmp_path, **{"gateway/response_filters.py": "def broken(:\n"})))
    assert "gateway/response_filters.py（读不懂）" in c["detail"] and "display_kind_for_event" not in c["detail"].split("不在或读不到：")[0]


def test_the_other_checks_are_not_changed_by_this_one(tmp_path):
    base = status(HC.doctor(host(tmp_path / "a")))
    withit = status(HC.doctor(full(tmp_path / "b")))
    assert {k: v for k, v in withit.items() if k != "turn_origin"} == {k: v for k, v in base.items() if k != "turn_origin"}


@pytest.mark.skipif(not os.path.isdir(REAL), reason="没有本机 Hermes 源码")
@pytest.mark.parametrize("rev,present", [("ae2a81ccc0", True), ("935c1e8962d43de3ef0adde30e80dda55d0247db", False)])
def test_pinned_revisions_read_through_git(rev, present):
    if subprocess.run(["git", "-C", REAL, "cat-file", "-e", rev], capture_output=True).returncode != 0:
        pytest.skip("本机 git 里没有这个提交")
    c = entry(HC.doctor_from_reader(git_reader(rev)))
    assert c["status"] == "ok"
    assert (("在：_SESSION_TURN_EVENT" in c["detail"]) and "不在或读不到" not in c["detail"]) == present, c["detail"]
