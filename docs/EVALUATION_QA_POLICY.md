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

“双方非空但值错误”按官方答疑的二分类解释只计 FP；Gold 有值而预测缺失仍计 FN。双方缺失只在可观察的字段槽位上计 TN。实体和完整记录没有可枚举的全体负样本，因此它们的 TN 只来自当前已对齐/已观察的空槽，不能解释为整个公告世界的真阴性数量。

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

三路线报告可以通过 `route_evaluation_report.py --metric-profile official_qa` 对已有预测重新计算；该操作不调用模型，也不改动预测、Gold 或原始材料。旧的 `local_proxy` profile 继续保留，仍使用开放抽取代理 `TP/(TP+FP+FN)`，历史报告不能与 `official_qa` 数字直接比较。

`official_qa` 是对答疑文字的本地可执行解释，不是主办方发布的正式评分器。正式比赛若进一步明确多值字段的错值、重复记录、实体和空值口径，应更新 profile 和版本化报告，并重新计算指标。
