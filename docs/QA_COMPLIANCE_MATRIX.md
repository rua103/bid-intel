# 赛题五 QA 合规矩阵

更新日期：2026-10-07

本文根据赛题原文摘录 [`D:\ICT\topic5_extracted.txt`](D:\ICT\topic5_extracted.txt)、当前仓库代码和已有测试整理。它是交付前的证据矩阵，不是主办方评分结论。

## 使用边界

- 官方提供任务基准原始 HTML 与附件；评分基准约 100 条，题目明确“不对外发放，仅用于评分”。项目没有官方 Gold。
- 仓库中的 reviewed Gold、tuning 集和 holdout 都是团队人工核验的本地验证材料，只能用于工程回归和本地指标，不能称为官方真值或官方成绩。
- mock 模型测试只验证适配器、错误处理和 schema 保护，不代表真实 Qwen/DeepSeek 的抽取准确率。
- 赛题答疑没有覆盖的事项不在本文擅自补充为“官方口径”；此类事项标为“需要人工确认”或“需要官方资源”。
- “已实现”表示代码和仓库内测试已有证据；“代码已实现但未现场验收”表示缺少命题方环境、真实端点或第二设备验证。

## 合规矩阵

| 事项 | 赛题/答疑要求 | 当前证据 | 状态 | 不能声称的内容 | 下一步动作 |
|---|---|---|---|---|---|
| 任务一七字段 | 提取产品/服务名称、品目、品牌（产品供应商）、规格型号、单价、数量、总价；覆盖 HTML 和附件 | `backend/app/parsers.py`、`backend/app/schemas.py`、`docs/ANNOTATION_GUIDE.md`；附件解析和抽取测试 | 已实现 | 不能声称所有隐藏题字段都正确或所有附件都可恢复 | 用官方评分平台运行后记录真实覆盖率、提取率和错误样例 |
| 七字段细粒度 | 赛题允许标的物最细粒度；数量/金额不可从未披露内容臆造 | `docs/ANNOTATION_GUIDE.md`、`docs/QUERY_SEMANTICS.md`；金额政策在 `backend/app/evaluation_policy.py` | 已实现 | 不能把推定值当原文抽取值或计入官方准确率 | 如展示推定，单独标记 `inferred`、保留证据并排除正式字段评测 |
| 品牌与产品供应商 | 题目将品牌称为产品供应商字段；业务术语又指生产厂商或授权经销商 | `backend/app/parsers.py`、`backend/app/model_adapter.py`、`docs/agent1-brand-supplier-report.md`；品牌/供应商回归测试 | 已实现 | 不能把品牌自动映射成法律主体，也不能声称已有独立 `product_supplier` 持久化实体 | 评审演示时分别展示品牌字段和中标供应商；若需法律实体维度，增加来源明确的独立字段 |
| 逻辑推定单价/总价 | 原文要求七字段；赛题没有授权用数量×单价补造缺失金额 | `docs/ANNOTATION_GUIDE.md` 明确不推导；`evaluation_policy.py` 使用 Decimal 和缺失策略 | 已实现 | 不能把推定金额当官方答案；不能保证源文件已披露的小数可被恢复 | 设计文档说明“原文值/推定值”分层和排除规则 |
| TP/FP/TN/FN | 答疑将有字段记 P、无字段/缺失记 N，要求 TP/FP/TN/FN；赛题公式为准确率×0.4+精确率×0.3+召回率×0.3 | `docs/EVALUATION_QA_POLICY.md`、`backend/app/evaluation_policy.py`、`backend/app/evaluation.py`、`backend/tests/test_official_qa_metrics.py` | 已实现（团队本地解释） | `official_qa` 不是主办方正式评分器；多值字段、重复记录、实体负样本边界未被官方形式化 | final 报告同时披露 profile、对齐范围和限制；若官方补充规则，版本化后重算 |
| 任务二场景一 | 指定采购单位，输出合作中标供应商、产品供应商、合作次数和交易总金额 | `backend/app/analytics.py`、`backend/app/main.py`、`docs/QUERY_SEMANTICS.md`；五场景 API/查询测试 | 代码已实现但未现场验收 | 不能声称产品供应商金额与官方隐藏答案已对齐；产品供应商独立持久化仍有限制 | 用官方基准数据核对场景一；保留金额来源证据和未披露值 |
| 任务二场景二 | 指定采购单位，输出 TOP5 参与投标主体及高频协同组合 | `analytics.py`、Neo4j 查询模板、前端查询组件、GAP 6a 报告 | 代码已实现但口径需冻结 | 赛题术语表把“投标参与方”特指未中标方；答疑若另有解释不能覆盖原文，`unknown` 不能自动当落标 | 在提交材料中明确采用 `nonwinner` 默认、`unknown` 单独保留，并用官方结果验收 |
| 任务二场景三 | 指定中标供应商，输出共同竞标主体及全部参与主体、频次和金额 | `analytics.py`、`graph.py`、`analytics_backend.py`、`docs/QUERY_SEMANTICS.md`；SQLite/Neo4j 对照测试 | 代码已实现但未现场验收 | 本地 6471/6471 只证明两种实现一致，不证明官方答案正确 | 统一场景二/三的 winner/nonwinner/unknown 解释，重新跑查询 oracle |
| 任务二场景四 | 多个中标供应商的共同采购单位、合作频次和合作金额 | 五场景 API、SQLite/Neo4j 后端、受控查询 API | 代码已实现但未现场验收 | 频次粒度和金额口径没有被题面完全形式化 | 在设计文档明确按采购包/唯一中标记录的当前口径，并保留项目级辅助统计 |
| 任务二场景五 | 多个中标供应商共同参与竞标项目、竞标结果、项目数量和项目总金额 | `analytics.py`、Neo4j Cypher、场景五金额回归测试、`docs/QUERY_SEMANTICS.md` | 代码已实现但未现场验收 | 不能把本地 Gold 查询一致性当官方场景达成率 | 用评分平台基准逐场景比对；报告项目级和包级明细及金额来源 |
| winner/nonwinner/unknown | 题目术语明确参与投标方特指参与但未中标；中标供应商可多个 | `schemas.py`、`storage.py`、`docs/ANNOTATION_GUIDE.md`；查询默认过滤明确 `nonwinner`，`unknown` 保留 | 已实现，边界需人工确认 | 资格审查失败是否算参与、结果未披露是否计入默认 TOP5，题面没有完整裁决 | 交付文档记录当前可复现口径，并在答辩时向评审确认 |
| 资格性/符合性审查失败 | 题目定义审查概念，但没有明确失败主体在场景统计中的计入规则 | 解析器保留主体和 outcome/warning；`docs/QUERY_SEMANTICS.md` 已列为边界 | 需要人工确认 | 不能从“出现审查表”推断已提交投标，也不能把失败自动等同 `nonwinner` | 保留原文证据；对模糊情形使用 `unknown`，在最终报告单列数量 |
| 跨包协同投标 | 题目定义同一或多个项目共同参与；未明确跨包计数单位 | 查询保留项目和包结构；`docs/QUERY_SEMANTICS.md` 说明项目级汇总与包级明细 | 需要人工确认 | 不能声称官方一定按包或按项目计频 | 同时展示项目去重计数、包级证据和参与结果，等待评审口径确认 |
| 附件失效/解析失败 | 数据集含 zip 内 doc/docx/xlsx/pdf/图片；平台需自动解析 | `backend/app/archive_files.py`、附件审计脚本、`docs/DATA_QUALITY.md`、`docs/agent3-attachment-audit-report.md`；失败隔离测试 | 代码已实现但未现场验收 | 19 个程序失败、7 个不支持格式、87 个无效下载不能靠模型恢复；不能声称附件 100% 可读 | 随交付包提供聚合统计和 warning 分类；有源字节且可转换时继续修复 |
| 隐藏约 100 题 | 赛题明确评分基准不对外发放 | 题目原文 PAGE 11；仓库没有该数据集 | 需要官方资源 | 不能证明隐藏题附件完整、不能提前计算官方准确率 | 只在官方平台按要求运行，记录输入规模、耗时和输出摘要 |
| Qwen/DeepSeek 使用 | 要求 Qwen/DeepSeek 系列开源国产模型，基座+外挂调优或合规微调，并声明版本和参数规模 | `backend/app/model_adapter.py`、`.env.example`、`docs/MODEL_ENDPOINT_ACCEPTANCE.md` | 代码已实现但未现场验收 | `deepseek-v4-flash` 配置名不是版本/参数/赛事资源证明；mock 不代表真实模型 | 取得官方指定 API/模型说明，补版本、参数规模、基座/微调和资源来源登记 |
| LoRA/QLoRA/微调合规 | 题面允许轻量化微调并要求基座符合赛事要求、提供佐证；当前项目没有训练过程 | `docs/MODEL_ENDPOINT_ACCEPTANCE.md` 明确缺少训练/资源证明；无训练产物 | 需要官方资源 | 不能声称做过 LoRA/QLoRA 或拥有合规微调权重 | 决赛采用官方指定 API 时记录资源来源；若微调，保留基座、数据、参数和许可证证据 |
| 2 vCPU/4 GB/Ubuntu 24.04 | 统一平台环境由命题方赛前提供；题面没有要求参赛队提前证明本地同环境 | `docs/agent3-resource-limited-validation.md`、`docs/CONSOLIDATION_REVIEW_2026-10-07.md`；已有 Docker 近似验证 | 代码已实现但未现场验收 | Docker/Windows 近似运行不能称 Ubuntu 24.04 实机通过 | 收到平台后做一次部署、批处理、查询和内存/耗时验收 |
| Neo4j | 题面“推荐采用 Neo4j”；任务要求关系建模、五场景可视化查询统计展示 | `backend/neo4j_runtime/`、`analytics_backend.py`、`docs/NEO4J_GRAPH_PROJECTION_AUDIT.md`；Docker 集成测试 | 已实现 | `/api/v1/graph` 仍是 SQLite 投影；不能声称所有可视化都由 Neo4j 驱动 | 若评审明确要求图展示也走 Neo4j，再单独迁移投影；当前边界需在文档披露 |
| Vue/Python 栈 | 任务三推荐 Vue 前端、Python 后端 | `frontend/` Vue、`backend/` FastAPI/Python；前端测试和构建、后端测试 | 已实现 | 不能把构建通过当作现场平台验收 | 在源码包标注版本和启动步骤 |
| 查询效率 | 五场景各执行一次，响应为前端发起到结果完全渲染；阈值决赛 <1 秒得满分 | `backend/app/benchmark.py`、`docs/DEVELOPMENT.md`、既有性能快照 | 代码已实现但未现场验收 | Python/SQLite 或本机 HTTP 基准不等同命题方统一平台和浏览器渲染耗时 | 在统一平台记录五场景前端端到端平均响应时间 |
| 过程性文档 | 技术路线对比分析、数据质量分析，Word/PDF | `docs/INNOVATION_ROADMAP.md`、`docs/DATA_QUALITY.md`、`docs/EVALUATION.md` 等 Markdown 草稿 | 当前不支持（正式格式未完成） | Markdown 草稿不能直接声称满足 Word/PDF 交付 | 冻结指标后生成 Word/PDF，并写清至少两条技术路线和效果数据 |
| 系统设计文档 | Word/PDF；业务需求、技术选型、数据模型、功能设计、功能测试效果五模块 | `docs/ARCHITECTURE.md`、`docs/DEVELOPMENT.md`、`deliverables/` 中草稿材料 | 当前不支持（正式定稿未完成） | 内部草稿不能当命题方模板；未拿到官方模板 | 根据官方模板整理 Word/PDF，补最终测试和限制 |
| 汇报 PPT | 决赛提交答辩 PPT，需核心亮点预览页；模板由主办方提供 | 当前按用户要求暂缓；无官方模板 | 需要官方资源 | 不能把内部 Markdown 或截图当正式 PPT | 收到模板后制作；避免出现特殊识别信息 |
| 汇报视频/系统演示 | 汇报/系统演示视频，1080P 横屏，系统演示 ≤5 分钟 | 当前按用户要求暂缓；没有最终录制文件 | 当前不支持 | 不能声称已完成现场演示材料 | 冻结系统和文档后录制并检查分辨率、时长和全流程 |
| 源码及配套资料 | 工程文件、版本/环境、注释、官方数据集提取结果、任务二模型、说明文档 | 仓库源码、`README.md`、`docs/DEVELOPMENT.md`、`backend/neo4j_runtime/README.md` | 代码已实现但交付包未验收 | `.data`、密钥、Gold、原始材料不能直接打包；当前未形成最终压缩包 | 从干净检出按白名单打包，附版本、运行和数据说明，排除敏感/大体积运行产物 |

## 关键口径结论

1. **官方没有提供 Gold。** `gold.reviewed.json`、tuning 集、holdout 都是团队本地验证材料；它们可以支持回归和内部路线比较，不能替代主办方隐藏评分集。
2. **`official_qa` 是对答疑文本的可复现解释。** 它补齐了可观察字段槽位的 TN，但官方尚未形式化多值字段、重复记录、实体级全体负样本和错值的全部细节。
3. **投标参与方的默认解释以题目术语为准。** 题面写明“参与但未中标”，因此当前默认查询只统计明确 `nonwinner`；`winner` 和 `unknown` 保留在明细与可审计查询中。若评审现场另行说明，应版本化修改并重算查询验证。
4. **Neo4j 是推荐技术，不是题面硬性唯一实现。** 当前五类分析 API 支持 Neo4j 并回退 SQLite；图可视化投影继续读取 SQLite，已在架构和审计文档中披露。
5. **模型合规不能从模型名推断。** 需要正式版本、参数规模、基座/微调状态和赛事资源来源证明；现有端点探测和 mock 回归只证明工程兼容性。

## 交付前动作清单

- [ ] 以冻结代码重新完成 rules/hybrid/model 留出集评测，并固定 `official_qa` 指标版本。
- [ ] 重新核对任务二 winner/nonwinner/unknown 口径，生成新一版 GAP 6a 查询报告。
- [ ] 用命题方统一平台验证真实模型、批处理速率和前端五场景响应时间。
- [ ] 取得或登记赛事指定模型资源证明；不在仓库记录 API Key。
- [ ] 完成系统设计文档、过程性文档的 Word/PDF 版本；按用户安排暂缓 PPT 和视频。
- [ ] 从干净检出整理源码及配套资料压缩包，排除 Gold、原始材料、数据库、`.env` 和 `.data` 运行产物。

## 证据使用规则

- 历史 `CHANGELOG.md` 数字只作历史快照；当前数字以对应提交的测试记录为准。
- 本地测试的 “passed” 证明代码行为，不证明官方隐藏数据的准确性。
- 没有证据的字段保持未知，不能为了填满矩阵推断版本、参数规模、附件语义对应关系或官方统计口径。
