"""聊天接口：以 SSE 推送问答事件。

对照旧实现：Streamlit 版要 AnswerJob 起后台线程 + queue.Queue + 0.2 秒轮询
fragment 才能拿到增量。这里 HTTP 连接本身就是流，那三样全部不需要了。

事件协议与 ``ReactAgent.stream_events`` 一一对应，未做任何改写：
    phase / content / tool_start / tool_end / sources / final
额外两条属于传输层：
    start  首帧，带 request_id
    error  流内异常（HTTP 状态已经是 200，错误只能走这里）
"""
from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from agent.react_agent import ReactAgent
from server import runtime
from server.deps import current_user
from server.schemas import CancelRequest, ChatRequest
from server.sse import SSE_HEADERS, EventBridge, format_event, watch_disconnect

router = APIRouter(prefix="/api", tags=["chat"])

# request_id -> cancel_event。断连检测也能停下生成，但显式取消更可靠，
# 而且能区分"用户点了停止"与"网络断了"。
_active: dict[str, threading.Event] = {}
_active_lock = threading.Lock()


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request, user_id: str = Depends(current_user)):
    try:
        agent = runtime.get_agent()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    # 越权检查放在流开始前，这样能返回真正的 403；
    # 若留到生成器里，就只会变成流里一条 error 事件。
    try:
        ReactAgent.require_owner(payload.thread_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    request_id = payload.request_id or uuid4().hex
    cancel = threading.Event()
    with _active_lock:
        _active[request_id] = cancel

    # lambda 必须延迟到线程里执行：同步生成器在创建时不跑代码，
    # 但把 agent 调用写在生成器表达式外会提前占用调用方线程。
    bridge = EventBridge(
        lambda: agent.stream_events(
            payload.query, payload.thread_id, user_id,
            request_id=request_id, cancel_event=cancel,
        ),
        cancel_event=cancel,
    ).start()

    async def stream():
        watcher = asyncio.create_task(watch_disconnect(request, cancel))
        try:
            yield format_event("start", {"request_id": request_id})
            async for event, data in bridge.events():
                yield format_event(event, data)
        except asyncio.CancelledError:
            # 客户端断开时 Starlette 会取消这个生成器
            cancel.set()
            raise
        except Exception as exc:  # noqa: BLE001 - 错误要送到前端展示
            yield format_event("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            watcher.cancel()
            bridge.close()
            with _active_lock:
                _active.pop(request_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/cancel")
def cancel_chat(payload: CancelRequest):
    """显式停止生成。找不到说明这一轮已经结束，按幂等处理。"""
    with _active_lock:
        event = _active.get(payload.request_id)
    if event is None:
        return {"ok": False, "reason": "该请求已结束或不存在"}
    event.set()
    return {"ok": True}
