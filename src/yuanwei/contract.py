"""原话与片段的数据契约（取自研究的 write_contract，只留产品用到的部分：规范 JSON、指纹、命名空间标记、消息合同、码点区间核对）。
位置约定：start/end 是 Unicode 码点下标，区间 [start, end)。"""
import hashlib
import json


ROLES = ("user", "assistant")


class ContractError(ValueError):
    def __init__(self, msg, code="contract"):
        super().__init__(msg)
        self.code = code


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def ns_tag(ns):
    return sha256_hex(ns)[:6]


def fragment_id(ns, msg_id, start, end):
    return "fr-" + sha256_hex("%s|%s|%d|%d" % (ns, msg_id, start, end))[:12]


def message_fingerprint(msg):
    """单条不可变消息的指纹：只看这条消息自己的字段；接收时间不算（重放时它会变，原话没变）。"""
    return sha256_hex(canonical({k: msg.get(k) for k in ("ns", "msg_id", "session_id", "seq", "role", "msg_time", "content")}))


def make_message(ns, session_id, seq, msg_id, role, content, msg_time=None, received_at=None):
    m = {"ns": ns, "msg_id": msg_id, "session_id": session_id, "seq": seq, "role": role,
         "content": content, "msg_time": msg_time, "received_at": received_at}
    m["fingerprint"] = message_fingerprint(m)
    return m


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def check_message(msg):
    if not isinstance(msg, dict):
        raise ContractError("消息必须是对象")
    for k in ("ns", "msg_id", "session_id", "role", "content", "seq", "fingerprint"):
        if k not in msg:
            raise ContractError("消息缺少字段 %s" % k)
    for k in ("ns", "msg_id", "session_id"):
        if not _nonempty_str(msg[k]):
            raise ContractError("%s 必须是非空字符串" % k)
    if msg["role"] not in ROLES:
        raise ContractError("role 必须是 %s" % (ROLES,))
    if not isinstance(msg["content"], str) or msg["content"] == "":
        raise ContractError("content 必须是非空字符串")
    if not isinstance(msg["seq"], int) or isinstance(msg["seq"], bool) or msg["seq"] < 0:
        raise ContractError("seq 必须是非负整数")
    if msg.get("msg_time") is not None and not _nonempty_str(msg["msg_time"]):
        raise ContractError("msg_time 缺失就必须是 None（保持未知），不能是空串")
    if not _nonempty_str(msg.get("received_at")):
        raise ContractError("received_at 必须由注入的时钟给出")
    if msg["fingerprint"] != message_fingerprint(msg):
        raise ContractError("指纹与内容不符", code="fingerprint")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_span(content, start, end, text):
    if not (_is_int(start) and _is_int(end)):
        raise ContractError("start/end 必须是整数", code="bad_span")
    if not (0 <= start < end <= len(content)):
        raise ContractError("区间 [%r,%r) 不在 0..%d 内" % (start, end, len(content)), code="bad_span")
    if not isinstance(text, str) or content[start:end] != text:
        raise ContractError("消息 [%d,%d) 取出 %r，与判断给出的原文 %r 不同（位置按 Unicode 码点）" % (start, end, content[start:end], text),
                            code="span_mismatch")
