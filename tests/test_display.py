"""
界面文案格式化的单元测试

守的是一类**看起来很不起眼、但直接影响用户理解**的问题：
过程面板里那一行「工具返回」该怎么写。

实际踩过的坑（2026-09-20 用户反馈）：把 `rag_summarize` 的返回正文贴进过程面板后，
用户看到一段灰色小字，**内容像是"上一轮的回答又打印了一遍"**
（因为它的返回是"检索材料的总结"，而相关问题检索到的材料与上一轮回答高度重叠）。

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from utils.display import DEFAULT_PREVIEW, format_tool_result

RAG_SUMMARY = "石头与科沃斯对比：两者均为一线品牌，品控和售后更完善。其中科沃斯创新性强；石头稳定性好、算法优秀。"


class TestRagToolResult(unittest.TestCase):

    def test_检索类工具不贴返回正文(self):
        """★ 回归：这正是用户看到"上一轮答案打印两遍"的根因"""
        text = format_tool_result("rag_summarize", RAG_SUMMARY)
        self.assertNotIn("科沃斯创新性强", text,
                         "检索材料的正文不能进过程面板 —— 它读起来就是回答")
        self.assertIn("已返回参考内容", text)
        self.assertIn(str(len(" ".join(RAG_SUMMARY.split()))), text)

    def test_检索类工具要指向材料的真正去处(self):
        """告诉用户材料在哪，而不是把材料搬过来"""
        self.assertIn("参考资料", format_tool_result("rag_summarize", RAG_SUMMARY))

    def test_检索为空时明确说无资料(self):
        text = format_tool_result("rag_summarize", "[检索为空]知识库中没有相关内容")
        self.assertIn("无相关资料", text)
        self.assertNotIn("[检索为空]", text, "哨兵是给程序看的，不该原样展示")


class TestOtherToolResult(unittest.TestCase):

    def test_短的结构化事实保留预览(self):
        """天气、月份这类短事实贴出来有助于看清过程，也不会被误认成回答"""
        text = format_tool_result("get_weather", "城市深圳天气为晴天，气温26摄氏度")
        self.assertIn("城市深圳天气为晴天", text)

    def test_过长的返回按上限截断(self):
        text = format_tool_result("fetch_external_data", "x" * 500)
        self.assertLessEqual(len(text), len("**工具返回** `fetch_external_data`：") + DEFAULT_PREVIEW)

    def test_换行与多余空白被压平(self):
        text = format_tool_result("get_user_id", "  1010\n\n ")
        self.assertIn("：1010", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
