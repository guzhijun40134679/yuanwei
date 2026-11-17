"""相关候选资格与说明。真实标定按模型层登记，运行身份仍逐个认证；缓存不跨 vector_fp 复用。"""
import hashlib
import math

from . import vectors as VX

MODEL_FIELDS = ("model_file_sha256", "dimension", "normalization", "prefix", "template", "text_processing")
# 2026-11-17：LM Studio 2.76.0 本轮拟合并一次留出通过。
# 自带 b11701 同一模型文件，按用户明确裁定沿用；运行程序本轮未实测。
# 规则（11-17 用户定）：model_key 在表里（同一个模型文件、同样的输入处理）就沿用门槛，不管运行程序是哪个；
# 完整身份在 vector_fps 名单里的按名单上的基准（measured／same_file_authorized_unmeasured），不在名单里的标 same_file_unlisted_runtime，状态里都注明「未实测」。
CALIBRATIONS = {'4023a4a74be5db96331d5cb09e9f5f10a20c28f06ee1b4c2460eafe577cb375e': {'threshold': 0.4319852256245753, 'vector_fps': ['50c6c118cbac3b20b7f90ff79e0403296bd57440efb8fe5067459f97093681be', 'b5251f11275ed2dd048531457b47e59237e0b3aa5e9729cbe140fbfadc7b29a7'], 'runtime_basis': {'50c6c118cbac3b20b7f90ff79e0403296bd57440efb8fe5067459f97093681be': 'measured', 'b5251f11275ed2dd048531457b47e59237e0b3aa5e9729cbe140fbfadc7b29a7': 'same_file_authorized_unmeasured'}, 'evidence': '345afe6441ca73774834b89e3ebf5258c2b7b7f80362caf08f58b618d158e14a', 'run_id': 'floor31-9c0d7fc111a84fb893477476b10b0b64'}}
UNCALIBRATED_MESSAGE = "语义阈值未标定：这个向量模型没标定，换了说法的内容暂时找不到；当前只保留关键词命中。"
RECOMMENDATION = "可以换回已标定的推荐向量模型及运行程序。"


def model_key(identity):
    VX.check_identity(identity)
    return hashlib.sha256(VX.canon({k: identity[k] for k in MODEL_FIELDS}).encode("utf-8")).hexdigest()


def keyword_status():
    return {"status": "keyword", "threshold": None, "model_key": None, "vector_fp": None, "message": "只用关键词检索", "recommendation": None}


def off_status():
    """相关度下限已关（配置 relevance_floor＝false）：恢复小修 31 之前的取材，不筛候选、不带来源说明；只给研究回放和旧参照用。"""
    return {"status": "off", "threshold": None, "model_key": None, "vector_fp": None, "message": "相关度下限已关（旧参照取材）", "recommendation": None}


def resolve(identity, vector_fp=None, registry=None):
    if identity is None:
        return {"status": "uncalibrated", "threshold": None, "model_key": None, "vector_fp": vector_fp, "message": UNCALIBRATED_MESSAGE, "recommendation": RECOMMENDATION}
    fp = VX.identity_fp(identity)
    if vector_fp is not None and fp != vector_fp:
        raise VX.VectorIdentityMismatch("相关度标定身份与本次 vector_fp 不一致")
    key = model_key(identity)
    record = (CALIBRATIONS if registry is None else registry).get(key)
    result = {"status": "uncalibrated", "threshold": None, "model_key": key, "vector_fp": fp, "message": UNCALIBRATED_MESSAGE, "recommendation": RECOMMENDATION}
    if not record:
        return result
    floor = record.get("threshold")
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor) or not -1 <= floor <= 1:
        raise ValueError("相关度标定门槛必须是 [-1, 1] 的有限数值")
    if not record.get("evidence"):
        raise ValueError("相关度标定缺少证据指纹")
    if fp in record.get("vector_fps", ()):
        basis = record.get("runtime_basis", {}).get(fp, "measured")
    else:
        basis = "same_file_unlisted_runtime"                          # 同一个模型文件（model_key 对得上），运行程序不在登记名单里：沿用门槛，注明未实测
    message = "沿用同模型文件的语义阈值；当前运行程序未实测" if basis in ("same_file_authorized_unmeasured", "same_file_unlisted_runtime") else "语义阈值已标定"
    return dict(result, status="calibrated", threshold=float(floor), message=message, recommendation=None, basis=basis)


def merge_reason(current, incoming):
    """多锚点/多路查询合并说明；关键词资格优先，其次保留最大余弦。"""
    if current is None:
        return dict(incoming)
    if current["kind"] == "keyword" and incoming["kind"] != "keyword":
        return current
    if incoming["kind"] == "keyword" and current["kind"] != "keyword":
        return dict(incoming)
    a, b = current.get("score"), incoming.get("score")
    return dict(incoming) if b is not None and (a is None or b > a) else current


def label(reason):
    if not reason:
        return ""
    kind = reason["kind"]
    if kind == "keyword":
        return "关键词命中"
    if kind == "semantic":
        return "仅意思相近｜余弦 %.4f" % reason["score"]
    if kind == "neighbor":
        return "附带邻近材料（关联 %s）" % reason["parent"]
    if kind == "occasion":
        return "附带同回材料（关联 %s）" % reason["parent"]
    raise ValueError("未知的材料来源类别")


def quote_prefix(row, role):
    parts = [role]
    if row.get("retrieval_reason"):
        parts.append(label(row["retrieval_reason"]))
    parts.append(row["msg_id"])
    if row.get("redacted"):
        parts.append("挡掉 %d 处" % row["redacted"])
    return "〔" + "｜".join(parts) + "〕"
