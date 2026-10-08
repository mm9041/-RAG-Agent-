import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.react_agent import ReactAgent, messages_to_history
from agent.tools.agent_tools import fetch_external_data
from agent.tools.middleware import reserve_tool, bounded_messages
from eval.baseline import compare_baseline, merge_snapshot
from tests.test_baseline import snapshot
from utils.config_handler import agent_conf, chroma_conf


class ScriptModel(BaseChatModel):
    bound: list = []
    loop: bool = False

    @property
    def _llm_type(self):
        return "offline-script"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"bound": tools})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.bound and (self.loop or not any(isinstance(m, ToolMessage) for m in messages)):
            message = AIMessage(content="", tool_calls=[{"name": "get_user_id",
                "args": {}, "id": "call-" + str(len(messages)), "type": "tool_call"}])
        else:
            message = AIMessage(content="离线最终答复")
        return ChatResult(generations=[ChatGeneration(message=message)])


class IdentityAndGraphTests(unittest.TestCase):
    def test_identity_hidden_and_missing_rejected(self):
        fields = fetch_external_data.tool_call_schema.model_json_schema()["properties"]
        self.assertEqual(set(fields), {"month"})
        with self.assertRaises(PermissionError):
            fetch_external_data.func("2025-12", SimpleNamespace(context={}))
        with self.assertRaises(ValueError):
            fetch_external_data.func("2025-13", SimpleNamespace(context={"user_id": "1001"}))

    def test_real_graph_injects_identity(self):
        with sqlite3.connect(":memory:", check_same_thread=False) as conn:
            cp = SqliteSaver(conn)
            with patch("agent.react_agent.get_chat_model", return_value=ScriptModel()), patch(
                "agent.tools.agent_tools.load_external_records", return_value={"1001": {"2025-12": {"x": "own"}},
                                                                          "2002": {"2025-12": {"x": "other"}}}):
                agent = ReactAgent(checkpointer=cp)
                result = agent.agent.invoke({"messages": [HumanMessage(content="报告")]},
                    config=agent._config("user-1001"), context=agent._context("1001"))
                outputs = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
                self.assertEqual(outputs, ["1001"])
                with self.assertRaises(PermissionError):
                    list(agent.stream_events("hi", "user-2002", "1001"))
                with self.assertRaises(PermissionError):
                    agent.load_history("user-2002", "1001")

    def test_clear_all_over_twenty_preserves_other_owner(self):
        with sqlite3.connect(":memory:", check_same_thread=False) as conn:
            cp = SqliteSaver(conn)
            with patch("agent.react_agent.get_chat_model", return_value=ScriptModel()):
                agent = ReactAgent(cp)
            for i in range(24):
                agent.agent.update_state(agent._config(f"user-1001-{i}"), {"messages": [HumanMessage(content=str(i))]})
            agent.agent.update_state(agent._config("user-10010-keep"), {"messages": [HumanMessage(content="other")]})
            self.assertEqual(len(agent.list_threads("1001")), 20)
            self.assertEqual(agent.clear_other_threads("1001", "user-1001-0"), 23)
            self.assertEqual(len(agent.list_threads("1001", None)), 1)
            self.assertEqual(len(agent.list_threads("10010", None)), 1)

    def test_model_loop_stops_and_stream_has_final(self):
        with sqlite3.connect(":memory:", check_same_thread=False) as conn, patch.dict(agent_conf, {"max_model_calls": 3}):
            with patch("agent.react_agent.get_chat_model", return_value=ScriptModel(loop=True)), patch(
                "agent.tools.agent_tools.load_external_records", return_value={}):
                agent = ReactAgent(SqliteSaver(conn))
                self.assertIn("离线最终答复", agent.answer("报告", "user-1001", "1001"))

    def test_expired_turn_has_persisted_final(self):
        with sqlite3.connect(":memory:", check_same_thread=False) as conn, patch.dict(agent_conf, {"turn_timeout": -1}):
            with patch("agent.react_agent.get_chat_model", return_value=ScriptModel()):
                agent = ReactAgent(SqliteSaver(conn))
                self.assertIn("上限", agent.answer("hi", "user-1001", "1001"))
                self.assertIn("上限", agent.load_history("user-1001", "1001")[-1]["content"])


class BudgetTests(unittest.TestCase):
    def test_parallel_budget_is_atomic(self):
        context = {"tool_call_total": 0, "tool_calls_by_name": {}}
        with ThreadPoolExecutor(max_workers=16) as pool:
            result = list(pool.map(lambda _: reserve_tool(context, "rag_summarize"), range(60)))
        self.assertEqual(sum(r is None for r in result), 4)
        self.assertEqual(context["tool_call_total"], 4)

    def test_trim_preserves_current_tool_pair(self):
        current = [HumanMessage(content="new"), AIMessage(content="", tool_calls=[
            {"name":"x", "args":{}, "id":"x"}]), ToolMessage(content="result", tool_call_id="x")]
        messages = [HumanMessage(content="old"*100), AIMessage(content="old answer")] + current
        self.assertEqual(bounded_messages(messages, 30), current)

    def test_sources_survive_history(self):
        source = {"file":"a", "snippet":"full", "chunk_id":"id"}
        history = messages_to_history([HumanMessage(content="q"), ToolMessage(content="x", tool_call_id="x",
            artifact={"sources":[source]}), AIMessage(content="a")])
        self.assertEqual(history[-1]["sources"], [source])


class BaselineTests(unittest.TestCase):
    def test_missing_key_and_judge_are_failures(self):
        new = snapshot(); del new["fingerprint"]["k"]
        self.assertEqual(compare_baseline(snapshot(), new)[0][0], "fail")
        self.assertTrue(any(level == "fail" for level, _ in compare_baseline(snapshot(), snapshot(model_a=None))))

    def test_merge_different_config_discards_old_sets(self):
        new = snapshot(embedding_model="new"); del new["sets"]["b"]; del new["refusals"]
        result = merge_snapshot(snapshot(), new)
        self.assertNotIn("b", result["sets"])
        self.assertNotIn("refusals", result)

    def test_config_comparison_still_detects_regression(self):
        result = compare_baseline(snapshot(), snapshot(embedding_model="new", model_a=1), allow_config_change=True)
        self.assertTrue(any("模型判据回退" in m for _, m in result))


class LocalEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(t) % 10) + 1, 1., 2., 3., 4., 5., 6., 7.] for t in texts]
    def embed_query(self, text):
        return self.embed_documents([text])[0]


class IndexTests(unittest.TestCase):
    def test_failed_incremental_keeps_old_and_retry_has_no_duplicates(self):
        from rag.vector_store import VectorStoreService
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp); data = root / "data"; data.mkdir()
            (data / "old.txt").write_text("old useful content", encoding="utf-8")
            with patch.dict(chroma_conf, {"persist_directory":str(root / "db"), "data_path":str(data)}), patch(
                "rag.vector_store.get_embed_model", return_value=LocalEmbeddings()):
                store = VectorStoreService(); store.load_document()
                old_meta = Path(store.meta_path).read_bytes()
                old_collection = store.vector_store._collection.name
                for name in ("a", "b"):
                    (data / (name + ".txt")).write_text(name + " useful content", encoding="utf-8")
                original = store._index_file
                calls = []
                def broken(path):
                    calls.append(path)
                    if len(calls) == 2:
                        raise RuntimeError("simulated embedding failure")
                    return original(path)
                with patch.object(store, "_index_file", side_effect=broken), self.assertRaises(RuntimeError):
                    store.load_document()
                self.assertEqual(Path(store.meta_path).read_bytes(), old_meta)
                self.assertEqual(store.vector_store._collection.name, old_collection)
                self.assertEqual(store.count(), 1)
                store.load_document()
                self.assertEqual(store.count(), 3)
                self.assertTrue(store._db_is_complete())
                ids = store.vector_store.get(include=[])["ids"]
                store.load_document()
                self.assertEqual(store.vector_store.get(include=[])["ids"], ids)
                with patch.object(store, "_index_file", side_effect=RuntimeError("full rebuild failed")):
                    with self.assertRaises(RuntimeError):
                        store.load_document(force=True)
                self.assertEqual(store.count(), 3)
                # Old readers can adopt the newly published generation.
                other = VectorStoreService()
                self.assertEqual(other.vector_store._collection.name, store.vector_store._collection.name)


class ConfigAndRagTests(unittest.TestCase):
    def test_env_is_project_anchored_and_external_env_wins(self):
        from utils import env
        with patch.object(env, "_env_loaded", False), patch.object(env, "load_dotenv") as load:
            env.ensure_env_loaded()
            path = Path(load.call_args.args[0])
            self.assertEqual(path.name, ".env")
            self.assertEqual(path.parent, Path(__file__).resolve().parents[1])
            self.assertFalse(load.call_args.kwargs["override"])

    def test_raw_rag_skips_summary_and_keeps_full_sources(self):
        from rag.rag_service import RagSummarizeService
        from rag.vector_store import RetrievalOutcome
        from langchain_core.documents import Document
        from unittest.mock import Mock
        service = object.__new__(RagSummarizeService)
        service.vector_store = Mock()
        service.chain = Mock()
        content = "完整原文" * 100
        service.vector_store.retrieve.return_value = RetrievalOutcome(hits=[(
            Document(page_content=content, metadata={"source":"a.pdf", "page":2, "chunk_id":"abc"}), .3)])
        result = service.summarize("q", raw=True)
        service.chain.invoke.assert_not_called()
        self.assertEqual(result.sources[0].snippet, content)
        self.assertEqual(result.sources[0].page, 2)
        self.assertIn("[1]", result.answer)

    def test_report_always_uses_summary(self):
        from agent.tools.agent_tools import rag_summarize
        from rag.rag_service import RagResult
        from unittest.mock import Mock
        service = Mock(); service.summarize.return_value = RagResult(answer="covered", doc_count=1)
        with patch.dict(agent_conf, {"rag_raw_documents":True}), patch(
            "agent.tools.agent_tools.get_rag_service", return_value=service):
            rag_summarize.func("q", SimpleNamespace(context={"report":True}))
        service.summarize.assert_called_once_with("q", raw=False, ref_start=1)


class ProviderAndUiTests(unittest.TestCase):
    def test_openai_compatible_payload(self):
        import httpx
        from langchain_openai import ChatOpenAI
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id":"offline", "object":"chat.completion", "created":1,
                "model":"offline", "choices":[{"index":0, "finish_reason":"stop",
                "message":{"role":"assistant", "content":"provider reply"}}],
                "usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client, sqlite3.connect(
                ":memory:", check_same_thread=False) as conn:
            model = ChatOpenAI(model="offline", api_key="synthetic-test-key", base_url="https://offline.invalid/v1",
                               http_client=client, max_retries=0)
            with patch("agent.react_agent.get_chat_model", return_value=model):
                agent = ReactAgent(SqliteSaver(conn))
                result = agent.agent.invoke({"messages":[HumanMessage(content="hi")]},
                    config=agent._config("user-1001"), context=agent._context("1001"))
            self.assertEqual(result["messages"][-1].content, "provider reply")
            self.assertNotIn("timeout", seen[0])
            schema = next(t["function"] for t in seen[0]["tools"] if t["function"]["name"] == "get_user_id")
            self.assertEqual(set(schema["parameters"]["properties"]), set())
            self.assertNotIn("fetch_external_data", [t["function"]["name"] for t in seen[0]["tools"]])

    def test_login_reads_env_before_password_check(self):
        import os
        from streamlit.testing.v1 import AppTest
        def load():
            os.environ["APP_PASSWORD"] = "synthetic-login-test"
        with patch.dict(os.environ, {}, clear=False), patch("utils.env.ensure_env_loaded", side_effect=load):
            os.environ.pop("APP_PASSWORD", None)
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
            app.run(timeout=10)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(item.label == "访问密码" for item in app.text_input))
            self.assertFalse(any("未设置 APP_PASSWORD" in item.value for item in app.warning))
