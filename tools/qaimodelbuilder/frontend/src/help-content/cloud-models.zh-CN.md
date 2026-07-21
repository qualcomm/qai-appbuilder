# 云模型接入

云模型面板可让你接入任何**兼容 OpenAI Chat Completions API** 的服务：官方 OpenAI、Azure OpenAI、DeepSeek、Moonshot、通义千问、智谱、以及本地网关（vLLM / Ollama 的 OpenAI 兼容端点）。

## 必填字段

- **Model ID**：显示名（例如 `deepseek-v3`）。仅在本机使用。
- **Provider**：厂商名，用于分组、快捷图标。
- **Base URL**：API 端点（不含 `/chat/completions` 那一段）。示例：
  - OpenAI 官方：`https://api.openai.com/v1`
  - DeepSeek：`https://api.deepseek.com/v1`
  - 本地 Ollama：`http://127.0.0.1:11434/v1`
- **API Key**：从厂商控制台申请到的密钥。密钥仅保存在本机，不会上传。
- **API Model ID**：请求体里真正发给厂商的 `model` 字段值。多数厂商用 model_id 相同，个别厂商（例如 Azure）要求填**部署名**。

## 常见问题

- **报 401**：API Key 无效或已过期，去厂商控制台重生成。
- **报 404 `The model … does not exist`**：Base URL 正确但 API Model ID 拼错。
- **返回速度慢/超时**：确认代理设置（下载中心 → 代理），或改成同一区域的镜像端点。
- **拿不准要不要 `/v1`**：多数 OpenAI 兼容端点都需要，若厂商文档写了 `/openai` / `/anthropic/v1`，照文档来。

## 参考

- OpenAI API 文档：<https://platform.openai.com/docs/api-reference>
- OpenAI 兼容端点规范：<https://platform.openai.com/docs/api-reference/chat>
