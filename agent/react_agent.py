"""
ReAct 智能体：装配 Agent，并以「事件流」的形式对外暴露执行过程

事件协议（stream_events 依次产出 (event, data)）：
    ("content",  str)                   模型输出的**文本增量**（token 级）
    ("tool_start", {"name","args"})     模型决定调用某工具，该轮文本已结束、工具即将执行
    ("tool_end",   {"name","content"})  工具执行完毕，content 为工具返回
    ("sources",   list[dict])           本次检索引用的资料（来自工具 artifact，不进模型上下文）

关于 content 的归属 —— 这里有个**容易误解**的点，写清楚：

    模型**没有独立的推理通道**（实测 `additional_kwargs` 为空、无 `reasoning_content`），
    所以拿不到它的内部思维链。界面上那个「思考」**不是模型的内部推理**，
    而是**提示词要求它写在正文里的叙述**
    （见 prompts/main_prompt.txt 的「输出规则」第 1 条："每次调用工具前，必须输出真实的自然语言思考过程"）。

    两者共用 content，因此只能靠**位置**区分：
        一段 content 之后紧跟 tool_start   -> 它是「调工具前的叙述」
        最后一段 content（之后无 tool_start）-> 它才是最终答案
    UI 层据此把前者收进过程面板、只把后者留在正文。

    ⚠️ 两点务必知道：
    - 那段叙述是模型的**自述**，属于"它对自己行为的解释"，
      **不是可验证的计算轨迹**，因此可能不准确；
    - 模型**有时不写叙述、直接发起工具调用**，此时过程面板里就没有「思考」条目
      （实测出现过 content 为空、只带 tool_calls 的 AI 消息）。
"""
import sqlite3
import hashlib
import os
import tempfile
from utils.index_lock import index_lock
from agent.control import register, unregister
import threading
import time
from utils.config_handler import agent_conf
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.runtime import AgentContext
from agent.tools.agent_tools import (enter_report_mode, fetch_external_data,
                                     get_current_month, get_user_id, get_user_location,
                                     get_weather, rag_summarize)
from agent.tools.middleware import log_before_model, monitor_tool, report_prompt_addendum, bound_model
from model.factory import get_chat_model
from utils.logger_handler import logger
from utils.path_tool import get_abs_path
from utils.prompt_loader import load_system_prompts

# 对话记忆落盘位置（相对项目根）
CHECKPOINT_DB = "checkpoints.sqlite"

# 默认会话 ID：固定值，这样刷新页面后还能接着上一轮聊
DEFAULT_THREAD_ID = "default"

# create_agent 编译出的图节点名：模型节点叫 model，工具节点叫 tools。
# 必须按「节点归属」过滤 messages 通道，只靠类型过滤是不够的——
# 工具内部若也调用了 LLM（本项目的 rag_summarize 就是），那些嵌套 token 同样会以
# AIMessageChunk 的形状混进来。实测一次问答 389 个 chunk 里有 158 个属于工具内部，
# 不过滤就会把工具内部的中间文本当成回复推给用户。
MODEL_NODE = "model"

_checkpointer = None
_checkpointer_lock = threading.RLock()


def get_checkpointer() -> SqliteSaver:
    """全局唯一的 SqliteSaver：让多轮记忆跨进程重启保留。

    check_same_thread=False 是必须的——Streamlit 会在不同线程里重跑脚本，
    默认的 sqlite 同线程校验会直接抛 ProgrammingError。
    """
    global _checkpointer

    with _checkpointer_lock:
        return _get_checkpointer_locked()


def _get_checkpointer_locked():
    global _checkpointer
    if _checkpointer is None:
        db_path = get_abs_path(CHECKPOINT_DB)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        _checkpointer = SqliteSaver(conn)
        logger.info(f"[Agent]对话记忆已挂载：{db_path}")

    return _checkpointer


def _as_text(content) -> str:
    """消息内容统一转字符串：多模态时 content 是 list，不能直接当 str 用"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


# 一轮对话没能跑完时，在历史末尾补的提示文案。
# 见 messages_to_history() 的说明：不补的话，界面上最后一条会是**用户自己的提问**，
# 看起来像"AI 没理我"，而实际是那一轮没跑完。
INCOMPLETE_TURN_NOTE = (
    "上一条回复没有完成，可能是刷新了页面、网络中断或执行出错。可以重新问一次。"
)


def is_user_visible_token(chunk, meta: dict | None) -> bool:
    """messages 流里的一条 chunk 是否该渲染给用户（**纯函数，便于测试**）。

    两重过滤，缺一不可：
    1. **类型**：只要 AIMessageChunk —— ToolMessage 这类完整消息不是 token；
    2. **节点**：`langgraph_node` 为 None（顶层）或 MODEL_NODE。
       ★ 第二重是修掉「389 个 chunk 里 158 个是工具内部 token」的那条：
       工具内部若再调 LLM，其 chunk 的 langgraph_node 是工具节点名，
       不过滤就会把工具内部的生成内容当成最终回答打给用户。
    """
    if "grounded_stream" in (meta or {}).get("tags", []):
        return False
    if not isinstance(chunk, AIMessageChunk):
        return False
    node = (meta or {}).get("langgraph_node")
    return node is None or node == MODEL_NODE


def messages_to_history(messages) -> list[dict]:
    """把模型记忆里的消息转成界面可渲染的 [{role, content}]（**纯函数，便于测试**）

    两条过滤规则：
    1. **只收 Human 和"不带 tool_calls 的 AI"**。
       带 `tool_calls` 的 AI 消息，其内容是"调工具前的思考"而不是答案，
       把它当回答渲染会误导用户；
    2. **末尾如果落在用户消息上，补一条"未完成回合"提示**。
       这是实际踩到的坑：一轮对话若在"模型决定调工具之后"中断
       （刷新页面 / 网络断 / 工具报错），记忆末尾会是一条
       **带 tool_calls 的空 AI 消息** —— 它被规则 1 过滤掉，
       于是界面上最后一条就只剩**用户自己刚发的问题**。
       用户看到的是"我问了但 AI 没回"，而代码里对此没有任何提示。
       补一条说明，至少让用户知道那一轮没跑完，而不是以为被忽略了。
    """
    history: list[dict] = []
    sources = []

    for message in messages:
        if isinstance(message, HumanMessage):
            sources = []
            history.append({"role": "user", "content": _as_text(message.content)})
        elif isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
            sources.extend(message.artifact.get("sources", []))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            text = _as_text(message.content).strip()
            if text:
                entry = {"role": "assistant", "content": text}
                if sources:
                    entry["sources"] = list(sources)
                history.append(entry)

    if history and history[-1]["role"] == "user":
        history.append({"role": "assistant", "content": INCOMPLETE_TURN_NOTE,
                        "incomplete": True})

    return history


# 兜底：遍历检查点时的扫描上限，防止历史极多时界面卡住
_THREAD_SCAN_LIMIT = 5000

# 会话标题的截断长度（取该会话的第一条用户提问当标题）
_TITLE_MAX_CHARS = 28


class ReactAgent:
    def __init__(self, checkpointer=None):
        self.checkpointer = checkpointer if checkpointer is not None else get_checkpointer()
        connection = getattr(self.checkpointer, "conn", None)
        if isinstance(connection, sqlite3.Connection):
            db_file = connection.execute("PRAGMA database_list").fetchone()[2]
            self._lock_namespace = os.path.abspath(db_file) if db_file else f"memory-{id(connection)}"
        else:
            self._lock_namespace = str(get_abs_path(CHECKPOINT_DB)) if checkpointer is None else str(id(checkpointer))
        from utils.config_handler import model_conf
        self._chat_model_name = model_conf["chat_model_name"]
        self.agent = create_agent(
            model=get_chat_model(),
            system_prompt=load_system_prompts(),
            tools=[rag_summarize, get_weather, get_user_location, get_user_id],
            middleware=[monitor_tool, log_before_model, report_prompt_addendum, bound_model],
            checkpointer=self.checkpointer,
            # 声明上下文 schema：key 拼错能被类型检查发现，也避免 LangChain
            # 按 None 序列化 context 时打出的 Pydantic 警告
            context_schema=AgentContext,
        )

    # ------------------------------------------------------------------ 会话

    @staticmethod
    def thread_id_for(user_id: str) -> str:
        """按身份隔离会话：不同用户的记忆天然分开，不会互相串到"""
        return f"user-{user_id}"

    @staticmethod
    def new_thread_id(user_id: str | None = None) -> str:
        """开一个新会话（与原会话不共享记忆）

        传 user_id 会带上身份前缀，便于在 checkpoints.sqlite 里分辨会话属于谁。
        """
        suffix = uuid4().hex[:12]
        return f"user-{user_id}-{suffix}" if user_id else f"session-{suffix}"

    @staticmethod
    def require_owner(thread_id: str, user_id: str | None) -> None:
        if not user_id or not str(user_id).isdigit():
            raise PermissionError("需要数字用户 ID")
        base = f"user-{user_id}"
        if thread_id != base and not thread_id.startswith(base + "-"):
            raise PermissionError("会话不属于当前用户")

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _context(user_id: str | None) -> AgentContext:
        """每轮构造一份新的运行期上下文。

        - report / rag_empty_count / 工具调用计数必须每轮重置，否则上一轮的状态会串到这一轮；
        - user_id 由调用方（Web 层）注入，模型无法自行指定，这是身份的安全边界。
        """
        context: AgentContext = {"report": False, "rag_empty_count": 0,
                                 "tool_call_total": 0, "tool_calls_by_name": {},
                                 "model_call_total": 0,
                                 "deadline": time.monotonic() + float(agent_conf.get("turn_timeout", 180))}
        if user_id:
            context["user_id"] = user_id
        return context

    # -------------------------------------------------------------- 执行与事件

    def _turn_lock(self, thread_id, timeout=240):
        namespace = getattr(self, "_lock_namespace", str(id(self)))
        key = hashlib.sha256((namespace + thread_id).encode()).hexdigest()
        return index_lock(os.path.join(tempfile.gettempdir(), "robot-agent-turns", key + ".lock"), timeout)

    def stream_events(self, query: str, thread_id: str = DEFAULT_THREAD_ID, user_id=None, **kwargs):
        from utils.model_settings import configuration_guard, refresh_model_settings
        with configuration_guard():
            refresh_model_settings()
            yield from self._stream_events_locked(query, thread_id, user_id, **kwargs)

    def _stream_events_locked(self, query: str, thread_id: str = DEFAULT_THREAD_ID,
                      user_id: str | None = None, *, request_id=None, cancel_event=None,
                      device_model="", washable="未知"):
        self.require_owner(thread_id, user_id)
        request_id = request_id or uuid4().hex
        yield ("phase", "等待当前会话可用")
        with self._turn_lock(thread_id):
            state = self.agent.get_state(self._config(thread_id))
            messages = list((state.values or {}).get("messages", []))
            for i, message in enumerate(messages):
                if isinstance(message, HumanMessage) and message.additional_kwargs.get("request_id") == request_id:
                    for previous in messages[i+1:]:
                        if isinstance(previous, HumanMessage):
                            break
                        if isinstance(previous, AIMessage) and not previous.tool_calls:
                            yield ("final", _as_text(previous.content))
                            return
                    raise RuntimeError("该请求未完成，请使用新的请求重新尝试")
            if cancel_event is not None and cancel_event.is_set():
                return
            # 补齐中断回合中缺失的工具结果，避免下一次请求被模型服务拒绝。
            pending = {}
            for m in messages:
                if isinstance(m, AIMessage):
                    pending.update({c["id"]: c for c in m.tool_calls})
                elif isinstance(m, ToolMessage):
                    pending.pop(m.tool_call_id, None)
            if pending:
                self.agent.update_state(self._config(thread_id), {"messages": [
                    ToolMessage(content="上次请求已中断，工具未完成。", tool_call_id=key, name=c["name"])
                    for key, c in pending.items()]}, as_node="tools")
            token = register(cancel_event) if cancel_event is not None else ""
            context = self._context(user_id)
            from utils.config_handler import model_conf
            context["refresh_chat_model"] = self._chat_model_name != model_conf["chat_model_name"]
            context.update(cancel_token=token, device_model=device_model, washable=washable, stream_response=True)
            try:
                for mode, payload in self.agent.stream(
                    {"messages": [HumanMessage(content=query, additional_kwargs={"request_id":request_id})]},
                    config=self._config(thread_id), stream_mode=["messages", "updates", "custom"], context=context):
                    if mode == "messages":
                        chunk, meta = payload
                        if is_user_visible_token(chunk, meta):
                            text = _as_text(chunk.content)
                            if text:
                                yield ("content", text)
                        continue
                    if mode == "custom":
                        if isinstance(payload, dict) and payload.get("answer_delta"):
                            yield ("content", payload["answer_delta"])
                        if isinstance(payload, dict) and payload.get("phase"):
                            yield ("phase", payload["phase"])
                        continue
                    for event, data in self._parse_updates(payload):
                        if event != "content":
                            yield event, data
                    for delta in (payload or {}).values():
                        for message in (delta or {}).get("messages", []) or []:
                            if isinstance(message, AIMessage) and not message.tool_calls:
                                yield ("final", _as_text(message.content))
            finally:
                unregister(token)

    @staticmethod
    def _parse_updates(payload: dict):
        """把 LangGraph 的节点更新翻译成工具事件"""
        for _node, delta in (payload or {}).items():
            for message in (delta or {}).get("messages", []) or []:

                if isinstance(message, AIMessage) and message.additional_kwargs.get("budget_stop"):
                    yield ("content", _as_text(message.content))
                elif isinstance(message, AIMessage) and message.tool_calls:
                    for call in message.tool_calls:
                        yield ("tool_start", {
                            "name": call.get("name", ""),
                            "args": call.get("args", {}),
                        })

                elif isinstance(message, ToolMessage):
                    # 工具可用 response_format="content_and_artifact" 附带
                    # 「不进入模型上下文」的数据，目前用来传引用来源（给界面展示）
                    artifact = message.artifact
                    if isinstance(artifact, dict) and artifact.get("sources"):
                        yield ("sources", artifact["sources"])

                    yield ("tool_end", {
                        "name": message.name or "",
                        "content": _as_text(message.content),
                    })

    # ------------------------------------------------------------------ 便捷方法

    def answer(self, query: str, thread_id: str = DEFAULT_THREAD_ID,
               user_id: str | None = None) -> str:
        """非流式获取最终答案（只保留最后一段文本，丢弃中间思考）"""
        buffer = []
        for event, data in self.stream_events(query, thread_id, user_id):
            if event == "content":
                buffer.append(data)
            elif event == "final":
                buffer = [data]
            elif event == "tool_start":
                buffer.clear()

        return "".join(buffer).strip()

    def load_history(self, thread_id: str, user_id: str) -> list[dict]:
        """从记忆里读出可展示的历史。

        界面历史直接取自记忆，而不是另存一份——否则刷新页面后
        「模型记得、界面空白」两边对不上。
        """
        self.require_owner(thread_id, user_id)
        try:
            state = self.agent.get_state(self._config(thread_id))
            messages = (state.values or {}).get("messages", [])
        except Exception as e:
            logger.warning(f"[Agent]读取会话 {thread_id} 的历史失败：{str(e)}")
            return []

        return messages_to_history(messages)

    # -------------------------------------------------------------- 会话管理

    def list_threads(self, user_id: str, limit: int | None = 20) -> list[dict]:
        """SQL 先筛选会话 ID，每个会话只反序列化最新检查点。"""
        self.require_owner(f"user-{user_id}", user_id)
        base = f"user-{user_id}"
        with self.checkpointer.cursor(transaction=False) as cur:
            rows = cur.execute(
                "SELECT thread_id, MAX(checkpoint_id) AS latest FROM checkpoints "
                "WHERE checkpoint_ns = '' AND (thread_id = ? OR thread_id LIKE ?) "
                "GROUP BY thread_id ORDER BY latest DESC",
                (base, base + "-%"),
            ).fetchall()
        records = []
        for thread_id, _ in (rows if limit is None else rows[:limit]):
            snapshot = self.checkpointer.get_tuple(self._config(thread_id))
            if snapshot is None:
                continue
            messages = snapshot.checkpoint.get("channel_values", {}).get("messages", [])
            first = next((m for m in messages if isinstance(m, HumanMessage)), None)
            records.append({"id": thread_id,
                "title": " ".join(_as_text(first.content).split())[:_TITLE_MAX_CHARS] if first else "（空会话）",
                "rounds": sum(isinstance(m, HumanMessage) for m in messages),
                "updated": str(snapshot.checkpoint.get("ts", ""))})
        return records

    def delete_thread(self, thread_id: str, user_id: str) -> None:
        self.require_owner(thread_id, user_id)
        with self._turn_lock(thread_id, timeout=1):
            self.checkpointer.delete_thread(thread_id)
        logger.info(f"[Agent]已删除会话：{thread_id}")

    def clear_other_threads(self, user_id: str, current: str) -> int:
        self.require_owner(current, user_id)
        targets = [s["id"] for s in self.list_threads(user_id, limit=None) if s["id"] != current]
        for thread_id in targets:
            self.delete_thread(thread_id, user_id)
        return len(targets)


if __name__ == '__main__':
    agent = ReactAgent()
    thread = "user-1007-cli-test"

    print(">>> 第一轮：给我生成我的使用报告（身份 = 1007）")
    print(agent.answer("给我生成我的使用报告", thread, user_id="1007")[:300])
    print()
    print(">>> 第二轮（指代型追问，验证记忆）：那我这个月要注意什么？")
    print(agent.answer("那我这个月要注意什么？", thread, user_id="1007")[:300])
    print()
    print(">>> 第三轮（验证身份来自会话，而非模型）：我是谁？")
    print(agent.answer("我是谁？我的用户ID是多少？", thread, user_id="1007")[:200])
