"""
流过滤与事件解析的回归测试

守的是 agent/ui 之间**最容易被静默改坏**的一层：

- `is_user_visible_token`：messages 流的两重过滤。★ 第二重（节点过滤）是
  修掉「389 个 chunk 里 158 个是工具内部 token」的 bug 后加上的 ——
  工具内部若再调 LLM，其 token 会被当成最终回答打给用户。
- `_parse_updates`：把节点更新翻译成 tool_start / sources 事件。
  它解析错了，界面的「过程面板」和「参考资料」就会错乱。

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from agent.react_agent import MODEL_NODE, ReactAgent, is_user_visible_token


def chunk(content="x", node=None):
    """构造一条 messages 流的 chunk（meta 按 LangGraph 的实际形状）"""
    meta = {} if node is None else {"langgraph_node": node}
    return AIMessageChunk(content=content), meta


class TestUserVisibleToken(unittest.TestCase):

    def test_顶层模型的token可见(self):
        c, meta = chunk("你好", node=None)                 # 顶层：无 langgraph_node
        self.assertTrue(is_user_visible_token(c, meta))

    def test_模型节点的token可见(self):
        c, meta = chunk("你好", node=MODEL_NODE)
        self.assertTrue(is_user_visible_token(c, meta))

    def test_回归_工具节点里的token不可见(self):
        """★ 就是那个 bug：工具内部再调 LLM，其 token 会被当成最终回答"""
        c, meta = chunk("内部思考", node="tools")
        self.assertFalse(is_user_visible_token(c, meta))

    def test_回归_工具内部模型节点的token不可见(self):
        c, meta = chunk("内部思考", node="rag_summarize")
        self.assertFalse(is_user_visible_token(c, meta))

    def test_非AIMessageChunk不可见(self):
        tm = ToolMessage(content="工具返回", tool_call_id="t1")
        self.assertFalse(is_user_visible_token(tm, {"langgraph_node": MODEL_NODE}))
        self.assertFalse(is_user_visible_token(AIMessage(content="完整消息"), None))

    def test_meta缺失时视为顶层(self):
        c, _ = chunk("你好")
        self.assertTrue(is_user_visible_token(c, None))
        self.assertTrue(is_user_visible_token(c, {}))


class TestParseUpdates(unittest.TestCase):

    def test_工具调用翻译成tool_start(self):
        msg = AIMessage(content="", tool_calls=[
            {"name": "rag_summarize", "args": {"query": "滤网"}, "id": "c1"}])
        events = list(ReactAgent._parse_updates({"model": {"messages": [msg]}}))
        self.assertEqual([(e, d["name"]) for e, d in events],
                         [("tool_start", "rag_summarize")])
        self.assertEqual(events[0][1]["args"], {"query": "滤网"})

    def test_带来源artifact的ToolMessage产出sources加tool_end(self):
        """工具返回会产生：sources（如有 artifact）→ tool_end（界面据此收尾）"""
        tm = ToolMessage(content="正文", tool_call_id="t1", name="rag_summarize",
                         artifact={"sources": [{"file": "a.txt"}]})
        events = list(ReactAgent._parse_updates({"tools": {"messages": [tm]}}))
        self.assertEqual([e for e, _ in events], ["sources", "tool_end"])
        self.assertEqual(events[0][1][0]["file"], "a.txt")
        self.assertEqual(events[1][1], {"name": "rag_summarize", "content": "正文"})

    def test_没有artifact的ToolMessage只产出tool_end(self):
        tm = ToolMessage(content="正文", tool_call_id="t1", name="get_weather")
        events = list(ReactAgent._parse_updates({"tools": {"messages": [tm]}}))
        self.assertEqual(events, [("tool_end", {"name": "get_weather", "content": "正文"})])

    def test_普通AI回答不产出工具事件(self):
        msg = AIMessage(content="这是最终回答")
        self.assertEqual(list(ReactAgent._parse_updates({"model": {"messages": [msg]}})), [])

    def test_空payload安全(self):
        self.assertEqual(list(ReactAgent._parse_updates(None)), [])
        self.assertEqual(list(ReactAgent._parse_updates({})), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
