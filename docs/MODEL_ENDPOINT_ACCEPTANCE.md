# 模型端点配置与换端点验收

本文说明模型配置的来源、目前能确认的合规信息，以及切换 Qwen/DeepSeek 端点时的验收步骤。模型 API Key 和完整端点地址属于运行时配置，不要写进报告、截图、日志或 Git。

## 当前配置能够证明什么

截至 2026-10-08，DeepSeek 官方文档列出的 Chat Completions 模型包括 `deepseek-flash` 和 `deepseek-v4-pro`，官方 OpenAI 兼容 Base URL 为 `https://api.deepseek.com`。模型名及地址应以服务方当前文档为准；本机页面截图中的 `deepseek-flash` 与该模型 ID 一致。模型名本身不是发布方出具的版本/资源合规证明。

当前代码配置保存 `model_base_url`、`model_api_key` 和 `model_name`；`.env` 另含输入上限、输出预算、超时及推理开关。它不记录模型发布版本、参数规模、是否微调、资源提供方或资源授权证明。名称只做 `qwen` / `deepseek` 前缀筛选，不能据此认定赛题合规。仓库既有记录称此前实测端点来自校内网关；现有记录没有证明该资源就是命题方提供的标准模型资源。

因此，当前仍缺以下合规证据：模型正式名称及发布版本、参数规模、基座或微调状态、资源来源及其授权/赛事提供证明。收到命题方或服务提供方的正式说明后，应把这些信息与日期、经办人和证据位置登记在项目交付材料中；不要把凭据填入登记表。

## 参数及保存位置

| 设置 | 用途 | 当前默认/行为 |
|---|---|---|
| `MODEL_BASE_URL` | OpenAI 兼容 API 根地址，也可直接填 `/chat/completions` 地址 | 可通过网页模型配置面板或 `.env` 设置；网页保存值优先于 `.env` |
| `MODEL_NAME` | 服务端公布的精确模型 ID | 当前 DeepSeek 示例为 `deepseek-flash`；前缀校验不验证版本或来源 |
| `MODEL_API_KEY` | 服务端鉴权 | 网页配置写入本机忽略目录 `backend/.data/model_config.json`；不得提交或复制到文档 |
| `MODEL_MAX_CHARS` | 单次发送的公告文本上限 | `12000` 字符，超过部分不会发送 |
| `MODEL_MAX_OUTPUT_TOKENS` | 单次生成上限 | `4096` tokens |
| `MODEL_TIMEOUT_SECONDS` | SSE 两个数据块之间的最大空闲时间 | `120` 秒；不是整次请求总时限 |
| `MODEL_STREAM_TOTAL_SECONDS` | 整次 SSE 的墙钟上限 | `600` 秒 |
| `MODEL_DISABLE_THINKING` | 是否在请求中关闭推理 | 默认 `true`；DeepSeek 官方 Chat Completions 使用 `thinking: {"type":"disabled"}`，旧兼容网关使用 `chat_template_kwargs` |

抽取使用 `stream=true`、`stream_options.include_usage=true` 和 `response_format=json_object`。DeepSeek 官方 Chat Completions 支持这些字段，并以 `thinking: {"type":"disabled"}` 关闭推理；`chat_template_kwargs` 是旧网关兼容写法，不能发给 DeepSeek 官方端点。来源：[Chat Completions API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion)、[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)。端点若拒绝参数，代码不会移除参数后偷偷重试；会返回可操作 warning，并让后台公告保持失败、可显式重试。

## 换端点步骤

1. **先取得资源证明。** 向资源提供方确认精确模型 ID、正式发布版本、参数规模、基座/微调状态和资源来源。记录官方文档或赛事资源说明的位置；如果缺少任何一项，明确标为待确认，不能从模型名猜测。
2. **配置新地址与凭据。** 在网页“模型配置”面板填写 API 根地址、服务端公布的精确模型 ID 和 API Key。DeepSeek 官方 OpenAI 兼容 Base URL 为 `https://api.deepseek.com`，程序会补 `/chat/completions`。网页保存项会覆盖 `.env` 中的端点、模型名和 Key；空白 Key 更新会保留已保存 Key。确认页面只显示打码值，避免共享完整配置文件。
3. **运行模型配置探测。** 页面“测试连接”发送小型 JSON 模式请求，并在提示词中明确要求返回 JSON（DeepSeek 官方 JSON 模式的要求）；HTTP 200 只表示短请求成功返回，不能证明端点确实应用了每个可选参数。401/403 表示凭据或权限问题；429 表示限流/额度问题；400/422 表示端点拒绝连接探测，需结合端点错误信息检查模型名、JSON 模式和请求参数。这一步不代表 SSE、长输入或完整结构化输出已通过。
4. **处理推理开关兼容性。** DeepSeek 官方 Chat Completions 在关闭推理时使用 `thinking: {"type":"disabled"}`。项目会按 DeepSeek 模型名前缀发送该字段；不会发送旧网关的 `chat_template_kwargs`。其他端点使用的参数形式须以其文档及长样本验收为准，不要只为让连接测试变绿就无依据地关闭开关。
5. **运行本地 mock 回归。** 在 `backend` 目录执行：

   ```powershell
   .venv/Scripts/python.exe -m pytest tests/test_model_adapter.py tests/test_model_config.py tests/test_jobs.py -q
   ```

   用例覆盖 401、429、超时、非法 JSON、非空截断 JSON、`finish_reason=length` 和不接受 `chat_template_kwargs` 的 400。mock 不访问外部 API，也不代表端点实测。
6. **用获准的小样本验证生产链路。** 选择一条可用于端点验收的长公告，在独立空数据集用 `model` 和 `hybrid` 各跑一次。至少确认流式响应结束、`finish_reason` 不是 `length`、JSON 完整、输出记录非空、证据片段能在输入中定位，且任务没有失败 warning。样本应足以触及通常输入长度和输出形态；短 `ping` 不能代替长输入验证。不要为端点切换调用 1038 条官方数据。
7. **记录验收结果。** 保存日期、模型 ID、版本/参数/微调状态、资源来源证据位置、非敏感端点标识、开关和 token/timeout 参数、样本范围、配置探测结果、mock 测试结果及长样本结论。不要保存 API Key 或完整私有 URL。

## 故障与重试判据

模型传输、参数或响应解析失败时，抽取器返回空候选和以“模型抽取失败”开头的具体 warning；空响应也有独立 warning。后台任务在提交数据库记录和成功结果检查点前识别这些 warning，故失败公告不会以成功预测落库。公告检查点会记为失败，默认续跑会跳过；修复端点/配置后，通过界面失败重试，或在作业目录执行：

```powershell
.venv/Scripts/python.exe -m app.jobs run <job-directory> --retry-failed
```

warning 会区分鉴权错误、限流、流式超时、非法/截断 JSON 和 HTTP 参数拒绝，并按 DeepSeek 官方 `thinking` 或旧网关 `chat_template_kwargs` 提示兼容性。mock 测试不代表真实 API 可用。
