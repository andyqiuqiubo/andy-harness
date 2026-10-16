"""REST API —— 知识库：KB / 文档 / 分段 / 嵌入模型管理、检索与技能入口。

契约来源：《知识库系统-完整解决方案.md》第 8 节（接口设计）与 5.4.3（检索入参/返回）。

路由分三个前缀（同模块导出，main.py 统一 include）：

- ``/api/kb``                      KB / 文档 / 分段 / 嵌入模型管理面（写接口走 require_admin）；
- ``/api/retrieval``               统一检索（方案 5.4.3 入参/返回形状）；
- ``/api/skills/knowledge_base_search``  技能入口，供大模型 function calling 调用（方案 5.5）。

服务解析：优先取 ServiceRegistry 中注册的 KnowledgeBaseService（main.py 装配）；
未注册时用主库现造兜底（与 attachments 等路由直接 ``Database()`` 的既有约定一致）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.infra.database import Database
from harness.kernel.services import ServiceRegistry
from harness.modules.knowledge_base.service import (
    KBServiceError,
    KnowledgeBaseService,
)

logger = logging.getLogger("harness.api.rest.knowledge_base")

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])
retrieval_router = APIRouter(prefix="/api/retrieval", tags=["knowledge-base"])
skill_router = APIRouter(prefix="/api/skills", tags=["knowledge-base"])

_services: ServiceRegistry | None = None


def setup_knowledge_base_routes(services: ServiceRegistry) -> None:
    """注册知识库路由（注入服务注册表）。"""
    global _services
    _services = services


def _service() -> KnowledgeBaseService:
    """取知识库服务：优先注册表实例，未注册则用主库现造（兜底不 503）。"""
    if _services is not None and _services.has(KnowledgeBaseService):
        svc: KnowledgeBaseService = _services.get(KnowledgeBaseService)
        return svc
    return KnowledgeBaseService(Database())


# KBServiceError.code → HTTP 状态码（缺省 400）
_STATUS_MAP = {
    "KB_NOT_FOUND": 404,
    "DOC_NOT_FOUND": 404,
    "KB_FILE_TOO_LARGE": 413,
    "DOC_ORIGIN_MISSING": 410,
    "KB_DIM_MISMATCH": 409,
}


def _api_error(e: KBServiceError) -> APIError:
    """把业务错误转成统一 API 错误（保留 code 供前端差异化处理）。"""
    return APIError(e.code, str(e), _STATUS_MAP.get(e.code, 400))


# ── 请求模型 ──────────────────────────────────────────────────────────


class CreateKBRequest(BaseModel):
    """建知识库请求（方案 8：POST /api/kb）。

    ``name`` 不做长度校验：空名由 service 抛 ``KB_NAME_REQUIRED`` 转 400。
    """

    name: str = Field(description="知识库名称")
    description: str = Field(default="", description="描述")
    embedding_model_id: str | None = Field(default=None, description="绑定的嵌入模型 id")
    default_retrieval_mode: str = Field(default="keyword", description="keyword/semantic/hybrid")


class UpdateKBRequest(BaseModel):
    """改知识库请求（均为可选，局部更新）。"""

    name: str | None = None
    description: str | None = None
    default_retrieval_mode: str | None = None


class SetEmbeddingModelRequest(BaseModel):
    """绑定 / 更换嵌入模型（方案 8：PUT /api/kb/{id}/embedding-model，置 need_reindex）。"""

    embedding_model_id: str | None = Field(default=None, description="模型 id；null 表示解绑")


class RetrievalRequest(BaseModel):
    """检索请求（方案 5.4.3 原样）。

    ``query`` 不做长度校验：空查询由 service 抛 ``QUERY_REQUIRED`` 转 400
    （方案 5.4.4 预期行为），而非框架默认的 422。
    """

    query: str = Field(description="查询文本")
    knowledge_base_ids: list[str] | None = Field(default=None, description="限定知识库；缺省全部")
    retrieval_mode: str | None = Field(default=None, description="keyword/semantic/hybrid")
    top_k: int | None = Field(default=None, ge=1, le=50, description="返回条数，默认 6")
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    rerank: bool | None = Field(default=None, description="是否重排；缺省按 KB 配置")


class SkillSearchRequest(BaseModel):
    """技能检索请求（方案 5.5.2 参数 + 强制开关）。

    ``force_retrieve`` / ``force_skip`` 是**流水线级**强制开关，仅供确定性编排
    覆盖模型决策；不通过 function calling 暴露给模型（工具 schema 见 tool.py）。
    """

    query: str = Field(description="检索 query（空串由 service 校验返 400）")
    knowledge_base_ids: list[str] | None = None
    retrieval_mode: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    rerank: bool | None = None
    force_retrieve: bool = Field(default=False, description="跳过子查询拆分，强制单次全量检索")
    force_skip: bool = Field(default=False, description="强制跳过检索（确定性流水线覆盖模型决策）")


class EmbeddingModelRequest(BaseModel):
    """嵌入 / 重排模型配置（方案 5.3）。密钥仅用于传输，落库为密文。"""

    name: str = Field(min_length=1)
    provider: str = "openai"
    model_name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    api_key: str = ""
    dimensions: int = Field(default=0, ge=0)
    normalize: bool = True
    batch_size: int = Field(default=32, ge=1)
    rerank_enabled: bool = False
    rerank_model: str = ""
    rerank_endpoint: str = ""
    rerank_api_key: str = ""
    rerank_top_n: int = Field(default=10, ge=1)


# ── 知识库管理面（/api/kb）────────────────────────────────────────────


@router.get("", summary="列出知识库（附文档/分段/向量统计）")
async def list_kbs() -> dict[str, Any]:
    """列出全部知识库；每项附 doc_count / segment_count / vector_count 供 UI 卡片。"""
    kbs = _service().list_kbs()
    return {"knowledge_bases": kbs, "total": len(kbs)}


@router.post("", summary="创建知识库")
async def create_kb(
    req: CreateKBRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """建知识库（方案 8：POST /api/kb，可同时绑定嵌入模型）。"""
    try:
        return _service().create_kb(
            req.name,
            req.description,
            req.embedding_model_id,
            req.default_retrieval_mode,
        )
    except KBServiceError as e:
        raise _api_error(e)


# ── 嵌入 / 重排模型配置（静态段须先于 /{kb_id} 注册）──────────────────


@router.get("/embedding-models", summary="列出嵌入/重排模型配置（密钥脱敏）")
async def list_embedding_models() -> dict[str, Any]:
    models = _service().list_embedding_models()
    return {"models": models, "total": len(models)}


@router.post("/embedding-models", summary="新建嵌入模型（保存前连通性探针）")
async def create_embedding_model(
    req: EmbeddingModelRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """新建模型配置。探针失败拒绝保存（方案 5.3.3）；密钥加密存储、返回脱敏。"""
    try:
        return await _service().create_embedding_model(**req.model_dump())
    except KBServiceError as e:
        raise _api_error(e)


@router.post("/embedding-models/probe", summary="测试嵌入模型连通性（不保存）")
async def probe_embedding_model(
    req: EmbeddingModelRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """「测试连接」按钮：探针成功返回实际维度，失败抛错（方案 5.3.3）。"""
    try:
        return await _service().probe_embedding_model(
            endpoint=req.endpoint,
            api_key=req.api_key,
            model_name=req.model_name,
            dimensions=req.dimensions,
        )
    except KBServiceError as e:
        raise _api_error(e)


@router.put("/embedding-models/{model_id}", summary="更新嵌入模型（连接参数变化时重新探针）")
async def update_embedding_model(
    model_id: str,
    req: EmbeddingModelRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """局部更新；仅传新密钥时才覆盖密文（避免空串覆盖）。"""
    try:
        updated = await _service().update_embedding_model(model_id, **req.model_dump(exclude_unset=True))
    except KBServiceError as e:
        raise _api_error(e)
    if updated is None:
        raise APIError("KB_MODEL_NOT_FOUND", "嵌入模型不存在", 404)
    return updated


@router.delete("/embedding-models/{model_id}", summary="删除嵌入模型（解除 KB 绑定）")
async def delete_embedding_model(
    model_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    ok = _service().delete_embedding_model(model_id)
    if not ok:
        raise APIError("KB_MODEL_NOT_FOUND", "嵌入模型不存在", 404)
    return {"deleted": model_id}


# ── 单个知识库 ────────────────────────────────────────────────────────


@router.get("/{kb_id}", summary="知识库详情")
async def get_kb(kb_id: str) -> dict[str, Any]:
    kb = _service().get_kb(kb_id)
    if not kb:
        raise APIError("KB_NOT_FOUND", "知识库不存在", 404)
    return kb


@router.put("/{kb_id}", summary="更新知识库（改名 / 改默认检索模式）")
async def update_kb(
    kb_id: str,
    req: UpdateKBRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """局部更新；未传的字段保持原值。嵌入模型变更走专用端点（会置 need_reindex）。"""
    fields = req.model_dump(exclude_unset=True)
    try:
        kb = _service().update_kb(kb_id, **fields)
    except KBServiceError as e:
        raise _api_error(e)
    if kb is None:
        raise APIError("KB_NOT_FOUND", "知识库不存在", 404)
    return kb


@router.delete("/{kb_id}", summary="删除知识库（含文档/分段/落盘文件）")
async def delete_kb(
    kb_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    ok = _service().delete_kb(kb_id)
    if not ok:
        raise APIError("KB_NOT_FOUND", "知识库不存在", 404)
    return {"deleted": kb_id}


@router.put("/{kb_id}/embedding-model", summary="绑定/更换嵌入模型（自动置 need_reindex）")
async def set_embedding_model(
    kb_id: str,
    req: SetEmbeddingModelRequest,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """换模型后 KB 标记 need_reindex；重建完成前旧索引仍可用（方案 5.3.4）。"""
    try:
        kb = _service().update_kb(kb_id, embedding_model_id=req.embedding_model_id)
    except KBServiceError as e:
        raise _api_error(e)
    if kb is None:
        raise APIError("KB_NOT_FOUND", "知识库不存在", 404)
    return kb


@router.post("/{kb_id}/reindex", summary="全量重建向量索引（换模型后一键重建）")
async def reindex_kb(
    kb_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """对全部分段重算向量（先清旧向量再重写，避免异维度残留，方案 5.3）。"""
    try:
        return await _service().build_vectors(kb_id, only_missing=False)
    except KBServiceError as e:
        raise _api_error(e)


# ── 文档 ──────────────────────────────────────────────────────────────


@router.get("/{kb_id}/documents", summary="列出知识库下的文档")
async def list_documents(kb_id: str) -> dict[str, Any]:
    docs = _service().list_documents(kb_id)
    return {"documents": docs, "total": len(docs)}


@router.post("/{kb_id}/documents", summary="上传文档（触发摄取管道）")
async def upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(...),
    strategy: str = Form("auto"),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """上传一个或多个文档：落盘原文件 → 解析 → 清洗 → 分段 → 建索引（方案 7.1）。

    ``strategy`` 支持 auto/S1/S2/S3/S4 手动覆盖分段策略（方案 5.2.1）。
    单个文档解析失败仅该文档 status=failed，不影响其余（方案 5.1.5）。
    """
    svc = _service()
    results: list[dict[str, Any]] = []
    for f in files:
        data = await f.read()
        try:
            doc = await svc.add_document(kb_id, f.filename or "unknown", data, strategy=strategy)
        except KBServiceError as e:
            raise _api_error(e)
        results.append(doc)
    return {"documents": results, "total": len(results)}


@router.get("/{kb_id}/documents/{doc_id}", summary="查询文档状态")
async def get_document(kb_id: str, doc_id: str) -> dict[str, Any]:
    doc = _service().get_document(doc_id)
    if not doc or doc.get("kb_id") != kb_id:
        raise APIError("DOC_NOT_FOUND", "文档不存在", 404)
    return doc


@router.delete("/{kb_id}/documents/{doc_id}", summary="删除文档（含分段与落盘文件）")
async def delete_document(
    kb_id: str,
    doc_id: str,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    doc = _service().get_document(doc_id)
    if not doc or doc.get("kb_id") != kb_id:
        raise APIError("DOC_NOT_FOUND", "文档不存在", 404)
    _service().delete_document(doc_id)
    return {"deleted": doc_id}


@router.post("/{kb_id}/documents/{doc_id}/reindex", summary="按文档重建索引")
async def reindex_document(
    kb_id: str,
    doc_id: str,
    strategy: str | None = None,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """清除旧分段后重跑摄取管道（方案 5.1.5）；strategy 可换分段策略重切。"""
    doc = _service().get_document(doc_id)
    if not doc or doc.get("kb_id") != kb_id:
        raise APIError("DOC_NOT_FOUND", "文档不存在", 404)
    try:
        return await _service().reindex_document(doc_id, strategy)
    except KBServiceError as e:
        raise _api_error(e)


@router.get("/{kb_id}/segments", summary="列出分段（排障；可按文档过滤）")
async def list_segments(kb_id: str, doc_id: str | None = None) -> dict[str, Any]:
    svc = _service()
    if doc_id:
        segs = [s for s in svc.list_segments(doc_id) if s.get("kb_id") == kb_id]
    else:
        segs = svc.segments.list_by_kb(kb_id)
    return {"segments": segs, "total": len(segs)}


# ── 检索（/api/retrieval，方案 5.4.3）─────────────────────────────────


@retrieval_router.post("", summary="知识库检索")
async def retrieval(req: RetrievalRequest) -> dict[str, Any]:
    """统一检索入口。

    - 空查询 → 400；空库 → 200 + 空列表；
    - semantic 无向量索引 → 400（missing_vector_index）；
    - hybrid 仅全文可用 → 200 + warn=missing_vector_index（优雅降级）。
    """
    try:
        return await _service().search(
            req.query,
            kb_ids=req.knowledge_base_ids,
            mode=req.retrieval_mode,
            top_k=req.top_k,
            score_threshold=req.score_threshold,
            rerank=req.rerank,
        )
    except KBServiceError as e:
        raise _api_error(e)


# ── 技能入口（/api/skills/knowledge_base_search，方案 5.5）────────────


@skill_router.post("/knowledge_base_search", summary="知识库检索技能（供大模型调用）")
async def knowledge_base_search(req: SkillSearchRequest) -> dict[str, Any]:
    """技能入口：提问分析 → 路由/多跳检索 → 来源标注。

    与 /api/retrieval 的差异：内部多一步提问分析（语言/实体/子查询/KB 路由），
    返回体附 analysis；支持 force_retrieve / force_skip 强制开关。
    """
    try:
        return await _service().skill_search(
            req.query,
            knowledge_base_ids=req.knowledge_base_ids,
            retrieval_mode=req.retrieval_mode,
            top_k=req.top_k,
            rerank=req.rerank,
            force_retrieve=req.force_retrieve,
            force_skip=req.force_skip,
        )
    except KBServiceError as e:
        raise _api_error(e)
