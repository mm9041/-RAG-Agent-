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

from fastapi import FastAPI, HTTPException, Request, Response, status
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
        if state.get("ready"):
            logger.info("客服组件已就绪，用时 %s 秒", state.get("boot_seconds"))
        else:
            # 非阻塞抢锁时可能已有其他初始化在跑，这里不算失败
            logger.info("客服组件已在初始化中，跳过本次预热")
    except Exception:  # noqa: BLE001 - 失败信息已存进 runtime，前端可读取并重试
        logger.exception("客服组件初始化失败，可在页面上点重新初始化")


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_boot, daemon=True, name="bootstrap").start()
    try:
        yield
    finally:
        # 只清共享引用；初始化线程是 daemon，进程退出时由系统回收
        runtime.shutdown()


app = FastAPI(title="智扫通智能客服", version="2.0.0", lifespan=lifespan)

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(feedback.router)
app.include_router(admin.router)


@app.api_route(
    "/api",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/api/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def api_not_found(rest: str = ""):  # noqa: ARG001
    """未命中的 API 一律返回 JSON 404。

    必须注册在真实路由之后：``{rest:path}`` 会匹配整个 /api 前缀；
    也必须在 SPA 兜底之前，否则 GET 会拿到 index.html、
    POST 会因为路径匹配而方法不匹配得到 405，前端都很难定位。
    ``/api`` 额外单列一条：``:path`` 转换器要求前缀带斜杠，不会匹配裸路径。
    """
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="接口不存在")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # noqa: ARG001
    """兜底成 JSON：默认的 HTML 错误页会让前端 fetch 解析失败。"""
    logger.exception("未捕获异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """补上 StaticFiles 不会自动发送的缓存策略。

    - ``/assets/`` 文件名带内容 hash，可以 immutable 长缓存；
    - HTML 入口必须每次回源校验，否则新构建的 hash 名引用不到；
    - API 一律不缓存（SSE 自己的头优先，setdefault 不会覆盖）。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/") and response.status_code in (200, 304):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-cache")
    elif path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


if (DIST / "assets").is_dir():
    # 静态资源带 hash 文件名，可以放心长缓存（Cache-Control 见 cache_headers）
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """没有图标文件时返回 204，避免 SPA 兜底把 index.html 当成图标返回。"""
    icon = DIST / "favicon.ico"
    if icon.is_file():
        return FileResponse(icon)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """前端单页路由兜底。

    必须在所有 API 路由、api_not_found 和 /assets 挂载之后注册；
    未知的 ``api`` 前缀由 api_not_found 处理，不会落到这里。

    ``full_path`` 来自 URL，必须校验解析后的真实路径仍在 dist 内，
    否则 ``../../.env`` 这类请求会把项目文件读出去。
    """
    if not (DIST / "index.html").is_file():
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "前端尚未构建。请在 web/ 目录执行 npm install && npm run build。"},
        )
    if full_path:
        root = DIST.resolve()
        candidate = (DIST / full_path).resolve()
        if root not in candidate.parents:
            # 越界路径不再伪装成 SPA（200），直接 404，便于日志识别扫描
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="路径不存在")
        if candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(DIST / "index.html")
