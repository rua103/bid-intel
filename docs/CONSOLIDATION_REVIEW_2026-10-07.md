# 2026-10-07 统一代码审查与提交验证

本轮在 `main` 当前工作树整合已有 Agent 成果，基于 `e250b48`；没有启动三路线最终评测，没有写入 Gold、官方原始材料或既有评测目录，也没有推送远端。

## 审查范围与修复

- 品牌与产品供应商：供应商列不映射为品牌，保留来源证据；修正模型保护规则误清空公司名前缀品牌的问题。
- 金额政策：使用 Decimal 容差、显式 `local_proxy` 配置、单位换算和显示舍入；报告 JSON/Markdown 可序列化。前端金额按字符串/BigInt 格式化，不用显示舍入改写原值。
- 受控查询 API/UI：五场景白名单、请求校验、澄清与手动回退；修复单主体请求 422、澄清状态丢失、共享场景来源公告范围及数据集切换后的迟到响应。
- 关系线索 API/UI：修复供应商筛选在配对前丢失共同投标对象、会话凭证和响应字段映射；明确展示项目/包去重口径与导入时间依据。
- 附件审计：叶文件哈希不再用压缩包哈希替代，成员存在不等于 HTML 引用；未知哈希显式标记，直接 ZIP 成员读取有 64 MiB 上限。完整台账默认输出到忽略目录。
- 资源验证：使用临时隔离库与 loopback 端口，真实查询公告证据详情；不把任务列表或硬编码资源值当作停止/恢复、资源限额验收。修复超时输出 JSON 序列化和 Windows 控制台编码问题。
- 共享文件与文档：核对 `main.py` 路由、`App.vue` 组件/会话接线，OpenAPI 包含两个受控查询端点及关系线索端点；README、GAP、ONBOARDING、DEVELOPMENT、CHANGELOG 和创新路线图同步当前状态，保留历史记录及其历史快照标记。

## 实际验证

宿主为 Windows，后端虚拟环境 Python 3.13.7。以下均在最终代码修复后执行：

| 工作目录 | 命令 | 结果 |
| --- | --- | --- |
| `backend` | `.\.venv\Scripts\python.exe -m pytest -q` | **339 passed, 1 skipped, 2 warnings**，112.61 秒 |
| `backend` | `.\.venv\Scripts\python.exe -m ruff check app tests` | **All checks passed** |
| `frontend` | `npm test` | **26 passed, 0 failed** |
| `frontend` | `npm run build` | **成功**，588 modules；保留大于 500 kB chunk 提示 |
| `backend` | `.\.venv\Scripts\python.exe -m ruff check scripts/attachment_audit.py ../scripts/resource_limited_validation.py` | **All checks passed** |

唯一跳过项为真实 Neo4j 集成测试：未设置 `BIDINTEL_TEST_NEO4J_URI` 和 `BIDINTEL_TEST_NEO4J_PASSWORD`。两个 warning 分别是 Starlette/httpx 弃用提示和合成 ZIP 重名测试的预期告警；没有绕过失败测试。Python 3.11 的 AST 语法检查覆盖 80 个后端/脚本文件，通过；这不等同于 Python 3.11 实机全套验证。

资源验证脚本修复后的实际宿主复验命令（从 `backend` 执行，保留旧报告）：

```powershell
.\.venv\Scripts\python.exe -c "import importlib.util,pathlib; spec=importlib.util.spec_from_file_location('validation',pathlib.Path('../scripts/resource_limited_validation.py')); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); module.OUT=pathlib.Path('.data/consolidation-resource-validation.json'); raise SystemExit(module.main())"
```

退出码 0；内含附件/任务回归 **50 passed**、Ruff、构建、离线样例及真实 HTTP 工作流均通过。五类查询都是 HTTP 200，公告详情 HTTP 200 且有来源定位和片段。该宿主运行没有强制 CPU/内存限制，不算 Ubuntu 24.04 / 2 vCPU / 4 GB 验收；此前容器失败保持历史记录。

## 提交范围与排除项

- 本轮代码、合成 fixture/测试、脚本和已审查文档属于提交范围；新增 API 均有测试。
- `docs/benchmarks/attachment-audit-ledger.json` **原位保留但排除提交**：14,882,192 字节，含完整官方附件来源成员路径及原始告警，未完成公开审查，且历史哈希/引用字段有已知缺陷。
- 提交 `docs/benchmarks/attachment-audit-summary.json`：仅 14,262 条历史台账的聚合计数、文件 SHA-256 和边界说明，没有来源正文、路径、告警、Gold 或模型响应。
- `.env`、账号配置、数据库、官方材料、Gold、模型响应、`backend/.data/` 运行记录及构建/依赖缓存均不提交；没有将个人目录或仓库外文件加入 Git。
- 初始和最终 diff 检查无空白错误；暂存区仅包含逐项确认的本轮文件。

## 已知限制与下一步评测

- 名称型包号仍要求明确连续标签证据，不能猜测；附件错误响应、解析失败、不支持格式和未知成员哈希仍需人工处理。
- 受控自然语言只做本地 mock/合成验证，真实模型准确率与真实端点尚未验收；日期/金额筛选会明确拒绝不支持的条件。主体目录最多 500 条，来源公告摘要最多 100 条。
- 关系线索直接使用 SQLite；时间范围是公告导入时间，不能当作公告发布时间。线索不是违法认定。
- 第二台设备 LAN、浏览器视觉、真实 Neo4j 本轮重验，以及 Ubuntu/资源限额验收尚未完成；前端存在大 chunk 提示。
- 本地 holdout 与 tuning 各 24 条，ID 分离；pilot-a 缺失，pilot-b 六条没有双人一致性裁决，且无法认证盲标。必须将结果标为团队本地 reviewed holdout，不能写成官方成绩或无偏独立标注研究。
- 对既有评测摘要只读核查确认：`rules 24/24`、`hybrid 23/24`、`model 22/24`，状态 `incomplete`。本轮修改了抽取/评测代码，旧运行不能直接生成当前代码的 final 结论。

**代码侧可以进入下一步三路线最终评测**：使用本次提交冻结代码和现有已确认提示/配置，按既有评测闸门重新运行或在代码身份检查允许时续跑，并完整记录上述 Gold 质量边界。本轮未执行这一步，不能提前发布路线排名或准确率结论。
