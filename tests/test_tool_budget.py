"""
代码级闸门的单元测试（A3 工具预算 + ③ 的空检索计数）

共同背景：这两道闸门都住在 `agent/runtime.py`（纯函数）+ `middleware.py`（接线），
职责是"把不能靠提示词劝的东西写进代码"。
背景（2026-09-20）：`main_prompt.txt` 原本写着"累计 5 次工具调用后就停止"，
但**全项目没有任何代码执行它** —— 而同项目自己的结论是"只靠提示词劝是没用的"。
更糟的是那个数字本身就是错的：报告流程的固定链路就要 4 次
（get_user_id → get_current_month → enter_report_mode → fetch_external_data），
"5 次"等于给它留 1 次检索，**零余量**。

所以这里守两件事：
1. 预算真的会拦（不拦就是没有）；
2. **合法的报告流程不能被误伤** —— 这是这条测试存在的真正理由。
"""
import unittest

from agent.runtime import (EMPTY_REASON_NO_COVERAGE, EMPTY_REASON_NO_DOCS,
                           EMPTY_REASON_PHRASE, EXTERNAL_DATA_TOOL,
                           MAX_TOOL_CALLS_PER_TOOL, MAX_TOOL_CALLS_PER_TURN,
                           RAG_TOOL_NAME, TOOL_BUDGET_MARKER, budget_blocked,
                           is_shortcircuit_worthy_empty)

# 实测到的合法报告链路：固定 4 次 + 检索最多 3 次
REPORT_SEQUENCE = ["get_user_id", "get_current_month", "enter_report_mode",
                   EXTERNAL_DATA_TOOL, RAG_TOOL_NAME, RAG_TOOL_NAME, RAG_TOOL_NAME]


def _walk(tools):
    """按顺序"执行"一串工具调用，返回每步的拦截结果与最终计数"""
    total, by_name, results = 0, {}, []
    for name in tools:
        results.append(budget_blocked(total, dict(by_name), name))
        if results[-1] is None:            # 只有放行的才真正执行（与中间件一致）
            total += 1
            by_name[name] = by_name.get(name, 0) + 1
    return results, total


class TestLegitimateFlowsNotBlocked(unittest.TestCase):
    """预算必须先保证不误伤合法路径 —— 旧提示词的 5 次就死在这里"""

    def test_完整报告流程七次调用全部放行(self):
        results, total = _walk(REPORT_SEQUENCE)
        self.assertEqual([None] * len(REPORT_SEQUENCE), results,
                         "报告流程（4 次固定 + 3 次检索）是合法路径，一次都不该被拦")
        self.assertEqual(len(REPORT_SEQUENCE), total)

    def test_预算高于实测合法上界(self):
        """合法上界实测是 7（报告链路固定 4 次 + 检索 1~3 次），预算必须留出余量"""
        self.assertGreater(MAX_TOOL_CALLS_PER_TURN, 7)


class TestTotalBudget(unittest.TestCase):
    def test_第N次仍放行第N加1次拦下(self):
        results, _ = _walk(["get_weather"] * MAX_TOOL_CALLS_PER_TURN)
        self.assertTrue(all(r is None for r in results), "上限内的调用不该被拦")

        blocked = budget_blocked(MAX_TOOL_CALLS_PER_TURN, {}, "get_weather")
        self.assertIsNotNone(blocked)
        self.assertIn(TOOL_BUDGET_MARKER, blocked)

    def test_拦截理由必须告诉模型现在该做什么(self):
        """只说"不许再调"会让模型原地打转；必须指向"基于已有信息作答或如实说没资料"。"""
        blocked = budget_blocked(MAX_TOOL_CALLS_PER_TURN, {}, "get_weather")
        self.assertIn("已有信息", blocked)
        self.assertTrue("全部停用" in blocked or "停止" in blocked)


class TestPerToolBudget(unittest.TestCase):
    def test_检索超过子预算就拦即使总数还没满(self):
        limit = MAX_TOOL_CALLS_PER_TOOL[RAG_TOOL_NAME]
        results, _ = _walk([RAG_TOOL_NAME] * limit)
        self.assertTrue(all(r is None for r in results))

        blocked = budget_blocked(limit, {RAG_TOOL_NAME: limit}, RAG_TOOL_NAME)
        self.assertIsNotNone(blocked)
        self.assertIn(RAG_TOOL_NAME, blocked)
        self.assertIn("不要再换措辞重复调用", blocked)

    def test_取数工具也有上限防重复换月份重试(self):
        limit = MAX_TOOL_CALLS_PER_TOOL[EXTERNAL_DATA_TOOL]
        self.assertIsNotNone(budget_blocked(5, {EXTERNAL_DATA_TOOL: limit},
                                            EXTERNAL_DATA_TOOL))

    def test_未列入上限的工具只受总预算约束(self):
        self.assertIsNone(budget_blocked(5, {"get_weather": 20}, "get_weather"))


class TestCounterDefaults(unittest.TestCase):
    def test_计数缺失时从零开始不抛异常(self):
        """上下文由调用方构造，缺 key 是正常情况（旧快照、CLI 直接调用），不能 KeyError"""
        self.assertIsNone(budget_blocked(0, {}, RAG_TOOL_NAME))


class TestEmptyRetrievalCounting(unittest.TestCase):
    """③ 短语兜底不得计入短路计数（part 3）

    旧行为：中间件只看"内容是否以 [检索为空] 开头"，三种判据一视同仁 ——
    于是那个**已知会误判**的启发式命中两次就把整轮检索停用。
    原因现在由工具经 artifact 的 empty_reason 显式带出来，不靠猜措辞。
    """

    def test_结构化判据计入(self):
        self.assertTrue(is_shortcircuit_worthy_empty(
            {"sources": [], "empty_reason": EMPTY_REASON_NO_DOCS}))
        self.assertTrue(is_shortcircuit_worthy_empty(
            {"sources": [], "empty_reason": EMPTY_REASON_NO_COVERAGE}))

    def test_短语兜底不计入(self):
        self.assertFalse(is_shortcircuit_worthy_empty(
            {"sources": [], "empty_reason": EMPTY_REASON_PHRASE}))

    def test_拿不到原因时保守地计入(self):
        """信息缺失时宁可保留原有拦截能力，也不悄悄放宽一道闸门。"""
        for artifact in (None, {}, "不是字典", {"sources": []}):
            self.assertTrue(is_shortcircuit_worthy_empty(artifact), f"artifact={artifact!r}")


if __name__ == "__main__":
    unittest.main()


class TestCountingHelpers(unittest.TestCase):
    """中间件计数逻辑的纯函数（从 middleware 抽出来，让计数规则可测）"""

    def test_计数先于执行_失败也算一次(self):
        from agent.runtime import bump_tool_counts
        total, by_tool = bump_tool_counts(3, {"rag_summarize": 2}, "rag_summarize")
        self.assertEqual(total, 4)
        self.assertEqual(by_tool["rag_summarize"], 3)

    def test_计数对None的by_tool也安全(self):
        from agent.runtime import bump_tool_counts
        total, by_tool = bump_tool_counts(0, None, "get_user_id")
        self.assertEqual((total, by_tool), (1, {"get_user_id": 1}))

    def test_空检索计数_可靠判据递增(self):
        from agent.runtime import EMPTY_RETRIEVAL_MARKER, rag_empty_count_after
        art = {"empty_reason": EMPTY_RETRIEVAL_MARKER}
        self.assertEqual(rag_empty_count_after(1, "rag_summarize",
                                              EMPTY_RETRIEVAL_MARKER + "无资料", art), 2)

    def test_空检索计数_命中资料即清零(self):
        from agent.runtime import rag_empty_count_after
        self.assertEqual(rag_empty_count_after(2, "rag_summarize", "HEPA 滤网…", None), 0)

    def test_回归_短语兜底不计入短路(self):
        """★ 短语兜底是已知会误判的启发式 —— 它不能把"两次猜测"变成"停用整轮检索" """
        from agent.runtime import EMPTY_REASON_PHRASE, EMPTY_RETRIEVAL_MARKER, rag_empty_count_after
        art = {"empty_reason": EMPTY_REASON_PHRASE}
        self.assertEqual(rag_empty_count_after(1, "rag_summarize",
                                              EMPTY_RETRIEVAL_MARKER + "……", art), 1,
                         "短语兜底命中不应递增计数")

    def test_回归_拿不到reason时按可靠处理(self):
        """artifact 缺失/变了 → 保守方向：宁可计数，也不悄悄放宽安全闸门"""
        from agent.runtime import EMPTY_RETRIEVAL_MARKER, rag_empty_count_after
        self.assertEqual(rag_empty_count_after(1, "rag_summarize",
                                              EMPTY_RETRIEVAL_MARKER + "……", None), 2)

    def test_非检索工具不影响空检索计数(self):
        from agent.runtime import rag_empty_count_after
        self.assertEqual(rag_empty_count_after(2, "get_weather", "晴", None), 2)
