"""包内资源（提示词、编码映射表）。加载时核 SHA-256，对不上就报错。"""
import hashlib
from importlib import resources as _res

from .errors import ConfigError

WRITE_PROMPTS = {
    "A": ("write-prompt-A.txt", "dfad13f7f7bb63d1578e057cad879606b0482f8504f209813fcbcce667f90cba"),
    "B0": ("write-prompt-B0.txt", "d9fe10729c83997045eeba7aed23423971e144a018d45e6dc409afb65e76cc04"),
}
ANSWER_R1 = ("answer-prompt-R1.txt", "3430504727e56056d6b9d1724d03ce4539fdd1f6c26c6a7c5a20da971f697546")
ANSWER_ADDENDUM = ("answer-prompt-addendum.txt", "6c83aa624b696004c1f47e70ee7d42d5d8c9f15bed3c26d43e0dce6eb4a356d3")


def read_bytes(name):
    return (_res.files("yuanwei") / "resources" / name).read_bytes()


def read_pinned(name, sha):
    raw = read_bytes(name)
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise ConfigError("资源 %s 的 SHA-256 是 %s…，登记的是 %s…" % (name, got[:12], sha[:12]))
    return raw.decode("utf-8")


def write_prompt(version):
    """返回 (提示词原文, sha256)。"""
    if version not in WRITE_PROMPTS:
        raise ConfigError("不认识的写入提示词版本 %r（只有 %s）" % (version, "、".join(WRITE_PROMPTS)))
    name, sha = WRITE_PROMPTS[version]
    return read_pinned(name, sha), sha


def answer_system():
    """回答系统提示 ＝ R1 回答提示 ＋ 闭环加句。"""
    return read_pinned(*ANSWER_R1) + read_pinned(*ANSWER_ADDENDUM)
