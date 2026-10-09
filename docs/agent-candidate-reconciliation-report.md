# Agent 2：跨文件候选融合与包号归属

日期：2026-10-09。仅修改独立模块、定向测试和本报告；没有改 Gold、原评测输出或模型配置，没有调用真实 API，没有 commit/push。本次 holdout 已参与诊断，以下不是新最终成绩，也不是泛化评测结论。

## 接口与接入

`backend/app/candidate_reconciliation.py` 暴露：

```python
reconcile_candidates(
    items, warnings=None, *, metadata=None,
    source_texts=None, source_metadata=None,
    source_links=(), package_aliases=(),
) -> list[ItemCandidate]
```

建议生产路径在逐文件用途过滤和本模式的抽取结束后，先调用旧 `_deduplicate(..., cross_source=False)` 保留同源 parser replay 处理，再调用此函数，作为唯一跨文件融合边界。旧宽松跨文件去重不能在此之后再运行，否则会绕过证据和歧义约束。逐文件 `source_metadata` 必须来自该模式实际获得的文件级结果，不能用公告级 metadata 填充每份附件。`metadata` 参数仅兼容调用方，不能作为某份文件的泛名证明。model 路线不能为了融合引入新的规则语义输入。

`source_links` 只接受调用者已验证的文件对。模块也可从全文中的精确附件 leaf filename 引用，或两文件独立抽取的相同非空 `project_number` 建立对应。共有 ZIP、文件名相似、只有一个已知包、相同品牌、同名都不构成对应依据。mixed 材料全文可不传入，避免历史段落的包标签或文件引用参与判断；缺 context 时不会靠公告共现自动融合。

`PackageAlias(source_file, left, right, evidence)` 与 `discover_package_aliases(source_texts)` 支持原文明确形如 `包号:A（采购包编号:LONG-A）` 的同义包映射。传入 alias 仍必须在对应全文中验证相同引文和同义括号结构。后缀相同、同处一段、项目编号与包号共现均不成立；歧义 alias 保留原包号并 warning。只在唯一候选融合后应用 alias，不会全局迁移未融合标的或主体。

## 安全边界

- 两份文件候选必须有辨别力的相同型号与数量，或相同价格搭配数量/型号；双方已填的类别、品牌、型号、数量、单位、价格全部不得冲突。
- 不同名称只有某行名称等于其自身逐文件 `project_name` 时才可匹配项目泛名；另外仍要求辨别力型号和数量都相同。`定制`、`自制` 等通用型号，或型号等于品牌，不用于这种身份判断。
- 若附件原始引文是编号分项行，具体名确实位于型号前，则采用具体名；否则保留稳定 HTML 锚点的泛名。原始名称和全部字段始终保留在 provenance。
- 不同已知包不能融合；相同已知包仍须至少一侧有包原文证据。`default` 只在强匹配、唯一对应且已知行有明确包标签/行号证据时补齐。可接受原始行 excerpt、leaf filename、全文唯一包标签；多包全文标签不能归属某一行，父 archive 包名也不能归属内容行。
- 相同表不同位置的采购行、同名多实例不会合并。候选图必须是完整 clique，且每个来源文件只有一个原始候选；缺包桥接、多对多歧义、链式可兼容但存在冲突的关系均保留全部行并 warning。
- 所有来源的原始完整候选以 JSON 存在现有 `source_evidence` 字段中，`format=candidate_reconciliation_v1`；保留全部 `source_file`、`source_location`、`source_evidence`、原包号/名字/其他原值，并附文件 metadata、显式 alias 引文和来源 link。无需扩 schema。旧展示将看到 JSON 文本；未来 UI 可按 format 展示多个来源，但不是本次修改范围。
- 输出使用稳定顺序；重复调用会识别原始来源集合，避免再次吞掉同一文件的另一采购实例。后续第三来源融合仍保留前一次 metadata/alias/link 证据。
- 不处理历史/模板/供应商声明材料用途过滤，不删除无对应候选，不声称这些材料过滤的收益。主体候选也没有变化。

## 实际执行的离线诊断

输入是 `backend/.data/annotation-tasks/official-holdout-20260929/evaluation-routes-qwen-20261009` 内原始 **model response cache 候选**，不是 `predictions-model.json` 中生产路径输出的 317 条。缓存含 326 个响应 JSON，其中 97 个文件响应有标的或主体，涵盖 24 公告，汇总 332 条标的。通过原 runner 的 `SHA256(json.dumps(model_identity, sort_keys=True) + parse_text)` 验证逐文件 parse text 对应，82 个文件 hash 对应，15 个文件无法对应当前 parse cache。

15 个无法对应文件的标的、主体及缓存模型 metadata 仍全部纳入输入，只不给这些文件推测全文，避免因丢文件产生虚假收益。按原 progress identity 的 source name/ZIP descendant 归组；未修改原缓存。调用仅传对应全文和原文件缓存 metadata；没有额外手工 source links 或包别名。主体保持原样，包数为逐公告标的与未变主体的 package union 之和。

| 范围 | 原始候选标的 | 融合后标的 | 包数前→后 | 安全融合组 |
|---|---:|---:|---:|---:|
| 全部 24 公告 | 332 | 309 | 59→59 | 23 |
| 青岛 `0fb4d82774c8ef7a4aef` | 63 | 55 | 2→2 | 8 |
| 广州 `3db0dee4037cdea5a8bd` | 56 | 56 | 1→1 | 0 |
| `5d3e8021bb2b4f45f91d` | 20 | 11 | 1→1 | 9 |
| `894c43b53632c1358ef6` | 27 | 24 | 4→4 | 3 |
| `c11847af857e3aee6e14` | 12 | 10 | 2→2 | 2 |
| `d42ed8c8595cd9b81200` | 6 | 5 | 2→2 | 1 |

其他公告候选数保持相同。总共 31 条不同 warning，其中字段冲突提示 15 条；warning 会去重，同名相同提示不按每个行实例重复。10 组涉及泛名和明细名对应。青岛保留通用/品牌充当型号等不充分候选；广州报价附件的 `category` 抽成品目号（例如 `1-1`）与正文类别冲突，14 条冲突保留，因此没有为降低数量而放宽 category。两个服务器同型号和数量但单价不同，不会被压成一行。

最终对缓存 24 公告逐一执行重复调用和输入倒序检查：全部幂等、全部顺序稳定。初次诊断发现具体名补齐会改变排序的边界，已在首遍按最终候选排序修复，并增加回归，不隐藏该问题。原评测、Gold 和 prediction 没有写入；没有计算或声称新的 Precision/Recall/成绩。

## 验证

实际运行：

```powershell
cd D:\ICT\bid-intel\backend
.venv/Scripts/python.exe -m pytest tests/test_candidate_reconciliation.py -q
.venv/Scripts/ruff.exe check app/candidate_reconciliation.py tests/test_candidate_reconciliation.py
```

39 passed；Ruff 全通过。回归覆盖无 context、同档案不推断、独立项目编号、空编号、名称不足、跨包和所有已填字段冲突、相同来源不同采购行、多实例/多对多、缺包桥接与原文不足、全文多包/枚举包列表和父 archive 不归属、泛名与具体分项、通用型号排除、明确引用、长码后缀不归并、明确及歧义包 alias、完整 provenance、幂等、顺序稳定以及第三文件二次融合。

文件清单：`backend/app/candidate_reconciliation.py`、`backend/tests/test_candidate_reconciliation.py`、`docs/agent-candidate-reconciliation-report.md`。本报告的例示公告 ID 仅用于诊断陈述；模块/测试没有公告 ID 或 Gold 特定值的匹配规则。
