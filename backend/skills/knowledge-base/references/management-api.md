# 知识库管理 REST API 参考

> 供模型在执行知识库维护操作时按需读取。基础地址 `http://127.0.0.1:8000`（或以运行环境为准）。
> 写接口在启用认证（`HARNESS_AUTH=1`）时需要管理员身份；未启用认证时直接可用。
> 所有响应为 JSON；错误统一为 `{code, message, detail, trace_id}`。

## 目录

- [1. 知识库 CRUD](#1-知识库-crud)
- [2. 文档上传与管理](#2-文档上传与管理)
- [3. 索引与嵌入模型](#3-索引与嵌入模型)
- [4. 检索（只读）](#4-检索只读)
- [5. 常用操作示例](#5-常用操作示例)

## 1. 知识库 CRUD

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/kb` | 建库：`{"name": "...", "description": "...", "default_retrieval_mode": "keyword|semantic|hybrid"}` |
| GET | `/api/kb` | 列表（含 `doc_count` / `segment_count` / `vector_count` / `need_reindex`） |
| GET | `/api/kb/{kb_id}` | 详情 |
| PUT | `/api/kb/{kb_id}` | 改名 / 改默认检索模式（局部更新，只传要改的字段） |
| DELETE | `/api/kb/{kb_id}` | 删除库（级联文档、分段、落盘文件，**不可恢复**） |

## 2. 文档上传与管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/kb/{kb_id}/documents` | 上传（multipart）：字段 `files`（一个或多个）、`strategy`（auto/S1/S2/S3/S4） |
| GET | `/api/kb/{kb_id}/documents` | 文档列表（状态 / 分段数 / 字数 / 资产数 / 策略） |
| GET | `/api/kb/{kb_id}/documents/{doc_id}` | 单文档状态 |
| DELETE | `/api/kb/{kb_id}/documents/{doc_id}` | 删除文档（含分段，不可恢复） |
| POST | `/api/kb/{kb_id}/documents/{doc_id}/reindex` | 按文档重建索引（可选 `?strategy=S2` 换策略重切） |

文档状态：`pending` / `processing` / `ready` / `empty`（解析后无文本）/ `failed`（`error` 字段含原因）。

支持格式：txt / md / csv / json / log / docx / pdf / 图片（png/jpg/gif/webp/bmp）；
音频（mp3/wav/m4a/flac/ogg/aac）需服务端配置 ASR 端点（`HARNESS_KB_ASR_ENDPOINT`）。

## 3. 索引与嵌入模型

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/kb/{kb_id}/segments` | 分段列表（排障；`?doc_id=` 过滤） |
| PUT | `/api/kb/{kb_id}/embedding-model` | 绑定/更换模型：`{"embedding_model_id": "..."}`，null 解绑；变更自动置 `need_reindex` |
| POST | `/api/kb/{kb_id}/reindex` | 全量重建向量（换模型后必须执行） |
| GET | `/api/kb/embedding-models` | 模型配置列表（密钥脱敏） |
| POST | `/api/kb/embedding-models` | 新建模型（保存前自动连通性探针，失败拒绝保存） |
| POST | `/api/kb/embedding-models/probe` | 仅测试连接（不保存），成功返回实际维度 |
| PUT / DELETE | `/api/kb/embedding-models/{model_id}` | 更新 / 删除模型配置 |

新建模型请求体（OpenAI 兼容）：
`{"name": "配置名", "model_name": "text-embedding-3-small", "endpoint": "http://127.0.0.1:11434/v1", "api_key": "...", "dimensions": 0}`（dimensions=0 时以探针实测维度回填）。

## 4. 检索（只读）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/retrieval` | 统一检索：`{"query": "...", "knowledge_base_ids": ["..."], "retrieval_mode": "hybrid", "top_k": 5, "score_threshold": 0.3, "rerank": true}` |
| POST | `/api/skills/knowledge_base_search` | 技能入口（多一步提问分析，返回体附 `analysis`；支持 `force_retrieve` / `force_skip`） |

> 日常问答一律用 `knowledge_base_search` 工具，不要直接拼 REST 请求；
> 本节端点仅在需要脚本化联调时使用。

## 5. 常用操作示例

```bash
# 列出知识库（拿 kb_id）
curl -s http://127.0.0.1:8000/api/kb

# 上传一个文档（strategy 可省略，默认 auto）
curl -s -X POST http://127.0.0.1:8000/api/kb/<kb_id>/documents \
  -F "files=@/path/to/手册.md" -F "strategy=auto"

# 查文档状态（轮询至 ready/failed）
curl -s http://127.0.0.1:8000/api/kb/<kb_id>/documents/<doc_id>

# 换模型后全量重建向量
curl -s -X POST http://127.0.0.1:8000/api/kb/<kb_id>/reindex
```

> 注意：Windows 的 Git Bash 下用 `curl -d` 直接带中文 JSON 会因编码报
> `error parsing the body`；把 JSON 写入文件后用 `--data-binary @file` 发送。
