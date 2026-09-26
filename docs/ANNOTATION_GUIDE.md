# 人工标注指南（协调员用）

> ⚠️ **这份是给协调员的**，里面有生成任务包、比较、合并的命令。
> **标注员请转去看 [人工标注手册（标注员用）](ANNOTATION_ANNOTATOR.md)** —— 那份没有命令，只有操作步骤。

适用于两位标注员和一位协调员。正式入口是导入协调员生成的 task bundle，不要手工上传整批官方原件。Gold 是本地人工评测数据，不是官方成绩。字段结构见 [Gold JSON Schema](../examples/evaluation_gold.schema.json)，标注口径补充见 [EVALUATION.md](EVALUATION.md)。

## 主流程：共同试标 6 条，再各做 9 条

1. 协调员从已完成的全量后台任务生成一个 24 条任务包，默认固定 seed；其中 pilot 是两人重复标的同一 6 条，剩余 18 条自动分成不重叠的 A、B 两批，各 9 条。[annotation_tasks_cli.py](../backend/app/annotation_tasks_cli.py) 同时产出唯一预测、抽样清单、任务 bundle 和 4 个可直接分发的 ZIP。
2. 先只发送 A 的 `pilot-a.ready.zip` 给标注员 A、B 的 `pilot-b.ready.zip` 给标注员 B。两人独立标完、逐条核验并导出 Gold。协调员运行 [annotation_compare_cli.py](../backend/app/annotation_compare_cli.py) 查看差异，再逐项对照官方原文人工裁决，形成唯一 `pilot.adjudicated.json`。比较工具只报差异，不投票、不选答案。
3. 口径校准完成后，发送 `annotator-a.ready.zip` 和 `annotator-b.ready.zip`。两批各 9 条、互不重叠。两人完成后，各自导出完整已核验 Gold。
4. 协调员把 6 条人工裁决后的 pilot Gold 与两个正式 Gold 合并，用 `--require-complete` 对照 canonical predictions，要求 24 条公告全部覆盖。两份重复 pilot 原始标注不能一同合并。

当前这份样本的真实覆盖数见任务包 `sample_summary.json`：有附件 22 条、正文表格有候选 13 条、严格无配对 ZIP 且有候选 2 条、OCR 提示 16 条、多包候选 3 条、候选超过 15 条 4 条、零候选 7 条。**这些标签会重叠，不是互斥分层**；正文有候选不等于没有附件。尤其“无配对 ZIP”只有 2 条，不能把它夸大成有代表性的“仅正文”组。

## 协调员：生成并分发任务包

本轮可直接分发的任务已经生成在 `.data\annotation-tasks\official-20260926-team-ready`，**不需要再运行生成命令**。将来要重建时，在 `backend` 目录运行下面命令；输出目录必须是 `.data` 下全新的目录，CLI 会拒绝覆盖非空目录。脚本会额外生成 4 个带队友操作卡和专属原件的可分发 ZIP。若用新目录重建，后面的比较/合并命令也要统一替换成新目录：

```powershell
.\.venv\Scripts\python.exe -m app.annotation_tasks_cli `
  --job-dir ".data\jobs\47508ea8390b4522a6766d79c0188b5d" `
  --output-dir ".data\annotation-tasks\official-20260926-team-rerun" `
  --sample-size 24 --pilot-size 6 --seed 20260926
```

协调员保管 `predictions.canonical.json`、`sample_manifest.csv` 和 `sample_summary.json`。直接分发生成的 ZIP：先给 A `pilot-a.ready.zip`、给 B `pilot-b.ready.zip`；试标口径统一后，再分别发 `annotator-a.ready.zip`、`annotator-b.ready.zip`。ZIP 内只有该阶段的 bundle、队友操作卡和对应原件；不要把另一人的专属原件发出去。任务包仅包含 24 条样本原件，不是全量语料。

## 标注员：导入、核验、保存

1. 解压收到的 ZIP，打开协调员发来的标注页面链接，直接进入独立标注工作台；点 **“载入我的标注任务”**，导入 ZIP 内的 bundle。此模式不需要后端/API Key；不要把 `localhost` 地址转发给另一台电脑，应使用协调员给的共享页面链接。确认顶部显示任务名和总条数。
2. 每条公告逐包对照原文和附件填写标的七字段、采购单位、中标方和投标方。解析/OCR 文本只是导航线索，扫描件必须打开 `sources` 下的原附件核对。某条材料缺失、损坏、无法阅读或包号有疑问时，不要勾该条，联系协调员。
3. 每条分别勾选 **“本公告已对照原文”**。修改该公告任一字段后，核验会自动撤销；改完重新对照并勾选。页面会显示已核验数，并可跳到下一条待核验。
4. 页面按 `task_id` 自动保存到当前浏览器的 **IndexedDB**。更换设备、浏览器配置或交接前，点 **“下载进度备份”**；文件名会自动带任务 ID。恢复时用同一个“载入我的标注任务”按钮载入进度备份；它会恢复来源、标注和每条核验快照。自动保存不能代替下载备份。
5. 一批 9 条全部核验后，点 **“导出已核验 Gold（9/9）”**；文件会自动命名为 `annotator-a.gold.reviewed.json` 或 `annotator-b.gold.reviewed.json`，直接交给协调员，不用手动重命名。未完成时导出的文件带 `.partial`，不能用于最终合并。可以另存 Gold 草稿作中途备份。

## 标注口径

证据顺序：明确改写结果的官方更正/补充公告优先；其次是该公告的中标/成交结果正文及明确对应的结果附件。其他同项目材料只有明确记载实际成交结果时才作为补充。招标/采购需求、预算、空白响应模板和常识推断不能当成交事实。同级官方来源互相冲突时记入任务外的协调记录，交协调员核原文裁决。

- **未披露不等于不可读**：原文确实未披露才留空/null。附件缺失、损坏、加密、OCR 不可靠、来源归属不明属于待解决问题，不得当作空值；该公告保持未核验。
- **七字段与金额**：记录产品/服务名称、品目、品牌、规格型号、数量、单价和总价。数量只填数值，不含“台”等单位。金额统一人民币元；不以单价乘数量推导未披露总价，也不把项目总中标额复制到每个标的。不要留下七字段全空的标的行，严格 schema 会拒绝它。
- **包与主体**：按官方包/标段记录；未分包用 `default`。预测只是候选结构，可能漏包或多包；以原文为准，页面可改包号、补加原文包、删除多余预测包。删除已有标注内容的包会弹出确认框；每个包号必须非空且唯一。比较和合并会保留包结构差异并报告，评分器会把不一致计为包级漏报/多报，不要为了让编号匹配预测而改错 Gold。
- **中标方与投标方**：原样记录官方名称，不自行合并简称、分公司或相似主体。中标方要在 `winners` 与 `bidders` 各记录一次，后者结果为 `winner`；只把明确参加投标的主体列入 `bidders`。明确未中标填 `nonwinner`；已确认投标但结果未披露填 `unknown`，不能因为没获奖信息就推定落标。中标金额只填明确披露的该主体/该包金额。
- **联合体**：将公告中的联合体整体名称作为一个投标/中标主体，不拆分成员，也不把同一中标额分配给各成员。Schema 没有联合体成员关系字段；如原文另列成员，记录在外部裁决/审计表。

协调员另存审计表：公告 ID、包号、字段、来源文件及页码/表格行、摘录、处理人与裁决。评测 JSON 是严格结构，不能添加过程备注、出处或争议字段。

## 试点比较与最终合并命令

以下路径可按实际下载位置替换，输出路径必须是新文件。pilot 导出文件会自动带 A/B 任务名；比较报告不作裁决：

```powershell
.\.venv\Scripts\python.exe -m app.annotation_compare_cli `
  --annotator-a ".data\annotation-tasks\official-20260926-team-ready\pilot-a.gold.reviewed.json" `
  --annotator-b ".data\annotation-tasks\official-20260926-team-ready\pilot-b.gold.reviewed.json" `
  --output ".data\annotation-tasks\official-20260926-team-ready\pilot.comparison.json"
```

协调员对照原文处理 `pilot.comparison.json` 中的每项差异，手工形成且只保留一份 6 条 `pilot.adjudicated.json`（`schema_version: "1.0"`、`status: "reviewed"`）。之后运行 [annotation_merge_cli.py](../backend/app/annotation_merge_cli.py) 做最终合并：

```powershell
.\.venv\Scripts\python.exe -m app.annotation_merge_cli `
  --gold-files ".data\annotation-tasks\official-20260926-team-ready\pilot.adjudicated.json" `
               ".data\annotation-tasks\official-20260926-team-ready\annotator-a.gold.reviewed.json" `
               ".data\annotation-tasks\official-20260926-team-ready\annotator-b.gold.reviewed.json" `
  --predictions ".data\annotation-tasks\official-20260926-team-ready\predictions.canonical.json" `
  --output ".data\annotation-tasks\official-20260926-team-ready\gold.merged.json" `
  --require-complete
```

合并器拒绝重复公告、draft/空 Gold、输入输出路径相同，以及 Gold 中不存在的公告 ID；`--require-complete` 还要求所有 24 条预测公告均有 Gold。Gold 与预测包号不同时会显示双方独有包号并继续合并，后续评测按包范围计算漏报/多报。工具不核验答案是否忠于原文，也不替代 pilot 人工裁决。新输出文件不得已有；命令失败时先处理清单或裁决，不要删行、改真值来规避检查。

当前有专用页面 [annotation.html](../frontend/annotation.html)，无需进入主仪表盘或启动 API；`npm run dev` 已监听局域网地址时，可将 `http://<协调员电脑局域网IP>:5173/annotation.html` 发给同一局域网队友。页面按任务 ID 使用 IndexedDB 隔离保存，修改会撤销核验，并可下载进度备份；包号重复、空白标的行、空主体名和非法数字会阻止勾选核验并给出提示。规则卡也会放进每个分发 ZIP：[ANNOTATION_QUICKSTART.txt](ANNOTATION_QUICKSTART.txt)。仍没有来源页码/摘录字段、争议标记或联合体成员结构。不要将任何本地评测结果称为官方准确率；评测指标口径见 [EVALUATION.md](EVALUATION.md)。
