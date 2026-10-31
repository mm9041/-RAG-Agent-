"""管理员令牌：签发、校验、撤销、过期、改密码失效。

原文件里的 AppTest 用例随 Streamlit 前端一起移除；鉴权契约本身
（utils/admin_auth.py 的 issue/valid/revoke）仍由 FastAPI 后端使用，
所以这层测试必须留着。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class AdminSessionTests(unittest.TestCase):
    def test_token_is_verified_revocable_and_expires(self):
        from utils.admin_auth import issue, valid, revoke

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ADMIN_PASSWORD": "test"}):
            db = str(Path(tmp) / "sessions.sqlite")
            with self.assertRaises(PermissionError):
                issue("wrong", db)
            token = issue("test", db)
            self.assertTrue(valid(token, db))
            self.assertFalse(valid("forged", db))
            with patch.dict(os.environ, {"ADMIN_PASSWORD": "changed"}):
                self.assertFalse(valid(token, db))
            with patch("utils.admin_auth.time.time", return_value=time.time() + 9 * 3600):
                self.assertFalse(valid(token, db))
            revoke(token, db)
            self.assertFalse(valid(token, db))
