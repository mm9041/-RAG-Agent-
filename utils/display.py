"""
界面文案的格式化（**纯函数，无 Streamlit 依赖，便于单测**）

把"过程面板里那一行该怎么写"从 app.py 抽出来，原因有两条：
1. 便于测试 —— 这类文案策略一旦被改坏，用户看到的东西就会变形；
2. 策略集中在一处 —— 见 format_tool_result() 的说明，那里记着一个踩过的坑。
"""
from agent.runtime import EMPTY_RETRIEVAL_MARKER

# 非检索类工具返回正文时的截断长度
DEFAULT_PREVIEW = 200


def format_tool_result(tool_name: str, content: str, preview: int = DEFAULT_PREVIEW) -> str:
    """过程面板里「工具返回」那一行的文案。

    ⚠️ **`rag_summarize` 的返回正文不能贴出来** —— 这是实际踩到的坑：

    它返回的是"检索材料的总结"，读起来就是一段回答；而**相关问题检索到的材料
    与上一轮回答高度重叠**，于是用户会看到一段灰色小字，内容像是
    "上一轮的回答又打了一遍"（它出现在过程面板里，等回答生成完、面板折叠后"消失"）。

    原则：**过程面板只说"发生了什么"，不搬"返回了什么内容"**。
    原始材料的展示另有归属 —— 界面下方的「参考资料」面板，
    用户主动展开才看，不会与回答混淆。

    其它工具（天气 / 月份 / 用户ID / 外部数据）返回的是**短的结构化事实**，
    贴出来有助于看清过程，也不会被误认成回答，所以保留预览。
    """
    cleaned = " ".join(str(content).split())

    if tool_name == "rag_summarize":
        if cleaned.startswith(EMPTY_RETRIEVAL_MARKER):
            return f"**工具返回** `{tool_name}`：无相关资料"
        return f"**工具返回** `{tool_name}`：已返回参考内容 {len(cleaned)} 字（材料见下方参考资料）"

    return f"**工具返回** `{tool_name}`：{cleaned[:preview]}"
