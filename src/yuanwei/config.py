"""配置：一个普通的数据类，能从字典或 TOML 文件读。密钥不进配置，只写环境变量的名字。"""
import dataclasses
import hashlib
import json
import os

from .cards import LightConfig
from .errors import ConfigError

PROMPT_VERSIONS = ("B0", "A")
LONG_LIMIT = 1500              # 超过这个码点数的单条不送去整理（记「超长未整理」），全文照存、全文建索引
MAX_PROVIDER_PARAMS = {"model": "deepseek-flash", "temperature": 0, "reasoning_effort": "high", "max_tokens": 65536}


@dataclasses.dataclass
class Config:
    # ---- 整理（写入）
    write_prompt: str = "B0"                  # B0（默认）或 A：切换只影响以后封的步
    allow_summary_edit: bool = True
    strict_grouping: bool = False
    light: LightConfig = dataclasses.field(default_factory=LightConfig)
    long_limit: int = LONG_LIMIT
    # ---- 封步（暂定的工程参数，不是验证过的）
    idle_minutes: int = 30
    max_step_messages: int = 40
    max_step_chars: int = 8000
    # ---- 读取
    retrieval: str = "keyword"                # keyword（基础，纯标准库）或 hybrid（要向量）
    vector_fallback: str = "strict"           # 混合模式缺向量时：strict＝报错（库默认，研究装置的护栏）；partial＝有向量的照走、缺的只靠关键词，并登记（向导写这个）
    index_version: int = 2                    # 新工作区用 2（单条不限 98 段）；参照回放用 1
    first_budget: int = 16000
    total_budget: int = 24000
    pool_cap: int = 10
    neighbors: int = 1
    recall_page_chars: int = 30000            # recall 一页最多多少字符（宿主单次返回上限更小时调小）
    get_messages_page_tokens: int = 8000
    # ---- 便条（自动档）：数值是离线测量前的暂定值；相关度门的门槛待测量后定，在那之前写「关」(None) 并标「待定」
    note_chars: int = 8000                    # 便条全文（开头说明、边界、原话、提示行、编号）最多多少字符
    note_tokens: int = 4200                   # 同时满足的估算词元上限（约 8,000 字符）；首段占三分之二
    note_oversize: str = "stop"               # 整条放不下：stop＝停（研究原规则），skip＝跳过接着装。两种在测量里分不出，默认保持 stop
    note_free_gate: bool = True               # 免查门（只管整句寒暄、应答和显式标明的宿主命令不查）；读取权限（禁读）不受这个开关影响，始终执行
    note_tau_kw: float = None                 # 关键词覆盖率门槛；None＝关（默认）
    note_tau_vec: float = None                # 向量相似度门槛；None＝关（默认）；纯关键词模式不起作用
    note_gates_status: str = "预览候选：关"
    rule_in_note: bool = False                # True：把常驻规矩那句放进便条开头
    note_max_listed: int = 10
    # ---- 费用（软阈值）
    daily_cap_cny: float = None               # 每日软阈值；None＝不设
    price_in_miss_cny_per_m: float = 2.0
    price_in_hit_cny_per_m: float = 0.04
    price_out_cny_per_m: float = 8.0
    worst_out_factor: int = 2
    # ---- 提供方（密钥只写环境变量名）
    provider: dict = dataclasses.field(default_factory=lambda: {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY", "timeout": 600,
                                                                 "params": dict(MAX_PROVIDER_PARAMS)})
    # 向量服务；还可以有：identity、runtime、timeout_query（5）、timeout_worker（60）、backfill_batch（16）、breaker_seconds（30）、worker_max_run_seconds（600）、backoff_seconds、allow_remote、weak_identity
    embedding: dict = dataclasses.field(default_factory=lambda: {"base_url": None, "api_key_env": None, "model": None, "timeout": 120})
    answer_timezone: str = "Asia/Bangkok"

    def validate(self):
        if self.write_prompt not in PROMPT_VERSIONS:
            raise ConfigError("write_prompt 只能是 %s，实际 %r" % (PROMPT_VERSIONS, self.write_prompt))
        if self.retrieval not in ("keyword", "hybrid"):
            raise ConfigError("retrieval 只能是 keyword 或 hybrid，实际 %r" % (self.retrieval,))
        if self.vector_fallback not in ("strict", "partial"):
            raise ConfigError("vector_fallback 只能是 strict 或 partial，实际 %r" % (self.vector_fallback,))
        if self.index_version not in (1, 2):
            raise ConfigError("index_version 只能是 1 或 2")
        if self.note_oversize not in ("stop", "skip"):
            raise ConfigError("note_oversize 只能是 stop 或 skip，实际 %r" % (self.note_oversize,))
        for k in ("note_tau_kw", "note_tau_vec"):
            v = getattr(self, k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1):
                raise ConfigError("%s 要么不设（关），要么是 0 到 1 之间的数，实际 %r" % (k, v))
        for k in ("note_chars", "note_tokens", "note_max_listed"):
            v = getattr(self, k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ConfigError("%s 必须是正整数" % k)
        for k in ("idle_minutes", "max_step_messages", "max_step_chars", "long_limit", "first_budget", "total_budget"):
            v = getattr(self, k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ConfigError("%s 必须是正整数" % k)
        return self

    def write_snapshot(self):
        """影响整理输入的那些项的完整快照（封步时存进这一步，执行和重试都按它来）。"""
        return {"light": self.light.as_dict(), "allow_summary_edit": self.allow_summary_edit, "strict_grouping": self.strict_grouping, "long_limit": self.long_limit}

    @staticmethod
    def snapshot_version(snap):
        """快照的指纹（就是写进每一步的配置版本）。"""
        return hashlib.sha256(json.dumps(snap, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    def config_version(self):
        """写进每一步的配置版本：影响整理输入的那些项的指纹。"""
        return self.snapshot_version(self.write_snapshot())

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        light = d.pop("light", None)
        c = cls(**d)
        if light:
            c.light = LightConfig(**light)
        return c.validate()

    @classmethod
    def load(cls, path):
        try:
            import tomllib
            with open(path, "rb") as f:
                return cls.from_dict(tomllib.load(f))
        except OSError as e:
            raise ConfigError("读不到配置文件：%s" % e)


def api_key(env_name, key_file=None):
    """取密钥：先读这个机器人自己的密钥文件（key_file，权限要对），再读全局密钥文件（只有给了 key_file 才看），再读环境变量；都没有就报错（文字里只有变量名，没有值）。"""
    from . import keystore
    v, src = keystore.lookup(env_name, key_file)
    if not v:
        if src.startswith("file_unusable"):
            raise ConfigError("密钥文件不能用（%s），环境变量 %s 也没有设置" % (src.split(":", 1)[1], env_name))
        raise ConfigError("环境变量 %s 没有设置" % env_name)
    return v
