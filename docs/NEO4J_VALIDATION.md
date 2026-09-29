# Neo4j 实际运行与验收记录

## 范围

本次验收使用正式 reviewed Gold：

`E:\Users\16606\桌面\official-20260926-LHH-YHR\official-20260926-LHH-YHR\gold.merged.json`

Gold SHA-256：`cfa3fa242bf6f1eec0bd474731e1d48a126df9259e9148c837c8f8458bb5759f`。

Neo4j 使用固定版本 `neo4j:5.26.14-community`。验证数据写入独立 dataset `gold-24-neo4j-20260929-r2`，不会与应用或其他 dataset 混用。

## 启动与测试命令

Docker 环境启动命令：

```powershell
cd backend/neo4j
docker compose up -d
```

服务使用 `bolt://127.0.0.1:7687`、数据库 `neo4j`、用户 `neo4j`。本机本次验收使用 Neo4j Test Harness 的 `bolt://127.0.0.1:63950`，认证密码为 `unused`；这与 Docker 配置不同，但执行的是同一 Neo4j 驱动和 Cypher 路径。

真实集成测试：

```powershell
cd backend
$env:BIDINTEL_TEST_NEO4J_URI = "bolt://127.0.0.1:63950"
$env:BIDINTEL_TEST_NEO4J_USER = "neo4j"
$env:BIDINTEL_TEST_NEO4J_PASSWORD = "unused"
..\.venv-neo4j\Scripts\python.exe -m pytest tests/test_graph.py -m integration -v
```

结果：`1 passed`。测试实际导入夹具、执行五类 Cypher、重复同步检查幂等性，并检查两个 dataset 互不污染。

## Gold 导入与五场景比较

验收脚本：`backend/scripts/validate_neo4j_gold.py`。报告：[`neo4j-gold-validation-20260929-r2.json`](../backend/docs/benchmarks/neo4j-gold-validation-20260929-r2.json)。

导入统计：

| 数据 | 数量 |
|---|---:|
| 公告/项目 | 24 / 24 |
| 采购包 | 39 |
| 标的 | 251 |
| 投标参与关系 | 111 |
| 中标关系 | 39 |
| Neo4j 节点 | 442 |
| Neo4j 关系 | 464 |

SQLite 与实时 Neo4j 逐项比较结果：`6471/6471` 通过，`0` 差异。五个场景检查数如下：

| 场景 | 检查数 | 失败 |
|---|---:|---:|
| 采购单位的中标供应商 | 24 | 0 |
| 高频投标主体 | 48 | 0 |
| 中标供应商共同竞标方 | 74 | 0 |
| 多家供应商共同采购单位 | 673 | 0 |
| 多主体共同投标项目 | 5652 | 0 |

覆盖证据：Gold 含多包项目、`unknown` 和 `nonwinner`；场景四包含 666 个两方调用和 7 个三方调用，场景五包含 5356 个两方调用和 296 个三方调用；Gold 中可构成的三方组合数为 297。

## 错误处理和限制

`create_driver`、`sync_to_neo4j`、`query_neo4j` 在驱动缺失、地址不可用或查询失败时抛出包含地址、dataset 或场景的 `RuntimeError`，不会静默返回空数组。真实集成测试没有 `skipif`；未设置连接环境变量时会以明确失败信息结束。

Neo4j 5.26 对未使用变量作用域的 `CALL { ... }` 发出弃用提示。本次提示不影响结果，后续升级 Neo4j 前应将其改为显式作用域语法。应用现有五个 API 仍由 SQLite 提供，本文只证明独立 Neo4j 导出和查询路径的实际运行与一致性。
