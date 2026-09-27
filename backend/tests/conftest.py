"""pytest 全局配置 —— 数据库测试隔离。

集成测试（如 test_api_rest.py）直接 import harness.main 并启动完整应用，
默认会读写真实的 data/harness.db，把测试会话写入开发数据。
此配置在测试会话开始时把 HARNESS_DB_PATH 指向临时目录，
让所有测试使用独立的临时数据库，避免污染真实数据。
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_test_db(tmp_path_factory: pytest.TempPathFactory) -> None:
    """将测试期间使用的数据库路径指向临时目录。"""
    import os

    os.environ["HARNESS_DB_PATH"] = str(
        tmp_path_factory.mktemp("test-db") / "test.db"
    )
