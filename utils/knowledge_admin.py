import hashlib
import io
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4
from utils.index_lock import index_lock
from utils.path_tool import get_abs_path


def authorized(password):
    expected = os.getenv("ADMIN_PASSWORD", "")
    return bool(expected) and secrets.compare_digest(str(password), expected)


def preview(name, content):
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("文件名不能包含路径")
    if len(content) > 20 * 1024 * 1024 or not content:
        raise ValueError("文件必须非空且不超过20MB")
    suffix = Path(name).suffix.lower()
    if suffix == ".txt":
        text = content.decode("utf-8-sig")
    elif suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("请先解密 PDF")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        raise ValueError("仅支持 UTF-8 TXT 和文本 PDF")
    if not text.strip():
        raise ValueError("文件未提取到文本，扫描PDF需先OCR")
    return text


def conflict_hints(text, existing_text=""):
    # 仅提示需人工检查的适用条件，不能自动判定文档矛盾。
    warnings = []
    if "HEPA" in text.upper() and ("水洗" in text or "清水" in text):
        warnings.append("包含 HEPA 清洗规则，请填写适用型号并核查说明书，不能泛化到所有滤网。")
    if "不可水洗" in text and "可水洗" in text.replace("不可水洗", ""):
        warnings.append("同时出现可水洗/不可水洗，请核对各自适用部件与型号。")
    if existing_text and "滤网" in text and "滤网" in existing_text:
        if (("不可水洗" in text and ("可水洗" in existing_text.replace("不可水洗", "") or "清水冲洗" in existing_text))
                or ("不可水洗" in existing_text and ("可水洗" in text.replace("不可水洗", "") or "清水冲洗" in text))):
            warnings.append("与已有资料存在不同的滤网清洗表述，请人工核对型号与适用条件；不同部件不一定构成矛盾。")
    return warnings


def publish(name, content, password, **kwargs):
    from utils.model_settings import configuration_guard, refresh_model_settings
    with configuration_guard():
        refresh_model_settings()
        return _publish(name, content, password, **kwargs)


def _publish(name, content, password, *, model="", scope="", replace=False, delete=False):
    if not authorized(password):
        raise PermissionError("需要管理员权限")
    from utils.config_handler import chroma_conf
    from rag.vector_store import VectorStoreService
    root = Path(get_abs_path(chroma_conf['data_path'])).resolve()
    if Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() not in ('.txt','.pdf'):
        raise ValueError("无效知识文件名")
    if not delete:
        preview(name, content)
    target = root / name
    if target.is_symlink():
        raise ValueError("不能更新符号链接")
    with index_lock(str(root / '.edit.lock'), timeout=240):
        old = target.read_bytes() if target.exists() else None
        if old is not None and not delete and not replace:
            raise ValueError("同名文件已存在，请确认替换")
        if delete and old is None:
            raise FileNotFoundError(name)
        catalog_path = root / '.catalog.json'
        old_catalog = catalog_path.read_bytes() if catalog_path.exists() else None
        catalog = json.loads(old_catalog) if old_catalog else {}
        if old is not None:
            backup = Path(get_abs_path('kb_versions')) / hashlib.sha256(name.encode()).hexdigest()
            backup.mkdir(parents=True, exist_ok=True)
            (backup / (hashlib.sha256(old).hexdigest() + Path(name).suffix)).write_bytes(old)
        temporary = root / ('.upload-' + uuid4().hex)
        try:
            if delete:
                target.unlink()
                catalog.pop(name, None)
            else:
                temporary.write_bytes(content)
                os.replace(temporary, target)
                catalog[name] = {'model':model.strip() or '未指定', 'scope':scope.strip() or '待核实',
                                 'version':hashlib.sha256(content).hexdigest()}
            catalog_path.write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
            return VectorStoreService().load_document()
        except BaseException:
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
            if old_catalog is None:
                catalog_path.unlink(missing_ok=True)
            else:
                catalog_path.write_bytes(old_catalog)
            raise
        finally:
            temporary.unlink(missing_ok=True)
