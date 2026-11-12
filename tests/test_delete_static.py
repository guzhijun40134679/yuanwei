"""R2 的静态守卫（现在就跑）：「只增不删」的约定的唯一例外是删除模块。除 yuanwei/deletion.py 以外，源码里任何地方都不能对原话、锚点、整理结果、步、绑定、向量发
DELETE，也不能 UPDATE 整理结果和原话正文。以后有人在别处悄悄加了这样的语句，这里就红。
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "yuanwei")
ALLOWED_FILES = {"deletion.py"}
FORBIDDEN = [
    re.compile(r"DELETE\s+FROM\s+(messages|anchors|org_rows|steps|bindings|turn_messages|vectors)\b", re.I),
    re.compile(r"UPDATE\s+org_rows\b", re.I),
    re.compile(r"UPDATE\s+messages\s+SET\s+(content|role|msg_time|fingerprint|host_msg_id)\b", re.I),
    re.compile(r"UPDATE\s+anchors\b", re.I),
    re.compile(r"DROP\s+TABLE\s+(messages|anchors|org_rows|steps|bindings|judgments)\b", re.I),
]


def scan(root):
    bad = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if not f.endswith(".py") or f in ALLOWED_FILES:
                continue
            p = os.path.join(dp, f)
            src = open(p, encoding="utf-8").read()
            for rx in FORBIDDEN:
                for m in rx.finditer(src):
                    bad.append((os.path.relpath(p, root), m.group(0)))
    return bad


def test_only_the_deletion_module_may_delete_or_rewrite_stored_history():
    assert scan(SRC) == []


def test_the_scanner_itself_catches_each_forbidden_statement(tmp_path):
    """把缺陷放回去它会亮：对每一种被禁的语句，放进一个临时文件，扫描器必须报出来。"""
    samples = ["DELETE FROM messages WHERE ns=?", "delete  from org_rows", "UPDATE org_rows SET data=?", "UPDATE messages SET content=''", "UPDATE anchors SET text=''",
               "DROP TABLE judgments", "DELETE FROM vectors WHERE fp=?", "DELETE FROM steps", "DELETE FROM bindings", "DELETE FROM turn_messages"]
    for i, s in enumerate(samples):
        d = tmp_path / ("t%d" % i)
        d.mkdir()
        (d / "other.py").write_text('c.execute("%s")\n' % s, encoding="utf-8")
        assert scan(str(d)), s
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "deletion.py").write_text('c.execute("DELETE FROM messages")\n', encoding="utf-8")
    assert scan(str(tmp_path / "ok")) == []                             # 删除模块自己放行


def test_known_non_history_deletes_are_not_flagged():
    """现有的 DELETE（判断记录重新编号、游标、读取登记等）不在禁止范围里：清单里本来就写了它们。"""
    for s in ("DELETE FROM judgments WHERE", "DELETE FROM read_log WHERE", "DELETE FROM current_turn", "DELETE FROM read_settings"):
        assert not any(rx.search(s) for rx in FORBIDDEN), s
