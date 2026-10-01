# 三路线评测准备

评测分两层：`app.gold_route_evaluation` 回放 `rules`、`hybrid`、`model` 并保存预测、字段/记录/实体指标、请求/耗时/token 遥测；`app.route_evaluation_report` 只读这些产物，补充包号集合对齐、重复候选和附件/OCR覆盖，并生成可粘贴到 GAP 的 Markdown 表。

所有报告必须写明“团队本地 Gold 验证，不是官方成绩”。`gold.merged.json` 是已使用过的调优 Gold；新的 holdout reviewed Gold 生成前不得运行最终留出评测，也不得把 holdout 的 draft bundle 或 canonical predictions 当真值。

从 `backend` 目录执行。Agent 1 完成并冻结抽取代码、提示词、模型参数后，先对受影响公告使用全新的输出目录做定向复评：

```powershell
$py = ".venv/Scripts/python.exe"
$gold = ".data/annotation-tasks/official-20260926-LHH-YHR/gold.merged.json"
$manifest = ".data/annotation-tasks/official-20260926-LHH-YHR/sample_manifest.csv"
$sourceRoot = "D:/ICT/official-corpus"
$run = ".data/annotation-tasks/official-20260926-LHH-YHR/evaluation-agent1-targeted"

$py -m app.gold_route_evaluation `
  --gold $gold --manifest $manifest --source-root $sourceRoot `
  --output-dir $run --scope attachments `
  --notice-id <受影响公告ID> --notice-id <另一个受影响公告ID>

$py -m app.route_evaluation_report `
  --gold $gold --manifest $manifest --run-dir $run `
  --dataset-role targeted --stage targeted
```

定向报告用于确认改动影响，推荐状态会保持 pending，不得当作最终路线结论。

留出集的 reviewed Gold 和来源清单就绪、抽取版本冻结后，最后只运行一次完整三路线评测：

```powershell
$holdoutGold = ".data/annotation-tasks/official-holdout-20260929/gold.reviewed.json"
$holdoutManifest = ".data/annotation-tasks/official-holdout-20260929/sample_manifest.csv"
$run = ".data/annotation-tasks/official-holdout-20260929/evaluation-routes-final"

$py -m app.gold_route_evaluation `
  --gold $holdoutGold --manifest $holdoutManifest --source-root $sourceRoot `
  --output-dir $run --scope attachments

$py -m app.route_evaluation_report `
  --gold $holdoutGold --manifest $holdoutManifest --run-dir $run `
  --dataset-role holdout --stage final --tuning-gold $gold
```

`--stage final` 要求 holdout 和 tuning Gold 都是 `reviewed` 且 notice ID 不重叠。报告的路线推荐使用透明的团队本地规则：最大化 `min(field_micro.weighted_score, records.weighted_score)`，再比较两者均值、包号集合精确对齐率，平局时选择重复候选更少、请求更少的路线。这只是团队本地决策规则，不是官方评分公式。
