"""版本号三处（pyproject.toml、yuanwei.__version__、hermes_plugin.yaml）必须一致；
装到 Hermes 上的说明里写的离线安装 wheel 文件名也要跟着（0.1.0a2 发布时发现没有测试盯这几处）。
tests/test_hermes_env_find.py 里故意留着的 0.1.0.dev0 是「旧版 plugin.yaml」的样例，不在这里管。"""
import re
import tomllib
from pathlib import Path

import yuanwei

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _plugin_yaml_version():
    text = (ROOT / "src" / "yuanwei" / "resources" / "hermes_plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)\s*$", text, re.M)
    assert m, "hermes_plugin.yaml 里没有 version 行"
    return m.group(1)


def test_three_version_strings_are_equal():
    py, mod, yml = _pyproject_version(), yuanwei.__version__, _plugin_yaml_version()
    assert py and mod and yml, "有一处版本号读出来是空的"
    assert py == mod == yml, "版本号不一致：pyproject=%r __version__=%r plugin.yaml=%r" % (py, mod, yml)


def test_install_guide_wheel_name_matches_version():
    text = (ROOT / "docs" / "装到Hermes上.md").read_text(encoding="utf-8")
    names = re.findall(r"yuanwei-([0-9][^\s/]*?)-py3-none-any\.whl", text)
    assert names, "安装说明里找不到 wheel 文件名"
    assert set(names) == {_pyproject_version()}, "安装说明里的 wheel 版本 %r 和 pyproject %r 不一致" % (sorted(set(names)), _pyproject_version())
