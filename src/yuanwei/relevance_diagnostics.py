"""相关度标定的只读诊断：只看身份，不启动引擎、不读向量/密钥/对话。"""
import json
from pathlib import Path

from . import relevance as RL
from .config import Config
from .errors import ConfigError, VectorIdentityMismatch


def from_config(cfg, identity=None, vector_fp=None):
    if not cfg.relevance_floor:
        return RL.off_status()
    if cfg.retrieval != "hybrid":
        return RL.keyword_status()
    identity = identity if identity is not None else (cfg.embedding or {}).get("identity")
    if identity is None:
        return {"status": "invalid_identity", "threshold": None,
                "message": "缺少向量编码身份，混合检索不能运行；请先核对向量配置。",
                "recommendation": "请重新选择推荐的向量模型并核对身份。"}
    try:
        return RL.resolve(identity, vector_fp=vector_fp)
    except (ValueError, TypeError, KeyError, VectorIdentityMismatch):
        return {"status": "invalid_identity", "threshold": None,
                "message": "向量身份不完整或不一致，混合检索不能运行；请先核对向量配置。",
                "recommendation": "请重新选择推荐的向量模型并核对身份。"}


def installed(home):
    """只读这个 Hermes 归属已确认的配置；不打开数据库或生成客户端。"""
    from . import hermes_profile as HP
    path = Path(home) / "yuanwei" / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not HP.home_check(raw, str(home))["ok"]:
            return {"status": "unconfirmed", "message": "原委设置的归属未确认，未核对语义阈值。"}
        cfg = Config.load(raw["config_path"]) if raw.get("config_path") else Config()
        from . import vectors as VX
        out = from_config(cfg)
        out["query_timeout"] = VX.query_timeout_status(cfg.embedding)
        return out
    except FileNotFoundError:
        if not path.exists():
            return {"status": "not_installed", "message": "未找到原委设置，未核对语义阈值。"}
        return {"status": "unavailable", "message": "向量配置读不到，未核对语义阈值。"}
    except (OSError, ValueError, TypeError, ConfigError):
        return {"status": "unavailable", "message": "向量配置读不到，未核对语义阈值。"}
