"""
工具 schema 的冒烟测试

**这条测试来自一次真实的翻车**（2026-09-20）：

为了给日志标记"来源"，给 `rag_summarize` 加了 `runtime: ToolRuntime = None`。
带默认值之后，LangChain 认不出它是"可注入的 runtime"，于是去给 Callable 生成
JSON schema → `PydanticInvalidForJsonSchema` → **agent 一调用就崩**；
而且 `runtime` 会泄漏进**模型可见**的 schema。

最刺眼的不是这个 bug，而是：**当时 103 个单元测试全绿**。
因为现有测试全都只调纯函数或 `.func()`，**没有任何一条覆盖"把工具绑定给模型"这一步** ——
而这一步恰恰是所有工具在真实运行时的必经之路。

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from langchain_core.tools import BaseTool

import agent.tools.agent_tools as tools_module
from agent.react_agent import ReactAgent


def all_tools() -> list[BaseTool]:
    """从 agent_tools 模块里自动发现所有工具（不手写清单，避免漏项）"""
    return [obj for obj in vars(tools_module).values()
            if isinstance(obj, BaseTool)]


class TestToolSchemas(unittest.TestCase):

    def test_能发现到工具(self):
        """防"自动发现"本身失效（那样下面的用例会空跑并通过）"""
        self.assertGreaterEqual(len(all_tools()), 7,
                                f"只发现到 {len(all_tools())} 个工具，自动发现可能失效了")

    def test_每个工具都能生成给模型看的schema(self):
        """★ 这就是那次翻车的守门用例

        `tool_call_schema` 正是工具被绑定给模型时用到的 schema；
        它生成失败 → 模型看不到这个工具，或直接抛异常中断整轮。
        """
        for tool in all_tools():
            with self.subTest(tool=tool.name):
                try:
                    schema = tool.tool_call_schema
                except Exception as e:
                    self.fail(f"工具 {tool.name} 的 schema 生成失败（agent 会崩）："
                              f"{type(e).__name__}: {e}")
                self.assertIsNotNone(schema)

    def test_可注入参数不能泄漏给模型(self):
        """`ToolRuntime` 是 LangChain 注入的，**不该出现在模型可见的参数里**。

        写错成 `ToolRuntime = None` 时会泄漏（实测），模型可能自己去填这个参数。
        """
        for tool in all_tools():
            with self.subTest(tool=tool.name):
                fields = set(getattr(tool.tool_call_schema, "model_fields", {}))
                self.assertNotIn("runtime", fields,
                                 f"工具 {tool.name} 把注入参数 runtime 泄漏给模型了")

    def test_文档字符串会作为工具描述(self):
        """描述缺失会让模型不知道该何时调用它 —— 本项目对工具描述要求很严"""
        for tool in all_tools():
            with self.subTest(tool=tool.name):
                description = tool.description or ""
                self.assertTrue(description.strip(),
                                f"工具 {tool.name} 没有描述")

    def test_装配agent时不会因工具而失败(self):
        """真实运行的必经一步：把工具交给 create_agent（它会绑定 schema）"""
        try:
            import sqlite3
            from unittest.mock import patch
            from langgraph.checkpoint.sqlite import SqliteSaver
            from tests.test_improvements import ScriptModel
            with sqlite3.connect(":memory:", check_same_thread=False) as conn, patch(
                    "agent.react_agent.get_chat_model", return_value=ScriptModel()):
                ReactAgent(SqliteSaver(conn))
        except Exception as e:
            self.fail(f"ReactAgent 装配失败：{type(e).__name__}: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
