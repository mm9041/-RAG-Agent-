"""``python -m server`` 启动入口。"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="智扫通 · FastAPI 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    # workers 必须为 1：SqliteSaver、向量库连接、cancel 注册表都是进程内单例，
    # 多 worker 会导致会话历史写入竞争、停止信号打不到正确的进程。
    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
