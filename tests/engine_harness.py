"""测试用的守护者入口（子进程）：从 YUANWEI_TEST_PINS 读假的钉住表，从 YUANWEI_TEST_CFG 读 Supervisor 的调参，然后跑守护者。生产的入口（yuanwei vectors serve）没有这两个口子。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
from yuanwei import enginepkg as EP  # noqa: E402
from yuanwei import enginerun as ER  # noqa: E402

pins = json.load(open(os.environ["YUANWEI_TEST_PINS"]))
cfg = json.loads(os.environ.get("YUANWEI_TEST_CFG", "{}"))
env_extra = json.loads(os.environ.get("YUANWEI_TEST_CHILD_ENV", "{}"))
paths = EP.Paths(sys.argv[1])
paths.ensure_dirs()
if "restart_delays" in cfg:
    cfg["restart_delays"] = tuple(cfg["restart_delays"])
ER.Supervisor(paths, pins=pins, cfg=cfg, env_extra=env_extra).run()
