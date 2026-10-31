"""管理端：知识文件、模型配置、反馈。

管理员凭据沿用 ``utils/admin_auth.py`` 的 sqlite 令牌机制（8 小时绝对过期、
改密码即失效），此处只做 cookie 搬运，安全语义与旧版一致。

所有会触碰磁盘索引的操作都走 ``to_thread``：发布资料会触发向量库重建，
耗时从几秒到几分钟不等，绝不能占住事件循环。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse

from server import runtime, security
from server.deps import admin_token
from server.schemas import LoginRequest, ModelsRequest
from utils.path_tool import get_abs_path

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _data_root() -> Path:
    from utils.config_handler import chroma_conf

    return Path(get_abs_path(chroma_conf["data_path"]))


def _kb_files() -> list[Path]:
    root = _data_root()
    if not root.exists():
        return []
    return sorted(
        (f for f in root.iterdir() if f.is_file() and f.suffix.lower() in (".txt", ".pdf")),
        key=lambda f: f.name,
    )


def _versions_dir(name: str) -> Path:
    return Path(get_abs_path("kb_versions")) / hashlib.sha256(name.encode()).hexdigest()


def _safe_name(name: str) -> str:
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件名不能包含路径")
    return name


# ------------------------------------------------------------------ 登录


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response):
    if not security.admin_password():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="管理员登录未启用")
    try:
        token = security.issue_admin_token(payload.password)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="管理员密码不正确") from exc
    from utils.admin_auth import SESSION_SECONDS

    response.set_cookie(
        security.ADMIN_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, token: str = Depends(admin_token)):
    security.revoke_admin_token(token)
    response.delete_cookie(security.ADMIN_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    token = request.cookies.get(security.ADMIN_COOKIE)
    return {
        "admin": security.admin_token_valid(token),
        "enabled": bool(security.admin_password()),
    }


# ------------------------------------------------------------------ 概览


@router.get("/overview")
def overview(_: str = Depends(admin_token)):
    from utils.config_handler import chroma_conf, model_conf
    from utils.customer_service import feedback_rows

    files = _kb_files()
    rows = feedback_rows()
    meta_path = Path(get_abs_path(chroma_conf["persist_directory"])) / "kb_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except (ValueError, OSError):
        meta = {}
    chunks = sum(int(v.get("chunks", 0)) for v in meta.get("files", {}).values())

    return {
        "files": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "likes": sum(1 for r in rows if r.get("rating") == "有帮助"),
        "feedback": len(rows),
        "indexed_files": len(meta.get("files", {})),
        "chunks": chunks,
        "models": {
            "chat": model_conf["chat_model_name"],
            "embedding": model_conf["embedding_model_name"],
            "rerank": chroma_conf.get("rerank_model", ""),
            "rerank_enabled": chroma_conf.get("rerank_enabled", False),
        },
        "system": runtime.status(),
    }


@router.get("/models")
def get_models(_: str = Depends(admin_token)):
    from utils.config_handler import chroma_conf, model_conf

    return {
        "chat": model_conf["chat_model_name"],
        "embedding": model_conf["embedding_model_name"],
        "rerank": chroma_conf.get("rerank_model", ""),
    }


@router.put("/models")
async def put_models(payload: ModelsRequest, token: str = Depends(admin_token)):
    from utils.config_handler import model_conf
    from utils.model_settings import ConfigurationBusy, apply_models

    changed_embedding = payload.embedding.strip() != model_conf["embedding_model_name"]
    if changed_embedding and not payload.confirm_rebuild:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="更换嵌入模型需要重新生成整个知识库，请先确认重建",
        )
    try:
        message = await asyncio.to_thread(
            apply_models, token, payload.chat, payload.embedding, payload.rerank
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ConfigurationBusy as exc:
        # 初始化/重建正持有读锁：快速失败，而不是让管理员等满超时
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 失败时原配置已回滚，把原因给前端
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"模型配置未能应用，原配置和知识库已保留：{exc}",
        ) from exc
    return {"ok": True, "message": message}


# ------------------------------------------------------------- 知识文件


@router.get("/knowledge")
def list_knowledge(_: str = Depends(admin_token)):
    items = []
    for path in _kb_files():
        try:
            content = path.read_bytes()
        except OSError:
            continue
        versions = sorted(v.name for v in _versions_dir(path.name).glob("*")) if _versions_dir(path.name).exists() else []
        items.append({
            "name": path.name,
            "size": len(content),
            "version": hashlib.sha256(content).hexdigest()[:12],
            "versions": len(versions),
        })
    return {"files": items}


@router.post("/knowledge/preview")
async def preview_knowledge(
    file: UploadFile = File(...),
    _: str = Depends(admin_token),
):
    from utils.knowledge_admin import conflict_hints, preview

    name = _safe_name(file.filename or "")
    content = await file.read()
    try:
        text = await asyncio.to_thread(preview, name, content)
    except Exception as exc:  # noqa: BLE001 - 解析失败是预期分支（加密PDF/扫描件）
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in _kb_files()
        if p.name != name and p.suffix.lower() == ".txt"
    )
    warnings = await asyncio.to_thread(conflict_hints, text, existing)
    return {
        "name": name,
        "text": text[:5000],
        "truncated": len(text) > 5000,
        "warnings": warnings,
        "exists": (_data_root() / name).exists(),
        "size": len(content),
    }


@router.post("/knowledge/publish")
async def publish_knowledge(
    file: UploadFile = File(...),
    model: str = Form(""),
    scope: str = Form(""),
    replace: bool = Form(False),
    _: str = Depends(admin_token),
):
    from utils.knowledge_admin import publish

    name = _safe_name(file.filename or "")
    content = await file.read()
    try:
        message = await asyncio.to_thread(
            publish, name, content, security.admin_password(),
            model=model, scope=scope, replace=replace,
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="同名文件已存在，请确认替换") from exc
    except Exception as exc:  # noqa: BLE001 - 失败时旧版本与旧索引都已还原
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "message": message}


@router.delete("/knowledge/{name}")
async def delete_knowledge(name: str, _: str = Depends(admin_token)):
    from utils.knowledge_admin import publish

    _safe_name(name)
    try:
        message = await asyncio.to_thread(
            publish, name, b"", security.admin_password(), delete=True
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="文件不存在") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "message": message}


@router.get("/knowledge/versions/{name}")
def list_versions(name: str, _: str = Depends(admin_token)):
    _safe_name(name)
    folder = _versions_dir(name)
    if not folder.exists():
        return {"versions": []}
    return {"versions": sorted(v.name for v in folder.glob("*") if v.is_file())}


@router.get("/knowledge/versions/{name}/{version_name}")
def download_version(name: str, version_name: str, _: str = Depends(admin_token)):
    _safe_name(name)
    _safe_name(version_name)
    path = _versions_dir(name) / version_name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="版本不存在")
    return FileResponse(path, filename=name)


# --------------------------------------------------------------- 反馈


@router.get("/feedback")
async def feedback(_: str = Depends(admin_token), limit: int = Query(default=200, ge=1, le=2000)):
    from utils.customer_service import feedback_rows

    rows = await asyncio.to_thread(feedback_rows)
    return {"rows": rows[:limit], "total": len(rows)}
