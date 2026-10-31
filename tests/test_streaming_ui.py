"""受约束生成的流式输出：核对通过后逐段吐出，且内部草稿不混进用户可见 token。

原文件里的两个 AppTest 用例（运行中保留历史、点赞按钮）随 Streamlit 前端移除；
点赞与停止改由 FastAPI 的接口测试覆盖。
"""
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from agent.grounding import validate_answer
from agent.react_agent import is_user_visible_token


class StreamingTests(unittest.TestCase):
    def test_grounded_answer_emits_real_incremental_text(self):
        source = {"ref": 1, "file": "a.txt", "snippet": "3-6个月更换"}
        messages = [
            HumanMessage(content="多久换"),
            ToolMessage(content="x", tool_call_id="t", artifact={"sources": [source]}),
        ]
        model = Mock()
        model.with_structured_output.return_value.stream.return_value = iter([
            {"covered": True},
            {"covered": True, "answer": "3-6"},
            {"covered": True, "answer": "3-6个月"},
            {"covered": True, "answer": "3-6个月[1]"},
        ])
        with patch("model.factory.get_chat_model", return_value=model), patch("utils.progress.answer_delta") as emit:
            answer = validate_answer("", messages, {"stream_response": True})
        pieces = [c.args[0] for c in emit.call_args_list if c.args[0]]
        self.assertEqual(len(pieces), 3)
        self.assertEqual("".join(pieces), answer)
        # 核对模型的原始输出带 grounded_stream 标签，不能被当成用户可见 token
        self.assertFalse(
            is_user_visible_token(
                AIMessageChunk(content='{"covered":'),
                {"langgraph_node": "model", "tags": ["grounded_stream"]},
            )
        )
