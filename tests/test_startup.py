import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class StartupTests(unittest.TestCase):
    def test_fresh_login_does_not_import_backend(self):
        code = """
import os, sys, json, time
start=time.perf_counter()
os.environ['APP_PASSWORD']='synthetic-login-test'
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('app.py').run(timeout=15)
print(json.dumps({'seconds':time.perf_counter()-start,
 'errors':len(app.exception), 'login':any(x.label=='访问密码' for x in app.text_input),
 'heavy':[m for m in ['model.factory','agent.react_agent','rag.vector_store','chromadb','langchain'] if m in sys.modules]}))
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, encoding="utf-8", timeout=40,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['errors'], 0)
        self.assertTrue(data['login'])
        self.assertEqual(data['heavy'], [])

    def test_resources_shared_but_kb_validation_is_not_cached(self):
        from utils.app_resources import get_agent, get_vector_service
        get_agent.clear(); get_vector_service.clear()
        try:
            with patch('agent.react_agent.ReactAgent') as agent_cls, patch('rag.vector_store.VectorStoreService') as store_cls:
                self.assertIs(get_agent(), get_agent())
                first = get_vector_service(); second = get_vector_service()
                self.assertIs(first, second)
                first.load_document(); second.load_document()
                agent_cls.assert_called_once_with()
                store_cls.assert_called_once_with()
                self.assertEqual(first.load_document.call_count, 2)
        finally:
            get_agent.clear(); get_vector_service.clear()

    def test_failed_initialization_can_retry(self):
        from utils.app_resources import get_agent
        get_agent.clear()
        try:
            expected = Mock()
            with patch('agent.react_agent.ReactAgent', side_effect=[RuntimeError('failed'), expected]):
                with self.assertRaises(RuntimeError):
                    get_agent()
                self.assertIs(get_agent(), expected)
        finally:
            get_agent.clear()
