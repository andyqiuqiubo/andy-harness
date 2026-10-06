"""pytest 全局配置 —— 数据库 / 工件 / MCP 配置的测试隔离。

集成测试（如 test_api_rest.py）直接 import harness.main 并启动完整应用，
默认会读写真实的 data/harness.db，把测试会话写入开发数据。
此配置在测试会话开始时把 HARNESS_DB_PATH 指向临时目录，
让所有测试使用独立的临时数据库，避免污染真实数据。
"""

import json

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_test_db(tmp_path_factory: pytest.TempPathFactory) -> None:
    """隔离测试期间使用的数据库 / 工件目录 / MCP 配置。"""
    import os

    os.environ["HARNESS_DB_PATH"] = str(tmp_path_factory.mktemp("test-db") / "test.db")
    # 权限状态文件同样隔离：否则用户运行时在 data/permission.json 里设置的
    # 覆盖（如 code_runner=auto）会压过默认策略，确认链路测试收不到
    # confirm_request 帧。
    os.environ["HARNESS_PERMISSION_STATE_FILE"] = str(tmp_path_factory.mktemp("test-permissions") / "permission.json")
    # 大工具输出落盘目录也隔离，避免测试把工件写进项目的 workspace/artifacts
    os.environ["HARNESS_ARTIFACTS_DIR"] = str(tmp_path_factory.mktemp("test-artifacts") / "artifacts")
    # 会话附件落盘目录也隔离（否则附件测试会把文件写进真实的 data/attachments）
    os.environ["HARNESS_ATTACHMENTS_DIR"] = str(tmp_path_factory.mktemp("test-attachments") / "attachments")
    # 附件存储目录同样隔离：否则附件测试会把上传文件写进真实的
    # data/attachments/，污染项目运行时数据（数据库已隔离但文件没有）。
    os.environ["HARNESS_ATTACHMENTS_DIR"] = str(tmp_path_factory.mktemp("test-attachments") / "attachments")
    # MCP 配置指向空的临时配置：否则测试会去读项目里的 mcp.json，
    # 每次启动应用都尝试连接真实远端 server（慢且依赖网络）。
    mcp_dir = tmp_path_factory.mktemp("test-mcp")
    mcp_cfg = mcp_dir / "mcp.json"
    mcp_cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    os.environ["MCP_CONFIG_PATH"] = str(mcp_cfg)
