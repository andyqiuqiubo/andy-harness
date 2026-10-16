"""知识库服务 —— 编排：上传→抽取→分段→索引、检索、重建索引、技能检索。

职责边界（本模块只做编排，不做底层实现）：
- :class:`KnowledgeBaseService` 组合 repository / parsers / chunking / embedding /
  retrieval / analysis，对上（REST 层）暴露稳定语义；
- 空文档（抽取后无正文且无资产）标 ``status=empty``，不报错；
- 检索在缺向量/空库时优雅降级（方案 5.4.4）：hybrid 降级并
  ``warn=missing_vector_index``；semantic 无向量索引则以
  ``KBServiceError(code=missing_vector_index)`` 上抛，REST 转 400。

嵌入为**可选项**：未配置嵌入模型时只建全文索引（keyword 可用）；
配置后 ``build_vectors`` 为每个分段写入向量，semantic/hybrid 随之生效。

图片资产（方案 5.1.2）：抽取后尽力补描述——
``HARNESS_KB_VLM="provider_id:model"`` 显式配置时调用 VLM 生成描述，
否则降级为占位（环境无 OCR 库），**图片始终以独立资产分段参与检索**。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from . import chunking
from .analysis import analyze_query, route_kbs
from .chunking import Asset, Segment
from .embedding import EmbeddingClient, RerankClient, decrypt_key, encrypt_key, vec_to_bytes
from .limits import (
    ASSET_DESCRIPTION_MAX_CHARS,
    DEFAULT_TOP_K,
    IMAGE_DESC_PLACEHOLDER,
    MAX_DOCUMENT_BYTES,
    MAX_SUBQUERIES,
    knowledge_assets_dir,
    knowledge_dir,
    knowledge_originals_dir,
)
from .parsers import ParseError, detect_stage, has_adapter, parse_document
from .repository import (
    KBDocumentRepository,
    KBEmbeddingModelRepository,
    KBSegmentRepository,
    KnowledgeBaseRepository,
)
from .retrieval import retrieve

logger = logging.getLogger("harness.kb.service")


class KBServiceError(Exception):
    """知识库业务错误（REST 层转 APIError）。

    ``code`` 供前端差异化处理（如 missing_vector_index → 引导配置模型）。
    """

    def __init__(self, message: str, code: str = "KB_ERROR") -> None:
        self.code = code
        super().__init__(message)


class KnowledgeBaseService:
    """知识库编排服务。构造时注入 Database，内部按需实例化各 repository。"""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.kbs = KnowledgeBaseRepository(db)
        self.docs = KBDocumentRepository(db)
        self.segments = KBSegmentRepository(db)
        self.models = KBEmbeddingModelRepository(db)

    # ── 知识库 ────────────────────────────────────────────────────────

    def create_kb(
        self,
        name: str,
        description: str = "",
        embedding_model_id: str | None = None,
        default_retrieval_mode: str = "keyword",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise KBServiceError("知识库名称不能为空", "KB_NAME_REQUIRED")
        if default_retrieval_mode not in ("keyword", "semantic", "hybrid"):
            raise KBServiceError("默认检索模式必须是 keyword/semantic/hybrid", "KB_INVALID_MODE")
        return self.kbs.create(name, description, embedding_model_id, default_retrieval_mode)

    def list_kbs(self) -> list[dict[str, Any]]:
        """列出知识库，并附文档数 / 分段数 / 向量覆盖（供 UI 卡片展示）。"""
        kbs = self.kbs.list()
        if not kbs:
            return []
        doc_rows = self._db.query("SELECT kb_id, COUNT(*) AS c FROM kb_documents GROUP BY kb_id")
        seg_rows = self._db.query(
            "SELECT kb_id, COUNT(*) AS c, SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS v "
            "FROM kb_segments GROUP BY kb_id"
        )
        doc_count = {r["kb_id"]: r["c"] for r in doc_rows}
        seg_stat = {r["kb_id"]: (r["c"], r["v"] or 0) for r in seg_rows}
        for kb in kbs:
            total, vecs = seg_stat.get(kb["id"], (0, 0))
            kb["doc_count"] = doc_count.get(kb["id"], 0)
            kb["segment_count"] = total
            kb["vector_count"] = vecs
        return kbs

    def get_kb(self, kb_id: str) -> dict[str, Any] | None:
        return self.kbs.get(kb_id)

    def update_kb(self, kb_id: str, **fields: Any) -> dict[str, Any] | None:
        kb = self.kbs.get(kb_id)
        if not kb:
            return None
        if "default_retrieval_mode" in fields and fields["default_retrieval_mode"] not in (
            "keyword",
            "semantic",
            "hybrid",
        ):
            raise KBServiceError("默认检索模式必须是 keyword/semantic/hybrid", "KB_INVALID_MODE")
        # 切换 / 解绑嵌入模型 → 标记 need_reindex，提醒重建向量（方案 5.3）
        if "embedding_model_id" in fields and fields["embedding_model_id"] != kb.get("embedding_model_id"):
            fields["need_reindex"] = 1
        return self.kbs.update(kb_id, **fields)

    def delete_kb(self, kb_id: str) -> bool:
        """删除知识库：分段+FTS 先删（FTS 虚表无级联），再删库与落盘文件。"""
        kb = self.kbs.get(kb_id)
        if not kb:
            return False
        for doc in self.docs.list_by_kb(kb_id):
            self.segments.delete_by_doc(doc["id"])
        ok = self.kbs.delete(kb_id)  # kb_documents / kb_segments 随 FK 级联
        self._remove_dir(Path(knowledge_dir()) / kb_id)
        return ok

    # ── 文档上传 → 抽取 → 分段 → 索引 ─────────────────────────────────

    async def add_document(
        self,
        kb_id: str,
        filename: str,
        data: bytes,
        strategy: str = "auto",
    ) -> dict[str, Any]:
        kb = self.kbs.get(kb_id)
        if not kb:
            raise KBServiceError("知识库不存在", "KB_NOT_FOUND")
        if not data:
            raise KBServiceError("文件内容为空", "KB_EMPTY_FILE")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise KBServiceError(f"文件过大（上限 {MAX_DOCUMENT_BYTES // 1024 // 1024}MB）", "KB_FILE_TOO_LARGE")

        stage = detect_stage(filename)
        if stage != "P1" and not has_adapter(filename):
            raise KBServiceError(
                f"暂不支持的格式 {filename}（P1 支持 txt/md/csv/json/log/docx/pdf/图片；"
                f"音视频等 P2 格式需先注册 FormatAdapter）",
                "KB_UNSUPPORTED_FORMAT",
            )

        doc = self.docs.create(
            kb_id=kb_id,
            name=filename,
            file_type=filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
            origin_path="",
            storage_stage=stage,
            status="processing",
        )
        doc_id = doc["id"]
        assets_dir = knowledge_assets_dir(kb_id, doc_id)
        originals_dir = knowledge_originals_dir(kb_id, doc_id)
        # 原始文件落盘（便于按文档重建 / 追溯）
        origin_path = f"{originals_dir}/{uuid.uuid4().hex}__{Path(filename).name}"
        try:
            Path(origin_path).write_bytes(data)
        except Exception as e:  # noqa: BLE001
            logger.warning("原始文件落盘失败（不影响索引）: %s", e)
            origin_path = ""

        return await self._process_document(kb, doc_id, filename, data, assets_dir, origin_path, strategy)

    async def _process_document(
        self,
        kb: dict[str, Any],
        doc_id: str,
        filename: str,
        data: bytes,
        assets_dir: str,
        origin_path: str,
        strategy: str,
    ) -> dict[str, Any]:
        """抽取 → 补图片描述 → 清洗 → 分段 → 写库（FTS 同步）。

        向量不在此处同步生成（依赖网络），由 ``build_vectors`` 补齐——
        上传完成后本方法会尽力自动补一次（仅缺失段），失败不阻塞上传。
        """
        try:
            # 解析放线程执行：P2 网络适配器（ASR 转写等）是阻塞 IO，不能卡事件循环；
            # P1 本地解析（PDF/DOCX 解压提取）同样受益，不阻塞其它请求。
            raw_text, assets = await asyncio.to_thread(parse_document, data, filename, assets_dir, doc_id)
        except ParseError as e:
            self.docs.update(doc_id, status="failed", error=str(e), origin_path=origin_path)
            raise KBServiceError(f"文档解析失败: {e}", "KB_PARSE_FAILED") from e

        if assets:
            await self._describe_assets(assets)

        cleaned = chunking.clean_text(raw_text)
        # 图片独立成段（方案 5.1.2：图片抽取为资产并参与检索，不因缺描述而丢）
        asset_segments = [chunking.build_asset_segment(a) for a in assets]

        if not cleaned.strip() and not asset_segments:
            # 空文档：无文本也无资产 → status=empty，不建索引（方案 5.2.4）
            self.docs.update(doc_id, status="empty", origin_path=origin_path, char_count=0, asset_count=0)
            return self.docs.get(doc_id) or {}

        if cleaned.strip():
            segs, used_strategy = chunking.segment_document(cleaned, [], strategy)
        else:
            segs, used_strategy = [], "S1"  # 纯图片文档
        segs.extend(asset_segments)

        self.segments.delete_by_doc(doc_id)  # 先清旧（重建场景）
        seg_ids = self._write_segments(kb["id"], doc_id, segs)

        self.docs.update(
            doc_id,
            status="ready",
            segment_count=len(seg_ids),
            char_count=sum(len(s.content) for s in segs),
            asset_count=len(assets),
            segment_strategy=used_strategy,
            origin_path=origin_path,
            error="",
        )
        doc = self.docs.get(doc_id) or {}

        # 上传后自动补向量（配置了嵌入模型才做；失败不阻塞，可稍后手动重建）
        if kb.get("embedding_model_id") and seg_ids:
            try:
                await self.build_vectors(kb["id"], only_missing=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("上传后自动建向量失败（可稍后手动重建）: %s", e)
        return doc

    def _write_segments(self, kb_id: str, doc_id: str, segs: list[Segment]) -> list[str]:
        """逐段写入 kb_segments（FTS 同步）。同一文档内相同 hash 去重（方案 7.1）。"""
        seg_ids: list[str] = []
        seen_hashes: set[str] = set()
        for pos, seg in enumerate(segs):
            h = seg.compute_hash()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            seg_ids.append(self.segments.create(doc_id=doc_id, kb_id=kb_id, segment=seg, position=pos, seg_id=seg.id))
        return seg_ids

    async def reindex_document(self, doc_id: str, strategy: str | None = None) -> dict[str, Any]:
        """按文档重建索引（方案 5.1.5）：清除旧分段后重跑摄取管道。"""
        doc = self.docs.get(doc_id)
        if not doc:
            raise KBServiceError("文档不存在", "DOC_NOT_FOUND")
        kb = self.kbs.get(doc["kb_id"])
        if not kb:
            raise KBServiceError("知识库不存在", "KB_NOT_FOUND")
        origin = doc.get("origin_path") or ""
        if not origin or not Path(origin).exists():
            raise KBServiceError("原始文件不存在，无法重建（请重新上传）", "DOC_ORIGIN_MISSING")
        data = Path(origin).read_bytes()
        assets_dir = knowledge_assets_dir(kb["id"], doc_id)
        used = strategy or doc.get("segment_strategy") or "auto"
        return await self._process_document(kb, doc_id, doc["name"], data, assets_dir, origin, used)

    def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        return self.docs.list_by_kb(kb_id)

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        return self.docs.get(doc_id)

    def delete_document(self, doc_id: str) -> bool:
        """删除文档：先删分段（含 FTS），再删记录与落盘文件。"""
        doc = self.docs.get(doc_id)
        if not doc:
            return False
        self.segments.delete_by_doc(doc_id)
        ok = self.docs.delete(doc_id)
        base = Path(knowledge_dir()) / doc["kb_id"]
        for sub in ("originals", "assets"):
            self._remove_dir(base / sub / doc_id)
        return ok

    def list_segments(self, doc_id: str) -> list[dict[str, Any]]:
        return self.segments.list_by_doc(doc_id)

    @staticmethod
    def _remove_dir(path: Path) -> None:
        import shutil

        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("清理目录失败 %s: %s", path, e)

    # ── 图片资产描述（OCR/VLM，方案 5.1.2）─────────────────────────────

    async def _describe_assets(self, assets: list[Asset]) -> None:
        """为图片资产尽力补描述；全部不可用时写占位，**绝不丢资产**。

        本环境无 OCR 库；VLM 经 ``HARNESS_KB_VLM="provider_id:model"`` 显式启用
        （复用 Provider 的 OpenAI 兼容视觉接口，同 attachment 的 image_url 约定）。
        """
        vlm_spec = (os.environ.get("HARNESS_KB_VLM") or "").strip()
        if vlm_spec and ":" in vlm_spec:
            provider_id, _, model = vlm_spec.partition(":")
            for asset in assets:
                desc = await self._vlm_describe(provider_id, model.strip(), asset)
                if desc:
                    asset.description = desc[:ASSET_DESCRIPTION_MAX_CHARS]
        for asset in assets:
            if not (asset.description or "").strip():
                asset.description = IMAGE_DESC_PLACEHOLDER

    async def _vlm_describe(self, provider_id: str, model: str, asset: Asset) -> str:
        """调用 VLM 生成单张图片描述；任何失败返回空串（由调用方降级占位）。"""
        import base64
        import mimetypes

        try:
            path = Path(asset.storage_path)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            from harness.main import _services  # noqa: PLC0415 — 避免顶层循环导入
            from harness.modules.model_manager.provider_registry import (  # noqa: PLC0415
                ProviderRegistry,
            )

            provider = _services.get(ProviderRegistry).get_provider(provider_id)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用一句中文描述这张图片的内容（用于检索索引）。"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ]
            text = ""
            async for chunk in provider.chat(messages=messages, model=model, stream=True):
                text += chunk.get("delta") or ""
            return text.strip()
        except Exception as e:  # noqa: BLE001
            logger.info("VLM 图片描述不可用（降级占位）: %s", e)
            return ""

    # ── 向量索引构建 ───────────────────────────────────────────────────

    async def build_vectors(self, kb_id: str, only_missing: bool = False) -> dict[str, Any]:
        """为知识库分段补齐嵌入向量。

        - 未配置嵌入模型 → 跳过（全文索引不受影响），返回提示；
        - ``only_missing=True`` 只补 ``embedding IS NULL`` 的段（上传后自动路径）；
        - 全量重建会清除旧向量后重写（换模型场景，方案 5.3）。
        """
        kb = self.kbs.get(kb_id)
        if not kb:
            raise KBServiceError("知识库不存在", "KB_NOT_FOUND")
        model_id = kb.get("embedding_model_id")
        if not model_id:
            return {"embedded": 0, "note": "未配置嵌入模型，已跳过（全文索引不受影响）"}
        client = self._embedding_client(model_id)
        if client is None:
            return {"embedded": 0, "note": "嵌入模型配置无效，已跳过"}

        if only_missing:
            segs = [s for s in self.segments.list_by_kb(kb_id) if not s.get("has_embedding")]
        else:
            segs = self.segments.list_by_kb(kb_id)
            # 全量重建：先清旧向量，避免换模型后残留异维度向量（方案 5.3）
            for s in segs:
                if s.get("has_embedding"):
                    self._db.execute("UPDATE kb_segments SET embedding = NULL WHERE id = ?", (s["id"],))
        if not segs:
            self.kbs.update(kb_id, need_reindex=0)
            return {"embedded": 0, "note": ""}

        texts = [s["clean_fulltext"] or s["content"] for s in segs]
        vectors = await client.embed(texts)
        dims = client.dimensions or (len(vectors[0]) if vectors else 0)
        for seg, vec in zip(segs, vectors, strict=False):
            if dims and len(vec) != dims:
                raise KBServiceError(
                    f"向量维度不一致（声明 {dims}，实际 {len(vec)}），请核对模型配置", "KB_DIM_MISMATCH"
                )
            self.segments.update_embedding(seg["id"], vec_to_bytes(vec))
        # 全文索引始终存在，向量写入后索引类型升级为 both（方案 6 index_type 语义）
        self.kbs.update(kb_id, need_reindex=0, index_type="both")
        return {"embedded": len(segs), "note": ""}

    def _embedding_client(self, model_id: str) -> EmbeddingClient | None:
        row = self.models.get(model_id)
        if not row:
            return None
        return EmbeddingClient(
            endpoint=row.get("endpoint", ""),
            api_key=decrypt_key(row.get("api_key_encrypted", "")),
            model_name=row.get("model_name", ""),
            dimensions=int(row.get("dimensions", 0) or 0),
            batch_size=int(row.get("batch_size", 32) or 32),
            do_normalize=bool(row.get("normalize", 1)),
        )

    def _rerank_client(self, model_id: str | None) -> RerankClient | None:
        if not model_id:
            return None
        row = self.models.get(model_id)
        if not row or not row.get("rerank_enabled"):
            return None
        endpoint = (row.get("rerank_endpoint") or "").strip()
        rerank_model = (row.get("rerank_model") or "").strip()
        if not endpoint or not rerank_model:
            return None
        return RerankClient(
            endpoint=endpoint,
            api_key=decrypt_key(row.get("rerank_api_key_encrypted", "")),
            model_name=rerank_model,
            top_n=int(row.get("rerank_top_n", 10) or 10),
        )

    # ── 嵌入 / 重排模型配置（方案 5.3）────────────────────────────────

    async def create_embedding_model(self, **fields: Any) -> dict[str, Any]:
        """新建嵌入模型。保存前必做连通性探针（失败拒绝保存，方案 5.3.3）。

        密钥加密存储；返回时脱敏。dimensions 未声明则以探针实际维度回填。
        """
        api_key = fields.pop("api_key", "") or ""
        rerank_key = fields.pop("rerank_api_key", "") or ""
        fields["api_key_encrypted"] = encrypt_key(api_key)
        fields["rerank_api_key_encrypted"] = encrypt_key(rerank_key)
        for required in ("name", "model_name", "endpoint"):
            if not (fields.get(required) or "").strip():
                raise KBServiceError(f"缺少必填项: {required}", "KB_MODEL_INCOMPLETE")

        probe_client = EmbeddingClient(
            endpoint=fields.get("endpoint", ""),
            api_key=api_key,
            model_name=fields.get("model_name", ""),
            dimensions=int(fields.get("dimensions", 0) or 0),
        )
        try:
            actual_dims = await probe_client.probe()
        except Exception as e:  # noqa: BLE001
            raise KBServiceError(f"连通性探测失败，配置未保存: {e}", "KB_PROBE_FAILED") from e
        if not fields.get("dimensions"):
            fields["dimensions"] = actual_dims
        fields["probe_ok"] = 1
        created = self.models.create(**fields)
        return self._mask_model(created)

    async def update_embedding_model(self, model_id: str, **fields: Any) -> dict[str, Any] | None:
        """更新嵌入模型；连接参数变化时重新探针（失败标记 probe_ok=0）。

        仅当传入新密钥时才覆盖密文（避免「空串覆盖」）。
        """
        row = self.models.get(model_id)
        if not row:
            return None
        if "api_key" in fields:
            key = fields.pop("api_key") or ""
            if key:
                fields["api_key_encrypted"] = encrypt_key(key)
        if "rerank_api_key" in fields:
            key = fields.pop("rerank_api_key") or ""
            if key:
                fields["rerank_api_key_encrypted"] = encrypt_key(key)
        for required in ("name", "model_name", "endpoint"):
            if required in fields and not (fields[required] or "").strip():
                raise KBServiceError(f"缺少必填项: {required}", "KB_MODEL_INCOMPLETE")
        if not fields:
            return self._mask_model(row)

        changed = any(fields.get(k, row.get(k)) != row.get(k) for k in ("endpoint", "model_name", "dimensions"))
        self.models.update(model_id, **fields)
        if changed:
            client = self._embedding_client(model_id)
            if client is not None:
                try:
                    await client.probe()
                    self.models.update(model_id, probe_ok=1)
                except Exception as e:  # noqa: BLE001
                    logger.warning("更新嵌入模型后探针失败: %s", e)
                    self.models.update(model_id, probe_ok=0)
        updated = self.models.get(model_id)
        return self._mask_model(updated) if updated else None

    async def probe_embedding_model(self, **fields: Any) -> dict[str, Any]:
        """独立探针（保存前「测试连接」按钮）：成功返回实际维度，失败抛错。"""
        client = EmbeddingClient(
            endpoint=str(fields.get("endpoint") or ""),
            api_key=str(fields.get("api_key") or ""),
            model_name=str(fields.get("model_name") or ""),
            dimensions=int(fields.get("dimensions") or 0),
        )
        try:
            dims = await client.probe()
        except Exception as e:  # noqa: BLE001
            raise KBServiceError(f"连通性探测失败: {e}", "KB_PROBE_FAILED") from e
        return {"ok": True, "dimensions": dims}

    def list_embedding_models(self) -> list[dict[str, Any]]:
        return [self._mask_model(m) for m in self.models.list()]

    def get_embedding_model(self, model_id: str) -> dict[str, Any] | None:
        row = self.models.get(model_id)
        return self._mask_model(row) if row else None

    def delete_embedding_model(self, model_id: str) -> bool:
        # 解除引用它的 KB 绑定（保留 need_reindex 标记提示用户）
        for kb in self.kbs.list():
            if kb.get("embedding_model_id") == model_id:
                self.kbs.update(kb["id"], embedding_model_id=None, need_reindex=1)
        return self.models.delete(model_id)

    @staticmethod
    def _mask_model(row: dict[str, Any]) -> dict[str, Any]:
        """脱敏：不返回任何密钥密文/明文，仅标记是否已配置。"""
        out = dict(row)
        out["has_api_key"] = bool(out.pop("api_key_encrypted", ""))
        out["has_rerank_key"] = bool(out.pop("rerank_api_key_encrypted", ""))
        return out

    # ── 检索（方案 5.4）───────────────────────────────────────────────

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        mode: str | None = None,
        top_k: int | None = None,
        score_threshold: float = 0.0,
        rerank: bool | None = None,
    ) -> dict[str, Any]:
        """统一检索（可跨多个知识库；``kb_ids`` 空 → 全部）。

        返回方案 5.4.3 形状::

            {segments: [{segment_id, content, score, source, asset_refs}],
             total, rerank_applied, mode, warn, reason, degraded, note}

        - 空查询 → 报错（400，方案 5.4.4）；空库 → 空列表不报错；
        - semantic 无向量索引 → ``missing_vector_index`` 错误；
          hybrid 仅全文可用 → 降级 + ``warn``；
        - 换模型未重建（need_reindex）仍可用旧索引（方案 5.3）。
        """
        query = (query or "").strip()
        if not query:
            raise KBServiceError("query 必填", "QUERY_REQUIRED")

        targets = self._resolve_kbs(kb_ids)
        if not targets:
            # 空库：HTTP 200 + 空列表（方案 5.4.4）
            return self._empty_result(mode or "keyword")

        mode = mode or targets[0].get("default_retrieval_mode") or "keyword"
        if mode not in ("keyword", "semantic", "hybrid"):
            raise KBServiceError("retrieval_mode 必须是 keyword/semantic/hybrid", "KB_INVALID_MODE")
        top_k = max(1, min(int(top_k or DEFAULT_TOP_K), 50))

        # 嵌入/重排按各 KB 自己的模型配置；多库混合时取第一个可用配置
        model_id = next((k.get("embedding_model_id") for k in targets if k.get("embedding_model_id")), None)
        emb_client = self._embedding_client(model_id) if model_id else None
        rk_enabled = any(k.get("rerank_enabled") for k in targets)
        if rerank is None:
            rerank = rk_enabled
        rk_client = self._rerank_client(model_id) if rerank else None

        # semantic 显式要求但无向量索引可用 → 报错引导（方案 5.4.4）
        if mode == "semantic" and emb_client is None:
            raise KBServiceError("语义检索需要向量索引：请先配置嵌入模型并重建索引", "missing_vector_index")

        result = await retrieve(
            self._db,
            [k["id"] for k in targets],
            query,
            mode=mode,
            top_k=top_k,
            embedding_client=emb_client,
            rerank_client=rk_client,
            score_threshold=float(score_threshold or 0.0),
        )
        return self._shape_result(result, mode)

    async def skill_search(
        self,
        query: str,
        knowledge_base_ids: list[str] | None = None,
        retrieval_mode: str | None = None,
        top_k: int = 5,
        rerank: bool | None = None,
        force_retrieve: bool = False,
        force_skip: bool = False,
    ) -> dict[str, Any]:
        """技能入口（方案 5.5）：提问分析 → 路由/多跳检索 → 来源标注。

        - ``force_skip=True`` → 直接返回空（供确定性流水线覆盖模型决策）；
        - ``force_retrieve=True`` → 跳过子查询拆分，强制单次全量检索；
        - 多跳子查询个数上限 :data:`MAX_SUBQUERIES`（成本控制，方案 5.5.5）；
        - 返回附 ``analysis``（语言/实体/子查询），供日志与前端展示。
        """
        if force_skip:
            return {**self._empty_result(retrieval_mode or "hybrid"), "analysis": None, "skipped": True}
        analysis = analyze_query(query)

        targets = self._resolve_kbs(knowledge_base_ids)
        if knowledge_base_ids and not targets:
            # 授权边界（方案 5.5.5）：显式指定的 KB 无一存在/可访问时返回空，
            # 不得回退全库检索——否则调用方限定的范围会被无声放大。
            return {
                **self._empty_result(retrieval_mode or "hybrid"),
                "analysis": analysis,
                "skipped": False,
            }
        if knowledge_base_ids:
            routed = [k["id"] for k in targets]
        else:
            routed = route_kbs(str(analysis["cleaned"]), targets)
            routed = [k for k in routed if k in {t["id"] for t in targets}]

        mode = retrieval_mode or (targets[0].get("default_retrieval_mode") if targets else None) or "hybrid"
        subqueries = [str(analysis["cleaned"])] if force_retrieve else [str(s) for s in analysis["subqueries"]]
        subqueries = subqueries[:MAX_SUBQUERIES]

        # 多跳：每个子查询独立检索（各自 top_k），按段 id 去重合并、取分数最高
        merged: dict[str, dict[str, Any]] = {}
        degraded = False
        warn = ""
        note = ""
        rerank_applied = False
        for sq in subqueries:
            try:
                part = await self.search(
                    sq,
                    kb_ids=routed or None,
                    mode=mode,
                    top_k=top_k,
                    rerank=rerank,
                )
            except KBServiceError as e:
                # semantic 缺索引在技能里降级为 hybrid（宁可降级不打断问答）
                if e.code == "missing_vector_index":
                    mode = "hybrid"
                    part = await self.search(sq, kb_ids=routed or None, mode="hybrid", top_k=top_k, rerank=rerank)
                else:
                    raise
            degraded = degraded or bool(part.get("degraded"))
            warn = warn or part.get("warn", "")
            note = note or part.get("note", "")
            rerank_applied = rerank_applied or bool(part.get("rerank_applied"))
            for seg in part["segments"]:
                sid = seg["segment_id"]
                prev = merged.get(sid)
                if prev is None or float(seg.get("score") or 0) > float(prev.get("score") or 0):
                    merged[sid] = seg

        segments = sorted(merged.values(), key=lambda s: float(s.get("score") or 0), reverse=True)[:top_k]
        logger.info(
            "kb skill_search query=%r subqueries=%d hits=%d mode=%s",
            query,
            len(subqueries),
            len(segments),
            mode,
        )
        return {
            "segments": segments,
            "total": len(segments),
            "rerank_applied": rerank_applied,
            "mode": mode,
            "warn": warn,
            "reason": "",
            "degraded": degraded,
            "note": note,
            "analysis": analysis,
            "skipped": False,
        }

    # ── 内部辅助 ──────────────────────────────────────────────────────

    def _resolve_kbs(self, kb_ids: list[str] | None) -> list[dict[str, Any]]:
        """解析目标知识库列表；空/None → 全部；过滤不存在的 id。"""
        all_kbs = self.kbs.list()
        if not kb_ids:
            return all_kbs
        wanted = set(kb_ids)
        return [k for k in all_kbs if k["id"] in wanted]

    @staticmethod
    def _empty_result(mode: str) -> dict[str, Any]:
        return {
            "segments": [],
            "total": 0,
            "rerank_applied": False,
            "mode": mode,
            "warn": "",
            "reason": "",
            "degraded": False,
            "note": "",
        }

    def _shape_result(self, result: dict[str, Any], mode: str) -> dict[str, Any]:
        """把 retrieve() 的内部结果转成方案 5.4.3 的对外形状（带来源标注）。"""
        hits = result.get("results", [])
        if hits:
            doc_ids = {h.get("doc_id") for h in hits}
            placeholders = ", ".join("?" for _ in doc_ids)
            doc_names: dict[str, str] = {}
            if doc_ids:
                for r in self._db.query(
                    f"SELECT id, name FROM kb_documents WHERE id IN ({placeholders})",  # noqa: S608 占位符由数量生成
                    tuple(doc_ids),
                ):
                    doc_names[r["id"]] = r["name"]
        else:
            doc_names = {}

        segments = []
        for h in hits:
            meta = h.get("metadata") or {}
            segments.append(
                {
                    "segment_id": h.get("id"),
                    "content": h.get("content", ""),
                    "score": float(h.get("score") or 0.0),
                    "position": h.get("position", meta.get("position", 0)),
                    "source": {
                        "doc_id": h.get("doc_id"),
                        "doc_name": doc_names.get(h.get("doc_id", ""), ""),
                        "kb_id": h.get("kb_id"),
                        "parent_id": h.get("parent_id"),
                    },
                    "asset_refs": meta.get("asset_ids", []),
                    "truncated": bool(meta.get("truncated")),
                }
            )
        return {
            "segments": segments,
            "total": len(segments),
            "rerank_applied": bool(result.get("rerank_applied")),
            "mode": result.get("mode", mode),
            "warn": result.get("warn", ""),
            "reason": result.get("reason", ""),
            "degraded": bool(result.get("degraded")),
            "note": result.get("note", ""),
        }
