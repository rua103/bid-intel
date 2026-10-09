# 有界并发生产后台任务

生产批处理复用 `app.jobs` 的 ProcessPoolExecutor 和公告级检查点。每个部署由一个文件锁协调器所有者串行管理持久任务状态；一个公告任务内的文档转换/解析、模型请求和等待队列分别限额。默认起步配置为 `JOB_WORKERS=2`、`DOCUMENT_WORKERS=1`、`MODEL_CONCURRENCY=2`、`MODEL_WAIT_QUEUE_CAPACITY=8`、`MODEL_WAIT_TIMEOUT_SECONDS=15`、`JOB_QUEUE_CAPACITY=8`。这些值不是容量承诺，应按服务器、文件类型、OCR 引擎和服务商额度压测后调整。

所有 Uvicorn worker、后台协调器和评测进程须在同一宿主机使用同一个 `JOB_RUNTIME_DIR`。未配置时使用数据库同目录的 `runtime`。进程间用文件锁租用文档、模型和队列槽位，进程退出会由操作系统释放租约。限制仅保证本地文件系统上的同机进程；不支持 NFS、多主机共享队列或分布式调度。服务运行期间改变额度会因 `runtime/limits.json` 冲突而拒绝启动，先停掉所有所有者，再配置新运行目录。

## 配置和启动

在 `backend/.env` 中配置实际服务地址和密钥，并分别设置下列限制。仓库只提供无密钥的模板：

```dotenv
JOB_WORKERS=2
DOCUMENT_WORKERS=1
MODEL_CONCURRENCY=2
MODEL_WAIT_QUEUE_CAPACITY=8
MODEL_WAIT_TIMEOUT_SECONDS=15
JOB_QUEUE_CAPACITY=8
JOB_RUNTIME_DIR=/var/lib/bid-intel/runtime
```

Ubuntu 启动示例（在已安装依赖的虚拟环境中）：

```bash
cd backend
export JOB_RUNTIME_DIR=/var/lib/bid-intel/runtime
export DOCUMENT_WORKERS=1 MODEL_CONCURRENCY=2 JOB_QUEUE_CAPACITY=8 JOB_WORKERS=2
export INTAKE_ROOT=/srv/bid-intel/intake
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

多 Web worker 可以改为 `--workers 2`，但每个 worker 必须使用相同运行目录和额度；后台队列由唯一 `jobs-coordinator.lock` owner 处理，模型额度由同目录槽位锁统一限制。多个批任务按队列顺序处理，单批内部按公告并发。此实现没有跨批任务的公平并发调度。

`JOB_WORKERS` 限制单个公告批次进程池的 worker 数；`DOCUMENT_WORKERS` 限制同部署中同时运行的附件展开、OCR、Office/PDF 转换和解析段；`MODEL_CONCURRENCY` 限制模型 HTTP 在途请求总数，包括交互查询、连接探测与评测；`MODEL_WAIT_QUEUE_CAPACITY` 限制等待模型槽位的请求数，`MODEL_WAIT_TIMEOUT_SECONDS` 限制每个请求等待模型槽位的最长时间；队列满或等待超时的交互查询返回 HTTP 429 且不发送模型请求。`JOB_QUEUE_CAPACITY` 限制等待运行的公告任务槽位和等待批任务接纳数。Web `health`、查询和状态 API 不会在事件循环内运行 CPU 解析；自然语言模型查询使用异步 HTTP I/O。

运行后端 API 时，用 `INTAKE_ROOT` 指向允许服务端处理的公告目录。后台任务通过现有 REST API 创建：

```http
POST /api/v1/jobs
X-Dataset-ID: <dataset-id>
Content-Type: application/json

{"source_directory":"/srv/bid-intel/intake/batch-01","mode":"hybrid","ocr":true}
```

队列达到上限时返回 HTTP 429。任务目录保存在数据库旁的 `jobs/<job-id>` 下，内含公告 manifest、冻结模型配置和密钥文件、公告进度与原子结果检查点。模型密钥以明文保存在本机 `model-secret.json`，不会写进状态、缓存键、日志、报告或 Git。POSIX 系统将文件权限设为 `0600`；Windows 会在写入前撤销继承 ACL，仅授权当前服务 SID、SYSTEM 和本机管理员。运行账户仍须保护数据库旁的任务目录和备份。

## 状态、暂停和恢复

```http
GET  /api/v1/jobs
GET  /api/v1/jobs/<job-id>
GET  /api/v1/jobs/<job-id>/report
POST /api/v1/jobs/<job-id>/pause
POST /api/v1/jobs/<job-id>/stop
POST /api/v1/jobs/<job-id>/resume
POST /api/v1/jobs/<job-id>/resume?retry_failed=true
```

暂停会停止继续调度；当前 HTTP 请求允许完成或超时，模型调用之间的缓存和已完成公告检查点会保留。恢复时已成功公告不会再次入库；中断的公告从已验证缓存恢复，或重做尚未完成的阶段。失败公告默认保持失败，用户修复额度、来源或配置问题后显式带 `retry_failed=true` 才重试。没有无限 429 自动重试、静默换模型或参数回退。SQLite `import_receipts` 以唯一任务/公告标识防止重复提交。

服务重启后，遗留的 `running` 状态可由状态 API 报为 `interrupted`，成功检查点仍在；对该任务调用 `/resume` 会重新排队。命令行运维也可用：

```bash
cd backend
python -m app.jobs status <job-id>
python -m app.jobs run /path/to/data/jobs/<job-id>
python -m app.jobs run /path/to/data/jobs/<job-id> --retry-failed
python -m app.jobs serve
```

命令行请求暂停/续跑示例（启用认证时使用已登录 session cookie）：

```bash
curl -X POST -H 'X-Dataset-ID: default' http://127.0.0.1:8000/api/v1/jobs/JOB_ID/pause
curl -X POST -H 'X-Dataset-ID: default' http://127.0.0.1:8000/api/v1/jobs/JOB_ID/stop
curl -X POST -H 'X-Dataset-ID: default' 'http://127.0.0.1:8000/api/v1/jobs/JOB_ID/resume?retry_failed=true'
```

`serve` 是短生命周期队列协调器：处理现有待办后，在空闲数秒时退出；API 创建/续跑请求会按需启动它。它也可从无存活 owner 的过期 `running` 任务状态重新排队。宿主机断电或进程崩溃时，若请求已发送但响应未记录，服务商可能已计费；任何客户端恢复策略都不能保证再次请求不重复计费。

源文件在任务创建时记录 SHA-256，处理前及入库前会再次核对；若处理期间文件变化，该公告失败且不会导入。该检查适用于避免意外并发修改，文件仍应由部署方作为只读输入管理。

本地解析缓存按输入字节、解析器版本、格式、选项和文档限制隔离；模型响应缓存按输入、抽取代码版本、任务模式、模型、端点、账户哈希、请求参数和抽取选项隔离。缓存按键加锁，先验证完整结构再命中，并用 fsync + 原子替换落盘；损坏结果作为 miss 重新计算。请求计量区分本轮真实 HTTP 请求、本地缓存恢复历史请求和服务商返回的 KV 缓存 token。服务商 usage 可能不提供。

模型适配只实现仓库使用的 OpenAI-compatible Chat Completions/SSE 形式和可配置 endpoint。适配层保留 Qwen 兼容网关的 `chat_template_kwargs` 与 DeepSeek OpenAI-compatible 的 `thinking` 参数差异，并要求收到 SSE `[DONE]`、收全尾部 usage、拒绝 `finish_reason=length` 的输出。离线契约测试使用 mock；这不构成真实端点验收，也没有实现 Anthropic/Gemini 原生协议。比赛允许的模型仍由业务层 Qwen/DeepSeek 限制决定。

初始并发值适用于小步部署和本地合成验证，不表示已经测出真实公告、OCR/Office 转换或真实供应商请求的容量。见 [有界并发离线验证](bounded-validation.md) 和 [三路线评测器](bounded-route-evaluation.md)。
