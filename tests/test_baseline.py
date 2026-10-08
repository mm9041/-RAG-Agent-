"""
评估基线比对逻辑的单元测试（对应 E3）

基线机制存在的理由：本项目吃过两次**静默质量回退**（换 embedding 后沿用旧阈值、
以及在误导性指标下得出"rerank 没必要"），两次的共同点是"只有评估集能发现"，
而**没人把数字存下来，就没有人能比对**。

⚠️ 这里只测**比对逻辑**（纯函数、离线、零成本）。
真实评估要调 embedding/重排/模型，做成单测会同时毁掉"零联网"和"零额度消耗"
两条性质，还会因为额度耗尽而随机变红 —— 所以跑评估是一条命令，比对才是被测对象。
"""
import unittest

from eval.baseline import (compare_baseline, format_problems, has_failures,
                           merge_snapshot)

FP = {"corpus_hash": "c", "prompts_hash": "p", "cases_hash": "q", "rag_raw_documents": False, "embedding_model": "e1", "chat_model_name": "c1", "rerank_model": "r1",
      "rerank_enabled": True, "max_distance": None, "k": 3,
      "rerank_fetch_k": 20, "chunk_size": 200, "chunk_overlap": 0, "blocks": 330}


def snapshot(model_a=20, keyword_a=19, model_b=19, keyword_b=16, refusals=6,
             **fp_overrides):
    fingerprint = dict(FP, **fp_overrides)
    data = {
        "fingerprint": fingerprint,
        "sets": {
            "a": {"keyword": keyword_a, "model": model_a, "total": 20},
            "b": {"keyword": keyword_b, "model": model_b, "total": 20},
        },
        "refusals": {"passed": refusals, "total": 6},
    }
    return data


def levels(problems):
    return [level for level, _ in problems]


class TestPass(unittest.TestCase):
    def test_完全一致时通过(self):
        self.assertEqual([], compare_baseline(snapshot(), snapshot()))
        self.assertFalse(has_failures(compare_baseline(snapshot(), snapshot())))

    def test_没跑judge时模型判据为None不参与比对(self):
        current = snapshot(model_a=None, model_b=None)
        self.assertTrue(has_failures(compare_baseline(snapshot(), current)))


class TestFailures(unittest.TestCase):
    def test_模型判据下降是回退(self):
        problems = compare_baseline(snapshot(), snapshot(model_a=18))
        self.assertIn("fail", levels(problems))
        self.assertTrue(any("模型判据回退" in m for _, m in problems))

    def test_拒答能力下降是回退(self):
        """max_distance 已关闭，"没资料"完全靠模型的 [无覆盖] 约定 → 这项没有商量余地。"""
        problems = compare_baseline(snapshot(), snapshot(refusals=5))
        self.assertIn("fail", levels(problems))
        self.assertTrue(any("拒答能力回退" in m for _, m in problems))

    def test_题目数变了不能拿旧数字比(self):
        current = snapshot()
        current["sets"]["a"]["total"] = 25
        self.assertIn("fail", levels(compare_baseline(snapshot(), current)))

    def test_缺基线时明确要求先拍(self):
        problems = compare_baseline(None, snapshot())
        self.assertEqual(["fail"], levels(problems))
        self.assertIn("--snapshot", problems[0][1])

    def test_缺指纹时报无法比对而非通过(self):
        current = snapshot()
        current.pop("fingerprint")
        self.assertIn("指纹", compare_baseline(snapshot(), current)[0][1])


class TestStaleFingerprint(unittest.TestCase):
    """配置指纹不一致 → 判"过期"，**并且不比数字**（换了 embedding 后数字本就该变）"""

    def test_换embedding后旧数字不可比(self):
        problems = compare_baseline(snapshot(), snapshot(model_a=15,
                                                        embedding_model="e2"))
        self.assertEqual(["fail"], levels(problems))
        message = problems[0][1]
        self.assertIn("过期", message)
        self.assertNotIn("模型判据回退", message, "必须只报「基线不适用」，不要报指标回退")
        self.assertIn("embedding_model", message)

    def test_去掉阈值也算配置变化(self):
        baseline = dict(snapshot(), fingerprint=dict(FP, max_distance=1.0))
        self.assertIn("max_distance", compare_baseline(baseline, snapshot())[0][1])


class TestWarnings(unittest.TestCase):
    def test_关键词判据下降只算提示(self):
        """该判据本身不可靠（口语化问法下 95% → 75%），不该因为它把检查判成失败。"""
        problems = compare_baseline(snapshot(), snapshot(keyword_a=12))
        self.assertEqual(["warn"], levels(problems))
        self.assertFalse(has_failures(problems))

    def test_本次没跑到某个集合只算提示(self):
        current = snapshot()
        current["sets"].pop("b")
        self.assertEqual(["fail"], levels(compare_baseline(snapshot(), current)))

    def test_本次没跑拒答检查只算提示(self):
        current = snapshot()
        current.pop("refusals")
        self.assertEqual(["fail"], levels(compare_baseline(snapshot(), current)))


class TestMerge(unittest.TestCase):
    def test_分两次跑A和B不该互相冲掉(self):
        only_a = {"fingerprint": FP, "sets": {"a": {"keyword": 19, "model": 20,
                                                    "total": 20}}}
        only_b = {"fingerprint": FP, "sets": {"b": {"keyword": 16, "model": 19,
                                                    "total": 20}},
                  "refusals": {"passed": 6, "total": 6}}
        merged = merge_snapshot(merge_snapshot(None, only_a), only_b)
        self.assertEqual({"a", "b"}, set(merged["sets"]))
        self.assertEqual(6, merged["refusals"]["passed"])

    def test_重新拍基线时指纹整体替换(self):
        old = {"fingerprint": FP, "sets": {}}
        merged = merge_snapshot(old, snapshot(embedding_model="e9"))
        self.assertEqual("e9", merged["fingerprint"]["embedding_model"])


class TestFormatting(unittest.TestCase):
    def test_空问题清单渲染成通过(self):
        self.assertIn("一致", format_problems([]))

    def test_有失败时输出里带级别标记(self):
        text = format_problems(compare_baseline(snapshot(), snapshot(model_a=1)))
        self.assertIn("[fail]", text)


if __name__ == "__main__":
    unittest.main()
