"""清单第 55 条（0.1.0a9）：没有改口、没有插话的轮次，存下的行、锚点、跳过原因、计数、MCP 式 status 和 0.1.0a8（21b88cf）逐字节相同。
做法：把 21b88cf 的 src 用 git archive 取到临时目录，用同一份剧本（a8_parity_script.py）在两套源码上各跑一次，比输出。"""
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
A8 = "21b88cf"


def _run(src):
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "a8_parity_script.py"), src, HERE], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout.strip().splitlines()[-1]


def test_turns_without_redirect_or_steer_are_byte_identical_to_a8(tmp_path):
    ok = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", A8], capture_output=True)
    if ok.returncode != 0:
        pytest.skip("本机 git 里没有 0.1.0a8 的提交")
    tar = subprocess.run(["git", "-C", ROOT, "archive", A8, "src"], capture_output=True, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(tar)).extractall(str(tmp_path / "a8"))
    old, new = _run(str(tmp_path / "a8" / "src")), _run(os.path.join(ROOT, "src"))
    assert old == new
    got = json.loads(new)
    assert got["recorded_turns"] >= 6 and got["skipped"] and got["clarify"]["answers_kept"] == 2                              # 剧本确实走到了：普通、长句、密钥、提问卡、机器行、没登记、机器人、带图、口令
    assert any("（用户答）靠窗那家" in r[1] for r in got["rows"]) and len(got["anchors"]) > 10 and "turns" not in got["mcp_status_keys"]
