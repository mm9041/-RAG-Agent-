"""用户端门禁：登录、登出、切换演示身份。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from server import security
from server.deps import current_user, optional_user
from server.schemas import LoginRequest, SwitchUserRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _set_user_cookie(response: Response, request: Request, user_id: str) -> None:
    response.set_cookie(
        security.USER_COOKIE,
        security.issue_user_token(user_id),
        max_age=security.COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
        path="/",
    )


@router.get("/me")
def me(request: Request, user_id: str | None = Depends(optional_user)):
    """前端首屏调用：是否需要登录、当前是谁、可选的演示用户。"""
    demo = security.demo_mode()
    effective = user_id or (security.DEMO_USERS[0] if demo else None)
    return {
        "authenticated": bool(user_id) or demo,
        "user_id": effective,
        "demo_mode": demo,
        "password_required": bool(security.app_password()),
        "users": security.DEMO_USERS,
        "admin_enabled": bool(security.admin_password()),
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response):
    expected = security.app_password()
    if not expected:
        # 演示模式：没有密码可校验，直接给出默认身份
        _set_user_cookie(response, request, security.DEMO_USERS[0])
        return {"ok": True, "user_id": security.DEMO_USERS[0], "demo_mode": True}
    if not secrets.compare_digest(payload.password, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="密码不正确")
    _set_user_cookie(response, request, security.DEMO_USERS[0])
    return {"ok": True, "user_id": security.DEMO_USERS[0], "demo_mode": False}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(security.USER_COOKIE, path="/")
    return {"ok": True}


@router.post("/switch")
def switch_user(payload: SwitchUserRequest, request: Request, response: Response,
                _: str = Depends(current_user)):
    """切换演示身份。身份范围限定在 DEMO_USERS 内，由服务端重新签发。"""
    if payload.user_id not in security.DEMO_USERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="未知的演示用户")
    _set_user_cookie(response, request, payload.user_id)
    return {"ok": True, "user_id": payload.user_id}
