"""管理员会话令牌：签发、校验、撤销。

只负责令牌本身，不碰任何 UI。Cookie 的读写由 FastAPI 层
（``server/routers/admin.py``）负责，这里是纯业务：
浏览器只保存随机令牌，服务端 sqlite 只存它的 sha256 摘要。

失效路径：8 小时绝对过期、修改 ADMIN_PASSWORD（指纹不匹配）、显式 revoke。
"""
import hashlib
import os
import secrets
import sqlite3
import time

from utils.path_tool import get_abs_path

SESSION_SECONDS = 8 * 60 * 60


def _fingerprint():
    return hashlib.sha256(os.getenv('ADMIN_PASSWORD', '').encode()).hexdigest()


def issue(password, db_path=None):
    from utils.knowledge_admin import authorized
    if not authorized(password):
        raise PermissionError('管理员密码不正确')
    token = secrets.token_urlsafe(32)
    with sqlite3.connect(db_path or get_abs_path('admin_sessions.sqlite')) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL, fingerprint TEXT)')
        conn.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + SESSION_SECONDS, _fingerprint()))
    return token


def valid(token, db_path=None):
    if not isinstance(token, str) or len(token) > 128 or not token or not os.getenv('ADMIN_PASSWORD'):
        return False
    path = db_path or get_abs_path('admin_sessions.sqlite')
    if not os.path.exists(path):
        return False
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone():
            return False
        row = conn.execute('SELECT expires,fingerprint FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return bool(row and row[0] > time.time() and secrets.compare_digest(row[1], _fingerprint()))


def revoke(token, db_path=None):
    path = db_path or get_abs_path('admin_sessions.sqlite')
    if not token or not os.path.exists(path):
        return
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))
