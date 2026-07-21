# 雲端模型接入

雲端模型面板可讓你接入任何**相容 OpenAI Chat Completions API** 的服務：官方 OpenAI、Azure OpenAI、DeepSeek、Moonshot、通義千問、智譜，或本地閘道（vLLM / Ollama 的 OpenAI 相容端點）。

## 必填欄位

- **Model ID**：顯示名（例如 `deepseek-v3`）。僅在本機使用。
- **Provider**：廠商標籤，用於分組與快捷圖示。
- **Base URL**：API 端點（不含 `/chat/completions`）。範例：
  - OpenAI 官方：`https://api.openai.com/v1`
  - DeepSeek：`https://api.deepseek.com/v1`
  - 本地 Ollama：`http://127.0.0.1:11434/v1`
- **API Key**：從廠商主控台申請取得的金鑰。僅保存在本機，不會上傳。
- **API Model ID**：請求本體中實際傳送給廠商的 `model` 值。多數廠商與 Model ID 相同；Azure 需填入**部署名稱**。

## 常見問題

- **回應 401**：API Key 無效或過期，請重新產生。
- **回應 404 `The model … does not exist`**：Base URL 正確但 API Model ID 拼寫錯誤。
- **回應緩慢／逾時**：確認代理設定（下載中心 → 代理），或改用同區域的鏡像端點。
- **是否需要 `/v1`？** 多數 OpenAI 相容端點需要；廠商文件寫 `/openai` 或 `/anthropic/v1` 時，請以文件為準。

## 參考

- OpenAI API 文件：<https://platform.openai.com/docs/api-reference>
- OpenAI Chat Completions 規格：<https://platform.openai.com/docs/api-reference/chat>
