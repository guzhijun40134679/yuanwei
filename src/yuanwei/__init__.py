"""原委（Yuanwei）：给 AI 助手用的长期记忆。记下对话原话，按「主线→哪一回」整理，回答前先查。预览版。"""
from .config import Config
from .errors import *  # noqa: F401,F403
from .memory import Memory

__version__ = "0.1.0a1"
__all__ = ["Memory", "Config", "__version__"]
