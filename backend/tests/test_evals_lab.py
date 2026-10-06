"""评测实验室 API 测试（脚本化 provider，全链路含后台运行）。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


class _ScriptedEvalProvider:
    """按最后一条 user 消息回放响应的脚本 provider。"""

    async def chat(self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any):
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if "你好" in last_user:
            yield {"delta": "你好！很高兴见到你。"}
        elif (
            "系统提示词" in str(messages[0].get("content"))
            if messages and messages[0].get("role") == "system"
            else False
        ):
            yield {"delta": "暗号已收到"}
        else:
            yield {"delta": "答案是 2"}


def _wait_done(client: TestClient, run_id: str, timeout_s: float = 20.0) -> dict[str, Any]:
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = client.get(f"/api/evals/runs/{run_id}").json()
        if run["status"] in ("done", "error"):
            return run
        time.sleep(0.2)
    raise AssertionError("评测运行超时未完成")


@pytest.fixture
def patched_provider(monkeypatch: pytest.MonkeyPatch):
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry,
        "get_provider",
        lambda self, provider_id, **kwargs: _ScriptedEvalProvider(),
    )


class TestDatasets:
    def test_create_list_delete(self, client: TestClient) -> None:
        cases = [
            {"id": "c1", "prompt": "你好", "expect_keywords": ["你好"]},
            {"id": "c2", "prompt": "1+1=?", "expect_keywords": ["2"], "forbid_tools": ["calculator"]},
        ]
        created = client.post(
            "/api/evals/datasets",
            json={"name": "冒烟数据集", "description": "测试", "cases": cases},
        )
        assert created.status_code == 200
        ds = created.json()
        assert ds["case_count"] == 2

        listed = client.get("/api/evals/datasets").json()
        assert any(d["id"] == ds["id"] for d in listed)

        assert client.delete(f"/api/evals/datasets/{ds['id']}").json()["status"] == "deleted"
        assert client.get(f"/api/evals/datasets/{ds['id']}").status_code == 404

    def test_invalid_cases_rejected(self, client: TestClient) -> None:
        # 缺 prompt 字段 → 评测框架校验失败 → 400
        bad = client.post(
            "/api/evals/datasets",
            json={"name": "bad", "cases": [{"id": "x", "expect_keywords": ["y"]}]},
        )
        assert bad.status_code == 400

    def test_empty_cases_rejected(self, client: TestClient) -> None:
        assert client.post("/api/evals/datasets", json={"name": "empty", "cases": []}).status_code == 400


class TestRuns:
    def test_full_run_with_scripted_provider(self, client: TestClient, patched_provider) -> None:
        cases = [
            {"id": "greet", "prompt": "你好", "expect_keywords": ["你好"]},
            {
                "id": "sum",
                "prompt": "1+1 等于几？不要用工具。",
                "expect_keywords": ["2"],
                "forbid_tools": ["calculator"],
            },
        ]
        ds = client.post("/api/evals/datasets", json={"name": "运行数据集", "cases": cases}).json()

        run = client.post(
            "/api/evals/runs",
            json={
                "dataset_id": ds["id"],
                "combos": [{"provider_id": "deepseek", "model": "test-model", "system_prompt": ""}],
            },
        ).json()
        assert run["status"] == "running"

        done = _wait_done(client, run["id"])
        assert done["status"] == "done", done.get("error")
        combo = done["results"][0]
        assert combo["status"] == "done"
        assert combo["pass_rate"] == 1.0
        assert combo["total"] == 2 and combo["passed"] == 2
        case_ids = {c["id"] for c in combo["cases"]}
        assert case_ids == {"greet", "sum"}

    def test_multi_combo_matrix(self, client: TestClient, patched_provider) -> None:
        cases = [{"id": "greet", "prompt": "你好", "expect_keywords": ["你好"]}]
        ds = client.post("/api/evals/datasets", json={"name": "矩阵", "cases": cases}).json()
        run = client.post(
            "/api/evals/runs",
            json={
                "dataset_id": ds["id"],
                "combos": [
                    {"provider_id": "deepseek", "model": "m-flash", "system_prompt": ""},
                    {"provider_id": "deepseek", "model": "m-pro", "system_prompt": "你是严谨的助手"},
                ],
            },
        ).json()
        done = _wait_done(client, run["id"])
        assert done["status"] == "done"
        assert len(done["results"]) == 2
        assert all(r["status"] == "done" and r["pass_rate"] == 1.0 for r in done["results"])
        # 两个组合的模型名按矩阵区分
        assert {r["model"] for r in done["results"]} == {"m-flash", "m-pro"}

    def test_system_prompt_reaches_model(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """系统提示词维度：注入的 system 消息必须出现在模型请求里。"""
        captured: list[list[dict[str, Any]]] = []

        class _Capture(_ScriptedEvalProvider):
            async def chat(self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any):
                captured.append([dict(m) for m in messages])
                async for c in super().chat(messages, model, stream, **kwargs):
                    yield c

        from harness.modules.model_manager.provider_registry import ProviderRegistry

        monkeypatch.setattr(ProviderRegistry, "get_provider", lambda self, pid, **kw: _Capture())

        cases = [{"id": "sp", "prompt": "说暗号", "expect_keywords": ["暗号"]}]
        ds = client.post("/api/evals/datasets", json={"name": "sp数据集", "cases": cases}).json()
        run = client.post(
            "/api/evals/runs",
            json={
                "dataset_id": ds["id"],
                "combos": [{"provider_id": "x", "model": "m", "system_prompt": "暗号是芝麻开门，请记住系统提示词"}],
            },
        ).json()
        done = _wait_done(client, run["id"])
        assert done["status"] == "done", done.get("error")
        assert done["results"][0]["pass_rate"] == 1.0
        assert captured, "provider 未被调用"
        assert any("系统提示词" in str(m.get("content", "")) for m in captured[0] if m.get("role") == "system")

    def test_run_unknown_dataset_404(self, client: TestClient) -> None:
        res = client.post(
            "/api/evals/runs",
            json={"dataset_id": "nope", "combos": [{"provider_id": "deepseek", "model": "m"}]},
        )
        assert res.status_code == 404

    def test_run_empty_combos_rejected(self, client: TestClient) -> None:
        cases = [{"id": "greet", "prompt": "你好", "expect_keywords": ["你好"]}]
        ds = client.post("/api/evals/datasets", json={"name": "空组合", "cases": cases}).json()
        res = client.post("/api/evals/runs", json={"dataset_id": ds["id"], "combos": []})
        # Pydantic min_length 校验 → 422
        assert res.status_code == 422
