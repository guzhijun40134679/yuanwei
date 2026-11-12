"""正文密钥过滤的规则表（删除与密钥过滤·交回点甲）。**唯一来源**：规则的类型、匹配边界、替换范围、正例、反例、理由都在这里；
设计文档第一部分的表是从这里抄的，不是另一份。生产代码（yuanwei/redact.py，乙来写）不能 import 这个文件（安装包里不带 tests/），
但必须在这张表上全过：每条正例过滤以后正好等于 expected，每条反例过滤以后一字不变。

**正例全部是离线自造的**：格式对、值是用确定的种子现算出来的（F 函数），没有向任何厂商申请过、没有调用过任何鉴权、从来没被授予过权限。
源码里没有一段像密钥的整串字面量（现算，免得静态扫描器把测试数据当真密钥）。GitHub 令牌的末 6 位校验和故意算错（tests/test_secret_rules_table.py 里核）；
这不等于「把正例造到规则覆盖范围外面」：规则只看前缀、字符集和长度，不核校验和，所以这些正例仍然在规则的覆盖范围里（同一个测试文件里核了）。
别家的校验和（AWS、Slack、Stripe 等）没有公开的校验位，靠随机体现算、不是任何一个真值。

每条规则：
* id、kind（占位符里的类型名）、priority（数小的优先；多条规则命中重叠区间时，合并成并集，类型取优先级最高的那条）；
* res：参考正则（规格，给乙看；乙可以写成别的形式，但必须过这张表）。命名组 s 是**被替换的范围**，没有命名组就整段匹配都换掉；
* boundary、replace：边界和替换范围的文字说明；reason：为什么这么挡。

占位符：`〔密钥已挡掉：{kind}〕`。只留类型，不留任何一截原文（前几位、后四位都不留）。
正例的 expected 由模板和槽位现算（把槽位里的值换成占位符），不是手写的；count 是这条消息实际遮掉的区间数（重叠命中合并成一个区间，只算一次）。
"""
import hashlib
import string

RULES_VERSION = 1
PLACEHOLDER = "〔密钥已挡掉：%s〕"

ALNUM = string.ascii_letters + string.digits
B64URL = ALNUM + "_-"
B64 = ALNUM + "+/"
HEX = "0123456789abcdef"
UPNUM = string.ascii_uppercase + string.digits
DIGITS = string.digits


def F(seed, n, alphabet=ALNUM):
    """确定的伪随机体：同一个 seed 永远得到同一串。只用来现算正例，不是任何真值。"""
    out, i = [], 0
    while len(out) < n:
        for b in hashlib.sha256(("yw-fake:%s:%d" % (seed, i)).encode()).digest():
            out.append(alphabet[b % len(alphabet)])
        i += 1
    return "".join(out[:n])


def _b62(n):
    s = ""
    while n:
        n, r = divmod(n, 62)
        s = ALNUM[r] + s
    return s


def gh_checksum_ok(token):
    """GitHub 新版令牌：前缀 + 30 位随机 + 6 位 base62(crc32(随机部分))。给测试核「正例的校验和是错的」用。"""
    import zlib
    body, tail = token[4:-6], token[-6:]
    return _b62(zlib.crc32(body.encode())).rjust(6, "0") == tail


def GH(prefix, seed):
    """GitHub 经典令牌的形状，**校验和故意算错**（把末位换成另一个字符）。"""
    import zlib
    body = F(seed, 30)
    good = _b62(zlib.crc32(body.encode())).rjust(6, "0")
    bad = good[:-1] + ("A" if good[-1] != "A" else "B")
    return prefix + body + bad


def P(pid, template, *slots, note=""):
    """正例。template 里的 <<1>>、<<2>>… 是槽位；slots 是 (值, 类型) 的列表。"""
    text, expected = template, template
    for i, (value, kind) in enumerate(slots, 1):
        text = text.replace("<<%d>>" % i, value)
        expected = expected.replace("<<%d>>" % i, PLACEHOLDER % kind)
    return {"id": pid, "text": text, "expected": expected, "count": len(slots), "slots": list(slots), "template": template, "note": note}


def N(nid, text, why):
    """反例：过滤以后一字不变，count 为 0。"""
    return {"id": nid, "text": text, "why": why}


L = r"(?<![A-Za-z0-9_-])"                      # 令牌的左边界：前面不是字母、数字、下划线、连字符
NOPH = r"(?!〔密钥已挡掉：)"                    # 值不能是已经换好的占位符（幂等）
PLACEHOLDER_VALUES = r"(?!(?:none|null|nil|undefined|true|false|\*+|x{3,}|-+|\?+)(?:[\s,;\"'`)\]}]|$))(?!<[^>\n]*>|\$|\{\{|%[sd(])"   # 明显不是真值的写法：None、null、true、false、****、<占位>、${变量}、$VAR、{{模板}}、%s
SEP = r"[\"']?\]?[ \t]*(?::=|=>|=|:|：)[ \t]*"

# 键名：英文。名字后面不能紧跟字母、数字、下划线（api_key_env、password_hash、tokens、token_count 都不算）；前面可以有最多 4 段「词＋_ . -」的前缀（OPENAI_API_KEY、db.password）
NAME_EN = (r"(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key|secret|client[_-]?secret|access[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
           r"private[_-]?key|token|passwd|password|pwd|passphrase)")
NAME_ZH = r"(?:API\s?密钥|API\s?Key|访问令牌|密码|口令|密钥|秘钥|令牌)"
PREFIX = r"(?:[A-Za-z0-9]{1,20}[_.-]){0,4}"

KV_EN_LEFT = r"(?i)(?<![A-Za-z0-9])" + PREFIX + NAME_EN + r"(?![A-Za-z0-9_])" + SEP
ASCII_VALUE = r"[\x21\x23-\x25\x28-\x2b\x2d-\x3a\x3c-\x5f\x61-\x7e]+"      # ASCII 可见字符，去掉 \" \' & , ; 反引号（不用「[^…\u0080-\U0010FFFF]」：忽略大小写时 s、k 会被连带排除）
ZH_LEFT = r"(?i)" + NAME_ZH + r"[ \t]*(?:(?:是|为)[ \t]*[:：]?|[:：=])[ \t]*"

RULES = []


def R(**kw):
    RULES.append(kw)
    return kw


# ================================================================ 一、各家固定前缀（优先级 10 上下）
R(id="T01", kind="Anthropic", priority=10, res=[L + r"(?P<s>sk-ant-[A-Za-z0-9_-]{20,})"],
  boundary="前面不能是字母、数字、下划线、连字符；sk-ant- 后面是 20 位以上的字母、数字、下划线、连字符", replace="整个令牌",
  reason="Anthropic 密钥的固定前缀。",
  positives=[
      P("T01-p1", "我把 key 贴给你：<<1>>，先别记下来。", ("sk-ant-api03-" + F("t01a", 40, B64URL), "Anthropic")),
      P("T01-p2", "export ANTHROPIC_API_KEY=<<1>>\nclaude --version", ("sk-ant-" + F("t01b", 32, B64URL), "Anthropic"), note="键名规则也会命中同一个值：合并成一个区间，类型取优先级高的 Anthropic"),
      P("T01-p3", "助手：你的密钥是 <<1>> 吗？", ("sk-ant-oat01-" + F("t01c", 36, B64URL), "Anthropic"), note="助手复述也要挡"),
  ],
  negatives=[
      N("T01-n1", "sk-ant-short 这个写法太短，不是密钥", "后面不足 20 位"),
      N("T01-n2", "mask-ant-" + F("t01n", 30, B64URL) + " 是个商品编号", "前面紧挨字母（mask-ant-…），左边界不过"),
  ])

R(id="T02", kind="OpenAI", priority=11, res=[L + r"(?P<s>sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,})"],
  boundary="sk-proj-、sk-svcacct-、sk-admin- 后面 20 位以上的字母、数字、下划线、连字符", replace="整个令牌", reason="OpenAI 新式密钥的固定前缀（含 sk-proj-）。",
  positives=[
      P("T02-p1", "用这个：<<1>>。", ("sk-proj-" + F("t02a", 56, B64URL), "OpenAI")),
      P("T02-p2", "OPENAI_API_KEY=<<1>>", ("sk-proj-" + F("t02b", 40, B64URL), "OpenAI"), note="键名规则也命中同一个值，合并成一个区间"),
      P("T02-p3", "{\"key\": \"<<1>>\"}", ("sk-svcacct-" + F("t02c", 44, B64URL), "OpenAI")),
      P("T02-p4", "key：<<1>>\n（上面是服务账号的）", ("sk-admin-" + F("t02d", 36, B64URL), "OpenAI")),
  ],
  negatives=[
      N("T02-n1", "sk-proj-demo 是我们项目的代号", "后面不足 20 位"),
      N("T02-n2", "desk-proj-" + F("t02n", 30, B64URL), "前面紧挨字母"),
  ])

R(id="T03", kind="DeepSeek", priority=12, res=[L + r"(?P<s>sk-[0-9a-f]{32})(?![A-Za-z0-9_-])"],
  boundary="sk- 后面正好 32 位小写十六进制，再往后不能接字母、数字、下划线、连字符", replace="整个令牌", reason="DeepSeek 密钥的形状（sk- 加 32 位十六进制）。",
  positives=[
      P("T03-p1", "deepseek 的 key 是 <<1>>", ("sk-" + F("t03a", 32, HEX), "DeepSeek")),
      P("T03-p2", "api_key = \"<<1>>\"\n", ("sk-" + F("t03b", 32, HEX), "DeepSeek"), note="键名规则也命中同一个值，合并成一个区间"),
      P("T03-p3", "（<<1>>）", ("sk-" + F("t03c", 32, HEX), "DeepSeek"), note="括号紧贴也算边界"),
  ],
  negatives=[
      N("T03-n1", "sk-" + F("t03n", 31, HEX) + " 少了一位", "31 位：不是 32 位十六进制，也不到通用 sk- 规则的 32 位字母数字"),
      N("T03-n2", "sk-learn 和 sk-image 都是包的名字", "后面太短"),
  ])

R(id="T04", kind="OpenAI", priority=13, res=[L + r"(?P<s>sk-[A-Za-z0-9]{40,})(?![A-Za-z0-9_])"],
  boundary="sk- 后面 40 位以上的字母数字（旧式 OpenAI 密钥 48 位）", replace="整个令牌", reason="OpenAI 旧式密钥。",
  positives=[
      P("T04-p1", "旧的那把：<<1>>", ("sk-" + F("t04a", 48), "OpenAI")),
      P("T04-p2", "OPENAI_KEY: <<1>> (已作废)", ("sk-" + F("t04b", 44), "OpenAI")),
  ],
  negatives=[
      N("T04-n1", "task-" + F("t04n", 44), "前面是 ta，sk- 前面紧挨字母，左边界不过"),
  ])

R(id="T05", kind="API密钥", priority=14, res=[L + r"(?P<s>sk-[A-Za-z0-9_-]{32,})"],
  boundary="sk- 后面 32 位以上的字母、数字、下划线、连字符（认不出是哪家的，统称 API密钥）", replace="整个令牌", reason="别家（OpenRouter 等）同样 sk- 开头的密钥，宁可多挡。",
  positives=[
      P("T05-p1", "openrouter 的：<<1>>", ("sk-or-v1-" + F("t05a", 56, HEX), "API密钥")),
      P("T05-p2", "token 在这 <<1>> 用完就删", ("sk-" + F("t05b", 34, B64URL), "API密钥")),
  ],
  negatives=[
      N("T05-n1", "ask-me-anything-about-everything-today-and-tomorrow 是个很长的网址片段", "sk-me-… 前面紧挨字母 a"),
      N("T05-n2", "sk-learn-pipeline 是个常见的包", "只有 sk-learn-pipeline 短词"),
  ])

R(id="T06", kind="Google", priority=15, res=[L + r"(?P<s>AIza[0-9A-Za-z_-]{35})(?![A-Za-z0-9_-])"],
  boundary="AIza 加 35 位（字母、数字、下划线、连字符），共 39 位；前后都不能再接这些字符", replace="整个令牌", reason="Google API 密钥的固定形状。",
  positives=[
      P("T06-p1", "地图的 key：<<1>>", ("AIza" + F("t06a", 35, B64URL), "Google")),
      P("T06-p2", "https://maps.example.com/api?key=<<1>>&zoom=3", ("AIza" + F("t06b", 35, B64URL), "Google"), note="网址参数规则也命中同一个值，合并成一个区间"),
  ],
  negatives=[
      N("T06-n1", "AIza" + F("t06n", 20, B64URL) + " 太短", "不是 39 位"),
      N("T06-n2", "MAIza" + F("t06m", 35, B64URL), "前面紧挨字母"),
  ])

R(id="T07", kind="GitHub", priority=16, res=[L + r"(?P<s>(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}))"],
  boundary="ghp_、gho_、ghu_、ghs_、ghr_ 加 36 位以上字母数字；或 github_pat_ 加 40 位以上字母数字下划线", replace="整个令牌",
  reason="GitHub 令牌的固定前缀。不核校验和（规则只看前缀、字符集、长度）。",
  positives=[
      P("T07-p1", "推送用 <<1>> 就行", (GH("ghp_", "t07a"), "GitHub")),
      P("T07-p2", "oauth：<<1>>", (GH("gho_", "t07b"), "GitHub")),
      P("T07-p3", "user token <<1>>。", (GH("ghu_", "t07c"), "GitHub")),
      P("T07-p4", "Actions 给的 <<1>>", (GH("ghs_", "t07d"), "GitHub")),
      P("T07-p5", "刷新用的 <<1>>", (GH("ghr_", "t07e"), "GitHub")),
      P("T07-p6", "细粒度的：<<1>>", ("github_pat_" + F("t07f", 22) + "_" + F("t07g", 59), "GitHub")),
  ],
  negatives=[
      N("T07-n1", "ghp_ 开头的是个人令牌，gho_ 是 OAuth 的", "只是在讲前缀，后面没有 36 位"),
      N("T07-n2", "github_pat_ 这个前缀文档里有写", "没有 40 位以上的体"),
  ])

R(id="T08", kind="GitLab", priority=17, res=[L + r"(?P<s>glpat-[A-Za-z0-9_-]{20,})"],
  boundary="glpat- 加 20 位以上字母数字下划线连字符", replace="整个令牌", reason="GitLab 个人令牌的固定前缀。",
  positives=[
      P("T08-p1", "gitlab：<<1>>", ("glpat-" + F("t08a", 20, B64URL), "GitLab")),
      P("T08-p2", "git clone https://oauth2:<<1>>@gitlab.example.com/x/y.git", ("glpat-" + F("t08b", 24, B64URL), "GitLab"), note="连接串规则也命中同一个值（密码位置），合并成一个区间；类型取 GitLab"),
  ],
  negatives=[N("T08-n1", "glpat-xxxx 是占位", "后面不足 20 位")])

R(id="T09", kind="Slack", priority=18, res=[L + r"(?P<s>(?:xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,}))"],
  boundary="xoxa-、xoxb-、xoxp-、xoxo-、xoxs-、xoxr- 加 10 位以上字母数字连字符；或 xapp-数字- 加 10 位以上", replace="整个令牌", reason="Slack 令牌的固定前缀。",
  positives=[
      P("T09-p1", "bot：<<1>>", ("xoxb-" + F("t09a", 12, DIGITS) + "-" + F("t09b", 12, DIGITS) + "-" + F("t09c", 24), "Slack")),
      P("T09-p2", "用户令牌 <<1>> 不要外传", ("xoxp-" + F("t09d", 12, DIGITS) + "-" + F("t09e", 12, DIGITS) + "-" + F("t09f", 12, DIGITS) + "-" + F("t09g", 32, HEX), "Slack")),
      P("T09-p3", "app：<<1>>", ("xapp-1-" + F("t09h", 11, UPNUM) + "-" + F("t09i", 13, DIGITS) + "-" + F("t09j", 40, HEX), "Slack")),
  ],
  negatives=[N("T09-n1", "xoxo 是亲亲抱抱的意思，xoxb- 是 bot 令牌前缀", "后面没有 10 位的体")])

R(id="T10", kind="AWS", priority=19, res=[L + r"(?P<s>(?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Za-z0-9])"],
  boundary="AKIA 或 ASIA 加 16 位大写字母数字，共 20 位；前后不能再接字母数字", replace="整个访问密钥编号", reason="AWS 访问密钥编号的固定形状。",
  positives=[
      P("T10-p1", "access key id：<<1>>", ("AKIA" + F("t10a", 16, UPNUM), "AWS")),
      P("T10-p2", "临时的：<<1>>，一小时有效", ("ASIA" + F("t10b", 16, UPNUM), "AWS")),
  ],
  negatives=[
      N("T10-n1", "AKIA" + F("t10n", 17, UPNUM) + " 多了一位", "21 位：右边界不过"),
      N("T10-n2", "ASIAN 国家的首字母缩写 ASIA 不是密钥", "后面没有 16 位"),
  ])

R(id="T11", kind="AWS", priority=55,
  res=[r"(?i)(?<![A-Za-z0-9])(?:aws[_-]?)?(?:secret[_-]?access[_-]?key|session[_-]?token)(?![A-Za-z0-9_])[\"']?[ \t]*(?::=|=>|=|:|：)[ \t]*[\"']?" + NOPH + r"(?P<s>[A-Za-z0-9/+=]{16,})"],
  boundary="键名是 aws_secret_access_key、secret_access_key、aws_session_token、session_token（大小写不论，CamelCase 的 SecretAccessKey、SessionToken 也算，下划线可有可无），"
           "后面接 =、:、：，值是 16 位以上的字母数字加 /+=", replace="只换值，键名留着",
  reason="AWS 的 secret 和 session 值没有固定前缀，只能靠键名上下文认。",
  positives=[
      P("T11-p1", "aws_secret_access_key = <<1>>", (F("t11a", 40, B64), "AWS"), note="通用键名规则也命中同一个值；合并后类型取 AWS（优先级 55 高于 60）"),
      P("T11-p2", "[default]\naws_access_key_id = <<1>>\naws_secret_access_key=<<2>>", ("AKIA" + F("t11b", 16, UPNUM), "AWS"), (F("t11c", 40, B64), "AWS")),
      P("T11-p3", "AWS_SESSION_TOKEN=<<1>>", (F("t11d", 120, B64), "AWS")),
      P("T11-p4", "{\"AccessKeyId\": \"<<1>>\", \"SecretAccessKey\": \"<<2>>\", \"SessionToken\": \"<<3>>\"}", ("ASIA" + F("t11e", 16, UPNUM), "AWS"), (F("t11f", 40, B64), "AWS"), (F("t11g", 96, B64), "AWS")),
  ],
  negatives=[
      N("T11-n1", "把 aws_secret_access_key 配到环境变量里就行", "只有键名，后面没有值"),
      N("T11-n2", "export AWS_SECRET_ACCESS_KEY=$SECRET", "值是变量引用 $…，不是真值"),
      N("T11-n3", "aws_secret_access_key = <your secret here>", "值是尖括号占位"),
  ])

R(id="T12", kind="Stripe", priority=20, res=[L + r"(?P<s>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})"],
  boundary="sk_live_、rk_live_（以及 sk_test_、rk_test_）加 16 位以上字母数字；pk_（公开密钥）不挡", replace="整个令牌", reason="Stripe 密钥的固定前缀。测试密钥也挡（宁可多挡）。",
  positives=[
      P("T12-p1", "stripe live：<<1>>", ("sk_live_" + F("t12a", 24), "Stripe")),
      P("T12-p2", "受限的：<<1>>", ("rk_live_" + F("t12b", 24), "Stripe")),
      P("T12-p3", "测试环境 <<1>>", ("sk_test_" + F("t12c", 24), "Stripe")),
  ],
  negatives=[
      N("T12-n1", "前端用的公开 key：pk_live_" + F("t12n", 24), "pk_ 是设计上就公开的可发布密钥，不挡（要不要改成也挡，交回点甲问题）"),
      N("T12-n2", "sk_live_ 开头的是线上密钥", "后面没有 16 位的体"),
  ])

R(id="T13", kind="HuggingFace", priority=21, res=[L + r"(?P<s>hf_[A-Za-z0-9]{30,})"],
  boundary="hf_ 加 30 位以上字母数字（不含下划线）", replace="整个令牌", reason="Hugging Face 令牌的固定前缀。",
  positives=[
      P("T13-p1", "HF 令牌 <<1>>", ("hf_" + F("t13a", 34), "HuggingFace")),
      P("T13-p2", "HUGGINGFACE_TOKEN=<<1>>\n", ("hf_" + F("t13b", 37), "HuggingFace")),
  ],
  negatives=[
      N("T13-n1", "hf_dataset_name 是个变量名", "下划线打断，体不到 30 位"),
      N("T13-n2", "chf_" + F("t13n", 34), "前面紧挨字母"),
  ])

R(id="T14", kind="Telegram", priority=22, res=[r"(?<![0-9A-Za-z])(?P<s>[0-9]{8,10}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])"],
  boundary="8 到 10 位数字、冒号、正好 35 位字母数字下划线连字符；前后不能紧挨字母数字", replace="整个令牌", reason="Telegram 机器人令牌的固定形状。",
  positives=[
      P("T14-p1", "bot：<<1>>", (F("t14a", 9, DIGITS) + ":AA" + F("t14b", 33, B64URL), "Telegram")),
      P("T14-p2", "TELEGRAM_BOT_TOKEN=<<1>>", (F("t14c", 10, DIGITS) + ":AA" + F("t14d", 33, B64URL), "Telegram")),
  ],
  negatives=[
      N("T14-n1", "会议 12:30 开始，比分 10:35", "数字太短、冒号后不是 35 位的体"),
      N("T14-n2", F("t14n", 9, DIGITS) + ":short", "冒号后面太短"),
  ])

R(id="T15", kind="PyPI", priority=23, res=[L + r"(?P<s>pypi-[A-Za-z0-9_-]{50,})"],
  boundary="pypi- 加 50 位以上字母数字下划线连字符", replace="整个令牌", reason="（自补）PyPI 上传令牌。用户要发 PyPI，这类最该挡。",
  positives=[P("T15-p1", "twine 用的 <<1>>", ("pypi-AgEIcHlwaS5vcmc" + F("t15a", 80, B64URL), "PyPI"))],
  negatives=[N("T15-n1", "pypi-mirror 是个镜像名", "后面太短")])

R(id="T16", kind="npm", priority=24, res=[L + r"(?P<s>npm_[A-Za-z0-9]{36})(?![A-Za-z0-9_-])"],
  boundary="npm_ 加正好 36 位字母数字", replace="整个令牌", reason="（自补）npm 令牌。",
  positives=[P("T16-p1", "NPM_TOKEN=<<1>>", ("npm_" + F("t16a", 36), "npm"))],
  negatives=[N("T16-n1", "npm_config_registry 是环境变量名", "体里有下划线、不到 36 位")])

# ================================================================ 二、私钥、JWT、连接串（20–40）
R(id="S01", kind="私钥", priority=30, res=[r"(?s)(?P<s>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----)"],
  boundary="从 BEGIN … PRIVATE KEY 行到对应的 END … PRIVATE KEY 行（含这两行），跨行", replace="整块换成一个占位符（一个区间）",
  reason="PEM 私钥块（RSA、EC、OPENSSH、ENCRYPTED、不带算法名的 PRIVATE KEY 都算）。",
  positives=[
      P("S01-p1", "我的私钥：\n<<1>>\n别外传。", ("-----BEGIN " + "RSA PRIVATE KEY" + "-----\n" + F("s01a", 64, B64) + "\n" + F("s01b", 64, B64) + "\n" + F("s01c", 40, B64) + "=\n-----END " + "RSA PRIVATE KEY" + "-----", "私钥")),
      P("S01-p2", "<<1>>", ("-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----\n" + F("s01d", 70, B64) + "\n-----END " + "OPENSSH PRIVATE KEY" + "-----", "私钥"), note="整条消息就是一块私钥"),
      P("S01-p3", "```\n<<1>>\n```\n这个是 ec 的", ("-----BEGIN " + "EC PRIVATE KEY" + "-----\n" + F("s01e", 64, B64) + "\n-----END " + "EC PRIVATE KEY" + "-----", "私钥")),
      P("S01-p4", "<<1>>", ("-----BEGIN " + "PRIVATE KEY" + "-----\n" + F("s01f", 64, B64) + "\n-----END " + "PRIVATE KEY" + "-----", "私钥"), note="不带算法名"),
      P("S01-p5", "两把：<<1>>\n和\n<<2>>", ("-----BEGIN " + "RSA PRIVATE KEY" + "-----\n" + F("s01g", 64, B64) + "\n-----END " + "RSA PRIVATE KEY" + "-----", "私钥"),
        ("-----BEGIN " + "ENCRYPTED PRIVATE KEY" + "-----\n" + F("s01h", 64, B64) + "\n-----END " + "ENCRYPTED PRIVATE KEY" + "-----", "私钥"), note="两块算两个区间"),
  ],
  negatives=[
      N("S01-n1", "-----BEGIN " + "PUBLIC KEY" + "-----\n" + F("s01n", 64, B64) + "\n-----END " + "PUBLIC KEY" + "-----", "公钥不是秘密"),
      N("S01-n2", "-----BEGIN " + "CERTIFICATE" + "-----\n" + F("s01m", 64, B64) + "\n-----END " + "CERTIFICATE" + "-----", "证书不是秘密"),
      N("S01-n3", "文件开头一行写的是 BEGIN PRIVATE KEY，结尾是 END PRIVATE KEY", "没有五个连字符的标记，只是在讲格式"),
      N("S01-n4", "ssh-rsa " + F("s01o", 60, B64) + " me@laptop", "ssh 公钥一行"),
  ])

R(id="S02", kind="私钥", priority=31, res=[r"(?s)(?P<s>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:(?!-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----).)*\Z)"],
  boundary="从 BEGIN … PRIVATE KEY 行起，后面再没有 END 行：一直换到这条消息的结尾", replace="从 BEGIN 到消息结尾（一个区间）",
  reason="截断的私钥（被截断、被分成两条消息发的前一半）：宁可多挡。",
  positives=[
      P("S02-p1", "密钥发你了：\n<<1>>", ("-----BEGIN " + "RSA PRIVATE KEY" + "-----\n" + F("s02a", 64, B64) + "\n" + F("s02b", 30, B64), "私钥"), note="没有 END，换到消息结尾"),
      P("S02-p2", "<<1>>", ("-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----", "私钥"), note="只剩 BEGIN 一行"),
      P("S02-p3", "第一块完整：<<1>>\n第二块断了：<<2>>", ("-----BEGIN " + "RSA PRIVATE KEY" + "-----\n" + F("s02c", 48, B64) + "\n-----END " + "RSA PRIVATE KEY" + "-----", "私钥"),
        ("-----BEGIN " + "EC PRIVATE KEY" + "-----\n" + F("s02d", 48, B64), "私钥"), note="完整的那块走 S01，断了的走 S02，两个区间"),
  ],
  negatives=[N("S02-n1", "-----BEGIN " + "PUBLIC KEY" + "-----\n" + F("s02n", 48, B64), "公钥被截断也不是秘密")])

R(id="S03", kind="JWT", priority=32, res=[L + r"(?P<s>eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*)"],
  boundary="eyJ 开头的两段 base64url（头、载荷）加点，再加签名段（可以为空）；前面不能紧挨字母数字下划线连字符", replace="整个令牌",
  reason="JWT。头和载荷都以 eyJ（花括号加引号的 base64）开头，是很强的形状。",
  positives=[
      P("S03-p1", "token 给你：<<1>>", ("eyJ" + F("s03a", 17, B64URL) + ".eyJ" + F("s03b", 40, B64URL) + "." + F("s03c", 43, B64URL), "JWT")),
      P("S03-p2", "Authorization: Bearer <<1>>", ("eyJ" + F("s03d", 17, B64URL) + ".eyJ" + F("s03e", 30, B64URL) + "." + F("s03f", 43, B64URL), "JWT"), note="Bearer 规则也命中同一个值：合并后类型取 JWT（优先级 32 高于 50）"),
      P("S03-p3", "不签名的：<<1>>", ("eyJ" + F("s03g", 17, B64URL) + ".eyJ" + F("s03h", 30, B64URL) + ".", "JWT"), note="alg=none，签名为空，末尾的点一起换"),
  ],
  negatives=[
      N("S03-n1", "这段 base64 是个 JSON 配置：eyJ" + F("s03n", 40, B64URL), "只有一段，没有点分隔的第二、三段，不是 JWT"),
      N("S03-n2", "版本号 v1.2.3 和域名 a.b.example.com", "普通带点的文字"),
  ])

R(id="C01", kind="连接串密码", priority=40,
  res=[r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]*:" + NOPH + r"(?P<s>[^\s/]+)@(?=[^\s/@]+)"],
  boundary="协议://用户:密码@主机：在 :// 到下一个 / 或空白之间（authority 段），第一个冒号之后、最后一个 @ 之前的是密码；用户名可以为空", replace="只换密码那一段，协议、用户名、@、主机都留着",
  reason="带密码的连接串（数据库、消息队列、git remote）。",
  positives=[
      P("C01-p1", "DATABASE_URL=postgres://app:<<1>>@db.internal:5432/main", (F("c01a", 14), "连接串密码")),
      P("C01-p2", "mongodb+srv://admin:<<1>>@cluster0.example.net/test", (F("c01b", 12, B64URL), "连接串密码")),
      P("C01-p3", "redis://:<<1>>@cache:6379/0", (F("c01c", 10), "连接串密码"), note="用户名为空"),
      P("C01-p4", "amqp://guest:<<1>>@mq:5672", ("pa:ss" + F("c01d", 6), "连接串密码"), note="密码里有冒号：第一个冒号之后到最后一个 @ 之前都是密码"),
      P("C01-p5", "git remote add origin https://me:<<1>>@git.example.com/me/repo.git", (F("c01e", 16, B64URL), "连接串密码")),
      P("C01-p6", "mysql://root:<<1>>@127.0.0.1/db 和 mysql://root:<<2>>@10.0.0.2/db", (F("c01f", 8), "连接串密码"), (F("c01g", 8), "连接串密码"), note="两条连接串两个区间"),
  ],
  negatives=[
      N("C01-n1", "打开 https://example.com:8080/docs?page=2", "冒号后面是端口，没有 @"),
      N("C01-n2", "ssh://git@github.com/me/repo.git", "只有用户名，没有密码"),
      N("C01-n3", "git@github.com:me/repo.git", "scp 写法，没有 ://"),
      N("C01-n4", "redis://localhost:6379/1", "没有 @"),
      N("C01-n5", "发到 user@example.com 或 mailto:me@example.com", "邮箱不是连接串"),
      N("C01-n6", "postgres://app:〔密钥已挡掉：连接串密码〕@db:5432/main", "已经换过的占位符不再换（幂等）"),
  ])

# ================================================================ 三、请求头、网址参数（50 上下）
R(id="H01", kind="请求头凭据", priority=50,
  res=[r"(?i)(?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:bearer|basic|token)[ \t]+(?P<s>[A-Za-z0-9._~+/=-]{8,})"],
  boundary="Authorization（或 Proxy-Authorization）后面接 Bearer、Basic、token 加空白，再接 8 位以上的令牌字符；大小写不论", replace="只换令牌，Authorization: Bearer 这几个字留着",
  reason="HTTP 的授权头。",
  positives=[
      P("H01-p1", "curl -H \"Authorization: Bearer <<1>>\" https://api.example.com/v1/me", (F("h01a", 32, B64URL), "请求头凭据")),
      P("H01-p2", "authorization: bearer <<1>>", (F("h01b", 24, B64URL), "请求头凭据"), note="小写"),
      P("H01-p3", "Authorization: Basic <<1>>", ("dXNlcjpmYWtlLXBhc3M" + F("h01c", 12, B64URL) + "==", "请求头凭据"), note="Basic 是 base64 的「用户:密码」"),
      P("H01-p4", "Proxy-Authorization: Basic <<1>>", (F("h01d", 20, B64URL), "请求头凭据")),
      P("H01-p5", "{\"Authorization\": \"Bearer <<1>>\"}", (F("h01e", 30, B64URL), "请求头凭据"), note="JSON 写法"),
  ],
  negatives=[
      N("H01-n1", "他是这件事的 bearer，我是 authorization 的审批人", "只是普通的词，后面没有令牌字符串"),
      N("H01-n2", "Authorization: Bearer <token>", "尖括号占位"),
      N("H01-n3", "请求要带 Authorization 头，值写 Bearer 加令牌", "没有真值"),
      N("H01-n4", "Authorization: Bearer 〔密钥已挡掉：请求头凭据〕", "已经换过（幂等）"),
  ])

R(id="H02", kind="请求头密钥", priority=51,
  res=[r"(?i)(?<![A-Za-z0-9_-])(?:x-api-key|x-auth-token|x-access-token|x-goog-api-key|api-key|ocp-apim-subscription-key)[\"']?[ \t]*:[ \t]*[\"']?" + NOPH + r"(?P<s>[^\s\"',;]{8,})"],
  boundary="X-API-Key、X-Auth-Token、X-Access-Token、X-Goog-Api-Key、Api-Key、Ocp-Apim-Subscription-Key 加冒号，值是 8 位以上非空白字符；大小写不论", replace="只换值",
  reason="常见的 API 密钥请求头。",
  positives=[
      P("H02-p1", "curl -H 'X-API-Key: <<1>>' http://localhost:8000", (F("h02a", 32, HEX), "请求头密钥")),
      P("H02-p2", "x-api-key: <<1>>\ncontent-type: application/json", (F("h02b", 24, B64URL), "请求头密钥")),
      P("H02-p3", "{\"X-Auth-Token\": \"<<1>>\"}", (F("h02c", 20, B64URL), "请求头密钥")),
  ],
  negatives=[
      N("H02-n1", "请求头里要带 X-API-Key，值我之后发你", "后面没有冒号加值"),
      N("H02-n2", "X-API-Key: <your key>", "尖括号占位"),
  ])

R(id="U01", kind="网址参数", priority=52,
  res=[r"(?i)(?<=[?&;])(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|password|passwd|pwd|auth|sig|signature|x-amz-signature|x-amz-security-token)=" + NOPH + r"(?!<[^>\n]*>|\$|\{\{)(?P<s>[^&\s#\"'<>)]+)",
       r"(?<=[?&;])key=" + NOPH + r"(?!<[^>\n]*>|\$|\{\{)(?P<s>[^&\s#\"'<>)]{12,})"],
  boundary="网址（或表单）里紧跟 ? & ; 的参数名，完整等于 api_key、apikey、access_token、auth_token、token、secret、client_secret、password、passwd、pwd、auth、sig、signature 之一，等号后面至少一个字符；"
           "参数名 key 要求值 12 位以上（太短的 key= 多半是别的意思）", replace="只换参数值，网址的其余部分留着",
  reason="网址查询参数里的密钥、令牌。",
  positives=[
      P("U01-p1", "https://api.example.com/v1/items?token=<<1>>&page=2", (F("u01a", 24, B64URL), "网址参数")),
      P("U01-p2", "GET /download?file=a.zip&access_token=<<1>> HTTP/1.1", (F("u01b", 30, B64URL), "网址参数")),
      P("U01-p3", "https://x.example.com/login?user=me&password=<<1>>", ("abc", "网址参数"), note="明确的 password 参数，再短也挡"),
      P("U01-p4", "https://x.example.com/cb?code=1&api_key=<<1>> 就这样", (F("u01c", 16, HEX), "网址参数"), note="到空白为止"),
      P("U01-p5", "https://maps.example.com/api?key=<<1>>", (F("u01d", 20, B64URL), "网址参数"), note="参数名 key，值 12 位以上"),
      P("U01-p6", "https://s3.example.com/f?X-Amz-Signature=<<1>>&x=1", (F("u01e", 64, HEX), "网址参数")),
  ],
  negatives=[
      N("U01-n1", "https://www.example.com/search?q=token+ring+network&page=2", "token 不是参数名，只是搜索词的一部分"),
      N("U01-n2", "https://example.com/m?tokenizer=gpt2&sort=key", "参数名是 tokenizer，不等于 token；sort=key 的 key 是值"),
      N("U01-n3", "https://example.com/p?key=name", "参数名 key 但值只有 4 位"),
      N("U01-n4", "https://example.com/very/long/path/with/many/segments/and-a-very-long-slug-name-that-goes-on-and-on-1234567890/index.html?ref=home&utm_source=newsletter&utm_campaign=spring",
        "正常的长网址，没有密钥类的参数名"),
      N("U01-n5", "https://example.com/x?token=<TOKEN>&password=${DB_PASSWORD}", "尖括号占位和变量引用"),
  ])

# ================================================================ 四、「键名＋值」的写法（60）
R(id="K01", kind="键值", priority=60,
  res=[KV_EN_LEFT + r"\"" + NOPH + PLACEHOLDER_VALUES + r"(?P<s>[^\"\n]+)\"",
       KV_EN_LEFT + r"'" + NOPH + PLACEHOLDER_VALUES + r"(?P<s>[^'\n]+)'",
       KV_EN_LEFT + NOPH + PLACEHOLDER_VALUES + r"(?P<s>" + ASCII_VALUE + r")"],
  boundary="键名（大小写不论）是 api_key、apikey、api_secret、secret_key、secret、client_secret、access_key、access_token、auth_token、refresh_token、private_key、token、passwd、password、pwd、passphrase 之一，"
           "键名前面可以有最多 4 段「词加 _ . -」的前缀（OPENAI_API_KEY、db.password），键名后面不能紧跟字母数字下划线（api_key_env、password_hash、tokens、token_count 都不算）；"
           "键名后可有引号、]，然后是 = : ： => := 之一；值可以是双引号内、单引号内（引号里可有空格），或不带引号的 ASCII 可见字符（到空白、引号、逗号、分号、全角标点为止）。"
           "值只要非空就挡，不管多短。以下写法不算值：None、null、nil、undefined、true、false、****、xxx、<…>、${…}、$VAR、{{…}}、%s、已经换好的占位符",
  replace="只换值本身（引号内的话，引号留着）；键名、分隔符留着",
  reason="明确的密码、密钥赋值：键名已经说明了这是秘密，值多短都挡。含 JSON、YAML、.env、Python 字典、命令行 --password=。",
  positives=[
      P("K01-p1", "api_key=<<1>>", (F("k01a", 20), "键值")),
      P("K01-p2", "API_KEY: <<1>>", (F("k01b", 20), "键值")),
      P("K01-p3", "secret: <<1>>", (F("k01c", 16, B64URL), "键值")),
      P("K01-p4", "token = <<1>>", (F("k01d", 24), "键值")),
      P("K01-p5", "password：<<1>>", ("Zx9#kLm2", "键值"), note="全角冒号"),
      P("K01-p6", "DB_PASSWORD=<<1>>", ("hunter2", "键值"), note="前缀 DB_"),
      P("K01-p7", "password = <<1>>", ("abc", "键值"), note="明确赋值，再短也挡"),
      P("K01-p8", "pwd=<<1>>", ("1", "键值"), note="一个字符也挡"),
      P("K01-p9", "{\"username\": \"me\", \"password\": \"<<1>>\"}", ("correct horse battery", "键值"), note="JSON 里带引号的值，值里有空格；引号留着"),
      P("K01-p10", "db:\n  user: app\n  password: '<<1>>'\n", ("p@ss w0rd!", "键值"), note="YAML 单引号"),
      P("K01-p11", "config = {'secret': '<<1>>', 'debug': True}", (F("k01e", 18), "键值"), note="Python 字典"),
      P("K01-p12", "client_secret: <<1>>\nclient_id: abc", (F("k01f", 24, B64URL), "键值")),
      P("K01-p13", "mysql --user=root --password=<<1>> -h db", (F("k01g", 10), "键值"), note="命令行参数"),
      P("K01-p14", "settings[\"access_token\"] = \"<<1>>\"", (F("k01h", 24), "键值"), note="下标写法"),
      P("K01-p15", "OPENAI_API_KEY=<<1>>", (F("k01i", 12, B64URL), "键值"), note="值不像任何一家的前缀，靠键名挡"),
      P("K01-p16", "PASSWORD: <<1>>\nPASSWD=<<2>>", ("a1", "键值"), ("b2", "键值"), note="大小写不论，两处"),
      P("K01-p17", "private_key = <<1>>", (F("k01j", 30, B64URL), "键值")),
      P("K01-p18", "refresh_token: <<1>>", (F("k01k", 30, B64URL), "键值")),
      P("K01-p19", "https_proxy 的 passphrase=<<1>> 在 ~/.ssh/config 里", (F("k01l", 10), "键值")),
  ],
  negatives=[
      N("K01-n1", "这个 token 数不够，prompt 太长了", "token 后面是「数」，不是分隔符"),
      N("K01-n2", "密码学和 password manager 都值得学", "只是词，没有赋值"),
      N("K01-n3", "the password policy requires 12 characters", "password 后面是空格加 policy，没有 = 或 :"),
      N("K01-n4", "api_key_env = \"DEEPSEEK_API_KEY\"", "键名是 api_key_env（后面接了 _env），值是环境变量的名字"),
      N("K01-n5", "password_hash = bcrypt(x)\nself.password_hint = 1", "键名后面紧跟下划线加词：不是这些名字"),
      N("K01-n6", "max_tokens: 65536\nnum_tokens=100\ntoken_count = 5\naccess_token_expiry = 3600", "tokens、token_count、access_token_expiry 不是键名本身"),
      N("K01-n7", "def login(password):\n    return check(password)", "只有变量名，没有赋值"),
      N("K01-n8", "password = None\ntoken: null\nsecret = ****\npassword: ${DB_PASSWORD}\ntoken = <your token>\nsecret: {{ secret }}", "明显不是真值的写法"),
      N("K01-n9", "password:\nsecret=\n", "没有值"),
      N("K01-n10", "keyword=python&tokenizer=bpe&secretary=Lee", "keyword、tokenizer、secretary 不是键名"),
      N("K01-n11", "password=〔密钥已挡掉：键值〕", "已经换过的占位符不再换（幂等）"),
      N("K01-n12", "token: 数量太多 password: 太简单了", "不带引号的值要求 ASCII 可见字符，中文句子不挡（见漏挡清单）"),
  ])

R(id="K02", kind="键值", priority=61,
  res=[ZH_LEFT + r"[\"“「‘']" + NOPH + PLACEHOLDER_VALUES + r"(?P<s>[^\"”」’'\n]+)[\"”」’']",
       ZH_LEFT + NOPH + PLACEHOLDER_VALUES + r"(?P<s>" + ASCII_VALUE + r")"],
  boundary="中文键名 密码、口令、密钥、秘钥、令牌、访问令牌、API 密钥（API Key）后面接 是／为（可再接冒号）或 ：: =，值是引号（半角、全角、「」）里的话，或不带引号的 ASCII 可见字符；"
           "值是中文的话（「密码是你的生日」）不算", replace="只换值", reason="用户用中文说的密码、密钥。",
  positives=[
      P("K02-p1", "我的密码是<<1>>，别告诉别人", ("Zq4!w8Lp", "键值")),
      P("K02-p2", "密钥：<<1>>", (F("k02a", 24, B64URL), "键值")),
      P("K02-p3", "API 密钥为 <<1>>。", (F("k02b", 24), "键值")),
      P("K02-p4", "口令=<<1>>", ("123456", "键值")),
      P("K02-p5", "令牌：“<<1>>”", ("abc 123", "键值"), note="全角引号里可以有空格"),
      P("K02-p6", "wifi 密码是：<<1>>", ("hello2024", "键值")),
      P("K02-p7", "密码：「<<1>>」", ("p w", "键值")),
      P("K02-p8", "密码是 <<1>> 然后 口令为 <<2>>", ("a1b2", "键值"), ("c3d4", "键值"), note="两处两个区间"),
  ],
  negatives=[
      N("K02-n1", "密码学是一门很有意思的课", "密码后面是「学」"),
      N("K02-n2", "密码是什么意思？", "值是中文"),
      N("K02-n3", "密钥管理和令牌桶算法都要懂", "没有分隔符和值"),
      N("K02-n4", "他的口令是你的生日", "值是中文"),
      N("K02-n5", "密码是〔密钥已挡掉：键值〕", "已经换过的占位符不再换（幂等）"),
      N("K02-n6", "密码是 None", "不是真值的写法"),
  ])

# ================================================================ 五、全局反例（不属于任何一条规则：哈希、网址、变量名）
GLOBAL_NEGATIVES = [
    N("G-n1", "这次提交的哈希是 " + F("g1", 40, HEX) + "，请对一下", "git 提交哈希 40 位十六进制：不是密钥"),
    N("G-n2", "sha256 是 " + F("g2", 64, HEX), "哈希值的讨论：64 位十六进制不挡"),
    N("G-n3", "订单号 123e4567-e89b-12d3-a456-426614174000 查一下", "UUID"),
    N("G-n4", "看这个链接 https://example.com/articles/2026/11/how-to-write-a-very-long-descriptive-slug-for-an-article-about-memory-systems-and-agents-part-3/index.html#section-4", "正常的长网址"),
    N("G-n5", "我的邮箱是 me@example.com，电话 +66 81 234 5678", "邮箱和电话不属于这个规则表（见未覆盖清单）"),
    N("G-n6", "Token 数和 API 调用次数都按月结算，key 指标看这张表", "文章里的 token、key 词，没有赋值"),
    N("G-n7", "sk-learn 的文档在 https://scikit-learn.org/，pip install scikit-learn", "sk- 开头的短词和包名"),
    N("G-n8", "函数 get_api_key() 返回环境变量里的值，调用方是 load_secret(name)", "代码里的函数名、变量名本身，没有赋值"),
    N("G-n9", "我们的 password 字段是必填的，token 字段由服务端生成", "「password 字段」「token 字段」只是名词"),
    N("G-n10", "2026-11-10T09:30:00Z 12:30:45 v1.2.3 192.168.1.10", "时间、版本号、IP"),
    N("G-n11", "data:image/png;base64," + F("g11", 90, B64), "一段 base64 图片数据：不是密钥"),
    N("G-n12", "今天的天气不错，我们去吃饭吧。", "普通文字一个字不能动"),
]

# ================================================================ 六、多规则重叠、合并、数量、幂等
OVERLAP = [
    P("M-p1", "OPENAI_API_KEY=<<1>>", ("sk-proj-" + F("m1", 40, B64URL), "OpenAI"), note="T02 和 K01 命中同一个区间：只算一次，类型取优先级高的 OpenAI"),
    P("M-p2", "Authorization: Bearer <<1>>", ("sk-ant-" + F("m2", 40, B64URL), "Anthropic"), note="T01 和 H01 命中同一个区间：Anthropic"),
    P("M-p3", "{\"api_key\": \"<<1>>\"}", ("sk-" + F("m3", 32, HEX), "DeepSeek"), note="T03、T05（不会，32 位十六进制后面是引号）和 K01：DeepSeek"),
    P("M-p4", "password=<<1>>", ("sk-proj-" + F("m4", 30, B64URL), "OpenAI"), note="密码赋值的值恰好是 OpenAI 密钥"),
    P("M-p5", "同一把用了两次：<<1>> 和 <<2>>", ("sk-ant-" + F("m5", 32, B64URL), "Anthropic"), ("sk-ant-" + F("m5", 32, B64URL), "Anthropic"), note="同一个值出现两次是两个区间，数 2；重叠才合并，重复不合并"),
    P("M-p6", "token=<<1>>&x=1 以及 密码是<<2>>", (F("m6", 12), "键值"), ("pw12", "键值"), note="两处各一个"),
    P("M-p7", "DATABASE_URL=postgres://u:<<1>>@h/db", ("hunter2", "连接串密码"), note="DATABASE_URL 不是密钥键名；只有连接串规则命中"),
    P("M-p8", "curl -H 'Authorization: Bearer <<1>>' 'https://x.example.com/a?token=<<2>>'", (F("m8a", 24, B64URL), "请求头凭据"), (F("m8b", 24, B64URL), "网址参数"), note="两个不同的位置"),
    P("M-p9", "git clone https://me:<<1>>@git.example.com/r.git，另外 aws_secret_access_key=<<2>>", (GH("ghp_", "m9"), "GitHub"), (F("m9b", 40, B64), "AWS"), note="连接串密码是 GitHub 令牌：合并后取 GitHub"),
]

# 已经换好的占位符：再过一遍一字不变，数 0（幂等）
IDEMPOTENT_TEXTS = [
    "我把 key 贴给你：" + PLACEHOLDER % "OpenAI" + "，先别记下来。",
    "OPENAI_API_KEY=" + PLACEHOLDER % "OpenAI",
    "Authorization: Bearer " + PLACEHOLDER % "JWT",
    "postgres://app:" + PLACEHOLDER % "连接串密码" + "@db:5432/main",
    "密码是" + PLACEHOLDER % "键值" + "，" + PLACEHOLDER % "私钥",
    "https://x.example.com/?token=" + PLACEHOLDER % "网址参数",
]

# ================================================================ 七、定下来的取舍（pin 住，免得悄悄变）
# 宁可多挡造成的、明说接受的误挡：过滤以后值被换掉（expected 是挡掉的样子）。要不要改，主会话定。
ACCEPTED_OVER_MASK = [
    P("OM-1", "参数 password: <<1>> 是必填的", ("str", "键值"), note="类型标注 password: str 里的 str 被当成值。接受：写代码时变量名后接冒号很常见，被换掉的只是一个词"),
    P("OM-2", "cd /tmp && PWD=<<1>>", ("/home/me", "键值"), note="环境变量 PWD 是当前目录，不是密码；pwd 在键名表里，接受"),
]

# 漏挡清单：现在认不出、过滤以后一字不变。这些是「已知的洞」，写在文档里，不假装能挡。
KNOWN_MISSES = [
    N("MISS-1", "密码是你猜（一个很难猜的中文句子）", "中文值：值是中文就不挡（防止把「密码是你的生日」这类话全挡）"),
    N("MISS-2", "sk - proj - " + F("x2", 30, B64URL), "被空格拆开、故意写成不连续的密钥"),
    N("MISS-3", "key: " + F("x3", 32, HEX), "键名是普通的 key，值是 32 位十六进制：键名不在表里（key 太泛），也没有厂商前缀"),
    N("MISS-4", "我的 azure 存储 key 是 " + F("x4", 86, B64) + "==", "Azure 存储密钥没有前缀，键名只写了英文 key（太泛，不在键名表里）"),
    N("MISS-5", "第一条：" + "sk-proj-" + F("x5a", 12, B64URL) + "；第二条（接上）：" + F("x5b", 28, B64URL), "一把密钥被拆在两条消息里，每一条单独看都不够长"),
    N("MISS-6", "base64 之后：" + "c2stcHJvai0" + F("x6", 40, B64), "密钥被 base64 之类的再编码以后"),
]

# 表里每一类行数的下限（测试核）
MIN_POSITIVES_PER_RULE = 1
MIN_NEGATIVES_PER_RULE = 1

# 派工单点名要覆盖的类型（测试核：每一项至少有一条规则）
REQUIRED_KINDS = ("OpenAI", "Anthropic", "DeepSeek", "Google", "GitHub", "GitLab", "Slack", "AWS", "Stripe", "HuggingFace", "Telegram", "私钥", "JWT", "连接串密码",
                  "请求头凭据", "请求头密钥", "网址参数", "键值")
# GitHub 前缀：派工单点名的六种都要有正例
GITHUB_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")
# 公开文档里出现过的示例密钥：不能当正例（它们虽然不是真值，但会在别处被当成「已知示例」，也容易让静态扫描器报警）
DOC_EXAMPLE_KEYS = ("AKIAIOSFODNN7" + "EXAMPLE", "wJalrXUtnFEMI/K7MD" + "ENG/bPxRfiCYEXAMPLEKEY")
