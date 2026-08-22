# 飞书通道：如何工作

QAI ModelBuilder 通过飞书开放平台的**自建应用**接入飞书，把机器人事件（用户消息、@ 提及、群组消息）路由到你在设置里选的 AI 模型。**首次连接**需要在飞书开放平台建应用并把 4 项凭证填到本面板，之后应用凭证保存在本地，重启后自动重连。

## 连接流程图

![飞书连接流程](/help-images/feishu-setup/connection-flow.svg)

1. 你在[飞书开放平台](https://open.feishu.cn/app)创建**企业自建应用**。
2. 在应用管理页面记下 **App ID** 和 **App Secret**（后者只在创建时明文显示一次）。
3. 在**事件订阅**里选择「WebSocket 长连接」模式并生成 **Encrypt Key** 与 **Verification Token**。
4. 把 4 项凭证填到本面板并点**保存**。
5. 点**连接** → 应用通过 WebSocket 与飞书事件网关建立长连接。
6. 面板状态切到「已连接」，此后收到的飞书消息会被路由给你在设置里配置的模型。

## 为什么用 WebSocket 长连接？

飞书官方开放平台既支持"回调 HTTP webhook"（需要公网 IP）也支持"WebSocket 长连接"（不需要公网 IP）。QAI ModelBuilder 通常跑在开发者的**本地机器**上，没有稳定公网 IP。WebSocket 模式由客户端主动建立与飞书网关的长连接，消息通过这条连接推给你——**你无需暴露任何服务端点**。

## 常见问题

- **`Invalid App Secret`**：从开放平台复制 App Secret 时常带隐藏空格。重复制一次，粘贴前 Ctrl+A 清空目标框再粘贴。
- **`Signature mismatch`**：Encrypt Key 和 Verification Token 位置填反了。回开放平台「事件订阅 → 加密策略」核对两个值分别对应哪个字段。
- **连上但收不到消息**：应用缺 `im:message`、`im:message:send_as_bot` 权限，或**应用版本未发布**。回开放平台加权限 → 创建应用版本 → 发布 → 面板断开重连。
- **连接超时**：企业内网需要走代理。展开本面板底部的**代理设置**，填 HTTPS 代理地址后重连。
- **想暂停机器人**：点面板的**断开连接**；本地凭证保留，下次点连接即可恢复，不需重新配置。

## 官方参考

- 飞书开放平台：<https://open.feishu.cn/app>
- 事件订阅开发文档：<https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/event-subscription-guide>
