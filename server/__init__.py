"""FastAPI 后端。

设计原则：这一层只做「协议转换 + 鉴权 + 生命周期」，
所有业务逻辑仍然在 agent/ rag/ model/ utils/ 里，一行不改。
"""
