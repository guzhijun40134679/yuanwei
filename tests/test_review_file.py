"""主会话（补充五之三验收）：待核文件自己写到一半（半个汉字、没有结尾换行）时，状态查询不报错，下一条待核记录不接在残行后面。"""
import json
import os

from helpers import Clock, Scripted
from yuanwei import Config, Memory


def open_mem(tmp_path):
    return Memory.open(str(tmp_path / "m.sqlite"), "w", config=Config(index_version=2), provider=Scripted("skip"), clock=Clock(), spawn="off")


def half_written_review(m):
    os.makedirs(m.config_dir, exist_ok=True)
    rec = json.dumps({"reason": "X", "at": "t", "line": "合同的事"}, ensure_ascii=False).encode("utf-8")
    cut = rec[: rec.index("合".encode("utf-8")) + 2]
    with open(m._review_path(), "wb") as f:
        f.write(cut)
    return cut


def test_status_survives_a_half_written_review_line(tmp_path):
    m = open_mem(tmp_path)
    half_written_review(m)
    assert m.status()["pending_review"] == 1


def test_next_review_record_does_not_glue_onto_a_half_written_line(tmp_path):
    m = open_mem(tmp_path)
    cut = half_written_review(m)
    m._write_pending({"x": 1})                               # 程序没能接收的项，会被挪进待核
    assert m.record("h", "S1", "m2", "assistant", "好的", time="2027-01-01")["raw_saved"] is True
    lines = [l for l in open(m._review_path(), "rb").read().split(b"\n") if l]
    assert lines[0] == cut                                   # 残行原样留着
    assert len(lines) == 2 and json.loads(lines[1].decode("utf-8"))["line"] == '{"x": 1}'
    assert m.status()["pending_review"] == 2
