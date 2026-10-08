from contextlib import closing
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langgraph.checkpoint.sqlite import SqliteSaver
from agent.react_agent import ReactAgent
from agent.grounding import wash_clarification, validate_answer, valid_citations
from tests.test_improvements import ScriptModel, LocalEmbeddings
from utils.config_handler import chroma_conf


class PlainModel(ScriptModel):
    def _generate(self, messages, **kwargs):
        time.sleep(.04)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='reply:'+str(messages[-1].content)))])


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:',check_same_thread=False)
        with patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
            self.agent=ReactAgent(SqliteSaver(self.conn))
    def tearDown(self): self.conn.close()

    def test_same_thread_preserves_both_turns(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda q:self.agent.answer(q,'user-1001','1001'),['A','B']))
        history=self.agent.load_history('user-1001','1001')
        self.assertEqual(len(history),4)
        self.assertEqual({m['content'] for m in history if m['role']=='user'},{'A','B'})
        self.assertEqual(set(results),{'reply:A','reply:B'})

    def test_request_id_is_idempotent(self):
        one=list(self.agent.stream_events('A','user-1001','1001',request_id='same'))
        two=list(self.agent.stream_events('A','user-1001','1001',request_id='same'))
        self.assertEqual([d for e,d in one if e=='final'],[d for e,d in two if e=='final'])
        self.assertEqual(len(self.agent.load_history('user-1001','1001')),2)

    def test_cancelled_queue_does_not_write(self):
        cancel=threading.Event();cancel.set()
        list(self.agent.stream_events('A','user-1001','1001',cancel_event=cancel))
        self.assertEqual(self.agent.load_history('user-1001','1001'),[])

    def test_unknown_filter_clarifies_without_model(self):
        result=self.agent.answer('HEPA滤网怎么清理？','user-1001','1001')
        self.assertIn('是否标注',result)
        self.assertNotIn('reply:',result)

    def test_interrupted_tool_call_can_continue(self):
        self.agent.agent.update_state(self.agent._config('user-1001'),{'messages':[
            HumanMessage(content='old'),AIMessage(content='',tool_calls=[{'name':'get_weather','args':{'city':'深圳'},'id':'pending'}])]})
        self.assertEqual(self.agent.answer('new','user-1001','1001'),'reply:new')
        state=self.agent.agent.get_state(self.agent._config('user-1001'))
        self.assertTrue(any(isinstance(m,ToolMessage) and m.tool_call_id=='pending' for m in state.values['messages']))


class GroundingTests(unittest.TestCase):
    def test_invalid_reference_rejected(self):
        self.assertTrue(valid_citations('内容[1]',[{'ref':1}]))
        self.assertFalse(valid_citations('内容[2]',[{'ref':1}]))
        self.assertFalse(valid_citations('内容[资料:bad]',[{'ref':1}]))

    def test_verifier_failure_returns_original_evidence(self):
        source={'ref':1,'file':'a','snippet':'原文只说3-6个月'}
        messages=[HumanMessage(content='多久换'),ToolMessage(content='x',tool_call_id='x',artifact={'sources':[source]})]
        model=Mock();model.with_structured_output.return_value.invoke.side_effect=RuntimeError('offline')
        with patch('model.factory.get_chat_model',return_value=model):
            result=validate_answer('每4-5个月换[7]',messages,{})
        self.assertIn('原文只说3-6个月',result)
        self.assertNotIn('4-5',result)
        self.assertNotIn('[7]',result)

    def test_uncovered_question_refuses_instead_of_dumping_chunks(self):
        source={'ref':1,'file':'a','snippet':'滤网维护原文'}
        messages=[HumanMessage(content='专利诉讼'),ToolMessage(content='x',tool_call_id='x',artifact={'sources':[source]})]
        model=Mock();model.with_structured_output.return_value.invoke.return_value=SimpleNamespace(covered=False,answer='无资料')
        with patch('model.factory.get_chat_model',return_value=model):
            answer=validate_answer('',messages,{})
        self.assertIn('未覆盖',answer)
        self.assertNotIn('滤网维护原文',answer)

    def test_followup_uses_topic_and_washability(self):
        messages=[HumanMessage(content='HEPA滤网多久换'),AIMessage(content='3-6个月'),HumanMessage(content='那怎么清理？')]
        self.assertIsNotNone(wash_clarification(messages,{}))
        self.assertIsNone(wash_clarification(messages,{'washable':'可水洗'}))
        self.assertIn('不要用水',wash_clarification(messages,{'washable':'不可水洗'}))


class FeedbackTests(unittest.TestCase):
    def test_update_feedback(self):
        from utils.customer_service import save_feedback, feedback_rows
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'feedback.sqlite')
            for rating in ['没帮助','有帮助']:
                save_feedback('1001','user-1001','answer','q','a',rating,'reason',[], 'v1',db)
            rows=feedback_rows(db)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['rating'],'有帮助')
            with self.assertRaises(PermissionError):
                save_feedback('1001','user-1002','answer','q','a','没帮助','r',[],'v1',db)



class KnowledgeTests(unittest.TestCase):
    def test_upload_preview_validation(self):
        from utils.knowledge_admin import preview, conflict_hints
        self.assertEqual(preview('a.txt','内容'.encode()),'内容')
        for name in ['../a.txt','a.exe','C:\\a.txt']:
            with self.assertRaises(ValueError): preview(name,b'hello')
        with self.assertRaises(ValueError):preview('a.txt',b'')
        self.assertTrue(conflict_hints('HEPA滤网用清水冲洗'))

    def test_publish_replace_delete_and_failure_rollback(self):
        from utils.knowledge_admin import publish
        from rag.vector_store import VectorStoreService
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, patch.dict(os.environ,{'ADMIN_PASSWORD':'test'}):
            root=Path(tmp);data=root/'data';data.mkdir()
            (data/'base.txt').write_text('base text',encoding='utf-8')
            with patch.dict(chroma_conf,{'data_path':str(data),'persist_directory':str(root/'db')}), patch(
                    'rag.vector_store.get_embed_model',return_value=LocalEmbeddings()), patch(
                    'utils.knowledge_admin.get_abs_path',side_effect=lambda s:str(root/s)):
                with self.assertRaises(PermissionError):publish('new.txt',b'new','wrong')
                publish('new.txt',b'new facts','test',model='M1',scope='manual')
                self.assertTrue((data/'new.txt').exists())
                with self.assertRaises(ValueError):publish('new.txt',b'updated','test')
                with patch.object(VectorStoreService,'load_document',side_effect=RuntimeError('network')):
                    with self.assertRaises(RuntimeError):publish('new.txt',b'bad update','test',replace=True)
                self.assertEqual((data/'new.txt').read_bytes(),b'new facts')
                publish('new.txt',b'','test',delete=True)
                self.assertFalse((data/'new.txt').exists())
                self.assertTrue(VectorStoreService()._db_is_complete())


class PageTests(unittest.TestCase):
    def test_ui_switch_background_answer_export_and_forms(self):
        from streamlit.testing.v1 import AppTest
        from utils.app_resources import get_agent,get_vector_service
        get_agent.clear();get_vector_service.clear()
        with sqlite3.connect(':memory:',check_same_thread=False) as conn, patch.dict(os.environ,{'ADMIN_PASSWORD':''}):
            with patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
                agent=ReactAgent(SqliteSaver(conn))
            with patch('utils.env.ensure_env_loaded'),patch('utils.app_resources.get_agent',return_value=agent),patch('utils.app_resources.get_vector_service') as store:
                store.return_value.load_document.return_value='ready'
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'))
                app.session_state['authed']=True;app.run(timeout=20)
                next(x for x in app.selectbox if x.label=='登录用户').select('1002').run(timeout=20)
                next(b for b in app.button if b.label=='开始新会话').click().run(timeout=20)
                self.assertEqual(len(app.exception),0)
                self.assertTrue(app.session_state['thread_id'].startswith('user-1002-'))
                app.chat_input[0].set_value('hello').run(timeout=20)
                job=app.session_state['answer_job'];self.assertTrue(job.done.wait(10))
                app.run(timeout=20)
                self.assertEqual(len(app.exception),0)
                self.assertTrue(any('reply:hello' in m.value for m in app.markdown))
                self.assertFalse(any(x.label=='这条回答有帮助吗？' for x in app.radio))
                self.assertTrue(any(x.label=='👍' for x in app.button))
                self.assertFalse(any(x.label=='人工客服转交草稿（不会自动发送）' for x in app.text_area))
        get_agent.clear();get_vector_service.clear()


class StopAndLockTests(unittest.TestCase):
    def test_running_job_can_stop(self):
        from utils.jobs import AnswerJob
        class SlowModel(PlainModel):
            def _generate(self,messages,**kwargs):
                time.sleep(.3)
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='should not be delivered'))])
        with sqlite3.connect(':memory:',check_same_thread=False) as conn, patch('agent.react_agent.get_chat_model',return_value=SlowModel()):
            agent=ReactAgent(SqliteSaver(conn))
            job=AnswerJob(agent,'hi','user-1001','1001')
            time.sleep(.1);job.cancel.set()
            self.assertTrue(job.done.wait(5))
            self.assertIsNone(job.error)
            history=agent.load_history('user-1001','1001')
            self.assertIn('停止',history[-1]['content'])
            self.assertNotIn('should not',history[-1]['content'])

    def test_file_checkpointers_share_lock_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'cp.sqlite')
            with closing(sqlite3.connect(db,check_same_thread=False)) as c1,closing(sqlite3.connect(db,check_same_thread=False)) as c2,patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
                a,b=ReactAgent(SqliteSaver(c1)),ReactAgent(SqliteSaver(c2))
                self.assertEqual(a._lock_namespace,b._lock_namespace)

    def test_conflict_across_files_is_flagged(self):
        from utils.knowledge_admin import conflict_hints
        self.assertTrue(any('已有资料' in w for w in conflict_hints('滤网不可水洗','滤网每月清水冲洗')))
