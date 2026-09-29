# Neo4j 运行与验收

该目录只放 Neo4j 的运行配置，目录名刻意不叫 `neo4j`，避免从 `backend` 目录启动 Python 时遮蔽 `neo4j` 驱动包。

启动 Community 实例：

```powershell
cd backend/neo4j_runtime
docker compose up -d
docker compose ps
```

连接地址为 `bolt://127.0.0.1:7687`，管理页为 `http://127.0.0.1:7474`，默认账号 `neo4j`，密码见 Compose 文件。共享机器上使用前请修改密码。

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

集成测试不跳过连接错误；没有配置或 Neo4j 不可用时应明确失败。应用当前 API 仍以 SQLite 为在线查询源，Neo4j 用于图谱导出、图查询和独立验收。

停止服务：

```powershell
docker compose down
```
