"""FastAPI 应用入口。

启动顺序刻意设计成"先监听、后预热"：
uvicorn 立刻开始接收请求，Agent 与向量库在后台线程装配，前端轮询
``/api/system/status`` 显示进度。旧版 Streamlit 是整页阻塞在初始化上，
失败就白屏；这里失败也能进到界面看错误、点重试。
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from utils.env import ensure_env_loaded

# 必须在导入任何读配置的模块之前加载 .env
ensure_env_loaded()

from server.log_setup import setup_server_logging  # noqa: E402

# 早于业务模块的第一次日志输出，否则启动阶段的信息会被静默丢弃
setup_server_logging()

from server import runtime  # noqa: E402
from server.routers import admin, auth, chat, feedback, sessions, system  # noqa: E402

logger = logging.getLogger("server")
DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def _boot() -> None:
    try:
        state = runtime.bootstrap()
        logger.info("客服组件已就绪，用时 %s 秒", state.get("boot_seconds"))
    except Exception:  # noqa: BLE001 - 失败信息已存进 runtime，前端可读取并重试
        logger.exception("客服组件初始化失败，可在页面上点重新初始化")


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_boot, daemon=True, name="bootstrap").start()
    yield


app = FastAPI(title="智扫通智能客服", version="2.0.0", lifespan=lifespan)

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(feedback.router)
app.include_router(admin.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # noqa: ARG001
    """兜底成 JSON：默认的 HTML 错误页会让前端 fetch 解析失败。"""
    logger.exception("未捕获异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


if (DIST / "assets").is_dir():
    # 静态资源带 hash 文件名，可以放心长缓存
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """前端单页路由兜底。

    必须在所有 API 路由之后注册；``api`` 前缀不能落到这里，
    否则打错的 API 地址会拿到一份 index.html 而不是 404。

    ``full_path`` 来自 URL，必须校验解析后的真实路径仍在 dist 内，
    否则 ``../../.env`` 这类请求会把项目文件读出去。
    """
    if full_path.startswith("api/"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="接口不存在")
    if not (DIST / "index.html").is_file():
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "前端尚未构建。请在 web/ 目录执行 npm install && npm run build。"},
        )
    if full_path:
        root = DIST.resolve()
        candidate = (DIST / full_path).resolve()
        if root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(DIST / "index.html")
