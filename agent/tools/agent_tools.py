import csv
import os
import re
import threading
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent.runtime import (EMPTY_REASON_NO_COVERAGE, EMPTY_REASON_NO_DOCS,
                           EMPTY_REASON_PHRASE, EMPTY_RETRIEVAL_MARKER,
                           NO_COVERAGE_MARKER)
from rag.rag_service import RagSummarizeService
from utils.config_handler import agent_conf, chroma_conf, model_conf
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

_rag_service: RagSummarizeService | None = None
_rag_signature = None
_rag_service_lock = threading.Lock()


def get_rag_service() -> RagSummarizeService:
    """懒加载 RAG 服务。

    旧版在模块顶层写 `rag = RagSummarizeService()`，意味着**只要 import 这个模块**
    就会创建向量库连接和 embedding 客户端（连带决定"用哪个目录的库"）。
    改成懒加载后，import 本模块零副作用，也更容易做单元测试。

    ⚠️ **必须加锁**（2026-09-21 实测踩到）：提示词允许"互不依赖的工具在同一轮并行调用"，
    两个 rag_summarize 并发进来时，第二个会在第一个尚未完成构造前也进入构造 ——
    两个 Chroma PersistentClient 互相踩踏（`AttributeError: bindings` / `KeyError: chroma_db`），
    结果是其中一个检索**静默失败**，报告流程悄悄丢一份资料。
    """
    global _rag_service, _rag_signature
    signature=(model_conf["chat_model_name"],model_conf["embedding_model_name"])
    if _rag_service is None or _rag_signature != signature:
        with _rag_service_lock:
            if _rag_service is None or _rag_signature != signature:
                _rag_service = RagSummarizeService()
                _rag_signature = signature
    return _rag_service


# 「资料不覆盖问题」的措辞表 —— **不可靠的兜底判据**，只在模型没按约定输出
# NO_COVERAGE_MARKER 时才用（比如提示词被误改）。
#
# 实测记录（2026-09-20，当日 178 次检索调用）：
#   - ② [无覆盖] 命中 56 次，③ 短语兜底命中 3 次，① 结构判据 30 次（全是评估用例）；
#   - 9 个短语里只有「未提及」「未涉及」真正命中过；
#   - 3 次命中里 1 次是**救场**（「扫地机器人的发明人是谁？」② 漏报、且那次重排降级，
#     是这层把它拦下来的），2 次是**误判**。
# 之所以保留，是防御"提示词契约失效"这一种情况，不是主判据。
# 判定优先级：doc_count==0（结构化） > NO_COVERAGE_MARKER（模型上报） > 本表（兜底）。
_NO_COVERAGE_PHRASES = (
    "未提供", "未涉及", "未提及", "未检索到", "未包含", "没有相关",
    "无法基于资料", "无法总结", "无法作出",
)

# ③ 短语兜底的两条触发条件（**必须同时成立**），依据是复现出来的真实样本：
#
#   误判样本（报告流程里模型自己拼的多主题检索词
#     「HEPA滤网更换周期 主刷更换保养 木地板清扫注意事项」）：
#       模型如实答上了 HEPA 与主刷两个主题，只有"木地板"没答上，
#       于是在 126 字答案的**第 113 字**、135 字答案的**第 122 字**（都在 90% 处）
#       写出"未提及" —— 这正是 prompts/rag_summarize.txt 第 6 条**要求**的写法
#       （"哪怕不完整、只能部分回答，就正常总结"）。
#     旧判据是"全文含短语"，于是把两个可用主题的完整回答整段丢掉，
#     还反过来命令模型"别检索了，告知用户没资料" → 报告静默缺内容。
#     （把检索词里的第三个主题去掉，③ 就不再触发 —— 确认它撞的是"部分覆盖"区间。）
#
#   救场样本（离题问题）：答案本身就是一句"资料没提到"，短语在开头、且很短。
#
# 所以区分"部分覆盖的如实说明"与"整题无资料"的依据是**位置 + 长度**，不是措辞。
# 窗口取 50 的依据（2026-09-21 用 5 个真实数据点校准，不是拍的）：
#   救场样本（整题无资料）：短语在 0~5 字    → 任何窗口都能接住
#   「防贼」误放行样本：    短语在 46 字     → 30 的窗口放过了它（回归），50 能接住
#   「部分覆盖」样本：      短语在 139~150 字 → 50 的窗口正确放行
# 样本只有 5 个，这个数是"当前证据下的最优"，不是定理 —— part 4 会持续记录
# （位置 + 长度），样本多了要回头复核。
_PHRASE_HEAD_WINDOW = 50    # 短语要落在回答开头这么多个字符以内
_PHRASE_MAX_ANSWER = 120    # 且答案要短到"基本就是一句覆盖声明"


def detect_phrase_fallback(answer: str) -> tuple[str | None, int, int]:
    """判断③短语兜底是否该触发。**纯函数，便于测试**。

    返回 (应当触发的短语 or None, 最早命中的短语位置, 答案长度)。
    位置与长度即使不触发也一并返回，是为了让调用方把它们打进日志 ——
    否则"这层到底误判没有"永远无法事后核对（今天有 2 次命中就因此拿不回原文）。
    """
    length = len(answer)
    hits = [(phrase, answer.find(phrase)) for phrase in _NO_COVERAGE_PHRASES]
    hits = [(phrase, pos) for phrase, pos in hits if pos >= 0]

    if not hits:
        return None, -1, length

    # 取**最早**出现的那次命中：只要有任何一个短语落在开头窗口里，就说明
    # 这段回答的立论是"资料没覆盖"，而不是"部分覆盖后如实补充说明"。
    phrase, pos = min(hits, key=lambda item: item[1])

    if pos <= _PHRASE_HEAD_WINDOW and length < _PHRASE_MAX_ANSWER:
        return phrase, pos, length

    return None, pos, length



def reference_month() -> str:
    """参考月份。

    mock 外部系统只覆盖 2025 年，直接返回真实月份会查不到任何记录，
    所以配置里用 demo_reference_month 覆盖；接入真实外部系统时删掉该配置项即可。
    """
    demo_month = agent_conf.get("demo_reference_month")
    if demo_month:
        return str(demo_month)

    return datetime.now().strftime("%Y-%m")


# --------------------------------------------------------------------- 检索工具

@tool(response_format="content_and_artifact", description=(
    "从扫地/扫拖机器人知识库中检索专业资料并总结成回答。"
    "入参 query 为贴合用户问题的核心检索词，纯文本字符串。"
    "使用场景：当回答需要产品专业信息（选购、故障排查、维护保养、环境适配、耗材等），"
    "而现有常识无法精准解答时，调用本工具获取专业内容。"
    f"若返回内容以 {EMPTY_RETRIEVAL_MARKER} 开头，表示知识库确实没有相关资料——"
    "此时严禁更换措辞重复调用本工具，应直接用已有信息作答，"
    "或如实告知用户知识库暂无相关资料。"
))
def rag_summarize(query: str, runtime: ToolRuntime) -> tuple[str, dict]:
    """检索 + 总结。

    response_format="content_and_artifact" 的含义：
    返回 (给模型看的正文, 附加产物)。artifact **不会进入模型上下文**，
    经 ToolMessage.artifact 传给上层，界面用它展示"这句话依据了哪些资料"。
    这样既能给用户看来源，又不会白占模型的 token。
    """
    # 【如何区分日志来源】这一层的日志要能分清**真实 agent 流量**与**评估脚本直接调用**，
    # 否则 ②/③ 的命中率分母会被评估流量稀释（实测某天 178 次 summarize 里 130 次来自脚本）。
    #
    # 做法：**看日志行位置**，不给工具加参数 —— agent 路径下，本层的日志一定夹在
    # `[tool monitor]执行工具：rag_summarize` 与其"调用成功"之间；直接 invoke 则没有这对包围行。
    # 实测该判据能区分历史上的 3 次命中（15:42 / 18:02 是 agent，17:15 是 direct）。
    #
    # ⚠️ 曾试过给本函数加 `runtime: ToolRuntime = None` 来显式标记来源，**结果是灾难**：
    # 带默认值会让 LangChain 认不出这是可注入的 runtime，于是去给 Callable 生成 JSON schema →
    # `PydanticInvalidForJsonSchema`，**agent 一调用就崩**；而且 `runtime` 会泄漏进模型可见的 schema。
    # 更刺眼的是当时 103 个单测全绿 —— 因为没有测试覆盖"把工具绑定给模型"这一步。
    # 现在 tests/test_tool_schema.py 专门守这条。

    raw = agent_conf.get("rag_raw_documents", False) and not (runtime.context or {}).get("report", False)
    with _rag_service_lock:
        context = runtime.context if runtime.context is not None else {}
        ref_start = context.get("next_ref", 1)
        context["next_ref"] = ref_start + int(chroma_conf["k"])
    result = get_rag_service().summarize(query, raw=raw, ref_start=ref_start)
    if raw and not result.is_empty:
        return result.answer, {"sources": [asdict(source) for source in result.sources]}

    if result.is_empty:
        # 第一道判据是结构化的：要么库是空的，要么召回到的东西全部超出距离阈值
        if result.retrieved:
            logger.warning(f"[rag_summarize]召回 {result.retrieved} 条但均超出距离阈值，"
                           f"最近一条 {result.best_distance:.4f}，query={query}")
        else:
            logger.warning(f"[rag_summarize]向量库中没有内容，query={query}")

        return (f"{EMPTY_RETRIEVAL_MARKER}知识库中没有与「{query}」相关的内容。"
                "不要更换措辞重复调用本工具，请直接告知用户知识库暂无相关资料。",
                {"sources": [], "empty_reason": EMPTY_REASON_NO_DOCS})

    # 第二道：让模型自己按**约定格式**声明"资料没覆盖"。
    # 这是提示词契约（prompts/rag_summarize.txt 第 6 条），属于模型主动上报，
    # 比"猜它用什么措辞"可靠得多。
    if result.answer.lstrip().startswith(NO_COVERAGE_MARKER):
        logger.info(f"[rag_summarize]模型标记资料未覆盖该问题，query={query}")
        return (f"{EMPTY_RETRIEVAL_MARKER}已检索到 {result.doc_count} 条资料，"
                "但它们均不覆盖该问题。不要更换措辞重复调用本工具，"
                "请直接告知用户知识库暂无相关资料。",
                {"sources": [], "empty_reason": EMPTY_REASON_NO_COVERAGE})

    # 第三道：短语兜底 —— **不可靠，仅作防御**（判据与实测依据见
    # _PHRASE_HEAD_WINDOW 上方的记录；只有"短语落在开头且答案很短"才触发）。
    phrase, pos, length = detect_phrase_fallback(result.answer)

    # 兜底命中时把**被丢弃的原文**记进日志：这层会把一个可用回答整段换成拒答指令，
    # 不留原文就事后无法判断它是救场还是误判（今天 3 次命中里有 2 次因为
    # 原文没记、会话又被删，已经永远无法复盘了）。
    discarded = " ".join(result.answer.split())[:200]

    if phrase is None:
        # 出现过短语但没达到触发条件 —— 单独记一行，让"新判据是否仍然成立"可观测。
        if pos >= 0:
            logger.info(f"[rag_summarize]短语兜底未触发（"
                        f"最早命中在第 {pos} 字、答案 {length} 字，不满足"
                        f"「开头 {_PHRASE_HEAD_WINDOW} 字内且 <{_PHRASE_MAX_ANSWER} 字」），"
                        f"保留回答，query={query}")
        return result.answer, {"sources": [asdict(source) for source in result.sources]}

    logger.warning(f"[rag_summarize]短语兜底命中（不可靠判据，命中=「{phrase}」"
                   f"位于第 {pos} 字，答案 {length} 字），query={query}\n"
                   f"  被丢弃的原文（前 200 字）：{discarded}")
    return (f"{EMPTY_RETRIEVAL_MARKER}已检索到 {result.doc_count} 条资料，"
            "但它们均不覆盖该问题。不要更换措辞重复调用本工具，"
            "请直接告知用户知识库暂无相关资料。",
            {"sources": [], "empty_reason": EMPTY_REASON_PHRASE})


# --------------------------------------------------------------------- 环境工具

@tool(description=(
    "获取指定城市的模拟天气与环境信息（非实时）（气温、湿度、风力、AQI、降雨概率），纯字符串返回。"
    "入参 city 为标准城市名称。"
    "使用场景：需要判断某城市环境是否适配扫地/扫拖机器人使用，"
    "或用户问题涉及天气、湿度对机器人使用的影响时。"
    "若需要知道用户当前所在城市，请先调用 get_user_location。"
))
def get_weather(city: str) -> str:
    return f"[模拟数据，非实时] 城市{city}天气为晴天，气温26摄氏度，空气湿度50%，南风1级，AQI21，最近6小时降雨概率极低"


@tool(description=(
    "获取演示城市名称（模拟位置，不代表当前用户实际位置），纯字符串返回，无需入参。"
    "使用场景：需要用户地理位置时，或需要为 get_weather 提供 city 参数时。"
))
def get_user_location() -> str:
    return "深圳（模拟位置）"


# ------------------------------------------------------- 身份与时间（会话注入）

@tool(description=(
    "获取当前登录会话的用户ID（数字字符串，如 1001），无需入参。"
    "使用场景：需要基于「当前用户的ID」检索其专属使用记录、生成个性化使用报告时。"
    "该ID由会话身份决定，调用方无法指定，也无法获取他人ID；"
    "严禁自行编造、猜测或推断用户ID，必须以此工具返回值为准。"
))
def get_user_id(runtime: ToolRuntime) -> str:
    """从 runtime.context 读会话身份。

    刻意不做成"模型可选"的参数：身份是会话属性，不是模型能决定的东西。
    旧版这里用 random.choice 随机返回一个用户，等于每次问"我的报告"
    都可能拿到别人的数据（真实系统里这就是越权取数）。
    """
    user_id = (runtime.context or {}).get("user_id")

    if not user_id:
        logger.error("[get_user_id]会话上下文中没有 user_id，调用方未注入身份")
        return ""

    return str(user_id)


@tool(description=(
    "获取参考月份，格式固定为 YYYY-MM（如 2025-12），纯字符串返回，无需入参。"
    "使用场景：用户未明确指定月份，且需要按「当前月份」检索用户记录或生成个性化报告时。"
))
def get_current_month() -> str:
    return reference_month()


# --------------------------------------------------------------------- 外部数据

@lru_cache(maxsize=1)
def load_external_records() -> dict[str, dict[str, dict[str, str]]]:
    """读取 mock 外部数据，组织为 {user_id: {month: {字段: 值}}}

    用 lru_cache 做进程内缓存，取代原来的「模块级可变全局 + None 哨兵」：
    调用方拿到的是同一份只读数据，但不再有一个可以被任何地方改掉的全局变量
    （缓存生命周期与调用绑定，也更方便测试时用 cache_clear() 重置）。

    用 csv.DictReader 而不是手写 line.split(",")：
    字段里一旦出现逗号，手写切分不会报错、只会**静默错位**（数据串列到别的列），
    这类问题极难发现。当前样本数据恰好没有逗号，属于运气好。
    """
    path = get_abs_path(agent_conf["external_data_path"])
    if not os.path.exists(path):
        raise FileNotFoundError(f"外部数据文件{path}不存在")

    records: dict[str, dict[str, dict[str, str]]] = {}

    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            user_id = (row.get("用户ID") or "").strip()
            month = (row.get("时间") or "").strip()

            if not user_id or not month:
                logger.warning(f"[外部数据]跳过字段缺失的行：{row}")
                continue

            records.setdefault(user_id, {})[month] = {
                "特征": (row.get("特征") or "").strip(),
                "效率": (row.get("清洁效率") or "").strip(),
                "耗材": (row.get("耗材") or "").strip(),
                "对比": (row.get("对比") or "").strip(),
            }

    logger.info(f"[外部数据]已加载 {len(records)} 个用户 / "
                f"{sum(len(months) for months in records.values())} 条记录")

    return records


@tool(description=(
    "从外部系统获取指定用户在指定月份的扫地/扫拖机器人使用记录，"
    "以多行「字段：值」文本返回（字段为 特征、效率、耗材、对比）；未检索到时返回空字符串。"
    "身份由系统注入，模型不能指定用户。唯一入参 month 严格遵循 YYYY-MM 格式。"
    "使用场景：需要为用户生成个人使用报告时——调用前应先通过 get_user_id 和 get_current_month "
    "取得入参（用户明确指定了月份则用用户给的），不要在入参缺失时凭猜测调用。"
))
def fetch_external_data(month: str, runtime: ToolRuntime) -> str:
    user_id = str((runtime.context or {}).get("user_id") or "")
    if not user_id:
        raise PermissionError("缺少已验证的会话身份")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("月份必须为 YYYY-MM")
    record = load_external_records().get(user_id, {}).get(month)

    if not record:
        logger.warning(f"[fetch_external_data]未能检索到用户：{user_id}在{month}的使用记录数据")
        return ""

    # 返回多行「键：值」而不是单行 JSON：
    # 模型读多行中文比读一长串带转义的 JSON 更准，也不会把 \n 当成字面量。
    # 同时让本函数的 -> str 标注变成真的（旧版标注 str 却返回 dict，靠 LangChain 兜底序列化）。
    return "\n".join(f"{key}：{value}" for key, value in record.items())


@tool(description=(
    "进入「报告生成模式」。无需入参，调用后系统会为后续回答追加报告写作规范"
    "（Markdown 格式、固定标题、需结合使用情况给出建议），并返回一句确认文本。"
    "使用场景：仅当明确识别出用户意图是「生成/查询个人使用报告」时调用"
    "（如「生成我的6月使用报告」「查一下我的机器人使用记录」）；"
    "用户仅咨询使用问题、故障排查、天气适配等非报告类需求时，严禁调用。"
    "它是报告流程的前置必需步骤——未调用它不得生成报告。"
))
def enter_report_mode() -> str:
    """报告模式开关。

    旧版这个工具叫 fill_context_for_report，是个"什么都不做、只为了让中间件
    设置一个 flag"的空函数，语义完全不自解释。现在它就是一个名副其实的模式开关：
    名字说明了它做什么，返回值如实说明"接下来会怎样"，中间件据此追加报告规范。
    """
    return "已进入报告生成模式：后续回答将遵循报告写作规范。"
