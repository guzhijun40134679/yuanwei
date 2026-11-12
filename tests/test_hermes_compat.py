"""交回点乙·第 2 步：Hermes 升级不重写（用户的要求）。yuanwei doctor hermes＝读本机装着的 Hermes 源码（只读），逐项核我们依赖的接口，
输出「哪项对、哪项变了、影响哪些功能」。这里用一棵合成的 Hermes 目录树（只有我们依赖的那些文件、那几处文字）测每一项会不会报，再对本机真实目录和几个历史提交核一遍。
真实的 Hermes 目录只读源码；不碰 ~/.hermes 的配置、记忆、会话。"""
import json
import os
import subprocess
import textwrap

import pytest

from yuanwei import cli
from yuanwei import hermes_compat as HC

REAL = os.path.expanduser("~/.hermes/hermes-agent")

BASE = '''
import abc
from typing import Any, Optional

class MemoryProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self): ...
    @abc.abstractmethod
    def is_available(self): ...
    @abc.abstractmethod
    def initialize(self, session_id, **kwargs): ...
    @abc.abstractmethod
    def get_tool_schemas(self): ...
    def system_prompt_block(self): return ""
    def prefetch(self, query, *, session_id=""): return ""
    def queue_prefetch(self, query, *, session_id=""): ...
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None): ...
    def handle_tool_call(self, tool_name, args, **kwargs): raise NotImplementedError
    def shutdown(self): ...
    def on_turn_start(self, turn_number, message, **kwargs): ...
    def on_session_end(self, messages): ...
    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs): ...
'''
TURN_CONTEXT = '''
def _memory_turn_start_and_prefetch(agent, original_user_message, turn_author=None):
    _author = turn_author if isinstance(turn_author, dict) else {}
    agent._memory_manager.on_turn_start(
        agent._user_turn_count, "q",
        author_id=_author.get("id") or None, author_name=_author.get("name") or None,
        author_is_bot=bool(_author.get("is_bot")),
    )
'''
RUN_AGENT = '''
class A:
    def _sync_external_memory_for_turn(self, *, original_user_message, final_response, interrupted, messages=None):
        sync_kwargs = {"session_id": self.session_id or "", **({"messages": messages} if messages is not None else {})}
        turn_author = getattr(self, "_turn_author", None)
        if turn_author is not None:
            sync_kwargs["turn_author"] = turn_author
        self._memory_manager.sync_all("u", "r", **sync_kwargs)
'''
METADATA = '''
_SYNTHETIC_USER_FLAGS = (
    "_todo_snapshot_synthetic", "_empty_recovery_synthetic", "_verification_stop_synthetic", "_pre_verify_synthetic",
    "_dropped_toolcall_nudge",
)
'''
COMPRESSOR = '''
def real(message):
    return message.get("role") == "user" and not message.get("display_kind")
'''
LOADER = '''
def user_plugins_dir():
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "plugins"
    return d if d.is_dir() else None
'''
MEMORY_PLUGINS = '''
def _is_memory_provider_dir(path):
    return "register_memory_provider" in "x" or "MemoryProvider" in "x"
def find_provider_dir(name): ...
'''
MEMORY_SETUP = '''
def cmd(config, name):
    config["memory"]["provider"] = name
'''
DEFAULTS = '''
DEFAULT_CONFIG = {
    "_config_version": 52,  # Config schema version
}
'''
AGENT_INIT = '''
def _memory_provider_init_kwargs(agent, platform):
    kwargs = {
        "session_id": agent.session_id,
        "platform": platform or "cli",
        "hermes_home": "h",
        "agent_context": platform if platform in ("cron", "subagent") else "primary",
    }
    if agent.session_cwd:
        kwargs["cwd"] = agent.session_cwd
    return kwargs
'''
PROJECTS_DB = '''
def projects_db_path():
    return get_hermes_home() / "projects.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_folders (project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL, PRIMARY KEY (project_id, path));
"""

def project_for_path(conn, path): ...
'''
OWNER_IDENTITY = '''
def host_owner_subject() -> str:
    from gateway.session_worker_reservation import owner_worker_subject
    return owner_worker_subject()
'''
RUNTIME_CWD = '''
def resolve_agent_cwd():
    return _resolve_configured_cwd(override_is_final=False) or Path(os.getcwd())
'''
LAUNCHERS = '''
def print_runtime_command(repo_root, argv):
    parser.add_argument("--module", default="hermes_cli.main")
    print(json.dumps(runtime_command(repo_root, args, module=options.module)))

if sys.argv[1:2] == ["--print-runtime-command"]:
    print_runtime_command(Path(__file__).resolve().parents[1], sys.argv[2:])
'''
MEMORY_MANAGER = '''
class MemoryManager:
    def add_provider(self, provider):
        schemas = list(provider.get_tool_schemas())
        self._providers.append(provider)
    def initialize_for_agent(self, **kwargs): ...
'''
AGENT_INIT += '''

def build(agent, mp, platform):
    agent._memory_manager.add_provider(mp)
    agent._memory_manager.initialize_for_agent(**_memory_provider_init_kwargs(agent, platform))
'''
SESSION_HOSTED_RPC = '''
async def _create(self):
    policy = replace(policy, source='bot_room', platform='bot_room', model=policy.model)
'''
TURN_AUTHOR = '''
def parse_turn_author(raw):
    return {"id": raw["id"], "name": raw["name"], "is_bot": bool(raw.get("is_bot"))}

def bot_author_id(profile, origin=None):
    origin = (origin or "").strip()
    return f"bot:{origin}/{profile}" if origin else f"bot:{profile}"
'''
PM_DECLARATIONS = '''
def read_python_declaration(plugin_dir):
    manifest = {}
    specs = []
    for key in ("pip_dependencies", "python_dependencies"):
        specs.extend(manifest.get(key, []))
    return specs
'''
PM_PLUGINS_STATE = '''
def _provider_from_config(home, config, installing=None):
    return (config.get("memory") or {}).get("provider")

def enabled_plugins_ordered(proposed_home=None):
    out = {}
    for home in homes:
        provider = _provider_from_config(home, config)
        if provider:
            out[home / "plugins"] = [provider]
    return out
'''
PM_WORKSPACE = '''
def members_stamp(plugin_dirs):
    for entry in plugin_dirs:
        declaration = read_python_declaration(entry)

def enabled_member_dirs():
    members = []
    for path in selected:
        declaration = read_python_declaration(path)
        if declaration.installs_packages:
            members.append(path)
    return members
'''
FILES = {"pm/plugin_declarations.py": PM_DECLARATIONS, "pm/plugins_state.py": PM_PLUGINS_STATE, "pm/workspace.py": PM_WORKSPACE, "gateway/session_hosted_rpc.py": SESSION_HOSTED_RPC, "agent/turn_author.py": TURN_AUTHOR, "gateway/owner_identity.py": OWNER_IDENTITY, "agent/runtime_cwd.py": RUNTIME_CWD, "hermes_cli/_launchers.py": LAUNCHERS, "agent/memory_manager.py": MEMORY_MANAGER, "agent/agent_init.py": AGENT_INIT, "hermes_cli/projects_db.py": PROJECTS_DB, "agent/memory_provider.py": BASE, "agent/turn_context.py": TURN_CONTEXT, "run_agent.py": RUN_AGENT, "agent/message_metadata.py": METADATA,
         "agent/context_compressor.py": COMPRESSOR, "plugins/plugin_loader.py": LOADER, "plugins/memory/__init__.py": MEMORY_PLUGINS,
         "hermes_cli/memory_setup.py": MEMORY_SETUP, "hermes_cli/config_defaults.py": DEFAULTS}


def host(tmp_path, **edit):
    """合成的 Hermes 目录树。edit 里给 {相对路径: (旧文字, 新文字)} 就改一处；新文字 None 表示整个文件不存在。"""
    root = tmp_path / "hermes-agent"
    for rel, text in FILES.items():
        if rel in edit and edit[rel] is None:
            continue
        if rel in edit:
            old, new = edit[rel]
            assert old in text, (rel, old)
            text = text.replace(old, new)
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return str(root)


def status(report):
    return {c["id"]: c["status"] for c in report["checks"]}


def test_baseline_synthetic_host_everything_ok_and_each_check_names_its_features(tmp_path):
    r = HC.doctor(host(tmp_path))
    assert all(s == "ok" for s in status(r).values()), status(r)
    assert r["summary"]["changed"] == 0 and r["summary"]["missing"] == 0 and r["exit_code"] == 0
    assert all(c["affects"] and c["what"] and c["where"] for c in r["checks"])
    assert {c["id"] for c in r["checks"]} == {d["id"] for d in HC.DEPENDENCIES}                      # 检查项和依赖表一一对应


@pytest.mark.parametrize("edit,check,affects", [
    ({"run_agent.py": ('sync_kwargs["turn_author"] = turn_author', "pass")}, "host_sync_kwargs", "bot_filter"),
    ({"agent/turn_context.py": ("author_is_bot=bool(_author.get(\"is_bot\")),", "")}, "host_turn_start_kwargs", "bot_filter"),
    ({"agent/memory_provider.py": ("messages=None, turn_author=None", "turn_author=None")}, "base_sync_turn", "write"),
    ({"agent/memory_provider.py": ("def handle_tool_call(self, tool_name, args, **kwargs)", "def handle_tool_call(self, tool_name, args)")}, "base_methods", "tools"),
    ({"agent/memory_provider.py": ("def get_tool_schemas(self): ...", "def get_tool_schemas(self): ...\n    @abc.abstractmethod\n    def brand_new_required(self): ...")}, "base_abstract", "plugin_load"),
    ({"agent/message_metadata.py": ('"_dropped_toolcall_nudge",', '"_dropped_toolcall_nudge", "_brand_new_nudge",')}, "machinery_markers", "write"),
    ({"agent/context_compressor.py": ("not message.get(\"display_kind\")", "True")}, "machinery_markers", "write"),
    ({"plugins/plugin_loader.py": ('get_hermes_home() / "plugins"', 'get_hermes_home() / "somewhere"')}, "plugin_discovery", "install"),
    ({"plugins/memory/__init__.py": ("register_memory_provider", "register_something_else")}, "plugin_discovery", "install"),
    ({"hermes_cli/memory_setup.py": ('["memory"]["provider"]', '["memory"]["backend"]')}, "provider_activation", "install"),
    ({"hermes_cli/config_defaults.py": ('"_config_version": 52', '"_config_version": 99')}, "config_format", "install"),
    ({"agent/agent_init.py": ('"agent_context": platform if platform in ("cron", "subagent") else "primary",', "")}, "init_kwargs", "agent_context"),
    ({"agent/agent_init.py": ('        kwargs["cwd"] = agent.session_cwd', '        kwargs["somewhere"] = agent.session_cwd')}, "init_kwargs", "workspace"),
    ({"pm/plugin_declarations.py": ('"python_dependencies"', '"py_deps"')}, "plugin_python_deps", "numpy_install"),
    ({"pm/plugin_declarations.py": ("def read_python_declaration(plugin_dir)", "def read_declaration(plugin_dir)")}, "plugin_python_deps", "numpy_install"),
    ({"pm/plugins_state.py": ("provider = _provider_from_config(home, config)", "provider = None")}, "plugin_python_deps", "numpy_install"),
    ({"pm/workspace.py": ("if declaration.installs_packages:", "if True:")}, "plugin_python_deps", "numpy_install"),
    ({"pm/workspace.py": ("        declaration = read_python_declaration(entry)\n", "        declaration = None\n")}, "plugin_python_deps", "numpy_install"),
    ({"pm/workspace.py": None}, "plugin_python_deps", "numpy_install"),
    ({"gateway/session_hosted_rpc.py": ("platform='bot_room'", "platform='bot_chat'")}, "group_room_platform", "group_room"),
    ({"gateway/session_hosted_rpc.py": None}, "group_room_platform", "group_room"),
    ({"agent/turn_author.py": ('f"bot:{origin}/{profile}" if origin else f"bot:{profile}"', 'f"robot:{profile}"')}, "bot_author_tag", "bot_filter"),
    ({"agent/turn_author.py": ('return {"id": raw["id"], "name": raw["name"], "is_bot": bool(raw.get("is_bot"))}', 'return {"id": raw["id"], "name": raw["name"]}')}, "bot_author_tag", "bot_filter"),
    ({"agent/turn_author.py": None}, "bot_author_tag", "bot_filter"),
    ({"gateway/owner_identity.py": ("def host_owner_subject()", "def somebody_else()")}, "owner_identity", "login_owner"),
    ({"gateway/owner_identity.py": None}, "owner_identity", "login_owner"),
    ({"agent/runtime_cwd.py": ("def resolve_agent_cwd()", "def resolve_other_cwd()")}, "runtime_cwd", "workspace"),
    ({"hermes_cli/_launchers.py": ('"--print-runtime-command"', '"--print-something"')}, "runtime_python", "numpy_install"),
    ({"hermes_cli/_launchers.py": ('parser.add_argument("--module", default="hermes_cli.main")', "pass")}, "runtime_python", "numpy_install"),
    ({"agent/memory_manager.py": ("schemas = list(provider.get_tool_schemas())", "schemas = []")}, "tool_schemas_at_registration", "tools"),
    ({"agent/agent_init.py": ("    agent._memory_manager.add_provider(mp)\n    agent._memory_manager.initialize_for_agent(**_memory_provider_init_kwargs(agent, platform))",
                              "    agent._memory_manager.initialize_for_agent(**_memory_provider_init_kwargs(agent, platform))\n    agent._memory_manager.add_provider(mp)")}, "tool_schemas_at_registration", "tools"),
    ({"hermes_cli/projects_db.py": ('get_hermes_home() / "projects.db"', 'get_hermes_home() / "p.db"')}, "projects_db", "workspace"),
    ({"hermes_cli/projects_db.py": ("project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL, PRIMARY KEY (project_id, path)", "pid TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL, PRIMARY KEY (pid, path)")}, "projects_db", "workspace"),
    ({"hermes_cli/projects_db.py": ("archived INTEGER NOT NULL DEFAULT 0", "gone INTEGER NOT NULL DEFAULT 0")}, "projects_db", "workspace"),
])
def test_each_change_flips_exactly_its_check_and_names_what_it_affects(tmp_path, edit, check, affects):
    r = HC.doctor(host(tmp_path, **edit))
    st = status(r)
    bad = {k: v for k, v in st.items() if v != "ok"}
    assert list(bad) == [check], bad
    c = [x for x in r["checks"] if x["id"] == check][0]
    assert affects in c["affects"] and c["detail"] and r["exit_code"] == 1


def test_a_new_flag_named_synthetic_is_covered_by_pattern_so_it_stays_ok(tmp_path):
    r = HC.doctor(host(tmp_path, **{"agent/message_metadata.py": ('"_dropped_toolcall_nudge",', '"_dropped_toolcall_nudge", "_brand_new_synthetic",')}))
    assert status(r)["machinery_markers"] == "ok"


def test_missing_file_and_missing_directory_are_reported_not_crashed(tmp_path):
    r = HC.doctor(host(tmp_path, **{"hermes_cli/memory_setup.py": None}))
    assert status(r)["provider_activation"] == "missing"
    r2 = HC.doctor(str(tmp_path / "nowhere"))
    assert r2["exit_code"] == 2 and r2["error"] == "hermes_dir_not_found" and r2["checks"] == []


def test_unparsable_source_is_a_failed_check_not_ok(tmp_path):
    r = HC.doctor(host(tmp_path, **{"agent/memory_provider.py": ("class MemoryProvider", "class MemoryProvider(((")}))
    assert status(r)["base_abstract"] in ("missing", "changed") and r["exit_code"] == 1


def test_probe_features_turn_changed_dependencies_off_one_by_one():
    import abc

    class B1(abc.ABC):
        @abc.abstractmethod
        def name(self): ...
        @abc.abstractmethod
        def is_available(self): ...
        @abc.abstractmethod
        def initialize(self, session_id, **kw): ...
        @abc.abstractmethod
        def get_tool_schemas(self): ...
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None): ...
        def handle_tool_call(self, tool_name, args, **kw): ...
        def prefetch(self, query, *, session_id=""): ...
        def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kw): ...
        def on_turn_start(self, turn_number, message, **kw): ...
    f = HC.probe_features(B1)
    assert all(v["on"] for v in f["features"].values()) and f["extra_abstract"] == []

    class B2(B1):
        def sync_turn(self, user_content, assistant_content, *, session_id=""): ...                       # 宿主不传 messages、turn_author
        handle_tool_call = None
        @abc.abstractmethod
        def newly_required(self): ...
    f2 = HC.probe_features(B2)
    assert f2["features"]["write"]["on"] is False and "messages" in f2["features"]["write"]["reason"]
    assert f2["features"]["bot_filter"]["on"] is False and f2["features"]["tools"]["on"] is False
    assert f2["extra_abstract"] == ["newly_required"] and f2["features"]["prefetch"]["on"] is True


def test_probe_features_on_an_object_that_is_not_a_provider_base_turns_everything_off():
    f = HC.probe_features(object)
    assert not any(v["on"] for v in f["features"].values()) and f["base_ok"] is False


# ------------------------------------------------------------------ 对本机真实目录和历史提交
def git_reader(rev):
    def rd(rel):
        r = subprocess.run(["git", "-C", REAL, "show", "%s:%s" % (rev, rel)], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    return rd


@pytest.mark.skipif(not os.path.isdir(REAL), reason="没有本机 Hermes 源码")
def test_real_local_hermes_has_no_missing_dependency():
    r = HC.doctor(REAL)
    st = status(r)
    assert "missing" not in st.values(), st
    assert r["hermes"]["commit"] and r["exit_code"] in (0, 1)


@pytest.mark.skipif(not os.path.isdir(REAL), reason="没有本机 Hermes 源码")
@pytest.mark.parametrize("rev,changed", [("935c1e8962d43de3ef0adde30e80dda55d0247db", {"base_sync_turn", "host_turn_start_kwargs", "host_sync_kwargs", "config_format", "init_kwargs", "owner_identity", "runtime_python", "group_room_platform", "bot_author_tag", "plugin_python_deps"}),
                                         ("0e427d788d", {"owner_identity", "group_room_platform", "plugin_python_deps"}), ("2bd378ed1b", set())])
def test_pinned_revisions_read_through_git_match_the_compat_table(rev, changed):
    """兼容表第 9 节的三版对照：Dolphin 钉的 935c1e89 比后两版老——sync_turn 没有 turn_author、宿主不传作者字段、设置格式 40；v0.22.0 只缺 gateway.owner_identity（那时还没有登录身份比较），现在这一版十九项全对（v0.22.0 另外没有群聊的 session_hosted_rpc.py、插件依赖声明的成员选择那一段还是老写法）。"""
    if subprocess.run(["git", "-C", REAL, "cat-file", "-e", rev], capture_output=True).returncode != 0:
        pytest.skip("本机 git 里没有这个提交")
    r = HC.doctor_from_reader(git_reader(rev))
    st = status(r)
    assert "missing" not in st.values(), (rev, st)
    assert {k for k, v in st.items() if v != "ok"} == changed, (rev, st)


def test_cli_doctor_prints_json_and_exit_code(tmp_path, capsys):
    rc = cli.main(["doctor", "hermes", "--hermes-dir", host(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["summary"]["ok"] == len(HC.DEPENDENCIES)
    rc = cli.main(["doctor", "hermes", "--hermes-dir", host(tmp_path / "b", **{"run_agent.py": ('sync_kwargs["turn_author"] = turn_author', "pass")})])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["summary"]["changed"] == 1
    rc = cli.main(["doctor", "hermes", "--hermes-dir", str(tmp_path / "nowhere")])
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "hermes_dir_not_found"


def test_every_dependency_id_appears_in_the_compat_table():
    p = os.path.join(os.path.dirname(__file__), "..", "..", "research", "personamem-pilot", "human-memory-comparison-2026-09-28", "圆桌", "产品打包", "第二段", "兼容表-2026-11-09.md")
    if not os.path.exists(p):
        pytest.skip("兼容表不在这里（只在工作区里）")
    t = open(p, encoding="utf-8").read()
    assert [d["id"] for d in HC.DEPENDENCIES if "`%s`" % d["id"] not in t] == []


@pytest.mark.parametrize("old,new", [("rewound=False, ", ""), ("parent_session_id=\"\", ", ""), ("reset=False, ", ""), (", **kwargs): ...", "): ...")])
def test_window_switch_kwargs_check_flips_when_on_session_switch_loses_a_field(tmp_path, old, new):
    line = "def on_session_switch(self, new_session_id, *, parent_session_id=\"\", reset=False, rewound=False, **kwargs): ..."
    r = HC.doctor(host(tmp_path, **{"agent/memory_provider.py": (line, line.replace(old, new, 1))}))
    assert status(r)["window_switch_kwargs"] == "changed"
    assert "window" in [c for c in r["checks"] if c["id"] == "window_switch_kwargs"][0]["affects"]
