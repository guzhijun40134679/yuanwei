"""固定口令 `#yuanwei-nomem`（关）和 `#yuanwei-mem`（开）。`#yuanwei-nomem` 管「别读长期记忆」（核心里按窗口持续，见 window.py；
没有窗口层的宿主里只管这一轮）；`#yuanwei-mem` 是重新允许用记忆的口令，同样只认第一物理行，还要过「用户本人、新输入」两项核对（window.py）。

规则（方案第 6 节、施工单第 3.1 节）：
* 只认**当前可信用户提交**的**第一物理行**：零缩进，整行就是口令；行尾允许空格或 Tab；换行允许 LF 或 CRLF；只折叠 ASCII 字母大小写；`#` 必须是 ASCII。
* 不做全角折叠、不模糊匹配、不在全消息里搜。句中、后面的行、引号、反引号、代码块、附件、工具输出、恢复回放、检索材料里的同样文字都不算——
  这些来源的文字根本不该交给这个函数；回放历史时调用方不执行旧口令（见 access.Access.complete_turn 的 replay 参数）。
* 以保留前缀 `#yuanwei-`（ASCII，零缩进，大小写不拘）开头、但整行不是口令（比如后面多了字、有孤立的 CR、行尾是 NBSP）：算「格式不对」，
  给出标准写法，暂停本轮新增读取，不悄悄当普通查询。
* 给任务用的正文可以去掉控制行（task_view），但那只是派生视图，不冒充原始输入。
"""
from typing import NamedTuple, Optional, Tuple

COMMAND = "#yuanwei-nomem"
COMMAND_ON = "#yuanwei-mem"
RESERVED_PREFIX = "#yuanwei-"
_ASCII_UP = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_TRAIL = " \t"


class Control(NamedTuple):
    kind: str                           # "none"｜"nomem"｜"mem"｜"malformed"
    span: Optional[Tuple[int, int]]     # 控制行在原文里的码点范围（不含换行）；none 时是 None
    hint: Optional[str]                 # 格式不对时给人看的提示；其余是 None


def ascii_lower(s):
    """只把 A–Z 换成 a–z；其余字符原样（不用 str.lower，免得开尔文符号之类变成 ASCII）。"""
    return s.translate(_ASCII_UP)


def standard_line():
    return COMMAND


def with_command(body=""):
    """生成标准写法：口令单独占第一行，正文另起一行；正文为空就只有口令。"""
    return COMMAND if not body else COMMAND + "\n" + body


MALFORMED_HINT = ("没认出禁读口令：格式不对。要停止本轮新增读取，请把口令单独写在第一行，整行只有 %s（后面不要有别的字），正文另起一行；"
                  "这一轮已先暂停新增读取。" % COMMAND)


def parse(text):
    if not isinstance(text, str):
        raise ValueError("口令解析的输入必须是文字")
    first, sep, _rest = text.partition("\n")
    raw_line = first[:-1] if (sep and first.endswith("\r")) else first          # 只有 CRLF 的 CR 算行尾；孤立的 CR 留在行里
    line = raw_line.rstrip(_TRAIL)
    low = ascii_lower(line)
    if low == COMMAND:
        return Control("nomem", (0, len(raw_line)), None)
    if low == COMMAND_ON:
        return Control("mem", (0, len(raw_line)), None)
    if ascii_lower(raw_line).startswith(RESERVED_PREFIX):
        return Control("malformed", (0, len(raw_line)), MALFORMED_HINT)
    return Control("none", None, None)


def task_view(text, control):
    """给任务用的正文：口令成立时去掉第一行（含换行）。派生视图，不改原文；格式不对、没有口令时原样返回。"""
    if control.kind not in ("nomem", "mem"):
        return text
    _first, sep, rest = text.partition("\n")
    return rest if sep else ""
