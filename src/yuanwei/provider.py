"""模型提供方：请求体的拼法，和一个兼容 OpenAI chat 接口的真实提供方（默认指向 DeepSeek，地址、模型、密钥的环境变量名都可配）。

参数照研究：deepseek-flash、温度 0、思考 high、max_tokens 65536。产品不钉服务指纹：每次返回的模型名和指纹由调用方记进费用账。
这个模块只写不在测试里用：所有测试和回放用假提供方（一个 callable，收到请求字典、返回文字或带用量的字典）。
"""
import json
import socket
import urllib.error
import urllib.request

from .ask import RetryableNetworkError
from .config import MAX_PROVIDER_PARAMS, api_key
from .errors import ProviderError

PARAMETERS = dict(MAX_PROVIDER_PARAMS)


def body_bytes(body):
    """请求字节：紧凑 JSON、不转义中文（取自研究的 body_bytes）。里面有孤立代理字符时改用 \\uXXXX 转义。"""
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def write_request_bytes(text, params=None):
    """整理请求的完整 API 请求体：request["text"] 原样作为唯一一条用户消息，没有系统消息（取自研究的 request_body）。"""
    return body_bytes({**(params or PARAMETERS), "messages": [{"role": "user", "content": text}]})


class OpenAICompatProvider:
    """调用方式：provider(request) → {"text","model","fingerprint","usage"}。request["body"] 若有就原样发，否则按 request["text"] 拼整理请求。"""

    def __init__(self, base_url, api_key_env, params=None, timeout=600, opener=None, key_file=None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key_env, self.key_file = api_key_env, key_file
        self.params = dict(params or PARAMETERS)
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def __call__(self, request):
        key = api_key(self.api_key_env, self.key_file) if self.api_key_env else None
        body = request.get("body_bytes") or write_request_bytes(request["text"], self.params)
        req = urllib.request.Request(self.url, data=body, headers=dict({"Content-Type": "application/json"}, **({"Authorization": "Bearer " + key} if key else {})))
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 429:
                raise RetryableNetworkError("HTTP %d" % e.code)
            raise ProviderError("提供方拒绝了请求：HTTP %d" % e.code)
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
            raise RetryableNetworkError(type(e).__name__)
        try:
            obj = json.loads(raw.decode("utf-8"))
            choice = obj["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise RetryableNetworkError("回应读不了")
        if choice.get("finish_reason") not in (None, "stop"):
            raise RetryableNetworkError("回应没有正常结束：%s" % choice.get("finish_reason"))
        return {"text": text, "model": obj.get("model"), "fingerprint": obj.get("system_fingerprint"), "usage": obj.get("usage")}
