# 飛書通道：如何工作

QAI ModelBuilder 透過飛書開放平台的**自建應用**接入飛書，把機器人事件（使用者訊息、@ 提及、群組訊息）路由到你在設定裡選的 AI 模型。**首次連線**需要在飛書開放平台建應用並把 4 項憑證填到本面板，之後應用憑證儲存在本地，重啟後自動重連。

## 連線流程圖

![飛書連線流程](/help-images/feishu-setup/connection-flow.svg)

1. 你在[飛書開放平台](https://open.feishu.cn/app)建立**企業自建應用**。
2. 在應用管理頁面記下 **App ID** 和 **App Secret**（後者只在建立時以明文顯示一次）。
3. 在**事件訂閱**裡選擇「WebSocket 長連線」模式並產生 **Encrypt Key** 與 **Verification Token**。
4. 把 4 項憑證填到本面板並點**儲存**。
5. 點**連線** → 應用透過 WebSocket 與飛書事件閘道建立長連線。
6. 面板狀態切到「已連線」，此後收到的飛書訊息會被路由給你在設定裡設定的模型。

## 為什麼用 WebSocket 長連線？

飛書官方開放平台既支援「回呼 HTTP webhook」（需要公網 IP）也支援「WebSocket 長連線」（不需要公網 IP）。QAI ModelBuilder 通常跑在開發者的**本機**上，沒有穩定公網 IP。WebSocket 模式由用戶端主動建立與飛書閘道的長連線，訊息透過這條連線推給你 ——**你無需暴露任何服務端點**。

## 常見問題

- **`Invalid App Secret`**：從開放平台複製 App Secret 時常帶隱藏空白。重複製一次，貼上前 Ctrl+A 清空目標框再貼上。
- **`Signature mismatch`**：Encrypt Key 和 Verification Token 位置填反了。回開放平台「事件訂閱 → 加密策略」核對兩個值分別對應哪個欄位。
- **連上但收不到訊息**：應用缺 `im:message`、`im:message:send_as_bot` 權限，或**應用版本未發布**。回開放平台加權限 → 建立應用版本 → 發布 → 面板中斷重連。
- **連線逾時**：企業內網需要走代理。展開本面板底部的**代理設定**，填 HTTPS 代理位址後重連。
- **想暫停機器人**：點面板的**中斷連線**；本地憑證保留，下次點連線即可恢復，不需重新設定。

## 官方參考

- 飛書開放平台：<https://open.feishu.cn/app>
- 事件訂閱開發文件：<https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/event-subscription-guide>
