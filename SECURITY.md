# 安全说明

## 不要提交的内容

- `backend/.env`、`frontend/.env`、API Key、Neo4j 密码、评审账号和签名密钥。
- `backend/.data/` 下的模型配置、评审凭据、原始官方材料、完整模型响应和本地 Gold 工作副本。
- 日志、截图或报告中的内网地址、Cookie、Authorization 头和个人信息。

仓库提供 `.env.example` 作为变量清单。评审账号脚本会把凭据写入 Git 忽略的 `backend/.data/reviewer-credentials.txt`，不要把该文件通过公开渠道发送。

## 运行建议

- 评审环境启用 `AUTH_ENABLED=true`，使用随机 `AUTH_SECRET_KEY` 和受控账号。
- 生产或共享网络不要使用示例 Neo4j 密码；修改 Compose 配置和 `.env` 后再启动。
- 只向可信模型端点发送必要的公告文本；模型响应和 token 遥测应保存在本地受控目录。
- 局域网演示只开放必要端口，按 [`LAN_ACCEPTANCE.md`](docs/LAN_ACCEPTANCE.md) 验证 CORS、Cookie 和防火墙。

## 报告问题

不要在公开 issue 中粘贴密钥、原始公告或完整响应。请先停止暴露服务、轮换相关凭据，并通过项目负责人约定的私密渠道提供复现步骤、受影响版本和最小脱敏日志。当前仓库未声明安全响应时限或漏洞赏金政策。
