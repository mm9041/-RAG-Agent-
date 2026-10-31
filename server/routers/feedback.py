"""点赞反馈。

``answer_id`` 的算法必须与旧版 ``utils/service_ui.py`` 的 like_answer 完全一致
（``sha256(f"{thread_id}:{index}:{content}")[:16]``），否则迁移后用户此前
点过的赞会全部显示成未点赞。前端按历史数组的下标算 index。
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from server.deps import current_user
from server.schemas import LikeRequest
from utils.customer_service import is_liked, save_feedback
from utils.path_tool import get_abs_path

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


def answer_key(thread_id: str, index: int, content: str) -> str:
    """与 Streamlit 版保持一致，勿改。"""
    return hashlib.sha256(f"{thread_id}:{index}:{content}".encode()).hexdigest()[:16]


def _kb_version() -> str:
    from utils.config_handler import chroma_conf

    meta = Path(get_abs_path(chroma_conf["persist_directory"])) / "kb_meta.json"
    try:
        return hashlib.sha256(meta.read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def liked_states(user_id: str, thread_id: str, messages: list[dict],
                 start: int = 0, end: int | None = None) -> dict[int, bool]:
    """返回 ``{全量下标: 是否已点赞}``。

    分页时只算 ``[start, end)`` 区间，但下标始终按**完整历史**计——
    点赞 key 里含下标，用页内下标会导致翻页后点赞错位。
    """
    stop = len(messages) if end is None else min(end, len(messages))
    states: dict[int, bool] = {}
    for index in range(start, stop):
        message = messages[index]
        if message.get("role") != "assistant" or message.get("incomplete"):
            states[index] = False
            continue
        key = answer_key(thread_id, index, message.get("content", ""))
        try:
            states[index] = bool(is_liked(user_id, thread_id, key))
        except Exception:  # noqa: BLE001 - 反馈库损坏不该让历史读不出来
            states[index] = False
    return states


@router.post("/like")
async def like(payload: LikeRequest, user_id: str = Depends(current_user)):
    # 归属校验：不能让 A 给 B 的会话点赞
    from agent.react_agent import ReactAgent

    try:
        ReactAgent.require_owner(payload.thread_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    key = answer_key(payload.thread_id, payload.index, payload.answer)
    version = await asyncio.to_thread(_kb_version)
    await asyncio.to_thread(
        save_feedback,
        user_id,
        payload.thread_id,
        key,
        payload.question,
        payload.answer,
        "有帮助",
        "点赞",
        payload.sources,
        (payload.sources or [{}])[0].get("kb_version", version),
    )
    return {"ok": True, "liked": True}


@router.get("/liked")
async def liked(
    thread_id: str = Query(min_length=1, max_length=200),
    answer_id: str = Query(min_length=1, max_length=64),
    user_id: str = Depends(current_user),
):
    state = await asyncio.to_thread(is_liked, user_id, thread_id, answer_id)
    return {"liked": bool(state)}


# 注意：这里刻意不提供"列出全部反馈"的接口。
# feedback_rows() 返回所有人的问题与回答，放在用户端路由下等于任意用户都能读到
# 别人的对话内容。管理端的 /api/admin/feedback 有管理员鉴权，是唯一入口。
