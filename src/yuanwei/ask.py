"""问模型一次：复用同一任务指纹的合格输出、带理由重问、按存储里的调用记录数限制发送次数（取自研究的 layer_step）。
judge 由调用方注入；网络类失败抛 RetryableNetworkError。"""
import json

from . import contract as WC


class RetryableNetworkError(Exception):
    """可重试的网络错误：占一次发送，重发时不带拒收理由。judge 抛别的异常算普通出错（model_failed）。"""


def feedback_text(reasons):
    return "\n\n【上一次输出没有被收下，原因】\n%s\n请改正后重新给出完整的 JSON 对象。" % "\n".join("- " + r for r in reasons)


def ask(store, ns, step_id, question, req, task_fp, convert, judge, config, base_seq, k, session_id, fb_builder=None, fb_version=None):
    """fb_builder(reasons, 上一次的原始输出) → 重问时附的反馈文字；不给＝feedback_text（v1）。fb_version 只在新反馈版本时给，记进请求和判断记录。
    返回 {"status": "ok"|"failed"|"model_failed", "result": 转换结果, "reused", "no": 通过的那条记录号, "reasons", "error"}。"""
    recs = store.list_judgments(ns, step_id, question)
    for rec in reversed(recs):
        v = rec.get("validation") or {}
        if rec.get("task_fp") == task_fp and v.get("ok") and isinstance(rec.get("raw_output"), str):
            r = convert(rec["raw_output"])
            if r.ok:
                return {"status": "ok", "result": r, "reused": True, "no": rec["attempt"], "reasons": [], "error": None}
    last_reasons, error = [], None
    while True:
        recs = store.list_judgments(ns, step_id, question)
        if len(recs) >= config.max_sends:
            if recs:
                last = recs[-1]
                last_reasons = (last.get("validation") or {}).get("reasons") or []
                error = last.get("error")
            return {"status": "failed", "result": None, "reused": False, "no": None, "reasons": last_reasons, "error": error}
        feedback = None
        if recs:
            last = recs[-1]
            lv = last.get("validation") or {}
            if last.get("task_fp") == task_fp and last.get("status") == "rejected" and lv.get("reasons"):
                feedback = list(lv["reasons"])
        request = dict(req)
        if feedback and fb_builder is not None:
            request["text"] = req["text"] + fb_builder(feedback, last.get("raw_output"))
        else:
            request["text"] = req["text"] + (feedback_text(feedback) if feedback else "")
        request["feedback"] = feedback
        if fb_version is not None:
            request["feedback_version"] = fb_version
        request["task_fp"] = task_fp
        request["request_fp"] = WC.sha256_hex(request["text"])
        request["step_id"] = step_id
        record = {"task_fp": task_fp, "request_fp": request["request_fp"], "request": request, "feedback": feedback,
                  "status": "requested", "raw_output": None, "validation": None, "error": None,
                  "base_seq": base_seq, "k": k, "step_id": step_id, "session_id": session_id}
        if fb_version is not None:
            record["feedback_version"] = fb_version
        no = store.save_judgment(ns, step_id, question, record)
        try:
            raw = judge(request)
        except RetryableNetworkError as e:
            store.update_judgment(ns, step_id, question, no, {"status": "network_error", "error": repr(e)})
            error = repr(e)
            continue
        except Exception as e:
            store.update_judgment(ns, step_id, question, no, {"status": "model_failed", "error": repr(e)})
            return {"status": "model_failed", "result": None, "reused": False, "no": no, "reasons": [], "error": repr(e)}
        raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        store.update_judgment(ns, step_id, question, no, {"raw_output": raw_text, "status": "output_received"})
        r = convert(raw_text)
        if r.ok:
            store.update_judgment(ns, step_id, question, no, {"validation": {"ok": True, "reasons": [], "notes": r.notes}, "status": "validated"})
            return {"status": "ok", "result": r, "reused": False, "no": no, "reasons": [], "error": None}
        store.update_judgment(ns, step_id, question, no, {"validation": {"ok": False, "reasons": r.problems, "notes": r.notes}, "status": "rejected"})
        last_reasons = r.problems
