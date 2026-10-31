"""进程级共享资源与启动预热。

在 lifespan 里一次性完成装配：服务开始监听时 Agent 与向量库已经就绪，
第一个访问者不用等依赖导入和建库，跨会话复用同一份资源。

**锁的粒度很关键**：初始化要跑几十秒到几分钟（建库、调 embedding），
如果状态读取和初始化共用一把锁，期间所有 /api/system/status 都会阻塞，
前端连"正在初始化"这个进度都看不到。所以拆成两把：
- ``_boot_lock`` 只串行化初始化本身，允许长持有；
- ``_state_lock`` 只保护字段读写，永远只持有微秒级。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("server.runtime")

_boot_lock = threading.Lock()
_state_lock = threading.Lock()

_state: dict[str, Any] = {
    "agent": None,
    "vector": None,
    "kb": None,
    "ready": False,
    "booting": False,
    "error": None,
    "started_at": None,
    "ready_at": None,
    "stage": "",
}


def _update(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def bootstrap() -> dict:
    """加载向量库与 Agent。可重复调用，成功后直接复用。

    同步实现：调用方（lifespan / 重试接口）负责用 to_thread 包一层。
    """
    with _boot_lock:
        if _read()["ready"]:
            return status()
        _update(booting=True, error=None, started_at=time.time(), stage="加载配置")

        from utils.model_settings import configuration_guard, refresh_model_settings

        try:
            from agent.react_agent import ReactAgent
            from rag.vector_store import VectorStoreService

            with configuration_guard():
                refresh_model_settings()
                _update(stage="检查知识库")
                logger.info("开始初始化向量库")
                vector = VectorStoreService()
                # load_document 按文件哈希决定直接加载 / 增量 / 全量重建
                kb = vector.load_document()
                logger.info("向量库就绪：%s", kb)
                _update(stage="装配对话服务")
                agent = ReactAgent()
        except Exception as exc:  # noqa: BLE001 - 原始错误要透给前端
            _update(booting=False, error=f"{type(exc).__name__}: {exc}", stage="初始化失败")
            raise
        _update(
            vector=vector,
            agent=agent,
            kb=kb,
            ready=True,
            booting=False,
            ready_at=time.time(),
            stage="就绪",
        )
        return status()


def _read() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def get_agent():
    """取出已装配的 Agent。未就绪时抛 RuntimeError，由路由层转成 503。"""
    state = _read()
    if not state["ready"] or state["agent"] is None:
        raise RuntimeError(state["error"] or "客服组件尚未初始化完成")
    return state["agent"]


def get_vector_service():
    state = _read()
    if state["vector"] is None:
        raise RuntimeError(state["error"] or "向量库尚未初始化完成")
    return state["vector"]


def status() -> dict:
    state = _read()
    started, ready_at = state["started_at"], state["ready_at"]
    return {
        "ready": state["ready"],
        "booting": state["booting"],
        "stage": state["stage"],
        "error": state["error"],
        "boot_seconds": round((ready_at or time.time()) - started, 2) if started else None,
        "kb": state["kb"],
    }
