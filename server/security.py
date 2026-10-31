"""用户端门禁与会话身份。

两类凭据，刻意分开：

- **用户端**（``APP_PASSWORD``）：无状态签名 cookie，携带 user_id。签名密钥落盘，
  进程重启不掉登录 —— 旧 Streamlit 版放在 session_state 里，刷新即丢。
- **管理员**：沿用 ``utils/admin_auth.py`` 的 sqlite 令牌（8h 绝对过期、
  改密码即失效），这里只负责把它放进 HttpOnly cookie。安全设计一行没改。

为什么身份必须服务端签发：如果让前端直接传 user_id，任何人改个请求体就能
读别人的会话历史。所以 user_id 只从签名 cookie 里读，请求参数一律不采信。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from utils.path_tool import get_abs_path

# 演示用户：与旧版 app.py 的 DEMO_USERS 保持一致
DEMO_USERS = [f"10{index:02d}" for index in range(1, 11)]

USER_COOKIE = "robot_user"
ADMIN_COOKIE = "robot_admin"
COOKIE_MAX_AGE = 7 * 24 * 3600

_secret_cache: bytes | None = None


def app_password() -> str:
    return os.getenv("APP_PASSWORD", "")


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def demo_mode() -> bool:
    """未设置 APP_PASSWORD 时的本地演示模式，与旧版语义一致。"""
    return not app_password()


def _secret() -> bytes:
    """签名密钥持久化到项目根，避免每次重启把所有人踢下线。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    path = get_abs_path(".session_secret")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        if len(data) >= 32:
            _secret_cache = data
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    with open(path, "wb") as handle:
        handle.write(data)
    _secret_cache = data
    return data


def _sign(body: str) -> str:
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]


def issue_user_token(user_id: str) -> str:
    if user_id not in DEMO_USERS:
        raise ValueError("未知的演示用户")
    body = f"{user_id}.{int(time.time()) + COOKIE_MAX_AGE}"
    return f"{body}.{_sign(body)}"


def read_user_token(token: str | None) -> str | None:
    """返回可信 user_id；token 无效/过期返回 None。"""
    if not token or len(token) > 128:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_id, expires, signature = parts
    if not hmac.compare_digest(signature, _sign(f"{user_id}.{expires}")):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    return user_id if user_id in DEMO_USERS else None


def issue_admin_token(password: str) -> str:
    """签发管理员令牌。校验逻辑复用 knowledge_admin.authorized。"""
    from utils.admin_auth import issue

    return issue(password)


def admin_token_valid(token: str | None) -> bool:
    if not token:
        return False
    from utils.admin_auth import valid

    try:
        return bool(valid(token))
    except Exception:  # noqa: BLE001 - 数据库损坏等情况一律视为未登录
        return False


def revoke_admin_token(token: str | None) -> None:
    if not token:
        return
    from utils.admin_auth import revoke

    try:
        revoke(token)
    except Exception:  # noqa: BLE001
        pass
