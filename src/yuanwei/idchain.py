"""各家宿主事件 → 可信身份。**只用这家宿主资料里真有的字段**（`第二段/宿主资料-官方说明-2026-11-09.md`），不加任何真实宿主没有的字段，不读对话记录，不按文字分类来源。

每个函数只做一件事：从一个宿主事件里取出（宿主、会话、分支、轮次、来源、要不要忽略）。接头（乙）拿 IdEvent 去调 Access.begin_turn；判断逻辑不放在接头里。
来源（origin）只有两种：user（这家宿主有字段能认定这是目标用户的输入）和 unknown（认不准）。认不准的事件不开新一轮、不清掉已有的拒绝、里面的口令文字不算控制（见 access.begin_turn）。

各家的事实（出处见兼容表）：
* claude-code：UserPromptSubmit 带 session_id、prompt_id（第一次用户输入之前没有）、cwd、prompt；定时任务、后台子代理回报、别的会话消息也会触发它，**文档没有区分来源的字段**，所以来源恒为 unknown。
* codex：UserPromptSubmit 带 session_id、turn_id、prompt；带 agent_id／agent_type 是子代理里的提示，忽略；没有这两个字段的算主线程提示（Stop 续跑产生的新提示是否触发 UserPromptSubmit 未核）。Stop 的 last_assistant_message 可能是 null。
* hermes：记忆插件 initialize 的 agent_context（primary／subagent／cron／flush）和 platform，on_turn_start 的 author_is_bot；回合没有可用的稳定编号（turn_number 回退后会重复），轮次标记由插件在每次 on_turn_start 自己生成。
* pi：input 事件带 text 和 source（interactive／rpc／extension）；input 不带 id，轮次标记由扩展自己生成；排队的输入（streamingBehavior 是 steer／followUp）不在 input 上登记。
"""
import collections

IdEvent = collections.namedtuple("IdEvent", "host session_id branch turn_id origin ignore text workspace_hint")


def _s(v):
    return isinstance(v, str) and bool(v)


def _ev(host, session_id, turn_id, origin, ignore, text, hint=None, branch=""):
    return IdEvent(host, session_id, branch, turn_id, origin, ignore, text, hint)


def claude_code_prompt(ev):
    sid, pid = ev.get("session_id"), ev.get("prompt_id")
    ignore = None
    if not (_s(sid) and _s(pid)):
        ignore = "identity_incomplete"
    elif ev.get("agent_id") or ev.get("agent_type"):
        ignore = "subagent"
    return _ev("claude-code", sid, pid, "unknown", ignore, ev.get("prompt"), ev.get("cwd"))


def codex_prompt(ev):
    sid, tid = ev.get("session_id"), ev.get("turn_id")
    ignore = None
    if not (_s(sid) and _s(tid)):
        ignore = "identity_incomplete"
    elif ev.get("agent_id") or ev.get("agent_type"):
        ignore = "subagent"
    return _ev("codex", sid, tid, "unknown" if ignore else "user", ignore, ev.get("prompt"), ev.get("cwd"))


def codex_stop(ev):
    """Stop：last_assistant_message 可能是 null（不记）；stop_hook_active 为真表示这是被别的钩子续跑之后的又一次 Stop，不是最终性的证明。"""
    return {"session_id": ev.get("session_id"), "turn_id": ev.get("turn_id"), "reply": ev.get("last_assistant_message"), "continued": bool(ev.get("stop_hook_active"))}


def hermes_turn(init, turn_number, message, author, token):
    """init＝插件 initialize 收到的参数；turn_number＝on_turn_start 的回合号（只作记录，不当轮次编号）；author＝{"id","name","is_bot"}；token＝插件为这一回合生成的唯一标记。"""
    sid = (init or {}).get("session_id")
    if not (_s(sid) and _s(token)):
        return _ev("hermes", sid, token, "unknown", "identity_incomplete", message)
    ctx = (init or {}).get("agent_context", "primary")
    if ctx != "primary":
        return _ev("hermes", sid, token, "unknown", "agent_context_%s" % ctx, message)
    if (init or {}).get("platform") == "cron":
        return _ev("hermes", sid, token, "unknown", "platform_cron", message)
    if (author or {}).get("is_bot"):
        return _ev("hermes", sid, token, "unknown", "bot_author", message)
    return _ev("hermes", sid, token, "user", None, message, (init or {}).get("cwd"))


def pi_input(ev, session_id, token):
    if not (_s(session_id) and _s(token)):
        return _ev("pi", session_id, token, "unknown", "identity_incomplete", ev.get("text"))
    src = ev.get("source")
    if src == "extension":
        return _ev("pi", session_id, token, "unknown", "extension_input", ev.get("text"))
    if src != "interactive":
        return _ev("pi", session_id, token, "unknown", "origin_unknown", ev.get("text"))
    if ev.get("streamingBehavior") in ("steer", "followUp"):
        return _ev("pi", session_id, token, "user", "queued_wait_for_run_start", ev.get("text"))
    return _ev("pi", session_id, token, "user", None, ev.get("text"))
