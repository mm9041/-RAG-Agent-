"""SSE 传输层。

两个必须解决的约束：

1. ``agent.stream_events`` 是**同步阻塞**生成器（模型调用在等网络）。在 async
   生成器里直接迭代会占住事件循环，所有用户一起卡。所以放进专用线程执行，
   事件经 ``asyncio.Queue`` 送回循环 —— 一个会话一个线程，互不影响。

2. 客户端断开要能真正停下生成。SSE 没有取消帧，因此轮询
   ``request.is_disconnected()``，检测到断连就 set ``threading.Event``，
   交给项目已有的 cancel 链路（utils/jobs.py 时代就有的 agent/control.py 注册表）
   去打断模型调用与退避等待。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Iterable

logger = logging.getLogger("server.sse")


def format_event(event: str, data: Any) -> str:
    """把一条事件编码成 SSE 帧。

    统一包成 JSON：``content`` 是模型增量文本，可能含换行；裸换行会破坏
    SSE 的 ``data:`` 行结构，JSON 转义后永远是单行，前端解析也统一。
    """
    payload = json.dumps({"d": data}, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 让 nginx 之类的反向代理不要攒包，否则流式会变成一次性吐出
    "X-Accel-Buffering": "no",
}


class EventBridge:
    """把同步生成器桥接成异步事件流。

    用法::

        bridge = EventBridge(lambda: agent.stream_events(...), cancel_event=ev).start()
        async for event, data in bridge.events():
            ...
        bridge.close()
    """

    def __init__(
        self,
        make_generator: Callable[[], Iterable[tuple[str, Any]]],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._make = make_generator
        self._cancel = cancel_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> "EventBridge":
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sse-bridge")
        self._thread.start()
        return self

    def _push(self, item: tuple[str, Any]) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # 事件循环已关闭：客户端断开、应用正在下线，丢弃即可
            pass

    def _run(self) -> None:
        try:
            for event, data in self._make():
                if self._closed:
                    break
                self._push(("event", (event, data)))
        except Exception as exc:  # noqa: BLE001 - 原样透传给前端
            logger.exception("问答流执行失败")
            self._push(("error", exc))
        finally:
            self._push(("end", None))

    async def events(self):
        assert self._queue is not None, "必须先调用 start()"
        while True:
            kind, payload = await self._queue.get()
            if kind == "end":
                return
            if kind == "error":
                raise payload
            yield payload

    def close(self) -> None:
        """关闭桥接；若配置了 cancel_event，顺带请求停止生成。"""
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()


async def watch_disconnect(request, cancel_event: threading.Event, interval: float = 0.5) -> None:
    """客户端断开时置位 cancel_event。

    单独一个 task 跑，因为它要在"生成器正阻塞于模型调用"时也能生效。
    """
    try:
        while not cancel_event.is_set():
            if await request.is_disconnected():
                cancel_event.set()
                return
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 断连检测失败不应影响主流程
        logger.debug("断连检测异常", exc_info=True)
