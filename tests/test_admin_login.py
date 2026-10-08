import os
import sqlite3
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, PropertyMock
from langgraph.checkpoint.sqlite import SqliteSaver
from agent.react_agent import ReactAgent
from tests.test_customer_features import PlainModel


class AdminLoginTests(unittest.TestCase):
    def test_sidebar_login_wrong_password_success_and_logout(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, sqlite3.connect(':memory:',check_same_thread=False) as conn, patch.dict(os.environ,{'ADMIN_PASSWORD':'synthetic-test'}), patch('utils.admin_auth.get_abs_path',side_effect=lambda s:str(Path(tmp)/s)):
            with patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
                agent=ReactAgent(SqliteSaver(conn))
            with patch('utils.env.ensure_env_loaded'),patch('utils.app_resources.get_agent',return_value=agent),patch('utils.app_resources.get_vector_service'),patch('utils.service_ui.feedback_rows',return_value=[]):
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'))
                app.session_state['authed']=True;app.run(timeout=20)
                self.assertEqual(len(app.exception),0)
                self.assertTrue(any(t.label=='管理员密码' for t in app.sidebar.text_input))
                self.assertFalse(any(t.label=='资料适用品牌/型号' for t in app.text_input))
                app.text_input(key='admin_login_password').set_value('wrong')
                next(b for b in app.button if b.label=='登录管理员').click().run(timeout=20)
                self.assertTrue(any('管理员密码不正确' in e.value for e in app.error))
                app.text_input(key='admin_login_password').set_value('synthetic-test')
                next(b for b in app.button if b.label=='登录管理员').click().run(timeout=20)
                self.assertEqual(len(app.exception),0)
                self.assertTrue(any(t.value=='管理概览' for t in app.title))
                self.assertEqual(len(app.chat_input),0)
                captions=[c.value for c in app.sidebar.caption]
                self.assertTrue(all(label in captions for label in ['嵌入模型','聊天模型','重排模型']))
                self.assertFalse(any(s.label=='登录用户' for s in app.selectbox))
                with patch('utils.model_settings.apply_models',return_value='配置已测试') as apply:
                    next(t for t in app.text_input if t.label=='聊天模型名称').set_value('chat-test-new')
                    next(b for b in app.button if b.label=='保存并应用模型').click().run(timeout=20)
                    self.assertEqual(len(app.exception),0)
                    self.assertEqual(apply.call_args.args[1],'chat-test-new')
                    apply.reset_mock()
                    next(t for t in app.text_input if t.label=='嵌入模型名称').set_value('embed-test-new')
                    next(b for b in app.button if b.label=='保存并应用模型').click().run(timeout=20)
                    apply.assert_not_called()
                    self.assertTrue(any('重建确认' in e.value for e in app.error))
                token=app.session_state['_admin_token']
                import streamlit as st
                from utils.admin_auth import COOKIE_NAME
                with patch.object(type(st.context),'cookies',new_callable=PropertyMock,return_value={COOKIE_NAME:token}):
                    refreshed=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run(timeout=20)
                    self.assertTrue(any(t.value=='管理概览' for t in refreshed.title))
                    self.assertEqual(len(refreshed.chat_input),0)
                app.radio(key='admin_navigation').set_value('知识文件').run(timeout=20)
                self.assertTrue(any(t.label=='资料适用品牌/型号' for t in app.text_input))
                self.assertFalse(any(t.label=='管理员密码' for t in app.text_input))
                next(b for b in app.sidebar.button if b.label=='退出管理员').click().run(timeout=20)
                self.assertEqual(len(app.exception),0)
                self.assertFalse(any(t.label=='资料适用品牌/型号' for t in app.text_input))
                self.assertTrue(any(t.label=='管理员密码' for t in app.sidebar.text_input))


class AdminSessionTests(unittest.TestCase):
    def test_token_is_verified_revocable_and_expires(self):
        from utils.admin_auth import issue, valid, revoke
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'ADMIN_PASSWORD':'test'}):
            db=str(Path(tmp)/'sessions.sqlite')
            with self.assertRaises(PermissionError):issue('wrong',db)
            token=issue('test',db)
            self.assertTrue(valid(token,db))
            self.assertFalse(valid('forged',db))
            with patch.dict(os.environ,{'ADMIN_PASSWORD':'changed'}):self.assertFalse(valid(token,db))
            with patch('utils.admin_auth.time.time',return_value=time.time()+9*3600):self.assertFalse(valid(token,db))
            revoke(token,db);self.assertFalse(valid(token,db))
