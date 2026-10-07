# 品牌与产品供应商字段审计报告

## 范围

审计了 `backend/app/parsers.py`、`backend/app/schemas.py`、`backend/app/model_adapter.py` 以及 rules、hybrid、model 路线的字段合并逻辑。未调用真实模型，未修改 holdout Gold、留出集评测目录、抽取 prompt 的评测副本或包号规则。

## 结论与改动

- `FIELD_ALIASES["brand"]` 移除了“产品供应商”“品牌产品供应商”，保留“品牌”“品牌如有”“货物品牌”“品牌名称”“制造商品牌”等明确品牌列。
- `ItemCandidate` / `ModelItemPayload` 未新增 `product_supplier`，避免未完成存储迁移。供应商公司仍保留在 `source_evidence`，主体字段继续使用已有 `ParticipantCandidate.organization_name`。
- 规则表格遇到仅有产品供应商的行时，`brand` 保持 `None`，并使用 `table_header_mapping_supplier_column_unmapped` 标记来源语义，便于上层核验。
- model 提示词明确区分品牌与供应商公司；适配层对“产品供应商/供应商名称”标签后紧邻的公司值进行语义拒绝，将误填 `brand` 清空并追加 warning。

## 回归样例

新增 `backend/tests/test_brand_supplier_regression.py`，覆盖：

1. 品牌 H3C、供应商中移建设有限公司；
2. 只有产品供应商时品牌为空且保留来源证据；
3. 品牌“联想”与中标方“联想（北京）有限公司”同名但字段语义分离；
4. 多包、多主体；
5. model stub 返回供应商作为 brand 时被清空并告警。

## 验证

执行：

```text
pytest tests/test_brand_supplier_regression.py tests/test_parsers.py tests/test_model_adapter.py tests/test_extraction_modes.py
ruff check app tests
```

本报告只记录本 Agent 的验证结果；最终验收由主 Agent 统一进行。

## 剩余风险

未增加独立 `product_supplier` 持久化字段，因此供应商列值只能通过来源证据和既有主体抽取结果核验。模型输出中若公司名未紧邻明确供应商标签，适配层不会使用公司名启发式强行判定，以避免误伤合法品牌名。
