# 本地抽取验证集与评估

这套工具评估本地人工标注集上的抽取结果。它不是官方评测，也不会产生或宣称官方实测得分。赛题提供的加权公式没有完整公开字段匹配、缺失值和准确率口径，本实现的匹配规则与 Accuracy 都是明确可复现的本地约定，收到官方说明后应逐项校准。

## 两份独立输入

- Gold：`examples/evaluation_gold.json`，人工标注文件，`status` 是 `draft` 或 `reviewed`。
- Predictions：`examples/evaluation_predictions.json`，算法输出文件，`status` 固定为 `predicted`。
- schema_version 固定/默认为 `1.0`。对应 JSON Schema 为 `examples/evaluation_gold.schema.json` 和 `examples/evaluation_predictions.schema.json`。
- 示例仅为人为构造的测试数据，并非官方数据或真实模型性能结果；预测样例故意包含错误。

结构：dataset → notices → packages。`notice_id` 是不可随数据库自增 ID 或重新导入改变的源公告标识，推荐用数据集给定标识，或公告规范相对路径/内容摘要；gold 与预测必须复用同一个映射。`package_id` 是公告内稳定包/标段 ID，没有分包时统一使用 `default`，不能在预测与标注里分别猜测。

每包必须显式有 `items`、`buyer`、`winners`、`bidders`。buyer 可为 null；其他列表可为空。每条 item 的七个字段 key 都必须存在，未知用 JSON null。字段是 `product_name`、`category`、`brand`、`model`、`unit_price`、`quantity`、`total_price`；item_id 用于审计。实体为 `entity_id` 和 `name`；winners 再带 `award_amount`（可 null），bidders 再带 `outcome`（winner、nonwinner、unknown）。

bidders 的口径是所有已确认投标的主体，包含中标方。非中标方是 outcome=nonwinner 的子集；unknown 不能推断为非中标。角色不互相自动补全：若同一主体既为中标方又出现在全体竞标人中，需要分别填写；这可检测角色提取遗漏。buyer/winners/bidders 之间不交叉抵消错误。

严格校验拒绝未知字段、缺失必需字段、错误类型、布尔数值、非有限数、未支持的数字单位、空 ID/空实体名、全空 item，以及重复 notice_id、包内重复 package_id/同列表重复 item_id 或 entity_id。不同列表可复用同一 entity_id，不同包可复用 item_id。ID 不是相似度匹配依据，只有 notice_id 和 package_id 是不可越过的匹配边界。名称相同但 ID 不同的重复输出不会被静默去重。

## 人工审核流程

1. 固定源公告 ID/包 ID 映射，保留一份原始算法输出作为 predictions。
2. 创建 status=draft 的独立标注文件，逐项对照源公告/附件核验全部标的字段及角色；模型草稿不能当作真值。
3. 由人工确认范围完整、空值含义和角色口径后，才把 status 改为 reviewed。应留存标注人/复核人与来源证据的过程记录（不混入严格评估 JSON）。
4. 运行评估，查看 JSON alignments 中错误字段和缺失记录，再回原始材料核对。训练/提示调优集与最终留出验证集应分离。

默认拒绝 draft Gold。`--allow-draft` 仅允许临时自查，输出 `provisional=true` 和明显草稿提示，不可作为人工核验结果。当前状态是整个文件的审核声明；文件里只要还有未审核公告，就不能标 reviewed。

## 匹配与归一化

在完全相同 notice_id + package_id 范围内进行一对一最大权重匹配（Hungarian），每对 item 的权重是七字段中正确且双方非空的字段数；不会先贪心选局部最佳记录，零权重边不匹配。先按稳定 item_id 排序，再确定性求解并处理同权重，因此输入列表顺序不影响报告。不同 item_id 可以匹配；同一个预测不能匹配两个真值。这个策略优化字段总匹配数，不承诺最大化完整记录数，亦不使用隐式同义词或语义嵌入。

- 文本/名称：Unicode NFKC、casefold、移除 Unicode 空白。保留标点、公司后缀、分公司、地域和型号连字符；不把简称或相似名称擅自合并。
- 数值：Decimal 解析，允许 JSON 有限数字、十进制/科学计数法字符串，以及正确的千分位分组。不先四舍五入；`0` 是有效值，不是缺失。
- 金额统一以人民币元比较，可显式解析 CNY/RMB/人民币/¥ 前缀、元后缀、万/亿缩放。例如 `人民币0.1万元` 和 `1000` 相等。外币、百分比和未支持单位直接拒绝，不做猜测。
- 数量仅用无单位数字或正确分组数字字符串，`2台` 必须在导出前转为 `2`。评估七字段不包含数量单位；如果单位可能不同，先人工统一口径再导出，不能用本框架宣称单位级正确。
- 判等容差：`abs(a-b) <= max(absolute_tolerance, relative_tolerance * max(abs(a),abs(b)))`。金额绝对容差默认 0.01 元，数量默认 0.000001，相对容差默认 1e-9，报告中记录实际参数。需要完全相等时将三个容差设为 0。
- null、空串、纯空白都视作缺失。双空不计 TP/TN；一边有值计 FP 或 FN；双方非空但值错，同时计一个 FP 和一个 FN。
- 真值和预测都是多重集：额外重复行/重复实体计 FP；如果真值确有两条相同内容但不同 item_id 的业务记录，需要预测两条才能覆盖。不要人为复制标注行提高权重。

实体按包隔离并按归一化名称一对一匹配，分别报告 buyer、winners、bidders、nonwinners；bidder_outcomes 要求名称与 outcome 同时一致。winner_amounts 要求同名中标方金额一致，与标的物总价分开。缺失/额外公告及包中的全部正例分别计 FN/FP；仅空公告不会产生虚构正例。

## 指标定义

每个字段和汇总 micro，以及完整记录、各类实体分别输出 TP/FP/FN、Precision、Recall、F1、Accuracy 和 Weighted：

| 指标 | 本地定义 |
|---|---|
| Precision | TP / (TP + FP) |
| Recall | TP / (TP + FN) |
| F1 | 2TP / (2TP + FP + FN) |
| Accuracy | **本地开放抽取代理** TP / (TP + FP + FN)；不是 (TP+TN)/总数 |
| Weighted | 0.4 × Accuracy + 0.3 × Precision + 0.3 × Recall；只是套用权重的本地代理 |

开放抽取没有可枚举真阴性 TN，双空也不能添加 TN 来提高得分。分母为 0 输出 JSON null/Markdown N/A，而不是把空数据集算作满分；Weighted 任一组成项未定义时也为 null。完整记录 TP 要求一对匹配记录的全部七字段都相等（包括空值位置一致），并且记录至少含一个非空字段。部分正确的记录在 records 中仍同时计 FP/FN，但正确字段在字段指标中保留 TP。报告保留逐条 alignment 和每字段状态，便于追溯统计。

## 命令行与 Python

从 `backend` 目录运行：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation_cli --gold ..\examples\evaluation_gold.json --predictions ..\examples\evaluation_predictions.json --json-output ..\output\evaluation-report.json --markdown-output ..\output\evaluation-report.md
```

可用参数：`--allow-draft`、`--money-tolerance`、`--quantity-tolerance`、`--relative-tolerance`。输入不合法或试图把报告覆盖到输入文件时返回退出码 2。两个输入必须是独立路径；JSON 和 Markdown 输出也必须不同。

```python
from app.evaluation import GoldDataset, PredictionDataset, evaluate_dataset, render_markdown

gold = GoldDataset.model_validate(gold_dict)
predictions = PredictionDataset.model_validate(predictions_dict)
report = evaluate_dataset(gold, predictions)  # draft 默认拒绝
json_ready = report.model_dump(mode="json")
markdown = render_markdown(report)
```

公开加载函数为 `load_gold(path)` 和 `load_predictions(path)`；`write_machine_report(report, path)` 保存 JSON。该模块不调用任何模型或读取 API Key，所以重复评估同一输入与参数得到相同报告。
