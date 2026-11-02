"""
总结服务类：用户提问，搜索参考资料，将提问和参考资料提交给模型，让模型总结回复
"""

from utils.progress import phase
from dataclasses import dataclass, field
import os
import hashlib
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from model.factory import get_chat_model
from rag.vector_store import VectorStoreService
from utils.config_handler import chroma_conf
from utils.logger_handler import logger
from utils.prompt_loader import load_rag_prompts


@dataclass
class RagSource:
    """一条被引用的参考资料（只用于界面展示，不进模型上下文）"""

    file: str           # 来源文件名
    distance: float     # 与问题的相似度距离（越小越相似）
    snippet: str        # 原文
    chunk_id: str = ""
    page: int | None = None
    ref: int = 0
    kb_version: str = ""


@dataclass
class RagResult:
    """检索 + 总结的结果

    把 retrieval 侧的统计单独带出来，是为了让上层能**结构性地**判断
    "到底有没有检索到相关内容"，而不是去猜模型输出里有没有"未提供"这种词
    （模型说"没找到"的措辞有无数种，实测漏判过"未提及"）。
    """

    answer: str
    doc_count: int                  # 通过阈值、真正送进上下文的资料条数
    retrieved: int = 0              # 阈值过滤之前的召回条数
    best_distance: float | None = None   # 最相似那条的距离，便于调阈值
    sources: list[RagSource] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.doc_count == 0


_SPLIT_PATTERN = re.compile(r"[？?]+")
_MIN_PART_CHARS = 4


def split_multi_intent(query: str, max_parts: int = 3) -> list[str]:
    """把「一句话问两件事」拆成多个子查询；单问句原样返回。

    为什么必须拆：向量检索把整句编码成**一个**向量，两个意图会互相稀释。
    实测「我家 65 平木地板养了猫，吸力选多大？主刷多久清理一次？」：
      - 回答「吸力选多大」的那块（选购指南 第 2 条）在 20 条粗召回里**排第 13**
        （距离 0.998），重排取 top-3 必然选不到；
      - 把它单独拿出来问「吸力选多大合适」，同一块**排第 1**。
    后果是工具层行为不稳定：模型偶尔自觉拆成两次 rag_summarize 就答对，
    自觉合成一次就只剩「主刷」相关原文，grounding 判不覆盖 → 固定拒答。
    这个不确定性不该留给模型去撞。

    只在**明显多问句**时拆：按问号切，每段至少 4 个字。
    陈述式或单问句（如「开机没反应，指示灯也不亮，怎么排查？」）切出来只有一段，
    直接返回原串，行为与拆分前完全一致 —— 所以这个改动对既有问题是零影响。

    ``max_parts`` 是成本上限：每拆一段就多一次 embedding + 重排。
    """
    parts = [p.strip(" \t\r\n，,、；;。") for p in _SPLIT_PATTERN.split(query)]
    parts = [p for p in parts if len(p) >= _MIN_PART_CHARS]
    return parts[:max_parts] if len(parts) > 1 else [query]


class RagSummarizeService(object):
    def __init__(self):
        self.vector_store = VectorStoreService()
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        self.model = get_chat_model()
        self.chain = self._init_chain()

    def _init_chain(self):
        chain = self.prompt_template | self.model | StrOutputParser()
        return chain

    def summarize(self, query: str, *, raw: bool = False, ref_start: int = 1) -> RagResult:
        return self.summarize_many(split_multi_intent(query), raw=raw, ref_start=ref_start)

    def summarize_many(self, queries: list[str], *, raw: bool = False,
                       ref_start: int = 1) -> RagResult:
        """按给定的子查询逐个检索并合并。调用方决定怎么切，这里只负责合并。"""
        started = time.monotonic()
        seen: set[str] = set()
        contexts: list[str] = []
        sources: list[RagSource] = []
        recalled = 0
        best_distance: float | None = None
        reranked = False

        for sub_query in queries:
            context, found, sub_recalled, sub_best, sub_reranked = self._search(
                sub_query, ref_start + len(sources), seen)
            if context:
                contexts.append(context)
            sources.extend(found)
            recalled += sub_recalled
            reranked = reranked or sub_reranked
            if sub_best is not None and (best_distance is None or sub_best < best_distance):
                best_distance = sub_best

        if not sources:
            return RagResult(answer="", doc_count=0, retrieved=recalled,
                             best_distance=best_distance)

        context = "".join(contexts)
        phase("整理检索结果" if raw else "生成资料摘要")
        answer = ("以下是检索候选原文，并不保证覆盖问题。仅依据相关原文回答，"
                  "资料无关时明确拒答，结论引用给定的 [数字]。\n" + context) if raw else self.chain.invoke({"input": queries[0], "context": context})
        logger.info("[RAG] mode=%s elapsed=%.3fs reranked=%s queries=%d sources=%d",
                    "raw" if raw else "summary", time.monotonic()-started, reranked,
                    len(queries), len(sources))

        return RagResult(answer=answer, doc_count=len(sources),
                         retrieved=recalled, best_distance=best_distance,
                         sources=sources)

    def _search(self, query: str, ref_start: int, seen: set[str]):
        """检索单个子查询，返回 (context, sources, recalled, best_distance, reranked)。

        ``seen`` 跨子查询共享：同一条原文被两个意图各召回一次时只保留第一次，
        编号也不会重复。
        """
        # 完整流水线在 vector_store.retrieve 里：粗召回 →（可选）距离阈值过滤 → 重排 → 截断到 k
        phase("检索资料")
        outcome = self.vector_store.retrieve(query)

        if not outcome.hits:
            # 注意区分两种"空"：库本身为空，还是被阈值整批滤掉了。
            # 阈值当前是关闭的（见 chroma.yml 的决策记录），所以这里通常只会是前者；
            # 但如果将来有人把 max_distance 填回去，这条日志要能说清是哪种情况。
            threshold = chroma_conf.get("max_distance")
            if outcome.recalled and threshold is not None:
                logger.warning(
                    f"[RAG]粗召回 {outcome.recalled} 条但全部超过距离阈值 {threshold}"
                    f"（最近一条 {outcome.best_distance:.4f}），判定为无相关资料，query={query}"
                )
            else:
                logger.warning(f"[RAG]向量库为空，query={query}")
            return "", [], outcome.recalled, outcome.best_distance, outcome.reranked

        context = ""
        sources: list[RagSource] = []
        # 读一次即可：_load_meta() 每次都真的打开并解析 kb_meta.json，
        # 放在循环里会随 k 线性放大磁盘 IO。
        kb_version = self.vector_store._load_meta().get("active_collection", "legacy")
        for doc, score in outcome.hits:
            chunk_id = str(doc.metadata.get("chunk_id") or hashlib.sha256(
                (str(doc.metadata) + doc.page_content).encode("utf-8")).hexdigest())
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            # 编号按**实际收下的条数**递增，跳过重复块时不会留下空洞
            ref = ref_start + len(sources)
            context += f"[{ref}] 适用型号：{doc.metadata.get('model', '未指定')}；条件：{doc.metadata.get('scope', '需核对具体型号')}\n{doc.page_content}\n"

            sources.append(RagSource(
                file=os.path.basename(str(doc.metadata.get("source", "未知来源"))),
                distance=round(float(score), 4),
                snippet=doc.page_content, chunk_id=chunk_id,
                page=doc.metadata.get("page"), ref=ref,
                kb_version=kb_version,
            ))

        logger.info(f"[RAG]query={query} | 粗召回 {outcome.recalled} → 阈值内重排取 "
                    f"{len(outcome.hits)} 条 | 实际重排={outcome.reranked}")
        return context, sources, outcome.recalled, outcome.best_distance, outcome.reranked

if __name__ == '__main__':
    rag = RagSummarizeService()

    for question in ["小户型适合哪些扫地机器人", "量子计算机的退相干时间怎么延长？"]:
        result = rag.summarize(question)
        print(f"Q: {question}")
        print(f"   召回 {result.retrieved} 条 / 阈值内 {result.doc_count} 条 "
              f"/ 最近距离 {result.best_distance:.4f} / 判定为空: {result.is_empty}")
        print(f"   回答: {result.answer[:80]}")
        print()
