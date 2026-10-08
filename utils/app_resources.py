"""进程内共享无用户状态的资源；用户身份与运行计数仍按每次调用传入。"""
import streamlit as st


@st.cache_resource(show_spinner=False)
def get_vector_service():
    from rag.vector_store import VectorStoreService
    return VectorStoreService()


@st.cache_resource(show_spinner=False)
def get_agent():
    from agent.react_agent import ReactAgent
    return ReactAgent()
