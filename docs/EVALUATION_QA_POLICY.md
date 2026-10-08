# 赛题答疑口径与本地实现

赛题答疑表中的准确率/精确率/召回率答疑把指标说明为二分类迁移：有字段记为 P 样本，无字段/缺失记为 N 样本；TP 是“有字段且标注正确”，FP 是“有字段但标注错误”，并要求结合 TP、FP、TN、FN 计算。该答疑没有继续形式化多值字段、重复记录、实体角色和整条记录的匹配边界。

仓库提供 `official_qa` profile 作为可复现的团队本地实现。它对每个已经进入一对一对齐范围的字段使用互斥分类：

| Gold | Prediction | 类别 |
|---|---|---|
| 有值 | 相同值 | TP |
| 有值 | 不同非空值 | FP |
| 有值 | 缺失 | FN |
| 缺失 | 有值 | FP |
| 缺失 | 缺失 | TN |

因此：

```text
Precision = TP / (TP + FP)
Recall = TP / (TP + FN)
F1 = 2TP / (2TP + FP + FN)
Accuracy = (TP + TN) / (TP + FP + TN + FN)
Weighted = 0.4 × Accuracy + 0.3 × Precision + 0.3 × Recall
```

“双方非空但值错误”在本地答疑解释中只计 FP；Gold 有值而预测缺失仍计 FN。双方缺失只在对齐和未匹配记录构成的可观察字段槽位上计 TN，不能解释为整个公告世界的真阴性数量。空公告或空包不会生成虚构记录来增加 TN。

完整记录是本地扩展口径：双方记录已匹配但任一字段错误或遗漏，记一个记录级 FP；整条预测缺失记 FN，额外预测记 FP，七字段一致记 TP。字段级遗漏仍记 FN，两种粒度不能相加。所有 item 在评分前必须至少有一个非空字段，全空 item 会被 Gold/Prediction schema 拒绝；完整记录、主体名单的 TN 恒为 0。同名中标方的金额比较允许双空记 TN，但不据此生成任何额外主体。

运行方式：

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.evaluation_cli `
  --gold ..\examples\evaluation_gold.json `
  --predictions ..\examples\evaluation_predictions.json `
  --metric-profile official_qa `
  --json-output ..\output\evaluation-official-qa.json `
  --markdown-output ..\output\evaluation-official-qa.md
```

三路线报告可以通过 `route_evaluation_report.py --metric-profile official_qa` 对已有预测重新计算；该操作不调用模型，也不改动预测、Gold 或原始材料，并保留原路线报告的金额/数量容差、整数部分匹配等设置。QA 口径要求严格缺失分类，冲突设置会被拒绝。报告中的 `metric_code_sha256` 记录本次评分代码，`run_identity.code_sha256` 保留抽取运行代码，二者不互相替换。`stage=final` 仍要求三路线全部完成、reviewed Gold 和独立性等检查通过。

旧的 `local_proxy` profile 继续保留，仍使用开放抽取代理 `TP/(TP+FP+FN)`；请求该口径时会核查缓存报告的类型和配置，防止把 QA 数字误贴成历史代理指标。历史报告不能与 `official_qa` 数字直接比较。

`official_qa` 是对答疑文字的本地可执行解释，不是主办方发布的正式评分器。正式比赛若进一步明确多值字段的错值、重复记录、实体和空值口径，应更新 profile 和版本化报告，并重新计算指标。
