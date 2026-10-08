"""
会话历史渲染规则的单元测试

对应一个真实 bug（2026-09-20 用户反馈）：
**打开页面后，最后一条消息是用户自己的提问，而不是 AI 的回复。**

根因：一轮对话若在"模型决定调工具之后"中断（刷新页面 / 网络断 / 工具报错），
记忆末尾会是一条**带 tool_calls 的空 AI 消息**；而 `messages_to_history`
会把它过滤掉（因为调工具前的文本是"思考"不是答案）——
结果界面上最后一条就只剩用户自己刚发的问题，**且没有任何提示**。

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.react_agent import INCOMPLETE_TURN_NOTE, messages_to_history


def ai(content="", tool_calls=None):
    return AIMessage(content=content, tool_calls=tool_calls or [])


TOOL_CALL = [{"name": "rag_summarize", "args": {"query": "x"}, "id": "call_1"}]


class TestNormalConversation(unittest.TestCase):

    def test_一问一答(self):
        history = messages_to_history([HumanMessage("你好"), ai("您好，有什么可以帮您？")])
        self.assertEqual([h["role"] for h in history], ["user", "assistant"])
        self.assertFalse(any(h.get("incomplete") for h in history))

    def test_多轮(self):
        history = messages_to_history([
            HumanMessage("问题一"), ai("回答一"),
            HumanMessage("问题二"), ai("回答二"),
        ])
        self.assertEqual([h["role"] for h in history],
                         ["user", "assistant", "user", "assistant"])
        self.assertTrue(history[-1]["content"].startswith("回答二"))

    def test_空历史(self):
        self.assertEqual(messages_to_history([]), [])

    def test_内容为空的AI消息被跳过(self):
        """无内容、无工具调用的 AI 消息没有渲染价值"""
        history = messages_to_history([HumanMessage("你好"), ai("")])
        self.assertEqual([h["role"] for h in history], ["user", "assistant"])
        self.assertTrue(history[-1].get("incomplete"))


class TestToolCallMessages(unittest.TestCase):

    def test_带工具调用的AI消息不当作回答渲染(self):
        """调工具前的文本是"思考"，当回答渲染会误导用户"""
        history = messages_to_history([
            HumanMessage("滤网多久换一次？"),
            ai("我来查一下资料。", TOOL_CALL),
            ToolMessage(content="HEPA 滤网 3-6 个月", tool_call_id="call_1"),
            ai("HEPA 滤网建议 3-6 个月更换一次。"),
        ])
        contents = [h["content"] for h in history]
        self.assertIn("HEPA 滤网建议 3-6 个月更换一次。", contents)
        self.assertNotIn("我来查一下资料。", contents,
                         "工具调用前的文本不该被当成回答")

    def test_回归_中断的回合必须补提示(self):
        """★ 这就是用户反馈的那个 bug：

        末尾是一条**带 tool_calls 的空 AI 消息**（模型决定调工具后中断），
        它被过滤掉后，界面上最后一条会变成用户自己的提问。
        正确行为：补一条"未完成"提示，至少让用户知道那一轮没跑完。
        """
        history = messages_to_history([
            HumanMessage("你好"), ai("您好！"),
            HumanMessage("扫地机器人有哪些品牌推荐"),
            ai("", TOOL_CALL),                      # ← 决定调工具后中断
        ])
        self.assertEqual([h["role"] for h in history],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(history[-1]["content"], INCOMPLETE_TURN_NOTE)
        self.assertTrue(history[-1].get("incomplete"),
                        "界面依据这个标记用 caption 渲染，不能漏")

    def test_回归_只有用户消息时也要补提示(self):
        """用户消息已落库、但这一轮完全没开始（例如刚发出就断网）"""
        history = messages_to_history([HumanMessage("帮我生成报告")])
        self.assertEqual([h["role"] for h in history], ["user", "assistant"])
        self.assertTrue(history[-1].get("incomplete"))

    def test_正常结束的回合不补提示(self):
        history = messages_to_history([
            HumanMessage("滤网多久换一次？"),
            ai("", TOOL_CALL),
            ToolMessage(content="3-6 个月", tool_call_id="call_1"),
            ai("建议 3-6 个月更换一次。"),
        ])
        self.assertFalse(any(h.get("incomplete") for h in history))
        self.assertEqual(history[-1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
