# GAP 2.12：场景一、四合作次数统计粒度修复

日期：2026-10-08。工作目录：`D:\ICT\bid-intel`。开始与结束 HEAD 均为 `main / 2ff3503ac1143fc6a8b7809cca7bbe4e5c9657c1`；未 commit、push、reset 或撤销。开始时存在的未跟踪文件 `docs/benchmarks/attachment-audit-ledger.json` 保留，未修改。没有调用真实模型、重跑抽取、修改 Gold 或读取 holdout 内容。

## 依据与实现选择

先阅读了 `GAP_ANALYSIS.md` 2.12、`QUERY_SEMANTICS.md` 和 `QA_COMPLIANCE_MATRIX.md`。核对 `D:\ICT\topic5_extracted.txt`：第 3 页场景一要求“合作次数、交易总金额”，场景四要求“与上述主体均存在合作关系的采购单位、合作频次及合作金额”；第 13 页术语表原文为“合作频次：特定主体之间在历史项目中发生合作或共同参与竞标的次数”。据此把合作频次按项目去重。

同时核对本地赛题 QA 工作簿及 `D:\ICT\qa_rows.txt`：表格第 6 行（答疑编号 3）明确产品供应商需单独查询、采用任务一品牌字段，并统计频次和金额；第 9 行补充前端至少分别展示中标供应商和产品供应商的金额/合作次数。上述 QA 没有确认场景四必须在共同项目或同包中标。以下字段与交集范围是依据原文的团队实现，不是官方已确认的评分规则，也不能从本地 Gold 验收推导官方成绩。

## 字段兼容性与排序

| 场景/字段 | 当前含义 | 兼容性 |
|---|---|---|
| 场景一中标供应商 `award_project_count` | 该供应商在指定采购单位下中标的去重项目数 | 新增字段，合作频次使用此值 |
| 场景四每供应商 `award_project_count` | 该供应商在本行共同采购单位下中标的去重项目数 | 新增字段，不是所选供应商的共同项目数 |
| `award_package_count` | 对应供应商的中标采购包去重数 | 原字段、原含义保留 |
| 场景四 `project_count_scope` | 固定为 `per_supplier_at_common_buyer` | 新增范围说明 |
| 场景一品牌 `project_count` / `package_count` | 出现品牌标的的去重项目数 / 包数 | 名称、含义保留 |
| `award_amount_total` / `award_amount_total_unique_awards` | 对应范围内唯一中标记录的披露金额合计 | 原字段保留，SQLite 显式使用 award ID；Neo4j 收集唯一 award 关系后 Decimal 汇总 |
| 品牌 `amount_total` | 品牌标的中已披露 `total_price` 合计 | 不复制中标金额、不映射法律实体、不推定缺失总价 |

场景一中标方和场景四每采购单位的供应商列表按项目数降序、名称、组织 ID 排序；同项目数不再由包数主导。品牌按项目数降序、品牌名称排序。采购单位列表仍按 buyer ID，选中主体回显仍按组织 ID；不另造共同项目频次排名。

场景四仍按所选中标供应商的合作采购单位取交集；不同供应商可以在该采购单位下的不同项目中标。包数、品牌证据与其他场景原有明细保留。`analytics_backend.py` 已核对：其 SQLite/Neo4j 分派直接透传 payload，不过滤新字段，因此无需新增接线。`main.py` 无修改。受控查询只补充结果说明，意图模型提示词未改，现有 JSON 结果展示直接保留新增字段。

## 修改前后样例

以下均来自独立合成样例，金额单位为元。

| 情形 | 修复前合作次数展示 | 修复后项目数 / 包数 | 金额前 → 后 |
|---|---|---|---|
| Zeta 在 Buyer A 的 P1 中标两个包，各 `0.10` | 2 包作为频次 | 1 / 2 | `0.20` → `0.20`，两笔等额 award 都计入 |
| Alpha 在 Buyer A 的 P1、P2 各中标一包 | 2 包 | 2 / 2 | `9007199254740993.11` → 同值，Decimal 精度保留 |
| P1 第一包有 Zeta、Beta 两个中标方，并有两个标的和四个参与主体 | 包频次 | 每供应商 1 项目 / 1 包（仅该包范围） | 该包两笔 award 共 `0.20`，不被标的/主体关联放大 |
| Buyer B 的 P3 仅 Alpha 中标、P4 仅 Zeta 中标 | 共同采购单位，供应商各 1 包 | 仍为共同采购单位，各 1 项目 / 1 包；共同项目为 0 但不用于本场景频次 | 合计 `3.00` → `3.00` |
| Brand Z 在 P1 两包中出现，披露两行 `0.03`，另一行缺失 | 已有项目数 1、包数 2 | 保持 1 / 2 | 标的总价 `0.06` → `0.06`，不替换成 award 金额 |
| Gamma / Unknown 中标金额均未披露 | `null` | 项目/包正常计数 | `null` → `null`，不补零 |

稳定排序样例：Buyer A 的 Beta、Gamma、Zeta 均为 1 个项目，Zeta 有 2 包；修复后顺序为 Beta、Gamma、Zeta，Alpha 的 2 项目排在三者前。

## 独立验证与实际执行结果

新增 `backend/tests/test_cooperation_project_frequency.py`。合成 `PROJECTS` 是独立输入，fixture 保存原始中标/标的记录及 ID；oracle 直接从这些记录构造项目集合、包集合、唯一 award 字典与 Decimal 金额，未调用生产聚合函数生成期望答案。覆盖一项目两包、跨项目、等额 award、多中标方/标的/参与主体、两/三供应商采购单位交集、无共同采购单位、空采购单位、不存在 ID、全未披露金额、稳定排序、API/受控查询和重叠 SQLite ID 的双数据集隔离。真实 Neo4j 与 SQLite 分别对同一独立答案比较，随后比较返回值。

新增前端测试从实际 `App.vue` 模板编译并渲染场景一/四结果区，检查项目数/包数分列、各供应商频次、交集边界和空金额不显示 `0.00`。

| 工作目录 | 实际命令 | 最终结果 |
|---|---|---|
| `backend` | `.\.venv\Scripts\python.exe -m pytest -q` | **364 passed、0 skipped、2 warnings，89.86 秒** |
| `backend` | `.\.venv\Scripts\python.exe -m ruff check app tests scripts` | **All checks passed** |
| `frontend` | `npm test` | **33 passed、0 failed、0 skipped** |
| `frontend` | `npm run build` | **成功**，589 modules；保留已有大于 500 kB chunk 提示 |
| `backend` | `.\.venv\Scripts\python.exe -m pytest -q tests/test_cooperation_project_frequency.py` | **3 passed**，包括真实 Neo4j oracle |
| 仓库 | `git diff --check` | **通过**；Git 提示 LF/CRLF 转换，不是 diff 错误 |

Neo4j 使用现有 Docker 容器 `neo4j_runtime-neo4j-1`、`neo4j:5.26.14-community`。最初引擎未启动，启动本机 Docker Desktop 后容器恢复 healthy。密码从本机容器配置读取到测试进程环境，没有输出密钥；测试使用独立 UUID 数据集并清理测试作用域，未清空其他数据集。完整 pytest 已包含已有真实 Neo4j 五场景 API 集成测试。

最初合成测试中数据集 ID 用了不合法的文本 ID、空采购单位未关联项目而未进入图镜像，前端测试缺一个模板 helper；只修正测试夹具后重新运行通过。完整通过数字来自修正后的实际执行，不隐去未运行项，也不把早期失败称为通过。后端两个 warning 分别是 Starlette/httpx 弃用提示和既有重复 ZIP 文件名测试提示。

24 条查询验证仅使用 reviewed tuning Gold `backend/.data/annotation-tasks/official-20260926-LHH-YHR/gold.merged.json`，SHA-256 `cfa3fa242bf6f1eec0bd474731e1d48a126df9259e9148c837c8f8458bb5759f`。执行命令：

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_neo4j_gold `
  --gold .data/annotation-tasks/official-20260926-LHH-YHR/gold.merged.json `
  --sqlite .data/query-validation/gap-2.12-20261008-185019/tuning.sqlite `
  --report .data/query-validation/gap-2.12-20261008-185019/tuning-neo4j.json
```

连接参数由 `BIDINTEL_TEST_NEO4J_URI/USER/PASSWORD` 环境变量提供，密码不出现在命令行或报告中。新目录保留旧报告；导入 24 项目、442 节点、464 关系。SQLite 与真实 Neo4j 独立 oracle 各 **6471 checked、0 failed**，两后端返回 **6471 checked、0 mismatched**。场景一 24 个、场景四 673 个 case 均通过；另外三场景分别 48、74、5652 个。Gold oracle 独立计算项目数/包数/金额、品牌聚合和排序。双后端验证 CLI 现同时检查两份 oracle 的失败数，避免两后端同错但比较无差异时返回成功。

## 修改文件

- `backend/app/analytics.py`
- `backend/app/graph.py`
- `backend/app/controlled_query.py`
- `backend/scripts/validate_gold_queries.py`
- `backend/scripts/validate_neo4j_gold.py`
- `backend/tests/test_cooperation_project_frequency.py`（新增）
- `frontend/src/App.vue`（仅场景一/四展示）
- `frontend/tests/cooperation-display.test.js`（新增）
- `docs/QUERY_SEMANTICS.md`
- `docs/QA_COMPLIANCE_MATRIX.md`（仅场景一/四行）
- `docs/GAP_ANALYSIS.md`（仅 2.12 和当前任务表第 8 项）
- `docs/CHANGELOG.md`（新增条目，保留历史）
- `docs/agent-gap-2.12-report.md`（本报告）

## 留给主 Agent 的边界

1. 当前“项目”是持久化 `projects.id`，本轮不改变更正公告、重复公告的跨公告项目识别/合并。真实业务同一项目若存为多个项目记录，需要另任务确认，不能在此次修复中改抽取或包号口径。
2. 场景四返回各供应商项目数，不返回或暗示共同项目数；金额为所选供应商在共同采购单位下唯一 award 披露金额之和。官方现场若有更具体要求，需版本化调整并保留这次证据。
3. 部分记录缺金额时只汇总已披露金额，全部缺失返回 `null`；总额不能宣称涵盖未披露交易。品牌仍不是法律实体。
4. 所有验证是团队本地工程证据；尚未经过官方隐藏基准或统一平台验收。请主 Agent 最终审查、合并与提交。
