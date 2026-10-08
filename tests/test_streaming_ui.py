import queue
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from langchain_core.messages import HumanMessage, ToolMessage, AIMessageChunk
from langgraph.checkpoint.sqlite import SqliteSaver
from agent.grounding import validate_answer
from agent.react_agent import ReactAgent, is_user_visible_token
from tests.test_customer_features import PlainModel


class StreamingUiTests(unittest.TestCase):
    def test_grounded_answer_emits_real_incremental_text(self):
        source={'ref':1,'file':'a.txt','snippet':'3-6个月更换'}
        messages=[HumanMessage(content='多久换'),ToolMessage(content='x',tool_call_id='t',artifact={'sources':[source]})]
        model=Mock()
        model.with_structured_output.return_value.stream.return_value=iter([
            {'covered':True}, {'covered':True,'answer':'3-6'}, {'covered':True,'answer':'3-6个月'},
            {'covered':True,'answer':'3-6个月[1]'}])
        with patch('model.factory.get_chat_model',return_value=model),patch('utils.progress.answer_delta') as emit:
            answer=validate_answer('',messages,{'stream_response':True})
        pieces=[c.args[0] for c in emit.call_args_list if c.args[0]]
        self.assertEqual(len(pieces),3)
        self.assertEqual(''.join(pieces),answer)
        self.assertFalse(is_user_visible_token(AIMessageChunk(content='{"covered":'),{'langgraph_node':'model','tags':['grounded_stream']}))

    def test_running_page_keeps_history_question_and_partial_answer(self):
        from streamlit.testing.v1 import AppTest
        with sqlite3.connect(':memory:',check_same_thread=False) as conn,patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
            agent=ReactAgent(SqliteSaver(conn));agent.answer('previous','user-1001','1001')
            job=SimpleNamespace(done=threading.Event(),cancel=threading.Event(),query='current question',
                history=agent.load_history('user-1001','1001'),thread_id='user-1001',user_id='1001',
                events=queue.Queue(),started=time.monotonic(),timings=[],error=None)
            job.events.put(('content','实时'));job.events.put(('content','回答'))
            with patch('utils.env.ensure_env_loaded'),patch('utils.app_resources.get_agent',return_value=agent),patch('utils.app_resources.get_vector_service'),patch('utils.customer_service.is_liked',return_value=False):
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'))
                app.session_state['authed']=True;app.session_state['answer_job']=job
                app.run(timeout=20)
                self.assertEqual(len(app.exception),0)
                texts=[m.value for m in app.markdown]
                for text in ('previous','reply:previous','current question','实时回答'):
                    self.assertIn(text,texts)
                self.assertGreaterEqual(len(app.chat_message),4)
                self.assertFalse(any('评价第' in e.label for e in app.expander))

    def test_like_under_answer_saves_without_form(self):
        from streamlit.testing.v1 import AppTest
        with sqlite3.connect(':memory:',check_same_thread=False) as conn,patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
            agent=ReactAgent(SqliteSaver(conn));agent.answer('hello','user-1001','1001')
            with patch('utils.env.ensure_env_loaded'),patch('utils.app_resources.get_agent',return_value=agent),patch('utils.app_resources.get_vector_service'),patch('utils.customer_service.is_liked',return_value=False),patch('utils.service_ui.save_feedback') as save:
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'))
                app.session_state['authed']=True;app.run(timeout=20)
                self.assertTrue(any(b.label=='👍' for b in app.chat_message[1].button))
                next(b for b in app.button if b.label=='👍').click().run(timeout=20)
                self.assertEqual(len(app.exception),0)
                save.assert_called_once()
                self.assertEqual(save.call_args.args[5],'有帮助')
                self.assertTrue(any(b.label=='👍 已点赞' for b in app.button))
                self.assertFalse(any(r.label=='这条回答有帮助吗？' for r in app.radio))
