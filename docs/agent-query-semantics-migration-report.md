# 任务二查询口径迁移报告

## 变更

- 场景二 `buyer_bidders`、场景三 `supplier_co_bidders` 及关系线索默认纳入
  `winner`、`nonwinner`、`unknown`；`unknown` 仍单独保留，不显示为未中标。
- `include_winners=false` 仅筛选明确 `nonwinner`，不包含 `unknown`。
- 场景一 `buyer_awardees` 新增 `product_suppliers`，按任务一 `brand` 原值聚合，返回项目数、采购包数、明确 `procurement_items.total_price` 汇总和逐条来源证据；这是题目品牌口径，不推定法律实体，不复制 award 交易额或推导缺失总价。
- SQLite、Neo4j、FastAPI 和受控查询默认值已同步；场景五继续同时返回项目汇总和采购包明细。
- 资格审查失败、租赁分类和跨包协同的未形式化边界保留在 `QUERY_SEMANTICS.md`，没有推断填值。

## 验证

- 查询、关系线索和 API 定向测试：通过。
- 后端全量：342 passed，1 skipped，2 warnings。
- 后端 Ruff：通过。
- Neo4j 集成测试因本机未配置 `BIDINTEL_TEST_NEO4J_URI/PASSWORD` 跳过；离线 Cypher/投影测试通过。

以上测试数字为迁移 Agent 完成时的历史快照，本轮统一验收结果由主 Agent 另行记录。

历史 `neo4j-release-db05fe5aca8b4215a0b5fb6a94a1d774.json` 的 SQLite/Neo4j 双后端 `6471/6471` 属于旧的 `nonwinner` 默认口径，不能继续作为新口径结果；旧目录未被覆盖。

本轮已生成独立新口径校验：`backend/.data/query-validation/qa-semantics-20261007-v2.json`，从 24 条 reviewed Gold 独立计算期望结果并对 `SQLite app.analytics` 比较，五场景合计 **6471/6471** 通过。报告明确 `Neo4j was not exercised`，因此它是新 SQLite oracle 证据，不是新 Neo4j Gold 等价证据，更不是官方成绩。

## 限制

- 官方不提供 Gold；所有查询校验仍是团队本地 reviewed Gold 的口径验证。
- 产品供应商名称来自品牌字段，品牌为空或未披露时没有产品供应商记录。
- 跨包共同参与的项目级与包级展示同时保留，但官方未形式化两者的最终统计规则。
