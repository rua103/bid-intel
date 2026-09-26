# 接手必读

> 面向**新加入的队友**和**接手本仓库的 AI agent**。整理于 2026-09-25。
>
> | 想找什么 | 去哪 |
> |---|---|
> | 开放问题、优先级、状态 | [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) |
> | **动代码前后要做什么** | **本文第十一节（必读）** |
> | 历史决策与验收记录 | [`CHANGELOG.md`](CHANGELOG.md) |
> | 五类查询口径 | [`QUERY_SEMANTICS.md`](QUERY_SEMANTICS.md) |
> | 指标怎么算 | [`EVALUATION.md`](EVALUATION.md) |
> | 实测数据 | [`benchmarks/`](benchmarks/) |
>
> 本文只写**能复用的知识**——怎么跑、红线是什么、哪些坑已经踩过、性能优化该按什么判据。
> 一次性的排查经过、验收边界、当时的数字表，都在 CHANGELOG，本文不重复。

## 一、这个项目在做什么

赛题五：**面向行业数据的智能实体挖掘与关系建模**。从政府采购公告（正文 + 附件）里：

1. **任务一（40 分）** 抽取标的物七字段——产品/服务名称、品目、品牌、规格型号、数量、单价、总价——以及投标主体与中标结果。
2. **任务二（25 分）** 构建并查询五类关系：采购单位合作供应商、高频投标主体、中标供应商的共同竞标者、多家供应商的共同采购单位、多家供应商的共同投标项目。
3. **任务三（25 分）** 可视化平台，把上面的结果查出来、看得见。
4. 综合印象 10 分。

计分口径（来自赛题原文）：**准确性 = 准确率 × 0.4 + 精确率 × 0.3 + 召回率 × 0.3**；任务一另有**处理速率 5 分**。

技术栈：Python/FastAPI + SQLite（Neo4j 可选）+ Vue3。模型必须是赛题允许的 **Qwen/DeepSeek 系列**（代码里有校验，模型名不以 qwen/deepseek 开头会直接跳过）。

## 二、现在到哪了

- ✅ **受支持格式的基础链路已回归**：解析（HTML/DOC/DOCX/XLS/XLSX/PDF）→ 抽取 → SQLite 入库 → 五类查询 → 前端。当前后端全量测试 `184 passed / 1 skipped`、Ruff clean；前端 `11 passed` 并可构建。
- ✅ **模型抽取已修好**并用真实公告验证（原本 0 条 → 19 条）。**P0 七项已完成本地回归**，验收边界见 [`CHANGELOG.md`](CHANGELOG.md)。
- ✅ **P1-1 结构化投标主体抽取已实现**：从带有投标/评审/报价/成交上下文的结构化表格提取主体、包号和明示结果，保留来源证据；仅凭排名不会推断中标。现有官方全量数据库仍是旧批次的 0 条主体/中标记录，不能据此说原公告没有主体；新规则也还没有 Gold 准确率验证。
- ✅ **P1-4 演示三件套已加入**：Windows 启动/停止脚本、包含 XLSX 附件的虚构 HTML/ZIP 样例、无需模型的离线合成数据集。离线数据有 3 条公告和 7 条投标参与记录，可走五类查询；不能用于比赛评分。
- ✅ **P1-5 评审登录已实现**：单评审账号 + HMAC 签名 HttpOnly Cookie，账号配置脚本和操作指南已加入。自动化覆盖本机登录/API 保护/退出流程；第二台物理设备的局域网登录、Cookie 与防火墙访问尚未验收。
- ❌ **没有人工金标**，所以没有任何可以对外宣称的准确率。
- ⚠️ **官方全量处理已完成，结果仍待核验**：1038 条公告已进入独立数据集，后台任务最终 1038/1038 完成、0 失败。使用 `rules + 本地 RapidOCR`，没有真实模型调用；产生 6814 条标的候选，但投标参与方与中标记录均为 0。87 个无效下载附件、少量损坏成员和不支持格式仍待处理。**这不是准确率结果**，必须对照原文人工标注；细节见 [官方接入检查](OFFICIAL_INTAKE_REVIEW.md)。
- ⚠️ **速度基线有范围**：本次规则 + OCR 使用 3 个进程，逐条检查点估算活动处理时间约 66 分 45 秒，中位每条 1.127 秒、P95 41.249 秒。它不代表 hybrid/model 模式速度；优化模型调用前先读第七节。

**开放问题不在这份文档里**——去 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) 的优先级表看，那里是唯一事实来源。

> ⚠️ 本仓库所有 `docs/benchmarks/*` 都是**开发验证**，不是官方成绩。合成压测的表头恰好是被支持的那一种形状，与模型准确率无关。

## 三、本地跑起来

后端（终端 A）：

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,ocr]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

前端（终端 B）：

```powershell
cd frontend
npm ci
npm run dev
```

打开 `http://localhost:5173`；API 文档在 `http://127.0.0.1:8000/docs`。

### 评审演示与账号

Windows 演示时可以先生成本机账号，再启动离线快照：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Configure-ReviewerAccount.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Demo.ps1 -Offline
# 演示结束
powershell -ExecutionPolicy Bypass -File .\scripts\Stop-Demo.ps1
```

账号文件位于 Git 忽略的 `backend/.data/reviewer-credentials.txt`；通过受控渠道交给评审方，不要提交或公开。默认开发配置 `AUTH_ENABLED=false`，运行账号脚本后登录才会启用。`-Offline` 建立独立虚构数据集并锁定 rules 模式，不需要 API Key；数据集可切换到“离线演示样例（纯虚构）”。脚本会把 Vite 和后端监听在所有网络接口，但远程访问尚未做第二台设备验证。账号设置、Cookie 和故障排查见 [`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md)。

模型配置**可以直接在网页「模型配置」面板填**（写入 `backend/.data/model_config.json`，重启不丢；`backend/.env` 作兜底默认值）。页面只显示打码尾号。

> ⚠️ 网页单次导入接口没有 mode 参数，默认走 `hybrid`（[`config.py`](../backend/app/config.py)）；配好模型后每份参与抽取的文档都会调用模型。批量任务是另一条链路：可在「大批量后台处理」中选择 rules/hybrid/model，默认 `rules + OCR`，会显示进度并支持暂停、续跑和失败重试。两种路径的处理范围与耗时不同；跑规则基线时请明确选 rules。

部署新环境时，请按 [DATA_INTAKE.md](DATA_INTAKE.md) 安装 LibreOffice（DOC）、基础 PDF/XLS 依赖及 `.[ocr]`（RapidOCR），并新建空数据集。默认数据集保留原有数据；每次请求独立选择库，模型配置仍为全局。默认 RapidOCR 不要求 Tesseract；若显式切换到 Tesseract 引擎，才需安装程序及中文语言包。

## 四、建议的阅读顺序

| 顺序 | 内容 | 为什么 |
|---|---|---|
| 1 | [`README.md`](../README.md) | 全局：能干什么、怎么跑、API 清单 |
| 2 | 本文第三节 + 第五节 | 先把环境跑通、把红线记住 |
| 3 | [`QUERY_SEMANTICS.md`](QUERY_SEMANTICS.md) | **任务二 25 分的核心**：五类查询当前口径 + 必须拿官方样例确认的歧义 |
| 4 | [`EVALUATION.md`](EVALUATION.md) | 本地指标怎么算：Hungarian 一对一匹配、阈值、Accuracy 为何是 `TP/(TP+FP+FN)` |
| 5 | [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) | 现在的任务清单 |
| 6 | 后端代码，按数据流读 | `schemas.py`（数据形状）→ `parsers.py`（解析）→ `model_adapter.py`（模型调用）→ `ingestion.py`（编排）→ `storage.py`（落库）→ `analytics.py`（五类查询）→ `main.py`（API） |
| 7 | [`frontend/src/App.vue`](../frontend/src/App.vue)、[`AnnotationWorkbench.vue`](../frontend/src/components/AnnotationWorkbench.vue) | 前端两大块：主界面 + 人工标注工作台 |
| 8 | [`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md) | 演示账号配置、评审登录和故障排查 |

## 五、红线

1. **不要把 `backend/.data/` 或 `backend/.env` 打包外发。** `model_config.json` 里是**明文 API Key**（已 gitignore，不会被提交，但别手动发出去，也别 `git add -f`，更不要贴进任何文档或 commit message）。
2. **不要把 `.data/` 的路径写成文档里的链接**——gitignore 了，别人 clone 下来是死链。
3. **不要给模型请求加顶层 `thinking` / `enable_thinking` / `reasoning_effort`**，也**不要**把 `chat_template_kwargs` 那个写法"简化"掉（原因见 6.1）。回归测试锁死了这个 payload 形状。
4. **不要把 `docs/benchmarks/*` 或合成压测的 100% 说成官方成绩。**
5. **不要把自动抽取结果当金标。** 标注集与留出验证集要分开。
6. 没有独立人工金标和明确评测口径时，**不要对外宣称准确率数字**；官方数据到手本身不满足这两个条件。

## 六、已经解决的坑（不要重新发现，代价很高）

### 6.1 模型抽取曾经是 0 条 —— 三层叠加原因

| 层 | 症状 | 结论 |
|---|---|---|
| ① 非流式请求 | 6 次请求全部 `ReadTimeout`，耗时齐刷刷停在 45.7~45.9 秒 | 改 `httpx.stream` 读 SSE。`httpx` 的 timeout 是**读超时**，非流式下退化成"整段生成总时长上限"，**调大超时值没用** |
| ② 顶层 `thinking` 参数 | `thinking:{"type":"disabled"}` 返回**空内容**且延迟翻三倍 | 删掉。加了反而更糟 |
| ③ **推理 token 吃光输出预算** | 请求成功返回，但输出被 `max_tokens` 截断，整篇 0 条 | **唯一有效**：`body["chat_template_kwargs"] = {"enable_thinking": False}` |

**第 ③ 层是真正卡住抽取的那层**，由 `MODEL_DISABLE_THINKING=true`（默认）控制。

> ⚠️ **陷阱**：用十几个 token 的短 prompt 测，不加任何参数看起来"正常"（11 秒、119 推理 token）。**换成 12000 字符的真实公告就完全不同**——会先烧约 5000 推理 token 再撞穿 `max_tokens`。**别用短 prompt 下结论。**

完整排查经过见 [`CHANGELOG.md`](CHANGELOG.md)。

### 6.2 模型网关的行为（当前配置）

实际地址与密钥在 `backend/.data/model_config.json`（本地，未提交）——**自己读，别写进文档**。已知特性：

- 只有 `deepseek-v4-flash` 一个 chat 模型，是**重推理模型**（这就是 6.1 第 ③ 层的根源）。
- 支持流式 + `stream_options.include_usage` + `response_format: json_object`。
- **忽略**顶层 `thinking` / `enable_thinking` / `reasoning_effort`。
- 只认 `chat_template_kwargs` 形式的关闭开关。

> ⚠️ **换端点（比如官方提供赛事模型资源）后必须重跑 A/B 验证。** 新端点若不认 `chat_template_kwargs`，会重新回到 0 条。验收清单见 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) 4.7。

### 6.3 `experiments.py` 拒绝覆盖已有报告

`backend/app/experiments.py` 默认**拒绝覆盖**已存在的 `--output`，在跑之前就退出（`exit 2`）。重跑同一路径要加 `--force`。

这条守卫是后加的：之前是跑完 5 分钟、烧完 API 额度之后才抛 `FileExistsError`，结果全丢。

## 七、性能：动手优化前先看这三条

**结论：延迟 ≈ 输出 token ÷ 19.5。** 两次独立实测互证（同一网关、同一模型）：

```
reasoning 开: 24576 token / 1216.3s = 20.21 token/秒
reasoning 关:  5494 token /  291.6s = 18.84 token/秒
```

token 量差 4.5 倍，**吐字速率几乎一样**。所以模型没有"变慢"过——它只是被要求多吐字。

### 提速方案的两条判据

任何提速方案，先问：

1. **它能减少输出 token 吗？**
2. **它能减少调用次数吗？**

两条都答不上来就是无效方案。已实测挡在这两条上的：

| 方案 | 为什么无效 |
|---|---|
| **RAG / 向量检索** | 省的是**输入**。prefill 并行、几乎不花时间；而且抽取是 recall 敏感任务，检索漏段直接丢实体。它的 embedder 还会**新增**每篇一次网络调用 |
| 压缩输入（12000 → 3000 字符） | 同上，实测数字不会动 |
| 提前截断输入 | 同上 |

### 真正有效的三条

| 手段 | 量级 | 前提 |
|---|---|---|
| **规则已覆盖就跳过模型** | 被跳过部分**全部**墙钟 | 规则基线 0.01s vs 模型 24-88s。**但必须先实测"规则能完整覆盖多少篇"**——现在规则在 20 篇里 16 篇产出 0 条，别预期 90% |
| **缩短 `source_evidence`** | 估算 20-35% | 证据回显占输出正文的 **46-50%**（实测）。让它"不超过 30 字，够定位即可"即可，不损失校验能力 |
| **并发调用** | 2-4×，近似线性 | 见下面的实现警告 |

### 并发的两个实现警告

1. **我们的模型调用是同步的**——`with httpx.stream(...)`（[`model_adapter.py`](../backend/app/model_adapter.py)）。直接 `asyncio.gather` 会**阻塞事件循环，等价于串行**。必须套 `asyncio.to_thread` / `ThreadPoolExecutor`，或走多进程。
2. **并发之前先给网关加退避**。当前实现撞到 429 是**直接失败**，不是"慢一点"；并发放大限流后会整批挂掉。

### 顺带澄清

- **SQLite 不是瓶颈**：1000 篇持久化 7.76 秒 = 总时长的 0.017%。别为性能上 PostgreSQL。
- **思考（reasoning）的账已经结清**：它解释的是那 4.2 倍（1216s → 291.6s），剩下的 24-90 秒是**真的在生成 JSON**。

## 八、队友不写代码也能做的事：去标数据

优先用 [队友标注指南](ANNOTATION_GUIDE.md) 和协调员生成的 `*.bundle.json`；任务包已配好空白 Gold、全量 rules+OCR 对应预测、解析文本和原始文件索引。无需队友重新上传官方大压缩包，也不会调用模型。

1. 打开前端「05 人工标注与质量评测」，载入分给自己的任务包；
2. **对照左侧来源文字和原始附件**，逐公告填写七字段、采购单位、中标方、投标方；
3. 每条单独勾选核验，所有字段修改会自动取消该条核验；下载 `gold.reviewed.json` 和进度备份。

协调员用 `app.annotation_compare_cli` 查看共同试标差异，用 `app.annotation_merge_cli` 合并后续互不重叠批次。手动上传/导入模式仍可用于小样本；切设备续标请导入进度备份。

**规矩**：自动抽取结果**永远不能**直接当人工标准答案；空白表示原文未披露，不是"没有"；金额统一为元；中标方要**同时**列入投标主体。

> ⚠️ 自动抽取结果永远不能当答案。没有独立金标、留出集和官方口径时，不要对外宣称准确率或官方成绩。

## 九、开发集

`backend/app/corpus.py` 是受限速率的公开开发集采集器：只允许 `ccgp.gov.cn`，每次请求至少间隔 1 秒，记录来源 URL、响应状态、下载时间、文件大小和 SHA-256；第三方附件域名明确记录为跳过。它不访问官方隐藏评测集，也不把公开开发集当作官方成绩。

2026-09-24 采集的 20 条公告在本机 `backend/.data/development-clean/`，**原始文件不纳入 Git**。它们只作为官方材料到达前的开发替代；官方附件的格式错配、扫描件、深层压缩和下载错误不能由这 20 条纯正文验证。

`docs/benchmarks/` 里的文件与用途：

| 文件 | 是什么 |
|---|---|
| `stream-compare-3.json` | **模型修复后**的三模式对比（当前基线） |
| `stream-compare-3-reasoning-on.json` | 同一语料、推理未关（修复前对照） |
| `clean-compare-3.json` | 修复前状态，只作历史留存 |
| `synthetic-1000.md` / `.json` | 离线合成压测，建立 SQLite 查询基线；不代表抽取准确率 |
| `p0-verification.json` | P0 修复的机器记录（只含公开文件名、SHA-256、布尔值和计数） |

## 十、如果你是 AI agent

官方修复结果位于独立数据集“官方数据修复后回归（规则基线·待核验）”，刷新前端后可切换查看；默认库和首轮试跑保留。机器记录 [official-intake-20260925.json](benchmarks/official-intake-20260925.json) 是首轮历史基线，[official-fixes-20260925.json](benchmarks/official-fixes-20260925.json) 是修复回归，两者均不是准确率报告。

上面九节对人同样适用。以下是额外的。

### 环境

**仓库根**：当前项目目录（Windows 11，Git Bash 可用）。子目录 `backend/`（FastAPI+SQLite）与 `frontend/`（Vue3）。

**Python 必须用 venv，全局 python 没装 pytest**：

```bash
cd backend
./.venv/Scripts/python.exe -m pytest -q             # 当前基线 184 passed, 1 skipped（需 LibreOffice 才能跑 DOC 集成用例）
./.venv/Scripts/python.exe -m ruff check app tests  # 期望 All checks passed
```

跑单条测试：`./.venv/Scripts/python.exe -m pytest tests/test_model_adapter.py -v`

**工具坑（都踩过）**：

官方兼容回归使用 tests/test_official_compatibility.py 和 tests/test_attachment_readiness.py。迁移机器需重新安装后端依赖（新增 olefile/filelock）并部署 LibreOffice。若 .env 显式配置过 60 秒 DOC 超时，应改为 DOCUMENT_CONVERSION_TIMEOUT_SECONDS=120 后重启后端，默认值变化不会覆盖 .env。

DOC 缓存位于原数据库目录的 document-cache，只缓存通过校验的成功结果，按内容而非文件名复用。首次转换和缓存复跑必须分开报告。本机完整回归证据位于 backend/.data/official-fixes-20260925，原文、成员清单和缓存不入 Git。当前 6 条回归不是全量接入，不应直接导出为 gold。

- **中文输出在管道里会乱码**（GBK 控制台）。跑脚本时前置 `PYTHONIOENCODING=utf-8`，或者把结果写文件再用 Read 工具读。
- **别在 Bash 工具里嵌套 heredoc**，会挂住直到超时。要跑多行 Python 就写成临时 `.py` 文件再执行。

### 工作纪律（验证有效，建议照做）

1. **报数字之前重新读源文件。** 这个仓库的会话里犯过两次：一次分析了过期的临时 JSON 报了错数；一次凭记忆写了 `finish_reason=length` 而那一轮根本没有该证据。两次都是被追问才发现的。
2. **区分「我复核过」和「别人报告的」**，写进文档时标明。`GAP_ANALYSIS.md` 的附录就是这么做的。
3. **改完跑全量测试 + ruff**，别只跑改动的那个文件。
4. **大改动前先 `git status`**——这个仓库的会话里工作目录被切换过多次。
5. **改完文档就更新 `GAP_ANALYSIS.md` 里对应的「状态」列**，别让它烂掉。

### 三个反直觉的事实（你可能不信，但都复核过）

1. **规则抽取不是可靠基线。** 它在真实公告上**既漏**（20 篇里 16 篇产出 0 条）**又错**（产出的多是「三、中标情况」「联系人：郑宁飞」这类章节标题和联系块，实测精确率约 22%）。模型抽出的那 15 条才是真标的。见 `GAP_ANALYSIS.md` 1.1。
2. **"模型只填了 2/7 个字段"是误诊。** 那条公告原文五列全写着「见附件」，真实值在附件里；模型返回 null 符合它 prompt 的「不做常识补全」约定。**不要再去查模型为什么"抽不出"那五个字段。** 见 1.4 的说明块。
3. **评测接口曾能三点击刷出 100%。** `/api/v1/evaluation/draft` 把同一个 `notices` 对象同时当 gold 和 predictions 返回，`/run` 没有同一性校验。已修，但改这块时要保持住。

### 协作约定

- **交流用中文；文档也是中文。**
- **说话要直接，错了就明说是错的**，不要粉饰。追问"这个解决了吗""在哪个问题里"就是在核对你有没有夸大。
- **commit message**：conventional commits 前缀 + 详细正文（说明**为什么**、附实测数字）。
- **不要加任何 AI 署名**（`Co-Authored-By`、`Generated with` 都不行）——用户明确要求过。作者统一 `fuhanying <fuhanying2710031@outlook.com>`。
- **不确定的事先问**，不要自己拍板做外向动作（推送、发布、删除）。

## 十一、文档约定（动代码前后都要做）

`GAP_ANALYSIS.md` 的状态列是**全队唯一的任务事实来源**。它靠每个人维护——你不更新，下一个人就会重复做你已经做完的事，或者两个人同时改同一处。

**这不是"有空再补"的事，是提交的一部分。**

### 开工之前

在 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) 的优先级表里，把那一行的**状态**改成：

```
进行中 · 你的名字 09-25
```

要做的条目不在优先级表里（只在正文，如 2.2、3.5）？在表末追加一行再标。

> **为什么必须先标**：不标，别人不知道这条已经有人认领；两个人同时开工，合并时必冲突。

### 改完提交时（同一个 commit 里一起做）

1. **`GAP_ANALYSIS.md`** — 状态改成 `已修（<提交号前 7 位>）`。
   - 做法与原文描述不同 → 补一句说明。
   - **发现这条判断本身是错的 → 改成 `不成立（原因）`，不要直接删行。** 删掉的话，下一个人会把同一个误判再犯一遍。
   - 影响到了别的条目 → 在那条的状态里写明关系，例如 `随 P0-6 修复`。
2. **`CHANGELOG.md`** — 加一条当天的记录：改了什么、**验收边界**、**实测数字**。
   - **没有实测数字的"已修"不算数。** "改好了"不是证据，"20 条公告 20/20 一致"才是。
   - 边界也要写：验证覆盖了什么、**没覆盖什么**（例如"未在第二台物理电脑上做网络验收"）。
3. **`ONBOARDING.md`** — 如果测试数量、跑法命令、红线、性能判据发生了变化，同步更新对应小节。

### 三条容易犯的错

| 错误 | 后果 |
|---|---|
| 只改代码不改文档 | 状态列烂掉，后来人重做 |
| 文档写"已修"但没数字 | 无法证伪，等于没修 |
| 把不成立的条目直接删掉 | 误判会被重新犯一遍 |

> 已有的先例可以照抄：[`CHANGELOG.md`](CHANGELOG.md) 里 2026-09-25 那条 P0 记录，就是"改了七项 + 逐项验收边界 + 旧模型回放表 + 明确写出没验证什么"的格式。
