"""小件：估算词元、二字组、提交后按收据恢复。"""
import math

from . import storeerr as WS

TOKEN_RATIO_PERCENT = 52  # 尺子 1：ceil(0.52 × 码点数)，用整数算避免浮点误差

# ---- 尺子 2（2026-11-17）：按字符类加权，整数千分比，最后向上取整。
#   甲：汉字、假名、全角符号和一切「别的字符」（表情、别的文字）：520‰，和尺子 1 的单字权重一样；
#   乙：拉丁字母、数字、英文标点、空白（U+0000–U+024F）：177‰（字母数字和标点空白用同一个系数，下面写成两个名字是留着以后拆开用的）；
# 系数由 修尺子/ 的校准定下（只在拟合那一半非评测语料上、照施工单 1.4 节的规则；锁定指纹 781359ae…），不是任何模型的上下文窗口保证，只是选材用的估算。
RULER_LATEST = 2
W_A = 520                    # 甲，千分比
W_ALNUM = 177                # 乙里的字母数字，千分比
W_PUNCT = 177                # 乙里的标点和空白，千分比
_LATIN_MAX = "\u024f"


def _b_counts(text):
    """（乙里的字母数字个数，乙里的标点空白个数，甲的个数）。"""
    n = len(text)
    if text.isascii():
        na = sum(1 for ch in text if ch.isalnum())
        return na, n - na, 0
    na = nb = 0
    for ch in text:
        if ch <= _LATIN_MAX:
            if ch.isalnum():
                na += 1
            else:
                nb += 1
    return na, nb, n - na - nb


def ruler2_weight(text):
    """尺子 2 的加权和（千分比，还没向上取整）。"""
    if W_ALNUM == W_PUNCT:                                  # 现在两个系数相等：不用再分字母数字和标点空白
        n = len(text)
        nb = n if text.isascii() else sum(1 for ch in text if ch <= _LATIN_MAX)
        return W_ALNUM * nb + W_A * (n - nb)
    na, nb, nc = _b_counts(text)
    return W_ALNUM * na + W_PUNCT * nb + W_A * nc


def est_tokens(text, ruler=1):
    """估算词元。ruler＝1：ceil(0.52 × 码点数)（取自研究的 est_tokens，原样保留）；ruler＝2：尺子 2，向上取整。"""
    if ruler == 2:
        return (ruler2_weight(text) + 999) // 1000
    if ruler != 1:
        raise ValueError("不认识的尺子版本 %r" % (ruler,))
    n = len(text)
    return (TOKEN_RATIO_PERCENT * n + 99) // 100


def est_tokens_line(text, ruler=1):
    """一行的估算 token：文字加一个换行（行末换行）；ruler 1 是 ceil(0.52 × (字数＋1))。就是「把一行当 text＋\\n 交给 est_tokens」。"""
    return est_tokens(text + "\n", ruler)


def est_lines(lines, ruler=1):
    """一个单元占几行，估算词元是各行之和，不是拼起来再算。"""
    return sum(est_tokens_line(x, ruler) for x in lines)


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
