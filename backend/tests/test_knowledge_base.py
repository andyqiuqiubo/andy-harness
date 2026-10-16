"""知识库系统测试 —— 清洗 / 分段 S1-S4 / 自动匹配 / 父子关联 / 摄取检索 / 技能 / API。

对应《知识库系统-完整解决方案.md》第 10 节验收标准：
- 分段与清洗：S1–S4 均能产出正确分段；自动匹配生效；清洗不丢数值；
- 分阶段存储：P1 文本/图文可上传并建索引；空文档 status=empty；不支持格式拒绝；
- 检索模式：keyword 返回预期结果；空库不报错；缺索引优雅降级；
- 技能化：技能检索可用；强制开关有效；授权边界（显式 KB 不可访问时不得回退全库）。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from harness.modules.knowledge_base import chunking
from harness.modules.knowledge_base.analysis import analyze_query, route_kbs
from harness.modules.knowledge_base.chunking import Asset
from harness.modules.knowledge_base.limits import MAX_SEGMENT_CHARS
from harness.modules.knowledge_base.service import (
    KBServiceError,
    KnowledgeBaseService,
)

# ── 清洗规则（方案 5.2.3）─────────────────────────────────────────────


class TestCleaning:
    def test_strip_zero_width_and_control(self) -> None:
        raw = "管理员\u200b密码​\x01重置"
        assert chunking.clean_text(raw) == "管理员密码重置"

    def test_normalize_newlines_and_blank_lines(self) -> None:
        raw = "第一段\r\n第二段\n\n\n\n第三段"
        assert chunking.clean_text(raw) == "第一段\n第二段\n\n第三段"

    def test_unescape_html_entities(self) -> None:
        assert "a & b < c" in chunking.clean_text("a &amp; b &lt; c")

    def test_collapse_repeated_punctuation(self) -> None:
        assert chunking.clean_text("真的吗。。。？") == "真的吗。？"

    def test_cjk_inner_spaces_removed(self) -> None:
        assert chunking.clean_text("管理员 密码 重置") == "管理员密码重置"

    def test_numbers_preserved(self) -> None:
        """清洗不得删除表格数值（方案 5.2.3 清洗边界）。"""
        raw = "订单号\tA2026\t金额 1,234.50 元"
        cleaned = chunking.clean_text(raw)
        assert "A2026" in cleaned and "1,234.50" in cleaned


# ── 分段策略 S1–S4（方案 5.2.2）────────────────────────────────────────


class TestSegmentation:
    def test_s1_merges_paragraphs(self) -> None:
        text = "\n\n".join(f"第{i}段内容。" for i in range(1, 6))
        segs, used = chunking.segment_document(text, [], "S1")
        assert used == "S1"
        assert len(segs) == 1  # 短段落合并为一段
        assert "第1段内容。" in segs[0].content

    def test_s1_hard_split_marks_truncated(self) -> None:
        text = "长" * (MAX_SEGMENT_CHARS * 2 + 100)
        segs, _ = chunking.segment_document(text, [], "S1")
        assert len(segs) >= 2
        assert any(s.metadata.get("truncated") for s in segs)

    def test_s2_heading_split_with_metadata(self) -> None:
        text = (
            "# 总则\n\n第一条：本手册适用于全体管理员与运维人员，涵盖密码管理与账号锁定规则。\n\n"
            "# 附则\n\n第二条：本手册自发布之日起生效，解释权归运维部所有，原规定同时废止。"
        )
        segs, used = chunking.segment_document(text, [], "S2")
        assert used == "S2"
        assert len(segs) == 2
        assert segs[0].metadata.get("headings") == "总则"
        assert segs[1].metadata.get("headings") == "附则"
        assert "密码管理" in segs[0].content

    def test_s3_parent_child_linkage(self) -> None:
        """父子分段：父段在前，子段 parent_id 必须等于父段行 id（修复验证）。"""
        text = "\n\n".join(f"第{i}章：" + "内容" * 300 for i in range(1, 4))
        segs, used = chunking.segment_document(text, [], "S3")
        assert used == "S3"
        parents = [s for s in segs if s.metadata.get("role") == "parent"]
        children = [s for s in segs if s.metadata.get("role") == "child"]
        assert parents and children
        parent_ids = {p.id for p in parents}
        for c in children:
            assert c.parent_id in parent_ids  # 修复前此处为 sha1 哈希，库内无法 JOIN

    def test_s4_qa_pairs(self) -> None:
        text = "问：如何重置密码？\n答：在设置页点击重置。\n问：如何改绑定手机？\n答：联系管理员。"
        segs, used = chunking.segment_document(text, [], "S4")
        assert used == "S4"
        assert len(segs) == 2
        assert segs[0].metadata.get("role") == "qa"
        assert "重置" in segs[0].content and "设置页" in segs[0].content

    def test_s4_falls_back_to_s2_without_qa(self) -> None:
        text = "# 标题\n\n这是一段足够长的正文内容，用于验证无问答结构时退化为结构感知切分。"
        segs, used = chunking.segment_document(text, [], "S4")
        assert used == "S4"  # 手动覆盖仍记 S4
        assert len(segs) == 1  # 内部退化为 S2 结构切分
        assert segs[0].metadata.get("headings") == "标题"

    def test_auto_strategy_matching(self) -> None:
        assert chunking.choose_strategy("问：a？\n答：b。") == "S4"
        assert chunking.choose_strategy("# 标题\n\n正文") == "S2"
        assert chunking.choose_strategy("纯文本" * 5000) == "S3"
        assert chunking.choose_strategy("短文本") == "S1"

    def test_asset_segment_always_indexed(self) -> None:
        """图片无描述时也成分段（方案 5.1.2：不因缺描述而丢分段）。"""
        asset = Asset(id="d_img1", filename="diagram.png", storage_path="/tmp/x.png", description="")
        seg = chunking.build_asset_segment(asset)
        assert seg.metadata.get("role") == "asset"
        assert "diagram.png" in seg.content
        assert seg.metadata.get("asset_ids") == ["d_img1"]

    def test_fulltext_includes_asset_description(self) -> None:
        asset = Asset(id="d_img1", filename="a.png", storage_path="/x", description="组织架构图")
        ft = chunking.build_fulltext("正文", [asset])
        assert "正文" in ft and "组织架构图" in ft


# ── 提问分析（方案 5.5.3）─────────────────────────────────────────────


class TestAnalysis:
    def test_language_and_filler_removal(self) -> None:
        a = analyze_query("请问如何重置管理员密码？")
        assert a["language"] == "zh"
        assert "请问" not in str(a["cleaned"])

    def test_entities_extracted(self) -> None:
        a = analyze_query('查一下订单 "A2026001" 的状态')
        assert "A2026001" in a["entities"]

    def test_subqueries_capped(self) -> None:
        a = analyze_query("问题一？问题二！问题三；问题四。问题五？")
        assert len(a["subqueries"]) <= 4

    def test_route_kbs_by_overlap(self) -> None:
        kbs = [
            {"id": "kb1", "name": "运维手册", "description": "密码与账号"},
            {"id": "kb2", "name": "菜谱大全", "description": "烹饪技巧"},
        ]
        assert route_kbs("密码重置", kbs) == ["kb1"]

    def test_route_kbs_fallback_all(self) -> None:
        kbs = [{"id": "kb1", "name": "甲", "description": ""}, {"id": "kb2", "name": "乙", "description": ""}]
        assert set(route_kbs("完全无关的词", kbs)) == {"kb1", "kb2"}


# ── 服务集成（摄取 / 检索 / 技能 / 重建）──────────────────────────────


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """隔离的知识库服务环境：内存库 + 临时落盘目录。"""
    from harness.infra.database import Database

    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    db = Database(":memory:")
    svc = KnowledgeBaseService(db)
    return svc


_MD_DOC = """# 运维手册

## 密码管理

管理员密码可通过设置页重置，重置后需重新登录。

## 账号锁定

连续 5 次输错密码，账号将锁定 30 分钟。
"""


class TestServiceIngestion:
    async def test_upload_md_and_keyword_search(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("运维知识库")
        doc = await kb_env.add_document(kb["id"], "运维手册.md", _MD_DOC.encode("utf-8"))
        assert doc["status"] == "ready"
        assert doc["segment_count"] >= 2
        assert doc["segment_strategy"] == "S2"

        result = await kb_env.search("密码重置", kb_ids=[kb["id"]], mode="keyword")
        assert result["total"] >= 1
        hit = result["segments"][0]
        assert "重置" in hit["content"]
        assert hit["source"]["doc_name"] == "运维手册.md"

    async def test_empty_document_status(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("空库测试")
        doc = await kb_env.add_document(kb["id"], "empty.txt", b"   \n\n  \n")
        assert doc["status"] == "empty"
        result = await kb_env.search("任意", kb_ids=[kb["id"]])
        assert result["segments"] == [] and result["total"] == 0

    async def test_unsupported_format_rejected(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("格式测试")
        # 用永远没有内置适配器的后缀（.mp3 在应用启动后已注册 AudioAdapter，
        # 不能再用它验证「不支持格式」——跨文件同进程运行时会受全局注册表影响）
        with pytest.raises(KBServiceError) as ei:
            await kb_env.add_document(kb["id"], "archive.xyz", b"\x00\x01\x02")
        assert ei.value.code == "KB_UNSUPPORTED_FORMAT"

    async def test_image_document_asset_segment(self, kb_env: KnowledgeBaseService) -> None:
        """单图文档：正文为空，但图片资产分段参与检索（方案 5.1.2）。"""
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        kb = kb_env.create_kb("图库")
        doc = await kb_env.add_document(kb["id"], "logo.png", png_1x1)
        assert doc["status"] == "ready"
        assert doc["asset_count"] == 1
        segs = kb_env.list_segments(doc["id"])
        assert any(s["metadata"].get("role") == "asset" for s in segs)

    async def test_reindex_document(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("重建测试")
        doc = await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        # S1 会把整篇合并为一段（总长 < MAX_SEGMENT_CHARS），与 S2 的段数不同属预期
        doc2 = await kb_env.reindex_document(doc["id"], strategy="S1")
        assert doc2["status"] == "ready"
        assert doc2["segment_strategy"] == "S1"
        n_after = len(kb_env.list_segments(doc["id"]))
        assert n_after >= 1
        # 关键：先清后写——同策略再重建一次，段数不得翻倍
        await kb_env.reindex_document(doc["id"], strategy="S1")
        assert len(kb_env.list_segments(doc["id"])) == n_after

    async def test_semantic_without_vector_index_errors(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("无向量", default_retrieval_mode="keyword")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        with pytest.raises(KBServiceError) as ei:
            await kb_env.search("密码", kb_ids=[kb["id"]], mode="semantic")
        assert ei.value.code == "missing_vector_index"

    async def test_hybrid_degrades_without_vector(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("降级测试")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.search("密码", kb_ids=[kb["id"]], mode="hybrid")
        assert result["degraded"] is True
        assert result["warn"] == "missing_vector_index"
        assert result["total"] >= 1  # 仍有关键词结果

    async def test_delete_kb_cascades(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("待删除")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        doc_id = kb_env.list_documents(kb["id"])[0]["id"]
        assert kb_env.delete_kb(kb["id"]) is True
        assert kb_env.get_kb(kb["id"]) is None
        assert kb_env.get_document(doc_id) is None  # FK 级联
        assert kb_env.list_segments(doc_id) == []


class TestSkillSearch:
    async def test_skill_search_basic(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("技能库", default_retrieval_mode="hybrid")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.skill_search("如何重置管理员密码", top_k=3)
        assert result["skipped"] is False
        assert result["total"] >= 1
        assert result["analysis"]["language"] == "zh"
        assert result["segments"][0]["source"]["doc_name"] == "a.md"

    async def test_force_skip(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("跳过库")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.skill_search("密码", force_skip=True)
        assert result["skipped"] is True
        assert result["segments"] == []

    async def test_force_retrieve_single_query(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("强制库")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.skill_search("密码重置？账号锁定？", force_retrieve=True)
        # 强制单次检索：子查询不拆分，仅一次检索
        assert result["total"] >= 1

    async def test_explicit_kb_ids_not_falling_back_to_all(self, kb_env: KnowledgeBaseService) -> None:
        """授权边界（方案 5.5.5）：显式 KB 不可访问 → 返回空，不得回退全库。"""
        kb_a = kb_env.create_kb("库A")
        await kb_env.add_document(kb_a["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.skill_search("密码", knowledge_base_ids=["kb_not_exist"])
        assert result["segments"] == []
        assert result["total"] == 0

    async def test_no_hit_returns_empty(self, kb_env: KnowledgeBaseService) -> None:
        kb = kb_env.create_kb("空结果库")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        result = await kb_env.skill_search("量子纠缠的十种烹饪方法")
        assert result["segments"] == []


class TestEmbeddingModelConfig:
    async def test_probe_failure_rejects_save(self, kb_env: KnowledgeBaseService) -> None:
        """连通性探测失败 → 拒绝保存（方案 5.3.3）。"""
        with pytest.raises(KBServiceError) as ei:
            await kb_env.create_embedding_model(
                name="坏模型",
                model_name="x",
                endpoint="http://127.0.0.1:9/v1/embeddings",
                api_key="sk-test",
                dimensions=8,
            )
        assert ei.value.code == "KB_PROBE_FAILED"
        assert kb_env.list_embedding_models() == []

    def test_mask_model_hides_keys(self, kb_env: KnowledgeBaseService) -> None:
        row = {"id": "m1", "api_key_encrypted": "abc", "rerank_api_key_encrypted": "def"}
        masked = KnowledgeBaseService._mask_model(row)
        assert "api_key_encrypted" not in masked
        assert masked["has_api_key"] is True and masked["has_rerank_key"] is True


class TestEmbeddingHygiene:
    async def test_segments_expose_has_embedding_not_blob(self, kb_env: KnowledgeBaseService) -> None:
        """分段结果不得携带 embedding 原始字节（FastAPI 无法序列化 bytes → 500）。"""
        import json

        kb = kb_env.create_kb("向量隐藏")
        doc = await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        segs = kb_env.list_segments(doc["id"])
        kb_env.segments.update_embedding(segs[0]["id"], b"\x00\x01\x02\x03")
        segs2 = kb_env.list_segments(doc["id"])
        s0 = next(s for s in segs2 if s["id"] == segs[0]["id"])
        assert "embedding" not in s0
        assert s0["has_embedding"] is True
        json.dumps(segs2)  # 可 JSON 序列化（修复前 bytes 会在此失败）

    async def test_build_vectors_detects_missing_by_flag(self, kb_env: KnowledgeBaseService) -> None:
        """only_missing 判定基于 has_embedding 布尔（repository 已剥离字节）。"""
        kb = kb_env.create_kb("补向量")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        # 未配置模型 → 跳过；断言 list_by_kb 不抛且带 has_embedding 字段
        segs = kb_env.segments.list_by_kb(kb["id"])
        assert segs and all("has_embedding" in s for s in segs)


# ── P2 FormatAdapter（方案 5.1.3 / M4）────────────────────────────────


class TestP2Adapters:
    def test_audio_adapter_requires_endpoint(self, monkeypatch) -> None:
        """未配置 ASR 端点 → ParseError（明确指引），不影响其它格式。"""
        from harness.modules.knowledge_base.adapters import AudioAdapter
        from harness.modules.knowledge_base.parsers import ParseError

        monkeypatch.delenv("HARNESS_KB_ASR_ENDPOINT", raising=False)
        with pytest.raises(ParseError) as ei:
            AudioAdapter().parse(b"\xff\xfb", "a.mp3", "/tmp", "d1")
        assert "HARNESS_KB_ASR_ENDPOINT" in str(ei.value)

    def test_audio_adapter_with_injected_transcriber(self, tmp_path) -> None:
        from harness.modules.knowledge_base.adapters import AudioAdapter

        adapter = AudioAdapter(transcribe=lambda data, name: f"{name} 的转写文本")
        text, assets = adapter.parse(b"\x00", "m.mp3", str(tmp_path), "d1")
        assert "m.mp3" in text and assets == []

    def test_register_builtin_adapters_enables_mp3(self, monkeypatch) -> None:
        from harness.modules.knowledge_base import parsers
        from harness.modules.knowledge_base.adapters import register_builtin_adapters

        # 用全新空注册表隔离：完整应用（TestClient）启动时会全局注册内置适配器，
        # 直接断言原始注册表状态会受同进程其它测试影响。
        monkeypatch.setattr(parsers, "_ADAPTERS", {})
        assert not parsers.has_adapter("x.mp3")
        register_builtin_adapters()
        assert parsers.has_adapter("x.mp3")
        assert parsers.detect_stage("x.mp3") == "P2"

    async def test_audio_upload_full_pipeline_with_fake_asr(self, kb_env: KnowledgeBaseService, monkeypatch) -> None:
        """注册适配器后音频可完成「上传→转写→分段→检索」全链路。"""
        from harness.modules.knowledge_base import parsers
        from harness.modules.knowledge_base.adapters import AudioAdapter

        monkeypatch.setattr(parsers, "_ADAPTERS", dict(parsers._ADAPTERS))
        parsers.register_adapter(
            AudioAdapter(transcribe=lambda data, name: "会议决定：下季度上线知识库检索功能，负责人张三。")
        )
        kb = kb_env.create_kb("音频库")
        doc = await kb_env.add_document(kb["id"], "meeting.mp3", b"fake-audio")
        assert doc["status"] == "ready"
        result = await kb_env.search("知识库检索", kb_ids=[kb["id"]])
        assert result["total"] >= 1

    async def test_audio_upload_without_asr_config_marks_failed(
        self, kb_env: KnowledgeBaseService, monkeypatch
    ) -> None:
        """已注册适配器但未配置端点：该文档 failed，错误信息可读。"""
        from harness.modules.knowledge_base import parsers
        from harness.modules.knowledge_base.adapters import register_builtin_adapters

        monkeypatch.setattr(parsers, "_ADAPTERS", dict(parsers._ADAPTERS))
        monkeypatch.delenv("HARNESS_KB_ASR_ENDPOINT", raising=False)
        register_builtin_adapters()
        kb = kb_env.create_kb("音频库2")
        with pytest.raises(KBServiceError) as ei:
            await kb_env.add_document(kb["id"], "m.mp3", b"\xff\xfb")
        assert ei.value.code == "KB_PARSE_FAILED"
        assert "ASR" in str(ei.value)
        assert kb_env.list_documents(kb["id"])[0]["status"] == "failed"


# ── 系统提示引导（方案 5.5.4 模型自主调用决策示例）──────────────────────


class TestKnowledgeBaseHint:
    def test_hint_empty_without_service(self) -> None:
        from harness.engine.agent_loop import AgentLoop
        from harness.kernel.hooks import HookManager
        from harness.kernel.services import ServiceRegistry

        loop = AgentLoop(services=ServiceRegistry(), hooks=HookManager())
        assert loop._render_kb_hint() == ""

    def test_hint_renders_when_kbs_exist(self, kb_env: KnowledgeBaseService) -> None:
        from harness.engine.agent_loop import AgentLoop
        from harness.kernel.hooks import HookManager
        from harness.kernel.services import ServiceRegistry

        reg = ServiceRegistry()
        reg.register(KnowledgeBaseService, kb_env, owner="test")
        loop = AgentLoop(services=reg, hooks=HookManager())
        assert loop._render_kb_hint() == ""  # 尚无知识库 → 不注入
        kb_env.create_kb("运维手册")
        hint = loop._render_kb_hint()
        assert "knowledge_base_search" in hint
        assert "运维手册" in hint
        assert "禁止编造" in hint


# ── 工具层（方案 5.5 function calling 暴露）────────────────────────────


class TestKnowledgeBaseSearchTool:
    async def test_tool_schema_and_execute(self, kb_env: KnowledgeBaseService) -> None:
        from harness.modules.knowledge_base.tool import KnowledgeBaseSearchTool

        kb = kb_env.create_kb("工具库")
        await kb_env.add_document(kb["id"], "a.md", _MD_DOC.encode("utf-8"))
        tool = KnowledgeBaseSearchTool(kb_env)
        assert tool.tool_name == "knowledge_base_search"
        assert tool.risk_level == "read"
        schema = tool.parameters_schema
        assert "query" in schema["required"]

        out = await tool.execute({"query": "密码重置", "top_k": 3})
        assert "来源" in out and "重置" in out

    async def test_tool_no_hit_message(self, kb_env: KnowledgeBaseService) -> None:
        from harness.modules.knowledge_base.tool import KnowledgeBaseSearchTool

        tool = KnowledgeBaseSearchTool(kb_env)
        out = await tool.execute({"query": "量子纠缠的十种烹饪方法"})
        assert out == "知识库中未找到相关信息。"

    async def test_tool_schema_hides_force_flags(self, kb_env: KnowledgeBaseService) -> None:
        """强制开关是流水线级能力，不通过 function calling 暴露给模型（方案 5.5.4）。"""
        from harness.modules.knowledge_base.tool import KnowledgeBaseSearchTool

        tool = KnowledgeBaseSearchTool(kb_env)
        props = tool.parameters_schema["properties"]
        assert "force_retrieve" not in props
        assert "force_skip" not in props


# ── REST API 冒烟（方案第 8 节契约）───────────────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestKnowledgeBaseAPI:
    def test_full_flow(self, client: TestClient) -> None:
        # 建库
        r = client.post("/api/kb", json={"name": "API测试库", "default_retrieval_mode": "hybrid"})
        assert r.status_code == 200, r.text
        kb_id = r.json()["id"]

        # 列表
        r = client.get("/api/kb")
        assert r.status_code == 200
        assert any(k["id"] == kb_id for k in r.json()["knowledge_bases"])

        # 上传文档（multipart）
        r = client.post(
            f"/api/kb/{kb_id}/documents",
            files={"files": ("api.md", _MD_DOC.encode("utf-8"), "text/markdown")},
            data={"strategy": "auto"},
        )
        assert r.status_code == 200, r.text
        doc = r.json()["documents"][0]
        assert doc["status"] == "ready"
        doc_id = doc["id"]

        # 文档状态
        r = client.get(f"/api/kb/{kb_id}/documents/{doc_id}")
        assert r.status_code == 200 and r.json()["segment_count"] >= 2

        # 分段列表
        r = client.get(f"/api/kb/{kb_id}/segments")
        assert r.status_code == 200 and r.json()["total"] >= 2

        # 检索
        r = client.post(
            "/api/retrieval",
            json={"query": "密码重置", "knowledge_base_ids": [kb_id], "retrieval_mode": "hybrid"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 1
        assert body["segments"][0]["source"]["doc_name"] == "api.md"
        assert body["degraded"] is True  # 未配嵌入模型，hybrid 降级并标记

        # 技能入口
        r = client.post(
            "/api/skills/knowledge_base_search",
            json={"query": "如何重置管理员密码", "knowledge_base_ids": [kb_id], "top_k": 3},
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] >= 1
        assert r.json()["analysis"]["language"] == "zh"

        # 强制跳过
        r = client.post(
            "/api/skills/knowledge_base_search",
            json={"query": "x", "knowledge_base_ids": [kb_id], "force_skip": True},
        )
        assert r.json()["skipped"] is True

        # 重建索引
        r = client.post(f"/api/kb/{kb_id}/documents/{doc_id}/reindex")
        assert r.status_code == 200 and r.json()["status"] == "ready"

        # 换模型置 need_reindex
        r = client.put(f"/api/kb/{kb_id}/embedding-model", json={"embedding_model_id": None})
        assert r.status_code == 200

        # 删除文档与库
        r = client.delete(f"/api/kb/{kb_id}/documents/{doc_id}")
        assert r.status_code == 200
        r = client.delete(f"/api/kb/{kb_id}")
        assert r.status_code == 200

    def test_empty_query_rejected(self, client: TestClient) -> None:
        r = client.post("/api/retrieval", json={"query": ""})
        assert r.status_code == 400
        assert r.json()["code"] == "QUERY_REQUIRED"

    def test_unknown_kb_404(self, client: TestClient) -> None:
        assert client.get("/api/kb/nope").status_code == 404

    def test_segments_endpoint_serializes_with_embeddings(self, client: TestClient) -> None:
        """分段接口在存在向量时也必须 200。

        回归测试：修复前 repository 直接把 embedding（float32 bytes）塞进响应，
        FastAPI 无法序列化 bytes → 500。现在应以 has_embedding 布尔暴露。
        """
        from harness.infra.database import Database

        r = client.post("/api/kb", json={"name": "序列化测试"})
        kb_id = r.json()["id"]
        r = client.post(
            f"/api/kb/{kb_id}/documents",
            files={"files": ("s.md", _MD_DOC.encode("utf-8"), "text/markdown")},
        )
        doc_id = r.json()["documents"][0]["id"]
        # 直接写入一个向量，模拟已建向量索引的分段
        db = Database()
        seg_id = db.query_one("SELECT id FROM kb_segments WHERE doc_id = ?", (doc_id,))["id"]
        db.execute("UPDATE kb_segments SET embedding = ? WHERE id = ?", (b"\x00\x01\x02\x03", seg_id))

        r = client.get(f"/api/kb/{kb_id}/segments")
        assert r.status_code == 200, r.text
        seg = next(s for s in r.json()["segments"] if s["id"] == seg_id)
        assert seg["has_embedding"] is True
        assert "embedding" not in seg

        # 清理
        client.delete(f"/api/kb/{kb_id}")

    def test_update_kb_mode(self, client: TestClient) -> None:
        r = client.post("/api/kb", json={"name": "改模式"})
        kb_id = r.json()["id"]
        r = client.put(f"/api/kb/{kb_id}", json={"default_retrieval_mode": "hybrid"})
        assert r.status_code == 200 and r.json()["default_retrieval_mode"] == "hybrid"
        r = client.put(f"/api/kb/{kb_id}", json={"default_retrieval_mode": "bad"})
        assert r.status_code == 400 and r.json()["code"] == "KB_INVALID_MODE"
        client.delete(f"/api/kb/{kb_id}")

    def test_create_kb_requires_name(self, client: TestClient) -> None:
        r = client.post("/api/kb", json={"name": ""})
        assert r.status_code in (400, 422)

    def test_embedding_models_list(self, client: TestClient) -> None:
        r = client.get("/api/kb/embedding-models")
        assert r.status_code == 200
        assert r.json()["models"] == []

    def test_embedding_probe_failure(self, client: TestClient) -> None:
        r = client.post(
            "/api/kb/embedding-models",
            json={
                "name": "坏模型",
                "model_name": "x",
                "endpoint": "http://127.0.0.1:9/v1/embeddings",
                "api_key": "sk-test",
            },
        )
        assert r.status_code == 400
        assert r.json()["code"] == "KB_PROBE_FAILED"
