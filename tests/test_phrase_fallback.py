"""
「③ 短语兜底」判据的回归测试（对应 ③ 的 part 2）

背景（2026-09-20 从生产日志里查出来的真实误判）：
报告流程中模型自己拼出的检索词是**多主题拼接**，例如
    「HEPA滤网更换周期 主刷更换保养 木地板清扫注意事项」
它如实答上了其中两个主题、第三个没答上，于是在 **126 字答案的第 113 字**
（另一条 135 字的第 122 字）写出"未提及" —— 这正是
`prompts/rag_summarize.txt` 第 6 条**要求**的写法（"哪怕只能部分回答也要正常总结"）。

而旧判据是"**全文**含任一短语即触发"，于是把两个可用主题的完整回答整段丢掉，
还反过来命令模型"别检索了，告知用户没资料" → 报告**静默**缺内容。

所以新判据靠**位置 + 长度**区分"部分覆盖的如实补充说明"与"整题没资料"：
短语必须落在回答开头窗口内、且答案短到基本就是一句覆盖声明。

⚠️ 一个诚实的说明：救场样本（离题问题）的"短语在开头"这一形态是**推断**，
当时没有记原文、会话也已删除，无法核对。所以本文件里"该触发的"用例
是按该形态**构造**的，不是复现的；真正被复现并钉死的是"不该触发的"那两类。
这也是 part 4（把被丢弃原文与短语位置打进日志）存在的理由。
"""
import unittest

from agent.tools.agent_tools import (_PHRASE_HEAD_WINDOW, _PHRASE_MAX_ANSWER,
                                     _NO_COVERAGE_PHRASES, detect_phrase_fallback)

# 复现样本的等价形态：答上了两个主题，在结尾如实说明第三个未提及
PARTIAL_COVERAGE_ANSWER = (
    "HEPA滤网更换周期为3-6个月，日常需每周用软毛刷轻刷表面灰尘，"
    "每1至2个月可用清水冲洗并彻底晾干后装回。"
    "主刷在普通家庭建议3-6个月更换一次，宠物家庭或长毛地毯环境建议1-3个月更换，"
    "拆卸后清除缠绕的毛发再复位安装即可。"
    "至于木地板清扫的注意事项，现有参考资料未提及防潮与转速的具体设置。"
)

# 整题无资料：短语就在开头，且整句就是一句"没覆盖"的声明
WHOLE_QUESTION_UNCOVERED = "未提及相关内容。现有参考资料集中在产品的使用、维护与选购，没有涉及该问题。"

# 开头命中、但内容其实很充实（说明"未提及"只是顺带一提）
LONG_ANSWER_WITH_HEAD_PHRASE = (
    "未提及该品牌的市占率数据；不过关于您关心的选购要点，资料给出了完整建议："
    "先看导航方式（dToF/LDS 优于随机碰撞），再看吸力与风道设计是否匹配户型面积，"
    "拖地部分重点关注是否支持自动抬升拖布、以及水箱容量的实际续航，"
    "另外地毯家庭建议确认主刷防缠绕结构与边刷的可拆卸设计，"
    "最后按预算比对同档位机型的自动集尘与自清洁基站配置。"
)


class TestPartialCoverageIsNotRefused(unittest.TestCase):
    """★ 核心回归：部分覆盖的如实回答**不能**再被丢掉"""

    def test_复现样本_短语在正文尾部不触发(self):
        phrase, pos, length = detect_phrase_fallback(PARTIAL_COVERAGE_ANSWER)
        self.assertIsNone(phrase, "多主题检索里答上了两个主题，不该判为整题无资料")
        # 顺带钉住样本形态本身：短语确实在很后面、且超过开头窗口
        self.assertGreater(pos, _PHRASE_HEAD_WINDOW)
        self.assertGreater(pos, length * 0.5, "复现样本的关键特征是短语出现在答案后段")

    def test_反证_旧判据全文命中会把这个好答案误杀(self):
        """若有人把判据退回"全文含短语即触发"，这条会失败提醒复核。"""
        legacy_hit = any(p in PARTIAL_COVERAGE_ANSWER for p in _NO_COVERAGE_PHRASES)
        self.assertTrue(legacy_hit, "样本里确实含短语（旧判据会触发）")
        self.assertIsNone(detect_phrase_fallback(PARTIAL_COVERAGE_ANSWER)[0],
                          "新判据必须放过它")


class TestWholeQuestionUncoveredStillRefused(unittest.TestCase):
    """② 漏报时这层还要能兜住 —— 它今天真实救过一次场（离题的"发明人是谁"）"""

    def test_短语在开头且答案很短则触发(self):
        phrase, pos, length = detect_phrase_fallback(WHOLE_QUESTION_UNCOVERED)
        self.assertIsNotNone(phrase, "整题无资料必须仍被拦下")
        self.assertLessEqual(pos, _PHRASE_HEAD_WINDOW)
        self.assertLess(length, _PHRASE_MAX_ANSWER)

    def test_两条判据必须同时成立_长答案不触发(self):
        """短语在开头但答案很长 → 说明它其实答了东西，不该整段丢掉。"""
        phrase, pos, length = detect_phrase_fallback(LONG_ANSWER_WITH_HEAD_PHRASE)
        self.assertLessEqual(pos, _PHRASE_HEAD_WINDOW, "样本形态：短语确实在开头")
        self.assertGreaterEqual(length, _PHRASE_MAX_ANSWER, "样本形态：答案不短")
        self.assertIsNone(phrase)


class TestNoPhraseAndObservability(unittest.TestCase):
    def test_没有短语时位置为负一(self):
        phrase, pos, length = detect_phrase_fallback("滤网建议每3至6个月更换一次。")
        self.assertIsNone(phrase)
        self.assertEqual(-1, pos)
        self.assertEqual(len("滤网建议每3至6个月更换一次。"), length)

    def test_不触发时也返回位置与长度_part4要靠它做观测(self):
        """part 4 要把"命中位置/答案长度"写进日志，才能事后判断判据是否仍然成立。"""
        _, pos, length = detect_phrase_fallback(PARTIAL_COVERAGE_ANSWER)
        self.assertGreater(pos, 0)
        self.assertEqual(len(PARTIAL_COVERAGE_ANSWER), length)

    def test_空字符串安全(self):
        self.assertEqual((None, -1, 0), detect_phrase_fallback(""))


if __name__ == "__main__":
    unittest.main()
