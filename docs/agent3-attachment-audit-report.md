# 附件异常审计台账

本报告记录 `backend/scripts/attachment_audit.py` 对 2026-09-26 完成的 1,038 条公告结果快照进行审计的历史结果，未修改原始 HTML、压缩包、解析结果、模型提示词或 holdout 目录。本次可提交的聚合摘要见 [`benchmarks/attachment-audit-summary.json`](benchmarks/attachment-audit-summary.json)，仅包含计数、明细快照哈希和验收边界。

逐来源明细台账 `docs/benchmarks/attachment-audit-ledger.json` 在本地原位保留，**本次不提交**：文件约 14.9 MB，包含全部官方附件成员路径与原始 warning，不是已完成公开审查的聚合统计。摘要只描述该历史文件，不代表当前修复后的脚本已对全部官方材料重新验证。

## 覆盖范围

每条记录使用包含公告号、来源路径和来源哈希的 SHA-256 生成稳定 `record_id`，包含文件类型、引用关系、压缩成员、状态、warning 分类、原始 warning、是否可重试、人工处置状态和补采备注。重复的 `source_file` 在同一结果中去重。历史生成器尚未正确区分顶层归档哈希与叶成员哈希，且曾把 ZIP 成员存在视为 HTML 引用命中，因此本快照的哈希及引用布尔值不能作为已核验的叶文件证据。

2026-10-07 对本地历史台账只读重计得到 14,262 条来源记录（以下是历史快照计数，不是当前解析成功率）：

| 状态 | 数量 |
| --- | ---: |
| parsed_or_partial | 10,956 |
| naming_mismatch | 2,206 |
| reference_material | 986 |
| orphan_or_unattributed | 0 |
| download_error_response | 87 |
| program_parse_failure | 19 |
| unsupported_format | 7 |
| archive_member_read_failure | 1 |

官方附件统计报告的 13,136 个解析叶文件与本台账的来源粒度不同：台账另有 1,038 条公告 HTML、87 条下载错误和 1 条未归属成员警告，因此总行数更高。下载错误 87、压缩成员读取失败 1、解析失败 19、不支持格式 7、参考材料 986 与历史统计相符；状态一致不等于附件内容完整或抽取准确。

## 分类和处置规则

- `corrupt_file` 仅用于明确的源内容损坏或容器展开失败；不会把程序异常推断成文件损坏。
- `download_error_response` 对应源附件不可用/错误响应，`retryable=true`，人工处置为 `pending`。
- `program_parse_failure` 保留原始文件，`retryable=false`；需要人工复核解析器或转码路径。
- `unsupported_format`、`reference_material` 分别表示当前解析器不支持和明确的参考材料，不标记为解析成功。
- `naming_mismatch` 来自内容识别格式与扩展名不一致的 warning；HTML、ZIP/RAR/7z 的名称不会静默丢弃。
- `orphan_or_unattributed` 表示来源未能在同公告 HTML 引用或压缩成员关系中找到归属，需人工核对；不会自动删除。

历史脚本优先读取结果 JSON 的 `source_hashes`，缺失时对 `official-corpus` 顶层文件计算 SHA-256，曾将该容器哈希用于叶成员记录。统一审查已修正脚本：仅在可读取真实成员内容时计算成员哈希；嵌套归档、RAR/7z 或超出 64 MiB 读取上限的成员将哈希标为未知并附加 `source_hash_unavailable`，不再用容器哈希代替。HTML 引用只按 HTML 链接确定，不把归档成员存在推定为引用命中。默认输出改为 `backend/.data/attachment-audit-ledger.json`。本地旧明细保持原样，不将其宣称为修复后的验收产物。

## 复现

```powershell
cd D:\ICT\bid-intel\backend
.venv\Scripts\python.exe -m scripts.attachment_audit --output .data\attachment-audit-ledger.json
.venv\Scripts\python.exe -m scripts.attachment_audit --format csv --output .data\attachment-audit-ledger.csv
.venv\Scripts\python.exe -m pytest -q tests/test_attachment_audit.py
.venv\Scripts\python.exe -m ruff check scripts/attachment_audit.py tests/test_attachment_audit.py
```

复现需要本地结果快照及官方材料；它们不随仓库提供，须通过 `--results` 和 `--corpus` 指定实际路径。输出应保存在被忽略的 `backend/.data/`，不要把完整来源清单或原始告警重新加入 Git。合成测试覆盖来源去重、引用/归档归属及异常分类；测试结果以统一验收记录为准，未宣称 Ubuntu 官方资源环境验收。
