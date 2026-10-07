# 资源受限部署准备与验证记录

验证日期：2026-10-07（Asia/Shanghai）

验证口径：**资源受限近似验证的历史快照**。执行主机为 Windows；Docker 容器使用 `python:3.12-slim` 的 Linux 用户态和 Python 3.12，不是 Ubuntu 24.04 验收环境。本轮代码审查修正验证器后没有重跑该容器，下面的失败和计时仍是修正前记录。

## 可复现命令

```powershell
cd D:\ICT\bid-intel
docker run --rm --cpus=2 --memory=4g `
  -v "${PWD}:/workspace" -w /workspace/backend python:3.12-slim `
  bash -lc "python -m pip install -e '.[dev]' && python /workspace/scripts/resource_limited_validation.py"
```

验证器为 [`scripts/resource_limited_validation.py`](../scripts/resource_limited_validation.py)，机器记录写入被忽略的 `backend/.data/resource-limited-validation.json`。历史脚本重建纯虚构离线样例、启动规则模式后端，检查健康状态、五类查询及任务列表，然后终止后端进程；当时的 `evidence_detail=available` 仅来自健康接口的公告数量，并未验证公告证据详情响应。

统一审查已将验证器改为临时隔离数据库、loopback 随机端口并实际请求公告详情检查来源定位及片段；任务列表检查明确不代表停止/恢复验收。资源限制由调用方容器参数证明，脚本不再写入硬编码“已验收”限额。这些代码修正不回填下面的历史容器结果。

## 历史运行结果（不是当前统一验收结果）

| 步骤 | 结果 | 记录 |
|---|---|---|
| 后端启动 | 通过 | Uvicorn 健康检查成功；API 工作流合计 2.765 秒，未单独记录启动计时 |
| 离线样例导入 | 通过 | `app.demo_seed`，3 条虚构公告；耗时在 JSON 记录中 |
| 五类查询 | 通过 | HTTP 状态 `[200, 200, 200, 200, 200]` |
| 证据详情可用性 | 未验收 | 只检查公告数量，不能据此宣称详情 API 或来源片段可用 |
| 任务停止/恢复相关入口 | 未验收 | 仅 `/api/v1/jobs` 列表 HTTP 200，未执行停止/恢复 |
| 前端构建 | 容器内阻塞 | Python-only 镜像没有 Node/npm；早期宿主模板语法失败已修复，当前结果见统一验收 |
| 后端回归 | 容器内部分失败 | 49 passed, 1 failed；`test_real_binary_doc_fixture` 返回旧版 Word 转换失败 warning。该镜像未部署可用的 Linux LibreOffice 转换环境；日志未单独定位更深层原因 |
| Ruff | 历史容器运行失败 | 记录包含 EXE002/RUF059/F821，涉及挂载文件执行位和当时工作树；当前宿主 Ruff 结果以统一验收为准，不将该历史记录改写为通过 |

宿主机 Windows 的启动/停止脚本仍按 README 使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Demo.ps1 -Offline
powershell -ExecutionPolicy Bypass -File .\scripts\Stop-Demo.ps1
```

宿主机脚本会记录进程、健康检查和日志路径；不能替代第二台设备或 Ubuntu 24.04 验收。宿主前端 Node 测试和生产构建已在统一验收中重跑，结果见 [`CHANGELOG.md`](CHANGELOG.md) 顶部。

## 风险与边界

2026-10-07 修正后宿主复验已通过：Windows / Python 3.13.7，附件与任务回归 50 passed、Ruff 通过、生产构建通过，隔离离线数据集五类查询均 HTTP 200，公告详情 HTTP 200 且含来源定位/片段。机器记录另存 `backend/.data/consolidation-resource-validation.json`，保留原历史记录；未限制宿主 CPU/内存，因此不算 2 vCPU / 4 GB 或 Ubuntu 验收。完整命令见 [统一审查记录](CONSOLIDATION_REVIEW_2026-10-07.md)。

- Docker 近似验证没有部署可用 Linux LibreOffice、OCR runtime 或 Node，因此不能覆盖旧 DOC、OCR 和生产前端构建。
- 2 vCPU / 4 GB 来自上述 `docker run` 参数；历史 JSON 的硬编码字段不是实测资源证明。裸机执行脚本不能据此宣称资源受限验收通过。
- 没有调用真实模型，`EXTRACTION_MODE=rules`；没有修改 holdout Gold、评测目录或官方原始附件。
- `backend/.data/resource-limited-validation.json` 是运行证据，不应提交 API Key、原始模型响应或官方材料。
