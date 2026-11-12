"""四家宿主的交付路径和身份链状态。全部是**可信配置**：宿主身份和上限从启动配置来，模型不能填「另一个宿主」来放宽。

每条交付路径登记四样：单位、阈值、包装开销（预留）、超限时宿主的做法（截断／转存）。我们自己的估算词元（材料预算）和宿主的单位不混算：
先按材料预算选料，再对**最终的完整返回**（含开头说明、分页提示、游标、数据边界）按宿主的单位核。8,000／24,000 都是我们的上限，不是任何一家的上限。

阈值出处：`第二段/宿主资料-官方说明-2026-11-09.md`。「现行文档写了、没有固定版本证明」的，路径的 verified 一律是 False（真机上交付是否真到，第三段验证）；
计数办法没核准的，按**保守**的数（宁可页小一点，不让宿主截断或转存）：
* utf16：JS 字符串长度的保守读法（增补平面字符算 2）；
* bytes：UTF-8 字节数，已知 UTF-8 字节数 ≥ UTF-16 码元数，所以同时满足「按字节」和「按 UTF-16 码元」的各种读法；
* codex_tok：Codex 自己的估算词元＝UTF-8 字节数÷4 向上取整（上游源码，宿主资料 §3.6）；
* claude_tok_proxy：Claude 的词元怎么数这里没有核准，拿 UTF-8 字节数当上界（假定一个词元至少覆盖一个字节），**非实测**。
"""
import dataclasses
from typing import Dict, Tuple

UNITS = ("cp", "utf16", "bytes", "codex_tok", "claude_tok_proxy")


def measure(unit, text):
    if unit == "cp":
        return len(text)
    if unit == "utf16":
        return sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    if unit in ("bytes", "claude_tok_proxy", "codex_tok"):
        n = len(text.encode("utf-8", "surrogatepass"))                 # 孤立代理字符按 3 字节数，不抛错
        return (n + 3) // 4 if unit == "codex_tok" else n
    raise ValueError("不认识的计数单位：%r" % (unit,))


@dataclasses.dataclass(frozen=True)
class Limit:
    unit: str
    threshold: int
    on_exceed: str            # 宿主超限时的做法（文字，给报告和兼容表看）


@dataclasses.dataclass(frozen=True)
class Path:
    name: str
    limits: Tuple[Limit, ...]
    overhead: Dict[str, int] = dataclasses.field(default_factory=dict)   # 每个单位预留多少给宿主／SDK 的包装；保守预留，非实测
    verified: bool = False                                                # 固定版本上的真机验证；这一段全是 False
    source: str = ""


def fits(path, text):
    """最终返回是否满足这条路径的**全部**限制。返回 (ok, [{unit, value（含预留）, threshold}])。"""
    detail, ok = [], True
    for l in path.limits:
        v = measure(l.unit, text) + path.overhead.get(l.unit, 0)
        detail.append({"unit": l.unit, "value": v, "threshold": l.threshold})
        if v > l.threshold:
            ok = False
    return ok, detail


def codex_required_limit(chars=8000, wrapper_chars=0):
    """Codex 钩子 additionalContextLimit 至少要设多大，才保证任意 chars 个字符（含我们自己包的 wrapper_chars 个字符）不被存成文件：
    UTF-8 每字符最多 4 字节，÷4 向上取整，每字符最多 1 个估算词元。"""
    return chars + wrapper_chars


@dataclasses.dataclass(frozen=True)
class HostProfile:
    name: str
    version_pin: str
    identity: str                       # in_process｜launch_bound｜call_bound｜unproven（读取工具身份链，见 idchain.py 和兼容表）
    origin_basis: str                   # 怎样认定「这是目标用户的输入」；认不准就写明
    paths: Dict[str, Path]
    note: str = ""

    @property
    def tools_opened(self):
        """记忆读取工具开不开放：拿不到可信身份链的宿主不开放。"""
        return self.identity != "unproven"

    def configured(self, codex_hook_limit=None):
        """返回一份改了配置的副本（原对象不变）。目前只有 Codex 的钩子上限可调（装接头时写进 additionalContextLimit）。"""
        if codex_hook_limit is None:
            return self
        if self.name != "codex":
            raise ValueError("只有 codex 有 additionalContextLimit")
        old = self.paths["hook_context"]
        new = dataclasses.replace(old, limits=(Limit("codex_tok", codex_hook_limit, old.limits[0].on_exceed),))
        return dataclasses.replace(self, paths=dict(self.paths, hook_context=new))


_MCP_OVH = {"utf16": 500, "bytes": 1500, "claude_tok_proxy": 1500, "codex_tok": 400, "cp": 500}
_HOOK_OVH = {"utf16": 300, "bytes": 900, "claude_tok_proxy": 900, "codex_tok": 0, "cp": 300}      # Codex 钩子的计数就是对钩子输出文本本身数（上游源码），没有宿主包装要预留

PROFILES = {
    "claude-code": HostProfile(
        "claude-code", "2.1.300", "unproven",
        "UserPromptSubmit 事件没有区分来源的字段（定时任务、后台子代理回报、别的会话消息都会触发）：来源认不准，自动记录默认不开",
        {"mcp_return": Path("mcp_return", (Limit("utf16", 50000, "超过 50,000 字符存成文件，对话里换成文件路径"),
                                           Limit("claude_tok_proxy", 25000, "超过 MAX_MCP_OUTPUT_TOKENS（默认 25,000 词元）报错或截断；10,000 词元只是警告线，不算上限")),
                            _MCP_OVH, False, "宿主资料 §2.9（现行文档，2.1.300 待核）"),
         "hook_context": Path("hook_context", (Limit("utf16", 10000, "每个字符串超过 10,000 字符存成文件，模型只拿到路径和前 2,000 字符的预览"),),
                              _HOOK_OVH, False, "宿主资料 §2.5（现行文档，2.1.300 待核；『字符』的计数单位未核，按 UTF-16 保守数）")},
        "静态 stdio MCP 拿不到会话和轮次身份：读取工具不开放"),
    "codex": HostProfile(
        "codex", "0.162.1", "unproven",
        "UserPromptSubmit 带 agent_id／agent_type 是子代理里的提示；没有这两个字段的算主线程提示。Stop 续跑产生的新提示是否触发 UserPromptSubmit 未核",
        {"mcp_return": Path("mcp_return", (Limit("codex_tok", 2500, "默认截断预算文档没写（未核）；这里先按钩子默认 2,500 估算词元保守取"),),
                                           _MCP_OVH, False, "宿主资料 §3.10（MCP 输出预算未核）"),
         "hook_context": Path("hook_context", (Limit("codex_tok", 2500, "默认约 2,500 估算词元（UTF-8 字节÷4）；超了存成文件，只给头尾预览；additionalContextLimit 可调"),),
                              _HOOK_OVH, False, "宿主资料 §3.6（上游源码 rust-v0.162.1，施工方没有本机副本，未复核）")},
        "静态 stdio MCP 拿不到会话和轮次身份：读取工具不开放"),
    "hermes": HostProfile(
        "hermes", "v0.22.0", "in_process",
        "记忆插件 initialize 的 agent_context（primary／subagent／cron／flush）、on_turn_start 的 author_is_bot；只收 primary 且不是机器人的用户回合",
        {"mcp_return": Path("mcp_return", (Limit("cp", 50000, "MCP 文本硬顶 2,000,000 字符；『预算层 50K 落盘阈值』的规则未核，保守取 50,000"),),
                            _MCP_OVH, False, "宿主资料 §4.1（50K 规则未核）"),
         "hook_context": Path("hook_context", (Limit("cp", 10000, "pre_llm_call 每个钩子超过 hooks.output_spill.max_chars（默认 10,000 字符）落盘；记忆预取默认不落盘但可开"),),
                              _HOOK_OVH, False, "宿主资料 §4.2、§4.3（本机源码 HA:tools/hook_output_spill.py:24-27，未在运行中的 Hermes 上实测）")},
        "读取工具由记忆插件自带（handle_tool_call 不带会话和轮次，靠插件实例里的状态）；静态 MCP 登记同样拿不到身份"),
    "pi": HostProfile(
        "pi", "main@415be82（没装）", "launch_bound",
        "input 事件的 source：只认 interactive；rpc 不自动等于真人，extension 是扩展自己发的，都不记",
        {"mcp_return": Path("mcp_return", (Limit("bytes", 20000, "文本结果超过 20 KB 去掉中间部分、存成临时文件；『20 KB』的计数办法没核准，按 UTF-8 字节 20,000 保守取"),),
                             _MCP_OVH, False, "宿主资料 §5.1（固定提交的文档；源码计数办法未核）"),
         "hook_context": Path("hook_context", (Limit("cp", 8000, "注入消息的大小上限没查到；先按 8,000 字符，接线时实测"),),
                              _HOOK_OVH, False, "宿主资料 §5.6（上限未核，自定 8,000）")},
        "读取工具走『扩展给每个会话单起 MCP 进程并带连接句柄』这条路（registerMcpServer 的行为没在装好的 Pi 上实测）"),
}


def with_call_binding(prof):
    """装接头时显式打开的一种身份链（只给 Claude Code、Codex）：调用前钩子把宿主给的调用编号登记到（会话、分支、轮次），MCP 读取时按宿主放进 _meta 的编号查。
    默认配置仍是 unproven（读取工具不开放）；没有钩子登记时每次调用都因为「调用没登记」被拒。真机证据：Claude Code 桌面 2.1.317、命令行 2.1.300，Codex 命令行 0.162.1、当次桌面环境（有限的连线证据）。"""
    if prof.name not in ("claude-code", "codex"):
        raise ValueError("只有 Claude Code、Codex 走调用编号这条身份链")
    return dataclasses.replace(prof, identity="call_bound", note="调用前钩子登记调用编号，MCP 按 _meta 里的编号查；没登记、有冲突、对不上轮次都拒绝")


def profile(name):
    if name not in PROFILES:
        raise ValueError("不认识的宿主：%r（只有 %s）" % (name, "、".join(sorted(PROFILES))))
    return PROFILES[name]
