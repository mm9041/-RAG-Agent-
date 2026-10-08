import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import yaml

from utils.config_handler import model_conf,chroma_conf
from utils.model_settings import apply_models,configuration_guard


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root=Path(self.temp.name);(self.root/'config').mkdir();(self.root/'data').mkdir()
        self.models=dict(model_conf,chat_model_name='chat-old',embedding_model_name='embed-old')
        self.chroma=dict(chroma_conf,rerank_model='rank-old',data_path=str(self.root/'data'),persist_directory=str(self.root/'db'))
        (self.root/'config/model.yml').write_text(yaml.safe_dump(self.models),encoding='utf-8')
        (self.root/'config/chroma.yml').write_text(yaml.safe_dump(self.chroma),encoding='utf-8')
        self.stack=[]
        for patcher in [patch.dict(model_conf,self.models),patch.dict(chroma_conf,self.chroma),
                        patch('utils.model_settings.get_abs_path',side_effect=lambda s:str(self.root/s)),patch('utils.admin_auth.valid',return_value=True)]:
            patcher.start();self.stack.append(patcher)
    def tearDown(self):
        for patcher in reversed(self.stack):patcher.stop()
        from model.factory import reset_models
        reset_models();self.temp.cleanup()

    def test_chat_and_rerank_change_without_rebuild(self):
        with patch('rag.vector_store.VectorStoreService') as store:
            result=apply_models('token','chat-new','embed-old','rank-new')
            store.assert_not_called()
        self.assertEqual(model_conf['chat_model_name'],'chat-new')
        self.assertEqual(chroma_conf['rerank_model'],'rank-new')
        self.assertIn('下一次',result)
        self.assertEqual(yaml.safe_load((self.root/'config/model.yml').read_text())['chat_model_name'],'chat-new')

    def test_requires_admin_and_valid_names(self):
        with patch('utils.admin_auth.valid',return_value=False),self.assertRaises(PermissionError):
            apply_models('bad','chat-new','embed-old','rank-new')
        for name in ['', 'bad model', 'bad\nname']:
            with self.assertRaises(ValueError):apply_models('ok',name,'embed-old','rank-old')

    def test_failed_rebuild_restores_files_and_active_manifest(self):
        db=self.root/'db';db.mkdir();meta=db/'kb_meta.json';meta.write_text('{"active_collection":"old"}')
        before=(self.root/'config/model.yml').read_bytes()
        def fail(**kwargs):
            meta.write_text('{"active_collection":"incomplete"}')
            raise RuntimeError('embedding unavailable')
        with patch('rag.vector_store.VectorStoreService') as store:
            store.return_value.load_document.side_effect=fail
            with self.assertRaises(RuntimeError):apply_models('ok','chat-new','embed-new','rank-new')
        self.assertEqual((self.root/'config/model.yml').read_bytes(),before)
        self.assertEqual(json.loads(meta.read_text())['active_collection'],'old')
        self.assertEqual(model_conf['embedding_model_name'],'embed-old')

    def test_real_rebuild_uses_new_embedding_dimension(self):
        from langchain_core.embeddings import Embeddings
        from rag.vector_store import VectorStoreService
        class Local(Embeddings):
            def __init__(self,n):self.n=n
            def embed_documents(self,texts):return [[1.0]*self.n for _ in texts]
            def embed_query(self,text):return [1.0]*self.n
        (self.root/'data/a.txt').write_text('Useful knowledge for testing.',encoding='utf-8')
        with patch('rag.vector_store.get_embed_model',side_effect=lambda:Local(4 if model_conf['embedding_model_name']=='embed-old' else 8)):
            old=VectorStoreService();old.load_document()
            name=old.vector_store._collection.name
            apply_models('ok','chat-old','embed-new','rank-old')
            new=VectorStoreService()
            self.assertTrue(new._db_is_complete())
            self.assertNotEqual(name,new.vector_store._collection.name)
            self.assertEqual(len(new.vector_store.get(include=['embeddings'])['embeddings'][0]),8)

    def test_config_writer_waits_for_active_reader(self):
        entered=threading.Event();finished=threading.Event()
        def writer():
            entered.set()
            with configuration_guard(write=True):finished.set()
        with configuration_guard():
            thread=threading.Thread(target=writer);thread.start();entered.wait(2)
            self.assertFalse(finished.wait(.1))
        thread.join(3);self.assertTrue(finished.is_set())


class ExistingAgentTests(unittest.TestCase):
    def test_existing_agent_uses_new_chat_model_on_next_turn(self):
        from agent.react_agent import ReactAgent
        from langgraph.checkpoint.sqlite import SqliteSaver
        from tests.test_customer_features import PlainModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatResult,ChatGeneration
        class NewModel(PlainModel):
            def _generate(self,messages,**kwargs):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='new-model-answer'))])
        with sqlite3.connect(':memory:',check_same_thread=False) as conn,patch('agent.react_agent.get_chat_model',return_value=PlainModel()):
            agent=ReactAgent(SqliteSaver(conn))
            with patch('utils.model_settings.refresh_model_settings'),patch.dict(model_conf,{'chat_model_name':'changed-model'}),patch('model.factory.get_chat_model',return_value=NewModel()):
                self.assertEqual(agent.answer('hi','user-1001','1001'),'new-model-answer')
