# 贡献指南

## 开始前

请先阅读 [`docs/ONBOARDING.md`](docs/ONBOARDING.md)、[`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md) 第零节和 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。不要把 API Key、评审密码、原始官方材料或 `.data` 目录提交到 Git。

## 提交改动

- 一个提交聚焦一个可验收问题，标题使用简短动词，例如 `fix:`、`feat:`、`docs:`。
- 解析、模型、查询或数据口径变化必须同时更新相关测试和 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。
- 评测变化必须说明数据集角色（tuning、targeted 或 holdout）、Gold 状态、notice ID 范围和限制。
- 不要把本地 Gold 写成官方 ground-truth，也不要把旧实验数字移植成当前结论。

## 提交前检查

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
cd ..\frontend
npm test
npm run build
cd ..
git diff --check
```

PR 或交接说明应包含问题、行为变化、验证命令、测试范围和已知限制。涉及模型请求时说明是否调用真实服务、请求数、失败数和成本风险。
