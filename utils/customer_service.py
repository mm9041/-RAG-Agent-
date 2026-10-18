import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from utils.path_tool import get_abs_path
from utils.redact import redact


def save_feedback(user_id, thread_id, answer_id, question, answer, rating, reason, sources, kb_version, db_path=None):
    from agent.react_agent import ReactAgent
    ReactAgent.require_owner(thread_id, user_id)
    if rating not in ("有帮助", "没帮助"):
        raise ValueError("无效反馈")
    with sqlite3.connect(db_path or get_abs_path("feedback.sqlite")) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, user_id TEXT, thread_id TEXT, payload TEXT, updated TEXT)")
        key = hashlib.sha256(f"{user_id}:{thread_id}:{answer_id}".encode()).hexdigest()
        # 反馈里存的是用户原话，手机号 / 地址 / 订单号的出现频率远高于日志。
        # 脱敏只在**入库前**做：界面上的对话历史、被点赞的那条回答仍是原文，
        # 不受影响；拿到反馈数据的运营侧看到的是掩码版本。
        # reason 也一起处理 —— 用户常在里面写"打我电话 138…"。
        payload = json.dumps(dict(question=redact(question), answer=redact(answer),
                                  rating=rating, reason=redact(reason),
                                  sources=sources, kb_version=kb_version), ensure_ascii=False)
        conn.execute("INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?, ?)",
                     (key, user_id, thread_id, payload, datetime.now(timezone.utc).isoformat()))


def feedback_rows(db_path=None):
    with sqlite3.connect(db_path or get_abs_path("feedback.sqlite")) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='feedback'").fetchone():
            return []
        return [dict(user_id=u, thread_id=t, **json.loads(payload), updated=updated)
                for u,t,payload,updated in conn.execute("SELECT user_id,thread_id,payload,updated FROM feedback ORDER BY updated DESC")]


def is_liked(user_id, thread_id, answer_id, db_path=None):
    from pathlib import Path
    path = db_path or get_abs_path("feedback.sqlite")
    if not Path(path).exists():
        return False
    key = hashlib.sha256(f"{user_id}:{thread_id}:{answer_id}".encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='feedback'").fetchone():
            return False
        row = conn.execute("SELECT payload FROM feedback WHERE id=? AND user_id=? AND thread_id=?",(key,user_id,thread_id)).fetchone()
    return bool(row and json.loads(row[0]).get("rating") == "有帮助")
