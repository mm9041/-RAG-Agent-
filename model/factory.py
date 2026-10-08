"""
模型工厂：对话模型与 Embedding 的**懒加载**单例

为什么不在模块顶层直接 `chat_model = ...`：
那样会在 **import 的瞬间**就创建网络客户端、读取环境变量。于是任何
`from model.factory import chat_model` 的模块（工具、服务、测试脚本、IDE 索引）
都会被动承担这个副作用 —— 没有 key 就 import 失败，测试也没法只导入逻辑。

改成 `get_chat_model()` / `get_embed_model()` 之后：
- import 本模块零副作用；
- 第一次真正使用才初始化，且全局只创建一次。

另外这里统一配置了**超时与重试**（见 config/model.yml 的说明）：
SDK 默认只按状态码重试，而 DashScope 把限流报成 400，落在重试范围之外，
所以 embedding 额外套了一层按**消息内容**分类的退避重试。
"""
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional

from utils.env import ensure_env_loaded as _ensure_env_loaded
from langchain.chat_models import init_chat_model
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import OpenAIEmbeddings

from utils.config_handler import model_conf
from utils.retry import call_with_retry

def _network_options() -> dict:
    """超时与重试参数，统一从 model.yml 读（读不到就用安全默认值）"""
    return {
        "request_timeout": model_conf.get("request_timeout", 120),
        "max_retries": model_conf.get("max_retries", 3),
    }


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        pass


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        return init_chat_model(
            model="openai:" + model_conf["chat_model_name"],
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=os.getenv("DASHSCOPE_BASE_URL"),
            **_network_options(),
        )


class EmbeddingsFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        inner = OpenAIEmbeddings(
            model=model_conf["embedding_model_name"],
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=os.getenv("DASHSCOPE_BASE_URL"),
            request_timeout=model_conf.get("request_timeout", 120),
            # 重试统一交给下面那层做，避免两层各退避一次、把等待时间叠成平方
            max_retries=0,
            # DashScope 的 OpenAI 兼容端点必须关掉这个，否则会按 token 长度预处理；
            # chunk_size 也要压小，否则一批请求会因为超出单次上限而失败。
            check_embedding_ctx_length=False,
            chunk_size=10,
        )
        return RetryingEmbeddings(
            inner,
            retries=model_conf.get("max_retries", 3),
            backoff=model_conf.get("retry_backoff", 2),
        )


class RetryingEmbeddings(Embeddings):
    """给 embedding 套一层按**消息内容**分类的退避重试。

    为什么不让 SDK 自己重试：SDK 只按 HTTP 状态码判断，而 DashScope 把限流
    报成 **400**（`"type": "ServiceUnavailable"`）—— 不在它的重试范围里。
    建库路径尤其怕这个：全量重建是"先删旧库再逐文件写"，
    中途一次限流就会让整次重建失败并留下半成品。

    代价说清楚：重试会把**整批**文本重发一次（批内分批由内层 chunk_size 负责），
    而不是只补失败的那几条。对本项目的数据量（几百块）可以接受。
    """

    def __init__(self, inner: Embeddings, retries: int, backoff: float):
        self._inner = inner
        self._retries = retries
        self._backoff = backoff

    def embed_documents(self, texts, **kwargs):
        return call_with_retry(
            lambda: self._inner.embed_documents(texts, **kwargs),
            what=f"embed_documents（{len(texts)} 条）",
            retries=self._retries,
            backoff=self._backoff,
        )

    def embed_query(self, text, **kwargs):
        return call_with_retry(
            lambda: self._inner.embed_query(text, **kwargs),
            what="embed_query",
            retries=self._retries,
            backoff=self._backoff,
        )


_chat_model: BaseChatModel | None = None
_embed_model: Embeddings | None = None
# 并行工具调用（提示词允许）会让两个 get_xxx 同时进入首次构造；
# 与 agent_tools.get_rag_service 同型风险，同用双检锁。
_MODEL_LOCK = threading.RLock()
_chat_name = None
_embed_name = None


def reset_models():
    global _chat_model,_embed_model,_chat_name,_embed_name
    with _MODEL_LOCK:
        _chat_model=_embed_model=None
        _chat_name=_embed_name=None


def get_chat_model() -> BaseChatModel:
    global _chat_model,_chat_name
    with _MODEL_LOCK:
        name=model_conf['chat_model_name']
        if _chat_model is None or _chat_name!=name:
            _ensure_env_loaded()
            _chat_model=ChatModelFactory().generator()
            _chat_name=name
        return _chat_model


def get_embed_model() -> Embeddings:
    global _embed_model,_embed_name
    with _MODEL_LOCK:
        name=model_conf['embedding_model_name']
        if _embed_model is None or _embed_name!=name:
            _ensure_env_loaded()
            _embed_model=EmbeddingsFactory().generator()
            _embed_name=name
        return _embed_model
