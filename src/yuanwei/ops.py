"""跳过一步（skip_step）：整步都是超长条时不调模型，用一张「跳过」收据结束这一步。不是模型失败，不算放弃，不计入连续放弃。
形状同放弃一步（只写这张收据；k 必须是下一个步序号；同一操作重放幂等，同 ID 异内容报 OpConflict）。新写的，研究里没有对应的操作。"""
from . import contract as WC
from . import storeerr as WS
from . import util as U


def _fp(obj):
    return WC.sha256_hex(WC.canonical(obj))


def apply_skip(store, ns, step_id, session_id, k, long_msg_ids, reason, clock):
    op = {"op": "skip_step", "op_id": "skip-" + step_id, "operator": "auto", "step_id": step_id, "session_id": session_id, "k": k, "reason": reason,
          "msg_fp_digest": _fp(sorted(long_msg_ids))}
    st = store.read_state(ns)
    rc = WS.find_receipt(st, "op:" + op["op_id"])
    if rc is not None:
        if rc["op_fp"] != _fp(op):
            raise WS.OpConflict("操作 ID %r 已用于另一个内容不同的操作" % op["op_id"])
        return {"status": "replayed", "receipt": rc}
    expected_k = WS.expected_k(st)
    if k != expected_k:
        raise WS.StepOrderError("步序号 %r 不对：已结束的最大步序号是 %d，下一步必须是 %d" % (k, expected_k - 1, expected_k))
    seq = st["seq"] + 1
    receipt = {"key": "op:" + op["op_id"], "kind": "op", "op": "skip_step", "op_id": op["op_id"], "op_fp": _fp(op), "step_id": step_id, "session_id": session_id,
               "k": k, "reason": reason, "operator": "auto", "base_seq": st["seq"], "commit_seq": seq, "created_seq": seq, "msg_fp_digest": op["msg_fp_digest"]}
    cs = {"ns": ns, "expected_prev_seq": st["seq"], "new_seq": seq, "inserts": {"receipts": [receipt]}, "updates": []}
    status, rcpt, err = U.commit_with_recovery(store, ns, cs, "op:" + op["op_id"], lambda r: r["op_fp"] == _fp(op))
    if status == "commit_failed":
        return {"status": "commit_failed", "receipt": None, "error": repr(err)}
    return {"status": "committed", "receipt": rcpt}
