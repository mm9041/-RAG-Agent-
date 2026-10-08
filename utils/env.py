"""轻量环境加载；登录页无需导入模型、LangChain 或 Chroma。"""
import threading
from dotenv import load_dotenv
from utils.path_tool import get_abs_path

_env_loaded = False
_lock = threading.Lock()


def ensure_env_loaded() -> None:
    global _env_loaded
    with _lock:
        if not _env_loaded:
            load_dotenv(get_abs_path(".env"), override=False)
            _env_loaded = True
