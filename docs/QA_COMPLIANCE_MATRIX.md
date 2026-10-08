# 赛题五 QA 合规矩阵

更新日期：2026-10-08（当前验收：后端 364 passed、含真实 Neo4j；前端 33 passed 与构建通过；2 warnings）

本文根据赛题原文摘录 [`D:\ICT\topic5_extracted.txt`](D:\ICT\topic5_extracted.txt)、赛题答疑表、当前仓库代码和已有测试整理。它是交付前的证据矩阵，不是主办方评分结论。

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
| 任务二场景一 | 指定采购单位，输出合作中标供应商、产品供应商、合作次数和交易总金额；答疑规定产品供应商采用任务一品牌字段 | 新增中标方 `award_project_count`，保留 `award_package_count`；品牌保留项目/包数和明确标的总价；GAP 2.12 合成 oracle、真实 Neo4j/tuning Gold 对照见 `agent-gap-2.12-report.md` | 代码和本地统一回归已通过 | 品牌不是法律实体；标的总价不等于中标组织的 award 交易额，不把未知金额补成成交额；本地项目计频实现不等于官方评分确认 | 保留中标供应商与品牌两组统计、金额来源及未披露值；官方平台另行验收 |
| 任务二场景二 | 指定采购单位，输出 TOP5 参与投标主体及高频协同组合；答疑补充“中与不中都算” | SQLite、Neo4j 查询模板、API 和前端已采用 winner/nonwinner/unknown 默认；新 SQLite oracle 已通过 | 代码和本地统一回归已通过 | `unknown` 表示结果未披露，不能当落标；同项目跨包协同边界未获正式裁决 | 冻结当前口径并披露边界；`include_winners=false` 仅保留明确 `nonwinner` |
| 任务二场景三 | 指定中标供应商，输出共同竞标主体及全部参与主体、频次和金额 | `analytics.py`、`graph.py`、`analytics_backend.py` 已同步默认口径；`qa-semantics-20261007-v2.json` 为新 SQLite oracle | 代码和本地统一回归已通过 | 历史双后端 6471/6471 不证明迁移后 Neo4j Gold 等价；新 SQLite 6471/6471 不代表官方成绩 | 以本轮真实集成验收记录说明 Neo4j 范围，不能复用历史数字冒充新结果 |
| 任务二场景四 | 多个中标供应商的共同采购单位、合作频次和合作金额；术语表描述历史项目中的次数 | SQLite/真实 Neo4j、API/受控查询、前端和独立 oracle 同步：每供应商 `award_project_count`、保留包数及唯一中标金额，返回 `project_count_scope` | 代码和本地回归已通过 | 采购单位交集不等于共同项目/同包中标；团队解释不是官方已确认的评分规则 | 设计文档保留每供应商项目计频、包数和唯一 award 金额边界；官方平台另行验收 |
| 任务二场景五 | 多个中标供应商共同参与竞标项目、竞标结果、项目数量和项目总金额 | `analytics.py`、Neo4j Cypher、场景五金额回归测试、`docs/QUERY_SEMANTICS.md` | 代码已实现但未现场验收 | 不能把本地 Gold 查询一致性当官方场景达成率 | 用评分平台基准逐场景比对；报告项目级和包级明细及金额来源 |
| winner/nonwinner/unknown | 原文术语特指未中标方，答疑补充“中与不中都算”；中标供应商可多个 | 查询默认纳入全部已记录参与主体；`unknown` 显示“结果未披露”；`include_winners=false` 仅包含明确 `nonwinner` | 已实现，边界需披露 | 不能将 `unknown` 推定为落标；资格审查失败等边界未被答疑完整形式化 | 交付文档记录当前可复现口径；若官方另行明确，版本化修改并重算 |
| 资格性/符合性审查失败 | 题目定义审查概念，但没有明确失败主体在场景统计中的计入规则 | 解析器保留主体和 outcome/warning；`docs/QUERY_SEMANTICS.md` 已列为边界 | 需要人工确认 | 不能从“出现审查表”推断已提交投标，也不能把失败自动等同 `nonwinner` | 保留原文证据；对模糊情形使用 `unknown`，在最终报告单列数量 |
| 跨包协同投标 | 题目定义同一或多个项目共同参与；未明确跨包计数单位 | 查询保留项目和包结构；`docs/QUERY_SEMANTICS.md` 说明项目级汇总与包级明细 | 需要人工确认 | 不能声称官方一定按包或按项目计频 | 同时展示项目去重计数、包级证据和参与结果，等待评审口径确认 |
| 附件失效/解析失败 | 数据集含 zip 内 doc/docx/xlsx/pdf/图片；平台需自动解析 | `backend/app/archive_files.py`、附件审计脚本、`docs/DATA_QUALITY.md`、`docs/agent3-attachment-audit-report.md`；失败隔离测试 | 代码已实现但未现场验收 | 19 个程序失败、7 个不支持格式、87 个无效下载不能靠模型恢复；不能声称附件 100% 可读 | 随交付包提供聚合统计和 warning 分类；有源字节且可转换时继续修复 |
| 隐藏约 100 题 | 赛题明确评分基准不对外发放 | 题目原文 PAGE 11；仓库没有该数据集 | 需要官方资源 | 不能证明隐藏题附件完整、不能提前计算官方准确率 | 只在官方平台按要求运行，记录输入规模、耗时和输出摘要 |
| Qwen/DeepSeek 使用 | 要求 Qwen/DeepSeek 系列及赛事规定资源；决赛将由官方指定 API | `model_adapter.py`、`.env.example`、`MODEL_ENDPOINT_ACCEPTANCE.md` 已具备换端点配置与失败保护 | 等待决赛官方资源与现场验收 | 当前开发端点名不等于官方资源身份；mock 不代表决赛真实链路 | 官方 API 发放后登记随附模型说明并验收长样本；当前不再要求队员补开发端点证明 |
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
3. **投标参与方的默认解释采用答疑口径。** 评审答疑明确“中与不中都算”，因此当前默认查询纳入 `winner`、`nonwinner`、`unknown`，并将 `unknown` 显示为“结果未披露”。`include_winners=false` 只统计明确 `nonwinner`，不包含 `unknown`。新口径 `qa-semantics-20261007-v2.json` 已完成 SQLite 与独立 Gold oracle 的 `6471/6471` 校验，未运行 Neo4j；历史双后端同数量结果属于迁移前口径。若评审现场另行说明，应版本化修改并重算查询验证。
4. **Neo4j 是推荐技术，不是题面硬性唯一实现。** 当前五类分析 API 支持 Neo4j 并回退 SQLite；图可视化投影继续读取 SQLite，已在架构和审计文档中披露。
5. **决赛模型由官方指定。** 当前保留换端点能力与待验收状态，官方 API 发放后依据随附说明登记模型信息并完成长样本验收；不重复向队员索要开发端点的资源证明。现有端点探测和 mock 回归只证明工程兼容性。

## 交付前动作清单

- [ ] 以冻结代码重新完成 rules/hybrid/model 留出集评测，并固定 `official_qa` 指标版本。
- [x] 同步任务二 winner/nonwinner/unknown 口径，生成新 SQLite oracle 报告 `qa-semantics-20261007-v2.json`（6471/6471）；本轮统一验收仍需按实际后端记录范围。
- [ ] 用命题方统一平台验证真实模型、批处理速率和前端五场景响应时间。
- [ ] 决赛官方 API 发放后登记其模型说明并验证长样本；不在仓库记录 API Key。
- [ ] 完成系统设计文档、过程性文档的 Word/PDF 版本；按用户安排暂缓 PPT 和视频。
- [ ] 从干净检出整理源码及配套资料压缩包，排除 Gold、原始材料、数据库、`.env` 和 `.data` 运行产物。

## 证据使用规则

- 历史 `CHANGELOG.md` 数字只作历史快照；当前数字以对应提交的测试记录为准。
- 本地测试的 “passed” 证明代码行为，不证明官方隐藏数据的准确性。
- 没有证据的字段保持未知，不能为了填满矩阵推断版本、参数规模、附件语义对应关系或官方统计口径。
