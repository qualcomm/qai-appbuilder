# Getting an API key

The internal edition ships with a **pre-configured list of cloud models** but no bundled API key — you supply your own, paste it here, and this panel stores it **locally only** (never uploaded to any server).

## Where to request one

- **OpenAI**: <https://platform.openai.com/api-keys> (sign in → "Create new secret key")
- **Azure OpenAI**: Azure Portal → your OpenAI resource → "Keys and Endpoint"
- **DeepSeek**: <https://platform.deepseek.com/api_keys>
- **Moonshot**: <https://platform.moonshot.cn/console/api-keys>
- **Zhipu GLM**: <https://open.bigmodel.cn/usercenter/apikeys>
- **Alibaba Qwen**: <https://dashscope.console.aliyun.com/apiKey>

## One-minute walkthrough

1. Open the relevant portal above and sign in / register.
2. Under the API key management page, click **Create** and **name the key** something recognisable (e.g. `qai-modelbuilder-dev`) — makes future revocation painless.
3. **Copy the key immediately.** Most vendors show the full value only at creation time; afterwards you only see the masked prefix.
4. Come back to this panel, paste it into the "API Key" field, and click **Save**.

## Security tips

- **Never** commit keys to Git or paste them into public chats / tickets.
- If a key leaks, revoke it in the vendor console right away, then create a fresh one.
- Prefer separate keys per purpose with usage caps — audit-friendly and limits blast radius.

## Verifying the key works

After saving, send a test message in chat (or use the panel's **Test connection** button, if present). If the model replies you're set. `401 Unauthorized` means the key is wrong or lacks permission.
