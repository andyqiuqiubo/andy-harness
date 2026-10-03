# Eval 评测任务集（E5）

本目录存放**任务级回归评测**的用例文件。每个用例描述一个任务及其客观期望，
评测框架（`harness.eval`）把任务跑过 AgentLoop 后按期望打分。

## 用例格式

顶层为对象（含 `cases` 列表）或直接是用例列表。每个用例：

| 字段 | 说明 |
|---|---|
| `id`（必填） | 稳定唯一标识，用于报告与历史对比 |
| `prompt`（必填） | 发给 Agent 的用户消息 |
| `expect_keywords` | 终答中应出现的关键词 |
| `keyword_mode` | `all`（默认，全部命中）或 `any`（命中任一） |
| `expect_tools` | 期望被调用的工具名（子集即可） |
| `forbid_tools` | 不应被调用的工具名（命中即失败） |
| `max_iterations` | 允许的最大工具迭代数（默认 10） |
| `max_latency_ms` | 允许的最大耗时（毫秒，0 不限） |
| `model` | 指定模型（默认不指定） |
| `tags` | 分组标签 |
| `notes` | 人工备注（不参与打分） |

## 运行

- **在线（真实模型）**：

  ```bash
  cd backend
  uv run python -m harness.eval --cases tests/evals/eval_cases.json --provider deepseek
  uv run python -m harness.eval -c tests/evals/eval_cases.json -p deepseek --tag math --report eval.md
  ```

  退出码：全部通过为 `0`，有失败为 `1`（可直接作为 CI 门槛）。

- **离线 / 确定性回归**：见 `tests/test_eval.py`，用脚本化 provider 跑同一批用例，
  不依赖网络、结果可复现。

## 新增用例建议

1. 一个用例只测一个能力点，`id` 用语义化短名（如 `tool-calculator`）。
2. 在线用例的 `expect_keywords` 选模型回答里**稳定出现**的词（数字、专有名词），
   避免用易变的措辞。
3. 用 `tags` 标能力维度（`math` / `tool` / `constraint` 等），便于按类筛选。
