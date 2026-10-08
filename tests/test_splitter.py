"""
切分预处理的单元测试

对应报告第二十一、二十二节修掉的两个真实召回缺口。**纯函数，不联网、秒级可跑。**

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.vector_store import normalize_entry_boundaries, strip_markdown_headings
from utils.config_handler import chroma_conf


class TestStripMarkdownHeadings(unittest.TestCase):

    def test_剥掉各级标题但保留正文(self):
        text = ("# 扫地机器人维护保养200条（纯保养维度，分通用基础/扫地专属/…）\n"
                "## 通用基础维护（50条）\n"
                "1. 每日使用后，用干软布擦拭机身外壳。\n"
                "2. 每次清扫完成，及时清理防撞条缝隙的毛发。")
        out = strip_markdown_headings(text)
        self.assertNotIn("# 扫地机器人维护保养", out)
        self.assertNotIn("## 通用基础维护", out)
        self.assertIn("1. 每日使用后", out)
        self.assertIn("2. 每次清扫完成", out)

    def test_正文里的井号不被误删(self):
        """`#` 出现在行中间（如型号、话题标签）时不该被当成标题"""
        text = "1. 型号 A#1 支持地毯增压。\n2. 见上文 #注意事项"
        self.assertEqual(strip_markdown_headings(text), text)

    def test_空输入(self):
        self.assertEqual(strip_markdown_headings(""), "")


class TestNormalizeEntryBoundaries(unittest.TestCase):

    def test_条目之间插入空行(self):
        text = "1. 第一条内容。\n2. 第二条内容。\n   - 这是第二条的补充说明"
        out = normalize_entry_boundaries(text)
        self.assertIn("1. 第一条内容。\n\n2. 第二条内容。", out)
        # 条目**内部**的换行（补充说明行）不应被改成空行，否则条目又被拆开了
        self.assertIn("2. 第二条内容。\n   - 这是第二条的补充说明", out)

    def test_中文顿号编号也识别(self):
        text = "1、第一条。\n2、第二条。"
        self.assertIn("1、第一条。\n\n2、第二条。", normalize_entry_boundaries(text))

    def test_非编号行不受影响(self):
        text = "普通段落一。\n普通段落二。"
        self.assertEqual(normalize_entry_boundaries(text), text)


class TestQuestionAndAnswerStayTogether(unittest.TestCase):
    """**回归测试**：报告二十二节那个 bug ——
    问句一行、回答一行时，切分器可能正好切在两者之间，
    导致出现"带着一个自己回答不了的问题"的块。
    """

    def _split(self, text):
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chroma_conf["chunk_size"],
            chunk_overlap=chroma_conf["chunk_overlap"],
            separators=chroma_conf["separators"],
        )
        return splitter.split_text(strip_markdown_headings(text))

    def test_预处理后问句与其回答在同一块(self):
        text = (
            "1. **扫地机器人是如何实现自主导航的？**\n"
            "- 通过激光雷达(LDS)、视觉传感器(VSLAM)或陀螺仪实现环境感知和定位。\n"
            "2. **LDS 激光导航和 VSLAM 视觉导航哪个更好？**\n"
            "- LDS 精度更高、不受光线影响；VSLAM 成本更低、可识别更多物体细节。\n"
            "3. **什么是 dToF 导航技术？**\n"
            "- 直接飞行时间测距(direct Time-of-Flight)，比传统 LDS 测距更精准。\n"
        )
        chunks = self._split(normalize_entry_boundaries(text))

        holder = [c for c in chunks if "dToF" in c]
        self.assertTrue(holder, "应该有一块包含 dToF 相关内容")
        self.assertTrue(
            any("直接飞行时间测距" in c for c in holder),
            "dToF 的问句和它的答案必须在同一块里（这正是当年那个 bug）",
        )

    def test_不做预处理时确实会切散(self):
        """反证：不加预处理，同样的文本会把问句与答案切开 ——
        说明这个预处理不是"加了也没用"的东西。"""
        text = (
            "1. **扫地机器人是如何实现自主导航的？**\n"
            "- 通过激光雷达(LDS)、视觉传感器(VSLAM)或陀螺仪实现环境感知和定位。\n"
            "2. **LDS 激光导航和 VSLAM 视觉导航哪个更好？**\n"
            "- LDS 精度更高、不受光线影响；VSLAM 成本更低、可识别更多物体细节。\n"
            "3. **什么是 dToF 导航技术？**\n"
            "- 直接飞行时间测距(direct Time-of-Flight)，比传统 LDS 测距更精准。\n"
        )
        chunks = self._split(text)          # 不调 normalize_entry_boundaries
        holder = [c for c in chunks if "dToF" in c]
        if holder:
            self.assertFalse(
                any("直接飞行时间测距" in c for c in holder),
                "若这里为真，说明切分器行为已变，第二十二节的 bug 可能不再需要该预处理"
                "（那就该重新评估：是删掉 normalize 还是保留）",
            )


class TestChunkOverlapIsNoop(unittest.TestCase):
    """记录一个实测事实：本库的语料下 `chunk_overlap` 配小值是**空转**。

    RecursiveCharacterTextSplitter 的重叠是"保留末尾若干个完整 split 单元"，
    而本库的单元是整行（远超 20 字），于是全被弹出、真实重叠为 0。
    配置已改为 0，这个用例用来固定"为什么是 0"这个结论。
    """

    def test_小重叠值在本语料上不产生真实重叠(self):
        text = "\n".join(f"{i}. 这是第{i}条说明，长度大约二三十个字，用来模拟真实行长。"
                         for i in range(1, 60))
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=200, chunk_overlap=20,
            separators=chroma_conf["separators"])
        chunks = splitter.split_text(text)
        overlaps = []
        for a, b in zip(chunks, chunks[1:]):
            n = 0
            for k in range(1, min(len(a), len(b)) + 1):
                if a[-k:] == b[:k]:
                    n = k
            overlaps.append(n)
        zero_ratio = sum(1 for v in overlaps if v == 0) / len(overlaps)
        self.assertGreater(
            zero_ratio, 0.5,
            f"实测重叠为 0 的比例只有 {zero_ratio:.0%}；"
            "若这个断言失败，说明切分器行为或语料形态已变，配置里的注释需要复核",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
