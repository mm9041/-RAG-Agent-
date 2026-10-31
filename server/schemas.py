"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    query: str = Field(max_length=4000)
    thread_id: str = Field(min_length=1, max_length=200)
    # 由前端生成以便重试幂等；不传则服务端生成
    request_id: str | None = Field(default=None, max_length=64)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("问题不能为空")
        return text


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


class SwitchUserRequest(BaseModel):
    user_id: str = Field(max_length=16)


class NewSessionRequest(BaseModel):
    pass


class ClearOthersRequest(BaseModel):
    current: str = Field(min_length=1, max_length=200)


class CancelRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)


class LikeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    # 该助手消息在历史数组中的下标；answer_id 由服务端按旧算法推导，
    # 前端不参与哈希，避免依赖 crypto.subtle 的可用性
    index: int = Field(ge=0)
    question: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=32000)
    sources: list[dict] = Field(default_factory=list)


class ModelsRequest(BaseModel):
    chat: str = Field(max_length=200)
    embedding: str = Field(max_length=200)
    rerank: str = Field(max_length=200)
    confirm_rebuild: bool = False


class PublishMeta(BaseModel):
    model: str = Field(default="", max_length=200)
    scope: str = Field(default="", max_length=500)
    replace: bool = False
