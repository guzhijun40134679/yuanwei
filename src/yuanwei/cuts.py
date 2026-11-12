"""截点（一次读取的范围）的两种格式。

旧格式（第一段起）：{"vclock", "org_seq", "index_version"}——原话按 visible_at ≤ vclock 读（它所属的步结束时才有 visible_at）；整理按 created_seq ≤ org_seq。
新格式（cut_format = 2）：{"cut_format": 2, "ns", "raw_seq", "org_seq", "index_version"}——原话按 seq ≤ raw_seq 读（入库提交成功就算，不等整理）；整理按 created_seq ≤ org_seq；
两个上界在同一条工作区记录、同一次查询里取。新格式里没有 vclock：不借这个字段名装原话序号；vclock 只留给旧格式和步的结束记录。
旧格式不换算成「当时最大的消息序号」：visible_at 不是按序号连续发布的（后面的会话先结束，前面还没结束的原话就不在里面），换算会把历史范围改掉。
"""
from .errors import BadCut

FORMAT = 2


def is_v2(cut):
    return isinstance(cut, dict) and cut.get("cut_format") == FORMAT


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_shape(cut):
    """只查形状，不查库。格式不认识、缺键、类型不对都是 BadCut。旧格式（没有 cut_format）只要求 vclock、org_seq 是整数。"""
    if not isinstance(cut, dict):
        raise BadCut("截点必须是一个字典")
    fmt = cut.get("cut_format")
    if fmt is None:
        if not (_int(cut.get("vclock")) and _int(cut.get("org_seq"))):
            raise BadCut("旧格式截点要有整数的 vclock 和 org_seq")
        return
    if fmt != FORMAT:
        raise BadCut("不认识的截点格式 %r" % (fmt,))
    for k in ("raw_seq", "org_seq", "index_version"):
        if not _int(cut.get(k)) or cut[k] < 0:
            raise BadCut("新格式截点的 %s 要是不小于 0 的整数，实际 %r" % (k, cut.get(k)))
    if not isinstance(cut.get("ns"), str) or not cut["ns"]:
        raise BadCut("新格式截点要写明工作区（ns）")


def label(cut):
    """给人看的截点标签。新格式 r<原话上界>·o<整理上界>；旧格式 v<vclock>·o<整理上界>。"""
    if is_v2(cut):
        return "r%d·o%d" % (cut["raw_seq"], cut["org_seq"])
    return "v%d·o%d" % (cut["vclock"], cut["org_seq"])


def directory_version(cut):
    """目录版本：新格式只跟整理上界走（只多了没整理的原话，目录内容不变，不用因此重复注入）；旧格式照旧。"""
    if is_v2(cut):
        return "dir-o%d" % cut["org_seq"]
    return "dir-v%d-o%d" % (cut["vclock"], cut["org_seq"])
