# GAP 优先级 1：cache-only 回放与 Gold v2 重评分

日期：2026-10-10。此报告记录开发回归材料上的离线诊断，不是官方成绩或独立 holdout 评测。Gold v2 是本地来源修订版；这 24 条公告此前参与诊断，不能称为未触碰的 ground-truth 或独立留出集。

## 基线与输入核验

- 独立 worktree 从基线 commit `ef8b5958238061aad43c0337385a356d0e29d9ea` 建立，开始时主 checkout 状态为两个预存未跟踪文档缓存 HTML；worktree 中的副本 hash 与主 checkout 相同。没有在主 checkout 或其他 Agent 的工作目录中运行回放。
- 旧运行 `evaluation-routes-qwen-20261009` 的 `progress.json` 为 completed，scope 是 attachments，覆盖 rules、hybrid、model 共 24 条公告。样本清单有 24 条记录、46 个来源文件；46/46 来源仍存在且其 SHA-256 与旧运行记录一致。样本清单 SHA-256：`a2ccf64b6750c927bd32f2abb6d7ff645b5d2d4d41d7a87344a0611139f518d1`。
- 旧 Gold 覆盖 24 公告、45 个包、117 个标的，SHA-256 `8c1c98e3a5eccd9ec731c990c65bde4215e40c9a6e189039872bf6c3ef7bced9`。Gold v2 覆盖同一组 24 公告、45 个包、120 个标的，SHA-256 `fae7e54480a0a140323bf2c3772bd488880b5c870e09ef43e771652f02b4d38d`；修订台账 SHA-256 `fc540b803f0544db6ce8bd879084590e26775d59df602d67d8dbc09bf28186a2`。
- 三份旧预测均有完整 24 条公告，公告 ID 与两版 Gold 相同。预测条目数 / SHA-256：rules 119 / `417294c1bea8096620f0ac81dbe0453e7a94f308cc2eb14ce949a2b37093700d`；hybrid 311 / `cf34c83e1a8824bd89e733c14bb1c61cb325fc0b9606a3b945ad704cadcb5bd8`；model 317 / `47f3e2f2bffdba98945aa14c8cf6e7b3fe4ee4d34da31e2edd237b14e46fb839`。预测文件没有缺失。
- 10 月 9 日 model 与 hybrid 各有 326 个结构有效的响应 JSON；cache inventory SHA-256 分别为 model `1451c0213c2ca3081ea88c941b59744bc865a792b09e2053800fd879cfda77f8`、hybrid `f3271c586108034f0cd312c40c5f9db786cbdca1059fcc0f7e6233d6bad52af4`。parse cache 有 237 个有效 JSON，inventory SHA-256 `2bc654bae463de441aa68a6093628a9de69d998a8181d7ab13486f8e826df22b`。此前候选核对发现 97 个 model 响应含标的或主体，其中 82 个能匹配当前 parse text，15 个缺少可关联正文；此限制不以模型响应文件存在来推定已解决。
- 旧 `progress.json` 的 `code_sha256` 只列出 9 个运行时代码文件；它们的 hash 与当前基线同路径版本均不同。旧 model-cache JSON 保存的是经当时 adapter 处理后的 metadata/items/participants/warnings 与历史调用计量，没有原始模型响应正文；旧缓存 key 也不绑定 prompt / adapter 源码 hash。因此本回放验证的是“缓存候选对象经当前基线用途过滤与融合后的行为”，不能证明旧候选由当前请求/prompt/adapter 版本产生，也不等于重放原始模型文本。
- 旧 local_proxy 报告 JSON 保持原样，SHA-256：rules `ea8a20702664680af0dcc2154de1880691274f6fbd431f645020bb80831a0cec`；hybrid `772960ee48ccb4a83f8dee548b253217603b3024e798367f6104a33e95e076f0`；model `662afe04d66e522f7746461675c2d4706598a59e87635d7eb78368bfa38fe41b`。旧 `gap-route-report.json`（`official_qa` 历史口径）hash `c64fd80116380f0049f4906a30703304a1a8e7372050b5a702b2825e736888d7`，也保持不变。

## 零模型请求控制与实测回放

原 route runner 的缓存 miss 会调用模型抽取函数，没有 cache-only 开关，不能安全直接复用。新增仅供本次离线回放的 `cache_only_replay.py` 和脚本；cache miss / 缓存无效时抛出不可被模型抽取器普通 `except Exception` 吞掉的硬失败，脚本不会重解析、写回缓存或调用请求 API。执行期间同步 `_stream_completion` 和异步 `async_stream_completion` 都由 transport interceptor 替换为硬失败。

运行前实际执行了故意缺少响应缓存键的测试：cache miss 立即失败，transport 计数保持 0；测试还直接触发生产抽取函数，确认模型传输入口会被 interceptor 拦截，并未执行 HTTP。回放结果中的同步与异步 transport 尝试均为 **0**，新模型 API 请求均为 **0**，model-cache miss 均为 **0**。10 个缺少 parse-cache 的公告在公告边界被隔离，其他公告继续只读运行；没有对缺失项静默重解析。

回放覆盖和候选统计：

| 路线 | 完整缓存公告 / 预期 | 响应 cache hit / miss | parse cache hit / miss | 用途过滤标的 / 主体输入→输出 | 融合前→后标的 | 完整子集标的 / 主体 | 子集公告-包实例数 | 子集包号代码集合 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| hybrid | 14 / 24 | 93 / 0 | 103 / 10 | 106→106 / 29→29 | 153→146 | 146 / 88 | 34 | `{1, 2, 3, 4, A, B, default, F}` |
| model | 14 / 24 | 93 / 0 | 103 / 10 | 105→105 / 29→29 | 149→139 | 139 / 89 | 32 | `{1, 2, 3, 4, A, B, default}` |

用途过滤在已读缓存的附件上实际执行；该 14 公告子集中没有候选行被用途过滤器删除。共完成 78 个附件正文用途判断：`current_procurement` 6、`historical_qualification` 9、`unknown` 63。历史用途 warning 在受限子集中出现，但没有因此推断过滤收益。两个历史报告中的 `5→0`、`9→0` 仍是各自旧的局部候选验证，不并入这次部分回放。

warning 分类是已完成 14 条公告内的累计出现次数，不是唯一 warning 文本数：

| 分类 | hybrid | model |
|---|---:|---:|
| 缓存模型原有 warning | 128 | 14 |
| 跨文件融合 warning | 23 | 25 |
| 用途不明待核查 | 26 | 26 |
| 历史用途排除提示 | 4 | 4 |
| 解析 / 附件 warning | 3 | 3 |
| 其他 warning | 11 | 9 |

10 条被隔离公告及首次发现的 parse-cache miss 文件如下。原始来源文件存在且 hash 校验通过；缺的是该解析身份对应的缓存记录。由于 miss 必须立即失败，这些公告未进入完整候选融合结果。

| 公告 ID | 缺失 parse-cache 的来源成员 |
|---|---|
| `0fb4d82774c8ef7a4aef` | `t20260811_27115600.zip!/专家评审报酬.pdf` |
| `3261c80a2facabbafbb6` | `t20260806_27088811.zip!/投标（响应）报价明细表.pdf` |
| `4bc9da58df430ccf2538` | `t20260506_26514795.zip!/包1供应商评审情况表.pdf` |
| `72e6117b3eaae1c7d82e` | `t20260807_27090509.zip!/报价明细表(第1轮-北京理工大学出版社有限责任公司).pdf` |
| `8895a165d160488536af` | `t20260707_26890270.zip!/供应商同类项目实施情况一览表.pdf` |
| `894c43b53632c1358ef6` | `t20260702_26863059.zip!/济南市不动产登记中心2026年度网络运维与网络安全服务报价明细附件.zip!/济南市不动产登记中心2026年度网络运维与网络安全服务报价明细附件/报价明细表(第1轮-山东君致系统集成有限公司).pdf` |
| `c11847af857e3aee6e14` | `t20260807_27097738.zip!/投标（响应）报价明细表.pdf` |
| `c87bf46d0a857c37b185` | `t20260601_26666876.zip!/山东省国土测绘院2026年自然资源调查监测专业技术服务（三）样本库建设及林业样地数据获取报价明细附件.zip!/山东省国土测绘院2026年自然资源调查监测专业技术服务（三）样本库建设及林业样地数据获取报价明细附件/投标（响应）报价明细表([1]).pdf` |
| `d50bff2bacee040e0af4` | `t20260803_27062795.zip!/山东劳动职业技术学院2026年度智慧课程建设服务采购项目报价明细附件.zip!/山东劳动职业技术学院2026年度智慧课程建设服务采购项目报价明细附件/报价明细表(第1轮-上海卓越睿新网络科技有限公司).pdf` |
| `d970911f94ad892683bc` | `t20260410_26386311.zip!/SDGP370000000202602000808-A_广州市乐得瑞科技有限公司.zip!/评委费用支付表.pdf` |

新回放数字不与历史原始候选融合数字混用：既有 `332→309`、包数 `59→59`、23 个融合组，是对原始 **model 响应缓存候选**做的 24 公告融合诊断，没有应用当前用途过滤，也不是旧 `predictions-model.json` 的 317 条。这次是在可完整回放的 14 公告上，通过当前 ingestion 中的用途过滤与融合：model 路线 `149→139`，hybrid `153→146`；相应包号代码集合如上，不能外推为 24 公告全量。

## Gold v2 对旧预测离线重评分

三份旧路线预测均直接用 v2 Gold 评分，完整覆盖 24 公告；旧预测字节未更改。评分口径沿用各旧 `evaluation-*.json` 的 `local_proxy`、strict missing、整数部分匹配 exact、金额绝对容差 0.01、数量绝对容差 1e-6、相对容差 1e-9。公式和评分代码未改；重评分命令只读取 Gold / prediction 并写到新目录。结果不是官方竞赛成绩。

| 路线 | Gold 标的数 v1→v2 | 字段 Weighted v1→v2 | 完整记录 Weighted v1→v2 | 预测条目数 |
|---|---:|---:|---:|---:|
| rules | 117→120 | 55.88%→55.13% | 16.02%→12.30% | 119 |
| hybrid | 117→120 | 51.19%→51.71% | 8.56%→9.32% | 311 |
| model | 117→120 | 55.39%→55.89% | 8.50%→7.92% | 317 |

这里的 v1 值取自保留的旧 `evaluation-*.json`，v2 值取自新生成的 `evaluation-*-v2-local_proxy.json`。评分代码仍是基线 commit 中的同一份代码；相关文件 SHA-256：`app/evaluation_cli.py` `53fc4e1918bee8dd9adc6e2e27c4aa273a658d9105a0242377b38a73bac122fb`、`app/evaluation.py` `b089f880a08c43a90875b9b0f6362a6f1db482c52da8f25e725e9687bbf50f7c`、`app/evaluation_policy.py` `e5134af2f4306a6e1e1f176dc380530d0343055a789db37011aaca3270556c7f`。新重评分报告与旧 Gold 报告、旧 `official_qa` gap 报告分别保存，不能把报告差异解释为抽取输出变化；变化来自 Gold 版本。

## 产物、hash 与复查命令

所有新运行产物位于 worktree 的 Git 忽略目录：

- 首次 fail-closed 执行摘要：`backend/.data/gap-priority1-cache-only-20261010-ef8b5958/cache-only-replay-summary.json`。它在首条公告遇到 parse-cache miss 后立即停止，0/24 公告完成，model transport 计数 0；SHA-256 `0bc8a968e43f07be647ff96075e87c845aab78eb1d99a3fcc190bf6fbce5435`。之后才将运行器调整为逐公告隔离，并在下项目录产生部分回放结果。
- 回放：`backend/.data/gap-priority1-cache-only-20261010-ef8b5958-r2/cache-only-replay-summary.json`，SHA-256 `40f56809db2a1e7c5ad8b7e11636fbf33dea1d25ef6f92c7ecd8332880ed6492`。
- 重评分：`backend/.data/gap-priority1-goldv2-score-20261010-ef8b5958/`，JSON / Markdown SHA-256：rules `48088a6280328914cf9a801431a6e389e5e5e4fddb2fb868c25f273d3ee5ab3e` / `ed61decc404f833d2836b42f9a35680d11a4961b2157bb58551a7ddf5498b4c4`；hybrid `e5866fb2e1bef474e69711bae28f0ed1ff925982ce1e03380862c7487ce0a201` / `54bd696a321bd043037a0fe7339a7acfd316a7152096b7b45e3bdd8ffc4db69a`；model `69d62419192c28ddf2180597e803ad6387ee215b15982eeb286c6e8561f661b6` / `a8ff29133e8f8bb3b916eabaa3130216fbece1ab43d9d0b6a7319829fd410c7f`。
- 本次新增代码与测试的文件 hash 按相对路径排序，以 `path + NUL + file-SHA256 + LF` 拼接后计算的代码 bundle SHA-256 为 `f07e4298e46364f5f6111d4d4c29ee1208a8796e4b671a1b4329b0ebbe37971a`。源文件分别为 `backend/app/cache_only_replay.py`（`5d9975c80dd322b483418560e93393e3367339aa711fb1fb84f9e2fa94b105a7`）、`backend/scripts/priority1_cache_readonly_replay.py`（`b54aad1f3c1d16ead6a0ad4b0e21bf93c79bfba5d4c747e2d1dbf655b3fe5f27`）、`backend/tests/test_priority1_cache_only_replay.py`（`2bcd15d59ee8dbac8f19644ccac9de0d468224a51fee6f0078cf013d2bcd3130`）。
- 输入核验摘要同时记录 Gold、Gold v2、三份预测、三份旧报告、旧运行 summary/progress、cache inventory 的 before/after hash；全部相等，字段 `source_gold_prediction_old_report_and_cache_unchanged` 为 `true`。新回放 summary 未保存本机绝对路径、原文、模型响应或 API Key。`.data` 全部被 `.gitignore` 排除。

复查时从 worktree 的 `backend` 目录运行。`<原 checkout>` 表示保存 10 月 9 日忽略数据的 checkout；以下路径采用相邻目录形式，输出目录应换成从未创建过的新唯一名称：

```powershell
cd backend
$python = '..\bid-intel\backend\.venv\Scripts\python.exe'
$ruff = '..\bid-intel\backend\.venv\Scripts\ruff.exe'
$input = '..\bid-intel\backend\.data\annotation-tasks\official-holdout-20260929'
$replay = '.data\gap-priority1-cache-only-<new-unique-id>'
& $python -m pytest tests\test_priority1_cache_only_replay.py tests\test_attachment_scope.py tests\test_candidate_reconciliation.py tests\test_gold_route_evaluation.py tests\test_evaluation.py tests\test_evaluation_diagnostics.py -q
& $ruff check app\cache_only_replay.py scripts\priority1_cache_readonly_replay.py tests\test_priority1_cache_only_replay.py
& $python -m scripts.priority1_cache_readonly_replay --input-root $input --output-dir $replay
```

v2 评分只读复查命令（示例以 model 路线为例；rules、hybrid 替换 mode，输出文件名同步替换）：

```powershell
$run = Join-Path $input 'evaluation-routes-qwen-20261009'
$goldV2 = Join-Path $input 'revisions\20261009-source-verified-v2\gold.reviewed.v2.json'
& $python -m app.evaluation_cli --gold $goldV2 --predictions (Join-Path $run 'predictions-model.json') --json-output '.data\gap-priority1-goldv2-<new-unique-id>\evaluation-model-v2-local_proxy.json' --markdown-output '.data\gap-priority1-goldv2-<new-unique-id>\evaluation-model-v2-local_proxy.md' --metric-profile local_proxy
```

实际测试：cache-only miss / transport 拦截定向测试及附件用途、跨文件融合、Gold route/evaluation 相关测试共 **111 passed**；完整后端回归 **522 passed、4 skipped、2 warnings**；Ruff 检查新增文件通过。三条 v2 重评分 CLI 均退出 0。回放退出码为 2，表示未覆盖全部 24 条而保留部分结果；这不是成功的全量回放。

## 尚未验证

- 缺少 parse-cache 的 10 条公告没有重解析；它们的附件用途分类、候选输出和融合结果未知。15 个既有缓存响应无法关联 parse text 的限制仍在。
- 10 月 9 日运行快照的抽取代码 hash 与当前基线不同，缓存没有 prompt / adapter 源码身份；旧候选兼容性只能按当前 schema 校验及已有证据字段复用，不能还原原始模型输出或证明同一抽取代码生成。
- 14 条可完整回放子集里没有观测到用途过滤删除候选；不能据此判断用途过滤总体收益。历史样例报告的局部 `5→0`、`9→0` 不能补足本次全量覆盖。
- v2 重评分只改变 Gold 输入；没有对回放候选新结果做 Gold 评分，因回放不完整且目的为候选范围/融合诊断。
- Gold v2 的修订与旧 Gold 都是团队本地开发回归材料，不是官方 ground-truth，也不是未触碰的独立 holdout。`332→309` 等历史结果、这次 14/24 回放和 v2 重评分是三类不同结果。
