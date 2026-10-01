# P1-5 第二台设备局域网验收清单

状态：**主机侧诊断已准备；第二台设备实测待完成。** 本清单不会把 localhost、TestClient 或主机自测记作远程验收。

## 演示主机启动

首次准备评审账号时运行一次（重置时会更换密码）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Configure-ReviewerAccount.ps1
```

在仓库根目录分别打开两个 PowerShell 终端。终端 A：

```powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

终端 B：

```powershell
Set-Location .\frontend
$env:VITE_API_BASE = ''
$env:VITE_API_PORT = '8000'
npm run dev -- --host 0.0.0.0 --port 5173
```

回到仓库根目录运行主机诊断：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Test-LanDemo.ps1
```

从输出中选演示主机与评审电脑处于同一局域网的 IPv4 地址，将 `http://<演示主机IPv4>:5173` 发给评审方；API 地址应为 `http://<同一IPv4>:8000`。不要给第二台电脑使用 `localhost` 或 `127.0.0.1`。诊断脚本只读检查监听、主机本地健康接口、LAN-origin CORS 预检和现有防火墙规则，不会自动开放端口。若 Windows 防火墙需要规则，请在可信 Private 网络按组织策略允许 TCP 5173 和 8000，并将远程地址限定为 LocalSubnet。

## 第二台设备操作

- [ ] 用另一台电脑访问 `http://<演示主机IPv4>:5173`，确认分析台和登录页加载；同时直接访问 `http://<演示主机IPv4>:8000/api/v1/health`，应返回 `status: ok`。
- [ ] 使用受控渠道交付的账号登录。刷新页面（F5）后仍显示已登录；关闭并重新打开同一浏览器配置文件，在 8 小时有效期内再次确认会话仍在。
- [ ] 在浏览器开发者工具的 Application/Storage → Cookies 中检查 API 主机下的 `bid_intel_session`：应有有效期，HttpOnly、SameSite=Strict；HTTP 演示的 Secure 应关闭。登录 API 请求应携带该 Cookie。不要复制、截图或发送 Cookie 值。
- [ ] 点击“退出登录”，确认回到登录页；受保护 API 在退出后应返回 401，重新登录后恢复访问。
- [ ] 保持第二台设备页面打开，在主机终端 A 按 Ctrl+C 停止 API，再刷新第二台设备页面：登录门应清楚提示“无法连接后端 API。请检查演示主机、网络、防火墙端口和 CORS 配置。”恢复 API 后点“重试连接”或刷新。
- [ ] 记录客户端、访问的主机 IPv4、时间和以上结果；由队友或本人在另一台设备完成后填写验收结论。

临时排错防火墙规则（仅由管理员在可信 Private 网络按策略执行；脚本不会替你执行）：

```powershell
New-NetFirewallRule -DisplayName 'Bid Intel demo LAN' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5173,8000 -RemoteAddress LocalSubnet -Profile Private
```

结束服务：在终端 A、B 分别按 Ctrl+C。评审账号保存在 Git 忽略的 `backend/.data/reviewer-credentials.txt`，仅通过受控渠道交付；不要提交该文件或将服务暴露到公网。
