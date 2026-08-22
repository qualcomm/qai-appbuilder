# Cloud model onboarding

The Cloud Models panel connects any service that speaks the **OpenAI Chat Completions API**: official OpenAI, Azure OpenAI, DeepSeek, Moonshot, Qwen, Zhipu, or a local gateway (vLLM / Ollama's OpenAI-compatible endpoint).

## Required fields

- **Model ID**: local display name (e.g. `deepseek-v3`). Never leaves this device.
- **Provider**: vendor label — used for grouping and a shortcut icon.
- **Base URL**: the API endpoint *without* the `/chat/completions` path. Examples:
  - OpenAI: `https://api.openai.com/v1`
  - DeepSeek: `https://api.deepseek.com/v1`
  - Local Ollama: `http://127.0.0.1:11434/v1`
- **API Key**: the token you generated in the vendor console. Stored locally only.
- **API Model ID**: the exact `model` value sent in the request body. Usually the same as Model ID; for Azure, put the **deployment name** here.

## Common issues

- **HTTP 401** — API key invalid or expired. Regenerate in the vendor console.
- **HTTP 404 `The model … does not exist`** — Base URL is fine but the API Model ID is wrong.
- **Slow / timing out** — check your proxy (Downloads → Proxy) or switch to a same-region mirror.
- **Do I need `/v1`?** — most OpenAI-compatible endpoints require it. If the vendor doc explicitly says `/openai` or `/anthropic/v1`, follow the doc.

## References

- OpenAI API reference: <https://platform.openai.com/docs/api-reference>
- OpenAI Chat Completions spec: <https://platform.openai.com/docs/api-reference/chat>
