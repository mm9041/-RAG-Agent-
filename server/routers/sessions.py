"""会话管理：列表、新建、历史、删除。

注意所有调用都过 ``asyncio.to_thread``：list_threads 要逐个反序列化
检查点，load_history 要读 sqlite，都是同步阻塞操作。直接在 async 函数里
调用会卡住事件循环——正在流的问答会跟着一起卡顿。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from agent.react_agent import ReactAgent
from server import runtime
from server.deps import current_user
from server.routers.feedback import liked_states
from server.schemas import ClearOthersRequest

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _agent():
    try:
        return runtime.get_agent()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("")
async def list_sessions(user_id: str = Depends(current_user)):
    agent = _agent()
    rows = await asyncio.to_thread(agent.list_threads, user_id)
    return {"sessions": rows, "default": ReactAgent.thread_id_for(user_id)}


@router.post("")
def new_session(user_id: str = Depends(current_user)):
    """新建会话。不写库，只是生成一个 ID —— 与旧版行为一致：
    空会话在发出第一条消息后才会出现在历史列表里。"""
    return {"thread_id": ReactAgent.new_thread_id(user_id)}


@router.get("/{thread_id}/history")
async def history(
    thread_id: str,
    user_id: str = Depends(current_user),
    limit: int = Query(default=60, ge=1, le=200),
    before: int | None = Query(default=None, ge=0),
):
    """读取会话历史，支持向上翻页。

    只返回最近 ``limit`` 条；``before`` 传上一页的 ``start`` 即可拿到更早的一段。
    ``start`` 必须一并返回：点赞的 answer_id 依赖消息在**完整历史**里的下标，
    前端不能用页内下标，否则翻页后点赞会写到别的消息上。
    """
    agent = _agent()
    try:
        messages = await asyncio.to_thread(agent.load_history, thread_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    total = len(messages)
    end = total if before is None else min(before, total)
    start = max(0, end - limit)
    page = messages[start:end]

    states = await asyncio.to_thread(liked_states, user_id, thread_id, messages, start, end)
    for offset, message in enumerate(page):
        message["liked"] = states.get(start + offset, False)

    return {
        "thread_id": thread_id,
        "messages": page,
        "start": start,
        "total": total,
        "has_more": start > 0,
    }


@router.post("/{thread_id}/truncate")
async def truncate(thread_id: str, user_id: str = Depends(current_user)):
    """回退最后一轮，供前端"重新生成"使用。"""
    agent = _agent()
    try:
        removed = await asyncio.to_thread(agent.truncate_last_turn, thread_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return {"ok": True, "removed": removed}


@router.delete("/{thread_id}")
async def delete_session(thread_id: str, user_id: str = Depends(current_user)):
    agent = _agent()
    try:
        await asyncio.to_thread(agent.delete_thread, thread_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/clear-others")
async def clear_others(payload: ClearOthersRequest, user_id: str = Depends(current_user)):
    agent = _agent()
    try:
        removed = await asyncio.to_thread(agent.clear_other_threads, user_id, payload.current)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return {"ok": True, "removed": removed}
