"""向量缓存与向量提供方（选装）。

缓存的键 ＝ 实际送进模型的完整文字的 sha256 ＋ 编码身份指纹（取自研究的 入口_向量：identity_fp／text_key）。
编码身份（固定字段）：提供方、模型标识、模型文件指纹、运行包版本、加载上下文长度、维度、归一化、输入前缀、任务模板、文字处理。
一次读取只接受和所绑定的编码身份完全相同的向量；有字段不同、或有「未知」，一律拒绝。缺向量就报缺，不退回关键词。
提供方：兼容 /v1/embeddings 的用户自配服务；回应要完整校验（index 完整唯一覆盖 0…n−1、维度一致、数值有限、非零）。
这个模块不下载模型、不启动任何服务（下载只在安装向导里、用户同意后由 embedsetup 让本机的 Ollama／LM Studio 做）。
缺向量：库默认（vector_fallback＝strict）报缺、不退回关键词；产品配置（partial）有向量的照走、缺的只靠关键词，并登记（见 retrieve.Index.info）。
"""
import hashlib
import ipaddress
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import MissingVector, ProviderError, VectorIdentityMismatch

UNKNOWN = "未知"
IDENTITY_FIELDS = ("provider", "model", "model_file_sha256", "runtime_version", "context_length", "dimension", "normalization", "prefix", "template", "text_processing")
EMBED_BATCH = 16


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def check_identity(d):
    if not isinstance(d, dict) or set(d) != set(IDENTITY_FIELDS):
        have = sorted(d) if isinstance(d, dict) else type(d).__name__
        raise VectorIdentityMismatch("编码身份的字段必须恰好是 %s；现在是 %s" % (list(IDENTITY_FIELDS), have))
    unk = [k for k in IDENTITY_FIELDS if d[k] == UNKNOWN or d[k] is None]
    if unk:
        raise VectorIdentityMismatch("编码身份有「未知」项 %s，不能放行检索" % unk)
    return d


def identity_fp(d):
    return hashlib.sha256(canon(check_identity(d)).encode("utf-8")).hexdigest()


def text_key(text):
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (UnicodeEncodeError, AttributeError):
        raise VectorIdentityMismatch("文字不是能编成 UTF-8 的字符串：%r" % (text,))


def check_vector(v, dim):
    if not isinstance(v, list) or not v:
        raise ProviderError("向量不是非空列表")
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ProviderError("向量里有非数值、布尔或非有限的数")
    if not any(v):
        raise ProviderError("零向量")
    if isinstance(dim, int) and len(v) != dim:
        raise ProviderError("向量维度 %d，身份里写的是 %d" % (len(v), dim))


def register(store, identity, vectors_by_text):
    """把 {文字: 向量} 存进库（同键同值是空操作，同键异值拒绝）。返回编码身份指纹。"""
    fp = identity_fp(identity)
    store.put_identity(fp, identity)
    items = {}
    for t, v in vectors_by_text.items():
        check_vector(list(v), identity["dimension"])
        items[text_key(t)] = list(v)
    store.put_vectors(fp, items)
    return fp


def missing_texts(store, fp, texts):
    have = store.get_vectors(fp, {text_key(t) for t in texts})
    return sorted({t for t in texts if text_key(t) not in have})


def parse_embeddings_response(obj, n, dim):
    """/v1/embeddings 的回应校验。返回按输入顺序排好的向量列表。"""
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, list) or len(data) != n:
        raise ProviderError("向量服务回应的条数 %s 不等于请求的 %d" % (len(data) if isinstance(data, list) else None, n))
    idx = [d.get("index") for d in data if isinstance(d, dict)]
    if sorted(idx) != list(range(n)):
        raise ProviderError("向量服务回应的 index 没有完整唯一地覆盖 0…%d" % (n - 1))
    out = [None] * n
    for d in data:
        v = d.get("embedding")
        check_vector(v, dim)
        out[d["index"]] = [float(x) for x in v]
    return out


class HttpEmbeddingClient:
    """兼容 /v1/embeddings 的客户端。不加前缀、不发 dimensions、分批、失败就停、不自动重试。"""

    def __init__(self, base_url, model, api_key=None, timeout=120, dimension=None, opener=None):
        self.url, self.model, self.api_key, self.timeout, self.dimension = base_url.rstrip("/") + "/embeddings", model, api_key, timeout, dimension
        self._open = opener or urllib.request.urlopen

    def embed_batch(self, texts):
        body = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with self._open(req, timeout=self.timeout) as r:
                obj = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 429:
                raise ServiceDown("向量服务出错：HTTP %d" % e.code)
            raise Rejected("向量服务出错：HTTP %d" % e.code)
        except (urllib.error.URLError, OSError) as e:
            raise ServiceDown("向量服务出错：%s" % type(e).__name__)
        except ValueError as e:
            raise ProviderError("向量服务出错：%s" % type(e).__name__)
        return parse_embeddings_response(obj, len(texts), self.dimension)


class ServiceDown(ProviderError):
    """向量服务连不上、超时、5xx、429、断路器打开：可以退避重试。消息里只有类别，没有请求或回应的正文。
    reason：可选的原因类别（自带引擎用：engine_starting、engine_failed、engine_stopped、engine_disabled、engine_identity_conflict、engine_identity_mismatch）；没有就是 service_down。"""
    reason = None

    def __init__(self, msg="", reason=None):
        super().__init__(msg or (reason or ""))
        if reason:
            self.reason = reason


def down_reason(e):
    """读取登记用的原因类别。"""
    return getattr(e, "reason", None) or "service_down"


class Rejected(ProviderError):
    """向量服务确定性地拒绝了这条输入（4xx，例如超长）：重试没用。"""


def is_local_url(url):
    """只认字面的 localhost、127.0.0.1 一类回环地址、[::1]。不做域名解析：指向局域网的别名、0.0.0.0、公网地址都不算本机。"""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class GuardedClient:
    """断路器：上一次失败以后 breaker_seconds 内不再去连（直接报 ServiceDown），免得每次读取都等满超时。状态只放进程内存。"""

    def __init__(self, inner, breaker_seconds=30, clock=time.monotonic):
        self.inner, self.breaker_seconds, self._clock = inner, breaker_seconds, clock
        self.open_until = 0.0
        self.last_error = None

    def embed_batch(self, texts):
        if self._clock() < self.open_until:
            raise ServiceDown("断路器：%s 秒内不再连" % self.breaker_seconds)
        try:
            out = self.inner.embed_batch(texts)
        except ServiceDown as e:
            self.open_until = self._clock() + self.breaker_seconds
            self.last_error = str(e)
            raise
        self.open_until = 0.0
        return out


def build_client(emb, clock=time.monotonic, opener=None, config_path=None):
    """按配置里的 embedding 造客户端（查询路径用：超时 timeout_query，加断路器）。没配地址返回 None；远端地址没许可就拒绝。
    自带引擎（managed）：交给 enginerun.resolve_client（四处造客户端的地方收成这一处）。"""
    emb = emb or {}
    if emb.get("managed"):
        from . import enginerun as ER
        return ER.resolve_client(emb, "query", config_path=config_path, clock=clock)
    url = emb.get("base_url")
    if not url or not emb.get("model"):
        return None
    if not is_local_url(url) and not emb.get("allow_remote"):
        raise ProviderError("向量服务地址不在本机，而且没有许可远端（allow_remote）")
    dim = (emb.get("identity") or {}).get("dimension")
    key = None
    if emb.get("api_key_env"):
        import os
        key = os.environ.get(emb["api_key_env"]) or None
    inner = HttpEmbeddingClient(url, emb["model"], api_key=key, timeout=emb.get("timeout_query", 5), dimension=dim, opener=opener)
    return GuardedClient(inner, emb.get("breaker_seconds", 30), clock)


def ensure_vectors(store, identity, texts, client, keep_first=False):
    """缺的文字用客户端算（分批；任何一批出错就停，已存的留着）。返回新算的条数。client.embed_batch(list[str]) → list[list[float]]。"""
    fp = identity_fp(identity)
    store.put_identity(fp, identity)
    miss = missing_texts(store, fp, texts)
    for i in range(0, len(miss), EMBED_BATCH):
        chunk = miss[i:i + EMBED_BATCH]
        vecs = client.embed_batch(chunk)
        for v in vecs:
            check_vector(v, identity["dimension"])
        store.put_vectors(fp, {text_key(t): v for t, v in zip(chunk, vecs)}, keep_first=keep_first)
    return len(miss)
