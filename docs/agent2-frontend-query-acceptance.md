# 前端任务二查询展示验收

本次只调整前端展示层，没有修改后端查询口径、Gold、holdout 或评测模块。

## 已覆盖

- 场景一保留中标供应商作为独立主体，并兼容后端新的 `product_suppliers`、`product_supplier_summary`、`productSuppliers` 字段；旧 API 返回的 `product_brands` 仍会显示为“产品供应商/品牌”列表。
- 产品供应商有项目数、采购包数、交易金额或来源证据时，页面会逐项显示；旧字段没有这些统计时只显示名称，不补造数字。
- 场景二和场景三显示参与统计口径。后端返回 `participation_outcomes` 时优先展示具体结果；`unknown` 统一显示为“结果未披露”，不会显示成“未中标”。
- 场景五保留项目汇总、采购包明细、参与主体和结果，并把 `winner`、`nonwinner`、`unknown` 显示为“中标”“未中标”“结果未披露”。
- 保留当前会话、数据集切换、SQLite/Neo4j 后端提示和公告证据跳转。

## 兼容性边界

- 前端兼容旧 `product_brands`，但旧 API 本身不提供产品供应商的项目/包/金额统计，因此页面不会推测这些数字。
- 前端不引入日期、金额或其他后端尚未支持的过滤语义。
- 最终参与口径仍以当前后端返回的 `participation_outcomes`、`include_winners` 和语义说明为准。

## 验证

- `npm test`：27 passed
- `npm run build`：通过；保留 Vite 关于主 bundle 较大的提示

