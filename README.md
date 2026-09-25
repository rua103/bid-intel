# 招采数据智能分析引擎

赛题五项目起步仓库：从招采公告和附件提取标的物字段，构建采购单位与投标主体关系，并提供可核验的查询平台。

## 文档

| 文档 | 给谁看 | 内容 |
|---|---|---|
| [`docs/ONBOARDING.md`](docs/ONBOARDING.md) | 新队友 / 接手的 AI agent | **先读这个**：怎么跑、红线、已踩过的坑、性能优化判据 |
| [`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md) | 所有人 | 开放问题清单 + 优先级 + 状态（唯一事实来源） |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | 所有人 | 历史决策、验收边界、排查经过（按日期倒序） |
| [`docs/QUERY_SEMANTICS.md`](docs/QUERY_SEMANTICS.md) | 任务二相关 | 五类查询口径 + 待官方确认的歧义 |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | 标注/评测相关 | 本地指标怎么算 |
| [`docs/benchmarks/`](docs/benchmarks/) | 所有人 | 实测数据（**开发验证，不是官方成绩**） |

附件依赖、数据集切换和官方数据到达后的操作顺序，见 [数据接入说明](docs/DATA_INTAKE.md)。

## 当前实现

- Python/FastAPI 后端支持单公告导入和多公告 ZIP 批量导入，识别附件归属并报告未匹配文件。
- 解析 HTML、DOC/DOCX、XLS/XLSX 和可复制文本 PDF 中的标的物表格，保留来源文件、表格位置和原文证据。DOC 需要 LibreOffice；PDF 表格及渲染库已列为基础依赖。
- 支持 UTF-8/常见 GBK 中文 ZIP 文件名；批量结果展示解压警告、未匹配附件和逐公告解析提示。
- 页面可新建、切换独立数据集，正式数据与开发数据分别入库、分别聚合，历史数据保留。
- SQLite 保存采购单位、项目、采购包、投标主体、中标记录和标的物；支持标的物与主体检索。
- 已实现赛题要求的五类关系查询，具体统计定义及待官方样例确认的歧义见 `docs/QUERY_SEMANTICS.md`。
- Vue 页面支持导入、检索和关系分析结果展示。
- 已加入人工标注工作台：从材料生成空白 gold 与独立的规则/模型/混合预测，对照原文填写七字段及主体，导出独立的 `gold`/`predictions` JSON 并计算本地指标。
- 已加入合成压测、真实公开开发集采集器、SQLite 图谱投影和可选 Neo4j 导出；这些结果均明确标注为开发验证，不是官方成绩。
- 模型适配层支持配置合规的 OpenAI-compatible Qwen/DeepSeek 服务；未配置模型时，只做可解释的表格列映射，不猜测缺失字段。
- 后端测试、代码检查和前端构建已通过；当前还没有用官方数据验证准确率或查询基准。

## 本地运行

终端 A 启动后端：

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

终端 B 启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

打开 `http://localhost:5173` 使用界面，或访问 `http://127.0.0.1:8000/docs` 查看 API。数据库默认写入 `backend/.data/bidintel.db`。`examples/demo_notice.html` 是虚构冒烟样例，不能用于比赛评分。

局域网演示时，在后端终端使用 `uvicorn app.main:app --host 0.0.0.0 --port 8000`，前端仍运行 `npm run dev`。另一台电脑访问 `http://<演示机局域网IP>:5173`，默认 API 指向同一 IP 的 8000 端口；需保证两台机器连通且这两个端口可访问。独立部署 API 时，在前端 `.env` 设置 `VITE_API_BASE` 并重启/重新构建，后端 `.env` 的 `CORS_ALLOWED_ORIGINS` 填前端完整来源（含协议和端口，多个用逗号分隔）并重启。示例见 `frontend/.env.example` 和 `backend/.env.example`。

配置模型时，可直接在 Web 页面「模型配置」面板填写并保存（写入 `.data/model_config.json`，重启不丢，`backend/.env` 作为兜底默认值），也可在 `backend/.env` 中填写赛事允许的 Qwen/DeepSeek OpenAI-compatible 服务地址、模型名和 API Key。API Key 在页面上只显示打码后的尾号。未配置时，表格列映射仍可运行，但不会自动抽取投标主体或非表格标的。

> ⚠️ **导入接口没有 mode 参数，默认走 `hybrid`**：配好模型后点一次导入，**每份文档**（正文 + 每个附件）都会真实调用一次模型并**串行**执行。一条带 3 个附件的公告会卡 1.5～6 分钟，期间界面只有按钮文字变化。单条实测耗时见 [`docs/benchmarks/stream-compare-3.json`](docs/benchmarks/stream-compare-3.json)。

图片 OCR（含扫描 PDF 逐页识别）已实现，开关 `OCR_ENABLED`，但**依赖系统级 Tesseract 程序与 `chi_sim` 语言包**（pip 装不了）；缺失时只会收到一条"未找到 Tesseract"警告。详见 [`docs/ONBOARDING.md`](docs/ONBOARDING.md)。

## API

- `GET /api/v1/health`
- `GET /api/v1/model-config`：读取模型配置（API Key 打码）。
- `PUT /api/v1/model-config`：保存模型配置；API Key 留空时沿用已保存值。
- `POST /api/v1/model-config/test`：按当前填写的地址、Key、模型名发起一次连接测试。
- `POST /api/v1/notices/import`：上传一份 HTML 或一个 ZIP（单个公告及其附件），解析并保存候选记录。
- `POST /api/v1/notices/import-batch`：上传包含多份 HTML 公告及配套附件的 ZIP，按文件名共同前缀分组导入并返回耗时、未匹配附件和逐公告摘要。
- `GET /api/v1/items`：按产品名、品牌、品目和型号检索。
- `GET /api/v1/organizations`：查看可用于关系查询的主体。
- `GET /api/v1/evaluation/schema`、`POST /api/v1/evaluation/draft`、`POST /api/v1/evaluation/run`：标注草稿、gold/predictions 评测和报告导出。
- `GET /api/v1/graph`：读取 SQLite 图谱可视化投影。
- `GET /api/v1/analytics/buyers/{buyer_id}/awardees`
- `GET /api/v1/analytics/buyers/{buyer_id}/bidders`
- `GET /api/v1/analytics/suppliers/{supplier_id}/co-bidders`
- `POST /api/v1/analytics/common-buyers`、`POST /api/v1/analytics/common-projects`

当前阶段的解析结果是“候选数据”，不能代替官方模型和隐藏集评测。接入官方数据后，要先人工标注一小批样本，再比较表格解析与合规 Qwen/DeepSeek 的效果。

数据模型和五类查询的暂定统计口径见 [docs/QUERY_SEMANTICS.md](docs/QUERY_SEMANTICS.md)。

## 下一步

1. 用标注工作台核验真实开发集，并把训练/提示调优集与留出验证集分开。
2. 根据验证集测量七个标的物字段的准确率、精确率和召回率，再改进抽取与名称归一化。
3. 根据官方样例确认五类关系查询的计数和金额口径，并逐条对照基准答案。
4. 补复杂 PDF 表格支持（`pdfplumber`/`pypdfium2` 目前是可选依赖、未安装），再准备演示与提交材料。

按优先级排好的完整清单见 [`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md)。
