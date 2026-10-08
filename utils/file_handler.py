import os
import hashlib
from utils.logger_handler import logger
from langchain_core.documents import Document
from langchain_community.document_loaders import PyPDFLoader, TextLoader


def get_file_md5_hex(filepath: str):     # 获取文件的md5的十六进制字符串

    if not os.path.exists(filepath):
        logger.error(f"[md5计算]文件{filepath}不存在")
        return

    if not os.path.isfile(filepath):
        logger.error(f"[md5计算]路径{filepath}不是文件")
        return

    md5_obj = hashlib.md5()

    chunk_size = 4096       # 4KB分片，避免文件过大爆内存
    try:
        with open(filepath, "rb") as f:     # 必须二进制读取
            while chunk := f.read(chunk_size):
                md5_obj.update(chunk)

            """
            chunk = f.read(chunk_size)
            while chunk:
                
                md5_obj.update(chunk)
                chunk = f.read(chunk_size)
            """
            md5_hex = md5_obj.hexdigest()
            return md5_hex
    except Exception as e:
        logger.error(f"计算文件{filepath}md5失败，{str(e)}")
        return None


def file_extension(name: str) -> str:
    """取文件名的**小写**后缀（不含点），如 "保养.PDF" -> "pdf"。

    为什么必须归一化大小写（实测踩到的静默漏库）：
    白名单原本用 `f.endswith(("txt","pdf"))`、分发用 `path.endswith("txt")`，
    两处都硬编码小写 —— 于是一个 `维护保养.PDF` 会**不进库、不报错、
    也不出现在任何文件统计里**，症状正是最难定位的那类（某些问题突然答不出）。
    """
    return os.path.splitext(name)[1].lower().lstrip(".")


def listdir_with_allowed_type(path: str, allowed_types: tuple[str, ...]) -> tuple[str, ...]:
    """返回文件夹内的文件列表（只保留指定后缀的文件）

    注意：路径不存在或不是文件夹时返回空元组——绝不能把 allowed_types 本身返回，
    否则调用方会把 "txt"、"pdf" 这样的后缀字符串当成文件路径往下传。

    配置里写 "txt" 还是 ".txt" 都可以（两侧都归一化成小写无点后缀再比对）。
    """
    allowed = {str(t).lower().lstrip(".") for t in allowed_types}
    files = []

    if not os.path.isdir(path):
        logger.error(f"[listdir_with_allowed_type]{path}不是文件夹")
        return ()

    for f in os.listdir(path):
        if file_extension(f) in allowed:
            files.append(os.path.join(path, f))

    return tuple(files)


def pdf_loader(filepath: str, passwd=None) -> list[Document]:
    return PyPDFLoader(filepath, passwd).load()


def txt_loader(filepath: str) -> list[Document]:
    return TextLoader(filepath, encoding="utf-8").load()
