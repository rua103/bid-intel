# 缺口分析（2026-09-24）

> 本文是一次全仓并行审查的结果汇总，覆盖抽取链路、任务二/三、前端、文档、工程质量五个方向，共 59 条代码级发现 + 13 条提交物层面的完整性检查。
>
> **可信度分级**：标 ✅ 的条目已由人工在真实代码与数据上复核过；其余为审查产出，**动手改之前请先自行确认**。
>
> **本文只列问题，不写修复方案。** 每条给出定位、影响和成本估算。

---

> ## 📌 状态列由每个人维护，这是提交的一部分
>
> 本文是全队唯一的任务事实来源。**你不更新，下一个人就会重做你已经做完的事，或者两个人同时改同一处。**
>
> **开工前** —— 把该行状态改成 `进行中 · 你的名字 09-25`
> **提交时**（同一个 commit 里一起做）——
>
> 1. 状态改成 `已修（<提交号前 7 位>）`；判断不成立的条目改成 `不成立（原因）`，**不要删行**
> 2. 到 [`CHANGELOG.md`](CHANGELOG.md) 记一条：改了什么 + **验收边界** + **实测数字**（没有数字的"已修"不算数）
> 3. 测试数量／跑法／红线有变 → 同步 [`ONBOARDING.md`](ONBOARDING.md)
>
> 完整约定与先例见 [`ONBOARDING.md`](ONBOARDING.md) 第十一节。
>
> **2026-09-25 更新**：P0 七项已完成本地回归验证，验收边界与证据见 [`CHANGELOG.md`](CHANGELOG.md)。下文问题描述、旧代码行号和旧实验数字保留为修复前证据，不代表当前结果。首轮 P0 修改曾漏掉多包合并、重复表头和元数据跨行污染，现已补修并增加回归测试。

## 零、优先级总表

排序依据：对分数的直接影响 ÷ 改造成本。**先做 P0，它们都是小时级且直接对应评分项。**

| # | 问题 | 影响 | 成本 | 状态 |
|---|---|---|---|---|
| P0-1 | ✅ hybrid 把同一标的原样重复输出 | 准确性（精确率） | 小时 | 已修并回归：原始行一对一匹配、明确包优先；不重复消耗规则行；歧义保留候选并警告 |
| P0-2 | ✅ 规则抽取把章节标题/联系块当标的行 | 准确性（精确率） | 小时 | 已修并回归：过滤重复表头、章节、联系块、附件占位；嵌套表格各读一次 |
| P0-3 | ✅ 评测接口三点击刷出 100% | 答辩诚信 | 小时 | 已修：草稿 gold 留空；无人工录入内容时拒绝评测；完全一致需显式确认 |
| P0-4 | 评审方远程访问被 CORS 锁死 | 任务三 15 分 + 演示 5 分 | 小时 | 已修：同主机名推导 API；CORS 放行私网演示源，可用配置添加部署域名 |
| P0-5 | 局域网下标注工作台按钮全抛异常 | 标注（gold 唯一入口） | 小时 | 已修：非安全上下文下为随机 UUID 提供回退 |
| P0-6 | 表头别名缺 CCGP 标准列名 | 提取率 10 分 | 小时 | 已修并回归：完整别名匹配、金额单位换算；采购包中标总额不冒充标的价格 |
| P0-7 | CCGP 头部键值表完全没读 | 覆盖率 + 提取率 | 小时 | 已修并回归：保留行/单元格边界；20 条原文头部名称、采购单位、总额逐项对上 |
| P1-1 | 只有中标方被抽成投标主体 | 任务二 25 分 | 天 | 待修 |
| P1-2 | 无 gold set | 准确性 15 分无法自证 | 天 | 待修 |
| P1-3 | 六类提交物一件未开工 | 综合 10 分 + 入场券 | 周 | 待修 |
| P1-4 | 演示三件套全缺（一键启动/样例包/离线兜底） | 演示 5 分 | 天 | 待修 |
| P1-5 | 无登录体系、无评审方操作说明 | 任务三 15 分 | 天 | 待修 |
| P1-6 | 没有清库/数据集隔离 | 任务二 25 分 | 小时 | 已修（本次实现提交）：独立 SQLite 数据集、请求级选择；原库保留，无删除端点；本地回归通过 |
| P1-7 | 附件 .doc/.xls 不支持、PDF 表格库没装 | 覆盖率 10 分 **+ 七字段里的五个** | 天 | 已修（本次实现提交）：真实二进制 DOC/XLS、中文 PDF 七字段通过；DOC 需 LibreOffice，扫描件与复杂布局仍待实测 |
| GAP 4.11 | ZIP 中文文件名与附件归属 | 附件覆盖率 | 小时 | 已修（本次实现提交）：UTF-8/GB18030/Unicode Path；解码与未匹配提示可见；本地回归通过 |

---

## 一、任务一：抽取链路（40 分）

### 1.1 ✅ 规则抽取把章节标题和联系块当标的行输出 · 已修 · 小时

**现象**：`rules` 模式在真实公告上产出的记录里绝大多数不是标的物。

**证据**：[`parsers.py:107-113`](../backend/app/parsers.py) 的表头检测用 `any(alias in cell ...)` 对单元格做子串匹配，一个把整行拼成一大坨的单格也能一次命中 3 个以上字段，被判成"表头"；随后 [`parsers.py:147-155`](../backend/app/parsers.py) 的判废只要求 4 个字段里有一个非空，于是该行之后**每一行都被收下**。

实测（`docs/benchmarks/stream-compare-3.json`，031f976924d449e1.html）规则产的 29 条里包含：

```
三、中标情况 / 四、评审专家名单 / 包号 / 序号
联系人：郑宁飞、郜琳娜、韩嵘 / 联系方式：0371-86258838、13592672705
收费标准：采购代理机构按照计价格[2002]1980号…
序号 名称 品牌（如有） 规格型号 数量 单价 1 见附件 见附件…
```

同一条公告模型抽出的 15 条（高温炉、马弗炉、1200真空管式炉、液压压片机…）才是真正的标的。

**影响**：准确性 15 分按精确率排名赋分，实测精确率约 **8/37 ≈ 22%**，落最低档。这是不花任何模型调用就能往上走的最大杠杆。

> ⚠️ **注意**：这一条推翻了此前"规则是可靠基线、模型增量有限"的判断。规则在真实 CCGP 公告上**既漏**（20 篇里 16 篇产出 0 条）**又错**。

### 1.2 ✅ hybrid 把同一标的原样重复一遍 · 已修 · 小时

**现象**：规则和模型都抽到的行，合并后变成两条。

**证据**：[`ingestion.py:33`](../backend/app/ingestion.py) 的合并硬要求 `package_code` 相等，但规则侧无包号时填 `default`（[`parsers.py:131`](../backend/app/parsers.py)）、模型侧填 `合同包1`，**永远匹配不上**，全部走 append 分支。`_deduplicate`（[`ingestion.py:59-80`](../backend/app/ingestion.py)）也拦不住，因为去重键里 `category` 是代码 `A02320800` vs 名称 `物理治疗、康复及体育治疗仪器设备`。

实测：

```
rules    3 items: ['便携式电动起立床', '中药熏蒸治疗仪', '主被动训练仪']
model    3 items: ['便携式电动起立床', '中药熏蒸治疗仪', '主被动训练仪']
hybrid   6 items: [前三项, 同样的三项再来一遍]
```

**影响**：hybrid 是默认模式（[`config.py:28`](../backend/app/config.py)），演示与提交结果都带这个重复。一条 3 行公告被判成 6 行 = 该条精确率 50%。

**附带的正面收益**：修好这个 join 还能把模型字段合并回规则行（031f976924d449e1.html 的 29 条规则行 `category`/`brand`/`model` 全空，模型有这些值），是准确性 15 分里性价比最高的一处改动。

### 1.3 ✅ 评测接口三点击就能刷出 100% · 已修 · 小时

**现象**：标注工作台走的"生成草稿 → 勾选已核验 → 计算指标"路径，会让五项指标全部显示 100%。

**证据**：[`evaluation_api.py:67-68`](../backend/app/evaluation_api.py) 用**同一个 `notices` 对象**同时构造 gold 和 predictions；前端 [`AnnotationWorkbench.vue:102`](../frontend/src/components/AnnotationWorkbench.vue) 在勾选时把 status 改成 `reviewed`，绕开 [`evaluation.py:387`](../backend/app/evaluation.py) 的 `allow_draft` 保护；[`evaluation_api.py:96-114`](../backend/app/evaluation_api.py) 的 `/run` 收两份上传文件直接计算，**没有任何同一性校验**。

对照组：[`evaluation_cli.py:35-36`](../backend/app/evaluation_cli.py) 有 `if len(inputs) != 2: raise ValueError("gold and predictions must be separate files")`，Web 路径没有同类保护。全后端唯一能产出 `status: predicted` 文件的地方就是 `evaluation_api.py:68`——**predictions 天然等于 gold**。

**影响**：直接威胁任务一"提取准确性"15 分的叙事与答辩诚信。评委问一句"你的 gold 和 predictions 是同一份文件吗"就当场穿帮。而 [`ONBOARDING.md`](ONBOARDING.md) 第五节红线第 5 条本就写着"不要把自动抽取结果当金标"，工具却把错误路径做成了最短路径。

### 1.4 表头别名缺 CCGP 标准列名 · 已修 · 小时

**证据**：[`parsers.py:27-44`](../backend/app/parsers.py) 的 `FIELD_ALIASES`。实测：

| 真实表头 | 结果 |
|---|---|
| `品目号 / 采购标的 / 品牌 / 规格型号 / 数量 / 单价(元) / 金额(元)` | **缺 `total_price`**（`金额元` 不含任一别名） |
| `序号 / 标项名称 / 标的名称 / 品牌 / 规格型号 / 数量 / 单价` | **缺 `product_name` 与 `category`**，整表被拒收，该公告产出 0 条 |
| `序号 / 名称 / 品牌（如有） / 规格型号 / 数量 / 单价` | 缺 `product_name` |
| `包号 / 采购内容 / 供应商名称 / 中标金额 / 单位 / 备注信息` | 只命中 `package_code` |

累计：`rules` 模式 `total_price` 仅 5/37 行、`brand`/`model`/`unit_price` 各 8/37；`model` 模式 `total_price` **0/19**；`hybrid` **0/51**。

**影响**：提取率 10 分（7 字段全提取得 10 分、5≤字段<7 得 5 分）——`total_price` 拿不到直接掉档。

> **⚠️ 「字段覆盖不全」要分两种情况读，别一律当成抽取 bug。**
>
> 逐条拆开 `docs/benchmarks/stream-compare-3.json` 的 `model` 模式：
>
> | | 条数 | 已填字段 | 说明 |
> |---|---|---|---|
> | #1 | 1 | product_name, brand, model, quantity, unit_price（**5/7**） | 正常 |
> | #2 | 3 | 前六项全有（**6/7**） | 正常，只差 `total_price` |
> | #3 | 15 | product_name, quantity（**2/7**） | **原文缺失**，见下 |
>
> **#3 不是抽取失败。** 该公告（`031f976924d449e1.html`）正文的标的表五列全是占位符：
>
> ```html
> <td>1</td> <td>见附件</td> <td>见附件</td> <td>见附件</td> <td>见附件</td> <td>见附件元</td>
> ```
>
> 「见附件」在该文件里出现 **5 次**，真实值在附件中。而模型 prompt 明写「原文缺失字段设为 null，不做常识补全」——**它返回 null 是正确行为**。
>
> 所以字段缺口只有两类，归属完全不同：
>
> | 缺口 | 是 bug 吗 | 该修哪里 |
> |---|---|---|
> | `total_price` 全模式为 0（`model` 0/19、`hybrid` 0/51、`rules` 5/37） | **是** | 本条（1.4） |
> | 「见附件」型公告缺那五个字段 | **不是** | 附件解析（1.8） |
>
> ⚠️ **对排期的影响**：赛题八说明官方数据「含公告及相关附件」，即"见附件"是官方数据的常态形态。**附件解析不是可选项——它直接决定七字段里那五个能不能填上。**

### 1.5 `category` 映射到「品目号」序号列 · 随 P0-6 修复 · 小时

**证据**：[`parsers.py:69-74`](../backend/app/parsers.py) 同一字段取**最左边**命中的列，而 `category` 别名含「品目」（[`parsers.py:38`](../backend/app/parsers.py)），「品目号」包含子串「品目」。实测产出为 `'1-1'`/`'1-2'`/`'1'`——全是行序号。

**影响**：填错比留空对精确率伤害更大，同时污染任务三的品目筛选。

### 1.6 CCGP 公告头部的 2 列键值表完全没被读 · 已修 · 小时

**证据**：实测 `rules` 模式 20 篇：`project_name` **0 篇**、`procurement_unit` **0 篇**、`project_budget` 0 篇、`announced_total_award` 0 篇。原因在 [`parsers.py:381-420`](../backend/app/parsers.py)：`extract_metadata` 只用正则找「标签+冒号」，而 CCGP 模板是 **2 列表格、无冒号**、已被 `_clean` 压成空格。实测该表内容形如 `['采购项目名称', '昌江县紧密县域医疗卫生强基工程设备采购项目']`、`['采购单位', '昌江黎族自治县医疗集团']`、`['总中标金额', '￥1472.608500 万元（人民币）']`——而 `labels` 里连「采购项目名称」都没有，只有「项目名称」。

**影响**：这是本仓库**最便宜的一大块**——每篇公告都有这张表，解析它即可把采购单位/项目名称/预算/中标总额从 0/20 抬到接近 20/20。

### 1.7 hybrid 让被污染的规则元数据覆盖干净的模型元数据 · high · 小时

**当前状态**：本条列出的项目编号串入章节内容问题随 P0-7 修复；保留来源行列后，原例返回 `豫财招标采购-2026-1094`。规则与模型其他实质性元数据冲突的裁决仍未泛化处理。

**证据**：[`ingestion.py:215-221`](../backend/app/ingestion.py) 规则值非空就赢。规则侧污染源是 [`parsers.py:401-410`](../backend/app/parsers.py) 的 lookahead：换行已被压成空格，终止条件只认「下一标签+冒号」或分号句号，于是把「二、」之后的内容一起吞掉。实测 `project_number`：模型给 `豫财招标采购-2026-1094`，hybrid 变成 `豫财招标采购-2026-1094 2、采购`；另两篇同样退化。

**影响**：这是**纯回退**——混合模式比纯模型模式更差。

### 1.8 附件覆盖两处硬缺口 · high · 天

**2026-09-25 当前状态**：P1-7 已补旧 XLS、DOC 转换，并将 PDF 表格/渲染依赖升为基础依赖；增加能力检查和失败提示。15 项附件测试覆盖真实格式与边界（其中扫描页仅验证渲染及模拟 OCR 接口）。DOC 存在生成器兼容差异，未声称所有附件可提取；详情见 [接入说明](DATA_INTAKE.md)。以下为修复前证据。

**证据**：[`corpus.py:21`](../backend/app/corpus.py) 的 `ATTACHMENT_SUFFIXES` 含 `.doc`/`.xls`，但 [`parsers.py:304-334`](../backend/app/parsers.py) 只分派 html/docx/xlsx/pdf/txt/图片，其余返回「暂不支持附件格式」。赛题原文任务一第 2 条写明附件「内含 doc/docx/xlsx/pdf 等」。另：[`parsers.py:266-267`](../backend/app/parsers.py) 在 `pdfplumber`/`pypdfium2` 缺失时只发一条 warning 就降级为纯文本，PDF 表格 **0 抽取**（这两个包在 `pyproject.toml` 里是可选 extra `ocr`，未安装）。实测唯一真实附件（19MB PDF）：1.17 秒抽出 15506 字、`items=0`。

**影响**：覆盖率 10 分（原文和附件各 5 分），附件这一半现在只能拿印象分。**但这一条的影响远不止覆盖率**——官方数据里「见附件」是常态（见 1.4 的说明），品牌/规格型号/单价/总价往往只存在于附件中，所以附件解析**同时决定七字段里那五个能不能填上**，直接连到提取率 10 分。

### 1.9 七个标字段没有任何归一化 · high · 天

**证据**：[`schemas.py:7-23`](../backend/app/schemas.py) 的七字段原样入库（[`storage.py:265-290`](../backend/app/storage.py)）。同一篇公告内品牌写法不一致（`深圳迈瑞生物医疗电子股份有限公司` vs `深圳迈瑞、安保`，且一个单元格装两个品牌未拆分）；品目一侧规则给代码、模型给名称，无映射；单位同时存在 `(套)`/`套`/`台/套` 三种写法。

**影响**：若官方按字段字符串逐个比对，未归一化的字段全部判错。

### 1.10 金额解析丢单位且盲抓第一个数字 · high · 小时

**证据**：[`parsers.py:78-88`](../backend/app/parsers.py) 的 `_decimal` 只做 `re.search(r"-?\d+(?:\.\d+)?")`。实测：`￥1472.608500 万元（人民币）` → `1472.6085`（**差 10000 倍**）；某条模型字段变成 `13000，QL/XZ -IIA`（单价数字与规格型号被拼在一起）；另一处表头 `中标（成交）金额(元)` 被映射成 `total_price`，而该列实际是「投标总价折扣率：99.8（%）」——当前仅因 `product_name` 列未映射、该行被丢弃而**侥幸未产出假值**。

**影响**：金额是赛题点名的两个字段，单位错 10000 倍在评审眼里是硬错。

### 1.11 `organization_aliases` 是只写不读的死表 · medium · 天

**证据**：全仓库 grep `organization_aliases` 仅命中 [`storage.py:30`](../backend/app/storage.py)（建表）与 [`storage.py:166`](../backend/app/storage.py)（INSERT），**没有任何 SELECT 消费者**。实际归并靠 `organizations.normalized_name` UNIQUE + [`storage.py:147-150`](../backend/app/storage.py) 的 `_normalized_entity_name`，后者只做 strip / 去内部空白 / casefold。

**影响**：这是任务二/三"同一主体跨公告归并"的基础设施；表已建好却空转，**等于团队以为做了归一化而实际没有**。

### 1.12 `max_tokens=4096` 给多标项公告设了硬召回天花板 · medium · 小时

**证据**：[`model_adapter.py:141`](../backend/app/model_adapter.py) → `max_tokens=4096`（[`config.py:15`](../backend/app/config.py)）。输出是「全部 items + participants + 每字段 + 每条 source_evidence」的单个 JSON，[`model_adapter.py:176-178`](../backend/app/model_adapter.py) 在 `json.loads` 失败时**直接返回空结果集，无部分恢复、无重试**。语料里多标项公告规模：某公告规则侧可见 30 行、另一篇 46 行，而模型对前者只回 15 条。

**影响**：超过 4096 输出 token 时整篇返回 0 条而非部分结果。**这是修 2.1 时最容易踩的连环坑**：prompt 里一加"所有投标主体都要输出"，此前能出 15 条标的的公告会变成 0 条标的 + 0 条主体。

### 1.13 附件原文静默截断在 12000 字，且每个附件单独消耗一次模型调用 · medium · 小时

**证据**：[`config.py:14`](../backend/app/config.py) `model_max_chars=12000`；[`model_adapter.py:133`](../backend/app/model_adapter.py) 截断后只追加一条 warning 不重试。实测真实附件解出 15506 字 → 被截掉 3506 字。调用次数：[`ingestion.py:195`](../backend/app/ingestion.py) 的调用在 `for document in expanded:` 循环体内，是**每份文件一次**而非每篇公告一次。

**影响**：官方数据集"一公告一附件"是常态，等于模型调用翻倍。

### 1.14 hybrid 是默认模式且无条件调模型 · medium · 小时

**证据**：[`config.py:28`](../backend/app/config.py) 默认 `hybrid`；[`ingestion.py:195`](../backend/app/ingestion.py) 的条件是 `if text and model_is_configured and mode != "rules"`——**完全没有"规则是否已抽到东西"的判断**。实测逐条耗时：`rules` 0.0097 / 0.0102 / 0.0213 s，`hybrid` 18.04 / 33.72 / 90.78 s，**相差约 3476 倍**。用 stub 对 30 条真实公告逐条跑：29 条全部无条件发起调用；其中某篇规则 0.035 秒已抽出 46 条，hybrid 仍调用模型且**一条都没多**。

**影响**：处理速率 5 分（按单篇排名，前 15% 才满分）——现在单篇 18-91 秒基本只能拿 1 分。1000 条公告按 hybrid 约需 13 小时，纯规则只需 14 秒。

### 1.15 合成基准的 100% 抽取率不能代表官方数据 · low · 小时

**证据**：[`benchmark.py:35`](../backend/app/benchmark.py) 生成的表头固定为 `采购标的/品目/品牌/规格型号/数量/单价/总价`——**恰好是被 `FIELD_ALIASES` 完美覆盖的那一种形状**（命中 7/7）。因此 [`synthetic-1000.md`](benchmarks/synthetic-1000.md) 写"实际标的数 2000；预期 2000"。同一套代码在真实 20 篇上只有 4 篇出标的。

**影响**：若 PPT 或数据质量报告引用这个 100% 数字，评审用真实数据一验即穿。

---

## 二、任务二 / 任务三（50 分）

### 2.1 只有中标方被抽成投标主体，场景 2/3/5 必然返回空集 · blocker · 天

**证据**：`docs/benchmarks/stream-compare-3.json` 的 totals：`model` 模式 3 篇公告 `participants_found=2`（**均为 winner**），`rules` 模式 **0**，`hybrid` 2。把该文件的 `model` 输出回放进 SQLite 后实测：场景 3 `top_co_bidders` 恒为 `[]`，场景 4/5 对所有主体对返回空，场景 2 `co_bidder_pairs` 恒为 `[]`。

真实公告里非中标参与方是**存在且规整**的：某篇的「中标（成交）候选供应商评审得分及报价表」明确列了 3 家（93.47/88.1/87），另一篇的「资格性审查/符合性审查/综合得分」表每个合同包列 3~5 家——**一条都没进库**。统计 50 篇：仅 3 篇（6%）原文含非中标参与方清单。示例公告正文只列中标方，模型只抽 1 条 participant 是**正确行为**。

**影响**：评分细则明确「查询结果与官方基准数据比对，结果不一致的场景视为未实现」——**空结果 = 未实现**。当前现实可得分约 5~10/25。

### 2.2 确定性抽取路径里没有「投标主体表格」解析器 · high · 天

**证据**：[`parsers.py:27`](../backend/app/parsers.py) 的 `FIELD_ALIASES` 只定义 7 个**标的物**字段，[`parse_item_tables`](../backend/app/parsers.py)（`parsers.py:100`）是仓库里唯一的表格解析器，只认标的物表头。因此 `rules` 模式的 participants 恒为 0，[`benchmark.py:114`](../backend/app/benchmark.py) 还用注释锁死了这个事实。

而真实公告的投标主体表非常规整（`供应商名称/供应商地址/中标金额/评审总得分`，或 `供应商/资格性审查/符合性审查/综合得分/得分排名/推荐排名`），**完全可以用与 `FIELD_ALIASES` 同样的表头映射法确定性解析**。

**影响**：这是场景 2/3/5 **唯一不烧模型调用、不需秒级延迟**的修复路径。规则基线 0.02 秒/篇，且结果可解释可复现。

### 2.3 Neo4j 路径写了但从未执行过，且完全不接 API · high · 天

**证据**：`import neo4j` → `ModuleNotFoundError`（`pyproject.toml` 里是可选 extra `graph`，未安装）；`pytest -rs` → 唯一跑 Neo4j 的测试被 `skipif` 门控（`test_graph.py:181`）；仓库无 CI、无 Dockerfile、无 docker-compose；[`main.py:19`](../backend/app/main.py) 只 import `graph.sqlite_graph`，全文**没有 import `query_neo4j`**，五个场景端点全部走 SQLite。

**影响**：赛题技术要求明写「推荐采用 Neo4j 完成业务场景的关系建模」，任务二要求「结合知识图谱建模、图检索增强技术」。现状是评委在平台里既看不到 Neo4j、也看不到图检索增强，**答辩若声称用了 Neo4j 拿不出运行证据**；且那 5 段 Cypher 从未执行，演示时可能直接报错。

### 2.4 场景 1 的「产品供应商」维度无数据 · high · 天

**证据**：`item_fields` 累计——`model` 模式 `product_name` 19 / `category` 3 / `brand` **4** / `model` 4 / `quantity` 19 / `unit_price` 4 / `total_price` 0。回放后 `analytics.py:89-95` 用 `procurement_items.brand` 反推产品供应商，`brand` 为空则该维度整体为空（实测 `product_brands` 全为 `[]`）。根因：上游公告的「中标情况」表经常只有「见附件」占位，真实品牌在附件里，而当前开发集**全是纯 HTML、零附件**。

### 2.5 场景 3 缺「总金额」，且 API 已返回的数据前端完全不渲染 · high · 小时

**证据**：[`analytics.py:153-163`](../backend/app/analytics.py) 的 `top_co_bidders` 只 SELECT `organization_id`/`canonical_name`/`package_count`，**无任何金额**；[`App.vue:386-389`](../frontend/src/App.vue) 的卡片只渲染 `top_co_bidders`，而 API 已完整返回的 `packages`（含 `project_name`/`buyer_name`/`target_award_amount`/`participants`）**一行都没用**。

**影响**：场景 3 原文要求「及全部参与投标主体信息，如频次、总金额等」。即使上游数据修好，这半句仍然拿不到——属于"数据齐全也判不一致"的确定性失分。**数据已经算出来了，只是没渲染，属于投入产出比最高的一处修复。**

### 2.6 场景 5 缺「项目总金额」聚合 · medium · 小时

**证据**：[`analytics.py:298-304`](../backend/app/analytics.py) 返回 `{required_entities, packages, package_count, project_count}`，实测 keys 确认无金额聚合；金额只以每个包的 `award_amount_total_unique_awards` 存在，没有跨包/跨项目合计。场景 5 原文要求「项目数量、项目总金额等核心指标」。

### 2.7 场景 2/3 的「参与投标主体」默认把中标方算进去 · medium · 小时

**证据**：[`main.py:171`](../backend/app/main.py) `include_winners: bool = True`；[`analytics.py:110`](../backend/app/analytics.py) 只在显式传 `false` 时才排除；**前端全文没有 `include_winners`**，即平台永远走默认（含中标方）。而赛题术语表定义「投标参与方：本题目中特指参与投标但未中标的主体」。

**影响**：官方基准若按术语表判，场景 2 的 TOP5 与场景 3 的名单会**整体错位**，两个场景各 5 分可能从"有结果"变成"结果不一致=未实现"。无 gold set 无法自证，属高风险未决项。

### 2.8 任务三标的物检索只开放 3/7 个字段的筛选 · medium · 小时

**证据**：[`main.py:140-155`](../backend/app/main.py) 支持 `query`/`category`/`brand`/`model` 四个参数；[`App.vue:333-335`](../frontend/src/App.vue) 只有 3 个输入框，`model` 从未被传过。数量/单价/总价三个字段连 API 都不支持筛选。表格展示本身是齐的（7 列 + 来源）。

### 2.9 五大场景只有纯文字列表，没有统计图表 · low · 天

**证据**：grep `echarts` 全前端只命中 [`RelationshipGraph.vue:3,70`](../frontend/src/components/RelationshipGraph.vue)（力导向图）。场景 1~5 的渲染是 `<p>`/`<small>` 文本，无任何柱状/排行/金额分布图。任务三原文要求「实现五大业务场景的可视化查询、统计与结果展示」。

### 2.10 查询效率未按赛题口径测过 · low · 小时

**证据**：[`synthetic-1000.md`](benchmarks/synthetic-1000.md) 自己声明「不含 HTTP、网络或前端渲染；不等同竞赛响应时间」。补测的 HTTP 层（300 篇库）5 个端点 p50 为 5.12/5.38/5.70/4.88/5.91 ms，远低于 1 秒。但赛题口径是「前端发起请求至结果**完全渲染完毕**的总时长」。另 `common_projects` 在合成集上已到 321ms，是唯一逼近 1 秒的场景。

**修法**：补一个浏览器 DevTools Performance 或 Playwright 计时的截图，即可把 5 分变成可举证。

---

## 三、前端与现场演示

### 3.1 ✅ 评审方远程访问被 CORS 与硬编码 apiBase 双向锁死 · 已修 · 小时

**证据**：[`App.vue:6`](../frontend/src/App.vue) `const apiBase = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'` 是唯一地址来源；[`main.py:47`](../backend/app/main.py) `allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]`；[`package.json:7`](../frontend/package.json) 却是 `vite --host 0.0.0.0`（明显打算局域网访问）。仓库**没有 `frontend/.env.example`**，grep `VITE_API_BASE` 全仓只命中 `App.vue:6`。后端没有 `StaticFiles`，`dist/` 又被 gitignore，因此不存在单源部署路径。

**影响**：评审方打开 `http://<演示机IP>:5173` 时 Origin 不在白名单 → 所有 API 被拦，`127.0.0.1:8000` 还指向评审方本机。赛题原文明确要求「提供评审方登录系统的操作说明及账号」。直接砸任务三功能完整性 15 分 + 技术栈合规 5 分 + 演示 5 分。

### 3.2 ✅ 局域网 http 下 `crypto.randomUUID` 不存在，标注工作台按钮全抛异常 · 已修 · 小时

**证据**：[`AnnotationWorkbench.vue:24`](../frontend/src/components/AnnotationWorkbench.vue) `const uid = prefix => \`${prefix}-${crypto.randomUUID()}\``，被 `addItem`/`addPerson`/`addPackage`/`setBuyer` 四处调用。`crypto.randomUUID` 是 **Secure Context 专属 API**（仅 https / localhost / 127.0.0.1）。而 `package.json:7` 的 `--host 0.0.0.0` 正是邀请用 `http://<局域网IP>:5173` 访问。

**影响**：标注工作台是**把草稿修成 gold 的唯一工具**，而准确性 15 分的唯一解锁路径就是它。评审方点「增加标的」就报错，等于现场证明标注功能不可用。修法很小（换时间戳+计数器，或加 fallback）。

### 3.3 场景三把后端算好的数据整个丢弃 · high · 小时

**证据**：[`App.vue:386-389`](../frontend/src/App.vue) 只渲染 `top_co_bidders`，响应里的 `packages` 一次都没用到。而后端 [`analytics.py:164-189`](../backend/app/analytics.py) 的 `packages` 已算好每个包的 `project_name`/`buyer_name`/`target_award_amount` 并逐包附 `participants`——**正是赛题要的「全部参与投标主体信息」**。

**影响**：场景三题面要求里有一半界面上根本看不到，评审判「未实现」即丢 5 分。**数据已经算出来了，只是没渲染。**

### 3.4 标的物检索无分页、默认 LIMIT 100 静默截断 · medium · 天

**证据**：[`App.vue:103-116`](../frontend/src/App.vue) 只拼三个参数、从不传 `limit`，全前端无页码/总数状态；[`main.py:140-155`](../backend/app/main.py) 默认 `limit=100`，接口**只返回 list 不返回 total**，超过 100 条时界面既不提示也不可翻页。返回的 `project_name`/`project_number`/`procurement_unit` 三个字段从不渲染，也没有单条详情视图（全前端 grep `dialog`/`modal` 无命中）。

### 3.5 平台无法浏览已导入的公告清单，且界面上写着一条不存在的功能承诺 · medium · 天

**证据**：[`App.vue:324`](../frontend/src/App.vue) 写着「可按公告标题查看每条记录的解析警告和候选字段来源。」，但整个前端**没有任何公告列表组件、没有点击事件**。后端也不存在列表接口（只有 `/health` 返回 `count_notices`）。后果：导入完成后看不到"我导入了哪 30 条、每条抽到几条、哪条有 warning"；刷新后连当次的 warnings 也消失。

**影响**：这是评委肉眼可见的空头承诺，答辩最容易被追问（「你说可以按标题查看，在哪？」）。同时挡住的是抽取溯源展示这条主线。

### 3.6 主体下拉框无搜索、被 500 条硬顶，且场景一/二共用同一个 ref · medium · 天

**证据**：[`App.vue:120`](../frontend/src/App.vue) `organizations?limit=500`，四处都是原生 `<select>` 直接 v-for 渲染，无输入过滤。**具体 bug**：[`App.vue:363`](../frontend/src/App.vue)（场景一）与 [`App.vue:373`](../frontend/src/App.vue)（场景二）都写了 `v-model="selectedBuyerId"`，绑定同一个 ref——**改一个另一个跟着变**。

**影响**：无法同时查看"A 单位的中标供应商"和"B 单位的高频投标人"。真实数据集主体数会远超 500，被截断的部分在界面上根本无法选中。

### 3.7 所有请求无超时、无取消、无进度反馈 · medium · 小时

**证据**：全前端 grep `AbortController`/`signal`/`EventSource`/`onUploadProgress` 无命中。已知模型单条 24-90 秒，一条带 3 附件的公告会串行多轮，期间界面只有按钮文字变化（`正在解析…`）。

**影响**：5 分钟演示视频里出现两次 90 秒静止画面，观感等同卡死；现场无法中途取消重来。最小修法：加 `AbortController` + 取消按钮 + 一条带"已用时 N 秒 / 模式"的提示。

### 3.8 标注工作台 deep watch 全量序列化进 localStorage，失败后静默丢标注 · medium · 小时

**证据**：[`AnnotationWorkbench.vue:29-36`](../frontend/src/components/AnnotationWorkbench.vue) 用 `{ deep: true }` watch `gold`，每次敲键都全量 `JSON.stringify`，而 `sources` 里存的是**完整原文**（[`evaluation_api.py:60-64`](../backend/app/evaluation_api.py) 把每份文档解析后的全文放进 `sources[].text`）。`:34` 的 catch 只提示不阻断，之后每次输入依旧失败，localStorage 里保留的是**最后一次成功写入的旧版本**。

**影响**：5MB 配额下必然触顶，后果是「标注了两小时、刷新全没了」，而准确性 15 分完全依赖这份 gold。

### 3.9 任务一「提取的数据表」没有任何导出通道 · medium · 小时

**证据**：前端无导出按钮；唯一下载逻辑只用于 gold/predictions/评测报告。后端 grep `csv` 零命中，无 `to_csv`/xlsx 写出，`pyproject.toml` 无 `console_scripts`。

**影响**：赛题提交物明确要求「任务一提取的数据表、任务二构建的数据模型」。目前只能绕过平台手工导数据，而这恰恰削弱了任务三"数据集自动化处理"的叙事。

### 3.10 echarts 全量引入且首屏同步加载 · low · 小时

**证据**：[`RelationshipGraph.vue:3`](../frontend/src/components/RelationshipGraph.vue) `import * as echarts from 'echarts'`（无按需引入）。构建产物 **1,143.32 kB，gzip 384.03 kB**（2026-09-25 重新测量；此数字随每次前端改动漂移，引用前请自己跑一次 `npm run build`）；bundle 里打进了用不到的 treemap/radar/sunburst/candlestick/sankey/gauge/themeRiver。且该组件由 [`App.vue:417`](../frontend/src/App.vue) 首屏直接渲染，无懒加载。

### 3.11 单页长滚动、无路由无导航 · low · 小时

**证据**：无 vue-router，`App.vue` 一次渲染 7 个区块，编号 00-05 之后接一个**无编号区块**。且 [`App.vue:290`](../frontend/src/App.vue) 的 `<p v-if="error">` 在导入面板内部，但 `error` 同时被 `searchItems` 和 `refreshHealth` 写入——后端不可用时「检索失败」会显示在上传框下面。

---

## 四、提交物与合规

> 这一组来自完整性检查，**是当前最大的整块缺口**：综合指标 10 分全部来自这里，且它是作品能否进入评审视野的前置条件。

### 4.1 六类提交物一件都还没开工 · blocker · 周

赛题逐条点名的交付物：

- 汇报 PPT（需核心内容预览页；决赛须用主办方模板，**不得出现校名/姓名等识别信息**）
- ≤10 分钟汇报视频
- ≤5 分钟系统演示视频（1080P 横屏 MP4，完整演示任务一/二全功能流程）
- 系统设计文档（Word+PDF：业务需求分析 / 技术选型依据 / 数据模型架构 / 功能详细设计 / 功能及测试效果）
- 过程性文档（Word+PDF：技术路线对比分析报告 + 数据质量分析报告）
- 源码及配套资料压缩包（标注软件版本与运行环境、关键部分注释、**任务一提取的数据表**、**任务二构建的数据模型**、功能说明与执行步骤）

`docs/` 下只有四份内部交接文档，`examples/` 只有一个自述「虚构冒烟样例、不能用于比赛评分」的 HTML，**没有任何交付物草稿、模板或分工**。

### 4.2 模型合规性缺版本声明与资源佐证 · high · 天

**证据**：唯一校验是 [`model_adapter.py:120`](../backend/app/model_adapter.py) 与 `:269` 的 `model.startswith(("qwen","deepseek"))`——**任意命名都能通过**。实测 benchmark 用的端点是校内网关上的 `deepseek-v4-flash`，既不是公开的 DeepSeek 版本号，也不是赛事组提供的资源。全仓库**没有任何地方声明模型版本、参数规模、是否微调**。

赛题七(三)6 是硬门槛：采用基座模型+外挂调优方案应使用赛事组提供的标准模型资源并声明版本与参数规模，训调方案须出具佐证材料。

### 4.3 官方基准数据集尚未接入，也没有接入计划 · high · 天

赛题八：基准数据集（约 1000 条，含附件）由命题方统一发放。当前语料是自采的 20 条 ccgp **纯 HTML、零附件**，因此"附件归属""附件型公告""扫描件"这些官方数据的常态形态在开发集里**毫无代表性**。

> ⚠️ **不要以为「官方数据到了只是换语料，管线一行不用改」。** 这个说法曾在旧文档里出现过，**没有证据**，且与 `parsers.py` 对 `.doc`/`.xls` 直接返回「暂不支持」的事实相矛盾。官方数据的常态形态（含附件、扫描件、多包）在自采开发集里毫无代表性。

### 4.4 现场演示三件套全缺 · high · 天

1. **一键启动**：仓库里没有任何 `.ps1`/`.sh`/`.bat`/`Makefile`/`docker-compose`，README 要求手工建 venv、`pip install -e`、`npm ci`、开两个终端。
2. **样例数据包**：唯一 demo 是单文件虚构 HTML，没有能演示「上传公告 + 附件 ZIP → 自动解析 → 结构化入库」的样例包。
3. **离线兜底**：hybrid 是默认模式，模型不可用时只做表格列映射并静默少抽，现场断网/额度耗尽/网关变更时**没有降级演示路径**（无预置入库快照、无缓存 fixture）。

另：OCR 依赖系统级 Tesseract + `chi_sim`（pip 装不了），命题方云环境若没有它，扫描件覆盖为 0。

### 4.5 没有登录/账号体系，也没有给评审方的操作说明 · high · 天

赛题八要求提供评审方登录系统的操作说明及账号，验证过程全程录屏。全仓库**无登录页、无用户表、无权限或会话**（grep `login`/`token`/`auth`/`session` 在 `backend/app` 与 `frontend/src` 均无命中）。README 只有开发者本地启动步骤。

### 4.6 没有清库/数据集隔离 · high · 小时

**2026-09-25 当前状态**：P1-6 已使用每数据集一个 SQLite 文件解决；新建空集可干净重跑，历史库保留。7 项数据集测试覆盖五查询/图谱、并发导入、重启发现及非法 ID；Edge 浏览器实际验证新建→导入→切换→刷新。没有实现删除端点。以下为修复前证据。

只有一个库 `backend/.data/bidintel.db`，**既没有数据集（dataset）维度，也没有任何清空/重置端点**（grep `reset`/`clear`/`truncate`/`drop` 全无命中）。评审上传官方 100 条后，开发集公告会一起进入合作频次、金额、项目数的聚合，**五大场景的数字将系统性对不上官方基准**。

### 4.7 模型换端点后的验收清单缺失 · high · 小时

现在能抽出 19 条，依赖一组针对当前网关实测出来的参数组合（`chat_template_kwargs` 等）。赛题八说明模型资源由命题方在统一平台环境提供，**base_url、模型名、能力集都不确定**：新端点若不认 `chat_template_kwargs`，会重新先烧约 5000 推理 token 撞穿 `max_tokens`，整篇返回 0 条且只留一句泛化 warning。仓库里没有这套「换端点验收清单」。

### 4.8 自测「准确率」是本地自创定义，与官方口径的关系没有说明 · medium · 小时

赛题只写准确率/精确率/召回率，没给定义。[`evaluation.py:26-30`](../backend/app/evaluation.py) 自己声明 `Accuracy is the local open-extraction proxy TP/(TP+FP+FN)`、真阴性未定义，阈值与容差也都是本地约定。提交物要求写「准确性测试结果」，答辩第一个问题就是「你这个准确率怎么算的」。需要一份口径说明，并明确与官方可能定义的差异。

### 4.9 并发与长批量任务没有工程保障 · high · 天

[`storage.py:120-125`](../backend/app/storage.py) 只设 `PRAGMA foreign_keys=ON`，**没有 WAL、没有 busy_timeout**；每次调用新建连接、无连接池；uvicorn 单进程。导入是 `run_in_threadpool` 里的分钟级阻塞调用，没有后台作业、进度端点或断点续跑。后果：评审边导边查会撞 `database is locked`；按赛题口径（处理速率 = 全部结果写入数据库的总耗时 / 公告总篇数）导入 100 条按现状约 **79 分钟**，任何超时都会让整次调用作废，而模型调用已经花掉。

### 4.10 任务二查询口径写死在 SQL 里 · high · 天

[`QUERY_SEMANTICS.md`](QUERY_SEMANTICS.md) 列了 6 条待官方确认的歧义，但只有 `include_winners` 做成了参数，**其余全部硬编码在 `analytics.py` 的 SQL 里**。官方样例一旦给出不同口径，改的是查询实现而不是配置。同时仓库里没有任何"给定语料 → 期望结果"的自建基准答案来回归这五个端点。

### 4.11 官方 ZIP 中文文件名乱码会导致附件整体失配 · medium · 小时

**2026-09-25 当前状态**：已修复常见编码路径并测试混合编码与嵌套 ZIP 归属；无须依赖全包单一 metadata_encoding。无标志时尝试 UTF-8/GB18030，不能覆盖所有未知代码页。坏包、CRC 无效、重名、未匹配均有提示。以下为修复前证据。

[`parsers.py:359`](../backend/app/parsers.py) 用 `zipfile.ZipFile(io.BytesIO(...))` 解压，**没有传 `metadata_encoding`**。Windows 下常见工具生成的 zip 若未置 UTF-8 文件名标志，Python 会按 cp437 解码，中文名变乱码；而附件归属完全靠文件名（`ingestion.py:107-153`），乱码后候选键与 HTML 的键交集为空，附件被判为 orphan 不进解析——**只会安静地出现在 `orphan_files` 里**。修法很小，但必须在拿到官方压缩包之前准备好。

### 4.12 自建评测集缺抽样方案与标注手册 · medium · 天

没有独立标注手册、没有分层抽样方案（表格型/附件型/多包型/扫描件型各标多少）、没有调优集与留出集的划分规则、没有双人复核与一致性检查。更关键的是：开发集 20 条为纯 HTML 无附件，**用它标出的 gold 验证不了附件抽取**，而覆盖率 10 分里附件正好占一半。

### 4.13 两类过程性文档所需的数据质量统计没有产出工具 · medium · 天

赛题要求提交技术路线对比分析报告与数据质量分析报告（后者明确要求脏数据类型、缺失值分布、附件解析难点）。现在这些数字只能靠临时脚本人工数。需要固定成可重跑的统计：七字段非空率矩阵、warning 分类分布、附件类型 × 解析结果交叉表、三模式 A/B 迭代对比表。

---

## 五、文档

### 5.1 已解决（2026-09-25 文档重构）

原先同一个事实散落在 2-4 份文档里，且存在**编号撞车**——旧 `HANDOFF.md` 用 P0-1/P0-2/P0-3 指代「实体归一化 / 无金标 / 查询口径」，与本文的 P0-1～P0-7 **完全不是一回事**。同一个仓库里说"P0-1"会指两个东西。

已重构为三份职责单一的文档：

| 现在 | 内容 |
|---|---|
| [`ONBOARDING.md`](ONBOARDING.md) | 接手必读（人 + AI agent 共用）：怎么跑、红线、已解决的坑、性能优化判据 |
| [`CHANGELOG.md`](CHANGELOG.md) | 历史决策、验收边界、排查经过（按日期倒序） |
| [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) | 本文——唯一的问题清单 |

删除 `HANDOFF.md`、`AGENT_BRIEF.md`、`P0_VERIFICATION.md`、`DEVELOPMENT_CORPUS.md`，内容按性质并入上述三份，无信息丢失（经过 → CHANGELOG，结论 → ONBOARDING）。

顺带修正的过时陈述：

- ✅ README 把**已实现**的 OCR 列为待办 → 改为说明开关与 Tesseract 依赖
- ✅ README 从未说明导入默认走 `hybrid` 且接口无 mode 参数 → 已补警告
- ✅ 旧 HANDOFF 把"关闭推理"的结论写反、写「修复后验证仍在运行」、测试数字停在 `61 passed` → 相关内容已按当前事实重写

### 5.2 待修

| 问题 | 证据 | 成本 |
|---|---|---|
| **QUERY_SEMANTICS 低估已实现能力** | [`QUERY_SEMANTICS.md:8`](QUERY_SEMANTICS.md) 写「拿到官方样例后再实现多包映射」，实际 [`storage.py:218-226`](../backend/app/storage.py) 已按 `package_code` 建多包行 | 小时 |

---

## 六、工程质量与安全

| 问题 | 证据 | 影响 |
|---|---|---|
| **模型调用预算只存在于离线脚本** | [`model_adapter.py:42-52`](../backend/app/model_adapter.py) 定义了 `model_call_budget`，但全仓库只有 [`experiments.py:62`](../backend/app/experiments.py) 一处使用；而 `/notices/import`、`/import-batch`、`/evaluation/draft` 全部没有预算包裹。**无鉴权，任何人 POST 一次就能烧掉整月额度** | 成本敞口 |
| **无任何 CI** | 无 `.github`、无 workflow yml、无 `.pre-commit-config.yaml`、无 Makefile。ruff/pytest 配置都在 [`pyproject.toml:38-42`](../backend/pyproject.toml) 但没有任何东西强制执行；前端 scripts 只有 dev/build，无 test/lint | 综合 10 分 + 无回归闸门 |
| **测试盲区** | 实跑 `pytest --cov=app`：`corpus.py` **0%**、`graph_cli.py` **0%**、`evaluation_api.py` **29%**（两个端点体未测）、`parsers.py` 56%（OCR/PDF/docx/xlsx 分支全未测）、`graph.py` 67%（Neo4j 五个场景的 `_read_scene` 一行未测）。注：`corpus.py` 虽 0% 覆盖但代码质量扎实（URL 白名单、robots.txt、限速、大小上限、重定向上限） | 任务二 Neo4j 半边零验证 |
| **上传限制自相矛盾** | [`config.py:9-10`](../backend/app/config.py) `max_batch_upload_mb=500`，但 [`parsers.py:25`](../backend/app/parsers.py) `MAX_EXPANDED_BYTES = 200MB`。500MB 是**死配置**，永远不生效；且整批文件先读进内存 | 演示事故高概率来源 |
| **模型异常被压成类型名** | [`model_adapter.py:177-178`](../backend/app/model_adapter.py) 丢弃异常对象、HTTP 状态码、响应体，只留类名。**401 / 429 / JSON 截断 / 超时四种截然不同的原因在界面上长得一模一样** | 排查全靠加日志重跑 |
| **批量导入只捕获 `ValueError`** | [`ingestion.py:274-289`](../backend/app/ingestion.py) 只捕 `ValueError`，非 `ValueError`（如 `sqlite3.Error`）会穿出，已成功的摘要全部丢弃，用户拿到 500 | 一条坏公告毁整批 |
| **API key 明文落盘、打码泄露末 4 位** | [`config.py:66-74`](../backend/app/config.py) 明文写入 `.data/model_config.json`，无权限收紧。**泄露历史核查是好消息**：`git check-ignore` 确认被忽略、`git log --all` 无记录、仓库无 `.env`。另 [`config.py:52-57`](../backend/app/config.py) 在配置文件损坏时静默 `return {}` | 共享屏幕时暴露片段 |
| **`_tmp_*`/`_probe_*` 草稿与 `.coverage`** | 审查时观察到仓库根与 `backend/` 有 11 个未跟踪草稿（`.gitignore` 的 `tmp-*.json` 规则匹配不上下划线前缀）。**复核时已不存在**——疑为审查过程自身的临时产物，或已被清理。`.coverage` 确实不在 `.gitignore` 里 | 交付观感 |

---

## 附录：可信度说明

- **已人工复核的条目**：1.1、1.2、1.3、3.1、3.2（五条 blocker，均为本次会话中直接在代码与 benchmark 数据上复现）。其中 1.1 与 1.2 的复核同时**推翻了此前"规则是可靠基线"的判断**。
- **其余条目**为并行审查产出，证据形式是 `file:line` + 实测数字，**但未逐条独立验证**。动手前请先确认。
- **已知的转述误差**：审查报告在描述 1.2 的重复行时把 `主被动训练仪` 写成了 `中药熏蒸治疗机`/`多功能训练床`，人工复核以 `docs/benchmarks/stream-compare-3.json` 的实际内容为准。
- **一处被推翻的早期判断**：此前把「某条公告只填了 2/7 个字段」当成抽取缺陷。人工复核后确认那是**原文缺失**——该公告正文五列全是「见附件」，模型返回 null 符合其 prompt 的「不做常识补全」约定。真正贯穿全模式的字段缺口只有 `total_price`，两种情况的区分与归属见 1.4 的说明块。**不要再去查模型为什么"抽不出"那条公告的五个字段。**
- 本文的排序是**建议**，不是结论。任务二/三合计 50 分，若 2.1（投标主体抽取）无法解决，其余任务二相关条目都失去意义——排期时应先确认这一条的可行性。
