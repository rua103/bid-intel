# 开发与验证

## 环境要求

- Windows、Python 3.11–3.13、Node.js/npm。
- 后端基础依赖：FastAPI、Uvicorn、SQLite 及文档解析库。
- `.[dev]` 提供 pytest、Ruff 和测试夹具依赖；`.[ocr]` 提供 RapidOCR；`.[graph]` 提供 Neo4j 驱动。
- 旧 DOC 转换需要 LibreOffice。Neo4j 集成测试需要 Docker Desktop 和 `backend/neo4j_runtime` 中的 Compose 配置。

## 初始化

```powershell
cd backend
# 以下以 Python 3.13 为例；若本机没有 3.13，请改为已安装的 3.12 或 3.11。
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,ocr,graph]"
Copy-Item .env.example .env

cd ..\frontend
npm ci
```

项目要求 Python 3.11–3.13（`backend/pyproject.toml` 的范围约束）。`scripts/Start-Demo.ps1` 会按 3.13、3.12、3.11 的顺序自动选择可用版本；手动执行初始化命令时，请把示例版本号替换为本机已安装的受支持版本。

API Key、Neo4j 密码和评审账号只放在本地 `.env` 或 Git 忽略的 `.data` 文件，不能写入提交、截图或文档。

## 本地运行

```powershell
# 终端 A
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# 终端 B
cd frontend
npm run dev
```

打开 `http://localhost:5173`；API 文档在 `http://127.0.0.1:8000/docs`。离线评审路径：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Configure-ReviewerAccount.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Demo.ps1 -Offline
# 结束后
powershell -ExecutionPolicy Bypass -File .\scripts\Stop-Demo.ps1
```

局域网演示须按 [`LAN_ACCEPTANCE.md`](LAN_ACCEPTANCE.md) 在第二台设备实测；启动脚本打印的本机地址和诊断结果不能代替跨设备验收。

## 测试与检查

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests

cd ..\frontend
npm test
npm run build
```

当前基线：后端 `364 passed, 0 skipped（含真实 Neo4j 集成）`（2 条 warning），Ruff 通过，前端 `33 passed`，生产构建通过但有大 chunk 警告。数字会随代码变化；提交前以本次实际命令输出为准。

### Neo4j 集成测试

```powershell
cd backend\neo4j_runtime
docker compose up -d

cd ..
$env:BIDINTEL_TEST_NEO4J_URI = "bolt://127.0.0.1:7687"
$env:BIDINTEL_TEST_NEO4J_USER = "neo4j"
$env:BIDINTEL_TEST_NEO4J_PASSWORD = "本地 Compose 密码"
.\.venv\Scripts\python.exe -m pytest tests/test_graph.py -m integration -v
```

不需要 Neo4j 时，离线测试应明确记录跳过原因；需要提交 Neo4j 验收证据时，必须记录版本、数据集、测试命令和结果。停止服务：`docker compose down`。

## 评测开发

- 调优集使用 `gold.merged.json`，用于发现解析和提示问题。
- holdout Gold 必须由独立标注完成，不能用 `predictions.canonical.json` 代替。
- 最终路线评测按 [`benchmarks/route-evaluation-runbook.md`](benchmarks/route-evaluation-runbook.md) 执行；不得在代码或提示未冻结前运行 `stage=final`。
- 评测运行目录和原始模型响应通常位于 Git 忽略的 `.data`，文档只链接可提交的报告或说明，不提交 API 响应和原始官方材料。

## 常见故障

| 现象 | 排查 |
|---|---|
| 前端提示无法连接 API | 检查后端端口、`VITE_API_BASE`、CORS 和局域网防火墙；运行 `scripts/Test-LanDemo.ps1` |
| 模型返回超时/429/非法 JSON | 检查模型端点、超时、配额和 JSON 模式；后台任务会标记可重试，不要把失败结果当成空抽取 |
| DOC 能打开但没有标的 | 检查 LibreOffice 转换和源表格；转换成功不等于字段解析成功 |
| 附件显示 orphan 或 source unavailable | 先核查文件名归属和下载响应；87 个无效下载响应属于源数据问题，不能靠 prompt 恢复 |
| Neo4j 查询回退 SQLite | 查看响应头 `X-Analytics-Backend`、容器日志和密码；回退是保护路径，需在报告中注明 |

## Git 工作流

1. 从最新 `main` 创建短分支，先读 [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md) 第零节。
2. 一个提交只解决一个可验收主题；代码改动同时更新测试、CHANGELOG 和受影响文档。
3. 提交前运行 `git diff --check`，确认没有 `.env`、密钥、`.data`、原始官方材料或构建缓存。
4. PR 描述写清问题、行为变化、验证命令、测试范围和已知限制。
