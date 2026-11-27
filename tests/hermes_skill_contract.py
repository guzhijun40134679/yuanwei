"""Hermes v0.23.2（ae2a81ccc0）里技能命令的脚手架：开头那一串固定字样、单个技能的构造函数、宿主自己取字的函数，逐字冻结（只读 agent/skill_commands.py，**不 import 宿主**）。
清单第 56 条第一批第 7 节的测试用它造真实形状的技能整段文字，并用宿主自己的取字函数核「宿主交给记忆的字」。
SOURCES：名字 → 片段（原文）；FINGERPRINTS：片段的 sha256。构造器用 exec 在空命名空间里把片段跑起来；宿主里读磁盘、读配置的几个函数用最小的假替身（见 _ns）。"""
import hashlib
import re
import sys
import types
from pathlib import Path
from typing import Any, Optional

SOURCES = {
    '_SKILL_INVOCATION_PREFIX': '_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "\n',
    '_SINGLE_SKILL_MARKER': '_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"\n',
    '_SINGLE_SKILL_INSTRUCTION': '_SINGLE_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "\n',
    '_RUNTIME_NOTE': '_RUNTIME_NOTE = "\\n\\n[Runtime note:"\n',
    '_BUNDLE_MARKER': '_BUNDLE_MARKER = " skill bundle,"\n',
    '_BUNDLE_USER_INSTRUCTION': '_BUNDLE_USER_INSTRUCTION = "\\nUser instruction: "\n',
    '_BUNDLE_FIRST_SKILL_BLOCK': '_BUNDLE_FIRST_SKILL_BLOCK = "\\n\\n[Loaded as part of the "\n',
    '_SKILL_DIR_NOTE': '_SKILL_DIR_NOTE = (\n    "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "\n    "`templates/config.yaml`) against that directory, then run them "\n    "with the terminal tool using the absolute path."\n)\n',
    '_SUPPORTING_FILES_END': '_SUPPORTING_FILES_END = "/scripts/foo.js`)."\n',
    '_SETUP_SKIPPED_NOTE': '_SETUP_SKIPPED_NOTE = (\n    "Required environment setup was skipped. Continue loading the skill "\n    "and explain any reduced functionality if it matters."\n)\n',
    'append_user_instruction': 'def append_user_instruction(parts: list, instruction: str) -> str:\n    """Append the instruction line to ``parts``; return the stable prefix, which\n    ends exactly at the instruction marker so (registered with\n    ``agent.prompt_cache_boundary``) the cache planner can break on the scaffold.\n    Single construction site guarantees the prefix is a byte-prefix of the message.\n\n    Shared by every builder that ends a static skill scaffold with the caller-supplied volatile instruction\n    (single-skill invocations, cron job prompts). Keeping construction in one place guarantees the\n    registered prefix stays a byte-prefix of the built message — the invariant the request-time split\n    depends on. See #81867.\n    """\n    stable_prefix = "\\n".join(parts) + "\\n" + _SINGLE_SKILL_INSTRUCTION\n    parts.append(f"{_SINGLE_SKILL_INSTRUCTION}{instruction}")\n    return stable_prefix\n',
    '_cut_after': 'def _cut_after(message: str, marker: str, stop_marker: str, find) -> Optional[str]:\n    """Text between *marker* (located with ``find``) and *stop_marker*, stripped; None if absent/empty."""\n    marker_idx = find(marker)\n    if marker_idx < 0:\n        return None\n    return message[marker_idx + len(marker):].split(stop_marker, 1)[0].strip() or None\n',
    'extract_user_instruction_from_skill_message': 'def extract_user_instruction_from_skill_message(content: Any) -> Optional[str]:\n    """Recover the user\'s instruction from a slash-skill-expanded or gateway\n    auto-load turn: the string unchanged when it is NOT scaffolding, the\n    extracted instruction when the scaffolding carried one, or ``None`` for a\n    bare ``/skill`` invocation."""\n    if not isinstance(content, str):\n        return None\n    if _AUTO_LOAD_HEADER_RE.match(content):\n        # Gateway auto-load scaffolding wraps the user\'s typed text in the same\n        # model-facing turn as the loaded skill bodies; keep only the typed text.\n        # A complete header is required so typed text that merely starts like one\n        # stays intact; a bare load carries no instruction.\n        return _auto_load_typed_text(content) or None\n    if not content.startswith(_SKILL_INVOCATION_PREFIX):\n        return content\n    if _BUNDLE_MARKER in content:\n        # Bundles put the instruction before the loaded skills: FIRST marker is the user\'s.\n        return _cut_after(content, _BUNDLE_USER_INSTRUCTION, _BUNDLE_FIRST_SKILL_BLOCK, content.find)\n    if _SINGLE_SKILL_MARKER in content:\n        # The instruction follows the skill body (which may quote the marker): LAST marker is the user\'s.\n        return _cut_after(content, _SINGLE_SKILL_INSTRUCTION, _RUNTIME_NOTE, content.rfind)\n    return None\n',
    '_scaffold_header': 'def _scaffold_header(\n    subject: str, loaded_names: list[str], *, lead_lines: list[str] | None = None,\n    missing: list[str] | None = None, disabled: list[str] | None = None,\n    extra_instruction: str = "", user_instruction: str = "",\n) -> str:\n    """Header for multi-skill messages (bundles and stacked invocations).\n    ``subject`` must end in " skill bundle" so the bundle-format extractor applies."""\n    lines = [\n        f"[IMPORTANT: The user has invoked the {subject}, "\n        f"loading {len(loaded_names)} skills together. Treat every skill below "\n        "as active guidance for this turn.]",\n        "",\n        *(lead_lines or []),\n        f"Skills loaded: {\', \'.join(loaded_names)}",\n    ]\n    if missing:\n        lines.append(f"Skills missing (skipped): {\', \'.join(missing)}")\n    if disabled:\n        lines.append(f"Skills disabled for this platform (skipped): {\', \'.join(disabled)}")\n    if extra_instruction:\n        lines += ["", f"Bundle instruction: {extra_instruction}"]\n    if user_instruction:\n        lines += ["", f"User instruction: {user_instruction}"]\n    return "\\n".join(lines)\n',
    '_build_skill_message': 'def _build_skill_message(\n    loaded_skill: dict[str, Any],\n    skill_dir: Path | None,\n    activation_note: str,\n    user_instruction: str = "",\n    runtime_note: str = "",\n    session_id: str | None = None,\n) -> str:\n    """Format a loaded skill into a user/system message payload."""\n    from tools.skills_tool import _skills_dir\n    # Preprocess first so downstream blocks see the expanded content.\n    content = preprocess_skill_content(\n        str(loaded_skill.get("content") or ""), skill_dir, session_id, skills_cfg=_load_skills_config(),\n    )\n    parts = [activation_note, "", content.strip()]\n    # Absolute skill dir lets the agent run bundled scripts without a skill_view() round-trip.\n    if skill_dir:\n        parts += ["", f"[Skill directory: {skill_dir}]", _SKILL_DIR_NOTE]\n    _inject_skill_config(loaded_skill, parts)\n    setup_note = _setup_note(loaded_skill)\n    if setup_note:\n        parts += ["", f"[Skill setup note: {setup_note}]"]\n    supporting = _supporting_files(loaded_skill, skill_dir)\n    if supporting and skill_dir:\n        try:\n            skill_view_target = str(skill_dir.relative_to(_skills_dir()))\n        except ValueError:\n            skill_view_target = str(loaded_skill.get("name") or skill_dir.name)\n        parts += ["", "[This skill has supporting files (paths relative to the skill directory above):]"]\n        parts += [f"- {sf}" for sf in supporting]\n        parts.append(\n            f\'\\nLoad any of these with skill_view(name="{skill_view_target}", \'\n            f\'file_path="<path>"), or run scripts directly by absolute path \'\n            f"(e.g. `node {skill_dir}{_SUPPORTING_FILES_END}"\n        )\n    stable_prefix = None\n    if user_instruction:\n        parts.append("")\n        # Everything before the volatile instruction is a stable scaffold; the\n        # registered boundary lets the cache planner break there (see append_user_instruction).\n        # Everything before the caller-supplied instruction is a stable scaffold; declare the exact boundary\n        # so the Anthropic cache planner can put a breakpoint on it instead of caching the whole message as\n        # one atomic block (#81867). The static instruction prose stays on the stable side; the volatile\n        # instruction (webhook payload, ticket IDs, timestamps) and any runtime note ride in the tail.\n        stable_prefix = append_user_instruction(parts, user_instruction)\n    if runtime_note:\n        parts += ["", f"[Runtime note: {runtime_note}]"]\n    message = "\\n".join(parts)\n    if stable_prefix is not None and message.startswith(stable_prefix) and len(message) > len(stable_prefix):\n        register_stable_prefix(stable_prefix)\n    return message\n',
    '_supporting_files': 'def _supporting_files(loaded_skill: dict[str, Any], skill_dir: Path | None) -> list[str]:\n    """Skill-relative support file paths: from ``linked_files`` or a disk walk."""\n    linked = (loaded_skill.get("linked_files") or {}).values()\n    supporting = [Path(entry).as_posix() for entries in linked if isinstance(entries, list) for entry in entries]\n    if not supporting and skill_dir:\n        for subdir in ("references", "templates", "scripts", "assets"):\n            files = sorted((skill_dir / subdir).rglob("*"))\n            # as_posix so listed paths match the footer\'s examples (scripts/foo.js)\n            # on every OS — str(relative_to) emits backslashes on Windows.\n            supporting += [f.relative_to(skill_dir).as_posix() for f in files if f.is_file() and not f.is_symlink()]\n    return supporting\n',
    '_setup_note': 'def _setup_note(loaded_skill: dict[str, Any]) -> Optional[str]:\n    if loaded_skill.get("setup_skipped"):\n        return _SETUP_SKIPPED_NOTE\n    return loaded_skill.get("gateway_setup_hint") or (\n        loaded_skill.get("setup_note") if loaded_skill.get("setup_needed") else None\n    ) or None\n',
}

FINGERPRINTS = {
    '_SKILL_INVOCATION_PREFIX': '6780793049adc80ccf4c5aa2f9ceb1d867359fa6324270fc0deafa556b17928b',
    '_SINGLE_SKILL_MARKER': '2b9b0e2ccf119e91490bac94e56250bcdfcde00a6e9a8d21c57b3b84e622803f',
    '_SINGLE_SKILL_INSTRUCTION': '8508743568a6594a7bdba22d2163d42485bb65b5023bd65ab893848e050e76fb',
    '_RUNTIME_NOTE': '795075ff5380564279dad71d48db63adf52bea86ccdfd8690b79670573e8bb4d',
    '_BUNDLE_MARKER': 'b12552541992f581f4440719cb0bca9eac9a88cd80aa16d832643ebf66ea2f0a',
    '_BUNDLE_USER_INSTRUCTION': '35ca5de50d83091b1ef2d510d9da4afe3c01ef8405eee90f1083aa74ae8fac23',
    '_BUNDLE_FIRST_SKILL_BLOCK': 'c8ce38c422a9c9740f2535b724d9bf8e125201c500f6911469e4dd9e7ec688e8',
    '_SKILL_DIR_NOTE': '3deb47c46f88024023e547fd9d8c6377e8420ee85172925001c652c74946b83a',
    '_SUPPORTING_FILES_END': 'f20097b09aeef65d311ce9e1d698f0f64d7434631945018f94c14d2e41599770',
    '_SETUP_SKIPPED_NOTE': 'b10897eb95477e72465d5bad625429b873373543e85f332ae217fc35abd172a7',
    'append_user_instruction': '7d8128b3f4b284dbc77a67427b6925771994142021fa3d5f4c8899a4209b796e',
    '_cut_after': '8aba241d84f2f72fafea098a7c15fc06226bd9c1ae2a63e7de29b62e742ac3dc',
    'extract_user_instruction_from_skill_message': '9db0f8d435a3b6ac141b36977435baf1c2c88ba61c81790cd036fd37e29c5953',
    '_scaffold_header': 'fd966b895dfd576416ab6a091539116ad059efc96a52d11b228bdbaa83cf3429',
    '_build_skill_message': '3f3f0ca0c623481bebfa79c24e6345cc03a7763e8e7820c6b005033d38add346',
    '_supporting_files': '3cc97f30829b88203c2503b69b2fa58f4322d305bbec385ec0edfe03d3f48e7b',
    '_setup_note': '6e7f3403757e9124a1275fab760f6d5c579cce8bbac68fa174485fb48f43b23e',
}


def check_fingerprints():
    return [k for k, t in SOURCES.items() if hashlib.sha256(t.encode()).hexdigest() != FINGERPRINTS[k]]


def _ns():
    """把冻结的片段跑起来的空命名空间。宿主里读磁盘、读配置的函数用最小的假替身：
    预处理不改正文、没有技能配置、没有自动加载（取字函数里自动加载那一支永远不命中）、登记稳定前缀什么也不做。"""
    ns = {"Any": Any, "Optional": Optional, "Path": Path, "re": re,
          "_AUTO_LOAD_HEADER_RE": re.compile(r"(?!x)x"), "_auto_load_typed_text": lambda c: "",
          "preprocess_skill_content": lambda content, skill_dir, session_id, skills_cfg=None: content,
          "_load_skills_config": lambda: {}, "_inject_skill_config": lambda loaded, parts: None,
          "register_stable_prefix": lambda prefix: None}
    for name in sorted(SOURCES, key=lambda n: (not n.startswith("_") or not n.isupper(), n)):
        if name.isupper() or (name.startswith("_") and name[1:].isupper()):
            exec(compile(SOURCES[name], name, "exec"), ns)
    for name in ("append_user_instruction", "_cut_after", "extract_user_instruction_from_skill_message", "_scaffold_header", "_supporting_files", "_setup_note", "_build_skill_message"):
        exec(compile(SOURCES[name], name, "exec"), ns)
    return ns


_NS = _ns()
SKILL_DIR_NOTE = _NS["_SKILL_DIR_NOTE"]
extract = _NS["extract_user_instruction_from_skill_message"]


def single(name, body, words="", skill_dir="/skills/demo", extra_files=(), runtime_note=""):
    """宿主 _build_skill_message 造的单个技能整段文字（/技能名 [字]）：开头是 build_skill_invocation_message 写的那句启用说明。"""
    fake = types.ModuleType("tools.skills_tool")
    fake._skills_dir = lambda: Path("/skills")
    saved = {k: sys.modules.get(k) for k in ("tools", "tools.skills_tool")}
    sys.modules["tools"], sys.modules["tools.skills_tool"] = types.ModuleType("tools"), fake
    try:
        note = '[IMPORTANT: The user has invoked the "%s" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]' % name
        loaded = {"content": body, "name": name, "linked_files": {"references": list(extra_files)} if extra_files else {}}
        return _NS["_build_skill_message"](loaded, Path(skill_dir) if skill_dir else None, note, user_instruction=words, runtime_note=runtime_note)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def bundle(names, blocks, words="", instruction="", missing=()):
    """宿主的技能包整段文字：头（_scaffold_header，字在头里、在所有技能之前）加每个技能一块。"""
    head = _NS["_scaffold_header"]('"%s" skill bundle' % "pack", list(names), lead_lines=["Bundle: pack"], missing=list(missing), disabled=[], extra_instruction=instruction, user_instruction=words)
    return "\n\n".join([head] + ['[Loaded as part of the "pack" skill bundle.]\n' + b for b in blocks])

