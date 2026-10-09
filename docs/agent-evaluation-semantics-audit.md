# 评测口径与 Gold 质量独立审计

日期：2026-10-09。范围：`official-holdout-20260929/evaluation-routes-qwen-20261009` 已完成的三路线预测；24 公告，Gold 45 包 / 117 标的。仅团队本地标签和本地指标；官方没有对外提供逐条 Gold。本审计未修改旧指标、Gold、预测、progress 或已有路线报告，未调用付费 API。

## 结论与优先级

| 优先级 | 问题与证据 | 影响与建议 |
|---|---|---|
| P1 | QA 记录口径将已配对但不完整记录只算 FP：model TP19、FP298、FN1、recall95%；其实 19/117=16.24% Gold 完整覆盖、19/317=5.99% 完整预测精确率 | 95% 不得表述为标的完整召回。并列显示 `exact_gold_coverage` 和 `exact_precision`；历史数字不回写 |
| P1 | QA 字段 TN995 中 850 来自预测独有行的空字段；字段槽位总数随预测条数变化 | QA accuracy 不是固定样本集准确率。输出 TN 来源及固定 Gold 槽位，不能把可观察空槽说成全世界真阴性 |
| P1 | Gold 有表头污染、不同服务被重复复制为同名、服务条目遗漏的具体回源证据，见下文 | 先做证据驱动的局部人工裁决与版本化，不能为抬高分数改 Gold；旧报告保留 |
| P1 | holdout 实际 provenance 为 9A+9B+6pilotB，无 pilotA；系统候选可能被标注员看到 | 不能声称双人一致性裁决已完成或盲标。用本集修复后它成为开发/回归集，后续建立新 holdout |
| P2 | 正文泛项目名与附件具体名存在冲突；model 还输出历史业绩材料、重复变体和额外包 | 额外行须按来源/范围分类。不要统一叫幻觉，也不要仅改匹配宽松度消除错误 |
| P2 | 当前推荐规则把字段和记录 weighted 的较小值作为主排序；QA 记录分数受只计整条未配对 FN 的 recall 支配 | 可描述 model 在当前团队规则下优于 hybrid；不足以声明生产达标，也不能外推官方排名。官方口径未定义时优先给多指标证据，避免单一自动决策 |

## 官方明示与团队约定的边界

审查来源：`D:/ICT/topic5_extracted.txt`、`D:/ICT/qa_rows.txt` E8、仓库 `EVALUATION.md` / `EVALUATION_QA_POLICY.md`、评测和推荐器代码。另有用户提供的 10-07 答疑聊天文字，由主 Agent 转述，不能声称本审计独立核验了官方评分器。

官方原文明确七字段及 HTML/附件覆盖、加权公式 0.4 accuracy + 0.3 precision + 0.3 recall；评分专用约 100 条原始数据不对外发放。E8 答复明确“提取到最细颗粒度”，无更细材料时允许聚合信息，均无标的时可为空；可以逻辑推定未公示字段，但“逻辑推定数据不作为精度指标的评估数据”。因此已披露/推定必须在正式评测输入中有明确边界。

用户提供的答疑原话为：“三个精度参数的计算方式要考虑TP,FP,TN和FN，本命题精度计算为二分类精度指标迁移的计算方式，建议按以下方式套用计算:有正确字段的记为P样本，无字段/缺失的记为N样本。TP为有字段，且标注正确的数量。FP为有字段，但标注错误的数量。N样本同理。”它没有形式化多值实体配对、负例全集、错值是否同时产生遗漏、整条记录或重复项的评分方式。

以下全部是团队本地扩展：稳定 notice/package ID 边界、七字段非空正确数的 Hungarian 最大权重配对、字符串 NFKC/casefold/去空白和数值容差、双空计 TN、非空错值仅 FP、QA 记录错值仅 FP、主体/记录 TN 恒0、模型路线推荐规则。不能以 `official_qa` 这个名称把这些扩展包装成官方完整规范。题目要求采用符合要求的模型，所以推荐器只比较 model/hybrid 并非天然缺陷；rules 是诊断基线。

`evaluation-*.json` 仍为旧 `local_proxy`，QA 数值在 `gap-route-report.json`。旧 local_proxy 的错值同时 FP/FN、双空不计 TN，与 QA 不可混用；报告必须显式 profile、数据和代码版本。

## 固定字段样本与开放集合

固定 Gold 样本可以定义为每个已审核 Gold item 的七个槽位：117×7=819，其中634个有值、185个空值。这一定义排除预测独有行，分母固定，但当前槽位的预测归属仍依赖一对一配对。更强的固定设计应事先制定来源中的记录锚点、包号和空值语义，并独立穷举所有需要判断的记录/字段，不从输出扩增负例。

开放记录、供应商名单或任意字符串空间不存在可枚举“全部非记录”。多重集 exact_precision = 完整匹配条数 / 全部预测条数，exact_gold_coverage = 完整匹配条数 / Gold条数，是可解释的开放集合诊断。后者覆盖的是本地标签，Gold漏标和粒度偏差会改变它的意义。完整记录 TN 不应凭空生成；标的为空不意味着存在一个全空 item，现有 schema 拒绝全空 item 是合理的。

可枚举负例只存在于提前固定的范围，例如已审核记录的 brand 是否披露，或明确列出的固定候选集合中某主体是否具有某角色。未配对预测行的空字段不是这种事先独立定义的负例。QA 现实现将这些空字段计 TN 是已披露的团队约定，不能外推为正确识别“无字段”的总体能力。

空值应区分未披露、不可恢复、非适用和标注未完成；当前 JSON null 无法保存这几种区别。0 是有效数值。七字段 exact 包括空值位置一致，所以模型填入真实更细字段、Gold保留null也会失败；推定值要排除评测或使用独立来源字段，不能为 exact 补造原文未披露值。

## 当前三路线的只读诊断

以下表格由新 CLI 读取现有三份 predictions、复用当前评测器后得出；没有把新诊断的名字复用为旧 recall。所有比例仅本地标签上的工程诊断。

| 路线 | 预测条目 | 当前配对 exact | 最大 exact 配对 | exact_precision | exact_gold_coverage | 配对但非exact | Gold未配对 | 预测未配对 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| rules | 119 | 23 | 23 | 19.33% | 19.66% | 51 | 43 | 45 |
| hybrid | 311 | 19 | 19 | 6.11% | 16.24% | 92 | 6 | 200 |
| model | 317 | 19 | 19 | 5.99% | 16.24% | 97 | 1 | 201 |

当前字段权重配对优化“七字段非空正确数之和”，并不承诺最大化完整条目数。新工具另以七字段完全匹配构建二分图、独立最大化 exact 条数。此轮两者差额均0，不应把它说成此轮低 exact 的原因；合成同权重例可产生当前exact0/最大exact1，说明一般风险实际可发生。两种结果独立命名，不替换历史 TP。数值沿用当前 tolerance 和整数匹配设置；API 允许传显式 config，CLI 使用默认 profile 配置并将配置输出。

| 路线 | 当前可观察槽位 | 固定Gold槽位 | TN配对行双空 | TN预测独有空字段 | TN Gold独有空字段 | TN合计 |
|---|---:|---:|---:|---:|---:|---:|
| rules | 1134 | 819 | 11 | 105 | 160 | 276 |
| hybrid | 2219 | 819 | 114 | 813 | 26 | 953 |
| model | 2226 | 819 | 143 | 850 | 2 | 995 |

model TN 中85.43%是预测独有行的空字段，不能视为匹配 Gold 记录的正确空值。model QA记录weighted32.69%中28.5个百分点仅来自0.3×95% recall；其19/117完整覆盖为16.24%。model/hybrid 的19个 exact集中在4公告，14个来自 `3db0dee4037cdea5a8bd`，一个公告贡献73.68% exact，24篇聚合micro无法证明跨公告稳定性。

| 路线 | QA字段weighted | QA记录weighted | 实际预测包数 | 缺Gold包数 | 额外包数 | normalized exact重复预测额外条 |
|---|---:|---:|---:|---:|---:|---:|
| rules | 63.28% | 21.93% | 37 | 18 | 10 | 2 |
| hybrid | 64.67% | 27.03% | 62 | 1 | 18 | 10 |
| model | 68.42% | 32.69% | 62 | 0 | 17 | 8 |

Gold亦有3个 exact重复额外条，均来自下文四条复制服务记录。不同 notice_id/package_id 不允许跨边界抵消；“同值但放错包”和“default包重复保留聚合候选”是结构问题，单靠字段正确数无法修复。

model 错误字段分布（wrong/missing/extra槽位合计）：product_name212、category130、quantity93、model92、total_price86、unit_price45、brand40。这里包含未配对预测的 extra，不是源事实错误的已裁决数量。

model 公告错误聚集：`3db0dee4037cdea5a8bd` Gold14/预测56，额外字段165；`0fb4d82774c8ef7a4aef` 19/63，wrong5+missing19+extra140；`8895a165d160488536af` 5/25，extra67；`894c43b53632c1358ef6` 4/24，missing1+extra42；`d50bff2bacee040e0af4` 6/18，wrong3+missing12+extra19。应优先回源审查这类聚集，诊断并不自动判定每条来源。

## 少量来源抽查

抽查直接读相应原 HTML，附件证据使用同批 annotation bundle 已保存解析文本与成员名称，没有新模型调用。附件证据尚未逐个原文件视觉复核，所以下述细粒度与金额组合建议进入人工裁决；不据此自动改标。

1. `ac9f8113852413b282e7` 原 HTML“四、主要标的信息”明确四服务：业务应用、业务底座、安全服务及其他服务、应用系统集成对接。Gold四条均为“肇庆高新区‘大旺即办’民意服务平台业务应用”，category相同、其余null，构成3个exact重复额外条；model分别输出四个名称。严格多重集评分因此只有一条配对成功，至少三条属于不同Gold名称/漏标的审查问题，不能都算无来源幻觉。
2. `8d387ed6ecaee8c35921` 原 HTML 标的为营养馕，Gold包1两个 product_name 含“产品/服务\t品目\t品牌\t规格型号\t数量\t单价（元）\t总价（元）”表头，winner name 含“中标方\t中标金额”；包2同类标的却为正常“营养馕”。Gold model还采用“150g/个；符合食品安全地方标准等原文要求”的压缩表述，原文为长要求段。归一化只去空白不能移除表头或识别语义等价，完整记录和主体指标会因标签污染/摘要风格失败。
3. `8895a165d160488536af` 原 HTML五行采购标的均为泛称“山东省泰安市肥城市公安局高点监控设备采购”，规格/价格区分条目，Gold据此五条泛名。bundle附件“本国产品相关附件”文本明确“高点全景监控设备DH-PSDW812ZMSJ-A270”“普通球机监控设备DH-SDT-5X2433-ZM-TG”。model既保留泛名又提取具体名及不完整变体。具体名有来源，泛名与具体名应是同业务条目的证据合并/命名粒度裁决，不能全部作为不同采购记录；也不能把所有附件具体名笼统叫幻觉。Gold与最细颗粒度答疑的一致性需要复核。
4. 同一 `8895...` model default包中“2025年度泰安市城市管理综合服务中心智慧路灯…”、“智慧停车场车道闸设备及监控系统…”等可定位于附件 `供应商同类项目实施情况一览表.pdf`。这是真实历史材料进入当前采购范围，属于范围污染，应过滤/隔离；它与没有源依据的模型编造不同。
5. `3db0dee4037cdea5a8bd` 原 HTML服务类列出“数据基础引擎平台安装调试服务”“数据对接服务”“集成实施服务”，Gold只14条货物，未包括这三服务。model已输出其名称。因此额外行中存在可明确定位的Gold遗漏候选。其金额等字段尚未由本审计独立确认，不把完整额外记录预判为正确。
6. `d50bff2bacee040e0af4` 原 HTML使用“2026年度智慧课程建设服务采购项目1/2”泛名，服务要求描述具体建设。Gold保留泛名，同时使用附件的45000/19、40000/3、11800/1等报价。bundle报价附件可见“工学一体化课堂实录”，model包2输出“在线精品课程资源建设”24000/8、“智慧课程建设”22975/8、“课程建设培训”13200/1。具体名来自更细报价/要求，泛名+附件数值的Gold政策应一致，需决定实体锚点及证据优先级；当前模型另混入业绩候选、重复泛名和A/B包，也是独立错误。
7. `4f051f12caeb9166dcbc` Gold三包 items 全空，但原 HTML主要标的信息有“包1/包2/包3”及每包“完成…200万页…整理及数字化加工工作”。model输出包名和服务项目名称。是否泛包名应构成标的、数量如何表达均需明确标注范围；它是来源存在而Gold范围未覆盖的审查候选，不能无证据定为模型幻觉。

建议逐条裁决时至少使用分类：无来源编造、历史业绩/模板范围污染、真实Gold遗漏、正文/附件粒度差异、重复/不完整变体、错误包号。类别不能仅靠alignment状态自动得出。记录来源成员/页/表格行、披露vs推定、关联业务item，保留review前后差异及裁决理由。

## 标注过程与后续留出方案

provenance明确收到9篇正式A、9篇正式B、6篇pilotB；未收到pilotA且未替代。六篇pilot仅单标，不存在可报告的A/B一致性统计或完整pilot裁决。bundle由系统候选构建，无法证明标注者没看候选，因此只能称reviewed本地标签，不能称无偏独立标注研究。`docs/EVALUATION.md` 描述的理想“双人共同pilot裁决”不能覆盖实际未发生的步骤。

provenance里的 `deepseek-v4-flash` 是旧运行身份；当前Qwen运行必须从本次 `progress.identity` / run identity解释（qwen3.8-max、attachments、thinking关闭、output16384），不要修改旧provenance冒充当前运行。

notice IDs与tuning不重叠仅证明样本ID边界，不证明没有近重复公告、附件共享、候选协助或调优泄漏。本次已回看holdout并据此修复/选择路线，后续再测它应标为开发回归/targeted，不能靠stage=final或reviewed布尔值恢复独立性。

无需现在重标全量。可先对上述少量明确疑点做来源裁决，产出版本化audit ledger与新Gold版本，旧Gold/旧分数保留；然后冻结代码、提示、合并范围规则和标注指南，从未参与调参的官方原始材料中按附件/无附件、扫描/文本、多包/单包、货物/服务、长/短、无标的分层抽取新holdout。排除现有tuning/holdout IDs和内容近重复，登记源sha256/抽样seed；标注界面只提供原文、空schema和指南，不预填预测。

新holdout至少做共同双标pilot并留存真实一致性和裁决；正式分工采用来源锚点，抽样双人复核全部类型，Gold reviewed之前审核空值、包号和完整性。冻结标签再运行模型，保留模型盲评时间和版本。报告同时给字段/记录、micro/每公告分布、包集合、错误分类和计时/成本，质量阈值应在新评测前定义。该方案是工程建议，不是官方新增要求。

## 新诊断接口与实测

新增仅三文件：`backend/app/evaluation_diagnostics.py`、`backend/tests/test_evaluation_diagnostics.py`、本报告。无已有逻辑改动，无commit/push。

Python：`diagnose(gold, predictions, profile="official_qa", config=None, allow_draft=False)` 返回JSON可序列化dict；`diagnose_files(gold_path, predictions_path, ...)` 只读输入。严格missing分类才允许诊断。输出 counts、exact_record、两种alignment目标、package_sets、TN来源、固定Gold字段槽位、重复多重集、错误字段分布、每包/每公告聚集和解释限制。不输出API配置、密钥或原文大段。

从backend运行，默认只stdout：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation_diagnostics --gold .data\annotation-tasks\official-holdout-20260929\gold.reviewed.json --run-dir .data\annotation-tasks\official-holdout-20260929\evaluation-routes-qwen-20261009
```

也支持 `--predictions <单份json>`、`--profile local_proxy`、草稿显式 `--allow-draft`。`--output <新json文件>` 可保存独立诊断；所有已存在输出路径一律拒绝，并采用create-exclusive写入，避免覆盖Gold/evaluation/progress。它不会批量调用抽取，也不改已有评分文件。

实测：三路线CLI退出0，得到上表数据。读取前后 SHA256 校验Gold及该run目录所有现有JSON/MD共15文件，全部未变。最大完整配对和当前配对的真实差额均0。定向合成验证涵盖QA部分记录与固定Gold覆盖的区别、TN三来源、重复多重集、包边界、同权重配对目标差异、金额容差、空分母、非strict拒绝、CLI目录模式/输出保护/错误输入。

```text
python -m pytest tests/test_evaluation_diagnostics.py -q
8 passed in 0.08s
python -m ruff check app/evaluation_diagnostics.py tests/test_evaluation_diagnostics.py
All checks passed!
python -m ruff format --check app/evaluation_diagnostics.py tests/test_evaluation_diagnostics.py
2 files already formatted
```

当前结果支持“在当前团队QA加权规则下model优于hybrid，且Gold包覆盖较完整”的有限排序。它同时显示二者完整Gold覆盖均16.24%、model完整precision略低于hybrid，以及标注与范围问题；这些证据不足以确认model/hybrid任何一条达到生产质量或官方评分要求。
