"""管理员模型配置。跨进程读写锁避免配置切换与正在运行的问答交错。"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import yaml
from utils.path_tool import get_abs_path


@contextmanager
def configuration_guard(write=False):
    conn=sqlite3.connect(get_abs_path('.config_guard.sqlite'),timeout=300)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.execute('BEGIN EXCLUSIVE' if write else 'BEGIN')
        conn.execute('SELECT * FROM guard').fetchall()
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_model_settings():
    from utils.config_handler import model_conf,chroma_conf
    model=yaml.safe_load(Path(get_abs_path('config/model.yml')).read_text(encoding='utf-8'))
    chroma=yaml.safe_load(Path(get_abs_path('config/chroma.yml')).read_text(encoding='utf-8'))
    for key in ("chat_model_name","embedding_model_name"):
        model_conf[key]=model[key]
    chroma_conf["rerank_model"]=chroma["rerank_model"]


def _atomic(path, data):
    tmp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def _set_fields(data, fields):
    text=data.decode('utf-8-sig')
    for key,value in fields.items():
        line=key+': '+json.dumps(value,ensure_ascii=False)
        pattern=r'(?m)^'+re.escape(key)+r':[^\r\n]*'
        if re.search(pattern,text):
            text=re.sub(pattern,lambda _:line,text)
        else:
            text=text.rstrip()+'\n'+line+'\n'
    return text.encode('utf-8')


def apply_models(token, chat, embedding, rerank):
    from utils.admin_auth import valid
    if not valid(token):
        raise PermissionError('需要管理员登录')
    values=[str(v).strip() for v in (chat,embedding,rerank)]
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}',v) for v in values):
        raise ValueError('模型名称不能为空，且只能包含字母、数字、点、下划线、冒号或斜杠/连字符')
    chat,embedding,rerank=values
    from utils.config_handler import model_conf,chroma_conf
    with configuration_guard(write=True):
        refresh_model_settings()
        if (chat,embedding,rerank)==(model_conf['chat_model_name'],model_conf['embedding_model_name'],chroma_conf.get('rerank_model')):
            return '模型配置未变化'
        model_path=Path(get_abs_path('config/model.yml'))
        chroma_path=Path(get_abs_path('config/chroma.yml'))
        old_model=model_path.read_bytes();old_chroma=chroma_path.read_bytes()
        changed_embedding=embedding!=model_conf['embedding_model_name']
        meta=Path(get_abs_path(chroma_conf['persist_directory']))/'kb_meta.json'
        old_meta=meta.read_bytes() if meta.exists() else None
        from model.factory import reset_models
        try:
            _atomic(model_path,_set_fields(old_model,{'chat_model_name':chat,'embedding_model_name':embedding}))
            _atomic(chroma_path,_set_fields(old_chroma,{'rerank_model':rerank}))
            refresh_model_settings()
            reset_models()
            if changed_embedding:
                from rag.vector_store import VectorStoreService
                VectorStoreService().load_document(force=True)
        except BaseException:
            _atomic(model_path,old_model);_atomic(chroma_path,old_chroma)
            if old_meta is not None:
                _atomic(meta,old_meta)
            else:
                meta.unlink(missing_ok=True)
            refresh_model_settings();reset_models()
            raise
        return '模型配置已应用，知识库已重建' if changed_embedding else '模型配置已应用，下一次问答使用新模型'
