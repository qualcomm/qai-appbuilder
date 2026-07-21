# 申请 API Key

内部版本已经预置了一批云模型的**接入配置**，但没有内置 API Key —— 你需要自己申请，然后在这个面板里粘贴进去。**API Key 只保存在本机磁盘，不会上传到任何服务器。**

## 常见厂商申请入口

- **OpenAI**：<https://platform.openai.com/api-keys>（登录后 → "Create new secret key"）
- **Azure OpenAI**：Azure Portal → 你的 OpenAI 资源 → "Keys and Endpoint"
- **DeepSeek**：<https://platform.deepseek.com/api_keys>
- **Moonshot**：<https://platform.moonshot.cn/console/api-keys>
- **智谱 GLM**：<https://open.bigmodel.cn/usercenter/apikeys>
- **阿里通义**：<https://dashscope.console.aliyun.com/apiKey>

## 一分钟操作步骤

1. 打开上面对应厂商的入口，登录/注册账号。
2. 在密钥管理页面点击「新建」或「创建」按钮，**给密钥起个能识别用途的名字**（例如 `qai-modelbuilder-dev`），便于将来吊销。
3. **立即复制密钥**——出于安全，多数厂商只在创建那一刻完整显示，之后再进入只能看到掩码前几位。
4. 回到本面板，粘贴到「API Key」输入框，点击**保存**。

## 安全提示

- **不要**把密钥提交进 Git、贴到公开聊天/工单里。
- 如果不小心泄露：立刻回到厂商控制台点「吊销」（Revoke），再重新创建。
- 建议给不同用途申请不同密钥并按用量限额，方便审计与止损。

## 如何验证密钥已生效

保存后，在聊天窗口发送一条测试消息（或点击面板里的**测试连接**按钮）。若模型正常回复即已生效；若报 `401 Unauthorized`，密钥错误或有权限问题。
