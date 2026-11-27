"""清单第 56 条第一批第 4～7 节的测试用：往 sys.modules 里放假的宿主模块（gateway.session_context、gateway.response_filters、agent.skill_commands），**不 import 真宿主**。
形状照 Hermes v0.23.2（gateway/session_context.py 的 _SESSION_TURN_EVENT 是 ContextVar；gateway/response_filters.py 的 display_kind_for_event 对「自注入的事件」给 display_kind、普通用户事件给 None；
agent/skill_commands.py 的 extract_user_instruction_from_skill_message 用冻结的宿主原文，见 hermes_skill_contract.py）。"""
import contextvars
import sys
import types


class Event:
    """假的 MessageEvent：只有这几个字段（真的是 gateway/platforms/event.py 里的 dataclass）。"""

    def __init__(self, text, internal=False, display_kind=None):
        self.text, self.internal, self.display_kind = text, internal, display_kind


def display_kind_for_event(event):
    """和宿主一样的结果：自注入事件有 kind，普通用户事件是 None。"""
    if getattr(event, "display_kind", None):
        return event.display_kind
    if getattr(event, "internal", False):
        return "process_complete"
    return None


def install(monkeypatch, event=None, *, kind_fn=display_kind_for_event, skill=True, gateway=True):
    """安装假模块；event 为 None 就是「没有当前事件」。返回那个 ContextVar，测试里可以随时 .set(新事件)。"""
    var = contextvars.ContextVar("HERMES_SESSION_TURN_EVENT", default=None)
    if event is not None:
        var.set(event)
    if gateway:
        gw = types.ModuleType("gateway")
        sc = types.ModuleType("gateway.session_context")
        sc._SESSION_TURN_EVENT = var
        rf = types.ModuleType("gateway.response_filters")
        rf.display_kind_for_event = kind_fn
        gw.session_context, gw.response_filters = sc, rf
        monkeypatch.setitem(sys.modules, "gateway", gw)
        monkeypatch.setitem(sys.modules, "gateway.session_context", sc)
        monkeypatch.setitem(sys.modules, "gateway.response_filters", rf)
    else:
        for name in ("gateway", "gateway.session_context", "gateway.response_filters"):
            monkeypatch.setitem(sys.modules, name, None)                       # None：import 时报 ImportError
    if skill:
        import hermes_skill_contract as C
        ag = types.ModuleType("agent")
        sk = types.ModuleType("agent.skill_commands")
        sk.extract_user_instruction_from_skill_message = C.extract
        ag.skill_commands = sk
        monkeypatch.setitem(sys.modules, "agent", ag)
        monkeypatch.setitem(sys.modules, "agent.skill_commands", sk)
    else:
        for name in ("agent", "agent.skill_commands"):
            monkeypatch.setitem(sys.modules, name, None)
    return var


def user_event(text):
    return Event(text)


def host_event(text):
    return Event(text, internal=True)
