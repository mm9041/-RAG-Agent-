"""
Agent 运行期约定：上下文类型定义 + 跨模块共享的常量

为什么要有这个模块：
1. **上下文要类型化**。传裸 dict 给 create_agent 时，没有 context_schema 会导致
   LangChain 按 None 去序列化 context，每次调用都打 Pydantic 警告（实测）；
   有了 TypedDict，key 拼错也能在类型检查阶段发现，而不是运行时静默拿到 None。
2. **空检索的哨兵串必须由工具与中间件共用一个常量**，否则一个改了一个没改，
   短路逻辑会静默失效——这类"两边字符串对不上"的 bug 极难排查。
"""
from typing import TypedDict

import warnings

# ---- 屏蔽 LangChain 上游的 Pydantic 序列化噪音 -------------------------------
# 现象：只要「声明了 ToolRuntime 参数的工具」被调用且本轮传了非空 context，
#   每次都会打两条 UserWarning: Pydantic serializer warnings:
#     PydanticSerializationUnexpectedValue(Expected `none` ... field_name='context')
# 实测归因（2026-09-19）：
#   工具无 ToolRuntime 参数                      -> 0 条
#   工具有 ToolRuntime 参数 + 传 context         -> 2 条（声明 context_schema 也挡不住）
#   工具有 ToolRuntime 参数 + 不传 context       -> 0 条
# 根因在 langchain 内部：Runtime 的 context 字段是泛型 ContextT，默认 None，
# 序列化真实 dict 时类型不匹配。属上游噪音，不影响功能。
# 只屏蔽这一条精确消息（warnings 的 message 参数按正则前缀匹配），
# 不用宽泛的 category 过滤，以免掩盖其它真正需要注意的警告。
warnings.filterwarnings(
    "ignore",
    message=r"Pydantic serializer warnings",
    category=UserWarning,
)

# 另注：工具里读 context 必须写成 (runtime.context or {})，因为当调用方
# 完全不传 context 时，runtime.context 是 None 而不是空 dict（实测会 AttributeError）。


class AgentContext(TypedDict, total=False):
    """Agent 的 runtime.context

    total=False：所有字段都可选，便于按场景只传需要的部分。
    """

    # 身份：由会话注入（Web 层 / 鉴权层），模型无法自行决定——这是安全边界
    refresh_chat_model: bool
    stream_response: bool
    next_ref: int
    device_model: str
    washable: str
    cancel_token: str
    model_call_total: int
    deadline: float
    user_id: str

    # 报告生成场景标记：中间件在 enter_report_mode 被调用后置位，
    # dynamic_prompt 据此把「报告生成规范」追加到系统提示词后面
    report: bool

    # 本轮「连续空检索」次数：用于短路模型换措辞反复重试（每轮调用时重置）
    rag_empty_count: int

    # 本轮已执行的工具调用次数：总数 + 按工具名，代码级预算用（每轮重置）
    tool_call_total: int
    tool_calls_by_name: dict[str, int]


# ---- 工具名常量（中间件按名字识别，避免到处写裸字符串）------------------------

# 检索工具
RAG_TOOL_NAME = "rag_summarize"

# 报告模式开关工具
REPORT_MODE_TOOL = "enter_report_mode"

# 外部使用记录取数工具
EXTERNAL_DATA_TOOL = "fetch_external_data"

# 空检索哨兵串：工具在"没检索到"时用它开头，中间件据此计数
EMPTY_RETRIEVAL_MARKER = "[检索为空]"

# 「资料未覆盖问题」标记：由 prompts/rag_summarize.txt 第 6 条约定模型在回答开头输出，
# rag_summarize 工具据此判定"检索到了东西、但资料答不上这个问题"。
#
# ⚠️ 这个字符串是**跨文件的契约**：改这里必须同步改提示词文件，
#    否则判定会静默失效（表现为：明明没覆盖，却被当成正常回答）。
NO_COVERAGE_MARKER = "[无覆盖]"

# 同一轮内允许的空检索次数上限，超过后直接短路掉后续检索调用
MAX_CONSECUTIVE_EMPTY_RETRIEVALS = 2

# ---- 「检索为空」的三种原因（工具→中间件的契约）------------------------------
#
# 为什么要把原因带出来：中间件原本只看"内容是否以 EMPTY_RETRIEVAL_MARKER 开头"来累加
# rag_empty_count，于是**不可靠的短语兜底层也会计数** —— 它误判两次就把整轮检索停用。
# 现在按原因区分：结构化判据（①②）计数，兜底启发式（③）不计数。

# ① 库里确实没有内容（doc_count == 0）
EMPTY_REASON_NO_DOCS = "no_docs"

# ② 模型自己按约定上报「资料不覆盖」（可靠，是提示词契约）
EMPTY_REASON_NO_COVERAGE = "no_coverage"

# ③ 短语兜底：靠措辞猜，**已知不可靠**，只在②失效时作防御
EMPTY_REASON_PHRASE = "phrase_fallback"

# ---- 工具调用预算（代码兜底，不靠提示词劝）------------------------------------
#
# 旧做法是把"累计 5 次工具调用就停"写在 main_prompt.txt 里，全项目**没有任何代码**
# 执行它 —— 而同项目自己的结论是"只靠提示词劝是没用的，模型不会听"。
#
# 数字依据 2026-09-20 实测，不凭感觉定：
#   报告流程的固定链路就要 **4 次**（get_user_id → get_current_month
#   → enter_report_mode → fetch_external_data，前两个由提示词第 4 条强制），
#   之后模型还要按主题做 1~3 次检索 → **合法上界 7 次**。
#   所以"5 次"恰好等于"固定 4 次 + 只剩 1 次检索"，**零余量**，
#   会把报告流程自己掐断（实测报告回合普遍落在 5~7 次）。
#   总预算取 10 = 合法上界 7 + 一次重试 + 一次额外主题检索的余量。
MAX_TOOL_CALLS_PER_TURN = 10

# 单个工具的上限：真正会跑飞的是检索循环（尤其它把多个主题拼成一次检索时），
# 所以给 rag_summarize 单独收紧；fetch_external_data 实测只调 1 次，给 2 次余量。
# 没列进来的工具只受总预算约束。
MAX_TOOL_CALLS_PER_TOOL: dict[str, int] = {
    RAG_TOOL_NAME: 4,
    EXTERNAL_DATA_TOOL: 2,
}

# 预算耗尽时工具返回内容的开头标记（界面与测试据此识别，语义同 EMPTY_RETRIEVAL_MARKER）
TOOL_BUDGET_MARKER = "[工具预算已用尽]"


def is_shortcircuit_worthy_empty(artifact) -> bool:
    """这次"检索为空"要不要计入短路计数。**纯函数，便于测试**。

    ①（库里没内容）与 ②（模型自己上报 [无覆盖]）是可靠判据 → 计入；
    ③（短语兜底）是**已知会误判**的启发式 —— 它撞的是"部分覆盖"这种形态
    （资料答上了一半，模型如实说另一半未提及），让它计数等于
    "两次猜测就把整轮检索停用"。

    拿不到 reason（工具改动过、或走了异常路径）时**按可靠处理**：
    宁可保留原有的拦截能力，也不要在信息缺失时悄悄放宽一道安全闸门。
    """
    reason = artifact.get("empty_reason") if isinstance(artifact, dict) else None
    return reason != EMPTY_REASON_PHRASE


def bump_tool_counts(total: int, by_tool: dict[str, int] | None,
                     tool_name: str) -> tuple[int, dict[str, int]]:
    """一次工具调用后，计数应变成多少（**纯函数，便于测试**）。

    语义：**先计数再执行** —— 失败/被拦的调用也算一次，
    因为"反复报错还反复重试"正是最典型的跑飞形态，只统计成功会让预算失效。
    """
    new_by_tool = dict(by_tool or {})
    new_by_tool[tool_name] = new_by_tool.get(tool_name, 0) + 1
    return total + 1, new_by_tool


def rag_empty_count_after(prev: int, tool_name: str, content: str,
                          artifact=None) -> int:
    """一次工具结果之后，"连续空检索"计数应变成多少（**纯函数，便于测试**）。

    只有**可靠判据**的空检索才计数（①库里没内容 / ②模型上报 [无覆盖]）；
    ③短语兜底是已知会误判的启发式（撞"部分覆盖"），**不计入** ——
    否则两次猜测就能把整轮检索停用。 reason 由工具经 artifact 显式带出。
    """
    if tool_name != RAG_TOOL_NAME:
        return prev                                    # 非检索工具不影响
    if not str(content).startswith(EMPTY_RETRIEVAL_MARKER):
        return 0                                       # 命中资料 → 清零
    if is_shortcircuit_worthy_empty(artifact):
        return prev + 1
    return prev                                        # 不可靠判据 → 维持


def budget_blocked(total: int, used_by_tool: dict[str, int],
                   tool_name: str) -> str | None:
    """这次工具调用是否超出预算。**纯函数，便于测试**。

    入参是**本次之前**的计数，因此语义直观：
        budget_blocked(0, {}, "rag_summarize") -> None      # 第 1 次，放行
        budget_blocked(10, {...}, "any_tool")  -> "..."     # 第 11 次，拦下

    返回拦截理由（None 表示放行）。理由会被当作工具观察结果喂回模型，
    所以要说清"别再试了、现在该做什么"。
    """
    if total >= MAX_TOOL_CALLS_PER_TURN:
        return (f"{TOOL_BUDGET_MARKER}本轮工具调用已达上限"
                f"（{MAX_TOOL_CALLS_PER_TURN} 次），工具已全部停用。"
                "请立即基于已有信息作答；若信息确实不足，"
                "请如实告知用户未能查到可靠资料，不要尝试继续调用任何工具。")

    limit = MAX_TOOL_CALLS_PER_TOOL.get(tool_name)
    if limit is not None and used_by_tool.get(tool_name, 0) >= limit:
        return (f"{TOOL_BUDGET_MARKER}工具 {tool_name} 本轮已调用 {limit} 次，"
                f"达到该工具上限，本次未执行。"
                "请改用已有的检索结果作答，不要再换措辞重复调用它。")

    return None

