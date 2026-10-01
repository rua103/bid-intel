# 发布与提交检查清单

本清单用于源码压缩包、评审演示和最终比赛材料准备。它不替代主办方最新通知；正式提交前应逐条核对官方模板和截止时间。

## 代码与环境

- [ ] `git status` 中没有 `.env`、API Key、密码、`.data`、原始官方文件、`node_modules`、虚拟环境或构建缓存。
- [ ] `README.md`、`docs/DEVELOPMENT.md` 和 `docs/REVIEWER_GUIDE.md` 的启动命令一致。
- [ ] 记录 Python、Node、Docker、Neo4j、LibreOffice 和 OCR 版本。
- [ ] `backend/.env.example` 与 `frontend/.env.example` 只包含变量名和安全示例值。
- [ ] `git diff --check` 通过。

## 自动化验证

- [ ] 后端 pytest 通过，Ruff 通过。
- [ ] 前端 `npm test` 和 `npm run build` 通过。
- [ ] 至少完成一次离线启动、导入、五类查询、标注页和退出流程。
- [ ] 若提交 Neo4j 能力，完成 Docker 集成测试，并记录 SQLite 对照结果。
- [ ] 若使用局域网演示，完成 [`LAN_ACCEPTANCE.md`](LAN_ACCEPTANCE.md) 的第二台设备签收。

## 数据与评测

- [ ] 明确区分官方原始材料、团队调优 Gold、独立 holdout Gold 和预测结果。
- [ ] 不把 `predictions.canonical.json` 当真值；不把 24 条 tuning 指标或全量产出写成官方准确率。
- [ ] 代码、提示词和归一化规则冻结后，才生成 `gold.reviewed.json` 并运行 `stage=final`。
- [ ] final 报告覆盖三条路线的全部 holdout 公告，包含字段/记录/实体指标、包号集合对齐、重复候选、请求数、耗时、token 和附件/OCR告警。
- [ ] 报告写明官方不提供逐条 ground-truth，指标是团队本地验证。

## 比赛要求映射

源码包应包含或链接到：

- [ ] 任务一七字段和主体抽取说明、来源证据和可导出的结果数据表。
- [ ] 任务二数据模型、五类查询口径、SQLite/Neo4j 选择和金额汇总定义。
- [ ] 任务三功能说明、运行步骤、评审账号交付方式和离线兜底。
- [ ] 依赖、版本、关键代码注释、测试结果和已知限制。
- [ ] 系统设计文档、技术路线对比分析和数据质量分析；PPT/视频按当前队伍计划另行制作。

## 发布后核对

- [ ] 评审机重新创建干净数据集，确认不会混入开发数据。
- [ ] 模型服务不可用时，离线 rules 路径仍能展示导入、关系查询和人工核验。
- [ ] Neo4j 不可用时，API 显示 SQLite 回退而不是静默声称使用图数据库。
- [ ] 所有对外截图和文档去除姓名、学校、内网地址、API Key、密码和本机绝对路径。

仓库当前没有声明开源许可证。若需要公开发布源码，请先由项目负责人选择并添加明确的 `LICENSE` 文件；在此之前不要在 README 中暗示“可自由再分发”。
