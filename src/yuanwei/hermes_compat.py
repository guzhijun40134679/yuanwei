"""我们依赖 Hermes 的哪些东西，以及怎么核（用户的要求：Hermes 每次升级，不要让我们重写很多东西）。

设计：
* **接头要薄**：插件文件只 import Hermes 公开的记忆插件基类（agent.memory_provider.MemoryProvider），规则全在 yuanwei 核心里。适配器另外**懒导入**三处 Hermes 内部模块（用到时才导入，导入或调用失败就退回保守做法，见下面 owner_identity、runtime_cwd 两条；Python 环境那条是跑 hermes 命令，不是 import）。
* **按功能探测，不按版本号写死**：运行时（probe_features）看基类里有没有我们要用的方法、参数对不对；对不上的那一项功能关掉、原因写进状态，别的照常，Hermes 本身照常工作。
* **一条核对命令**：yuanwei doctor hermes 读本机装着的 Hermes 源码（只读，不执行、不碰 ~/.hermes 的配置记忆会话），对着下面 DEPENDENCIES 逐项核，输出哪项对、哪项变了、影响哪些功能。
* DEPENDENCIES 是唯一的清单：doctor、兼容表第 9 节、测试都从它来。要依赖新东西，先加到这里。
"""
import ast
import json
import os
import re

# ---------------------------------------------------------------- 依赖清单
DEPENDENCIES = [
    {"id": "base_abstract", "what": "MemoryProvider 的必须实现的方法只有 name、is_available、initialize、get_tool_schemas", "where": "agent/memory_provider.py: class MemoryProvider（abstractmethod）",
     "affects": ["plugin_load"], "if_changed": "插件文件会给多出来的必须实现的方法补空实现，Hermes 仍能加载我们；多出来的方法名写进状态"},
    {"id": "base_methods", "what": "initialize、get_tool_schemas、handle_tool_call（带 **kwargs）、prefetch、on_turn_start（带 **kwargs）、on_session_switch、on_session_end、shutdown、system_prompt_block 的名字和参数", "where": "agent/memory_provider.py: class MemoryProvider",
     "affects": ["tools", "prefetch", "session_switch"], "if_changed": "对应功能关掉（读取工具、回答前交内容、压缩续接），写入和别的功能照常"},
    {"id": "window_switch_kwargs", "what": "on_session_switch 带 parent_session_id、reset、rewound 三个参数，并且收 **kwargs（reason 值 compression／branch／resume／new_session 在 kwargs 里，探测不到取值）", "where": "agent/memory_provider.py: MemoryProvider.on_session_switch",
     "affects": ["window"], "if_changed": "窗口跟着会话号变（压缩同一窗口、分叉复制状态、重置新窗口、恢复查绑定）要靠这几个字段；缺一个就按「缺」处理：走「恢复」那一条（查绑定，查不到就暂停读取），不默认开"},
    {"id": "base_sync_turn", "what": "sync_turn(user_content, assistant_content, *, session_id, messages, turn_author)", "where": "agent/memory_provider.py: MemoryProvider.sync_turn",
     "affects": ["write", "bot_filter"], "if_changed": "没有 messages：写入关掉（没法核当前这一轮是不是后台自注入的）；没有 turn_author：机器人作者过滤关掉，按老宿主处理"},
    {"id": "host_turn_start_kwargs", "what": "宿主调用 on_turn_start 时带 author_id、author_name、author_is_bot", "where": "agent/turn_context.py: _memory_turn_start_and_prefetch 里的 .on_turn_start(...) 调用",
     "affects": ["bot_filter"], "if_changed": "没有这几个字段就当老宿主（0.20.0 那种），状态里标 legacy_no_author_field"},
    {"id": "host_sync_kwargs", "what": "宿主在每轮结束调 sync_all 时带 messages 和 turn_author", "where": "run_agent.py: _sync_external_memory_for_turn（含 .sync_all(...) 的函数）",
     "affects": ["write", "bot_filter"], "if_changed": "不带 messages：写入关掉；不带 turn_author：机器人作者过滤关掉"},
    {"id": "machinery_markers", "what": "宿主区分真人提问和自注入轮次的标记：用户行的 display_kind，和 _SYNTHETIC_USER_FLAGS 里的合成标记", "where": "agent/conversation_compression.py 或 agent/message_metadata.py: _SYNTHETIC_USER_FLAGS；agent/context_compressor.py: display_kind",
     "affects": ["write"], "if_changed": "宿主多出来的、名字不以 _synthetic 结尾的合成标记我们认不出，会被当真人提问记下来：要把它加进 hermes_adapter.SYNTHETIC_USER_FLAGS"},
    {"id": "plugin_discovery", "what": "用户插件目录 $HERMES_HOME/plugins/<名字>/ 里的记忆提供者会被发现并加载（register_memory_provider）", "where": "plugins/plugin_loader.py（老版本在 plugins/memory/__init__.py）: get_hermes_home() / \"plugins\"；plugins/memory/__init__.py: register_memory_provider",
     "affects": ["install"], "if_changed": "setup 装不上：先看新的安装位置再改 hermes_setup.py 里的 PLUGIN_REL"},
    {"id": "provider_activation", "what": "memory.provider 这个设置键选定外部记忆提供者", "where": "hermes_cli/memory_setup.py: config[\"memory\"][\"provider\"]",
     "affects": ["install"], "if_changed": "setup/uninstall 改的键变了：改 hermes_setup.py 里的 MEMORY_PROVIDER_KEY"},
    {"id": "init_kwargs", "what": "宿主初始化插件时总会传 agent_context、platform、hermes_home，有会话工作目录就传 cwd", "where": "agent/agent_init.py: _memory_provider_init_kwargs（老版本是 initialize 前拼的 _init_kwargs）",
     "affects": ["agent_context", "workspace"], "if_changed": "agent_context 缺了：这个实例按「说不清」处理，不读不写（不当成主对话）；cwd 没给时（Hermes 只有会话有工作目录才传）：经典终端（平台 cli）取 Hermes 进程目录（优先 resolve_agent_cwd）照项目库判，其他本机入口（tui、desktop、local）和网关平台记进「生活」；cwd 这个键整个不传了：经典终端也只靠进程目录，别的入口全部落进生活"},
    {"id": "projects_db", "what": "项目库：$HERMES_HOME/projects.db 的 projects(id, name, archived) 和 project_folders(project_id, path)；项目归属＝cwd 在文件夹里（最长的赢，归档的不算）", "where": "hermes_cli/projects_db.py: projects_db_path、SCHEMA_SQL、project_for_path",
     "affects": ["workspace"], "if_changed": "认不出项目：这一轮不读不写（不落进生活）；这一版没有项目功能：所有对话落进生活；要重核 hermes_workspace.resolve 的规则"},
    {"id": "owner_identity", "what": "gateway.owner_identity.host_owner_subject()：tui／desktop 带着登录身份时，拿它和宿主主人的系统身份比，一样才当主人（适配器懒导入，只比较、只在内存里用）", "where": "gateway/owner_identity.py: host_owner_subject",
     "affects": ["login_owner"], "if_changed": "没有这个函数或调不了：带登录身份的 tui／desktop 不当主人（login_identity_unverifiable），要在设置里配 owners 才记；经典终端和 local 不受影响"},
    {"id": "runtime_cwd", "what": "agent.runtime_cwd.resolve_agent_cwd()：智能体在哪个目录干活（适配器懒导入，经典终端没给 cwd 时用）", "where": "agent/runtime_cwd.py: resolve_agent_cwd",
     "affects": ["workspace"], "if_changed": "没有这个函数或调不了：退回 os.getcwd()（Hermes 进程目录），可能和配置的工作目录不一样"},
    {"id": "runtime_python", "what": "hermes --print-runtime-command --module site：问 Hermes 自己用哪个 Python 和 sys.path，装 NumPy、查 NumPy 都靠它找「Hermes 实际用的环境」（向导跑这条命令，不 import）", "where": "hermes_cli/_launchers.py: print_runtime_command（--module、--print-runtime-command）",
     "affects": ["numpy_install"], "if_changed": "找不到 Hermes 的真实 Python 环境：不替你装 NumPy、状态里写「没能确认」，向量退回只用关键词；可以用 --hermes-python 自己指一个"},
    {"id": "tool_schemas_at_registration", "what": "MemoryManager.add_provider 在 initialize 之前就调 get_tool_schemas()，并把工具名记进路由表、不再刷新（所以插件在初始化之前也要交固定的工具清单）；agent_init 里先 add_provider 再 initialize", "where": "agent/memory_manager.py: MemoryManager.add_provider；agent/agent_init.py: add_provider 在 initialize_for_agent／initialize_all 之前",
     "affects": ["tools"], "if_changed": "顺序变了或不再预读：初始化之前交固定清单的做法要重核（不再需要，或者交空清单会 Unknown tool）"},
    {"id": "group_room_platform", "what": "群聊里成员机器人回话那一轮走的平台名是 bot_room（initialize 的 platform）；原委把它列为明确的「不记也不查」平台", "where": "gateway/session_hosted_rpc.py: replace(policy, source='bot_room', platform='bot_room', …)",
     "affects": ["group_room"], "if_changed": "平台名变了或去掉了：群聊的轮次会被当成别的网关平台，只靠「没配主人」碰巧不记；要把新名字加进 hermes_profile.GROUP_ROOM_PLATFORMS"},
    {"id": "bot_author_tag", "what": "机器人互发的消息，Hermes 给这一轮贴作者标签 {id: \"bot:<档案>\", is_bot: True}（bot_author_id、is_bot）；原委靠 author_is_bot／turn_author.is_bot 不记、也不让这一轮读", "where": "agent/turn_author.py: bot_author_id（返回 bot:…）、字段 is_bot",
     "affects": ["bot_filter"], "if_changed": "标签改名或去掉了：机器人发起的轮次认不出，会被当成本人的话（本机平台没有别的可靠依据）；要对着新字段改 hermes_adapter 的机器人判断"},
    {"id": "plugin_python_deps", "what": "插件依赖声明：plugin.yaml 里的 python_dependencies／pip_dependencies 由 read_python_declaration 读；当前 memory.provider 并进各家目录的成员表（_provider_from_config）；重建环境时按成员表带上依赖（enabled_member_dirs 用 installs_packages，members_stamp 把 plugin.yaml 内容算进环境指纹）。原委靠它让 Hermes 重建环境时自动带上 NumPy", "where": "pm/plugin_declarations.py: read_python_declaration；pm/plugins_state.py: _provider_from_config、enabled_plugins_ordered；pm/workspace.py: enabled_member_dirs、members_stamp",
     "affects": ["numpy_install"], "if_changed": "声明机制变了或去掉了：Hermes 重建环境时不会再自动带上 NumPy，向量会在重建后悄悄退回只用关键词；status 会报「没有 NumPy」，向导装的 NumPy 要重新装；要对着新机制改 plugin.yaml 的声明写法"},
    {"id": "config_format", "what": "设置文件的格式版本（_config_version）是我们核过的版本", "where": "hermes_cli/config_defaults.py（或 hermes_cli/config.py）: \"_config_version\"",
     "affects": ["install"], "if_changed": "只用 memory.provider 这一个键，格式变了多半不影响；但要重新核这个键的位置，并重跑 setup 记下新格式版本"},
]
KNOWN_CONFIG_FORMATS = (50, 52)                      # 核过的设置格式版本：v0.22.0 之前是 50，本机现在这一版是 52
KNOWN_ABSTRACT = frozenset(["name", "is_available", "initialize", "get_tool_schemas"])
EXPECTED = {                                          # 方法名 → 我们要用的参数名（"**" ＝要带 **kwargs）
    "initialize": ["session_id", "**"], "get_tool_schemas": [], "handle_tool_call": ["tool_name", "args", "**"], "prefetch": ["query", "session_id"],
    "on_turn_start": ["turn_number", "message", "**"], "on_session_switch": ["new_session_id", "parent_session_id", "reset", "rewound", "**"],
    "on_session_end": ["messages"], "shutdown": [], "system_prompt_block": [],
}
SYNC_PARAMS = ["user_content", "assistant_content", "session_id", "messages", "turn_author"]
OUR_SYNTHETIC_FLAGS = ("_todo_snapshot_synthetic", "_empty_recovery_synthetic", "_verification_stop_synthetic", "_pre_verify_synthetic", "_dropped_toolcall_nudge")
FEATURES = ("tools", "prefetch", "write", "bot_filter", "session_switch")


# ---------------------------------------------------------------- 运行时：按功能探测（看基类对象）
def _params_of(fn):
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    names = [p.name for p in sig.parameters.values() if p.name != "self"]
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        names.append("**")
    return names


def probe_features(base):
    """看宿主的 MemoryProvider 基类，决定哪些功能能开。返回 {"base_ok", "features": {名: {"on", "reason"}}, "extra_abstract": [...]}。基类不像基类，全部关掉。"""
    abstract = getattr(base, "__abstractmethods__", None)
    feats = {f: {"on": False, "reason": "host_base_not_a_provider_base"} for f in FEATURES}
    if abstract is None or not isinstance(base, type):
        return {"base_ok": False, "features": feats, "extra_abstract": []}
    extra = sorted(set(abstract) - KNOWN_ABSTRACT)

    def have(name, need):
        fn = getattr(base, name, None)
        if fn is None or not callable(fn):
            return "host_%s_missing" % name
        got = _params_of(fn)
        if got is None:
            return None
        miss = [n for n in need if n != "**" and n not in got and "**" not in got] + (["**kwargs"] if "**" in need and "**" not in got else [])
        return "host_%s_without_%s" % (name, "_".join(miss)) if miss else None

    def setf(name, why):
        feats[name] = {"on": why is None, "reason": why}
    setf("tools", have("get_tool_schemas", EXPECTED["get_tool_schemas"]) or have("handle_tool_call", EXPECTED["handle_tool_call"]))
    setf("prefetch", have("prefetch", EXPECTED["prefetch"]))
    setf("session_switch", have("on_session_switch", ["new_session_id"]))
    st = getattr(base, "sync_turn", None)
    got = _params_of(st) if callable(st) else None
    if got is None:
        setf("write", "host_sync_turn_missing")
        setf("bot_filter", "host_sync_turn_missing")
    else:
        setf("write", None if ("messages" in got or "**" in got) and "user_content" in got else "host_sync_turn_without_messages")
        setf("bot_filter", None if "turn_author" in got or "**" in got else "host_sync_turn_without_turn_author")
    return {"base_ok": True, "features": feats, "extra_abstract": extra}


# ---------------------------------------------------------------- 核对命令：只读源码，不执行
def _parse(rd, rel):
    t = rd(rel)
    if t is None:
        return None, "missing"
    try:
        return ast.parse(t), None
    except SyntaxError:
        return None, "unparsable"


def _class_methods(tree, cls):
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            out = {}
            for f in n.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = f.args
                    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs if x.arg != "self"] + (["**"] if a.kwarg else [])
                    abstract = any((isinstance(d, ast.Attribute) and d.attr == "abstractmethod") or (isinstance(d, ast.Name) and d.id == "abstractmethod") for d in f.decorator_list)
                    out[f.name] = {"params": names, "abstract": abstract}
            return out
    return None


def _res(dep, status, detail):
    return {"id": dep["id"], "what": dep["what"], "where": dep["where"], "affects": list(dep["affects"]), "status": status, "detail": detail, "if_changed": dep["if_changed"]}


def _dep(i):
    return next(d for d in DEPENDENCIES if d["id"] == i)


def _check_base(rd):
    tree, err = _parse(rd, "agent/memory_provider.py")
    methods = _class_methods(tree, "MemoryProvider") if tree else None
    out = []
    if methods is None:
        why = "没有 agent/memory_provider.py" if err == "missing" else ("读不懂这个文件" if err == "unparsable" else "找不到 MemoryProvider 类")
        return [_res(_dep(i), "missing", why) for i in ("base_abstract", "base_methods", "window_switch_kwargs", "base_sync_turn")]
    abstract = {n for n, m in methods.items() if m["abstract"]}
    extra, gone = sorted(abstract - KNOWN_ABSTRACT), sorted(KNOWN_ABSTRACT - abstract)
    out.append(_res(_dep("base_abstract"), "ok" if not extra and not gone else "changed",
                    "必须实现的方法：%s" % sorted(abstract) if not extra and not gone else "多出来的必须实现的方法：%s；不再是必须的：%s" % (extra, gone)))
    bad = []
    for name, need in EXPECTED.items():
        m = methods.get(name)
        if m is None:
            bad.append("%s 不见了" % name)
            continue
        miss = [n for n in need if n != "**" and n not in m["params"] and "**" not in m["params"]] + (["**kwargs"] if "**" in need and "**" not in m["params"] else [])
        if miss:
            bad.append("%s 少了参数 %s" % (name, miss))
    out.append(_res(_dep("base_methods"), "ok" if not bad else "changed", "各方法的名字和参数对得上" if not bad else "；".join(bad)))
    ss = methods.get("on_session_switch")
    need = ["parent_session_id", "reset", "rewound", "**"]
    miss = [n for n in need if ss is None or n not in ss["params"]]
    out.append(_res(_dep("window_switch_kwargs"), "ok" if not miss else "changed", "on_session_switch 参数：%s" % ss["params"] if not miss else ("on_session_switch 不见了" if ss is None else "on_session_switch 少了 %s" % miss)))
    sm = methods.get("sync_turn")
    if sm is None:
        out.append(_res(_dep("base_sync_turn"), "changed", "sync_turn 不见了"))
    else:
        miss = [n for n in SYNC_PARAMS if n not in sm["params"] and "**" not in sm["params"]]
        out.append(_res(_dep("base_sync_turn"), "ok" if not miss else "changed", "sync_turn 参数：%s" % sm["params"] if not miss else "sync_turn 少了参数 %s" % miss))
    return out


def _calls(tree, attr):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr:
            yield n


def _check_host(rd):
    out = []
    dep = _dep("host_turn_start_kwargs")
    tree, err = _parse(rd, "agent/turn_context.py")
    if tree is None:
        out.append(_res(dep, "missing", "没有 agent/turn_context.py 或读不懂"))
    else:
        kws = set()
        for c in _calls(tree, "on_turn_start"):
            kws |= {k.arg for k in c.keywords if k.arg}
        need = {"author_id", "author_name", "author_is_bot"}
        out.append(_res(dep, "ok" if need <= kws else "changed", "宿主传的字段：%s" % sorted(kws) if need <= kws else "宿主调 on_turn_start 时没带 %s（老宿主）" % sorted(need - kws)))
    dep = _dep("host_sync_kwargs")
    tree, err = _parse(rd, "run_agent.py")
    if tree is None:
        out.append(_res(dep, "missing", "没有 run_agent.py 或读不懂"))
    else:
        seen, found = set(), False
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(True for _ in _calls(f, "sync_all")):
                found = True
                seen |= {n.value for n in ast.walk(f) if isinstance(n, ast.Constant) and isinstance(n.value, str)} | {k.arg for c in _calls(f, "sync_all") for k in c.keywords if k.arg}
        miss = [k for k in ("messages", "turn_author") if k not in seen]
        out.append(_res(dep, "missing" if not found else ("ok" if not miss else "changed"),
                        "找不到调 sync_all 的函数" if not found else ("宿主带的字段：messages、turn_author" if not miss else "宿主调 sync_all 时没带 %s" % miss)))
    dep = _dep("machinery_markers")
    flags, where = None, None
    for rel in ("agent/conversation_compression.py", "agent/message_metadata.py", "agent/context_compressor.py"):
        tree, err = _parse(rd, rel)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_SYNTHETIC_USER_FLAGS" for t in n.targets) and isinstance(n.value, (ast.Tuple, ast.List)):
                flags, where = [e.value for e in n.value.elts if isinstance(e, ast.Constant)], rel
    cc = rd("agent/context_compressor.py") or ""
    has_dk = bool(re.search(r"\.get\(\s*[\"']display_kind[\"']\s*\)", cc))
    if flags is None or not has_dk:
        out.append(_res(dep, "missing", "找不到 %s" % ("_SYNTHETIC_USER_FLAGS" if flags is None else "对 display_kind 的判断")))
    else:
        unknown = [f for f in flags if f not in OUR_SYNTHETIC_FLAGS and not f.endswith("_synthetic")]
        out.append(_res(dep, "ok" if not unknown else "changed", "宿主的合成标记都认得（共 %d 个，名字以 _synthetic 结尾的按模式也认）" % len(flags) if not unknown else "宿主有我们认不出的合成标记：%s" % unknown))
    return out


def _check_install(rd):
    out = []
    dep = _dep("plugin_discovery")
    loader, mem = rd("plugins/plugin_loader.py") or "", rd("plugins/memory/__init__.py")
    if mem is None:
        out.append(_res(dep, "missing", "没有 plugins/memory/__init__.py"))
    else:
        ok_dir = bool(re.search(r"get_hermes_home\(\)\s*/\s*[\"']plugins[\"']", loader + "\n" + mem))                  # 老版本在 plugins/memory/__init__.py 里，新版本挪到了 plugins/plugin_loader.py
        ok_reg = "register_memory_provider" in mem
        out.append(_res(dep, "ok" if ok_dir and ok_reg else "changed", "用户插件目录是 $HERMES_HOME/plugins，注册入口是 register_memory_provider" if ok_dir and ok_reg
                        else "变了：%s" % "；".join(x for x, f in (("用户插件目录不再是 $HERMES_HOME/plugins", ok_dir), ("找不到 register_memory_provider", ok_reg)) if not f)))
    dep = _dep("provider_activation")
    ms = rd("hermes_cli/memory_setup.py")
    if ms is None:
        out.append(_res(dep, "missing", "没有 hermes_cli/memory_setup.py"))
    else:
        ok = bool(re.search(r"\[[\"']memory[\"']\]\[[\"']provider[\"']\]", ms))
        out.append(_res(dep, "ok" if ok else "changed", "设置键是 memory.provider" if ok else "找不到 config[\"memory\"][\"provider\"]"))
    dep = _dep("config_format")
    ver = None
    for rel in ("hermes_cli/config_defaults.py", "hermes_cli/config.py"):
        t = rd(rel)
        m = re.search(r"[\"']_config_version[\"']\s*:\s*(\d+)", t or "")
        if m:
            ver = int(m.group(1))
            break
    if ver is None:
        out.append(_res(dep, "missing", "找不到 _config_version"))
    else:
        out.append(_res(dep, "ok" if ver in KNOWN_CONFIG_FORMATS else "changed", "设置格式版本 %d（核过的：%s）" % (ver, list(KNOWN_CONFIG_FORMATS)) if ver in KNOWN_CONFIG_FORMATS else "设置格式版本 %d，没核过（核过的：%s）" % (ver, list(KNOWN_CONFIG_FORMATS))))
    return out


def _check_workspace(rd):
    out = []
    dep = _dep("init_kwargs")
    tree, err = _parse(rd, "agent/agent_init.py")
    if tree is None:
        out.append(_res(dep, "missing", "没有 agent/agent_init.py 或读不懂"))
    else:
        best = None
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consts = {n.value for n in ast.walk(f) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                if {"hermes_home", "session_id"} <= consts and (best is None or len(consts) < len(best)):
                    best = consts
        if best is None:
            out.append(_res(dep, "changed", "找不到拼初始化参数的地方（含 hermes_home、session_id 的函数）"))
        else:
            miss = [k for k in ("agent_context", "platform", "cwd") if k not in best]
            out.append(_res(dep, "ok" if not miss else "changed", "初始化参数里有 agent_context、platform、cwd" if not miss else "初始化参数里没有 %s" % miss))
    dep = _dep("projects_db")
    t = rd("hermes_cli/projects_db.py")
    if t is None:
        out.append(_res(dep, "changed", "这一版没有项目库（hermes_cli/projects_db.py）：所有对话落进生活"))
    else:
        ok_path = bool(re.search(r"get_hermes_home\(\)\s*/\s*[\"']projects\.db[\"']", t))

        def cols(table):
            m = re.search(r"CREATE TABLE IF NOT EXISTS %s\s*\((.*?)\);" % table, t, re.S)
            return m.group(1) if m else ""
        pc, fc = cols("projects"), cols("project_folders")
        missing = [x for x, ok in (("路径 projects.db", ok_path), ("projects.id", bool(re.search(r"\bid\b", pc))), ("projects.name", bool(re.search(r"\bname\b", pc))), ("projects.archived", bool(re.search(r"\barchived\b", pc))),
                                   ("project_folders.project_id", bool(re.search(r"\bproject_id\b", fc))), ("project_folders.path", bool(re.search(r"\bpath\b", fc)))) if not ok]
        out.append(_res(dep, "ok" if not missing else "changed", "项目库路径和两张表的列都对得上" if not missing else "变了：%s" % "、".join(missing)))
    return out


def _function_node(tree, name, cls=None):
    for n in ast.walk(tree):
        if cls and isinstance(n, ast.ClassDef) and n.name == cls:
            for f in n.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name:
                    return f
        elif not cls and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _check_lazy(rd):
    """2026-11-11 真机试装修补用到的几处：两处懒导入、Python 环境的查法、add_provider 的顺序。这一版没有的功能记「changed」（和 projects_db 一样：功能不在，不是读不到）。"""
    out = []
    for dep_id, rel, fn, absent in (("owner_identity", "gateway/owner_identity.py", "host_owner_subject", "这一版没有 gateway/owner_identity.py 或里面没有 host_owner_subject：带登录身份的 tui／desktop 不当主人"),
                                    ("runtime_cwd", "agent/runtime_cwd.py", "resolve_agent_cwd", "这一版没有 agent/runtime_cwd.py 或里面没有 resolve_agent_cwd：退回进程目录")):
        dep = _dep(dep_id)
        tree, err = _parse(rd, rel)
        if tree is None:
            out.append(_res(dep, "changed", absent if err == "missing" else "读不懂 %s" % rel))
        else:
            out.append(_res(dep, "ok" if _function_node(tree, fn) else "changed", "%s 里有 %s()" % (rel, fn) if _function_node(tree, fn) else absent))
    dep = _dep("runtime_python")
    t = rd("hermes_cli/_launchers.py")
    if t is None:
        out.append(_res(dep, "changed", "这一版没有 hermes_cli/_launchers.py：问不到 Hermes 的真实 Python 环境"))
    else:
        miss = [x for x, ok in (("def print_runtime_command", "def print_runtime_command" in t), ("--print-runtime-command", "--print-runtime-command" in t), ("--module", "--module" in t)) if not ok]
        out.append(_res(dep, "ok" if not miss else "changed", "有 print_runtime_command、--print-runtime-command 和 --module" if not miss else "找不到 %s" % "、".join(miss)))
    dep = _dep("tool_schemas_at_registration")
    mm, _e1 = _parse(rd, "agent/memory_manager.py")
    ai, _e2 = _parse(rd, "agent/agent_init.py")
    if mm is None or ai is None:
        out.append(_res(dep, "missing", "没有 agent/memory_manager.py 或 agent/agent_init.py，或读不懂"))
    else:
        add = _function_node(mm, "add_provider", "MemoryManager")
        reads = bool(add and any(True for _ in _calls(add, "get_tool_schemas")))
        order = False
        for f in ast.walk(ai):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                adds = [c.lineno for c in _calls(f, "add_provider")]
                inits = [c.lineno for a in ("initialize_for_agent", "initialize_all") for c in _calls(f, a)]
                if adds and inits and min(adds) < min(inits):
                    order = True
        bad = [x for x, ok in (("add_provider 里不再调 get_tool_schemas", reads), ("agent_init 里不再是先 add_provider 再 initialize", order)) if not ok]
        out.append(_res(dep, "ok" if not bad else "changed", "add_provider 在 initialize 之前读工具清单；agent_init 先 add_provider 再 initialize" if not bad else "变了：%s" % "；".join(bad)))
    return out


def _check_plugin_deps(rd):
    """插件依赖声明机制（pm/ 下三处）。这一版没有这套机制（老版本）记「changed」，和别的「功能不在」一样。"""
    dep = _dep("plugin_python_deps")
    bad = []
    pd, e1 = _parse(rd, "pm/plugin_declarations.py")
    ps, e2 = _parse(rd, "pm/plugins_state.py")
    ws, e3 = _parse(rd, "pm/workspace.py")
    if pd is None or ps is None or ws is None:
        gone = [n for n, t, e in (("pm/plugin_declarations.py", pd, e1), ("pm/plugins_state.py", ps, e2), ("pm/workspace.py", ws, e3)) if t is None]
        return [_res(dep, "changed", "这一版没有（或读不懂）%s：没有插件依赖声明机制" % "、".join(gone))]
    consts = {n.value for n in ast.walk(pd) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    if _function_node(pd, "read_python_declaration") is None:
        bad.append("read_python_declaration 不见了")
    for key in ("python_dependencies", "pip_dependencies"):
        if key not in consts:
            bad.append("不再读 %s" % key)
    eo = _function_node(ps, "enabled_plugins_ordered")
    if _function_node(ps, "_provider_from_config") is None or eo is None or not any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_provider_from_config" for n in ast.walk(eo)):
        bad.append("当前记忆提供者不再并进成员表（_provider_from_config）")
    em = _function_node(ws, "enabled_member_dirs")
    if em is None or not any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "read_python_declaration" for n in ast.walk(em)) or not any(isinstance(n, ast.Attribute) and n.attr == "installs_packages" for n in ast.walk(em)):
        bad.append("enabled_member_dirs 不再按 read_python_declaration／installs_packages 选成员")
    ms = _function_node(ws, "members_stamp")
    if ms is None or not any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "read_python_declaration" for n in ast.walk(ms)):
        bad.append("members_stamp 不再把声明文件算进环境指纹")
    return [_res(dep, "ok" if not bad else "changed", "声明由 read_python_declaration 读（python_dependencies／pip_dependencies）；记忆提供者并进成员表；重建按成员表带上依赖、声明文件进环境指纹" if not bad else "变了：%s" % "；".join(bad))]


def _check_bots(rd):
    """机器人（profile）和群聊：群聊平台名、机器人作者标签。这一版没有的功能记「changed」（功能不在，不是读不到）。"""
    out = []
    dep = _dep("group_room_platform")
    tree, err = _parse(rd, "gateway/session_hosted_rpc.py")
    if tree is None:
        out.append(_res(dep, "changed", "这一版没有 gateway/session_hosted_rpc.py（或读不懂）：群聊平台名没核到" if err == "missing" else "读不懂 gateway/session_hosted_rpc.py"))
    else:
        found = any(k.arg == "platform" and isinstance(k.value, ast.Constant) and k.value.value == "bot_room" for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords)
        out.append(_res(dep, "ok" if found else "changed", "群聊那一轮的平台是 bot_room" if found else "找不到 platform='bot_room'：群聊平台名可能变了"))
    dep = _dep("bot_author_tag")
    tree, err = _parse(rd, "agent/turn_author.py")
    if tree is None:
        out.append(_res(dep, "changed", "这一版没有 agent/turn_author.py（或读不懂）：机器人作者标签没核到" if err == "missing" else "读不懂 agent/turn_author.py"))
    else:
        fn = _function_node(tree, "bot_author_id")
        has_prefix = bool(fn and any(isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("bot:") for n in ast.walk(fn)))
        has_flag = any(isinstance(n, ast.Constant) and n.value == "is_bot" for n in ast.walk(tree))
        bad = [x for x, ok in (("bot_author_id 不见了或不再返回 bot:…", has_prefix), ("字段 is_bot 不见了", has_flag)) if not ok]
        out.append(_res(dep, "ok" if not bad else "changed", "有 bot_author_id（bot:…）和 is_bot 字段" if not bad else "变了：%s" % "；".join(bad)))
    return out


def doctor_from_reader(rd, hermes_info=None):
    checks = _check_base(rd) + _check_host(rd) + _check_install(rd) + _check_workspace(rd) + _check_lazy(rd) + _check_bots(rd) + _check_plugin_deps(rd)
    order = {d["id"]: i for i, d in enumerate(DEPENDENCIES)}
    checks.sort(key=lambda c: order[c["id"]])
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "changed", "missing")}
    impact = sorted({a for c in checks if c["status"] != "ok" for a in c["affects"]})
    return {"hermes": hermes_info or {}, "checks": checks, "summary": summary, "affected_features": impact, "exit_code": 0 if summary["changed"] == 0 and summary["missing"] == 0 else 1}


def doctor(hermes_dir):
    if not os.path.isdir(hermes_dir):
        return {"error": "hermes_dir_not_found", "hermes": {}, "checks": [], "summary": {"ok": 0, "changed": 0, "missing": 0}, "affected_features": [], "exit_code": 2}

    def rd(rel):
        try:
            with open(os.path.join(hermes_dir, rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    info = {"dir": hermes_dir, "commit": None}
    try:
        with open(os.path.join(hermes_dir, "install-stamp.json"), encoding="utf-8") as f:
            info["commit"] = json.load(f).get("commit")
    except Exception:                                                                          # noqa: BLE001
        pass
    return doctor_from_reader(rd, info)


def config_format_note(hermes_home, host_format):
    """装的时候记下的设置格式版本，和宿主现在的比。"""
    try:
        with open(os.path.join(hermes_home, "yuanwei", "install.json"), encoding="utf-8") as f:
            rec = json.load(f).get("config_format_version")
    except Exception:                                                                          # noqa: BLE001
        rec = None
    changed = rec is not None and rec != host_format
    return {"recorded": rec, "host": host_format, "changed": changed,
            "advice": ("设置文件格式从 %s 变到 %s：重新运行 yuanwei setup hermes，核对 memory.provider 的位置没变并记下新格式版本" % (rec, host_format)) if changed else "没变"}
