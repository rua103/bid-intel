# Neo4j 图谱投影接入审计

## 结论

当前赛题不要求 `GET /api/v1/graph` 必须使用 Neo4j。保持 SQLite 图谱投影，不新增生产代码，是本次提交的建议。现有实现已经满足“关系模型 + 五类业务场景可视化查询、统计与展示”的功能要求；Neo4j 继续作为五类分析 API 的可选后端即可。

## 赛题依据

- 技术背景对 Neo4j 的表述是“推荐采用 Neo4j 完成业务场景的关系建模”，属于推荐技术路线，不是强制依赖。
- 任务二要求构建采购单位、中标供应商、投标参与方关系模型，并完成五类场景的分析与可视化检索；任务三要求平台提供数据处理、检索及五类场景的可视化查询、统计和结果展示，没有规定图数据库实现。
- 评分项考察五类场景达成率、平台功能完整性、Vue + Python 技术栈和查询效率，没有 Neo4j 图投影单项。

对应原文证据位于 `D:\ICT\topic5_extracted.txt`：Neo4j“推荐采用”见第 13–14 行；任务二/三要求见第 56–91 行；评分项见第 178–205 行及第 284–301 行。

## 代码与文档核对

- `docs/ARCHITECTURE.md` 将 SQLite 定义为写入源，并将 Neo4j 定义为可选的关系查询镜像；同一节明确 `/api/v1/graph` 仍读取 SQLite。
- `docs/QUERY_SEMANTICS.md` 和 `README.md` 均明确 Neo4j 只切换五类分析 API，图可视化接口是 SQLite 投影。
- `backend/app/main.py` 的 `/api/v1/graph` 直接调用 `sqlite_graph`，保持现有节点、边、分类和计数响应结构。
- `backend/app/datasets.py` 通过 `X-Dataset-ID` 解析到独立 SQLite 文件；现有图接口因此天然按数据集隔离。未知或格式非法的数据集不会静默回退到默认库。
- `backend/app/analytics_backend.py` 的 Neo4j 路径只服务五类分析 API，并在连接、同步或查询失败时回退 SQLite；没有将图可视化接口误标为 Neo4j。

## 验证结果

本次未修改生产代码或现有测试。使用仓库已有运行配置完成以下检查：

- Docker Neo4j `neo4j:5.26.14-community`：容器健康。
- 在 `backend` 目录执行 `.\\.venv\\Scripts\\python.exe -m pytest tests/test_graph.py -m integration -v`：`1 passed, 6 deselected`。
- 在 `backend` 目录执行 `.\\.venv\\Scripts\\python.exe -m pytest -q`（Neo4j 环境变量已配置）：`297 passed`；仅有既有的 2 条 warning。
- 在 `backend` 目录执行 `.\\.venv\\Scripts\\python.exe -m ruff check app tests`：`All checks passed`。

已有测试覆盖 SQLite 节点/边语义、包级关系和金额去重、预览截断、Neo4j 分析镜像的数据集隔离与幂等同步、Neo4j 五类分析结果与 SQLite 对照，以及分析 API 的回退标记。由于赛题没有要求 Neo4j 图投影，本次没有为未必要的图投影路径增加同步、JSON 对照、回退和额外 Docker 测试面。

## 建议

提交时维持 `/api/v1/graph` 的 SQLite 默认投影，并在演示和设计文档中明确“Neo4j 用于可选的五类分析后端，图可视化使用 SQLite”。只有在未来实测跨多跳图分析或数据规模证明 SQLite 投影成为瓶颈时，才考虑新增显式的 Neo4j 图投影路径；届时应保持 SQLite 默认/回退、`X-Dataset-ID` 隔离和现有 JSON 结构，并补充等价性、空数据集、重复同步和不可用回退测试。
