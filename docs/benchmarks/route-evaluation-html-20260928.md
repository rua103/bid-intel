# 任务一：Rules / Hybrid / Model 同 Gold 评测

范围：`html`，24 条 reviewed Gold；不含 ZIP、PDF 或 OCR 附件回放。Gold 文件未修改。

评测输出：[`evaluation-routes-html-20260928-retry16384`](../../backend/.data/annotation-tasks/official-20260926-LHH-YHR/evaluation-routes-html-20260928-retry16384)。

Gold SHA-256：`cfa3fa242bf6f1eec0bd474731e1d48a126df9259e9148c837c8f8458bb5759f`。

| 路线 | 完成 | 字段 Weighted | 完整记录 Weighted | 字段 TP/FP/FN | 记录 TP/FP/FN | 包号集合精确对齐 |
|---|---:|---:|---:|---:|---:|---:|
| rules | 24/24 | 89.5457% | 74.3357% | 1343/57/172 | 189/35/62 | 16/24 |
| hybrid | 24/24 | 83.6201% | 66.6153% | 1376/256/139 | 191/83/60 | 17/24 |
| model | 24/24 | 90.8097% | 73.8601% | 1375/62/140 | 193/43/58 | 17/24 |

Weighted 是仓库 `evaluation.py` 的本地开放抽取代理：`0.4 × Accuracy + 0.3 × Precision + 0.3 × Recall`，不是真实官方竞赛分。三路 predictions 均为相同的 24 个 Gold notice ID；Gold 为 39 个包、251 条 item。

| 路线 | 保留结果墙钟合计 | 单篇中位数 | 保留请求 | Prompt tokens | Completion tokens |
|---|---:|---:|---:|---:|---:|
| rules | 2.344s | 0.084s | 0 | N/A | N/A |
| hybrid | 2457.482s | 48.646s | 24 | 50,488 | 46,650 |
| model | 2422.279s | 46.716s | 24 | 50,488 | 46,243 |

模型为 `deepseek-v4-flash`。4096 上限时 4 条长公告截断；8192 后仍有 1 条截断；16384 后三路线才全部完成。三阶段在**三个独立目录**运行，没有跨目录模型缓存命中；每个阶段、每条模型路线都重新请求了 24 条公告。上表只统计最终 16384 目录的保留结果，不能作为实际总成本。

| 输出上限 | hybrid 请求 / prompt / completion tokens | model 请求 / prompt / completion tokens |
|---|---:|---:|
| 4096 | 24 / 50,488 / 35,989 | 24 / 50,488 / 35,748 |
| 8192 | 24 / 50,488 / 45,886 | 24 / 50,488 / 45,507 |
| 16384 | 24 / 50,488 / 46,650 | 24 / 50,488 / 46,243 |
| **实际累计** | **72 / 151,464 / 128,525** | **72 / 151,464 / 127,498** |

两条模型路线实际共调用 **144 次**，合计 **302,928 prompt tokens、256,023 completion tokens**。各阶段用量从对应输出目录的 `progress.json` 中逐条 `model_calls` 复核；最终 `summary.json` 只含最后一轮的 24 次调用。

结论：当前 HTML-only 代理分中，model 的字段分比 rules 高 **1.2640 个百分点**，但完整记录低 **0.4756 个百分点**；hybrid 两项均低于 rules，且字段 FP 明显增加。不能据此宣称 hybrid 更好，也不能称 model 全面胜出。model 的主体指标仍包含 parser 保留的 bidder table 结果。附件回放、OCR 影响和官方评分规则仍未在本记录中验证。
