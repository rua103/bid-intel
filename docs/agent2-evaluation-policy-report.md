# 金额精度与评测政策报告

## 范围

本报告覆盖 `backend/app/evaluation.py`、`route_evaluation_report.py` 与前端金额显示。金额底层继续使用 `Decimal`，原始金额不量化；统一两位小数只发生在显示和报告展示层。金额字段按语义区分：预算/公告总额属于展示金额，中标金额用于中标实体评测，单价与总价属于标的物字段，不能互相替代。

## 可配置政策

`backend/app/evaluation_policy.py` 提供独立 `EvaluationPolicy`：

- `amount_storage_precision`：精度声明字段，当前不据此量化或截断原始值，也不改变数据库存储格式；
- `display_decimal_places`：显示位数，默认 2；
- `unit_conversion`：元、万元、亿元换算；
- `amount_tolerance`：Decimal 金额容差，默认 0.01 元；
- `integer_part_matching`：精确匹配或仅整数部分匹配；
- `missing_value_policy`：严格缺失、仅双方缺失、缺失未知。

`local_proxy` 是唯一内置 profile，显式复现当前本地最终评测默认行为。没有把任何官方规则写成默认值；官方 profile 必须在主办方规则确认后显式新增和选择。

## 规则来源边界

- 团队本地代理口径：当前 evaluator 的 Decimal 解析、0.01 元金额容差、缺失值计数和本地 route report。
- 来自答疑的规则：现有标注说明中“见附件但未取得附件时留空”等处理，仅作为团队标注约定记录。
- 尚待主办方确认：整数部分匹配、预算金额是否参与官方评分、官方金额舍入/单位和缺失值评分方式。

## 验证

独立测试覆盖元/万元/亿元、0.004/0.005 元显示边界、整数相同而小数不同、缺失值策略，并保留现有多中标人、多包、报告路由测试。报告路由重复诊断已改用 Decimal 指纹，避免 float 精度碰撞。

## 风险

前端格式化器按十进制字符串处理并用 `BigInt` 进位，已有 Decimal 字符串响应可保留大额精度；若上游已把金额转换为 JavaScript `Number`，精度可能在进入格式化器前丢失，格式化器无法恢复。当前不改变后台留出集评测目录、Gold、抽取 prompt 或默认 route 行为。
