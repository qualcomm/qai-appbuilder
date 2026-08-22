# 微信通道：iLink Bot 如何運作

QAI ModelBuilder 透過 **iLink Bot**（第三方 WebSocket 中繼）接入你的微信個人號——並未直接呼叫微信 SDK。首次連線需一次**掃碼授權**；之後憑證會保存在本機，重啟後自動重連。

## 授權時序圖

![iLink Bot 授權時序](/help-images/wechat-ilink/authorization-flow.svg)

1. 你在設定面板按下**連線微信**。
2. QAI ModelBuilder 開啟 iLink Bot 的授權頁面。
3. 你用微信掃描 QR code。
4. iLink Bot 顯示「是否允許綁定此機器人？」確認畫面。
5. 你按下**確認**。
6. iLink Bot 透過 webhook 回傳**長效工作階段 token** 給 QAI ModelBuilder。
7. 面板狀態切換為「已連線」，之後收到的微信訊息會被路由至你設定的模型。

## 為何要繞經 iLink？

微信官方未對個人號開放機器人 API。iLink Bot 提供了受管的 WebSocket 長連線，將私訊事件推送給我們的服務端。**你的帳號與聊天內容不會經過 QAI 伺服器**——iLink 是唯一的中轉方。

## 常見問題

- **QR 逾時**：預設 3 分鐘。按「重新整理 QR」再掃一次即可。
- **掃碼後沒反應**：確認微信 App 在前景執行且未被積極省電回收，然後重掃。
- **斷線後自動重連失敗**：多半為 iLink 伺服器暫時不可達。查看面板錯誤詳情；若超過 5 分鐘仍失敗，前往 iLink 官網確認服務狀態。
- **想解除綁定**：按下面板的**中斷連線**；本機憑證會立即失效。

## 官方參考

- iLink Bot 官網：<https://ilink.dev/>
