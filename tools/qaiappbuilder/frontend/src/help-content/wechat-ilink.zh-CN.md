# 微信通道：iLink Bot 如何工作

QAI ModelBuilder 通过 **iLink Bot**（第三方 WebSocket 代理）接入你的微信个人号——不直接调用微信 SDK。首次连接需要一次**扫码授权**，之后本地会存住授权凭证，重启后自动重连。

## 授权时序图

![iLink Bot 授权时序](/help-images/wechat-ilink/authorization-flow.svg)

1. 你在设置面板点击 **连接微信**。
2. QAI ModelBuilder 打开 iLink Bot 的授权页面 URL。
3. 你用微信扫码。
4. iLink Bot 弹出「是否允许绑定该机器人」的确认。
5. 你点击**确认**。
6. iLink Bot 通过 webhook 回调 QAI ModelBuilder，传回**长期会话 token**。
7. 面板状态切到「已连接」，此后收到的微信消息会被路由给你在设置里配置的模型。

## 为什么绕一层 iLink？

微信官方没有对个人号开放机器人 API。iLink Bot 提供了托管的 WebSocket 长连接，把私信 → 事件推给我们的服务端。**你的账号、聊天内容都不会经过 QAI 服务器**——iLink 是唯一的中转方。

## 常见问题

- **二维码过期**：默认 3 分钟。点击「刷新二维码」重来即可。
- **扫码后长时间没反应**：确认微信 → 设置 → 通用 → 后台常驻/未清缓存，然后重扫。
- **断线后自动重连失败**：多为 iLink 服务器临时不可达。查看面板的错误详情；若持续超过 5 分钟，去 iLink 官网确认服务状态。
- **想解绑**：点击面板的**断开连接**；本地凭证会立即失效。

## 官方参考

- iLink Bot 官网：<https://ilink.dev/>
