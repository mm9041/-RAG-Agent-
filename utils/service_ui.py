import hashlib
import json
import os
from pathlib import Path
import streamlit as st
from utils.path_tool import get_abs_path
from utils.customer_service import save_feedback, feedback_rows


def like_answer(message, question, index, thread_id, user_id):
    key = hashlib.sha256(f"{thread_id}:{index}:{message['content']}".encode()).hexdigest()[:16]
    liked_key = f"liked_{key}"
    from utils.customer_service import is_liked
    if liked_key not in st.session_state:
        st.session_state[liked_key] = is_liked(user_id,thread_id,key)
    liked = st.session_state[liked_key]
    if st.button("👍 已点赞" if liked else "👍", key=f"like_{key}", help="这条回复有帮助", disabled=liked):
        from utils.config_handler import chroma_conf
        meta = Path(get_abs_path(chroma_conf['persist_directory'])) / 'kb_meta.json'
        version = hashlib.sha256(meta.read_bytes()).hexdigest() if meta.exists() else 'unknown'
        save_feedback(user_id,thread_id,key,question,message['content'],"有帮助","点赞",message.get('sources',[]),
                      (message.get('sources') or [{}])[0].get('kb_version',version))
        st.session_state[liked_key] = True
        st.rerun()


def render_admin_login():
    from utils.admin_auth import issue
    if not os.getenv('ADMIN_PASSWORD'):
        st.caption("管理员登录未启用")
        return
    with st.expander("管理员登录", expanded=False):
        with st.form("admin_login_form", clear_on_submit=True):
            password = st.text_input("管理员密码", type="password", key="admin_login_password")
            submitted = st.form_submit_button("登录管理员")
        if submitted:
            try:
                st.session_state['_admin_token'] = issue(password)
                st.rerun()
            except PermissionError:
                st.error("管理员密码不正确")


def knowledge_status():
    from utils.config_handler import model_conf,chroma_conf
    st.caption("知识库状态")
    meta_path=Path(get_abs_path(chroma_conf['persist_directory']))/'kb_meta.json'
    try:
        meta=json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.exists() else {}
        count=sum(v.get('chunks',0) for v in meta.get('files',{}).values())
        st.write(f"已索引 {len(meta.get('files',{}))} 个文件 · {count} 个片段")
    except (ValueError,OSError):
        st.write("知识库状态暂不可读取")
    st.caption("嵌入模型")
    st.write(model_conf['embedding_model_name'])
    st.caption("聊天模型")
    st.write(model_conf['chat_model_name'])
    st.caption("重排模型")
    st.write(chroma_conf.get('rerank_model', '未配置'))
    if not chroma_conf.get('rerank_enabled', False):
        st.caption("重排未启用")


def model_settings_form():
    from utils.config_handler import model_conf,chroma_conf
    from utils.model_settings import apply_models
    st.subheader("模型配置")
    flash=st.session_state.pop('model_settings_notice',None)
    if flash:
        st.success(flash)
    with st.form('model_settings_form'):
        chat=st.text_input('聊天模型名称',value=model_conf['chat_model_name'])
        embedding=st.text_input('嵌入模型名称',value=model_conf['embedding_model_name'])
        rerank=st.text_input('重排模型名称',value=chroma_conf.get('rerank_model',''))
        st.caption('填写当前服务支持的模型 ID。聊天/重排模型保存后用于下一次问答；更换嵌入模型会重新索引全部资料。')
        confirmed=st.checkbox('我确认更换嵌入模型时重新生成知识库向量')
        submitted=st.form_submit_button('保存并应用模型',type='primary')
    if submitted:
        if embedding.strip()!=model_conf['embedding_model_name'] and not confirmed:
            st.error('更换嵌入模型需要勾选重建确认。')
            return
        try:
            with st.spinner('正在等待进行中的问答完成，并应用模型配置…'):
                message=apply_models(st.session_state.get('_admin_token',''),chat,embedding,rerank)
            st.session_state['model_settings_notice']=message
            st.rerun()
        except Exception as exc:
            st.error(f'模型配置未能应用，原配置和知识库已保留：{exc}')


def render_admin_page():
    from utils.admin_auth import restore, logout
    if not restore():
        st.stop()
    with st.sidebar:
        st.title("智扫通 · 管理端")
        st.caption("管理员工作台")
        section=st.radio("管理导航",["概览","知识文件","反馈与点赞"],key="admin_navigation")
        st.divider()
        knowledge_status()
        st.divider()
        if st.button("退出管理员",key="admin_logout",use_container_width=True):
            logout()
            st.rerun()
    st.title({"概览":"管理概览","知识文件":"知识库管理","反馈与点赞":"反馈与点赞"}[section])
    st.caption("管理知识资料、查看运行配置与用户反馈")
    if section == '知识文件':
        admin_panel()
    elif section == '反馈与点赞':
        rows=feedback_rows()
        likes=sum(r['rating']=='有帮助' for r in rows)
        left,right=st.columns(2)
        left.metric("点赞",likes);right.metric("反馈总数",len(rows))
        if rows:
            st.dataframe([{'问题':r['question'],'反馈':r['rating'],'时间':r['updated']} for r in rows],use_container_width=True,hide_index=True)
        else:
            st.info("还没有收到用户反馈。")
    else:
        from utils.config_handler import chroma_conf,model_conf
        root=Path(get_abs_path(chroma_conf['data_path']))
        files=[f for f in root.iterdir() if f.is_file() and f.suffix.lower() in ('.txt','.pdf')] if root.exists() else []
        rows=feedback_rows()
        c1,c2,c3=st.columns(3)
        c1.metric("知识文件",len(files));c2.metric("资料体积",f"{sum(f.stat().st_size for f in files)/1024:.1f} KB");c3.metric("用户点赞",sum(r['rating']=='有帮助' for r in rows))
        model_settings_form()
        st.subheader("工作入口")
        st.write("在左侧选择“知识文件”上传、替换或删除资料；选择“反馈与点赞”查看用户反馈。")


def admin_panel():
    from utils.knowledge_admin import preview,conflict_hints,publish
    from utils.admin_auth import restore
    if not restore():
        return
    password = os.getenv('ADMIN_PASSWORD','')
    with st.container():
        from utils.config_handler import chroma_conf
        root=Path(get_abs_path(chroma_conf['data_path']))
        names=sorted(f.name for f in root.iterdir() if f.is_file() and f.suffix.lower() in ('.txt','.pdf'))
        st.subheader("已发布资料")
        for name in names:
            content=(root/name).read_bytes()
            st.caption(f"{name} · {len(content)} 字节 · 版本 {hashlib.sha256(content).hexdigest()[:12]}")
        if names:
            version_file = st.selectbox("查看历史版本", names, key="version_file")
            version_dir = Path(get_abs_path('kb_versions')) / hashlib.sha256(version_file.encode()).hexdigest()
            versions = sorted(version_dir.glob('*')) if version_dir.exists() else []
            if versions:
                version_name = st.selectbox("已备份版本", [v.name for v in versions])
                st.download_button("下载历史版本", (version_dir/version_name).read_bytes(), file_name=version_file)
            else:
                st.caption("该文件暂无历史备份版本。")
        st.divider()
        st.subheader("发布新资料")
        uploaded=st.file_uploader("上传 TXT / 文本 PDF（最多20MB）",type=['txt','pdf'])
        model=st.text_input("资料适用品牌/型号")
        scope=st.text_input("适用条件与来源（例如说明书版本、滤网类型）")
        replace=st.checkbox("确认替换同名文件")
        if uploaded:
            try:
                content=uploaded.getvalue(); text=preview(uploaded.name,content)
                st.text_area("文本预览",text[:5000],height=180)
                existing = "\n".join((root/name).read_text(encoding='utf-8',errors='replace') for name in names if name != uploaded.name and name.lower().endswith('.txt'))
                for warning in conflict_hints(text, existing): st.warning(warning)
                if st.button("发布并更新索引"):
                    with st.spinner("正在发布，失败会保留旧版本…"):
                        st.success(publish(uploaded.name,content,password,model=model,scope=scope,replace=replace))
                    st.session_state['kb_status']='知识库已更新'
            except Exception as exc:
                st.error(str(exc))
        if names:
            st.divider()
            st.subheader("移除资料")
            selected=st.selectbox("删除知识文件",names)
            confirmed=st.checkbox("确认删除所选文件并更新索引")
            if st.button("删除并更新索引",disabled=not confirmed):
                try:
                    with st.spinner("正在更新索引…"):
                        st.success(publish(selected,b'',password,delete=True))
                except Exception as exc: st.error(str(exc))
