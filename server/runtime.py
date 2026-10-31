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
    # 失败/停止时的结束时刻：没有它，status() 会在失败后让 boot_seconds
    # 一直增长，界面看起来像"还在初始化"，实际早已结束。
    "finished_at": None,
    "stage": "",
}


def _update(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def bootstrap() -> dict:
    """加载向量库与 Agent。可重复调用，成功后直接复用。

    同步实现：调用方（lifespan / 重试接口）负责用 to_thread 包一层。

    **非阻塞抢锁**：已有初始化在进行时立即返回当前进度，而不是让第二个
    调用方排在建库后面等几分钟——重试按钮等不到响应只会超时。
    """
    if not _boot_lock.acquire(blocking=False):
        return status()
    try:
        if _read()["ready"]:
            return status()
        _update(
            booting=True, error=None, started_at=time.time(),
            ready_at=None, finished_at=None, stage="加载配置",
        )

        try:
            from utils.model_settings import configuration_guard, refresh_model_settings
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
            _update(
                booting=False, error=f"{type(exc).__name__}: {exc}",
                stage="初始化失败", finished_at=time.time(),
            )
            raise
        _update(
            vector=vector,
            agent=agent,
            kb=kb,
            ready=True,
            booting=False,
            ready_at=time.time(),
            finished_at=time.time(),
            stage="就绪",
        )
        return status()
    finally:
        _boot_lock.release()


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
    started = state["started_at"]
    # 结束时刻优先取 ready_at，其次 finished_at（失败/停止），都没有才按"仍在跑"算
    finished = state["ready_at"] or state["finished_at"]
    return {
        "ready": state["ready"],
        "booting": state["booting"],
        "stage": state["stage"],
        "error": state["error"],
        "boot_seconds": round((finished or time.time()) - started, 2) if started else None,
        "kb": state["kb"],
    }


def shutdown() -> None:
    """释放共享引用，由 lifespan 在进程退出时调用。

    只清引用、不复位 `kb`（管理页仍想看到最后一次建库摘要），
    也不关闭 SqliteSaver 的连接：它是进程级单例，句柄交给进程退出回收。
    """
    with _state_lock:
        _state.update(
            agent=None,
            vector=None,
            ready=False,
            booting=False,
            stage="已停止",
            finished_at=time.time(),
        )
    logger.info("客服组件已卸载")
