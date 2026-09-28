# 任务一：6 条 Gold 附件 pilot 三路线评测

范围：同一批 6 条共同试标、已裁定的 reviewed Gold，分别回放 HTML 与配对 ZIP 的全部可展开文件；5 条有 ZIP，1 条只有 HTML。使用本地 RapidOCR 和 `deepseek-v4-flash`，模型输出上限 16384。Gold 未修改，SHA-256 为 `cfa3fa242bf6f1eec0bd474731e1d48a126df9259e9148c837c8f8458bb5759f`。输出位于 [`evaluation-routes-attachments-pilot-20260928`](../../backend/.data/annotation-tasks/official-20260926-LHH-YHR/evaluation-routes-attachments-pilot-20260928)。三路线 predictions 均含相同的 6 个 notice ID，无缺失或额外公告。

| 路线 | 完成 | 字段 Weighted | 完整记录 Weighted | 字段 TP/FP/FN | 记录 TP/FP/FN |
|---|---:|---:|---:|---:|---:|
| rules | 6/6 | 92.1179% | 74.9145% | 653/58/23 | 81/22/18 |
| hybrid | 6/6 | 70.8524% | 47.2286% | 654/428/22 | 81/159/18 |
| model | 6/6 | 77.9191% | 49.1913% | 653/268/23 | 80/134/19 |

Weighted 是仓库 `evaluation.py` 的本地开放抽取代理分：`0.4 × Accuracy + 0.3 × Precision + 0.3 × Recall`，不等于官方竞赛成绩。本轮模型路线相对 rules 几乎没有增加 TP，却使 hybrid/model 的字段 FP 分别增加 370/210；完整记录 FP 分别增加 137/112。附件中的声明、需求模板和重复候选需要逐项核验，目前不能把新增候选认定为召回提升。

| 路线 | 实际累计墙钟 | 单公告最终尝试中位数 | 实际请求 | 成功响应 | Prompt tokens | Completion tokens |
|---|---:|---:|---:|---:|---:|---:|
| rules | 29.9s | 1.9s | 0 | 0 | N/A | N/A |
| hybrid | 2182.9s | 202.3s | 44 | 44 | 66,190 | 38,683 |
| model | 3478.8s | 213.1s | 45 | 44 | 66,190 | 41,634 |

两条模型路线共 **89 次实际请求、132,380 prompt tokens、80,317 completion tokens**。model 最后一条首次尝试有 1 次 `RemoteProtocolError`，该次无供应商 token usage；续跑复用成功响应后完成，失败请求和首次尝试的 1596.6 秒均计入上表。请求数按调用 ID 去重，缓存命中不重复计费。`summary.json` 包含逐公告耗时、解析缓存和请求用量；token 是接口报告的数量，不等于货币费用。

连同[此前 HTML-only 的三轮独立运行](route-evaluation-html-20260928.md)，这组路线评测可核对的历史总量为 **233 次模型请求、435,308 prompt tokens、336,340 completion tokens**；其中附件 pilot 单独占 89 次。HTML 结果目录自身只保留最后一轮的请求统计，不能用单个 `summary.json` 代替历史总量。

逐条请求数（rules 均为 0；顺序为 hybrid/model）：`08e592` 1/1、`42e867` 4/4、`7ed1fb` 6/6、`95c7f4` 5/5、`a3a603` 25/25、`e9d6a9` 3/4（model 含 1 次断线）。最慢的 `95c7f4` 两路线约 843/854 秒；hybrid/model 的模型调用累计耗时约 2159/3445 秒。调用量和时延随附件数与长文输出急剧变化，不能由 HTML-only 的单篇中位数外推。

本阶段只覆盖 6/24 条 Gold 的附件回放。它显示这批附件上 rules 显著优于 hybrid/model，但不代表余下 18 条、全部官方语料或官方评分。后续应先定位本批 FP 的来源，再决定是否扩展到 24 条；保持 Gold 和抽取优先级不变，避免按试标样本调指标。
