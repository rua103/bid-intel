# 附件用途控制：离线诊断报告

本报告记录 `backend/app/attachment_scope.py` 的设计和对已有留出集缓存的只读核查。没有调用真实或付费 API，没有重跑全量，没有修改 Gold 或原评测文件。原留出集已经被既有评测用于诊断，因此以下内容不宣称是未触碰的独立测试集。

## 接口与接入点

`classify_attachment_scope(filename, text=None)` 适合在解析前和解析后调用。`text=None` 只产生文件名提示并返回 `requires_parse=True`；任何文件名（包括“业绩”）都不会单独丢弃来源。解析后返回 `AttachmentScopeDecision`，包含 `scope`、`extract_items`、`extract_participants`、`extract_metadata`、`reason_codes` 和带文件名的 `warnings`。

`filter_attachment_items(filename, text, items, decision=None)` 和 `filter_attachment_participants(filename, text, participants, decision=None)` 分别适合规则解析和模型候选合并后调用。历史材料会保留 `source_files` 但排除标的、参与主体和元数据；评审材料保留当前参与主体但禁用元数据；混合材料按候选证据落入的正文分节逐条处理，并禁用项目元数据通道。证据找不到、跨分节或用途不明时保守保留并告警。函数要求判定、候选与附件同源，避免误用其他文件的决策。

主入口可采用以下顺序：对每个附件先 `pre = classify_attachment_scope(filename)`；解析获得正文后重新分类；纯历史附件跳过模型调用但保留来源；规则和模型候选合并后分别调用 items/participants filter；按 `extract_participants` 和 `extract_metadata` 决定是否加入参与主体和元数据。本次接入建议只处理非 primary 附件，不扩大 primary HTML 正文分类，也不改变公告分组逻辑。预解析只保守提示；减少模型请求发生在解析后确认纯历史用途之时。

混合正文不得以全文形式进入跨文件 reconciliation 的 `source_texts`。跨文件合并会在此映射里发现附件关联、包号标签与包号别名；历史分节中的这些信息不能支撑当前清单记录。建议仅将非混合的当前标的附件全文提供给该通道，混合文件仍保留已过滤的当前候选及原始证据。2026-10-09只读整合复核用合成数据验证：当前打印机记录未带包号证据，混合文件的历史分节提及“采购包1”和另一份报价明细文件时，提供混合全文会把两条记录合成包1的一条；禁用混合全文context后两条保留（包号default/1），不会借历史关联填包。此复核没有修改共享 ingestion 或加入新的计数测试，本模块回归仍为26项。

## 控制规则

用途判断依赖正文标题、分节和合同字段。行首 `供应商同类项目实施情况一览表`、`案例一览表` 确认历史表；“业绩”叶文件名与合同字段组合也可确认历史资格用途；“本次采购清单”“分项报价明细”“中标标的”及真实品名/品牌/规格/数量表头确认当前标的。行首评审/资格审查结果标题确认当前投标证据，普通“综合得分/评审总得分”提及不触发整篇排除。未知结构标题停止继承历史分节。父级 ZIP 名称不会覆盖叶文件用途；“业绩要求”这类要求提交材料的条款不会被误判为已完成历史表。当前项目编号出现在历史合同表中也不会把历史项目变成当前采购标的。

## 回归结果

`backend/tests/test_attachment_scope.py` 覆盖：三个真实错误形态的历史表拒收；真实当前货物清单保留；含“业绩”字样的当前招标要求保留；当前评审保留主体通道且禁用元数据；混合文件逐候选排除历史分节、保留当前分节；重复/缺失证据和用途不明告警；文件名提示不触发丢弃；资格材料和父级压缩包边界。

执行命令：

```text
工作目录：D:/ICT/bid-intel/backend
.venv/Scripts/python.exe -m pytest tests/test_attachment_scope.py -q
26 passed
.venv/Scripts/python.exe -m ruff check app/attachment_scope.py tests/test_attachment_scope.py
All checks passed!
```

缓存目录 `backend/.data/annotation-tasks/official-holdout-20260929/evaluation-routes-qwen-20261009/cache/` 已只读扫描：237份解析缓存、326份模型缓存。通过模型候选的连续证据与解析正文的空白归一化匹配，完成下列核查。

| 来源 | 解析/模型缓存摘要 | 原候选数 | 正文可定位证据 | 判定后保留 |
| --- | --- | ---: | ---: | ---: |
| `t20260811_27115600.zip!/案例一览表.png` | parse `08e34679...` / model `8a4f16fe...` | 5 | 5 | 0 |
| `t20260702_26863059.zip!/附件.zip!/新建文件夹/采购包1业绩.bmp` | parse `177dcecd...` / model `d62ee552...` | 9 | 9 | 0 |
| `t20260707_26890270.zip!/供应商同类项目实施情况一览表.pdf` | model `1547d5bf...`；现有parse缓存未找到相同正文 | 9 | 未验证 | 未运行正文回放 |

前两项均判定为 `historical_qualification`，原因同时包含 `historical_table_heading` 与 `history_filename_and_contract_fields`。PNG包含“陈景润的科学人生出版项目”，BMP包含“2024年度济南市应急指挥平台运行保障服务项目”。虽然第一份 OCR 页脚有一条“页、签字盖章页及相应标的明细等内容”，结构分节规则会将其视为历史表脚注而非新用途。若接入前跳过这两份已确认历史附件，可避免2次无价值模型请求；该数值是两份样例的条件性估计，不是全量节省量。

PDF模型缓存中确认存在智慧停车場历史项目等9条候选，但不能从现有parse-cache验证对应原文，因此仅用同类表标题形态建立合成回归，不宣称该真实PDF正文已成功回放。历史证据“在原文中出现”和“属于当前采购”是两个独立条件；前两项展示前者成立而后者不成立。这只是离线影响诊断，不是新的模型评测分数，不能据此报告召回率或新的留出集指标。

## 局限

正文 OCR 质量、乱码和复杂跨页表格可能使分节无法定位，此时保守保留并告警。当前实现不猜包号、不依据公告 ID 或 Gold 项目名硬编码，也不判断金额是否属于当前项目。混合材料对有连续历史证据的参与主体可排除；边界不足或证据不明时会保留并警告，无法保证所有历史关联方都被剔除。主 Agent 接入 ingestion 后应确保纯历史附件仍加入 `source_files`，并在最终结果 warnings 中保留本模块告警。本 Agent 只创建本报告、独立用途模块和测试三个文件，未修改共享 ingestion/model_adapter、Gold、原评测文件，未commit/push。

整合复核确认：纯历史附件在规则与模型两个参与主体通道上均过滤，且三种抽取禁用时模型调用前结束；混合及评审附件禁用项目元数据，因而不会加入规则元数据文本或 per-source metadata；primary公告仍沿用现有处理。这是已识别边界的保守修复，不能宣称所有历史附件、无清晰正文边界的混合材料或primary公告中的历史内容均已解决。
