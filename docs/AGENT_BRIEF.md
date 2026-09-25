# 接手简报（面向 AI Agent）

> 写给接手本仓库编码工作的 AI agent。假设你没有任何上下文，且**不应该**重新发现已经付过代价的知识。
>
> 读完本文后，你的任务清单在 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md)。本文只讲**环境、状态、红线和已解决的坑**。
>
> **2026-09-25 状态更新**：P0 七项已补修并通过本地回归，见 [P0 验收记录](P0_VERIFICATION.md)，相关修改随本次提交保存。下文旧提交快照仅供追溯，接手时以 `git status` 和 `git log` 为准。

## 0. 一句话

政府采购公告（正文 + 附件）的实体抽取与关系建模，参赛作品。代码能跑、测试全绿、前端能开，但**抽取质量有严重问题**——`docs/GAP_ANALYSIS.md` 里 59 条代码级发现。你的工作大概率是从那里的 P0 开始修。

## 1. 环境与命令

**仓库根**：`D:/ICT/bid-intel`（Windows 11，Git Bash 可用）。子目录 `backend/`（FastAPI+SQLite）与 `frontend/`（Vue3）。

**Python 必须用 venv，全局 python 没装 pytest**：

```bash
cd backend
./.venv/Scripts/python.exe -m pytest -q          # 当前 92 passed, 1 skipped
./.venv/Scripts/python.exe -m ruff check app tests  # 期望 All checks passed
```

跑单条测试：`./.venv/Scripts/python.exe -m pytest tests/test_model_adapter.py -v`

**工具坑（都踩过）**：

- **中文输出在管道里会乱码**（GBK 控制台）。跑脚本时前置 `PYTHONIOENCODING=utf-8`，或者干脆把结果写文件再用 Read 工具读。
- **别在 Bash 工具里嵌套 heredoc**，会挂住直到超时。要跑多行 Python 就写成临时 `.py` 文件再执行。
- 仓库里有 `.data/`（gitignored），真实语料、SQLite 库和模型配置都在里面。**读它没问题，写进 docs 的链接不行**——别人 clone 下来是死链。

## 2. 仓库状态

- P0 提交前的快照：分支 `main` 领先 `origin/main` 7 个提交，HEAD 为 `8eb05e9`；P0 修复在其后提交。
- 本次只保存本地提交，未推送；后续推送前先问用户。工作区状态以 `git status` 为准。
- 最近提交：

```
04755cf docs: split field coverage into a real bug and a missing attachment
ae7e6b3 docs: add a prioritised gap analysis from the full-repo review
1b0de1d docs: record the verified extraction results and correct the fix narrative
b71351e fix: refuse to overwrite an experiment report before spending model calls
5763752 fix: disable reasoning so extraction stops returning truncated JSON
8d51365 chore: ignore the Vite cache directory
a95be2f fix: stream model extraction and drop the thinking switch
```

**文档分工**：

| 文件 | 给谁看 | 内容 |
|---|---|---|
| [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) | **你** | 59 条问题 + 13 条提交物缺口 + 优先级表。**这是你的任务清单** |
| [`HANDOFF.md`](HANDOFF.md) | 人类队友 | 项目现状、阅读顺序、模型修复的完整经过 |
| [`QUERY_SEMANTICS.md`](QUERY_SEMANTICS.md) | 人类队友 | 五类查询口径 + 待官方确认的歧义 |
| [`EVALUATION.md`](EVALUATION.md) | 人类队友 | 本地指标怎么算 |
| [`DEVELOPMENT_CORPUS.md`](DEVELOPMENT_CORPUS.md) | 人类队友 | 开发集来源 + 排障历史 |

> ⚠️ **`GAP_ANALYSIS.md` 里只有 1.1 / 1.2 / 1.3 / 3.1 / 3.2 五条经人工复核，其余 54 条未独立验证。** 动手前先自己确认，别直接照着改。附录里写了哪些可信。

## 3. 红线（违反会产生真实损害）

1. **不要提交 `backend/.data/` 或 `backend/.env`。** `.data/model_config.json` 里是**明文 API Key**。已被 `.gitignore` 覆盖，但不要手动 `git add -f`，也不要把内容贴进任何文档或 commit message。
2. **不要把 `.data/` 的路径写成 docs 里的链接**——gitignored，别人机器上是死链。
3. **不要给模型请求加顶层 `thinking` / `enable_thinking` / `reasoning_effort`**，也**不要**把 `chat_template_kwargs` 那个写法"简化"掉。实测三个顶层字段全被网关忽略，只有 chat-template 形式生效。回归测试锁死了这个 payload 形状（`tests/test_model_adapter.py`）。
4. **commit 不要加任何 AI 署名**（`Co-Authored-By`、`Generated with` 都不行）。**用户明确要求过。** 作者统一为 `fuhanying <fuhanying2710031@outlook.com>`（已配在 git config 里）。
5. **不要为了"验证"批量调用模型。** 每次调用都是真钱 + 24~90 秒。要跑模型实验就用 `--max-model-calls` 限制，或先写 stub 测试。
6. **不要把 `docs/benchmarks/*` 或合成压测的 100% 说成官方成绩。** 合成数据的表头恰好是被支持的那一种形状，不代表真实数据。

## 4. 已解决的坑（不要重新发现，代价很高）

### 4.1 模型抽取曾经是 0 条 —— 三层叠加原因

完整记录见 [`HANDOFF.md`](HANDOFF.md) 第五节。摘要：

| 层 | 症状 | 修法 |
|---|---|---|
| ① 非流式请求 | 6 次请求全部 `ReadTimeout`，耗时齐刷刷停在 45.7~45.9 秒 | 改 `httpx.stream` 读 SSE。`httpx` 的 timeout 是**读超时**，非流式下退化成"整段生成总时长上限"，**调大超时值没用** |
| ② `thinking` 参数 | `thinking:{"type":"disabled"}` 返回**空内容**且延迟翻三倍 | 删掉。加了反而更糟 |
| ③ **推理 token 吃光输出预算** | 请求成功返回，但 `finish_reason=length`，JSON 截断，整篇 0 条 | **唯一有效**：`body["chat_template_kwargs"] = {"enable_thinking": False}` |

**第 ③ 层是真正卡住抽取的那层**，靠 `MODEL_DISABLE_THINKING=true`（默认）控制。

> ⚠️ **陷阱**：用十几个 token 的短 prompt 测，不加任何参数看起来"正常"（11 秒、119 推理 token）。**换成 12000 字符的真实公告就完全不同**——会先烧约 5000 推理 token 再撞穿 `max_tokens=4096`。别用短 prompt 下结论。

修复后实测（`docs/benchmarks/stream-compare-3.json`）：`model` 模式 0→19 条，总耗时 1216.3s→291.6s，输出 token 24576→5494。

### 4.2 模型网关的行为（当前配置）

实际地址与密钥在 `backend/.data/model_config.json`（本地，未提交）——**自己读，别写进文档**。已知特性：

- 只有 `deepseek-v4-flash` 一个 chat 模型，是**重推理模型**（这就是上面第 ③ 层的根源）。
- 支持流式 + `stream_options.include_usage` + `response_format: json_object`。
- **忽略**顶层 `thinking` / `enable_thinking` / `reasoning_effort`。
- 只认 `chat_template_kwargs` 形式的关闭开关。

> ⚠️ 换端点（比如官方提供赛事模型资源）后必须**重跑 A/B 验证**。新端点若不认 `chat_template_kwargs`，会重新回到 0 条。仓库里还没有这套"换端点验收清单"（`GAP_ANALYSIS.md` 4.7）。

### 4.3 实验脚本的输出文件

`backend/app/experiments.py` 默认**拒绝覆盖**已存在的 `--output`，会在跑之前就退出（`exit 2`）。重跑同一路径要加 `--force`。

这条守卫是后加的：之前是跑完 5 分钟、烧完 API 额度之后才抛 `FileExistsError`，结果全丢。

## 5. 你该做什么

去读 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) 的**第零节优先级表**。P0 七条都是小时级、且直接对应评分项：

| # | 问题 | 影响 |
|---|---|---|
| P0-1 | hybrid 把同一标的原样重复输出（合并键 `package_code` 不匹配） | 准确性（精确率） |
| P0-2 | 规则抽取把章节标题/联系块当标的行 | 准确性（精确率） |
| P0-3 | 评测接口三点击刷出 100% | 答辩诚信 |
| P0-4 | 评审方远程访问被 CORS 锁死 | 任务三 15 分 + 演示 5 分 |
| P0-5 | 局域网下标注工作台按钮全抛异常（`crypto.randomUUID`） | gold 唯一入口 |
| P0-6 | 表头别名缺 CCGP 标准列名（`金额(元)`） | 提取率 10 分 |
| P0-7 | CCGP 头部键值表完全没读 | 覆盖率 + 提取率 |

**排期注意**：任务二/三合计 50 分，但如果 `GAP_ANALYSIS.md` 的 **2.1**（只有中标方被抽成投标主体，导致场景 2/3/5 必然返回空集）解决不了，其余任务二相关条目都失去意义。**先确认 2.1 可行不可行，再决定其余排期。**

## 6. 工作纪律（这次会话验证有效，建议照做）

1. **报数字之前重新读源文件。** 这次会话里犯过两次：一次分析了过期的临时 JSON、报了错数；一次凭记忆写了 `finish_reason=length` 而那一轮根本没有该证据。两次都是被用户追问才发现的。
2. **区分「我复核过」和「别人报告的」。** 写进文档时标明。`GAP_ANALYSIS.md` 的附录就是这么做的，照这个标准来。
3. **改完跑全量测试 + ruff**，别只跑改动的那个文件。
4. **大改动前先 `git status`** 确认工作区状态——这个仓库的会话里工作目录被切换过多次。
5. **改完文档就更新 `GAP_ANALYSIS.md` 里对应的「状态」列**，别让它烂掉。

## 7. 三个反直觉的事实（你可能不信，但都复核过）

1. **规则抽取不是可靠基线。** 它在真实公告上**既漏**（20 篇里 16 篇产出 0 条）**又错**（产出的多是「三、中标情况」「联系人：郑宁飞」这类章节标题和联系块，实测精确率约 22%）。模型抽出的那 15 条才是真标的。见 `GAP_ANALYSIS.md` 1.1。
2. **"模型只填了 2/7 个字段"是误诊。** 那条公告原文五列全写着「见附件」，真实值在附件里；模型返回 null 符合它 prompt 的「不做常识补全」约定。**不要再去查模型为什么"抽不出"那五个字段。** 见 1.4 的说明块。
3. **评测接口能三点击刷出 100%。** `/api/v1/evaluation/draft` 把同一个 `notices` 对象同时当 gold 和 predictions 返回，`/run` 没有同一性校验；CLI 有保护，Web 路径没有。见 1.3。

## 8. 用户偏好

- 交流用中文；文档也是中文。
- 说话要直接，**错了就明说是错的**，不要粉饰。这次会话里用户多次追问"这个解决了吗""在哪个问题里"，就是在核对我有没有夸大。
- commit message：conventional commits 前缀 + 详细正文（说明**为什么**、附实测数字），无 AI 署名。
- 不确定的事先问，不要自己拍板做外向动作（推送、发布、删除）。
