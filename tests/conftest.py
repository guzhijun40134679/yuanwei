import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


import pytest  # noqa: E402

from yuanwei import hermes_workspace as _HW  # noqa: E402

REAL_LOCAL_PROCESS_CWD = _HW.local_process_cwd
REAL_HOME = os.path.expanduser("~")                       # 导入时（还没被测试隔离改掉 HOME）的真实家目录：测试里只用来断言「没有碰它」


@pytest.fixture(autouse=True)
def _no_real_process_cwd(monkeypatch):
    """经典终端没给 cwd 时接头会取进程目录；测试里不许依赖跑测试那一刻的真实目录，默认当作「取不到」，要用的测试自己给。"""
    monkeypatch.setattr(_HW, "local_process_cwd", lambda: None)


@pytest.fixture(autouse=True)
def _never_run_the_real_hermes_or_lms(monkeypatch):
    """铁律：测试不跑真的 hermes、lms、ollama。默认的 System 在真实 PATH 上找命令；这里拦住：命令是这几个名字、又不在临时目录里，就直接报错。"""
    import tempfile

    from yuanwei import embedsetup as ES
    real_run = ES.System.run
    tmp_root = os.path.realpath(tempfile.gettempdir())

    def guarded(self, argv, timeout=None):
        exe = str(argv[0])
        if os.path.basename(exe) in ("hermes", "lms", "ollama") and not os.path.realpath(exe).startswith(tmp_root):
            raise AssertionError("测试不许跑真的 %s：%s" % (os.path.basename(exe), exe))
        return real_run(self, argv, timeout)
    monkeypatch.setattr(ES.System, "run", guarded)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path_factory):
    """测试里的 ~ 指向一个空的临时目录：任何地方用 ~ 找 ~/.hermes、~/.lmstudio、~/.yuanwei 都碰不到真的。要用的测试自己再设 HOME。"""
    h = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("YUANWEI_KEY_DIR", raising=False)                  # 全局密钥目录也只能落在这个临时 HOME 里（~/.yuanwei/keys），不许被真实环境里的设置指到别处
