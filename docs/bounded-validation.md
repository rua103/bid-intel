# 有界并发验证记录

本验证套件只请求绑定到 `127.0.0.1` 临时端口的合成 SSE 服务，凭据均为虚构字符串，不发起外部网络连接。它覆盖文件锁在 Windows `spawn` 子进程下的共享配额、同一进程内的请求重叠、OpenAI-compatible SSE 收尾 usage、429 不重试、超时、截断 JSON、非法 JSON、停止信号以及原子 JSON 文件恢复。

从主工作区 `backend` 目录运行定向回归：

```powershell
Set-Location D:\ICT\bid-intel\backend
$py = '.\.venv\Scripts\python.exe'
& $py -m pytest tests/test_bounded_contract.py tests/test_bounded_route_evaluation.py tests/test_job_cache.py tests/test_jobs.py tests/test_model_adapter.py tests/test_model_config.py tests/test_controlled_query.py tests/test_route_evaluation_report.py tests/test_attachment_scope.py tests/test_candidate_reconciliation.py tests/test_evaluation_diagnostics.py tests/test_ingestion_quality_boundaries.py -q
& $py -m ruff check app tests scripts
```

在 Docker Desktop 的 Linux `python:3.12-slim` 容器上已用 2 个 CPU、4 GiB 限额运行离线测试。测试中读取 cgroup v2 `memory.peak`；峰值包含依赖安装和测试进程。

```powershell
docker run --rm --cpus=2 --memory=4g `
  -v D:\ICT\bid-intel:/work `
  -w /work/backend python:3.12-slim `
  sh -lc 'pip install -e ".[dev]" && python -m pytest tests/test_bounded_contract.py tests/test_job_cache.py tests/test_bounded_route_evaluation.py tests/test_jobs.py -q -s && printf "MEMORY_PEAK_BYTES=" && cat /sys/fs/cgroup/memory.peak'
```

## 基线和边界

隔离开发目录从独立 worktree 创建。主工作区 `HEAD` 是 `2e96753bba363bd29a5b8c102afe62ff6bedd533`，开发基线包含当时未提交的接口兼容性修改；正在使用的评测身份记录里的 `model_adapter.py` 与 `config.py` SHA-256 对应当时工作树版本，没有假定运行版等于 `HEAD`。本任务没有写主工作区、停止进程或读取请求内容。主工作区有其他任务的后续改动，冻结补丁用于区分归属。

Qwen 与 DeepSeek 契约测试验证不同端点路径以及已有思考参数约定：Qwen 兼容网关发送 `chat_template_kwargs.enable_thinking=false`，DeepSeek 兼容端点发送 `thinking.type=disabled`。两者均使用 OpenAI-compatible Chat Completions/SSE 请求与 JSON response format。测试不代表任何真实服务商端点已验收，也不声称支持 Anthropic 或 Gemini 原生协议。现有业务模型允许范围仍由应用保留的 Qwen/DeepSeek 规则决定。

文件锁的并发额度只适用于共享同一台主机本地文件系统运行目录的进程。跨主机和 NFS 调度不在此保证范围内。网络断开时，客户端不能判定服务商是否已收到或计费；解除限制后重跑可能再次计费。

## 实测记录

主工作区整合后（2026-10-09）全量后端回归：`501 passed, 3 skipped, 2 warnings`；`ruff check app tests scripts` 通过；前端 `33 passed`，生产构建通过并保留既有的大 chunk 提示。此轮未配置真实 Neo4j或 LibreOffice，不代表这些可选外部集成已在本轮重验；没有调用真实模型 API。

隔离 worktree 的 Linux 合成回归当时使用以下命令：

```bash
python -m pytest tests/test_bounded_contract.py tests/test_job_cache.py \
  tests/test_bounded_route_evaluation.py tests/test_jobs.py -q -s
```

隔离 worktree 中的 Linux 合成验证结果为 `77 passed, 1 warning in 55.06s`；cgroup v2 `memory.peak=693071872` bytes（约 `661 MiB`），限额 `2 CPU / 4 GiB`。Docker 镜像为 `python:3.12-slim`（Debian），该记录用于说明 Linux spawn/锁行为，不是主工作区整合版本的全量 Linux 回归，也未在 Ubuntu 24.04 镜像验收。

容器内 6 请求 SSE 合成样本：`peak_in_flight=2`、总耗时 `0.411 s`、吞吐 `14.59 requests/s`、平均请求耗时（含接纳等待）`0.256 s`；长 SSE 在途时 health 请求耗时 `0.003834 s`。这些是 loopback 短响应指标，不能外推真实供应商时延或公告吞吐。测量包含依赖安装。

覆盖显式 `spawn` 的多个评测协调器和生产工作池、多个评测 run 共用模型并发上限、跨进程缓存；文档读取和解析分别测得额度 1/2 时峰值 1/2。还覆盖暂停/恢复、worker 崩溃保留兄弟结果、telemetry 重启恢复、成功收据幂等、429/超时/截断/非法 JSON、SQL 输入在调用模型前被拒绝、并发 1 预测与原串行一致、final 闸门正反向检查。

bounded worktree 早期 Windows 记录为 `398 passed, 4 skipped`；最终冻结版本的定向套件为 `127 passed, 1 warning in 103.25s`。随后该功能选择性整合进主工作区，并以主工作区完整回归 `501 passed, 3 skipped, 2 warnings` 作为当前代码基线。之前一次冻结版全量回归的失败历史保留在原记录中，不替代当前主工作区结果。`git diff --check` 通过。

这轮 cgroup 数字包含合成测试和依赖安装，不包含真实公告解析、OCR、LibreOffice 转换、生产 Web 流量或真实服务商延迟；不足以外推实际部署吞吐和内存需求。默认 `DOCUMENT_WORKERS=1`、`MODEL_CONCURRENCY=2` 是保守起始设置，仍需按实际端点、附件和服务器负载再压测。
