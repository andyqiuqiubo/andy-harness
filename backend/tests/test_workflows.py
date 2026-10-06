"""工作流编排器（图编排）API 测试（脚本化 provider 全链路）。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


def _patch_scripted_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    class _Scripted:
        async def chat(self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any):
            last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            if "诗" in last_user:
                yield {"delta": f"《{last_user[:20]}》成稿"}
            else:
                yield {"delta": f"基于「{last_user[:40]}」的终稿"}

    monkeypatch.setattr(ProviderRegistry, "get_provider", lambda self, pid, **kw: _Scripted())


def _sample_graph() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "position": {"x": 0, "y": 0},
                "data": {"variables": [{"name": "topic", "type": "string", "default": ""}]},
            },
            {
                "id": "llm1",
                "type": "llm",
                "position": {"x": 300, "y": 0},
                "data": {
                    "provider_id": "deepseek",
                    "model": "deepseek-flash",
                    "system_prompt": "",
                    "user_prompt": "写一首关于{{start.topic}}的诗",
                },
            },
            {
                "id": "end1",
                "type": "end",
                "position": {"x": 600, "y": 0},
                "data": {"outputs": [{"name": "poem", "value": "{{llm1.text}}"}]},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm1", "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e2", "source": "llm1", "target": "end1", "sourceHandle": "out", "targetHandle": "in"},
        ],
    }


class TestWorkflows:
    def test_crud_and_chain_run(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scripted_provider(monkeypatch)

        wf = client.post(
            "/api/workflows",
            json={"name": "诗生成流程", "description": "主题→大模型→结束", "graph": _sample_graph()},
        ).json()
        assert wf["id"]
        assert len(wf["graph"]["nodes"]) == 3

        run = client.post(
            f"/api/workflows/{wf['id']}/run",
            json={"inputs": {"topic": "秋游"}},
        ).json()
        assert run["status"] == "success", run.get("error")
        results = run["engine_result"]["results"]
        assert results["start"]["status"] == "success"
        assert results["llm1"]["status"] == "success"
        assert "秋游" in results["llm1"]["outputs"]["text"]
        assert run["engine_result"]["end_outputs"]["end1"]["poem"] == results["llm1"]["outputs"]["text"]

        # 运行历史按工作流过滤
        runs = client.get(f"/api/workflows/runs?workflow_id={wf['id']}").json()
        assert any(r["id"] == run["id"] for r in runs)

        # 更新（保存）
        upd = client.put(
            f"/api/workflows/{wf['id']}",
            json={"name": "诗生成流程V2", "description": "", "graph": _sample_graph()},
        ).json()
        assert upd["name"] == "诗生成流程V2"

        # 删除工作流连带历史
        assert client.delete(f"/api/workflows/{wf['id']}").json()["status"] == "deleted"
        assert client.get(f"/api/workflows/runs/{run['id']}").status_code == 404

    def test_list_runs_before_detail_routes(self, client: TestClient) -> None:
        """GET /workflows/runs 不能被 GET /workflows/{workflow_id} 吞掉。"""
        res = client.get("/api/workflows/runs")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_step_failure_marks_run_error(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        class _FailProvider:
            async def chat(self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any):
                raise RuntimeError("模型不可用")

        monkeypatch.setattr(ProviderRegistry, "get_provider", lambda self, pid, **kw: _FailProvider())

        wf = client.post(
            "/api/workflows",
            json={"name": "失败流程", "graph": _sample_graph()},
        ).json()
        run = client.post(f"/api/workflows/{wf['id']}/run", json={"inputs": {"topic": "x"}}).json()
        assert run["status"] == "error"
        assert run["engine_result"]["results"]["llm1"]["status"] == "error"

    def test_create_validation(self, client: TestClient) -> None:
        # 空名称 → 400
        res = client.post("/api/workflows", json={"name": "", "graph": {"nodes": [], "edges": []}})
        assert res.status_code == 400
        # 缺少开始/结束节点：运行期校验 → error
        wf = client.post(
            "/api/workflows",
            json={
                "name": "非法图",
                "graph": {
                    "nodes": [{"id": "a", "type": "llm", "position": {"x": 0, "y": 0}, "data": {}}],
                    "edges": [],
                },
            },
        ).json()
        run = client.post(f"/api/workflows/{wf['id']}/run", json={"inputs": {}}).json()
        assert run["status"] == "error"

    def test_run_unknown_workflow_404(self, client: TestClient) -> None:
        res = client.post("/api/workflows/nope/runs", json={"inputs": {}})
        assert res.status_code == 404
