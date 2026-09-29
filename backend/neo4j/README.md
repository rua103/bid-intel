# Neo4j 验收环境

该目录提供固定版本的 Neo4j Community 运行配置。它只承载图谱验收数据；应用的 SQLite 数据库仍是独立文件。

## 启动

在本目录执行：

```powershell
docker compose up -d
docker compose ps
```

服务地址为 `bolt://127.0.0.1:7687`，浏览器管理页为 `http://127.0.0.1:7474`。默认账号是 `neo4j`，默认密码是 Compose 文件中的 `change-this-password`。部署到共享环境前请改密码，并同步修改验证命令。

## 安装 Python 驱动

在仓库的 `backend` 目录执行：

```powershell
..\.venv\Scripts\python.exe -m pip install -e ".[graph,dev]"
```

## 运行真实集成测试

```powershell
$env:BIDINTEL_TEST_NEO4J_URI = "bolt://127.0.0.1:7687"
$env:BIDINTEL_TEST_NEO4J_USER = "neo4j"
$env:BIDINTEL_TEST_NEO4J_PASSWORD = "change-this-password"
..\.venv\Scripts\python.exe -m pytest tests/test_graph.py -m integration -v
```

集成测试没有 `skipif`。环境变量缺失或 Neo4j 不可用时会失败并给出启动提示；查询和导出连接失败会抛出带场景/地址的 `RuntimeError`，不会伪造空数组。

## 导入 reviewed Gold 并逐项比较

从 `backend` 目录执行。`--report` 指向新文件，脚本不会覆盖已有报告：

```powershell
$gold = "E:\Users\16606\桌面\official-20260926-LHH-YHR\official-20260926-LHH-YHR\gold.merged.json"
..\.venv\Scripts\python.exe -m scripts.validate_neo4j_gold `
  --gold $gold `
  --uri $env:BIDINTEL_TEST_NEO4J_URI `
  --user $env:BIDINTEL_TEST_NEO4J_USER `
  --password $env:BIDINTEL_TEST_NEO4J_PASSWORD `
  --dataset gold-24-neo4j-20260929 `
  --report docs/benchmarks/neo4j-gold-validation-<timestamp>.json
```

脚本会把 reviewed Gold 导入新的 SQLite 临时库，再用同一份数据替换 Neo4j 中指定 dataset 的内容，执行五个场景并逐项比较。报告包含 Gold SHA-256、导入节点/关系统计、每个场景的检查数、两方/三方调用覆盖和非空结果样本。

## 停止

```powershell
docker compose down
```

如需清除本地 Neo4j 数据（不可恢复），使用 `docker compose down -v`。
