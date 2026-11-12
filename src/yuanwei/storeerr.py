"""存储层的错误类和收据查询小件（取自研究的 write_store）。"""


class StoreError(Exception):
    pass


class MessageConflict(StoreError):
    """同一消息 ID、内容不同。"""


class PublishedRange(StoreError):
    """原话序号落在已经发布过的原话范围里（序号不大于这个工作区已入库的最大序号），而且不是同一条的重送：不许往已发布的截点里补洞。补写要用实际入库时拿到的新序号。"""


class BlockedByDeletion(StoreError):
    """这条写入被删除的屏障挡住（确认删除那一轮、被删消息的旧键重送、删除前开始的迟到写入）。不是故障。"""

    def __init__(self, reason):
        super().__init__("被删除屏障挡住：%s" % reason)
        self.reason = reason


class DeletionInterrupt(StoreError):
    """整理或写入进行中发现这个工作区的删除版本变了：这一轮作废，不是失败。"""


class StaleBase(StoreError):
    """变更集基于的组织状态版本已经过期。"""


class ChangeSetInvalid(StoreError):
    pass


class KeyConflict(StoreError):
    """要新增的记录键已存在（不允许覆盖）。"""


class ReceiptExists(StoreError):
    pass


class TransactionFailed(StoreError):
    pass


class StepConflict(StoreError):
    """同一步 ID、输入指纹不同。"""


class OpConflict(StoreError):
    """同一操作 ID、内容不同。"""


class StepOrderError(StoreError):
    """步序号不是「已结束的最大步序号＋1」：后面的会话不能抢在前面还没结束（提交或放弃）的会话之前处理。"""


def find_receipt(state, key):
    for r in state["tables"]["receipts"]:
        if r["key"] == key:
            return r
    return None


def expected_k(state):
    """下一个该整理的步序号：最小的、既不在已结束的收据里、也不在已删步号里的正整数。没删除过时已结束的步序号是 1…m 的前缀，结果就是 max+1。"""
    done = set(ended_steps(state)) | set(state.get("deleted_ks") or ())
    k = 1
    while k in done:
        k += 1
    return k


def ended_steps(state):
    """已结束的步：已提交（kind=step），或已放弃、已跳过（kind=op、op=abandon_step／skip_step）。返回 {k: 收据}。"""
    out = {}
    for r in state["tables"]["receipts"]:
        if r["kind"] == "step" or (r["kind"] == "op" and r.get("op") in ("abandon_step", "skip_step")):
            out[r["k"]] = r
    return out


def abandon_receipt(state, step_id):
    for r in state["tables"]["receipts"]:
        if r["kind"] == "op" and r.get("op") == "abandon_step" and r["step_id"] == step_id:
            return r
    return None
