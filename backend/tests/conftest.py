"""pytest 全局配置 —— 数据库 / 工件 / MCP 配置的测试隔离。

集成测试（如 test_api_rest.py）直接 import harness.main 并启动完整应用，
默认会读写真实的 data/harness.db，把测试会话写入开发数据。

**必须在模块顶层设置**（而不是放 fixture 里）：`infra/database.Database` 是
进程级单例，`__new__` 返回全局 `_DB_SINGLETON`，`__init__` 只在首次构造时读
`HARNESS_DB_PATH`。pytest 的收集阶段会 import 测试模块（`test_health.py`
顶层就 `import harness.main`），此时若 env 未设置，单例会锁定真实
`data/harness.db`——等 session fixture 再设 env 已经无效，测试会话
（「附件测试」/ m0-m4 之类）就会写进用户真实数据库。
conftest 模块顶层在收集之前执行，是唯一可靠的时机。
"""

import json
import os
import tempfile

# ── 顶层隔离（早于任何测试模块 import harness.main）────────────────
_TEST_ROOT = tempfile.mkdtemp(prefix="harness-test-")
os.environ["HARNESS_DB_PATH"] = os.path.join(_TEST_ROOT, "test.db")
# 权限状态文件同样隔离：否则用户运行时在 data/permission.json 里设置的
# 覆盖（如 code_runner=auto）会压过默认策略，确认链路测试收不到
# confirm_request 帧。
os.environ["HARNESS_PERMISSION_STATE_FILE"] = os.path.join(
    _TEST_ROOT, "permission.json"
)
# 大工具输出落盘目录、会话附件、知识库原文件都隔离，避免写进项目
# workspace/artifacts、data/attachments、data/knowledge。
os.environ["HARNESS_ARTIFACTS_DIR"] = os.path.join(_TEST_ROOT, "artifacts")
os.environ["HARNESS_ATTACHMENTS_DIR"] = os.path.join(_TEST_ROOT, "attachments")
os.environ["HARNESS_KNOWLEDGE_DIR"] = os.path.join(_TEST_ROOT, "knowledge")
# MCP 配置指向空的临时配置：否则测试会去读项目里的 mcp.json，
# 每次启动应用都尝试连接真实远端 server（慢且依赖网络）。
_mcp_cfg = os.path.join(_TEST_ROOT, "mcp.json")
with open(_mcp_cfg, "w", encoding="utf-8") as _f:
    json.dump({"mcpServers": {}}, _f)
os.environ["MCP_CONFIG_PATH"] = _mcp_cfg
