"""
向量库服务：负责知识库的加载（全量重建 / 按文件增量更新 / 直接加载）与检索

与旧版的三个关键差异：
1. persist_directory 用 get_abs_path 锚定到项目根，不再交给 Chroma 按 cwd 解析；
2. 去重清单从项目根的 md5.text 换成 chroma_db/kb_meta.json，除文件哈希外还记录切分参数
   与 embedding 模型，参数一变旧库自动失效；
3. 建库入口不只是 __main__ 的自测，应用启动时会调用 load_document() 确保库可用。
"""
import json
import hashlib
from uuid import uuid4
from collections import Counter
from utils.index_lock import index_lock
import os
import re
import sqlite3
from dataclasses import dataclass, field

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from model.factory import get_embed_model
from rag.reranker import get_reranker
from utils.config_handler import chroma_conf, model_conf
from utils.file_handler import (file_extension, get_file_md5_hex,
                                listdir_with_allowed_type, pdf_loader, txt_loader)
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 切分逻辑一旦修改（比如改了章节识别方式），把这个数字 +1，旧向量库会自动全量重建
SPLITTER_VERSION = 3

META_FILE_NAME = "kb_meta.json"

# Markdown 标题行（最多 3 个前导空格 + 1~6 个 # + 空格 + 标题内容）
_MARKDOWN_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+.*$", re.M)

# 「编号条目」的起始位置，例如 "12. xxx" 或 "3. **什么是…**"
_ENTRY_BOUNDARY = re.compile(r"\n(?=\d+[.、]\s*\S)")


def strip_markdown_headings(text: str) -> str:
    """剥掉 Markdown 标题行（文档标题与章节标题）后再切分。

    为什么必须剥 —— 这是实测出来的一个真实召回缺口：
    本库的文档常在开头写一行很长的元信息标题，例如
        「# 扫地机器人+扫拖一体机器人 维护保养200条（纯保养维度，分通用基础/扫地专属/
          扫拖一体拖地专属/耗材专项/环境适配/长期存放/故障预防，覆盖全机型日常维护）」
    它会被切进第一个块，**能占掉整块近一半的字符**。
    而 embedding 是把整块压成一个向量 —— 一半的字在讲"这份文档是什么"，
    这个块对所有**具体**问题的相似度都会被稀释。

    实测（2026-09-20）：问题「防撞条缝隙的毛发怎么清理？」的答案就在这样一个块里，
    剥标题前该块向量距离 **0.9414**、全库排 **第 32 位**，并被 `max_distance=0.9`
    直接过滤掉 —— 也就是说它**连候选池都没进过**，rerank 再强也救不回来。
    剥掉标题后该块语义变纯，距离降到 0.8409、排到第 1 位。

    标题里确实可能藏着关键词，但那种"靠文档标题才能答上"的问题在本场景几乎不存在；
    真实的问答都落在正文条目上，所以剥掉的收益远大于损失（已用评估集验证）。
    """
    return _MARKDOWN_HEADING.sub("", text)


def normalize_entry_boundaries(text: str) -> str:
    """把「编号条目」之间的单换行升级成空行，让切分器在**条目边界**优先断开。

    为什么必须这么做 —— 实测出来的第二个真实召回缺口：
    本库文档是「问句一行 + 回答一行」的条目式结构，例如
        3. **什么是 dToF 导航技术？**
        - 直接飞行时间测距(direct Time-of-Flight)……
    而 `RecursiveCharacterTextSplitter` 用 `\\n` 当分隔符、按 200 字打包，
    **很可能正好切在"问句"和"它的回答"之间**。实测就抓到一个这样的块：
    长度 164 字，结尾停在「3. 什么是 dToF 导航技术？」这句问句上，
    **答案被切进了下一块** —— 结果这个块"带着一个它回答不了的问题"，
    检索命中它也没用（关键词能匹配上问句，但答案不在）。

    做法：把条目间换行替换成空行。这样切分器的第一优先级分隔符 `\\n\\n`
    就落在条目边界上，**每个条目（问+答）成为不可拆的原子单元**。
    好处是不必启用 `is_separator_regex`（正则模式下 `"."` 这类分隔符语义会变，
    风险大），配置里的 `separators` 一个字都不用改。
    """
    return _ENTRY_BOUNDARY.sub("\n\n", text)


@dataclass
class RetrievalOutcome:
    """一次检索的完整结果（含诊断信息）

    `hits` 是**最终**送去生成上下文的那几条（已过阈值、已重排、已截断到 k）；
    另外几个字段用于日志与调参，不参与业务判断。
    """

    hits: list[tuple[Document, float]] = field(default_factory=list)
    recalled: int = 0                 # 粗召回条数（阈值过滤之前）
    best_distance: float | None = None   # 粗召回里最相似那条的距离
    reranked: bool = False            # 是否真正执行了重排（False 含"降级"）


def find_stale_segment_dirs(persist_directory: str, registered_ids: set[str]) -> list[str]:
    """找出可以安全删除的段目录 —— **纯函数，便于测试**。

    Chroma 会在持久化目录下为每个"段"建一个 UUID 目录。而全量重建时我们调的是
    `reset_collection()`，它**只清数据、不删段目录** —— 于是每重建一次就留下一个
    空目录（本项目今天重建十几次，就积了 5 个）。

    删除条件刻意收得很紧，**两个条件同时满足才返回**：
      1. 目录名**不在** `registered_ids`（即当前 `segments` 表里查不到）；
      2. 目录**是空的**。
    这样即使我对 Chroma 内部约定的理解有偏差，也**不可能删掉任何有数据的目录**。

    返回的是目录的绝对路径列表。

    ⚠️ `registered_ids` 传**空集**在这里等价于"所有目录都可删"（条件 1 全部放行）。
    调用方若拿不到登记信息，必须**不要调用本函数**（见 VectorStoreService._cleanup_stale_segments）。
    """
    if not os.path.isdir(persist_directory):
        return []

    stale = []
    for name in os.listdir(persist_directory):
        path = os.path.join(persist_directory, name)
        if not os.path.isdir(path) or name in registered_ids:
            continue
        if os.listdir(path):          # 非空 -> 一律不碰
            continue
        stale.append(path)
    return stale


class VectorStoreService:
    def __init__(self):
        # 关键：锚定项目根，避免"在哪个目录启动就在哪里建一个新库"
        self.persist_directory = get_abs_path(chroma_conf["persist_directory"])
        self.meta_path = os.path.join(self.persist_directory, META_FILE_NAME)
        self.data_path = get_abs_path(chroma_conf["data_path"])

        self.embedding_name = model_conf["embedding_model_name"]
        self.vector_store = Chroma(
            collection_name=self._load_meta().get("active_collection", chroma_conf["collection_name"]),
            embedding_function=get_embed_model(),
            persist_directory=self.persist_directory,
        )

        self.spliter = RecursiveCharacterTextSplitter(
            chunk_size=chroma_conf["chunk_size"],
            chunk_overlap=chroma_conf["chunk_overlap"],
            separators=chroma_conf["separators"],
            length_function=len,
        )

    # ------------------------------------------------------------------ 检索

    def get_retriever(self):
        return self.vector_store.as_retriever(search_kwargs={"k": chroma_conf["k"]})

    def search_with_distance(self, query: str, k: int | None = None) -> list[tuple[Document, float]]:
        """**原始**相似度检索：返回 [(文档, 距离)]，距离越小越相似。

        只做向量检索，不做阈值过滤、不做重排 —— 留给诊断脚本与评估集使用。
        业务路径请用 retrieve()，那才是完整的检索流水线。
        """
        self._refresh_active()
        return self.vector_store.similarity_search_with_score(query, k=k or chroma_conf["k"])

    def retrieve(self, query: str) -> RetrievalOutcome:
        """完整的检索流水线：粗召回 →（可选）距离阈值过滤 → 重排 → 截断到 k

        重排默认开启（chroma.yml: rerank_enabled）。实测块级 hit@3 从 58% 提到 83%。
        注意重排**只改顺序、不改距离**：距离仍是该文档在向量检索阶段的值，
        因此 best_distance 的语义（以及可选的 max_distance 阈值）不受重排影响。

        **距离阈值当前是关闭的**（chroma.yml: `max_distance:` 留空）。
        阈值本身仍支持，填上数值即生效 —— 见配置里的决策记录。
        关闭后"没资料"的判定完全走模型侧的 `[无覆盖]` 标记（见 agent_tools）。
        """
        top_k = chroma_conf["k"]
        max_distance = chroma_conf.get("max_distance")
        reranker = get_reranker()

        fetch_k = chroma_conf.get("rerank_fetch_k", top_k) if reranker.enabled else top_k
        recalled_hits = self.search_with_distance(query, k=fetch_k)

        if not recalled_hits:
            return RetrievalOutcome()

        best_distance = min(score for _, score in recalled_hits)

        kept = ([(doc, score) for doc, score in recalled_hits if score <= max_distance]
                if max_distance is not None else list(recalled_hits))

        if not kept:
            return RetrievalOutcome(recalled=len(recalled_hits), best_distance=best_distance)

        picked, did_rerank = reranker.rerank(query, [doc for doc, _ in kept], top_k=top_k)

        return RetrievalOutcome(
            hits=[kept[index] for index in picked],
            recalled=len(recalled_hits),
            best_distance=best_distance,
            reranked=did_rerank,
        )

    def count(self) -> int:
        """库内向量块数量。

        langchain_chroma 没有公开的 count 方法。优先用公开 get() 取 ids 数量；
        数据量大时退化为 chromadb 原生的 collection.count()（chroma 自己的公开 API，
        只是需要经 langchain 包装器的 _collection 访问，故放在兜底分支里）。
        """
        try:
            return len(self.vector_store.get(include=[])["ids"])
        except Exception:
            try:
                return self.vector_store._collection.count()
            except Exception as e:
                logger.error(f"[知识库]统计向量块数量失败：{str(e)}")
                return 0

    # -------------------------------------------------------------- 元数据管理

    def _catalog_hash(self):
        path = os.path.join(self.data_path, ".catalog.json")
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def _current_params(self) -> dict:
        """会直接影响向量内容与语义的参数集合，任一变化都必须重建"""
        return {
            "splitter_version": SPLITTER_VERSION,
            "catalog_hash": self._catalog_hash(),
            "embedding_model": model_conf["embedding_model_name"],
            "collection_name": chroma_conf["collection_name"],
            "chunk_size": chroma_conf["chunk_size"],
            "chunk_overlap": chroma_conf["chunk_overlap"],
            "separators": list(chroma_conf["separators"]),
            "allow_knowledge_file_type": list(chroma_conf["allow_knowledge_file_type"]),
        }

    def _load_meta(self) -> dict:
        if not os.path.exists(self.meta_path):
            return {}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[知识库]{META_FILE_NAME} 解析失败，将按全量重建处理：{str(e)}")
            return {}

    def _save_meta(self, files: dict):
        """写入元数据。必须在向量写入成功之后调用——meta 是"库已完整"的唯一凭据，
        中途失败时不写 meta，下次启动就会自动重试。"""
        os.makedirs(self.persist_directory, exist_ok=True)
        meta = {
            "params": self._current_params(),
            "files": files,
            "active_collection": self.vector_store._collection.name,
        }
        temporary = self.meta_path + "." + uuid4().hex + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.meta_path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def _db_is_complete(self) -> bool:
        """判断现有向量库是否真的可用。

        三道检查，缺一不可：
        1. `kb_meta.json` 存在；
        2. `chroma.sqlite3` 存在；
        3. **库里确实有向量**。

        第 3 条是 2026-09-20 踩坑后补的，很关键：
        全量重建是「先 `_drop_all()` 再逐文件写」，如果中途失败（例如嵌入接口限额报错），
        就会留下 **"kb_meta.json 还在、向量却是 0 条" 的半成品**。
        只查前两条会把这个半成品判为"完整"，应用遂**直接加载一个空库**，
        然后对任何问题都答"知识库暂无相关资料" —— **一个静默的、极难定位的故障**。
        """
        meta_exists = os.path.exists(self.meta_path)
        db_exists = os.path.exists(os.path.join(self.persist_directory, "chroma.sqlite3"))
        if not (meta_exists and db_exists):
            return False

        try:
            manifest = self._load_meta().get("files", {})
            expected = {path: info["chunks"] for path, info in manifest.items() if info.get("chunks", 0)}
            actual = Counter(m.get("source") for m in self.vector_store.get(include=["metadatas"])["metadatas"])
            return bool(expected) and dict(actual) == expected
        except Exception as e:
            # 库文件损坏/集合不存在等都算"不可用"，交给上层走全量重建
            logger.warning(f"[知识库]读取现有向量库失败，将重建：{type(e).__name__}: {e}")
            return False

    # -------------------------------------------------------------- 文件与索引

    def _scan_files(self) -> dict:
        """扫描 data/ 目录，返回 {绝对路径: {"hash": md5, "size": 字节数}}"""
        allowed_paths = listdir_with_allowed_type(
            self.data_path, tuple(chroma_conf["allow_knowledge_file_type"])
        )

        result = {}
        for path in allowed_paths:
            md5_hex = get_file_md5_hex(path)
            if not md5_hex:
                logger.warning(f"[知识库]无法计算 {path} 的哈希，跳过该文件")
                continue
            result[path] = {"hash": md5_hex, "size": os.path.getsize(path)}
        return result

    def _read_documents(self, path: str) -> list[Document]:
        # 后缀统一走 file_extension（小写归一化）—— 原来直接 endswith("txt")，
        # 大写后缀的文件会在扫描阶段就被漏掉、在这里又被静默跳过（见 utils/file_handler.py）
        extension = file_extension(path)
        if extension == "txt":
            return txt_loader(path)
        if extension == "pdf":
            return pdf_loader(path)
        return []

    def _index_file(self, path: str) -> int:
        """把单个文件切分后写入向量库，返回写入的块数（0 表示该文件无有效内容）"""
        documents = self._read_documents(path)
        catalog_path = os.path.join(self.data_path, ".catalog.json")
        if os.path.exists(catalog_path):
            with open(catalog_path, encoding="utf-8") as f:
                profile = json.load(f).get(os.path.basename(path), {})
            for document in documents:
                document.metadata.update(profile)

        if not documents:
            logger.warning(f"[知识库]{path} 内没有有效文本内容，跳过")
            return 0

        # 切分前两步预处理（都是实测出来的召回缺口，详见各自函数注释）：
        #   1. 剥掉 Markdown 标题行 —— 标题会稀释所在块的语义；
        #   2. 把条目间换行升级成空行 —— 防止"问句"和"它的回答"被切到两个块里。
        for document in documents:
            text = strip_markdown_headings(document.page_content)
            document.page_content = normalize_entry_boundaries(text)

        split_documents = self.spliter.split_documents(documents)

        # 预处理后可能出现纯空白块，过滤掉避免写入无意义的向量
        split_documents = [doc for doc in split_documents if doc.page_content.strip()]

        if not split_documents:
            logger.warning(f"[知识库]{path} 分片后没有有效文本内容，跳过")
            return 0

        ids = []
        for index, doc in enumerate(split_documents):
            identity = json.dumps([path, index, doc.page_content], ensure_ascii=False)
            chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            doc.metadata["chunk_id"] = chunk_id
            ids.append(chunk_id)
        self.vector_store.add_documents(split_documents, ids=ids)
        logger.info(f"[知识库]{path} 索引成功，写入 {len(split_documents)} 块")
        return len(split_documents)

    def _delete_by_source(self, path: str):
        """按 source 元数据删除某个文件对应的全部向量。

        删除失败必须显式抛错：静默忽略会让新旧向量同时存在，检索时出现重复内容。
        """
        try:
            self.vector_store.delete(where={"source": path})
        except Exception as e:
            raise RuntimeError(f"删除 {path} 的旧向量失败，为避免新旧向量混杂已中止：{e}") from e

    def _drop_all(self):
        """清空整个集合（全量重建前调用），失败同样显式抛错"""
        try:
            self.vector_store.reset_collection()
        except Exception as e:
            raise RuntimeError(f"清空旧向量库失败，为避免新旧向量混杂已中止：{e}") from e

    def _registered_segment_ids(self) -> set[str] | None:
        """读 `segments` 表，拿到"当前登记在册的段 id"。

        直接读 Chroma 的 sqlite 是不得已 —— 它没有公开"列出段目录"的 API。
        这里**只读不写**。

        ⚠️ 读不到时返回 **None**（而不是空集），表示"无法判断哪些段在用"。
        这个区分是 2026-09-20 复现出来的一个真 fail-open 的补丁：原先失败时返回空集，
        而空集在 `find_stale_segment_dirs` 里的语义是"**没有任何段登记在册**" ——
        那等于把删除范围放到**最大**，连正在使用的空段目录都会被判为可删
        （实测：传 registered_ids=set() 时，已登记的空段目录出现在待删列表里）。
        "安全地什么都不删"与"宽松地什么都能删"是两个相反方向，不能都写成 set()。
        """
        db_path = os.path.join(self.persist_directory, "chroma.sqlite3")
        if not os.path.exists(db_path):
            logger.warning(f"[知识库]未找到 {db_path}，无法判断段登记情况，跳过残留目录清理")
            return None
        try:
            with sqlite3.connect(db_path) as conn:
                return {str(row[0]) for row in conn.execute("select id from segments")}
        except Exception as e:
            logger.warning(f"[知识库]读取 segments 表失败，跳过残留目录清理：{e}")
            return None

    def _cleanup_stale_segments(self) -> int:
        """清理"未登记且为空"的段目录（全量重建会不断留下这种空目录）

        为什么会有残留：全量重建走的是 `reset_collection()`，它只清数据、不删段目录。
        判定与删除见 find_stale_segment_dirs()；这里再叠两层保险 ——
        1. **拿不到登记集（None 或空集）就整段跳过**，一个目录都不碰；
        2. 用 `os.rmdir` 而不是 `rmtree`，**目录非空时会直接报错**，删不掉任何有内容的东西。
        """
        registered = self._registered_segment_ids()

        # None = 读失败；空集 = 一个段都没登记（正常重建后至少应有 1 个）。
        # 两种情况都"宁可留残留"：删错的代价是毁库，留残留的代价只是几个空目录。
        if not registered:
            return 0

        stale = find_stale_segment_dirs(self.persist_directory, registered)

        removed = 0
        for path in stale:
            try:
                os.rmdir(path)
                removed += 1
            except OSError as e:
                # 清理残留属于"锦上添花"，失败不应影响建库结果
                logger.warning(f"[知识库]删除残留段目录失败（不影响使用）：{path}（{e}）")

        if removed:
            logger.info(f"[知识库]已清理 {removed} 个残留段目录")
        return removed

    # ------------------------------------------------------------------ 主入口

    def _refresh_active(self):
        name = self._load_meta().get("active_collection", chroma_conf["collection_name"])
        if self.vector_store._collection.name != name or self.embedding_name != model_conf["embedding_model_name"]:
            self.embedding_name = model_conf["embedding_model_name"]
            self.vector_store = Chroma(collection_name=name, embedding_function=get_embed_model(),
                                       persist_directory=self.persist_directory)

    def load_document(self, force: bool = False) -> str:
        with index_lock(os.path.join(self.persist_directory, "build.lock")):
            return self._build_locked(force)

    def _build_locked(self, force: bool) -> str:
        self._refresh_active()
        build_params = self._current_params()
        current = self._scan_files()
        old = self._load_meta()
        previous = old.get("files", {})
        old_params = dict(old.get("params") or {})
        old_params.setdefault("catalog_hash", "")
        rebuild = force or old_params != self._current_params() or not self._db_is_complete()
        changed = {path for path, info in current.items()
                   if path not in previous or previous[path].get("hash") != info["hash"]}
        removed = set(previous) - set(current)
        if not rebuild and not changed and not removed:
            return f"内容未变化，直接加载：{self.count()} 块"
        if not current:
            raise ValueError("未找到有效知识文件，保留旧集合")
        active = self.vector_store
        staging = Chroma(collection_name=chroma_conf["collection_name"] + "_" + uuid4().hex,
                         embedding_function=get_embed_model(), persist_directory=self.persist_directory)
        self.vector_store = staging
        try:
            manifest = {}
            for path, info in current.items():
                if not rebuild and path not in changed:
                    # 复用已有 embedding；分批复制，不发模型请求。
                    chunks = 0
                    while chunks < previous[path]["chunks"]:
                        batch = active.get(where={"source": path}, limit=256, offset=chunks,
                                           include=["documents", "metadatas", "embeddings"])
                        if not batch["ids"]:
                            break
                        staging._collection.upsert(ids=batch["ids"], documents=batch["documents"],
                            metadatas=batch["metadatas"], embeddings=batch["embeddings"])
                        chunks += len(batch["ids"])
                else:
                    chunks = self._index_file(path)
                manifest[path] = dict(info, chunks=chunks)
            if not sum(item["chunks"] for item in manifest.values()):
                raise ValueError("知识文件没有可索引文本，保留旧集合")
            if self._scan_files() != current or self._current_params() != build_params:
                raise RuntimeError("构建期间源文件发生变化，未发布，请重试")
            actual = Counter(m.get("source") for m in staging.get(include=["metadatas"])["metadatas"])
            expected = {path: info["chunks"] for path, info in manifest.items() if info["chunks"]}
            if dict(actual) != expected:
                raise RuntimeError("新集合完整性校验失败，未发布")
            self._save_meta(manifest)  # 原子发布；旧集合保留供正在执行的检索读取。
        except BaseException:
            self.vector_store = active
            try:
                staging.delete_collection()
            except Exception:
                logger.exception("清理未发布集合失败")
            raise
        mode = "全量重建" if rebuild else "增量更新"
        return f"{mode}完成：{len(manifest)} 个文件，{sum(v['chunks'] for v in manifest.values())} 块"


if __name__ == '__main__':
    import time

    vs = VectorStoreService()
    start = time.time()
    print(vs.load_document())
    print(f"耗时 {time.time() - start:.1f}s，库内 {vs.count()} 块")

    print("-" * 20)
    for r in vs.get_retriever().invoke("迷路"):
        print(r.metadata.get("source", "?"))
        print(r.page_content[:60])
        print("-" * 20)
