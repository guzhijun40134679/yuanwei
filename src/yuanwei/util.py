"""小件：估算词元、二字组、提交后按收据恢复。"""
import math

from . import storeerr as WS

TOKEN_RATIO_PERCENT = 52  # ceil(0.52 × 码点数)，用整数算避免浮点误差


def est_tokens(text):
    """估算词元 ＝ ceil(0.52 × 码点数)（取自研究的 est_tokens）。"""
    n = len(text)
    return (TOKEN_RATIO_PERCENT * n + 99) // 100


def est_tokens_line(text):
    """一行的估算 token：ceil(0.52 × (字数＋1))。字数按 Python 的 len；
    ＋1 是行末换行。这就是「把一行当 text＋\\n 交给甲的 est_tokens」。"""
    return est_tokens(text + "\n")


def est_lines(lines):
    """一个单元占几行，估算词元是各行之和，不是拼起来再算。"""
    return sum(est_tokens_line(x) for x in lines)


def bigrams(text):
    out, run = set(), []
    for ch in text + "\0":
        if ch.isalnum():
            run.append(ch.lower())
        else:
            for i in range(len(run) - 1):
                out.add(run[i] + run[i + 1])
            run = []
    return out


def commit_with_recovery(store, ns, cs, receipt_key, same_input):
    """提交；结果拿不准（抛了错）时按收据键查一次再决定。返回 (status, receipt, error)。"""
    try:
        return "committed", store.commit(cs), None
    except (WS.StaleBase, WS.ChangeSetInvalid, WS.KeyConflict, WS.ReceiptExists) as e:
        # 这些是提交前就能确定「没写」的拒绝；但收据已存在的话要认出来
        if isinstance(e, WS.ReceiptExists):
            rc = WS.find_receipt(store.read_state(ns), receipt_key)
            if rc is not None and same_input(rc):
                return "recovered", rc, None
        return "commit_failed", None, e
    except WS.DeletionInterrupt:                           # 整理途中发生了删除：不是「结果不明」，原样往上抛，这一步作废
        raise
    except Exception as e:      # 响应丢失、事务出错：不知道有没有生效
        rc = WS.find_receipt(store.read_state(ns), receipt_key)
        if rc is not None and same_input(rc):
            return "recovered", rc, e
        if rc is not None:
            raise WS.StepConflict("收据 %r 存在但输入不同" % receipt_key) from e
        return "commit_failed", None, e
