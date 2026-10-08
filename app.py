from __future__ import annotations

import os
import re

import streamlit as st
from utils.env import ensure_env_loaded

ensure_env_loaded()
from utils.model_settings import configuration_guard, refresh_model_settings
with configuration_guard():
    refresh_model_settings()


# 演示用户：mock 外部数据里只有这 10 个用户
DEMO_USERS = [f"10{index:02d}" for index in range(1, 11)]

# 访问密码从环境变量读。没设置时进入「本地演示模式」并在页面上明确告警，
# 而不是默默允许所有人访问。
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# 删除操作的确认态标记
DELETE_CURRENT = "current"
DELETE_OTHERS = "others"

# 提示词要求模型"每次调工具前输出思考过程"，所以它常自己带一个「思考：」前缀；
# 而面板里已经标注了「模型说明」，不去掉就会显示成「模型说明：思考：…」
_THINKING_PREFIX = re.compile(r"^\s*(?:\*\*)?\s*思考\s*(?:\*\*)?\s*[:：]\s*")


def clean_thinking(text: str) -> str:
    return _THINKING_PREFIX.sub("", text).strip() or text.strip()


def on_user_change():
    """切换登录身份时切到该身份的主会话（身份 = 会话隔离维度）"""
    st.session_state["thread_id"] = ReactAgent.thread_id_for(st.session_state["user_id"])
    st.session_state.pop("pending_delete", None)
    st.session_state.pop("queued_prompt", None)
    st.session_state.pop("answer_job", None)
    st.session_state.pop("_admin_session_password", None)


def render_login() -> bool:
    """应用级访问控制。返回 True 表示可以继续渲染。

    注意这是**演示级**的门禁：单一共享密码、没有账号体系、没有会话过期。
    真实系统请换成正式鉴权，并把登录态里的 user_id 注入 Agent 上下文
    （这条链路本项目已预留：context["user_id"] → get_user_id 工具）。
    """
    if not APP_PASSWORD:
        st.warning("未设置 APP_PASSWORD，当前任何人可访问，仅供本地演示使用。")
        return True

    if st.session_state.get("authed"):
        return True

    st.subheader("请先登录")
    with st.form("login_form"):
        password = st.text_input("访问密码", type="password")
        submitted = st.form_submit_button("登录")

    if submitted:
        if password == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("密码不正确")

    return False


def render_session_panel(agent: ReactAgent, user_id: str) -> None:
    """侧栏的会话面板：历史会话切换 / 新建 / 删除 / 清理

    这个面板是为了补上原来的缺口：以前点「开始新会话」后，
    旧会话其实还在库里，但界面上再也回不去了（体验上等于丢了）。
    """
    current = st.session_state["thread_id"]

    sessions = agent.list_threads(user_id)
    ids = [s["id"] for s in sessions]
    if current not in ids:
        # 新建的会话还没产生任何记忆，列表里可能没有它
        ids.insert(0, current)

    labels = {s["id"]: f"{s['title']}（{s['rounds']} 轮）" for s in sessions}
    labels.setdefault(current, "（新会话，还没开始聊）")

    picked = st.selectbox(
        "历史会话", ids, index=ids.index(current),
        format_func=lambda thread_id: labels.get(thread_id, thread_id),
        key=f"history_{user_id}_{current}",
    )
    if picked != current:
        st.session_state["thread_id"] = picked
        st.session_state.pop("pending_delete", None)
        st.rerun()

    if st.button("开始新会话"):
        st.session_state["thread_id"] = ReactAgent.new_thread_id(user_id)
        st.session_state.pop("pending_delete", None)
        st.rerun()

    # 删除一定要二次确认：会话数据不可逆，一步删掉太危险
    pending = st.session_state.get("pending_delete")
    if pending not in (DELETE_CURRENT, DELETE_OTHERS):
        if st.button("删除当前会话"):
            st.session_state["pending_delete"] = DELETE_CURRENT
            st.rerun()
        if len(ids) > 1 and st.button("清理其他会话"):
            st.session_state["pending_delete"] = DELETE_OTHERS
            st.rerun()
        return

    target = ("除当前会话外的全部会话" if pending == DELETE_OTHERS
              else f"会话「{labels.get(current, current)}」")
    st.warning(f"确认删除{target}？该操作不可恢复。")

    col_confirm, col_cancel = st.columns(2)
    if col_confirm.button("确认删除", type="primary"):
        if pending == DELETE_OTHERS:
            removed = agent.clear_other_threads(user_id, current)
            st.session_state["_flash"] = f"已清理 {removed} 个会话"
        else:
            agent.delete_thread(current, user_id)
            st.session_state["thread_id"] = ReactAgent.new_thread_id(user_id)
            st.session_state["_flash"] = "已删除该会话"
        st.session_state.pop("pending_delete", None)
        st.rerun()

    if col_cancel.button("取消"):
        st.session_state.pop("pending_delete", None)
        st.rerun()


def render_sources(sources):
    unique = {}
    for source in sources:
        key = source.get("chunk_id") or (source["file"], source["snippet"])
        unique.setdefault(key, source)
    if unique:
        with st.expander(f"检索参考资料（{len(unique)} 个片段，需核对与结论的对应关系）", expanded=False):
            for source in unique.values():
                page = source.get("page")
                label = source["file"] + (f" · 第 {page + 1} 页" if isinstance(page, int) else "")
                st.markdown(f"**{label}** · 距离 {source['distance']}")
                st.caption(f"引用 [{source.get('ref', '旧记录')}]")
                st.text(source["snippet"])


from utils.admin_auth import restore as restore_admin, sync_cookie
st.set_page_config(page_title="智扫通", page_icon="🤖", layout="wide")
is_admin = restore_admin()
sync_cookie()
if is_admin:
    from utils.service_ui import render_admin_page
    render_admin_page()
    st.stop()

st.html("""<style>
[data-testid="stChatMessage"] {max-width: 88%; margin-right:auto; margin-left:0;}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    flex-direction:row-reverse; margin-left:auto; margin-right:0; width:fit-content;
    background:var(--secondary-background-color, #eef2f8); border-radius:18px;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
    min-width:0; text-align:left;
}
@media (max-width:640px) { [data-testid="stChatMessage"] {max-width:96%;} }
</style>""")
st.title("智扫通机器人智能客服")
st.divider()

if not render_login():
    st.stop()

if "agent" not in st.session_state:
    # 首屏已显示；首次依赖导入也包含在可见的进度提示中。
    try:
        with st.status("正在初始化客服…", expanded=True) as startup:
            st.write("正在加载客服组件…")
            from utils.app_resources import get_agent, get_vector_service
            st.write("正在检查知识库…")
            # 复用连接，但每个新页面会话仍校验源文件，避免缓存掩盖知识库更新。
            with configuration_guard():
                refresh_model_settings()
                kb_status = get_vector_service().load_document()
                st.write("正在准备对话服务…")
                shared_agent = get_agent()
            startup.update(label="客服已就绪", state="complete", expanded=False)
    except Exception as e:
        st.error(f"客服初始化失败：{type(e).__name__}：{e}")
        st.info("请检查模型配置、网络和知识文件。修复后可重试，已发布的知识库会保留。")
        if st.button("重新初始化"):
            st.rerun()
        st.stop()
    st.session_state["kb_status"] = kb_status
    st.session_state["agent"] = shared_agent

# 只有登录通过后才导入重依赖；后续重跑由 Python 的模块缓存复用。
from agent.react_agent import ReactAgent
from utils.display import format_tool_result
from utils.service_ui import render_admin_login, knowledge_status

agent: ReactAgent = st.session_state["agent"]

# 会话状态初始化必须在侧栏渲染之前完成 —— 侧栏要按 user_id 列历史会话，
# 而 user_id 又是侧栏里那个选择框的 key（先在 session_state 里给默认值，
# 等价于把选择框的默认选项设成第一个用户）。
if "user_id" not in st.session_state:
    st.session_state["user_id"] = DEMO_USERS[0]
if "thread_id" not in st.session_state:
    # 默认取「该身份的主会话」：刷新页面还能接着上一轮聊，
    # 且不同身份之间的记忆互相隔离
    st.session_state["thread_id"] = ReactAgent.thread_id_for(st.session_state["user_id"])

with st.sidebar:
    knowledge_status()

    st.divider()
    st.caption("登录身份（由会话注入，模型无法更改）")
    st.selectbox("登录用户", DEMO_USERS, key="user_id", on_change=on_user_change,
                 disabled=bool(st.session_state.get("answer_job") and not st.session_state["answer_job"].done.is_set()),
                 label_visibility="collapsed")
    render_admin_login()

    st.divider()
    st.caption("会话")
    if not (st.session_state.get("answer_job") and not st.session_state["answer_job"].done.is_set()):
        render_session_panel(agent, st.session_state["user_id"])

    flash = st.session_state.pop("_flash", None)
    if flash:
        st.success(flash)

    if APP_PASSWORD:
        st.divider()
        if st.button("退出登录"):
            st.session_state["authed"] = False
            st.session_state.pop("_admin_session_password", None)
            st.rerun()

thread_id: str = st.session_state["thread_id"]
user_id: str = st.session_state["user_id"]

from utils.service_ui import like_answer
from utils.jobs import AnswerJob
import queue
import time

job = st.session_state.get("answer_job")
if job is not None and job.done.is_set() and (job.thread_id != thread_id or job.user_id != user_id):
    st.session_state.pop("answer_job", None)
    job = None
# 后台生成时仍保留整个聊天区，不用状态面板替换历史。
running = job is not None and not job.done.is_set()
history = job.history if running else agent.load_history(thread_id,user_id)
question = ""
for index, message in enumerate(history):
    with st.chat_message(message['role']):
        if message['role'] == 'user':
            question = message['content']
        if message.get('incomplete'):
            st.caption(message['content'])
        else:
            st.markdown(message['content'])
            render_sources(message.get('sources',[]))
            if message['role'] == 'assistant':
                like_answer(message,question,index,thread_id,user_id)

if running:
    st.chat_message("user").markdown(job.query)
    @st.fragment(run_every=0.2)
    def render_running_job():
        current_job = st.session_state["answer_job"]
        while True:
            try:
                event, data = current_job.events.get_nowait()
                if event == "phase":
                    st.session_state["job_phase"] = data
                elif event == "content":
                    st.session_state["job_text"] = st.session_state.get("job_text", "") + data
                elif event == "final":
                    st.session_state["job_text"] = data
                elif event == "tool_start":
                    st.session_state["job_text"] = ""
            except queue.Empty:
                break
        with st.chat_message("assistant"):
            st.caption(f"{st.session_state.get('job_phase','正在处理')} · {time.monotonic()-current_job.started:.1f} 秒")
            st.markdown(st.session_state.get("job_text", "") or "正在准备回复…")
        if st.button("停止生成", key="cancel_job", disabled=current_job.cancel.is_set()):
            current_job.cancel.set()
        if current_job.cancel.is_set():
            st.caption("已请求停止，正在等待当前调用结束；完成前不会启动新的问答。")
        if current_job.done.is_set():
            st.rerun()
    render_running_job()
    st.chat_input("正在回复，可点击停止生成", disabled=True, key="running_input")
    st.stop()

if job is not None:
    st.caption(f"上次处理耗时 {(job.ended or time.monotonic())-job.started:.1f} 秒")
    if job.error:
        st.error(f"本次处理失败：{job.error}")
        if st.button("重试上次问题"):
            st.session_state['queued_prompt'] = job.query
    with st.expander("处理阶段时间"):
        for phase, elapsed in job.timings:
            st.caption(f"{elapsed:.1f}s：{phase}")

prompt = st.chat_input("请输入您的问题")
prompt = prompt or st.session_state.pop('queued_prompt',None)
if prompt:
    st.session_state['answer_job'] = AnswerJob(agent,prompt,thread_id,user_id)
    st.session_state['job_phase'] = '开始处理'
    st.session_state['job_text'] = ''
    st.rerun()
