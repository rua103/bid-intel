# 评审方操作说明与账号配置

本文说明评审方如何登录、导入材料、查看五类关系查询，并在模型不可用时使用本地规则流程。演示机的用户名和密码必须由项目组单独配置，不能提交到公开仓库。

## 演示前配置账号

执行 `scripts/Configure-ReviewerAccount.ps1` 会生成强密码和签名密钥，将它们写入本机 `backend/.env`，并把评审账号保存在 Git 忽略的 `backend/.data/reviewer-credentials.txt`。该脚本不会把密码打印到终端。该账号文件只通过受控渠道交给评审方。

如果需要自行配置，在 `backend/.env` 中启用账号并设置凭据：

```dotenv
AUTH_ENABLED=true
AUTH_USERNAME=reviewer
AUTH_PASSWORD=<项目组设置的强密码>
AUTH_SECRET_KEY=<至少 32 字节随机值>
AUTH_SESSION_HOURS=8
AUTH_COOKIE_SECURE=false
```

可用 Python 生成两个随机值，再手动写入本机 `.env`：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

演示机使用 HTTP 时 `AUTH_COOKIE_SECURE=false`；部署到 HTTPS 后改为 `true`。配置完成后重启后端。用户名和密码通过比赛要求的受控渠道交给评审方；不要写进 Git、录屏或公开演示画面。会话 Cookie 为 HttpOnly、SameSite=Strict，默认 8 小时过期；账号未配置完整时，受保护 API 会 fail closed 并返回 503。

## 登录与演示步骤

1. 启动后端和前端，确认两者可从评审电脑访问；打开 `http://<演示机局域网地址>:5173`。
2. 输入项目组提供的评审账号。分析台与 `/annotation.html` 共用登录会话；结束演示时点击页面顶部“退出登录”。
3. 在数据导入区域选择公告 HTML 与同名附件 ZIP，或载入预先准备好的样例任务包。未配置或无法连接模型时，选规则抽取/本地 OCR 路径；模型配置不是规则流程的前置条件。
4. 先检查导入回执中的解析警告，再查看标的候选和采购单位/供应商关系；场景二、三、五应以已抽到的参与主体记录为前提。
5. 打开人工标注页时，对照原文逐公告核验，不把预测候选当成 Gold。运行评测前确认输入是独立核验的 Gold 和对应 predictions。

## 登录故障

- `503 登录未配置`：确认 `.env` 的 `AUTH_ENABLED=true` 且用户名、密码、签名密钥均非空，然后重启后端。
- 页面无法连接：先确认后端 `http://<演示机局域网地址>:8000/api/v1/health` 可访问，再检查防火墙和前后端地址。
- Cookie 登录后仍返回 401：确认前端和 API 使用相同主机名（端口可以不同），浏览器允许本地 Cookie；清除旧 Cookie 后重登。

## 账号边界

当前实现是单个评审账号的轻量会话认证，没有用户注册、角色分级或逐用户审计。它用于受控演示环境，不代表生产级身份管理；不要把演示机直接暴露到公网。
