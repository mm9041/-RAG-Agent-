"""交互等待预算与阶段反馈，使用 HTTP mock 和内存检查点验证实际 Agent 接线。"""
import json
import sqlite3
import threading
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.react_agent import ReactAgent
from agent.tools.agent_tools import get_user_id
from agent.tools.middleware import bound_model
from model.factory import ChatModelFactory, EmbeddingsFactory
from tests.test_improvements import ScriptModel
from utils.config_handler import agent_conf, model_conf


class DecisionSettingsTests(unittest.TestCase):
    def call_model(self, messages=None, flags=None, remaining=180, timeout=45):
        request = ModelRequest(
            model=ScriptModel(), messages=messages or [HumanMessage(content="你好")],
            tools=[get_user_id], runtime=SimpleNamespace(context={"deadline": 100 + remaining}),
            model_settings={"extra_body": {"existing_option": "kept"}},
        )
        seen = []

        def handler(actual):
            seen.append(actual)
            return ModelResponse(result=[AIMessage(content="回复")])

        with patch.dict(agent_conf, flags or {}), patch.dict(model_conf, {"chat_request_timeout": timeout}), \
                patch("agent.tools.middleware.time.monotonic", return_value=100), \
                patch("agent.tools.middleware.phase") as emit:
            bound_model.wrap_model_call(request, handler)
        return seen[0], [call.args[0] for call in emit.call_args_list]

    def test_first_decision_disables_thinking_and_keeps_other_options(self):
        actual, phases = self.call_model(flags={"decision_disable_thinking": True})
        self.assertEqual(actual.model_settings["extra_body"],
                         {"existing_option": "kept", "enable_thinking": False})
        self.assertEqual(phases[0], "正在理解问题")
        self.assertNotIn("max_retries", actual.model_settings)
        self.assertEqual(actual.tools, [get_user_id])

    def test_decision_switch_can_restore_provider_default(self):
        actual, _ = self.call_model(flags={"decision_disable_thinking": False,
                                          "answer_disable_thinking": True})
        self.assertNotIn("enable_thinking", actual.model_settings["extra_body"])

    def test_answer_switch_remains_independent(self):
        messages = [HumanMessage(content="天气"),
                    ToolMessage(content="网页摘要", tool_call_id="weather", name="get_weather")]
        for decision, answer in ((True, False), (False, True)):
            with self.subTest(decision=decision, answer=answer):
                actual, phases = self.call_model(messages, {"decision_disable_thinking": decision,
                                                            "answer_disable_thinking": answer})
                self.assertEqual("enable_thinking" in actual.model_settings["extra_body"], answer)
                self.assertEqual(phases[0], "正在生成回复")

    def test_old_tool_results_do_not_change_current_decision_switch(self):
        messages = [HumanMessage(content="旧问题"),
                    ToolMessage(content="旧摘要", tool_call_id="old", name="get_weather"),
                    AIMessage(content="旧回复"), HumanMessage(content="新问题")]
        actual, phases = self.call_model(messages, {"decision_disable_thinking": False,
                                                   "answer_disable_thinking": True})
        self.assertNotIn("enable_thinking", actual.model_settings["extra_body"])
        self.assertEqual(phases[0], "正在理解问题")

    def test_model_timeout_respects_chat_configuration_and_turn_budget(self):
        for remaining, configured, expected in ((180, 45, 45), (7, 45, 7), (180, 12, 12)):
            with self.subTest(remaining=remaining, configured=configured):
                actual, _ = self.call_model(remaining=remaining, timeout=configured)
                self.assertEqual(actual.model_settings["timeout"], expected)


class ClientBudgetTests(unittest.TestCase):
    def test_chat_defaults_are_independent_from_embedding_budget(self):
        with patch.dict(model_conf, {"request_timeout": 120, "max_retries": 3}), \
                patch("model.factory.init_chat_model") as create:
            model_conf.pop("chat_request_timeout", None)
            model_conf.pop("chat_max_retries", None)
            ChatModelFactory().generator()
        self.assertEqual(create.call_args.kwargs["request_timeout"], 45)
        self.assertEqual(create.call_args.kwargs["max_retries"], 0)

    def test_chat_client_accepts_explicit_timeout_and_retry_settings(self):
        with patch.dict(model_conf, {"chat_request_timeout": 20, "chat_max_retries": 1}), \
                patch("model.factory.init_chat_model") as create:
            ChatModelFactory().generator()
        self.assertEqual(create.call_args.kwargs["request_timeout"], 20)
        self.assertEqual(create.call_args.kwargs["max_retries"], 1)

    def test_embedding_keeps_original_retry_policy(self):
        with patch.dict(model_conf, {"request_timeout": 120, "max_retries": 3,
                                    "chat_request_timeout": 45, "chat_max_retries": 0}), \
                patch("model.factory.OpenAIEmbeddings") as create:
            embedding = EmbeddingsFactory().generator()
        self.assertEqual(create.call_args.kwargs["request_timeout"], 120)
        self.assertEqual(create.call_args.kwargs["max_retries"], 0)
        self.assertEqual(embedding._retries, 3)


class ImmediateFeedbackTests(unittest.TestCase):
    def test_phase_is_available_before_configuration_lock_is_entered(self):
        agent = ReactAgent.__new__(ReactAgent)
        with patch("utils.model_settings.configuration_guard") as guard:
            events = agent.stream_events("你好", "user-1001", "1001")
            self.assertEqual(next(events), ("phase", "等待服务配置就绪"))
            guard.assert_not_called()
            events.close()

    def test_already_cancelled_request_does_not_wait_for_configuration(self):
        agent = ReactAgent.__new__(ReactAgent)
        cancel = threading.Event()
        cancel.set()
        with patch("utils.model_settings.configuration_guard") as guard:
            self.assertEqual(list(agent.stream_events("你好", "user-1001", "1001",
                                                     cancel_event=cancel)), [])
            guard.assert_not_called()

    def test_cancel_after_initial_phase_skips_execution(self):
        agent = ReactAgent.__new__(ReactAgent)
        cancel = threading.Event()
        with patch("utils.model_settings.configuration_guard"), \
                patch("utils.model_settings.refresh_model_settings") as refresh:
            events = agent.stream_events("你好", "user-1001", "1001", cancel_event=cancel)
            next(events)
            cancel.set()
            self.assertEqual(list(events), [])
            refresh.assert_not_called()

    def test_wrong_owner_is_rejected_before_waiting_for_configuration(self):
        agent = ReactAgent.__new__(ReactAgent)
        with patch("utils.model_settings.configuration_guard") as guard:
            with self.assertRaises(PermissionError):
                list(agent.stream_events("你好", "user-1002", "1001"))
            guard.assert_not_called()

    def test_stream_execution_inherits_callback_and_earlier_deadline(self):
        from utils.progress import current_progress_context, progress_context
        agent = ReactAgent.__new__(ReactAgent)
        agent._chat_model_name = model_conf["chat_model_name"]
        agent.agent = Mock()
        agent.agent.get_state.return_value = SimpleNamespace(values={"messages": []})
        active_contexts = []
        runtime_deadlines = []

        def stream(*args, **kwargs):
            active_contexts.append(current_progress_context())
            runtime_deadlines.append(kwargs["context"]["deadline"])
            yield "custom", {"phase": "正在查询天气"}

        agent.agent.stream.side_effect = stream
        cancel = threading.Event()
        callback = Mock()
        deadline = time.monotonic() + 60
        with patch.object(agent, "_turn_lock", return_value=nullcontext()), \
                patch("utils.model_settings.configuration_guard", return_value=nullcontext()), \
                patch("utils.model_settings.refresh_model_settings"), \
                progress_context(deadline=deadline, on_event=callback):
            events = list(agent.stream_events("天气", "user-1001", "1001", cancel_event=cancel))
        self.assertIn(("phase", "正在查询天气"), events)
        self.assertIs(active_contexts[0].cancel_event, cancel)
        self.assertIs(active_contexts[0].on_event, callback)
        self.assertEqual(active_contexts[0].deadline, deadline)
        self.assertEqual(runtime_deadlines, [deadline])
        self.assertIsNone(current_progress_context())


class ProviderWiringTests(unittest.TestCase):
    def test_real_agent_keeps_identity_tools_and_emits_stages(self):
        requests = []
        timeouts = []
        weather_client = Mock()
        weather_client.search.return_value = {"results": [
            {"content": "深圳天气网页摘要", "url": "https://weather.example.invalid"}]}

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            timeouts.append(request.extensions.get("timeout", {}))
            if len(requests) == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "identity", "type": "function", "function": {
                        "name": "get_user_id", "arguments": "{}"}},
                    {"id": "weather", "type": "function", "function": {
                        "name": "get_weather", "arguments": json.dumps({"city": "深圳"})}}]}
                finish = "tool_calls"
            else:
                identity = next(m["content"] for m in payload["messages"]
                                if m.get("tool_call_id") == "identity")
                message = {"role": "assistant", "content": "您的用户ID是" + identity}
                finish = "stop"
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 1, "model": "offline",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                sqlite3.connect(":memory:", check_same_thread=False) as conn, \
                patch.dict(agent_conf, {"decision_disable_thinking": True,
                                        "answer_disable_thinking": True}), \
                patch.dict(model_conf, {"chat_request_timeout": 9}), \
                patch("agent.tools.agent_tools._get_tavily_client", return_value=weather_client):
            model = ChatOpenAI(model="offline", api_key="synthetic-test-key",
                               base_url="https://offline.invalid/v1", http_client=client, max_retries=0)
            with patch("agent.react_agent.get_chat_model", return_value=model):
                agent = ReactAgent(SqliteSaver(conn))
                events = list(agent.agent.stream(
                    {"messages": [HumanMessage(content="我在深圳，请查天气并告诉我的用户ID")]},
                    config=agent._config("user-1001"), context=agent._context("1001"),
                    stream_mode=["updates", "custom"]))

        self.assertEqual(len(requests), 2)
        self.assertTrue(all(payload.get("enable_thinking") is False for payload in requests))
        self.assertTrue(all("max_retries" not in payload and "timeout" not in payload
                            for payload in requests))
        self.assertTrue(all(settings.get("read") == 9 for settings in timeouts))
        functions = {item["function"]["name"]: item["function"] for item in requests[0]["tools"]}
        self.assertEqual(set(functions), {
            "rag_summarize", "get_weather", "get_user_location", "get_user_id",
            "get_current_month", "fetch_external_data", "enter_report_mode",
        })
        self.assertEqual(functions["get_user_id"]["parameters"].get("properties"), {})
        phases = [payload["phase"] for mode, payload in events if mode == "custom" and "phase" in payload]
        self.assertIn("正在理解问题", phases)
        self.assertIn("正在查询天气", phases)
        self.assertIn("正在生成回复", phases)
        weather_client.search.assert_called_once()
        self.assertEqual(next(m["content"] for m in requests[1]["messages"]
                              if m.get("tool_call_id") == "identity"), "1001")


if __name__ == "__main__":
    unittest.main()
