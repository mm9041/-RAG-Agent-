"""引用编号格式只做确定性验证，不改变模型答复或增加模型调用。"""
import unittest

from agent.grounding import valid_citations


class CitationFormatTests(unittest.TestCase):
    sources = [{"ref": 1}, {"ref": 2}, {"ref": 3}]

    def test_original_single_and_adjacent_citations(self):
        for answer in ("结论[1]。", "结论[1][2]。", "结论[1]，另一个结论[3]。"):
            with self.subTest(answer=answer):
                self.assertTrue(valid_citations(answer, self.sources))

    def test_known_grouped_citations_and_whitespace(self):
        for answer in ("结论[1,2]。", "结论[1，2]。", "结论[1、2]。",
                       "结论[ 1, 2 ]。", "结论[1,2][3]。", "结论[1,1]。"):
            with self.subTest(answer=answer):
                self.assertTrue(valid_citations(answer, self.sources))

    def test_unknown_single_and_grouped_references_are_rejected(self):
        for answer in ("结论[4]。", "结论[1,4]。", "结论[1，4]。", "结论[4、5]。",
                       "正确[1]，但另一个结论[8,9]。", "正确[1,2]，但另一个结论[8]。"):
            with self.subTest(answer=answer):
                self.assertFalse(valid_citations(answer, self.sources))

    def test_answer_without_citations_is_rejected(self):
        self.assertFalse(valid_citations("没有给出引用。", self.sources))
        self.assertFalse(valid_citations("结论[1]。", []))

    def test_non_numeric_markdown_labels_are_not_citations(self):
        for answer in ("请查看[说明书](https://example.invalid)。",
                       "请查看[第2章](https://example.invalid)。", "[提示] 请确认型号。"):
            with self.subTest(answer=answer):
                self.assertFalse(valid_citations(answer, self.sources))
                self.assertTrue(valid_citations(answer + "依据[1]。", self.sources))

    def test_forbidden_legacy_marker_is_still_rejected(self):
        self.assertFalse(valid_citations("结论[1]。[资料:说明书]", self.sources))
        self.assertFalse(valid_citations("结论[1,2]。[资料:说明书]", self.sources))

    def test_malformed_numeric_group_cannot_hide_behind_valid_reference(self):
        for bad in ("[1,]", "[1,,2]", "[1,two]", "[1;2]", "[1 2]", "[1-2]",
                    "[1.2]", "[+1]", "[-1]", "[,1]", "[1", "[1[2]]", "[[1]]", "[1,\n2]",
                    "[\n1]", "[\n8]"):
            with self.subTest(bad=bad):
                self.assertFalse(valid_citations("合法结论[1]，但有坏引用" + bad, self.sources))

    def test_source_ids_still_use_exact_identity(self):
        self.assertTrue(valid_citations("结论[1,2]。", [{"ref": "1"}, {"ref": "2"}]))
        self.assertFalse(valid_citations("结论[01]。", self.sources))


if __name__ == "__main__":
    unittest.main()
