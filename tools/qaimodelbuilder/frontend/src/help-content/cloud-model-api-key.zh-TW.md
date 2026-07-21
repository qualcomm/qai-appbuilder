# 申請 API Key

內部版本已預置一批雲端模型的**接入設定**，但未內建 API Key——你需要自行申請並在本面板貼上。**API Key 僅保存在本機磁碟，不會上傳至任何伺服器。**

## 各廠商申請入口

- **OpenAI**：<https://platform.openai.com/api-keys>（登入後 → 「Create new secret key」）
- **Azure OpenAI**：Azure Portal → 你的 OpenAI 資源 → 「Keys and Endpoint」
- **DeepSeek**：<https://platform.deepseek.com/api_keys>
- **Moonshot**：<https://platform.moonshot.cn/console/api-keys>
- **智譜 GLM**：<https://open.bigmodel.cn/usercenter/apikeys>
- **阿里通義**：<https://dashscope.console.aliyun.com/apiKey>

## 一分鐘操作步驟

1. 開啟上述對應廠商的入口，登入或註冊帳號。
2. 在金鑰管理頁面按下「新增」或「建立」，**替金鑰命名一個可辨識用途**（例如 `qai-modelbuilder-dev`），未來吊銷更方便。
3. **立即複製金鑰**：多數廠商只在建立當下顯示完整值，之後僅顯示遮罩前綴。
4. 回到本面板，貼上「API Key」欄位並按下**儲存**。

## 安全提示

- **切勿**把金鑰提交進 Git 或貼到公開聊天／工單。
- 若金鑰外洩：立即在廠商主控台按下「吊銷」（Revoke），再重新建立。
- 建議為不同用途申請不同金鑰並設定用量上限，便於稽核與止損。

## 如何驗證金鑰

儲存後，在聊天中送一則測試訊息（或按下面板中的**測試連線**按鈕）。若模型正常回覆表示已生效；若回 `401 Unauthorized`，代表金鑰錯誤或權限不足。
