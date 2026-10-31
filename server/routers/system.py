"""系统状态：就绪检查、初始化重试。

旧版 Streamlit 若首屏初始化失败只能整页白屏；这里把状态暴露成接口，
前端可以显示具体错误并给"重新初始化"按钮。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from server import runtime

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
def status():
    return runtime.status()


@router.get("/info")
def info():
    """用户端侧栏要展示的知识库信息。不涉及敏感配置，公开即可。"""
    import json
    from pathlib import Path

    from utils.config_handler import chroma_conf, model_conf
    from utils.path_tool import get_abs_path

    meta_path = Path(get_abs_path(chroma_conf["persist_directory"])) / "kb_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except (ValueError, OSError):
        meta = {}
    return {
        "indexed_files": len(meta.get("files", {})),
        "chunks": sum(int(v.get("chunks", 0)) for v in meta.get("files", {}).values()),
        "models": {
            "chat": model_conf["chat_model_name"],
            "embedding": model_conf["embedding_model_name"],
            "rerank": chroma_conf.get("rerank_model", ""),
            "rerank_enabled": bool(chroma_conf.get("rerank_enabled", False)),
        },
    }


@router.post("/retry")
async def retry():
    state = runtime.status()
    # 已就绪或已有初始化在跑：立即返回当前进度，不再排一次队。
    # bootstrap() 内部也是非阻塞抢锁，这里提前判断只是让响应语义更清楚。
    if state["ready"] or state["booting"]:
        return {"ok": state["ready"], **state}
    try:
        # bootstrap 会建库并装配 Agent，必须放到线程里，否则卡死事件循环
        state = await asyncio.to_thread(runtime.bootstrap)
    except Exception as exc:  # noqa: BLE001 - 错误本身就是要给前端看的结果
        return {"ok": False, **runtime.status(), "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(state["ready"]), **state}
