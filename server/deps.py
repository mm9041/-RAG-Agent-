"""FastAPI 依赖：从 cookie 解析可信身份。

刻意不接受任何来自请求体的 user_id —— 身份只能由服务端签发的 cookie 决定，
否则改个请求体就能读别人的会话。
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from server import security


def _cookie(request: Request, name: str) -> str | None:
    return request.cookies.get(name)


def current_user(request: Request) -> str:
    """用户端身份。演示模式（未设 APP_PASSWORD）下回落到默认演示用户。"""
    token = _cookie(request, security.USER_COOKIE)
    user_id = security.read_user_token(token)
    if user_id:
        return user_id
    if security.demo_mode():
        # 演示模式没有真正的门禁，但仍由服务端决定用哪个身份
        return security.DEMO_USERS[0]
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="请先登录")


def optional_user(request: Request) -> str | None:
    token = _cookie(request, security.USER_COOKIE)
    return security.read_user_token(token)


def admin_token(request: Request) -> str:
    token = _cookie(request, security.ADMIN_COOKIE)
    if not security.admin_token_valid(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="需要管理员登录")
    return token  # type: ignore[return-value]
