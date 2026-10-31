"""每轮请求的进度、取消和时间预算，不依赖 Streamlit。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressContext:
    cancel_event: object | None = None
    deadline: float | None = None
    on_event: Callable | None = None


_progress_context = ContextVar("answer_progress_context", default=None)


def current_progress_context() -> ProgressContext | None:
    return _progress_context.get()


@contextmanager
def progress_context(*, cancel_event=None, deadline=None, on_event=None):
    """嵌套调用继承取消和进度出口，内层不能延长外层的时间预算。"""
    parent = current_progress_context()
    if parent is not None:
        cancel_event = parent.cancel_event if cancel_event is None else cancel_event
        on_event = parent.on_event if on_event is None else on_event
        if parent.deadline is not None:
            deadline = parent.deadline if deadline is None else min(deadline, parent.deadline)
    current = ProgressContext(cancel_event, deadline, on_event)
    token = _progress_context.set(current)
    try:
        yield current
    finally:
        _progress_context.reset(token)


def _emit(event, data, payload):
    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except (RuntimeError, ImportError):
        current = current_progress_context()
        if current is not None and current.on_event is not None:
            current.on_event(event, data)
    else:
        # 在图内只走 custom 流；同时通知回调会让 UI 收到重复 token。
        try:
            writer(payload)
        except RuntimeError:
            # 进度出口结束不能让已完成的模型/工具调用变成失败。
            pass


def phase(label):
    _emit("phase", label, {"phase": label})


def answer_delta(text):
    if not text:
        return
    _emit("content", text, {"answer_delta": text})
