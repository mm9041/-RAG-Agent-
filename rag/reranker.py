"""
检索重排（两段式检索的第二段）

为什么需要它：
向量检索是双塔（bi-encoder）——query 和 chunk 各自编码成向量再算相似度，
**缺少 query 与 chunk 之间的细粒度交互**。后果是粗排经常把"字面像但没用"的块排到前面。
实测本库：块级 hit@3 只有 **58%**，约四成问题答案所在的块根本没进 top-3。
换成 cross-encoder 重排后（fetch 10 → 重排 → 取 3）块级 hit@3 提到 **83%**，
救回 3 例、弄丢 0 例。

三条硬约束（都是踩过或想清楚才写下来的）：

1. **rerank 只能重排「已召回的候选」，捞不回没召回的** —— 所以粗召回要开大
   （`rerank_fetch_k`），否则重排无从发挥；
2. **分数方向相反**：rerank 是「分数越高越相关」，向量库是「距离越小越相似」。
   因此这里只输出**下标**，不改距离 —— `max_distance` 阈值仍由向量距离判断，
   两个尺度不会混在一起；
3. **外部服务会挂** —— 失败一律降级为"保持向量顺序取前 k 条"并记 warning。
   检索不该因为重排服务抖动而整体失败。但"降级"只对**报错**有效、对**挂住**无效，
   所以调用必须带超时（见 Reranker.timeout 与 chroma.yml 的 rerank_timeout）。
"""
import importlib.util
import os

from langchain_core.documents import Document

from utils.config_handler import chroma_conf
from utils.env import ensure_env_loaded
from utils.logger_handler import logger
from utils.retry import call_with_retry


class Reranker:
    """重排器。

    ⚠️ 配置**每次调用时现读**，不在 __init__ 里缓存。
    踩过坑：早先把 `enabled` 缓存在实例属性上，而 get_reranker() 又是懒加载单例，
    结果运行期改配置不生效 —— 评估脚本里三组配置跑出完全相同的结果，
    排查后才定位到是单例把第一组的开关状态缓存住了。
    现在配置是唯一权威，改了就生效（dict 查一次的开销可忽略）。
    """

    @property
    def enabled(self) -> bool:
        return bool(chroma_conf.get("rerank_enabled", False))

    @property
    def model(self) -> str:
        return chroma_conf.get("rerank_model", "gte-rerank-v2")

    @property
    def timeout(self) -> int:
        """重排请求的总超时（秒）。

        为什么要单独配一个：`config/model.yml` 的 request_timeout 只作用在
        openai 兼容客户端上（chat + embedding），重排走的是 dashscope 自己的 SDK。
        实测它默认 300 秒（DEFAULT_REQUEST_TIMEOUT_SECONDS），意味着重排服务卡住时
        用户要干等 5 分钟 —— 而下面那条"失败自动降级"只对**报错**生效，对**挂住**无效。
        """
        return int(chroma_conf.get("rerank_timeout", 30))

    def rerank(self, query: str, docs: list[Document], top_k: int) -> tuple[list[int], bool]:
        """按与 query 的相关性重排，返回被选中文档的**下标**（相关性降序）。

        返回 (下标列表, 是否真正执行了重排)。
        - 未启用、候选本来就不多于 top_k、或调用失败 -> 返回原顺序下标 + False；
        - 只返回下标而不是 Document，是为了让调用方用原列表取回文档，
          从而**保留下标对应的向量距离**（距离语义不被重排污染）。
        """
        if not docs:
            return [], False

        if not self.enabled or len(docs) <= top_k:
            return list(range(min(top_k, len(docs)))), False

        try:
            # 延迟导入：未启用重排时不必付出 dashscope 的导入成本
            from utils.progress import phase
            phase("重排检索资料")
            from dashscope import TextReRank

            def _call():
                response = TextReRank.call(
                    model=self.model,
                    query=query,
                    documents=[doc.page_content for doc in docs],
                    top_n=top_k,
                    return_documents=False,
                    api_key=os.getenv("DASHSCOPE_API_KEY"),
                    # 走的是 SDK 的**具名参数**链路（kwargs → _build_api_request
                    # 的 request_timeout → HttpRequest.timeout），不会污染请求体；
                    # 属未文档化的 plumbing，故有 tests/test_reranker.py 守着。
                    request_timeout=self.timeout,
                )
                if response.status_code != 200:
                    # 把状态码与消息一起抛出去，好让 utils.retry 能按消息分类：
                    # 限流（`Too many requests`）该退避重试；
                    # 额度用尽（`quota exhausted`）/ 模型不存在则该立即失败，等下去也没用。
                    raise RuntimeError(
                        f"status={response.status_code}, message={response.message}")

                return [item.index for item in response.output.results]

            picked = call_with_retry(
                _call,
                what=f"rerank({self.model})",
                retries=int(chroma_conf.get("rerank_retries", 2)),
                backoff=float(chroma_conf.get("retry_backoff", 2)),
            )
            return picked, True

        except Exception as e:
            logger.warning(f"[Reranker]重排失败，降级为向量顺序（{type(e).__name__}: {e}）")
            return list(range(min(top_k, len(docs)))), False


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """懒加载单例（与模型工厂一致：import 不产生副作用）"""
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


def rerank_health() -> str:
    """给界面侧栏用的重排健康状态。

    **完全不联网**（只用 importlib 查依赖是否可导入），所以启动零额外开销。

    存在的理由是一个查出来很贵、看起来无害的隐患：
    `from dashscope import TextReRank` 写在 try 块内，而 `dashscope` 一度
    **不在 requirements.txt 里** —— ImportError 是 Exception 的子类，会被下面那句
    "重排失败，降级为向量顺序"一起吞掉。于是一台照 README 装出来的干净环境会
    **静默**失去重排能力（实测块级 hit@3 从 12/12 掉回 58%），
    不报错、界面不显示、启动检查也不看 —— 正是本项目栽过两次的那类"静默质量回退"。
    """
    reranker = get_reranker()

    if not reranker.enabled:
        return "重排：未启用（检索结果按向量距离顺序取前 k 条）"

    if importlib.util.find_spec("dashscope") is None:
        return "重排：⚠️ 已启用但依赖 dashscope 未安装 —— 每次检索都会静默降级为向量顺序"

    return f"重排：{reranker.model}（超时 {reranker.timeout}s，失败自动降级）"



if __name__ == '__main__':
    ensure_env_loaded()
    from rag.vector_store import VectorStoreService

    store = VectorStoreService()
    reranker = get_reranker()
    print(f"重排启用: {reranker.enabled} | 模型: {reranker.model}")

    for question in ["吸力应该选多大才够用？", "防撞条缝隙的毛发怎么清理？"]:
        docs = [d for d, _ in store.search_with_distance(question, k=10)]
        picked, did = reranker.rerank(question, docs, top_k=3)
        print(f"\nQ: {question}  （实际重排: {did}）")
        for rank, index in enumerate(picked, 1):
            print(f"  [{rank}] {docs[index].page_content[:60]}")
