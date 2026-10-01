# Neo4j 运行与验收

该目录只放 Neo4j 的运行配置，目录名刻意不叫 `neo4j`，避免从 `backend` 目录启动 Python 时遮蔽 `neo4j` 驱动包。

启动 Community 实例：

```powershell
cd backend/neo4j_runtime
docker compose up -d
docker compose ps
```

连接地址为 `bolt://127.0.0.1:7687`，管理页为 `http://127.0.0.1:7474`，默认账号 `neo4j`，密码见 Compose 文件。共享机器上使用前请修改密码。

启用五类在线分析 API 时，在 `backend/.env` 中配置 `ANALYTICS_BACKEND=neo4j`，并设置 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD` 和 `NEO4J_DATABASE`。默认 `ANALYTICS_BACKEND=sqlite`。Vue 分析页沿用同一组 API，并显示实际使用的查询后端；Neo4j 连接、同步或查询不可用时，请求会自动回退到 SQLite。Neo4j 保留每个逻辑数据集独立的镜像，首次查询以及 SQLite 数据库版本变化后的下一次分析查询会同步该数据集的完整快照。

安装驱动并运行离线单元测试：

```powershell
cd backend
..\.venv\Scripts\python.exe -m pip install -e ".[graph,dev]"
..\.venv\Scripts\python.exe -m pytest tests/test_graph.py -q
```

配置真实集成测试：

```powershell
$env:BIDINTEL_TEST_NEO4J_URI = "bolt://127.0.0.1:7687"
$env:BIDINTEL_TEST_NEO4J_USER = "neo4j"
$env:BIDINTEL_TEST_NEO4J_PASSWORD = "change-this-password"
..\.venv\Scripts\python.exe -m pytest tests/test_graph.py -m integration -v
```

集成测试不跳过连接错误；没有配置或 Neo4j 不可用时应明确失败。在线分析 API 的真实 Neo4j 覆盖位于 `tests/test_graph.py` 的集成测试中，覆盖五个接口及 SQLite 结果一致性。`/api/v1/graph` 的可视化投影仍来自 SQLite；Neo4j 切换只控制五类关系分析查询。

停止服务：

```powershell
docker compose down
```
