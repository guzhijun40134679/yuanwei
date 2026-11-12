"""真机试装修补（一）乙（2026-11-11）：找到 Hermes 真正导入包的那个 Python 环境，装 NumPy。
今天 Hermes 的布局：~/.local/bin/hermes（sh）→ hermes-agent/.hermes/bin/hermes（sh）→ tools/python-…/bin/python3 -I -c <启动代码>；
第三方包在 installs/<id>/environments/<id>/venv（uv 管，没有 pip，pyvenv.cfg 里有 uv = 版本）；11-09 更新前的旧环境 hermes-agent/venv 还在，但 Hermes 已经不用了。
这里用真的子进程造一套同样布局的假 Hermes（启动代码是我们自己写的、只会回答 --print-runtime-command；解释器用测试自己的 Python，环境用真的 venv），不碰、不跑真的 Hermes。"""
import json
import os
import shlex
import stat
import sys
import venv

import pytest

from helpers_wizard import FAKE_KEY, FakeHTTP, FakeIO, FakeSystem, make_home
from yuanwei import cli
from yuanwei import embedsetup as ES
from yuanwei import hermes_setup as HS
from yuanwei import wizard as WZ

STARTUP = r'''
import sys, json
SP = %(sp)r
a = sys.argv[1:]
if a[:1] == ["--print-runtime-command"]:
    mod = a[a.index("--module") + 1] if "--module" in a else "hermes_cli.main"
    boot = "import sys, runpy; sys.path.insert(0, %%r); runpy.run_module(%%r, run_name='__main__', alter_sys=True)" %% (SP, mod)
    print(json.dumps([sys.executable, "-I", "-c", boot]))
    sys.exit(0)
sys.exit(3)
'''


def _exe(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _venv(root, uv_version=None):
    venv.EnvBuilder(with_pip=False, symlinks=True).create(str(root))
    if uv_version:
        with open(root / "pyvenv.cfg", "a") as f:
            f.write("uv = %s\n" % uv_version)
    sp = next((root / "lib").glob("python3*/site-packages"))
    return str(root / "bin" / "python"), str(sp)


class Layout:
    """~/.hermes 的今天的布局。"""

    def __init__(self, tmp, with_old_venv=True, runtime_works=True, generations=2):
        self.home = tmp / ".hermes"
        agent = self.home / "hermes-agent"
        self.gens = []
        for i in range(generations):
            self.gens.append(_venv(self.home / "installs" / ("i%d" % i) / "environments" / ("e%d" % i) / "venv", uv_version="0.13.5" if i == generations - 1 else "0.12.3"))
        self.env_python, self.sp = self.gens[-1]                                              # 最新一代是 Hermes 现在用的
        self.old_python = None
        if with_old_venv:
            self.old_python, _ = _venv(agent / "venv")                                       # 11-09 更新前的旧环境
        self.uv_new = _exe(self.home / "tools" / "uv-0.13.5-darwin-arm64" / "uv", "#!/bin/sh\nexit 0\n")
        self.uv_old = _exe(self.home / "tools" / "uv-0.12.3-darwin-arm64" / "uv", "#!/bin/sh\nexit 0\n")
        self.interpreter = str(self.home / "tools" / "python-3.14.9+20261021-darwin-arm64" / "bin" / "python3")
        os.makedirs(os.path.dirname(self.interpreter))
        os.symlink(sys.executable, self.interpreter)                                         # 「跑 Hermes 的解释器」：用测试自己的 Python 冒充
        inner = agent / ".hermes" / "bin" / "hermes"
        script = _exe(self.home / "fake-startup.py", STARTUP % {"sp": self.sp})
        if runtime_works:
            _exe(inner, "#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(self.interpreter), shlex.quote(script)))
        else:
            _exe(inner, "#!/bin/sh\nexit 2\n")
        self.outer = _exe(tmp / "home" / ".local" / "bin" / "hermes", "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(str(inner)))
        self.sys = ES.System(env={"PATH": os.path.dirname(self.outer) + ":/usr/bin:/bin"})


@pytest.fixture
def lay(tmp_path):
    return Layout(tmp_path)


# ---------------------------------------------------------------- 找对
def test_finds_the_environment_hermes_really_imports_from_not_sh_not_the_old_venv(lay):
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env["python"] == lay.env_python and env["source"] == "hermes_runtime"
    assert env["interpreter"] == lay.interpreter                                              # 跑 Hermes 的解释器和装包的环境分开记
    assert env["matches_hermes"] is True and env["has_pip"] is False and env["hermes_path_known"] is True
    assert env["python"] not in ("/bin/sh", lay.old_python) and env["python"] != env["interpreter"]
    assert env["version"].split(".")[:1] == ["3"]
    assert ES.find_hermes_python(lay.sys, str(lay.home), lay.outer) == lay.env_python


def test_the_older_generations_and_the_old_venv_are_never_picked_when_hermes_can_say_where_it_imports_from(lay):
    older = lay.gens[0][0]
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env["python"] != older and env["python"] != lay.old_python


def test_uv_is_the_one_the_environment_was_built_with_else_the_newest(lay, tmp_path):
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env["uv"] == lay.uv_new
    os.unlink(lay.uv_new)
    env2 = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env2["uv"] == lay.uv_old                                                           # 建环境那一版没了：退到还在的
    assert ES.find_uv_in(None, str(tmp_path / "nothing")) is None


def test_hermes_unreachable_falls_back_to_the_old_paths_but_never_to_sh(tmp_path):
    lay = Layout(tmp_path, runtime_works=False)
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)                               # 启动命令问不到：sys.path 不知道，只能走旧的两条路
    assert env["hermes_path_known"] is False
    assert env["python"] == lay.old_python and env["source"] == "legacy_venv" and env["matches_hermes"] is None   # 对不对得上没法核，如实标出来
    assert env["python"] != "/bin/sh"
    assert not any(t.get("python") == "/bin/sh" and "rejected" not in t for t in env["tried"])


def test_a_launcher_whose_first_line_is_sh_is_rejected_not_used(tmp_path):
    lay = Layout(tmp_path, with_old_venv=False, runtime_works=False)                          # 今天的 hermes 脚本第一行就是 #!/bin/sh
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env["python"] is None
    assert {"source": "launcher_shebang", "python": "/bin/sh", "rejected": "not_python"} in env["tried"]
    assert ES.has_numpy(lay.sys, env["python"]) is None                                       # 找不到就是没核到，不当成有也不当成没有


def test_explicit_hermes_python_is_checked_too(lay):
    ok = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer, explicit=lay.env_python)
    assert ok["python"] == lay.env_python and ok["source"] == "explicit"
    sh = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer, explicit="/bin/sh")
    assert sh["python"] is None and sh["tried"][-1]["rejected"] == "not_python"
    gone = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer, explicit=str(lay.home / "nope" / "python"))
    assert gone["python"] is None
    old = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer, explicit=lay.old_python)      # 是 Python，但不是 Hermes 实际导入包的那套：不收
    assert old["python"] is None and old["tried"][-1]["rejected"] == "not_hermes_environment"


def test_python_older_than_3_11_is_rejected():
    s = FakeSystem(commands={"py": lambda a: (0, "", "")})
    s.py_version = (3, 10, 9)
    env = ES.find_hermes_env(s, "/nowhere", explicit="/fake/bin/py")
    assert env["python"] is None and env["tried"][-1]["rejected"] == "too_old" and env["tried"][-1]["version"] == "3.10.9"
    s.py_version = (3, 11, 0)
    assert ES.find_hermes_env(s, "/nowhere", explicit="/fake/bin/py")["python"] == "/fake/bin/py"


def test_probe_reads_back_a_real_version_and_rejects_garbage(lay):
    info = ES.probe_python(lay.sys, lay.env_python)
    assert info["version"] >= (3, 11, 0) and info["has_pip"] is False and info["site_packages"]
    assert ES.probe_python(lay.sys, "/bin/sh") is None and ES.probe_python(lay.sys, "") is None and ES.probe_python(lay.sys, None) is None
    echo = FakeSystem(commands={"py": lambda a: (0, "3.14.0\n", "")})
    echo.run = lambda argv, timeout=None: (0, "Python 3.14.0\n", "")                          # 有输出但不是我们要的回答：不算
    assert ES.probe_python(echo, "/fake/bin/py") is None


def test_parse_site_path():
    text = "sys.path = [\n    '/a',\n    '/b/lib/python3.14/site-packages',\n]\nUSER_BASE: '/x' (exists)\n"
    assert ES.parse_site_path(text) == ["/a", "/b/lib/python3.14/site-packages"]
    assert ES.parse_site_path("nothing") is None and ES.parse_site_path(None) is None and ES.parse_site_path("sys.path = [\n  'a'") is None


# ---------------------------------------------------------------- NumPy：查、装
def _record_uv(lay, make_numpy=True, rc=0):
    body = "#!/bin/sh\necho \"$@\" > %s\n" % shlex.quote(str(lay.home / "uv-called.txt"))
    if make_numpy:
        body += "mkdir -p %s/numpy && echo \"__version__ = '9.9'\" > %s/numpy/__init__.py\n" % (shlex.quote(lay.sp), shlex.quote(lay.sp))
    body += "exit %d\n" % rc
    _exe(__import__("pathlib").Path(lay.uv_new), body)


def test_numpy_is_checked_with_the_installed_environments_own_python(lay):
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert ES.has_numpy(lay.sys, env["python"]) is False
    os.makedirs(os.path.join(lay.sp, "numpy"))
    open(os.path.join(lay.sp, "numpy", "__init__.py"), "w").write("__version__ = '9.9'\n")
    assert ES.has_numpy(lay.sys, env["python"]) is True


def test_install_uses_hermes_own_uv_when_the_environment_has_no_pip(lay):
    _record_uv(lay)
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert ES.numpy_install_command(env) == [lay.uv_new, "pip", "install", "--python", lay.env_python, "numpy>=1.26"]
    assert ES.install_numpy(lay.sys, env) == (True, None)
    assert (lay.home / "uv-called.txt").read_text().split() == ["pip", "install", "--python", lay.env_python, "numpy>=1.26"]
    assert ES.has_numpy(lay.sys, env["python"]) is True


def test_install_failure_and_silent_failure_are_reported_not_taken_for_success(lay):
    _record_uv(lay, make_numpy=False, rc=0)                                                   # uv 说成功了、可是 NumPy 并没有装上
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert ES.install_numpy(lay.sys, env) == (False, "still_missing")
    _record_uv(lay, make_numpy=False, rc=7)
    assert ES.install_numpy(lay.sys, env) == (False, "exit_7")


def test_n3_install_done_but_the_check_cannot_run_is_unconfirmed_not_success_and_not_missing(lay, monkeypatch):
    _record_uv(lay)
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    monkeypatch.setattr(ES, "has_numpy", lambda sys_, python: None)                          # 装完以后查的进程起不来
    assert ES.install_numpy(lay.sys, env) == (False, "unconfirmed_after_install")


def test_no_pip_and_no_uv_means_no_install_and_says_so(lay):
    os.unlink(lay.uv_new)
    os.unlink(lay.uv_old)
    env = ES.find_hermes_env(lay.sys, str(lay.home), lay.outer)
    assert env["uv"] is None and ES.numpy_install_command(env) is None
    assert ES.install_numpy(lay.sys, env) == (False, "no_installer")


def test_pip_is_used_only_when_the_environment_really_has_pip():
    env = {"python": "/e/bin/python", "has_pip": True, "uv": "/u/uv"}
    assert ES.numpy_install_command(env) == ["/e/bin/python", "-m", "pip", "install", "numpy>=1.26"]
    assert ES.numpy_install_command(dict(env, has_pip=False)) == ["/u/uv", "pip", "install", "--python", "/e/bin/python", "numpy>=1.26"]
    assert ES.numpy_install_command({"python": None}) is None and ES.numpy_install_command(None) is None


# ---------------------------------------------------------------- 向导：计划、同意、状态
BASE = ["--config-method", "yaml", "--src-path", "/x/src"]


def _fake(numpy_present, installs, uv=True, pip=False):
    state = {"n": numpy_present}

    def py(argv):
        return (0, "2.0", "") if state["n"] else (1, "", "ModuleNotFoundError")

    def uvf(argv):
        installs.append(list(argv))
        state["n"] = True
        return 0, "", ""
    cmds = {"py": py}
    if uv:
        cmds["uv"] = uvf
    http = FakeHTTP("ollama")

    def ollama(argv):
        return 0, "", ""
    cmds["ollama"] = ollama
    s = FakeSystem(http=http, commands=cmds, env={"DEEPSEEK_API_KEY": FAKE_KEY})
    s.py_pip = pip
    return s


def _args(h, *more):
    return ["setup", "--host", "hermes", "--preset", "recommended", "--hermes-home", h, "--vectors", "ollama", "--hermes-python", "/fake/bin/py"] + BASE + list(more)


def test_plan_shows_the_full_install_command_and_the_loss_warning(tmp_path, capsys):
    h = make_home(tmp_path)
    s = _fake(False, [])
    assert cli._setup_main(_args(h), sys_=s) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "里没有 NumPy" in lines and "/fake/bin/uv pip install --python /fake/bin/py 'numpy>=1.26'" in lines
    assert "已经告诉 Hermes 原委需要 NumPy" in lines and "它以后重建环境时会自动带上" in lines and "万一还是丢了，向量会自动退回只用关键词" in lines and "再运行上面这一条命令就能装回" in lines
    assert "NumPy 会丢" not in lines and "Hermes 依赖  插件文件 plugin.yaml 里声明了需要 numpy>=1.26" in lines and "在 Hermes v0.22.0（+10397，2026.9.24）上已确认" in lines and "其他发行方式没验证" in lines and "还没在真机上确认" not in lines
    assert s.ran and not [r for r in s.ran if r[:2] == ["/fake/bin/uv", "pip"]]               # 计划阶段什么都没装


def test_plan_says_plainly_when_there_is_no_way_to_install(tmp_path, capsys):
    h = make_home(tmp_path)
    s = _fake(False, [], uv=False, pip=False)
    assert cli._setup_main(_args(h), sys_=s) == 0
    lines = "\n".join(json.loads(capsys.readouterr().out)["plan"]["lines"])
    assert "没找到往那个环境里装 NumPy 的办法" in lines and "--hermes-python" in lines
    assert cli._setup_main(_args(h, "--install-numpy", "--yes"), sys_=s) == 2                # 同意了也装不了：报出来，什么都不改
    err = capsys.readouterr().err
    assert "numpy_install_failed" in err and "no_installer" in err


def test_setup_installs_with_uv_only_after_consent(tmp_path, capsys):
    h = make_home(tmp_path)
    installs = []
    s = _fake(False, installs)
    assert cli._setup_main(_args(h, "--yes"), sys_=s) == 2                                    # 没同意：不装
    assert "numpy_not_consented" in capsys.readouterr().err and installs == []
    assert cli._setup_main(_args(h, "--install-numpy", "--yes"), sys_=s) == 0
    assert installs == [["/fake/bin/uv", "pip", "install", "--python", "/fake/bin/py", "numpy>=1.26"]]


def test_interactive_wizard_shows_the_command_and_the_warning_before_asking(tmp_path):
    h = make_home(tmp_path)
    s = _fake(False, [])
    io = FakeIO(["1", "1", "y", "y", "y", "y", "n", "1", "y", "y"])
    cli._setup_main(["setup", "--hermes-home", h, "--hermes-python", "/fake/bin/py"] + BASE, io=io, sys_=s)
    assert "/fake/bin/uv pip install --python /fake/bin/py 'numpy>=1.26'" in io.text and "已经告诉 Hermes 原委需要 NumPy" in io.text and "NumPy 会丢" not in io.text


def test_status_reports_numpy_in_the_hermes_environment_with_the_command_to_put_it_back(tmp_path, capsys):
    h = make_home(tmp_path)
    s = _fake(True, [])
    assert cli._setup_main(_args(h, "--install-numpy", "--yes"), sys_=_fake(True, [])) == 0
    capsys.readouterr()
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], sys_=s)
    ok = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert ok["has_numpy"] is True and ok["python"] == "/fake/bin/py" and ok["install_command"] is None and ok["needed_by_vectors"] is True
    lost = _fake(False, [])                                                                   # Hermes 更新重建了环境：NumPy 没了
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], sys_=lost)
    out = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert out["has_numpy"] is False and out["install_command"] == ["/fake/bin/uv", "pip", "install", "--python", "/fake/bin/py", "numpy>=1.26"] and "自动退回只用关键词" in out["note"]
    assert "Hermes 可能重建了它的包环境" in out["note"] and out["plugin_declares_numpy"] is True and "插件文件里已经声明了需要 NumPy" in out["note"]       # 声明在：说明 Hermes 下次重建会带上
    os.unlink(os.path.join(h, "plugins", "yuanwei", "plugin.yaml"))                          # 装得早／声明不在：也报出来，并说怎么办
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], sys_=lost)
    out3 = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert out3["plugin_declares_numpy"] is None and "没有找到 NumPy 的依赖声明" in out3["note"] and "重新运行 yuanwei setup" in out3["note"]
    nothing = FakeSystem(commands={})                                                         # 找不到 Python：没核到，不当成有
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h], sys_=nothing)
    out2 = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert out2["has_numpy"] is None and out2["python"] is None and "--hermes-python" in out2["note"]


def test_status_without_a_system_does_not_look_for_numpy(tmp_path):
    h = make_home(tmp_path)
    assert "numpy_in_hermes" not in HS.install_status(h)


def test_the_real_hermes_is_never_run_by_tests():
    with pytest.raises(AssertionError):
        ES.System().run(["/nonexistent-dir/hermes", "--version"])                                # conftest 的拦截：不在临时目录里的 hermes／lms／ollama，默认的 System 也不许跑
    assert ES.System().run(["/nonexistent-dir/other", "x"])[0] is None                         # 别的名字照旧（起不来返回 None）


# ================================================================ 向 Hermes 声明 NumPy 依赖（2026-11-11 晚）
def _hermes_read_python_declaration(path):
    """照 Hermes pm/plugin_declarations.py: read_python_declaration 读 plugin.yaml 那一段的逻辑（没有 pyproject 时）：pip_dependencies、python_dependencies 两个键，都得是字符串列表。"""
    import yaml
    data = yaml.safe_load(open(path, encoding="utf-8-sig").read()) or {}
    assert isinstance(data, dict)
    specs = []
    for key in ("pip_dependencies", "python_dependencies"):
        values = data.get(key, [])
        assert isinstance(values, list) and all(isinstance(v, str) for v in values), key
        specs.extend(values)
    return data, tuple(dict.fromkeys(specs))


def test_plugin_yaml_declares_numpy_in_the_format_hermes_reads_and_matches_the_vector_extra():
    import tomllib
    from yuanwei import hermes_setup as HS
    p = os.path.join(os.path.dirname(HS.__file__), "resources", "hermes_plugin.yaml")
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("没有 PyYAML：照下面的文本核")
    data, specs = _hermes_read_python_declaration(p)
    assert specs == (ES.NUMPY_REQ,) == ("numpy>=1.26",) and data["name"] == "yuanwei"
    from packaging.requirements import Requirement
    assert [Requirement(x).name for x in specs] == ["numpy"]
    root = os.path.join(os.path.dirname(HS.__file__), "..", "..")
    pyproject = tomllib.load(open(os.path.join(root, "pyproject.toml"), "rb"))
    assert pyproject["project"]["optional-dependencies"]["vector"] == list(specs)                  # 版本范围和 pyproject 的 vector 一致
    assert ES.plugin_declares_numpy(os.path.dirname(os.path.dirname(os.path.dirname(p)))) is None   # 辅助函数：目录里没有装好的插件文件 → 没核到


def test_declaration_text_is_declared_unconditionally_and_the_helper_reads_what_the_installer_wrote(tmp_path):
    from yuanwei import hermes_setup as HS
    for vectors in ("none", "lmstudio"):
        h = make_home(tmp_path / vectors, "hh")
        HS.apply_install(hermes_home=h, db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml")
        assert ES.plugin_declares_numpy(h) is True
    h2 = make_home(tmp_path / "old", "hh")
    os.makedirs(os.path.join(h2, "plugins", "yuanwei"))
    for text, want in (("name: yuanwei\n", False), ("name: yuanwei\npython_dependencies:\n  - \"numpy>=1.26\"\n", True), ("name: yuanwei\npip_dependencies:\n  - numpy\n", True),
                       ("name: yuanwei\npython_dependencies:\n  - requests\n", False), ("name: yuanwei\n# python_dependencies:\n#   - numpy\n", False)):
        open(os.path.join(h2, "plugins", "yuanwei", "plugin.yaml"), "w").write(text)
        assert ES.plugin_declares_numpy(h2) is want, text


def test_reinstall_for_a_user_installed_before_lists_plugin_yaml_as_an_update_with_a_backup(tmp_path, monkeypatch):
    from yuanwei import hermes_setup as HS
    h = make_home(tmp_path, "hh")
    old = "name: yuanwei\nversion: 0.1.0.dev0\ndescription: \"原委\"\n"
    monkeypatch.setattr(HS, "plugin_yaml_text", lambda: old)                                  # 声明之前装的那一版
    HS.apply_install(hermes_home=h, db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml")
    assert ES.plugin_declares_numpy(h) is False
    monkeypatch.undo()
    plan = HS.plan_install(hermes_home=h, db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml")
    acts = {os.path.basename(a["path"]): a for a in plan["actions"] if a["kind"] == "write_file"}
    assert "plugin.yaml" in acts and acts["plugin.yaml"]["new"] is False and "backup" in acts["plugin.yaml"]
    assert "__init__.py" not in acts                                                           # 插件主文件没变，不动
    HS.apply_install(hermes_home=h, db=None, mode="tools_only", src_path="/pkg/src", hermes_cmd=None, config_method="yaml")
    assert ES.plugin_declares_numpy(h) is True and [n for n in os.listdir(os.path.join(h, "plugins", "yuanwei")) if n.startswith("plugin.yaml.yuanwei-backup-")]


def test_status_never_reports_numpy_present_just_because_the_plugin_declares_it(tmp_path, capsys):
    h = make_home(tmp_path)
    assert cli._setup_main(_args(h, "--install-numpy", "--yes"), sys_=_fake(True, [])) == 0
    capsys.readouterr()
    assert ES.plugin_declares_numpy(h) is True                                                  # 声明在
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h, "--hermes-python", "/fake/bin/py"], sys_=_fake(False, []))       # 环境里实际没有
    lost = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert lost["plugin_declares_numpy"] is True and lost["has_numpy"] is False                 # 探测结果照实报，不被声明盖掉
    cli._status_hermes_main(["status", "hermes", "--hermes-home", h], sys_=FakeSystem(commands={}))                                 # 找不到环境：核不了
    unknown = json.loads(capsys.readouterr().out)["numpy_in_hermes"]
    assert unknown["plugin_declares_numpy"] is True and unknown["has_numpy"] is None and "没能确认" in unknown["note"]             # 「核不了」那一支还在，不说成有
