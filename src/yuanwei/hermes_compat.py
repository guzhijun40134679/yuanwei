"""我们依赖 Hermes 的哪些东西，以及怎么核（用户的要求：Hermes 每次升级，不要让我们重写很多东西）。

设计：
* **接头要薄**：插件文件只 import Hermes 公开的记忆插件基类（agent.memory_provider.MemoryProvider），规则全在 yuanwei 核心里。适配器另外**懒导入**几处 Hermes 内部模块（用到时才导入，导入或调用失败就退回保守做法，见下面 owner_identity、runtime_cwd、host_turn_cwd 三条；Python 环境那条是跑 hermes 命令，不是 import）。
* **按功能探测，不按版本号写死**：运行时（probe_features）看基类里有没有我们要用的方法、参数对不对；对不上的那一项功能关掉、原因写进状态，别的照常，Hermes 本身照常工作。
* **一条核对命令**：yuanwei doctor hermes 读本机装着的 Hermes 源码（只读，不执行、不碰 ~/.hermes 的配置记忆会话），对着下面 DEPENDENCIES 逐项核，输出哪项对、哪项变了、影响哪些功能。
* DEPENDENCIES 是唯一的清单：doctor、兼容表第 9 节、测试都从它来。要依赖新东西，先加到这里。
"""
import ast
import hashlib
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
    {"id": "memory_query_text", "what": "开始一轮前的 _memory_query_text／flatten_message_text 及其文字块规则与 v0.23.2 一致", "where": "agent/turn_context.py: _memory_query_text；agent/message_content.py: flatten_message_text 及辅助函数",
     "affects": ["write"], "if_changed": "图片轮次的开始文字可能变了；先核新拍平规则，配不上时不记并显示原因"},
    {"id": "memory_sync_text", "what": "同步前的 _summarize_user_message_for_log、图片块与 native／API 图片提示（含默认问句）构造规则，和交给记忆的那句话（original_user_message 取 persist_user_message）的取法，与 v0.23.2 一致", "where": "agent/codex_responses_adapter.py: _summarize_user_message_for_log、_iter_content_parts 及类型集合；agent/image_routing.py: build_native_content_parts；gateway/session_api_media.py: restore_api_images；agent/turn_context.py: build_turn_context 里的 original_user_message = persist_user_message if … else user_message",
     "affects": ["write"], "if_changed": "图片标记、图片提示的拼接或交给记忆的那句话的取法可能变了；配不上的带图轮次不记并显示原因，先核新形状再改，不能靠放宽匹配"},
    {"id": "clarify_result", "what": "助手问用户的 clarify 工具：结果行的 name／tool_name 是 clarify，正文是 {responses:[{question, choices_offered, status, user_response}], outcome[, notice]}；点选的答案已经换回原选项文字、去掉「(Recommended)」（0.1.0a8，清单第 53 条）", "where": "tools/clarify_tool.py: RECOMMENDED_LABEL、strip_recommended、_clean_answer、_response_status、_result",
     "affects": ["clarify_answers"], "if_changed": "提问卡里的回答收不到，这一轮其余照常记；clarify-counts.json 里会出现 clarify_dropped:unknown_shape（bad_json、host_rewritten 不是格式变了：前者多半是 Hermes 外发前的密钥清洗弄坏了 JSON，后者是裁剪、去重、溢出这类固定改写）。先核新格式再放宽解析，不能靠猜"},
    {"id": "clarify_turn_markers", "what": "认「这一轮从哪开始」用的两张表：Hermes 技能脚手架的开头（当一轮开头看）和 Hermes 自己追加的固定催促行（到达工具调用上限、压缩后续接；当脚手架看），和 hermes_clarify.TURN_START_PREFIXES、HOST_NUDGE_TEXTS 一致（0.1.0a8）", "where": "agent/skill_commands.py: _SKILL_INVOCATION_PREFIX、_AUTO_LOAD_PREFIX；agent/context_compressor.py: COMPRESSION_CONTINUATION_USER_CONTENT、_LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT、MAX_ITERATIONS_SUMMARY_REQUEST；agent/tool_guardrails.py: _build_result_reference_stub（同一轮重复结果引用的开头，第 3 轮加）",
     "affects": ["clarify_answers"], "if_changed": "技能轮里可能把上一轮的提问卡回答再存一次，或者碰到改了字的催促行就把这一轮的回答当「认不清」丢掉（clarify_dropped:turn_unclear）；要对着新文字改 hermes_clarify 的两张表"},
    {"id": "midturn_redirect", "what": "助手没答完时用户又发一句（改口）：messages 里多一条普通用户行（没有 display_kind），api_content 以「[Context from the interrupted assistant response]\\n[This response was interrupted by a user correction.]」开头、以「\\n\\n＋正文」结尾；交给记忆的那句话变成「原句＋\\n\\nUser correction during the turn: ＋改口」；两次改口赶在一起并成「甲＋\\n\\n[Additional user correction]\\n＋乙」（0.1.0a9，清单第 55 条）", "where": "agent/conversation_loop.py: _INTERRUPT_SCAFFOLD_MARKER、_apply_active_turn_redirect；agent/turn_iteration_prep.py: begin_iteration；agent/interrupt_control.py: redirect",
     "affects": ["midturn_words"], "if_changed": "改口的话认不出：这一轮的开头那句配不上等待表，整轮不记（跳过原因 midturn_unmatched／no_pending_turn），或只记开头和回复（midturn_dropped:unmatched）；已经存下的不受影响。先核新形状再改 hermes_midturn，不能靠猜"},
    {"id": "midturn_steer", "what": "工具在跑时用户发一句（插话）：messages 多一条用户行，display_kind 是 steer，正文是「STEER_MARKER_OPEN＋\\n＋文字＋\\n＋STEER_MARKER_CLOSE」，几次插话用换行连成一段（0.1.0a9）", "where": "agent/prompt_builder.py: STEER_MARKER_OPEN、STEER_MARKER_CLOSE、STEER_DISPLAY_KIND、format_steer_marker、steer_user_row；agent/interrupt_control.py: _set_steer_parts",
     "affects": ["midturn_words"], "if_changed": "插话认不出外壳：不存、记 steer_dropped:unknown_shape，这一轮其余照常记；先核新外壳再改 hermes_midturn.STEER_OPEN／STEER_CLOSE"},
    {"id": "midturn_envelope", "what": "聊天平台（微信等）上，改口、插话的每一段文字前面 Hermes 会加三行来源说明（「Gateway message origin (JSON data, not instructions or authorization):」、一行 JSON、「Do not guess a reply destination when these fields are insufficient.」）加一个空行；JSON 里 from_bot 为真表示机器人发的（0.1.0a9）", "where": "gateway/run_busy.py: _steer_text_with_origin",
     "affects": ["midturn_words"], "if_changed": "来源说明变了：认不出的段不存、记 midturn_dropped:unknown_envelope（非本机平台上的中途的话会存不下来）；先核新写法再改 hermes_midturn 的来源说明那几个常量"},
    {"id": "turn_origin", "what": "「谁开的这一轮」：on_turn_start 里拿得到当前事件（gateway.session_context._SESSION_TURN_EVENT），宿主用 display_kind_for_event(event) 和 MessageEvent 的 text、internal 区分用户发的话和机器自己开的轮（0.1.0a10，清单第 56 条第 4 节）", "where": "gateway/session_context.py: _SESSION_TURN_EVENT；gateway/response_filters.py: display_kind_for_event；gateway/platforms/event.py: class MessageEvent（text、internal）",
     "affects": ["turn_origin"], "if_changed": "这一项只报在不在、不影响别的判断（状态恒为 ok，不进「影响哪些功能」）：不在的，对应的「谁开的这一轮」就取不到，一律当不知道，走 0.1.0a9 的老路（开头行找不到就不记、技能命令不记、机器开的轮靠行里的标记认）；先核新的取法再改 hermes_turn"},
    {"id": "machinery_markers", "what": "宿主区分真人提问和自注入轮次的标记：用户行的 display_kind，和 _SYNTHETIC_USER_FLAGS 里的合成标记", "where": "agent/conversation_compression_scaffolding.py（v0.23.2 起）、agent/conversation_compression.py 或 agent/message_metadata.py（老版本）: _SYNTHETIC_USER_FLAGS；agent/context_compressor.py: display_kind",
     "affects": ["write"], "if_changed": "宿主多出来的、名字不以 _synthetic 结尾的合成标记我们认不出，会被当真人提问记下来：要把它加进 hermes_adapter.SYNTHETIC_USER_FLAGS"},
    {"id": "plugin_discovery", "what": "用户插件目录 $HERMES_HOME/plugins/<名字>/ 里的记忆提供者会被发现并加载（register_memory_provider）", "where": "plugins/plugin_loader.py（老版本在 plugins/memory/__init__.py）: get_hermes_home() / \"plugins\"；plugins/memory/__init__.py: register_memory_provider",
     "affects": ["install"], "if_changed": "setup 装不上：先看新的安装位置再改 hermes_setup.py 里的 PLUGIN_REL"},
    {"id": "provider_activation", "what": "memory.provider 这个设置键选定外部记忆提供者", "where": "hermes_cli/memory_setup.py: config[\"memory\"][\"provider\"]",
     "affects": ["install"], "if_changed": "setup/uninstall 改的键变了：改 hermes_setup.py 里的 MEMORY_PROVIDER_KEY"},
    {"id": "init_kwargs", "what": "宿主初始化插件时总会传 agent_context、platform、hermes_home，有会话工作目录就传 cwd", "where": "agent/agent_init_memory_provider.py（v0.23.2 起）或 agent/agent_init.py（老版本）: _memory_provider_init_kwargs（更老的版本是 initialize 前拼的 _init_kwargs）",
     "affects": ["agent_context", "workspace"], "if_changed": "agent_context 缺了：这个实例按「说不清」处理，不读不写（不当成主对话）；cwd 没给时（Hermes 只有会话有工作目录才传）：经典终端（平台 cli）取 Hermes 进程目录（优先 resolve_agent_cwd）照项目库判；其他本机入口（tui、desktop、local）先问 Hermes 这一轮的文件夹（见 host_turn_cwd），问不到才记进「生活」；网关聊天平台记进「生活」、不问；cwd 这个键整个不传了：经典终端也只靠进程目录，别的本机入口靠 host_turn_cwd，都不行就落进生活"},
    {"id": "projects_db", "what": "项目库：$HERMES_HOME/projects.db 的 projects(id, name, archived) 和 project_folders(project_id, path)；项目归属＝cwd 在文件夹里（最长的赢，归档的不算）", "where": "hermes_cli/projects_db.py: projects_db_path、SCHEMA_SQL、project_for_path",
     "affects": ["workspace"], "if_changed": "认不出项目：这一轮不读不写（不落进生活）；这一版没有项目功能：所有对话落进生活；要重核 hermes_workspace.resolve 的规则"},
    {"id": "owner_identity", "what": "gateway.owner_identity.host_owner_subject()：tui／desktop 带着登录身份时，拿它和宿主主人的系统身份比，一样才当主人（适配器懒导入，只比较、只在内存里用）", "where": "gateway/owner_identity.py: host_owner_subject",
     "affects": ["login_owner"], "if_changed": "没有这个函数或调不了：带登录身份的 tui／desktop 不当主人（login_identity_unverifiable），要在设置里配 owners 才记；经典终端和 local 不受影响"},
    {"id": "runtime_cwd", "what": "agent.runtime_cwd.resolve_agent_cwd()：智能体在哪个目录干活（适配器懒导入，经典终端没给 cwd 时用）", "where": "agent/runtime_cwd.py: resolve_agent_cwd",
     "affects": ["workspace"], "if_changed": "没有这个函数或调不了：退回 os.getcwd()（Hermes 进程目录），可能和配置的工作目录不一样"},
    {"id": "host_turn_cwd", "what": "agent.runtime_cwd.scoped_session_cwd()，取不到再 gateway.session_policy.active_policy().cwd：宿主没传 cwd 时（桌面版建助手不传，Hermes 上游 #144869），问 Hermes「这一轮的文件夹」（适配器懒导入，只给 local、tui、desktop 用；绝不用进程目录；2026-11-12，小问题 28）", "where": "agent/runtime_cwd.py: scoped_session_cwd；gateway/session_policy.py: active_policy",
     "affects": ["workspace"], "if_changed": "两个函数都没有：桌面版会退回记进「生活」；插件照常工作，这不是致命错误；只缺一个时至少还有一个候选接口在（有任意一个来源就报 ok，doctor 照实列出哪个在、哪个不在；实际能不能取到值、作用域对不对，要另外核）；等 Hermes 上游修好 #144869、直接传 cwd 以后，这一项就不再需要"},
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
    {"id": "prefetch_spill_limit", "what": "记忆插件预取结果的上限：设置 memory.prefetch_spill_enabled（默认关）、hooks.output_spill.max_chars（默认 DEFAULT_MAX_CHARS＝10,000）；关着时上限 max(max_chars×10, 100000)，开着时按 max_chars 截；设置由 hermes_cli.config.load_config() 读（插件在 Hermes 进程里只读这一处）", "where": "agent/memory_manager.py: 预取的 spill 判断（约 292–299 行）；tools/hook_output_spill.py: DEFAULT_MAX_CHARS；hermes_cli/config.py: load_config",
     "affects": ["prefetch"], "if_changed": "名字变了：便条在 Hermes 里的字符上限读不到，退回保守的 10,000 字符（英文便条只能装一部分词元额度），状态里写明；插件照常加载"},
    {"id": "config_format", "what": "设置文件的格式版本（_config_version）是我们核过的版本", "where": "hermes_cli/config_defaults.py（或 hermes_cli/config.py）: \"_config_version\"",
     "affects": ["install"], "if_changed": "只用 memory.provider 这一个键，格式变了多半不影响；但要重新核这个键的位置，并重跑 setup 记下新格式版本"},
]
KNOWN_CONFIG_FORMATS = (50, 52, 53)                  # 核过的设置格式版本：v0.22.0 之前是 50，v0.23.0 那一版是 52
# 53（v0.23.2）：52→53 唯一的迁移是 agent.clarify_timeout 3600→90（hermes_cli/config_migrations.py），不碰 memory.provider，所以兼容
KNOWN_ABSTRACT = frozenset(["name", "is_available", "initialize", "get_tool_schemas"])
EXPECTED = {                                          # 方法名 → 我们要用的参数名（"**" ＝要带 **kwargs）
    "initialize": ["session_id", "**"], "get_tool_schemas": [], "handle_tool_call": ["tool_name", "args", "**"], "prefetch": ["query", "session_id"],
    "on_turn_start": ["turn_number", "message", "**"], "on_session_switch": ["new_session_id", "parent_session_id", "reset", "rewound", "**"],
    "on_session_end": ["messages"], "shutdown": [], "system_prompt_block": [],
}
SYNC_PARAMS = ["user_content", "assistant_content", "session_id", "messages", "turn_author"]
OUR_SYNTHETIC_FLAGS = ("_todo_snapshot_synthetic", "_empty_recovery_synthetic", "_verification_stop_synthetic", "_pre_verify_synthetic", "_dropped_toolcall_nudge")
FEATURES = ("tools", "prefetch", "write", "bot_filter", "session_switch")
HOOK_CHARS_FALLBACK = 10000                           # 读不到 Hermes 设置时便条的保守字符上限（hooks.output_spill.max_chars 的默认值）


def prefetch_limit(load_config=None, default_max_chars=None):
    """Hermes 里记忆插件预取结果的字符上限，照 Hermes 自己的读法（只读 hermes_cli.config.load_config()，只在 Hermes 进程里能用）：
    memory.prefetch_spill_enabled 开了：上限＝hooks.output_spill.max_chars；没开（默认）：上限＝max(max_chars×10, 100000)；
    max_chars 没设就用 DEFAULT_MAX_CHARS（读不到就 10,000）。任何一步读不到、类型不对：退回保守的 10,000，并写明原因，**不抛错**（读设置失败不能影响插件加载）。
    返回 {"chars", "basis": no_spill｜spill｜fallback, "max_chars", "reason"}。load_config、default_max_chars 给测试注入。"""
    def fallback(reason):
        return {"chars": HOOK_CHARS_FALLBACK, "basis": "fallback", "max_chars": None, "reason": reason}
    try:
        if load_config is None:
            from hermes_cli.config import load_config                                     # noqa: PLC0415  只在 Hermes 进程里有
        cfg = load_config()
        if not isinstance(cfg, dict):
            return fallback("设置读出来不是字典")
        if default_max_chars is None:
            try:
                from tools.hook_output_spill import DEFAULT_MAX_CHARS as default_max_chars   # noqa: PLC0415
            except Exception:                                                              # noqa: BLE001
                default_max_chars = HOOK_CHARS_FALLBACK
        mem = cfg.get("memory")
        mem = mem if isinstance(mem, dict) else {}
        spill = mem.get("prefetch_spill_enabled", False)
        if spill is None:
            spill = False
        if not isinstance(spill, bool):
            return fallback("memory.prefetch_spill_enabled 不是布尔值")
        hooks = cfg.get("hooks")
        out = hooks.get("output_spill") if isinstance(hooks, dict) else None
        mx = out.get("max_chars") if isinstance(out, dict) else None
        if mx is None:
            mx = default_max_chars
        if not isinstance(mx, int) or isinstance(mx, bool) or mx <= 0:
            return fallback("hooks.output_spill.max_chars 不是正整数")
        if spill:
            return {"chars": mx, "basis": "spill", "max_chars": mx, "reason": None}
        return {"chars": max(mx * 10, 100000), "basis": "no_spill", "max_chars": mx, "reason": None}
    except Exception as e:                                                                 # noqa: BLE001
        return fallback("读不到 Hermes 设置（%s）" % type(e).__name__)


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
    for rel in ("agent/conversation_compression_scaffolding.py", "agent/conversation_compression.py", "agent/message_metadata.py", "agent/context_compressor.py"):   # 新模块在前：谁先定义这个集合就以谁为准
        tree, err = _parse(rd, rel)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_SYNTHETIC_USER_FLAGS" for t in n.targets) and isinstance(n.value, (ast.Tuple, ast.List)):
                flags, where = [e.value for e in n.value.elts if isinstance(e, ast.Constant)], rel
                break
        if flags is not None:
            break
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
    trees = [t for t in (_parse(rd, rel)[0] for rel in ("agent/agent_init_memory_provider.py", "agent/agent_init.py")) if t is not None]     # v0.23.2 把拼参数的函数挪到了新模块，老版本在 agent_init.py
    if not trees:
        out.append(_res(dep, "missing", "没有 agent/agent_init_memory_provider.py 也没有 agent/agent_init.py，或读不懂"))
    else:
        best, named = None, False
        for tree in trees:                                                         # 先找叫 _memory_provider_init_kwargs 的函数（谁先有算谁，找到了就以它为准）；都没有再退回按内容找
            for f in ast.walk(tree):
                if isinstance(f, ast.FunctionDef) and f.name == "_memory_provider_init_kwargs":
                    consts = {n.value for n in ast.walk(f) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                    best, named = (consts if {"hermes_home", "session_id"} <= consts else None), True
                    break
            if named:
                break
        for tree in () if named else trees:
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
    dep = _dep("host_turn_cwd")
    gone, have = [], []
    for rel, fn, label in (("agent/runtime_cwd.py", "scoped_session_cwd", "agent.runtime_cwd.scoped_session_cwd"), ("gateway/session_policy.py", "active_policy", "gateway.session_policy.active_policy")):
        tree, err = _parse(rd, rel)
        if tree is None:
            gone.append("%s（%s）" % (label, "没有 %s" % rel if err == "missing" else "读不懂 %s" % rel))
        elif _function_node(tree, fn) is None:
            gone.append("%s（%s 里没有 %s）" % (label, rel, fn))
        else:
            have.append(label)
    if not gone:
        out.append(_res(dep, "ok", "有 agent.runtime_cwd.scoped_session_cwd() 和 gateway.session_policy.active_policy()"))
    elif have:                                                                           # 至少还有一个候选接口在：ok（只说明接口存在，取值和作用域要另外核），照实说哪个在、哪个不在
        out.append(_res(dep, "ok", "只有 %s，至少还有一个候选接口在（实际能不能取到值、作用域对不对，要另外核）；找不到：%s" % ("、".join(have), "；".join(gone))))
    else:
        out.append(_res(dep, "changed", "两个都找不到：%s；桌面版会退回记进「生活」（插件照常工作，不是致命错误）" % "；".join(gone)))
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


def _check_prefetch_limit(rd):
    """预取上限相关的名字还在不在（只读源码文字，不执行）。老版本没有这套设置记「changed」，和别的「功能不在」一样。"""
    dep = _dep("prefetch_spill_limit")
    mm, hs, cf = rd("agent/memory_manager.py"), rd("tools/hook_output_spill.py"), rd("hermes_cli/config.py")
    gone = [n for n, t in (("agent/memory_manager.py", mm), ("tools/hook_output_spill.py", hs), ("hermes_cli/config.py", cf)) if t is None]
    if gone:
        return [_res(dep, "changed", "这一版没有（或读不了）%s：没有这套预取上限设置，便条的字符上限退回保守的 10,000" % "、".join(gone))]
    bad = []
    if "prefetch_spill_enabled" not in mm:
        bad.append("memory_manager.py 里没有 prefetch_spill_enabled")
    if "max_chars" not in mm and "max_chars" not in hs:
        bad.append("不再有 max_chars")
    if "DEFAULT_MAX_CHARS" not in hs:
        bad.append("hook_output_spill.py 里没有 DEFAULT_MAX_CHARS")
    if "def load_config" not in cf:
        bad.append("hermes_cli/config.py 里没有 load_config")
    return [_res(dep, "ok" if not bad else "changed", "预取上限的几个名字都在（prefetch_spill_enabled、max_chars、DEFAULT_MAX_CHARS、load_config）" if not bad else "变了：%s" % "；".join(bad))]


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



# 小修 38：只读源码 AST 指纹；不 import/执行宿主，不读配置。指纹包含两个拍平函数
# 的纯辅助函数与类型集合；函数变化（包括未认识的新形状）必须重新核，不能只核名字。
IMAGE_TEXT_CONTRACTS = {'agent/message_content.py': (['_NON_TEXT_PART_TYPES', '_TEXT_KEYS', '_field', '_text_from_part', 'flatten_message_text'], 'e944c2421954a691e549f077e1278c3b2546c0dd699f320225df0bd6e29d6926'), 'agent/turn_context.py': (['_memory_query_text'], '80dcae22be040f735eba8e3adf1750f2982352e6bac3d26a400461430b0f66db'), 'agent/codex_responses_adapter.py': (['_TEXT_PART_TYPES', '_IMAGE_PART_TYPES', '_nonempty_str', '_str_or_empty', '_part_type', '_iter_content_parts', '_summarize_user_message_for_log'], '23219f8667f68454824b2eea23c21f332083982784eab9f5aa3b1c67127fb9f0'), 'agent/image_routing.py': (['build_native_content_parts'], '749e912352669f80c0a966a0a8bf7e52b0c4e2faae39c9b86f61dc77ec9dc312'), 'gateway/session_api_media.py': (['restore_api_images'], '8f31ccdd86912673ce2b22cefa7d3fbd0d603c613fd887ec0f6a4b6a753f4040')}


def _contract_shape(node):
    if isinstance(node, ast.AST):
        # 兼容 Python 3.10–3.14：空 type_params 是解析器加的字段，不是宿主行为。
        return [type(node).__name__, [(k, _contract_shape(v)) for k, v in ast.iter_fields(node) if k != "type_params" or v]]
    if isinstance(node, list):
        return [_contract_shape(v) for v in node]
    return node


# 小修 38 补：宿主在 build_turn_context 里把「交给记忆的那句话」（开始登记和同步都用它）定成 persist_user_message，没有才用发给模型的那份。
# 这一行在大函数里，只按这一条语句的 AST 指纹核；找不到或变了，就不能再相信「记忆那头拿到的是用户打的字或一模一样的字＋提示」。
PERSIST_LINE_HASH = "9f56889f971af600f4b459e2eadfc02999776f76c01d398c39c9f53bcac5c7b1"


def _persist_line(tree):
    """build_turn_context 里 original_user_message 的赋值语句（只认唯一一条）的指纹；找不到返回 None，多于一条返回 "multiple"。"""
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "original_user_message" for t in n.targets)]
    if not nodes:
        return None
    if len(nodes) > 1:
        return "multiple"
    return hashlib.sha256(json.dumps(_contract_shape([nodes[0]]), ensure_ascii=True).encode()).hexdigest()


def _check_image_text(rd):
    out = []
    for dep_id, paths in (("memory_query_text", ("agent/message_content.py", "agent/turn_context.py")),
                          ("memory_sync_text", ("agent/codex_responses_adapter.py", "agent/image_routing.py", "gateway/session_api_media.py"))):
        missing, changed = [], []
        for rel in paths:
            tree, err = _parse(rd, rel)
            if tree is None:
                missing.append(rel)
                continue
            names, expected = IMAGE_TEXT_CONTRACTS[rel]
            nodes = [n for n in tree.body if (n.name if isinstance(n, ast.FunctionDef) else
                     n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) else None) in names]
            if len(nodes) != len(names):
                missing.append(rel + "（函数或类型集合不全）")
            elif hashlib.sha256(json.dumps(_contract_shape(nodes), ensure_ascii=True).encode()).hexdigest() != expected:
                changed.append(rel)
        if dep_id == "memory_sync_text":                                                       # 交给记忆的那句话取 persist_user_message 的那一行（agent/turn_context.py）
            tree, err = _parse(rd, "agent/turn_context.py")
            line = _persist_line(tree) if tree is not None else None
            if line is None:
                missing.append("agent/turn_context.py（找不到 original_user_message 取 persist_user_message 的那一行）")
            elif line != PERSIST_LINE_HASH:
                changed.append("agent/turn_context.py（original_user_message 的取法）")
        status = "missing" if missing else "changed" if changed else "ok"
        detail = ("没核：缺少或读不懂 " + "、".join(missing)) if missing else ("拍平规则或交给记忆的那句话的取法变了，没核新行为：" + "、".join(changed)) if changed else "拍平函数、图片提示和默认问句的构造、交给记忆的那句话的取法，与核过的 Hermes v0.23.2 一致（只读源码）"
        out.append(_res(_dep(dep_id), status, detail))
    return out


# 0.1.0a8（清单第 53 条）：clarify 结果的构造函数，只读源码 AST 指纹（对着 Hermes ae2a81ccc0／v0.23.2 算；2bd378ed1b 相同；0e427d788d 是下面认作「对」的旧指纹之一（_result 还没有 shown 换回，格式相同）；935c1e89 没有后三个函数）。
# doctor 只在线下核，不开关运行时的功能；运行时靠严格解析把关（hermes_clarify.parse，格式不对整条拒收）。
CLARIFY_CONTRACTS = {"tools/clarify_tool.py": (["RECOMMENDED_LABEL", "strip_recommended", "_clean_answer", "_response_status", "_result"], "16fda8ede5f7fe29c07923adab9d2ace527f449cb663d8fcd5b9d7fc243ed570")}
# 更早的两版（5eea87882a 到 v0.21.6；v0.21.7 到 v0.22.5）：结果格式和现在一样（解析照样认），只是 _result 还没把展示用的选项换回原文（shown），
# 点选的展示文字和原选项不一样时会被记成「用户答」。算 ok，说明里写清楚（复核第 14 条）。
CLARIFY_OLDER_OK = {"e079516ebc9bfd7683ba0d02ea52ea7d8a794f2b65a0e3f181852cee77ca62f6": "5eea87882a–v0.21.6",
                    "83dd409638091752734f514b8735296fff732d7493715e85666a3cf15d058b1c": "v0.21.7–v0.22.5"}


def _check_clarify(rd):
    missing, changed, older = [], [], []
    for rel, (names, expected) in CLARIFY_CONTRACTS.items():
        tree, err = _parse(rd, rel)
        if tree is None:
            missing.append(rel)
            continue
        nodes = [n for n in tree.body if (n.name if isinstance(n, ast.FunctionDef) else
                 n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) else None) in names]
        if len(nodes) != len(names):
            missing.append(rel + "（函数或常量不全）")
        else:
            got = hashlib.sha256(json.dumps(_contract_shape(nodes), ensure_ascii=True).encode()).hexdigest()
            if got in CLARIFY_OLDER_OK:
                older.append(CLARIFY_OLDER_OK[got])
            elif got != expected:
                changed.append(rel)
    status = "missing" if missing else "changed" if changed else "ok"
    detail = ("没核：缺少或读不懂 " + "、".join(missing)) if missing else ("clarify 结果的构造变了，没核新格式：" + "、".join(changed)) if changed else \
        ("clarify 结果的格式和 v0.23.2 相同，回答照收；这一版（%s）还没把展示用的选项换回原文，点选的展示文字和原选项不一样时会记成「用户答」（只读源码）" % "、".join(older)) if older else \
        "clarify 结果的构造（字段、状态、点选换回原选项、去掉推荐标签）与核过的 Hermes v0.23.2 一致（只读源码）"
    return [_res(_dep("clarify_result"), status, detail)]


def _joined_consts(node):
    """f-string 里的字面段和插值段：[("c", 字面)｜("v", 变量名或 None)]。"""
    out = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            out.append(("c", v.value))
        else:
            out.append(("v", v.value.id if isinstance(getattr(v, "value", None), ast.Name) else None))
    return out


def _joineds(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)] if fn is not None else []


def _dicts_with(fn, *keys):
    out = []
    for n in ast.walk(fn) if fn is not None else ():
        if isinstance(n, ast.Dict):
            ks = [k.value for k in n.keys if isinstance(k, ast.Constant)]
            if all(k in ks for k in keys):
                out.append((n, ks))
    return out


def _dict_values(d):
    return {k.value: v for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)}


def _check_midturn(rd):
    """0.1.0a9（清单第 55 条）：改口、插话、聊天平台来源说明三项，只读源码的 AST（不执行）；对不上只报、不猜。"""
    from . import hermes_midturn as HM
    out = []
    # ---- 改口
    dep, bad, miss = _dep("midturn_redirect"), [], []
    cl, e1 = _parse(rd, "agent/conversation_loop.py")
    tp, e2 = _parse(rd, "agent/turn_iteration_prep.py")
    ic, e3 = _parse(rd, "agent/interrupt_control.py")
    for rel, t in (("agent/conversation_loop.py", cl), ("agent/turn_iteration_prep.py", tp), ("agent/interrupt_control.py", ic)):
        if t is None:
            miss.append(rel)
    if not miss:
        marker = _literal_assigns(cl, ("_INTERRUPT_SCAFFOLD_MARKER",)).get("_INTERRUPT_SCAFFOLD_MARKER")
        want_marker = HM.REDIRECT_API_PREFIX.split("\n", 1)[1]
        fn = _function_node(cl, "_apply_active_turn_redirect")
        if marker != want_marker:
            bad.append("_INTERRUPT_SCAFFOLD_MARKER 变了或不在")
        if fn is None:
            bad.append("没有 _apply_active_turn_redirect")
        else:
            ctx = HM.REDIRECT_API_PREFIX.split("\n", 1)[0]
            ok_join = any(_joined_consts(j)[:1] == [("c", ctx + "\n")] and [c for c in _joined_consts(j) if c[0] == "c"] == [("c", ctx + "\n"), ("c", "\n\n")] and _joined_consts(j)[-1] == ("v", "text") for j in _joineds(fn))
            if not ok_join:
                bad.append("api_content 的拼法（开头两句、结尾「\\n\\n＋正文」）变了")
            rows = [(d, ks) for d, ks in _dicts_with(fn, "role", "content", "api_content")]
            if not rows:
                bad.append("改口用户行不再带 api_content")
            elif any("display_kind" in ks for _d, ks in rows):
                bad.append("改口用户行带了 display_kind（原委靠它没有来认改口行）")
            else:
                vals = _dict_values(rows[0][0])
                if not (isinstance(vals.get("role"), ast.Constant) and vals["role"].value == "user"):
                    bad.append("改口行的 role 不再是 user")
                if not (isinstance(vals.get("content"), ast.Name) and vals["content"].id == "text"):
                    bad.append("改口行的 content 不再就是用户的原话（text）")
            parts = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "checkpoint_parts" for t in n.targets)]
            if not parts or not (isinstance(parts[0].value, ast.List) and parts[0].value.elts and isinstance(parts[0].value.elts[0], ast.Name) and parts[0].value.elts[0].id == "_INTERRUPT_SCAFFOLD_MARKER"):
                bad.append("checkpoint_parts 不再以 _INTERRUPT_SCAFFOLD_MARKER 开头")
        bi = _function_node(tp, "begin_iteration")
        if bi is None or not any(_joined_consts(j) == [("v", "original_user_message"), ("c", HM.REDIRECT_SEP), ("v", "_redirect_text")] for j in _joineds(bi)):
            bad.append("begin_iteration 里拼「原句＋\\n\\nUser correction during the turn: ＋改口」的那一句变了")
        rd_fn = _function_node(ic, "redirect")
        if rd_fn is None or not any(_joined_consts(j) == [("v", "existing"), ("c", HM.REDIRECT_MERGE), ("v", "cleaned")] for j in _joineds(rd_fn)):
            bad.append("redirect 里合并两次改口的那一句变了")
    out.append(_res(dep, "missing" if miss else "changed" if bad else "ok",
                    ("没核：缺少或读不懂 " + "、".join(miss)) if miss else ("改口的形状变了，没核新行为：" + "；".join(bad)) if bad else
                    "改口行（api_content 两句开头、没有 display_kind）、拼进原句的「User correction during the turn: 」、合并两次改口的「[Additional user correction]」和 v0.23.2 一致（只读源码）"))
    # ---- 插话
    dep, bad, miss = _dep("midturn_steer"), [], []
    pb, e4 = _parse(rd, "agent/prompt_builder.py")
    for rel, t in (("agent/prompt_builder.py", pb), ("agent/interrupt_control.py", ic)):
        if t is None:
            miss.append(rel)
    if not miss:
        lit = _literal_assigns(pb, ("STEER_MARKER_OPEN", "STEER_MARKER_CLOSE", "STEER_DISPLAY_KIND"))
        for name, want in (("STEER_MARKER_OPEN", HM.STEER_OPEN), ("STEER_MARKER_CLOSE", HM.STEER_CLOSE), ("STEER_DISPLAY_KIND", HM.STEER_DISPLAY_KIND)):
            if lit.get(name) != want:
                bad.append("%s 变了或不在" % name)
        fm = _function_node(pb, "format_steer_marker")
        if fm is None or not any(_joined_consts(j) == [("c", "\n\n"), ("v", "STEER_MARKER_OPEN"), ("c", "\n"), ("v", "steer_text"), ("c", "\n"), ("v", "STEER_MARKER_CLOSE")] for j in _joineds(fm)):
            bad.append("format_steer_marker 的拼法变了")
        su = _function_node(pb, "steer_user_row")
        found = _dicts_with(su, "role", "content", "display_kind")
        if not found:
            bad.append("steer_user_row 不再给 display_kind")
        else:
            d = found[0][0]
            vals = _dict_values(d)
            if not (isinstance(vals.get("role"), ast.Constant) and vals["role"].value == "user"):
                bad.append("steer_user_row 的 role 不再是 user")
            if not (isinstance(vals["display_kind"], ast.Name) and vals["display_kind"].id == "STEER_DISPLAY_KIND"):
                bad.append("steer_user_row 的 display_kind 不是 STEER_DISPLAY_KIND")
            c = vals["content"]
            if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "lstrip" and isinstance(c.func.value, ast.Call)
                    and isinstance(c.func.value.func, ast.Name) and c.func.value.func.id == "format_steer_marker"):
                bad.append("steer_user_row 的正文不再是 format_steer_marker(…).lstrip()")
        sp = _function_node(ic, "_set_steer_parts")
        if sp is None or not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "join" and isinstance(n.func.value, ast.Constant) and n.func.value.value == "\n" for n in ast.walk(sp)):
            bad.append("几次插话不再用换行连成一段（_set_steer_parts）")
    out.append(_res(dep, "missing" if miss else "changed" if bad else "ok",
                    ("没核：缺少或读不懂 " + "、".join(miss)) if miss else ("插话的形状变了，没核新行为：" + "；".join(bad)) if bad else
                    "插话行（display_kind＝steer、两个外壳常量、换行连成一段）和 v0.23.2 一致（只读源码）"))
    # ---- 聊天平台的来源说明
    dep, bad = _dep("midturn_envelope"), []
    rb, e5 = _parse(rd, "gateway/run_busy.py")
    if rb is None:
        out.append(_res(dep, "missing", "没核：缺少或读不懂 gateway/run_busy.py"))
    else:
        fn = _function_node(rb, "_steer_text_with_origin")
        want = [("c", HM.ENVELOPE_HEAD + "\n"), ("v", "encoded"), ("c", "\n" + HM.ENVELOPE_FOOT + "\n\n"), ("v", "text")]
        if fn is None:
            bad.append("没有 _steer_text_with_origin")
        else:
            if not any(_joined_consts(j) == want for j in _joineds(fn)):
                bad.append("三行来源说明的字、JSON 那一行的位置或结尾的空行变了")
            if not any(isinstance(n, ast.Constant) and n.value == "from_bot" for n in ast.walk(fn)):
                bad.append("JSON 里不再有 from_bot")
            dumps = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "dumps"]
            if not dumps:
                bad.append("来源说明那一行不再用 json.dumps 写")
            elif any(k.arg == "indent" for n in dumps for k in n.keywords):
                bad.append("json.dumps 加了 indent（来源说明的 JSON 不再是一行）")
        out.append(_res(dep, "changed" if bad else "ok", ("来源说明变了，没核新行为：" + "；".join(bad)) if bad else
                        "三行来源说明（首行、JSON 一行、末行）加空行和 from_bot 与 v0.23.2 一致（只读源码）"))
    return out


def _literal_assigns(tree, names):
    """模块顶层 名字 = 字面量 的值；不是字面量或找不到的不收。"""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            try:
                out[n.targets[0].id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    return out


def _dedupe_stub_prefix(rd):
    """agent/tool_guardrails.py 里 _build_result_reference_stub 第一个以「[hermes note:」开头的 f-string 的开头字面；找不到函数返回 None，函数在但没有这种字面返回 ""。"""
    tree, err = _parse(rd, "agent/tool_guardrails.py")
    if tree is None:
        return None
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_result_reference_stub"), None)
    if fn is None:
        return None
    for n in ast.walk(fn):
        if isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant) and isinstance(n.values[0].value, str) and n.values[0].value.startswith("[hermes note:"):
            return n.values[0].value
    return ""


def _check_clarify_markers(rd):
    from . import hermes_clarify as HCL
    want = (("agent/skill_commands.py", ("_SKILL_INVOCATION_PREFIX", "_AUTO_LOAD_PREFIX"), set(HCL.TURN_START_PREFIXES)),
            ("agent/context_compressor.py", ("COMPRESSION_CONTINUATION_USER_CONTENT", "_LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT", "MAX_ITERATIONS_SUMMARY_REQUEST"), set(HCL.HOST_NUDGE_TEXTS)))
    missing, changed = [], []
    for rel, names, ours in want:
        tree, err = _parse(rd, rel)
        if tree is None:
            missing.append(rel)
            continue
        got = _literal_assigns(tree, names)
        if len(got) != len(names):
            missing.append("%s（缺 %s）" % (rel, "、".join(x for x in names if x not in got)))
        elif set(got.values()) != ours:
            changed.append(rel)
    stub = _dedupe_stub_prefix(rd)                                                           # 同一轮里一字不差的重复结果换成的引用：开头还是不是 HOST_REWRITE_PREFIXES 里那一条
    if stub is None:
        missing.append("agent/tool_guardrails.py（找不到 _build_result_reference_stub）")
    elif not stub.startswith(HCL.HOST_REWRITE_PREFIXES[-1]):
        changed.append("agent/tool_guardrails.py（重复结果引用的开头）")
    status = "missing" if missing else "changed" if changed else "ok"
    detail = ("没核：缺少或读不懂 " + "、".join(missing)) if missing else ("认一轮开头用的文字变了，没核：" + "、".join(changed)) if changed else \
        "技能脚手架的开头、固定催促行的文字、同一轮重复结果的引用开头和原委的表一致（只读源码）"
    return [_res(_dep("clarify_turn_markers"), status, detail)]


def _check_turn_origin(rd):
    """只报在不在（只读源码的 AST，不执行）：三处缺哪一处、哪处读不到，都写进说明；状态恒为 ok，不影响别的判断。"""
    present, absent = [], []
    tree, err = _parse(rd, "gateway/session_context.py")
    if tree is None:
        absent.append("gateway/session_context.py（%s）" % ("读不到" if err == "missing" else "读不懂"))
    else:
        names = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)} | \
                {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
        (present if "_SESSION_TURN_EVENT" in names else absent).append("_SESSION_TURN_EVENT")
    tree, err = _parse(rd, "gateway/response_filters.py")
    if tree is None:
        absent.append("gateway/response_filters.py（%s）" % ("读不到" if err == "missing" else "读不懂"))
    else:
        (present if _function_node(tree, "display_kind_for_event") is not None else absent).append("display_kind_for_event")
    tree, err = _parse(rd, "gateway/platforms/event.py")
    if tree is None:
        absent.append("gateway/platforms/event.py（%s）" % ("读不到" if err == "missing" else "读不懂"))
    else:
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "MessageEvent"), None)
        if cls is None:
            absent.append("MessageEvent")
        else:
            fields = {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            for f in ("text", "internal"):
                (present if f in fields else absent).append("MessageEvent.%s" % f)
    detail = ("在：" + "、".join(present) if present else "在：无") + ("；不在或读不到：" + "、".join(absent) + "（只报在不在，不影响别的判断；缺的那部分「谁开的这一轮」取不到，当不知道）" if absent else "（只报在不在，只读源码）")
    return [_res(_dep("turn_origin"), "ok", detail)]


def doctor_from_reader(rd, hermes_info=None):
    checks = _check_base(rd) + _check_host(rd) + _check_install(rd) + _check_workspace(rd) + _check_lazy(rd) + _check_bots(rd) + _check_plugin_deps(rd) + _check_prefetch_limit(rd) + _check_image_text(rd) + _check_clarify(rd) + _check_clarify_markers(rd) + _check_midturn(rd) + _check_turn_origin(rd)
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
