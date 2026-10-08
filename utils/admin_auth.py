"""管理员会话：浏览器仅保存随机令牌，服务端保存其摘要；刷新、进程重启可恢复。"""
import hashlib
import os
import secrets
import sqlite3
import time
from utils.path_tool import get_abs_path

COOKIE_NAME = 'robot_admin_' + hashlib.sha256(get_abs_path('.').encode()).hexdigest()[:12]
SESSION_SECONDS = 8 * 60 * 60


def _fingerprint():
    return hashlib.sha256(os.getenv('ADMIN_PASSWORD','').encode()).hexdigest()


def issue(password, db_path=None):
    from utils.knowledge_admin import authorized
    if not authorized(password):
        raise PermissionError('管理员密码不正确')
    token = secrets.token_urlsafe(32)
    with sqlite3.connect(db_path or get_abs_path('admin_sessions.sqlite')) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL, fingerprint TEXT)')
        conn.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),time.time()+SESSION_SECONDS,_fingerprint()))
    return token


def valid(token, db_path=None):
    if not isinstance(token,str) or len(token)>128 or not token or not os.getenv('ADMIN_PASSWORD'):
        return False
    path = db_path or get_abs_path('admin_sessions.sqlite')
    if not os.path.exists(path):
        return False
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone():
            return False
        row=conn.execute('SELECT expires,fingerprint FROM sessions WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return bool(row and row[0]>time.time() and secrets.compare_digest(row[1],_fingerprint()))


def revoke(token, db_path=None):
    path=db_path or get_abs_path('admin_sessions.sqlite')
    if not token or not os.path.exists(path):
        return
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM sessions WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),))


def restore():
    import streamlit as st
    token=st.session_state.get('_admin_token') or st.context.cookies.get(COOKIE_NAME,'')
    if valid(token):
        st.session_state['_admin_token']=token
        return True
    st.session_state.pop('_admin_token',None)
    return False


def logout():
    import streamlit as st
    revoke(st.session_state.pop('_admin_token',None))
    st.session_state.pop('_admin_session_password',None)


_cookie_component = None
_cookie_owner = None

def sync_cookie():
    import streamlit as st
    import streamlit.components.v2 as components
    from streamlit.runtime import get_instance
    global _cookie_component, _cookie_owner
    owner = get_instance()
    if _cookie_component is None or _cookie_owner is not owner:
        _cookie_owner = owner
        _cookie_component=components.component('admin_cookie',js="""
export default function(component) {
    const {data} = component;
    const secure = window.location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = data.name + '=' + encodeURIComponent(data.token) +
        '; Path=/; SameSite=Strict; Max-Age=' + (data.token ? data.age : 0) + secure;
}
""")
    _cookie_component(data={'name':COOKIE_NAME,'token':st.session_state.get('_admin_token',''),'age':SESSION_SECONDS},key='admin_cookie_sync')
