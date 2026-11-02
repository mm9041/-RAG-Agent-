"""多意图拆分检索。

背景：一次向量检索只能用一个向量表达一个意图。实测「我家 65 平木地板养了猫，
吸力选多大？主刷多久清理一次？」里，回答"吸力"的那块在 20 条粗召回中排第 13，
重排取 top-3 必然选不到；拆开后单独检索它排第 1。

这里守三件事：
1. 只在**明显多问句**时拆，单问句必须原样返回（否则会影响既有行为）
2. 编号连续、跨子查询去重
3. `ref_start` 生效（工具层靠它避免多次调用撞号）
"""
from __future__ import annotations

import unittest

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from rag.rag_service import RagResult, RagSummarizeService, split_multi_intent
from rag.vector_store import RetrievalOutcome


def _doc(text: str, chunk_id: str) -> Document:
    return Document(page_content=text, metadata={"chunk_id": chunk_id, "source": "a.txt"})


class _FakeStore:
    """按 query 精确返回预设结果，避免测试依赖真实向量库。"""

    def __init__(self, mapping: dict[str, list[Document]]) -> None:
        self.mapping = mapping

    def retrieve(self, query: str) -> RetrievalOutcome:
        docs = self.mapping.get(query, [])
        return RetrievalOutcome(
            hits=[(d, 0.5 + i / 10) for i, d in enumerate(docs)],
            recalled=len(docs),
            best_distance=0.5 if docs else None,
            reranked=False,
        )

    def _load_meta(self) -> dict:
        return {"active_collection": "test"}


def _service(mapping: dict[str, list[Document]]) -> RagSummarizeService:
    service = RagSummarizeService.__new__(RagSummarizeService)  # 跳过 __init__ 里的建库
    service.vector_store = _FakeStore(mapping)
    return service


class SplitTests(unittest.TestCase):
    def test_two_questions_are_split(self):
        parts = split_multi_intent("我家 65 平木地板养了猫，吸力选多大？主刷多久清理一次？")
        self.assertEqual(parts, ["我家 65 平木地板养了猫，吸力选多大", "主刷多久清理一次"])

    def test_single_question_is_untouched(self):
        q = "扫地机器人开机没反应，指示灯也不亮，怎么排查？"
        self.assertEqual(split_multi_intent(q), [q])

    def test_no_question_mark_is_untouched(self):
        for q in ("你好", "扫地机器人怎么保养", ""):
            self.assertEqual(split_multi_intent(q), [q])

    def test_trailing_and_empty_segments_are_dropped(self):
        # 末尾的空段不该变成一个子查询；但**单问句**是原样返回的（连问号都不动），
        # 因为不走拆分路径，行为必须和改动前完全一致。
        self.assertEqual(split_multi_intent("滤网多久换？"), ["滤网多久换？"])
        self.assertEqual(
            split_multi_intent("吸力选多大？？主刷多久清理？"),
            ["吸力选多大", "主刷多久清理"],
        )

    def test_parts_are_capped(self):
        parts = split_multi_intent("甲甲甲甲？乙乙乙乙？丙丙丙丙？丁丁丁丁？", max_parts=3)
        self.assertEqual(len(parts), 3)


class MergeTests(unittest.TestCase):
    def test_each_intent_gets_its_own_retrieval(self):
        service = _service({
            "吸力选多大": [_doc("家用≥3000Pa", "c1")],
            "主刷多久清理一次": [_doc("每周清理主刷", "c2")],
        })
        result = service.summarize("吸力选多大？主刷多久清理一次？", raw=True)
        self.assertEqual(result.doc_count, 2)
        self.assertEqual([s.ref for s in result.sources], [1, 2])
        self.assertEqual([s.file for s in result.sources], ["a.txt", "a.txt"])
        self.assertIn("[1]", result.answer)
        self.assertIn("[2]", result.answer)

    def test_same_chunk_is_not_counted_twice(self):
        shared = _doc("同一块", "same")
        service = _service({
            "第一个问题": [shared, _doc("独有A", "a")],
            "第二个问题": [shared, _doc("独有B", "b")],
        })
        result = service.summarize("第一个问题？第二个问题？", raw=True)
        ids = [s.chunk_id for s in result.sources]
        self.assertEqual(ids, ["same", "a", "b"])
        self.assertEqual([s.ref for s in result.sources], [1, 2, 3])

    def test_ref_start_is_respected(self):
        service = _service({"甲问题内容": [_doc("A", "c1")], "乙问题内容": [_doc("B", "c2")]})
        result = service.summarize("甲问题内容？乙问题内容？", raw=True, ref_start=5)
        self.assertEqual([s.ref for s in result.sources], [5, 6])

    def test_empty_result_stays_empty(self):
        service = _service({})
        result = service.summarize("甲问题内容？乙问题内容？", raw=True)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.sources, [])


class RetrievalQueryTests(unittest.TestCase):
    """工具层怎么选子查询：多问句用原话，单问句用模型关键词。"""

    def _runtime(self, *human_contents: str):
        class _Runtime:
            def __init__(self) -> None:
                self.state = {
                    "messages": [
                        HumanMessage(content=c) if c else AIMessage(content="x")
                        for c in human_contents
                    ]
                }
                self.context: dict = {}

        return _Runtime()

    def test_multi_question_uses_user_wording_not_model_keywords(self):
        from agent.tools.agent_tools import retrieval_queries

        runtime = self._runtime("我家 65 平木地板养了猫，吸力选多大？主刷多久清理一次？")
        # 模型把问题改写成了关键词串，且丢掉了问号
        queries = retrieval_queries("木地板 养猫宠物 吸力档位选择 主刷清理周期", runtime)
        self.assertEqual(queries, ["我家 65 平木地板养了猫，吸力选多大", "主刷多久清理一次"])

    def test_single_question_keeps_model_query(self):
        from agent.tools.agent_tools import retrieval_queries

        runtime = self._runtime("那电池呢？")
        self.assertEqual(retrieval_queries("扫地机器人电池更换周期", runtime),
                         ["扫地机器人电池更换周期"])

    def test_missing_state_falls_back_to_model_query(self):
        from agent.tools.agent_tools import retrieval_queries

        class _Bare:
            state = None
            context: dict = {}

        self.assertEqual(retrieval_queries("滤网更换", _Bare()), ["滤网更换"])

    def test_takes_the_latest_human_message(self):
        from agent.tools.agent_tools import retrieval_queries

        runtime = self._runtime("第一个问题？第二个问题？", "只说一句")
        self.assertEqual(retrieval_queries("模型改写", runtime), ["模型改写"])


class DuplicateCallTests(unittest.TestCase):
    """同一轮里重复调同一个查询，不该再检索一遍。"""

    def test_second_identical_call_short_circuits(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from agent.tools.agent_tools import rag_summarize

        service = Mock()
        service.summarize_many.return_value = RagResult(answer="资料正文", doc_count=1)
        runtime = SimpleNamespace(
            context={"next_ref": 1},
            state={"messages": [HumanMessage(content="滤网多久换一次")]},
        )
        with patch("agent.tools.agent_tools.get_rag_service", return_value=service):
            rag_summarize.func("滤网更换", runtime)
            text, artifact = rag_summarize.func("滤网更换", runtime)

        self.assertEqual(service.summarize_many.call_count, 1)
        self.assertIn("已经检索过", text)
        self.assertEqual(artifact["sources"], [])
        # 第二次没有真的检索，就不该再占编号
        self.assertEqual(runtime.context["next_ref"], 1 + 3)


if __name__ == "__main__":
    unittest.main()
