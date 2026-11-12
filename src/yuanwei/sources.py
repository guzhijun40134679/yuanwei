"""来源核对（补充施工单·乙 §2）：这一条用户输入是不是用户本人从宿主的用户入口提交的。**只用真机记录里看到过的字段名**，字段缺了、版本没核过、读不出来，一律不放行（verdict＝fail），
不沿用以前的 human 结果，不跳过坏行。这是对受信宿主记录的兼容解析，不是公开的稳定接口，也不是对自然人的身份认证。

* Claude Code：记录文件（钩子 transcript_path 指的那份）里每条 user 消息带 turnOrigin、origin.kind，promptId 等于钩子的 prompt_id。
  已核版本：桌面 2.1.317。命令行 2.1.300 的那几次核对用了 --no-session-persistence，没有可持久核对的记录，不在白名单里。
  钩子事件和记录条目之间**没有共同的消息编号**：这里用内容指纹（辅助，不单独证明来源）把钩子收到的每条输入对到记录条目，一字不差的两条按出现的先后一一对应；
  指纹对不上（记录里的正文被包装过之类，没有在真机上核过相等）就一直对不上，到时限算失败——偏保守，不放行。
* Codex：记录里每轮开头的 task_started 带 turn_attribution，MCP 调用的 _meta 里带 x-codex-turn-metadata；官方源码写明一轮里追加的输入单独记来源、不改这一轮的 turn_trigger，
  所以一轮的触发方式证明不了这一轮后来的每条输入。没有逐条证据时只支持「这一轮只有一条输入」。记录文件的布局没有在这一步读，本模块吃已经取出来的字段。
"""
import json

from .access import Access

CC_SOURCE_VERSIONS = frozenset(["2.1.317"])                    # Claude Code 桌面
CODEX_SOURCE_VERSIONS = frozenset(["0.167.0-alpha"])           # Codex 桌面（composer 只在这里见过）

# ---------------------------------------------------------------- Claude Code
_ERR_PARTIAL = "partial_line"


def cc_transcript_path_ok(path, session_id):
    """记录路径只来自可信钩子：绝对路径、文件名正好是 <会话号>.jsonl。不扫别的会话找「像的」。"""
    import os
    return isinstance(path, str) and isinstance(session_id, str) and bool(session_id) and os.path.isabs(path) and os.path.basename(path) == session_id + ".jsonl"


def _cc_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in content):
        return "\n".join(b["text"] for b in content)
    return None


def cc_entries(raw, prompt_id, engine_version):
    """从记录文件的全文里取出这一轮（promptId）的用户消息条目，按出现顺序。返回 {"ok": True, "entries": [...]} 或 {"ok": False, "error": ..., "pending": 是不是「还没写完、以后再看」}。
    条目＝{"fp", "verdict": human|other|fail, "label"}。人＝turnOrigin 和 origin.kind 都是 human；都有、都不是 human＝other；缺字段或互相矛盾＝fail。
    坏行（中间的）和版本没核过都是失败；最后一行没写完（没有结尾换行且解析不了）算「待核」，以后再看。"""
    if engine_version not in CC_SOURCE_VERSIONS:
        return {"ok": False, "error": "version_unsupported", "pending": False}
    if not isinstance(raw, str) or not isinstance(prompt_id, str) or not prompt_id:
        return {"ok": False, "error": "bad_input", "pending": False}
    lines = raw.split("\n")
    complete = raw.endswith("\n")
    if complete:
        lines = lines[:-1]
    entries = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        last_partial = (i == len(lines) - 1) and not complete
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                raise ValueError("not an object")
        except Exception:                                                              # noqa: BLE001
            return {"ok": False, "error": _ERR_PARTIAL if last_partial else "bad_line", "pending": last_partial}
        if e.get("type") != "user" or e.get("promptId") != prompt_id:
            continue
        content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue                                                                   # 工具结果也是 user 条目，不是用户的输入
        text = _cc_text(content)
        to, o = e.get("turnOrigin"), e.get("origin")
        kind = o.get("kind") if isinstance(o, dict) else None
        if text is None:
            entries.append({"fp": "", "verdict": "fail", "label": "content_shape"})
            continue
        fp = Access.text_fp(text)
        if not isinstance(to, str) or not isinstance(kind, str):
            entries.append({"fp": fp, "verdict": "fail", "label": "fields_missing"})
        elif to == "human" and kind == "human":
            entries.append({"fp": fp, "verdict": "human", "label": "human"})
        elif to != "human" and kind != "human":
            entries.append({"fp": fp, "verdict": "other", "label": to})
        else:
            entries.append({"fp": fp, "verdict": "fail", "label": "labels_conflict"})
    return {"ok": True, "entries": entries}


def match_inputs(inputs, entries):
    """把已登记的输入（按登记先后）和记录条目按内容指纹一一对应：同一个指纹的第 k 个输入对第 k 个条目。
    返回 (verdicts, unmatched)：对上的给判定；没对上的输入不给判定（还在待核）；没被任何输入对上的条目数＝unmatched（记录里有我们没收到的输入）。"""
    pools = {}
    for e in entries:
        pools.setdefault(e["fp"], []).append(e)
    verdicts, used = [], 0
    for inp in inputs:
        pool = pools.get(inp["fp"])
        if pool:
            e = pool.pop(0)
            used += 1
            verdicts.append({"input_key": inp["input_key"], "verdict": e["verdict"], "label": e["label"]})
    return verdicts, len(entries) - used


def verify_cc(acc, host, session_id, turn_id, raw, engine_version, prompt_id=None, branch=""):
    """Claude Code：用记录全文核这一轮所有还待核的输入。prompt_id 就是 turn_id（钩子的 prompt_id 当轮次号）。返回 {"state": pending|resolved|failed, ...}。"""
    pid = prompt_id or turn_id
    res = cc_entries(raw, pid, engine_version)
    pend = [i for i in acc.inputs_of(host, session_id, turn_id, branch) if i["state"] == "pending"]
    if not res["ok"]:
        if res["pending"]:
            return {"state": "pending", "error": res["error"]}                         # 还没写完：以后再核（到时限由 expire_inputs 算失败）
        acc.resolve_inputs(host, session_id, turn_id, [{"input_key": i["input_key"], "verdict": "fail", "label": res["error"]} for i in pend], branch)
        return {"state": "failed", "error": res["error"]}
    allin = acc.inputs_of(host, session_id, turn_id, branch)
    verdicts, unmatched = match_inputs([i for i in allin if i["state"] == "pending"], _drop_matched(allin, res["entries"]))
    r = acc.resolve_inputs(host, session_id, turn_id, verdicts, branch, unmatched=unmatched if _all_inputs_matched(allin, verdicts) else 0)
    return {"state": "resolved", "applied": r["applied"], "unmatched": unmatched, "hints": r["hints"], "ignored": r.get("ignored")}


def _drop_matched(allin, entries):
    """已经核过的输入占掉记录里对应的条目（按指纹先后），剩下的条目才拿去对还待核的输入，免得同一条被核两次。"""
    pools = {}
    for e in entries:
        pools.setdefault(e["fp"], []).append(e)
    for i in allin:
        if i["state"] != "pending" and pools.get(i["fp"]):
            pools[i["fp"]].pop(0)
    return [e for v in pools.values() for e in v]


def _all_inputs_matched(allin, verdicts):
    """还待核的输入都有了判定，才谈「记录里多出来的条目」：只有部分输入落盘时，多出的条目可能只是还没轮到。"""
    pend = [i["input_key"] for i in allin if i["state"] == "pending"]
    got = {v["input_key"] for v in verdicts}
    return all(k in got for k in pend)


# ---------------------------------------------------------------- Codex
CODEX_TRIGGERS = {
    "composer": "candidate",                                                         # 桌面端实测过的入口
    "remote_ios": "candidate_off", "edit_user_message": "candidate_off",             # 待核候选，默认不启用
    "exec": "closed", "queue": "closed", "capacity_retry_manual": "closed", "resume_interrupted_task": "closed", "send_user_message_async_question": "closed",
    "guardian_review": "excluded", "app_tool_send_message": "excluded", "app_tool_create_thread": "excluded", "automation_heartbeat_scheduled": "excluded",
}


def _present(d, key):
    return isinstance(d, dict) and key in d


def codex_source_verdict(hook_event, record_attr, meta_turn, engine_version, enabled_candidates=frozenset(), session_thread_source=None):
    """Codex 一轮的来源判定：写入、读取都用这一个。
    hook_event＝UserPromptSubmit 事件（只看 agent_id、agent_type 有没有）；record_attr＝记录里这一轮的 turn_attribution（可以是 None）；
    meta_turn＝MCP _meta 里的 x-codex-turn-metadata（可以是 None）；session_thread_source＝会话头部的 thread_source（record_attr 或 meta 里没有时用）。
    记录和 _meta 都有的字段必须一致；字段缺了和「明确写着没有发起代理」分开，缺了就是失败。返回 {"verdict": human|other|fail, "reason"}。"""
    if engine_version not in CODEX_SOURCE_VERSIONS:
        return {"verdict": "fail", "reason": "version_unsupported"}
    if not isinstance(record_attr, dict) and not isinstance(meta_turn, dict):
        return {"verdict": "fail", "reason": "no_attribution"}
    if isinstance(hook_event, dict) and (hook_event.get("agent_id") or hook_event.get("agent_type")):
        return {"verdict": "other", "reason": "subagent_in_hook"}                    # 钩子带了子代理证据：不被记录里的空字段盖掉
    for k in ("turn_id", "turn_trigger"):
        a = record_attr.get(k) if isinstance(record_attr, dict) else None
        b = meta_turn.get(k) if isinstance(meta_turn, dict) else None
        if a is not None and b is not None and a != b:
            return {"verdict": "fail", "reason": "record_meta_mismatch_" + k}
    trig = (record_attr or {}).get("turn_trigger") if isinstance(record_attr, dict) and (record_attr or {}).get("turn_trigger") is not None else (meta_turn or {}).get("turn_trigger")
    if isinstance(record_attr, dict):
        if not _present(record_attr, "initiating_agent_path"):
            return {"verdict": "fail", "reason": "initiating_agent_path_missing"}      # 没有这个版本「省略＝没有代理」的证据：缺了就是缺了
        iap = record_attr["initiating_agent_path"]
        if iap not in (None, "", [], {}):
            return {"verdict": "other", "reason": "subagent_initiated"}
    ts = (meta_turn or {}).get("thread_source") if isinstance(meta_turn, dict) and (meta_turn or {}).get("thread_source") is not None else session_thread_source
    if ts is None:
        return {"verdict": "fail", "reason": "thread_source_missing"}
    if ts != "user":
        return {"verdict": "other", "reason": "thread_source_" + str(ts)}
    cls = CODEX_TRIGGERS.get(trig) if isinstance(trig, str) else None
    if trig is None:
        return {"verdict": "fail", "reason": "turn_trigger_missing"}
    if cls is None:
        return {"verdict": "fail", "reason": "turn_trigger_unknown"}
    if cls == "excluded":
        return {"verdict": "other", "reason": "trigger_" + trig}
    if cls == "closed":
        return {"verdict": "fail", "reason": "trigger_not_opened_" + trig}
    if cls == "candidate_off" and trig not in enabled_candidates:
        return {"verdict": "fail", "reason": "trigger_not_enabled_" + trig}
    return {"verdict": "human", "reason": "trigger_" + trig}


def verify_single_input(acc, host, session_id, turn_id, verdict, branch=""):
    """没有逐条证据时的保守做法：只支持「这一轮只有一条输入」。输入多于一条、或一条都没有，都算失败，不把一个判定套到所有输入上。"""
    inputs = acc.inputs_of(host, session_id, turn_id, branch)
    pend = [i for i in inputs if i["state"] == "pending"]
    if len(inputs) != 1:
        acc.resolve_inputs(host, session_id, turn_id, [{"input_key": i["input_key"], "verdict": "fail", "label": "multi_input_unverified" if inputs else "no_input"} for i in pend], branch)
        return {"state": "failed", "reason": "input_count_%d" % len(inputs)}
    r = acc.resolve_inputs(host, session_id, turn_id, [{"input_key": pend[0]["input_key"], "verdict": verdict["verdict"], "label": verdict["reason"]}] if pend else [], branch)
    return {"state": "resolved", "applied": r["applied"], "hints": r["hints"], "ignored": r.get("ignored")}
