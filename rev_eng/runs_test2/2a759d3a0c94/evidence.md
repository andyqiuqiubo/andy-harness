# Evidence v2 证据包 `2a759d3a0c94`

- Schema：andy-harness.evidence/v2
- 生成时间：2026-10-15T20:50:38+0800
- 证据条数：3
- 链头（chain_head）：`99b9b54265c7118f5f26e4e76e641d295495a3c3bfa5a3b8c132329553725882`

## 分析对象

- 名称：Hello.class
- 类型：pe32+
- 大小：1207 字节
- SHA-256：`2fb106f349f7394827320162046df88cd494e8fa579eb764f011fb8f79f01fc8`

## 溯源（provenance）

- **run_id**：2a759d3a0c94
- **pipeline**：andy-harness:reverse-engineering/1.0
- **stages**：[{"stage": "rea_static", "engine": "none", "target_engine": "rea+ghidra", "status": "skipped"}, {"stage": "java_decompile", "engine": "javap", "target_engine": "javap", "status": "ok"}, {"stage": "frida_dynamic", "engine": "none", "target_engine": "frida", "status": "skipped"}, {"stage": "firecracker_sandbox", "engine": "none", "target_engine": "firecracker", "status": "skipped"}, {"stage": "pentagi_analysis", "engine": "none", "target_engine": "pentagi", "status": "skipped"}, {"stage": "rebuild_exe", "engine": "none", "target_engine": "tcc-rebuild", "status": "skipped"}]
- **notes**：
- **environment**：{"os": "Windows-10-10.0.18363-SP0", "machine": "AMD64", "python": "3.13.14", "hostname": "<HOST>"}

## 证据链

| # | 阶段 | 类型 | 说明 | 产物 | SHA-256（前16） | 链哈希（前16） |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | java_decompile | java_decompile | javap 反编译文本（1677 字符，2 个方法） | `payloads/Hello_decompiled.java.txt` | `07c98f22bf421ff5` | `66a44d3751a5c312` |
| 2 | evidence_v2 | facts_ledger | 跨阶段流转的结构化事实台账 | `payloads/facts-ledger.json` | `b0730140c35154b3` | `23f92901d1e84bb6` |
| 3 | evidence_v2 | run_manifest | 流水线运行清单（各阶段状态/引擎/耗时） | `payloads/run-manifest.json` | `f7553bf76bfbd5b1` | `99b9b54265c7118f` |

> 每条证据的链哈希 = sha256(prev_hash|seq|stage|kind|payload_sha256)，任一环被篡改都会导致后续链断裂。校验：`verify_bundle()`。
