"""只读发现聊天平台，供安装向导与诊断提示使用；不导入或启动 Hermes。

只返回平台名、启用依据和固定提示，不返回凭据或允许聊天的账号。配置的 enabled
是显式设置；环境变量只证明有配置候选，不能证明平台在线。只看指定家目录，不找父档案。
YAML 仅解析常见的块映射；复杂写法明确报检测受限，不伪装成没有聊天平台。
"""
import json
import os
import re
from pathlib import Path

NAMES = {
    "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
    "weixin": "微信", "whatsapp": "WhatsApp", "whatsapp_cloud": "WhatsApp Cloud",
    "signal": "Signal", "matrix": "Matrix", "mattermost": "Mattermost",
    "feishu": "飞书", "dingtalk": "钉钉", "wecom": "企业微信",
    "wecom_callback": "企业微信回调", "qqbot": "QQ", "bluebubbles": "iMessage",
    "email": "电子邮件", "sms": "短信", "yuanbao": "元宝", "relay": "Relay",
}
EXCLUDED = frozenset({"cli", "local", "tui", "desktop", "bot_room", "api_server", "webhook", "msgraph_webhook"})
# 只查非空设置，不保留值。2026-11-17 只读核对 Hermes 源码：
# plugins/platforms/<平台>/gateway_env.py 的 ENV_OVERRIDES；不是把 required_env 当启用规则。
# Telegram:15 / Discord:9 / Slack:23 / Signal:10 / Feishu:9 / DingTalk:9 /
# WeCom:9,19 / QQ:33 / BlueBubbles:11 / WhatsApp Cloud:10 / Email:9 / SMS:13。
# 微信另按 adapter.py:1262,1271 的账号+token完整配置；Mattermost 按 adapter.py:732。
# Matrix adapter.py:3254-3263 是 homeserver + (access_token 或 password)，
# Yuanbao gateway_env.py:10 支持旧 APP_KEY；Relay gateway/config_env.py:312-328。
ENV_GROUPS = {
    "telegram": (("TELEGRAM_BOT_TOKEN",),), "discord": (("DISCORD_BOT_TOKEN",),),
    "slack": (("SLACK_BOT_TOKEN",),), "weixin": (("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"),),
    "whatsapp": (("WHATSAPP_ENABLED",),),
    "whatsapp_cloud": (("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "WHATSAPP_CLOUD_ACCESS_TOKEN"),),
    "signal": (("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"),),
    "yuanbao": (("YUANBAO_APP_ID", "YUANBAO_APP_SECRET"), ("YUANBAO_APP_KEY", "YUANBAO_APP_SECRET")),
    "qqbot": (("QQ_APP_ID", "QQ_CLIENT_SECRET"),),
    "relay": (("GATEWAY_RELAY_URL",),),
    "mattermost": (("MATTERMOST_URL", "MATTERMOST_TOKEN"),),
    "matrix": (("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"), ("MATRIX_HOMESERVER", "MATRIX_PASSWORD")),
    "feishu": (("FEISHU_APP_ID", "FEISHU_APP_SECRET"),),
    "dingtalk": (("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),),
    "wecom": (("WECOM_BOT_ID", "WECOM_SECRET"),),
    "wecom_callback": (("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"),),
    "bluebubbles": (("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),),
    "email": (("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"),),
    "sms": (("TWILIO_ACCOUNT_SID",),),
}
_ENV_NAMES = frozenset(key for groups in ENV_GROUPS.values() for group in groups for key in group)
_PLATFORM = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_KEY = re.compile(r"(?P<indent> *)(?P<key>[A-Za-z_][\w-]*|'[^']+'|\"[^\"]+\")\s*:(?P<value>.*)\Z")
_ASSIGN = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)\Z")
_LIMIT = 2 * 1024 * 1024


def label(platform):
    return NAMES.get(platform, platform)


def hint(platform):
    text = "在这个平台私聊 Hermes，发送 /whoami，填写它显示的用户编号；不要填显示名或机器人的编号。"
    if platform == "weixin":
        text += "微信安装结束时的 User ID 也可核对；WEIXIN_ACCOUNT_ID 是机器人编号，不能用作本人账号。"
    return text + " Hermes 的允许用户名单／配对只表示能聊天，不自动算本人；请自行核对。"


def _without_comment(text):
    quote = None
    escaped = False
    for i, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].strip()
    return text.strip()


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = _without_comment(value)
        if word.startswith(("'", '"')):
            if len(word) < 2 or word[-1] != word[0] or word[0] in word[1:-1]:
                return None
            word = word[1:-1]
        word = word.lower()
        if word in ("true", "yes", "on", "1"):
            return True
        if word in ("false", "no", "off", "0"):
            return False
    return None


class _ObjectPairs(list):
    """JSON 对象保留重复键，待判断是不是平台相关再检查。"""


def _platform_branch(path):
    if not path:
        return True
    if path[0] == "platforms" or path[0] in NAMES:
        return True
    return path[0] == "gateway" and (len(path) == 1 or path[1] == "platforms" or path[1] in NAMES)


def _paths(path, notes):
    """只产出 mapping path → enabled；None 表示本文件不可可信地判启用。"""
    try:
        if not path.exists():
            return {}
        if path.stat().st_size > _LIMIT:
            raise ValueError("large")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError):
        notes.append("%s 读不了或过大，聊天平台检测不完整。" % path.name)
        return None
    if text.lstrip().startswith("{") or path.suffix == ".json":
        try:
            data = json.loads(text, object_pairs_hook=_ObjectPairs)
            if not isinstance(data, _ObjectPairs):
                raise ValueError("mapping required")
            out = {}
            def walk(obj, prefix=()):
                seen = set()
                for key, value in obj:
                    if not isinstance(key, str):
                        continue
                    here = prefix + (key,)
                    if not _platform_branch(here):
                        continue
                    if key in seen:
                        raise ValueError("duplicate platform key")
                    seen.add(key)
                    if key == "enabled":
                        out[here] = value
                    elif isinstance(value, _ObjectPairs):
                        walk(value, here)
            walk(data)
            return out
        except (ValueError, RecursionError):
            notes.append("%s 格式读不懂或有重复键，聊天平台检测不完整。" % path.name)
            return None
    stack, out, seen = [], {}, set()
    opaque_indent = None
    previous = None
    started = False
    limited = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if opaque_indent is not None:
            if indent > opaque_indent:
                continue                 # block scalar / list 的正文绝不是配置键
            opaque_indent = None
        if raw.strip() == "---" and not started:
            started = True
            continue
        started = True
        if raw.lstrip().startswith("- ") and stack and stack[-1][1] not in ("platforms", "gateway"):
            opaque_indent = stack[-1][0]
            continue                     # 普通字段列表（如 allow_from）不参与 enabled 判定
        match = _KEY.fullmatch(raw)
        if not match:
            limited = True
            break
        key = match["key"].strip("\"'")
        if previous is not None and indent > previous[0] and not previous[1]:
            limited = True               # scalar 后不能突然长出 mapping 子键
            break
        while stack and stack[-1][0] >= indent:
            stack.pop()
        here = tuple(k for _, k in stack) + (key,)
        if not _platform_branch(here):
            # 模型、工具等配置不决定聊天平台。整棵跳过，包括普通 flow list/dict，
            # 其正文不能伪装成平台键；相关区的别名/合并仍会被拒绝并提示。
            opaque_indent = indent
            previous = (indent, False)
            continue
        if here in seen:
            limited = True               # 包含父 mapping 重复，不能静默合并成新配置
            break
        seen.add(here)
        value = _without_comment(match["value"])
        previous = (indent, not value)
        if not value:
            stack.append((indent, key))
        elif re.fullmatch(r"[|>](?:[1-9][+-]?|[+-][1-9]?|)", value):
            opaque_indent = indent
        elif key == "enabled":
            if _bool(value) is None:
                limited = True
                break
            out[here] = value
        elif value.startswith(("{", "[", "&", "*", "!")) or key == "<<":
            limited = True
            break
    if limited:
        notes.append("config.yaml 含复杂、重复或无法确认的 YAML 写法，不据此判定平台已开启；可用 --owner 平台:编号 明确登记。")
        return None
    return out


def _platform_path(path):
    """返回 (平台, 优先级)，对应 Hermes 的平台配置合并顺序。"""
    if len(path) == 4 and path[:2] == ("gateway", "platforms"):
        return path[2], 1
    if len(path) == 3 and path[0] == "platforms":
        return path[1], 2
    if len(path) == 3 and path[0] == "gateway" and path[1] in NAMES:
        return path[1], 3
    if len(path) == 2 and path[0] in NAMES:
        return path[0], 4
    return None, 0


def _env_present(key, value):
    if key == "WHATSAPP_ENABLED":
        # gateway_env.py:8-18：false/0/no 还会关闭 YAML 开着的平台；空值不等于禁用。
        word = _without_comment(value).strip("\"'").lower() if isinstance(value, str) else ""
        return False if word in ("false", "0", "no") else (True if _bool(value) is True else None)
    if not isinstance(value, str):
        return False
    return bool(_without_comment(value).strip("\"'").strip())


def _dotenv_presence(path, notes):
    found = {}
    try:
        if not path.exists():
            return found
        if path.stat().st_size > _LIMIT:
            raise ValueError("large")
        with path.open(encoding="utf-8-sig") as stream:
            multiline = None
            for raw in stream:
                # 忽略多行引号正文里的伪赋值，不解析或保留凭据。
                if multiline:
                    if raw.rstrip().endswith(multiline):
                        multiline = None
                    continue
                line = raw.strip()
                match = _ASSIGN.fullmatch(line)
                if not match:
                    continue
                key, raw_value = match.groups()
                value = _without_comment(raw_value)
                if value.startswith(("'", '"')) and not (len(value) > 1 and value.endswith(value[0])):
                    multiline = value[0]
                if key in _ENV_NAMES:
                    found[key] = _env_present(key, value)
    except (OSError, UnicodeError, ValueError):
        notes.append("Hermes 环境变量文件读不了或过大，聊天平台检测不完整。")
    return found


def _env_for_home(home, env):
    selected = env.get("HERMES_HOME")
    if selected:
        return Path(os.path.expanduser(str(selected))).resolve() == home.resolve()
    # 主档案终端的变量不能借给另一个机器人。显式 HERMES_HOME 匹配才用。
    return home.parent.name != "profiles"


def discover(home, env=None):
    """只看指定家目录的启用设置与变量存在性；不查网络、不检查登录有效性。"""
    home = Path(home)
    env = os.environ if env is None else env
    notes, settings = [], {}
    for filename in ("gateway.json", "config.yaml"):
        entries = _paths(home / filename, notes)
        if entries is None:
            settings.clear()                 # 当前配置不可读时不拿旧 gateway.json 宣称已启用
            continue
        ranked = []
        for path, raw in entries.items():
            platform, priority = _platform_path(path)
            if platform is None or platform in EXCLUDED or not _PLATFORM.fullmatch(platform):
                continue
            enabled = _bool(raw)
            if enabled is None:
                notes.append("%s 的 %s 启用设置读不懂，平台检测不完整。" % (filename, platform))
                continue
            ranked.append((priority, platform, enabled))
        for _, platform, enabled in sorted(ranked):
            settings[platform] = (enabled, filename)
    present = _dotenv_presence(home / ".env", notes)
    process_keys = set()
    if _env_for_home(home, env):
        for key in _ENV_NAMES:
            if key in env:
                present[key] = _env_present(key, env[key])
                process_keys.add(key)
    if present.get("WHATSAPP_ENABLED") is False:
        settings["whatsapp"] = (False, "environment")
    platforms = []
    for platform in sorted(set(settings) | set(ENV_GROUPS)):
        explicit, source = settings.get(platform, (None, None))
        if explicit is False:
            continue
        groups = [group for group in ENV_GROUPS.get(platform, ()) if all(present.get(key, False) for key in group)]
        if explicit is not True and not groups:
            continue
        state = "enabled" if explicit is True else "configured"
        if source is None:
            source = "environment" if any(key in process_keys for key in groups[0]) else "hermes_dotenv"
        platforms.append({"platform": platform, "name": label(platform), "source": source, "state": state, "hint": hint(platform)})
    if any(p["state"] == "configured" for p in platforms):
        notes.append("检测到聊天平台的配置候选；没有启动 Hermes 或联网，不能据此确认已连接。")
    return {"platforms": platforms, "notes": list(dict.fromkeys(notes))}
