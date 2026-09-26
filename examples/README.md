# 可复现演示材料

仓库内演示数据全部为**虚构数据**，只能用于软件流程演示，不能用于竞赛准确率、官方评分或真实采购决策。

- `demo_notice.html`：虚构公告正文，可与同名 `demo_notice.zip` 一起上传。
- `offline_demo` 数据集：由 `scripts/Start-Demo.ps1 -Offline` 从代码中的虚构固定夹具重建，展示三条采购项目、多个投标主体和五类查询。该路径不依赖模型 API、局域网和官方材料。
- `synthetic/`、`gold.reviewed.json` 等既有样例：只用于开发回归，不能冒充官方 gold。

联网准备依赖并配置好评审账号后，现场启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Demo.ps1 -Offline
```

页面就绪后切换至“离线演示样例（纯虚构）”数据集。关闭时执行 `scripts/Stop-Demo.ps1`。样例库与启动日志写入被 Git 忽略的 `backend/.data/`，不会修改默认业务库。
